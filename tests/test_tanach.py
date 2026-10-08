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
    'ח': 'הסבתא אפתה עוגה מתוקה לכל הנכדים בליל שבת שמח',
}
# a second, independent source (two renderings of it = one source)
SECOND = {
    'א': VERSES['א'],
    'ב': VERSES['ב'],
    'ג': 'האיש קנה מחברתה [מחברתו] בשוק העיר הקטנה ביום שני בבוקר',
    'ד': VERSES['ד'],
    'ה': VERSES['ה'].replace('לאט', 'לאת'),     # one source against one
    'ח': VERSES['ח'],
}
# the same source rendered again, disagreeing with itself in verse ד
SECOND_PLAIN = dict(SECOND, ד=VERSES['ד'].replace('הנכונה', 'הנכונח'))
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
    'h': 'וכך כתוב הסבתא אפתה עוגח מתוקה לכל הנכדים בליל שבת שמח וזה נכון',
}
# word -> (errtype, detector suggestion)
FLAGGED = {
    'פרץ': ('edit1_sub', 'פרס'),
    'הירק': ('spelling_variant', 'הירוק'),
    'מחברתה': ('edit1_sub', 'מחברתו'),
    'מהד': ('edit1_sub', 'מהר'),
    'קדים': ('edit1_sub', 'כדים'),
    'חדס': ('edit1_sub', 'חדש'),
    'עוגח': ('edit1_sub', 'עוגה'),
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
                (3, 'Second plain', 'https://two.example/a.xml',
                 SECOND_PLAIN)]
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
        cls.boost = {r[0]: r[1] for r in con.execute(
            f'SELECT word, ({core.RANK_SQL}) - ('
            + core.RANK_SQL.replace('tanach', '0') +
            ') FROM occurrences_full')}
        con.close()
        core.report(Config(), out)
        import csv
        with open(os.path.join(out, 'errors_edit1_sub.csv'),
                  encoding='utf-8-sig', newline='') as f:
            cls.csv_rows = {r['word']: r for r in csv.DictReader(f)}

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
        # the verse reads differently from the detector: a human decides,
        # and the disagreement earns no rank bonus
        self.assertEqual(r['tanach'], 4)
        self.assertEqual(r['evidence_kind'], 'tanach_disagrees')
        self.assertEqual(r['tanach_reading'], 'פרח')
        self.assertEqual(self.boost['פרץ'], 0)
        alts = json.loads(r['alternatives'])
        self.assertEqual([a['suggestion'] for a in alts], ['פרס', 'פרח'])
        self.assertEqual(alts[0]['by'], 'detector')
        tan = alts[1]
        self.assertEqual(tan['by'], 'tanach')
        self.assertEqual(tan['ref'], f'{TITLE} א:א')
        self.assertEqual(tan['independent_sources'], 2)
        self.assertEqual(tan['works'], 1)
        self.assertEqual(tan['occurrences'], 3)        # primary + 2 versions
        self.assertGreaterEqual(tan['aligned_tokens'], 5)

    def test_h_bonus_only_when_verse_and_detector_agree(self):
        r = self._row('עוגח')
        self.assertEqual(r['suggestion'], 'עוגה')
        self.assertEqual(r['tanach'], 3)
        self.assertEqual(r['evidence_kind'], 'tanach_verse_variant')
        self.assertEqual(self.boost['עוגח'], 4.0)
        alts = json.loads(r['alternatives'])
        self.assertTrue(alts[0]['agrees_with_tanach'])

    def test_csv_reports_carry_the_tanach_reading(self):
        self.assertEqual(self.csv_rows['פרץ']['suggestion'], 'פרס')
        self.assertEqual(self.csv_rows['פרץ']['tanach_reading'], 'פרח')
        self.assertEqual(self.csv_rows['חדס']['tanach_reading'], '')

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
        for w in ('קדים', 'חדס', 'הירק', 'מחברתה', 'מהד', 'פרץ'):
            r = self._row(w)
            self.assertNotEqual(r['tanach'], 2, w)
            self.assertEqual(self.boost[w], 0, w)
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
        self.assertEqual(c['verses'], 8)
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
        errs = [r for r in rows
                if json.loads(r[4])['evidence_kind'] == 'tanach_edition_variant']
        self.assertEqual([(r[1], r[2]) for r in errs], [('אדוס', 'אדום')])
        ev = json.loads(errs[0][4])
        self.assertEqual(ev['independent_sources'], 2)
        self.assertEqual(ev['minority_source'], 'host:three.example')
        # the plene spelling and the qere/ketiv slot are labelled, not errors
        self.assertEqual(st['plene'], 1)
        self.assertEqual(st['qere_ketiv'], 1)
        reasons = sorted(json.loads(r[4])['reason'] for r in rows
                         if r not in errs)
        self.assertEqual(reasons, ['intra_source', 'one_against_one',
                                   'plene', 'qere_ketiv'])

    def test_unresolved_disagreements_are_reported_not_dropped(self):
        rows, _ = self.idx.edition_errors()
        by = {json.loads(r[4])['reason']: r for r in rows
              if json.loads(r[4])['evidence_kind']
              == 'tanach_edition_unresolved'}
        one = json.loads(by['one_against_one'][4])
        self.assertEqual(sorted(one['readings']), ['לאט', 'לאת'])
        self.assertEqual(by['one_against_one'][2], '')    # no canonical
        intra = json.loads(by['intra_source'][4])
        self.assertEqual(intra['source'], 'host:two.example')
        self.assertEqual(sorted(intra['readings']), ['הנכונה', 'הנכונח'])

    def test_four_aligned_tokens_are_not_enough(self):
        self.assertIsNone(self.idx.evidence(
            'פרץ', 'שם', 'אדום', 'משהו אחר שם פרץ אדום יפה מאד ועוד דבר'))

    def test_common_words_only_context_does_not_align(self):
        ok = self.idx.evidence('פרץ', 'שם', 'אדום', QUOTES['c'])
        self.assertIsNotNone(ok)
        old = self.t.COMMON_FREQ
        self.t.COMMON_FREQ = 1          # every context word is now "common"
        try:
            self.assertIsNone(self.idx.evidence('פרץ', 'שם', 'אדום',
                                                QUOTES['c']))
        finally:
            self.t.COMMON_FREQ = old

    def test_plene_is_one_inner_vav_or_yod(self):
        pe = self.t.plene_equal
        self.assertTrue(pe('הירוק', 'הירק'))
        self.assertTrue(pe('שמים', 'שמם'))
        self.assertFalse(pe('שומר', 'שימר'))       # substitution
        self.assertFalse(pe('ויאמר', 'יאמר'))      # leading conjunction
        self.assertFalse(pe('יאמר', 'אמר'))        # leading prefix
        self.assertFalse(pe('אמרו', 'אמור'))       # transposition
        self.assertFalse(pe('הירוק', 'הירוק'))

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
         'Dicta', '3'),
        ('קפץ', 'edit1_sub', 'קפה', 2.0, 0, 0, 0, 2, 'ספר', 'ר', '10', 's',
         'Dicta', '3'),
        ('גשמ', 'edit1_sub', 'גשם', 2.0, 0, 0, 0, 2, 'ספר', 'ר', '11', 's',
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
                (3, 'tanach_error', 'tanach_edition', 'אדוס', '9', None),
                (4, 'error', 'edit1_sub', 'קפץ', '10', 2),
                (5, 'error', 'edit1_sub', 'גשמ', '11', 2)):
            con.execute('INSERT INTO findings(id, family, errtype, word, '
                        'unit, ref, tanach) VALUES(?,?,?,?,?,?,?)',
                        (fid, fam, et, w, unit, 'ר', tan))
            status = {4: 'not_error', 5: 'ignored'}.get(fid, 'approved')
            con.execute('INSERT INTO review VALUES(?,?,?,?,?)',
                        (fid, status, None, None, now))
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
        # decisions about the word itself do not depend on the verse reading
        self.assertEqual(rows['קפץ']['status'], 'not_error')
        self.assertEqual(rows['גשמ']['status'], 'ignored')
        dropped = con.execute(
            "SELECT COUNT(*) FROM history WHERE action = 'legacy_recheck'"
            ).fetchone()[0]
        self.assertEqual(dropped, 2)

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

    def _ui_state(self):
        """(word -> status, decisions.db (word, unit) -> verdict, number of
        legacy_recheck history entries, word -> finding id)."""
        from magiah.webui import db as uidb
        con = uidb.connect(self.dir)
        st = dict(con.execute('SELECT f.word, r.status FROM findings f '
                              'LEFT JOIN review r ON r.finding_id = f.id'))
        ids = dict(con.execute('SELECT word, id FROM findings'))
        n = con.execute("SELECT COUNT(*) FROM history "
                        "WHERE action = 'legacy_recheck'").fetchone()[0]
        con.close()
        dec = sqlite3.connect(os.path.join(self.dir, 'decisions.db'))
        d = {(w, u): v for w, u, v in dec.execute(
            'SELECT word, unit, verdict FROM decisions')}
        dec.close()
        return st, d, n, ids

    # statuses set through the UI before the re-check mark existed, so
    # decisions.db mirrors them (and the UI owns those rows)
    SEED = ((1, 'error', 'edit1_sub', 'פרץ', '7', 2, 'פרח', 'approved'),
            (2, 'error', 'edit1_sub', 'בייתה', '8', 0, 'ביתה', 'approved'),
            (3, 'tanach_error', 'tanach_edition', 'אדוס', '9', None, 'אדום',
             'approved'),
            (4, 'error', 'edit1_sub', 'קפץ', '10', 2, 'קפה', 'not_error'),
            (5, 'error', 'edit1_sub', 'גשמ', '11', 2, 'גשם', 'ignored'),
            (6, 'error', 'edit1_sub', 'שמש', '12', 2, 'שמס', 'unsure'),
            (7, 'error', 'edit1_sub', 'ירח', '13', 2, 'ירך', 'fixed'))
    SEED_ST = {'פרץ': 'approved', 'בייתה': 'approved', 'אדוס': 'approved',
               'קפץ': 'not_error', 'גשמ': 'ignored', 'שמש': 'unsure',
               'ירח': 'fixed'}
    SEED_DEC = {('פרץ', '7'): 'accept', ('בייתה', '8'): 'accept',
                ('אדוס', '9'): 'accept', ('קפץ', '10'): 'reject',
                ('גשמ', '11'): 'ignore', ('ירח', '13'): 'accept'}
    EXPECT_ST = dict(SEED_ST, פרץ=None, אדוס=None)
    EXPECT_DEC = {k: v for k, v in SEED_DEC.items()
                  if k not in (('פרץ', '7'), ('אדוס', '9'))}

    def _seed_ui(self):
        from magiah.webui import db as uidb
        rep = sqlite3.connect(os.path.join(self.dir, 'report.db'))
        rep.executemany(
            f'INSERT INTO occurrences_full VALUES({",".join("?"*14)})', [
                ('שמש', 'edit1_sub', 'שמס', 2.0, 0, 0, 0, 2, 'ספר', 'ר',
                 '12', 's', 'Dicta', '3'),
                ('ירח', 'edit1_sub', 'ירך', 2.0, 0, 0, 0, 2, 'ספר', 'ר',
                 '13', 's', 'Dicta', '3')])
        rep.commit()
        rep.close()
        con = uidb.connect(self.dir)
        for fid, fam, et, w, unit, tan, sugg, _ in self.SEED:
            con.execute('INSERT INTO findings(id, family, errtype, word, '
                        'unit, ref, tanach, suggestion, source) '
                        'VALUES(?,?,?,?,?,?,?,?,?)',
                        (fid, fam, et, w, unit, 'ר', tan, sugg, 'ספר'))
        con.commit()
        for fid, *_, status in self.SEED:
            uidb.set_status(con, self.dir, [fid], status)
        con.close()

    def test_dropped_legacy_approval_is_withdrawn_from_decisions_db(self):
        """A dropped approval must not come back through decisions.db: not via
        "import legacy decisions", and not in the old review tool."""
        from magiah.webui import db as uidb
        self._seed_ui()
        expect_st, expect_dec = self.EXPECT_ST, self.EXPECT_DEC
        counts = uidb.import_all(self.dir)
        self.assertEqual(counts['legacy_approvals_dropped'], 2)
        st, dec, n, _ = self._ui_state()
        self.assertEqual(st, expect_st)
        self.assertEqual(dec, expect_dec)
        self.assertEqual(n, 2)                  # the drop stays in history

        # "import legacy decisions" has nothing to bring back...
        con = uidb.connect(self.dir)
        uidb.migrate_legacy_decisions(con, self.dir)
        con.close()
        self.assertEqual(self._ui_state()[:3], (expect_st, expect_dec, 2))
        # ...and a second refresh changes nothing
        counts = uidb.import_all(self.dir)
        self.assertEqual(counts['legacy_approvals_dropped'], 0)
        self.assertEqual(self._ui_state()[:3], (expect_st, expect_dec, 2))

    def test_book_rescan_logs_and_withdraws_dropped_legacy_approvals(self):
        from magiah.webui import db as uidb
        con = uidb.connect(self.dir)
        for fid, w, unit, tan, sugg in ((1, 'פרץ', '7', 2, 'פרח'),
                                        (2, 'בייתה', '8', 0, 'ביתה'),
                                        (3, 'קפץ', '10', 2, 'קפה')):
            con.execute('INSERT INTO findings(id, family, errtype, word, '
                        'unit, ref, tanach, suggestion, source, origin, doc) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (fid, 'error', 'edit1_sub', w, unit, 'ר', tan, sugg,
                         'ספר', 'Dicta', '3'))
        con.commit()
        uidb.set_status(con, self.dir, [1, 2], 'approved')
        uidb.set_status(con, self.dir, [3], 'not_error')
        con.close()

        def found(word, unit, sugg):
            return {'word': word, 'errtype': 'edit1_sub', 'suggestion': sugg,
                    'score': 2.0, 'unit': unit, 'ref': 'ר', 'source': 'ספר',
                    'origin': 'Dicta', 'snippet': 's'}
        counts = uidb.import_book_scan(self.dir, {
            'doc': '3', 'title': 'ספר',
            'findings': [found('פרץ', '7', 'פרס'), found('בייתה', '8', 'ביתה'),
                         found('קפץ', '10', 'קפה')]})
        self.assertEqual(counts['legacy_approvals_dropped'], 1)
        expect_st = {'פרץ': None, 'בייתה': 'approved', 'קפץ': 'not_error'}
        expect_dec = {('בייתה', '8'): 'accept', ('קפץ', '10'): 'reject'}
        st, dec, n, ids = self._ui_state()
        self.assertEqual(st, expect_st)
        self.assertEqual(dec, expect_dec)
        con = uidb.connect(self.dir)
        hist = con.execute("SELECT finding_id, word, old_status FROM history "
                           "WHERE action = 'legacy_recheck'").fetchall()
        uidb.migrate_legacy_decisions(con, self.dir)
        con.close()
        # logged against the re-scanned row, as a full refresh would
        self.assertEqual([tuple(h) for h in hist],
                         [(ids['פרץ'], 'פרץ', 'approved')])
        self.assertEqual(self._ui_state()[:2], (expect_st, expect_dec))
        # the re-scan removed this book's own history; its drop is not an
        # undo step either (else undo would approve the re-scanned row)
        con = uidb.connect(self.dir)
        self.assertIsNone(uidb.undo(con, self.dir))
        con.close()
        self.assertIsNone(self._ui_state()[0]['פרץ'])

    def _undo_all(self):
        from magiah.webui import db as uidb
        con = uidb.connect(self.dir)
        entries = []
        try:
            for _ in range(50):
                res = uidb.undo(con, self.dir)
                if res is None:
                    break
                entries += res['entries']
        finally:
            con.close()
        return entries

    def test_undo_never_restores_a_dropped_legacy_approval(self):
        from magiah.webui import db as uidb
        self._seed_ui()
        uidb.import_all(self.dir)
        con = uidb.connect(self.dir)
        res = uidb.undo(con, self.dir)
        con.close()
        # the user's own last action (ירח -> fixed), not the refresh's drop
        self.assertEqual([(e['word'], e['restored']) for e in res['entries']],
                         [('ירח', 'pending')])
        self.assertEqual(self._ui_state()[0],
                         dict(self.EXPECT_ST, ירח=None))
        # undoing everything walks back the user's actions only
        entries = self._undo_all()
        self.assertNotIn('approved', [e['restored'] for e in entries])
        st, dec, n, _ = self._ui_state()
        self.assertEqual(set(st.values()), {None})
        self.assertEqual(dec, {})
        self.assertEqual(n, 2)

    def test_locked_decisions_db_changes_nothing_and_heals(self):
        """decisions.db locked by another program (a read transaction, or a
        pending write): the refresh fails in Hebrew and changes nothing; the
        next refresh drops and withdraws as usual."""
        from unittest import mock
        from magiah.webui import db as uidb
        self._seed_ui()
        base = self.dir
        try:
            for mode in ('BEGIN', 'BEGIN IMMEDIATE'):
                with self.subTest(lock=mode):
                    self.dir = os.path.join(base, mode.replace(' ', '_'))
                    os.makedirs(self.dir)
                    for f in ('report.db', 'ui_review.db', 'decisions.db'):
                        shutil.copy(os.path.join(base, f), self.dir)
                    before = self._ui_state()
                    self.assertEqual(before[1], self.SEED_DEC)
                    holder = sqlite3.connect(
                        os.path.join(self.dir, 'decisions.db'),
                        isolation_level=None)
                    try:
                        holder.execute(mode)
                        holder.execute('SELECT * FROM decisions').fetchall()
                        with mock.patch.object(uidb, 'DECISIONS_TIMEOUT', 0.2):
                            with self.assertRaises(uidb.DecisionsLocked) as cm:
                                uidb.import_all(self.dir)
                    finally:
                        holder.execute('ROLLBACK')
                        holder.close()
                    self.assertIn('decisions.db', str(cm.exception))
                    self.assertIsInstance(cm.exception, PermissionError)
                    # nothing dropped, nothing withdrawn, ownership intact
                    self.assertEqual(self._ui_state(), before)
                    con = uidb.connect(self.dir)
                    owned = set(tuple(r) for r in con.execute(
                        'SELECT word, unit FROM owned_decisions'))
                    con.close()
                    self.assertTrue(set(self.SEED_DEC) <= owned)
                    # the lock is gone: the next refresh does it all
                    counts = uidb.import_all(self.dir)
                    self.assertEqual(counts['legacy_approvals_dropped'], 2)
                    self.assertEqual(counts['legacy_decisions_withdrawn'], 2)
                    con = uidb.connect(self.dir)
                    uidb.migrate_legacy_decisions(con, self.dir)
                    con.close()
                    self.assertEqual(self._ui_state()[:3],
                                     (self.EXPECT_ST, self.EXPECT_DEC, 2))
        finally:
            self.dir = base


class BibleBookShareTest(unittest.TestCase):
    """The 90% heRef threshold is exact: 90% passes at every book size."""

    def _books(self, parsed, total):
        from magiah import tanach
        con = sqlite3.connect(':memory:')
        con.executescript('''
            CREATE TABLE category(id INTEGER PRIMARY KEY, parentId INT,
                                  title TEXT);
            CREATE TABLE book(id INTEGER PRIMARY KEY, categoryId INT,
                              sourceId INT, title TEXT);
            CREATE TABLE line(id INTEGER PRIMARY KEY, bookId INT,
                              heRef TEXT);''')
        con.executemany('INSERT INTO category VALUES(?,?,?)',
                        [(ROOT, None, 'תנ״ך'), (TORAH, ROOT, 'תורה')])
        con.execute('INSERT INTO book VALUES(1, ?, 1, ?)', (TORAH, TITLE))
        refs = ([f'{TITLE}, א, א'] * parsed
                + [f'{TITLE} הקדמה'] * (total - parsed))
        con.executemany('INSERT INTO line(bookId, heRef) VALUES(1, ?)',
                        [(r,) for r in refs])
        books = tanach.find_bible_books(con)[0]
        con.close()
        return books

    def test_exactly_ninety_percent_is_enough(self):
        for total in (10, 30, 70, 130, 1000, 4370):
            with self.subTest(total=total):
                self.assertEqual(len(self._books(total * 9 // 10, total)), 1)
                self.assertEqual(self._books(total * 9 // 10 - 1, total), [])


class ReportWriteFailureTest(_DBCase):
    """A failed report.db write leaves the last good report.db and no
    report.db.tmp behind; a report.db held open is a Hebrew StageError."""

    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.dir, 'out')
        os.makedirs(self.out)
        lex = {w: 1000 for w in _fixture_words()}
        for w in FLAGGED:
            lex[w] = 1
        with open(os.path.join(self.out, core.LEXICON_F), 'wb') as f:
            pickle.dump(lex, f)
        with open(os.path.join(self.out, core.FLAGGED_F), 'wb') as f:
            pickle.dump({w: (1, et, s, 1000, 3.0)
                         for w, (et, s) in FLAGGED.items()}, f)
        self._run()
        self.report = os.path.join(self.out, core.REPORT_DB_F)
        with open(self.report, 'rb') as f:
            self.before = f.read()

    def _run(self):
        spec = {'type': 'sqlite', 'path': self.db, 'table': 'line',
                'id_col': 'id', 'text_col': 'content', 'preset': 'otzaria'}
        core.locate(spec, Config(workers=1, n_chunks=2), self.out)

    def _assert_untouched(self):
        with open(self.report, 'rb') as f:
            self.assertEqual(f.read(), self.before)
        self.assertFalse(os.path.exists(self.report + '.tmp'))

    def test_failed_write_removes_tmp(self):
        from unittest import mock
        with mock.patch.object(core.tanach, 'write_evidence',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._run()
        self._assert_untouched()

    def test_report_in_use_is_a_hebrew_stage_error(self):
        from unittest import mock
        real = os.replace

        def replace(src, dst):
            if os.path.basename(dst) == core.REPORT_DB_F:
                raise PermissionError(13, 'in use', dst)
            return real(src, dst)
        with mock.patch.object(core.os, 'replace', side_effect=replace):
            with self.assertRaises(core.StageError) as cm:
                self._run()
        self.assertNotIsInstance(cm.exception, core.PartialRead)
        self.assertIn('פתוח בתוכנה אחרת', str(cm.exception))
        self._assert_untouched()

    @unittest.skipUnless(os.name == 'nt', 'Windows file locking')
    def test_report_open_elsewhere_on_windows(self):
        holder = sqlite3.connect(self.report)
        try:
            holder.execute('SELECT COUNT(*) FROM errors').fetchone()
            with self.assertRaises(core.StageError):
                self._run()
        finally:
            holder.close()
        self._assert_untouched()


class PartialTanachReadTest(_DBCase):
    """A Tanach index built from a partial read never feeds report.db."""

    def _run(self, out):
        spec = {'type': 'sqlite', 'path': self.db, 'table': 'line',
                'id_col': 'id', 'text_col': 'content', 'preset': 'otzaria'}
        core.locate(spec, Config(workers=1, n_chunks=2), out)

    def test_unreadable_version_row_stops_locate_and_keeps_report(self):
        out = os.path.join(self.dir, 'out')
        os.makedirs(out)
        lex = {w: 1000 for w in _fixture_words()}
        for w in FLAGGED:
            lex[w] = 1
        with open(os.path.join(out, core.LEXICON_F), 'wb') as f:
            pickle.dump(lex, f)
        with open(os.path.join(out, core.FLAGGED_F), 'wb') as f:
            pickle.dump({w: (1, et, s, 1000, 3.0)
                         for w, (et, s) in FLAGGED.items()}, f)
        self._run(out)
        report = os.path.join(out, core.REPORT_DB_F)
        with open(report, 'rb') as f:
            before = f.read()
        # a version row only the Tanach index reads (the main pass skips
        # versions): a BLOB in a database without a zstd dictionary
        con = sqlite3.connect(self.db)
        con.execute("UPDATE version_line SET content = X'00FF' "
                    "WHERE versionId = 2 AND content IS NOT NULL "
                    "AND lineId = (SELECT MIN(lineId) FROM version_line "
                    "WHERE versionId = 2 AND content IS NOT NULL)")
        con.commit()
        con.close()
        with self.assertRaises(core.PartialRead) as cm:
            self._run(out)
        self.assertIn('לא הצליח לקרוא 1 שורות', str(cm.exception))
        with open(report, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(report + '.tmp'))
        with open(os.path.join(out, core.COVERAGE_F.format(stage='locate')),
                  encoding='utf-8') as f:
            cov = json.load(f)
        self.assertFalse(cov['complete'])
        self.assertEqual(cov['passes']['tanach_index']['decode_errors'], 1)
        # ...and the consumers refuse the older report.db
        with self.assertRaises(core.PartialRead):
            core.report(Config(), out)


class EditionRankTest(unittest.TestCase):
    def test_only_resolved_edition_variants_rank(self):
        from magiah.webui import db as uidb
        d = tempfile.mkdtemp(prefix='magiah_tanach_ed_')
        try:
            path = os.path.join(d, 'report.db')
            make_legacy_report(path)
            con = sqlite3.connect(path)
            con.executescript('''
                DROP TABLE tanach_errors_full;
                CREATE TABLE tanach_errors_full(word, canonical, source, ref,
                                                unit, snippet, origin,
                                                evidence);''')
            con.executemany(
                'INSERT INTO tanach_errors_full VALUES(?,?,?,?,?,?,?,?)', [
                    ('אדוס', 'אדום', 'ס', 'ר', '9', 's', 'Sefaria', json.dumps(
                        {'evidence_kind': 'tanach_edition_variant'})),
                    ('לאת', '', 'ס', 'ר', '12', 's', 'Sefaria', json.dumps(
                        {'evidence_kind': 'tanach_edition_unresolved',
                         'reason': 'one_against_one'}))])
            con.commit()
            con.close()
            uidb.import_all(d)
            con = uidb.connect(d)
            rank = dict(con.execute(
                "SELECT word, rank FROM findings WHERE family='tanach_error'"))
            con.close()
            self.assertEqual(rank, {'אדוס': 4.0, 'לאת': 0.0})
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
