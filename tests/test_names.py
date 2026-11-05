# -*- coding: utf-8 -*-
"""The name list: a misspelled known name after a title is still reported."""
import os
import pickle
import shutil
import sqlite3
import tempfile
import unittest
from collections import Counter

from magiah import book_scan, core
from magiah.config import Config


def small_cfg(**kw):
    base = dict(rare_max=2, common_min=3, part_min=3, join_min=3,
                ed1_ratio=3, split_obs_min=1, split_obs_min_short=1,
                exp_prefilter=0.0, foreign_ratio=1.0, workers=1, n_chunks=2)
    base.update(kw)
    return Config(**base)


def write_lib(root, books):
    for rel, lines in books.items():
        p = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')


# Invented words: אברהם is the name, אכרהם a one-letter slip of it.
NAME, SLIP = 'אברהם', 'אכרהם'
FLAGGED = {SLIP: (1, 'edit1_sub', NAME, 150, 4.0)}
FREQ = {'אמר': 200, 'ר': 200, NAME: 150, 'הדבר': 200, 'יוסף': 200,
        'בן': 200, SLIP: 1}


def occ_words(line, names, cfg=None, in_corpus=True):
    res = core.locate_line(line, FREQ, cfg or small_cfg(names_sugg=100),
                           FLAGGED, None, names, in_corpus)
    return [o[0] for o in res[0]]


class CountNames(unittest.TestCase):

    def test_one_or_two_words_after_a_title(self):
        c = Counter()
        core.count_names(['אמר', 'ר', 'יוסף', 'בן', 'שמעון', 'היום'], c)
        self.assertEqual(c, Counter({'יוסף': 1, 'בן': 1, 'שמעון': 1,
                                     'היום': 1}))

    def test_no_title_no_names(self):
        c = Counter()
        core.count_names(['אמר', 'יוסף', 'היום'], c)
        self.assertEqual(c, Counter())

    def test_abbreviations_and_single_letters_are_not_names(self):
        c = Counter()
        core.count_names(['ר', 'ח', 'ר', 'רמב"ם'], c)
        self.assertEqual(c, Counter())


class NameSlip(unittest.TestCase):

    def test_without_a_list_or_the_option_nothing_changes(self):
        self.assertFalse(core.name_slip(['ר', SLIP], 1, SLIP, NAME, None,
                                        small_cfg(names_sugg=100)))
        self.assertFalse(core.name_slip(['ר', SLIP], 1, SLIP, NAME,
                                        {NAME: 500}, small_cfg()))

    def test_unseen_word_with_a_known_name_as_suggestion(self):
        cfg = small_cfg(names_sugg=100)
        self.assertTrue(core.name_slip(['ר', SLIP], 1, SLIP, NAME,
                                       {NAME: 100}, cfg))
        self.assertFalse(core.name_slip(['ר', SLIP], 1, SLIP, NAME,
                                        {NAME: 99}, cfg))

    def test_this_occurrence_is_not_evidence_for_itself(self):
        cfg = small_cfg(names_sugg=100)
        names = {NAME: 500, SLIP: 1}
        self.assertTrue(core.name_slip(['ר', SLIP], 1, SLIP, NAME, names,
                                       cfg))
        # ...unless the text is not part of the corpus the list came from
        self.assertFalse(core.name_slip(['ר', SLIP], 1, SLIP, NAME, names,
                                        cfg, in_corpus=False))
        # seen as a name somewhere else too: a name of its own
        self.assertFalse(core.name_slip(['ר', SLIP], 1, SLIP, NAME,
                                        {NAME: 500, SLIP: 2}, cfg))


class LocateLine(unittest.TestCase):

    LINE = f'אמר ר {SLIP} הדבר'

    def test_without_a_list_the_name_is_skipped(self):
        self.assertEqual(occ_words(self.LINE, None), [])

    def test_option_off_the_name_is_skipped(self):
        self.assertEqual(occ_words(self.LINE, {NAME: 500},
                                   cfg=small_cfg()), [])

    def test_misspelled_known_name_is_reported(self):
        self.assertEqual(occ_words(self.LINE, {NAME: 500}), [SLIP])

    def test_rare_name_like_a_rare_suggestion_is_skipped(self):
        self.assertEqual(occ_words(self.LINE, {NAME: 50}), [])

    def test_the_third_word_belongs_to_the_name_with_a_list(self):
        line = f'אמר ר יוסף בן {SLIP}'
        # the old two-word name: the third word is checked as any word
        self.assertEqual(occ_words(line, None), [SLIP])
        # with the list: inside the name, and seen there elsewhere
        self.assertEqual(occ_words(line, {NAME: 500, SLIP: 5}), [])
        self.assertEqual(occ_words(line, {NAME: 500}, cfg=small_cfg(
            names_sugg=100, name_window=2)), [SLIP])

    def test_split_rules_ignore_names_as_before(self):
        flagged = {SLIP: (1, 'missing_space', 'אכר הם', 0, 3.0)}
        res = core.locate_line(self.LINE, FREQ, small_cfg(names_sugg=100),
                               flagged, None, {SLIP: 9})
        self.assertEqual([o[0] for o in res[0]], [SLIP])


BOOK_A = 'Src/a.txt'
A_LINES = [f'אמר ר {SLIP} הדבר']
B_LINES = [f'אמר ר {NAME} הדבר'] * 120


def report_rows(out, doc):
    con = sqlite3.connect(os.path.join(out, core.REPORT_DB_F))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        'SELECT * FROM occurrences_full WHERE doc = ?', (doc,))]
    con.close()
    return rows


class Pipeline(unittest.TestCase):
    """The list is built with the lexicon; full and book scans agree."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='magiah_names_')
        cls.lib = os.path.join(cls.tmp, 'lib')
        write_lib(cls.lib, {BOOK_A: A_LINES, 'Src/b.txt': B_LINES})
        cls.spec = {'type': 'library', 'path': cls.lib}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_full(self, cfg):
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        core.build_lexicon(self.spec, cfg, out)
        core.detect(self.spec, cfg, out)
        core.locate(self.spec, cfg, out)
        core.report(cfg, out)
        return out

    def test_lexicon_stage_writes_the_list(self):
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        core.build_lexicon(self.spec, small_cfg(), out)
        names = core.load_names(out)
        self.assertEqual(names[NAME], 120)
        self.assertEqual(names[SLIP], 1)
        self.assertEqual(names['הדבר'], 121)

    def test_a_list_of_another_lexicon_is_not_used(self):
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        core.build_lexicon(self.spec, small_cfg(), out)
        lp = os.path.join(out, core.LEXICON_F)
        with open(lp, 'rb') as f:
            lex = pickle.load(f)
        lex['חדש'] = 1
        with open(lp, 'wb') as f:
            pickle.dump(lex, f)
        self.assertIsNone(core.load_names(out))

    def test_default_keeps_the_old_rule(self):
        out = self.run_full(small_cfg())
        self.assertEqual([r for r in report_rows(out, BOOK_A)
                          if r['word'] == SLIP], [])

    def test_full_and_book_scan_report_the_misspelled_name(self):
        cfg = small_cfg(names_sugg=100)
        out = self.run_full(cfg)
        full = [r for r in report_rows(out, BOOK_A) if r['word'] == SLIP]
        self.assertEqual([r['suggestion'] for r in full], [NAME])
        res = book_scan.scan_book(out, 'library', BOOK_A, cfg=cfg,
                                  library_dir=self.lib, verify_ctx=True,
                                  spec=self.spec)
        book = [r for r in res['findings'] if r['word'] == SLIP]
        self.assertEqual([(r['suggestion'], r['unit'], r['evidence'])
                          for r in book],
                         [(r['suggestion'], r['unit'], r['evidence'])
                          for r in full])

    def test_file_outside_the_library_keeps_a_name_the_corpus_knows(self):
        cfg = small_cfg(names_sugg=100)
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        core.build_lexicon(self.spec, cfg, out)
        path = os.path.join(self.tmp, 'new book.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'אמר ר {SLIP} הדבר\n')
        res = book_scan.scan_book(out, 'file', path, cfg=cfg,
                                  library_dir=self.lib, spec=self.spec)
        # the corpus has SLIP once in a name position (in a.txt): a name
        self.assertEqual([r for r in res['findings'] if r['word'] == SLIP],
                         [])


if __name__ == '__main__':
    unittest.main()
