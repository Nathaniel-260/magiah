# -*- coding: utf-8 -*-
"""The name list: a misspelled known name after a title is still reported."""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from collections import Counter

from magiah import book_scan, core
from magiah.config import Config
from magiah.normalize import tokenize
from magiah.webui import patcher, scanner

from test_anchor_write import TempCase


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


# Invented words: אברהם is the name, אכרהם a one-letter slip of it; הדכר a
# slip of the common word הדבר.
NAME, SLIP = 'אברהם', 'אכרהם'
TYPO, WORD = 'הדכר', 'הדבר'
FLAGGED = {SLIP: (1, 'edit1_sub', NAME, 150, 4.0),
           TYPO: (1, 'edit1_sub', WORD, 200, 4.0)}
FREQ = {'אמר': 200, 'ר': 200, NAME: 150, WORD: 200, 'יוסף': 200,
        'משה': 200, 'חיים': 200, 'בן': 200, SLIP: 1, TYPO: 1}


def occ_words(line, names, cfg=None, in_corpus=True):
    res = core.locate_line(line, FREQ, cfg or small_cfg(names_sugg=100),
                           FLAGGED, None, names, in_corpus)
    return [o[0] for o in res[0]]


def names_of(lines):
    c = Counter()
    for ln in lines:
        core.count_names(tokenize(ln), c)
    return dict(c)


class CountNames(unittest.TestCase):

    def test_up_to_three_words_after_a_title(self):
        c = Counter()
        core.count_names(['אמר', 'ר', 'יוסף', 'משה', 'שמעון', 'היום'], c)
        self.assertEqual(c, Counter({'יוסף': 1, 'משה': 1, 'שמעון': 1}))

    def test_no_title_no_names(self):
        c = Counter()
        core.count_names(['אמר', 'יוסף', 'היום'], c)
        self.assertEqual(c, Counter())

    def test_ben_and_bar_are_not_titles(self):
        # בן אדם, בר מצוה: not names
        c = Counter()
        core.count_names(['בן', 'אדם', 'בר', 'מצוה'], c)
        self.assertEqual(c, Counter())

    def test_the_list_titles_include_the_rule_titles(self):
        self.assertTrue(core.NAME_TRIGGERS <= core.NAME_LIST_BEFORE)
        self.assertGreaterEqual(core.NAME_COUNT_WINDOW, core.NAME_WINDOW)

    def test_abbreviations_and_single_letters_are_not_names(self):
        c = Counter()
        core.count_names(['ר', 'ח', 'ר', 'רמב"ם'], c)
        self.assertEqual(c, Counter())


class NameSlip(unittest.TestCase):

    def test_without_a_list_or_the_option_nothing_changes(self):
        self.assertFalse(core.name_slip(['ר', SLIP], 1, NAME, None,
                                        small_cfg(names_sugg=100)))
        self.assertFalse(core.name_slip(['ר', SLIP], 1, NAME,
                                        {NAME: 500}, small_cfg()))

    def test_negative_threshold_is_off(self):
        self.assertFalse(core.names_on(small_cfg(names_sugg=-1)))
        self.assertFalse(core.name_slip(['ר', TYPO], 1, WORD,
                                        {NAME: 500}, small_cfg(names_sugg=-1)))
        self.assertEqual(occ_words(f'אמר ר {TYPO}', {NAME: 500},
                                   small_cfg(names_sugg=-1)), [])

    def test_unseen_word_with_a_known_name_as_suggestion(self):
        cfg = small_cfg(names_sugg=100)
        self.assertTrue(core.name_slip(['ר', SLIP], 1, NAME,
                                       {NAME: 100}, cfg))
        self.assertFalse(core.name_slip(['ר', SLIP], 1, NAME,
                                        {NAME: 99}, cfg))

    def test_this_occurrence_is_not_evidence_for_itself(self):
        cfg = small_cfg(names_sugg=100)
        names = {NAME: 500, SLIP: 1}
        self.assertTrue(core.name_slip(['ר', SLIP], 1, NAME, names, cfg))
        # ...unless the text is not part of the corpus the list came from
        self.assertFalse(core.name_slip(['ר', SLIP], 1, NAME, names, cfg,
                                        in_corpus=False))
        # seen as a name somewhere else too: a name of its own
        self.assertFalse(core.name_slip(['ר', SLIP], 1, NAME,
                                        {NAME: 500, SLIP: 2}, cfg))

    def test_counted_window_matches_the_subtraction(self):
        # a 3rd-word name is counted, so a corpus line with it at 3rd place
        # and the same word 2nd elsewhere: both counted, never "unseen"
        names = names_of([f'אמר ר משה חיים {SLIP}', f'אמר ר {SLIP} הדבר']
                         + [f'אמר ר {NAME} הדבר'] * 120)
        self.assertEqual(names[SLIP], 2)
        self.assertEqual(occ_words(f'אמר ר {SLIP} הדבר', names), [])


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

    def test_the_option_never_hides_a_finding(self):
        # the name is two words after the title, with or without the list:
        # a typo of a common word in third place is reported either way
        line = f'אמר ר יוסף משה {TYPO}'
        self.assertEqual(occ_words(line, None, cfg=small_cfg()), [TYPO])
        self.assertEqual(occ_words(line, {NAME: 500}), [TYPO])
        line = f'אמר ר יוסף {TYPO}'
        self.assertEqual(occ_words(line, {NAME: 500}, cfg=small_cfg()), [])
        self.assertEqual(occ_words(line, {NAME: 500}), [])

    def test_split_rules_ignore_names_as_before(self):
        flagged = {SLIP: (1, 'missing_space', 'אכר הם', 0, 3.0)}
        res = core.locate_line(self.LINE, FREQ, small_cfg(names_sugg=100),
                               flagged, None, {SLIP: 9})
        self.assertEqual([o[0] for o in res[0]], [SLIP])


class WebuiValidation(unittest.TestCase):

    def test_negative_names_sugg_is_refused(self):
        d = tempfile.mkdtemp()
        try:
            with self.assertRaises(ValueError):
                scanner._merge_config(d, {'names_sugg': '-1'})
            self.assertEqual(
                scanner._merge_config(d, {'names_sugg': '100'}).names_sugg,
                100)
        finally:
            shutil.rmtree(d, ignore_errors=True)


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

    def lexicon(self, cfg=None):
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        core.build_lexicon(self.spec, cfg or small_cfg(), out)
        return out

    def test_lexicon_stage_writes_the_list(self):
        rec = core.load_names(self.lexicon())
        names = rec['names']
        self.assertEqual(names[NAME], 120)
        self.assertEqual(names[SLIP], 1)
        self.assertEqual(names['הדבר'], 121)
        self.assertEqual(sorted(rec['docs']), [BOOK_A, 'Src/b.txt'])

    def test_a_list_of_another_lexicon_is_not_used(self):
        out = self.lexicon()
        old = os.path.join(self.tmp, 'old_names.pkl')
        shutil.copy(os.path.join(out, core.NAMES_F), old)
        core.build_lexicon(self.spec, small_cfg(), out)
        self.assertIsNotNone(core.load_names(out))
        shutil.copy(old, os.path.join(out, core.NAMES_F))
        self.assertIsNone(core.load_names(out))

    def test_a_lexicon_without_an_id_has_no_list(self):
        out = self.lexicon()
        cov = os.path.join(out, core.COVERAGE_F.format(stage='lexicon'))
        with open(cov, encoding='utf-8') as f:
            rec = json.load(f)
        del rec['lexicon_id']
        with open(cov, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        self.assertIsNone(core.load_names(out))

    def test_a_copied_results_folder_keeps_its_list(self):
        out = self.lexicon()
        dst = os.path.join(self.tmp, 'copied')
        shutil.copytree(out, dst)
        # a copy has fresh mtimes; the list still belongs to the lexicon
        os.utime(os.path.join(dst, core.LEXICON_F), (1, 1))
        self.assertIsNotNone(core.load_names(dst))

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
        out = self.lexicon(cfg)
        path = os.path.join(self.tmp, 'new book.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'אמר ר {SLIP} הדבר\n')
        res = book_scan.scan_book(out, 'file', path, cfg=cfg,
                                  library_dir=self.lib, spec=self.spec)
        # the corpus has SLIP once in a name position (in a.txt): a name
        self.assertEqual([r for r in res['findings'] if r['word'] == SLIP],
                         [])

    def test_a_corpus_file_opened_by_path_is_in_the_corpus(self):
        cfg = small_cfg(names_sugg=100)
        out = self.lexicon(cfg)
        res = book_scan.scan_book(out, 'file',
                                  os.path.join(self.lib, 'Src', 'a.txt'),
                                  cfg=cfg, library_dir=self.lib)
        self.assertEqual([r['word'] for r in res['findings']
                          if r['word'] == SLIP], [SLIP])

    def test_a_book_of_another_library_is_not_in_the_corpus(self):
        cfg = small_cfg(names_sugg=100)
        out = self.lexicon(cfg)
        lib2 = os.path.join(self.tmp, 'lib2')
        write_lib(lib2, {BOOK_A: A_LINES})
        lib_res = book_scan.scan_book(out, 'library', BOOK_A, cfg=cfg,
                                      library_dir=lib2)
        file_res = book_scan.scan_book(out, 'file',
                                       os.path.join(lib2, 'Src', 'a.txt'),
                                       cfg=cfg, library_dir=lib2)
        for res in (lib_res, file_res):
            self.assertEqual([r for r in res['findings']
                              if r['word'] == SLIP], [])

    def test_book_scan_says_when_the_list_is_missing(self):
        cfg = small_cfg(names_sugg=100)
        out = self.lexicon(cfg)
        os.remove(os.path.join(out, core.NAMES_F))
        said = []
        book_scan.scan_book(out, 'library', BOOK_A, cfg=cfg,
                            library_dir=self.lib, progress=said.append)
        self.assertTrue(any(core.NAMES_F in s for s in said))


PAD = ' '.join(['והלכה כדברי האומר שהתפלה במקום קרבן היא'] * 3)


def scanned(line, w, fix, names, **kw):
    """The findings of `line` as the review gives them to the fixer."""
    freq = Counter({t: 10 ** 6 for t in tokenize(line)})
    freq[w] = 1
    occ = core.locate_line(line, freq, Config(**kw),
                           {w: ('error', 'edit1_sub', fix)}, None, names)[0]
    return [{'id': i + 1, 'lineno': 0, 'word': x, 'correction': fix,
             'suggestion': fix, 'errtype': 'edit1_sub', 'snippet': snip,
             'occurrence': i, 'expected_count': len(occ)}
            for i, (x, _p, _n, snip) in enumerate(occ)]


class FixerFollowsTheNameList(TempCase):
    """The fixer's model of the scan skips the same copies the scan did."""

    def test_a_name_slip_after_a_title_can_be_written(self):
        line = f'אמר רבי {SLIP} כעבד לפני {SLIP} ולכן ' + PAD
        fs = scanned(line, SLIP, NAME, {NAME: 500}, names_sugg=100)
        self.assertEqual(len(fs), 2)
        starts = []
        for f in fs:
            with self.subTest(occ=f['occurrence']):
                p = patcher.plan_edit(self.doc(line + '\n'), dict(f))
                starts.append(p.start)
        self.assertEqual(starts, [line.index(SLIP), line.rindex(SLIP)])

    def test_the_name_copy_is_still_skipped_without_the_list(self):
        line = f'אמר רבי {SLIP} כעבד לפני {SLIP} ולכן ' + PAD
        fs = scanned(line, SLIP, NAME, {NAME: 500})
        self.assertEqual(len(fs), 1)
        p = patcher.plan_edit(self.doc(line + '\n'), dict(fs[0]))
        self.assertEqual(p.start, line.rindex(SLIP))

    def test_a_third_word_copy_is_reported_and_written(self):
        line = f'אמר רבי יוסף משה {TYPO} כעבד לפני {TYPO} ולכן ' + PAD
        fs = scanned(line, TYPO, WORD, {NAME: 500}, names_sugg=100)
        self.assertEqual(len(fs), 2)
        p = patcher.plan_edit(self.doc(line + '\n'), dict(fs[1]))
        self.assertEqual(p.start, line.rindex(TYPO))


if __name__ == '__main__':
    unittest.main()
