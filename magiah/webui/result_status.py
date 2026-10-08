# -*- coding: utf-8 -*-
"""Are the findings on screen those of the latest scan? (UI_SPEC §9f)

The review UI shows ui_review.db, imported from report.db. A scan that fails
never replaces report.db, so after a failed, partial, cancelled or killed
scan the old results are still there, complete and readable — and nothing
used to say that they are not the latest scan's. The answer now comes from
the files, not from the server's memory, so it survives a restart and a
reload, and it disappears by itself once a scan succeeds.

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
import os
import time
from datetime import datetime

from .. import core, runstate
from . import hebrew

T = hebrew.RESULT_STATUS
LEVELS = ('error', 'warning', 'info')


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


def _imported_mtime(con, outdir):
    """The mtime of the report.db the findings on screen were imported from,
    or None if unknown.

    Recorded by every import (``meta.report_mtime``). A ui_review.db from
    before that has only ``last_import``; report.db then dates the results
    only if it has not changed since — otherwise nothing is claimed.
    """
    value = _meta(con, 'report_mtime')
    if value is not None:
        try:
            return float(value)
        except ValueError:
            return None
    current, last = _report_mtime(outdir), _meta(con, 'last_import')
    if current is None or last is None:
        return None
    try:
        return current if current <= datetime.fromisoformat(
            last).timestamp() else None
    except ValueError:
        return None


def _notice(kind, level, title, text, hint=None, details=None, action=None,
            stale=False):
    return {'kind': kind, 'level': level, 'stale': stale, 'title': title,
            'text': text, 'hint': hint, 'details': details, 'action': action,
            'action_label': T[f'action_{action}'] if action else None}


def _stage_he(stage):
    return hebrew.STAGE_LABELS.get(stage, {}).get('hebrew', stage or '?')


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
        for stage in core._READ_STAGES:
            details = core.coverage_problem(outdir, stage)
            if details:
                break
        else:
            return None
        state = 'partial'
        what = T['stale_what']['coverage'].format(stage=_stage_he(stage))
    if ctx['results_at']:
        shown = T['stale_shown'].format(results_at=ctx['results_at'])
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


def book_notice(con, outdir, ctx):
    """The latest single-book scan failed or was cut off. It changed nothing
    (its merge is one transaction), so the full results are not stale."""
    p = runstate.book_problem(outdir)
    if not p:
        return None
    # isolated: a book key is often a path, which the surrounding
    # right-to-left text would otherwise scramble
    what = T['book_what'][p['state']].format(
        book='\u2068' + (p['title'] or p['key']) + '\u2069',
        started=_minute(p['started_at']))
    return _notice('book_scan_incomplete', 'warning', T['book_title'],
                   what + ' ' + T['book_after'], T['book_todo'], p['reason'],
                   'book_scan')


PROVIDERS = (stale_notice, refresh_notice, book_notice)


def build(con, outdir):
    """The result status of `outdir` (`con`: its ui_review.db)."""
    imported = _imported_mtime(con, outdir)
    has_results = con.execute('SELECT 1 FROM findings LIMIT 1').fetchone() \
        is not None
    ctx = {'results_at': _clock(imported) if imported and has_results
           else None,
           'has_results': has_results}
    notices = [n for n in (p(con, outdir, ctx) for p in PROVIDERS) if n]
    notices.sort(key=lambda n: LEVELS.index(n['level']))
    return {'stale': any(n['stale'] for n in notices),
            'results_at': ctx['results_at'], 'notices': notices}
