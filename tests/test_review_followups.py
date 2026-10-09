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
from magiah.webui import db                                     # noqa: E402
from test_review_state import Case, ServerCase, whitelist_cfg   # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
