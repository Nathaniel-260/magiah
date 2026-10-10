# -*- coding: utf-8 -*-
"""Tests for מצב מתקן — writing corrections into the book's own .txt file.

Run:  python -X utf8 -m unittest discover -s tests -v

Nothing here touches the real library: every test builds its own book files
under tempfile.mkdtemp(). stdlib unittest only, to match the project's
no-dependency rule.

The test that matters most is
:meth:`TestNeverTouchesAnotherBook.test_only_the_targeted_file_changes`. In the
real corpus the filename stem 'פרק א' belongs to 35 different books, so a
worklist keyed on the book TITLE would scatter one book's corrections across
34 others. That test pins the behaviour; if it ever fails, corrections are
landing in unrelated passages and the feature must not ship.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from magiah import normalize                                    # noqa: E402
from magiah.webui import db, patcher, server                    # noqa: E402


def write(path, text, encoding='utf-8', bom=False):
    """Write a fixture in BINARY: text mode would rewrite '\\n' to '\\r\\n' on
    Windows and the test would assert against bytes it never produced."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = text.encode(encoding)
    with open(path, 'wb') as f:
        f.write((b'\xef\xbb\xbf' if bom else b'') + data)
    return path


def raw(path):
    """The file's exact bytes — the unit these tests assert in."""
    with open(path, 'rb') as f:
        return f.read()


def text_of(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def scan_snippet(line, word, k=0):
    """The snippet as the scanner stores it: a +-45 char window of the CLEANED
    line around the k-th occurrence (the whole line if there is none)."""
    text = normalize.clean(line)
    want = word.split()
    toks = list(normalize.TOKEN_RE.finditer(text))
    hits = [(toks[i].start(), toks[i + len(want) - 1].end())
            for i in range(len(toks) - len(want) + 1)
            if [m.group() for m in toks[i:i + len(want)]] == want]
    if not 0 <= k < len(hits):
        return text.strip()
    s, e = hits[k]
    return text[max(0, s - 45):e + 45].strip()


def write_doc(doc, outdir):
    """Test helper: the fixer's two write steps, backup first and then an
    atomic replace, without the lock and journal fixer_api wraps them in
    (tested in test_anchor_write). Production code has no such shortcut."""
    with open(doc.path, 'rb') as f:
        before = f.read()
    backup, sha = patcher.write_backup(outdir, doc.path, before)
    return {'backup': backup, 'backup_sha': sha,
            'fingerprint': patcher.atomic_write(doc.path, doc.encode())}


def plan_all(doc, findings, *a, **kw):
    """patcher.plan_all with the scan-time snippet every real finding has."""
    for f in findings:
        n = f.get('lineno', -1)
        if 'snippet' not in f and 0 <= n < len(doc.lines):
            f['snippet'] = scan_snippet(doc.lines[n], f['word'],
                                        f.get('occurrence') or 0)
    return patcher.plan_all(doc, findings, *a, **kw)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_test_')
        self.outdir = os.path.join(self.tmp, 'scan')
        self.lib = os.path.join(self.tmp, 'lib')
        os.makedirs(self.outdir)
        os.makedirs(self.lib)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# the mapped tokenizer
# ---------------------------------------------------------------------------

class TestCleanMapped(unittest.TestCase):
    """clean_mapped must be clean() plus offsets — never a second dialect."""

    SAMPLES = [
        'ויל<big>ך</big> משֶה אל יֹותבת־העיר',
        '<sup style="color: #2563eb; font-weight: bold;">4</sup> וְיֶהֱמוּ גַלָיו',
        '<blockquote><p>אֲיֻמָּתִי בְּחֵן</p> <p>וְעִם קְדוֹשֶׁיךָ</p></blockquote>',
        'א&amp;ב &quot;ציטוט&quot; &#1488;&#1489;',
        '&#x5d0;&#x5d1; טקסט',
        'ר׳ יוסי אמר רמב"ם',
        'טמנוּ שׁלום הּא',
        'מילה​עם‫תווי בקרה',
        'ל קוחה מבית אביה',
        '',
        '   ',
        '<b></b>',
        'א' * 200,
    ]

    def test_matches_clean(self):
        for s in self.SAMPLES:
            with self.subTest(s=s[:40]):
                got, omap, emap = normalize.clean_mapped(s)
                self.assertEqual(got, normalize.clean(s))
                self.assertEqual(len(omap), len(got))
                self.assertEqual(len(emap), len(got))

    def test_token_spans_match_tokenize(self):
        for s in self.SAMPLES:
            with self.subTest(s=s[:40]):
                spans = normalize.token_spans(s)
                self.assertEqual([t for t, _a, _b in spans],
                                 normalize.tokenize(s))

    def test_each_span_renormalizes_to_its_token(self):
        for s in self.SAMPLES:
            for tok, a, b in normalize.token_spans(s):
                with self.subTest(s=s[:30], tok=tok):
                    self.assertTrue(0 <= a < b <= len(s))
                    self.assertEqual(normalize.clean(s[a:b]).strip(), tok)

    def test_span_covers_nikud(self):
        spans = normalize.token_spans('אל יָם הגדול')
        tok, a, b = [s for s in spans if s[0] == 'ים'][0]
        self.assertEqual('אל יָם הגדול'[a:b], 'יָם')

    def test_token_spanning_markup_is_one_span(self):
        line = 'ויל<big>ך</big> משה'
        spans = [s for s in normalize.token_spans(line) if s[0] == 'וילך']
        self.assertEqual(len(spans), 1)
        self.assertEqual(line[spans[0][1]:spans[0][2]], 'ויל<big>ך')

    def test_phrase_spans_covers_multi_token_word(self):
        # the extra_space family reports a split word: 'ל קוחה' -> 'לקוחה'
        line = 'והנה הבת ל קוחה מבית'
        spans = normalize.phrase_spans(line, 'ל קוחה')
        self.assertEqual(len(spans), 1)
        self.assertEqual(line[spans[0][1]:spans[0][2]], 'ל קוחה')

    def test_phrase_spans_finds_every_occurrence(self):
        line = 'יותבת אמר יותבת ועוד יותבת'
        self.assertEqual(len(normalize.phrase_spans(line, 'יותבת')), 3)


# ---------------------------------------------------------------------------
# reading and writing files
# ---------------------------------------------------------------------------

class TestByteRoundTrip(TempCase):
    """Read + write with no edits must reproduce the file byte for byte."""

    BODY = 'שורה ראשונה\nשורה שנייה יותבת כאן\nשורה שלישית'

    def _roundtrip(self, data):
        p = os.path.join(self.tmp, 'b.txt')
        with open(p, 'wb') as f:
            f.write(data)
        doc = patcher.read_doc(p)
        self.assertEqual(doc.encode(), data)

    def test_utf8_lf(self):
        self._roundtrip(self.BODY.encode('utf-8'))

    def test_utf8_crlf(self):
        self._roundtrip(self.BODY.replace('\n', '\r\n').encode('utf-8'))

    def test_utf8_with_bom(self):
        self._roundtrip(b'\xef\xbb\xbf' + self.BODY.encode('utf-8'))

    def test_bom_is_not_invented(self):
        # utf-8-sig would decode BOM-less text and re-encode WITH a BOM
        self._roundtrip(self.BODY.encode('utf-8'))
        p = os.path.join(self.tmp, 'b.txt')
        self.assertFalse(patcher.read_doc(p).bom)

    def test_cp1255(self):
        self._roundtrip(self.BODY.encode('cp1255'))

    def test_trailing_newline_kept(self):
        self._roundtrip((self.BODY + '\n').encode('utf-8'))

    def test_no_trailing_newline_kept(self):
        self._roundtrip(self.BODY.encode('utf-8'))

    def test_mixed_line_endings(self):
        self._roundtrip('א\r\nב\nג\rד'.encode('utf-8'))

    def test_empty_file(self):
        self._roundtrip(b'')

    def test_blank_lines(self):
        self._roundtrip('א\n\n\nב\n'.encode('utf-8'))

    def test_undecodable_is_refused(self):
        p = write(os.path.join(self.tmp, 'x.txt'), '')
        with open(p, 'wb') as f:
            f.write(b'\xff\xfe\x00\x01\xff\xff\xfe\xfe')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.read_doc(p)
        self.assertEqual(cm.exception.code, 'undecodable')

    def test_non_txt_is_refused(self):
        p = write(os.path.join(self.tmp, 'x.md'), 'טקסט')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.read_doc(p)
        self.assertEqual(cm.exception.code, 'not_txt')


# ---------------------------------------------------------------------------
# planning and applying edits
# ---------------------------------------------------------------------------

class TestPlanAndApply(TempCase):
    def doc(self, text):
        return patcher.read_doc(write(os.path.join(self.tmp, 'b.txt'), text))

    def test_only_the_span_changes(self):
        line = 'ויל<big>ך</big> משֶה אל יֹותבת־העיר וישב'
        doc = self.doc(line)
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'יותבת',
                   'correction': 'יֹושבת'}])
        self.assertEqual(failures, [])
        p = plans[0]
        self.assertEqual(line[p.start:p.end], 'יֹותבת')
        patcher.apply_edits(doc, plans)
        new = doc.lines[0]
        self.assertEqual(new[:p.start], line[:p.start])
        self.assertEqual(new[p.start + len(p.new_text):], line[p.end:])
        self.assertIn('<big>', new)          # markup untouched
        self.assertIn('־העיר', new)          # maqaf untouched

    def test_bracket_mode_exact_output(self):
        doc = self.doc('אל יָם הגדול')
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'ים', 'correction': 'ימה'}],
            default_mode=patcher.MODE_BRACKET)
        self.assertEqual(failures, [])
        # correction in round parens FIRST, then the original in square ones
        self.assertEqual(plans[0].new_text, '(ימה) [יָם]')
        patcher.apply_edits(doc, plans)
        self.assertEqual(doc.lines[0], 'אל (ימה) [יָם] הגדול')

    def test_bracket_keeps_the_original_nikud(self):
        # the DB's `word` is normalized; writing it back would strip vowels
        doc = self.doc('אל יָם הגדול')
        plans, _ = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'ים', 'correction': 'ימה'}],
            default_mode=patcher.MODE_BRACKET)
        self.assertIn('יָם', plans[0].new_text)

    def test_multi_edit_one_line(self):
        line = 'יותבת אמרה כי מחורז הוא ושוב יותבת חזרה'
        findings = [
            {'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
             'occurrence': 0, 'expected_count': 2},
            {'id': 2, 'lineno': 0, 'word': 'מחורז', 'correction': 'מחוז'},
            {'id': 3, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
             'occurrence': 1, 'expected_count': 2}]
        doc = self.doc(line)
        plans, failures = plan_all(doc, findings)
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        self.assertEqual(doc.lines[0],
                         'יושבת אמרה כי מחוז הוא ושוב יושבת חזרה')

    def test_multi_edit_is_order_independent(self):
        """Proves the descending-offset sort, not luck: 25,700 real lines
        carry more than one finding, and left-to-right application would
        mis-place every edit after the first."""
        line = 'יותבת אמרה כי מחורז הוא ושוב יותבת חזרה'
        findings = [
            {'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבתXX',
             'occurrence': 0, 'expected_count': 2},
            {'id': 2, 'lineno': 0, 'word': 'מחורז', 'correction': 'מ'},
            {'id': 3, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבתYY',
             'occurrence': 1, 'expected_count': 2}]
        results = []
        for order in (findings, list(reversed(findings)),
                      [findings[1], findings[2], findings[0]]):
            doc = self.doc(line)
            plans, _ = plan_all(doc, order)
            patcher.apply_edits(doc, plans)
            results.append(doc.lines[0])
        self.assertEqual(len(set(results)), 1, results)

    def test_a_word_split_by_a_tag_is_refused_in_both_modes(self):
        """``ויל<big>ך</big>`` is one token whose raw span is ``ויל<big>ך`` —
        it swallows the OPENING tag but not the closing one, because the tag
        opens mid-word. Replacing leaves an orphaned ``</big>``; bracketing
        yields ``[ויל<big>ך]</big>``. Both corrupt the markup, so both refuse.
        """
        for mode in (patcher.MODE_REPLACE, patcher.MODE_BRACKET):
            with self.subTest(mode=mode):
                doc = self.doc('ויל<big>ך</big> משה אל העיר')
                _plans, failures = plan_all(
                    doc, [{'id': 1, 'lineno': 0, 'word': 'וילך',
                           'correction': 'וילכו'}], default_mode=mode)
                self.assertEqual([f['code'] for f in failures],
                                 ['word_spans_markup'])

    def test_a_clean_word_on_a_marked_up_line_still_applies(self):
        """The refusal is about the word, not the line: a normal word sharing
        a line with markup must still be correctable, tags intact."""
        doc = self.doc('ויל<big>ך</big> משה אל העיר')
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'משה',
                   'correction': 'מושה'}])
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        self.assertEqual(doc.lines[0], 'ויל<big>ך</big> מושה אל העיר')

    def test_bracket_mode_with_several_edits_on_one_line(self):
        """Bracket mode makes each replacement LONGER than what it replaced,
        so a line carrying three of them is the case where left-to-right
        application would drift furthest off."""
        doc = self.doc('אמר יותבת וגם מחורז ועוד יותבת בסוף')
        plans, failures = plan_all(doc, [
            {'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
             'occurrence': 0, 'expected_count': 2},
            {'id': 2, 'lineno': 0, 'word': 'מחורז', 'correction': 'מחוז'},
            {'id': 3, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
             'occurrence': 1, 'expected_count': 2}],
            default_mode=patcher.MODE_BRACKET)
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        self.assertEqual(
            doc.lines[0],
            'אמר (יושבת) [יותבת] וגם (מחוז) [מחורז] ועוד (יושבת) [יותבת] בסוף')

    def test_mode_can_be_overridden_per_correction(self):
        """A per-book default with a per-finding override: one correction in
        brackets, the rest replaced outright, in a single write."""
        doc = self.doc('אמר יותבת וגם מחורז ועוד יותבת בסוף')
        plans, failures = plan_all(doc, [
            {'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
             'occurrence': 0, 'expected_count': 2},
            {'id': 2, 'lineno': 0, 'word': 'מחורז', 'correction': 'מחוז'},
            {'id': 3, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
             'occurrence': 1, 'expected_count': 2}],
            default_mode=patcher.MODE_REPLACE,
            modes={2: patcher.MODE_BRACKET})
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        self.assertEqual(doc.lines[0],
                         'אמר יושבת וגם (מחוז) [מחורז] ועוד יושבת בסוף')

    def test_extra_space_multi_token(self):
        doc = self.doc('והנה הבת ל קוחה מבית אביה')
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'ל קוחה',
                   'correction': 'לקוחה'}])
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        self.assertEqual(doc.lines[0], 'והנה הבת לקוחה מבית אביה')

    def test_missing_space_one_token_to_two_words(self):
        doc = self.doc('אמר להם ולאדירה היא')
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'ולאדירה',
                   'correction': 'ולא דירה'}])
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        self.assertEqual(doc.lines[0], 'אמר להם ולא דירה היא')

    # -- refusals ----------------------------------------------------------

    def _code(self, findings, text='אמר רבי יותבת בן זומא'):
        doc = self.doc(text)
        _plans, failures = plan_all(doc, findings)
        return failures[0]['code'] if failures else None

    def test_token_not_found(self):
        self.assertEqual(self._code(
            [{'id': 1, 'lineno': 0, 'word': 'נעדרת', 'correction': 'x'}]),
            'token_not_found')

    def test_line_gone(self):
        self.assertEqual(self._code(
            [{'id': 1, 'lineno': 99, 'word': 'יותבת', 'correction': 'x'}]),
            'line_gone')

    def test_no_correction(self):
        self.assertEqual(self._code(
            [{'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': ''}]),
            'no_correction')

    def test_ambiguous_occurrence(self):
        self.assertEqual(self._code(
            [{'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'x',
              'occurrence': 9, 'expected_count': 2}],
            'יותבת אמר יותבת'), 'ambiguous_occurrence')

    def test_occurrence_count_changed(self):
        """The line gained or lost a copy of the word since the scan: the
        stored index no longer means what it meant, so we must not guess."""
        self.assertEqual(self._code(
            [{'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'x',
              'occurrence': 0, 'expected_count': 3}],
            'יותבת אמר יותבת'), 'occurrence_count_changed')

    def test_lone_survivor_is_not_assumed_to_be_the_target(self):
        """The scan saw the word twice and only one copy is left.

        "There is only one candidate now, so it must be the right one" is
        exactly the reasoning that corrects the wrong occurrence: the finding
        meant the SECOND of two, and the survivor may well be the first.
        """
        self.assertEqual(self._code(
            [{'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'יושבת',
              'occurrence': 1, 'expected_count': 2,
              'snippet': 'אמר יותבת וגם יותבת שוב'}],
            'אמר יושבת וגם יותבת שוב'), 'occurrence_count_changed')

    def test_findings_without_a_count_still_apply(self):
        """Older rows carry no expected_count; they must not become unusable."""
        doc = self.doc('אמר רבי יותבת בן זומא')
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'יותבת',
                   'correction': 'יושבת'}])
        self.assertEqual(failures, [])
        self.assertEqual(plans[0].confidence, 'exact')

    def test_overlapping_edits(self):
        doc = self.doc('אמר יותבת שלום')
        _plans, failures = plan_all(doc, [
            {'id': 1, 'lineno': 0, 'word': 'יותבת', 'correction': 'א'},
            {'id': 2, 'lineno': 0, 'word': 'יותבת', 'correction': 'ב'}])
        self.assertTrue(any(f['code'] == 'overlapping_edits'
                            for f in failures), failures)

    def test_explicit_span_must_still_be_the_word(self):
        doc = self.doc('אמר רבי יותבת בן זומא')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(doc, {'id': 1, 'lineno': 0, 'word': 'יותבת',
                                    'correction': 'x',
                                    'snippet': 'אמר רבי יותבת בן זומא'},
                                    explicit=(0, 3))
        self.assertEqual(cm.exception.code, 'token_not_found')

    def test_db_unit_is_refused(self):
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.resolve_unit('12345')
        self.assertEqual(cm.exception.code, 'db_book')

    def test_path_traversal_is_refused(self):
        for unit in ('file:../../../etc/passwd:0',
                     'file:..\\..\\x.txt:0',
                     'file:a/b/../../../c.txt:0'):
            with self.subTest(unit=unit):
                with self.assertRaises(patcher.PatchError) as cm:
                    patcher.resolve_unit(unit, self.lib)
                self.assertEqual(cm.exception.code, 'outside_library')

    def test_units_that_do_not_name_a_file_are_refused(self):
        """'x.txt/..' and friends normalize to a DIRECTORY — often the library
        root — which is not a book and must never reach the writer."""
        for unit in ('file::0', 'file:.:0', 'file:x.txt/..:0', 'local::0'):
            with self.subTest(unit=unit):
                with self.assertRaises(patcher.PatchError) as cm:
                    patcher.resolve_unit(unit, self.lib)
                self.assertIn(cm.exception.code, ('bad_unit', 'db_book'))


# ---------------------------------------------------------------------------
# the book changed between the scan and the fix
# ---------------------------------------------------------------------------

class TestDriftedLines(TempCase):
    """A book edited after the scan must still be fixable — safely.

    Inserting or deleting a line shifts every finding below it. Refusing them
    all would force a full re-scan over one added line, so the finding is
    relocated — but only to an unambiguous, snippet-corroborated line, because
    guessing here is exactly how a correction reaches an unrelated passage.
    """

    def doc(self, text):
        return patcher.read_doc(write(os.path.join(self.tmp, 'b.txt'), text))

    def test_finds_the_line_after_an_insertion(self):
        d = self.doc('כותרת\nשורה חדשה\nאמר רבי יותבת בן זומא\nסוף\n')
        plan = patcher.plan_edit(d, {
            'id': 1, 'lineno': 1,            # where the scan saw it
            'word': 'יותבת', 'correction': 'יושבת',
            'snippet': 'אמר רבי יותבת בן זומא'})
        self.assertEqual(plan.lineno, 2)     # where it actually is now
        self.assertTrue(plan.drifted)
        self.assertEqual(plan.confidence, 'moved')
        self.assertEqual(d.lines[plan.lineno][plan.start:plan.end], 'יותבת')

    def test_refuses_when_several_lines_could_match(self):
        """Two candidate lines means no way to tell which one was meant."""
        d = self.doc('אמר רבי יותבת בן זומא\nכותרת\nאמר רבי יותבת בן זומא\n')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, {
                'id': 1, 'lineno': 1, 'word': 'יותבת',
                'correction': 'יושבת', 'snippet': 'אמר רבי יותבת בן זומא'})
        self.assertEqual(cm.exception.code, 'ambiguous_line')

    def test_refuses_without_a_snippet_to_corroborate(self):
        """With no snippet there is nothing to confirm the line with, so the
        nearest occurrence must NOT be assumed to be the right one."""
        d = self.doc('כותרת\nשורה\nאמר רבי יותבת בן זומא\n')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, {'id': 1, 'lineno': 1, 'word': 'יותבת',
                                  'correction': 'יושבת'})
        self.assertEqual(cm.exception.code, 'token_not_found')

    def test_does_not_search_beyond_the_window(self):
        lines = ['מילוי'] * 400 + ['אמר רבי יותבת בן זומא']
        d = self.doc('\n'.join(lines) + '\n')
        with self.assertRaises(patcher.PatchError):
            patcher.plan_edit(d, {
                'id': 1, 'lineno': 0, 'word': 'יותבת',
                'correction': 'יושבת', 'snippet': 'אמר רבי יותבת בן זומא'})

    def test_a_hand_fixed_word_is_left_alone(self):
        """The corrector already fixed it in an editor: there is nothing to
        do, and nothing may be overwritten."""
        d = self.doc('כותרת\nועוד אמר מחוז דבר\n')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, {
                'id': 1, 'lineno': 1, 'word': 'מחורז',
                'correction': 'מחוז', 'snippet': 'ועוד אמר מחורז דבר'})
        self.assertEqual(cm.exception.code, 'token_not_found')

    # -- parallel verses: the case that makes drift dangerous --------------
    # Hebrew religious texts repeat long formulas verbatim across many
    # verses. When a word has been hand-fixed, the nearest surviving copy is
    # typically its TWIN a few lines down — a different passage that shares
    # the formula. Relocating there writes the correction into the wrong
    # verse, silently. These tests pin the discrimination.

    FORMULA = "וידבר ה' אל משה לאמר דבר אל בני ישראל ואמרת אלהם"
    VERSE_A = FORMULA + ' איש כי יהיה בו נגעימ בעור בשרו'
    VERSE_B = FORMULA + ' אשה כי תזריע וילדה זכר נגעימ אחרים'

    def _formulaic(self, **edits):
        lines = ['%s פסוק מספר %d' % (self.FORMULA, i) for i in range(30)]
        for n, text in edits.items():
            lines[int(n[1:])] = text        # keys look like 'n10'
        return lines

    def test_a_hand_fix_never_relocates_onto_a_parallel_verse(self):
        lines = self._formulaic(n10=self.VERSE_A.replace('נגעימ', 'נגעים'),
                                n20=self.VERSE_B)
        d = self.doc('\n'.join(lines) + '\n')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, {'id': 1, 'lineno': 10, 'word': 'נגעימ',
                                  'correction': 'נגעים',
                                  'snippet': self.VERSE_A})
        self.assertEqual(cm.exception.code, 'token_not_found')
        # and the twin is untouched
        self.assertIn('נגעימ', d.lines[20])

    def test_two_viable_candidates_refuse_rather_than_pick_one(self):
        lines = self._formulaic(n10=self.VERSE_A, n20=self.VERSE_B)
        lines.insert(0, 'שורה שנוספה')      # everything drifts by one
        d = self.doc('\n'.join(lines) + '\n')
        with self.assertRaises(patcher.PatchError):
            patcher.plan_edit(d, {'id': 1, 'lineno': 10, 'word': 'נגעימ',
                                  'correction': 'נגעים',
                                  'snippet': self.VERSE_A})

    def test_genuine_drift_still_relocates_in_a_formulaic_book(self):
        """The strict identity test must not make drift useless: a real
        insertion in a formulaic book still relocates correctly — on the
        scan's own snippet window, which pins the sentence."""
        lines = self._formulaic(n10=self.VERSE_A)
        lines.insert(0, 'שורה שנוספה')
        d = self.doc('\n'.join(lines) + '\n')
        plan = patcher.plan_edit(d, {'id': 1, 'lineno': 10, 'word': 'נגעימ',
                                     'correction': 'נגעים',
                                     'snippet': scan_snippet(self.VERSE_A,
                                                             'נגעימ')})
        self.assertEqual(plan.lineno, 11)
        self.assertTrue(plan.drifted)
        self.assertEqual(plan.confidence, 'moved')

    def test_shared_words_alone_never_relocate(self):
        """A snippet that is not the scan's window (here the whole verse)
        only shows that most words are shared, which a parallel verse does
        too: the moved line is offered for a click, never written."""
        lines = self._formulaic(n10=self.VERSE_A)
        lines.insert(0, 'שורה שנוספה')
        d = self.doc('\n'.join(lines) + '\n')
        f = {'id': 1, 'lineno': 10, 'word': 'נגעימ', 'correction': 'נגעים',
             'snippet': self.VERSE_A}
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'moved_unproven')
        self.assertEqual(cm.exception.extra['candidate_lines'], [11])
        a, b = normalize.phrase_spans(d.lines[11], 'נגעימ')[0][1:]
        plan = patcher.plan_edit(d, f, explicit=(a, b))
        self.assertEqual((plan.lineno, plan.confidence), (11, 'manual'))

    def test_an_unchanged_book_is_fixed_in_place(self):
        lines = self._formulaic(n10=self.VERSE_A)
        d = self.doc('\n'.join(lines) + '\n')
        plan = patcher.plan_edit(d, {'id': 1, 'lineno': 10, 'word': 'נגעימ',
                                     'correction': 'נגעים',
                                     'snippet': self.VERSE_A})
        self.assertEqual(plan.lineno, 10)
        self.assertFalse(plan.drifted)


# ---------------------------------------------------------------------------
# writing, backups, restore
# ---------------------------------------------------------------------------

class TestWriteAndRestore(TempCase):
    def setUp(self):
        super().setUp()
        self.orig = 'אמר רבי יותבת בן זומא\nשורה שנייה\n'
        self.path = write(os.path.join(self.lib, 'ספר.txt'), self.orig)

    def _apply(self):
        doc = patcher.read_doc(self.path)
        plans, _ = plan_all(
            doc, [{'id': 1, 'lineno': 0, 'word': 'יותבת',
                   'correction': 'יושבת'}])
        patcher.apply_edits(doc, plans)
        return write_doc(doc, self.outdir)

    def test_backup_holds_the_original(self):
        res = self._apply()
        self.assertTrue(os.path.isfile(res['backup']))
        self.assertEqual(raw(res['backup']), self.orig.encode('utf-8'))

    def test_write_is_surgical(self):
        self._apply()
        self.assertEqual(text_of(self.path), 'אמר רבי יושבת בן זומא\nשורה שנייה\n')

    def test_no_temp_file_left(self):
        self._apply()
        leftovers = [x for x in os.listdir(self.lib) if x.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_backup_restores_the_original_bytes(self):
        res = self._apply()
        data = patcher.read_backup(self.outdir, res['backup'],
                                   res['backup_sha'])
        patcher.atomic_write(self.path, data)
        self.assertEqual(raw(self.path), self.orig.encode('utf-8'))

    def test_backup_name_separates_same_named_books(self):
        other = write(os.path.join(self.lib, 'sub', 'ספר.txt'), 'טקסט')
        a = patcher.backup_path(self.outdir, self.path)
        b = patcher.backup_path(self.outdir, other)
        self.assertNotEqual(os.path.basename(a), os.path.basename(b))

    def test_fingerprint_detects_change(self):
        fp = patcher.fingerprint(self.path)
        self.assertEqual(patcher.fingerprint(self.path), fp)
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write('x')
        self.assertNotEqual(patcher.fingerprint(self.path), fp)


# ---------------------------------------------------------------------------
# THE regression guard
# ---------------------------------------------------------------------------

class TestNeverTouchesAnotherBook(TempCase):
    """One filename, many books.

    In the real corpus the stem 'פרק א' names 35 distinct files. The fixer must
    therefore key on the path carried by `unit`, never on `source`. If this
    class fails, corrections are reaching books they were never meant for.
    """

    def setUp(self):
        super().setUp()
        self.text = 'אמר רבי יותבת בן זומא בשם רבו\n'
        self.paths = [
            write(os.path.join(self.lib, folder, 'פרק א.txt'), self.text)
            for folder in ('ספר ראשון', 'ספר שני', 'ספר שלישי')]

    def test_book_keys_are_distinct(self):
        keys = {patcher.book_key_of('file:%s/פרק א.txt:0' % f)
                for f in ('ספר ראשון', 'ספר שני', 'ספר שלישי')}
        self.assertEqual(len(keys), 3)

    def test_only_the_targeted_file_changes(self):
        before = [raw(p) for p in self.paths]
        _kind, abspath, lineno = patcher.resolve_unit(
            'file:ספר שני/פרק א.txt:0', self.lib)
        self.assertEqual(os.path.abspath(abspath),
                         os.path.abspath(self.paths[1]))
        doc = patcher.read_doc(abspath)
        plans, failures = plan_all(
            doc, [{'id': 1, 'lineno': lineno, 'word': 'יותבת',
                   'correction': 'יושבת'}])
        self.assertEqual(failures, [])
        patcher.apply_edits(doc, plans)
        write_doc(doc, self.outdir)

        after = [raw(p) for p in self.paths]
        self.assertEqual(after[0], before[0], 'ספר ראשון was modified!')
        self.assertNotEqual(after[1], before[1], 'the target was not written')
        self.assertEqual(after[2], before[2], 'ספר שלישי was modified!')
        self.assertIn('יושבת', after[1].decode('utf-8'))


# ---------------------------------------------------------------------------
# the HTTP API
# ---------------------------------------------------------------------------

class TestApi(TempCase):
    def setUp(self):
        super().setUp()
        self.text = ('בס"ד\n'
                     'אמר רבי יותבת בן זומא בשם רבו הגדול\n'
                     'ועוד אמר יותבת דבר אחר וגם מחורז הוא\n')
        self.paths = [
            write(os.path.join(self.lib, folder, 'פרק א.txt'), self.text)
            for folder in ('ספר ראשון', 'ספר שני', 'ספר שלישי')]
        with open(os.path.join(self.outdir, 'run_config.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'corpus': {'type': 'library', 'path': self.lib}}, f)

        con = db.connect(self.outdir)
        sql = ('INSERT INTO findings(id, family, errtype, word, suggestion, '
               'rank, verified, origin, source, ref, unit, doc, snippet) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)')
        lines = self.text.splitlines()
        for fid, word, sugg, unit in (
                (1, 'יותבת', 'יושבת', 'file:ספר שני/פרק א.txt:1'),
                (2, 'יותבת', 'יושבת', 'file:ספר ראשון/פרק א.txt:1'),
                (3, 'מחורז', 'מחוז', 'file:ספר שני/פרק א.txt:2')):
            snip = scan_snippet(lines[int(unit.rsplit(':', 1)[1])], word)
            con.execute(sql, (fid, 'error', 'edit1_sub', word, sugg, 5.0, 1,
                              'testOrigin', 'פרק א', 'ref', unit, None, snip))
        con.executemany(
            "INSERT INTO review VALUES(?,'approved',NULL,NULL,'t')",
            [(1,), (2,), (3,)])
        con.commit()
        con.close()

        server.Handler.outdir = self.outdir
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, path, body=None):
        url = 'http://127.0.0.1:%d%s' % (self.port, path)
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(
            url, data=data, method='POST' if data else 'GET',
            headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def test_books_are_separate_per_file(self):
        code, res = self.call('/api/fixer/books?statuses=approved')
        self.assertEqual(code, 200)
        same = [b for b in res['books'] if b['title'] == 'פרק א']
        self.assertEqual(len(same), 2)
        self.assertEqual(len({b['folder'] for b in same}), 2)

    def test_doc_returns_only_this_files_findings(self):
        key = urllib.request.quote('file:ספר שני/פרק א.txt')
        code, res = self.call('/api/fixer/doc?key=' + key)
        self.assertEqual(code, 200)
        self.assertEqual(sorted(i['id'] for i in res['items']), [1, 3])
        self.assertTrue(all(i['anchor']['ok'] for i in res['items']))

    def test_stale_fingerprint_is_refused(self):
        code, res = self.call('/api/fixer/apply', {
            'key': 'file:ספר שני/פרק א.txt',
            'fingerprint': 'sha256:stale', 'items': [{'id': 1}]})
        self.assertEqual(code, 409)
        self.assertEqual(res['code'], 'file_changed')

    def test_a_finding_from_another_file_is_refused(self):
        """The guard that makes the 35-way filename collision harmless."""
        key = 'file:ספר שני/פרק א.txt'
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        before = [raw(p) for p in self.paths]
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': doc['fingerprint'],
            'items': [{'id': 2}]})          # id 2 lives in 'ספר ראשון'
        self.assertEqual(code, 409)
        self.assertEqual(res['code'], 'unit_mismatch')
        self.assertEqual([raw(p) for p in self.paths], before)

    def test_apply_writes_only_the_target(self):
        key = 'file:ספר שני/פרק א.txt'
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        before = [raw(p) for p in self.paths]
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': doc['fingerprint'],
            'items': [{'id': 1}, {'id': 3}], 'mark_fixed': True})
        self.assertEqual(code, 200, res)
        self.assertEqual(len(res['applied']), 2)
        after = [raw(p) for p in self.paths]
        self.assertEqual(after[0], before[0])
        self.assertNotEqual(after[1], before[1])
        self.assertEqual(after[2], before[2])
        text = after[1].decode('utf-8')
        self.assertIn('יושבת', text)
        self.assertIn('מחוז', text)
        self.assertTrue(text.startswith('בס"ד\n'))

    def test_partial_failure_writes_nothing(self):
        key = 'file:ספר שני/פרק א.txt'
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        before = [raw(p) for p in self.paths]
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': doc['fingerprint'],
            'items': [{'id': 1},
                      {'id': 3, 'explicit_start': 0, 'explicit_end': 2}]})
        self.assertEqual(code, 409)
        self.assertEqual([raw(p) for p in self.paths], before)

    def test_bracket_mode_over_http(self):
        rich = write(os.path.join(self.lib, 'ספר ראשון', 'מנוקד.txt'),
                     '<sup>1</sup> וְיֶהֱמוּ אל יֹותבת־העיר\n')
        con = db.connect(self.outdir)
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (4, 'error', 'edit1_sub', 'יותבת', 'יושבת', 5.0, 1, 'o',
             'מנוקד', 'r', 'file:ספר ראשון/מנוקד.txt:0', None,
             scan_snippet('<sup>1</sup> וְיֶהֱמוּ אל יֹותבת־העיר', 'יותבת')))
        con.execute("INSERT INTO review VALUES(4,'approved',NULL,NULL,'t')")
        con.commit()
        con.close()
        key = 'file:ספר ראשון/מנוקד.txt'
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': doc['fingerprint'],
            'default_mode': 'bracket', 'items': [{'id': 4}]})
        self.assertEqual(code, 200, res)
        got = text_of(rich)
        self.assertIn('(יושבת) [יֹותבת]', got)
        self.assertIn('<sup>1</sup>', got)      # markup intact
        self.assertIn('וְיֶהֱמוּ', got)             # other nikud intact
        self.assertIn('־העיר', got)             # maqaf intact

    def test_undo_file_restores_bytes(self):
        key = 'file:ספר שני/פרק א.txt'
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        before = raw(self.paths[1])
        _c, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': doc['fingerprint'],
            'items': [{'id': 1}]})
        code, res2 = self.call('/api/fixer/undo_file',
                               {'edit_id': res['edit_id']})
        self.assertEqual(code, 200, res2)
        self.assertEqual(raw(self.paths[1]), before)

    def test_a_locked_or_read_only_book_is_refused_in_hebrew(self):
        """os.replace onto a read-only or locked book fails with
        "[WinError 5] Access is denied: '<temp>' -> '<book>'"; the user is
        told, in Hebrew, which book and why, and nothing is written."""
        key = 'file:ספר שני/פרק א.txt'
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        before = raw(self.paths[1])
        real = patcher.os.replace

        def denied(src, dst):
            raise PermissionError(13, 'Access is denied', src, None, dst)
        patcher.os.replace = denied
        try:
            code, res = self.call('/api/fixer/apply', {
                'key': key, 'fingerprint': doc['fingerprint'],
                'items': [{'id': 1}]})
        finally:
            patcher.os.replace = real
        self.assertEqual(code, 423, res)
        self.assertEqual(res['code'], 'access_denied')
        self.assertIn(self.paths[1], res['error'])
        self.assertNotIn('Access is denied', res['error'])
        self.assertNotIn('.tmp', res['error'])
        self.assertEqual(raw(self.paths[1]), before)

    def test_any_permission_error_is_reported_in_hebrew(self):
        from magiah.webui import fixer_api
        real = fixer_api.doc

        def denied(*a, **kw):
            raise PermissionError(13, 'Access is denied', r'C:\x\book.txt')
        fixer_api.doc = denied
        try:
            code, res = self.call('/api/fixer/doc?key=' +
                                  urllib.request.quote('file:x.txt'))
        finally:
            fixer_api.doc = real
        self.assertEqual(code, 423, res)
        self.assertIn(r'C:\x\book.txt', res['error'])
        self.assertNotIn('Access is denied', res['error'])

    def test_mode_is_remembered_per_book(self):
        key = 'file:ספר שני/פרק א.txt'
        self.call('/api/fixer/mode', {'key': key, 'mode': 'bracket'})
        _c, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        self.assertEqual(doc['default_mode'], 'bracket')

    def test_a_drifted_book_applies_where_the_corrector_was_shown(self):
        """The doc endpoint relocates a finding; the apply endpoint rebuilds
        the line from the ORIGINAL unit. If the two ever disagreed, the edit
        would land on the line the scan named rather than the line the
        corrector saw marked — so pin that they agree.
        """
        before = ('כותרת\n'
                  'הקדמה חדשה א\n'
                  'הקדמה חדשה ב\n'
                  'אמר רבי יותבת בן זומא בשם רבו\n')     # scanned as line 1
        p = write(os.path.join(self.lib, 'זז.txt'), before)
        con = db.connect(self.outdir)
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (7, 'error', 'edit1_sub', 'יותבת', 'יושבת', 5.0, 1, 'o', 'זז',
             'r', 'file:זז.txt:1', None, 'אמר רבי יותבת בן זומא בשם רבו'))
        con.execute("INSERT INTO review VALUES(7,'approved',NULL,NULL,'t')")
        con.commit()
        con.close()

        key = 'file:זז.txt'
        code, d = self.call('/api/fixer/doc?' +
                            urllib.parse.urlencode({'key': key}))
        self.assertEqual(code, 200, d)
        item = d['items'][0]
        self.assertTrue(item['anchor']['ok'], item['anchor'])
        self.assertEqual(item['lineno'], 3)          # relocated
        self.assertEqual(item['anchor']['confidence'], 'moved')

        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': d['fingerprint'],
            'items': [{'id': 7}]})
        self.assertEqual(code, 200, res)
        self.assertEqual(res['applied'][0]['lineno'], 3)
        after = text_of(p).splitlines()
        self.assertIn('יושבת', after[3])
        # the hand-inserted preface, and everything else, is untouched
        self.assertEqual(after[:3],
                         ['כותרת', 'הקדמה חדשה א', 'הקדמה חדשה ב'])

    def test_an_unapproved_finding_is_never_written(self):
        """The server, not the browser, is the boundary.

        The UI declines to arm an undecided finding, but a stale tab, a
        replayed request or a future client path must not be able to commit
        something nobody judged into a book.
        """
        con = db.connect(self.outdir)
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (11, 'error', 'edit1_sub', 'זומא', 'זומה', 5.0, 1, 'testOrigin',
             'פרק א', 'r', 'file:ספר שני/פרק א.txt:1', None, ''))
        con.commit()          # no review row -> effective status 'pending'
        con.close()

        key = 'file:ספר שני/פרק א.txt'
        code, d = self.call('/api/fixer/doc?' + urllib.parse.urlencode(
            {'key': key, 'statuses': 'approved,unsure,pending'}))
        self.assertEqual(code, 200, d)
        before = [raw(p) for p in self.paths]

        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': d['fingerprint'],
            'items': [{'id': 11}]})
        self.assertEqual(code, 409, res)
        self.assertEqual(res.get('code'), 'not_approved', res)
        self.assertEqual([raw(p) for p in self.paths], before)

    def test_one_unapproved_finding_blocks_the_whole_batch(self):
        """All-or-nothing still holds: an approved sibling is not written
        either, so the corrector is never left with a half-applied book."""
        con = db.connect(self.outdir)
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (12, 'error', 'edit1_sub', 'זומא', 'זומה', 5.0, 1, 'testOrigin',
             'פרק א', 'r', 'file:ספר שני/פרק א.txt:1', None, ''))
        con.commit()
        con.close()
        key = 'file:ספר שני/פרק א.txt'
        code, d = self.call('/api/fixer/doc?' + urllib.parse.urlencode(
            {'key': key, 'statuses': 'approved,unsure,pending'}))
        before = [raw(p) for p in self.paths]
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': d['fingerprint'],
            'items': [{'id': 1}, {'id': 12}]})     # 1 approved, 12 pending
        self.assertEqual(code, 409, res)
        self.assertEqual([raw(p) for p in self.paths], before)

    def test_db_book_is_listed_but_not_editable(self):
        con = db.connect(self.outdir)
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (9, 'error', 'edit1_sub', 'אבג', 'אבד', 1.0, 1, 'o',
             'ספר מהמסד', 'r', '55123', None, ''))
        con.execute("INSERT INTO review VALUES(9,'approved',NULL,NULL,'t')")
        con.commit()
        con.close()
        _c, res = self.call('/api/fixer/books?statuses=approved')
        dbb = [b for b in res['books'] if not b['editable']]
        self.assertEqual(len(dbb), 1)
        code, res2 = self.call('/api/fixer/apply', {
            'key': dbb[0]['key'], 'fingerprint': 'x', 'items': [{'id': 9}]})
        self.assertEqual(code, 400)
        self.assertEqual(res2['code'], 'db_book')

    # -- keys from a request are never trusted as paths ---------------------

    def test_crafted_file_key_cannot_leave_the_library(self):
        secret = write(os.path.join(self.tmp, 'outside.txt'), 'סוד\n')
        for key in ('file:../outside.txt', 'file:..\\outside.txt',
                    'file:ספר שני/../../outside.txt'):
            with self.subTest(key=key):
                code, res = self.call(
                    '/api/fixer/doc?key=' + urllib.request.quote(key))
                self.assertEqual(code, 400)
                self.assertEqual(res['code'], 'outside_library')
                self.assertNotIn('lines', res)
        self.assertFalse(os.path.exists(secret + '.magiah.lock'))

    def test_local_key_for_an_unscanned_file_is_refused(self):
        """A local key is an absolute path; one the review DB has no finding
        for must not open (or lock, or write) any file at all."""
        other = write(os.path.join(self.tmp, 'אחר', 'לא נסרק.txt'),
                      'אמר רבי יותבת\n')
        before = raw(other)
        key = 'local:' + other
        code, res = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        self.assertEqual(code, 400)
        self.assertEqual(res['code'], 'not_scanned')
        self.assertNotIn('lines', res)
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': patcher.fingerprint(other),
            'items': [{'id': 1}]})
        self.assertNotEqual(code, 200)
        self.assertEqual(raw(other), before)
        self.assertEqual(os.listdir(os.path.dirname(other)), ['לא נסרק.txt'])

    def test_local_key_for_a_scanned_file_still_works(self):
        book = write(os.path.join(self.tmp, 'מקומי', 'ספר בודד.txt'),
                     self.text)
        # as a book scan writes it (book_source._load_file_book): the real
        # path, which on macOS is /private/var/... for a /var/... temp dir
        unit = 'local:%s:1' % os.path.realpath(book)
        con = db.connect(self.outdir)
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (7, 'error', 'edit1_sub', 'יותבת', 'יושבת', 5.0, 1, 'o',
             'ספר בודד', 'r', unit, None,
             scan_snippet(self.text.splitlines()[1], 'יותבת')))
        con.execute("INSERT INTO review VALUES(7,'approved',NULL,NULL,'t')")
        con.commit()
        con.close()
        key = patcher.book_key_of(unit)
        code, doc = self.call('/api/fixer/doc?key=' + urllib.request.quote(key))
        self.assertEqual(code, 200, doc)
        self.assertEqual([i['id'] for i in doc['items']], [7])
        self.assertEqual(os.path.normcase(doc['book']['path']),
                         os.path.normcase(os.path.realpath(book)))
        code, res = self.call('/api/fixer/apply', {
            'key': key, 'fingerprint': doc['fingerprint'],
            'items': [{'id': 7}]})
        self.assertEqual(code, 200, res)
        self.assertIn('יושבת', text_of(book))

    def test_contained_path_guard(self):
        inside = patcher.contained_path(
            os.path.join(self.lib, 'a', '.', 'b.txt'), self.lib)
        self.assertEqual(inside, os.path.normpath(
            os.path.join(os.path.abspath(self.lib), 'a', 'b.txt')))
        for bad in (os.path.join(self.lib, '..', 'x.txt'), self.lib,
                    self.lib + 'x' + os.sep + 'y.txt'):
            with self.subTest(path=bad):
                with self.assertRaises(patcher.PatchError) as cm:
                    patcher.contained_path(bad, self.lib)
                self.assertEqual(cm.exception.code, 'outside_library')


if __name__ == '__main__':
    unittest.main(verbosity=2)
