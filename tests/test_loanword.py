# -*- coding: utf-8 -*-
"""Greek/Latin loanword transliteration variants (cfg.loanword): rank-only
demotion — the finding and its suggestion stay exactly as without it."""
import unittest

from magiah import core
from magiah.config import Config

COMMON = {
    'אונקלוס': 100,     # Onkelos
    'טופסין': 100,      # tofsin
    'פלטריון': 100,     # -ion
    'ספטוס': 100,       # short word with a loanword ending
    'אנדרוס': 100,      # long -os word, look-alike (ד/ר) swap
    'מתקבלים': 100,     # long word, no loanword ending
}
RARE = {
    'אונגלוס': 1,       # ג for ק
    'טיפסין': 1,        # י for ו, but -in is mostly Hebrew/Aramaic plural
    'פלתריון': 1,       # ת for ט
    'ספתוס': 1,         # ת for ט, but only 5 letters
    'אנררוס': 1,        # ר for ד: an OCR pair, not a transliteration
    'מטקבלים': 1,       # ט for ת without a loanword ending
}


def evaluate(w, **kw):
    cfg = Config(rare_max=2, common_min=3, ed1_ratio=3, part_min=1000,
                 **kw)
    lex = core.Lexicon({**COMMON, **RARE}, cfg)
    recs, _ = core.evaluate_word(w, RARE[w], lex, core.edit1_probe(lex))
    return {r[0]: r for r in recs}


class Loanword(unittest.TestCase):

    def test_off_by_default(self):
        self.assertEqual(Config().loanword, 0)
        self.assertEqual(evaluate('אונגלוס'),
                         evaluate('אונגלוס', loanword=0))

    def test_demoted_in_score_only(self):
        for w, sugg in (('אונגלוס', 'אונקלוס'), ('פלתריון', 'פלטריון')):
            off = evaluate(w)['edit1_sub']
            on = evaluate(w, loanword=1)['edit1_sub']
            self.assertEqual(off[1], sugg)
            self.assertEqual(on[:3], off[:3])          # same suggestion
            self.assertAlmostEqual(off[3] - on[3], 1.5)

    def test_never_dropped(self):
        on = evaluate('אונגלוס', loanword=1, loanword_penalty=100.0)
        self.assertEqual(on['edit1_sub'][1], 'אונקלוס')

    def test_short_words_untouched(self):
        self.assertEqual(evaluate('ספתוס', loanword=1),
                         evaluate('ספתוס'))
        self.assertLess(evaluate('ספתוס', loanword=1, loanword_min_len=5)
                        ['edit1_sub'][3], evaluate('ספתוס')['edit1_sub'][3])

    def test_other_pairs_and_shapes_untouched(self):
        for w in ('אנררוס', 'מטקבלים', 'טיפסין'):
            self.assertEqual(evaluate(w, loanword=1), evaluate(w), w)
            self.assertIn('edit1_sub', evaluate(w))

    def test_penalty_rule(self):
        cfg = Config(loanword=1)
        pen = core.loanword_penalty
        self.assertEqual(pen('אונגלוס', 'sub', 'ג', 'ק', cfg), 1.5)
        self.assertEqual(pen('אונגלוס', 'sub', 'ק', 'ג', cfg), 1.5)
        self.assertEqual(pen('אונגלוס', 'swap', 'ג', 'ק', cfg), 0.0)
        self.assertEqual(pen('אונגלוס', 'sub', 'ד', 'ר', cfg), 0.0)
        for a, b in core.LOANWORD_PAIRS:
            self.assertIn((b, a), core.LOANWORD_PAIRS)


if __name__ == '__main__':
    unittest.main()
