# -*- coding: utf-8 -*-
"""Ranked alternatives of an edit-1 finding (magiah.alternatives).

All words are invented letter strings."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from magiah import alternatives, book_scan, core, realword
from magiah.config import Config
from magiah.webui import db as uidb

from test_realword import small_cfg, write_lib

W = 'גלבת'                  # the rare word (twice in the scanned book)
X, Y, Z = 'גלכת', 'נלבת', 'גלבה'   # its edit-1 neighbours: ב/כ, ג/נ, ת/ה
P, N = 'פזקו', 'שטעם'       # neighbours Y is seen between
Q, M = 'רצפו', 'עדקי'       # neighbours none of them is seen beside

BOOK_A = 'Src/a.txt'
A_LINES = [f'{P} {W} {N}', f'{Q} {W} {M}']
OTHER = {'Src/b.txt': [f'{P} {Y} {N}'] * 40,
         'Src/c.txt': [X] * 100 + [Y] * 20 + [Z] * 12 + [f'{Q} {M}'] * 5}


def lexicon():
    freq = {W: 2, X: 100, Y: 60, Z: 12, P: 40, N: 40, Q: 5, M: 5}
    return core.Lexicon(freq, small_cfg())


def counts(c2=None, c3=None, words=(W, X, Y, Z), prev=P, nxt=N):
    need2 = {(prev, x) for x in words} | {(x, nxt) for x in words}
    need3 = {(prev, x, nxt) for x in words}
    return realword.Counts(c2 or {}, c3 or {}, need2, need3)


class Candidates(unittest.TestCase):

    def setUp(self):
        self.lex = lexicon()
        self.cands = alternatives.candidates(W, 2, self.lex,
                                             core.edit1_probe(self.lex))

    def test_the_detectors_candidates_in_its_order(self):
        self.assertEqual([c[0] for c in self.cands], [X, Y, Z])
        # the first is the finding's suggestion
        recs, _ = core.evaluate_word(W, 2, self.lex,
                                     core.edit1_probe(self.lex))
        self.assertEqual(recs[0][:2], ('edit1_sub', X))

    def test_edit1_record_is_the_detectors_record(self):
        recs, _ = core.evaluate_word(W, 2, self.lex,
                                     core.edit1_probe(self.lex))
        self.assertEqual(core.edit1_record(W, self.cands[0], self.lex),
                         recs[0])

    def test_only_edit1_findings(self):
        for t in ('edit1_sub', 'edit1_ins', 'edit1_del', 'edit1_swap',
                  'spelling_variant'):
            self.assertTrue(alternatives.eligible(t))
        for t in ('missing_space', 'nonfinal_end', 'final_midword',
                  'lost_quotes', 'ocr_profile', 'real_word'):
            self.assertFalse(alternatives.eligible(t))


class Rank(unittest.TestCase):

    def setUp(self):
        self.lex = lexicon()
        self.cands = alternatives.candidates(W, 2, self.lex,
                                             core.edit1_probe(self.lex))

    def rank(self, c, prev=P, nxt=N, show=3):
        return alternatives.rank(self.lex, c, prev, W, nxt, self.cands,
                                 show)

    def words(self, alts):
        return [a['suggestion'] for a in alts]

    def test_the_context_overturns_the_frequency_order(self):
        c = counts({(P, Y): 40, (Y, N): 40}, {(P, Y, N): 40})
        alts = self.rank(c)
        self.assertEqual(self.words(alts), [Y, X, Z])
        self.assertEqual({a['by'] for a in alts}, {'context'})
        self.assertEqual([a['ctx_hits'] for a in alts], [80, 0, 0])

    def test_a_weak_preference_keeps_the_detectors_first_choice(self):
        c = counts({(P, Y): 40, (Y, N): 40}, {(P, Y, N): 40})
        with mock.patch.object(alternatives, 'MARGIN', 1e9):
            alts = self.rank(c)
        # first place stays the detector's; the rest follow the context
        self.assertEqual(self.words(alts), [X, Y, Z])
        self.assertEqual(alts[0]['by'], 'context')
        self.assertIsNone(alternatives.reranked(W, self.cands, alts,
                                                self.lex))

    def test_bigrams_without_a_trigram_rank_too(self):
        with mock.patch.object(alternatives, 'MARGIN', 0.0):
            alts = self.rank(counts({(P, Z): 10, (Z, N): 10}))
        self.assertEqual(self.words(alts)[0], Z)

    def test_no_counts_keeps_the_detectors_order(self):
        alts = self.rank(None)
        self.assertEqual(self.words(alts), [X, Y, Z])
        self.assertEqual({a['by'] for a in alts}, {'frequency'})
        self.assertEqual({a['ctx_hits'] for a in alts}, {None})

    def test_no_neighbour_keeps_the_detectors_order(self):
        c = counts({(P, Y): 40, (Y, N): 40})
        alts = self.rank(c, prev='', nxt='')
        self.assertEqual(self.words(alts), [X, Y, Z])
        self.assertEqual(alts[0]['by'], 'frequency')

    def test_unseen_neighbours_keep_the_detectors_order(self):
        alts = self.rank(counts())
        self.assertEqual(self.words(alts), [X, Y, Z])
        self.assertEqual(alts[0]['by'], 'frequency')
        self.assertEqual([a['ctx_hits'] for a in alts], [0, 0, 0])

    def test_uncounted_ngrams_keep_the_detectors_order(self):
        c = counts({(P, Y): 10, (Y, N): 10}, words=(W, Y))
        self.assertEqual(self.words(self.rank(c)), [X, Y, Z])

    def test_at_most_show_distinct_never_the_word_itself(self):
        c = counts({(P, Y): 40, (Y, N): 40})
        for show in (0, 1, 2, 3, 5):
            got = self.words(self.rank(c, show=show))
            self.assertEqual(len(got), min(show, 3))
            self.assertEqual(len(set(got)), len(got))
            self.assertNotIn(W, got)

    def test_rerank(self):
        c = counts({(P, Y): 40, (Y, N): 40}, {(P, Y, N): 40})
        new = alternatives.reranked(W, self.cands, self.rank(c), self.lex)
        self.assertEqual(new[:3], ('edit1_sub', Y, 60))
        # the detector's own first choice, or a frequency order: no change
        self.assertIsNone(alternatives.reranked(W, self.cands,
                                                self.rank(None), self.lex))
        self.assertIsNone(alternatives.reranked(W, self.cands, [],
                                                self.lex))


class BookScan(unittest.TestCase):
    """The alternatives inside a single-book scan over a small library."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_alt_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        write_lib(self.lib, {BOOK_A: A_LINES, **OTHER})
        self.spec = {'type': 'library', 'path': self.lib}
        core.build_lexicon(self.spec, small_cfg(), self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, verify_ctx=True, **kw):
        res = book_scan.scan_book(self.out, 'library', BOOK_A,
                                  cfg=small_cfg(**kw), library_dir=self.lib,
                                  verify_ctx=verify_ctx, spec=self.spec)
        res.pop('seconds')
        return res

    def rows(self, res):
        return [r for r in res['findings'] if r['word'] == W]

    def test_off_by_default(self):
        self.assertEqual((Config().alternatives, Config().alt_rerank),
                         (0, 0))
        for r in self.scan()['findings']:
            self.assertNotIn('alternatives', r)

    def test_on_only_adds_the_field(self):
        off = self.scan()
        on = self.scan(alternatives=3)
        for r in on['findings']:
            r.pop('alternatives', None)
        self.assertEqual(on, off)

    def test_corpus_scope_ranks_by_the_neighbours(self):
        a, b = self.rows(self.scan(alternatives=3))
        # the suggestion itself does not change
        self.assertEqual((a['suggestion'], b['suggestion']), (X, X))
        self.assertEqual([x['suggestion'] for x in a['alternatives']],
                         [Y, X, Z])
        self.assertEqual(a['alternatives'][0]['by'], 'context')
        self.assertEqual(a['alternatives'][0]['ctx_hits'], 80)
        # the second line's neighbours were never seen beside them
        self.assertEqual([x['suggestion'] for x in b['alternatives']],
                         [X, Y, Z])
        self.assertEqual(b['alternatives'][0]['by'], 'frequency')

    def test_show(self):
        a, _ = self.rows(self.scan(alternatives=2))
        self.assertEqual([x['suggestion'] for x in a['alternatives']],
                         [Y, X])

    def test_the_book_itself_is_not_counted(self):
        # the scanned book itself puts Z between P and N, more often than
        # the rest of the library puts Y there: it must not vouch for it
        write_lib(self.lib, {BOOK_A: A_LINES + [f'{P} {Z} {N}'] * 80})
        core.build_lexicon(self.spec, small_cfg(), self.out)
        a, _ = self.rows(self.scan(alternatives=3))
        self.assertEqual(a['alternatives'][0]['suggestion'], Y)
        self.assertEqual(a['alternatives'][1]['ctx_hits'], 0)

    def test_book_scope_keeps_the_detectors_order(self):
        a, _ = self.rows(self.scan(verify_ctx=False, alternatives=3))
        self.assertEqual([x['suggestion'] for x in a['alternatives']],
                         [X, Y, Z])
        self.assertEqual(a['alternatives'][0]['by'], 'frequency')
        self.assertIsNone(a['alternatives'][0]['ctx_hits'])

    def test_rerank_changes_only_where_the_context_speaks(self):
        a, b = self.rows(self.scan(alternatives=3, alt_rerank=1))
        self.assertEqual((a['suggestion'], b['suggestion']), (Y, X))
        # verified like any other suggestion
        self.assertEqual(a['ctx_hits'], 80)
        self.assertEqual(a['evidence'], 'context')
        # off without the alternatives
        a, _ = self.rows(self.scan(alt_rerank=1))
        self.assertEqual(a['suggestion'], X)

    def test_the_review_db_keeps_them_in_extra(self):
        uidb.merge_book_scan(self.out, self.scan(alternatives=3))
        con = uidb.connect(self.out)
        try:
            ex = {r[0]: json.loads(r[1]) for r in con.execute(
                'SELECT unit, extra FROM findings WHERE word = ?', (W,))}
        finally:
            con.close()
        self.assertEqual(len(ex), 2)
        for e in ex.values():
            self.assertTrue(e['book_scan'])
            self.assertEqual(len(e['alternatives']), 3)

    def test_the_review_db_is_unchanged_when_off(self):
        uidb.merge_book_scan(self.out, self.scan())
        con = uidb.connect(self.out)
        try:
            extras = {r[0] for r in con.execute('SELECT extra FROM findings')}
        finally:
            con.close()
        self.assertEqual(extras, {json.dumps(
            {'book_scan': True, 'ctx_scope': 'corpus'})})


if __name__ == '__main__':
    unittest.main()
