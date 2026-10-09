# -*- coding: utf-8 -*-
"""Regression tests for the fixer follow-ups: one class per audited item.

Every fixture is built under a temp dir; nothing here touches a real
library. The shared fixtures come from test_anchor_write.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from magiah import normalize                                    # noqa: E402
from magiah.webui import patcher                                # noqa: E402
from test_anchor_write import TempCase, finding                 # noqa: E402


# ---------------------------------------------------------------------------
# the mapped tokenizer
# ---------------------------------------------------------------------------

class TestControlCharactersInALine(TempCase):
    """NUL, U+0001 and U+0002 used to be the mapper's own markers: a line
    holding one was mapped differently from clean(), so its findings were
    refused (or anchored by a map that was not clean's)."""

    LINES = ('אמר\x00 רבי יותבת בן זומא',
             '\x01אמר רבי יותבת\x02 בן <b>זומא</b>',
             'אמר רבי\x00<b>\x01</b> יותבת &amp;\x02 בן זומא')

    def test_the_map_is_cleans_own(self):
        for line in self.LINES:
            with self.subTest(line=repr(line)):
                got, omap, emap = normalize.clean_mapped(line)
                self.assertEqual(got, normalize.clean(line))
                self.assertEqual(len(omap), len(got))
                for tok, a, b in normalize.token_spans(line):
                    self.assertEqual(normalize.clean(line[a:b]).strip(), tok)

    def test_a_finding_on_such_a_line_is_written_exactly(self):
        for line in self.LINES:
            with self.subTest(line=repr(line)):
                d = self.doc(line + '\n')
                plan = patcher.plan_edit(d, finding(line, 'יותבת', 'יושבת'))
                self.assertEqual(plan.confidence, 'exact')
                patcher.apply_edits(d, [plan])
                self.assertEqual(d.encode(), (line.replace(
                    'יותבת', 'יושבת') + '\n').encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
