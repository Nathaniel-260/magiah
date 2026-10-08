# -*- coding: utf-8 -*-
"""Run-state markers: a scan that fails, reads partially or never finishes
must leave the results marked as not current — and only a scan that rebuilds
them may clear that.

Every test runs the real pipeline (or the real CLI) over a small folder of
text files; the only things simulated are the failures themselves.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from magiah import cli, core, runstate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUNE = ['--workers', '1', '--n-chunks', '1']


def make_library(path):
    """Enough text for a real finding: בדאשית is one letter from the
    frequent בראשית."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'ספר.txt'), 'w', encoding='utf-8') as f:
        f.write('בראשית ברא אלהים את השמים ואת הארץ\n' * 60)
        f.write('בדאשית ברא אלהים את השמים ואת הארץ\n')


def break_library(path):
    """A '*.txt' entry that cannot be opened: the read stages count it as an
    unread input and stop with core.PartialRead."""
    os.makedirs(os.path.join(path, 'שבור.txt'), exist_ok=True)


def mend_library(path):
    os.rmdir(os.path.join(path, 'שבור.txt'))


def run_cli(*argv):
    """cli.main quietly; returns (exit code, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, err.getvalue()


def scan_record(out):
    with open(os.path.join(out, runstate.STATE_DIR, 'scan.json'),
              encoding='utf-8') as f:
        return json.load(f)


class RunStateCase(unittest.TestCase):
    """Each test gets its own library and output folder. Most start from a
    folder after one good full scan; that scan is run once per class and
    copied (a full run costs seconds: every stage starts a worker pool)."""

    @classmethod
    def setUpClass(cls):
        cls._template = tempfile.TemporaryDirectory(prefix='magiah_rs_t_')
        lib = os.path.join(cls._template.name, 'lib')
        cls._template_out = os.path.join(cls._template.name, 'out')
        make_library(lib)
        rc, err = run_cli('all', '--textdir', lib, '--out',
                          cls._template_out, *TUNE)
        assert not rc, err

    @classmethod
    def tearDownClass(cls):
        cls._template.cleanup()

    def start_from_good_scan(self):
        """self.out as a good full scan left it (record, coverage, report)."""
        shutil.copytree(self._template_out, self.out)
        self.assertIsNone(runstate.scan_problem(self.out))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='magiah_rs_')
        self.lib = os.path.join(self.tmp.name, 'lib')
        self.out = os.path.join(self.tmp.name, 'out')
        make_library(self.lib)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, command='all'):
        return run_cli(command, '--textdir', self.lib, '--out', self.out,
                       *TUNE)

    def good_scan(self):
        rc, err = self.scan()
        self.assertFalse(rc, err)
        self.assertIsNone(runstate.scan_problem(self.out))


class PipelineRunTest(RunStateCase):

    def test_successful_run_is_recorded_done(self):
        self.good_scan()
        rec = scan_record(self.out)
        (run,) = rec['runs'].values()
        self.assertEqual(run['state'], 'done')
        self.assertEqual(run['stages'], ['lexicon', 'detect', 'locate',
                                         'report'])
        self.assertEqual(set(rec['stages']), {'lexicon', 'detect', 'locate'})
        self.assertFalse(os.path.exists(
            os.path.join(self.out, runstate.STATE_DIR, 'scan.json.tmp')))

    def test_partial_read_is_stale_until_a_complete_run(self):
        self.start_from_good_scan()
        break_library(self.lib)
        rc, err = self.scan()
        self.assertEqual(rc, 1)
        p = runstate.scan_problem(self.out)
        self.assertEqual(p['state'], 'partial')
        self.assertEqual(p['stage'], 'lexicon')
        self.assertIn('לא הצליח לקרוא', p['reason'])
        mend_library(self.lib)
        self.good_scan()                    # a complete run clears it

    def test_exception_is_recorded_with_its_stage(self):
        self.start_from_good_scan()
        with mock.patch.object(core, 'detect',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.scan()
        p = runstate.scan_problem(self.out)
        self.assertEqual(p['state'], 'failed')
        self.assertEqual(p['stage'], 'detect')
        self.assertEqual(p['reason'], 'RuntimeError: boom')

    def test_ctrl_c_is_recorded_cancelled(self):
        self.start_from_good_scan()
        with mock.patch.object(core, 'locate',
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.scan()
        p = runstate.scan_problem(self.out)
        self.assertEqual((p['state'], p['stage']), ('cancelled', 'locate'))

    def test_killed_process_reads_as_interrupted(self):
        self.start_from_good_scan()
        # a real process, killed in the middle of `locate`: nothing gets to
        # finalize the record, exactly as with a power cut
        code = ('import os, sys; sys.path.insert(0, sys.argv[1]); '
                'from magiah import cli, core; '
                'core.locate = lambda *a, **k: os._exit(9); '
                'cli.main(sys.argv[2:])')
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code, REPO, 'all',
             '--textdir', self.lib, '--out', self.out, *TUNE],
            capture_output=True, cwd=REPO)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self.assertEqual(scan_record(self.out)['runs'][
            scan_record(self.out)['stages']['locate']]['state'], 'running')
        p = runstate.scan_problem(self.out)
        self.assertEqual((p['state'], p['stage']), ('interrupted', 'locate'))

        # `report` alone succeeds (the old report.db is complete) but does
        # not rebuild the results: the warning stays
        rc, err = self.scan('report')
        self.assertFalse(rc, err)
        self.assertEqual(runstate.scan_problem(self.out)['state'],
                         'interrupted')
        # the next run persists what it found
        self.assertEqual(scan_record(self.out)['runs'][
            scan_record(self.out)['stages']['locate']]['state'],
            'interrupted')
        self.good_scan()

    def test_running_run_is_in_progress_not_interrupted(self):
        self.start_from_good_scan()
        run = runstate.ScanRun(self.out, ['lexicon', 'detect'])
        try:
            run.enter('lexicon')
            self.assertIsNone(runstate.scan_problem(self.out))
        finally:
            run.close()                     # dies without finishing
        p = runstate.scan_problem(self.out)
        self.assertEqual((p['state'], p['stage']), ('interrupted', 'lexicon'))

    def test_run_that_stops_before_its_planned_stages_is_stale(self):
        # the UI's default rescan: calibrate fails, so detect and locate —
        # which it was going to rebuild — never run
        self.start_from_good_scan()
        stages = ['lexicon', 'calibrate', 'detect', 'locate', 'report']
        with runstate.ScanRun(self.out, stages, via='ui') as run:
            run.enter('lexicon')
            run.enter('calibrate')
            run.fail('calibrate failed')
        p = runstate.scan_problem(self.out)
        self.assertEqual((p['state'], p['stage']), ('failed', 'calibrate'))
        self.assertEqual(p['stages'], stages)

    def test_failures_outside_the_results_chain_are_not_stale(self):
        self.start_from_good_scan()
        # calibrate alone, and the CSV export after a completed locate,
        # leave report.db exactly as current as it was
        with runstate.ScanRun(self.out, ['calibrate']) as run:
            run.enter('calibrate')
            run.fail('x')
        self.assertIsNone(runstate.scan_problem(self.out))
        with runstate.ScanRun(self.out, ['locate', 'report']) as run:
            run.enter('locate')
            run.enter('report')
            run.fail('x')
        self.assertIsNone(runstate.scan_problem(self.out))

    def test_second_concurrent_run_is_refused(self):
        self.start_from_good_scan()
        with runstate.ScanRun(self.out, ['lexicon']):
            rc, err = self.scan()
            self.assertEqual(rc, 1)
            self.assertIn('סריקה אחרת כבר רצה', err)
        self.assertIsNone(runstate.scan_problem(self.out))

    def test_stage_subprocess_records_its_failure_in_the_parent_run(self):
        self.start_from_good_scan()
        before = set(scan_record(self.out)['runs'])
        break_library(self.lib)
        run = runstate.ScanRun(self.out, ['lexicon', 'detect'], via='ui')
        try:
            run.enter('lexicon')
            with mock.patch.dict(os.environ,
                                 {runstate.ENV_RUN_ID: run.id}):
                rc, _ = self.scan('lexicon')
            self.assertEqual(rc, 1)
            # the child started no run of its own: it recorded its failure
            # in the parent's
            rec = scan_record(self.out)
            self.assertEqual(set(rec['runs']), before | {run.id})
            self.assertEqual(rec['stages']['lexicon'], run.id)
            self.assertEqual(rec['runs'][run.id]['state'], 'partial')
            # the parent only knows an exit code; the child's reason stays
            run.fail('exit code 1')
        finally:
            run.close()
        p = runstate.scan_problem(self.out)
        self.assertEqual((p['state'], p['stage']), ('partial', 'lexicon'))
        self.assertIn('לא הצליח לקרוא', p['reason'])

    def test_folder_without_marker_reports_nothing(self):
        # results from before run states were recorded work as they did
        self.start_from_good_scan()
        shutil.rmtree(os.path.join(self.out, runstate.STATE_DIR))
        self.assertIsNone(runstate.scan_problem(self.out))
        self.assertEqual(runstate.book_problems(self.out), [])

    def test_unreadable_marker_is_unknown_and_replaced_by_next_run(self):
        self.start_from_good_scan()
        with open(os.path.join(self.out, runstate.STATE_DIR, 'scan.json'),
                  'w', encoding='utf-8') as f:
            f.write('{"version": 1, "runs": ')
        self.assertEqual(runstate.scan_problem(self.out)['state'], 'unknown')
        self.good_scan()

    def test_concurrent_reads_always_see_a_whole_record(self):
        self.start_from_good_scan()
        stop, seen = threading.Event(), []

        def reader():
            while not stop.is_set():
                seen.append(runstate.scan_problem(self.out))
        t = threading.Thread(target=reader)
        t.start()
        try:
            with runstate.ScanRun(self.out, list(core._READ_STAGES)) as run:
                for _ in range(30):
                    for st in core._READ_STAGES:
                        run.enter(st)
        finally:
            stop.set()
            t.join()
        self.assertTrue(seen)
        # in progress or done — never "unknown" (a torn read) nor a crash
        self.assertEqual({p and p['state'] for p in seen}, {None})


    def test_damaged_entries_read_as_unknown_and_are_replaced(self):
        self.start_from_good_scan()
        path = os.path.join(self.out, runstate.STATE_DIR, 'scan.json')
        rec = scan_record(self.out)
        owner = rec['stages']['detect']
        rec['runs'][owner]['state'] = 'exploded'
        rec['runs']['junk'] = 5
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        self.assertEqual(runstate.scan_problem(self.out)['state'], 'unknown')
        rec['stages']['locate'] = ['not', 'an', 'id']
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        runstate.scan_problem(self.out)              # no crash
        self.good_scan()                             # nor for the next run

    def test_file_system_without_locks_neither_blocks_nor_alarms(self):
        # some network / FUSE mounts cannot lock: that must not read as
        # "another scan holds the folder" (nothing could ever run) nor as
        # "the run died" (every run in progress would be reported broken)
        self.start_from_good_scan()

        def no_locks(fd):
            raise OSError(errno.ENOLCK, 'No locks available')
        with mock.patch.object(runstate, '_lock_once', no_locks),                 contextlib.redirect_stderr(io.StringIO()):
            run = runstate.ScanRun(self.out, ['lexicon'])
            try:
                run.enter('lexicon')
                self.assertIsNone(runstate.scan_problem(self.out))
                run.done()
            finally:
                run.close()
        self.assertIsNone(runstate.scan_problem(self.out))


class BookRunTest(RunStateCase):

    def book(self, key, source='file'):
        return run_cli('book', '--book', key, '--book-source', source,
                       '--textdir', self.lib, '--out', self.out, *TUNE)

    def missing(self, name):
        """A book scan that fails: the file does not exist."""
        rc, err = self.book(os.path.join(self.lib, name + '.txt'))
        self.assertEqual(rc, 1)
        return err

    def failed_keys(self):
        return [os.path.basename(p['key'])
                for p in runstate.book_problems(self.out)]

    def test_failed_book_scan_leaves_the_scan_record_alone(self):
        self.start_from_good_scan()
        before = scan_record(self.out)
        self.missing('אין כזה')
        self.assertEqual(scan_record(self.out), before)
        self.assertIsNone(runstate.scan_problem(self.out))
        (p,) = runstate.book_problems(self.out)
        self.assertEqual(p['state'], 'failed')
        self.assertEqual(p['source'], 'file')
        self.assertTrue(p['reason'])

    def test_successful_book_scan_does_not_clear_a_stale_scan(self):
        self.start_from_good_scan()
        with mock.patch.object(core, 'locate',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.scan()
        # the book scan uses the (complete) lexicon and merges its findings,
        # but the full scan's results are as stale as before
        rc, err = self.book(os.path.join(self.lib, 'ספר.txt'))
        self.assertFalse(rc, err)
        self.assertEqual(runstate.scan_problem(self.out)['stage'], 'locate')
        self.assertEqual(runstate.book_problems(self.out), [])

    def test_failures_are_kept_per_book(self):
        # a scan of one book says nothing about another: fail X, fail Y,
        # then succeed X -> only Y is left
        self.start_from_good_scan()
        x = os.path.join(self.lib, 'ספר.txt')
        os.rename(x, x + '.away')
        self.missing('ספר')
        self.missing('אחר')
        self.assertEqual(self.failed_keys(), ['אחר.txt', 'ספר.txt'])
        os.rename(x + '.away', x)
        # spelled differently, still the same book
        rc, err = self.book(os.path.join(self.lib, '.', 'ספר.txt'))
        self.assertFalse(rc, err)
        self.assertEqual(self.failed_keys(), ['אחר.txt'])
        # a book's newer failure replaces its older one; a cancelled scan
        # changed nothing and keeps it
        self.missing('אחר')
        self.assertEqual(self.failed_keys(), ['אחר.txt'])
        with runstate.BookRun(self.out, 'file',
                              os.path.join(self.lib, 'אחר.txt')) as run:
            run.cancel()
        self.assertEqual(self.failed_keys(), ['אחר.txt'])

    def test_failure_list_is_capped(self):
        for i in range(runstate.MAX_BOOK_FAILURES + 3):
            with runstate.BookRun(self.out, 'db', str(i)) as run:
                run.fail('boom')
        keys = [p['key'] for p in runstate.book_problems(self.out)]
        self.assertEqual(len(keys), runstate.MAX_BOOK_FAILURES)
        self.assertNotIn('0', keys)                  # the oldest went first
        self.assertIn(str(runstate.MAX_BOOK_FAILURES + 2), keys)

    def test_killed_and_cancelled_book_scans(self):
        run = runstate.BookRun(self.out, 'db', '7')
        self.assertEqual(runstate.book_problems(self.out), [])  # in progress
        run.close()
        (p,) = runstate.book_problems(self.out)
        self.assertEqual((p['state'], p['key']), ('interrupted', '7'))
        # the next scan files the dead one under its book for good
        with runstate.BookRun(self.out, 'db', '8') as run:
            pass
        self.assertEqual([p['key'] for p in runstate.book_problems(self.out)],
                         ['7'])
        with runstate.BookRun(self.out, 'db', '07') as run:
            run.cancel()                    # nothing was merged
        self.assertEqual(len(runstate.book_problems(self.out)), 1)
        with runstate.BookRun(self.out, 'db', '07'):
            pass                            # the same book, done
        self.assertEqual(runstate.book_problems(self.out), [])


if __name__ == '__main__':
    unittest.main()
