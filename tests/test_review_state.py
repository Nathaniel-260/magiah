# -*- coding: utf-8 -*-
"""Review state: decision scopes, stale approvals, book identity, undo,
legacy migration, actor provenance, the card queue and the to_send export.

Run:  python -X utf8 -m pytest -q tests/test_review_state.py

Every test builds its own scan folder under tempfile.mkdtemp(); nothing here
touches a real library. The card-queue tests drive the real
``static/cardqueue.js`` through node against a live server and are skipped
when node is not installed.
"""
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from magiah import book_scan, core                              # noqa: E402
from magiah.webui import db, export, server                     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, 'js', 'card_queue_harness.js')
NODE = shutil.which('node')

FCOLS = ('id, family, errtype, word, suggestion, rank, verified, origin, '
         'source, ref, unit, doc, snippet')


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_rs_')
        self.outdir = os.path.join(self.tmp, 'scan')
        os.makedirs(self.outdir)
        self.con = db.connect(self.outdir)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, fid, word, sugg, unit, doc='1', source='ספר', rank=5.0,
            family='error', errtype='edit1_sub', origin='o1', ref='r'):
        self.con.execute(
            f'INSERT INTO findings({FCOLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (fid, family, errtype, word, sugg, rank, 0, origin, source, ref,
             unit, doc, ''))
        self.con.commit()

    def eff(self, fid):
        return db.get_finding(self.con, fid)['effective_status']

    def review(self, fid):
        r = self.con.execute(
            'SELECT r.*, x.scope, x.approved_suggestion, x.decided_by, '
            'x.flag, x.prev_decision FROM review r '
            'LEFT JOIN review_ext x ON x.finding_id = r.finding_id '
            'WHERE r.finding_id = ?', (fid,)).fetchone()
        return dict(r) if r else None


def whitelist_cfg():
    return types.SimpleNamespace(whitelist=())


# ---------------------------------------------------------------------------
# D1 — decision scopes
# ---------------------------------------------------------------------------

class TestD1Scopes(Case):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10', doc='1')
        self.add(2, 'בית', 'ביתו', '11', doc='1')
        self.add(3, 'בית', 'בות', '20', doc='2', source='אחר')
        self.add(4, 'בית', 'ביתו', '21', doc='2', source='אחר')

    def test_occurrence_rejection_is_not_global(self):
        db.set_status(self.con, self.outdir, [1], 'not_error')
        self.assertEqual(self.eff(1), 'not_error')
        self.assertEqual(self.eff(2), 'pending')
        self.assertNotIn('בית', core.load_review_rejections(self.outdir))
        self.assertNotIn('בית', book_scan._load_whitelist(whitelist_cfg(),
                                                          self.outdir))

    def test_word_scope_is_the_only_global_exclusion(self):
        db.set_status(self.con, self.outdir, [1], 'not_error', scope='word')
        self.assertIn('בית', core.load_review_rejections(self.outdir))
        self.assertIn('בית', book_scan._load_whitelist(whitelist_cfg(),
                                                       self.outdir))
        self.assertEqual(self.eff(3), 'not_error')

    def test_replacement_rejection_is_per_pair(self):
        db.set_status(self.con, self.outdir, [1], 'not_error',
                      scope='replacement')
        self.assertEqual(self.eff(2), 'not_error')    # same word -> same fix
        self.assertEqual(self.eff(4), 'not_error')
        self.assertEqual(self.eff(3), 'pending')      # another fix: still open
        self.assertNotIn('בית', core.load_review_rejections(self.outdir))

    def test_book_convention_is_per_doc(self):
        db.set_status(self.con, self.outdir, [1], 'not_error', scope='book')
        self.assertEqual(self.eff(2), 'not_error')
        self.assertEqual(self.eff(3), 'pending')
        self.assertEqual(self.eff(4), 'pending')
        self.assertNotIn('בית', core.load_review_rejections(self.outdir))

    def test_legacy_per_unit_reject_is_not_global(self):
        dec = sqlite3.connect(os.path.join(self.outdir, db.DECISIONS_F))
        dec.execute('''CREATE TABLE decisions(
            word TEXT, unit TEXT, errtype TEXT, verdict TEXT,
            suggestion TEXT, source TEXT, ref TEXT, PRIMARY KEY(word, unit))''')
        dec.execute("INSERT INTO decisions VALUES('שלם','77','e','reject',"
                    "'','','')")
        dec.execute("INSERT INTO decisions VALUES('שלום','*','e','reject',"
                    "'','','')")
        dec.commit()
        dec.close()
        got = core.load_review_rejections(self.outdir)
        self.assertNotIn('שלם', got)
        self.assertIn('שלום', got)


# ---------------------------------------------------------------------------
# D2 — an approval is bound to the suggestion that was approved
# ---------------------------------------------------------------------------

def book_result(doc, title, findings, space=()):
    return {'doc': doc, 'title': title, 'origin': 'o1',
            'findings': [dict(word=w, errtype='edit1_sub', suggestion=s,
                              score=5.0, origin='o1', source=title, ref='r',
                              unit=u, doc=doc, snippet='')
                         for w, s, u in findings],
            'space_errors': [dict(part1=a, part2=b, joined=a + b,
                                  join_freq=9, origin='o1', source=title,
                                  ref='r', unit=u, doc=doc, snippet='')
                             for a, b, u in space]}


def make_report(outdir, occ, space=()):
    """A minimal report.db with the *_full tables import_all reads."""
    p = os.path.join(outdir, db.REPORT_DB_F)
    if os.path.exists(p):
        os.remove(p)
    con = sqlite3.connect(p)
    con.executescript('''
        CREATE TABLE occurrences_full(word, errtype, suggestion, score,
            ctx_hits, sugg_local, book_repeat, tanach, source, ref, unit,
            snippet, origin, doc);
        CREATE TABLE space_errors_full(part1, part2, joined, join_freq,
            source, ref, unit, snippet, origin);
        CREATE TABLE tanach_errors_full(word, canonical, source, ref, unit,
            snippet, origin);
        CREATE TABLE tanach_matches_full(word, source, ref, unit, snippet,
            origin);''')
    con.executemany('INSERT INTO occurrences_full VALUES'
                    '(?,?,?,5.0,0,0,0,0,?,?,?,?,?,?)',
                    [(w, 'edit1_sub', s, src, 'r', u, '', 'o1', d)
                     for w, s, u, d, src in occ])
    con.executemany('INSERT INTO space_errors_full VALUES(?,?,?,9,?,?,?,?,?)',
                    [(a, b, a + b, src, 'r', u, '', 'o1')
                     for a, b, u, src in space])
    con.commit()
    con.close()


class TestD2StaleApproval(Case):
    def _fid(self, word):
        return self.con.execute('SELECT id FROM findings WHERE word = ?',
                                (word,)).fetchone()[0]

    def test_book_rescan_with_new_suggestion_drops_to_pending(self):
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('אבי', 'אביו', '10')]))
        db.set_status(self.con, self.outdir, [self._fid('אבי')], 'approved')
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('אבי', 'אבא', '10')]))
        fid = self._fid('אבי')
        self.assertEqual(self.eff(fid), 'pending')
        rv = self.review(fid)
        self.assertEqual(rv['flag'], 'stale_approval')
        prev = json.loads(rv['prev_decision'])
        self.assertEqual(prev['status'], 'approved')
        self.assertEqual(prev['approved_suggestion'], 'אביו')

    def test_book_rescan_same_suggestion_stays_approved(self):
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('אבי', 'אביו', '10')]))
        db.set_status(self.con, self.outdir, [self._fid('אבי')], 'approved')
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('אבי', 'אביו', '10')]))
        self.assertEqual(self.eff(self._fid('אבי')), 'approved')

    def test_custom_suggestion_stays_bound(self):
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('אבי', 'אביו', '10')]))
        db.set_status(self.con, self.outdir, [self._fid('אבי')], 'approved',
                      custom_suggestion='אביה')
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('אבי', 'אבא', '10')]))
        fid = self._fid('אבי')
        self.assertEqual(self.eff(fid), 'approved')
        self.assertEqual(self.review(fid)['custom_suggestion'], 'אביה')

    def test_full_import_with_new_suggestion_drops_to_pending(self):
        make_report(self.outdir, [('אבי', 'אביו', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        fid = self._fid('אבי')
        db.set_status(self.con, self.outdir, [fid], 'approved')
        make_report(self.outdir, [('אבי', 'אבא', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        self.assertEqual(self._fid('אבי'), fid)
        self.assertEqual(self.eff(fid), 'pending')
        self.assertEqual(self.review(fid)['flag'], 'stale_approval')


# ---------------------------------------------------------------------------
# D3 — book identity is the doc, never the title
# ---------------------------------------------------------------------------

class TestD3BookIdentity(Case):
    def test_same_title_scan_leaves_other_book_alone(self):
        # book 1 and book 2 share a title; book 2 has doc-less full-scan
        # rows (extra_space / tokdiag) carrying decisions
        self.add(1, 'בית', 'ביתו', '100', doc='1')
        self.add(2, 'בית', 'ביתו', '200', doc='2')
        self.add(3, 'ב ית', 'בית', '201', doc=None, family='extra_space',
                 errtype='extra_space')
        self.add(4, 'שלם', 'שלום', '202', doc=None, family='tokdiag',
                 errtype='tokdiag')
        db.set_status(self.con, self.outdir, [2, 3, 4], 'approved')
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('בית', 'ביתו', '100')]))
        for fid in (2, 3, 4):
            self.assertEqual(self.eff(fid), 'approved', fid)

    def test_rescan_still_replaces_own_docless_rows(self):
        self.add(1, 'בית', 'ביתו', '100', doc='1')
        self.add(2, 'ב ית', 'בית', '101', doc=None, family='extra_space',
                 errtype='extra_space')
        self.add(3, 'ש לם', 'שלם', 'file:d/a.txt:5', doc=None,
                 family='extra_space', errtype='extra_space')
        db.import_book_scan(self.outdir, book_result(
            '1', 'ספר', [('בית', 'ביתו', '100'), ('בית', 'ביתו', '102')]))
        self.assertIsNone(db.get_finding(self.con, 2))
        self.assertIsNotNone(db.get_finding(self.con, 3))   # another doc
        db.import_book_scan(self.outdir, book_result('d/a.txt', 'ספר', []))
        self.assertIsNone(db.get_finding(self.con, 3))

    def test_import_all_gives_every_family_a_doc(self):
        make_report(self.outdir,
                    [('בית', 'ביתו', '100', '1', 'ספר'),
                     ('בית', 'ביתו', 'file:dir/a.txt:3', 'dir/a.txt', 'a')],
                    space=[('ב', 'ית', '100', 'ספר'),
                           ('ש', 'לם', 'file:dir/a.txt:4', 'a')])
        db.import_all(self.outdir)
        docs = {r[0]: r[1] for r in self.con.execute(
            "SELECT unit, doc FROM findings WHERE family = 'extra_space'")}
        self.assertEqual(docs, {'100': '1', 'file:dir/a.txt:4': 'dir/a.txt'})

    def test_books_and_filter_use_doc(self):
        self.add(1, 'בית', 'ביתו', '100', doc='1')
        self.add(2, 'בית', 'ביתו', '200', doc='2')
        books = db.get_books(self.con)
        self.assertEqual(len(books), 2)
        self.assertEqual({b['source'] for b in books}, {'ספר'})
        key = [b for b in books if b['key'] == '1'][0]['key']
        rows, total = db.query_findings(self.con, {'book_key': key})
        self.assertEqual([r['id'] for r in rows], [1])
        fl = db.get_fixlist(self.con, statuses='pending')
        self.assertEqual(len(fl['books']), 2)
        one = db.get_fixlist(self.con, book_key='2', statuses='pending')
        self.assertEqual([i['id'] for i in one['items']], [2])


# ---------------------------------------------------------------------------
# D4 — undo restores the whole previous decision
# ---------------------------------------------------------------------------

class TestD4Undo(Case):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10')

    def test_undo_restores_custom_fix_and_note(self):
        db.set_status(self.con, self.outdir, [1], 'approved',
                      custom_suggestion='בתים', note='הערה')
        db.set_status(self.con, self.outdir, [1], 'approved',
                      custom_suggestion='ביתה', note='אחרת')
        db.undo(self.con, self.outdir)
        rv = self.review(1)
        self.assertEqual(rv['status'], 'approved')
        self.assertEqual(rv['custom_suggestion'], 'בתים')
        self.assertEqual(rv['note'], 'הערה')

    def test_undo_removes_a_note_that_was_not_there(self):
        db.set_status(self.con, self.outdir, [1], 'approved')
        db.set_status(self.con, self.outdir, [1], 'unsure', note='שאלה')
        db.undo(self.con, self.outdir)
        rv = self.review(1)
        self.assertEqual(rv['status'], 'approved')
        self.assertIsNone(rv['note'])

    def test_undo_back_to_no_row(self):
        db.set_status(self.con, self.outdir, [1], 'unsure', note='x')
        db.undo(self.con, self.outdir)
        self.assertIsNone(self.review(1))

    def test_undo_restores_scoped_rules(self):
        db.set_status(self.con, self.outdir, [1], 'not_error', scope='book')
        db.set_status(self.con, self.outdir, [1], 'ignored', scope='book')
        db.undo(self.con, self.outdir)
        r = self.con.execute('SELECT status FROM book_rules').fetchall()
        self.assertEqual([x[0] for x in r], ['not_error'])
        db.undo(self.con, self.outdir)
        self.assertEqual(
            self.con.execute('SELECT COUNT(*) FROM book_rules').fetchone()[0],
            0)
        self.assertIsNone(self.review(1))


# ---------------------------------------------------------------------------
# D5 — migrating existing databases
# ---------------------------------------------------------------------------

OLD_UI_SCHEMA = '''
CREATE TABLE findings(id INTEGER PRIMARY KEY, family TEXT NOT NULL,
  errtype TEXT NOT NULL, word TEXT, suggestion TEXT, score REAL, rank REAL,
  ctx_hits INTEGER, sugg_local INTEGER, book_repeat INTEGER, tanach INTEGER,
  verified INTEGER NOT NULL DEFAULT 0, origin TEXT, source TEXT, ref TEXT,
  unit TEXT, doc TEXT, snippet TEXT, extra TEXT);
CREATE TABLE review(finding_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
  note TEXT, custom_suggestion TEXT, updated_at TEXT NOT NULL);
CREATE TABLE word_rules(word TEXT PRIMARY KEY, status TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE history(id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  action TEXT NOT NULL, finding_id INTEGER, word TEXT, old_status TEXT,
  new_status TEXT, note TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE owned_decisions(word TEXT NOT NULL, unit TEXT NOT NULL,
  PRIMARY KEY(word, unit));
CREATE TABLE file_edits(id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  path TEXT NOT NULL, book_key TEXT NOT NULL, backup TEXT NOT NULL,
  mode TEXT NOT NULL, finding_ids TEXT NOT NULL, detail TEXT NOT NULL,
  fp_before TEXT, fp_after TEXT, undone_at TEXT);
'''


class TestD5Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_rs_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_databases_are_upgraded_in_place(self):
        ui = sqlite3.connect(os.path.join(self.tmp, db.UI_DB_F))
        ui.executescript(OLD_UI_SCHEMA)
        ui.execute(f"INSERT INTO findings({FCOLS}) VALUES(1,'error','e',"
                   "'בית','ביתו',5,0,'o','ספר','r','10','1','')")
        ui.execute(f"INSERT INTO findings({FCOLS}) VALUES(2,'extra_space',"
                   "'extra_space','ב ית','בית',1,0,'o','ספר','r',"
                   "'file:d/a.txt:2',NULL,'')")
        ui.execute("INSERT INTO review VALUES(1,'approved',NULL,NULL,'t')")
        ui.commit()
        ui.close()
        dec = sqlite3.connect(os.path.join(self.tmp, db.DECISIONS_F))
        dec.execute('''CREATE TABLE decisions(word TEXT, unit TEXT,
            errtype TEXT, verdict TEXT, suggestion TEXT, source TEXT,
            ref TEXT, PRIMARY KEY(word, unit))''')
        dec.execute("INSERT INTO decisions VALUES('שלם','7','e','reject',"
                    "'','','')")
        dec.execute("INSERT INTO decisions VALUES('שלום','*','e','reject',"
                    "'','','')")
        dec.commit()
        dec.close()

        con = db.connect(self.tmp)
        try:
            rv = dict(con.execute(
                'SELECT r.status, x.scope, x.approved_suggestion FROM review r'
                ' JOIN review_ext x ON x.finding_id = r.finding_id'
                ).fetchone())
            self.assertEqual(rv['status'], 'approved')
            self.assertEqual(rv['scope'], 'occurrence')
            self.assertEqual(rv['approved_suggestion'], 'ביתו')
            self.assertEqual(con.execute(
                'SELECT doc FROM findings WHERE id = 2').fetchone()[0],
                'd/a.txt')
            # the legacy decisions are adopted with an explicit scope
            res = db.migrate_legacy_decisions(con, self.tmp)
            self.assertEqual(res['word_rules'], 1)
        finally:
            con.close()
        dec = sqlite3.connect(os.path.join(self.tmp, db.DECISIONS_F))
        scopes = dict(dec.execute('SELECT word, scope FROM decision_scope'))
        dec.close()
        self.assertEqual(scopes, {'שלם': 'occurrence', 'שלום': 'global'})
        self.assertEqual(core.load_review_rejections(self.tmp), {'שלום'})

    def test_legacy_tool_can_still_write_after_upgrade(self):
        db._decisions_con(self.tmp).close()      # creates + upgrades
        from magiah import review
        # the legacy writer names its columns, so the added ones are harmless
        dec = sqlite3.connect(os.path.join(self.tmp, db.DECISIONS_F))
        dec.execute(review.DECISION_INSERT.format(db='main'),
                    ('בית', '3', 'e', 'reject', '', '', ''))
        dec.commit()
        dec.close()
        self.assertEqual(core.load_review_rejections(self.tmp), set())


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class ServerCase(Case):
    def setUp(self):
        super().setUp()
        server.Handler.outdir = self.outdir
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, path, body=None):
        url = 'http://127.0.0.1:%d%s' % (self.port, path)
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(
            url, data=data, method='POST' if data else 'GET',
            headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))


# ---------------------------------------------------------------------------
# D6 — who decided
# ---------------------------------------------------------------------------

class TestD6Actor(ServerCase):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10')
        self.add(2, 'שלם', 'שלום', '11')

    def test_agent_and_human_are_kept_apart(self):
        code, _ = self.call('/api/status', {'ids': [1], 'status': 'approved',
                                            'actor': 'agent'})
        self.assertEqual(code, 200)
        self.call('/api/status', {'ids': [2], 'status': 'approved'})
        self.assertEqual(self.review(1)['decided_by'], 'agent')
        self.assertEqual(self.review(2)['decided_by'], 'human')
        h = self.con.execute('SELECT decided_by FROM history '
                             'WHERE finding_id = 1').fetchone()[0]
        self.assertEqual(h, 'agent')
        st = db.get_stats(self.con)
        self.assertEqual(st['by_actor']['agent'].get('approved'), 1)
        self.assertEqual(st['by_actor']['human'].get('approved'), 1)
        export.export_fixes(self.con, self.outdir)
        with open(os.path.join(self.outdir, 'to_send',
                               'approved_fixes_all.csv'),
                  encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        by_word = {r['word']: r for r in rows}
        self.assertEqual(by_word['בית']['decided_by'], 'agent')
        self.assertEqual(by_word['שלם']['decided_by'], 'human')


# ---------------------------------------------------------------------------
# G — the card queue's server contract
# ---------------------------------------------------------------------------

class TestG1Paging(Case):
    def setUp(self):
        super().setUp()
        for i in range(1, 121):
            # many equal ranks: the keyset must break ties by id
            self.add(i, 'בית', 'ביתו', str(i), rank=float(i % 7))

    def test_offset_paging_over_shrinking_set_skips(self):
        """Documents why OFFSET paging cannot drive the queue."""
        seen, page = set(), 1
        while True:
            rows, total = db.query_findings(self.con, {'status': 'pending'},
                                            page=page, page_size=50)
            if not rows:
                break
            for r in rows:
                seen.add(r['id'])
                db.set_status(self.con, self.outdir, [r['id']], 'approved')
            page += 1
        self.assertLess(len(seen), 120)

    def test_keyset_cursor_visits_every_finding(self):
        for sort in ('rank', 'random', 'word', 'source'):
            with self.subTest(sort=sort):
                self.con.execute('DELETE FROM review')
                self.con.commit()
                seen, cursor = [], ''
                while True:
                    rows, total, cursor = db.query_findings_page(
                        self.con, {'status': 'pending'}, sort=sort,
                        page_size=50, cursor=cursor, seed=7)
                    for r in rows:
                        seen.append(r['id'])
                        db.set_status(self.con, self.outdir, [r['id']],
                                      'approved')
                    if not cursor:
                        break
                self.assertEqual(sorted(seen), list(range(1, 121)))


class TestG3Idempotent(ServerCase):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10')

    def nhist(self):
        return self.con.execute(
            'SELECT COUNT(*) FROM history WHERE finding_id = 1').fetchone()[0]

    def test_expected_status_mismatch_is_409(self):
        body = {'ids': [1], 'status': 'approved', 'expect_status': 'pending'}
        self.assertEqual(self.call('/api/status', body)[0], 200)
        code, res = self.call('/api/status', body)
        self.assertEqual(code, 409)
        self.assertEqual(res['current'], {'1': 'approved'})
        self.assertEqual(self.nhist(), 1)

    def test_repeated_write_is_a_noop(self):
        db.set_status(self.con, self.outdir, [1], 'approved')
        res = db.set_status(self.con, self.outdir, [1], 'approved')
        self.assertEqual(res['updated'], 0)
        self.assertEqual(self.nhist(), 1)

    def test_cursor_over_http(self):
        code, res = self.call('/api/findings?status=pending&cursor=&page_size=1')
        self.assertEqual(code, 200)
        self.assertEqual([r['id'] for r in res['rows']], [1])
        self.assertIsNone(res['next_cursor'])


@unittest.skipIf(NODE is None, 'node is not installed')
class TestCardQueueJs(ServerCase):
    """Drives static/cardqueue.js (the code app.js runs) against the API."""

    def setUp(self):
        super().setUp()
        for i in range(1, 121):
            self.add(i, 'בית', 'ביתו', str(i), rank=float(i % 5))

    def run_js(self, scenario):
        out = subprocess.run(
            [NODE, HARNESS, 'http://127.0.0.1:%d' % self.port, scenario],
            capture_output=True, timeout=120)
        self.assertEqual(out.returncode, 0,
                         out.stderr.decode('utf-8', 'replace')[-2000:])
        return json.loads(out.stdout.decode('utf-8'))

    def pending(self):
        return db.query_findings(self.con, {'status': 'pending'},
                                 page_size=1)[1]

    def test_g1_g5_full_workflow_visits_all(self):
        res = self.run_js('approve_all')
        self.assertEqual(res['visited'], 120)
        self.assertEqual(res['state'], 'done')
        self.assertEqual(self.pending(), 0)

    def test_g2_skip_comes_back_and_is_not_done(self):
        res = self.run_js('skip_first')
        self.assertTrue(res['skipped_returned'])
        self.assertEqual(res['state'], 'done')
        self.assertEqual(self.pending(), 0)

    def test_g3_double_action_saves_once(self):
        res = self.run_js('double')
        self.assertEqual(res['saves'], 1)
        self.assertEqual(res['shifted'], 1)
        n = self.con.execute('SELECT COUNT(*) FROM history').fetchone()[0]
        self.assertEqual(n, 1)

    def test_g4_completion_uses_server_count(self):
        # another reviewer re-opens a card this client already decided
        res = self.run_js('reopen_behind')
        self.assertEqual(res['state'], 'remaining')
        self.assertEqual(res['remaining'], 1)


# ---------------------------------------------------------------------------
# H — to_send export
# ---------------------------------------------------------------------------

class TestHExport(Case):
    def setUp(self):
        super().setUp()
        self.send = os.path.join(self.outdir, 'to_send')
        self.add(1, 'בית', 'ביתו', '10', origin='alpha')
        self.add(2, 'שלם', 'שלום', '11', origin='beta')
        self.add(3, 'ספר', 'ספרו', '12', origin='beta')
        self.add(4, 'אבי', 'אביו', '13', origin='beta', doc='2')

    def test_h1_origin_file_removed_after_last_fix_undone(self):
        os.makedirs(self.send, exist_ok=True)
        user_file = os.path.join(self.send, 'approved_fixes_mine.csv')
        with open(user_file, 'w', encoding='utf-8') as f:
            f.write('my own notes\n')
        db.set_status(self.con, self.outdir, [1], 'approved')
        db.set_status(self.con, self.outdir, [2], 'approved')
        export.export_fixes(self.con, self.outdir)
        alpha = os.path.join(self.send, 'approved_fixes_alpha.csv')
        self.assertTrue(os.path.exists(alpha))
        db.undo(self.con, self.outdir)           # undo beta
        db.undo(self.con, self.outdir)           # undo alpha
        export.export_fixes(self.con, self.outdir)
        self.assertFalse(os.path.exists(alpha))
        self.assertFalse(os.path.exists(
            os.path.join(self.send, 'approved_fixes_beta.csv')))
        self.assertTrue(os.path.exists(user_file))

    def test_h2_rejections_keep_their_scope(self):
        db.set_status(self.con, self.outdir, [1], 'not_error')
        db.set_status(self.con, self.outdir, [2], 'not_error', scope='word')
        db.set_status(self.con, self.outdir, [3], 'not_error',
                      scope='replacement')
        db.set_status(self.con, self.outdir, [4], 'not_error', scope='book')
        export.export_fixes(self.con, self.outdir)
        with open(os.path.join(self.send, 'rejected_words.txt'),
                  encoding='utf-8') as f:
            self.assertEqual(f.read().split(), ['שלם'])

        def rows(name):
            with open(os.path.join(self.send, name),
                      encoding='utf-8-sig') as f:
                return list(csv.DictReader(f))
        occ = rows('rejected_occurrences.csv')
        self.assertEqual([r['word'] for r in occ], ['בית'])
        self.assertEqual(occ[0]['scope'], 'occurrence')
        self.assertEqual(occ[0]['decided_by'], 'human')
        rep = rows('rejected_replacements.csv')
        self.assertEqual([(r['word'], r['suggestion']) for r in rep],
                         [('ספר', 'ספרו')])
        conv = rows('book_conventions.csv')
        self.assertEqual([(r['word'], r['doc']) for r in conv],
                         [('אבי', '2')])

    def test_h3_fixes_carry_approved_suggestion(self):
        db.set_status(self.con, self.outdir, [1], 'approved',
                      custom_suggestion='בתים')
        export.export_fixes(self.con, self.outdir)
        with open(os.path.join(self.send, 'approved_fixes_all.csv'),
                  encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]['suggestion'], 'בתים')
        self.assertEqual(rows[0]['approved_suggestion'], 'בתים')
        self.assertEqual(rows[0]['decided_by'], 'human')


# ===========================================================================
# Review round 2
# ===========================================================================

def walk(con, filters=None, **kw):
    """Every id a full keyset walk yields, in order."""
    seen, cursor = [], ''
    while True:
        rows, _total, cursor = db.query_findings_page(
            con, filters or {}, cursor=cursor, **kw)
        seen += [r['id'] for r in rows]
        if not cursor:
            return seen


class TestR2Paging(Case):
    """NULLs, empty strings and ties in every sort key; seeds that differ."""

    def setUp(self):
        super().setUp()
        import random
        rnd = random.Random(1)
        rows = []
        for i in range(1, 401):
            rows.append((i, 'error', 'edit1_sub',
                         rnd.choice([None, 'אב', 'גד', '']), 'ס',
                         rnd.choice([None, 1.0, 2.5, 2.5, -1.0, 0.0]), 0, 'o',
                         rnd.choice([None, 'א', 'ב', '']), 'r',
                         rnd.choice([None, '', '5', '17', 'file:x/y.txt:3',
                                     'abc']), 'd', ''))
        self.con.executemany(f'INSERT INTO findings({FCOLS}) '
                             'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        self.con.commit()

    def test_every_sort_walks_every_row_once(self):
        for sort in ('rank', 'source', 'word', 'random'):
            for d in ('desc', 'asc'):
                with self.subTest(sort=sort, d=d):
                    seen = walk(self.con, sort=sort, direction=d,
                                page_size=7, seed=12345)
                    self.assertEqual(sorted(seen), list(range(1, 401)))

    def test_random_order_depends_on_the_seed(self):
        a = walk(self.con, sort='random', page_size=50, seed=1)
        b = walk(self.con, sort='random', page_size=50, seed=999999)
        succ = {a[i]: a[i + 1] for i in range(len(a) - 1)}
        shared = sum(1 for i in range(len(b) - 1)
                     if succ.get(b[i]) == b[i + 1])
        self.assertLess(shared, 20)
        self.assertEqual(a, walk(self.con, sort='random', page_size=13,
                                 seed=1))

    def test_cursor_pages_skip_the_count(self):
        _rows, total, cur = db.query_findings_page(self.con, {}, page_size=5,
                                                   cursor='')
        self.assertIsNone(total)
        _rows, total, cur = db.query_findings_page(
            self.con, {}, page_size=5, cursor='', with_total=True)
        self.assertEqual(total, 400)


@unittest.skipIf(os.environ.get('MAGIAH_SKIP_PERF'), 'perf tests disabled')
class TestR2PagePerf(unittest.TestCase):
    """A card page must stay interactive on a real-size review database."""

    N = 300_000
    LIMIT = 0.3

    @classmethod
    def setUpClass(cls):
        import random
        cls.tmp = tempfile.mkdtemp(prefix='magiah_perf_')
        con = db.connect(cls.tmp)
        rnd = random.Random(7)
        letters = 'אבגדהוזחטיכלמנסעפצקרשת'

        def w():
            return ''.join(rnd.choice(letters)
                           for _ in range(rnd.randint(2, 5)))
        con.executemany(
            f'INSERT INTO findings({FCOLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ((i, 'error', 'edit1_sub', w(), w(), round(rnd.random() * 10, 2),
              0, 'o1', 'ספר%d' % (i % 90), 'r', str(rnd.randint(1, 10 ** 6)),
              str(i % 700), '') for i in range(1, cls.N + 1)))
        con.executemany("INSERT INTO review(finding_id, status, note, "
                        "custom_suggestion, updated_at) "
                        "VALUES(?, 'approved', NULL, NULL, 't')",
                        [(i,) for i in range(1, cls.N, 10)])
        con.commit()
        con.execute('ANALYZE')
        con.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_card_pages_are_fast(self):
        import time
        con = db.connect(self.tmp)
        try:
            for sort in ('rank', 'source', 'word', 'random'):
                cursor = ''
                for page in range(4):
                    t = time.perf_counter()
                    rows, _t, cursor = db.query_findings_page(
                        con, {'status': 'pending'}, sort=sort,
                        page_size=50, cursor=cursor, seed=5)
                    took = time.perf_counter() - t
                    self.assertEqual(len(rows), 50)
                    self.assertLess(took, self.LIMIT, (sort, page, took))
        finally:
            con.close()


class TestR2StaleUndo(Case):
    def _fid(self):
        return self.con.execute('SELECT id FROM findings').fetchone()[0]

    def _decisions(self):
        dec = sqlite3.connect(os.path.join(self.outdir, db.DECISIONS_F))
        try:
            return dec.execute('SELECT verdict, suggestion FROM decisions '
                               "WHERE unit != '*'").fetchall()
        finally:
            dec.close()

    def _exported(self):
        export.export_fixes(self.con, self.outdir)
        with open(os.path.join(self.outdir, 'to_send',
                               'approved_fixes_all.csv'),
                  encoding='utf-8-sig') as f:
            return [(r['word'], r['suggestion']) for r in csv.DictReader(f)]

    def test_undo_onto_a_changed_suggestion_is_stale(self):
        make_report(self.outdir, [('אבג', 'אבד', '10', 'd1', 'ספר')])
        db.import_all(self.outdir)
        fid = self._fid()
        db.set_status(self.con, self.outdir, [fid], 'approved')
        db.set_status(self.con, self.outdir, [fid], 'unsure')
        make_report(self.outdir, [('אבג', 'אבה', '10', 'd1', 'ספר')])
        db.import_all(self.outdir)
        db.undo(self.con, self.outdir)
        self.assertNotEqual(self.eff(fid), 'approved')
        self.assertEqual(self.review(fid)['flag'], 'stale_approval')
        self.assertEqual(self._exported(), [])
        self.assertNotIn(('accept', 'אבה'), self._decisions())

    def test_restore_onto_a_changed_suggestion_is_stale(self):
        make_report(self.outdir, [('אבג', 'אבד', '10', 'd1', 'ספר')])
        db.import_all(self.outdir)
        fid = self._fid()
        db.set_status(self.con, self.outdir, [fid], 'approved')
        backup = os.path.basename(db.write_backup(self.con, self.outdir))
        db.reset(self.con, self.outdir)
        make_report(self.outdir, [('אבג', 'אבה', '10', 'd1', 'ספר')])
        db.import_all(self.outdir)
        db.restore_backup(self.con, self.outdir, backup)
        self.assertNotEqual(self.eff(fid), 'approved')
        self.assertEqual(self._exported(), [])
        self.assertNotIn(('accept', 'אבה'), self._decisions())

    def test_export_ships_what_was_approved(self):
        self.add(1, 'אבג', 'אבד', '10')
        db.set_status(self.con, self.outdir, [1], 'approved')
        # the finding's own suggestion moved without a re-import check
        self.con.execute("UPDATE findings SET suggestion = 'אבה'")
        self.con.commit()
        self.assertEqual(self._exported(), [('אבג', 'אבד')])


class TestR2Writes(Case):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10')

    def test_scoped_approval_is_refused(self):
        for scope in ('book', 'word'):
            for st in ('approved', 'fixed'):
                with self.subTest(scope=scope, status=st):
                    with self.assertRaises(ValueError):
                        db.set_status(self.con, self.outdir, [1], st,
                                      scope=scope)
        db.set_status(self.con, self.outdir, [1], 'not_error', scope='book')

    def test_expect_status_is_checked_inside_the_write(self):
        orig = db._decisions_con

        def slow(outdir):
            import time
            time.sleep(0.3)
            return orig(outdir)
        res = {}

        def tab(name, status):
            c = db.connect(self.outdir)
            try:
                res[name] = db.set_status(c, self.outdir, [1], status,
                                          expect_status='pending')['updated']
            except db.StatusConflict:
                res[name] = 409
            finally:
                c.close()
        db._decisions_con = slow
        try:
            ts = [threading.Thread(target=tab, args=('a', 'approved')),
                  threading.Thread(target=tab, args=('b', 'not_error'))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            db._decisions_con = orig
        self.assertEqual(sorted(res.values()), [1, 409])
        self.assertEqual(self.con.execute(
            'SELECT COUNT(*) FROM history').fetchone()[0], 1)

    def test_decision_scope_is_accurate_and_reset(self):
        self.add(2, 'שלם', 'שלום', '11', doc='2')
        db.set_status(self.con, self.outdir, [1], 'not_error', scope='book')
        db.set_status(self.con, self.outdir, [2], 'not_error',
                      scope='replacement')
        dec = sqlite3.connect(os.path.join(self.outdir, db.DECISIONS_F))
        got = dict(dec.execute('SELECT word, scope FROM decision_scope'))
        dec.close()
        self.assertEqual(got, {'בית': 'book', 'שלם': 'replacement'})
        db.reset(self.con, self.outdir, scope='all')
        dec = sqlite3.connect(os.path.join(self.outdir, db.DECISIONS_F))
        n = dec.execute('SELECT COUNT(*) FROM decision_scope').fetchone()[0]
        dec.close()
        self.assertEqual(n, 0)


class TestR2Compat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_rs_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_word_rules_keeps_its_three_columns(self):
        con = db.connect(self.tmp)
        try:
            con.execute("INSERT INTO findings(id, family, errtype, word) "
                        "VALUES(1, 'error', 'e', 'בית')")
            db.set_status(con, self.tmp, [1], 'not_error', scope='word',
                          decided_by='agent')
            # the original code's positional write must keep working
            con.execute('INSERT OR REPLACE INTO word_rules VALUES(?,?,?)',
                        ('שלם', 'not_error', 't'))
            rows = con.execute(f'''
                SELECT {db.WORD_DECIDER} FROM word_rules w
                WHERE w.word = 'בית' ''').fetchall()
            self.assertEqual(rows[0][0], 'agent')
        finally:
            con.close()

    def test_rev2_database_is_repaired(self):
        con = db.connect(self.tmp)
        con.execute('ALTER TABLE word_rules ADD COLUMN decided_by TEXT')
        con.execute("INSERT INTO word_rules VALUES('בית','not_error','t',"
                    "'agent')")
        con.execute("UPDATE meta SET value = '2' WHERE key = 'schema_rev'")
        con.commit()
        con.close()
        con = db.connect(self.tmp)
        try:
            cols = [r[1] for r in con.execute('PRAGMA table_info(word_rules)')]
            self.assertEqual(cols, ['word', 'status', 'updated_at'])
            self.assertEqual(con.execute(
                "SELECT decided_by FROM word_rule_ext WHERE word = 'בית'"
            ).fetchone()[0], 'agent')
        finally:
            con.close()

    def test_newer_schema_is_not_downgraded(self):
        con = db.connect(self.tmp)
        con.execute("UPDATE meta SET value = '99' WHERE key = 'schema_rev'")
        con.commit()
        con.close()
        con = db.connect(self.tmp)
        try:
            self.assertEqual(con.execute("SELECT value FROM meta WHERE "
                                         "key = 'schema_rev'").fetchone()[0],
                             '99')
        finally:
            con.close()


class TestR2Manifest(Case):
    def setUp(self):
        super().setUp()
        self.send = os.path.join(self.outdir, 'to_send')
        self.add(1, 'בית', 'ביתו', '10', origin='alpha')
        self.add(2, 'שלם', 'שלום', '11', origin='beta')

    def test_locked_file_stays_tracked(self):
        from unittest import mock
        db.set_status(self.con, self.outdir, [1], 'approved')
        db.set_status(self.con, self.outdir, [2], 'approved')
        beta = os.path.join(self.send, 'approved_fixes_beta.csv')
        real_open = open

        def locked(path, *a, **k):
            if str(path) == beta and a and 'w' in a[0]:
                raise PermissionError(path)
            return real_open(path, *a, **k)
        export.export_fixes(self.con, self.outdir)
        # beta's file is open in Excel during the next export
        with mock.patch('builtins.open', locked):
            with self.assertRaises(PermissionError):
                export.export_fixes(self.con, self.outdir)
        db.undo(self.con, self.outdir)                    # beta's only fix
        export.export_fixes(self.con, self.outdir)
        self.assertFalse(os.path.exists(beta))

    def test_first_run_recognises_our_old_files(self):
        os.makedirs(self.send, exist_ok=True)
        gone = os.path.join(self.send, 'approved_fixes_gone.csv')
        mine = os.path.join(self.send, 'approved_fixes_notes.csv')
        with open(gone, 'w', encoding='utf-8-sig') as f:
            f.write(','.join(export.FIXES_HEADER[:8]) + '\n')
        with open(mine, 'w', encoding='utf-8') as f:
            f.write('my own notes\n')
        export.export_fixes(self.con, self.outdir)
        self.assertFalse(os.path.exists(gone))
        self.assertTrue(os.path.exists(mine))


@unittest.skipIf(NODE is None, 'node is not installed')
class TestR2CardQueueJs(ServerCase):
    def setUp(self):
        super().setUp()
        for i in range(1, 31):
            self.add(i, 'בית', 'ביתו', str(i), rank=float(i % 5))

    run_js = TestCardQueueJs.run_js

    def test_reset_while_loading_runs_one_loop(self):
        res = self.run_js('reset_race')
        self.assertEqual(res['max_concurrent'], 1)
        self.assertEqual(res['dupes'], 0)

    def test_restore_dedups_by_id(self):
        res = self.run_js('restore_dup')
        self.assertEqual(res['copies'], 1)

    def test_noop_write_is_not_an_undo_step(self):
        res = self.run_js('noop_changed')
        self.assertEqual(res['first'], True)
        self.assertEqual(res['second'], False)


if __name__ == '__main__':
    unittest.main()
