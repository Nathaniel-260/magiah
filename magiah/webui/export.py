# -*- coding: utf-8 -*-
"""Exports: Excel workbook per origin (§7A) + legacy to_send/ files (§7B).

The legacy export is byte-format-compatible with the old review UI's
to_send/ output (same 8-column header, UTF-8 BOM, per-origin split) but is
sourced from the findings table directly, so book/ref/origin/snippet are
never lost to a failed join (fix for defect §8.1).
"""
import csv
import os
import re
from datetime import datetime

from . import hebrew
from .db import EFF, JOINS, UNIT_ORDER

FIXES_HEADER = ['word', 'suggestion', 'errtype', 'book', 'ref', 'line_id',
                'origin', 'snippet']

# Excel sheet names: max 31 chars, no : \ / ? * [ ]
_SHEET_BAD = re.compile(r'[:\\/?*\[\]]')


def _sheet_name(name, used):
    name = _SHEET_BAD.sub(' ', str(name)).strip() or 'גיליון'
    name = name[:31]
    base, i = name, 2
    while name.lower() in used:
        suffix = f' {i}'
        name = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(name.lower())
    return name


# ---------------------------------------------------------------------------
# §7B — legacy to_send/ export
# ---------------------------------------------------------------------------

def export_fixes(con, outdir):
    """Write to_send/approved_fixes_all.csv + approved_fixes_<origin>.csv
    (+ rejected_words.txt), old byte format, from the findings table.

    Fixes = findings whose effective status is approved or fixed; a user
    custom_suggestion wins over the automatic suggestion. Rejected words =
    word_rules 'not_error' + words of per-occurrence not_error findings.
    """
    send_dir = os.path.join(outdir, 'to_send')
    os.makedirs(send_dir, exist_ok=True)
    fixes = con.execute(f'''
        SELECT f.word, COALESCE(NULLIF(r.custom_suggestion, ''),
                                f.suggestion, '') AS suggestion,
               COALESCE(f.errtype, ''), COALESCE(f.source, ''),
               COALESCE(f.ref, ''), COALESCE(f.unit, ''),
               COALESCE(f.origin, ''), COALESCE(f.snippet, '')
        FROM findings f {JOINS}
        WHERE {EFF} IN ('approved', 'fixed')
        ORDER BY COALESCE(f.origin, ''), f.source,
                 ''' + UNIT_ORDER.format(u='f.unit') + '''
        ''').fetchall()
    locked = []

    def _write(path, rows):
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                wr = csv.writer(f)
                wr.writerow(FIXES_HEADER)
                wr.writerows(rows)
        except PermissionError:
            locked.append(path)

    _write(os.path.join(send_dir, 'approved_fixes_all.csv'), fixes)
    by_origin = {}
    for row in fixes:
        by_origin.setdefault(row[6] or 'Unknown', []).append(row)
    for org, rows in by_origin.items():
        safe = re.sub(r'[^\w.\-]+', '_', org)
        _write(os.path.join(send_dir, f'approved_fixes_{safe}.csv'), rows)

    rejected = sorted({r[0] for r in con.execute(
        "SELECT word FROM word_rules WHERE status = 'not_error'")} |
        {r[0] for r in con.execute('''
            SELECT f.word FROM findings f
            JOIN review v ON v.finding_id = f.id
            WHERE v.status = 'not_error' AND f.word IS NOT NULL''')})
    p2 = os.path.join(send_dir, 'rejected_words.txt')
    try:
        with open(p2, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rejected))
    except PermissionError:
        locked.append(p2)
    if locked:
        raise PermissionError(hebrew.MESSAGES['file_locked'] +
                              ', '.join(locked))
    return {'fixes': len(fixes), 'rejected': len(rejected),
            'origins': sorted(by_origin), 'dir': send_dir}


# ---------------------------------------------------------------------------
# §7A — Excel workbook per origin
# ---------------------------------------------------------------------------

MAIN_HEADERS = ['ספר', 'מראה מקום', 'סוג שגיאה', 'המילה במקור',
                'הצעת תיקון', 'ציון', 'מאומת', 'סטטוס', 'הערה',
                'קטע מהטקסט', 'מזהה שורה']

_ROW_SQL = f'''
    SELECT f.source, f.ref, f.errtype, f.word,
           COALESCE(NULLIF(r.custom_suggestion, ''), f.suggestion),
           f.rank, f.verified, {EFF}, r.note, f.snippet, f.unit
    FROM findings f {JOINS}
    WHERE f.origin = ?'''


def _fmt_row(r, with_errtype=True):
    out = [r[0] or '', r[1] or '']
    if with_errtype:
        out.append(hebrew.errtype_hebrew(r[2]))
    out += [r[3] or '', r[4] or '',
            r[5] if r[5] is not None else '',
            'כן' if r[6] else '',
            hebrew.status_hebrew(r[7]),
            r[8] or '', r[9] or '', r[10] or '']
    return out


def _all_rows(con, origin):
    cur = con.cursor()
    for r in cur.execute(_ROW_SQL + ' ORDER BY f.source, f.errtype, '
                                    'f.rank DESC, f.id', (origin,)):
        yield _fmt_row(r, with_errtype=True)


def _errtype_rows(con, origin, errtype):
    cur = con.cursor()
    for r in cur.execute(_ROW_SQL + ' AND f.errtype = ? ORDER BY f.source, '
                                    'f.rank DESC, f.id', (origin, errtype)):
        yield _fmt_row(r, with_errtype=False)


def _summary_rows(con, origin):
    """Sheet 1 'סיכום': errtype × status counts + top books + timestamp."""
    yield ['ייצוא: ' + datetime.now().strftime('%d/%m/%Y %H:%M'), '', '', '']
    yield ['מאגר: ' + hebrew.origin_hebrew(origin), '', '', '']
    yield ['', '', '', '']
    yield ['— סיכום לפי סוג שגיאה וסטטוס —', '', '', '']
    yield ['סוג שגיאה', 'סטטוס', 'כמות', '']
    for et, st, n in con.execute(f'''
            SELECT f.errtype, {EFF}, COUNT(*) FROM findings f {JOINS}
            WHERE f.origin = ? GROUP BY f.errtype, {EFF}
            ORDER BY COUNT(*) DESC''', (origin,)):
        yield [hebrew.errtype_hebrew(et), hebrew.status_hebrew(st), n, '']
    yield ['', '', '', '']
    yield ['— הספרים עם הכי הרבה ממצאים —', '', '', '']
    yield ['ספר', 'ממצאים', 'טופלו', '']
    for src, total, done in con.execute(f'''
            SELECT f.source, COUNT(*),
                   SUM(CASE WHEN {EFF} != 'pending' THEN 1 ELSE 0 END)
            FROM findings f {JOINS} WHERE f.origin = ?
            GROUP BY f.source ORDER BY COUNT(*) DESC LIMIT 100''', (origin,)):
        yield [src or '', total, done or 0, '']


def export_xlsx(con, outdir, origin=None):
    """Write שגיאות_<origin>.xlsx to <outdir>/excel/ for one origin (or all
    origins when origin is None). Returns the list of written paths.

    Raises PermissionError (Hebrew message naming the locked files) if any
    target workbook is open in Excel — nothing is skipped silently.
    """
    from .xlsx import write_workbook  # written by the xlsx build agent
    excel_dir = os.path.join(outdir, 'excel')
    os.makedirs(excel_dir, exist_ok=True)
    if origin:
        origins = [origin]
    else:
        origins = [r[0] for r in con.execute(
            'SELECT DISTINCT origin FROM findings '
            "WHERE origin IS NOT NULL AND origin != '' "
            'ORDER BY origin')]
    paths, locked = [], []
    for org in origins:
        heb = hebrew.origin_hebrew(org)
        fname = 'שגיאות_' + re.sub(r'[^\w.\-]+', '_', heb) + '.xlsx'
        path = os.path.join(excel_dir, fname)
        used = set()
        sheets = [
            {'name': _sheet_name('סיכום', used),
             'headers': ['סיכום', '', '', ''],
             'rows': _summary_rows(con, org)},
            {'name': _sheet_name('כל השגיאות — לפי ספר', used),
             'headers': MAIN_HEADERS,
             'rows': _all_rows(con, org)},
        ]
        ets = [r[0] for r in con.execute(
            'SELECT errtype, COUNT(*) FROM findings WHERE origin = ? '
            'GROUP BY errtype ORDER BY COUNT(*) DESC', (org,))]
        per_et_headers = [h for h in MAIN_HEADERS if h != 'סוג שגיאה']
        for et in ets:
            sheets.append({
                'name': _sheet_name(hebrew.errtype_hebrew(et), used),
                'headers': per_et_headers,
                'rows': _errtype_rows(con, org, et)})
        try:
            write_workbook(path, sheets)
            paths.append(path)
        except PermissionError:
            locked.append(path)
    if locked:
        raise PermissionError(hebrew.MESSAGES['file_locked'] +
                              ', '.join(locked))
    return paths
