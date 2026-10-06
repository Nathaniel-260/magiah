# -*- coding: utf-8 -*-
"""The Tanach evidence layer: which books are editions, which editions are
independent, when a quotation is evidence, and what a finding may inherit.

The fixture is synthetic: the "verses" are everyday word sequences placed in
a schema-6 style database (category tree, heRefs, book_version/version_line),
so nothing here depends on real biblical text.
"""
import json
import os
import pickle
import shutil
import sqlite3
import tempfile
import unittest

from magiah import core
from magiah.config import Config

ROOT, TORAH, PRAYER, SIDDUR, OTHER = 1, 2, 3, 4, 5
BIBLE, SIDDUR_BOOK, QUOTER, BIBLE_COPY = 1, 2, 3, 4
TITLE = 'בראשית'

KQ_PRIMARY = ('<span class="mam-kq"><span class="mam-kq-k">(מחברתה)</span> '
              '<span class="mam-kq-q">[מחברתו]</span></span>')

# chapter א, verses א..ז of the synthetic "Bible" book (primary edition)
VERSES = {
    'א': 'הילד הלך מחר אל הגן הגדול וראה שם פרח אדום יפה מאד בצהריים',
    'ב': 'השמש זרחה היום על ההר הירוק בבוקר שקט ונעים מאוד',
    'ג': 'האיש קנה ' + KQ_PRIMARY + ' בשוק העיר הקטנה ביום שני בבוקר',
    'ד': 'אמר המורה לתלמידים שבו בשקט וכתבו מהר את התשובה הנכונה',
    'ה': 'אמר המורה לתלמידים שבו בשקט וכתבו לאט את התשובה הנכונה',
    'ו': 'הנער שתה מים קרים מן הבאר העמוקה בערב חם',
    'ז': 'הדוד בנה בית חדש ליד הנהר הרחב בשנה שעברה',
}
# a second, independent source (two renderings of it = one source)
SECOND = {
    'א': VERSES['א'],
    'ב': VERSES['ב'],
    'ג': 'האיש קנה מחברתה [מחברתו] בשוק העיר הקטנה ביום שני בבוקר',
    'ד': VERSES['ד'],
    'ה': VERSES['ה'],
}
# a third independent source: one real deviation, one plene spelling, and the
# ketiv written plainly in the qere/ketiv slot
THIRD = {
    'א': VERSES['א'].replace('אדום', 'אדוס'),
    'ב': VERSES['ב'].replace('הירוק', 'הירק'),
    'ג': 'האיש קנה מחברתה בשוק העיר הקטנה ביום שני בבוקר',
}

SIDDUR_LINES = [
    'ברוך המקום הנער שתה מים קרים מן הבאר העמוקה בערב חם',
    'ועוד אמר המורה לתלמידים שבו בשקט וכתבו מהר את התשובה הנכונה',
]
# quotations with a typo in the word under test, inside a longer sentence
QUOTES = {
    'c': 'וכך כתוב הילד הלך מחר אל הגן הגדול וראה שם פרץ אדום יפה מאד '
         'בצהריים וזה נכון',
    'd': 'וכך כתוב השמש זרחה היום על ההר הירק בבוקר שקט ונעים מאוד וזה נכון',
    'e': 'וכך כתוב האיש קנה מחברתה בשוק העיר הקטנה ביום שני בבוקר וזה נכון',
    'f': 'וכך כתוב אמר המורה לתלמידים שבו בשקט וכתבו מהד את התשובה הנכונה '
         'וזה נכון',
    'a': 'וכך כתוב הנער שתה מים קדים מן הבאר העמוקה בערב חם וזה נכון',
    'b': 'וכך כתוב הדוד בנה בית חדס ליד הנהר הרחב בשנה שעברה וזה נכון',
}
# word -> (errtype, detector suggestion)
FLAGGED = {
    'פרץ': ('edit1_sub', 'פרס'),
    'הירק': ('spelling_variant', 'הירוק'),
    'מחברתה': ('edit1_sub', 'מחברתו'),
    'מהד': ('edit1_sub', 'מהר'),
    'קדים': ('edit1_sub', 'כדים'),
    'חדס': ('edit1_sub', 'חדש'),
}


def make_bible_db(path, third_source=False):
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE source(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE category(id INTEGER PRIMARY KEY, parentId INT,
                              title TEXT, level INT);
        CREATE TABLE book(id INTEGER PRIMARY KEY, categoryId INT,
                          sourceId INT, title TEXT, heRef TEXT,
                          hasTeamim INT DEFAULT 0, dependenceType TEXT);
        CREATE TABLE line(id INTEGER PRIMARY KEY, bookId INT, lineIndex INT,
                          heRef TEXT, tocEntryId INT, charCount INT);
        CREATE TABLE line_content(id INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE book_version(id INTEGER PRIMARY KEY, bookId INT,
                                  versionTitle TEXT, versionSource TEXT,
                                  hasContent INT);
        CREATE TABLE version_line(versionId INT, lineId INT, content TEXT,
                                  charCount INT,
                                  PRIMARY KEY(versionId, lineId));
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta VALUES('db_schema_version', '6');
        INSERT INTO source VALUES(1, 'Sefaria'), (2, 'OtherSource');
    ''')
    con.executemany('INSERT INTO category VALUES(?,?,?,?)', [
        (ROOT, None, 'תנ״ך', 0), (TORAH, ROOT, 'תורה', 1),
        (PRAYER, None, 'סדר התפילה', 0), (SIDDUR, PRAYER, 'סידור', 1),
        (OTHER, None, 'ספרים', 0)])
    con.executemany('INSERT INTO book VALUES(?,?,?,?,?,?,NULL)', [
        (BIBLE, TORAH, 1, TITLE, TITLE, 1),
        (SIDDUR_BOOK, SIDDUR, 2, 'סידור', 'סידור', 1),      # teamim, not Bible
        (QUOTER, OTHER, 2, 'ספר מצטט', 'ספר מצטט', 0),
        (BIBLE_COPY, TORAH, 1, TITLE, TITLE, 1)])          # same import twice
    lid = [0]
    verse_line = {}

    def add(book, idx, ref, text):
        lid[0] += 1
        con.execute('INSERT INTO line VALUES(?,?,?,?,NULL,?)',
                    (lid[0], book, idx, ref, len(text)))
        con.execute('INSERT INTO line_content VALUES(?,?)', (lid[0], text))
        return lid[0]

    add(BIBLE, 0, None, f'<h1>{TITLE}</h1>')
    for i, (v, text) in enumerate(VERSES.items(), 1):
        verse_line[v] = add(BIBLE, i, f'{TITLE}, א, {v}', f'({v}) {text}')
    for i, text in enumerate(SIDDUR_LINES):
        add(SIDDUR_BOOK, i, f'סידור {i}', text)
    for i, key in enumerate(sorted(QUOTES)):
        add(QUOTER, i, f'ספר מצטט {i}', QUOTES[key])
    add(BIBLE_COPY, 0, f'{TITLE}, א, ז', '(ז) ' + VERSES['ז'])

    versions = [(1, 'Primary', 'https://one.example/masorah', {}),
                (2, 'Second', 'https://two.example/a.xml', SECOND),
                (3, 'Second plain', 'https://two.example/a.xml', SECOND)]
    if third_source:
        versions.append((4, 'Third', 'https://three.example/t', THIRD))
    for vid, title, src, texts in versions:
        con.execute('INSERT INTO book_version VALUES(?,?,?,?,1)',
                    (vid, BIBLE, title, src))
        for v, line in verse_line.items():
            text = texts.get(v)
            con.execute('INSERT INTO version_line VALUES(?,?,?,0)',
                        (vid, line, f'({v}) {text}' if text else None))
    con.commit()
    con.close()


def _fixture_words():
    words = set()
    for text in (list(VERSES.values()) + list(SECOND.values())
                 + SIDDUR_LINES + list(QUOTES.values())):
        from magiah.normalize import tokenize
        words.update(tokenize(text))
    return words


class _DBCase(unittest.TestCase):
    third_source = False

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='magiah_tanach_')
        self.db = os.path.join(self.dir, 'seforim.db')
        make_bible_db(self.db, third_source=self.third_source)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# end to end: the locate stage on the fixture (runs on the old code too)
# ---------------------------------------------------------------------------

class LocateTest(_DBCase):
    @classmethod
    def setUpClass(cls):
        cls._shared = tempfile.mkdtemp(prefix='magiah_tanach_locate_')
        db = os.path.join(cls._shared, 'seforim.db')
        make_bible_db(db)
        out = os.path.join(cls._shared, 'out')
        os.makedirs(out)
        lex = {w: 1000 for w in _fixture_words()}
        for w in FLAGGED:
            lex[w] = 1
        with open(os.path.join(out, core.LEXICON_F), 'wb') as f:
            pickle.dump(lex, f)
        flagged = {w: (1, et, s, 1000, 3.0) for w, (et, s) in FLAGGED.items()}
        with open(os.path.join(out, core.FLAGGED_F), 'wb') as f:
            pickle.dump(flagged, f)
        spec = {'type': 'sqlite', 'path': db, 'table': 'line', 'id_col': 'id',
                'text_col': 'content', 'preset': 'otzaria'}
        core.locate(spec, Config(workers=1, n_chunks=2), out)
        con = sqlite3.connect(os.path.join(out, core.REPORT_DB_F))
        con.row_factory = sqlite3.Row
        cls.rows = {r['word']: dict(r) for r in con.execute(
            'SELECT * FROM occurrences_full')}
        cls.matches = [dict(r) for r in con.execute(
            'SELECT * FROM tanach_matches_full')]
        cls.rank = {r[0]: r[1] for r in con.execute(
            f'SELECT word, {core.RANK_SQL} FROM occurrences_full')}
        con.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._shared, ignore_errors=True)

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def _row(self, word):
        self.assertIn(word, self.rows, f'{word} vanished from the report')
        return self.rows[word]

    def test_a_non_bible_teamim_book_is_not_a_witness(self):
        # the verse exists in the Bible book (one source) and in a siddur
        # with teamim; the siddur must not make it two witnesses
        r = self._row('קדים')
        self.assertNotEqual(r['tanach'], 2)
        self.assertNotEqual(r['tanach'], 3)
        # the old code also replaced the detector's suggestion here
        self.assertEqual(r['suggestion'], 'כדים')
        self.assertNotEqual(r.get('evidence_kind'), 'tanach_verse_variant')

    def test_b_same_edition_imported_twice_is_one_witness(self):
        r = self._row('חדס')
        self.assertNotIn(r['tanach'], (2, 3))
        self.assertNotEqual(r.get('evidence_kind'), 'tanach_verse_variant')

    def test_c_detector_suggestion_kept_and_tanach_reading_is_alternative(self):
        r = self._row('פרץ')
        self.assertEqual(r['suggestion'], 'פרס')       # the detector's
        self.assertEqual(r['tanach'], 3)
        self.assertEqual(r['evidence_kind'], 'tanach_verse_variant')
        alts = json.loads(r['alternatives'])
        self.assertEqual([a['suggestion'] for a in alts], ['פרס', 'פרח'])
        self.assertEqual(alts[0]['by'], 'detector')
        tan = alts[1]
        self.assertEqual(tan['by'], 'tanach')
        self.assertEqual(tan['ref'], f'{TITLE} א:א')
        self.assertEqual(tan['independent_sources'], 2)
        self.assertEqual(tan['works'], 1)
        self.assertEqual(tan['occurrences'], 3)        # primary + 2 versions

    def test_d_plene_difference_is_not_an_error(self):
        r = self._row('הירק')
        self.assertNotIn(r['tanach'], (2, 3))
        self.assertEqual(r['suggestion'], 'הירוק')
        self.assertEqual(r['evidence_kind'], 'tanach_plene')
        self.assertIsNone(r['alternatives'])

    def test_e_qere_ketiv_reading_is_not_evidence(self):
        r = self._row('מחברתה')
        self.assertNotIn(r['tanach'], (2, 3))
        self.assertEqual(r['evidence_kind'], 'tanach_qere_ketiv')

    def test_f_ambiguous_alignment_gives_no_evidence(self):
        r = self._row('מהד')
        self.assertNotIn(r['tanach'], (2, 3))
        self.assertEqual(r['suggestion'], 'מהר')
        self.assertEqual(r['evidence_kind'], 'tanach_ambiguous')

    def test_g_no_rank_bonus_without_verified_evidence(self):
        for w in ('קדים', 'חדס', 'הירק', 'מחברתה', 'מהד'):
            r = self._row(w)
            self.assertNotEqual(r['tanach'], 2, w)
        self.assertEqual(self.matches, [])


# ---------------------------------------------------------------------------
# the index itself
# ---------------------------------------------------------------------------

class IndexTest(_DBCase):
    third_source = True

    def setUp(self):
        super().setUp()
        from magiah import tanach
        self.t = tanach
        self.idx = tanach.build_index(self.db)

    def test_only_the_verified_bible_book_is_an_edition(self):
        books = {e['book_id'] for e in self.idx.editions.values()}
        self.assertEqual(books, {BIBLE, BIBLE_COPY})
        self.assertEqual(self.idx.report['has_teamim_books'], 3)
        self.assertEqual(sum(self.idx.report['has_teamim_not_bible'].values()),
                         1)

    def test_independent_sources_not_editions(self):
        c = self.idx.counts()
        self.assertEqual(c['works'], 1)
        self.assertEqual(c['verses'], 7)
        self.assertEqual(c['editions'], 5)             # b1, b4, v2, v3, v4
        self.assertEqual(c['independent_sources'], 3)  # one, two, three
        groups = {e['group'] for e in self.idx.editions.values()
                  if e['book_id'] == BIBLE_COPY}
        self.assertEqual(groups, {self.idx.editions['b1']['group']})

    def test_same_host_versions_are_one_source(self):
        ev = self.idx.evidence('פרץ', 'שם', 'אדום', QUOTES['c'])
        self.assertEqual(ev.kind, self.t.VARIANT)
        d = ev.to_dict(self.idx)
        self.assertEqual(d['independent_sources'], 3)
        self.assertEqual(d['occurrences'], 4)

    def test_edition_error_needs_two_independent_agreeing_sources(self):
        rows, st = self.idx.edition_errors()
        self.assertEqual([(r[1], r[2]) for r in rows], [('אדוס', 'אדום')])
        ev = json.loads(rows[0][4])
        self.assertEqual(ev['evidence_kind'], 'tanach_edition_variant')
        self.assertEqual(ev['independent_sources'], 2)
        self.assertEqual(ev['minority_source'], 'host:three.example')
        # the plene spelling and the qere/ketiv slot are counted, not reported
        self.assertEqual(st['plene'], 1)
        self.assertEqual(st['qere_ketiv'], 1)

    def test_partial_quote_is_not_evidence(self):
        # only two context tokens match the verse
        self.assertIsNone(self.idx.evidence(
            'פרץ', 'שם', 'אדום', 'משהו אחר שם פרץ אדום ועוד דבר אחר לגמרי'))

    def test_verse_tokens_mark_qere_ketiv(self):
        toks = self.t.verse_tokens('(ג) ' + VERSES['ג'])
        self.assertEqual([t[0] for t in toks][:3], ['האיש', 'קנה', 'מחברתו'])
        self.assertTrue(toks[2][1])
        self.assertEqual(toks[2][2], ('מחברתה',))
        plain = self.t.verse_tokens('(ג) ' + SECOND['ג'])
        self.assertEqual([t[0] for t in plain], [t[0] for t in toks])


# ---------------------------------------------------------------------------
# findings of the old mechanism: flagged, no bonus, no inherited decision
# ---------------------------------------------------------------------------

OCC_COLS = ('word, errtype, suggestion, score, ctx_hits, sugg_local, '
            'book_repeat, tanach, source, ref, unit, snippet, origin, doc')


def make_legacy_report(path):
    con = sqlite3.connect(path)
    con.executescript(f'''
        CREATE TABLE occurrences_full({OCC_COLS});
        CREATE TABLE space_errors_full(part1, part2, joined, join_freq,
                                       source, ref, unit, snippet, origin);
        CREATE TABLE tanach_matches_full(word, source, ref, unit, snippet,
                                         origin);
        CREATE TABLE tanach_errors_full(word, canonical, source, ref, unit,
                                        snippet, origin);
    ''')
    con.executemany(f'INSERT INTO occurrences_full VALUES({",".join("?"*14)})', [
        ('פרץ', 'edit1_sub', 'פרח', 2.0, 0, 0, 0, 2, 'ספר', 'ר', '7', 's',
         'Dicta', '3'),
        ('בייתה', 'edit1_sub', 'ביתה', 2.0, 0, 0, 0, 0, 'ספר', 'ר', '8', 's',
         'Dicta', '3')])
    con.execute('INSERT INTO tanach_errors_full VALUES(?,?,?,?,?,?,?)',
                ('אדוס', 'אדום', 'ספר', 'ר', '9', 's', 'Sefaria'))
    con.commit()
    con.close()


class LegacyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='magiah_tanach_legacy_')
        make_legacy_report(os.path.join(self.dir, 'report.db'))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_rank_formula_gives_old_mechanism_nothing(self):
        con = sqlite3.connect(':memory:')
        con.execute('CREATE TABLE t(score, errtype, ctx_hits, sugg_local, '
                    'book_repeat, tanach)')
        con.executemany('INSERT INTO t VALUES(2.0, ?, 1, 0, 0, ?)',
                        [('edit1_sub', 0), ('edit1_sub', 2)])
        r0, r2 = [r[0] for r in con.execute(
            f'SELECT {core.RANK_SQL} FROM t ORDER BY tanach')]
        self.assertEqual(r0, r2)

    def test_webui_flags_legacy_and_does_not_inherit_decisions(self):
        from magiah.webui import db as uidb
        con = uidb.connect(self.dir)
        # decisions made before this change, on the old evidence
        now = '2026-01-01T00:00:00'
        for fid, fam, et, w, unit, tan in (
                (1, 'error', 'edit1_sub', 'פרץ', '7', 2),
                (2, 'error', 'edit1_sub', 'בייתה', '8', 0),
                (3, 'tanach_error', 'tanach_edition', 'אדוס', '9', None)):
            con.execute('INSERT INTO findings(id, family, errtype, word, '
                        'unit, ref, tanach) VALUES(?,?,?,?,?,?,?)',
                        (fid, fam, et, w, unit, 'ר', tan))
            con.execute('INSERT INTO review VALUES(?,?,?,?,?)',
                        (fid, 'approved', None, None, now))
        con.commit()
        con.close()

        uidb.import_all(self.dir)
        con = uidb.connect(self.dir)
        rows = {r['word']: dict(r) for r in con.execute(
            'SELECT f.*, r.status FROM findings f '
            'LEFT JOIN review r ON r.finding_id = f.id')}
        legacy = rows['פרץ']
        self.assertEqual(json.loads(legacy['extra'])['evidence_kind'],
                         'tanach_legacy')
        self.assertTrue(json.loads(legacy['extra'])['recheck'])
        self.assertEqual(legacy['rank'], 2.0 - 1.0)    # no +4 for tanach=2
        self.assertIsNone(legacy['status'])            # approval not inherited
        self.assertEqual(rows['בייתה']['status'], 'approved')  # untouched
        ed = rows['אדוס']
        self.assertEqual(ed['rank'], 0.0)
        self.assertEqual(json.loads(ed['extra'])['evidence_kind'],
                         'tanach_legacy')
        self.assertIsNone(ed['status'])

        # a decision taken AFTER the re-check mark survives a refresh
        con.execute("INSERT INTO review VALUES(?, 'not_error', NULL, NULL, ?)",
                    (legacy['id'], now))
        con.commit()
        con.close()
        uidb.import_all(self.dir)
        con = uidb.connect(self.dir)
        st = con.execute(
            "SELECT r.status FROM findings f JOIN review r "
            "ON r.finding_id = f.id WHERE f.word = 'פרץ'").fetchone()
        con.close()
        self.assertEqual(st[0], 'not_error')


if __name__ == '__main__':
    unittest.main()
