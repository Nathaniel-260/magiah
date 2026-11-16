# -*- coding: utf-8 -*-
"""Abbreviations typed with a geresh for gershayim or the reverse
(cfg.swapped_quotes): on by default; 0 turns it off, and then nothing
changes."""
import tempfile
import unittest

from magiah import book_scan, cli, core
from magiah.config import Config
from magiah.normalize import tokenize

COMMON = {
    'כגב"ד': 400,       # gershayim abbreviation
    'פ\'לנטר': 400,     # geresh inside a transliterated word
    'שד"ק': 300,        # both forms frequent: two real abbreviations
    'שד\'ק': 300,
    'זכ"ר': 40,         # frequent, but not ed1_ratio times the rare form
    'מוטגל': 500,       # plain words
    'בלסר': 500,
}
RARE = {
    'כגב\'ד': 1,        # geresh for gershayim
    'פ"לנטר': 1,        # gershayim for geresh
    'זכ\'ר': 2,
    'חפ\'ט': 1,         # no swapped form at all
}


def evaluate(w, freq=None, **kw):
    cfg = Config(rare_max=2, common_min=30, ed1_ratio=50, part_min=1000,
                 **kw)
    freq = {**COMMON, **RARE} if freq is None else freq
    lex = core.Lexicon(freq, cfg)
    recs, _ = core.evaluate_word(w, freq.get(w, 0), lex,
                                 core.edit1_probe(lex))
    return {r[0]: r for r in recs}


class SwappedQuotes(unittest.TestCase):

    def test_off_when_disabled(self):
        for w in list(RARE) + list(COMMON):
            self.assertEqual(evaluate(w, swapped_quotes=0), {}, w)

    def test_on_by_default(self):
        self.assertEqual(Config().swapped_quotes, 1)
        self.assertEqual(evaluate('כגב\'ד')['swapped_quotes'][1], 'כגב"ד')
        self.assertEqual(evaluate('כגב\'ד'),
                         evaluate('כגב\'ד', swapped_quotes=1))

    def test_cli_default_and_disable(self):
        # --swapped-quotes is unset by default (None keeps the Config
        # default, on); an explicit 0 turns it off
        self.assertIn('swapped_quotes', cli.TUNABLE)
        self.assertEqual(Config.from_run_config({}).swapped_quotes, 1)
        self.assertEqual(
            Config.from_run_config({'swapped_quotes': 0}).swapped_quotes, 0)

    def test_edges(self):
        self.assertEqual(evaluate('', swapped_quotes=1), {})
        self.assertEqual(evaluate('\'', swapped_quotes=1), {})
        # nikud-bearing token: not in the lexicon form, no finding
        self.assertEqual(evaluate('כְגב\'ד', swapped_quotes=1), {})

    def test_other_outputs_unaffected(self):
        # lost_quotes (a rare no-mark sequence) is the same on or off
        freq = {**COMMON, **RARE, 'כגבד': 1}
        on = evaluate('כגבד', freq, swapped_quotes=1)
        off = evaluate('כגבד', freq, swapped_quotes=0)
        self.assertEqual(on, off)

    def test_geresh_for_gershayim(self):
        rec = evaluate('כגב\'ד', swapped_quotes=1)['swapped_quotes']
        self.assertEqual(rec[1:3], ('כגב"ד', 400))
        self.assertAlmostEqual(rec[3], 5 + 2.60206, places=4)

    def test_gershayim_for_geresh(self):
        rec = evaluate('פ"לנטר', swapped_quotes=1)['swapped_quotes']
        self.assertEqual(rec[1:3], ('פ\'לנטר', 400))

    def test_both_forms_frequent(self):
        for w in ('שד"ק', 'שד\'ק'):
            self.assertEqual(evaluate(w, swapped_quotes=1), {}, w)

    def test_ratio_and_missing_form(self):
        # 40 < ed1_ratio * 2; and no swapped form in the lexicon at all
        self.assertEqual(evaluate('זכ\'ר', swapped_quotes=1), {})
        self.assertEqual(evaluate('חפ\'ט', swapped_quotes=1), {})

    def test_unknown_book_word(self):
        # a book word missing from the lexicon (fw = 0) counts as 1
        freq = dict(COMMON)
        rec = evaluate('כגב\'ד', freq, swapped_quotes=1)['swapped_quotes']
        self.assertEqual(rec[1], 'כגב"ד')

    def test_plain_words_untouched(self):
        for w in ('מוטגל', 'בלסר'):
            self.assertEqual(evaluate(w, swapped_quotes=1), evaluate(w), w)

    def test_hebrew_marks_normalized(self):
        # ׳ and ״ arrive as ' and ", so the rule sees one form per mark
        self.assertEqual(tokenize('כגב׳ד פ״לנטר'),
                         ['כגב\'ד', 'פ"לנטר'])

    def test_registered_like_lost_quotes(self):
        for group in (core.LOCAL_TYPES, core.STRUCTURAL_TYPES):
            self.assertIn('swapped_quotes', group)
        self.assertEqual(core.evidence_kind('swapped_quotes', 'כגב"ד',
                                            400, 0, 0, 0), 'structural')
        self.assertIn('swapped_quotes', cli.TUNABLE)   # --swapped-quotes 1

    def test_book_scan(self):
        freq = {**COMMON, **RARE}
        book = _Book(['מוטגל כגב\'ד בלסר', 'בלסר פ״לנטר מוטגל'])
        with tempfile.TemporaryDirectory() as d:
            for on in (0, 1):
                cfg = Config(rare_max=2, common_min=30, ed1_ratio=50,
                             part_min=1000, swapped_quotes=on)
                _, errors, _, _, _ = book_scan.detect_in_book(book, freq,
                                                              cfg, d)
                got = {w: e[1:3] for w, e in errors.items()}
                occ, _, _ = book_scan._locate(book, freq, cfg, errors, None)
                if not on:
                    self.assertEqual((got, occ), ({}, []))
                    continue
                self.assertEqual(got, {
                    'כגב\'ד': ('swapped_quotes', 'כגב"ד'),
                    'פ"לנטר': ('swapped_quotes', 'פ\'לנטר')})
                self.assertEqual([(o[0], o[2]) for o in occ],
                                 [('u0', 'כגב\'ד'), ('u1', 'פ"לנטר')])


class _Book:
    def __init__(self, lines):
        self.lines = [(f'u{i}', f'r{i}', t) for i, t in enumerate(lines)]


if __name__ == '__main__':
    unittest.main()
