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
from unittest import mock

from magiah import book_scan
from magiah.config import Config
from magiah.webui import hebrew, scanner

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
                         'book_domain_min_words': 4,
                         'book_domain_min_types': 1, **kw})

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
        for key in ('book_domain_max', 'book_domain_min_words',
                    'book_domain_min_types', 'book_domain_min_lexicon'):
            self.assertIn(key, hebrew.CONFIG_LABELS)

    def test_minimum_is_distinct_words_too(self):
        # 500 occurrences of ONE unknown word: a short repetitive text, not
        # a sample of a language; the default minimums (200 occurrences,
        # 100 different words) do not judge it
        self.assertEqual((Config().book_domain_min_words,
                          Config().book_domain_min_types), (200, 100))
        present = Counter({'אאאא': 500})
        f = SimpleNamespace(kind='file')
        d = book_scan.book_domain(f, {}, present, self.cfg(
            book_domain_min_words=200, book_domain_min_types=100))
        self.assertEqual((d['words'], d['types'], d['share']), (500, 1, 1.0))
        self.assertFalse(d['out'])
        # ... while 120 different unknown words, twice each, are judged
        many = Counter({w: 2 for w in made_up(120)})
        d = book_scan.book_domain(f, {}, many, self.cfg(
            book_domain_min_words=200, book_domain_min_types=100))
        self.assertEqual((d['words'], d['types']), (240, 120))
        self.assertTrue(d['out'])
        # both minimums apply: enough different words, too few occurrences
        d = book_scan.book_domain(f, {}, Counter({w: 1 for w in made_up(120)}),
                                  self.cfg(book_domain_min_words=200,
                                           book_domain_min_types=100))
        self.assertFalse(d['out'])

    def test_own_count_is_the_only_this_book_rule(self):
        # for a corpus book a word is unknown (rare_max 0) exactly when the
        # lexicon has it no more often than the book does: the same
        # `freq <= present` test as book_repeat's only_this_book
        for kind in ('library', 'db'):
            for freq_w, n in ((5, 5), (6, 5), (4, 5), (1, 1), (2, 1)):
                d = book_scan.book_domain(
                    SimpleNamespace(kind=kind), {'אבגד': freq_w},
                    Counter({'אבגד': n}), self.cfg(rare_max=0))
                self.assertEqual(d['unknown'] == n, freq_w <= n,
                                 (kind, freq_w, n))

    def test_lexicon_problem(self):
        freq = {'אבג': 10}
        none = mock.patch.object(book_scan.core, 'accepted_gaps',
                                 return_value={})
        with none:
            self.assertEqual(book_scan.domain_lexicon_problem(
                freq, Config(), 'x'), 'small')
            self.assertEqual(book_scan.domain_lexicon_problem(
                freq, Config(book_domain_min_lexicon=11), 'x'), 'small')
            self.assertIsNone(book_scan.domain_lexicon_problem(
                freq, Config(book_domain_min_lexicon=10), 'x'))
            self.assertIsNone(book_scan.domain_lexicon_problem(
                {}, Config(book_domain_min_lexicon=0), 'x'))
        # built partial under --allow-unread: refused whatever its size
        part = mock.patch.object(book_scan.core, 'accepted_gaps',
                                 return_value={'lexicon': {'unread_rows': 1}})
        with part:
            self.assertEqual(book_scan.domain_lexicon_problem(
                freq, Config(book_domain_min_lexicon=0), 'x'), 'partial')

    def test_hebrew_strings_are_not_mojibake(self):
        # the bytes are UTF-8 Hebrew: no C1 controls, no geresh-mojibake
        bad = [chr(c) for c in range(0x80, 0xa0)] + ['\u05f3', '\u05b2',
                                                      '\u20ac']
        import magiah.book_scan as bs
        import magiah.config as cf
        for mod in (bs, cf, hebrew):
            with open(mod.__file__, 'rb') as f:
                text = f.read().decode('utf-8')
            self.assertFalse([c for c in bad if c in text], mod.__file__)
        for key in ('book_domain_max', 'book_domain_min_words',
                    'book_domain_min_types', 'book_domain_min_lexicon'):
            lab = hebrew.CONFIG_LABELS[key]
            for s in (lab['hebrew'], lab['explanation']):
                self.assertTrue(any('א' <= c <= 'ת' for c in s))
                self.assertFalse([c for c in bad if c in s])


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
        cfg = small_cfg(**{'book_domain_min_words': 10,
                           'book_domain_min_types': 5,
                           'book_domain_min_lexicon': 0, **kw})
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

    def test_split_alternatives_follow_the_gate(self):
        off = self.scan(self.outside)
        on = self.scan(self.outside, book_domain_max=0.10)
        self.assertTrue(on['domain']['out'])
        self.assertTrue(on['domain']['left_out'].get('missing_space'))
        self.assertTrue([a for a in off['split_alternatives']
                         if a['is_primary']])
        # the missing_space finding is gone: no primary alternative, and no
        # alternative of any word whose findings were all left out
        self.assertFalse([a for a in on['split_alternatives']
                          if a['is_primary']])
        shown = {r['word'] for r in on['findings']}
        gone = {r['word'] for r in off['findings']} - shown
        self.assertTrue(gone)
        self.assertFalse({a['word'] for a in on['split_alternatives']} & gone)
        # inside the domain nothing is filtered
        self.assertEqual(self.scan(self.inside)['split_alternatives'],
                         self.scan(self.inside,
                                   book_domain_max=0.10)['split_alternatives'])

    def test_small_lexicon_refuses_the_gate(self):
        off = self.scan(self.outside)
        log = []
        on = self.scan(self.outside, log, book_domain_max=0.10,
                       book_domain_min_lexicon=10 ** 9)
        dom = on.pop('domain')
        self.assertFalse(dom['out'])
        self.assertEqual(dom['refused'], 'small')
        self.assertEqual(dom['left_out'], {})
        self.assertGreater(dom['share'], 0.10)      # it would have gated
        self.assertEqual(on, off)
        self.assertTrue(any('book_domain_min_lexicon' in s for s in log))
        # the default floor refuses this tiny test library too
        floor = Config().book_domain_min_lexicon
        self.assertEqual(self.scan(
            self.outside, book_domain_max=0.10,
            book_domain_min_lexicon=floor)['domain']['refused'], 'small')

    def test_partial_lexicon_refuses_the_gate(self):
        off = self.scan(self.outside)
        log = []
        with mock.patch.object(book_scan, 'domain_lexicon_problem',
                               return_value='partial'):
            on = self.scan(self.outside, log, book_domain_max=0.10)
        self.assertEqual(on.pop('domain')['refused'], 'partial')
        self.assertEqual(on, off)
        self.assertTrue(any('--allow-unread' in s for s in log))

    def test_higher_threshold_keeps_the_book(self):
        share = self.scan(self.outside,
                          book_domain_max=0.10)['domain']['share']
        on = self.scan(self.outside, book_domain_max=share + 0.001)
        self.assertFalse(on['domain']['out'])


class Wiring(unittest.TestCase):
    """The settings the web UI shows and passes on."""

    def test_ui_schema_and_overrides(self):
        tmp = tempfile.mkdtemp(prefix='magiah_domain_ui_')
        self.addCleanup(shutil.rmtree, tmp, True)
        fields = {f['key']: f for f in scanner.scan_config(tmp)['fields']}
        for key, typ, default in (
                ('book_domain_max', 'float', 0.0),
                ('book_domain_min_words', 'int', 200),
                ('book_domain_min_types', 'int', 100),
                ('book_domain_min_lexicon', 'int', 1000000)):
            f = fields[key]
            self.assertEqual((f['type'], f['default']), (typ, default), key)
            self.assertNotEqual(f['hebrew'], key)
            self.assertTrue(f['explanation'])
        cfg = scanner._merge_config(tmp, {'book_domain_max': '0.05',
                                          'book_domain_min_types': '50'})
        self.assertEqual((cfg.book_domain_max, cfg.book_domain_min_types),
                         (0.05, 50))


if __name__ == '__main__':
    unittest.main()
