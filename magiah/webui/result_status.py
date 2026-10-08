# -*- coding: utf-8 -*-
"""Are the findings on screen those of the latest scan? (UI_SPEC §9f)

The review UI shows ui_review.db, imported from report.db. A scan that fails
never replaces report.db, so after a failed, partial, cancelled or killed
scan the old results are still there, complete and readable — and nothing
used to say that they are not the latest scan's. The answer now comes from
the files, not from the server's memory, so it survives a restart and a
reload, and it disappears by itself once a scan succeeds.

Latest is not the same as complete: a scan allowed to skip unreadable input
rows (``--allow-unread``) succeeds, and its findings are the latest but rest
on gaps. That is said too (``accepted_partial``), never as stale.

:func:`build` returns one structure, ``result_status``::

    {'stale': bool,          # the results are not from the latest scan
     'results_at': str|None, # when the results shown were produced
     'notices': [{'kind', 'level', 'stale', 'title', 'text', 'hint',
                  'details', 'action', 'action_label'}, ...]}

`level` is error / warning / info; `hint` says what to do (or None);
`details` is the full reason (or None);
`action` names the button the banner offers — 'scan' / 'book_scan' (open
that part of the scan panel), 'refresh' (reload the findings) or None — and
`action_label` is its text. Every notice comes from one provider
in :data:`PROVIDERS`; another kind of warning about the results is one more
provider, and the banner shows it with no further change.
"""
import json
import os
import time

from .. import core, runstate
from . import hebrew

T = hebrew.RESULT_STATUS
LEVELS = ('error', 'warning', 'info')
# books named in the text of the failed-book-scans notice (all are in details)
BOOKS_NAMED = 3


def _clock(epoch):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(epoch))


def _minute(stamp):
    """'YYYY-MM-DD HH:MM:SS' -> 'YYYY-MM-DD HH:MM' (unknown -> '?')."""
    return (stamp or '?')[:16]


def _meta(con, key):
    row = con.execute('SELECT value FROM meta WHERE key = ?',
                      (key,)).fetchone()
    return row[0] if row else None


def _report_mtime(outdir):
    try:
        return os.path.getmtime(os.path.join(outdir, core.REPORT_DB_F))
    except OSError:
        return None


def _imported_mtime(con):
    """The mtime of the report.db the findings on screen were imported from
    (recorded by every import), or None — a ui_review.db imported before it
    was recorded. Nothing is guessed: ``last_import`` also moves with every
    single-book merge, so it cannot date the full scan's results.
    """
    try:
        return float(_meta(con, 'report_mtime'))
    except (TypeError, ValueError):
        return None


def _notice(kind, level, title, text, hint=None, details=None, action=None,
            stale=False):
    return {'kind': kind, 'level': level, 'stale': stale, 'title': title,
            'text': text, 'hint': hint, 'details': details, 'action': action,
            'action_label': T[f'action_{action}'] if action else None}


def _stage_he(stage):
    return hebrew.STAGE_LABELS.get(stage, {}).get('hebrew', stage or '?')


def _refused_stage(outdir):
    """``(stage, message)`` of the first stage of the results chain whose
    output the CLI refuses for a partial read — ``(None, None)`` if none.
    Only an output the CLI refuses outright makes the results stale; one
    written partial under ``--allow-unread`` came from a run that succeeded
    (:func:`coverage_notice` tells of its gaps) and must not show up here."""
    for stage in core._READ_STAGES:
        problem = core.failed_coverage(outdir, stage)
        if problem:
            return stage, problem
    return None, None


def stale_notice(con, outdir, ctx):
    """The results are not those of the latest scan.

    The run record (magiah.runstate) is the evidence: it also covers a scan
    that crashed or was killed. A folder without one — or whose record finds
    nothing — still has its coverage files, which the CLI itself refuses to
    build on (:func:`magiah.core.coverage_problem`); the UI agrees with it.
    """
    p = runstate.scan_problem(outdir)
    if p:
        state = p['state']
        what = T['stale_what'][state].format(
            started=_minute(p['started_at']), stage=_stage_he(p['stage']),
            path=p['path'])
        details = p['reason']
    else:
        stage, details = _refused_stage(outdir)
        if not stage:
            return None
        state = 'partial'
        what = T['stale_what']['coverage'].format(stage=_stage_he(stage))
    if ctx['results_at'] and ctx['has_results']:
        shown = T['stale_shown'].format(results_at=ctx['results_at'])
    elif ctx['results_at']:
        # a scan did complete — and found nothing
        shown = T['stale_shown_empty'].format(results_at=ctx['results_at'])
    elif ctx['has_results']:
        shown = T['stale_shown_undated']
    else:
        shown = T['stale_none']
    text = ' '.join([what, shown]
                    + ([T['stale_keep']] if ctx['has_results'] else []))
    title = T['stale_title'][state]
    if ctx['has_results']:
        shown_t = T['stale_title_shown']
        title += shown_t.get(state, shown_t['default'])
    return _notice('scan_incomplete', 'error', title, text, T['stale_todo'],
                   details, 'scan', stale=True)


def _meta_json(con, key):
    """A JSON object stored in `meta`, or None (absent or unreadable)."""
    try:
        value = json.loads(_meta(con, key) or 'null')
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def coverage_notice(con, outdir, ctx):
    """The findings on screen rest on input rows a scan skipped as
    unreadable, on explicit request (``--allow-unread``): the full scan's
    (``meta.coverage``, copied from report.db by every import) and each
    single-book scan's (``meta.book_coverage``, per book).

    Not stale: a run accepted partial succeeded, and its results are the
    latest — with known gaps. It shows beside the stale notice when a later
    scan failed, since the findings shown are then still these; and it goes
    away only when the findings shown no longer rest on skipped rows.
    """
    C = hebrew.COVERAGE_NOTICE
    scan = _meta_json(con, 'coverage')
    books = {d: b for d, b in (_meta_json(con, 'book_coverage') or {}).items()
             if isinstance(b, dict)}
    if not scan and not books:
        return None

    def count(rec, key):
        value = rec.get(key)
        return value if isinstance(value, int) else None

    def num(rec, key):
        value = count(rec, key)
        return '?' if value is None else f'{value:,}'

    def kind(rec):
        k = rec.get('unread_kind')
        return k if k in ('db', 'files', 'mixed') else 'input'

    def what(rec):
        return C['what'][kind(rec)].format(rows=num(rec, 'unread_rows'))

    text, refs, listed_all = [], [], True
    if scan:
        inherited = scan.get('inherited')
        text.append(C['scan'].format(what=what(scan), why=C['why'][kind(scan)],
                                     limit=num(scan, 'allow_unread'))
                    + (C['scan_lexicon'] if isinstance(inherited, dict)
                       and 'lexicon' in inherited else ''))
    for doc, b in sorted(books.items(),
                         key=lambda kv: str(kv[1].get('title') or kv[0])):
        text.append(C['book_lexicon' if b.get('inherited') else 'book']
                    .format(title=b.get('title') or doc, what=what(b),
                            limit=num(b, 'allow_unread')))
    # titled and advised by what was skipped, across every record
    kinds = set()
    for rec in ([scan] if scan else []) + list(books.values()):
        kinds |= {'mixed': {'db', 'files'}}.get(kind(rec), {kind(rec)})
    overall = kinds.pop() if len(kinds) == 1 else 'input'
    for rec in ([scan] if scan else []) + list(books.values()):
        got = [r for r in rec.get('unread_refs') or () if isinstance(r, dict)]
        refs += got
        rows = count(rec, 'unread_rows')
        listed_all = listed_all and rows is not None and len(got) >= rows
    lines, seen = [], set()
    for r in refs:
        if r.get('unit') not in seen:
            seen.add(r.get('unit'))
            lines.append(core.ref_text(r))
    if not listed_all:
        lines.append(C['more'])
    return _notice('accepted_partial', 'warning', C['title'][overall],
                   ' '.join(text), C['hint'][overall],
                   '\n'.join(lines) or None)


def refresh_notice(con, outdir, ctx):
    """report.db changed after the findings shown were imported from it."""
    stored = _meta(con, 'report_mtime')
    current = _report_mtime(outdir)
    if stored is None or current is None:
        return None          # never claimed without a recorded import
    try:
        if abs(float(stored) - current) < 1e-6:
            return None
    except ValueError:
        return None
    return _notice('refresh_needed', 'info', T['refresh_title'],
                   T['refresh_text'].format(report_at=_clock(current),
                                            results_at=ctx['results_at']
                                            or '?'),
                   action='refresh')


def _db_title(con, key):
    """The title the findings give the database book `key`, or None (a
    failed scan never learned it; an id alone tells a person nothing)."""
    key = str(key).strip()
    if not key.isdigit():
        return None
    row = con.execute('SELECT source FROM findings WHERE doc = ? AND '
                      "source IS NOT NULL AND source != '' LIMIT 1",
                      (str(int(key)),)).fetchone()
    return row[0] if row else None


def _book_label(con, p, short=False):
    """A book named in the banner: its title once a scan learned it, else
    the key it was asked for (`short`: a path's file name only) — isolated,
    since a key is often a path, which the surrounding right-to-left text
    would otherwise scramble."""
    key = str(p['key'])
    if p['title']:
        name = p['title']
    elif p['source'] == 'db':
        name = _db_title(con, key) or T['book_db_key'].format(key=key)
    else:
        name = key.replace('\\', '/').rsplit('/', 1)[-1] if short else key
    return '⁨' + name + '⁩'


def _todo(base, problems):
    """What to do, plus — when a full scan reads one of the books — that a
    successful full scan clears the notice too."""
    corpus = any(str(p.get('book')).startswith(runstate._CORPUS_BOOKS)
                 for p in problems)
    return T[base] + (T['book_todo_full'] if corpus else '')


def book_notice(con, outdir, ctx):
    """Books whose latest single-book scan failed or was cut off — one
    notice for all of them, each book listed once. Such a scan changed
    nothing (its merge is one transaction), so the full results are not
    stale, and a later successful scan of a book takes only that book off."""
    problems = runstate.book_problems(outdir)
    if not problems:
        return None
    if len(problems) == 1:
        (p,) = problems
        what = T['book_what'][p['state']].format(
            book=_book_label(con, p), started=_minute(p['started_at']))
        return _notice('book_scan_incomplete', 'warning', T['book_title'],
                       what + ' ' + T['book_after'],
                       _todo('book_todo', problems),
                       p['reason'], 'book_scan')
    shown = problems[:BOOKS_NAMED]
    books = ', '.join(f'«{_book_label(con, p, short=True)}»' for p in shown)
    if len(problems) > len(shown):
        books += ' ' + T['books_more'].format(n=len(problems) - len(shown))
    details = '\n\n'.join(
        T['books_detail'][p['state']].format(
            book=_book_label(con, p), started=_minute(p['started_at']))
        + (':\n' + p['reason'] if p['reason'] else '')
        for p in problems)
    return _notice('book_scan_incomplete', 'warning',
                   T['books_title'].format(n=len(problems)),
                   T['books_what'].format(books=books) + ' '
                   + T['books_after'], _todo('books_todo', problems), details,
                   'book_scan')


PROVIDERS = (stale_notice, coverage_notice, refresh_notice, book_notice)


def build(con, outdir):
    """The result status of `outdir` (`con`: its ui_review.db)."""
    imported = _imported_mtime(con)
    has_results = con.execute('SELECT 1 FROM findings LIMIT 1').fetchone() \
        is not None
    # dated by the recorded import alone: also an import of a scan that
    # found nothing, which is no "no scan yet"
    ctx = {'results_at': _clock(imported) if imported else None,
           'has_results': has_results}
    notices = [n for n in (p(con, outdir, ctx) for p in PROVIDERS) if n]
    notices.sort(key=lambda n: LEVELS.index(n['level']))
    return {'stale': any(n['stale'] for n in notices),
            'results_at': ctx['results_at'], 'notices': notices}
