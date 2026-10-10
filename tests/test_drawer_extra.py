# -*- coding: utf-8 -*-
"""The finding drawer shows a finding's Tanach evidence as readable Hebrew.

Witnesses, readings and minority editions used to be printed as raw JSON,
with English keys ("source", "editions") and bare host codes
("host:two.example"). The rendering (app.js extraLines) runs through node
against the labels the server sends in /api/meta.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from magiah.webui import hebrew

NODE = shutil.which('node')
HERE = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(os.path.dirname(HERE), 'magiah', 'webui', 'static',
                      'app.js')
HARNESS = os.path.join(HERE, 'js', 'drawer_extra_harness.js')
META = {'evidence_labels': hebrew.EVIDENCE_LABELS,
        'extra_labels': hebrew.EXTRA_LABELS,
        'origins': [{'name': 'Sefaria', 'hebrew': 'ספריא'}]}
LATIN_KEY = re.compile(r'\b(source|editions|edition|book_id|version_id|'
                       r'host|witnesses|readings)\b')


@unittest.skipIf(NODE is None, 'node is not installed')
class DrawerExtraTest(unittest.TestCase):

    def render(self, *cases):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'cases.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'meta': META, 'cases': [
                    {'key': k, 'value': v} for k, v in cases]}, f,
                    ensure_ascii=False)
            out = subprocess.run([NODE, HARNESS, APP_JS, p],
                                 capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode('utf-8',
                                                              'replace'))
        return json.loads(out.stdout.decode('utf-8'))

    def assert_readable(self, lines):
        for line in lines:
            self.assertNotIn('{', line)
            self.assertNotIn('"', line)
            self.assertNotIn('host:', line)
            self.assertNotIn('source:', line)
            self.assertIsNone(LATIN_KEY.search(line), line)

    def test_witnesses(self):
        (lines,) = self.render(('witnesses', [
            {'source': 'host:two.example', 'editions': ['Second',
                                                        'Second plain']},
            {'source': 'source:Sefaria', 'editions': ['בראשית (Sefaria)']}]))
        self.assertEqual(lines, ['אתר two.example: Second, Second plain',
                                 'מאגר ספריא: בראשית (Sefaria)'])
        self.assert_readable(lines)

    def test_readings_of_several_sources_and_of_one(self):
        many, one = self.render(
            ('readings', {'לאט': {'host:one.example': ['Primary']},
                          'לאת': {'host:two.example': ['Second', 'Second '
                                                       'plain']}}),
            ('readings', {'הנכונה': ['Second'], 'הנכונח': ['Second plain']}))
        self.assertEqual(many, ['«לאט» — אתר one.example (Primary)',
                                '«לאת» — אתר two.example (Second, Second '
                                'plain)'])
        self.assertEqual(one, ['«הנכונה» — Second', '«הנכונח» — Second plain'])
        self.assert_readable(many + one)

    def test_minority_source_editions_and_word_editions(self):
        src, eds, where = self.render(
            ('minority_source', 'host:three.example'),
            ('minority_editions', ['Third']),
            ('word_editions', [
                {'edition': 'Third', 'book_id': 1, 'version_id': 4},
                {'edition': 'בראשית (Sefaria)', 'book_id': 7,
                 'version_id': None}]))
        self.assertEqual(src, ['אתר three.example'])
        self.assertEqual(eds, ['Third'])
        self.assertEqual(where, ['Third (גרסה 4)',
                                 'בראשית (Sefaria) (טקסט ראשי של ספר 7)'])
        self.assert_readable(src + eds + where)

    def test_codes_flags_and_unknown_objects(self):
        kind, reason, flag, scope, other, nothing = self.render(
            ('evidence_kind', 'tanach_edition_variant'),
            ('reason', 'one_against_one'),
            ('book_scan', True),
            ('ctx_scope', 'book'),
            ('some_future_key', {'edition': 'X', 'version_id': 2}),
            ('alt', None))
        self.assertEqual(kind, [hebrew.EVIDENCE_LABELS[
            'tanach_edition_variant']])
        self.assertEqual(reason, [hebrew.EVIDENCE_LABELS['one_against_one']])
        self.assertEqual(flag, ['כן'])
        self.assertEqual(scope, ['בתוך הספר בלבד'])
        # an object nobody planned for: labelled keys, still no JSON
        self.assertEqual(other, ['מהדורה: X', 'מזהה גרסה: 2'])
        self.assertEqual(nothing, [])

    def test_every_key_the_evidence_writes_has_a_hebrew_label(self):
        for key in ('evidence_kind', 'reason', 'ref', 'verse_line',
                    'reading', 'readings', 'aligned_tokens', 'occurrences',
                    'works', 'independent_sources', 'witnesses',
                    'minority_source', 'minority_editions', 'word_editions',
                    'source', 'canonical', 'book_scan', 'ctx_scope'):
            self.assertIn(key, hebrew.EXTRA_LABELS)


if __name__ == '__main__':
    unittest.main()
