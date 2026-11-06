# -*- coding: utf-8 -*-
"""Real-word errors judged by the neighbouring words (magiah.realword)."""
import math
import os
import pickle
import shutil
import tempfile
import unittest
from collections import Counter
from unittest import mock

from magiah import book_scan, core, realword
from magiah.config import Config
from magiah.webui import scanner


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
        # off: the result has no trace of the check
        self.assertNotIn('real_word', res)
        self.assertEqual(self.hints(res), [])
        self.assertFalse(os.path.isdir(
            os.path.join(self.out, realword.COUNTS_DIR)))

    def test_corpus_scan_finds_the_slip(self):
        res = self.scan(verify_ctx=True)
        rows = self.hints(res)
        self.assertEqual([(r['word'], r['suggestion']) for r in rows],
                         [('ספד', 'ספר')])
        # a language-model judgement: never 'verified', no context bonus
        self.assertEqual(rows[0]['evidence'], 'context_lm')
        self.assertIn(rows[0]['evidence'], core.EVIDENCE_KINDS)
        self.assertEqual((rows[0]['ctx_hits'], rows[0]['sugg_local']),
                         (0, 0))
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


class CountNgramsWindow(unittest.TestCase):

    def test_bigram_and_trigram_at_the_end_of_the_list(self):
        c2, c3 = Counter(), Counter()
        n2, n3 = {('א', 'ב'), ('ב', 'ג')}, {('א', 'ב', 'ג')}
        realword.count_ngrams(['א', 'ב', 'ג'], n2, {'א', 'ב'}, n3, {'א'},
                              c2, c3)
        self.assertEqual(c2, Counter({('א', 'ב'): 1, ('ב', 'ג'): 1}))
        self.assertEqual(c3, Counter({('א', 'ב', 'ג'): 1}))

    def test_needs_of_a_lone_word_is_empty(self):
        self.assertEqual(realword.needs([]), (set(), set()))
        self.assertEqual(
            realword.needs([(0, 0, 0, 0, '', 'ספד', '', ['ספר'])]),
            (set(), set()))

    def test_every_trigram_starts_with_a_wanted_bigram(self):
        n2, n3 = realword.needs([(0, 0, 0, 0, 'ראה', 'ספד', 'גדול',
                                  ['ספר'])])
        self.assertTrue(n3)
        self.assertTrue(all((a, b) in n2 for a, b, _ in n3))


class Tie(unittest.TestCase):
    """The book's convention needs MORE uses than the suggestion (as the
    docs say): an equal count is a repeated slip, still a hint."""

    def test_equal_counts_are_a_hint(self):
        W, S, P, N = 'ספד', 'ספר', 'ראה', 'גדול'
        lex = core.Lexicon({W: 1000, S: 1000, P: 1000, N: 1000}, small_cfg())
        cfg = small_cfg(real_word_odds=2.0)
        c = realword.Counts({(P, S): 500, (S, N): 500}, {(P, S, N): 500},
                            {(P, W), (P, S), (W, N), (S, N)},
                            {(P, W, N), (P, S, N)})
        judge = realword.judge
        self.assertEqual(judge(lex, c, P, W, N, [S], {W: 5, S: 5}, cfg)
                         ['state'], 'hint')
        self.assertEqual(judge(lex, c, P, W, N, [S], {W: 5, S: 4}, cfg)
                         ['state'], 'convention')


class OwnFrequency(unittest.TestCase):
    """The unigram frequencies leave the book out, like the n-gram counts."""

    def test_the_books_own_occurrences_are_subtracted(self):
        W, S = 'ספד', 'ספר'
        lex = core.Lexicon({W: 1000, S: 1000}, small_cfg())
        c = realword.Counts({}, {}, set(), set())
        _, plain = realword.posterior(lex, c, '', W, '', [S])
        _, mine = realword.posterior(lex, c, '', W, '', [S], {W: 1000})
        self.assertEqual(mine[1], plain[1])
        self.assertAlmostEqual(mine[0], math.log(0.5 / lex.N))
        self.assertLess(mine[0], plain[0])

    def test_never_below_zero(self):
        lex = core.Lexicon({'ספד': 3, 'ספר': 1000}, small_cfg())
        c = realword.Counts({}, {}, set(), set())
        _, sc = realword.posterior(lex, c, '', 'ספד', '', ['ספר'],
                                   {'ספד': 10})
        self.assertAlmostEqual(sc[0], math.log(0.5 / lex.N))


class _Book:
    def __init__(self, lines):
        self.lines = [(f'u{i}', f'r{i}', t) for i, t in enumerate(lines)]


class Judged(unittest.TestCase):
    """Which occurrences are judged at all."""

    def setUp(self):
        self.lex = core.Lexicon({'ספד': 50, 'ספר': 50, 'ראה': 50, 'גדול': 50,
                                 'משה': 50, 'רבי': 50}, small_cfg())

    def words(self, lines, covered=frozenset()):
        return [(u, w) for u, _, _, _, _, w, _, _ in realword.judged(
            _Book(lines), self.lex, {}, core._editorial_adjacent,
            core.is_foreign_line, covered, core.NAME_TRIGGERS)
            if w == 'ספד']

    def test_name_position_after_a_title_is_skipped(self):
        self.assertEqual(self.words(['ראה ספד גדול']), [('u0', 'ספד')])
        self.assertEqual(self.words(['רבי ספד גדול']), [])
        self.assertEqual(self.words(['רבי משה ספד גדול']), [])
        self.assertEqual(self.words(['רבי משה ראה ספד']), [('u0', 'ספד')])

    def test_covered_tokens_are_skipped_on_their_line_only(self):
        lines = ['ראה ספד גדול', 'ראה ספד גדול']
        self.assertEqual(self.words(lines, {('u0', 'ספד')}),
                         [('u1', 'ספד')])

    def test_covered_from_joins_and_ocr_rows(self):
        joins = [('u0', 'r0', 'בד', 'ק', 'בדק', 100, 'snip')]
        ocr = [('u1', 'r1', 'ספד', 7, 'ספר', 100, 'snip')]
        self.assertEqual(book_scan._covered(joins, ocr),
                         {('u0', 'בד'), ('u0', 'ק'), ('u1', 'ספד')})


class CorruptedCounts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_rwc_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj):
        p = realword.counts_path(self.tmp, 'D')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            pickle.dump(obj, f)

    def test_missing_key_is_unreadable(self):
        self.write({'format': realword.FORMAT, 'doc': 'D',
                    'lexicon': realword.lexicon_stamp(self.tmp), 'c2': {}})
        self.assertEqual(realword.load_counts(self.tmp, 'D'),
                         (None, 'unreadable'))

    def test_wrong_types_are_unreadable(self):
        self.write({'format': realword.FORMAT, 'doc': 'D',
                    'lexicon': realword.lexicon_stamp(self.tmp), 'c2': 5,
                    'c3': {}, 'need2': set(), 'need3': set()})
        self.assertEqual(realword.load_counts(self.tmp, 'D'),
                         (None, 'unreadable'))


class Settings(unittest.TestCase):

    def test_check_config(self):
        self.assertIsNone(realword.check_config(Config()))
        for odds in (float('nan'), float('inf'), float('-inf')):
            self.assertEqual(
                realword.check_config(Config(real_word_odds=odds)),
                'real_word_odds')
        self.assertEqual(realword.check_config(Config(real_word_rep=0)),
                         'real_word_rep')

    def test_ui_refuses_invalid_values(self):
        tmp = tempfile.mkdtemp(prefix='magiah_rwcfg_')
        try:
            for key, val in (('real_word_odds', 'nan'),
                             ('real_word_odds', 'inf'),
                             ('real_word_rep', '0')):
                with self.assertRaises(ValueError) as cm:
                    scanner._merge_config(tmp, {key: val})
                self.assertIn(key, str(cm.exception))
            cfg = scanner._merge_config(tmp, {'real_word_odds': '4.5',
                                              'real_word_rep': '2'})
            self.assertEqual((cfg.real_word_odds, cfg.real_word_rep),
                             (4.5, 2))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class VerifyContextShape(unittest.TestCase):

    def test_four_items_whether_on_or_off(self):
        cfg = small_cfg()
        off = book_scan.verify_context(None, cfg, (), {})
        self.assertEqual(len(off), 4)
        self.assertIsNone(off[3])
        on = book_scan.verify_context(None, cfg, (), {},
                                      rw_need=(set(), set(), 'D'))
        self.assertEqual(len(on), 4)
        self.assertEqual(on[3], (Counter(), Counter()))


class Overlaps(unittest.TestCase):
    """A token another rule already reports on that line is not judged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_rwo_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        self.spec = {'type': 'library', 'path': self.lib}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, key, **kw):
        cfg = small_cfg(**{'real_word': 1, 'real_word_odds': -1e9, **kw})
        return book_scan.scan_book(self.out, 'library', key, cfg=cfg,
                                   library_dir=self.lib, verify_ctx=True,
                                   spec=self.spec)

    def test_extra_space_join(self):
        # 'בד' (freq 3) + 'ק' join to the frequent 'בדק'; 'כד' is a common
        # confusable neighbour of 'בד'
        write_lib(self.lib, {'S/a.txt': ['הוא בד ק את'],
                             'S/b.txt': ['הוא בד שם', 'הוא בד שם'],
                             'S/c.txt': ['הוא בדק את'] * 100
                             + ['הוא כד ק את'] * 40})
        core.build_lexicon(self.spec, small_cfg(), self.out)
        res = self.scan('S/a.txt')
        self.assertIn(('בד', 'ק'), [(r['part1'], r['part2'])
                                    for r in res['space_errors']])
        self.assertNotIn('בד', [r['word'] for r in res['findings']
                                if r['errtype'] == realword.ERRTYPE])

    def test_ocr_profile_hit(self):
        write_lib(self.lib, {BOOK_A: A_LINES, **OTHER})
        core.build_lexicon(self.spec, small_cfg(), self.out)
        doc = book_scan.load_book('library', BOOK_A,
                                  library_dir=self.lib).doc
        with mock.patch.object(book_scan.core, 'load_ocr_profiles',
                               return_value=({doc: ['דר']}, 'test')):
            res = self.scan(BOOK_A, ocr_rare_max=10, ocr_ratio=2)
        by = {(r['errtype'], r['word']) for r in res['findings']}
        self.assertIn(('ocr_profile', 'ספד'), by)
        self.assertNotIn((realword.ERRTYPE, 'ספד'), by)

    def test_without_the_profile_it_is_judged(self):
        write_lib(self.lib, {BOOK_A: A_LINES, **OTHER})
        core.build_lexicon(self.spec, small_cfg(), self.out)
        res = self.scan(BOOK_A, ocr_rare_max=10, ocr_ratio=2)
        by = {(r['errtype'], r['word']) for r in res['findings']}
        self.assertIn((realword.ERRTYPE, 'ספד'), by)


class InvalidSettingsInABookScan(unittest.TestCase):

    def test_refused(self):
        tmp = tempfile.mkdtemp(prefix='magiah_rwbad_')
        try:
            lib, out = os.path.join(tmp, 'lib'), os.path.join(tmp, 'out')
            os.makedirs(out)
            write_lib(lib, {BOOK_A: A_LINES, **OTHER})
            spec = {'type': 'library', 'path': lib}
            core.build_lexicon(spec, small_cfg(), out)
            with self.assertRaises(book_scan.BookScanError):
                book_scan.scan_book(
                    out, 'library', BOOK_A,
                    cfg=small_cfg(real_word=1, real_word_rep=0),
                    library_dir=lib, verify_ctx=False, spec=spec)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
