# -*- coding: utf-8 -*-
"""Exports: Excel workbook per origin (§7A) + legacy to_send/ files (§7B).

The legacy export is byte-format-compatible with the old review UI's
to_send/ output (same 8-column header, UTF-8 BOM, per-origin split) but is
sourced from the findings table directly, so book/ref/origin/snippet are
never lost to a failed join (fix for defect §8.1).
"""
import csv
import json
import os
import re
from datetime import datetime

from . import hebrew
from ..tanach import word_editions_text
from .db import EFF, EXT_JOIN, JOINS, UNIT_ORDER, WORD_DECIDER

# the first 8 columns are the old byte format; consumers reading by name or
# by position keep working, the decision's provenance is appended
FIXES_HEADER = ['word', 'suggestion', 'errtype', 'book', 'ref', 'line_id',
                'origin', 'snippet', 'approved_suggestion', 'decided_by',
                'edition']
# the header before `edition` (the Tanach edition holding the word) existed
_OLD_FIXES_HEADER = FIXES_HEADER[:10]
OCC_HEADER = ['word', 'suggestion', 'unit', 'doc', 'source', 'ref', 'origin',
              'scope', 'decided_by']
REPL_HEADER = ['word', 'suggestion', 'decided_by', 'updated_at']
CONV_HEADER = ['word', 'doc', 'source', 'decided_by', 'updated_at']
# files this exporter wrote last time; only these may ever be removed
MANIFEST_F = '.magiah_export_manifest.json'

# who made the decision behind a finding's effective status: '' while it is
# pending, 'unknown' for decisions recorded before this was tracked
DECIDER = f'''CASE
    WHEN r.status IS NOT NULL THEN COALESCE(x.decided_by, 'unknown')
    WHEN rb.status IS NOT NULL THEN COALESCE(rb.decided_by, 'unknown')
    WHEN rr.status IS NOT NULL THEN COALESCE(rr.decided_by, 'unknown')
    WHEN w.status IS NOT NULL THEN COALESCE({WORD_DECIDER}, 'unknown')
    ELSE '' END'''
# what an approved finding is corrected to: the user's own correction, else
# the suggestion as it was when approved (a re-scan may have changed it)
APPROVED_FIX = ("COALESCE(NULLIF(r.custom_suggestion, ''), "
                "x.approved_suggestion, f.suggestion, '')")
# the correction a row shows: what was approved while it stands approved,
# else the current one (a stale approval keeps its old approved_suggestion
# in review_ext, but the row now proposes something else and is pending)
SHOWN_FIX = (f"CASE WHEN {EFF} IN ('approved', 'fixed') THEN {APPROVED_FIX} "
             "ELSE COALESCE(NULLIF(r.custom_suggestion, ''), f.suggestion, "
             "'') END")
ACTOR_HEBREW = {'human': 'אדם', 'agent': 'סוכן', 'unknown': 'לא ידוע'}

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

def _read_manifest(send_dir):
    try:
        with open(os.path.join(send_dir, MANIFEST_F), encoding='utf-8') as f:
            names = json.load(f)
        return {n for n in names if isinstance(n, str)}
    except (OSError, ValueError):
        return set()


def _own_old_file(path):
    """A per-origin file from before the manifest existed: ours only when
    its header row is exactly the one this exporter writes."""
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            head = next(csv.reader(f), None)
    except (OSError, UnicodeDecodeError):
        return False
    return head in (FIXES_HEADER, _OLD_FIXES_HEADER, FIXES_HEADER[:8])


def export_fixes(con, outdir):
    """Write to_send/: approved_fixes_all.csv + approved_fixes_<origin>.csv
    (old 8 columns + approved_suggestion, decided_by) and the rejections,
    each in its own scope:

    * rejected_words.txt — global exclusions only (word rules 'not_error');
    * rejected_occurrences.csv — single occurrences marked not an error;
    * rejected_replacements.csv — rejected (word -> suggestion) pairs;
    * book_conventions.csv — words that are correct in one book.

    Fixes = findings whose effective status is approved or fixed; a user
    custom_suggestion wins over the automatic suggestion. A per-origin file
    this exporter wrote earlier but that now has no rows is removed (tracked
    in a manifest, so files the user put in to_send/ are never touched).
    """
    send_dir = os.path.join(outdir, 'to_send')
    os.makedirs(send_dir, exist_ok=True)
    fixes = con.execute(f'''
        SELECT f.word, {APPROVED_FIX} AS suggestion,
               COALESCE(f.errtype, ''), COALESCE(f.source, ''),
               COALESCE(f.ref, ''), COALESCE(f.unit, ''),
               COALESCE(f.origin, ''), COALESCE(f.snippet, ''),
               {APPROVED_FIX},
               {DECIDER}, f.extra
        FROM findings f {JOINS} {EXT_JOIN}
        WHERE {EFF} IN ('approved', 'fixed')
        ORDER BY COALESCE(f.origin, ''), f.source,
                 ''' + UNIT_ORDER.format(u='f.unit') + '''
        ''').fetchall()
    # a Tanach edition finding names the edition to correct (its line id is
    # the primary text's, shared by every edition of the verse)
    fixes = [(*r[:-1], word_editions_text(r[-1])) for r in fixes]
    # every file this export is responsible for, written or not (a file
    # locked in Excel is still ours and must stay in the manifest)
    locked, written = [], set()

    def _write(name, header, rows):
        path = os.path.join(send_dir, name)
        written.add(name)
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                wr = csv.writer(f)
                wr.writerow(header)
                wr.writerows(rows)
        except PermissionError:
            locked.append(path)

    _write('approved_fixes_all.csv', FIXES_HEADER, fixes)
    by_origin = {}
    for row in fixes:
        by_origin.setdefault(row[6] or 'Unknown', []).append(row)
    for org, rows in by_origin.items():
        safe = re.sub(r'[^\w.\-]+', '_', org)
        _write(f'approved_fixes_{safe}.csv', FIXES_HEADER, rows)

    rejected = sorted(r[0] for r in con.execute(
        "SELECT word FROM word_rules WHERE status = 'not_error'"))
    p2 = os.path.join(send_dir, 'rejected_words.txt')
    written.add('rejected_words.txt')
    try:
        with open(p2, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rejected))
    except PermissionError:
        locked.append(p2)
    occ = con.execute(f'''
        SELECT f.word, COALESCE(f.suggestion, ''), COALESCE(f.unit, ''),
               COALESCE(f.doc, ''), COALESCE(f.source, ''),
               COALESCE(f.ref, ''), COALESCE(f.origin, ''),
               COALESCE(x.scope, 'occurrence'),
               COALESCE(x.decided_by, 'unknown')
        FROM findings f JOIN review r ON r.finding_id = f.id {EXT_JOIN}
        WHERE r.status = 'not_error' AND f.word IS NOT NULL
          AND COALESCE(x.scope, 'occurrence') = 'occurrence'
        ORDER BY f.source, f.word, f.id''').fetchall()
    _write('rejected_occurrences.csv', OCC_HEADER, occ)
    repl = con.execute('''
        SELECT word, suggestion, COALESCE(decided_by, 'unknown'), updated_at
        FROM replacement_rules WHERE status = 'not_error'
        ORDER BY word, suggestion''').fetchall()
    _write('rejected_replacements.csv', REPL_HEADER, repl)
    conv = con.execute('''
        SELECT b.word, b.doc,
               COALESCE((SELECT f.source FROM findings f
                         WHERE f.doc = b.doc LIMIT 1),
                        CASE WHEN b.doc LIKE 'src:%'
                             THEN substr(b.doc, 5)
                             WHEN b.doc LIKE 'line:%'
                             THEN (SELECT f.source FROM findings f
                                   WHERE f.unit = substr(b.doc, 6)
                                     AND COALESCE(f.doc, '') = '' LIMIT 1)
                             ELSE '' END, ''),
               COALESCE(b.decided_by, 'unknown'), b.updated_at
        FROM book_rules b WHERE b.status = 'not_error'
        ORDER BY b.doc, b.word''').fetchall()
    _write('book_conventions.csv', CONV_HEADER, conv)

    # files we generated before that no longer have rows go away, so an
    # undone fix never lingers in a stale per-origin file
    if os.path.exists(os.path.join(send_dir, MANIFEST_F)):
        previous = _read_manifest(send_dir)
    else:
        # first export since the manifest exists: our earlier per-origin
        # files are recognised by name pattern and our own header row
        previous = {n for n in os.listdir(send_dir)
                    if n.startswith('approved_fixes_') and n.endswith('.csv')
                    and _own_old_file(os.path.join(send_dir, n))}
    for name in sorted(previous - written):
        path = os.path.join(send_dir, name)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            locked.append(path)
            written.add(name)          # still ours: retry on the next export
    try:
        with open(os.path.join(send_dir, MANIFEST_F), 'w',
                  encoding='utf-8') as f:
            json.dump(sorted(written), f, ensure_ascii=False)
    except OSError:
        pass
    if locked:
        raise PermissionError(hebrew.MESSAGES['file_locked'] +
                              ', '.join(locked))
    return {'fixes': len(fixes), 'rejected': len(rejected),
            'rejected_occurrences': len(occ),
            'rejected_replacements': len(repl),
            'book_conventions': len(conv),
            'origins': sorted(by_origin), 'dir': send_dir}


# ---------------------------------------------------------------------------
# §7A — Excel workbook per origin
# ---------------------------------------------------------------------------

MAIN_HEADERS = ['ספר', 'מראה מקום', 'סוג שגיאה', 'המילה במקור',
                'הצעת תיקון', 'ציון', 'מאומת', 'סטטוס', 'הערה',
                'קטע מהטקסט', 'מזהה שורה', 'הוחלט ע״י', 'מהדורה']

_ROW_SQL = f'''
    SELECT f.source, f.ref, f.errtype, f.word,
           {SHOWN_FIX},
           f.rank, f.verified, {EFF}, r.note, f.snippet, f.unit,
           {DECIDER}, x.flag, f.extra
    FROM findings f {JOINS} {EXT_JOIN}
    WHERE f.origin = ?'''


def _fmt_row(r, with_errtype=True):
    out = [r[0] or '', r[1] or '']
    if with_errtype:
        out.append(hebrew.errtype_hebrew(r[2]))
    out += [r[3] or '', r[4] or '',
            r[5] if r[5] is not None else '',
            'כן' if r[6] else '',
            hebrew.status_hebrew(r[7]) + (
                ' — ' + hebrew.MESSAGES['stale_mark']
                if r[12] == 'stale_approval' and r[7] == 'pending' else ''),
            r[8] or '', r[9] or '', r[10] or '',
            ACTOR_HEBREW.get(r[11], r[11] or ''),
            word_editions_text(r[13])]
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
