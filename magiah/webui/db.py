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
* tanach_edition — rank = 4.0 flat (mirrors the ``tanach = 3`` bonus in
  RANK_SQL) for verified rows (report.db carries an ``evidence`` column);
  0.0 for rows of the old trigram heuristic.
* tanach_match — rank = 0.0 (informational only).

Tanach findings of the old heuristic (``tanach = 2``, or tanach_* rows of a
report.db without an ``evidence`` column) are marked
``{"evidence_kind": "tanach_legacy", "recheck": true}`` in ``extra`` and get
no rank bonus. A review decision made before that mark existed is NOT carried
onto them: it was given on the strength of evidence now known to be unsound.
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
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime

from . import hebrew, result_status
from ..corpus_hybrid import DEFAULT_LIBRARY
# one URI builder for every sqlite3 connect: pathname2url breaks UNC paths
from ..textsource import sqlite_uri as _uri

UI_DB_F = 'ui_review.db'
REPORT_DB_F = 'report.db'
DECISIONS_F = 'decisions.db'
TOKDIAG_GLOB = '*tokdiag_source_he.csv'
BACKUP_DIR = 'backups'
# meta key: the library root (and scan time) of the report import_all imported
IMPORT_SOURCE_KEY = 'import_source'

# --- copied VERBATIM from magiah/core.py (keep in sync) --------------------
RANK_SQL = '''score
              + CASE WHEN errtype LIKE 'edit1%' THEN
                  CASE WHEN ctx_hits > 0 THEN 1.5 ELSE -1.0 END
                ELSE 0 END
              + CASE WHEN sugg_local >= 10 THEN 1.5
                     WHEN sugg_local >= 3 THEN 0.7 ELSE 0 END
              - CASE WHEN book_repeat = 1 THEN 3.0 ELSE 0 END
              + CASE WHEN tanach = 3 THEN 4.0 ELSE 0 END'''

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
-- single-book scans select/delete a whole book by doc; without this each
-- merge scans the entire (300 MB+) findings table three times
CREATE INDEX IF NOT EXISTS idx_f_doc ON findings(doc);

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
-- who set a word rule; beside word_rules, whose 3 columns earlier versions
-- write positionally
CREATE TABLE IF NOT EXISTS word_rule_ext(
  word TEXT PRIMARY KEY, decided_by TEXT
);

-- prev_state: JSON snapshot of what this entry replaced, so undo restores
-- it exactly — {"review": row|null} or {"kind", "key", "row": row|null}
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,
  finding_id INTEGER, word TEXT,
  old_status TEXT, new_status TEXT,
  note TEXT,
  prev_state TEXT, decided_by TEXT
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- per-decision details kept beside `review` (whose column list must stay
-- fixed: positional INSERTs into it exist outside this module).
--   scope: occurrence | replacement | book | word
--   approved_suggestion: the correction actually approved (D2)
--   flag: 'stale_approval' when a re-scan changed what was approved
CREATE TABLE IF NOT EXISTS review_ext(
  finding_id INTEGER PRIMARY KEY,
  scope TEXT, approved_suggestion TEXT, decided_by TEXT,
  flag TEXT, prev_decision TEXT
);

-- scoped rules beside word_rules (global): a book's spelling convention and
-- a rejected (word -> suggestion) pair
CREATE TABLE IF NOT EXISTS book_rules(
  word TEXT NOT NULL, doc TEXT NOT NULL, status TEXT NOT NULL,
  decided_by TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(word, doc)
);
CREATE TABLE IF NOT EXISTS replacement_rules(
  word TEXT NOT NULL, suggestion TEXT NOT NULL, status TEXT NOT NULL,
  decided_by TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(word, suggestion)
);

-- provenance for decisions.db: which (word, unit) keys THIS ui wrote.
-- decisions.db itself must stay byte-compatible with the old tool, so the
-- ownership marker lives here. A key that is absent is presumed legacy and
-- is never deleted by a sync (conservative default for existing installs).
CREATE TABLE IF NOT EXISTS owned_decisions(
  word TEXT NOT NULL, unit TEXT NOT NULL,
  PRIMARY KEY(word, unit)
);

-- Fixer mode: one row per batch of corrections written into a book file.
-- This is the file-level audit trail, separate from `history` (which records
-- status changes): reverting a status must not silently imply that the book
-- on disk was reverted too, so the two are tracked independently and the UI
-- offers two distinct undo actions.
CREATE TABLE IF NOT EXISTS file_edits(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  path TEXT NOT NULL,          -- absolute path actually written
  book_key TEXT NOT NULL,      -- 'file:<rel>' / 'local:<abs>'
  backup TEXT NOT NULL,        -- the .bak taken BEFORE the write
  mode TEXT NOT NULL,          -- default mode of the batch
  finding_ids TEXT NOT NULL,   -- JSON array
  detail TEXT NOT NULL,        -- JSON: per-edit lineno/start/end/old/new
  fp_before TEXT, fp_after TEXT,
  undone_at TEXT               -- NULL until the file is restored
);
CREATE INDEX IF NOT EXISTS idx_fe_key ON file_edits(book_key);
'''

# Tables SCHEMA creates; connect() only runs the script when one is missing.
SCHEMA_TABLES = {'findings', 'review', 'word_rules', 'history', 'meta',
                 'owned_decisions', 'file_edits', 'review_ext', 'book_rules',
                 'replacement_rules', 'word_rule_ext'}

# columns added to tables that predate them (additive; see _migrate)
ADDED_COLUMNS = {
    'history': (('prev_state', 'TEXT'), ('decided_by', 'TEXT')),
}
SCHEMA_REV = 3

# A book's stable identity. `source` is only a display title and several
# books share one, so it is used only when a row has no doc at all.
BOOKKEY = "COALESCE(NULLIF(f.doc, ''), 'src:' || COALESCE(f.source, ''))"


def _rule_status(col):
    """A rule's status as it reaches one finding. An approval made by a rule
    (word-wide, book or replacement) does not reach a row marked
    tanach_legacy: such a row needs its own re-check, like a dropped per-row
    approval. A rule's not_error / ignored judges the word itself and still
    applies."""
    return (f"CASE WHEN {col} IN ('approved', 'fixed') AND "
            f"COALESCE(f.extra, '') LIKE '%tanach_legacy%' THEN NULL "
            f"ELSE {col} END")


# effective status, narrowest decision first: this occurrence, the book's
# convention, the (word, suggestion) pair, the global word rule, else pending
EFF = (f"COALESCE(r.status, {_rule_status('rb.status')}, "
       f"{_rule_status('rr.status')}, {_rule_status('w.status')}, 'pending')")
JOINS = ('LEFT JOIN review r ON r.finding_id = f.id '
         'LEFT JOIN book_rules rb ON rb.word = f.word '
         f'AND rb.doc = {BOOKKEY} '
         'LEFT JOIN replacement_rules rr ON rr.word = f.word '
         'AND rr.suggestion = f.suggestion '
         'LEFT JOIN word_rules w ON w.word = f.word')
EXT_JOIN = 'LEFT JOIN review_ext x ON x.finding_id = f.id'
# who set the word rule aliased `w`
WORD_DECIDER = '(SELECT e.decided_by FROM word_rule_ext e WHERE e.word = w.word)'
EXT_COLS = ('x.scope AS scope, x.approved_suggestion AS approved_suggestion, '
            'x.decided_by AS decided_by, x.flag AS flag, '
            'x.prev_decision AS prev_decision')

SCOPES = ('occurrence', 'replacement', 'book', 'word')
ACTORS = ('human', 'agent')

KEY_COLS = "family, COALESCE(word,''), COALESCE(unit,''), errtype, " \
           "COALESCE(ref,'')"

# Tanach evidence of the old (prev, next)-trigram heuristic. `lg` per row:
# 0 = not legacy, 1 = legacy and already marked for re-check, 2 = legacy from
# before the mark. An APPROVAL given under other evidence than the row now has
# may rest on the legacy suggestion and is dropped (logged in `history`);
# not_error / ignored / unsure judge the word itself and are kept.
LEGACY_EXTRA = json.dumps({'evidence_kind': 'tanach_legacy', 'recheck': True})
_NEW_TANACH = ("COALESCE({p}extra,'') LIKE '%tanach_verse_match%' OR "
               "COALESCE({p}extra,'') LIKE '%tanach_edition_variant%' OR "
               "COALESCE({p}extra,'') LIKE '%tanach_edition_unresolved%'")
LEGACY_LG_SQL = (
    "CASE WHEN COALESCE({p}extra,'') LIKE '%tanach_legacy%' THEN 1 "
    "WHEN COALESCE({p}tanach,0) = 2 OR ({p}family IN "
    "('tanach_error','tanach_match') AND NOT (" + _NEW_TANACH + ")) "
    "THEN 2 ELSE 0 END")


def _legacy_lg(prefix=''):
    return LEGACY_LG_SQL.format(p=prefix)


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
    con.create_function('magiah_doc_of_unit', 1, doc_of_unit,
                        deterministic=True)
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not SCHEMA_TABLES <= have:
        con.executescript(SCHEMA)
    if _schema_rev(con) < SCHEMA_REV:
        _migrate(con)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                         "AND name='idx_f_doc'").fetchone():
        # added with the single-book scan; existing databases predate it and
        # would otherwise scan the whole findings table on every book merge
        try:
            con.execute('CREATE INDEX IF NOT EXISTS idx_f_doc '
                        'ON findings(doc)')
            con.commit()
        except sqlite3.OperationalError:
            pass                       # another connection is building it
    return con


def doc_of_unit(unit):
    """The book (doc) a line unit belongs to, when the unit itself says so.

    'file:<rel>:<n>' -> <rel>, 'local:<abs>:<n>' -> 'local:<abs>' and a text
    folder's '<rel>:<n>' -> <rel> (the doc each corpus writes for them). A
    plain DB line id carries no book and yields None.
    """
    if not isinstance(unit, str):
        return None
    base, sep, n = unit.rpartition(':')
    if not sep or not base or not n.isdigit():
        return None
    if base.startswith('file:'):
        return base[len('file:'):] or None
    return base


def _fill_missing_docs(cur, table):
    """Give doc-less rows of `table` a doc: from the unit itself, else from
    another row on the same line (only when that line maps to one doc).
    Only the doc-less rows and their lines are visited."""
    # GLOB keeps the Python UDF to the '<x>:<n>' units it can resolve, and
    # it runs once per such row
    cur.execute(f'''UPDATE {table} SET doc = magiah_doc_of_unit(unit)
                    WHERE (doc IS NULL OR doc = '')
                      AND unit GLOB '*:[0-9]*' ''')
    cur.execute(f'''CREATE TEMP TABLE u2d AS
        SELECT unit, MIN(doc) AS doc FROM {table}
        WHERE doc IS NOT NULL AND doc != '' AND unit IN (
            SELECT unit FROM {table} WHERE doc IS NULL OR doc = '')
        GROUP BY unit HAVING COUNT(DISTINCT doc) = 1''')
    cur.execute('CREATE UNIQUE INDEX temp.ix_u2d ON u2d(unit)')
    cur.execute(f'''UPDATE {table}
        SET doc = (SELECT u.doc FROM u2d u WHERE u.unit = {table}.unit)
        WHERE (doc IS NULL OR doc = '')
          AND unit IN (SELECT unit FROM u2d)''')
    cur.execute('DROP TABLE u2d')


def _schema_rev(con):
    row = con.execute(
        "SELECT value FROM meta WHERE key = 'schema_rev'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _migrate(con):
    """Bring an existing ui_review.db up to SCHEMA_REV, additively.

    rev 2: review rows are scoped 'occurrence' (a review row was always one
    finding's decision), approvals record the suggestion they were made on,
    doc-less rows get a doc where one can be derived.
    rev 3: word_rules is back to its original 3 columns (decided_by moves to
    word_rule_ext) and the keyset sort indexes exist.
    Never touches a database of a newer revision; safe to run concurrently.
    """
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
        for name, typ in cols:
            if name not in have:
                try:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {typ}')
                except sqlite3.OperationalError:
                    pass               # another connection just added it
    try:
        cur = con.cursor()
        cur.execute('BEGIN IMMEDIATE')
        rev = _schema_rev(con)
        if rev >= SCHEMA_REV:
            con.rollback()
            return
        if rev < 2:
            cur.execute('''INSERT OR IGNORE INTO review_ext(finding_id, scope)
                           SELECT finding_id, 'occurrence' FROM review''')
            cur.execute('''UPDATE review_ext SET approved_suggestion = (
                    SELECT COALESCE(NULLIF(r.custom_suggestion, ''),
                                    f.suggestion, '')
                    FROM review r JOIN findings f ON f.id = r.finding_id
                    WHERE r.finding_id = review_ext.finding_id)
                WHERE approved_suggestion IS NULL AND finding_id IN (
                    SELECT finding_id FROM review
                    WHERE status IN ('approved', 'fixed'))''')
            _fill_missing_docs(cur, 'findings')
        if rev < 3:
            have = {r[1] for r in con.execute('PRAGMA table_info(word_rules)')}
            if 'decided_by' in have:
                cur.execute('''INSERT OR REPLACE INTO word_rule_ext
                               SELECT word, decided_by FROM word_rules
                               WHERE decided_by IS NOT NULL''')
                cur.execute('ALTER TABLE word_rules DROP COLUMN decided_by')
            for stmt in _sort_index_sql().split(';'):
                if stmt.strip():
                    cur.execute(stmt)
        cur.execute("INSERT OR REPLACE INTO meta VALUES('schema_rev', ?)",
                    (str(SCHEMA_REV),))
        con.commit()
    except sqlite3.OperationalError:
        con.rollback()                 # locked: the next connection retries


DECISIONS_TIMEOUT = 30.0      # seconds to wait for a decisions.db lock


def _decisions_con(outdir):
    """decisions.db. Its `decisions` table stays exactly as the legacy tool
    created it; the explicit scope of each row lives in `decision_scope`
    (unit '*' = global, anything else = this occurrence only)."""
    con = sqlite3.connect(os.path.join(outdir, DECISIONS_F),
                          timeout=DECISIONS_TIMEOUT)
    con.execute(f'PRAGMA busy_timeout={int(DECISIONS_TIMEOUT * 1000)}')
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if 'decisions' not in have:
        con.execute('''CREATE TABLE IF NOT EXISTS decisions(
            word TEXT, unit TEXT, errtype TEXT, verdict TEXT,
            suggestion TEXT, source TEXT, ref TEXT,
            PRIMARY KEY(word, unit))''')
    if 'decision_scope' not in have:
        con.execute('''CREATE TABLE IF NOT EXISTS decision_scope(
            word TEXT NOT NULL, unit TEXT NOT NULL, scope TEXT NOT NULL,
            decided_by TEXT, PRIMARY KEY(word, unit))''')
        # backfill legacy rows: only an explicit '*' row is global
        con.execute('''INSERT OR IGNORE INTO decision_scope(word, unit, scope)
            SELECT COALESCE(word, ''), COALESCE(unit, ''),
                   CASE WHEN unit = '*' THEN 'global' ELSE 'occurrence' END
            FROM decisions''')
    con.commit()
    return con


# ---------------------------------------------------------------------------
# importer
# ---------------------------------------------------------------------------

def _find_tokdiag_csv(outdir):
    hits = sorted(glob.glob(os.path.join(outdir, TOKDIAG_GLOB)))
    return hits[-1] if hits else None


def _tanach_extra(evidence_json, verified):
    if not verified:
        return json.loads(LEGACY_EXTRA)
    try:
        return json.loads(evidence_json) if evidence_json else {}
    except ValueError:
        return {}


class DecisionsLocked(PermissionError, ValueError):
    """decisions.db is locked by another program. Carries a Hebrew message;
    the server answers 423 with it, a book scan reports it as its error."""


def _withdraw_todo(con, keys):
    """Which (word, unit) `keys` have a decisions.db mirror to withdraw after
    their approval was dropped: keys this UI owns (an unowned row is the old
    tool's own decision) on which no approved / fixed finding still stands."""
    keys = {(w or '', u or '') for w, u in keys}
    return [(word, unit) for word, unit in sorted(keys)
            if _owns_decision(con, word, unit) and not con.execute(
                '''SELECT 1 FROM review r
                   JOIN findings f ON f.id = r.finding_id
                   WHERE COALESCE(f.word,'') = ? AND COALESCE(f.unit,'') = ?
                     AND r.status IN ('approved', 'fixed') LIMIT 1''',
                (word, unit)).fetchone()]


def _withdraw_accepts(dec, todo):
    """Delete the 'accept' rows of `todo` (see :func:`_withdraw_todo`) on an
    open decisions.db connection, leaving the commit to the caller. Returns
    the keys under which nothing is left; their ownership markers go only
    after decisions.db committed (:func:`_drop_owned`)."""
    gone = []
    for word, unit in todo:
        dec.execute("DELETE FROM decisions WHERE word = ? AND unit = ? "
                    "AND verdict = 'accept'", (word, unit))
        # nothing of ours left under this key (also after a retry)
        if not dec.execute('SELECT 1 FROM decisions '
                           'WHERE word = ? AND unit = ?',
                           (word, unit)).fetchone():
            dec.execute('DELETE FROM decision_scope '
                        'WHERE word = ? AND unit = ?', (word, unit))
            gone.append((word, unit))
    return gone


def _drop_owned(con, gone):
    for word, unit in gone:
        con.execute('DELETE FROM owned_decisions WHERE word = ? AND unit = ?',
                    (word, unit))


def _count_keys(gone, keys):
    """How many of the withdrawn keys `gone` are among `keys`."""
    return len(set(gone) & {(w or '', u or '') for w, u in keys})


def _clear_dropped_decisions(con, outdir, keys):
    """Withdraw the decisions.db 'accept' rows of dropped approvals: legacy
    Tanach approvals an import dropped, and approvals that went stale because
    their finding now proposes another correction.

    The approval is gone from `review`, but its mirror in decisions.db still
    carries the old suggestion: the old review tool would keep honouring it,
    and /api/import_legacy would turn it back into an approval. Only rows
    this UI owns are removed (an unowned row is the old tool's own decision),
    and only while no other approved / fixed finding still stands on the same
    (word, unit) key. The `history` record of the drop is kept.

    Order: runs inside the caller's still-open ui_review.db transaction and
    commits decisions.db FIRST; the ownership markers are deleted (in the
    caller's transaction) only after that commit. If the caller's commit then
    fails, the next refresh recomputes the same drop and finds nothing left
    to withdraw. If decisions.db cannot be written (another program holds a
    lock on it) nothing is withdrawn and :class:`DecisionsLocked` is raised,
    so the caller rolls back the drop as well and the next refresh redoes
    both. Returns the (word, unit) keys withdrawn.
    """
    if not keys or not os.path.exists(os.path.join(outdir, DECISIONS_F)):
        return []
    todo = _withdraw_todo(con, keys)
    if not todo:
        return []
    dec = None
    try:
        dec = _decisions_con(outdir)
        gone = _withdraw_accepts(dec, todo)
        dec.commit()
    except sqlite3.OperationalError as e:
        raise DecisionsLocked(hebrew.MESSAGES['decisions_locked']) from e
    finally:
        if dec is not None:
            dec.close()                 # without a commit: rolled back
    _drop_owned(con, gone)
    return gone


def _decisions_for_write(outdir):
    """decisions.db with its write lock already taken, for an action that
    writes it in one transaction with ui_review.db (undo, restoring a
    backup). Another program's lock fails the action up front with
    :class:`DecisionsLocked` (HTTP 423), before anything was changed."""
    dec = None
    try:
        dec = _decisions_con(outdir)
        dec.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as e:
        if dec is not None:
            dec.close()
        raise DecisionsLocked(
            hebrew.MESSAGES['decisions_locked_action']) from e
    return dec


def _commit_decisions_first(con, dec, gone=()):
    """Commit decisions.db, then ui_review.db (dropping the ownership markers
    of `gone`, the keys :func:`_withdraw_accepts` emptied, in between). If
    ui_review.db then fails, nothing there changed and repeating the action
    rewrites the same decisions.db rows; the other way round a failure would
    leave decisions.db behind for good."""
    try:
        dec.commit()
    except sqlite3.OperationalError as e:
        raise DecisionsLocked(
            hebrew.MESSAGES['decisions_locked_action']) from e
    _drop_owned(con, gone)
    con.commit()


def config_root_for_report(outdir):
    """The configured library (run_config.json), standing in as the root of
    a report.db that does not name its own (one written before scan_meta).

    None when run_config.json was rewritten after report.db: a later scan
    has been started since, so the setting may describe that scan instead.
    """
    from . import scanner
    rc = os.path.join(outdir, scanner.RUN_CONFIG)
    rep = os.path.join(outdir, REPORT_DB_F)
    try:
        if os.path.isfile(rc) and os.path.isfile(rep) \
                and os.path.getmtime(rc) > os.path.getmtime(rep):
            return None
    except OSError:
        return None
    try:
        lib = scanner.scan_config(outdir)['corpus'].get('library_dir')
    except Exception:                       # config unreadable -> the default
        lib = None
    return os.path.abspath(lib or DEFAULT_LIBRARY)


def _report_source(cur, outdir):
    """The identity of the report.db attached as ``rep``: the library root
    its scan read and when it was written, from its own scan_meta table."""
    if cur.execute("SELECT 1 FROM rep.sqlite_master WHERE type='table' "
                   "AND name='scan_meta'").fetchone():
        meta = dict(cur.execute('SELECT key, value FROM rep.scan_meta'))
        return {'root': meta.get('library_root') or None,
                'scanned_at': meta.get('scanned_at') or None,
                'basis': 'scan_meta'}
    root = config_root_for_report(outdir)
    return {'root': root, 'scanned_at': None,
            'basis': 'run_config' if root else 'unknown'}


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
    # identifies the report.db these findings come from (result_status.py:
    # a newer report.db means a refresh is due); taken before reading, so a
    # report.db replaced meanwhile reads as newer, never as already loaded
    report_mtime = os.path.getmtime(report_path)
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
        occ_cols = {r[1] for r in cur.execute(
            'PRAGMA rep.table_info(occurrences_full)')}
        evid = ''
        if {'evidence_kind', 'alternatives'} <= occ_cols:
            evid = ("""WHEN evidence_kind IS NOT NULL THEN
                       '{"evidence_kind": "' || evidence_kind ||
                       '", "alternatives": ' || COALESCE(alternatives, 'null')
                       || '}'""")
        cur.execute(f'''
            INSERT INTO imp
            SELECT 'error', errtype, word, suggestion, score,
                   ROUND({RANK_SQL}, 2), ctx_hits, sugg_local, book_repeat,
                   tanach,
                   CASE WHEN {VERIFIED_SQL} THEN 1 ELSE 0 END,
                   origin, source, ref, unit, doc, snippet,
                   CASE WHEN tanach = 2 THEN ? {evid} ELSE NULL END
            FROM rep.occurrences_full ORDER BY rowid''', (LEGACY_EXTRA,))
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
        verified = 'evidence' in {r[1] for r in cur.execute(
            'PRAGMA rep.table_info(tanach_errors_full)')}
        for word, canonical, src, ref, unit, snip, org, evid in cur.execute(
                'SELECT word, canonical, source, ref, unit, snippet, origin, '
                + ('evidence' if verified else 'NULL') +
                ' FROM rep.tanach_errors_full ORDER BY rowid').fetchall():
            extra = _tanach_extra(evid, verified)
            extra['canonical'] = canonical
            # only an outvoted minority reading ranks; unresolved rows inform
            rank = 4.0 if extra.get('evidence_kind') == \
                'tanach_edition_variant' else 0.0
            rows.append((
                'tanach_error', 'tanach_edition', word, canonical,
                rank, rank, None, None, None, None, 0, org, src, ref, unit,
                None, snip, json.dumps(extra, ensure_ascii=False)))
        cur.executemany('INSERT INTO imp VALUES(' +
                        ','.join('?' * 18) + ')', rows)
        counts['tanach_error'] = len(rows)

        # -- family 'tanach_match' -------------------------------------------
        rows = []
        verified = 'evidence' in {r[1] for r in cur.execute(
            'PRAGMA rep.table_info(tanach_matches_full)')}
        for word, src, ref, unit, snip, org, evid in cur.execute(
                'SELECT word, source, ref, unit, snippet, origin, '
                + ('evidence' if verified else 'NULL') +
                ' FROM rep.tanach_matches_full ORDER BY rowid').fetchall():
            rows.append((
                'tanach_match', 'tanach_match', word, None,
                0.0, 0.0, None, None, None, None, 0, org, src, ref, unit,
                None, snip, json.dumps(_tanach_extra(evid, verified),
                                       ensure_ascii=False)))
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

        # report.db has no doc for extra_space / tanach / tokdiag rows; a
        # book scan must still find (and only find) its own rows by doc
        _fill_missing_docs(cur, 'imp')

        # -- stable-id assignment (see module docstring) ---------------------
        cur.execute(f'''CREATE TEMP TABLE imp2 AS
            SELECT imp.*, rowid AS irow, {_legacy_lg()} AS lg,
                   ROW_NUMBER() OVER (PARTITION BY {KEY_COLS}
                                      ORDER BY rowid) AS seq
            FROM imp''')
        cur.execute('''CREATE INDEX temp.ix_imp2 ON imp2(
            family, word, unit, errtype, ref, seq)''')
        cur.execute(f'''CREATE TEMP TABLE oldmap AS
            SELECT id, family, COALESCE(word,'') AS w, COALESCE(unit,'') AS u,
                   errtype, COALESCE(ref,'') AS r, {_legacy_lg('f.')} AS lg,
                   ROW_NUMBER() OVER (PARTITION BY {KEY_COLS}
                                      ORDER BY id) AS seq
            FROM findings f''')
        cur.execute('''CREATE INDEX temp.ix_oldmap ON oldmap(
            family, w, u, errtype, r, seq)''')
        cur.execute('''CREATE TEMP TABLE assign AS
            SELECT i.irow AS irow, o.id AS old_id,
                   (o.lg = 2 OR o.lg != i.lg) AS lg_changed
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
        hw = cur.execute(
            "SELECT value FROM meta WHERE key='max_finding_id'").fetchone()
        try:
            max_id = max(max_id, int(hw[0])) if hw else max_id
        except (TypeError, ValueError):
            pass

        # Books that exist ONLY because of a single-book scan have no rows in
        # report.db, so the rebuild below would erase them and every decision
        # made on them. Carry those rows (and their review rows) across intact
        # — a full scan of a corpus that does not contain that book says
        # nothing about it. A book present in BOTH is rebuilt from report.db as
        # usual: report.db is the newer, corpus-wide truth for it.
        cur.execute('''CREATE TEMP TABLE keep_docs AS
            SELECT DISTINCT doc FROM findings
            WHERE doc IS NOT NULL AND doc != ''
              AND extra LIKE '%"book_scan": true%'
              AND doc NOT IN (SELECT DISTINCT COALESCE(doc,'')
                              FROM imp WHERE doc IS NOT NULL)''')
        cur.execute('''CREATE TEMP TABLE keep_rows AS
            SELECT * FROM findings
            WHERE doc IN (SELECT doc FROM keep_docs)''')
        kept = cur.execute('SELECT COUNT(*) FROM keep_rows').fetchone()[0]

        # rebuilding every row through ~10 secondary indexes costs more
        # than building them once afterwards, so they are dropped meanwhile
        index_sql = [r[0] for r in cur.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'findings' AND sql IS NOT NULL")]
        for name, in cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'findings' AND sql IS NOT NULL").fetchall():
            cur.execute(f'DROP INDEX "{name}"')
        cur.execute('DELETE FROM findings WHERE id NOT IN '
                    '(SELECT id FROM keep_rows)')
        cols17 = ('family, errtype, word, suggestion, score, rank, ctx_hits, '
                  'sugg_local, book_repeat, tanach, verified, origin, source, '
                  'ref, unit, doc, snippet, extra')
        icols = ', '.join('i.' + c.strip() for c in cols17.split(','))
        # a preserved book-scan row may hold an id that identity-matching also
        # assigned to a rebuilt row; the preserved row keeps it, so re-point
        # the colliding rebuilt row at a fresh id instead
        cur.execute('''UPDATE assign SET old_id = NULL
            WHERE old_id IN (SELECT id FROM keep_rows)''')
        cur.execute(f'''INSERT INTO findings(id, {cols17})
            SELECT a.old_id, {icols}
            FROM imp2 i JOIN assign a ON a.irow = i.irow
            WHERE a.old_id IS NOT NULL''')
        cur.execute(f'''INSERT INTO findings(id, {cols17})
            SELECT ? + ROW_NUMBER() OVER (ORDER BY i.irow), {icols}
            FROM imp2 i JOIN assign a ON a.irow = i.irow
            WHERE a.old_id IS NULL''', (max_id,))
        for sql in index_sql:
            cur.execute(sql)
        total = cur.execute('SELECT COUNT(*) FROM findings').fetchone()[0]
        new_max = cur.execute(
            'SELECT COALESCE(MAX(id), 0) FROM findings').fetchone()[0]
        cur.execute("INSERT OR REPLACE INTO meta VALUES('max_finding_id', ?)",
                    (str(max(max_id, new_max)),))

        # approvals that may rest on legacy Tanach evidence: re-check
        legacy = cur.execute('''
            SELECT r.finding_id, f.word, f.unit FROM review r
            JOIN assign a ON a.old_id = r.finding_id AND a.lg_changed
            JOIN findings f ON f.id = r.finding_id
            WHERE r.status = 'approved' ''').fetchall()
        ts = _now()
        for fid, word, _unit in legacy:
            cur.execute('INSERT INTO history(ts, action, finding_id, word, '
                        'old_status, new_status, note) '
                        'VALUES(?,?,?,?,?,?,?)',
                        (ts, 'legacy_recheck', fid, word, 'approved', None,
                         'tanach_legacy'))
            cur.execute('DELETE FROM review WHERE finding_id = ?', (fid,))
        counts['legacy_approvals_dropped'] = len(legacy)

        # drop review rows of vanished findings; count what survived
        cur.execute('''DELETE FROM review WHERE finding_id NOT IN
                       (SELECT id FROM findings)''')
        cur.execute('''DELETE FROM review_ext WHERE finding_id NOT IN
                       (SELECT finding_id FROM review)''')
        # approvals whose finding now proposes another correction
        stale = _mark_stale_approvals(cur)
        preserved = cur.execute('SELECT COUNT(*) FROM review').fetchone()[0]
        # withdraw the decisions.db mirror of both kinds of dropped approval,
        # decisions.db first (raises DecisionsLocked: all rolled back, the
        # next refresh redoes it)
        legacy_keys = [(w, u) for _, w, u in legacy]
        gone = _clear_dropped_decisions(con, outdir, legacy_keys + stale)

        counts.update({
            'total': total,
            # `kept` rows were never candidates for matching, so they are
            # neither "added" nor "removed" by this refresh
            'added': total - matched - kept,
            'removed': old_total - matched - kept,
            'preserved': preserved,
            'legacy_decisions_withdrawn': _count_keys(gone, legacy_keys),
            'decisions_withdrawn': len(gone),
            'book_scan_kept': kept,
            'stale_approvals': len(stale),
        })
        # what the scan behind these findings could not read: report.db
        # carries it only when locate wrote it partial (--allow-unread)
        _set_meta_json(cur, 'coverage', _report_coverage(cur))
        # a book scan's record stays only while the book's rows do
        books = _meta_json(cur, 'book_coverage') or {}
        kept_docs = {r[0] for r in cur.execute('SELECT doc FROM keep_docs')}
        _set_meta_json(cur, 'book_coverage',
                       {d: v for d, v in books.items() if d in kept_docs})
        cur.execute("INSERT OR REPLACE INTO meta VALUES('last_import', ?)",
                    (_now(),))
        cur.execute("INSERT OR REPLACE INTO meta VALUES('report_mtime', ?)",
                    (repr(report_mtime),))
        cur.execute("INSERT OR REPLACE INTO meta VALUES('import_counts', ?)",
                    (json.dumps(counts, ensure_ascii=False),))
        # which library these rows were scanned from, committed with the rows
        # themselves: the fixer reads it from here and never infers it from
        # timestamps (a book-scan merge also moves last_import)
        cur.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)',
                    (IMPORT_SOURCE_KEY,
                     json.dumps(_report_source(cur, outdir),
                                ensure_ascii=False)))
        cur.execute('DROP TABLE imp')
        cur.execute('DROP TABLE imp2')
        cur.execute('DROP TABLE oldmap')
        cur.execute('DROP TABLE assign')
        cur.execute('DROP TABLE keep_rows')
        cur.execute('DROP TABLE keep_docs')
        con.commit()
        con.execute('DETACH DATABASE rep')

        if migrate_legacy:
            counts['migrated'] = migrate_legacy_decisions(con, outdir)
        counts['seconds'] = round(time.time() - t0, 1)
        return counts
    finally:
        con.close()


# ---------------------------------------------------------------------------
# coverage: findings that rest on rows the scan could not read
# ---------------------------------------------------------------------------

def _meta_json(con, key):
    """A JSON object stored in `meta`, or None (absent or unreadable)."""
    row = con.execute('SELECT value FROM meta WHERE key = ?',
                      (key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _set_meta_json(con, key, value):
    if value:
        con.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)',
                    (key, json.dumps(value, ensure_ascii=False)))
    else:
        con.execute('DELETE FROM meta WHERE key = ?', (key,))


def _gap_summary(rec):
    """What the UI needs of a coverage record (core.gap_record): not the row
    ids, which may run to thousands."""
    return {k: rec[k] for k in ('unread_rows', 'allow_unread', 'unread_refs',
                                'inherited', 'unread_kind') if k in rec}


def _report_coverage(con):
    """The gap summary report.db (attached as `rep`) carries, or None."""
    if not con.execute("SELECT 1 FROM rep.sqlite_master WHERE type='table' "
                       "AND name='coverage'").fetchone():
        return None
    row = con.execute('SELECT info FROM rep.coverage').fetchone()
    try:
        info = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        info = None
    if not isinstance(info, dict):
        # a mark that cannot be read still says "partial", count unknown
        return {'unread_rows': None, 'allow_unread': None, 'unread_refs': []}
    return _gap_summary(info)


def import_book_scan(outdir, result):
    """Merge ONE book's scan into `findings`, additively.

    Unlike :func:`import_all` — which rebuilds the whole table from report.db —
    this touches only the rows of the scanned book:

    * rows of other books are never read or rewritten, so a book scan cannot
      disturb the rest of the review;
    * rows of *this* book are replaced (a re-scan is the new truth for it, not
      a second copy), and their review decisions are carried over by the same
      ``(family, word, unit, errtype, ref)`` + sequence identity ``import_all``
      uses, so approving a finding and re-scanning does not lose the approval;
    * ids are allocated above a monotonic high-water mark (``meta.max_finding_id``)
      rather than the live ``MAX(id)``. Deleting this book's rows can lower
      ``MAX(id)``, and reissuing those ids would silently attach this book's
      new findings to another book's leftover ``history`` rows — an undo would
      then write a decision onto the wrong finding.

    Which rows belong to the book
    -----------------------------
    ``doc`` is the book identity; the title is NOT (several books share one).
    ``import_all`` derives a doc for every row it can, but a DB line holding
    only an extra_space / tokdiag finding has no doc. Such a doc-less row is
    taken as this book's only when its unit names this doc, or when it has
    this book's title AND its line id lies within the span of this book's own
    line ids — a same-titled other book lives on other line ids.

    A book that yields no findings still counts as scanned: its old rows are
    removed, which is the correct outcome for a book the user has just fixed.

    Returns a dict of counts (added / replaced / preserved decisions).
    """
    doc = result.get('doc')
    if not doc:
        raise ValueError(hebrew.SCAN_MESSAGES['book_no_doc'])
    title = result.get('title') or ''
    con = connect(outdir)
    try:
        cur = con.cursor()
        cur.execute('BEGIN IMMEDIATE')
        line_ids = [v for v in cur.execute(
            "SELECT MIN(CAST(unit AS INTEGER)), MAX(CAST(unit AS INTEGER)) "
            "FROM findings WHERE doc = ? AND unit != '' "
            "AND unit NOT GLOB '*[^0-9]*'", (doc,)).fetchone()
            if v is not None]
        line_ids += [int(r['unit']) for r in (result.get('findings') or []) +
                     (result.get('space_errors') or [])
                     if str(r.get('unit') or '').isdigit()]
        lo, hi = (min(line_ids), max(line_ids)) if line_ids else (1, 0)
        where = ('''(f.doc = ? OR ((f.doc IS NULL OR f.doc = '') AND (
                       magiah_doc_of_unit(f.unit) = ?
                       OR (f.source = ? AND f.unit != ''
                           AND f.unit NOT GLOB '*[^0-9]*'
                           AND CAST(f.unit AS INTEGER) BETWEEN ? AND ?))))''')
        wparams = (doc, doc, title, lo, hi)

        # -- remember the decisions currently attached to this book ---------
        cur.execute(f'''CREATE TEMP TABLE oldbook AS
            SELECT f.id AS id, f.family AS family,
                   COALESCE(f.word,'') AS w, COALESCE(f.unit,'') AS u,
                   f.errtype AS errtype, COALESCE(f.ref,'') AS r,
                   {_legacy_lg('f.')} AS lg,
                   ROW_NUMBER() OVER (PARTITION BY {KEY_COLS}
                                      ORDER BY f.id) AS seq
            FROM findings f WHERE {where}''', wparams)
        old_total = cur.execute('SELECT COUNT(*) FROM oldbook').fetchone()[0]
        cur.execute('''CREATE TEMP TABLE oldrev AS
            SELECT o.family, o.w, o.u, o.errtype, o.r, o.seq,
                   r.status, r.note, r.custom_suggestion, r.updated_at,
                   x.scope, x.approved_suggestion, x.decided_by, x.flag,
                   x.prev_decision
            FROM oldbook o JOIN review r ON r.finding_id = o.id
            LEFT JOIN review_ext x ON x.finding_id = o.id
            WHERE o.lg = 0 OR r.status != 'approved' ''')
        # ...and the legacy approvals it leaves behind (as in import_all)
        cur.execute('''CREATE TEMP TABLE dropped AS
            SELECT o.family, o.w, o.u, o.errtype, o.r, o.seq
            FROM oldbook o JOIN review r ON r.finding_id = o.id
            WHERE o.lg != 0 AND r.status = 'approved' ''')

        # -- out with the old rows of this book ------------------------------
        cur.execute('DELETE FROM review WHERE finding_id IN '
                    '(SELECT id FROM oldbook)')
        cur.execute('DELETE FROM review_ext WHERE finding_id IN '
                    '(SELECT id FROM oldbook)')
        # history rows of removed findings would otherwise dangle onto whatever
        # id is issued next
        cur.execute('DELETE FROM history WHERE finding_id IN '
                    '(SELECT id FROM oldbook)')
        cur.execute('DELETE FROM findings WHERE id IN '
                    '(SELECT id FROM oldbook)')

        # -- in with the new -------------------------------------------------
        # Staged in a temp table so `rank` and `verified` can be computed by
        # the very same RANK_SQL / VERIFIED_SQL expressions import_all uses —
        # re-deriving the formula in Python here would be a second source of
        # truth that could silently drift from core.py.
        # `rank` for non-error families is log-scaled in Python (as in
        # import_all): SQLite's LOG() is a compile-time option and must not be
        # relied on — the frozen build may ship without it.
        cur.execute('''CREATE TEMP TABLE bimp(
            family TEXT, errtype TEXT, word TEXT, suggestion TEXT,
            score REAL, ctx_hits INT, sugg_local INT, book_repeat INT,
            tanach INT, origin TEXT, source TEXT, ref TEXT, unit TEXT,
            snippet TEXT, extra TEXT, flat_rank REAL)''')
        extra_json = json.dumps({'book_scan': True,
                                 'ctx_scope': result.get('ctx_scope')},
                                ensure_ascii=False)
        err_rows = [('error', r.get('errtype') or '', r.get('word'),
                     r.get('suggestion'), float(r.get('score') or 0.0),
                     int(r.get('ctx_hits') or 0), int(r.get('sugg_local') or 0),
                     int(r.get('book_repeat') or 0), int(r.get('tanach') or 0),
                     r.get('origin'), r.get('source'), r.get('ref'),
                     r.get('unit'), r.get('snippet'), extra_json, None)
                    for r in (result.get('findings') or [])]
        space_rows = []
        for r in result.get('space_errors') or []:
            jf = int(r.get('join_freq') or 0)
            space_rows.append((
                'extra_space', 'extra_space',
                (r.get('part1') or '') + ' ' + (r.get('part2') or ''),
                r.get('joined'), float(jf), None, None, None, None,
                r.get('origin'), r.get('source'), r.get('ref'), r.get('unit'),
                r.get('snippet'),
                json.dumps({'part1': r.get('part1'), 'part2': r.get('part2'),
                            'joined': r.get('joined'), 'join_freq': jf,
                            'book_scan': True}, ensure_ascii=False),
                round(math.log10(jf + 1), 2)))
        cur.executemany('INSERT INTO bimp VALUES(' + ','.join('?' * 16) + ')',
                        err_rows + space_rows)

        cols = ('family, errtype, word, suggestion, score, rank, ctx_hits, '
                'sugg_local, book_repeat, tanach, verified, origin, source, '
                'ref, unit, doc, snippet, extra')
        # Monotonic id high-water mark. MAX(id) alone is unsafe here: this
        # book's rows were just deleted, so if they held the highest ids those
        # ids would be reissued to a *different* book's findings — and any
        # history row still pointing at them (undo) would then act on the wrong
        # finding. The mark only ever grows.
        max_id = cur.execute(
            'SELECT COALESCE(MAX(id), 0) FROM findings').fetchone()[0]
        row = cur.execute(
            "SELECT value FROM meta WHERE key='max_finding_id'").fetchone()
        try:
            max_id = max(max_id, int(row[0])) if row else max_id
        except (TypeError, ValueError):
            pass
        cur.execute(f'''INSERT INTO findings(id, {cols})
            SELECT ? + ROW_NUMBER() OVER (ORDER BY rowid),
                   family, errtype, word, suggestion, score,
                   CASE WHEN family = 'error' THEN ROUND({RANK_SQL}, 2)
                        ELSE flat_rank END,
                   ctx_hits, sugg_local, book_repeat, tanach,
                   CASE WHEN family = 'error' AND ({VERIFIED_SQL})
                        THEN 1 ELSE 0 END,
                   origin, source, ref, unit, ?, snippet, extra
            FROM bimp ORDER BY rowid''', (max_id, doc))
        added = cur.rowcount
        cur.execute('DROP TABLE bimp')
        cur.execute("INSERT OR REPLACE INTO meta VALUES('max_finding_id', ?)",
                    (str(max_id + added),))

        # -- carry the old decisions onto the matching new rows -------------
        cur.execute(f'''CREATE TEMP TABLE newbook AS
            SELECT f.id AS id, f.family AS family,
                   COALESCE(f.word,'') AS w, COALESCE(f.unit,'') AS u,
                   f.errtype AS errtype, COALESCE(f.ref,'') AS r,
                   ROW_NUMBER() OVER (PARTITION BY {KEY_COLS}
                                      ORDER BY f.id) AS seq
            FROM findings f WHERE f.doc = ? AND f.id > ?''', (doc, max_id))
        cur.execute('''INSERT OR REPLACE INTO review(
                finding_id, status, note, custom_suggestion, updated_at)
            SELECT n.id, o.status, o.note, o.custom_suggestion, o.updated_at
            FROM newbook n JOIN oldrev o
              ON o.family = n.family AND o.w = n.w AND o.u = n.u
             AND o.errtype = n.errtype AND o.r = n.r AND o.seq = n.seq''')
        preserved = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur.execute('''INSERT OR REPLACE INTO review_ext(
                finding_id, scope, approved_suggestion, decided_by, flag,
                prev_decision)
            SELECT n.id, o.scope, o.approved_suggestion, o.decided_by, o.flag,
                   o.prev_decision
            FROM newbook n JOIN oldrev o
              ON o.family = n.family AND o.w = n.w AND o.u = n.u
             AND o.errtype = n.errtype AND o.r = n.r AND o.seq = n.seq''')
        # approvals whose re-scanned row proposes another correction
        stale = _mark_stale_approvals(
            cur, 'f.doc = ? AND f.id > ?', (doc, max_id))

        # -- log the dropped legacy approvals --------------------------------
        # The old ids are gone with their history; the entry points at the
        # new row of the same identity when the re-scan still has one.
        legacy = cur.execute('''
            SELECT n.id, d.w, d.u FROM dropped d LEFT JOIN newbook n
              ON n.family = d.family AND n.w = d.w AND n.u = d.u
             AND n.errtype = d.errtype AND n.r = d.r AND n.seq = d.seq
            ''').fetchall()
        ts = _now()
        for fid, word, _unit in legacy:
            cur.execute('INSERT INTO history(ts, action, finding_id, word, '
                        'old_status, new_status, note) '
                        'VALUES(?,?,?,?,?,?,?)',
                        (ts, 'legacy_recheck', fid, word, 'approved', None,
                         'tanach_legacy'))
        # ...and withdraw the decisions.db mirror of both kinds of dropped
        # approval, decisions.db first (raises DecisionsLocked: all rolled
        # back, the next re-scan redoes it)
        legacy_keys = [(w, u) for _, w, u in legacy]
        gone = _clear_dropped_decisions(con, outdir, legacy_keys + stale)

        # each scan of the book replaces its coverage record; a complete one
        # removes it
        books = _meta_json(cur, 'book_coverage') or {}
        books.pop(doc, None)
        if result.get('coverage'):
            books[doc] = dict(_gap_summary(result['coverage']), title=title)
        _set_meta_json(cur, 'book_coverage', books)
        cur.execute("INSERT OR REPLACE INTO meta VALUES('last_import', ?)",
                    (_now(),))
        for t in ('oldbook', 'oldrev', 'dropped', 'newbook'):
            cur.execute(f'DROP TABLE {t}')
        con.commit()
        return {'doc': doc, 'title': result.get('title'),
                'added': added, 'replaced': old_total,
                'preserved': preserved, 'stale_approvals': len(stale),
                'legacy_approvals_dropped': len(legacy),
                'legacy_decisions_withdrawn': _count_keys(gone, legacy_keys),
                'decisions_withdrawn': len(gone),
                'findings': len(result.get('findings') or []),
                'space_errors': len(result.get('space_errors') or [])}
    finally:
        con.close()


def _mark_stale_approvals(cur, where='1', params=()):
    """Approvals whose finding now proposes a different correction drop back
    to 'pending' with flag 'stale_approval'; the old decision is kept in
    prev_decision. A user-typed correction stays bound to its occurrence.
    Returns the (word, unit) of every demoted finding."""
    rows = cur.execute(f'''
        SELECT f.id, f.word, f.unit, r.status, r.note, r.updated_at,
               x.approved_suggestion, x.decided_by, x.scope
        FROM findings f JOIN review r ON r.finding_id = f.id
        JOIN review_ext x ON x.finding_id = f.id
        WHERE {where} AND r.status = 'approved'
          AND COALESCE(r.custom_suggestion, '') = ''
          AND x.approved_suggestion IS NOT NULL
          AND x.approved_suggestion != COALESCE(f.suggestion, '')''',
        params).fetchall()
    ts = _now()
    for r in rows:
        prev = json.dumps({'status': r[3], 'approved_suggestion': r[6],
                           'decided_by': r[7], 'scope': r[8],
                           'updated_at': r[5]}, ensure_ascii=False)
        cur.execute("UPDATE review SET status = 'pending', updated_at = ? "
                    'WHERE finding_id = ?', (ts, r[0]))
        cur.execute("UPDATE review_ext SET flag = 'stale_approval', "
                    'prev_decision = ? WHERE finding_id = ?', (prev, r[0]))
    return [(r[1], r[2]) for r in rows]


def _mark_stale_ids(con, ids):
    if not ids:
        return []
    return _mark_stale_approvals(
        con.cursor(), 'f.id IN (SELECT value FROM json_each(?))',
        (json.dumps(list(ids)),))


def _ids_of(con, stale, candidates):
    """Which of `candidates` were just marked stale."""
    if not stale:
        return []
    return [r[0] for r in con.execute(
        "SELECT finding_id FROM review_ext WHERE flag = 'stale_approval' "
        'AND finding_id IN (SELECT value FROM json_each(?))',
        (json.dumps(list(candidates)),))]


def migrate_legacy_decisions(con, outdir):
    """OPT-IN migration of the old decisions.db into review / word_rules.

    accept -> approved (decision suggestion kept as custom_suggestion when it
    differs from the finding's own suggestion); reject -> not_error; ignore
    -> ignored. A per-unit row is a decision on that occurrence only (scope
    'occurrence'); a unit='*' row becomes a word_rules row of the same status
    (the old tool's "reject everywhere" -> 'not_error', the only global
    scope; this UI's earlier versions also mirrored word-wide approvals
    there). Matching is on (word, unit); existing review rows are never
    overwritten. The decider of a legacy row is unknown, so decided_by stays
    empty. Returns counts.

    Exception: an accept on a Tanach-backed finding (a tanach_legacy row, a
    row with tanach != 0, or a tanach_* family row) is imported as 'unsure',
    without a custom suggestion, its old suggestion kept only in the note
    (counted in ``recheck``). Such an accept was given while the old trigram
    heuristic replaced the suggestion and boosted the rank, so it may approve
    a correction nobody would approve today. 'unsure' rather than skipping:
    the user still sees that an old decision exists, and nothing is approved
    or exported until they decide again. A differing suggestion alone is not
    enough to demote: the old tool's "accept with my correction" writes one.
    """
    dec_path = os.path.join(outdir, DECISIONS_F)
    out = {'review': 0, 'word_rules': 0, 'unmatched': 0, 'decisions': 0,
           'recheck': 0}
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
            # the old tool writes '*' only for "reject everywhere"; this UI
            # mirrors every word-wide status there, an approval included
            con.execute('INSERT OR REPLACE INTO word_rules(word, status, '
                        'updated_at) VALUES(?,?,?)', (word, status, ts))
            out['word_rules'] += 1
            continue
        fids = con.execute(
            "SELECT id, suggestion, (COALESCE(tanach, 0) != 0 OR family IN "
            "('tanach_error', 'tanach_match') OR COALESCE(extra, '') LIKE "
            "'%tanach_legacy%') FROM findings WHERE word = ? AND unit = ?",
            (word, unit)).fetchall()
        if not fids:
            out['unmatched'] += 1
            continue
        for fid, fsugg, tanach_backed in fids:
            st, note, custom = status, None, None
            if verdict == 'accept' and tanach_backed:
                st = 'unsure'
                note = hebrew.MESSAGES['legacy_accept_recheck'].format(
                    sugg=sugg or '—')
            elif (verdict == 'accept' and sugg and sugg != (fsugg or '')):
                custom = sugg
            n = con.execute(
                'INSERT OR IGNORE INTO review(finding_id, status, note, '
                'custom_suggestion, updated_at) VALUES(?,?,?,?,?)',
                (fid, st, note, custom, ts)).rowcount
            if n:
                # bound to what was approved, like an approval made here
                con.execute(
                    'INSERT OR REPLACE INTO review_ext(finding_id, scope, '
                    'approved_suggestion) VALUES(?,?,?)',
                    (fid, 'occurrence',
                     (custom or fsugg or '') if st == 'approved' else None))
            out['review'] += n
            if st == 'unsure':
                out['recheck'] += n
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


def get_meta(con, outdir=None):
    """Labels, counts and — given the output folder — ``result_status``:
    whether the findings are those of the latest scan (result_status.py)."""
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
            'evidence_labels': hebrew.EVIDENCE_LABELS,
            'extra_labels': hebrew.EXTRA_LABELS,
            # no findings yet -> the UI shows its "run a scan first" screen
            'no_scan': total == 0,
            'last_import': last_import[0] if last_import else None,
            'result_status': (result_status.build(con, outdir)
                              if outdir else None)}


def get_books(con, origin=None, q=None):
    """Books by stable key (doc). Two books with one title stay two
    entries; `source` is the title to display, `key` is what to filter on."""
    where, params = [], []
    if origin:
        where.append('f.origin = ?')
        params.append(origin)
    if q:
        where.append("f.source LIKE '%' || ? || '%'")
        params.append(q)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = con.execute(f'''
        SELECT {BOOKKEY} AS bkey, MIN(f.source), COUNT(*) AS count,
               SUM(CASE WHEN {EFF} = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN {EFF} = 'approved' THEN 1 ELSE 0 END)
        FROM findings f {JOINS} {wsql}
        GROUP BY bkey ORDER BY COUNT(*) DESC''', params).fetchall()
    return [{'key': r[0], 'source': r[1] or '', 'count': r[2],
             'pending_count': r[3] or 0, 'approved_count': r[4] or 0}
            for r in rows]


def _book_key_where(key):
    """SQL for "this finding belongs to book `key`" (see BOOKKEY); split so
    the common doc case can use idx_f_doc."""
    if key.startswith('src:'):
        return "(f.doc IS NULL OR f.doc = '') AND f.source = ?", key[4:]
    return 'f.doc = ?', key


def _findings_where(filters):
    where, params = [], []
    if filters.get('origin'):
        where.append('f.origin = ?')
        params.append(filters['origin'])
    if filters.get('book_key'):
        sql, p = _book_key_where(filters['book_key'])
        where.append(sql)
        params.append(p)
    elif filters.get('book'):
        # legacy: by display title, which several books may share
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
        # not %-formatting: EFF itself holds LIKE '%...%' patterns
        where.append(f"{EFF} IN ({','.join('?' * len(sts))})")
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


def _direction(sort, direction):
    d = 'ASC' if str(direction).lower() == 'asc' else 'DESC'
    if sort == 'rank' and direction in (None, ''):
        d = 'DESC'
    return d


def _count(con, filters, wsql, params):
    # the status joins cost ~4 lookups per row; only a status filter needs them
    joins = JOINS if filters.get('status') else ''
    return con.execute(f'SELECT COUNT(*) FROM findings f {joins} {wsql}',
                       params).fetchone()[0]


def query_findings(con, filters, sort='rank', direction='desc',
                   page=1, page_size=50):
    page = max(1, int(page or 1))
    page_size = min(500, max(1, int(page_size or 50)))
    order = SORTS.get(sort, SORTS['rank']).format(d=_direction(sort,
                                                               direction))
    wsql, params = _findings_where(filters)
    total = _count(con, filters, wsql, params)
    rows = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion, {EXT_COLS}
        FROM findings f {JOINS} {EXT_JOIN} {wsql}
        ORDER BY {order} LIMIT ? OFFSET ?''',
        params + [page_size, (page - 1) * page_size]).fetchall()
    return [_rowdict(r) for r in rows], total


# Keyset sort keys. Each list is the ORDER BY of one sort (all one
# direction, then f.id), and SORT_INDEXES indexes exactly these expressions:
# a page is then an index range seek, never a sort of the whole table.
# NULLs are folded so that `<`/`>` on the keys agree with ORDER BY.
_UNIT_KEY = 'COALESCE(%s, 0)' % UNIT_ORDER.format(u='{p}unit')
KEYSET_KEYS = {
    'rank': ['COALESCE({p}rank, -1e300)'],
    'source': ["COALESCE({p}source, '')", _UNIT_KEY],
    'word': ["COALESCE({p}word, '')", "COALESCE({p}unit, '')"],
}
SORT_INDEXES = {
    'idx_f_kr': KEYSET_KEYS['rank'],
    'idx_f_ks': KEYSET_KEYS['source'],
    'idx_f_kw': KEYSET_KEYS['word'],
}


def _sort_index_sql():
    return ''.join(
        'CREATE INDEX IF NOT EXISTS %s ON findings(%s);\n'
        % (name, ', '.join(e.format(p='') for e in exprs))
        for name, exprs in SORT_INDEXES.items())


def _mix32(v):
    v = ((v ^ (v >> 16)) * 0x45d9f3b) & 0xffffffff
    v = ((v ^ (v >> 16)) * 0x45d9f3b) & 0xffffffff
    return v ^ (v >> 16)


def _shuffle(x, bits, keys):
    """A seed-keyed bijection on [0, 2**bits) (balanced Feistel network)."""
    h = bits // 2
    mask = (1 << h) - 1
    left, right = x >> h, x & mask
    for k in keys:
        left, right = right, left ^ (_mix32(right ^ k) & mask)
    return (left << h) | right


def _id_bits(con):
    """The random walk's width: every id issued so far is below 2**bits
    (the high-water mark counts too: ids are never reissued)."""
    top = con.execute('SELECT MAX(id) FROM findings').fetchone()[0] or 0
    row = con.execute(
        "SELECT value FROM meta WHERE key='max_finding_id'").fetchone()
    try:
        top = max(top, int(row[0])) if row else top
    except (TypeError, ValueError):
        pass
    bits = max(2, top.bit_length())
    return bits + bits % 2


def _random_page(con, wsql, params, page_size, cursor, seed):
    """'random' sort: walk a seed-keyed permutation of the id space.

    Position k of the walk holds id _shuffle(k); every id is visited once,
    a different seed gives a different order, and a page costs only primary
    key lookups for a few hundred candidate ids — no table-wide sort.

    The cursor is [position, bits]. One that is not a position of a walk
    this table could have started (two integers, an even width no wider
    than the ids issued so far) is refused: a crafted width of 62 made the
    server walk 2**62 positions.
    """
    bits = _id_bits(con)
    k = -1
    if cursor:
        try:
            ck, cbits = json.loads(cursor)
            if not all(type(v) is int for v in (ck, cbits)):
                raise TypeError
        except (ValueError, TypeError):
            raise ValueError(hebrew.MESSAGES['bad_request'])
        # ids only grow: a cursor's walk is never wider than one started now
        if not (2 <= cbits <= bits and cbits % 2 == 0
                and -1 <= ck < (1 << cbits)):
            raise ValueError(hebrew.MESSAGES['bad_request'])
        k, bits = ck, cbits
    digest = hashlib.sha256(str(seed or 0).encode('utf-8')).digest()
    keys = [int.from_bytes(digest[i:i + 4], 'big') for i in range(0, 16, 4)]
    end = 1 << bits
    where = (wsql + ' AND ' if wsql else 'WHERE ') + \
        'f.id IN (SELECT value FROM json_each(?))'
    found, batch = [], page_size * 2
    while len(found) <= page_size and k < end - 1:
        span = range(k + 1, min(end, k + 1 + batch))
        ids = [_shuffle(i, bits, keys) for i in span]
        rows = {r['id']: r for r in con.execute(f'''
            SELECT f.*, {EFF} AS effective_status, r.note AS note,
                   r.custom_suggestion AS custom_suggestion, {EXT_COLS}
            FROM findings f {JOINS} {EXT_JOIN} {where}''',
            params + [json.dumps(ids)])}
        found += [(i, rows[fid]) for i, fid in zip(span, ids) if fid in rows]
        k = span[-1]
        batch = min(batch * 4, 50000)
    more = len(found) > page_size
    found = found[:page_size]
    nxt = json.dumps([found[-1][0], bits]) if more else None
    return [_rowdict(r) for _, r in found], nxt


def query_findings_page(con, filters, sort='rank', direction='desc',
                        page_size=50, cursor='', seed=None, with_total=False):
    """Keyset ("cursor") paging: rows strictly after `cursor` in a stable
    order. Unlike OFFSET paging it never skips rows when decisions remove
    earlier rows from a status-filtered set — the card queue relies on it.

    The total is counted only on request: it costs a scan of the filtered
    set, which a page itself never needs.
    Returns (rows, total or None, next_cursor); next_cursor is None on the
    last page.
    """
    page_size = min(500, max(1, int(page_size or 50)))
    wsql, params = _findings_where(filters)
    total = _count(con, filters, wsql, params) if with_total else None
    if sort == 'random':
        rows, nxt = _random_page(con, wsql, params, page_size, cursor, seed)
        return rows, total, nxt
    d = _direction(sort, direction)
    exprs = [e.format(p='f.') for e in KEYSET_KEYS.get(sort,
                                                       KEYSET_KEYS['rank'])]
    keys = exprs + ['f.id']
    if cursor:
        try:
            vals = json.loads(cursor)
        except ValueError:
            vals = None
        if not isinstance(vals, list) or len(vals) != len(keys):
            raise ValueError(hebrew.MESSAGES['bad_request'])
        op = '<' if d == 'DESC' else '>'
        # the redundant bound on the leading key is what lets SQLite seek
        # into the index instead of scanning it from the start
        cond = (f'{exprs[0]} {op}= ? AND ({", ".join(keys)}) {op} '
                f'({", ".join("?" * len(keys))})')
        wsql = (wsql + ' AND ' if wsql else 'WHERE ') + cond
        params = params + [vals[0]] + vals
    kcols = ', '.join(f'{e} AS _k{i}' for i, e in enumerate(keys))
    order = ', '.join(f'{e} {d}' for e in keys)
    rows = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion, {EXT_COLS}, {kcols}
        FROM findings f {JOINS} {EXT_JOIN} {wsql}
        ORDER BY {order} LIMIT ?''', params + [page_size + 1]).fetchall()
    more = len(rows) > page_size
    out, last = [], None
    for r in rows[:page_size]:
        d_ = _rowdict(r)
        last = [d_.pop(f'_k{i}') for i in range(len(keys))]
        out.append(d_)
    return out, total, (json.dumps(last) if more else None)


def get_finding(con, fid):
    row = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion,
               r.updated_at AS updated_at, {EXT_COLS}
        FROM findings f {JOINS} {EXT_JOIN} WHERE f.id = ?''',
        (fid,)).fetchone()
    if row is None:
        return None
    d = _rowdict(row)
    d['history'] = [dict(h) for h in con.execute(
        '''SELECT * FROM history
           WHERE finding_id = ? OR (action IN ('word_rule', 'book_rule',
                                               'replacement_rule')
                                    AND word = ?)
           ORDER BY id DESC LIMIT 50''', (fid, d['word']))]
    return d


# ---------------------------------------------------------------------------
# status writes + decisions.db sync
# ---------------------------------------------------------------------------

class StatusConflict(ValueError):
    """The finding is no longer in the state the client acted on (G3): a
    double click, or another tab/reviewer got there first."""

    def __init__(self, current):
        super().__init__(hebrew.MESSAGES['status_conflict'])
        self.current = current


def _own_decision(con, word, unit):
    """Record that this UI owns the decisions.db row keyed (word, unit)."""
    con.execute('INSERT OR IGNORE INTO owned_decisions VALUES(?,?)',
                (word or '', unit or ''))


def _owns_decision(con, word, unit):
    return con.execute(
        'SELECT 1 FROM owned_decisions WHERE word = ? AND unit = ?',
        (word or '', unit or '')).fetchone() is not None


def _sync_decision(con, dec, finding, status, custom_suggestion=None,
                   word_scope=False, decided_by=None, scope=None):
    """Mirror one status into old-schema decisions.db.

    Only a word-scope decision becomes the global unit='*' row (the one the
    detect whitelist reads); every other decision is mirrored on its own
    unit, with its real scope in decision_scope (an audit table for readers
    of decisions.db). ``custom_suggestion`` is the correction to record —
    callers pass the approved one, never a suggestion the user did not see. ``con`` is the ui_review.db connection, used for the ownership
    table. Returns a Hebrew warning string when a delete was declined because
    the matching decisions.db row is a legacy row this UI never wrote.
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
        dec.execute('DELETE FROM decision_scope WHERE word = ? AND unit = ?',
                    (word or '', unit))
        con.execute('DELETE FROM owned_decisions WHERE word = ? AND unit = ?',
                    (word or '', unit or ''))
    else:
        f = dict(finding)
        sugg = custom_suggestion or f.get('suggestion') or ''
        dec.execute('INSERT OR REPLACE INTO decisions(word, unit, errtype, '
                    'verdict, suggestion, source, ref) VALUES(?,?,?,?,?,?,?)',
                    (word, unit, f.get('errtype') or '', verdict, sugg,
                     f.get('source') or '', f.get('ref') or ''))
        dec.execute('INSERT OR REPLACE INTO decision_scope VALUES(?,?,?,?)',
                    (word or '', unit,
                     'global' if word_scope else (scope or 'occurrence'),
                     decided_by))
        _own_decision(con, word, unit)
    return None


REVIEW_COLS = ('status', 'note', 'custom_suggestion', 'updated_at')
EXT_FIELDS = ('scope', 'approved_suggestion', 'decided_by', 'flag',
              'prev_decision')
# what makes two review states different (timestamps and actor do not)
_SAME_FIELDS = ('status', 'note', 'custom_suggestion', 'scope',
                'approved_suggestion', 'flag')

RULES = {   # scope -> (table, key columns)
    'word': ('word_rules', ('word',)),
    'book': ('book_rules', ('word', 'doc')),
    'replacement': ('replacement_rules', ('word', 'suggestion')),
}


def _get_review(con, fid):
    """The full decision on one finding (review + review_ext), or None."""
    r = con.execute(f'''
        SELECT r.status, r.note, r.custom_suggestion, r.updated_at,
               {', '.join('x.' + c for c in EXT_FIELDS)}
        FROM review r LEFT JOIN review_ext x ON x.finding_id = r.finding_id
        WHERE r.finding_id = ?''', (fid,)).fetchone()
    return dict(r) if r else None


def _put_review(con, fid, row):
    """Write (or with row=None delete) the full decision on one finding."""
    if row is None:
        con.execute('DELETE FROM review WHERE finding_id = ?', (fid,))
        con.execute('DELETE FROM review_ext WHERE finding_id = ?', (fid,))
        return
    con.execute('INSERT OR REPLACE INTO review(finding_id, status, note, '
                'custom_suggestion, updated_at) VALUES(?,?,?,?,?)',
                (fid,) + tuple(row.get(c) for c in REVIEW_COLS))
    con.execute('INSERT OR REPLACE INTO review_ext(finding_id, %s) '
                'VALUES(?,%s)' % (', '.join(EXT_FIELDS),
                                  ','.join('?' * len(EXT_FIELDS))),
                (fid,) + tuple(row.get(c) for c in EXT_FIELDS))


def _rule_key(scope, f):
    if scope == 'book':
        return {'word': f['word'],
                'doc': f['doc'] or 'src:' + (f['source'] or '')}
    if scope == 'replacement':
        return {'word': f['word'], 'suggestion': f['suggestion'] or ''}
    return {'word': f['word']}


def _get_rule(con, scope, key):
    table, cols = RULES[scope]
    who = (WORD_DECIDER.replace('w.word', 'word_rules.word')
           if scope == 'word' else 'decided_by')
    r = con.execute(
        f'SELECT status, {who} AS decided_by, updated_at FROM {table} WHERE '
        + ' AND '.join(f'{c} = ?' for c in cols),
        [key[c] for c in cols]).fetchone()
    return dict(r) if r else None


def _put_rule(con, scope, key, row):
    table, cols = RULES[scope]
    vals = [key[c] for c in cols]
    where = ' AND '.join(f'{c} = ?' for c in cols)
    if scope == 'word':
        # word_rules keeps its original 3 columns (positional writers exist)
        if row is None:
            con.execute('DELETE FROM word_rules WHERE word = ?', vals)
            con.execute('DELETE FROM word_rule_ext WHERE word = ?', vals)
            return
        con.execute('INSERT OR REPLACE INTO word_rules(word, status, '
                    'updated_at) VALUES(?,?,?)',
                    vals + [row['status'], row.get('updated_at') or _now()])
        con.execute('INSERT OR REPLACE INTO word_rule_ext VALUES(?,?)',
                    vals + [row.get('decided_by')])
        return
    if row is None:
        con.execute(f'DELETE FROM {table} WHERE {where}', vals)
        return
    con.execute(f'INSERT OR REPLACE INTO {table}({", ".join(cols)}, status, '
                f'decided_by, updated_at) VALUES({",".join("?" * len(cols))}'
                ',?,?,?)',
                vals + [row['status'], row.get('decided_by'),
                        row.get('updated_at') or _now()])


def _sync_rule(con, dec, scope, key, status, decided_by=None):
    """Only the global (word) rule has a decisions.db mirror."""
    if scope != 'word':
        return None
    fake = {'word': key['word'], 'unit': '*', 'errtype': '',
            'suggestion': '', 'source': '', 'ref': ''}
    return _sync_decision(con, dec, fake, status, word_scope=True,
                          decided_by=decided_by)


def set_status(con, outdir, ids, status, note=None, custom_suggestion=None,
               scope='occurrence', decided_by='human', expect_status=None):
    """Set the status of one or more findings (one undo step).

    scope says how far the decision reaches beyond the clicked finding:
    'occurrence' (this finding only), 'replacement' (every finding proposing
    the same word -> suggestion), 'book' (the word in this book) or 'word'
    (everywhere — the only scope that feeds the detect whitelist).

    expect_status, when given, is the effective status the client saw; if a
    finding no longer has it nothing is written and StatusConflict is raised.
    A write that changes nothing is skipped and leaves no history entry.
    History stores the complete prior state, so undo restores it exactly.
    """
    if status not in hebrew.STATUSES:
        raise ValueError(hebrew.MESSAGES['bad_status'])
    if scope not in SCOPES:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    if status in ('approved', 'fixed') and scope in ('book', 'word'):
        # an approval is bound to one suggestion; a book/word rule would
        # approve whatever a future scan proposes
        raise ValueError(hebrew.MESSAGES['scoped_approval'])
    if decided_by not in ACTORS:
        decided_by = 'human'
    ids = [int(i) for i in ids]
    if not ids:
        raise ValueError(hebrew.MESSAGES['no_ids'])
    # the expect_status check and the write are one transaction, so two tabs
    # cannot both pass the check
    if not con.in_transaction:
        con.execute('BEGIN IMMEDIATE')
    try:
        found = []
        for fid in ids:
            f = con.execute(f'''
                SELECT f.*, {EFF} AS eff FROM findings f {JOINS}
                WHERE f.id = ?''', (fid,)).fetchone()
            if f is not None:
                found.append(f)
        if expect_status is not None and any(f['eff'] != expect_status
                                             for f in found):
            raise StatusConflict({str(f['id']): f['eff'] for f in found})
        return _set_status_locked(con, outdir, ids, found, status, note,
                                  custom_suggestion, scope, decided_by)
    except BaseException:
        con.rollback()
        raise


def _set_status_locked(con, outdir, ids, found, status, note,
                       custom_suggestion, scope, decided_by):
    ts = _now()
    action = 'bulk' if len(ids) > 1 else 'set_status'
    # decisions.db committed before ui_review.db: a write that changes
    # nothing is skipped, so a retry could never re-sync a decisions.db that
    # fell behind a committed ui_review.db
    dec = _decisions_for_write(outdir)
    updated = word_rules = 0
    warnings = []

    def warn(w):
        if w and w not in warnings:
            warnings.append(w)

    try:
        for f in found:
            fid = f['id']
            old = _get_review(con, fid)
            if status == 'pending' and not note and not custom_suggestion:
                new = None
            else:
                base = old or {}
                new = {'status': status,
                       'note': note if note is not None else base.get('note'),
                       'custom_suggestion': (
                           custom_suggestion if custom_suggestion is not None
                           else base.get('custom_suggestion')),
                       'updated_at': ts, 'scope': scope,
                       'decided_by': decided_by, 'flag': None,
                       'prev_decision': base.get('prev_decision')}
                new['approved_suggestion'] = (
                    (new['custom_suggestion'] or f['suggestion'] or '')
                    if status in ('approved', 'fixed') else None)
            same = (old is None and new is None) or (
                old is not None and new is not None
                and all(old.get(k) == new.get(k) for k in _SAME_FIELDS))
            rule = None
            if scope in RULES and f['word']:
                key = _rule_key(scope, f)
                old_rule = _get_rule(con, scope, key)
                if not (old_rule is None and status == 'pending') and not (
                        old_rule is not None
                        and old_rule['status'] == status):
                    rule = (key, old_rule)
            if same and rule is None:
                continue
            if not same:
                con.execute(
                    'INSERT INTO history(ts, action, finding_id, word, '
                    'old_status, new_status, note, prev_state, decided_by) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (ts, action, fid, f['word'], f['eff'], status, note,
                     json.dumps({'review': old}, ensure_ascii=False),
                     decided_by))
                _put_review(con, fid, new)
                warn(_sync_decision(
                    con, dec, f, new['status'] if new else 'pending',
                    new and (new['approved_suggestion']
                             or new['custom_suggestion']),
                    decided_by=decided_by, scope=scope))
                updated += 1
            if rule is not None:
                key, old_rule = rule
                con.execute(
                    'INSERT INTO history(ts, action, finding_id, word, '
                    'old_status, new_status, note, prev_state, decided_by) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (ts, scope + '_rule', None, f['word'],
                     old_rule['status'] if old_rule else None, status, note,
                     json.dumps({'kind': scope, 'key': key, 'row': old_rule},
                                ensure_ascii=False), decided_by))
                _put_rule(con, scope, key, None if status == 'pending' else
                          {'status': status, 'decided_by': decided_by,
                           'updated_at': ts})
                warn(_sync_rule(con, dec, scope, key, status, decided_by))
                word_rules += 1
        _commit_decisions_first(con, dec)
    finally:
        dec.close()                     # without a commit: rolled back
    out = {'updated': updated, 'word_rules': word_rules, 'status': status,
           'scope': scope, 'decided_by': decided_by, 'ts': ts}
    if warnings:
        out['warnings'] = warnings
    return out


# history actions undo skips: its own entries, and imports' legacy drops
NOT_UNDOABLE = "('undo', 'legacy_recheck')"


def _legacy_dropped_after(con, fid, hid):
    """An import dropped this finding's approval (``legacy_recheck``) after
    history entry `hid`: undoing that entry must not approve it again."""
    return con.execute("SELECT 1 FROM history WHERE action = 'legacy_recheck' "
                       'AND finding_id = ? AND id > ? LIMIT 1',
                       (fid, hid)).fetchone() is not None


def undo(con, outdir):
    """Revert the most recent not-yet-undone history group (one API call =
    one ts = one undo step, bulk included). Returns what was reverted.

    Entries carrying prev_state are restored exactly (review row, note,
    custom suggestion, scope, rule rows — or their absence); older entries
    written before prev_state existed fall back to restoring the status.

    A ``legacy_recheck`` entry is not a user action but an import dropping an
    approval that rested on the old Tanach heuristic; undo never restores it
    and reverts the user's last own action instead. Nor does reverting an
    earlier entry bring such an approval back: an approval an import dropped
    after that entry is restored as pending.

    decisions.db is written in the same step and committed first (see
    :func:`_commit_decisions_first`); a lock on it fails the undo with
    :class:`DecisionsLocked` and changes nothing. ui_review.db is locked
    before decisions.db, in the order every status write takes them, and
    for the whole step, so two undo requests revert two steps."""
    if not con.in_transaction:
        con.execute('BEGIN IMMEDIATE')
    try:
        undone = {r[0] for r in con.execute(
            "SELECT note FROM history WHERE action = 'undo'")}
        row = con.execute(
            f"SELECT ts FROM history WHERE action NOT IN {NOT_UNDOABLE} "
            + ('AND ts NOT IN (%s) ' % ','.join('?' * len(undone))
               if undone else '')
            + 'ORDER BY id DESC LIMIT 1',
            list(undone)).fetchone()
        if row is not None:
            entries = con.execute(
                f"SELECT * FROM history WHERE ts = ? AND action NOT IN "
                f"{NOT_UNDOABLE} ORDER BY id DESC", (row[0],)).fetchall()
            dec = _decisions_for_write(outdir)
    except BaseException:
        con.rollback()
        raise
    if row is None:
        con.rollback()
        return None
    group_ts = row[0]
    reverted, restored = [], []
    warnings = []

    def warn(w):
        if w and w not in warnings:
            warnings.append(w)

    try:
        for e in entries:
            old = e['old_status']
            prev = None
            if e['prev_state']:
                try:
                    prev = json.loads(e['prev_state'])
                except ValueError:
                    prev = None
            relegacy = False
            if prev is not None and 'kind' in prev:
                kind, key, rrow = prev['kind'], prev['key'], prev['row']
                _put_rule(con, kind, key, rrow)
                warn(_sync_rule(con, dec, kind, key,
                                rrow['status'] if rrow else 'pending',
                                rrow and rrow.get('decided_by')))
            elif prev is not None and e['finding_id'] is not None:
                rv = prev.get('review')
                if rv and rv.get('status') == 'approved' and \
                        _legacy_dropped_after(con, e['finding_id'], e['id']):
                    rv, relegacy = None, True
                _put_review(con, e['finding_id'], rv)
                restored.append(e['finding_id'])
                f = con.execute('SELECT * FROM findings WHERE id = ?',
                                (e['finding_id'],)).fetchone()
                if f is not None:
                    warn(_sync_decision(
                        con, dec, f, rv['status'] if rv else 'pending',
                        rv and (rv.get('approved_suggestion')
                                or rv.get('custom_suggestion')),
                        decided_by=rv and rv.get('decided_by'),
                        scope=rv and rv.get('scope')))
            elif e['action'] == 'word_rule':
                if old is None or old == 'pending':
                    con.execute('DELETE FROM word_rules WHERE word = ?',
                                (e['word'],))
                else:
                    con.execute('INSERT OR REPLACE INTO word_rules(word, '
                                'status, updated_at) VALUES(?,?,?)',
                                (e['word'], old, _now()))
                warn(_sync_rule(con, dec, 'word', {'word': e['word']},
                                old or 'pending'))
            elif e['finding_id'] is not None:
                f = con.execute('SELECT * FROM findings WHERE id = ?',
                                (e['finding_id'],)).fetchone()
                if old == 'approved' and \
                        _legacy_dropped_after(con, e['finding_id'], e['id']):
                    old, relegacy = None, True
                if old is None or old == 'pending':
                    _put_review(con, e['finding_id'], None)
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
                    warn(_sync_decision(con, dec, f, old or 'pending'))
            entry = {'finding_id': e['finding_id'], 'word': e['word'],
                     'restored': 'pending' if relegacy else (old or 'pending'),
                     'was': e['new_status']}
            if relegacy:
                entry['legacy_recheck'] = True
            reverted.append(entry)
        con.execute('INSERT INTO history(ts, action, note) VALUES(?,?,?)',
                    (_now(), 'undo', group_ts))
        # an approval brought back onto a finding whose suggestion has since
        # changed must not come back as approved...
        stale = _mark_stale_ids(con, restored)
        stale_ids = set(_ids_of(con, stale, restored))
        for e in reverted:
            if e['finding_id'] in stale_ids:
                e['restored'] = 'pending'
                e['stale_approval'] = True
        # ...nor stay in decisions.db, which the loop above just wrote
        gone = _withdraw_accepts(dec, _withdraw_todo(con, stale))
        _commit_decisions_first(con, dec, gone)
    except BaseException:
        con.rollback()
        raise
    finally:
        dec.close()                     # without a commit: rolled back
    out = {'reverted': len(reverted), 'group_ts': group_ts,
           'entries': reverted, 'stale_approvals': len(stale)}
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
            SELECT MIN(f.source), f.origin, COUNT(*),
                   SUM(CASE WHEN {EFF} != 'pending' THEN 1 ELSE 0 END),
                   {status_cols}, {BOOKKEY} AS bkey
            FROM findings f {JOINS}
            GROUP BY bkey, f.origin
            ORDER BY COUNT(*) DESC LIMIT 50'''):
        src, org, total, done = row[0], row[1], row[2], row[3]
        books.append({'source': src or '', 'key': row[-1],
                      'origin': org or '',
                      'origin_hebrew': hebrew.origin_hebrew(org),
                      'total': total, 'done': done or 0,
                      'statuses': {s: row[4 + i] or 0 for i, s in
                                   enumerate(hebrew.STATUS_ORDER)}})
    totals = {st: n for st, n in con.execute(
        f'SELECT {EFF}, COUNT(*) FROM findings f {JOINS} GROUP BY {EFF}')}
    # per-finding decisions by who made them; '' = made before this was
    # recorded (unknown), never counted as a human decision
    by_actor = {}
    for actor, st, n in con.execute('''
            SELECT COALESCE(x.decided_by, ''), r.status, COUNT(*)
            FROM review r LEFT JOIN review_ext x
              ON x.finding_id = r.finding_id
            GROUP BY 1, 2'''):
        by_actor.setdefault(actor or 'unknown', {})[st] = n
    return {'origins': origins, 'errtypes': errtypes, 'books': books,
            'totals': totals, 'by_actor': by_actor}


def get_fixlist(con, book=None, origin=None, statuses=None, book_key=None):
    """Fixer-mode worklist. Without a book returns the books (by stable key)
    that still have findings in the requested statuses (default: approved),
    with remaining counts; with `book_key` (or the legacy title `book`)
    returns that book's worklist in reading order (unit asc)."""
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
    if book is None and book_key is None:
        rows = con.execute(f'''
            SELECT MIN(f.source), f.origin, COUNT(*), {BOOKKEY} AS bkey
            FROM findings f {JOINS}
            WHERE {EFF} IN ({sph}){owhere}
            GROUP BY bkey, f.origin ORDER BY COUNT(*) DESC''',
            params).fetchall()
        books = [{'source': r[0] or '', 'key': r[3], 'origin': r[1] or '',
                  'origin_hebrew': hebrew.origin_hebrew(r[1]),
                  'remaining': r[2]} for r in rows]
        return {'books': books, 'rows': books, 'total': len(books)}
    if book_key is not None:
        bsql, bval = _book_key_where(book_key)
    else:
        bsql, bval = 'f.source = ?', book
    params.append(bval)
    rows = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion, {EXT_COLS}
        FROM findings f {JOINS} {EXT_JOIN}
        WHERE {EFF} IN ({sph}){owhere} AND {bsql}
        ORDER BY {UNIT_ORDER.format(u='f.unit')} ASC, f.id ASC''',
        params).fetchall()
    total = con.execute(f'''
        SELECT COUNT(*) FROM findings f {JOINS}
        WHERE {EFF} IN ('approved','fixed') AND {bsql}''',
        (bval,)).fetchone()[0]
    fixed = con.execute(f'''
        SELECT COUNT(*) FROM findings f {JOINS}
        WHERE {EFF} = 'fixed' AND {bsql}''', (bval,)).fetchone()[0]
    items = [_rowdict(r) for r in rows]
    return {'book': book, 'book_key': book_key, 'items': items,
            'rows': items, 'total': len(items), 'fixed': fixed,
            'total_approved': total}


# ---------------------------------------------------------------------------
# Fixer mode — per-FILE worklists and the file-edit log
# ---------------------------------------------------------------------------
# Everything here keys on the file path carried by `unit`, never on `source`.
# `source` is only the filename stem, and in the real corpus one stem covers
# many different books (measured: 'פרק א' -> 35 distinct files), so grouping a
# writable worklist by `source` would let a correction land in a book it was
# never meant for.

def get_fixer_books(con, origin=None, statuses=None, query=None):
    """Books that still have work, grouped by the FILE they live in.

    DB-backed books have no file to edit; they are still listed (so nothing
    disappears from the corrector's view) but marked ``editable: False`` and
    offered as an export instead.
    """
    from . import patcher
    if not statuses:
        statuses = ['approved']
    if isinstance(statuses, str):
        statuses = [s for s in statuses.split(',') if s]
    sph = ','.join('?' * len(statuses))
    params = list(statuses)
    where = ''
    if origin:
        where = ' AND f.origin = ?'
        params.append(origin)
    rows = con.execute(f'''
        SELECT f.unit, f.origin, f.source, {EFF} AS st, COUNT(*)
        FROM findings f {JOINS}
        WHERE ({EFF} IN ({sph}) OR {EFF} = 'fixed'){where}
        GROUP BY f.unit, f.origin, f.source, st''', params).fetchall()

    books = {}
    for unit, origin_v, source, st, n in rows:
        key = patcher.book_key_of(unit)
        editable = key is not None
        if key is None:                    # a DB book: one entry per title
            key = 'db:%s|%s' % (origin_v or '', source or '')
        b = books.get(key)
        if b is None:
            title, folder = source or '', ''
            if editable:
                rel = key.split(':', 1)[1]
                title = os.path.splitext(rel.rsplit('/', 1)[-1])[0] or title
                folder = rel.rsplit('/', 1)[0] if '/' in rel else ''
                folder = folder.replace('\\', '/')
            b = books[key] = {
                'key': key, 'title': title, 'folder': folder,
                'origin': origin_v or '',
                'origin_hebrew': hebrew.origin_hebrew(origin_v),
                'editable': editable, 'kind': key.split(':', 1)[0],
                'remaining': 0, 'fixed': 0, 'total': 0}
        b['total'] += n
        if st == 'fixed':
            b['fixed'] += n
        else:
            b['remaining'] += n
    out = list(books.values())
    if query:
        q = query.strip()
        if q:
            out = [b for b in out
                   if q in b['title'] or q in b['folder']]
    out.sort(key=lambda b: (-b['remaining'], b['title']))
    return out


def get_fixer_items(con, key, statuses=None, origin=None):
    """The worklist for ONE file, in reading order, with occurrence numbers.

    Rows are matched by the file part of `unit` (see the module note above),
    so two books sharing a filename never share a worklist.

    Occurrence numbers are assigned over EVERY finding of the file, whatever
    its status or origin, and only then is the list filtered. The writer
    (fixer_api.apply) numbers them that way, and the page must agree with it:
    numbered among the shown statuses only, a repeated word whose sibling is
    'not_error' looked "count changed" on the page while apply accepted it.
    """
    from . import patcher
    if not statuses:
        statuses = ['approved']
    if isinstance(statuses, str):
        statuses = [s for s in statuses.split(',') if s]
    # narrow with LIKE (indexable-ish, keeps the scan small), then confirm
    # each row by parsing its unit — LIKE alone could match a longer path
    rows = con.execute(f'''
        SELECT f.*, {EFF} AS effective_status, r.note AS note,
               r.custom_suggestion AS custom_suggestion
        FROM findings f {JOINS}
        WHERE f.unit LIKE ?
        ORDER BY {UNIT_ORDER.format(u='f.unit')} ASC, f.id ASC''',
        (key + ':%',)).fetchall()
    items = []
    for r in rows:
        d = _rowdict(r)
        if patcher.book_key_of(d.get('unit')) != key:
            continue                       # LIKE over-matched a longer path
        parsed = patcher.resolve_unit_lineno(d.get('unit'))
        if parsed is None:
            continue
        d['lineno'] = parsed
        d['correction'] = d.get('custom_suggestion') or d.get('suggestion') or ''
        items.append(d)
    patcher.assign_occurrences(items)
    keep = set(statuses) | {'fixed'}
    return [d for d in items if d['effective_status'] in keep
            and (not origin or d.get('origin') == origin)]


def get_fixer_mode(con, key, default='replace'):
    row = con.execute('SELECT value FROM meta WHERE key = ?',
                      ('fixer_mode:' + key,)).fetchone()
    return (row[0] if row and row[0] in ('replace', 'bracket') else default)


def set_fixer_mode(con, key, mode):
    if mode not in ('replace', 'bracket'):
        raise ValueError(hebrew.MESSAGES['bad_request'])
    con.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)',
                ('fixer_mode:' + key, mode))
    con.commit()
    return mode


# Fixer additions, applied lazily so existing databases upgrade in place:
# file_edits gains journal_id (ties a row to its write-journal intent, which
# makes recovery idempotent), backup_sha and source_root; fixer_sources holds
# the library root each scan was made against ('report' or 'doc:<doc>') and,
# for book scans, the fingerprint of the bytes the scan read (file_sha and
# file_size, taken when the file was read — not when the result was merged).
_FILE_EDIT_COLS = ('journal_id', 'backup_sha', 'source_root')
_SOURCE_COLS = (('file_sha', 'TEXT'), ('file_size', 'INTEGER'))


def _ensure_fixer_schema(con):
    have = {r[1] for r in con.execute('PRAGMA table_info(file_edits)')}
    missing = [c for c in _FILE_EDIT_COLS if c not in have]
    src = {r[1] for r in con.execute('PRAGMA table_info(fixer_sources)')}
    if not missing and all(c in src for c, _t in _SOURCE_COLS):
        return
    for col in missing:
        try:
            con.execute(f'ALTER TABLE file_edits ADD COLUMN {col} TEXT')
        except sqlite3.OperationalError:
            pass                       # another connection added it first
    con.execute('''CREATE TABLE IF NOT EXISTS fixer_sources(
        scope TEXT PRIMARY KEY, root TEXT NOT NULL, stamp TEXT,
        recorded_at TEXT NOT NULL, file_sha TEXT, file_size INTEGER)''')
    for col, typ in _SOURCE_COLS:
        if src and col not in src:
            try:
                con.execute(f'ALTER TABLE fixer_sources ADD COLUMN {col} {typ}')
            except sqlite3.OperationalError:
                pass
    con.commit()


def record_file_edit(con, path, book_key, backup, mode, finding_ids, detail,
                     fp_before, fp_after, journal_id=None, backup_sha=None,
                     source_root=None):
    _ensure_fixer_schema(con)
    cur = con.execute(
        'INSERT INTO file_edits(ts, path, book_key, backup, mode, '
        'finding_ids, detail, fp_before, fp_after, undone_at, journal_id, '
        'backup_sha, source_root) '
        'VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?)',
        (_now(), path, book_key, backup, mode,
         json.dumps(finding_ids), detail, fp_before, fp_after, journal_id,
         backup_sha, source_root))
    con.commit()
    return cur.lastrowid


def find_file_edit_by_journal(con, journal_id):
    _ensure_fixer_schema(con)
    row = con.execute('SELECT id FROM file_edits WHERE journal_id = ?',
                      (journal_id,)).fetchone()
    return row[0] if row else None


def get_live_edit_entries(con, key):
    """``{finding_id: detail entry}`` of the newest live edit per finding."""
    out = {}
    for row in con.execute('SELECT detail FROM file_edits WHERE book_key = ? '
                           'AND undone_at IS NULL ORDER BY id', (key,)):
        try:
            entries = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        for e in entries:
            if isinstance(e, dict) and e.get('id') is not None:
                out[e['id']] = e
    return out


def record_source_root(con, scope, root, stamp=None, file_sha=None,
                       file_size=None):
    _ensure_fixer_schema(con)
    con.execute('INSERT OR REPLACE INTO fixer_sources(scope, root, stamp, '
                'recorded_at, file_sha, file_size) VALUES(?,?,?,?,?,?)',
                (scope, os.path.abspath(root) if root else '', stamp, _now(),
                 file_sha, file_size))
    con.commit()


def get_source_root(con, scope):
    _ensure_fixer_schema(con)
    row = con.execute('SELECT root, stamp, file_sha, file_size '
                      'FROM fixer_sources WHERE scope = ?',
                      (scope,)).fetchone()
    return ({'root': row[0] or None, 'stamp': row[1], 'file_sha': row[2],
             'file_size': row[3]} if row else None)


def advance_scan_file_sha(con, scope, old, new, new_size):
    """The fixer's own write moved the file from `old` to `new` without
    moving any line, so line numbers recorded at scan time stay valid.

    Only a fingerprint taken when the scan READ the file (it has a size) is
    carried forward; one recorded at merge time proves nothing to carry."""
    _ensure_fixer_schema(con)
    con.execute('UPDATE fixer_sources SET file_sha = ?, file_size = ? '
                'WHERE scope = ? AND file_sha = ? AND file_size IS NOT NULL',
                (new, new_size, scope, old))
    con.commit()


def get_import_source(con):
    """What import_all recorded about the report it imported —
    ``{'root', 'scanned_at', 'basis'}`` — or None for an import made by an
    older version, which recorded nothing. A book-scan merge never changes
    it: those rows carry their own source (``fixer_sources``)."""
    row = con.execute('SELECT value FROM meta WHERE key = ?',
                      (IMPORT_SOURCE_KEY,)).fetchone()
    try:
        src = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None
    return src if isinstance(src, dict) else None


def full_import_stamp(con):
    """A value that changes with every full import (import_all writes
    import_counts; a book-scan merge does not), so a root pinned to an old
    import can tell it is stale. NOT last_import: book-scan merges move it."""
    row = con.execute("SELECT value FROM meta WHERE key = 'import_counts'"
                      ).fetchone()
    return (row[0] if row else None) or ''


def record_book_scan_source(outdir, result):
    """Pin the library root a single-book scan read, under 'doc:<doc>',
    with the fingerprint of the bytes the scan read.

    The scan's library comes from the request, not from run_config.json, so
    without this the fixer could only guess which folder the rows describe.
    The fingerprint is the scan's own (``file_sha`` / ``file_size`` of the
    result), never a hash of the file taken now: a book edited while it was
    being scanned would then look unchanged, and its rows would skip the
    identity checks that tell a line from its parallel twin.
    """
    if result.get('kind') not in ('library', 'file') \
            or not result.get('doc') or not result.get('path'):
        return None
    root = None
    if result['kind'] == 'library':
        root = os.path.abspath(result['path'])
        for _part in str(result['doc']).split('/'):
            root = os.path.dirname(root)
    sha, size = result.get('file_sha'), result.get('file_size')
    if not sha or size is None:
        sha = size = None              # an unproven read is never "unchanged"
    con = connect(outdir)
    try:
        record_source_root(con, 'doc:' + result['doc'], root, file_sha=sha,
                           file_size=size)
    finally:
        con.close()
    return root


def merge_book_scan(outdir, result):
    """Merge a single-book scan and record where it came from — the one
    path both the web UI and the CLI use, so neither can forget the root."""
    counts = import_book_scan(outdir, result)
    record_book_scan_source(outdir, result)
    return counts


def get_file_edits(con, key=None, limit=50, include_undone=True):
    sql = 'SELECT * FROM file_edits'
    params, where = [], []
    if key:
        where.append('book_key = ?')
        params.append(key)
    if not include_undone:
        where.append('undone_at IS NULL')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    out = []
    for r in con.execute(sql, params):
        d = dict(r)
        try:
            d['finding_ids'] = json.loads(d['finding_ids'])
        except (ValueError, TypeError):
            d['finding_ids'] = []
        try:
            d['detail'] = json.loads(d['detail'])
        except (ValueError, TypeError):
            d['detail'] = []
        out.append(d)
    return out


def get_file_edit(con, edit_id):
    row = con.execute('SELECT * FROM file_edits WHERE id = ?',
                      (edit_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d['finding_ids'] = json.loads(d['finding_ids'])
    except (ValueError, TypeError):
        d['finding_ids'] = []
    try:
        d['detail'] = json.loads(d['detail'])
    except (ValueError, TypeError):
        d['detail'] = []
    return d


def mark_edit_undone(con, edit_id):
    con.execute('UPDATE file_edits SET undone_at = ? WHERE id = ?',
                (_now(), edit_id))
    con.commit()


def edits_touching(con, finding_ids):
    """Live (not undone) file edits that wrote any of these findings.

    Used to warn that undoing a STATUS does not restore the book file. Every
    live edit is scanned, with no cap: a missed match would silently drop that
    warning, and a corrector who is not told the file still holds the
    correction will assume Ctrl+Z put the book back. `detail` is the large
    column and is not needed here, so it is left in the database.
    """
    want = set(finding_ids)
    out = []
    for row in con.execute('SELECT id, ts, path, book_key, backup, mode, '
                           'finding_ids, fp_before, fp_after, undone_at '
                           'FROM file_edits WHERE undone_at IS NULL '
                           'ORDER BY id DESC'):
        d = dict(row)
        try:
            d['finding_ids'] = json.loads(d['finding_ids'])
        except (ValueError, TypeError):
            d['finding_ids'] = []
        if want & set(d['finding_ids']):
            out.append(d)
    return out


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
               v.updated_at, f.family, f.word, f.unit, f.errtype, f.ref,
               x.scope, x.approved_suggestion, x.decided_by, x.flag,
               x.prev_decision
        FROM review v JOIN findings f ON f.id = v.finding_id
        LEFT JOIN review_ext x ON x.finding_id = v.finding_id''')]
    word_rules = [dict(r) for r in con.execute(
        'SELECT w.*, e.decided_by FROM word_rules w '
        'LEFT JOIN word_rule_ext e ON e.word = w.word')]
    book_rules = [dict(r) for r in con.execute('SELECT * FROM book_rules')]
    replacement_rules = [dict(r) for r in con.execute(
        'SELECT * FROM replacement_rules')]
    history = [dict(r) for r in con.execute(
        'SELECT * FROM history ORDER BY id')]
    # the file-edit log records which books were physically rewritten and
    # where their .bak files are; losing it to a reset would strand the
    # backups, so it travels with every UI backup
    file_edits = [dict(r) for r in con.execute(
        'SELECT * FROM file_edits ORDER BY id')]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'ts': _now(), 'review': review, 'word_rules': word_rules,
                   'book_rules': book_rules,
                   'replacement_rules': replacement_rules,
                   'history': history, 'file_edits': file_edits},
                  f, ensure_ascii=False)
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
    for t in ('review', 'review_ext', 'word_rules', 'word_rule_ext',
              'book_rules', 'replacement_rules', 'history'):
        con.execute(f'DELETE FROM {t}')
    con.commit()
    counts['decisions'] = 0
    if scope == 'all':
        dec = _decisions_con(outdir)
        try:
            counts['decisions'] = dec.execute(
                'SELECT COUNT(*) FROM decisions').fetchone()[0]
            dec.execute('DELETE FROM decisions')
            dec.execute('DELETE FROM decision_scope')
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
    # ui_review.db first, then decisions.db: the order every write takes
    if not con.in_transaction:
        con.execute('BEGIN IMMEDIATE')
    try:
        dec = _decisions_for_write(outdir)
    except BaseException:
        con.rollback()
        raise
    out = {'review': 0, 'word_rules': 0, 'unmatched': 0, 'history': 0}
    warnings, restored = [], []
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
                row = {k: rv.get(k) for k in REVIEW_COLS + EXT_FIELDS}
                row['updated_at'] = row['updated_at'] or _now()
                row['scope'] = row['scope'] or 'occurrence'
                _put_review(con, fid, row)
                restored.append(fid)
                f = con.execute('SELECT * FROM findings WHERE id = ?',
                                (fid,)).fetchone()
                w = _sync_decision(con, dec, f, rv['status'],
                                   row['approved_suggestion']
                                   or row['custom_suggestion'],
                                   decided_by=row['decided_by'],
                                   scope=row['scope'])
                if w and w not in warnings:
                    warnings.append(w)
                out['review'] += 1
        for wr in data.get('word_rules', []):
            _put_rule(con, 'word', {'word': wr['word']}, wr)
            fake = {'word': wr['word'], 'unit': '*', 'errtype': '',
                    'suggestion': '', 'source': '', 'ref': ''}
            w = _sync_decision(con, dec, fake, wr['status'], word_scope=True)
            if w and w not in warnings:
                warnings.append(w)
            out['word_rules'] += 1
        for scope in ('book', 'replacement'):
            table, cols = RULES[scope]
            for rr in data.get(table, []):
                _put_rule(con, scope, {c: rr.get(c) for c in cols}, rr)
                out['word_rules'] += 1
        for h in data.get('history', []):
            con.execute('INSERT INTO history(ts, action, finding_id, word, '
                        'old_status, new_status, note, prev_state, '
                        'decided_by) VALUES(?,?,?,?,?,?,?,?,?)',
                        (h.get('ts'), h.get('action'), h.get('finding_id'),
                         h.get('word'), h.get('old_status'),
                         h.get('new_status'), h.get('note'),
                         h.get('prev_state'), h.get('decided_by')))
            out['history'] += 1
        # the backup may predate a re-scan that changed what was approved:
        # such an approval returns as pending, and leaves decisions.db again
        stale = _mark_stale_ids(con, restored)
        out['stale_approvals'] = len(stale)
        gone = _withdraw_accepts(dec, _withdraw_todo(con, stale))
        _commit_decisions_first(con, dec, gone)
    except BaseException:
        con.rollback()
        raise
    finally:
        dec.close()                     # without a commit: rolled back
    if warnings:
        out['warnings'] = warnings
    return out
