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
# the mapped tokenizer and per-line work
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


class TestPerLineWorkIsShared(TempCase):
    """Anchoring cost (findings on a line) x (line length): 50 findings on
    one 200 KB line took 22-26 s. Each line is now tokenized once."""

    def test_a_long_line_is_tokenized_once_for_all_its_findings(self):
        words = []
        typos = ['קדבנ' + chr(0x05D0 + i) for i in range(20)]
        for i in range(3000):
            words.append(typos[i // 150] if i % 150 == 75 else 'שלום')
        line = ' '.join(words)
        d = self.doc('פתיחה\n' + line + '\nסוף\n')
        rows = [dict(finding(line, t, 'קרבן', lineno=1, fid=i + 1),
                     occurrence=0, expected_count=1)
                for i, t in enumerate(typos)]
        calls = []
        real = normalize.token_spans_full

        def counting(text):
            calls.append(len(text))
            return real(text)
        normalize.token_spans_full = counting
        try:
            patcher.anchor_rows(d, rows)
        finally:
            normalize.token_spans_full = real
        self.assertEqual([r['anchor'].get('code') for r in rows
                          if not r['anchor']['ok']], [])
        self.assertEqual([r['anchor']['start'] for r in rows],
                         [line.index(t) for t in typos])
        self.assertEqual(calls.count(len(line)), 1, calls)

    def test_twins_are_still_found_past_an_entity_newline(self):
        """Nearby lines are searched as one joined text; an entity that
        decodes to a newline must not shift the line numbers found."""
        line = 'אמר רבי יותבת בן זומא'
        for first in ('פתיחה', 'א&#10;ב&#x0A;ג'):
            with self.subTest(first=first):
                d = self.doc('\n'.join((first, line, 'כותרת', line)) + '\n')
                with self.assertRaises(patcher.PatchError) as cm:
                    patcher.plan_edit(d, finding(line, 'יותבת', 'יושבת',
                                                 lineno=1))
                self.assertEqual(cm.exception.code, 'ambiguous_line')
                self.assertEqual(cm.exception.extra['candidate_lines'],
                                 [1, 3])

    def test_an_edited_line_is_not_answered_from_the_cache(self):
        line = 'אמר רבי יותבת בן זומא'
        d = self.doc(line + '\n')
        f = finding(line, 'יותבת', 'יושבת')
        plan = patcher.plan_edit(d, f)
        patcher.apply_edits(d, [plan])
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'token_not_found')


if __name__ == '__main__':
    unittest.main()
