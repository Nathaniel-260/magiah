# -*- coding: utf-8 -*-
"""ui_review.db — importer, query layer, status writes, decisions.db sync.

Stable finding identity across re-imports
-----------------------------------------
``import_all`` rebuilds the ``findings`` table from report.db on every run,
but review data (``review`` rows keyed by finding_id) must survive a
refresh. A naive delete+reinsert would renumber ids, so instead findings are
matched to their previous incarnation by the deterministic identity key
``(family, word, unit, errtype, ref)``. Because the same key can appear more
than once (the same word twice on one line), each row also gets a sequence
number: rows sharing a key are numbered 1..n in deterministic source order
(report.db rowid order for the new rows, ascending id order for the old
rows), and matching is done on (key, seq). Matched rows keep their old id;
new rows get fresh ids above the previous maximum; review rows whose finding
vanished are deleted (their words remain in word_rules if a word-scope rule
existed). The whole rebuild runs in a single transaction.

Rank formula
------------
``RANK_SQL`` below is copied VERBATIM from magiah.core (do not edit here
without editing there) and is precomputed into the ``rank`` column at import
time for family='error'. Other families have no corpus-evidence columns, so
their rank is documented per family:

* extra_space  — rank = round(log10(join_freq + 1), 2)  (log-scaled join
  frequency: the more often the joined form appears, the more confident).
* tanach_edition — rank = 4.0 flat (mirrors the ``tanach = 2`` bonus in
  RANK_SQL: a deviation from the agreed Tanach text is strong evidence).
* tanach_match — rank = 0.0 (informational only).
* tokdiag      — rank = round(log10(freq + 1), 2) (log-scaled frequency).

decisions.db sync-back
----------------------
Every status write mirrors the decision into the old-schema decisions.db so
the legacy pipeline (detect whitelist feedback, old review UI) keeps
working: approved/fixed -> 'accept', not_error -> 'reject' (word-scope ->
unit '*'), ignored -> 'ignore', pending/unsure -> the row is deleted.

Ownership: decisions.db is keyed on (word, unit) only, so a row this UI is
about to delete may in fact be a *legacy* decision (from the old ``magiah
review`` tool) that merely shares the key. Every sync-write therefore
records the key in ``owned_decisions`` (in ui_review.db — decisions.db's
schema must stay byte-compatible with the old tool), and a sync-delete only
fires for owned keys. An unowned collision is left intact and reported as a
Hebrew warning in the API response instead of being silently destroyed.
"""
import csv
import glob
import json
import math
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime

from . import hebrew

UI_DB_F = 'ui_review.db'
REPORT_DB_F = 'report.db'
DECISIONS_F = 'decisions.db'
TOKDIAG_GLOB = '*tokdiag_source_he.csv'
BACKUP_DIR = 'backups'

# --- copied VERBATIM from magiah/core.py (keep in sync) --------------------
RANK_SQL = '''score
              + CASE WHEN errtype LIKE 'edit1%' THEN
                  CASE WHEN ctx_hits > 0 THEN 1.5 ELSE -1.0 END
                ELSE 0 END
              + CASE WHEN sugg_local >= 10 THEN 1.5
                     WHEN sugg_local >= 3 THEN 0.7 ELSE 0 END
              - CASE WHEN book_repeat = 1 THEN 3.0 ELSE 0 END
              + CASE WHEN tanach = 2 THEN 4.0 ELSE 0 END'''

VERIFIED_SQL = 'book_repeat = 0 AND (ctx_hits > 0 OR sugg_local >= 3)'
# ---------------------------------------------------------------------------

SCHEMA = '''
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY,
  family TEXT NOT NULL,
  errtype TEXT NOT NULL,
  word TEXT, suggestion TEXT,
  score REAL, rank REAL,
  ctx_hits INTEGER, sugg_local INTEGER, book_repeat INTEGER, tanach INTEGER,
  verified INTEGER NOT NULL DEFAULT 0,
  origin TEXT, source TEXT, ref TEXT, unit TEXT, doc TEXT, snippet TEXT,
  extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_f_origin ON findings(origin);
CREATE INDEX IF NOT EXISTS idx_f_source ON findings(source);
CREATE INDEX IF NOT EXISTS idx_f_errtype ON findings(errtype);
CREATE INDEX IF NOT EXISTS idx_f_word_unit ON findings(word, unit);
CREATE INDEX IF NOT EXISTS idx_f_rank ON findings(rank);

CREATE TABLE IF NOT EXISTS review(
  finding_id INTEGER PRIMARY KEY REFERENCES findings(id),
  status TEXT NOT NULL,
  note TEXT,
  custom_suggestion TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_rules(
  word TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,
  finding_id INTEGER, word TEXT,
  old_status TEXT, new_status TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- provenance for decisions.db: which (word, unit) keys THIS ui wrote.
-- decisions.db itself must stay byte-compatible with the old tool, so the
-- ownership marker lives here. A key that is absent is presumed legacy and
-- is never deleted by a sync (conservative default for existing installs).
CREATE TABLE IF NOT EXISTS owned_decisions(
  word TEXT NOT NULL, unit TEXT NOT NULL,
  PRIMARY KEY(word, unit)
);
'''

# Tables SCHEMA creates; connect() only runs the script when one is missing.
SCHEMA_TABLES = {'findings', 'review', 'word_rules', 'history', 'meta',
                 'owned_decisions'}

# effective status: per-finding review wins, else the word rule, else pending
EFF = "COALESCE(r.status, w.status, 'pending')"
JOINS = ('LEFT JOIN review r ON r.finding_id = f.id '
         'LEFT JOIN word_rules w ON w.word = f.word')

KEY_COLS = "family, COALESCE(word,''), COALESCE(unit,''), errtype, " \
           "COALESCE(ref,'')"

# Reading-order sort key for a unit id. DB units are plain integers, but
# file-based units are 'file:<relpath>:<lineno>' (§9c) — a plain
# CAST(unit AS INTEGER) yields 0 for every one of those, which silently
# destroys the ordering. Take the trailing digit run instead, so both
# forms sort by their real line number.
UNIT_ORDER = ("CAST(substr({u}, length(rtrim({u}, '0123456789')) + 1) "
              'AS INTEGER)')

FINDING_COLS = ['id', 'family', 'errtype', 'word', 'suggestion', 'score',
                'rank', 'ctx_hits', 'sugg_local', 'book_repeat', 'tanach',
                'verified', 'origin', 'source', 'ref', 'unit', 'doc',
                'snippet', 'extra']


def _now():
    return datetime.now().isoformat(timespec='microseconds')


def _uri(path, ro=False):
    u = 'file:' + urllib.request.pathname2url(os.path.abspath(path))
    return u + '?mode=ro' if ro else u


def connect(outdir):
    """Open (and if needed create) ui_review.db. URI mode is enabled so that
    ATTACH statements can attach report.db read-only.

    The schema is only applied when the database is new or missing a table:
    executescript() takes a write lock and implicitly commits, so running it on
    every connection made concurrent requests collide with "database is locked".
    """
    path = os.path.join(outdir, UI_DB_F)
    con = sqlite3.connect(_uri(path), uri=True, check_same_thread=False,
                          timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=30000')
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not SCHEMA_TABLES <= have:
        con.executescript(SCHEMA)
    return con


def _decisions_con(outdir):
    con = sqlite3.connect(os.path.join(outdir, DECISIONS_F), timeout=30.0)
    con.execute('PRAGMA busy_timeout=30000')
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='decisions'").fetchone():
        con.execute('''CREATE TABLE IF NOT EXISTS decisions(
            word TEXT, unit TEXT, errtype TEXT, verdict TEXT,
            suggestion TEXT, source TEXT, ref TEXT,
            PRIMARY KEY(word, unit))''')
        con.commit()
    return con


# ---------------------------------------------------------------------------
# importer
# ---------------------------------------------------------------------------

def _find_tokdiag_csv(outdir):
    hits = sorted(glob.glob(os.path.join(outdir, TOKDIAG_GLOB)))
    return hits[-1] if hits else None


def import_all(outdir, migrate_legacy=False):
    """(Re)build the findings table from report.db + the tokdiag CSV.

    Idempotent: review / word_rules / history survive; finding ids are kept
    stable via identity matching (see module docstring). Legacy decisions.db
    migration is OPT-IN (migrate_legacy=True or POST /api/import_legacy) —
    the default first-run state is everything pending.

    Returns a dict of counts incl. added / removed / preserved decisions.
    """
    t0 = time.time()
    report_path = os.path.join(outdir, REPORT_DB_F)
    if not os.path.exists(report_path):
        raise FileNotFoundError(
            hebrew.MESSAGES['report_missing'].format(outdir=outdir))
    con = connect(outdir)
    try:
        con.execute('ATTACH DATABASE ? AS rep', (_uri(report_path, ro=True),))
        # A report.db can exist but hold no results: a 0-byte file left by an
        # interrupted/out-of-order stage, or a scan that died before `locate`
        # wrote its tables. Treat that exactly like "no scan yet" instead of
        # letting a raw OperationalError escape (it used to stop the whole UI
        # from starting).
        if not con.execute("SELECT name FROM rep.sqlite_master "
                           "WHERE type='table' AND name='occurrences_full'"
                           ).fetchone():
            con.execute('DETACH DATABASE rep')
            raise FileNotFoundError(
                hebrew.MESSAGES['report_incomplete'].format(outdir=outdir))
        cur = con.cursor()
        cur.execute('BEGIN')
        cur.execute('''CREATE TEMP TABLE imp(
            family TEXT, errtype TEXT, word TEXT, suggestion TEXT,
            score REAL, rank REAL, ctx_hits INT, sugg_local INT,
            book_repeat INT, tanach INT, verified INT,
            origin TEXT, source TEXT, ref TEXT, unit TEXT, doc TEXT,
            snippet TEXT, extra TEXT)''')

        counts = {}
        # -- family 'error' ------------------------------------------------
        cur.execute(f'''
            INSERT INTO imp
            SELECT 'error', errtype, word, suggestion, score,
                   ROUND({RANK_SQL}, 2), ctx_hits, sugg_local, book_repeat,
                   tanach,
                   CASE WHEN {VERIFIED_SQL} THEN 1 ELSE 0 END,
                   origin, source, ref, unit, doc, snippet, NULL
            FROM rep.occurrences_full ORDER BY rowid''')
        counts['error'] = cur.rowcount

        # -- family 'extra_space' -------------------------------------------
        rows = []
        for p1, p2, joined, jf, src, ref, unit, snip, org in cur.execute(
                'SELECT part1, part2, joined, join_freq, source, ref, unit,'
                ' snippet, origin FROM rep.space_errors_full '
                'ORDER BY rowid').fetchall():
            jf = jf or 0
            rows.append((
                'extra_space', 'extra_space',
                (p1 or '') + ' ' + (p2 or ''), joined,
                float(jf), round(math.log10(jf + 1), 2),
                None, None, None, None, 0, org, src, ref, unit, None, snip,
                json.dumps({'part1': p1, 'part2': p2, 'joined': joined,
                            'join_freq': jf}, ensure_ascii=False)))
        cur.executemany('INSERT INTO imp VALUES(' +
                        ','.join('?' * 18) + ')', rows)
        counts['extra_space'] = len(rows)

        # -- family 'tanach_error' -------------------------------------------
        rows = []
        for word, canonical, src, ref, unit, snip, org in cur.execute(
                'SELECT word, canonical, source, ref, unit, snippet, origin '
                'FROM rep.tanach_errors_full ORDER BY rowid').fetchall():
            rows.append((
                'tanach_error', 'tanach_edition', word, canonical,
                4.0, 4.0, None, None, None, None, 0, org, src, ref, unit,
                None, snip,
                json.dumps({'canonical': canonical}, ensure_ascii=False)))
        cur.executemany('INSERT INTO imp VALUES(' +
                        ','.join('?' * 18) + ')', rows)
        counts['tanach_error'] = len(rows)

        # -- family 'tanach_match' -------------------------------------------
        rows = []
        for word, src, ref, unit, snip, org in cur.execute(
                'SELECT word, source, ref, unit, snippet, origin '
                'FROM rep.tanach_matches_full ORDER BY rowid').fetchall():
            rows.append((
                'tanach_match', 'tanach_match', word, None,
                0.0, 0.0, None, None, None, None, 0, org, src, ref, unit,
                None, snip, None))
        cur.executemany('INSERT INTO imp VALUES(' +
                        ','.join('?' * 18) + ')', rows)
        counts['tanach_match'] = len(rows)

        # -- family 'tokdiag' (CSV, optional) --------------------------------
        tok_csv = _find_tokdiag_csv(outdir)
        tok_rows = []
        if tok_csv:
            with open(tok_csv, encoding='utf-8-sig', newline='') as f:
                for rec in csv.DictReader(f):
                    try:
                        freq = int(rec.get('freq') or 0)
                    except ValueError:
                        freq = 0
                    tok_rows.append((
                        'tokdiag', 'tokdiag', rec.get('term'),
                        rec.get('suggestion'), float(freq),
                        round(math.log10(freq + 1), 2),
                        None, None, None, None, 0,
                        None,  # origin resolved below
                        rec.get('book'), rec.get('heRef'),
                        rec.get('line_id'), None, rec.get('context'),
                        json.dumps({'category': rec.get('category'),
                                    'freq': freq}, ensure_ascii=False)))
            # resolve origin by matching the Otzaria line id against rows
            # already imported from report.db (cheap: one temp-table scan)
            need = {r[14] for r in tok_rows if r[14]}
            unit2org = {}
            for unit, org in cur.execute(
                    "SELECT unit, origin FROM imp "
                    "WHERE origin IS NOT NULL AND origin != ''"):
                if unit in need and unit not in unit2org:
                    unit2org[unit] = org
            tok_rows = [r[:11] + (unit2org.get(r[14],
                        hebrew.UNKNOWN_ORIGIN),) + r[12:] for r in tok_rows]
            cur.executemany('INSERT INTO imp VALUES(' +
                            ','.join('?' * 18) + ')', tok_rows)
        counts['tokdiag'] = len(tok_rows)

        # -- stable-id assignment (see module docstring) ---------------------
        cur.execute(f'''CREATE TEMP TABLE imp2 AS
            SELECT imp.*, rowid AS irow,
                   ROW_NUMBER() OVER (PARTITION BY {KEY_COLS}
                                      ORDER BY rowid) AS seq
            FROM imp''')
        cur.execute('''CREATE INDEX temp.ix_imp2 ON imp2(
            family, word, unit, errtype, ref, seq)''')
        cur.execute(f'''CREATE TEMP TABLE oldmap AS
            SELECT id, family, COALESCE(word,'') AS w, COALESCE(unit,'') AS u,
                   errtype, COALESCE(ref,'') AS r,
                   ROW_NUMBER() OVER (PARTITION BY {KEY_COLS}
                                      ORDER BY id) AS seq
            FROM findings f''')
        cur.execute('''CREATE INDEX temp.ix_oldmap ON oldmap(
            family, w, u, errtype, r, seq)''')
        cur.execute('''CREATE TEMP TABLE assign AS
            SELECT i.irow AS irow, o.id AS old_id
            FROM imp2 i LEFT JOIN oldmap o
              ON o.family = i.family AND o.w = COALESCE(i.word, '')
             AND o.u = COALESCE(i.unit, '') AND o.errtype = i.errtype
             AND o.r = COALESCE(i.ref, '') AND o.seq = i.seq''')
        cur.execute('CREATE INDEX temp.ix_assign ON assign(irow)')
        cur.execute('CREATE INDEX temp.ix_imp2_irow ON imp2(irow)')
        old_total = cur.execute('SELECT COUNT(*) FROM findings').fetchone()[0]
        matched = cur.execute('SELECT COUNT(*) FROM assign '
                              'WHERE old_id IS NOT NULL').fetchone()[0]
        max_id = cur.execute(
            'SELECT COALESCE(MAX(id), 0) FROM findings').fetchone()[0]

        cur.execute('DELETE FROM findings')
        cols17 = ('family, errtype, word, suggestion, score, rank, ctx_hits, '
                  'sugg_local, book_repeat, tanach, verified, origin, source, '
                  'ref, unit, doc, snippet, extra')
        icols = ', '.join('i.' + c.strip() for c in cols17.split(','))
        cur.execute(f'''INSERT INTO findings(id, {cols17})
            SELECT a.old_id, {icols}
            FROM imp2 i JOIN assign a ON a.irow = i.irow
            WHERE a.old_id IS NOT NULL''')
        cur.execute(f'''INSERT INTO findings(id, {cols17})
            SELECT ? + ROW_NUMBER() OVER (ORDER BY i.irow), {icols}
            FROM imp2 i JOIN assign a ON a.irow = i.irow
            WHERE a.old_id IS NULL''', (max_id,))
        total = cur.execute('SELECT COUNT(*) FROM findings').fetchone()[0]

        # drop review rows of vanished findings; count what survived
        cur.execute('''DELETE FROM review WHERE finding_id NOT IN
                       (SELECT id FROM findings)''')
        preserved = cur.execute('SELECT COUNT(*) FROM review').fetchone()[0]

        counts.update({
            'total': total,
            'added': total - matched,
            'removed': old_total - matched,
            'preserved': preserved,
        })
        cur.execute("INSERT OR REPLACE INTO meta VALUES('last_import', ?)",
                    (_now(),))
        cur.execute("INSERT OR REPLACE INTO meta VALUES('import_counts', ?)",
                    (json.dumps(counts, ensure_ascii=False),))
        cur.execute('DROP TABLE imp')
        cur.execute('DROP TABLE imp2')
        cur.execute('DROP TABLE oldmap')
        cur.execute('DROP TABLE assign')
        con.commit()
        con.execute('DETACH DATABASE rep')

        if migrate_legacy:
            counts['migrated'] = migrate_legacy_decisions(con, outdir)
        counts['seconds'] = round(time.time() - t0, 1)
        return counts
    finally:
        con.close()


def migrate_legacy_decisions(con, outdir):
    """OPT-IN migration of the old decisions.db into review / word_rules.

    accept -> approved (decision suggestion kept as custom_suggestion when it
    differs from the finding's own suggestion); reject with unit='*' -> a
    word_rules 'not_error' row; per-unit reject -> not_error; ignore ->
    ignored. Matching is on (word, unit); existing review rows are never
    overwritten. Returns counts.
    """
    dec_path = os.path.join(outdir, DECISIONS_F)
    out = {'review': 0, 'word_rules': 0, 'unmatched': 0, 'decisions': 0}
    if not os.path.exists(dec_path):
        return out
    dec = _decisions_con(outdir)
    try:
        rows = dec.execute('SELECT word, unit, errtype, verdict, suggestion '
                           'FROM decisions').fetchall()
    finally:
        dec.close()
    ts = _now()
    status_map = {'accept': 'approved', 'reject': 'not_error',
                  'ignore': 'ignored'}
    for word, unit, errtype, verdict, sugg in rows:
        out['decisions'] += 1
        status = status_map.get(verdict)
        if status is None:
            out['unmatched'] += 1
            continue
        # the user explicitly chose to adopt these decisions -> the UI now
        # owns them and may delete them again on a later pending/unsure write
        _own_decision(con, word, unit)
        if unit == '*':
            con.execute('INSERT OR REPLACE INTO word_rules VALUES(?,?,?)',
                        (word, 'not_error', ts))
            out['word_rules'] += 1
            continue
        fids = con.execute(
            'SELECT id, suggestion FROM findings WHERE word = ? AND unit = ?',
            (word, unit)).fetchall()
        if not fids:
            out['unmatched'] += 1
            continue
        for fid, fsugg in fids:
            custom = None
            if (verdict == 'accept' and sugg and sugg != (fsugg or '')):
                custom = sugg
            n = con.execute(
                'INSERT OR IGNORE INTO review VALUES(?,?,?,?,?)',
                (fid, status, None, custom, ts)).rowcount
            out['review'] += n
    con.commit()
    return out


# ---------------------------------------------------------------------------
# query layer
# ---------------------------------------------------------------------------

def _rowdict(row):
    d = dict(row)
    if d.get('extra'):
        try:
            d['extra'] = json.loads(d['extra'])
        except (ValueError, TypeError):
            pass
    return d


def get_meta(con):
    origins = []
    for raw, cnt, done in con.execute(f'''
            SELECT f.origin, COUNT(*),
                   SUM(CASE WHEN {EFF} != 'pending' THEN 1 ELSE 0 END)
            FROM findings f {JOINS}
            GROUP BY f.origin ORDER BY COUNT(*) DESC'''):
        origins.append({'name': raw or '', 'hebrew': hebrew.origin_hebrew(raw),
                        'count': cnt, 'done_count': done or 0})
    et_counts = {}
    for et, cnt, pend in con.execute(f'''
            SELECT f.errtype, COUNT(*),
                   SUM(CASE WHEN {EFF} = 'pending' THEN 1 ELSE 0 END)
            FROM findings f {JOINS} GROUP BY f.errtype'''):
        et_counts[et] = (cnt, pend or 0)
    errtypes = []
    for key in hebrew.ERRTYPE_ORDER + sorted(set(et_counts) -
                                             set(hebrew.ERRTYPE_ORDER)):
        cnt, pend = et_counts.get(key, (0, 0))
        info = hebrew.ERRTYPES.get(key, {})
        errtypes.append({
            'key': key,
            'hebrew': info.get('hebrew', key),
            'short': info.get('short', ''),
            'explanation': info.get('explanation', ''),
            'count': cnt, 'pending_count': pend})
    statuses = [{'key': k,
                 'hebrew': hebrew.STATUSES[k]['hebrew'],
                 'icon': hebrew.STATUSES[k]['icon'],
                 'explanation': hebrew.STATUS_EXPLANATIONS.get(k, '')}
                for k in hebrew.STATUS_ORDER]
    columns = [{'key': k,
                'hebrew': hebrew.COLUMNS[k]['hebrew'],
                'explanation': hebrew.COLUMNS[k]['explanation']}
               for k in hebrew.COLUMN_ORDER]
    last_import = con.execute(
        "SELECT value FROM meta WHERE key='last_import'").fetchone()
    # Sum the per-origin counts already computed above rather than running a
    # second COUNT(*) over the whole findings table (a full scan on a ~300MB db).
    total = sum(o['count'] for o in origins)
    return {'origins': origins, 'errtypes': errtypes, 'statuses': statuses,
            'columns': columns, 'total': total,
            # no findings yet -> the UI shows its "run a scan first" screen
            'no_scan': total == 0,
            'last_import': last_import[0] if last_import else None}


def get_books(con, origin=None, q=None):
    where, params = [], []
    if origin:
        where.append('f.origin = ?')
        params.append(origin)
    if q:
        where.append("f.source LIKE '%' || ? || '%'")
        params.append(q)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = con.execute(f'''
        SELECT f.source, COUNT(*) AS count,
               SUM(CASE WHEN {EFF} = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN {EFF} = 'approved' THEN 1 ELSE 0 END)
        FROM findings f {JOINS} {wsql}
        GROUP BY f.source ORDER BY COUNT(*) DESC''', params).fetchall()
    return [{'source': r[0] or '', 'count': r[1], 'pending_count': r[2] or 0,
             'approved_count': r[3] or 0}
            for r in rows]


def _findings_where(filters):
    where, params = [], []
    if filters.get('origin'):
        where.append('f.origin = ?')
        params.append(filters['origin'])
    if filters.get('book'):
        where.append('f.source = ?')
        params.append(filters['book'])
    if filters.get('errtype'):
        ets = filters['errtype']
        if isinstance(ets, str):
            ets = [e for e in ets.split(',') if e]
        where.append('f.errtype IN (%s)' % ','.join('?' * len(ets)))
        params.extend(ets)
    if filters.get('status'):
        sts = filters['status']
        if isinstance(sts, str):
            sts = [s for s in sts.split(',') if s]
        where.append(f'{EFF} IN (%s)' % ','.join('?' * len(sts)))
        params.extend(sts)
    if filters.get('verified') not in (None, '', '0'):
        where.append('f.verified = 1')
    if filters.get('min_rank') not in (None, ''):
        where.append('f.rank >= ?')
        params.append(float(filters['min_rank']))
    if filters.get('q'):
        where.append("(f.word LIKE '%'||?||'%' OR "
                     "f.suggestion LIKE '%'||?||'%' OR "
                     "f.snippet LIKE '%'||?||'%' OR f.ref LIKE '%'||?||'%')")
        params.extend([filters['q']] * 4)
    return ('WHERE ' + ' AND '.join(where)) if where else '', params


SORTS = {
    'rank': 'f.rank {d}, f.id',
    'random': 'RANDOM()',
    'source': 'f.source {d}, ' + UNIT_ORDER.format(u='f.unit') + ' {d}',
    'word': 'f.word {d}, f.id',
}


def query_findings(con, filters, sort='rank', direction='desc',
                   page=1, page_size=50):
    page = max(1, int(page or 1))
    page_size = min(500, max(1, int(page_size or 50)))
    d = 'ASC' if str(direction).lower() == 'asc' else 'DESC'
    if sort == 'rank' and direction in (None, ''):
        d = 'DESC'
    order = SORTS.get(sort, SORTS['rank']).format(d=d)
    wsql, params = _findings_where(filters)
    total = con.execute(
        f'SELECT COUNT(*) FROM findings f {JOINS} {wsql}',
        params).fetchone()[0]
    rows = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion
        FROM findings f {JOINS} {wsql}
        ORDER BY {order} LIMIT ? OFFSET ?''',
        params + [page_size, (page - 1) * page_size]).fetchall()
    return [_rowdict(r) for r in rows], total


def get_finding(con, fid):
    row = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion,
               r.updated_at AS updated_at
        FROM findings f {JOINS} WHERE f.id = ?''', (fid,)).fetchone()
    if row is None:
        return None
    d = _rowdict(row)
    d['history'] = [dict(h) for h in con.execute(
        '''SELECT * FROM history
           WHERE finding_id = ? OR (action = 'word_rule' AND word = ?)
           ORDER BY id DESC LIMIT 50''', (fid, d['word']))]
    return d


# ---------------------------------------------------------------------------
# status writes + decisions.db sync
# ---------------------------------------------------------------------------

def _own_decision(con, word, unit):
    """Record that this UI owns the decisions.db row keyed (word, unit)."""
    con.execute('INSERT OR IGNORE INTO owned_decisions VALUES(?,?)',
                (word or '', unit or ''))


def _owns_decision(con, word, unit):
    return con.execute(
        'SELECT 1 FROM owned_decisions WHERE word = ? AND unit = ?',
        (word or '', unit or '')).fetchone() is not None


def _sync_decision(con, dec, finding, status, custom_suggestion=None,
                   word_scope=False):
    """Mirror one status into old-schema decisions.db.

    ``con`` is the ui_review.db connection, used for the ownership table.
    Returns a Hebrew warning string when a delete was declined because the
    matching decisions.db row is a legacy row this UI never wrote, else None.
    """
    verdict = hebrew.STATUSES[status]['verdict']
    word = finding['word']
    unit = '*' if word_scope else (finding['unit'] or '')
    if verdict is None:
        exists = dec.execute(
            'SELECT 1 FROM decisions WHERE word = ? AND unit = ?',
            (word, unit)).fetchone() is not None
        if exists and not _owns_decision(con, word, unit):
            return hebrew.MESSAGES['legacy_decision_kept'] + (word or '')
        dec.execute('DELETE FROM decisions WHERE word = ? AND unit = ?',
                    (word, unit))
        con.execute('DELETE FROM owned_decisions WHERE word = ? AND unit = ?',
                    (word or '', unit or ''))
    else:
        sugg = custom_suggestion or finding['suggestion'] or ''
        dec.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?,?)',
                    (word, unit, finding['errtype'] or '', verdict, sugg,
                     finding['source'] or '', finding['ref'] or ''))
        _own_decision(con, word, unit)
    return None


def set_status(con, outdir, ids, status, note=None, custom_suggestion=None,
               scope='occurrence'):
    """Set the status of one or more findings (one undo step).

    scope='word' additionally writes a word_rules row (reject-everywhere
    semantics) for each distinct word — decisions.db gets a unit='*' row.
    Every change is appended to history and mirrored into decisions.db.
    """
    if status not in hebrew.STATUSES:
        raise ValueError(hebrew.MESSAGES['bad_status'])
    ids = [int(i) for i in ids]
    if not ids:
        raise ValueError(hebrew.MESSAGES['no_ids'])
    ts = _now()
    action = 'bulk' if len(ids) > 1 else 'set_status'
    dec = _decisions_con(outdir)
    updated = word_rules = 0
    warnings = []
    try:
        for fid in ids:
            f = con.execute(f'''
                SELECT f.*, {EFF} AS eff FROM findings f {JOINS}
                WHERE f.id = ?''', (fid,)).fetchone()
            if f is None:
                continue
            con.execute('INSERT INTO history(ts, action, finding_id, word, '
                        'old_status, new_status, note) VALUES(?,?,?,?,?,?,?)',
                        (ts, action, fid, f['word'], f['eff'], status, note))
            if status == 'pending' and not note and not custom_suggestion:
                con.execute('DELETE FROM review WHERE finding_id = ?', (fid,))
            else:
                con.execute('''
                    INSERT INTO review(finding_id, status, note,
                                       custom_suggestion, updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(finding_id) DO UPDATE SET
                      status = excluded.status,
                      note = COALESCE(excluded.note, review.note),
                      custom_suggestion = COALESCE(excluded.custom_suggestion,
                                                   review.custom_suggestion),
                      updated_at = excluded.updated_at''',
                    (fid, status, note, custom_suggestion, ts))
            w = _sync_decision(con, dec, f, status, custom_suggestion)
            if w and w not in warnings:
                warnings.append(w)
            updated += 1
            if scope == 'word' and f['word']:
                old_ws = con.execute(
                    'SELECT status FROM word_rules WHERE word = ?',
                    (f['word'],)).fetchone()
                con.execute('INSERT INTO history(ts, action, finding_id, '
                            'word, old_status, new_status, note) '
                            'VALUES(?,?,?,?,?,?,?)',
                            (ts, 'word_rule', None, f['word'],
                             old_ws[0] if old_ws else None, status, note))
                if status == 'pending':
                    con.execute('DELETE FROM word_rules WHERE word = ?',
                                (f['word'],))
                else:
                    con.execute(
                        'INSERT OR REPLACE INTO word_rules VALUES(?,?,?)',
                        (f['word'], status, ts))
                w = _sync_decision(con, dec, f, status, custom_suggestion,
                                   word_scope=True)
                if w and w not in warnings:
                    warnings.append(w)
                word_rules += 1
        con.commit()
        dec.commit()
    finally:
        dec.close()
    out = {'updated': updated, 'word_rules': word_rules, 'status': status,
           'ts': ts}
    if warnings:
        out['warnings'] = warnings
    return out


def undo(con, outdir):
    """Revert the most recent not-yet-undone history group (one API call =
    one ts = one undo step, bulk included). Returns what was reverted."""
    undone = {r[0] for r in con.execute(
        "SELECT note FROM history WHERE action = 'undo'")}
    row = con.execute(
        "SELECT ts FROM history WHERE action != 'undo' "
        + ('AND ts NOT IN (%s) ' % ','.join('?' * len(undone))
           if undone else '')
        + 'ORDER BY id DESC LIMIT 1',
        list(undone)).fetchone()
    if row is None:
        return None
    group_ts = row[0]
    entries = con.execute(
        "SELECT * FROM history WHERE ts = ? AND action != 'undo' "
        'ORDER BY id DESC', (group_ts,)).fetchall()
    dec = _decisions_con(outdir)
    reverted = []
    warnings = []
    try:
        for e in entries:
            old = e['old_status']
            if e['action'] == 'word_rule':
                if old is None or old == 'pending':
                    con.execute('DELETE FROM word_rules WHERE word = ?',
                                (e['word'],))
                else:
                    con.execute(
                        'INSERT OR REPLACE INTO word_rules VALUES(?,?,?)',
                        (e['word'], old, _now()))
                fake = {'word': e['word'], 'unit': '*', 'errtype': '',
                        'suggestion': '', 'source': '', 'ref': ''}
                w = _sync_decision(con, dec, fake, old or 'pending',
                                   word_scope=True)
                if w and w not in warnings:
                    warnings.append(w)
            elif e['finding_id'] is not None:
                f = con.execute('SELECT * FROM findings WHERE id = ?',
                                (e['finding_id'],)).fetchone()
                if old is None or old == 'pending':
                    con.execute('DELETE FROM review WHERE finding_id = ?',
                                (e['finding_id'],))
                else:
                    con.execute('''
                        INSERT INTO review(finding_id, status, note,
                                           custom_suggestion, updated_at)
                        VALUES(?,?,NULL,NULL,?)
                        ON CONFLICT(finding_id) DO UPDATE SET
                          status = excluded.status,
                          updated_at = excluded.updated_at''',
                        (e['finding_id'], old, _now()))
                if f is not None:
                    w = _sync_decision(con, dec, f, old or 'pending')
                    if w and w not in warnings:
                        warnings.append(w)
            reverted.append({'finding_id': e['finding_id'], 'word': e['word'],
                             'restored': old or 'pending',
                             'was': e['new_status']})
        con.execute('INSERT INTO history(ts, action, note) VALUES(?,?,?)',
                    (_now(), 'undo', group_ts))
        con.commit()
        dec.commit()
    finally:
        dec.close()
    out = {'reverted': len(reverted), 'group_ts': group_ts,
           'entries': reverted}
    if warnings:
        out['warnings'] = warnings
    return out


def get_history(con, limit=100):
    limit = min(1000, max(1, int(limit or 100)))
    return [dict(r) for r in con.execute(
        'SELECT * FROM history ORDER BY id DESC LIMIT ?', (limit,))]


def get_stats(con):
    def matrix(col):
        out = {}
        for key, st, n in con.execute(f'''
                SELECT f.{col}, {EFF}, COUNT(*) FROM findings f {JOINS}
                GROUP BY f.{col}, {EFF}'''):
            out.setdefault(key or '', {})[st] = n
        return out
    origin_m = matrix('origin')
    origins = [{'name': k, 'hebrew': hebrew.origin_hebrew(k),
                'statuses': v, 'total': sum(v.values())}
               for k, v in sorted(origin_m.items(),
                                  key=lambda kv: -sum(kv[1].values()))]
    errtype_m = matrix('errtype')
    errtypes = [{'key': k, 'hebrew': hebrew.errtype_hebrew(k),
                 'statuses': v, 'total': sum(v.values())}
                for k, v in sorted(errtype_m.items(),
                                   key=lambda kv: -sum(kv[1].values()))]
    status_cols = ', '.join(
        f"SUM(CASE WHEN {EFF} = '{s}' THEN 1 ELSE 0 END)"
        for s in hebrew.STATUS_ORDER)
    books = []
    for row in con.execute(f'''
            SELECT f.source, f.origin, COUNT(*),
                   SUM(CASE WHEN {EFF} != 'pending' THEN 1 ELSE 0 END),
                   {status_cols}
            FROM findings f {JOINS}
            GROUP BY f.source, f.origin
            ORDER BY COUNT(*) DESC LIMIT 50'''):
        src, org, total, done = row[0], row[1], row[2], row[3]
        books.append({'source': src or '', 'origin': org or '',
                      'origin_hebrew': hebrew.origin_hebrew(org),
                      'total': total, 'done': done or 0,
                      'statuses': {s: row[4 + i] or 0 for i, s in
                                   enumerate(hebrew.STATUS_ORDER)}})
    totals = {st: n for st, n in con.execute(
        f'SELECT {EFF}, COUNT(*) FROM findings f {JOINS} GROUP BY {EFF}')}
    return {'origins': origins, 'errtypes': errtypes, 'books': books,
            'totals': totals}


def get_fixlist(con, book=None, origin=None, statuses=None):
    """Fixer-mode worklist. With book=None returns the books that still have
    findings in the requested statuses (default: approved), with remaining
    counts; with a book returns its worklist in reading order (unit asc)."""
    if not statuses:
        statuses = ['approved']
    if isinstance(statuses, str):
        statuses = [s for s in statuses.split(',') if s]
    sph = ','.join('?' * len(statuses))
    params = list(statuses)
    owhere = ''
    if origin:
        owhere = ' AND f.origin = ?'
        params.append(origin)
    if book is None:
        rows = con.execute(f'''
            SELECT f.source, f.origin, COUNT(*) FROM findings f {JOINS}
            WHERE {EFF} IN ({sph}){owhere}
            GROUP BY f.source, f.origin ORDER BY COUNT(*) DESC''',
            params).fetchall()
        books = [{'source': r[0] or '', 'origin': r[1] or '',
                  'origin_hebrew': hebrew.origin_hebrew(r[1]),
                  'remaining': r[2]} for r in rows]
        return {'books': books, 'rows': books, 'total': len(books)}
    params.append(book)
    rows = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion
        FROM findings f {JOINS}
        WHERE {EFF} IN ({sph}){owhere} AND f.source = ?
        ORDER BY {UNIT_ORDER.format(u='f.unit')} ASC, f.id ASC''',
        params).fetchall()
    total = con.execute(f'''
        SELECT COUNT(*) FROM findings f {JOINS}
        WHERE {EFF} IN ('approved','fixed') AND f.source = ?''',
        (book,)).fetchone()[0]
    fixed = con.execute(f'''
        SELECT COUNT(*) FROM findings f {JOINS}
        WHERE {EFF} = 'fixed' AND f.source = ?''', (book,)).fetchone()[0]
    items = [_rowdict(r) for r in rows]
    return {'book': book, 'items': items, 'rows': items,
            'total': len(items), 'fixed': fixed, 'total_approved': total}


# ---------------------------------------------------------------------------
# §9b — backup / reset / restore
# ---------------------------------------------------------------------------

def write_backup(con, outdir):
    """Write review + word_rules + history to a timestamped JSON backup.
    Review rows carry the finding identity so a restore can survive a
    re-import that renumbered ids."""
    bdir = os.path.join(outdir, BACKUP_DIR)
    os.makedirs(bdir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(bdir, f'ui_backup_{ts}.json')
    review = [dict(r) for r in con.execute('''
        SELECT v.finding_id, v.status, v.note, v.custom_suggestion,
               v.updated_at, f.family, f.word, f.unit, f.errtype, f.ref
        FROM review v JOIN findings f ON f.id = v.finding_id''')]
    word_rules = [dict(r) for r in con.execute('SELECT * FROM word_rules')]
    history = [dict(r) for r in con.execute(
        'SELECT * FROM history ORDER BY id')]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'ts': _now(), 'review': review, 'word_rules': word_rules,
                   'history': history}, f, ensure_ascii=False)
    return path


def reset(con, outdir, scope='statuses'):
    """Clear all review state (after writing a backup). scope='all' also
    empties decisions.db — the escape hatch from the whitelist feedback."""
    if scope not in ('statuses', 'all'):
        raise ValueError(hebrew.MESSAGES['bad_request'])
    backup = write_backup(con, outdir)
    counts = {
        'review': con.execute('SELECT COUNT(*) FROM review').fetchone()[0],
        'word_rules': con.execute(
            'SELECT COUNT(*) FROM word_rules').fetchone()[0],
        'history': con.execute('SELECT COUNT(*) FROM history').fetchone()[0],
    }
    con.execute('DELETE FROM review')
    con.execute('DELETE FROM word_rules')
    con.execute('DELETE FROM history')
    con.commit()
    counts['decisions'] = 0
    if scope == 'all':
        dec = _decisions_con(outdir)
        try:
            counts['decisions'] = dec.execute(
                'SELECT COUNT(*) FROM decisions').fetchone()[0]
            dec.execute('DELETE FROM decisions')
            dec.commit()
            con.execute('DELETE FROM owned_decisions')
            con.commit()
        finally:
            dec.close()
    return {'backup': backup, 'cleared': counts, 'scope': scope}


def list_backups(outdir):
    bdir = os.path.join(outdir, BACKUP_DIR)
    out = []
    for p in sorted(glob.glob(os.path.join(bdir, 'ui_backup_*.json')),
                    reverse=True):
        st = os.stat(p)
        ts = datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')
        out.append({'file': os.path.basename(p), 'size': st.st_size,
                    'ts': ts, 'mtime': ts})
    return out


def restore_backup(con, outdir, filename):
    """Re-import a backup written by write_backup. Review rows are matched
    best-effort by finding identity (family, word, unit, errtype, ref);
    restored statuses are re-synced into decisions.db."""
    base = os.path.basename(filename)
    if not re.fullmatch(r'ui_backup_[\w.-]+\.json', base):
        raise ValueError(hebrew.MESSAGES['bad_request'])
    path = os.path.join(outdir, BACKUP_DIR, base)
    if not os.path.exists(path):
        raise FileNotFoundError(hebrew.MESSAGES['not_found'])
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    dec = _decisions_con(outdir)
    out = {'review': 0, 'word_rules': 0, 'unmatched': 0, 'history': 0}
    warnings = []
    try:
        for rv in data.get('review', []):
            fids = con.execute('''
                SELECT id FROM findings
                WHERE family = ? AND COALESCE(word,'') = ?
                  AND COALESCE(unit,'') = ? AND errtype = ?
                  AND COALESCE(ref,'') = ?''',
                (rv.get('family'), rv.get('word') or '', rv.get('unit') or '',
                 rv.get('errtype'), rv.get('ref') or '')).fetchall()
            if not fids:
                out['unmatched'] += 1
                continue
            for (fid,) in fids:
                con.execute('INSERT OR REPLACE INTO review VALUES(?,?,?,?,?)',
                            (fid, rv['status'], rv.get('note'),
                             rv.get('custom_suggestion'),
                             rv.get('updated_at') or _now()))
                f = con.execute('SELECT * FROM findings WHERE id = ?',
                                (fid,)).fetchone()
                w = _sync_decision(con, dec, f, rv['status'],
                                   rv.get('custom_suggestion'))
                if w and w not in warnings:
                    warnings.append(w)
                out['review'] += 1
        for wr in data.get('word_rules', []):
            con.execute('INSERT OR REPLACE INTO word_rules VALUES(?,?,?)',
                        (wr['word'], wr['status'],
                         wr.get('updated_at') or _now()))
            fake = {'word': wr['word'], 'unit': '*', 'errtype': '',
                    'suggestion': '', 'source': '', 'ref': ''}
            w = _sync_decision(con, dec, fake, wr['status'], word_scope=True)
            if w and w not in warnings:
                warnings.append(w)
            out['word_rules'] += 1
        for h in data.get('history', []):
            con.execute('INSERT INTO history(ts, action, finding_id, word, '
                        'old_status, new_status, note) VALUES(?,?,?,?,?,?,?)',
                        (h.get('ts'), h.get('action'), h.get('finding_id'),
                         h.get('word'), h.get('old_status'),
                         h.get('new_status'), h.get('note')))
            out['history'] += 1
        con.commit()
        dec.commit()
    finally:
        dec.close()
    if warnings:
        out['warnings'] = warnings
    return out
