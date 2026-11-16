# -*- coding: utf-8 -*-
"""`magiah guard`: a correction that turns a common word into an unknown one
is suspect; one that turns an unknown word into a common one is supported.

Invented words only. Run:  python -X utf8 -m unittest tests.test_guard -v
"""
import contextlib
import io
import os
import pickle
import shutil
import tempfile
import unittest

from magiah import cli, guard
from magiah.config import Config

# corpus counts: common >= 30, rare <= 2 (the Config defaults)
FREQ = {
    'אמר': 900, 'שמעון': 400, 'דברים': 700, 'טובים': 500, 'אמרו': 300,
    'כלבו': 1, 'שמעיו': 0, 'גבלו': 2, 'לחם': 120, 'בבית': 800,
    'הכנסת': 600, 'בביתהכנסת': 0, 'מזרון': 10,
}


def verdicts(before, after, cfg=None):
    return [(r['old'], r['new'], r['verdict'])
            for r in guard.compare(FREQ, before, after, cfg or Config())]


class TestCompare(unittest.TestCase):

    def test_unchanged_text_has_no_changes(self):
        self.assertEqual(verdicts('אמר שמעון דברים', 'אמר שמעון דברים'), [])

    def test_common_word_broken_is_suspect(self):
        self.assertEqual(verdicts('אמר שמעון דברים', 'אמר שמעיו דברים'),
                         [('שמעון', 'שמעיו', 'suspect')])

    def test_rare_word_fixed_is_supported(self):
        self.assertEqual(verdicts('אמר כלבו דברים', 'אמר לחם דברים'),
                         [('כלבו', 'לחם', 'supported')])

    def test_common_to_common_is_neutral(self):
        # a real-word change: the lexicon cannot tell which is right
        self.assertEqual(verdicts('אמר דברים', 'אמרו דברים'),
                         [('אמר', 'אמרו', 'neutral')])

    def test_middle_frequency_is_neutral(self):
        # 10 is neither rare (<= 2) nor common (>= 30)
        self.assertEqual(verdicts('אמר מזרון', 'אמר שמעיו'),
                         [('מזרון', 'שמעיו', 'neutral')])

    def test_join_is_one_unit_by_its_least_frequent_word(self):
        # בבית + הכנסת (both common) -> one unknown word
        self.assertEqual(verdicts('בבית הכנסת דברים', 'בביתהכנסת דברים'),
                         [('בבית הכנסת', 'בביתהכנסת', 'suspect')])

    def test_split_is_one_unit(self):
        self.assertEqual(verdicts('בביתהכנסת דברים', 'בבית הכנסת דברים'),
                         [('בביתהכנסת', 'בבית הכנסת', 'supported')])

    def test_deleted_word_is_never_suspect(self):
        self.assertEqual(verdicts('אמר שמעון דברים', 'אמר דברים'),
                         [('שמעון', '', 'neutral')])

    def test_inserted_unknown_word(self):
        # nothing was there before: f_old is 0, so it is not "broken"
        self.assertEqual(verdicts('אמר דברים', 'אמר שמעיו דברים'),
                         [('', 'שמעיו', 'neutral')])

    def test_several_changes_in_reading_order_with_lines(self):
        rows = guard.compare(FREQ, 'אמר כלבו\nשמעון דברים',
                             'אמר לחם\nשמעיו דברים', Config())
        self.assertEqual([(r['line'], r['verdict']) for r in rows],
                         [(1, 'supported'), (2, 'suspect')])
        self.assertEqual((rows[1]['f_old'], rows[1]['f_new']), (400, 0))

    def test_markup_and_nikud_are_ignored(self):
        # the same normalization as the scan: tags and points are not words
        self.assertEqual(verdicts('<b>אמר</b> שִׁמְעוֹן', 'אמר שמעון'), [])

    def test_thresholds_come_from_config(self):
        cfg = Config(common_min=1000)
        self.assertEqual(verdicts('אמר שמעון', 'אמר שמעיו', cfg),
                         [('שמעון', 'שמעיו', 'neutral')])
        cfg = Config(rare_max=0)
        self.assertEqual(verdicts('אמר גבלו', 'אמר לחם', cfg),
                         [('גבלו', 'לחם', 'neutral')])


class TestCommand(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        with open(os.path.join(self.out, 'lexicon.pkl'), 'wb') as f:
            pickle.dump(FREQ, f)
        self.before = self._write('before.txt', 'אמר כלבו\nשמעון דברים\n')
        self.after = self._write('after.txt', 'אמר לחם\nשמעיו דברים\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p

    def _run(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['guard', '--out', self.out, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_lists_only_suspect_changes(self):
        code, out, _ = self._run('--before', self.before,
                                 '--after', self.after)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], '2\tsuspect\tשמעון\tשמעיו\t400\t0')
        self.assertIn('2 changes -- 1 suspect, 1 supported, 0 neutral',
                      lines[-1])
        self.assertEqual(len(lines), 2)

    def test_guard_all_lists_every_change(self):
        _, out, _ = self._run('--before', self.before, '--after', self.after,
                              '--guard-all')
        self.assertEqual([ln.split('\t')[1] for ln in out.splitlines()[:-1]],
                         ['supported', 'suspect'])

    def test_writes_nothing(self):
        before = sorted(os.listdir(self.out))
        self._run('--before', self.before, '--after', self.after)
        self.assertEqual(sorted(os.listdir(self.out)), before)

    def test_needs_both_files(self):
        code, _, err = self._run('--before', self.before)
        self.assertEqual(code, 1)
        self.assertIn('--after', err)

    def test_no_lexicon_is_refused_in_hebrew(self):
        os.remove(os.path.join(self.out, 'lexicon.pkl'))
        code, _, err = self._run('--before', self.before,
                                 '--after', self.after)
        self.assertEqual(code, 1)
        self.assertIn('lexicon.pkl', err)

    def test_missing_file_is_refused(self):
        code, _, err = self._run('--before', self.before,
                                 '--after', os.path.join(self.tmp, 'nope'))
        self.assertEqual(code, 1)
        self.assertTrue(err)

    def test_cp1255_file_is_read(self):
        p = os.path.join(self.tmp, 'after1255.txt')
        with open(p, 'wb') as f:
            f.write('אמר לחם\nשמעיו דברים\n'.encode('cp1255'))
        code, out, _ = self._run('--before', self.before, '--after', p)
        self.assertEqual(code, 0)
        self.assertIn('suspect\tשמעון\tשמעיו', out)

    def test_thresholds_from_the_command_line(self):
        _, out, _ = self._run('--before', self.before, '--after', self.after,
                              '--common-min', '1000')
        self.assertIn('0 suspect', out)


if __name__ == '__main__':
    unittest.main()
