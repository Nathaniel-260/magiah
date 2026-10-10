# -*- coding: utf-8 -*-
"""The double-click launchers install the zstd decoder Python < 3.14 needs.

seforim.db (schema 6) stores its text as zstd frames. Python 3.14 decodes
them with the standard library; older interpreters need the `zstandard`
package, and neither launcher.py nor run-magiah.bat ever installed it, so a
user on 3.9-3.13 met "no zstd decoder" on the first scan.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import launcher  # noqa: E402


def _quiet():
    return contextlib.redirect_stdout(io.StringIO())


class EnsureZstdTest(unittest.TestCase):

    def run_ensure(self, version, installed, frozen=False, rc=0,
                   installs=True):
        """launcher._ensure_zstd with the interpreter, the import and pip
        simulated; returns (result, pip commands run, output)."""
        calls = []
        state = {'installed': installed}

        def have():
            return state['installed']

        def pip(cmd):
            calls.append(cmd)
            if rc == 0 and installs:
                state['installed'] = True
            return rc
        out = io.StringIO()
        with mock.patch.object(launcher, '_has_zstandard', have), \
                mock.patch.object(sys, 'version_info', version), \
                contextlib.redirect_stdout(out):
            if frozen:
                with mock.patch.object(sys, 'frozen', True, create=True):
                    res = launcher._ensure_zstd(run=pip)
            else:
                res = launcher._ensure_zstd(run=pip)
        return res, calls, out.getvalue()

    def test_installs_when_missing_before_3_14(self):
        res, calls, out = self.run_ensure((3, 9, 25), installed=False)
        self.assertTrue(res)
        (cmd,) = calls
        self.assertEqual(cmd[:4], [sys.executable, '-m', 'pip', 'install'])
        self.assertIn('zstandard>=0.22', cmd)
        self.assertIn('zstandard', out)

    def test_nothing_to_do_when_present(self):
        res, calls, _ = self.run_ensure((3, 12, 0), installed=True)
        self.assertTrue(res)
        self.assertEqual(calls, [])

    def test_nothing_to_do_on_3_14(self):
        res, calls, _ = self.run_ensure((3, 14, 0), installed=False)
        self.assertTrue(res)
        self.assertEqual(calls, [])

    def test_frozen_exe_never_runs_pip(self):
        # the exe bundles what it was built with; it has no pip to call
        res, calls, _ = self.run_ensure((3, 12, 0), installed=False,
                                        frozen=True)
        self.assertEqual(calls, [])

    def test_failed_install_warns_in_hebrew_and_goes_on(self):
        res, calls, out = self.run_ensure((3, 11, 0), installed=False, rc=1)
        self.assertFalse(res)
        self.assertEqual(len(calls), 1)
        self.assertIn('pip install zstandard', out)
        self.assertIn('לא הותקנה', out)

    def test_ui_start_asks_for_it(self):
        with mock.patch.object(launcher, '_ensure_zstd') as ensure, \
                mock.patch.object(launcher, '_free_port', return_value=1), \
                mock.patch.object(sys, 'argv', ['launcher.py', '--out',
                                                os.path.join(REPO, 'x')]), \
                mock.patch('magiah.webui.server.serve',
                           side_effect=KeyboardInterrupt), _quiet():
            self.assertEqual(launcher.main(), 0)
        ensure.assert_called_once_with()

    def test_stage_mode_does_not_run_pip(self):
        # a stage subprocess of a UI scan must not stop to install anything
        with mock.patch.object(launcher, '_ensure_zstd') as ensure, \
                mock.patch.object(sys, 'argv', ['launcher.py', 'report',
                                                '--out', 'x']), \
                mock.patch('magiah.cli.main', return_value=0):
            self.assertEqual(launcher.main(), 0)
        ensure.assert_not_called()


class BatchFileTest(unittest.TestCase):
    """run-magiah.bat and its Hebrew-named twin install zstandard too (the
    .bat may fall back to `-m magiah ui`, without launcher.py)."""

    def read(self, name):
        with open(os.path.join(REPO, name), encoding='utf-8') as f:
            return f.read()

    def test_both_batch_files_install_it_and_stay_identical(self):
        bat = self.read('run-magiah.bat')
        self.assertEqual(bat, self.read('הפעלת מגיה.bat'))
        check = bat.index("__import__('zstandard')")
        install = bat.index('-m pip install "zstandard>=0.22"')
        launch = bat.index('launcher.py" --out')
        # checked, then installed, before the UI starts
        self.assertLess(check, install)
        self.assertLess(install, launch)
        self.assertIn('sys.version_info >= (3, 14)', bat)


if __name__ == '__main__':
    unittest.main()
