# -*- coding: utf-8 -*-
"""The four-stage detection pipeline.

1. ``lexicon`` — count the frequency of every word in the corpus.
2. ``detect``  — flag suspect word types (rare words close to frequent ones);
   missing-space candidates are verified against the corpus: the split is kept
   only if the same word sequence actually occurs *with* spaces elsewhere.
3. ``locate``  — find every occurrence of a flagged word, detect extra-space
   errors, and context-verify edit-distance-1 corrections: does the corrected
   word actually appear next to the same neighboring words elsewhere?
4. ``report``  — export ranked CSV files.

Everything is derived from the corpus itself — no external dictionaries, so
Aramaic, rabbinic Hebrew and abbreviations are handled naturally.
"""
import csv
import json
import math
import os
import pickle
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from multiprocessing import Pool

from .config import Config
from . import tanach
from .corpus import OTZARIA_DB, make_corpus
from .textsource import OtzariaDB, ReadStats, TextSourceError
from .normalize import (CONFUSABLE, FINALS, FROM_FINAL, PREFIX_LETTERS,
                        SUFFIX_LETTERS, TO_FINAL, is_abbrev, tokenize)

LEXICON_F = 'lexicon.pkl'
FLAGGED_F = 'flagged.pkl'
SPLITS_F = 'split_cands.pkl'
REPORT_DB_F = 'report.db'


class StageError(Exception):
    """A stage was run before the stage that produces its input.

    Carries a ready-to-print Hebrew message; the CLI prints it and exits 1
    instead of dumping a traceback, and the UI's scan log shows it as-is.
    """


# Hebrew name of the stage that produces each prerequisite, for the message.
_STAGE_OF = {
    LEXICON_F: ('lexicon', 'מילון'),
    FLAGGED_F: ('detect', 'איתור'),
    SPLITS_F: ('detect', 'איתור'),
    REPORT_DB_F: ('locate', 'מיקום'),
}


def _require(out_dir, filename, stage, check_coverage=True, allow_unread=0):
    """Fail cleanly when a prerequisite of `stage` is missing.

    First-run guard: every stage except `lexicon` consumes a file produced by
    an earlier stage. Without this the user gets a raw FileNotFoundError /
    "no such table" traceback in English — and, for the report.db cases, a
    0-byte report.db left behind by sqlite3.connect() that then blocks the UI
    from starting at all.

    The file is also refused when the latest run of the stage that produces
    it did not read its whole input (:func:`coverage_problem`), unless it was
    written partial under ``--allow-unread`` and this run's `allow_unread`
    accepts as many unread rows.
    """
    path = os.path.join(out_dir, filename)
    need_cmd, need_he = _STAGE_OF.get(filename, ('all', 'סריקה'))
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        problem = check_coverage and coverage_problem(out_dir, need_cmd,
                                                      allow_unread)
        if problem:
            raise PartialRead(problem)
        return path
    raise StageError(
        f'לא ניתן להריץ את שלב "{stage}": חסר הקובץ {filename}, '
        f'שנוצר בשלב "{need_he}".\n'
        f'יש להריץ קודם:  python -X utf8 -m magiah {need_cmd} '
        f'--out "{out_dir}"\n'
        f'או להריץ סריקה מלאה:  python -X utf8 -m magiah all '
        f'--out "{out_dir}"')

# ---------------------------------------------------------------------------
# worker plumbing
# ---------------------------------------------------------------------------
_W = {}


def _init(spec, cfg_dict, loads):
    """Pool initializer: rebuild the corpus adapter and load shared data."""
    _W.clear()
    _W['corpus'] = make_corpus(spec)
    _W['cfg'] = Config.from_dict(cfg_dict)
    for key, path in loads.items():
        with open(path, 'rb') as f:
            _W[key] = pickle.load(f)
    if 'split_cands' in _W:
        _W['seq'] = SeqCounter(parts for _, parts in _W.pop('split_cands'))
    if 'ctx_pairs' in _W:
        _W['ctx_first'] = {a for a, _ in _W['ctx_pairs']}


def _pool(spec, cfg, loads=None):
    return Pool(cfg.workers, initializer=_init,
                initargs=(spec, cfg.to_dict(), loads or {}))


def _chunk_stats_begin():
    """Fresh per-chunk read counters on the worker's corpus adapter."""
    _W['corpus'].stats = ReadStats()


def _chunk_stats_end():
    return _W['corpus'].stats.to_dict()


COVERAGE_F = 'coverage_{stage}.json'
# unread rows located per coverage record (ReadStats samples as many)
_MAX_REFS = 20
# the scan-panel field that sets Config.allow_unread (webui/hebrew.py)
ALLOW_UNREAD_UI = 'שורות לא קריאות מותרות'


class PartialRead(StageError):
    """Rows of the input could not be read; the stage output is partial."""


def _replace_atomically(path, write):
    """``write(tmp_path)``, then move it over `path` in one step — so a stage
    that fails midway never leaves a half-written file in place of the last
    good one."""
    tmp = path + '.tmp'
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dump_pickle(obj):
    def write(path):
        with open(path, 'wb') as f:
            pickle.dump(obj, f, protocol=4)
    return write


def unread_limit(cfg):
    """``Config.allow_unread`` as the non-negative count it must be."""
    try:
        return max(0, int(cfg.allow_unread or 0))
    except (TypeError, ValueError):
        return 0


def spec_db(spec):
    """The seforim.db a corpus spec reads, or None — where an unread row's
    line id can be turned into a book and a reference."""
    if spec.get('type') == 'hybrid':
        return spec.get('db') or OTZARIA_DB
    if spec.get('preset') == 'otzaria':
        return spec.get('path')
    return None


@dataclass
class _Policy:
    """A run's stance on unreadable rows.

    `limit` is how many it may skip (``Config.allow_unread``), `inherited`
    maps each upstream stage whose output it consumes, accepted partial, to
    that output's coverage record, and `db` is where to locate unread rows.
    """
    limit: int = 0
    inherited: dict = field(default_factory=dict)
    db: str = None


def _policy(spec, cfg, out_dir, upstream=None):
    """The policy of a stage run that consumes the output of `upstream`
    (already cleared by :func:`_require`)."""
    return _Policy(unread_limit(cfg),
                   accepted_gaps(out_dir, upstream) if upstream else {},
                   spec_db(spec))


def _line_id(unit):
    """The seforim.db line a unit names — ``'123'``, or ``'ver:<v>:123'`` for
    that line's row in an alternative version — or None (a file)."""
    if unit.startswith('ver:'):
        unit = unit.rsplit(':', 1)[-1]
    return int(unit) if unit.isdigit() else None


def _unread_refs(samples, db_path, prior=()):
    """Where unread rows are: ``[{unit, book, ref, error}]``, one per unit,
    at most ``_MAX_REFS``, the `samples` first and then the `prior` refs.

    A sample names a seforim.db row by its line id (see :func:`_line_id`),
    looked up in `db_path` for the book title and heRef, or a library/textdir
    book by its relative path. The lookup is best effort: locating a bad row
    must never turn its coverage record into a crash.
    """
    refs, seen = [], set()
    for unit, error in samples:
        unit = str(unit)
        if unit not in seen and len(refs) < _MAX_REFS:
            seen.add(unit)
            refs.append({'unit': unit, 'book': '', 'ref': '',
                         'error': str(error)})
    ids = [i for i in map(_line_id, (r['unit'] for r in refs))
           if i is not None]
    places = {}
    if ids and db_path:
        try:
            with OtzariaDB(db_path) as odb:
                places = odb.describe_lines(ids)
        except (TextSourceError, sqlite3.Error):
            pass
    for r in refs:
        lid = _line_id(r['unit'])
        if lid is not None:
            r['book'], r['ref'] = places.get(lid, ('', ''))
        else:
            r['book'] = os.path.splitext(
                r['unit'].replace('\\', '/').rsplit('/', 1)[-1])[0]
    for r in prior:
        if r.get('unit') not in seen and len(refs) < _MAX_REFS:
            seen.add(r.get('unit'))
            refs.append(dict(r))
    return refs


def _missing_rows(passes, inherited=None):
    """``(rows, units)``: how many distinct rows an output is missing, and
    the ids of those known by id.

    One bad row is met by every pass that reads it — `locate` reads the
    corpus, then the Bible books again for the Tanach index, then the corpus
    again for context — and by every stage before it; a user who accepted
    that one row must not be asked to accept three. Rows are therefore
    counted by id across `passes` (ReadStats) and the `inherited` records.
    The Tanach index also reads version rows no other pass reads, so the
    largest per-pass count would not do. Past ReadStats.UNITS_CAP ids the
    sum is used instead: it can only overcount, which stops a run that would
    have been allowed, never the reverse.
    """
    inherited = inherited or {}
    units, exact = set(), True
    for s in passes.values():
        exact = exact and s.units_complete()
        units |= s.unread_units
    for g in inherited.values():
        exact = exact and len(g['unread_units']) == g['unread_rows']
        units.update(g['unread_units'])
    if exact:
        return len(units), units
    return (sum(s.unread() for s in passes.values())
            + sum(g['unread_rows'] for g in inherited.values())), units


def _unread_kind(units):
    """What the unread `units` are, for wording: 'db' (database rows, by
    id), 'files' (text files, by path — an unreadable file is one unit),
    'mixed', or None when no unit is known."""
    kinds = {'db' if _line_id(str(u)) is not None else 'files'
             for u in units}
    if len(kinds) > 1:
        return 'mixed'
    return kinds.pop() if kinds else None


def gap_record(passes, inherited, limit, db_path):
    """What an output is missing, for its coverage record — None if nothing.

    `passes` maps each read of the input to its ReadStats; `inherited` is
    :attr:`_Policy.inherited`. Rows are counted once (:func:`_missing_rows`).
    """
    rows, units = _missing_rows(passes, inherited)
    if not rows:
        return None
    samples = [x for s in passes.values() for x in s.error_samples]
    prior = [r for g in inherited.values() for r in g['unread_refs']]
    rec = {'accepted': rows <= limit, 'allow_unread': limit,
           'unread_rows': rows,
           'unread_refs': _unread_refs(samples, db_path, prior),
           'unread_units': sorted(units)[:ReadStats.UNITS_CAP]}
    kind = _unread_kind(units)
    if kind:
        rec['unread_kind'] = kind
    if inherited:
        rec['inherited'] = {st: g['unread_rows']
                            for st, g in inherited.items()}
    return rec


def _counts(stats):
    """A pass's counts for its coverage record: the ids of its unread rows
    are left out — the record's ``unread_units`` holds them all, once."""
    d = stats.to_dict()
    d.pop('unread_units', None)
    return d


def _coverage_info(stage, stats, extra=None, passes=None, policy=None):
    """The coverage record of a stage run (see :func:`_write_coverage`).

    `passes` holds the stage's further reads of the input (Tanach index,
    context verification); the output is complete only if every pass was and
    no upstream output it consumed was partial. A complete record has exactly
    the keys it always had; a partial one adds those of :func:`gap_record`.
    """
    passes = passes or {}
    policy = policy or _Policy()
    unread = sum(s.unread() for s in (stats, *passes.values()))
    gaps = gap_record({'main': stats, **passes}, policy.inherited,
                      policy.limit, policy.db)
    info = {'stage': stage, 'complete': gaps is None, 'unread': unread,
            **_counts(stats), **(extra or {})}
    if passes:
        info['passes'] = {k: _counts(v) for k, v in passes.items()}
    info.update(gaps or {})
    return info


def _write_coverage(out_dir, info):
    """Persist what a stage actually read (the run's coverage evidence)."""
    stage = info['stage']
    path = os.path.join(out_dir, COVERAGE_F.format(stage=stage))

    def write(tmp):
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
    _replace_atomically(path, write)
    passes = {k: ReadStats.from_dict(v)
              for k, v in (info.get('passes') or {}).items()}
    print(f'[{stage}] coverage: lines={info["lines"]:,} '
          f'chars={info["chars"]:,} '
          f'decode_errors={info["decode_errors"]:,} '
          f'missing={info["missing"]:,} '
          f'version_lines_skipped={info["version_lines_skipped"]:,}'
          + ''.join(f' {k}.unread={v.unread():,}' for k, v in passes.items())
          + ('' if info['complete'] else
             f' unread_rows={info["unread_rows"]:,} '
             f'accepted={info["accepted"]} '
             f'allow_unread={info["allow_unread"]:,}'),
          flush=True)
    if info.get('accepted'):
        print(_accepted_warning(info, path), flush=True)
    return info


def _fail_if_partial(stage, stats, out_dir, extra=None, passes=None,
                     policy=None):
    """A pass with unreadable rows must never be reported as complete.

    Called BEFORE the stage writes its output: a read that missed more rows
    than the run accepts (by default: any) records its coverage file and
    stops, so the previous output stays untouched (and is then refused by
    its consumers — see :func:`coverage_problem`). Within the limit the stage
    goes on, and its coverage record marks the output partial.
    """
    passes = passes or {}
    policy = policy or _Policy()
    # the rows inherited count too: each part within the limit does not make
    # their union so — and an output is written only if its record says
    # accepted
    if _missing_rows({'main': stats, **passes},
                     policy.inherited)[0] <= policy.limit:
        return
    info = _write_coverage(out_dir, _coverage_info(stage, stats, extra,
                                                   passes, policy))
    path = os.path.join(out_dir, COVERAGE_F.format(stage=stage))
    rows, limit = info['unread_rows'], info['allow_unread']
    lines = [f'שלב "{_STAGE_HE.get(stage, stage)}" לא הצליח לקרוא '
             f'{rows:,} שורות מהקלט, ולכן התוצאה חלקית ואינה מוצגת '
             f'כהצלחה.']
    if policy.inherited:
        lines.append('(כולל שורות שחסרו כבר בתוצרים של השלבים שקדמו לו.)')
    if limit:
        lines.append(f'הריצה אישרה לדלג על {limit:,} שורות לכל היותר '
                     f'(‎--allow-unread {limit}‎).')
    lines += refs_lines(info['unread_refs'], rows)
    lines += [proceed_hint(rows, _all_cmd(out_dir)), f'פרטים: {path}']
    raise PartialRead('\n'.join(lines))


def ref_text(r):
    """One unread row, for a person: book, reference and its id or path."""
    where = ', '.join(x for x in (r.get('book'), r.get('ref')) if x)
    unit = str(r.get('unit', ''))
    if unit.isdigit():
        label = f'מזהה שורה {unit}'
    elif unit.startswith('ver:') and _line_id(unit) is not None:
        label = (f'מזהה שורה {_line_id(unit)}, בגרסה '
                 f'{unit.split(":")[1]}')
    else:
        label = unit
    return f'{where} ({label})' if where else label


def refs_lines(refs, rows, show=5):
    """Hebrew lines listing the first `show` unread rows."""
    if not refs:
        return []
    out = ['שורות שלא נקראו:']
    out += ['    ' + ref_text(r) for r in refs[:show]]
    if rows > min(show, len(refs)):
        out.append(f'    ועוד {rows - min(show, len(refs)):,}')
    return out


def proceed_hint(rows, cmd=None):
    """How to go on despite `rows` unreadable rows (Hebrew): `cmd` again with
    ``--allow-unread`` — or, without `cmd`, the same scan again with it."""
    how = (f'\n    {cmd} --allow-unread {rows}' if cmd else
           f' יש להריץ את אותה סריקה שוב עם ‎--allow-unread {rows}‎')
    return (f'אם אי אפשר לתקן את מקור הנתונים, אפשר להמשיך בלי השורות האלה, '
            f'והתוצאה תסומן כחלקית:{how}\n'
            f'(בממשק: השדה "{ALLOW_UNREAD_UI}" בהגדרות המתקדמות של הסריקה)')


def _all_cmd(out_dir):
    """The full-scan command, as the messages suggest it."""
    return f'python -X utf8 -m magiah all --out "{out_dir}"'


def _accepted_warning(info, path):
    """The Hebrew notice printed when a stage wrote an output partial."""
    stage = info['stage']
    rows = info['unread_rows']
    lines = [f'[{stage}] אזהרה: התוצאה חלקית — {rows:,} שורות מהקלט לא '
             f'נקראו ואינן כלולות בה. הדבר אושר במפורש '
             f'(‎--allow-unread {info["allow_unread"]}‎), והתוצאה מסומנת '
             f'כחלקית בקובץ: {path}']
    if info.get('inherited'):
        names = [f'"{_STAGE_HE.get(s, s)}"' for s in info['inherited']]
        lines.append('התוצאה נשענת על תוצרים חלקיים של '
                     + ('שלב ' if len(names) == 1 else 'השלבים ')
                     + ', '.join(names) + '.')
    return '\n'.join(lines + refs_lines(info['unread_refs'], rows))


_STAGE_HE = {cmd: he for cmd, he in _STAGE_OF.values()}
# the stages that read the corpus, in order; each one's output is derived from
# the outputs of the ones before it
_READ_STAGES = ('lexicon', 'detect', 'locate')


def _chain(stage):
    """`stage` and the reading stages its output is derived from."""
    upto = _READ_STAGES.index(stage) + 1 if stage in _READ_STAGES else 0
    return _READ_STAGES[:upto] or (stage,)


def coverage_problem(out_dir, stage, allow_unread=0):
    """Why the output of `stage` must not be used — or None if it may.

    The rule: ``coverage_<stage>.json`` describes the stage's LATEST attempt,
    and an output is used only if the latest attempt of its stage, and of
    every stage before it, read the whole input. A partial attempt never
    replaces the output, so an older complete output may still be on disk
    after a failed run; it is refused all the same — it does not reflect the
    input the user last scanned, and using it silently would hide the
    failure. No coverage file at all (output written before coverage was
    recorded) is accepted.

    One exception, and only on request: an output written partial under
    ``--allow-unread`` is used by a run whose own `allow_unread` covers as
    many rows. A run that does not say so refuses it like any partial one —
    accepting missing rows is stated by each run, never inherited from the
    run that wrote the output.
    """
    for st in _chain(stage):
        state, rows, info = _latest_coverage(out_dir, st)
        path = os.path.join(out_dir, COVERAGE_F.format(stage=st))
        he = _STAGE_HE.get(st, st)
        if state == 'partial':
            return _partial_problem(out_dir, st, rows)
        if state == 'accepted' and rows > allow_unread:
            ran = ('ריצה זו לא אישרה דילוג על שורות שלא נקראו'
                   if not allow_unread else
                   f'ריצה זו אישרה רק {allow_unread:,}')
            return (f'התוצרים של שלב "{he}" חלקיים: {rows:,} שורות מהקלט '
                    f'לא נקראו, והדבר אושר במפורש בריצה שיצרה אותם '
                    f'(‎--allow-unread {info["allow_unread"]}‎). {ran}, '
                    f'ולכן היא לא תשתמש בתוצרים האלה.\n'
                    f'כדי להמשיך על בסיס התוצרים החלקיים יש להוסיף לפקודה '
                    f'‎--allow-unread {rows}‎ (בממשק: השדה '
                    f'"{ALLOW_UNREAD_UI}" בהגדרות המתקדמות של הסריקה).\n'
                    f'לתוצאה מלאה יש להריץ סריקה מלאה על מקור נתונים '
                    f'תקין:  {_all_cmd(out_dir)}\n'
                    f'פרטים: {path}')
    return None


def _partial_problem(out_dir, stage, rows):
    """The refusal of an output whose latest attempt read partially and was
    not accepted (see :func:`coverage_problem`)."""
    path = os.path.join(out_dir, COVERAGE_F.format(stage=stage))
    msg = (f'הריצה האחרונה של שלב "{_STAGE_HE.get(stage, stage)}" לא קראה '
           f'את כל הקלט ({rows:,} שורות לא נקראו), ולכן אין להשתמש בתוצרים '
           f'שלו ושל השלבים שאחריו.\n'
           f'יש לתקן את הבעיה ולהריץ שוב:  {_all_cmd(out_dir)}\n')
    if rows:
        msg += proceed_hint(rows, _all_cmd(out_dir)) + '\n'
    return msg + f'פרטים: {path}'


def failed_coverage(out_dir, stage):
    """Why NO run may use the output of `stage` — the latest attempt of a
    stage in its chain read partially and stopped — or None.

    Unlike :func:`coverage_problem` this holds no run's limit: an output
    written partial under ``--allow-unread`` is the result of a run that
    succeeded, accepted with gaps, and a run that states as many rows may
    use it. Only a failed read makes the output out of date.
    """
    for st in _chain(stage):
        state, rows, _ = _latest_coverage(out_dir, st)
        if state == 'partial':
            return _partial_problem(out_dir, st, rows)
    return None


def accepted_gaps(out_dir, stage):
    """``{stage: coverage record}`` of the accepted-partial outputs that the
    output of `stage` rests on (itself included). Policy is not checked
    here — that is :func:`coverage_problem`'s job."""
    out = {}
    for st in _chain(stage):
        state, _, info = _latest_coverage(out_dir, st)
        if state == 'accepted':
            out[st] = info
    return out


def _latest_coverage(out_dir, stage):
    """``(state, rows, info)`` of the latest attempt of `stage`.

    `state` is 'complete' (also: no record — output from before coverage was
    recorded), 'accepted' (partial, written under ``--allow-unread``; `info`
    then carries a validated ``unread_rows`` and ``unread_refs``) or
    'partial' (refused — also a record that cannot be read: unreadable
    evidence is no evidence). `rows` counts the rows not read.
    """
    path = os.path.join(out_dir, COVERAGE_F.format(stage=stage))
    if not os.path.exists(path):
        return 'complete', 0, None
    try:
        with open(path, encoding='utf-8') as f:
            info = json.load(f)
        if info.get('complete') is True:
            return 'complete', 0, info
        # records written before unread_rows existed only have the sum
        rows = int(info.get('unread_rows',
                            info.get('unread', info.get('decode_errors', 0))))
        refs = info.get('unread_refs')
        limit = info.get('allow_unread')
        # accepted only within the limit it was accepted under
        if (info.get('accepted') is True and 'unread_rows' in info
                and isinstance(limit, int) and 0 < rows <= limit
                and isinstance(refs, list)):
            units = info.get('unread_units')
            info['unread_rows'] = rows
            info['unread_refs'] = [dict(r, unit=str(r.get('unit', '')))
                                   for r in refs if isinstance(r, dict)]
            # ids are only an aid to counting: without them, rows add up
            info['unread_units'] = ([str(u) for u in units]
                                    if isinstance(units, list) else [])
            return 'accepted', rows, info
    except (OSError, ValueError, TypeError, AttributeError):
        rows = 0
    return 'partial', max(rows, 0), None


# ---------------------------------------------------------------------------
# stage 1: lexicon
# ---------------------------------------------------------------------------

def _count_chunk(chunk):
    _chunk_stats_begin()
    c = Counter()
    for _, text in _W['corpus'].iter_texts(chunk):
        c.update(tokenize(text))
    return c, _chunk_stats_end()


def build_lexicon(spec, cfg, out_dir):
    t0 = time.time()
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)
    lex = Counter()
    stats = ReadStats()
    with _pool(spec, cfg) as pool:
        for i, (c, st) in enumerate(
                pool.imap_unordered(_count_chunk, chunks), 1):
            lex.update(c)
            stats.add(ReadStats.from_dict(st))
            print(f'  [lexicon] chunk {i}/{len(chunks)}  types={len(lex):,}  '
                  f'({time.time()-t0:.0f}s)', flush=True)
    print(f'[lexicon] tokens={sum(lex.values()):,}  types={len(lex):,}  '
          f'time={time.time()-t0:.0f}s', flush=True)
    extra = {'tokens': sum(lex.values()), 'types': len(lex),
             'chunks': len(chunks)}
    policy = _policy(spec, cfg, out_dir)
    # coverage first: a partial lexicon must not replace the last good one
    _fail_if_partial('lexicon', stats, out_dir, extra, policy=policy)
    _replace_atomically(os.path.join(out_dir, LEXICON_F),
                        _dump_pickle(dict(lex)))
    _write_coverage(out_dir, _coverage_info('lexicon', stats, extra,
                                            policy=policy))


# ---------------------------------------------------------------------------
# stage 2: type-level detection
# ---------------------------------------------------------------------------

def _dld1(a, b):
    """Damerau-Levenshtein distance-1 check.
    Returns (kind, ch_a, ch_b, pos) or None; pos is the edit position in the
    longer string (for sub/swap: in either)."""
    la, lb = len(a), len(b)
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            i = diff[0]
            return ('sub', a[i], b[i], i)
        if (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]):
            return ('swap', a[diff[0]], a[diff[1]], diff[0])
        return None
    if abs(la - lb) != 1:
        return None
    kind = 'ins' if la > lb else 'del'   # relative to a: extra / missing letter
    long, short = (a, b) if la > lb else (b, a)
    for i in range(len(short)):
        if long[i] != short[i]:
            return (kind, long[i], None, i) if long[i + 1:] == short[i:] else None
    return (kind, long[-1], None, len(long) - 1)


# Possessive/plural suffixes, longest first. A rare word that is a frequent
# stem plus one of these is legitimate inflection (שמחזקם = שמחזק+ם), not a
# typo.
_SUFFIXES = ('כם', 'כן', 'הם', 'הן', 'נו', 'יו', 'יה', 'ות', 'ים', 'ין',
             'ם', 'ן', 'ו', 'ה', 'י', 'ך')


def _frequent_stem(w, freq, thresh):
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            stem = w[:-len(suf)]
            if freq.get(stem, 0) >= thresh:
                return True
            if stem[-1] in TO_FINAL and \
                    freq.get(stem[:-1] + TO_FINAL[stem[-1]], 0) >= thresh:
                return True
    return False


# ---------------------------------------------------------------------------
# shared per-word rules: core.detect and book_scan call exactly these, so the
# two paths cannot drift. Only the edit-1 candidate SOURCE differs (a deletion
# index for the whole lexicon vs direct probing for one book); both feed the
# same evaluation and are tested to give identical results.
# ---------------------------------------------------------------------------

HEB_LETTERS = 'אבגדהוזחטיכלמנסעפצקרשתךםןףץ'

# error types whose suggestion is a single word the book itself may also use
LOCAL_TYPES = ('edit1_sub', 'edit1_ins', 'edit1_del', 'edit1_swap',
               'nonfinal_end', 'spelling_variant', 'lost_quotes')

CALIBRATION_META_F = 'calibration_meta.json'
LEARNED_F = 'confusion_learned.json'
OCR_PROFILES_F = 'ocr_profiles.pkl'
SPLIT_ALTS_F = 'split_alts.pkl'

# What actually supports a finding (report column `evidence`). Book-scope
# kinds are deliberately distinct from corpus-wide ones.
EVIDENCE_KINDS = ('tanach', 'context', 'context_book', 'book_local',
                  'split_observed', 'split_observed_book', 'structural',
                  'ocr_profile', 'suspicion', 'none')
STRUCTURAL_TYPES = ('nonfinal_end', 'final_midword', 'lost_quotes')


class Lexicon:
    """The global lexicon plus the derived lookups every rule needs."""

    def __init__(self, freq, cfg, learned=None):
        self.freq, self.cfg = freq, cfg
        self.learned = learned or {}
        self.N = sum(freq.values())
        # abbreviations that lost their gershayim: רמבם -> רמב"ם
        self.abbrev_map = {}
        for a, fa in freq.items():
            if fa >= cfg.common_min and is_abbrev(a):
                k = a.replace('"', '').replace("'", '')
                if len(k) >= 2 and (k not in self.abbrev_map
                                    or freq[self.abbrev_map[k]] < fa):
                    self.abbrev_map[k] = a

    def is_common(self, w):
        return (self.freq.get(w, 0) >= self.cfg.common_min
                and not is_abbrev(w) and 2 <= len(w) <= 18)

    def expected(self, parts):
        """Expected spaced count of `parts` if the words were independent."""
        N = self.N or 1
        return N * math.prod(self.freq.get(p, 0) / N for p in parts)


class Edit1Index:
    """Edit-1 candidates via a SymSpell deletion index over the common
    words: built once, then queried for every rare type of the lexicon."""

    def __init__(self, lex):
        self.lex = lex
        self.del_index = {}
        for w in lex.freq:
            if lex.is_common(w):
                for i in range(len(w)):
                    self.del_index.setdefault(w[:i] + w[i + 1:], []).append(w)

    def __call__(self, w):
        cands = set(self.del_index.get(w, ()))
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            cands.update(self.del_index.get(d, ()))
            if self.lex.is_common(d):
                cands.add(d)
        return cands


def edit1_probe(lex):
    """Edit-1 candidates by probing the lexicon directly — no index; cheaper
    when only a single book's types are asked about."""
    is_common = lex.is_common

    def cands(w):
        out = set()
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            if is_common(d):
                out.add(d)
            for ch in HEB_LETTERS:
                if ch != w[i]:
                    sub = w[:i] + ch + w[i + 1:]
                    if is_common(sub):
                        out.add(sub)
                ins = w[:i] + ch + w[i:]
                if is_common(ins):
                    out.add(ins)
            if i + 1 < len(w) and w[i] != w[i + 1]:
                sw = w[:i] + w[i + 1] + w[i] + w[i + 2:]
                if is_common(sw):
                    out.add(sw)
        for ch in HEB_LETTERS:
            if is_common(w + ch):
                out.add(w + ch)
        return out
    return cands


def segmentations(w, freq, cfg):
    """Plausible missing-space splits of `w`, each still to be verified.

    Every 2-part split whose parts are frequent, plus the best few 3-part
    ones. A single "best" split chosen by part frequency alone misses the
    real one when a shorter, more frequent prefix exists (בכ|למקום vs
    בכל|מקום), so the choice is left to the spaced-sequence evidence.
    """
    n, mp = len(w), cfg.max_part

    def ok(p):
        return 2 <= len(p) <= mp and freq.get(p, 0) >= cfg.part_min

    out = [(w[:i], w[i:]) for i in range(2, n - 1)
           if ok(w[:i]) and ok(w[i:])]
    if cfg.max_parts >= 3 and n <= 3 * mp:
        three = []
        for i in range(2, n - 3):
            if not ok(w[:i]):
                continue
            for j in range(i + 2, n - 1):
                if ok(w[i:j]) and ok(w[j:]):
                    three.append((w[:i], w[i:j], w[j:]))
        three.sort(key=lambda p: (-min(freq[x] for x in p), p))
        out.extend(three[:cfg.split_alts_3])
    return out


def learned_bonus(count):
    """Score bonus of a substitution learned `count` times by calibration."""
    return min(2.5, 0.5 + 0.8 * math.log10(1 + count))


def _frequency_penalty(fw, cfg):
    """Structural rules also run above the rarity cutoff; a more frequent
    spelling is less likely to be a typo, so it ranks lower."""
    rm = max(cfg.rare_max, 1)
    return math.log10(fw / rm) if fw > rm else 0.0


def evaluate_word(w, fw, lex, edit1_cands):
    """Every per-word rule for one word type.

    Returns ``(records, splits)``: records are ``(errtype, suggestion,
    sugg_freq, score)``; splits are ``(parts, strong)`` candidates that still
    need spaced-sequence evidence. `fw` may be 0 (a book word unknown to the
    lexicon), so ratios use ``max(fw, 1)``.
    """
    cfg, freq = lex.cfg, lex.freq
    recs, splits = [], []
    # geresh/gershayim mark abbreviations, which follow their own spelling
    if len(w) < 3 or is_abbrev(w):
        return recs, splits
    fw1 = max(fw, 1)

    # structural: non-final letter at word end
    if w[-1] in TO_FINAL and fw <= cfg.nonfinal_max:
        wf = w[:-1] + TO_FINAL[w[-1]]
        ff = freq.get(wf, 0)
        if ff >= cfg.part_min and ff >= 50 * fw1:
            recs.append(('nonfinal_end', wf, ff, 4 + math.log10(ff / fw1)))

    # structural: final-form letter mid-word — checked above the rarity
    # cutoff too (up to struct_max); a frequent form is an established one
    mid_final = [i for i, ch in enumerate(w[:-1]) if ch in FINALS]
    if mid_final and fw <= max(cfg.struct_max, cfg.rare_max):
        for i in mid_final:              # perhaps a missing space there
            a, b = w[:i + 1], w[i + 1:]
            if freq.get(a, 0) >= 10 and freq.get(b, 0) >= 10 and len(b) >= 2:
                splits.append(((a, b), True))
        if not splits:                   # perhaps final form for regular
            w2 = ''.join(FROM_FINAL.get(ch, ch) if i < len(w) - 1 else ch
                         for i, ch in enumerate(w))
            f2 = freq.get(w2, 0)
            pen = _frequency_penalty(fw, cfg)
            if f2 >= cfg.common_min:
                recs.append(('final_midword', w2, f2,
                             5 + math.log10(f2) - pen))
            else:
                # a suspicion only: no corpus word to propose
                recs.append(('final_midword', '', 0, 3.0 - pen))

    if fw > cfg.rare_max:
        return recs, splits

    # missing space — every plausible segmentation, verified later
    if len(w) >= 5 and not splits:
        for parts in segmentations(w, freq, cfg):
            if lex.expected(parts) >= cfg.exp_prefilter:
                splits.append((parts, False))

    # abbreviation that lost its gershayim (רמבם -> רמב"ם)
    ab = lex.abbrev_map.get(w)
    if ab and freq[ab] >= cfg.ed1_ratio * fw1:
        recs.append(('lost_quotes', ab, freq[ab],
                     5 + math.log10(freq[ab] / fw1)))

    # frequent stem + inflection suffix: legitimate morphology
    if _frequent_stem(w, freq, cfg.common_min):
        return recs, splits

    # edit distance 1 from a frequent word (sorted: ties break the same way
    # whichever candidate source produced the set)
    best_c, best_score, best_kind, best_ch = None, 0.0, '', ''
    for c in sorted(edit1_cands(w)):
        if c == w or not lex.is_common(c):
            continue
        fc = freq[c]
        if fc < cfg.ed1_ratio * fw1:
            continue
        r = _dld1(w, c)
        if r is None:
            continue
        kind, ch_a, ch_b, pos = r
        if kind == 'ins':
            # a doubled letter (למללך, נגמרר) is always suspicious
            doubled = ((pos > 0 and w[pos - 1] == ch_a)
                       or (pos + 1 < len(w) and w[pos + 1] == ch_a))
            # extra letter in the leading prefix cluster (דלאליעזר) or a
            # trailing inflection suffix (מזדעזעה): morphology, not a typo
            if (not doubled and pos <= 2 and ch_a in PREFIX_LETTERS
                    and all(x in PREFIX_LETTERS for x in w[:pos])):
                continue
            if not doubled and pos == len(w) - 1 and ch_a in SUFFIX_LETTERS:
                continue
        elif kind == 'sub' and pos == 0 \
                and ch_a in PREFIX_LETTERS and ch_b in PREFIX_LETTERS:
            # Hebrew vs Aramaic prefix variation (אידועים = אַ+ידועים)
            continue
        score = math.log10(fc / fw1)
        if kind == 'sub':
            lc = lex.learned.get(ch_a + ch_b, 0)
            if lc:
                score += learned_bonus(lc)
            elif not lex.learned and (ch_a, ch_b) in CONFUSABLE:
                score += 2
        elif kind in ('ins', 'del') and ch_a in 'וי':
            score += 1
        elif kind == 'swap':
            score += 1
        if score > best_score:
            best_c, best_score, best_kind, best_ch = c, score, kind, ch_a
    if best_c:
        # extra/missing ו or י is ktiv male/chaser variation: own class
        if best_kind in ('ins', 'del') and best_ch in 'וי':
            errtype = 'spelling_variant'
        else:
            errtype = f'edit1_{best_kind}'
        recs.append((errtype, best_c, freq[best_c], best_score))
    return recs, splits


def record_best(errors, w, fw, rec):
    """Keep the highest-scoring explanation per word."""
    errtype, sugg, fs, score = rec
    cur = errors.get(w)
    if cur is None or score > cur[4]:
        errors[w] = (fw, errtype, sugg, fs, score)


class SeqCounter:
    """Counts CONTIGUOUS occurrences of word sequences (split candidates)
    in token lists; shared by the corpus pass and the book scan."""

    def __init__(self, seqs):
        self.seqs = list(seqs)
        self.first, self.pairs = set(), {}
        for idx, parts in enumerate(self.seqs):
            self.first.add(parts[0])
            self.pairs.setdefault((parts[0], parts[1]), []).append(idx)

    def count(self, toks, counts):
        first, pairs, seqs = self.first, self.pairs, self.seqs
        n = len(toks)
        for i in range(n - 1):
            if toks[i] in first:
                for idx in pairs.get((toks[i], toks[i + 1]), ()):
                    parts = seqs[idx]
                    if i + len(parts) <= n and all(
                            toks[i + k] == parts[k]
                            for k in range(2, len(parts))):
                        counts[idx] = counts.get(idx, 0) + 1


def _split_score(strong, obs, exp, minlen, cfg, scope):
    """Score of a split given its spaced evidence, or None if rejected.

    'corpus': the full pipeline's rule. 'book': only one book was searched,
    so an observation there is capped below the corpus band, and an
    unobserved split may stand on part frequencies alone (low score).

    Chance (`exp`) gates only splits with a 2-letter part. A split into two
    longer words is accepted on `split_obs_min` spaced observations even
    when the pair is about as common as chance predicts: two frequent words
    glued together (אבלנראה, והנהכתב) are exactly such a pair, and the glued
    form itself is rare. Association only ranks the accepted alternatives
    (:func:`resolve_splits`) and decides whether a split may displace a
    correction (:func:`settle_split`).
    """
    if scope == 'corpus':
        if obs <= 0:
            return None
        if strong:              # a final-form letter mid-word proves the break
            ok = obs >= 1
        elif minlen >= 3:
            ok = obs >= cfg.split_obs_min
        else:
            ok = obs >= cfg.split_obs_min_short and obs >= 2 * exp
        if not ok:
            return None
        return (4 + math.log10(obs) + (2 if strong else 0)
                + (1 if minlen >= 3 else 0))
    if obs:
        return 3.5 + min(1.0, math.log10(obs + 1)) \
            + (1.0 if strong else 0.0) + (0.5 if minlen >= 3 else 0.0)
    if strong:
        return 3.5          # a final-form letter mid-word proves the break
    if minlen >= 3 and exp >= 1:
        return 2.5
    return None


def resolve_splits(cands, observed, lex, scope):
    """Choose one word's segmentation by evidence.

    `cands` are ``(parts, strong)`` and `observed` their spaced counts.
    Returns ``(best, alts)``: best is ``(parts, obs, score, strong)`` or None
    — the accepted candidate with the highest score plus association (how
    far the spaced count exceeds chance, log10(obs/exp) capped at 3; corpus
    scope only, a book's count is not comparable with corpus expectation),
    then the fewest parts; alts lists every candidate as
    ``(parts, obs, exp, accepted)``.
    """
    best, best_key, alts = None, None, []
    for (parts, strong), obs in zip(cands, observed):
        exp = lex.expected(parts)
        score = _split_score(strong, obs, exp, min(len(p) for p in parts),
                             lex.cfg, scope)
        alts.append((parts, obs, exp, score is not None))
        if score is not None:
            key = (score + (_association(obs, exp) if scope == 'corpus'
                            else 0.0), -len(parts))
            if best_key is None or key > best_key:
                best, best_key = (parts, obs, score, strong), key
    return best, alts


def _association(obs, exp):
    if obs <= 0:
        return 0.0
    if exp <= 0:
        return 3.0
    return max(0.0, min(3.0, math.log10(obs / exp)))


def settle_split(errors, w, fw, best, lex, scope, strong=None):
    """Record a confirmed split, unless it would displace a correction.

    A word may also have an edit-1 style correction whose support (context,
    book) is only measured later. A split replaces such a correction only on
    strong evidence: a final-form letter break, or enough observations far
    above chance (corpus scope; in a book, enough in-book observations).
    Returns 'primary' (the split is the word's finding) or 'alternative'.
    """
    parts, obs, score = best[0], best[1], best[2]
    if strong is None:
        strong = best[3] if len(best) > 3 else False
    rec = ('missing_space', ' '.join(parts), obs, score)
    cur = errors.get(w)
    if cur is not None and (cur[1] in LOCAL_TYPES or uses_context(cur[1])):
        cfg = lex.cfg
        if scope == 'corpus':
            decisive = strong or (
                obs >= cfg.split_obs_min
                and obs >= cfg.split_override_ratio * lex.expected(parts))
        else:
            decisive = strong or obs >= cfg.split_obs_min
        if not decisive:
            return 'alternative'
        errors[w] = (fw, *rec)
        return 'primary'
    record_best(errors, w, fw, rec)
    return 'primary' if errors[w][1] == 'missing_space' else 'alternative'


def keep_structural_suspicion(errors, w, fw, cands, lex):
    """A final-form letter mid-word is wrong whether or not the split it
    suggests is ever seen spaced; record the suspicion without inventing a
    replacement when no split could be confirmed."""
    if w in errors or not any(strong for _, strong in cands):
        return
    cfg = lex.cfg
    record_best(errors, w, fw,
                ('final_midword', '', 0, 3.0 - _frequency_penalty(fw, cfg)))



def uses_context(errtype):
    """Error types whose correction is verified by neighbouring words."""
    return errtype.startswith('edit1') or errtype == 'spelling_variant'


def evidence_kind(errtype, sugg, sugg_freq, ctx_hits, sugg_local, tanach,
                  ctx_scope='corpus', split_scope='corpus'):
    """The strongest kind of support actually found for one occurrence."""
    if tanach == 3:              # verse reading confirmed by the reference
        return 'tanach'
    if errtype == 'missing_space':
        if sugg_freq and sugg_freq > 0:     # sugg_freq = spaced observations
            return ('split_observed' if split_scope == 'corpus'
                    else 'split_observed_book')
        return 'none'
    if ctx_hits > 0:
        return 'context' if ctx_scope == 'corpus' else 'context_book'
    if sugg_local >= 3:
        return 'book_local'
    if not sugg:
        return 'suspicion'
    if errtype in STRUCTURAL_TYPES:
        return 'structural'     # a deterministic orthographic rule
    if errtype == 'ocr_profile':
        return 'ocr_profile'    # the book's (reviewed) systematic confusion
    return 'none'


def book_repeat_flag(errtype, n_located, only_this_book):
    """All of a word's (2+) occurrences sit in one book: the author's own
    spelling rather than a typo. Corpus scope: located occurrences span one
    doc; book scope: the lexicon count is fully explained by this book."""
    return 1 if (errtype in LOCAL_TYPES and n_located >= 2
                 and only_this_book) else 0


# ---------------------------------------------------------------------------
# calibration provenance: learned weights are used only when they say where
# they came from
# ---------------------------------------------------------------------------

def _file_digest(path):
    import hashlib
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def write_calibration_meta(out_dir, info):
    """Write calibration_meta.json with the hashes of the learned files it
    vouches for; a file edited or replaced afterwards no longer matches."""
    import json
    info = dict(info)
    info['hashes'] = {
        fn: _file_digest(os.path.join(out_dir, fn))
        for fn in (LEARNED_F, OCR_PROFILES_F)} \
        if info.get('source') in ('human_review', 'machine_unreviewed') \
        else {}
    with open(os.path.join(out_dir, CALIBRATION_META_F), 'w',
              encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return info


def _calibration_meta(out_dir):
    import json
    try:
        with open(os.path.join(out_dir, CALIBRATION_META_F),
                  encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def calibration_source(out_dir):
    """'human_review' | 'machine_unreviewed' | None (absent, disabled by a
    calibrate with nothing to learn, or legacy)."""
    meta = _calibration_meta(out_dir)
    src = meta.get('source') if meta else None
    return src if src in ('human_review', 'machine_unreviewed') else None


def _vouched(out_dir, fn):
    """Source of a learned file whose hash matches the meta, else None."""
    meta = _calibration_meta(out_dir)
    src = meta.get('source') if meta else None
    if src not in ('human_review', 'machine_unreviewed'):
        return None
    path = os.path.join(out_dir, fn)
    want = (meta.get('hashes') or {}).get(fn)
    if not want or _file_digest(path) != want:
        return None
    return src


def load_learned(out_dir):
    """``(learned, source)``. A confusion_learned.json without provenance (an
    older `calibrate` learned it from the machine's own findings) or whose
    hash does not match calibration_meta.json is ignored, so unreviewed
    output never feeds back silently."""
    import json
    source = _vouched(out_dir, LEARNED_F)
    if source is None:
        return {}, None
    try:
        with open(os.path.join(out_dir, LEARNED_F), encoding='utf-8') as f:
            learned = json.load(f)
    except (OSError, ValueError):
        return {}, None
    return (learned if isinstance(learned, dict) else {}), source


def load_ocr_profiles(out_dir):
    """``(profiles, source)``, same provenance rule as :func:`load_learned`."""
    source = _vouched(out_dir, OCR_PROFILES_F)
    if source is None:
        return {}, None
    with open(os.path.join(out_dir, OCR_PROFILES_F), 'rb') as f:
        return pickle.load(f), source


def calibration_rows(rows):
    """Reviewed findings that can teach a letter substitution: the
    edit-distance-1 and OCR-profile classes (not structural rules)."""
    return [r for r in rows if r.get('errtype') and (
        r['errtype'].startswith('edit1') or r['errtype'] == 'ocr_profile')]


def learn_confusion(pairs_of_words):
    """Counter of letter substitutions in (word, correction) pairs."""
    pairs = Counter()
    for w, s in pairs_of_words:
        r = _dld1(w or '', s or '')
        if r and r[0] == 'sub':
            pairs[r[1] + r[2]] += 1
    return pairs


REVIEW_POSITIVE = ('approved', 'fixed')
REVIEW_NEGATIVE = ('not_error',)


def reviewed_findings(out_dir, statuses):
    """Human review decisions from ui_review.db as dicts, or ``([], why)``.

    Rows are limited to ``decided_by = 'human'`` when that column exists;
    without it every review row is a click in the review UI.
    """
    from .textsource import TextSourceError, connect_ro
    path = os.path.join(out_dir, 'ui_review.db')
    if not os.path.isfile(path):
        return [], 'ui_review.db not found'
    try:
        con = connect_ro(path)
    except (TextSourceError, sqlite3.Error) as e:
        return [], f'ui_review.db unreadable: {e}'
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'findings', 'review'} <= tables:
            return [], 'ui_review.db has no review tables'
        cols = {r[1] for r in con.execute('PRAGMA table_info(review)')}
        human = " AND r.decided_by = 'human'" if 'decided_by' in cols else ''
        ph = ','.join('?' * len(statuses))
        rows = [dict(zip(('id', 'word', 'suggestion', 'errtype', 'doc',
                          'rank', 'status', 'score', 'machine_suggestion'),
                         r)) for r in con.execute(
            f"SELECT f.id, f.word, COALESCE(NULLIF(r.custom_suggestion, ''), "
            f"f.suggestion), f.errtype, COALESCE(f.doc, ''), f.rank, "
            f"r.status, f.score, f.suggestion "
            f"FROM findings f JOIN review r ON r.finding_id = f.id "
            f"WHERE f.family = 'error' AND r.status IN ({ph}){human} "
            f"ORDER BY f.id", statuses)]
    finally:
        con.close()
    return rows, ('' if rows else 'no human-reviewed findings')


def calibrate(cfg, out_dir, from_machine=False):
    """Learn a letter-substitution confusion matrix and per-book OCR profiles
    from HUMAN-REVIEWED findings (ui_review.db: approved/fixed).

    Learning from the machine's own report would reinforce its unreviewed
    suggestions, so that happens only with ``from_machine=True`` and the
    result is labelled 'machine_unreviewed'. With neither, nothing is
    learned and the existing files are left as they are.
    """
    import json
    rows, why = reviewed_findings(out_dir, REVIEW_POSITIVE)
    rows = calibration_rows(rows)
    if rows:
        source = 'human_review'
        conf_rows = [(r['word'], r['suggestion']) for r in rows]
        prof_rows = [(r['word'], r['suggestion'], r['doc']) for r in rows]
    elif from_machine:
        source = 'machine_unreviewed'
        # coverage NOT checked: calibrate only learns letter weights from the
        # last complete report.db, and the UI runs it as the first step of a
        # rescan — refusing it would block the very rescan that repairs a
        # failed `locate`
        con = sqlite3.connect(_require(out_dir, REPORT_DB_F, 'כיול',
                                       check_coverage=False))
        conf_rows = list(con.execute(
            "SELECT DISTINCT word, suggestion FROM occurrences_full "
            "WHERE errtype = 'edit1_sub' AND ctx_hits > 0 AND score >= 4"))
        try:
            prof_rows = list(con.execute(
                "SELECT word, suggestion, doc FROM occurrences_full "
                "WHERE errtype = 'edit1_sub' AND score >= 4 "
                "AND (ctx_hits > 0 OR sugg_local >= 3)"))
        except sqlite3.OperationalError:      # older report.db without doc
            prof_rows = []
        con.close()
    else:
        print(f'[calibrate] nothing learned: {why or "no approved findings"}.'
              ' Calibration learns only from human-reviewed findings; '
              'review findings in the UI first, or pass '
              '--calibrate-from-machine to learn (labelled unreviewed) from '
              'the machine report. Earlier learned files are now disabled.',
              flush=True)
        return write_calibration_meta(out_dir, {
            'source': 'none', 'rows': 0, 'reason': why,
            'created': time.strftime('%Y-%m-%d %H:%M:%S')})

    pairs = learn_confusion(conf_rows)
    with open(os.path.join(out_dir, LEARNED_F), 'w', encoding='utf-8') as f:
        json.dump(dict(pairs), f, ensure_ascii=False, indent=1)
    print(f'[calibrate] source={source}: {len(pairs)} substitution pairs '
          f'from {sum(pairs.values()):,} findings', flush=True)
    for p, c in pairs.most_common(15):
        print(f'    {p[0]} -> {p[1]}: {c:,}', flush=True)

    # per-book OCR profiles: many substitutions of one letter pair in one
    # book prove a systematic OCR fault; `locate` rescans those books
    profiles = {}
    for w, s, doc in prof_rows:
        r = _dld1(w or '', s or '')
        if r and r[0] == 'sub':
            profiles.setdefault(doc, Counter())[r[1] + r[2]] += 1
    profiles = {d: {p: c for p, c in cnt.items() if c >= cfg.ocr_pair_min}
                for d, cnt in profiles.items()}
    profiles = {d: ps for d, ps in profiles.items() if ps}
    with open(os.path.join(out_dir, OCR_PROFILES_F), 'wb') as f:
        pickle.dump(profiles, f, protocol=4)
    info = write_calibration_meta(out_dir, {
        'source': source, 'rows': len(conf_rows), 'pairs': len(pairs),
        'profiles': len(profiles),
        'created': time.strftime('%Y-%m-%d %H:%M:%S')})
    print(f'[calibrate] OCR profiles: {len(profiles)} books, '
          f'{sum(len(v) for v in profiles.values())} systematic pairs',
          flush=True)
    if source == 'machine_unreviewed':
        print('[calibrate] WARNING: learned from UNREVIEWED machine findings '
              '(labelled machine_unreviewed in calibration_meta.json)',
              flush=True)
    return info


def _split_verify_chunk(chunk):
    """Count how often each split candidate occurs *with* spaces."""
    _chunk_stats_begin()
    seq = _W['seq']
    counts = {}
    for _, text in _W['corpus'].iter_texts(chunk):
        seq.count(tokenize(text), counts)
    return counts, _chunk_stats_end()


def load_review_rejections(out_dir):
    """Words rejected in review with GLOBAL scope (decisions.db unit '*').

    A rejection of one occurrence, of one suggested replacement or of a
    book's own spelling is not a statement about the word elsewhere, so it
    must never become a corpus-wide whitelist entry.
    """
    dec_path = os.path.join(out_dir, 'decisions.db')
    if not os.path.exists(dec_path):
        return set()
    dcon = sqlite3.connect(dec_path, timeout=30.0)
    try:
        return {r[0] for r in dcon.execute(
            "SELECT DISTINCT word FROM decisions "
            "WHERE verdict='reject' AND unit='*'")}
    except sqlite3.OperationalError:
        return set()
    finally:
        dcon.close()


def detect(spec, cfg, out_dir):
    t0 = time.time()
    with open(_require(out_dir, LEXICON_F, 'איתור',
                       allow_unread=unread_limit(cfg)), 'rb') as f:
        freq = pickle.load(f)
    policy = _policy(spec, cfg, out_dir, 'lexicon')
    N = sum(freq.values())
    print(f'[detect] lexicon: {len(freq):,} types, {N:,} tokens', flush=True)

    # optional whitelist: words listed here are never flagged (suppression
    # only — the corpus remains the sole source of correction candidates)
    whitelist = set()
    for path in (cfg.whitelist or ()):
        with open(path, encoding='utf-8') as f:
            whitelist.update(line.strip() for line in f if line.strip())
    if whitelist:
        print(f'[detect] whitelist: {len(whitelist):,} words', flush=True)

    # words the user rejected EVERYWHERE in review are never flagged again
    rejected = load_review_rejections(out_dir)
    if rejected:
        whitelist |= rejected
        print(f'[detect] review rejections honored: {len(rejected):,} '
              f'words', flush=True)

    # substitution weights learned by `magiah calibrate` (if it was run)
    learned, lsource = load_learned(out_dir)
    if lsource:
        print(f'[detect] learned confusion matrix: {len(learned)} pairs '
              f'(source: {lsource})', flush=True)
    elif os.path.exists(os.path.join(out_dir, LEARNED_F)):
        print(f'[detect] {LEARNED_F} has no provenance (learned from '
              f'unreviewed findings by an older version) - ignored',
              flush=True)

    lex = Lexicon(freq, cfg, learned)
    index = Edit1Index(lex)
    print(f'[detect] del-index={len(index.del_index):,}  '
          f'({time.time()-t0:.0f}s)', flush=True)

    errors = {}       # word -> (freq, errtype, suggestion, sugg_freq, score)
    split_cands = {}  # word -> [(parts, strong), ...]
    for n, (w, fw) in enumerate(freq.items(), 1):
        if w in whitelist:
            continue
        if n % 500000 == 0:
            print(f'  [detect] scanned {n:,} types ({time.time()-t0:.0f}s)',
                  flush=True)
        recs, splits = evaluate_word(w, fw, lex, index)
        for rec in recs:
            record_best(errors, w, fw, rec)
        if splits:
            split_cands[w] = splits
    del index

    # --- verify split candidates against the corpus -----------------------
    cand_list = [(w, parts) for w, cs in split_cands.items()
                 for parts, _ in cs]
    print(f'[detect] split candidates to verify: {len(cand_list):,} '
          f'({len(split_cands):,} words)  ({time.time()-t0:.0f}s)',
          flush=True)
    splits_path = os.path.join(out_dir, SPLITS_F)
    with open(splits_path, 'wb') as f:
        pickle.dump(cand_list, f, protocol=4)
    corpus = make_corpus(spec)
    counts = Counter()
    vstats = ReadStats()
    chunks = corpus.chunks(cfg.n_chunks)
    with _pool(spec, cfg, {'split_cands': splits_path}) as pool:
        for i, (c, st) in enumerate(
                pool.imap_unordered(_split_verify_chunk, chunks), 1):
            counts.update(c)
            vstats.add(ReadStats.from_dict(st))
            if i % 6 == 0:
                print(f'  [verify] chunk {i}/{len(chunks)} '
                      f'({time.time()-t0:.0f}s)', flush=True)
    n_ok, alts_out, idx = 0, {}, 0
    for w, cs in split_cands.items():
        obs = [counts.get(idx + k, 0) for k in range(len(cs))]
        idx += len(cs)
        best, alts = resolve_splits(cs, obs, lex, 'corpus')
        if not best:
            keep_structural_suspicion(errors, w, freq[w], cs, lex)
        if best:
            settle_split(errors, w, freq[w], best, lex, 'corpus')
            alts_out[w] = [(' '.join(p), o, e, ok and p == best[0])
                           for p, o, e, ok in alts]
            n_ok += 1
    print(f'[verify] confirmed {n_ok:,}/{len(split_cands):,} split words',
          flush=True)
    _fail_if_partial('detect', vstats, out_dir, policy=policy)
    _replace_atomically(os.path.join(out_dir, SPLIT_ALTS_F),
                        _dump_pickle(alts_out))

    _replace_atomically(os.path.join(out_dir, FLAGGED_F), _dump_pickle(errors))
    _write_coverage(out_dir, _coverage_info(
        'detect', vstats, {'calibration': lsource or 'none'}, policy=policy))
    print(f'[detect] flagged={len(errors):,}  time={time.time()-t0:.0f}s',
          flush=True)
    for k, v in Counter(v[1] for v in errors.values()).most_common():
        print(f'    {k}: {v:,}', flush=True)


# ---------------------------------------------------------------------------
# Tanach reference (Otzaria corpora). magiah.tanach identifies the Bible books
# explicitly (category + title + verse heRefs), aligns their editions per
# (work, chapter, verse) and groups them into independent sources. A quotation
# counts as evidence only when it aligns with ONE verse position; nothing here
# replaces the detector's suggestion — a Tanach reading is an alternative.
# ---------------------------------------------------------------------------

def _build_verse_index(db_path, stats):
    """The verified-editions index (see magiah.tanach). Every row it reads
    (book lines and version lines) is counted in `stats`; the caller decides
    whether a partial read may be used."""
    return tanach.build_index(db_path, stats)


def _tanach_edition_errors(vidx):
    """Minority readings of one independent source against >= 2 agreeing
    independent sources, within the same work and verse.

    Computed from the index already built — the database is not read again,
    so this pass has nothing of its own to count or to close."""
    rows, st = vidx.edition_errors()
    print(f'[tanach] edition disagreements: {st}', flush=True)
    return rows


def _tanach_check(db_path, all_occ, flagged, lstats, out_dir, passes,
                  policy=None):
    """Compare occurrences with the verse they quote.

    Returns ``(kept, tan_info, matches, edition_rows, evidence_rows)``.
    An occurrence leaves the main report only when >= 2 independent sources
    read exactly that word at the aligned verse position.

    The index read is recorded as the ``tanach_index`` pass of `locate`; a
    partial read stops the stage here, before report.db is touched."""
    t1 = time.time()
    tstats = passes['tanach_index'] = ReadStats()
    vidx = _build_verse_index(db_path, tstats)
    _fail_if_partial('locate', lstats, out_dir, passes=passes, policy=policy)
    c = vidx.counts()
    print(f"[tanach] index: {c['works']} works, {c['verses']:,} verses, "
          f"{c['editions']} editions, {c['independent_sources']} independent "
          f"sources ({time.time()-t1:.0f}s)", flush=True)
    kept, tan_info, matches, evidence_rows = [], [], [], []
    kinds = Counter()
    cache = {}
    for oc in all_occ:
        w, uid, doc, prev, nxt, snip = oc
        key = (w, prev, nxt, snip)
        if key not in cache:
            cache[key] = vidx.evidence(w, prev, nxt, snip)
        ev = cache[key]
        code = tanach.TANACH_NONE
        if ev is not None:
            fr = flagged.get(w)
            code, kind = ev.decide(fr[2] if fr else '')
            kinds[kind] += 1
            evidence_rows.append(tanach.evidence_row(
                vidx, ev, w, uid, doc, snip,
                (fr[1], fr[2]) if fr else ('', '')))
            if ev.kind == tanach.MATCH:
                matches.append((w, uid, doc, snip, evidence_rows[-1][-1]))
                continue
        kept.append(oc)
        tan_info.append((code, ev.reading if code else ''))
    print(f'[tanach] aligned occurrences by kind: {dict(kinds)}', flush=True)
    edition_rows = _tanach_edition_errors(vidx)
    return kept, tan_info, matches, edition_rows, evidence_rows


# stage-3 helper: a rare word right after a title is a person/place name
NAME_TRIGGERS = frozenset((
    'ר', 'רב', 'רבי', 'הרב', 'הגאון', 'מו"ה', 'מוה"ר', 'מהר"ר', 'כמוהר"ר',
    'מהור"ר', 'אבד"ק', 'בק"ק', 'דק"ק', 'מק"ק', 'בכפר', 'בעיר', 'במדינת',
    'לעיר', 'העיר'))

# ---------------------------------------------------------------------------
# stage 3: locate occurrences + extra-space + context verification
# ---------------------------------------------------------------------------

def _snippet(text, s, e):
    """Bounded context of one token — never the whole (possibly huge) line."""
    return text[max(0, s - 45):e + 45].strip()


def _editorial_adjacent(text, s, e):
    """A trailing geresh marks an abbreviation (וכוננ' = וכוננה); an adjacent
    bracket/asterisk marks editorial notation (קיצ[ו]ר, תקינ(?))."""
    return ((e < len(text) and text[e] in '\'"([{*&')
            or (s > 0 and text[s - 1] in ')]}*&'))


def ocr_profile_hit(w, fw, prof, freq, cfg):
    """``(correction, freq)`` of `w` along a book's profiled letter pairs.

    In books with a proven systematic substitution, words above the global
    rarity cutoff are rescanned for exactly those pairs.
    """
    if not (3 <= len(w) <= 15) or is_abbrev(w) \
            or not (1 <= fw <= cfg.ocr_rare_max):
        return None
    hit = None
    for pair in prof:
        a, b = pair[0], pair[1]
        i0 = w.find(a)
        while i0 != -1:
            w2 = w[:i0] + b + w[i0 + 1:]
            f2 = freq.get(w2, 0)
            if (f2 >= cfg.common_min and f2 >= cfg.ocr_ratio * fw
                    and (hit is None or f2 > hit[1])):
                hit = (w2, f2)
            i0 = w.find(a, i0 + 1)
    return hit


def ocr_score(fw, fs):
    return 2 + math.log10(fs / max(fw, 1))


def locate_line(content, freq, cfg, flagged, prof=None):
    """All findings of one line; core.locate and the book scan both use it.

    Returns None for a skipped foreign/garbled line, else ``(occ, joins,
    ocr)``: occ ``[(word, prev, next, snippet)]`` for flagged words, joins
    ``[(part1, part2, joined, join_freq, snippet)]``, ocr ``[(word, freq,
    correction, corr_freq, snippet)]``. Snippets exist only for reported
    tokens and are bounded, so a multi-MB line stays linear in time and
    memory and is never truncated.
    """
    from .normalize import TOKEN_RE, clean
    text = clean(content)
    toks = list(TOKEN_RE.finditer(text))
    # foreign-language / heavily garbled lines: too many uncommon words for
    # point-fixes to mean anything. 1-2 letter tokens dilute the ratio.
    n_real = uncommon = 0
    for m in toks:
        t = m.group()
        if len(t) >= 3:
            n_real += 1
            if freq.get(t, 0) < cfg.common_min:
                uncommon += 1
    if n_real >= 5 and uncommon > cfg.foreign_ratio * n_real:
        return None

    # pass 1 — extra space: adjacent pair whose concatenation is a frequent
    # word. Such tokens are excluded from letter-level reporting below, so
    # one textual error yields one report row.
    joins, joined_idx = [], set()
    for k in range(len(toks) - 1):
        m, nx = toks[k], toks[k + 1]
        w, b = m.group(), nx.group()
        if is_abbrev(w):
            continue
        # the gap must be pure whitespace — a bracket, hyphen or asterisk
        # means editorial markup (בחשבונ(י)ך, ל-נקותם)
        if (nx.start() - m.end() <= 1 and not is_abbrev(b)
                and not text[m.end():nx.start()].strip()):
            mn = min(freq.get(w, 0), freq.get(b, 0))
            if mn <= 3:
                fj = freq.get(w + b, 0)
                if fj >= cfg.join_min and fj >= 30 * max(1, mn):
                    joins.append((w, b, w + b, fj,
                                  text[max(0, m.start() - 45):
                                       nx.end() + 45].strip()))
                    joined_idx.update((k, k + 1))

    # pass 2 — occurrences of flagged word types (+ OCR-profile layer)
    occ, ocr = [], []
    for k, m in enumerate(toks):
        w = m.group()
        if prof and w not in flagged:
            fw = freq.get(w, 0)
            hit = ocr_profile_hit(w, fw, prof, freq, cfg)
            if hit and not _editorial_adjacent(text, m.start(), m.end()):
                ocr.append((w, fw, hit[0], hit[1],
                            _snippet(text, m.start(), m.end())))
        fr = flagged.get(w)
        if fr is None or k in joined_idx:
            continue
        s, e = m.start(), m.end()
        if _editorial_adjacent(text, s, e):
            continue
        # a footnote marker glued to a word ('...א)') is notation, not an
        # extra last letter
        if (e < len(text) and text[e] == ')' and fr[1] == 'edit1_ins'
                and fr[2] == w[:-1]):
            continue
        prev = toks[k - 1].group() if k else ''
        nxt = toks[k + 1].group() if k + 1 < len(toks) else ''
        # a rare word right after a title (ר' פלוני, מוה"ר ר' פלוני) is a
        # person/place name, not a typo
        if fr[1] not in ('missing_space', 'final_midword') \
                and (prev in NAME_TRIGGERS
                     or (k >= 2 and toks[k - 2].group() in NAME_TRIGGERS)):
            continue
        occ.append((w, prev, nxt, _snippet(text, s, e)))
    return occ, joins, ocr


def _locate_chunk(chunk):
    _chunk_stats_begin()
    freq, flagged, cfg = _W['lexicon'], _W['flagged'], _W['cfg']
    profiles = _W.get('ocr_profiles') or {}
    occ, joins, ocr = [], [], []
    for uid, doc, content in _W['corpus'].iter_texts_docs(chunk):
        res = locate_line(content, freq, cfg, flagged, profiles.get(doc))
        if res is None:
            continue
        o, j, c = res
        occ.extend((w, uid, doc, p, n, s) for w, p, n, s in o)
        joins.extend((uid,) + x for x in j)
        ocr.extend((w, fw, sg, fs, uid, doc, s) for w, fw, sg, fs, s in c)
    return occ, joins, ocr, _chunk_stats_end()


def count_pairs(toks, pairs, first, counts):
    """Count the wanted adjacent (word, word) pairs in one token list."""
    for i in range(len(toks) - 1):
        t = toks[i]
        if t in first:
            p = (t, toks[i + 1])
            if p in pairs:
                counts[p] += 1


def _ctx_count_chunk(chunk):
    """One corpus pass that feeds the verification signals:
    * ctx: how often does (neighbor, correction) occur as an adjacent pair?
    * local: how often does each proposed correction occur in the same book
      as the flagged word?
    * seq (book scan only): spaced occurrences of split candidates."""
    _chunk_stats_begin()
    pairs, first = _W['ctx_pairs'], _W['ctx_first']
    book_need = _W['book_need']
    seq = _W.get('seq')
    counts, local, seq_counts = Counter(), Counter(), {}
    for _, doc, text in _W['corpus'].iter_texts_docs(chunk):
        toks = tokenize(text)
        need = book_need.get(doc)
        if need is not None:
            for t in toks:
                if t in need:
                    local[(doc, t)] += 1
        count_pairs(toks, pairs, first, counts)
        if seq is not None:
            seq.count(toks, seq_counts)
    return counts, local, seq_counts, _chunk_stats_end()


def write_scan_meta(con, spec):
    """Record in report.db which corpus produced it, so the fixer can write
    each finding into the library it was found in, not the current setting."""
    import json
    from datetime import datetime
    root = (os.path.abspath(spec['path'])
            if spec.get('type') in ('library', 'hybrid') and spec.get('path')
            else '')
    con.execute('CREATE TABLE IF NOT EXISTS scan_meta('
                'key TEXT PRIMARY KEY, value TEXT)')
    con.executemany('INSERT OR REPLACE INTO scan_meta VALUES(?,?)', [
        ('corpus', json.dumps(spec, ensure_ascii=False)),
        ('library_root', root),
        ('scanned_at', datetime.now().isoformat(timespec='microseconds'))])


def locate(spec, cfg, out_dir):
    t0 = time.time()
    with open(_require(out_dir, FLAGGED_F, 'מיקום',
                       allow_unread=unread_limit(cfg)), 'rb') as f:
        flagged = pickle.load(f)
    policy = _policy(spec, cfg, out_dir, 'detect')
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)

    all_occ, all_joins, all_ocr = [], [], []
    lstats = ReadStats()
    loads = {'lexicon': os.path.join(out_dir, LEXICON_F),
             'flagged': os.path.join(out_dir, FLAGGED_F)}
    prof_path = os.path.join(out_dir, OCR_PROFILES_F)
    psource = calibration_source(out_dir)
    if os.path.exists(prof_path):
        if psource:
            loads['ocr_profiles'] = prof_path
            print(f'[locate] OCR profiles in use (source: {psource})',
                  flush=True)
        else:
            print(f'[locate] {OCR_PROFILES_F} has no provenance - ignored',
                  flush=True)
    with _pool(spec, cfg, loads) as pool:
        for i, (occ, joins, ocr, st) in enumerate(
                pool.imap_unordered(_locate_chunk, chunks), 1):
            lstats.add(ReadStats.from_dict(st))
            all_occ.extend(occ)
            all_joins.extend(joins)
            all_ocr.extend(ocr)
            print(f'  [locate] chunk {i}/{len(chunks)}  occ={len(all_occ):,}  '
                  f'space={len(all_joins):,}  ocr={len(all_ocr):,}  '
                  f'({time.time()-t0:.0f}s)', flush=True)
    # a partial main pass is refused before the Tanach and context passes
    # spend more time on it
    passes = {}                         # name -> ReadStats of each later pass
    _fail_if_partial('locate', lstats, out_dir, policy=policy)

    # --- Tanach reference check (Otzaria only) ----------------------------
    # Verified verse matches go to a separate review file (the reference
    # itself might be wrong); a differing verse reading is stored next to the
    # detector's suggestion as an alternative, never in place of it.
    tanach_matches, tanach_errors_rows, tanach_evidence = [], [], []
    tan_info = None
    if spec.get('preset') == 'otzaria':
        (all_occ, tan_info, tanach_matches, tanach_errors_rows,
         tanach_evidence) = _tanach_check(spec['path'], all_occ, flagged,
                                          lstats, out_dir, passes,
                                          policy)
        print(f'[tanach] verse matches (separate review file): '
              f'{len(tanach_matches):,}  edition variants: '
              f'{len(tanach_errors_rows):,}', flush=True)

    # words whose (very few) occurrences all sit in a single book are usually
    # the author's own idiosyncratic spelling, not typos
    w_count, w_docs = Counter(), {}
    for w, _, doc, _, _, _ in all_occ:
        w_count[w] += 1
        w_docs.setdefault(w, set()).add(doc)
    repeat_words = {w for w, n in w_count.items()
                    if book_repeat_flag(flagged[w][1], n, len(w_docs[w]) == 1)}
    print(f'[locate] same-book-repeat words suppressed: {len(repeat_words):,}',
          flush=True)

    # context verification for edit-distance-1 suggestions (does the corrected
    # word occur next to the same neighbors elsewhere?) + book-local counts
    # (does the corrected word occur in this very book?)
    ctx_pairs, book_need = set(), {}
    for w, uid, doc, prev, nxt, _ in all_occ:
        fr = flagged[w]
        if uses_context(fr[1]):
            sugg = fr[2]
            if prev:
                ctx_pairs.add((prev, sugg))
            if nxt:
                ctx_pairs.add((sugg, nxt))
        if fr[1] in LOCAL_TYPES and fr[2]:
            book_need.setdefault(doc, set()).add(fr[2])
    print(f'[locate] context pairs to verify: {len(ctx_pairs):,}', flush=True)
    ctx_counts, local_counts = Counter(), Counter()
    if ctx_pairs or book_need:
        ctx_path = os.path.join(out_dir, 'ctx_pairs.pkl')
        with open(ctx_path, 'wb') as f:
            pickle.dump(ctx_pairs, f, protocol=4)
        need_path = os.path.join(out_dir, 'book_need.pkl')
        with open(need_path, 'wb') as f:
            pickle.dump(book_need, f, protocol=4)
        cstats = passes['context'] = ReadStats()
        with _pool(spec, cfg, {'ctx_pairs': ctx_path,
                               'book_need': need_path}) as pool:
            for i, (c, lc, _sc, st) in enumerate(
                    pool.imap_unordered(_ctx_count_chunk, chunks), 1):
                ctx_counts.update(c)
                local_counts.update(lc)
                cstats.add(ReadStats.from_dict(st))
                if i % 6 == 0:
                    print(f'  [context] chunk {i}/{len(chunks)} '
                          f'({time.time()-t0:.0f}s)', flush=True)
        _fail_if_partial('locate', lstats, out_dir, passes=passes,
                         policy=policy)

    # --- write the report database ---------------------------------------
    # built under a temp name and moved into place only once complete, so a
    # failed run never replaces the last good report.db
    db_path = os.path.join(out_dir, REPORT_DB_F)
    tmp_path = db_path + '.tmp'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    con = sqlite3.connect(tmp_path)
    # occurrences.occ_sugg/occ_score: an occurrence-level suggestion (OCR
    # profiles differ per book) that overrides the word-level one in enrich;
    # evidence: what actually supports the finding (see evidence_kind)
    try:
        con.executescript('''
        CREATE TABLE errors(word TEXT PRIMARY KEY, freq INT, errtype TEXT,
                            suggestion TEXT, sugg_freq INT, score REAL);
        CREATE TABLE occurrences(word TEXT, unit TEXT, doc TEXT, ctx_hits INT,
                                 sugg_local INT, book_repeat INT, tanach INT,
                                 tanach_sugg TEXT, snippet TEXT,
                                 evidence TEXT, occ_sugg TEXT,
                                 occ_score REAL);
        CREATE TABLE space_errors(unit TEXT, part1 TEXT, part2 TEXT,
                                  joined TEXT, join_freq INT, snippet TEXT);
        CREATE TABLE tanach_matches(word TEXT, unit TEXT, doc TEXT,
                                    snippet TEXT, evidence TEXT);
        CREATE TABLE tanach_errors(unit TEXT, word TEXT, canonical TEXT,
                                   snippet TEXT, evidence TEXT);
        CREATE TABLE split_alternatives(word TEXT, parts TEXT, observed INT,
                                        expected REAL, chosen INT,
                                        is_primary INT);
        ''')
        write_scan_meta(con, spec)
        con.executemany('INSERT OR REPLACE INTO errors VALUES(?,?,?,?,?,?)',
                        [(w, *v) for w, v in flagged.items()])
        rows = []
        for j, (w, uid, doc, prev, nxt, snip) in enumerate(all_occ):
            fr = flagged[w]
            hits = 0
            if uses_context(fr[1]):
                sugg = fr[2]
                hits = (ctx_counts.get((prev, sugg), 0)
                        + ctx_counts.get((sugg, nxt), 0))
            local = local_counts.get((doc, fr[2]), 0) if fr[1] in LOCAL_TYPES else 0
            tan, tsugg = tan_info[j] if tan_info else (0, '')
            ev = evidence_kind(fr[1], tsugg or fr[2], fr[3], hits, local, tan)
            rows.append((w, uid, doc, hits, local,
                         1 if w in repeat_words else 0, tan, tsugg, snip,
                         ev, None, None))
        con.executemany('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        rows)
        # OCR-profile findings: word-level entry + occurrence rows carrying their
        # own book's suggestion (the word-level one is only the first seen)
        for w, fw, sugg, fs, uid, doc, snip in all_ocr:
            if w not in flagged:
                score = ocr_score(fw, fs)
                con.execute('INSERT OR IGNORE INTO errors VALUES(?,?,?,?,?,?)',
                            (w, fw, 'ocr_profile', sugg, fs, score))
                con.execute('INSERT INTO occurrences '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (w, uid, doc, 0, 0, 0, 0, '', snip,
                             evidence_kind('ocr_profile', sugg, fs, 0, 0, 0),
                             sugg, score))
        if all_ocr:
            print(f'[locate] ocr-profile findings: {len(all_ocr):,}', flush=True)
        alts_path = os.path.join(out_dir, SPLIT_ALTS_F)
        if os.path.exists(alts_path):
            with open(alts_path, 'rb') as f:
                alts = pickle.load(f)
            # chosen: the best-evidenced segmentation; is_primary: that split is
            # the word's reported finding (else a correction kept precedence)
            con.executemany(
                'INSERT INTO split_alternatives VALUES(?,?,?,?,?,?)',
                [(w, p, o, e, 1 if ch else 0,
                  1 if flagged[w][1] == 'missing_space' else 0)
                 for w, lst in alts.items() if w in flagged
                 for p, o, e, ch in lst])
        con.executemany('INSERT INTO tanach_matches VALUES(?,?,?,?,?)',
                        tanach_matches)
        con.executemany('INSERT INTO tanach_errors VALUES(?,?,?,?,?)',
                        tanach_errors_rows)
        tanach.write_evidence(con, tanach_evidence)
        con.executemany('INSERT INTO space_errors VALUES(?,?,?,?,?,?)', all_joins)
        con.commit()
        corpus.enrich(con)
        info = _coverage_info('locate', lstats,
                             {'ocr_profiles': psource or 'none'},
                             passes=passes, policy=policy)
        if not info['complete']:
            # the report carries its own coverage record, so whatever shows
            # it (the review UI) knows it is partial without trusting a file
            # beside it that a later run may have replaced
            con.execute('CREATE TABLE coverage(info TEXT)')
            con.execute('INSERT INTO coverage VALUES(?)',
                        (json.dumps(info, ensure_ascii=False),))
            con.commit()
        con.close()
        try:
            os.replace(tmp_path, db_path)
        except PermissionError as e:
            # Windows: report.db is held open (the review UI, a DB viewer);
            # the last good report.db stays exactly as it was
            raise StageError(
                f'לא ניתן לעדכן את {db_path}: הקובץ פתוח בתוכנה אחרת '
                '(למשל ממשק הסקירה). הקובץ הקודם נשאר כמות שהוא. '
                'יש לסגור את התוכנה ולהריץ שוב את שלב "מיקום".') from e
    except BaseException:
        con.close()
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    _write_coverage(out_dir, info)
    print(f'[locate] occurrences={len(rows):,}  space_errors={len(all_joins):,}'
          f'  time={time.time()-t0:.0f}s -> {db_path}', flush=True)


# ---------------------------------------------------------------------------
# stage 4: report
# ---------------------------------------------------------------------------

# Ranking combines the base score with three corpus-evidence signals:
# * ctx_hits    — the correction was seen next to the same neighbors (edit1)
# * sugg_local  — the correction is used inside the very same book
# * book_repeat — all occurrences of the word sit in one book (idiosyncratic
#                 spelling, not a typo) — strong demotion
# plus the Tanach signal: tanach = 3 (an aligned verse whose reading, backed
# by >= 2 independent sources, differs from the word AND equals the detector's
# suggestion). tanach = 4 (the verse reads otherwise than the detector) and
# tanach = 2 (rows of the old trigram heuristic) earn nothing.
RANK_SQL = '''score
              + CASE WHEN errtype LIKE 'edit1%' THEN
                  CASE WHEN ctx_hits > 0 THEN 1.5 ELSE -1.0 END
                ELSE 0 END
              + CASE WHEN sugg_local >= 10 THEN 1.5
                     WHEN sugg_local >= 3 THEN 0.7 ELSE 0 END
              - CASE WHEN book_repeat = 1 THEN 3.0 ELSE 0 END
              + CASE WHEN tanach = 3 THEN 4.0 ELSE 0 END'''

# a finding is "verified" when the context or the book itself supports the
# proposed correction and the word is not an in-book spelling convention.
# The column `evidence` names WHICH support was found (and its scope).
VERIFIED_SQL = 'book_repeat = 0 AND (ctx_hits > 0 OR sugg_local >= 3)'


def _open_report(path):
    try:
        return open(path, 'w', newline='', encoding='utf-8-sig')
    except PermissionError:
        print(f'[report] SKIPPED (file open in another program): {path}',
              flush=True)
        return None


def _write_reports(con, dest_dir, extra_where, params, top):
    """Write the full set of report CSVs into dest_dir, optionally filtered
    (extra_where/params) to a single source repository."""
    limit = f'LIMIT {top}' if top else ''
    types = [r[0] for r in con.execute(
        'SELECT DISTINCT errtype FROM occurrences_full ORDER BY errtype')]
    # the Tanach reading sits next to the detector's suggestion, never in it
    have = {r[1] for r in con.execute('PRAGMA table_info(occurrences_full)')}
    ev = 'evidence' if 'evidence' in have else "''"      # older report.db
    tan_col = ("COALESCE(tanach_reading, '')" if 'tanach_reading' in have
               else "''")
    cols = ('word, suggestion, {tan}, ROUND({rank}, 2), ctx_hits, '
            'sugg_local, {ev}, source, ref, unit, snippet').format(
                rank=RANK_SQL, tan=tan_col, ev=ev)
    header = ['word', 'suggestion', 'tanach_reading', 'rank', 'ctx_hits',
              'sugg_local', 'evidence', 'source', 'ref', 'unit', 'snippet']
    for t in types:
        variants = [(f'errors_{t}.csv', f'errtype = ?{extra_where}')]
        if t.startswith('edit1'):
            # edit1 is the noisiest class — also export the high-precision
            # subset where corpus evidence supports the correction
            variants.append((f'errors_{t}_verified.csv',
                             f'errtype = ? AND {VERIFIED_SQL}{extra_where}'))
        for fname, where in variants:
            path = os.path.join(dest_dir, fname)
            out = _open_report(path)
            if out is None:
                continue
            with out as f:
                wr = csv.writer(f)
                wr.writerow(header)
                n = 0
                for row in con.execute(f'''
                        SELECT {cols} FROM occurrences_full WHERE {where}
                        ORDER BY {RANK_SQL} DESC {limit}''', (t, *params)):
                    wr.writerow(row)
                    n += 1
            print(f'[report] {n:,} rows -> {path}', flush=True)
    # Tanach review files (Otzaria corpora)
    for tbl, fname, sel, hdr in (
            ('tanach_matches_full', 'tanach_matches.csv',
             'word, source, ref, unit, snippet',
             ['word', 'source', 'ref', 'unit', 'snippet']),
            ('tanach_errors_full', 'tanach_edition_errors.csv',
             'word, canonical, source, ref, unit, snippet',
             ['word', 'canonical', 'source', 'ref', 'unit', 'snippet'])):
        try:
            con.execute(f'SELECT 1 FROM {tbl} LIMIT 1')
        except sqlite3.OperationalError:
            continue
        if 'evidence' in {r[1] for r in con.execute(
                f'PRAGMA table_info({tbl})')}:
            sel, hdr = sel + ', evidence', hdr + ['evidence']
        path = os.path.join(dest_dir, fname)
        out = _open_report(path)
        if out is None:
            continue
        with out as f:
            wr = csv.writer(f)
            wr.writerow(hdr)
            n = 0
            for row in con.execute(
                    f'SELECT {sel} FROM {tbl} WHERE 1=1{extra_where}',
                    params):
                wr.writerow(row)
                n += 1
        print(f'[report] {n:,} rows -> {path}', flush=True)

    path = os.path.join(dest_dir, 'space_errors.csv')
    out = _open_report(path)
    if out is not None:
        with out as f:
            wr = csv.writer(f)
            wr.writerow(['part1', 'part2', 'joined', 'join_freq',
                         'source', 'ref', 'unit', 'snippet'])
            n = 0
            for row in con.execute(f'''
                    SELECT part1, part2, joined, join_freq, source, ref,
                           unit, snippet
                    FROM space_errors_full WHERE 1=1{extra_where}
                    ORDER BY join_freq DESC {limit}''', params):
                wr.writerow(row)
                n += 1
        print(f'[report] {n:,} space errors -> {path}', flush=True)


def report(cfg, out_dir, top=0):
    con = sqlite3.connect(_require(out_dir, REPORT_DB_F, 'דוחות',
                                   allow_unread=unread_limit(cfg)))
    gaps = accepted_gaps(out_dir, 'locate')
    if gaps:
        rows = max(g['unread_rows'] for g in gaps.values())
        path = os.path.join(out_dir, COVERAGE_F.format(stage=list(gaps)[-1]))
        print(f'[report] אזהרה: הדוחות מבוססים על סריקה חלקית — {rows:,} '
              f'שורות מהקלט לא נקראו (אושר במפורש ב-‎--allow-unread‎). '
              f'פרטים: {path}', flush=True)
    _write_reports(con, out_dir, '', (), top)
    # a separate folder per source repository (Sefaria, Dicta, wikisource...)
    try:
        origins = [r[0] for r in con.execute(
            "SELECT DISTINCT origin FROM occurrences_full "
            "WHERE origin IS NOT NULL AND origin != '' ORDER BY origin")]
    except sqlite3.OperationalError:
        origins = []
    for org in origins:
        safe = re.sub(r'[^\w.\-]+', '_', org)
        d = os.path.join(out_dir, 'by_source', safe)
        os.makedirs(d, exist_ok=True)
        print(f'[report] ===== {org} =====', flush=True)
        _write_reports(con, d, ' AND origin = ?', (org,), top)
    con.close()
