# -*- coding: utf-8 -*-
"""Book domain gate (Config.book_domain_max): a book most of whose words the
lexicon has never seen loses its rarity-based findings, and the scan says
so; structural / OCR-profile / extra-space findings stay. Off by default,
and off means the result is exactly what it was."""
import os
import shutil
import tempfile
import unittest
from collections import Counter
from types import SimpleNamespace

from magiah import book_scan
from magiah.config import Config
from magiah.webui import hebrew

from test_detection import A_LINES, B_LINES, BOOK_A, LIB_TOP, PROFILES, \
    run_full, small_cfg, write_lib, write_profiles

LETTERS = 'זטצקפגש'


def made_up(n):
    """`n` different invented 4-letter words, none of them in the library."""
    out = []
    for a in LETTERS:
        for b in LETTERS:
            for c in LETTERS:
                out.append('ק' + a + b + c)
    return out[:n]


class Measure(unittest.TestCase):

    def cfg(self, **kw):
        return Config(**{'book_domain_max': 0.10,
                         'book_domain_min_words': 4, **kw})

    def test_share_counts_long_words_only(self):
        present = Counter({'ספר': 6, 'קזזז': 2, 'אב': 50, 'רמב"ם': 50})
        d = book_scan.book_domain(SimpleNamespace(kind='file'),
                                  {'ספר': 100}, present, self.cfg())
        self.assertEqual((d['words'], d['unknown']), (8, 2))
        self.assertEqual(d['share'], 0.25)
        self.assertTrue(d['out'])

    def test_own_occurrences_do_not_count_for_a_corpus_book(self):
        # the lexicon counted this book: its 3 uses of the word are its own
        freq = {'ספר': 100, 'קזזז': 3}
        present = Counter({'ספר': 9, 'קזזז': 3})
        lib = book_scan.book_domain(SimpleNamespace(kind='library'), freq,
                                    present, self.cfg())
        self.assertEqual(lib['unknown'], 3)
        # a file from outside the corpus: the lexicon's count is all others'
        f = book_scan.book_domain(SimpleNamespace(kind='file'), freq,
                                  present, self.cfg())
        self.assertEqual(f['unknown'], 0)

    def test_threshold_is_strict(self):
        present = Counter({'ספר': 9, 'קזזז': 1})
        d = book_scan.book_domain(SimpleNamespace(kind='file'),
                                  {'ספר': 100}, present, self.cfg())
        self.assertEqual(d['share'], 0.1)
        self.assertFalse(d['out'])

    def test_short_book_is_not_judged(self):
        present = Counter({'קזזז': 3})
        d = book_scan.book_domain(SimpleNamespace(kind='file'), {}, present,
                                  self.cfg())
        self.assertEqual(d['share'], 1.0)
        self.assertFalse(d['out'])
        self.assertIn('מעט מדי מילים',
                      book_scan.book_domain_message('x', dict(d, left_out={})))

    def test_switch(self):
        self.assertFalse(book_scan.domain_on(Config()))
        self.assertFalse(book_scan.domain_on(Config(book_domain_max=-1)))
        self.assertFalse(book_scan.domain_on(Config(book_domain_max=None)))
        self.assertFalse(book_scan.domain_on(
            Config(book_domain_max=float('nan'))))
        self.assertTrue(book_scan.domain_on(Config(book_domain_max=0.1)))

    def test_labels(self):
        for key in ('book_domain_max', 'book_domain_min_words'):
            self.assertIn(key, hebrew.CONFIG_LABELS)


class Scan(unittest.TestCase):
    """A small library; outside .txt files are scanned against it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='magiah_domain_')
        cls.lib = os.path.join(cls.tmp, 'lib')
        cls.out = os.path.join(cls.tmp, 'out')
        os.makedirs(cls.out)
        write_lib(cls.lib, {BOOK_A: A_LINES, f'{LIB_TOP}/b.txt': B_LINES})
        cls.spec = {'type': 'library', 'path': cls.lib}
        write_profiles(cls.out, PROFILES)
        run_full(cls.spec, small_cfg(), cls.out)
        # the fixture's slips amid ordinary text: in the lexicon's domain
        cls.inside = os.path.join(cls.tmp, 'inside.txt')
        write_lib(cls.tmp, {'inside.txt': B_LINES * 2 + A_LINES})
        # ... and with many words the lexicon never saw
        words = made_up(60)
        cls.outside = os.path.join(cls.tmp, 'outside.txt')
        write_lib(cls.tmp, {'outside.txt': A_LINES + [
            ' '.join(words[i:i + 6]) for i in range(0, len(words), 6)]})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def scan(self, path, log=None, **kw):
        cfg = small_cfg(**{'book_domain_min_words': 10, **kw})
        res = book_scan.scan_book(self.out, 'file', path, cfg=cfg,
                                  progress=(log.append if log is not None
                                            else None))
        res.pop('seconds')
        return res

    @staticmethod
    def types(res):
        return Counter(r['errtype'] for r in res['findings'])

    def test_off_by_default_result_unchanged(self):
        self.assertEqual(Config().book_domain_max, 0)
        res = self.scan(self.outside)
        self.assertNotIn('domain', res)
        self.assertEqual(res, self.scan(self.outside, book_domain_max=0))

    def test_inside_domain_nothing_changes(self):
        off = self.scan(self.inside)
        log = []
        on = self.scan(self.inside, log, book_domain_max=0.10)
        dom = on.pop('domain')
        self.assertFalse(dom['out'])
        self.assertEqual(dom['left_out'], {})
        self.assertEqual(on, off)
        self.assertTrue(any('בתוך תחום המילון' in s for s in log))

    def test_outside_domain_rarity_findings_left_out(self):
        off = self.scan(self.outside)
        log = []
        on = self.scan(self.outside, log, book_domain_max=0.10)
        dom = on['domain']
        self.assertTrue(dom['out'])
        self.assertGreater(dom['share'], 0.10)
        before, after = self.types(off), self.types(on)
        # the fixture's book has rarity-based findings to lose ...
        self.assertTrue(any(t in before for t in book_scan.DOMAIN_TYPES))
        for t in book_scan.DOMAIN_TYPES:
            self.assertNotIn(t, after)
            self.assertEqual(dom['left_out'].get(t, 0), before.get(t, 0))
        # ... and every other kind stays, row for row
        kept = [r for r in off['findings']
                if r['errtype'] not in book_scan.DOMAIN_TYPES]
        self.assertTrue(kept)
        self.assertIn('final_midword', after)
        self.assertEqual(on['findings'], kept)
        self.assertEqual(on['space_errors'], off['space_errors'])
        # the scan says so, by name
        self.assertTrue(any('מחוץ לתחום המילון' in s
                            and 'book_domain_max' in s for s in log))

    def test_minimum_words(self):
        on = self.scan(self.outside, book_domain_max=0.10,
                       book_domain_min_words=10 ** 6)
        self.assertFalse(on['domain']['out'])
        on.pop('domain')
        self.assertEqual(on, self.scan(self.outside))

    def test_higher_threshold_keeps_the_book(self):
        share = self.scan(self.outside,
                          book_domain_max=0.10)['domain']['share']
        on = self.scan(self.outside, book_domain_max=share + 0.001)
        self.assertFalse(on['domain']['out'])


if __name__ == '__main__':
    unittest.main()
