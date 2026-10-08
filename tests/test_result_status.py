# -*- coding: utf-8 -*-
"""The review UI must never present the results of an older scan as the
latest one: /api/meta's result_status, /api/refresh's answer, and the scans
the UI itself runs.

Real pipeline runs over a small library; real HTTP server; real stage
subprocesses for the UI scans.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from magiah import core, runstate
from magiah.webui import db, scanner, server

from test_runstate import (REPO, TUNE, RunStateCase, break_library,
                           mend_library)


def meta_status(out):
    con = db.connect(out)
    try:
        return db.get_meta(con, out)
    finally:
        con.close()


class ResultStatusCase(RunStateCase):

    def import_good_scan(self):
        self.start_from_good_scan()
        db.import_all(self.out)
        st = meta_status(self.out)['result_status']
        self.assertEqual(st['notices'], [])
        self.assertFalse(st['stale'])
        self.assertTrue(st['results_at'])
        return st['results_at']

    def assert_stale(self, title_part, results_at):
        meta = meta_status(self.out)
        st = meta['result_status']
        self.assertTrue(st['stale'])
        (n,) = [n for n in st['notices'] if n['kind'] == 'scan_incomplete']
        self.assertEqual((n['level'], n['action']), ('error', 'scan'))
        self.assertIn(title_part, n['title'])
        self.assertIn('הממצאים המוצגים אינם מעודכנים', n['title'])
        self.assertIn(results_at, n['text'])
        # the user's results stay on screen
        self.assertEqual(meta['total'], 1)
        self.assertFalse(meta['no_scan'])
        return n


class MetaTest(ResultStatusCase):

    def test_partial_scan(self):
        at = self.import_good_scan()
        break_library(self.lib)
        self.assertEqual(self.scan()[0], 1)
        n = self.assert_stale('לא קראה את כל הקלט', at)
        self.assertIn('בניית מילון', n['text'])
        self.assertIn('לא הצליח לקרוא', n['details'])

    def test_crashed_scan(self):
        at = self.import_good_scan()
        code = ('import os, sys; sys.path.insert(0, sys.argv[1]); '
                'from magiah import cli, core; '
                'core.detect = lambda *a, **k: os._exit(9); '
                'cli.main(sys.argv[2:])')
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code, REPO, 'all',
             '--textdir', self.lib, '--out', self.out, *TUNE],
            capture_output=True, cwd=REPO)
        self.assertEqual(proc.returncode, 9)
        n = self.assert_stale('נקטעה באמצע', at)
        self.assertIn('איתור', n['text'])
        self.assertIsNone(n['details'])          # nobody lived to say why

    def test_failed_scan_survives_a_new_connection(self):
        # nothing is kept in memory: a restarted server says the same
        at = self.import_good_scan()
        with mock.patch.object(core, 'locate',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.scan()
        n = self.assert_stale('נכשלה', at)
        self.assertEqual(n['details'], 'RuntimeError: boom')
        self.assertEqual(self.assert_stale('נכשלה', at), n)

    def test_success_clears_and_asks_for_a_refresh(self):
        self.import_good_scan()
        break_library(self.lib)
        self.scan()
        mend_library(self.lib)
        time.sleep(0.05)                 # a report.db mtime the import lacks
        self.good_scan()
        st = meta_status(self.out)['result_status']
        self.assertFalse(st['stale'])
        (n,) = st['notices']
        self.assertEqual((n['kind'], n['level'], n['action']),
                         ('refresh_needed', 'info', 'refresh'))
        db.import_all(self.out)
        self.assertEqual(meta_status(self.out)['result_status']['notices'],
                         [])

    def test_folder_from_before_run_states(self):
        # an old folder: no run_state/, a ui_review.db without report_mtime
        self.start_from_good_scan()
        db.import_all(self.out)
        shutil.rmtree(os.path.join(self.out, runstate.STATE_DIR))
        con = sqlite3.connect(os.path.join(self.out, db.UI_DB_F))
        con.execute("DELETE FROM meta WHERE key = 'report_mtime'")
        con.commit()
        con.close()
        st = meta_status(self.out)['result_status']
        self.assertEqual(st['notices'], [])
        self.assertFalse(st['stale'])
        self.assertIsNone(st['results_at'])      # not known, not guessed
        # once it fails, the warning says so without inventing a date
        break_library(self.lib)
        self.scan()
        (n,) = meta_status(self.out)['result_status']['notices']
        self.assertIn('מסריקה קודמת שהושלמה', n['text'])

    def test_folder_from_before_run_states_with_partial_coverage(self):
        # the CLI refuses such results (coverage_problem); the UI agrees
        at = self.import_good_scan()
        break_library(self.lib)
        self.scan()
        shutil.rmtree(os.path.join(self.out, runstate.STATE_DIR))
        n = self.assert_stale('לא קראה את כל הקלט', at)
        self.assertIn('בניית מילון', n['text'])
        self.assertIn('coverage_lexicon.json', n['details'])

    def test_no_findings_yet(self):
        break_library(self.lib)
        self.scan()
        st = meta_status(self.out)['result_status']
        (n,) = st['notices']
        self.assertTrue(st['stale'])
        self.assertIsNone(st['results_at'])
        self.assertEqual(n['title'], 'הסריקה האחרונה לא קראה את כל הקלט')
        self.assertIn('עדיין לא הושלמה', n['text'])

    def test_completed_scan_that_found_nothing(self):
        # a scan did complete — it is no "no scan has completed yet"
        at = self.import_good_scan()
        con = sqlite3.connect(os.path.join(self.out, db.UI_DB_F))
        con.execute('DELETE FROM findings')
        con.commit()
        con.close()
        break_library(self.lib)
        self.scan()
        st = meta_status(self.out)['result_status']
        self.assertEqual(st['results_at'], at)
        (n,) = st['notices']
        self.assertIn(at, n['text'])
        self.assertIn('לא מצאה ממצאים', n['text'])
        self.assertNotIn('עדיין לא הושלמה', n['text'])

    def test_failed_book_scan_is_its_own_warning(self):
        self.import_good_scan()
        rc, _ = run_book(self, os.path.join(self.lib, 'אין כזה.txt'))
        self.assertEqual(rc, 1)
        st = meta_status(self.out)['result_status']
        self.assertFalse(st['stale'])            # the full scan is intact
        (n,) = st['notices']
        self.assertEqual((n['kind'], n['level']),
                         ('book_scan_incomplete', 'warning'))
        self.assertIn('אין כזה.txt', n['text'])
        self.assertTrue(n['details'])


def run_book(case, key):
    from test_runstate import run_cli
    return run_cli('book', '--book', key, '--book-source', 'file',
                   '--textdir', case.lib, '--out', case.out, *TUNE)


class RefreshApiTest(ResultStatusCase):

    def setUp(self):
        super().setUp()
        server.Handler.outdir = self.out
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, path, body=None):
        url = 'http://127.0.0.1:%d%s' % (self.srv.server_address[1], path)
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(
            url, data=data, method='POST' if data else 'GET',
            headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def test_refresh_after_a_failed_scan_does_not_claim_success(self):
        self.import_good_scan()
        break_library(self.lib)
        self.scan()
        code, res = self.call('/api/refresh', {})
        self.assertEqual(code, 200)
        self.assertTrue(res['stale'])
        self.assertNotIn('הרענון הושלם', res['message'])
        self.assertIn('מהסריקה הקודמת שהושלמה', res['message'])
        self.assertEqual(res['result_status']['notices'][0]['kind'],
                         'scan_incomplete')
        code, meta = self.call('/api/meta')
        self.assertEqual(code, 200)
        self.assertTrue(meta['result_status']['stale'])
        self.assertEqual(meta['total'], 1)

    def test_refresh_after_a_good_scan(self):
        self.import_good_scan()
        code, res = self.call('/api/refresh', {})
        self.assertEqual(code, 200)
        self.assertFalse(res['stale'])
        self.assertTrue(res['message'].startswith('הרענון הושלם'))
        self.assertEqual(res['result_status']['notices'], [])


class UiScanTest(ResultStatusCase):
    """Scans run the way the UI runs them: stage subprocesses under one run
    owned by the scanner."""

    def setUp(self):
        super().setUp()
        # the scanner offers library/hybrid/sqlite; a library is one folder
        # per origin
        origin = os.path.join(self.tmp.name, 'repo', 'DictaToOtzaria')
        os.makedirs(origin)
        shutil.copy(os.path.join(self.lib, 'ספר.txt'), origin)
        self.repo = os.path.dirname(origin)

    def ui_scan(self):
        scanner.start_scan(self.out, None, {'workers': 1, 'n_chunks': 1},
                           {'mode': 'library', 'library_dir': self.repo})
        scanner._thread.join(120)
        return scanner.get_status()

    def test_failed_stage_records_its_reason_and_success_clears(self):
        self.assertEqual(self.ui_scan()['state'], 'done')
        db.import_all(self.out)
        at = meta_status(self.out)['result_status']['results_at']
        with mock.patch.object(scanner, '_stage_cmd', lambda stage, out: (
                [sys.executable, '-X', 'utf8', '-c',
                 'import sys; sys.path.insert(0, sys.argv[1]); '
                 'from magiah import cli, core; '
                 'core.locate = lambda *a, **k: 1 / 0; '
                 'sys.exit(cli.main(sys.argv[2:]))',
                 REPO, stage, '--out', out])):
            self.assertEqual(self.ui_scan()['state'], 'failed')
        n = self.assert_stale('נכשלה', at)
        self.assertIn('מיקום', n['text'])
        # the reason came from the stage process, not just its exit code
        self.assertIn('ZeroDivisionError', n['details'])
        self.assertEqual(self.ui_scan()['state'], 'done')
        self.assertFalse(meta_status(self.out)['result_status']['stale'])

    def test_cancelled_scan(self):
        self.ui_scan()
        db.import_all(self.out)
        at = meta_status(self.out)['result_status']['results_at']
        hold = threading.Event()
        real_popen = subprocess.Popen

        def popen(*a, **k):                  # cancel while a stage runs
            p = real_popen(*a, **k)
            hold.set()
            return p
        with mock.patch.object(scanner.subprocess, 'Popen', popen):
            scanner.start_scan(self.out, None, {'workers': 1, 'n_chunks': 1},
                               {'mode': 'library',
                                'library_dir': self.repo})
            hold.wait(30)
            scanner.cancel()
            scanner._thread.join(120)
        self.assertEqual(scanner.get_status()['state'], 'cancelled')
        self.assert_stale('בוטלה', at)

    def test_book_scan_from_the_ui_keeps_the_full_scan_warning(self):
        self.ui_scan()
        db.import_all(self.out)
        at = meta_status(self.out)['result_status']['results_at']
        with mock.patch.object(scanner, '_stage_cmd', lambda stage, out: (
                [sys.executable, '-c', 'import sys; sys.exit(3)'])):
            self.ui_scan()
        n = self.assert_stale('נכשלה', at)
        self.assertIn('קוד שגיאה 3', n['details'])
        scanner.start_book_scan(self.out, 'library',
                                'DictaToOtzaria/ספר.txt',
                                corpus_overrides={'mode': 'library',
                                                  'library_dir': self.repo})
        scanner._thread.join(120)
        self.assertEqual(scanner.get_status()['state'], 'done')
        st = meta_status(self.out)['result_status']
        self.assertTrue(st['stale'])
        self.assertEqual([x['kind'] for x in st['notices']],
                         ['scan_incomplete'])


if __name__ == '__main__':
    unittest.main()
