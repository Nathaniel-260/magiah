# -*- coding: utf-8 -*-
"""Robustness of the ranked alternatives (magiah.alternatives):

* the detector's suggestion and the alternatives' first candidate are the
  same word on synthetic book scans (library and file books, words in the
  lexicon or not), and a mismatch could never be reported as a rerank;
* the MARGIN override, with n-gram counts on both sides of it.

All words are invented letter strings."""
import math
import os
import random
import shutil
import tempfile
import unittest
from unittest import mock

from magiah import alternatives, book_scan, core
from magiah.book_source import load_book

from test_alternatives import (BOOK_A, N, OTHER, P, W, X, Y, Z, A_LINES,
                               counts, lexicon)
from test_realword import small_cfg, write_lib

LN10 = math.log(10)
# ו/י, ד/ר, ב/כ are confusable pairs: plenty of edit-1 neighbours
ALPHA = 'אבוידרכ'


def gap(lex, c, cands):
    """How far (log10 odds) the best candidate is above the detector's
    first, by the same scores :func:`alternatives.rank` sorts on."""
    sc = alternatives.scores(lex, c, P, W, N, cands)
    return (max(sc) - sc[0]) / LN10, sc.index(max(sc))


class MarginWithCounts(unittest.TestCase):
    """The context-override threshold, with real n-gram counts."""

    def setUp(self):
        self.lex = lexicon()
        self.cands = alternatives.candidates(W, 2, self.lex,
                                             core.edit1_probe(self.lex))
        self.assertEqual([c[0] for c in self.cands], [X, Y, Z])

    def counts_for(self, k):
        return counts({(P, Y): k, (Y, N): k}, {(P, Y, N): k})

    def test_fixture_straddles_the_margin(self):
        # smallest k that clears the margin; one less must not
        k = next(k for k in range(1, 500)
                 if gap(self.lex, self.counts_for(k), self.cands)[0]
                 > alternatives.MARGIN)
        g_hi, who = gap(self.lex, self.counts_for(k), self.cands)
        g_lo, who_lo = gap(self.lex, self.counts_for(k - 1), self.cands)
        self.assertEqual((who, who_lo), (1, 1))     # Y is the context's pick
        self.assertGreater(g_hi, alternatives.MARGIN)
        self.assertGreater(g_lo, 0.0)
        self.assertLess(g_lo, alternatives.MARGIN)
        self.assertGreater(k - 1, 0)

    def ranked(self, k):
        return alternatives.rank(self.lex, self.counts_for(k), P, W, N,
                                 self.cands, 3)

    def test_above_the_margin_the_context_goes_first(self):
        k = next(k for k in range(1, 500)
                 if gap(self.lex, self.counts_for(k), self.cands)[0]
                 > alternatives.MARGIN)
        alts = self.ranked(k)
        self.assertEqual([a['suggestion'] for a in alts][0], Y)
        self.assertEqual(alts[0]['by'], 'context')
        new = alternatives.reranked(W, self.cands, alts, self.lex)
        self.assertEqual(new[:3], ('edit1_sub', Y, 60))

    def test_below_the_margin_the_detector_keeps_first_place(self):
        k = next(k for k in range(1, 500)
                 if gap(self.lex, self.counts_for(k), self.cands)[0]
                 > alternatives.MARGIN) - 1
        alts = self.ranked(k)
        words = [a['suggestion'] for a in alts]
        self.assertEqual(words[0], X)
        # the others still follow the context
        self.assertEqual(words[1], Y)
        self.assertEqual({a['by'] for a in alts}, {'context'})
        self.assertIsNone(alternatives.reranked(W, self.cands, alts,
                                                self.lex))


def make_library(k_ypn):
    """The usual library, Y seen between P and N `k_ypn` times (Y's own
    frequency kept constant)."""
    return {BOOK_A: A_LINES,
            'Src/b.txt': [f'{P} {Y} {N}'] * k_ypn + [Y] * (40 - k_ypn),
            'Src/c.txt': OTHER['Src/c.txt']}


class BookScanBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_altr_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        self.spec = {'type': 'library', 'path': self.lib}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, books, **kw):
        shutil.rmtree(self.lib, ignore_errors=True)
        write_lib(self.lib, books)
        core.build_lexicon(self.spec, small_cfg(**kw), self.out)

    def scan(self, key=BOOK_A, source='library', verify_ctx=True, **kw):
        res = book_scan.scan_book(self.out, source, key, cfg=small_cfg(**kw),
                                  library_dir=self.lib,
                                  verify_ctx=verify_ctx, spec=self.spec)
        res.pop('seconds')
        return res

    def first_book_rows(self, res):
        return [r for r in res['findings'] if r['word'] == W]


class MarginInABookScan(BookScanBase):
    """MARGIN end to end: counts come from the corpus pass."""

    # lines of ``P Y N`` in the other book: the context's preference for Y
    # over X (log10 odds) is just above MARGIN at K_HI and just under at
    # K_LO (asserted below, from the scores the scan itself used)
    K_LO, K_HI = 12, 30

    def run_k(self, k, **kw):
        self.build(make_library(k))
        seen = []
        orig = alternatives.scores

        def spy(*a, **k_):
            r = orig(*a, **k_)
            seen.append((max(r) - r[0]) / LN10)
            return r
        with mock.patch.object(alternatives, 'scores', spy):
            res = self.scan(alternatives=3, **kw)
        return self.first_book_rows(res), seen

    def test_both_sides_of_the_margin(self):
        for k, over in ((self.K_HI, True), (self.K_LO, False)):
            with self.subTest(k=k):
                rows, seen = self.run_k(k)
                a, b = rows
                self.assertTrue(seen)
                if over:
                    self.assertGreater(seen[0], alternatives.MARGIN)
                    # the alternatives' order follows the context
                    self.assertEqual(a['alternatives'][0]['suggestion'], Y)
                else:
                    self.assertLess(seen[0], alternatives.MARGIN)
                    self.assertGreater(seen[0], 0.0)
                    self.assertEqual(a['alternatives'][0]['suggestion'], X)
                    self.assertEqual(a['alternatives'][1]['suggestion'], Y)
                self.assertEqual(a['alternatives'][0]['by'], 'context')
                # plain mode: the suggestion never moves
                self.assertEqual((a['suggestion'], b['suggestion']), (X, X))

    def test_rerank_changes_the_suggestion_only_over_the_margin(self):
        rows, _ = self.run_k(self.K_HI, alt_rerank=1)
        self.assertEqual(rows[0]['suggestion'], Y)
        self.assertEqual(rows[0]['alternatives'][0]['suggestion'], Y)
        # second occurrence: its neighbours were never seen
        self.assertEqual(rows[1]['suggestion'], X)
        rows, _ = self.run_k(self.K_LO, alt_rerank=1)
        self.assertEqual(rows[0]['suggestion'], X)
        self.assertEqual(rows[0]['alternatives'][0]['suggestion'], X)


class Fuzz(BookScanBase):
    """Synthetic books, of every kind, against random libraries."""

    SEEDS = range(8)

    @staticmethod
    def words(rng, n):
        out = set()
        while len(out) < n:
            out.add(''.join(rng.choice(ALPHA)
                            for _ in range(rng.choice((3, 3, 4)))))
        return sorted(out)

    @staticmethod
    def lines(rng, vocab, n):
        wts = [1.0 / (i + 1) for i in range(len(vocab))]
        return [' '.join(rng.choices(vocab, wts, k=rng.randint(3, 7)))
                for _ in range(n)]

    def material(self, seed):
        rng = random.Random(seed)
        vocab = self.words(rng, 22)
        rng.shuffle(vocab)
        books = {f'Src/b{i}.txt': self.lines(rng, vocab, 120)
                 for i in range(3)}
        # a library book made of the same words (in the lexicon) ...
        books[BOOK_A] = self.lines(rng, vocab[:14] + self.words(rng, 6), 40)
        # ... and a file from outside the library (partly unknown words)
        outside = os.path.join(self.tmp, 'outside.txt')
        return books, outside, self.lines(rng, vocab[:8] + self.words(rng, 8),
                                          40)

    def check(self, off, plain, rer):
        # same findings in the same order, whatever is asked for
        def ident(res):
            return [(r['unit'], r['word'], r['errtype'])
                    for r in res['findings']]
        self.assertEqual(ident(off), ident(plain))
        self.assertEqual(ident(off), ident(rer))
        n_alt = n_change = 0
        for o, p, r in zip(off['findings'], plain['findings'],
                           rer['findings']):
            self.assertNotIn('alternatives', o)
            # alternatives alone never touch the finding
            q = dict(p)
            alts = q.pop('alternatives', None)
            self.assertEqual(q, o)
            if alts:
                n_alt += 1
                words = [a['suggestion'] for a in alts]
                self.assertEqual(len(set(words)), len(words))
                self.assertNotIn(o['word'], words)
                # the suggestion is on the list; where the neighbours did
                # not speak, it is the first
                self.assertIn(o['suggestion'], words)
                if alts[0]['by'] == 'frequency':
                    self.assertEqual(words[0], o['suggestion'])
            # rerank: a change is exactly "the context put another first"
            if r['suggestion'] != o['suggestion']:
                n_change += 1
                ra = r['alternatives']
                self.assertEqual(ra[0]['by'], 'context')
                self.assertEqual(ra[0]['suggestion'], r['suggestion'])
                self.assertEqual(alts[0]['suggestion'], r['suggestion'])
                self.assertTrue(alternatives.eligible(o['errtype']))
            elif alts:
                # no change: the list still opens with the suggestion
                self.assertEqual(r['alternatives'][0]['suggestion'],
                                 o['suggestion'])
        return n_alt, n_change

    def test_detector_and_alternatives_agree(self):
        total_alt = 0
        for seed in self.SEEDS:
            books, outside, out_lines = self.material(seed)
            self.build(books)
            write_lib(self.tmp, {'outside.txt': out_lines})
            for source, key in (('library', BOOK_A), ('file', outside)):
                with self.subTest(seed=seed, source=source):
                    off = self.scan(key, source)
                    plain = self.scan(key, source, alternatives=50)
                    rer = self.scan(key, source, alternatives=50,
                                    alt_rerank=1)
                    na, _ = self.check(off, plain, rer)
                    total_alt += na
                    self.assertTrue(self.detector_agrees(key, source))
        # the fuzz must have something to judge
        self.assertGreater(total_alt, 0)

    def detector_agrees(self, key, source):
        """For every eligible finding of the book, the detector's
        suggestion is the first of the alternatives' candidates."""
        cfg = small_cfg()
        book = load_book(source, key, library_dir=self.lib)
        freq = book_scan.load_lexicon(self.out, 0)
        lex, errors, _, _, _ = book_scan.detect_in_book(book, freq, cfg,
                                                        self.out)
        get = book_scan._alt_candidates(lex, errors, freq)
        for w, (_, errtype, sugg, _, _) in errors.items():
            if alternatives.eligible(errtype):
                cs = get(w)
                if not cs or cs[0][0] != sugg:
                    return False
        return True


class MismatchIsNeverARerank(BookScanBase):
    """If the candidates ever disagreed with the detector's suggestion, the
    scan must not call the difference a context rerank."""

    def setUp(self):
        super().setUp()
        self.build(make_library(40))
        self.off = self.scan()
        self.plain = self.scan(alternatives=3)
        self.real = alternatives.candidates

    def scan_with(self, fake, **kw):
        with mock.patch.object(alternatives, 'candidates', fake):
            return self.scan(alternatives=3, **kw)

    def test_a_shuffled_candidate_order_changes_nothing(self):
        def rev(*a):
            return list(reversed(self.real(*a)))
        res = self.scan_with(rev)
        for o, p, r in zip(self.off['findings'], self.plain['findings'],
                           res['findings']):
            self.assertEqual(r['suggestion'], o['suggestion'])
            if 'alternatives' in r:
                self.assertEqual(r['alternatives'][0]['suggestion'],
                                 p['alternatives'][0]['suggestion'])
        res = self.scan_with(rev, verify_ctx=False, alt_rerank=1)
        for o, r in zip(self.off['findings'], res['findings']):
            self.assertEqual(r['suggestion'], o['suggestion'])

    def test_candidates_without_the_suggestion_give_no_alternatives(self):
        def drop(*a):
            return [c for c in self.real(*a) if c[0] != X]
        res = self.scan_with(drop, alt_rerank=1)
        for o, r in zip(self.off['findings'], res['findings']):
            self.assertEqual(r['suggestion'], o['suggestion'])
            self.assertEqual(r['errtype'], o['errtype'])
            self.assertEqual(r['score'], o['score'])
            self.assertNotIn('alternatives', r)

    def test_anchor(self):
        lex = lexicon()
        cs = alternatives.candidates(W, 2, lex, core.edit1_probe(lex))
        self.assertEqual([c[0] for c in alternatives.anchor(cs, Z)],
                         [Z, X, Y])
        self.assertEqual(alternatives.anchor(cs, X), cs)
        self.assertEqual(alternatives.anchor(cs, 'zzz'), [])
        self.assertEqual(alternatives.anchor([], X), [])
        # nothing to rerank to / from
        alts = [{'suggestion': Y, 'by': 'context', 'ctx_hits': 1}]
        self.assertIsNone(alternatives.reranked(W, [], alts, lex))
        self.assertIsNone(alternatives.reranked(W, cs, [
            {'suggestion': 'zzz', 'by': 'context', 'ctx_hits': 1}], lex))


if __name__ == '__main__':
    unittest.main()
