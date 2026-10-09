# -*- coding: utf-8 -*-
"""A scan whose input cannot be read ends with a Hebrew error and exit code
1, and leaves the outputs of the last good scan as they were.

A scan of nothing is the dangerous case: a library folder that is missing,
empty or unreadable used to read as a corpus without a single typo, so the
scan wrote an empty report.db and a refresh of the review then removed every
finding.
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from magiah import cli, core, corpus_hybrid
from magiah.config import Config

TUNE = ['--workers', '1', '--n-chunks', '1']
OUTPUTS = ('lexicon.pkl', 'flagged.pkl', 'report.db', 'coverage_lexicon.json',
           'coverage_detect.json', 'coverage_locate.json', 'run_config.json')


def make_library(root):
    """A one-book library repo (one folder per origin)."""
    origin = os.path.join(root, 'DictaToOtzaria')
    os.makedirs(origin, exist_ok=True)
    with open(os.path.join(origin, 'ספר.txt'), 'w', encoding='utf-8') as f:
        f.write('בראשית ברא אלהים את השמים ואת הארץ\n' * 60)
        f.write('בדאשית ברא אלהים את השמים ואת הארץ\n')


def run_cli(*argv):
    """cli.main quietly; returns (exit code, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, err.getvalue()


def snapshot(out):
    """The bytes of every output a scan writes."""
    snap = {}
    for name in OUTPUTS:
        p = os.path.join(out, name)
        if os.path.exists(p):
            with open(p, 'rb') as f:
                snap[name] = f.read()
    return snap


def report_rows(out):
    con = sqlite3.connect(os.path.join(out, core.REPORT_DB_F))
    try:
        return con.execute('SELECT COUNT(*) FROM occurrences_full'
                           ).fetchone()[0]
    finally:
        con.close()


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpl = tempfile.TemporaryDirectory(prefix='magiah_input_t_')
        cls._lib = os.path.join(cls._tmpl.name, 'lib')
        make_library(cls._lib)
        cls._out = os.path.join(cls._tmpl.name, 'out')
        rc, err = run_cli('all', '--library', cls._lib, '--out', cls._out,
                          *TUNE)
        assert rc in (0, None), err

    @classmethod
    def tearDownClass(cls):
        cls._tmpl.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='magiah_input_')
        self.lib = os.path.join(self.tmp.name, 'lib')
        shutil.copytree(self._lib, self.lib)
        self.out = os.path.join(self.tmp.name, 'out')
        shutil.copytree(self._out, self.out)
        # the copy remembers this test's own library
        rc_path = os.path.join(self.out, 'run_config.json')
        with open(rc_path, encoding='utf-8') as f:
            rc = json.load(f)
        rc['corpus']['path'] = self.lib
        with open(rc_path, 'w', encoding='utf-8') as f:
            json.dump(rc, f, ensure_ascii=False, indent=2)
        self.before = snapshot(self.out)
        self.assertGreater(report_rows(self.out), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_refused(self, rc, err, *phrases):
        self.assertEqual(rc, 1)
        self.assertNotIn('Traceback', err)
        for p in phrases:
            self.assertIn(p, err)

    def assert_untouched(self, *changed):
        after = snapshot(self.out)
        for name, data in self.before.items():
            if name not in changed:
                self.assertEqual(after.get(name), data, name)
        self.assertGreater(report_rows(self.out), 0)


class EmptyLibraryTest(_Case):

    def test_missing_library_folder(self):
        missing = os.path.join(self.tmp.name, 'אין כזו')
        rc, err = run_cli('all', '--library', missing, '--out', self.out,
                          *TUNE)
        self.assert_refused(rc, err, 'תיקיית הספרייה לא נמצאה', missing)
        # nothing at all is written, not even the remembered setting
        self.assert_untouched()
        self.assertFalse(os.path.exists(missing))

    def test_missing_library_folder_from_run_config(self):
        # the remembered library is gone (no --library on the command line)
        shutil.rmtree(self.lib)
        rc, err = run_cli('all', '--out', self.out, *TUNE)
        self.assert_refused(rc, err, 'תיקיית הספרייה לא נמצאה')
        self.assert_untouched()

    def test_hybrid_with_missing_library_folder(self):
        missing = os.path.join(self.tmp.name, 'nope')
        db = os.path.join(self.tmp.name, 'seforim.db')
        rc, err = run_cli('all', '--library', missing, '--db', db,
                          '--out', self.out, *TUNE)
        self.assert_refused(rc, err, 'תיקיית הספרייה לא נמצאה')
        self.assert_untouched()

    def test_library_without_books(self):
        os.remove(os.path.join(self.lib, 'DictaToOtzaria', 'ספר.txt'))
        rc, err = run_cli('all', '--library', self.lib, '--out', self.out,
                          *TUNE)
        self.assert_refused(rc, err, 'לא נמצאו קובצי ספרים')
        # the library setting is the same; the outputs are untouched
        self.assert_untouched('run_config.json')

    def test_unreadable_library_folder(self):
        real = os.listdir

        def listdir(path='.'):
            if os.path.abspath(path) == os.path.abspath(self.lib):
                raise PermissionError(13, 'Access is denied', path)
            return real(path)
        with mock.patch.object(corpus_hybrid.os, 'listdir', listdir):
            rc, err = run_cli('all', '--library', self.lib, '--out',
                              self.out, *TUNE)
        self.assert_refused(rc, err, 'לא ניתן לקרוא את תיקיית הספרייה')
        self.assert_untouched()

    def test_each_reading_stage_refuses_a_library_emptied_meanwhile(self):
        os.remove(os.path.join(self.lib, 'DictaToOtzaria', 'ספר.txt'))
        for stage in ('lexicon', 'detect', 'locate'):
            rc, err = run_cli(stage, '--out', self.out, *TUNE)
            self.assert_refused(rc, err, 'לא נמצאו קובצי ספרים')
        self.assert_untouched()

    def test_missing_text_folder(self):
        missing = os.path.join(self.tmp.name, 'nope')
        rc, err = run_cli('all', '--textdir', missing, '--out', self.out,
                          *TUNE)
        self.assert_refused(rc, err, 'תיקיית הטקסטים לא נמצאה')
        self.assert_untouched()

    def test_text_folder_without_text_files(self):
        empty = os.path.join(self.tmp.name, 'empty')
        os.makedirs(empty)
        rc, err = run_cli('all', '--textdir', empty, '--out', self.out,
                          *TUNE)
        self.assert_refused(rc, err, 'לא נמצאו קובצי טקסט')
        self.assert_untouched('run_config.json')


class NoTextReadTest(unittest.TestCase):
    """The backstop for any source: a pass that read no line writes
    nothing."""

    def test_empty_table(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, 'empty.db')
            con = sqlite3.connect(db)
            con.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, txt TEXT)')
            con.commit()
            con.close()
            spec = {'type': 'sqlite', 'path': db, 'table': 't',
                    'id_col': 'id', 'text_col': 'txt'}
            out = os.path.join(d, 'out')
            os.makedirs(out)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(core.StageError) as cm:
                    core.build_lexicon(spec, Config(workers=1, n_chunks=1),
                                       out)
            self.assertIn('לא קרא אף שורת טקסט', str(cm.exception))
            self.assertEqual(os.listdir(out), [])

    def test_cli_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, 'empty.db')
            con = sqlite3.connect(db)
            con.execute('CREATE TABLE line(id INTEGER PRIMARY KEY, '
                        'content TEXT)')
            con.execute("INSERT INTO line VALUES(1, NULL)")
            con.commit()
            con.close()
            rc, err = run_cli('all', '--sqlite', db, '--out',
                              os.path.join(d, 'out'), *TUNE)
            self.assertEqual(rc, 1)
            self.assertIn('לא קרא אף שורת טקסט', err)
            self.assertFalse(os.path.exists(
                os.path.join(d, 'out', core.LEXICON_F)))


if __name__ == '__main__':
    unittest.main()
