# -*- coding: utf-8 -*-
"""Review state, follow-ups: rejections read from a locked decisions.db,
reset ordering, the Excel export of stale rows, approvals bound on restore
and upgrade, book keys of doc-less rows, the stored effective status and
the card queue after a rule decision.

Run:  python -X utf8 -m unittest tests.test_review_followups

Every test builds its own scan folder under tempfile.mkdtemp().
"""
import os
import sqlite3
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from magiah import book_scan, core                              # noqa: E402
from magiah.webui import db, export, hebrew                     # noqa: E402
from test_review_state import (Case, ServerCase, make_report,   # noqa: E402
                               whitelist_cfg)


def hold(path, mode='BEGIN EXCLUSIVE'):
    """Another program holding a lock on `path`."""
    con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    con.execute(mode)
    return con


# ---------------------------------------------------------------------------
# a locked decisions.db is never read as "no rejections"
# ---------------------------------------------------------------------------

class TestLockedRejections(Case):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10')
        db.set_status(self.con, self.outdir, [1], 'not_error', scope='word')
        self.dec = os.path.join(self.outdir, db.DECISIONS_F)

    def test_same_timeout_as_the_review_ui(self):
        self.assertEqual(core.REVIEW_DECISIONS_TIMEOUT, db.DECISIONS_TIMEOUT)

    def test_locked_decisions_db_fails_with_a_hebrew_stage_error(self):
        holder = hold(self.dec)
        try:
            with mock.patch.object(core, 'REVIEW_DECISIONS_TIMEOUT', 0.2):
                for load in (lambda: core.load_review_rejections(self.outdir),
                             lambda: book_scan._load_whitelist(
                                 whitelist_cfg(), self.outdir)):
                    with self.assertRaises(core.StageError) as cm:
                        load()
                    self.assertIn('decisions.db', str(cm.exception))
                    self.assertIn('נעול', str(cm.exception))
        finally:
            holder.execute('ROLLBACK')
            holder.close()
        self.assertEqual(core.load_review_rejections(self.outdir), {'בית'})

    def test_a_lock_released_within_the_timeout_is_waited_for(self):
        import threading
        holder = hold(self.dec)
        threading.Timer(0.3, lambda: (holder.execute('ROLLBACK'),
                                      holder.close())).start()
        with mock.patch.object(core, 'REVIEW_DECISIONS_TIMEOUT', 10.0):
            self.assertEqual(core.load_review_rejections(self.outdir),
                             {'בית'})

    def test_damaged_decisions_db_is_not_read_as_empty(self):
        with open(self.dec, 'wb') as f:
            f.write(b'not a database at all' * 100)
        with self.assertRaises(core.StageError):
            core.load_review_rejections(self.outdir)

    def test_decisions_db_without_its_table_has_no_rejections(self):
        os.remove(self.dec)
        sqlite3.connect(self.dec).close()
        self.assertEqual(core.load_review_rejections(self.outdir), set())


# ---------------------------------------------------------------------------
# reset(scope='all') clears decisions.db first, or nothing at all
# ---------------------------------------------------------------------------

def decisions_rows(outdir):
    dec = sqlite3.connect(os.path.join(outdir, db.DECISIONS_F))
    try:
        return sorted(dec.execute('SELECT word, unit, verdict FROM decisions'))
    finally:
        dec.close()


class TestResetOrder(ServerCase):
    def setUp(self):
        super().setUp()
        self.add(1, 'בית', 'ביתו', '10')
        self.add(2, 'שלם', 'שלום', '11')
        db.set_status(self.con, self.outdir, [1], 'approved')
        db.set_status(self.con, self.outdir, [2], 'not_error', scope='word')

    def counts(self):
        return [self.con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                for t in ('review', 'word_rules', 'history',
                          'owned_decisions')]

    def test_locked_decisions_db_is_423_and_clears_nothing(self):
        before, dec_before = self.counts(), decisions_rows(self.outdir)
        holder = hold(os.path.join(self.outdir, db.DECISIONS_F),
                      'BEGIN IMMEDIATE')
        try:
            with mock.patch.object(db, 'DECISIONS_TIMEOUT', 0.2):
                code, res = self.call('/api/reset', {'scope': 'all'})
        finally:
            holder.execute('ROLLBACK')
            holder.close()
        self.assertEqual(code, 423)
        self.assertIn('decisions.db', res['error'])
        self.assertEqual(self.counts(), before)
        self.assertEqual(decisions_rows(self.outdir), dec_before)
        # and ui_review.db is not left locked
        other = db.connect(self.outdir)
        try:
            other.execute('BEGIN IMMEDIATE')
            other.rollback()
        finally:
            other.close()
        code, res = self.call('/api/reset', {'scope': 'all'})
        self.assertEqual(code, 200)
        self.assertEqual(self.counts(), [0, 0, 0, 0])
        self.assertEqual(decisions_rows(self.outdir), [])
        self.assertEqual(core.load_review_rejections(self.outdir), set())

    def test_decisions_db_commits_before_ui_review_db(self):
        orig, seen = db._commit_decisions_first, []

        def spy(con, dec, gone=()):
            seen.append(con.execute('SELECT COUNT(*) FROM review')
                        .fetchone()[0])
            return orig(con, dec, gone)
        with mock.patch.object(db, '_commit_decisions_first', spy):
            db.reset(self.con, self.outdir, scope='all')
        self.assertEqual(seen, [0])
        self.assertFalse(self.con.in_transaction)

    def test_statuses_reset_leaves_decisions_db(self):
        dec_before = decisions_rows(self.outdir)
        db.reset(self.con, self.outdir)
        self.assertEqual(self.counts()[:3], [0, 0, 0])
        self.assertEqual(decisions_rows(self.outdir), dec_before)


# ---------------------------------------------------------------------------
# the Excel export shows a stale row's current suggestion
# ---------------------------------------------------------------------------

class TestXlsxStale(Case):
    def test_stale_row_shows_the_current_suggestion(self):
        import zipfile
        make_report(self.outdir, [('אבי', 'אביו', '10', '1', 'ספר'),
                                  ('בית', 'ביתו', '11', '1', 'ספר')])
        db.import_all(self.outdir)
        ids = dict(self.con.execute('SELECT word, id FROM findings'))
        db.set_status(self.con, self.outdir, list(ids.values()), 'approved')
        make_report(self.outdir, [('אבי', 'אבא', '10', '1', 'ספר'),
                                  ('בית', 'ביתו', '11', '1', 'ספר')])
        db.import_all(self.outdir)
        self.assertEqual(self.eff(ids['אבי']), 'pending')
        rows = {r[3]: r for r in export._all_rows(self.con, 'o1')}
        self.assertEqual(rows['אבי'][4], 'אבא')        # not the old אביו
        self.assertIn(hebrew.MESSAGES['stale_mark'], rows['אבי'][7])
        self.assertEqual(rows['בית'][4], 'ביתו')       # still approved
        self.assertNotIn(hebrew.MESSAGES['stale_mark'], rows['בית'][7])
        path, = export.export_xlsx(self.con, self.outdir, 'o1')
        with zipfile.ZipFile(path) as z:
            xml = ''.join(z.read(n).decode('utf-8') for n in z.namelist()
                          if n.startswith('xl/worksheets/'))
        self.assertIn('אבא', xml)
        self.assertNotIn('אביו', xml)

    def test_custom_fix_on_an_open_row_is_shown(self):
        self.add(1, 'בית', 'ביתו', '10')
        db.set_status(self.con, self.outdir, [1], 'unsure',
                      custom_suggestion='בתים')
        row, = export._all_rows(self.con, 'o1')
        self.assertEqual(row[4], 'בתים')


# ---------------------------------------------------------------------------
# approvals come back bound to the suggestion they approved
# ---------------------------------------------------------------------------

# the review fields a backup carried before approved_suggestion existed
OLD_BACKUP_FIELDS = ('finding_id', 'status', 'note', 'custom_suggestion',
                     'updated_at', 'family', 'word', 'unit', 'errtype', 'ref')


def as_old_backup(path):
    """Rewrite a backup as an earlier version wrote it."""
    import json
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    data['review'] = [{k: rv.get(k) for k in OLD_BACKUP_FIELDS}
                      for rv in data['review']]
    for k in ('book_rules', 'replacement_rules', 'file_edits'):
        data.pop(k, None)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return os.path.basename(path)


class TestRestoreBinding(Case):
    def fid(self, word='אבי'):
        return self.con.execute('SELECT id FROM findings WHERE word = ?',
                                (word,)).fetchone()[0]

    def exported(self):
        import csv
        export.export_fixes(self.con, self.outdir)
        with open(os.path.join(self.outdir, 'to_send',
                               'approved_fixes_all.csv'),
                  encoding='utf-8-sig') as f:
            return [(r['word'], r['suggestion']) for r in csv.DictReader(f)]

    def approve_backup_reset(self, scope='statuses', old=True):
        make_report(self.outdir, [('אבי', 'אביו', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        db.set_status(self.con, self.outdir, [self.fid()], 'approved')
        path = db.write_backup(self.con, self.outdir)
        # (reset's own backup may land on the same name within a second)
        db.reset(self.con, self.outdir, scope)
        return as_old_backup(path) if old else os.path.basename(path)

    def test_old_backup_after_a_rescan_binds_through_decisions_db(self):
        backup = self.approve_backup_reset()
        make_report(self.outdir, [('אבי', 'אבא', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        res = db.restore_backup(self.con, self.outdir, backup)
        fid = self.fid()
        self.assertEqual(self.eff(fid), 'pending')
        rv = self.review(fid)
        self.assertEqual(rv['flag'], 'stale_approval')
        self.assertEqual(rv['approved_suggestion'], 'אביו')
        self.assertEqual(res['stale_approvals'], 1)
        self.assertEqual(self.exported(), [])
        self.assertEqual(decisions_rows(self.outdir), [])

    def test_old_backup_that_cannot_be_bound_is_demoted(self):
        backup = self.approve_backup_reset(scope='all')   # no mirror left
        make_report(self.outdir, [('אבי', 'אבא', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        res = db.restore_backup(self.con, self.outdir, backup)
        fid = self.fid()
        self.assertEqual(self.eff(fid), 'unsure')
        self.assertIn('אבא', self.review(fid)['note'])
        self.assertIsNone(self.review(fid)['approved_suggestion'])
        self.assertEqual(res['unbound_recheck'], 1)
        self.assertEqual(self.exported(), [])
        self.assertEqual(decisions_rows(self.outdir), [])

    def test_old_backup_without_a_rescan_binds_the_current_suggestion(self):
        backup = self.approve_backup_reset(scope='all')
        db.restore_backup(self.con, self.outdir, backup)
        fid = self.fid()
        self.assertEqual(self.eff(fid), 'approved')
        self.assertEqual(self.review(fid)['approved_suggestion'], 'אביו')
        self.assertEqual(self.exported(), [('אבי', 'אביו')])

    def test_old_backup_custom_fix_is_its_own_binding(self):
        make_report(self.outdir, [('אבי', 'אביו', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        db.set_status(self.con, self.outdir, [self.fid()], 'approved',
                      custom_suggestion='אביה')
        path = db.write_backup(self.con, self.outdir)
        db.reset(self.con, self.outdir, 'all')
        backup = as_old_backup(path)
        make_report(self.outdir, [('אבי', 'אבא', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        db.restore_backup(self.con, self.outdir, backup)
        self.assertEqual(self.eff(self.fid()), 'approved')
        self.assertEqual(self.exported(), [('אבי', 'אביה')])

    def test_new_backup_keeps_its_binding(self):
        backup = self.approve_backup_reset(scope='all', old=False)
        make_report(self.outdir, [('אבי', 'אבא', '10', '1', 'ספר')])
        db.import_all(self.outdir)
        db.restore_backup(self.con, self.outdir, backup)
        self.assertEqual(self.review(self.fid())['flag'], 'stale_approval')


class TestRestoreLegacyDrop(Case):
    """probe_restore_legacy: a refresh drops an approval resting on the old
    Tanach heuristic; restoring a backup taken before must not undo that."""

    def setUp(self):
        super().setUp()
        from test_tanach import make_legacy_report
        make_legacy_report(os.path.join(self.outdir, db.REPORT_DB_F))
        self.con.execute(
            "INSERT INTO findings(id, family, errtype, word, unit, ref, "
            "tanach, suggestion, source) VALUES(1, 'error', 'edit1_sub', "
            "'פרץ', '7', 'ר', 2, 'פרח', 'ספר')")
        self.con.commit()

    def test_refresh_dropped_approval_is_not_restored(self):
        db.set_status(self.con, self.outdir, [1], 'approved')
        backup = os.path.basename(db.write_backup(self.con, self.outdir))
        self.assertEqual(db.import_all(self.outdir)['legacy_approvals_dropped'],
                         1)
        res = db.restore_backup(self.con, self.outdir, backup)
        self.assertEqual(res['legacy_recheck'], 1)
        self.assertEqual(self.eff(1), 'unsure')
        self.assertIn('פרח', self.review(1)['note'])
        self.assertEqual(decisions_rows(self.outdir), [])

    def test_drop_is_kept_when_the_history_was_reset_too(self):
        db.set_status(self.con, self.outdir, [1], 'approved')
        backup = os.path.basename(db.write_backup(self.con, self.outdir))
        db.reset(self.con, self.outdir, 'all')
        db.import_all(self.outdir)             # marks the row, drops nothing
        db.restore_backup(self.con, self.outdir, backup)
        self.assertEqual(self.eff(1), 'unsure')

    def test_approval_given_after_the_mark_is_restored(self):
        db.import_all(self.outdir)             # the row is marked legacy now
        db.set_status(self.con, self.outdir, [1], 'approved')
        backup = os.path.basename(db.write_backup(self.con, self.outdir))
        db.reset(self.con, self.outdir, 'all')
        db.restore_backup(self.con, self.outdir, backup)
        self.assertEqual(self.eff(1), 'approved')


class TestUpgradeBinding(unittest.TestCase):
    """An older ui_review.db upgraded in place (schema rev < 2) and
    word-wide approvals of earlier versions (rev < 4)."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='magiah_fu_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from test_review_state import FCOLS, OLD_UI_SCHEMA
        ui = sqlite3.connect(os.path.join(self.tmp, db.UI_DB_F))
        ui.executescript(OLD_UI_SCHEMA)
        rows = [(1, 'אבי', 'אבא', '10'), (2, 'בית', 'ביתו', '11'),
                (3, 'שלם', 'שלום', '12'), (4, 'דבר', 'דברו', '13'),
                (5, 'דבר', 'דברי', '14')]
        ui.executemany(f'INSERT INTO findings({FCOLS}) VALUES(?,?,?,?,?,?,?,'
                       '?,?,?,?,?,?)',
                       [(i, 'error', 'e', w, s, 5, 0, 'o', 'ספר', 'r', u,
                         '1', '') for i, w, s, u in rows])
        # approved at 10:00, the findings re-imported at 11:00 by a version
        # that kept approvals whatever the new suggestion was
        ui.executemany("INSERT INTO review VALUES(?,'approved',NULL,NULL,"
                       "'2026-01-01T10:00:00.000000')", [(1,), (2,), (3,)])
        ui.execute("INSERT INTO meta VALUES('last_import', "
                   "'2026-01-01T11:00:00.000000')")
        ui.execute("INSERT INTO word_rules VALUES('דבר', 'approved', 't')")
        ui.executemany('INSERT INTO owned_decisions VALUES(?,?)',
                       [('אבי', '10'), ('בית', '11'), ('דבר', '*')])
        ui.commit()
        ui.close()
        dec = db._decisions_con(self.tmp)
        dec.executemany(
            "INSERT INTO decisions VALUES(?,?,'e','accept',?,'ספר','r')",
            [('אבי', '10', 'אביו'),       # approved before the re-scan
             ('בית', '11', 'ביתו'),       # unchanged since
             ('שלם', '12', 'שלום'),       # not this UI's row: no binding
             ('דבר', '*', 'דברו')])       # a word-wide approval
        dec.commit()
        dec.close()

    def test_upgrade_binds_or_demotes_each_approval(self):
        con = db.connect(self.tmp)
        try:
            def st(fid):
                return db.get_finding(con, fid)['effective_status']
            ext = dict(con.execute('SELECT finding_id, approved_suggestion '
                                   'FROM review_ext'))
            self.assertEqual(ext[2], 'ביתו')
            self.assertEqual(st(2), 'approved')
            # bound to what was approved, which is no longer proposed
            self.assertEqual(ext[1], 'אביו')
            self.assertEqual(st(1), 'approved')   # stale on the next import
            # cannot be bound: unsure, with a note naming the suggestion
            self.assertEqual(st(3), 'unsure')
            self.assertIn('שלום', db.get_finding(con, 3)['note'])
            # the word-wide approval is bound to its pair
            self.assertEqual(st(4), 'approved')
            self.assertEqual(st(5), 'pending')
            self.assertEqual(con.execute(
                'SELECT COUNT(*) FROM word_rules').fetchone()[0], 0)
        finally:
            con.close()
        # the unowned decisions.db row is left alone
        self.assertIn(('שלם', '12', 'accept'), decisions_rows(self.tmp))

    def test_locked_decisions_db_postpones_the_upgrade(self):
        holder = hold(os.path.join(self.tmp, db.DECISIONS_F),
                      'BEGIN IMMEDIATE')
        try:
            with mock.patch.object(db, 'MIGRATE_DECISIONS_TIMEOUT', 0.1):
                con = db.connect(self.tmp)
                self.assertLess(db._schema_rev(con), db.SCHEMA_REV)
                con.close()
        finally:
            holder.execute('ROLLBACK')
            holder.close()
        con = db.connect(self.tmp)
        try:
            self.assertEqual(db._schema_rev(con), db.SCHEMA_REV)
            self.assertEqual(db.get_finding(con, 3)['effective_status'],
                             'unsure')
        finally:
            con.close()

    def test_legacy_word_accept_with_a_suggestion_is_a_pair_rule(self):
        con = db.connect(self.tmp)
        try:
            con.execute('DELETE FROM review')
            con.commit()
            dec = sqlite3.connect(os.path.join(self.tmp, db.DECISIONS_F))
            dec.execute("INSERT INTO decisions VALUES('בית','*','e',"
                        "'accept','ביתו','','')")
            dec.execute("INSERT INTO decisions VALUES('שלם','*','e',"
                        "'accept','','','')")
            dec.commit()
            dec.close()
            db.migrate_legacy_decisions(con, self.tmp)
            rules = con.execute('SELECT word, suggestion, status FROM '
                                'replacement_rules ORDER BY word').fetchall()
            self.assertIn(('בית', 'ביתו', 'approved'), [tuple(r) for r in
                                                          rules])
            # no suggestion to bind to: stays word-wide (documented)
            self.assertEqual(con.execute(
                "SELECT status FROM word_rules WHERE word = 'שלם'"
            ).fetchone()[0], 'approved')
        finally:
            con.close()


if __name__ == '__main__':
    unittest.main()
