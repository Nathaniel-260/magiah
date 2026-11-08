# -*- coding: utf-8 -*-
"""A sound-alike letter swap one book makes in many different words is that
book's spelling (book_repeat = 2, ranked lower, never removed); look-alike
(OCR) pairs and isolated swaps stay ordinary findings."""
import os
import shutil
import sqlite3
import tempfile
import unittest

from magiah import book_scan, core
from magiah.webui import db as webdb

from test_detection import report_rows, run_full, small_cfg, write_lib, \
    write_profiles

LIB_TOP = 'Src'
BOOK_A = f'{LIB_TOP}/a.txt'
BOOK_B = f'{LIB_TOP}/b.txt'

# (book's spelling, the corpus spelling)
ALEPH_FOR_AYIN = [('אבודה', 'עבודה'), ('אגמת', 'עגמת'), ('אמידה', 'עמידה')]
SAMEKH_FOR_SIN = ('יסראל', 'ישראל')     # one isolated swap: a real typo
DALET_FOR_RESH = [('ספד', 'ספר'), ('אמד', 'אמר'), ('עיד', 'עיר')]  # OCR

A_LINES = ([f'הוא {w} גדולה' for w, _ in ALEPH_FOR_AYIN]
           + [f'כל {SAMEKH_FOR_SIN[0]} שם']
           + [f'לו {w} טוב' for w, _ in DALET_FOR_RESH])
B_LINES = ([f'הוא {c} גדולה' for _, c in ALEPH_FOR_AYIN] * 6
           + [f'כל {SAMEKH_FOR_SIN[1]} שם'] * 6
           + [f'לו {c} טוב' for _, c in DALET_FOR_RESH] * 6)


class PairsUnit(unittest.TestCase):

    def items(self, pairs, doc='d'):
        return [(doc, w, 'edit1_sub', s) for w, s in pairs]

    def test_many_different_words_make_a_convention(self):
        got = core.book_spelling_pairs(self.items(ALEPH_FOR_AYIN), 3)
        self.assertEqual(got, {'d': {'אע'}})

    def test_repeats_of_one_word_do_not(self):
        items = self.items([ALEPH_FOR_AYIN[0]] * 5)
        self.assertEqual(core.book_spelling_pairs(items, 3), {})

    def test_visual_pairs_never_become_a_convention(self):
        self.assertEqual(
            core.book_spelling_pairs(self.items(DALET_FOR_RESH), 1), {})
        for a, b in core.PHONETIC_PAIRS:
            self.assertNotIn((a, b), core.CONFUSABLE)
        self.assertNotIn(('ו', 'י'), core.PHONETIC_PAIRS)
        self.assertNotIn(('ה', 'ח'), core.PHONETIC_PAIRS)

    def test_direction_matters(self):
        rev = [(s, w) for w, s in ALEPH_FOR_AYIN[:2]]
        got = core.book_spelling_pairs(
            self.items(ALEPH_FOR_AYIN[:2] + rev), 3)
        self.assertEqual(got, {})

    def test_reviewed_ocr_profile_pair_stays_an_error(self):
        got = core.book_spelling_pairs(self.items(ALEPH_FOR_AYIN), 3,
                                       {'d': {'אע': 8}})
        self.assertEqual(got, {})
        # another book's profile does not matter
        got = core.book_spelling_pairs(self.items(ALEPH_FOR_AYIN), 3,
                                       {'x': {'אע': 8}})
        self.assertEqual(got, {'d': {'אע'}})

    def test_per_book(self):
        items = (self.items(ALEPH_FOR_AYIN[:2], 'd1')
                 + self.items(ALEPH_FOR_AYIN[2:], 'd2'))
        self.assertEqual(core.book_spelling_pairs(items, 3), {})

    def test_off(self):
        self.assertEqual(
            core.book_spelling_pairs(self.items(ALEPH_FOR_AYIN), 0), {})

    def test_only_substitutions(self):
        self.assertIsNone(core.phonetic_pair('spelling_variant', 'אבודה',
                                             'עבודה'))
        self.assertIsNone(core.phonetic_pair('edit1_sub', 'אבוד', 'עבודה'))

    def test_value(self):
        p = {'אע'}
        self.assertEqual(core.book_repeat_value('edit1_sub', 'אבודה',
                                                'עבודה', 1, True, p), 2)
        # all occurrences in one book wins (the stronger demotion)
        self.assertEqual(core.book_repeat_value('edit1_sub', 'אבודה',
                                                'עבודה', 2, True, p), 1)
        self.assertEqual(core.book_repeat_value('edit1_sub', 'יסראל',
                                                'ישראל', 1, True, p), 0)


def rank_of(row):
    con = sqlite3.connect(':memory:')
    cols = ('score', 'errtype', 'ctx_hits', 'sugg_local', 'book_repeat',
            'tanach')
    con.execute(f'CREATE TABLE t({",".join(cols)})')
    con.execute(f'INSERT INTO t VALUES({",".join("?" * len(cols))})',
                [row.get(c) or 0 if c != 'errtype' else row[c]
                 for c in cols])
    return con.execute(f'SELECT {core.RANK_SQL} FROM t').fetchone()[0]


class Pipeline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='magiah_spell_')
        cls.lib = os.path.join(cls.tmp, 'lib')
        write_lib(cls.lib, {BOOK_A: A_LINES, BOOK_B: B_LINES})
        cls.spec = {'type': 'library', 'path': cls.lib}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_case(self, name, profiles=None, **kw):
        out = os.path.join(self.tmp, name)
        os.makedirs(out)
        if profiles is not None:
            write_profiles(out, profiles)
        cfg = small_cfg(**{'book_spelling_min': 3, **kw})
        run_full(self.spec, cfg, out)
        full = {r['word']: r for r in report_rows(out, BOOK_A)}
        scans = [book_scan.scan_book(out, 'library', BOOK_A, cfg=cfg,
                                     library_dir=self.lib, verify_ctx=v,
                                     spec=self.spec)['findings']
                 for v in (False, True)]
        return full, [{r['word']: r for r in s} for s in scans]

    def check(self, rows, want):
        for w, v in want.items():
            self.assertIn(w, rows)
            self.assertEqual(rows[w]['errtype'], 'edit1_sub', w)
            self.assertEqual(rows[w]['book_repeat'], v, w)

    def test_convention_is_demoted_not_removed(self):
        full, scans = self.run_case('on')
        want = {w: 2 for w, _ in ALEPH_FOR_AYIN}
        want[SAMEKH_FOR_SIN[0]] = 0           # isolated: still a finding
        want.update({w: 0 for w, _ in DALET_FOR_RESH})   # OCR pair
        for rows in [full] + scans:
            self.check(rows, want)
        w = ALEPH_FOR_AYIN[0][0]
        r = dict(full[w])
        demoted = rank_of(r)
        r['book_repeat'] = 0
        self.assertAlmostEqual(rank_of(r) - demoted, 2.0)

    def test_off(self):
        full, scans = self.run_case('off', book_spelling_min=0)
        for rows in [full] + scans:
            self.check(rows, {w: 0 for w, _ in ALEPH_FOR_AYIN})

    def test_reviewed_ocr_profile_overrides(self):
        full, scans = self.run_case('prof', profiles={BOOK_A: {'אע': 8}})
        for rows in [full] + scans:
            self.check(rows, {w: 0 for w, _ in ALEPH_FOR_AYIN})


class RankSqlInSync(unittest.TestCase):

    def test_webui_copy_matches_core(self):
        self.assertEqual(webdb.RANK_SQL, core.RANK_SQL)
        self.assertEqual(webdb.VERIFIED_SQL, core.VERIFIED_SQL)


if __name__ == '__main__':
    unittest.main()
