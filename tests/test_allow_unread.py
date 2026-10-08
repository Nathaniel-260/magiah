# -*- coding: utf-8 -*-
"""--allow-unread: going on past a bounded number of unreadable rows.

The default is the fail-safe one (any unreadable row stops the stage). With
the option a stage may skip up to N rows; its output is then marked partial —
never complete — and every consumer, the single-book scan and the review UI
included, must either accept that explicitly (the same option, covering as
many rows) or refuse it.
"""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from magiah import core
from magiah.config import Config
from magiah.textsource import OtzariaDB, ReadStats

from test_tanach import BIBLE, make_bible_db
from test_textsource import (_corrupt_row, _coverage, _otzaria_spec, _zstd,
                             make_schema6_db)

# the keys a complete lexicon record had before --allow-unread existed
COMPLETE_LEXICON_KEYS = {'stage', 'complete', 'unread', *ReadStats.FIELDS,
                         'error_samples', 'tokens', 'types', 'chunks'}


def _quiet():
    """Swallow a stage's progress output."""
    return contextlib.redirect_stdout(io.StringIO())


def _report_coverage(out):
    con = sqlite3.connect(os.path.join(out, core.REPORT_DB_F))
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='coverage'").fetchone():
            return None
        return json.loads(con.execute('SELECT info FROM coverage')
                          .fetchone()[0])
    finally:
        con.close()


@unittest.skipIf(_zstd is None, 'needs compression.zstd (Python 3.14+)')
class _Case(unittest.TestCase):
    """A schema-6 database with two unreadable rows: line 2 (book 1) is a
    broken zstd frame and line 4 (book 2) has no line_content row."""

    BAD = {'corrupt_id': 2, 'missing_id': 4}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'seforim.db')
        make_schema6_db(self.db, **self.BAD)
        self.out = os.path.join(self.tmp.name, 'out')
        os.makedirs(self.out)
        self.spec = _otzaria_spec(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, allow_unread=0):
        return Config(workers=1, n_chunks=2, allow_unread=allow_unread)

    def run_stage(self, name, allow_unread=0):
        fn = {'lexicon': core.build_lexicon, 'detect': core.detect,
              'locate': core.locate}[name]
        with _quiet():
            fn(self.spec, self.cfg(allow_unread), self.out)

    def repair_db(self):
        fixed = os.path.join(self.tmp.name, 'fixed.db')
        make_schema6_db(fixed)
        os.replace(fixed, self.db)


class ReaderTest(_Case):
    def test_describe_lines_names_book_and_reference(self):
        with OtzariaDB(self.db) as odb:
            self.assertEqual(odb.describe_lines([2, 4, 99]),
                             {2: ('ספר א', 'ref 2'), 4: ('ספר ב', 'ref 4')})

    def test_unread_refs_locate_rows_and_files(self):
        samples = [('2', 'boom'), ('2', 'again'),
                   ('DictaToOtzaria/ספר ג.txt', 'OSError')]
        refs = core._unread_refs(samples, self.db)
        self.assertEqual(refs, [
            {'unit': '2', 'book': 'ספר א', 'ref': 'ref 2', 'error': 'boom'},
            {'unit': 'DictaToOtzaria/ספר ג.txt', 'book': 'ספר ג', 'ref': '',
             'error': 'OSError'}])

    def test_unread_refs_are_bounded_and_never_crash(self):
        samples = [(str(i), 'x') for i in range(100)]
        refs = core._unread_refs(samples, os.path.join(self.tmp.name, 'no'))
        self.assertEqual(len(refs), core._MAX_REFS)
        self.assertEqual(refs[0]['book'], '')     # db missing: unlocated


class ReadStatsUnitsTest(unittest.TestCase):
    def test_units_are_counted_kept_and_merged(self):
        a, b = ReadStats(), ReadStats()
        a.unread_row(2, 'decode_errors', 'boom')
        b.unread_row('2', 'missing', 'no line_content row')
        b.unread_row('ver:1:3', 'decode_errors', 'boom')
        self.assertEqual((a.decode_errors, b.missing, b.unread()), (1, 1, 2))
        # two passes met row 2: three failed reads, two rows
        self.assertEqual(core._missing_rows({'a': a, 'b': b})[0], 2)
        chunk = ReadStats()                 # chunks of one pass never overlap
        chunk.unread_row(7, 'decode_errors', 'x')
        a.add(chunk)
        self.assertEqual(a.unread_units, {'2', '7'})
        self.assertTrue(a.units_complete())
        back = ReadStats.from_dict(a.to_dict())
        self.assertEqual(back.unread_units, a.unread_units)
        self.assertNotIn('unread_units', ReadStats().to_dict())

    def test_past_the_cap_rows_are_summed_never_undercounted(self):
        with mock.patch.object(ReadStats, 'UNITS_CAP', 2):
            a, b = ReadStats(), ReadStats()
            for i in range(3):
                a.unread_row(i, 'decode_errors', 'x')
                b.unread_row(i, 'decode_errors', 'x')
            self.assertFalse(a.units_complete())
            self.assertEqual(core._missing_rows({'a': a, 'b': b})[0], 6)
            inherited = {'lexicon': {'unread_rows': 3,
                                     'unread_units': ['0', '1']}}
            c = ReadStats()
            c.unread_row(0, 'decode_errors', 'x')
            self.assertEqual(core._missing_rows({'c': c}, inherited)[0], 4)

    def test_inherited_rows_are_united_by_id(self):
        c = ReadStats()
        c.unread_row(5, 'decode_errors', 'x')
        inherited = {'lexicon': {'unread_rows': 2,
                                 'unread_units': ['4', '5']}}
        self.assertEqual(core._missing_rows({'c': c}, inherited)[0], 2)


class ThresholdTest(_Case):
    def test_default_stops_and_names_the_option(self):
        with _quiet(), self.assertRaises(core.PartialRead) as cm:
            core.build_lexicon(self.spec, self.cfg(), self.out)
        msg = str(cm.exception)
        self.assertIn('לא הצליח לקרוא 2 שורות', msg)
        self.assertIn('--allow-unread 2', msg)
        self.assertIn(core.ALLOW_UNREAD_UI, msg)
        self.assertIn('ספר א, ref 2', msg)                # where the rows are
        self.assertFalse(os.path.exists(
            os.path.join(self.out, core.LEXICON_F)))
        cov = _coverage(self.out, 'lexicon')
        self.assertFalse(cov['complete'])
        self.assertFalse(cov['accepted'])
        self.assertEqual((cov['unread_rows'], cov['allow_unread']), (2, 0))

    def test_boundary(self):
        for n, ok in ((1, False), (2, True), (3, True)):
            with self.subTest(allow_unread=n):
                lex = os.path.join(self.out, core.LEXICON_F)
                if os.path.exists(lex):
                    os.remove(lex)
                if ok:
                    self.run_stage('lexicon', n)
                    self.assertTrue(os.path.exists(lex))
                else:
                    with self.assertRaises(core.PartialRead) as cm:
                        self.run_stage('lexicon', n)
                    self.assertIn('אישרה לדלג על 1 שורות', str(cm.exception))
                    self.assertFalse(os.path.exists(lex))
                cov = _coverage(self.out, 'lexicon')
                self.assertEqual(cov['accepted'], ok)
                self.assertEqual(cov['allow_unread'], n)

    def test_accepted_record_is_partial_not_complete(self):
        self.run_stage('lexicon', 2)
        cov = _coverage(self.out, 'lexicon')
        self.assertIs(cov['complete'], False)
        self.assertIs(cov['accepted'], True)
        self.assertEqual(cov['unread_rows'], 2)
        self.assertEqual(cov['unread'], 2)
        self.assertNotIn('inherited', cov)
        self.assertEqual(
            [(r['unit'], r['book'], r['ref']) for r in cov['unread_refs']],
            [('2', 'ספר א', 'ref 2'), ('4', 'ספר ב', 'ref 4')])
        self.assertEqual(cov['unread_units'], ['2', '4'])
        self.assertNotIn('unread_units', cov['passes'] if 'passes' in cov
                         else {})
        self.assertIn('ZstdError', cov['unread_refs'][0]['error'])
        self.assertEqual(cov['unread_refs'][1]['error'], 'no line_content row')

    def test_complete_record_is_unchanged(self):
        self.repair_db()
        self.run_stage('lexicon', 5)                # option given, not needed
        cov = _coverage(self.out, 'lexicon')
        self.assertIs(cov['complete'], True)
        self.assertEqual(set(cov), COMPLETE_LEXICON_KEYS)

    def test_negative_limit_is_the_default(self):
        self.assertEqual(core.unread_limit(Config(allow_unread=-3)), 0)
        self.assertEqual(core.unread_limit(Config(allow_unread='x')), 0)
        with self.assertRaises(core.PartialRead):
            self.run_stage('lexicon', -3)


class PropagationTest(_Case):
    def setUp(self):
        super().setUp()
        self.run_stage('lexicon', 2)

    def test_later_run_without_the_option_refuses(self):
        with _quiet(), self.assertRaises(core.PartialRead) as cm:
            core.detect(self.spec, self.cfg(), self.out)
        msg = str(cm.exception)
        self.assertIn('מילון', msg)
        self.assertIn('לא אישרה', msg)
        self.assertIn('--allow-unread 2', msg)
        self.assertFalse(os.path.exists(
            os.path.join(self.out, core.FLAGGED_F)))

    def test_lower_limit_refuses(self):
        with self.assertRaises(core.PartialRead) as cm:
            self.run_stage('detect', 1)
        self.assertIn('אישרה רק 1', str(cm.exception))

    def test_every_output_downstream_is_marked(self):
        self.run_stage('detect', 2)
        self.run_stage('locate', 2)
        det, loc = _coverage(self.out, 'detect'), _coverage(self.out, 'locate')
        self.assertEqual(det['inherited'], {'lexicon': 2})
        self.assertEqual(loc['inherited'], {'lexicon': 2, 'detect': 2})
        for cov in (det, loc):
            self.assertIs(cov['complete'], False)
            self.assertIs(cov['accepted'], True)
        # report.db carries its own mark, for whatever displays it
        self.assertEqual(_report_coverage(self.out), loc)

        with self.assertRaises(core.PartialRead):
            core.report(self.cfg(), self.out)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            core.report(self.cfg(2), self.out)
        self.assertIn('סריקה חלקית', printed.getvalue())

    def test_inherited_mark_survives_a_complete_upstream_rerun(self):
        # flagged.pkl was derived from the partial lexicon; rebuilding the
        # lexicon on a repaired database does not make it complete
        self.run_stage('detect', 2)
        self.repair_db()
        self.run_stage('lexicon')
        self.assertTrue(_coverage(self.out, 'lexicon')['complete'])
        with self.assertRaises(core.PartialRead) as cm:
            self.run_stage('locate')
        self.assertIn('איתור', str(cm.exception))

    def test_complete_rerun_clears_every_mark(self):
        self.run_stage('detect', 2)
        self.run_stage('locate', 2)
        self.repair_db()
        for st in ('lexicon', 'detect', 'locate'):
            self.run_stage(st)
            self.assertTrue(_coverage(self.out, st)['complete'])
        self.assertIsNone(_report_coverage(self.out))
        with _quiet():
            core.report(self.cfg(), self.out)

    def test_refused_latest_attempt_beats_an_older_accepted_one(self):
        lex = os.path.join(self.out, core.LEXICON_F)
        with open(lex, 'rb') as f:
            accepted = f.read()
        with self.assertRaises(core.PartialRead):
            self.run_stage('lexicon')
        with open(lex, 'rb') as f:
            self.assertEqual(f.read(), accepted)      # not replaced...
        with self.assertRaises(core.PartialRead) as cm:
            self.run_stage('detect', 2)               # ...and not used
        self.assertIn('הריצה האחרונה', str(cm.exception))
        self.assertIn('--allow-unread 2', str(cm.exception))


class RecordValidationTest(_Case):
    """Records written before --allow-unread, or damaged, are never taken for
    an accepted partial output."""

    def write_record(self, info):
        with open(os.path.join(self.out, 'coverage_lexicon.json'), 'w',
                  encoding='utf-8') as f:
            if isinstance(info, str):
                f.write(info)
            else:
                json.dump(info, f)

    def test_legacy_partial_record_is_refused_with_a_hint(self):
        self.write_record({'stage': 'lexicon', 'complete': False,
                           'unread': 3, 'decode_errors': 3})
        msg = core.coverage_problem(self.out, 'lexicon', allow_unread=10)
        self.assertIn('--allow-unread 3', msg)

    def test_incomplete_acceptance_is_refused(self):
        good = {'stage': 'lexicon', 'complete': False, 'unread': 1,
                'accepted': True, 'allow_unread': 1, 'unread_rows': 1,
                'unread_refs': []}
        self.write_record(good)
        self.assertIsNone(core.coverage_problem(self.out, 'lexicon', 1))
        for broken in ({k: v for k, v in good.items() if k != 'unread_rows'},
                       dict(good, allow_unread='1'),
                       dict(good, unread_refs='x'),
                       dict(good, unread_rows='many'),
                       dict(good, unread_rows=-5),
                       dict(good, unread_rows=0),
                       dict(good, accepted='yes')):
            with self.subTest(broken=broken):
                self.write_record(broken)
                self.assertIsNotNone(
                    core.coverage_problem(self.out, 'lexicon', 100))
                self.assertEqual(core.accepted_gaps(self.out, 'lexicon'), {})

    def test_unreadable_record_is_refused(self):
        self.write_record('{not json')
        msg = core.coverage_problem(self.out, 'lexicon', 100)
        self.assertIn('0 שורות', msg)
        self.assertNotIn('--allow-unread', msg)        # no count to offer


class DistinctRowsTest(_Case):
    """One bad row is met by every pass of `locate`; it is still one row."""

    BAD = {'corrupt_id': 2}

    def use_bible_db(self):
        """PR #5's Bible fixture, its first verse line unreadable: the main
        pass reads that line and the Tanach index reads it again."""
        db = os.path.join(self.tmp.name, 'bible.db')
        make_bible_db(db)
        con = sqlite3.connect(db)
        lid = con.execute('SELECT MIN(id) FROM line WHERE bookId = ? '
                          'AND heRef IS NOT NULL', (BIBLE,)).fetchone()[0]
        # a BLOB in a database without a zstd dictionary cannot be decoded
        con.execute("UPDATE line_content SET content = X'00FF' WHERE id = ?",
                    (lid,))
        con.commit()
        con.close()
        self.spec = _otzaria_spec(db)
        return str(lid)

    def test_one_row_met_by_several_passes_counts_once(self):
        unit = self.use_bible_db()
        for st in ('lexicon', 'detect', 'locate'):
            self.run_stage(st, 1)
        cov = _coverage(self.out, 'locate')
        self.assertEqual(cov['passes']['tanach_index']['decode_errors'], 1)
        self.assertGreaterEqual(cov['unread'], 2)    # main + Tanach index
        self.assertEqual(cov['unread_rows'], 1)
        self.assertIs(cov['accepted'], True)
        self.assertEqual([r['unit'] for r in cov['unread_refs']], [unit])

    def test_version_row_only_the_tanach_index_reads_adds_a_row(self):
        unit = self.use_bible_db()
        db = self.spec['path']
        con = sqlite3.connect(db)
        vid, lid = con.execute(
            'SELECT versionId, lineId FROM version_line WHERE versionId = 2 '
            'AND content IS NOT NULL AND lineId != ? ORDER BY lineId',
            (int(unit),)).fetchone()
        con.execute("UPDATE version_line SET content = X'00FF' "
                    'WHERE versionId = ? AND lineId = ?', (vid, lid))
        con.commit()
        con.close()
        self.run_stage('lexicon', 1)
        self.run_stage('detect', 1)
        with self.assertRaises(core.PartialRead) as cm:
            self.run_stage('locate', 1)
        self.assertIn('לא הצליח לקרוא 2 שורות', str(cm.exception))
        self.assertIn(f'מזהה שורה {lid}, בגרסה {vid}', str(cm.exception))
        self.run_stage('locate', 2)
        cov = _coverage(self.out, 'locate')
        self.assertEqual(cov['unread_rows'], 2)
        self.assertEqual(sorted(cov['unread_units']),
                         sorted([unit, f'ver:{vid}:{lid}']))

    def test_more_bad_rows_than_accepted_stops_locate(self):
        self.run_stage('lexicon', 1)
        self.run_stage('detect', 1)
        _corrupt_row(self.db, 3)                      # input degraded since
        with self.assertRaises(core.PartialRead) as cm:
            self.run_stage('locate', 1)
        self.assertIn('לא הצליח לקרוא 2 שורות', str(cm.exception))
        self.assertFalse(os.path.exists(
            os.path.join(self.out, core.REPORT_DB_F)))


if __name__ == '__main__':
    unittest.main()
