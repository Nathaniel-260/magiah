# -*- coding: utf-8 -*-
"""Real-word errors judged by the neighbouring words (magiah.realword)."""
import os
import shutil
import tempfile
import unittest
from collections import Counter

from magiah import book_scan, core, realword
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


class Candidates(unittest.TestCase):

    def setUp(self):
        self.lex = core.Lexicon({'ספר': 10, 'ספד': 10, 'ספך': 1, 'מלך': 10,
                                 'מלכ': 10, 'רב': 10, 'דב': 10},
                                small_cfg())

    def test_confusable_substitution_to_a_common_word(self):
        self.assertEqual(realword.candidates('ספד', self.lex), ['ספר'])
        self.assertEqual(realword.candidates('רב', self.lex), ['דב'])

    def test_final_and_regular_forms(self):
        self.assertIn('מלכ', realword.candidates('מלך', self.lex))

    def test_only_common_words_are_judged(self):
        self.assertTrue(realword.checked('ספר', self.lex))
        self.assertFalse(realword.checked('ספך', self.lex))
        self.assertFalse(realword.checked('ר"ב', self.lex))


class CountNgrams(unittest.TestCase):

    def test_counts_only_the_wanted_ngrams(self):
        n2 = {('א', 'ב'), ('ג', 'ד')}
        n3 = {('א', 'ב', 'ג')}
        c2, c3 = Counter(), Counter()
        realword.count_ngrams(['א', 'ב', 'ג', 'ד', 'א', 'ב'], n2,
                              {'א', 'ג'}, n3, {'א'}, c2, c3)
        self.assertEqual(c2, Counter({('א', 'ב'): 2, ('ג', 'ד'): 1}))
        self.assertEqual(c3, Counter({('א', 'ב', 'ג'): 1}))


class Judge(unittest.TestCase):
    """The decision on one occurrence, with counts in memory."""

    W, S, PREV, NXT = 'ספד', 'ספר', 'ראה', 'גדול'

    def setUp(self):
        self.lex = core.Lexicon({self.W: 1000, self.S: 1000, self.PREV: 1000,
                                 self.NXT: 1000}, small_cfg())
        self.cfg = small_cfg(real_word_odds=2.0)

    def counts(self, word, n=500):
        """Counts in which `word` sits n times between PREV and NXT."""
        c2 = {(self.PREV, word): n, (word, self.NXT): n}
        c3 = {(self.PREV, word, self.NXT): n}
        need2 = {(self.PREV, x) for x in (self.W, self.S)} \
            | {(x, self.NXT) for x in (self.W, self.S)}
        need3 = {(self.PREV, x, self.NXT) for x in (self.W, self.S)}
        return realword.Counts(c2, c3, need2, need3)

    def judge(self, counts, present=None):
        return realword.judge(self.lex, counts, self.PREV, self.W, self.NXT,
                              [self.S], present or {}, self.cfg)

    def test_the_context_of_the_suggestion_gives_a_hint(self):
        v = self.judge(self.counts(self.S))
        self.assertEqual(v['state'], 'hint')
        self.assertEqual(v['sugg'], self.S)
        self.assertGreater(v['odds'], 2.0)

    def test_the_context_of_the_word_itself_gives_none(self):
        v = self.judge(self.counts(self.W))
        self.assertEqual(v['state'], 'ok')
        self.assertLess(v['odds'], 0)

    def test_threshold(self):
        odds = self.judge(self.counts(self.S))['odds']
        self.cfg.real_word_odds = odds + 0.01
        self.assertEqual(self.judge(self.counts(self.S))['state'], 'ok')

    def test_book_convention_is_relative_to_the_suggestion(self):
        c = self.counts(self.S)
        self.assertEqual(self.judge(c, {self.W: 5, self.S: 1})['state'],
                         'convention')
        # a repeated slip: the book also writes the suggestion as often
        self.assertEqual(self.judge(c, {self.W: 5, self.S: 5})['state'],
                         'hint')
        # below real_word_rep the book's count does not matter
        self.assertEqual(self.judge(c, {self.W: 2})['state'], 'hint')

    def test_uncounted_neighbours_are_stale(self):
        v = realword.judge(self.lex, self.counts(self.S), self.PREV, self.W,
                           'קטן', [self.S], {}, self.cfg)
        self.assertEqual(v['state'], 'stale')


# The scanned book: one slip (ספד for ספר between ראה and גדול) and the same
# word used rightly in another context.
BOOK_A = 'Src/a.txt'
A_LINES = ['ראה ספד גדול', 'עשו ספד מר']
OTHER = {'Src/b.txt': ['ראה ספר גדול'] * 100,
         'Src/c.txt': ['עשו ספד מר'] * 5}


class BookScan(unittest.TestCase):
    """The check inside a single-book scan over a small library."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_rw_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        write_lib(self.lib, {BOOK_A: A_LINES, **OTHER})
        self.spec = {'type': 'library', 'path': self.lib}
        core.build_lexicon(self.spec, small_cfg(), self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, verify_ctx, **kw):
        cfg = small_cfg(**{'real_word': 1, 'real_word_odds': 1.0, **kw})
        return book_scan.scan_book(self.out, 'library', BOOK_A, cfg=cfg,
                                   library_dir=self.lib,
                                   verify_ctx=verify_ctx, spec=self.spec)

    def hints(self, res):
        return [r for r in res['findings']
                if r['errtype'] == realword.ERRTYPE]

    def test_off_by_default(self):
        res = book_scan.scan_book(self.out, 'library', BOOK_A,
                                  cfg=small_cfg(), library_dir=self.lib,
                                  verify_ctx=True, spec=self.spec)
        self.assertIsNone(res['real_word'])
        self.assertEqual(self.hints(res), [])
        self.assertFalse(os.path.isdir(
            os.path.join(self.out, realword.COUNTS_DIR)))

    def test_corpus_scan_finds_the_slip(self):
        res = self.scan(verify_ctx=True)
        rows = self.hints(res)
        self.assertEqual([(r['word'], r['suggestion']) for r in rows],
                         [('ספד', 'ספר')])
        self.assertEqual(rows[0]['evidence'], 'context')
        self.assertEqual(rows[0]['ctx_hits'], 200)
        self.assertEqual(rows[0]['unit'], f'file:{BOOK_A}:0')
        self.assertEqual(res['real_word']['counts'], 'this_scan')
        self.assertEqual(res['real_word']['hints'], 1)
        self.assertEqual(res['real_word']['checked'], 2)

    def test_default_threshold_is_conservative(self):
        res = self.scan(verify_ctx=True, real_word_odds=Config().real_word_odds)
        self.assertEqual(self.hints(res), [])
        self.assertEqual(res['real_word']['checked'], 2)

    def test_the_book_itself_is_not_counted(self):
        self.scan(verify_ctx=True)
        counts, why = realword.load_counts(self.out, BOOK_A)
        self.assertIsNone(why)
        self.assertEqual(counts.tri('ראה', 'ספר', 'גדול'), 100)
        self.assertEqual(counts.tri('ראה', 'ספד', 'גדול'), 0)
        self.assertEqual(counts.bi('ראה', 'ספד'), 0)
        self.assertEqual(counts.tri('עשו', 'ספד', 'מר'), 5)

    def test_book_scope_reuses_the_saved_counts(self):
        first = self.scan(verify_ctx=True)
        again = self.scan(verify_ctx=False)
        self.assertEqual(again['real_word']['counts'], 'saved')
        self.assertEqual(self.hints(again), self.hints(first))

    def test_book_scope_without_counts_says_so(self):
        res = self.scan(verify_ctx=False)
        self.assertEqual(res['real_word']['counts'], 'none')
        self.assertEqual(self.hints(res), [])

    def test_counts_of_another_lexicon_are_not_used(self):
        self.scan(verify_ctx=True)
        lp = os.path.join(self.out, core.LEXICON_F)
        st = os.stat(lp)
        os.utime(lp, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        res = self.scan(verify_ctx=False)
        self.assertEqual(res['real_word']['counts'], 'lexicon')
        self.assertEqual(self.hints(res), [])

    def test_an_edited_neighbourhood_is_not_judged(self):
        self.scan(verify_ctx=True)
        write_lib(self.lib, {BOOK_A: ['ראה ספד קטן', 'עשו ספד מר']})
        res = self.scan(verify_ctx=False)
        self.assertEqual(res['real_word']['stale'], 1)
        self.assertEqual(res['real_word']['checked'], 1)
        self.assertEqual(self.hints(res), [])

    def test_existing_findings_do_not_change(self):
        cfg = small_cfg()
        off = book_scan.scan_book(self.out, 'library', BOOK_A, cfg=cfg,
                                  library_dir=self.lib, verify_ctx=True,
                                  spec=self.spec)
        on = self.scan(verify_ctx=True)
        rest = [r for r in on['findings'] if r['errtype'] != realword.ERRTYPE]
        self.assertEqual(rest, off['findings'])


class FullPipelineUnchanged(unittest.TestCase):
    """The context worker's new output leaves a full scan as it was."""

    def test_locate_runs(self):
        tmp = tempfile.mkdtemp(prefix='magiah_rwfull_')
        try:
            lib, out = os.path.join(tmp, 'lib'), os.path.join(tmp, 'out')
            os.makedirs(out)
            write_lib(lib, {'Src/a.txt': ['הילד ראה ספד גדול'],
                            'Src/b.txt': ['הילד ראה ספר גדול'] * 6})
            spec, cfg = {'type': 'library', 'path': lib}, small_cfg()
            core.build_lexicon(spec, cfg, out)
            core.detect(spec, cfg, out)
            core.locate(spec, cfg, out)
            self.assertTrue(os.path.isfile(os.path.join(out,
                                                        core.REPORT_DB_F)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
