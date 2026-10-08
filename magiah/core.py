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
from multiprocessing import Pool

from .config import Config
from . import tanach
from .corpus import make_corpus
from .textsource import ReadStats
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


def _require(out_dir, filename, stage, check_coverage=True):
    """Fail cleanly when a prerequisite of `stage` is missing.

    First-run guard: every stage except `lexicon` consumes a file produced by
    an earlier stage. Without this the user gets a raw FileNotFoundError /
    "no such table" traceback in English — and, for the report.db cases, a
    0-byte report.db left behind by sqlite3.connect() that then blocks the UI
    from starting at all.

    The file is also refused when the latest run of the stage that produces
    it did not read its whole input (:func:`coverage_problem`).
    """
    path = os.path.join(out_dir, filename)
    need_cmd, need_he = _STAGE_OF.get(filename, ('all', 'סריקה'))
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        problem = check_coverage and coverage_problem(out_dir, need_cmd)
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
        pairs, first = {}, set()
        for idx, (_, parts) in enumerate(_W['split_cands']):
            first.add(parts[0])
            pairs.setdefault((parts[0], parts[1]), []).append(idx)
        _W['sc_pairs'], _W['sc_first'] = pairs, first
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


def _write_coverage(out_dir, stage, stats, extra=None, passes=None):
    """Persist what a stage actually read (the run's coverage evidence).

    `passes` holds the stage's further reads of the input (Tanach index,
    context verification); the stage is complete only if every pass was.
    """
    passes = passes or {}
    unread = sum(s.unread() for s in (stats, *passes.values()))
    info = {'stage': stage, 'complete': unread == 0, 'unread': unread,
            **stats.to_dict(), **(extra or {})}
    if passes:
        info['passes'] = {k: v.to_dict() for k, v in passes.items()}

    def write(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
    _replace_atomically(os.path.join(out_dir, COVERAGE_F.format(stage=stage)),
                        write)
    print(f'[{stage}] coverage: lines={stats.lines:,} chars={stats.chars:,} '
          f'decode_errors={stats.decode_errors:,} missing={stats.missing:,} '
          f'version_lines_skipped={stats.version_lines_skipped:,}'
          + ''.join(f' {k}.unread={v.unread():,}' for k, v in passes.items()),
          flush=True)
    return info


def _fail_if_partial(stage, stats, out_dir, extra=None, passes=None):
    """A pass with unreadable rows must never be reported as complete.

    Called BEFORE the stage writes its output: a partial read records its
    coverage file and stops, so the previous output stays untouched (and is
    then refused by its consumers — see :func:`coverage_problem`).
    """
    passes = passes or {}
    if not any(s.unread() for s in (stats, *passes.values())):
        return
    info = _write_coverage(out_dir, stage, stats, extra, passes)
    raise PartialRead(
        f'שלב "{_STAGE_HE.get(stage, stage)}" לא הצליח לקרוא '
        f'{info["unread"]:,} שורות '
        f'מהקלט, ולכן התוצאה חלקית ואינה מוצגת כהצלחה.' + chr(10) +
        f'פרטים: {os.path.join(out_dir, COVERAGE_F.format(stage=stage))}')


_STAGE_HE = {cmd: he for cmd, he in _STAGE_OF.values()}
# the stages that read the corpus, in order; each one's output is derived from
# the outputs of the ones before it
_READ_STAGES = ('lexicon', 'detect', 'locate')


def coverage_problem(out_dir, stage):
    """Why the output of `stage` must not be used — or None if it may.

    The rule: ``coverage_<stage>.json`` describes the stage's LATEST attempt,
    and an output is used only if the latest attempt of its stage, and of
    every stage before it, read the whole input. A partial attempt never
    replaces the output, so an older complete output may still be on disk
    after a failed run; it is refused all the same — it does not reflect the
    input the user last scanned, and using it silently would hide the
    failure. No coverage file at all (output written before coverage was
    recorded) is accepted.
    """
    upto = _READ_STAGES.index(stage) + 1 if stage in _READ_STAGES else 0
    for st in _READ_STAGES[:upto] or (stage,):
        problem = _stage_coverage_problem(out_dir, st)
        if problem:
            return problem
    return None


def _stage_coverage_problem(out_dir, stage):
    path = os.path.join(out_dir, COVERAGE_F.format(stage=stage))
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            info = json.load(f)
        if info.get('complete') is True:
            return None
        unread = int(info.get('unread', info.get('decode_errors', 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        unread = 0                      # unreadable evidence is no evidence
    he = _STAGE_HE.get(stage, stage)
    return (f'הריצה האחרונה של שלב "{he}" לא קראה את כל הקלט '
            f'({unread:,} שורות לא נקראו), ולכן אין להשתמש בתוצרים שלו '
            f'ושל השלבים שאחריו.\n'
            f'יש לתקן את הבעיה ולהריץ שוב:  python -X utf8 -m magiah '
            f'all --out "{out_dir}"\n'
            f'פרטים: {path}')


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
    # coverage first: a partial lexicon must not replace the last good one
    _fail_if_partial('lexicon', stats, out_dir, extra)
    _replace_atomically(os.path.join(out_dir, LEXICON_F),
                        _dump_pickle(dict(lex)))
    _write_coverage(out_dir, 'lexicon', stats, extra)


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


def _segment(w, freq, cfg):
    """Split w into a sequence of frequent parts (missing-space candidate).
    Dynamic programming, minimizing the number of parts."""
    n = len(w)
    best = [None] * (n + 1)              # best[i] = (nparts, -minfreq, parts)
    best[0] = (0, -math.inf, [])
    for i in range(n):
        if best[i] is None:
            continue
        for j in range(i + 2, min(i + cfg.max_part, n) + 1):
            f = freq.get(w[i:j], 0)
            if f < cfg.part_min:
                continue
            cand = (best[i][0] + 1, max(best[i][1], -f), best[i][2] + [w[i:j]])
            if best[j] is None or cand[:2] < best[j][:2]:
                best[j] = cand
    if best[n] is None or not (2 <= best[n][0] <= cfg.max_parts):
        return None
    return best[n][2], -best[n][1]


def _split_verify_chunk(chunk):
    """Count how often each split candidate occurs *with* spaces."""
    _chunk_stats_begin()
    first, pairs, cands = _W['sc_first'], _W['sc_pairs'], _W['split_cands']
    counts = Counter()
    for _, text in _W['corpus'].iter_texts(chunk):
        toks = tokenize(text)
        for i in range(len(toks) - 1):
            if toks[i] in first:
                for idx in pairs.get((toks[i], toks[i + 1]), ()):
                    parts = cands[idx][1]
                    if len(parts) == 2 or (i + 2 < len(toks)
                                           and toks[i + 2] == parts[2]):
                        counts[idx] += 1
    return counts, _chunk_stats_end()


def calibrate(cfg, out_dir):
    """Learn a letter-substitution confusion matrix from the current report's
    high-confidence, context-verified findings. The corpus teaches us which
    substitutions actually happen (ד/ר, ט/מ...) and how often; the next
    `detect` run uses these counts instead of the hand-written pair list."""
    import json
    # coverage NOT checked: calibrate only learns letter weights from the last
    # complete report.db, and the UI runs it as the first step of a rescan —
    # refusing it would block the very rescan that repairs a failed `locate`
    con = sqlite3.connect(_require(out_dir, REPORT_DB_F, 'כיול',
                                   check_coverage=False))
    pairs = Counter()
    for w, s in con.execute(
            "SELECT DISTINCT word, suggestion FROM occurrences_full "
            "WHERE errtype = 'edit1_sub' AND ctx_hits > 0 AND score >= 4"):
        r = _dld1(w, s)
        if r and r[0] == 'sub':
            pairs[r[1] + r[2]] += 1
    path = os.path.join(out_dir, 'confusion_learned.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(dict(pairs), f, ensure_ascii=False, indent=1)
    print(f'[calibrate] {len(pairs)} substitution pairs from '
          f'{sum(pairs.values()):,} confirmed findings -> {path}', flush=True)
    for p, c in pairs.most_common(15):
        print(f'    {p[0]} -> {p[1]}: {c:,}', flush=True)

    # per-book OCR profiles: a book with many confirmed substitutions of the
    # same letter pair has a proven systematic OCR fault; the next `locate`
    # rescans such books with heightened sensitivity for exactly those pairs
    profiles = {}
    try:
        it = con.execute(
            "SELECT word, suggestion, doc FROM occurrences_full "
            "WHERE errtype = 'edit1_sub' AND score >= 4 "
            "AND (ctx_hits > 0 OR sugg_local >= 3)")
    except sqlite3.OperationalError:      # older report.db without doc
        it = ()
    for w, s, doc in it:
        r = _dld1(w, s)
        if r and r[0] == 'sub':
            profiles.setdefault(doc, Counter())[r[1] + r[2]] += 1
    profiles = {d: {p: c for p, c in cnt.items() if c >= cfg.ocr_pair_min}
                for d, cnt in profiles.items()}
    profiles = {d: ps for d, ps in profiles.items() if ps}
    con.close()
    ppath = os.path.join(out_dir, 'ocr_profiles.pkl')
    with open(ppath, 'wb') as f:
        pickle.dump(profiles, f, protocol=4)
    n_pairs = sum(len(v) for v in profiles.values())
    print(f'[calibrate] OCR profiles: {len(profiles)} books, '
          f'{n_pairs} systematic pairs -> {ppath}', flush=True)


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
    with open(_require(out_dir, LEXICON_F, 'איתור'), 'rb') as f:
        freq = pickle.load(f)
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
    learned = {}
    conf_path = os.path.join(out_dir, 'confusion_learned.json')
    if os.path.exists(conf_path):
        import json
        with open(conf_path, encoding='utf-8') as f:
            learned = json.load(f)
        print(f'[detect] learned confusion matrix: {len(learned)} pairs',
              flush=True)

    common = {w: f for w, f in freq.items()
              if f >= cfg.common_min and not is_abbrev(w) and 2 <= len(w) <= 18}
    del_index = {}
    for w in common:                     # SymSpell-style deletion index
        for i in range(len(w)):
            del_index.setdefault(w[:i] + w[i + 1:], []).append(w)
    print(f'[detect] common={len(common):,}  del-index={len(del_index):,}  '
          f'({time.time()-t0:.0f}s)', flush=True)

    # abbreviations that lost their gershayim: map רמבם -> רמב"ם
    abbrev_map = {}
    for a, fa in freq.items():
        if fa >= cfg.common_min and is_abbrev(a):
            k = a.replace('"', '').replace("'", '')
            if len(k) >= 2 and (k not in abbrev_map
                                or freq[abbrev_map[k]] < fa):
                abbrev_map[k] = a

    errors = {}       # word -> (freq, errtype, suggestion, sugg_freq, score)
    split_cands = {}  # word -> (parts_tuple, strong)

    def record(w, fw, errtype, sugg, fs, score):
        cur = errors.get(w)
        if cur is None or score > cur[4]:
            errors[w] = (fw, errtype, sugg, fs, score)

    def add_split(w, parts, strong):
        cur = split_cands.get(w)
        if cur is None or (strong and not cur[1]):
            split_cands[w] = (tuple(parts), strong)

    n_scanned = 0
    for w, fw in freq.items():
        if w in whitelist:
            continue
        # non-final letter at word end (checked up to medium frequency)
        if len(w) >= 3 and w[-1] in TO_FINAL and not is_abbrev(w) and fw <= 20:
            wf = w[:-1] + TO_FINAL[w[-1]]
            ff = freq.get(wf, 0)
            if ff >= cfg.part_min and ff >= 50 * fw:
                record(w, fw, 'nonfinal_end', wf, ff, 4 + math.log10(ff / fw))

        if fw > cfg.rare_max or len(w) < 3 or is_abbrev(w):
            continue
        n_scanned += 1
        if n_scanned % 200000 == 0:
            print(f'  [detect] scanned {n_scanned:,} rare types '
                  f'({time.time()-t0:.0f}s)', flush=True)

        # 1) final-form letter in the middle of a word
        mid_final = [i for i, ch in enumerate(w[:-1]) if ch in FINALS]
        if mid_final:
            handled = False
            for i in mid_final:          # perhaps a missing space at that point
                a, b = w[:i + 1], w[i + 1:]
                if freq.get(a, 0) >= 10 and freq.get(b, 0) >= 10 and len(b) >= 2:
                    add_split(w, (a, b), True)
                    handled = True
            if not handled:              # perhaps final form instead of regular
                w2 = ''.join(FROM_FINAL.get(ch, ch) if i < len(w) - 1 else ch
                             for i, ch in enumerate(w))
                f2 = freq.get(w2, 0)
                if f2 >= cfg.common_min:
                    record(w, fw, 'final_midword', w2, f2, 5 + math.log10(f2))
                else:
                    record(w, fw, 'final_midword', '', 0, 3.0)

        # 2) missing space — verified against corpus bigrams afterwards
        if len(w) >= 5 and w not in split_cands:
            seg = _segment(w, freq, cfg)
            if seg:
                parts, _ = seg
                exp = N * math.prod(freq[p] / N for p in parts)
                if exp >= cfg.exp_prefilter:
                    add_split(w, parts, False)

        # 3) abbreviation that lost its gershayim (רמבם -> רמב"ם)
        ab = abbrev_map.get(w)
        if ab and freq[ab] >= cfg.ed1_ratio * fw:
            record(w, fw, 'lost_quotes', ab, freq[ab],
                   5 + math.log10(freq[ab] / fw))

        # 4) frequent stem + inflection suffix — legitimate morphology, so
        # letter-level flagging is skipped entirely
        if _frequent_stem(w, freq, cfg.common_min):
            continue

        # 5) edit distance 1 from a frequent word
        cands = set()
        if w in del_index:
            cands.update(del_index[w])
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            if d in del_index:
                cands.update(del_index[d])
            if freq.get(d, 0) >= cfg.common_min and len(d) >= 2:
                cands.add(d)
        best_c, best_score, best_kind, best_ch = None, 0.0, '', ''
        for c in cands:
            if c == w:
                continue
            fc = freq.get(c, 0)
            if fc < cfg.common_min or fc < cfg.ed1_ratio * fw:
                continue
            r = _dld1(w, c)
            if r is None:
                continue
            kind, ch_a, ch_b, pos = r
            if kind == 'ins':
                # a doubled letter (למללך, נגמרר) is always suspicious — the
                # morphology exemptions below never apply to it
                doubled = ((pos > 0 and w[pos - 1] == ch_a)
                           or (pos + 1 < len(w) and w[pos + 1] == ch_a))
                # "extra" letter within the leading prefix cluster (דלאליעזר =
                # ד+ל+אליעזר, ובהבעל = ו+ב+הבעל) or a trailing inflection
                # suffix (מזדעזעה) — legitimate morphology, not a typo
                if (not doubled and pos <= 2 and ch_a in PREFIX_LETTERS
                        and all(c in PREFIX_LETTERS for c in w[:pos])):
                    continue
                if not doubled and pos == len(w) - 1 and ch_a in SUFFIX_LETTERS:
                    continue
            elif kind == 'sub' and pos == 0 \
                    and ch_a in PREFIX_LETTERS and ch_b in PREFIX_LETTERS:
                # א/ה/ד... swapped at word start is usually Hebrew vs Aramaic
                # prefix variation (אידועים = אַ+ידועים), not a typo
                continue
            score = math.log10(fc / fw)
            if kind == 'sub':
                lc = learned.get(ch_a + ch_b, 0)
                if lc:
                    score += min(2.5, 0.5 + 0.8 * math.log10(1 + lc))
                elif not learned and (ch_a, ch_b) in CONFUSABLE:
                    score += 2
            elif kind in ('ins', 'del') and ch_a in 'וי':
                score += 1
            elif kind == 'swap':
                score += 1
            if score > best_score:
                best_c, best_score, best_kind, best_ch = c, score, kind, ch_a
        if best_c:
            # extra/missing ו or י is usually ktiv male/chaser variation, not
            # a typo — reported separately so the user can decide policy
            if best_kind in ('ins', 'del') and best_ch in 'וי':
                errtype = 'spelling_variant'
            else:
                errtype = f'edit1_{best_kind}'
            record(w, fw, errtype, best_c, freq[best_c], best_score)

    # --- verify split candidates against the corpus -----------------------
    cand_list = [(w, parts) for w, (parts, _) in split_cands.items()]
    print(f'[detect] split candidates to verify: {len(cand_list):,}  '
          f'({time.time()-t0:.0f}s)', flush=True)
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
    n_ok = 0
    for idx, (w, parts) in enumerate(cand_list):
        obs = counts.get(idx, 0)
        if not obs:
            continue
        strong = split_cands[w][1]
        minlen = min(len(p) for p in parts)
        exp = N * math.prod(freq[p] / N for p in parts)
        if strong:
            ok = obs >= 1
        elif minlen >= 3:
            ok = obs >= cfg.split_obs_min
        else:
            ok = obs >= cfg.split_obs_min_short and obs >= 2 * exp
        if ok:
            record(w, freq[w], 'missing_space', ' '.join(parts), obs,
                   4 + math.log10(obs) + (2 if strong else 0)
                   + (1 if minlen >= 3 else 0))
            n_ok += 1
    print(f'[verify] confirmed {n_ok:,}/{len(cand_list):,} splits', flush=True)
    _fail_if_partial('detect', vstats, out_dir)

    _replace_atomically(os.path.join(out_dir, FLAGGED_F), _dump_pickle(errors))
    _write_coverage(out_dir, 'detect', vstats)
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


def _tanach_check(db_path, all_occ, flagged, lstats, out_dir, passes):
    """Compare occurrences with the verse they quote.

    Returns ``(kept, tan_info, matches, edition_rows, evidence_rows)``.
    An occurrence leaves the main report only when >= 2 independent sources
    read exactly that word at the aligned verse position.

    The index read is recorded as the ``tanach_index`` pass of `locate`; a
    partial read stops the stage here, before report.db is touched."""
    t1 = time.time()
    tstats = passes['tanach_index'] = ReadStats()
    vidx = _build_verse_index(db_path, tstats)
    _fail_if_partial('locate', lstats, out_dir, passes=passes)
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

def _locate_chunk(chunk):
    _chunk_stats_begin()
    freq, flagged, cfg = _W['lexicon'], _W['flagged'], _W['cfg']
    profiles = _W.get('ocr_profiles') or {}
    occ, joins, ocr = [], [], []
    from .normalize import TOKEN_RE, clean
    for uid, doc, content in _W['corpus'].iter_texts_docs(chunk):
        text = clean(content)
        toks = list(TOKEN_RE.finditer(text))
        # skip foreign-language / heavily garbled lines: too many of their
        # words are uncommon for point-fixes to be meaningful. Tokens of 1-2
        # letters are ignored — they are frequent in any language and would
        # dilute the ratio.
        real = [m.group() for m in toks if len(m.group()) >= 3]
        if len(real) >= 5:
            uncommon = sum(1 for t in real if freq.get(t, 0) < cfg.common_min)
            if uncommon > cfg.foreign_ratio * len(real):
                continue
        # pass 1 — extra space: adjacent pair whose concatenation is a
        # frequent word. Tokens explained this way are excluded from
        # letter-level reporting below, so one textual error yields exactly
        # one report row (הצ פרדעים -> extra-space only, not also edit1 on
        # the פרדעים fragment).
        joined_idx = set()
        for k, m in enumerate(toks[:-1]):
            w = m.group()
            if is_abbrev(w):
                continue
            nx = toks[k + 1]
            b = nx.group()
            # the gap must be pure whitespace — a bracket, hyphen or
            # asterisk between the tokens means editorial markup
            # (בחשבונ(י)ך, ל-נקותם), not an accidental space
            if (nx.start() - m.end() <= 1 and not is_abbrev(b)
                    and not text[m.end():nx.start()].strip()):
                mn = min(freq.get(w, 0), freq.get(b, 0))
                if mn <= 3:
                    fj = freq.get(w + b, 0)
                    if fj >= cfg.join_min and fj >= 30 * max(1, mn):
                        s = m.start()
                        joins.append((uid, w, b, w + b, fj,
                                      text[max(0, s - 45):nx.end() + 45].strip()))
                        joined_idx.update((k, k + 1))

        # pass 2 — occurrences of flagged word types
        prof = profiles.get(doc)
        for k, m in enumerate(toks):
            w = m.group()
            # OCR-profile layer: in books with a proven systematic
            # substitution, scan even words above the global rarity cutoff
            # for exactly the profiled letter pairs
            if (prof and w not in flagged and 3 <= len(w) <= 15
                    and not is_abbrev(w)):
                fw = freq.get(w, 0)
                if 1 <= fw <= cfg.ocr_rare_max:
                    hit = None
                    for pair in prof:
                        a, b = pair[0], pair[1]
                        i0 = w.find(a)
                        while i0 != -1:
                            w2 = w[:i0] + b + w[i0 + 1:]
                            f2 = freq.get(w2, 0)
                            if (f2 >= cfg.common_min
                                    and f2 >= cfg.ocr_ratio * fw
                                    and (hit is None or f2 > hit[1])):
                                hit = (w2, f2)
                            i0 = w.find(a, i0 + 1)
                    if hit:
                        s, e = m.start(), m.end()
                        # skip abbreviations (trailing geresh) and editorial
                        # notation, as in the flagged-word path
                        if not (e < len(text) and text[e] in '\'"([{*&') \
                                and not (s > 0 and text[s - 1] in ')]}*&'):
                            ocr.append((w, fw, hit[0], hit[1], uid, doc,
                                        text[max(0, s - 45):e + 45].strip()))
            if w in flagged and k not in joined_idx:
                s, e = m.start(), m.end()
                # a trailing geresh marks an abbreviation (וכוננ' = וכוננה);
                # an adjacent bracket/asterisk marks editorial notation
                # (קיצ[ו]ר, תקינ(?)) — neither is a broken word
                if e < len(text) and text[e] in '\'"([{*&':
                    continue
                if s > 0 and text[s - 1] in ')]}*&':
                    continue
                prev = toks[k - 1].group() if k else ''
                nxt = toks[k + 1].group() if k + 1 < len(toks) else ''
                # a rare word right after a title (ר' פלוני, אבד"ק פלוני)
                # is a person/place name, not a typo
                if flagged[w][1] not in ('missing_space', 'final_midword') \
                        and (prev in NAME_TRIGGERS
                             or (k >= 2 and toks[k - 2].group() in NAME_TRIGGERS)):
                    continue
                occ.append((w, uid, doc, prev, nxt,
                            text[max(0, s - 45):e + 45].strip()))
    return occ, joins, ocr, _chunk_stats_end()


def _ctx_count_chunk(chunk):
    """One corpus pass that feeds both verification signals:
    * ctx: how often does (neighbor, correction) occur as an adjacent pair?
    * local: how often does each proposed correction occur in the same book
      as the flagged word?"""
    _chunk_stats_begin()
    pairs, first = _W['ctx_pairs'], _W['ctx_first']
    book_need = _W['book_need']
    counts, local = Counter(), Counter()
    for _, doc, text in _W['corpus'].iter_texts_docs(chunk):
        toks = tokenize(text)
        need = book_need.get(doc)
        for i, t in enumerate(toks):
            if need is not None and t in need:
                local[(doc, t)] += 1
            if t in first and i + 1 < len(toks):
                p = (t, toks[i + 1])
                if p in pairs:
                    counts[p] += 1
    return counts, local, _chunk_stats_end()


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
    with open(_require(out_dir, FLAGGED_F, 'מיקום'), 'rb') as f:
        flagged = pickle.load(f)
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)

    all_occ, all_joins, all_ocr = [], [], []
    lstats = ReadStats()
    loads = {'lexicon': os.path.join(out_dir, LEXICON_F),
             'flagged': os.path.join(out_dir, FLAGGED_F)}
    prof_path = os.path.join(out_dir, 'ocr_profiles.pkl')
    if os.path.exists(prof_path):
        loads['ocr_profiles'] = prof_path
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
    _fail_if_partial('locate', lstats, out_dir)

    # --- Tanach reference check (Otzaria only) ----------------------------
    # Verified verse matches go to a separate review file (the reference
    # itself might be wrong); a differing verse reading is stored next to the
    # detector's suggestion as an alternative, never in place of it.
    tanach_matches, tanach_errors_rows, tanach_evidence = [], [], []
    tan_info = None
    if spec.get('preset') == 'otzaria':
        (all_occ, tan_info, tanach_matches, tanach_errors_rows,
         tanach_evidence) = _tanach_check(spec['path'], all_occ, flagged,
                                          lstats, out_dir, passes)
        print(f'[tanach] verse matches (separate review file): '
              f'{len(tanach_matches):,}  edition variants: '
              f'{len(tanach_errors_rows):,}', flush=True)

    # words whose (very few) occurrences all sit in a single book are usually
    # the author's own idiosyncratic spelling, not typos
    LOCAL_TYPES = ('edit1_sub', 'edit1_ins', 'edit1_del', 'edit1_swap',
                   'nonfinal_end', 'spelling_variant', 'lost_quotes')
    w_count, w_docs = Counter(), {}
    for w, _, doc, _, _, _ in all_occ:
        w_count[w] += 1
        w_docs.setdefault(w, set()).add(doc)
    repeat_words = {w for w, n in w_count.items()
                    if n >= 2 and len(w_docs[w]) == 1
                    and flagged[w][1] in LOCAL_TYPES}
    print(f'[locate] same-book-repeat words suppressed: {len(repeat_words):,}',
          flush=True)

    # context verification for edit-distance-1 suggestions (does the corrected
    # word occur next to the same neighbors elsewhere?) + book-local counts
    # (does the corrected word occur in this very book?)
    ctx_pairs, book_need = set(), {}
    for w, uid, doc, prev, nxt, _ in all_occ:
        fr = flagged[w]
        if fr[1].startswith('edit1') or fr[1] == 'spelling_variant':
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
            for i, (c, lc, st) in enumerate(
                    pool.imap_unordered(_ctx_count_chunk, chunks), 1):
                ctx_counts.update(c)
                local_counts.update(lc)
                cstats.add(ReadStats.from_dict(st))
                if i % 6 == 0:
                    print(f'  [context] chunk {i}/{len(chunks)} '
                          f'({time.time()-t0:.0f}s)', flush=True)
        _fail_if_partial('locate', lstats, out_dir, passes=passes)

    # --- write the report database ---------------------------------------
    # built under a temp name and moved into place only once complete, so a
    # failed run never replaces the last good report.db
    db_path = os.path.join(out_dir, REPORT_DB_F)
    tmp_path = db_path + '.tmp'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    con = sqlite3.connect(tmp_path)
    try:
        con.executescript('''
        CREATE TABLE errors(word TEXT PRIMARY KEY, freq INT, errtype TEXT,
                            suggestion TEXT, sugg_freq INT, score REAL);
        CREATE TABLE occurrences(word TEXT, unit TEXT, doc TEXT, ctx_hits INT,
                                 sugg_local INT, book_repeat INT, tanach INT,
                                 tanach_sugg TEXT, snippet TEXT);
        CREATE TABLE space_errors(unit TEXT, part1 TEXT, part2 TEXT,
                                  joined TEXT, join_freq INT, snippet TEXT);
        CREATE TABLE tanach_matches(word TEXT, unit TEXT, doc TEXT,
                                    snippet TEXT, evidence TEXT);
        CREATE TABLE tanach_errors(unit TEXT, word TEXT, canonical TEXT,
                                   snippet TEXT, evidence TEXT);
        ''')
        write_scan_meta(con, spec)
        con.executemany('INSERT OR REPLACE INTO errors VALUES(?,?,?,?,?,?)',
                        [(w, *v) for w, v in flagged.items()])
        rows = []
        for j, (w, uid, doc, prev, nxt, snip) in enumerate(all_occ):
            fr = flagged[w]
            hits = 0
            if fr[1].startswith('edit1') or fr[1] == 'spelling_variant':
                sugg = fr[2]
                hits = (ctx_counts.get((prev, sugg), 0)
                        + ctx_counts.get((sugg, nxt), 0))
            local = (local_counts.get((doc, fr[2]), 0)
                     if fr[1] in LOCAL_TYPES else 0)
            tan, tsugg = tan_info[j] if tan_info else (0, '')
            rows.append((w, uid, doc, hits, local,
                         1 if w in repeat_words else 0, tan, tsugg, snip))
        con.executemany('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?)',
                        rows)
        # OCR-profile findings: word-level entry + occurrence rows
        for w, fw, sugg, fs, uid, doc, snip in all_ocr:
            if w not in flagged:
                con.execute('INSERT OR IGNORE INTO errors '
                            'VALUES(?,?,?,?,?,?)',
                            (w, fw, 'ocr_profile', sugg, fs,
                             2 + math.log10(fs / max(fw, 1))))
                con.execute('INSERT INTO occurrences '
                            'VALUES(?,?,?,?,?,?,?,?,?)',
                            (w, uid, doc, 0, 0, 0, 0, '', snip))
        if all_ocr:
            print(f'[locate] ocr-profile findings: {len(all_ocr):,}',
                  flush=True)
        con.executemany('INSERT INTO tanach_matches VALUES(?,?,?,?,?)',
                        tanach_matches)
        con.executemany('INSERT INTO tanach_errors VALUES(?,?,?,?,?)',
                        tanach_errors_rows)
        tanach.write_evidence(con, tanach_evidence)
        con.executemany('INSERT INTO space_errors VALUES(?,?,?,?,?,?)',
                        all_joins)
        con.commit()
        corpus.enrich(con)
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
    _write_coverage(out_dir, 'locate', lstats, passes=passes)
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
# proposed correction and the word is not an in-book spelling convention
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
    tan_col = ("COALESCE(tanach_reading, '')" if 'tanach_reading' in have
               else "''")
    cols = ('word, suggestion, {tan}, ROUND({rank}, 2), ctx_hits, '
            'sugg_local, source, ref, unit, snippet').format(
                rank=RANK_SQL, tan=tan_col)
    header = ['word', 'suggestion', 'tanach_reading', 'rank', 'ctx_hits',
              'sugg_local', 'source', 'ref', 'unit', 'snippet']
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
    con = sqlite3.connect(_require(out_dir, REPORT_DB_F, 'דוחות'))
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
