# -*- coding: utf-8 -*-
"""Detection consistency: full pipeline vs single-book scan, split evidence,
per-occurrence OCR suggestions, evidence kinds, calibration provenance."""
import csv
import json
import math
import os
import pickle
import shutil
import sqlite3
import tempfile
import time
import tracemalloc
import unittest

from magiah import book_scan, core
from magiah.book_source import BookText
from magiah.config import Config

LIB_TOP = 'Src'
BOOK_A = f'{LIB_TOP}/a.txt'
BOOK_D = f'{LIB_TOP}/d.txt'

# The book under test. Every line exercises one rule.
A_LINES = (
    ['הילד ראה ספד גדול',          # edit1_sub (ספר), context-verified
     'ראה ספר טוב',                # book-local context for the same pair
     'בכלמקום יש עץ']              # missing space, DP picks the wrong split
    + ['שלוםעליכם אמר הילד'] * 3   # final letter mid-word, seen 3 times
    + ['כןס גדול היה שם'] * 4      # final letter mid-word, no split, 4 times
    + ['דםק הוא']                  # structural suspicion, no suggestion
    + ['אדכל טוב'] * 2)            # OCR-profile word (pair ד->ר here)
D_LINES = ['אדכל טוב', 'הלך הילד']  # same word, other profile (כ->ב)
B_LINES = (['הילד ראה ספר גדול'] * 6
           + ['בכל מקום יש עץ'] * 4
           + ['הלך למקום בכ'] * 6
           + ['שלום עליכם אמר הילד'] * 10
           + ['כנס גדול היה שם'] * 4
           + ['ארכל טוב'] * 6
           + ['אדבל טוב'] * 6)

PROFILES = {BOOK_A: {'דר': 8}, BOOK_D: {'כב': 8}}


def small_cfg(**kw):
    base = dict(rare_max=2, common_min=3, part_min=3, join_min=3,
                ed1_ratio=3, split_obs_min=1, split_obs_min_short=1,
                exp_prefilter=0.0, foreign_ratio=1.0, ocr_pair_min=1,
                ocr_rare_max=5, ocr_ratio=2, workers=1, n_chunks=2)
    base.update(kw)
    return Config(**base)


def write_lib(root, books):
    for rel, lines in books.items():
        p = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')


def write_profiles(out, profiles, source='human_review'):
    with open(os.path.join(out, 'ocr_profiles.pkl'), 'wb') as f:
        pickle.dump(profiles, f)
    core.write_calibration_meta(out, {'source': source})


def run_full(spec, cfg, out):
    core.build_lexicon(spec, cfg, out)
    core.detect(spec, cfg, out)
    core.locate(spec, cfg, out)
    core.report(cfg, out)


def report_rows(out, doc=None):
    con = sqlite3.connect(os.path.join(out, core.REPORT_DB_F))
    con.row_factory = sqlite3.Row
    sql = 'SELECT * FROM occurrences_full'
    rows = [dict(r) for r in (con.execute(sql + ' WHERE doc = ?', (doc,))
                              if doc else con.execute(sql))]
    con.close()
    return rows


class PipelineFixture(unittest.TestCase):
    """One full run over a small library, shared by the tests below."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='magiah_det_')
        cls.lib = os.path.join(cls.tmp, 'lib')
        cls.out = os.path.join(cls.tmp, 'out')
        os.makedirs(cls.out)
        write_lib(cls.lib, {BOOK_A: A_LINES, BOOK_D: D_LINES,
                            f'{LIB_TOP}/b.txt': B_LINES})
        cls.spec = {'type': 'library', 'path': cls.lib}
        cls.cfg = small_cfg()
        write_profiles(cls.out, PROFILES)
        run_full(cls.spec, cls.cfg, cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows_for(self, word, doc=BOOK_A):
        return [r for r in report_rows(self.out, doc) if r['word'] == word]

    def scan_a(self, verify_ctx):
        return book_scan.scan_book(self.out, 'library', BOOK_A, cfg=self.cfg,
                                   library_dir=self.lib,
                                   verify_ctx=verify_ctx, spec=self.spec)


class FullVsBookParity(PipelineFixture):
    """T1: same evidence scope -> same findings."""

    KEYS = ('word', 'errtype', 'suggestion', 'ctx_hits', 'sugg_local',
            'book_repeat', 'unit', 'snippet', 'evidence')

    def _norm(self, rows):
        return sorted(tuple(r[k] for k in self.KEYS)
                      + (round(r['score'], 6),) for r in rows)

    def test_book_scan_in_corpus_scope_equals_full_pipeline(self):
        full = report_rows(self.out, BOOK_A)
        res = self.scan_a(verify_ctx=True)
        self.assertEqual(res['ctx_scope'], 'corpus')
        self.assertEqual(res['split_scope'], 'corpus')
        self.assertTrue(full)
        self.assertEqual(self._norm(full), self._norm(res['findings']))

    def test_space_errors_agree(self):
        con = sqlite3.connect(os.path.join(self.out, core.REPORT_DB_F))
        full = sorted(con.execute(
            'SELECT part1, part2, joined, join_freq, unit, snippet '
            "FROM space_errors_full WHERE unit LIKE ?", (f'file:{BOOK_A}:%',)))
        con.close()
        res = self.scan_a(verify_ctx=False)
        book = sorted((r['part1'], r['part2'], r['joined'], r['join_freq'],
                       r['unit'], r['snippet']) for r in res['space_errors'])
        self.assertEqual(full, book)

    def test_book_scope_is_labelled_as_book_scope(self):
        res = self.scan_a(verify_ctx=False)
        self.assertEqual(res['ctx_scope'], 'book')
        self.assertEqual(res['split_scope'], 'book')
        sub = [r for r in res['findings'] if r['word'] == 'ספד']
        self.assertEqual(len(sub), 1)
        self.assertEqual(sub[0]['evidence'], 'context_book')
        full = self.rows_for('ספד')
        self.assertEqual(full[0]['evidence'], 'context')

    def test_no_reader_uses_str_splitlines(self):
        import inspect
        for mod in (core, book_scan):
            self.assertNotIn('.splitlines(', inspect.getsource(mod))


class SplitAlternatives(PipelineFixture):
    """T4: several segmentations are verified, the evidenced one wins."""

    def test_full_pipeline_finds_the_observed_split(self):
        rows = self.rows_for('בכלמקום')
        self.assertEqual([(r['errtype'], r['suggestion']) for r in rows],
                         [('missing_space', 'בכל מקום')])
        self.assertEqual(rows[0]['evidence'], 'split_observed')

    def test_alternatives_are_kept_with_their_counts(self):
        con = sqlite3.connect(os.path.join(self.out, core.REPORT_DB_F))
        alts = {p: (obs, chosen) for p, obs, chosen in con.execute(
            'SELECT parts, observed, chosen FROM split_alternatives '
            'WHERE word = ?', ('בכלמקום',))}
        con.close()
        self.assertEqual(alts['בכל מקום'], (4, 1))
        self.assertEqual(alts['בכ למקום'], (0, 0))

    def test_segmentations_lists_every_two_part_split(self):
        freq = {'בכ': 6, 'למקום': 6, 'בכל': 4, 'מקום': 5}
        segs = core.segmentations('בכלמקום', freq, small_cfg())
        self.assertIn(('בכ', 'למקום'), segs)
        self.assertIn(('בכל', 'מקום'), segs)


class StructuralAboveRarity(PipelineFixture):
    """T5: structural rules are not gated by the rarity threshold."""

    def test_final_letter_split_seen_three_times(self):
        rows = self.rows_for('שלוםעליכם')
        self.assertEqual(len(rows), 3)
        self.assertEqual({(r['errtype'], r['suggestion']) for r in rows},
                         {('missing_space', 'שלום עליכם')})

    def test_final_letter_midword_seen_four_times(self):
        rows = self.rows_for('כןס')
        self.assertEqual(len(rows), 4)
        self.assertEqual({(r['errtype'], r['suggestion']) for r in rows},
                         {('final_midword', 'כנס')})

    def test_suspicion_never_gets_an_invented_suggestion(self):
        rows = self.rows_for('דםק')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['errtype'], 'final_midword')
        self.assertEqual(rows[0]['suggestion'], '')
        self.assertEqual(rows[0]['evidence'], 'suspicion')

    def test_abbreviations_stay_exempt(self):
        lex = core.Lexicon({'ם"ק': 4, 'מק': 50}, small_cfg(), {})
        recs, splits = core.evaluate_word('ם"ק', 4, lex,
                                          core.edit1_probe(lex))
        self.assertEqual((recs, splits), ([], []))


class OcrProfilePerOccurrence(PipelineFixture):
    """T2: one word, two books, two profiled suggestions."""

    def test_each_book_keeps_its_own_suggestion(self):
        a = self.rows_for('אדכל', BOOK_A)
        d = self.rows_for('אדכל', BOOK_D)
        self.assertEqual({r['suggestion'] for r in a}, {'ארכל'})
        self.assertEqual({r['suggestion'] for r in d}, {'אדבל'})
        self.assertTrue(all(r['errtype'] == 'ocr_profile' for r in a + d))


class EvidenceColumns(PipelineFixture):
    """T5: the evidence kind is exposed in report.db and the CSV."""

    def test_csv_has_evidence_column(self):
        with open(os.path.join(self.out, 'errors_edit1_sub.csv'),
                  encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertIn('evidence', rows[0])
        self.assertEqual({r['evidence'] for r in rows if r['word'] == 'ספד'},
                         {'context'})

    def test_verified_csv_requires_real_support(self):
        path = os.path.join(self.out, 'errors_edit1_sub_verified.csv')
        with open(path, encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows)
        self.assertTrue(all(r['evidence'] != 'none' for r in rows))


class ThreePartSplitEvidence(unittest.TestCase):
    """T3: A-B and B-C seen apart never confirm the sequence A B C."""

    FREQ = {'אבג': 200, 'דהו': 200, 'זחט': 200, 'אבגדהוזחט': 1}

    def _scan(self, lines):
        tmp = tempfile.mkdtemp(prefix='magiah_3p_')
        try:
            with open(os.path.join(tmp, core.LEXICON_F), 'wb') as f:
                pickle.dump(self.FREQ, f)
            path = os.path.join(tmp, 'book.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            res = book_scan.scan_book(tmp, 'file', path, cfg=small_cfg(),
                                      library_dir=os.path.join(tmp, 'nolib'))
            return [r for r in res['findings'] if r['word'] == 'אבגדהוזחט']
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_separate_pairs_do_not_confirm(self):
        rows = self._scan(['אבג דהו', 'דהו זחט', 'אבגדהוזחט'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['evidence'], 'none')
        self.assertAlmostEqual(rows[0]['score'], 2.5)

    def test_contiguous_sequence_confirms(self):
        rows = self._scan(['אבג דהו זחט', 'אבגדהוזחט'])
        self.assertEqual(rows[0]['evidence'], 'split_observed_book')

    def test_shared_sequence_counter(self):
        seq = core.SeqCounter([('א', 'ב', 'ג'), ('א', 'ב')])
        counts = {}
        seq.count(['א', 'ב', 'ד', 'ב', 'ג'], counts)
        self.assertEqual(counts, {1: 1})
        seq.count(['א', 'ב', 'ג'], counts)
        self.assertEqual(counts, {0: 1, 1: 2})


class Edit1Generators(unittest.TestCase):
    """The full scan's deletion index and the book scan's probing must
    produce identical findings (shared evaluation, two candidate sources)."""

    def test_index_and_probe_agree(self):
        freq = {'ספר': 40, 'ספד': 1, 'גדול': 40, 'גדל': 1, 'גודל': 40,
                'בית': 40, 'בות': 1, 'ביית': 1, 'תיב': 1, 'ביתה': 40,
                'שלום': 40, 'שלומ': 1, 'שולם': 1}
        lex = core.Lexicon(freq, small_cfg(), {})
        idx, probe = core.Edit1Index(lex), core.edit1_probe(lex)
        for w, fw in freq.items():
            self.assertEqual(core.evaluate_word(w, fw, lex, idx),
                             core.evaluate_word(w, fw, lex, probe), w)


class LongLineGuard(unittest.TestCase):
    """T6: a 2 MB single line is scanned whole, in linear time, and only
    suspect words carry a stored context."""

    def test_two_megabyte_line(self):
        tmp = tempfile.mkdtemp(prefix='magiah_long_')
        try:
            freq = {'שלום': 10 ** 6, 'בית': 10 ** 6, 'ספר': 1000, 'ספד': 1}
            with open(os.path.join(tmp, core.LEXICON_F), 'wb') as f:
                pickle.dump(freq, f)
            reps = 2 * 1024 * 1024 // len('שלום בית ')
            line = 'ספד ' + 'שלום בית ' * reps + 'ספד'
            path = os.path.join(tmp, 'long.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(line + '\n')
            tracemalloc.start()
            t0 = time.time()
            res = book_scan.scan_book(tmp, 'file', path, cfg=Config(),
                                      library_dir=os.path.join(tmp, 'nolib'))
            elapsed = time.time() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            hits = [r for r in res['findings'] if r['word'] == 'ספד']
            self.assertEqual(len(hits), 2)              # nothing truncated
            self.assertTrue(all(len(r['snippet']) <= 100 for r in hits))
            self.assertLess(elapsed, 30)
            # tokens of the line are held transiently; per-token contexts
            # (~0.47M snippets) would push this far past the bound
            self.assertLess(peak, 150 * 1024 * 1024, peak)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CalibrationProvenance(unittest.TestCase):
    """T5: calibration learns only from human-reviewed decisions."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix='magiah_cal_')
        con = sqlite3.connect(os.path.join(self.out, core.REPORT_DB_F))
        con.execute('CREATE TABLE occurrences_full(word, suggestion, errtype, '
                    'ctx_hits, score, sugg_local, doc)')
        con.executemany('INSERT INTO occurrences_full VALUES(?,?,?,?,?,?,?)',
                        [('ספד', 'ספר', 'edit1_sub', 5, 6.0, 0, 'd1')] * 3)
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def _learned(self):
        p = os.path.join(self.out, 'confusion_learned.json')
        if not os.path.exists(p):
            return None
        with open(p, encoding='utf-8') as f:
            return json.load(f)

    def _review(self, rows, decided_by=None):
        from magiah.webui import db as uidb
        con = uidb.connect(self.out)
        if decided_by is not None:
            con.execute('ALTER TABLE review ADD COLUMN decided_by TEXT')
        for i, (word, sugg, status, doc, who) in enumerate(rows, 1):
            con.execute('INSERT INTO findings(id, family, errtype, word, '
                        'suggestion, doc, verified) '
                        "VALUES(?, 'error', 'edit1_sub', ?, ?, ?, 0)",
                        (i, word, sugg, doc))
            if decided_by is None:
                con.execute('INSERT INTO review(finding_id, status, '
                            "updated_at) VALUES(?, ?, 'x')", (i, status))
            else:
                con.execute('INSERT INTO review(finding_id, status, '
                            "updated_at, decided_by) VALUES(?, ?, 'x', ?)",
                            (i, status, who))
        con.commit()
        con.close()

    def test_machine_findings_alone_teach_nothing(self):
        info = core.calibrate(small_cfg(), self.out)
        self.assertEqual(info['source'], 'none')
        self.assertIsNone(self._learned())

    def test_explicit_machine_flag_is_labelled_unreviewed(self):
        info = core.calibrate(small_cfg(), self.out, from_machine=True)
        self.assertEqual(info['source'], 'machine_unreviewed')
        self.assertEqual(self._learned(), {'דר': 1})
        learned, source = core.load_learned(self.out)
        self.assertEqual(source, 'machine_unreviewed')

    def test_learns_from_human_review_only(self):
        self._review([('בות', 'בית', 'approved', 'd1', None),
                      ('גדוך', 'גדול', 'not_error', 'd1', None),
                      ('חבר', 'חכר', 'pending', 'd1', None)])
        info = core.calibrate(small_cfg(), self.out)
        self.assertEqual(info['source'], 'human_review')
        self.assertEqual(self._learned(), {'וי': 1})

    def test_decided_by_column_filters_machine_decisions(self):
        self._review([('בות', 'בית', 'approved', 'd1', 'human'),
                      ('ספד', 'ספר', 'approved', 'd1', 'machine')],
                     decided_by=True)
        core.calibrate(small_cfg(), self.out)
        self.assertEqual(self._learned(), {'וי': 1})

    def test_legacy_learned_file_without_provenance_is_ignored(self):
        with open(os.path.join(self.out, 'confusion_learned.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'דר': 50}, f)
        self.assertEqual(core.load_learned(self.out), ({}, None))


class EvaluationHarness(unittest.TestCase):

    def test_split_is_by_doc_and_deterministic(self):
        from magiah import eval as ev
        rows = [{'doc': f'd{i % 7}', 'rank': i, 'label': i % 2,
                 'errtype': 'edit1_sub', 'word': 'בות', 'suggestion': 'בית'}
                for i in range(70)]
        a1, b1 = ev.split_by_doc(rows)
        a2, b2 = ev.split_by_doc(rows)
        self.assertEqual((a1, b1), (a2, b2))
        self.assertFalse({r['doc'] for r in a1} & {r['doc'] for r in b1})
        self.assertEqual(len(a1) + len(b1), 70)

    def test_precision_on_held_out_half(self):
        from magiah import eval as ev
        rows = []
        for d in range(10):
            rows.append({'doc': f'd{d}', 'rank': 9.0, 'label': 1,
                         'errtype': 'edit1_sub', 'word': 'בות',
                         'suggestion': 'בית'})
            rows.append({'doc': f'd{d}', 'rank': 1.0, 'label': 0,
                         'errtype': 'edit1_sub', 'word': 'גדוך',
                         'suggestion': 'גדול'})
        rep = ev.evaluate(rows, ks=(1, 100))
        held = rep['held_out']
        self.assertEqual(held['n'], len(rep['held_out_rows']))
        self.assertEqual(held['precision_at'][1], 1.0)
        self.assertAlmostEqual(held['precision_at'][100], 0.5)

    def test_no_labels_says_so(self):
        from magiah import eval as ev
        tmp = tempfile.mkdtemp()
        try:
            rep = ev.run(tmp)
            self.assertEqual(rep['status'], 'no_labels')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# review round: evidence kinds, split choice, calibration hygiene, eval
# ---------------------------------------------------------------------------

class StructuralEvidenceKinds(PipelineFixture):

    def test_structural_rules_are_labelled(self):
        self.assertEqual({r['evidence'] for r in self.rows_for('כןס')},
                         {'structural'})

    def test_ocr_profile_rows_are_labelled(self):
        self.assertEqual({r['evidence'] for r in self.rows_for('אדכל')},
                         {'ocr_profile'})

    def test_kinds(self):
        ek = core.evidence_kind
        self.assertEqual(ek('nonfinal_end', 'אדם', 90, 0, 0, 0), 'structural')
        self.assertEqual(ek('lost_quotes', 'רמב"ם', 90, 0, 0, 0),
                         'structural')
        self.assertEqual(ek('final_midword', 'כנס', 9, 0, 0, 0), 'structural')
        self.assertEqual(ek('final_midword', '', 0, 0, 0, 0), 'suspicion')
        self.assertEqual(ek('ocr_profile', 'ארכל', 6, 0, 0, 0), 'ocr_profile')
        self.assertEqual(ek('edit1_sub', 'ספר', 9, 0, 0, 3), 'tanach')
        self.assertEqual(ek('edit1_sub', 'ספר', 9, 0, 0, 4), 'none')

    def test_verse_reading_does_not_fill_an_empty_suggestion(self):
        suspicion = (1, 'final_midword', '', 0, 3.0)
        self.assertEqual(core.occurrence_evidence(suspicion, 0, 0, 4),
                         'suspicion')
        self.assertEqual(core.occurrence_evidence(suspicion, 0, 0, 3),
                         'tanach')
        structural = (1, 'final_midword', 'כנס', 9, 5.0)
        self.assertEqual(core.occurrence_evidence(structural, 0, 0, 4),
                         'structural')


def _lex(freq, **kw):
    return core.Lexicon(freq, small_cfg(**kw), {})


class SplitChoiceByAssociation(unittest.TestCase):
    FILL = {'ספר': 10 ** 6}

    def test_better_associated_alternative_wins(self):
        freq = {'בכ': 1000, 'למקום': 1000, 'בכל': 50, 'מקום': 50,
                **self.FILL}
        lex = _lex(freq)
        a, b = ('בכ', 'למקום'), ('בכל', 'מקום')
        best, _ = core.resolve_splits([(a, False), (b, False)], [5, 4],
                                      lex, 'corpus')
        self.assertEqual(best[0], b)

    def test_frequent_pair_near_chance_is_confirmed(self):
        # real misses of a chance gate (spaced count vs expected, Otzaria
        # subset): two frequent words glued together are as common as
        # chance spaced, and are still a missing space
        cases = [(('אבל', 'נראה'), 32, 34.9), (('והנה', 'כתב'), 6, 6.9),
                 (('ונראה', 'שלא'), 7, 7.4), (('אבל', 'עיין'), 17, 27.0),
                 (('אלא', 'דאי'), 17, 19.0)]
        n = 10 ** 7
        for parts, obs, exp in cases:
            f = round(math.sqrt(exp * n))
            lex = _lex({parts[0]: f, parts[1]: f, 'ספר': n - 2 * f},
                       split_obs_min=3)
            self.assertGreater(lex.expected(parts), obs, parts)
            best, alts = core.resolve_splits([(parts, False)], [obs], lex,
                                             'corpus')
            self.assertIsNotNone(best, parts)
            self.assertEqual(best[0], parts)
            self.assertTrue(alts[0][3])

    def test_below_split_obs_min_is_not_confirmed(self):
        lex = _lex({'גדול': 50, 'מאוד': 50, **self.FILL}, split_obs_min=3)
        best, alts = core.resolve_splits([(('גדול', 'מאוד'), False)], [2],
                                         lex, 'corpus')
        self.assertIsNone(best)
        self.assertFalse(alts[0][3])

    def test_short_part_still_needs_twice_chance(self):
        lex = _lex({'בכ': 10 ** 5, 'למקום': 10 ** 5, **self.FILL},
                   split_obs_min_short=1)
        parts = ('בכ', 'למקום')
        obs = int(1.5 * lex.expected(parts))
        best, _ = core.resolve_splits([(parts, False)], [obs], lex, 'corpus')
        self.assertIsNone(best)

    def test_strong_split_needs_no_association(self):
        freq = {'שלום': 10 ** 5, 'עליכם': 10 ** 5, **self.FILL}
        lex = _lex(freq)
        best, _ = core.resolve_splits([(('שלום', 'עליכם'), True)], [2], lex,
                                      'corpus')
        self.assertIsNotNone(best)


class WeakSplitKeepsCorrection(unittest.TestCase):
    FREQ = {'גדול': 10 ** 4, 'מאוד': 10 ** 4, 'ספר': 10 ** 6}

    def _settle(self, obs):
        lex = _lex(self.FREQ, split_obs_min=3)
        errors = {'גדולמאוד': (1, 'edit1_sub', 'גדולמאוז', 40, 3.0)}
        best = (('גדול', 'מאוד'), obs, 9.0)
        role = core.settle_split(errors, 'גדולמאוד', 1, best, lex, 'corpus')
        return role, errors['גדולמאוד']

    def test_weak_split_stays_an_alternative(self):
        role, rec = self._settle(obs=4)        # exp ~ 98: no association
        self.assertEqual(role, 'alternative')
        self.assertEqual(rec[1], 'edit1_sub')

    def test_strong_evidence_split_takes_over(self):
        role, rec = self._settle(obs=5000)
        self.assertEqual(role, 'primary')
        self.assertEqual(rec[1], 'missing_space')

    def test_split_without_competitor_is_primary(self):
        lex = _lex(self.FREQ)
        errors = {}
        role = core.settle_split(errors, 'גדולמאוד', 1,
                                 (('גדול', 'מאוד'), 4, 5.0), lex, 'corpus')
        self.assertEqual(role, 'primary')


class CalibrationHygiene(CalibrationProvenance):

    def test_calibrate_without_human_rows_disables_old_files(self):
        core.calibrate(small_cfg(), self.out, from_machine=True)
        self.assertEqual(core.load_learned(self.out)[1], 'machine_unreviewed')
        info = core.calibrate(small_cfg(), self.out)
        self.assertEqual(info['source'], 'none')
        self.assertEqual(core.load_learned(self.out), ({}, None))
        self.assertEqual(core.load_ocr_profiles(self.out), ({}, None))

    def test_edited_learned_file_is_ignored(self):
        core.calibrate(small_cfg(), self.out, from_machine=True)
        with open(os.path.join(self.out, 'confusion_learned.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'דר': 999}, f)
        self.assertEqual(core.load_learned(self.out), ({}, None))


class ScannerCalibrateGate(CalibrationProvenance):

    def test_report_alone_does_not_enable_calibrate(self):
        from magiah.webui import scanner
        self.assertFalse(scanner.calibrate_available(self.out))

    def test_human_decisions_enable_calibrate(self):
        from magiah.webui import scanner
        self._review([('בות', 'בית', 'approved', 'd1', None)])
        self.assertTrue(scanner.calibrate_available(self.out))

    def test_label_mentions_review(self):
        from magiah.webui import hebrew
        text = hebrew.STAGE_LABELS['calibrate']['explanation']
        self.assertNotIn('report.db', text)
        self.assertIn('ui_review.db', text)


class EvalWithoutLeakage(unittest.TestCase):

    def _rows(self):
        from magiah import eval as ev
        rows = []
        run_bonus = core.learned_bonus(9)
        for d in range(12):
            doc = f'd{d}'
            # stored score/rank include the run's learned bonus for ד->ר
            rows.append({'doc': doc, 'word': 'ספד', 'suggestion': 'ספר',
                         'machine_suggestion': 'ספר', 'errtype': 'edit1_sub',
                         'score': 1.0 + run_bonus, 'rank': 2.5 + run_bonus,
                         'label': 1})
            rows.append({'doc': doc, 'word': 'כןס', 'suggestion': 'כנס',
                         'machine_suggestion': 'כנס',
                         'errtype': 'final_midword', 'score': 5.0,
                         'rank': 5.0, 'label': 1})
        return ev, rows

    def test_run_bonus_is_removed_and_half_learned_bonus_applied(self):
        ev, rows = self._rows()
        rep = ev.evaluate(rows, ks=(100,), learned_run={'דר': 9})
        calib, held = ev.split_by_doc(rows)
        n_calib_pos = sum(1 for r in calib if r['errtype'] == 'edit1_sub')
        self.assertTrue(held and n_calib_pos)
        self.assertEqual(rep['calibration']['pairs'], {'דר': n_calib_pos})
        for r in rep['held_out_rows']:
            if r['errtype'] != 'edit1_sub':
                continue
            self.assertAlmostEqual(r['rank_default'], 2.5 + 2)  # CONFUSABLE
            self.assertAlmostEqual(r['rank_calibrated'],
                                   2.5 + core.learned_bonus(n_calib_pos))

    def test_only_correction_types_teach_pairs(self):
        ev, rows = self._rows()
        rep = ev.evaluate(rows, ks=(100,))
        self.assertNotIn('ןנ', rep['calibration']['pairs'])

    def test_contamination_is_flagged(self):
        from magiah import eval as ev
        out = tempfile.mkdtemp()
        try:
            from magiah.webui import db as uidb
            con = uidb.connect(out)
            for i in range(1, 21):
                con.execute("INSERT INTO findings(id, family, errtype, word, "
                            "suggestion, score, rank, doc, verified) VALUES"
                            "(?, 'error', 'edit1_sub', 'בות', 'בית', 3, 3, "
                            "?, 0)", (i, f'd{i}'))
                con.execute("INSERT INTO review(finding_id, status, "
                            "updated_at) VALUES(?, ?, 'x')",
                            (i, 'approved' if i % 2 else 'not_error'))
            con.commit()
            con.close()
            self.assertFalse(ev.run(out)['contaminated'])
            core.calibrate(small_cfg(), out)
            self.assertTrue(ev.run(out)['contaminated'])
        finally:
            shutil.rmtree(out, ignore_errors=True)


class EvalGlobalRejections(unittest.TestCase):

    def test_word_rules_and_global_decisions_are_negatives(self):
        from magiah import eval as ev
        from magiah.webui import db as uidb
        out = tempfile.mkdtemp()
        try:
            con = uidb.connect(out)
            for i, w in enumerate(('בות', 'גדוך', 'חבר', 'שמש'), 1):
                con.execute("INSERT INTO findings(id, family, errtype, word, "
                            "suggestion, score, rank, doc, verified) VALUES"
                            "(?, 'error', 'edit1_sub', ?, 'x', 3, 3, 'd', 0)",
                            (i, w))
            con.execute("INSERT INTO review VALUES(1, 'approved', NULL, "
                        "NULL, 'x')")
            con.execute("INSERT INTO word_rules VALUES('גדוך', 'not_error', "
                        "'x')")
            con.commit()
            con.close()
            dec = sqlite3.connect(os.path.join(out, 'decisions.db'))
            dec.execute('CREATE TABLE decisions(word, unit, errtype, verdict, '
                        'suggestion, source, ref)')
            dec.execute("INSERT INTO decisions VALUES('חבר', '*', '', "
                        "'reject', '', '', '')")
            dec.commit()
            dec.close()
            labels = sorted((r['word'], r['label'])
                            for r in ev.load_labels(out))
            self.assertEqual(labels, [('בות', 1), ('גדוך', 0), ('חבר', 0)])
        finally:
            shutil.rmtree(out, ignore_errors=True)


class WorkerMemory(unittest.TestCase):

    def test_split_list_is_freed_after_indexing(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'sc.pkl')
            with open(path, 'wb') as f:
                pickle.dump([('ab', ('א', 'ב'))], f)
            core._init({'type': 'textdir', 'path': tmp},
                       small_cfg().to_dict(), {'split_cands': path})
            self.assertIn('seq', core._W)
            self.assertNotIn('split_cands', core._W)
        finally:
            core._W.clear()
            shutil.rmtree(tmp, ignore_errors=True)

class UnverifiedFinalLetterSplit(unittest.TestCase):
    """A final letter mid-word stays a suspicion when its split is never
    seen spaced (the parts are frequent, but never adjacent)."""

    LINES = (['אמר שלום לכולם וגם עליכם אמרו'] * 12
             + ['ואמר שלוםעליכם לכולם'])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_fin_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        write_lib(self.lib, {BOOK_A: self.LINES})
        self.spec = {'type': 'library', 'path': self.lib}
        self.cfg = small_cfg()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_scan_keeps_the_suspicion(self):
        run_full(self.spec, self.cfg, self.out)
        rows = [r for r in report_rows(self.out) if r['word'] == 'שלוםעליכם']
        self.assertEqual([(r['errtype'], r['suggestion']) for r in rows],
                         [('final_midword', '')])

    def test_book_scan_in_corpus_scope_keeps_the_suspicion(self):
        core.build_lexicon(self.spec, self.cfg, self.out)
        res = book_scan.scan_book(self.out, 'library', BOOK_A, cfg=self.cfg,
                                  library_dir=self.lib, spec=self.spec,
                                  verify_ctx=True)
        rows = [r for r in res['findings'] if r['word'] == 'שלוםעליכם']
        self.assertEqual([(r['errtype'], r['suggestion']) for r in rows],
                         [('final_midword', '')])


class FootnoteMarkerLetter(unittest.TestCase):
    """A footnote marker letter glued to a word before ')' (e.g. 'word' + 'א)')
    is notation, not an extra letter; a real word ending at ')' still counts."""

    def _lex(self):
        return {'אמר': 50, 'לנו': 50, 'שלום': 50, 'דבר': 50}

    def test_marker_before_paren_is_not_an_extra_letter(self):
        flagged = {'שלוםא': (1, 'edit1_ins', 'שלום', 50, 3.0)}
        res = core.locate_line('אמר לנו שלוםא) דבר', self._lex(), small_cfg(),
                               flagged)
        self.assertEqual(res[0], [])

    FREQ = {'ספר': 100, 'גדול': 100, 'ועיין': 100, 'שם': 100, 'בספר': 100,
            'טוב': 100}
    BSFRR = {'בספרר': (1, 'edit1_ins', 'בספר', 100, 3.0)}

    def _words(self, line):
        res = core.locate_line(line, self.FREQ, small_cfg(), self.BSFRR)
        return [o[0] for o in res[0]]

    def test_glued_marker_after_word_is_notation(self):      # KSK style
        self.assertEqual(self._words('ספר טוב בספרר) גדול'), [])

    def test_closed_paren_earlier_does_not_count(self):
        self.assertEqual(self._words('(א) ספר טוב בספרר) גדול'), [])

    def test_extra_letter_inside_parentheses_is_reported(self):
        self.assertEqual(self._words('ועיין שם (בספרר) גדול'), ['בספרר'])
        self.assertEqual(self._words('ועיין (שם בספרר) גדול'), ['בספרר'])

    def test_extra_letter_without_paren_is_reported(self):
        self.assertEqual(self._words('ועיין שם בספרר גדול'), ['בספרר'])

    def test_final_letter_is_not_a_marker(self):
        flagged = {'שלומם': (1, 'edit1_ins', 'שלומ', 50, 3.0)}
        res = core.locate_line('אמר לנו שלומם) דבר', self._lex(),
                               small_cfg(), flagged)
        self.assertEqual([o[0] for o in res[0]], ['שלומם'])

    def test_other_errors_before_paren_are_still_reported(self):
        flagged = {'שלמ': (1, 'nonfinal_end', 'שלם', 50, 3.0)}
        res = core.locate_line('אמר לנו שלמ) דבר', self._lex(), small_cfg(),
                               flagged)
        self.assertEqual([o[0] for o in res[0]], ['שלמ'])


class FrequentPairSplit(unittest.TestCase):
    """Two frequent words glued together are a missing space even when the
    spaced pair is no more common than chance predicts (full and book scan
    alike)."""

    LINES = (['אבל נראה לי'] * 3 + ['אבל אמר'] * 47 + ['נראה שם'] * 47
             + ['אבלנראה לי'])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_pair_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        write_lib(self.lib, {BOOK_A: self.LINES})
        self.spec = {'type': 'library', 'path': self.lib}
        self.cfg = small_cfg(split_obs_min=3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _found(self, rows):
        return [(r['errtype'], r['suggestion'], r['evidence'])
                for r in rows if r['word'] == 'אבלנראה']

    def test_full_and_book_scan_confirm_the_split(self):
        run_full(self.spec, self.cfg, self.out)
        con = sqlite3.connect(os.path.join(self.out, core.REPORT_DB_F))
        obs, exp = con.execute(
            'SELECT observed, expected FROM split_alternatives '
            "WHERE word = 'אבלנראה' AND parts = 'אבל נראה'").fetchone()
        con.close()
        self.assertLess(obs, exp)               # spaced below chance
        want = [('missing_space', 'אבל נראה', 'split_observed')]
        self.assertEqual(self._found(report_rows(self.out)), want)
        res = book_scan.scan_book(self.out, 'library', BOOK_A, cfg=self.cfg,
                                  library_dir=self.lib, spec=self.spec,
                                  verify_ctx=True)
        self.assertEqual(self._found(res['findings']), want)



class TamperedOcrProfiles(unittest.TestCase):
    """An ocr_profiles.pkl that no longer matches calibration_meta.json is
    ignored by the full scan exactly as by the book scan."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_tamper_')
        self.lib = os.path.join(self.tmp, 'lib')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        write_lib(self.lib, {BOOK_A: A_LINES, BOOK_D: D_LINES,
                             f'{LIB_TOP}/b.txt': B_LINES})
        self.spec = {'type': 'library', 'path': self.lib}
        self.cfg = small_cfg()
        write_profiles(self.out, {BOOK_D: {'כב': 8}})
        # replaced after calibration vouched for it: the meta still says
        # human_review, but its hash no longer matches
        with open(os.path.join(self.out, core.OCR_PROFILES_F), 'wb') as f:
            pickle.dump(PROFILES, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_and_book_scan_both_ignore_it(self):
        self.assertEqual(core.load_ocr_profiles(self.out), ({}, None))
        run_full(self.spec, self.cfg, self.out)
        with open(os.path.join(self.out, 'coverage_locate.json'),
                  encoding='utf-8') as f:
            self.assertEqual(json.load(f)['ocr_profiles'], 'none')
        full = report_rows(self.out, BOOK_A)
        self.assertTrue(full)
        self.assertFalse([r for r in report_rows(self.out)
                          if r['errtype'] == 'ocr_profile'])
        res = book_scan.scan_book(self.out, 'library', BOOK_A, cfg=self.cfg,
                                  library_dir=self.lib, spec=self.spec,
                                  verify_ctx=True)
        self.assertEqual(res['calibration']['ocr_profiles'], 'none')
        norm = FullVsBookParity()._norm
        self.assertEqual(norm(full), norm(res['findings']))


if __name__ == '__main__':
    unittest.main()
