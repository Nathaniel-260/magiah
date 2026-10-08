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
import threading
import unittest
import urllib.request
from unittest import mock

from magiah import book_scan, book_source, cli, core
from magiah.config import Config
from magiah.textsource import OtzariaDB, ReadStats
from magiah.webui import db as uidb, hebrew, scanner, server

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


class UnionLimitTest(_Case):
    """Parts each within the limit can miss different rows; the limit holds
    for their union, so a written output is always an accepted one."""

    def test_stage_counts_inherited_and_own_rows_together(self):
        clean = os.path.join(self.tmp.name, 'one.db')
        make_schema6_db(clean, corrupt_id=2)          # lexicon misses row 2
        self.spec = _otzaria_spec(clean)
        self.run_stage('lexicon', 1)
        make_schema6_db(os.path.join(self.tmp.name, 'other.db'),
                        corrupt_id=3)                 # detect misses row 3
        self.spec = _otzaria_spec(os.path.join(self.tmp.name, 'other.db'))
        with self.assertRaises(core.PartialRead) as cm:
            self.run_stage('detect', 1)
        self.assertIn('לא הצליח לקרוא 2 שורות', str(cm.exception))
        self.assertIn('כולל שורות שחסרו', str(cm.exception))
        self.assertFalse(os.path.exists(
            os.path.join(self.out, core.FLAGGED_F)))
        self.run_stage('detect', 2)
        cov = _coverage(self.out, 'detect')
        self.assertEqual((cov['unread_rows'], cov['accepted']), (2, True))
        self.assertEqual(sorted(cov['unread_units']), ['2', '3'])

    def test_book_scan_counts_book_and_lexicon_rows_together(self):
        lex_db = os.path.join(self.tmp.name, 'lex.db')
        make_schema6_db(lex_db, missing_id=4)        # lexicon misses row 4
        with _quiet():
            core.build_lexicon(_otzaria_spec(lex_db), self.cfg(1), self.out)
        # book 1 of self.db misses row 2: within 1, and so is the lexicon
        with self.assertRaises(book_scan.BookScanError) as cm:
            book_scan.scan_book(self.out, 'db', '1', cfg=self.cfg(1),
                                db_path=self.db)
        self.assertIn('בסך הכול', str(cm.exception))
        self.assertIn('--allow-unread 2', str(cm.exception))
        cov = book_scan.scan_book(self.out, 'db', '1', cfg=self.cfg(2),
                                  db_path=self.db)['coverage']
        self.assertEqual((cov['unread_rows'], cov['accepted']), (2, True))


class CliTest(_Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main([*argv, '--otzaria', '--db', self.db,
                           '--out', self.out, '--workers', '1',
                           '--n-chunks', '2'])
        return rc, out.getvalue(), err.getvalue()

    def run_config(self):
        with open(os.path.join(self.out, cli.RUN_CONFIG),
                  encoding='utf-8') as f:
            return json.load(f)['config']

    def test_option_must_be_a_non_negative_count(self):
        for bad in ('-1', 'abc', '1.5'):
            with self.subTest(value=bad), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main(['lexicon', '--allow-unread', bad, '--out', self.out])

    def test_blocked_then_accepted_then_required_again(self):
        rc, _, err = self.main('lexicon')
        self.assertEqual(rc, 1)
        self.assertIn('--allow-unread 2', err)
        self.assertNotIn('Traceback', err)

        rc, out, _ = self.main('lexicon', '--allow-unread', '2')
        self.assertFalse(rc)
        self.assertIn('אזהרה', out)
        # never remembered: run_config.json is what it was before the option
        self.assertEqual(set(self.run_config()),
                         set(Config().to_dict()) - {'allow_unread'})

        rc, _, err = self.main('detect')
        self.assertEqual(rc, 1)
        self.assertIn('--allow-unread 2', err)
        rc, _, _ = self.main('detect', '--allow-unread', '2')
        self.assertFalse(rc)

    def test_a_value_planted_in_run_config_is_ignored(self):
        self.main('lexicon', '--allow-unread', '2')
        path = os.path.join(self.out, cli.RUN_CONFIG)
        with open(path, encoding='utf-8') as f:
            rc_json = json.load(f)
        rc_json['config']['allow_unread'] = 99
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rc_json, f)
        rc, _, err = self.main('detect')
        self.assertEqual(rc, 1)
        self.assertIn('לא אישרה', err)
        self.assertNotIn('allow_unread', self.run_config())


class BookScanTest(_Case):
    """A single-book scan follows the full scan's rule: the bad rows of a
    book are skipped only when allowed, and the result is then partial."""

    def setUp(self):
        super().setUp()
        self.clean = os.path.join(self.tmp.name, 'clean.db')
        make_schema6_db(self.clean)

    def lexicon(self, db, allow_unread=0):
        with _quiet():
            core.build_lexicon(_otzaria_spec(db), self.cfg(allow_unread),
                               self.out)

    def scan(self, key, db, allow_unread=0):
        return book_scan.scan_book(self.out, 'db', key,
                                   cfg=self.cfg(allow_unread), db_path=db)

    def test_book_with_a_bad_row_is_refused_with_the_way_on(self):
        with self.assertRaises(book_source.BookNotFound) as cm:
            book_source.load_book('db', '1', db_path=self.db)
        msg = str(cm.exception)
        self.assertIn('ספר א', msg)
        self.assertIn('ref 2', msg)
        self.assertIn('--allow-unread 1', msg)
        self.assertIn(core.ALLOW_UNREAD_UI, msg)

    def test_boundary(self):
        for n, ok in ((0, False), (1, True), (2, True)):
            with self.subTest(allow_unread=n):
                if not ok:
                    with self.assertRaises(book_source.BookNotFound):
                        book_source.load_book('db', '1', db_path=self.db,
                                              allow_unread=n)
                    continue
                b = book_source.load_book('db', '1', db_path=self.db,
                                          allow_unread=n)
                self.assertEqual([u for u, _, _ in b.lines], ['1'])
                self.assertEqual(b.stats.unread(), 1)

    def test_book_that_is_all_unreadable_says_so(self):
        _corrupt_row(self.db, 1)
        with self.assertRaises(book_source.BookNotFound) as cm:
            book_source.load_book('db', '1', db_path=self.db, allow_unread=5)
        self.assertIn('אף אחת', str(cm.exception))

    def test_scan_skips_the_rows_and_marks_the_result(self):
        self.lexicon(self.clean)
        res = self.scan('1', self.db, 1)
        self.assertEqual(res['lines'], 1)
        cov = res['coverage']
        self.assertIs(cov['accepted'], True)
        self.assertEqual(cov['unread_rows'], 1)
        self.assertEqual([r['unit'] for r in cov['unread_refs']], ['2'])
        self.assertEqual(cov['unread_refs'][0]['book'], 'ספר א')
        self.assertNotIn('inherited', cov)

    def test_complete_scan_is_unmarked(self):
        self.lexicon(self.clean)
        self.assertIsNone(self.scan('1', self.clean, 3)['coverage'])

    def test_partial_lexicon_needs_the_option_and_marks_the_result(self):
        self.lexicon(self.db, 2)
        for n in (0, 1):
            with self.subTest(allow_unread=n), \
                    self.assertRaises(book_scan.BookScanError) as cm:
                self.scan('1', self.clean, n)
            self.assertIn('--allow-unread 2', str(cm.exception))
        cov = self.scan('1', self.clean, 2)['coverage']
        self.assertEqual(cov['inherited'], {'lexicon': 2})
        self.assertEqual(cov['unread_rows'], 2)
        self.assertEqual(sorted(r['unit'] for r in cov['unread_refs']),
                         ['2', '4'])

    def test_context_pass_obeys_the_limit(self):
        pairs = {('בראשית', 'ברא')}
        with self.assertRaises(book_scan.BookScanError) as cm:
            book_scan.verify_context(self.spec, self.cfg(1), pairs, {})
        self.assertIn('--allow-unread 2', str(cm.exception))
        st = ReadStats()
        book_scan.verify_context(self.spec, self.cfg(2), pairs, {}, stats=st)
        self.assertEqual(st.unread(), 2)

    def test_cli_book_command(self):
        self.lexicon(self.clean)
        base = ['book', '--book', '1', '--otzaria', '--db', self.db,
                '--out', self.out]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), _quiet():
            self.assertEqual(cli.main(base), 1)
        self.assertIn('--allow-unread 1', err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(base + ['--allow-unread', '1']), 0)
        self.assertIn('חלקית', out.getvalue())


class ReviewUiTest(_Case):
    """The review UI says, persistently, when what it shows is partial."""

    def status(self):
        con = uidb.connect(self.out)
        try:
            return uidb.get_meta(con, self.out)['result_status']
        finally:
            con.close()

    def notice(self):
        """The banner's notice about skipped rows, or None."""
        found = [n for n in self.status()['notices']
                 if n['kind'] == 'accepted_partial']
        return found[0] if found else None

    def full_scan(self, allow_unread):
        for st in ('lexicon', 'detect', 'locate'):
            self.run_stage(st, allow_unread)
        uidb.import_all(self.out)

    def test_partial_scan_is_noticed_until_a_complete_one(self):
        self.full_scan(2)
        n = self.notice()
        self.assertEqual((n['kind'], n['level'], n['stale'], n['action']),
                         ('accepted_partial', 'warning', False, None))
        self.assertIn('2 שורות במסד הנתונים', n['text'])
        self.assertIn('--allow-unread 2', n['text'])
        self.assertIn('מסד הנתונים', n['title'])
        # the lexicon was built without them too
        self.assertIn(hebrew.COVERAGE_NOTICE['scan_lexicon'], n['text'])
        self.assertEqual(n['details'].split('\n'),
                         ['ספר א, ref 2 (מזהה שורה 2)',
                          'ספר ב, ref 4 (מזהה שורה 4)'])
        self.assertTrue(n['hint'])
        # a run accepted partial succeeded: its results are not stale
        self.assertFalse(self.status()['stale'])
        self.repair_db()
        self.full_scan(0)
        self.assertIsNone(self.notice())

    def test_text_folder_scan_speaks_of_files_not_the_database(self):
        lib = os.path.join(self.tmp.name, 'lib')
        os.makedirs(os.path.join(lib, 'שבור.txt'))     # cannot be opened
        with open(os.path.join(lib, 'ספר.txt'), 'w', encoding='utf-8') as f:
            f.write('בראשית ברא אלהים את השמים ואת הארץ\n' * 20)
        self.spec = {'type': 'textdir', 'path': lib}
        self.full_scan(1)
        n = self.notice()
        C = hebrew.COVERAGE_NOTICE
        self.assertEqual(n['title'], C['title']['files'])
        self.assertEqual(n['hint'], C['hint']['files'])
        self.assertIn('1 קובצי טקסט', n['text'])
        self.assertNotIn('מסד', n['title'] + n['text'] + n['hint'])
        self.assertIn('שבור.txt', n['details'])

    def test_a_record_that_does_not_say_is_worded_generally(self):
        self.full_scan(2)
        con = sqlite3.connect(os.path.join(self.out, uidb.UI_DB_F))
        cov = json.loads(con.execute(
            "SELECT value FROM meta WHERE key = 'coverage'").fetchone()[0])
        del cov['unread_kind']
        con.execute("UPDATE meta SET value = ? WHERE key = 'coverage'",
                    (json.dumps(cov),))
        con.commit()
        con.close()
        n = self.notice()
        self.assertEqual(n['title'],
                         hebrew.COVERAGE_NOTICE['title']['input'])
        self.assertIn('2 שורות קלט', n['text'])

    def test_served_by_api_meta(self):
        self.full_scan(2)
        server.Handler.outdir = self.out
        srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f'http://127.0.0.1:{srv.server_address[1]}/api/meta'
            with urllib.request.urlopen(url, timeout=30) as r:
                meta = json.loads(r.read().decode('utf-8'))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual([n['kind'] for n in meta['result_status']['notices']],
                         ['accepted_partial'])

    def test_unreadable_mark_still_warns(self):
        self.full_scan(2)
        con = sqlite3.connect(os.path.join(self.out, core.REPORT_DB_F))
        con.execute("UPDATE coverage SET info = '{broken'")
        con.commit()
        con.close()
        uidb.import_all(self.out)
        n = self.notice()
        self.assertIn('דילגה על ? שורות', n['text'])     # count unknown
        self.assertIn('הרשימה חלקית', n['details'])

    def test_book_scan_notice_follows_the_book(self):
        clean = os.path.join(self.tmp.name, 'clean.db')
        make_schema6_db(clean)
        with _quiet():
            core.build_lexicon(_otzaria_spec(clean), self.cfg(), self.out)
        res = book_scan.scan_book(self.out, 'db', '1', cfg=self.cfg(1),
                                  db_path=self.db)
        uidb.import_book_scan(self.out, res)
        n = self.notice()
        self.assertIn('ספר א', n['text'])
        self.assertIn('מזהה שורה 2', n['details'])
        # a complete re-scan of the same book clears it
        uidb.import_book_scan(self.out, book_scan.scan_book(
            self.out, 'db', '1', cfg=self.cfg(), db_path=clean))
        self.assertIsNone(self.notice())

    def test_full_import_drops_book_records_it_supersedes(self):
        clean = os.path.join(self.tmp.name, 'clean.db')
        make_schema6_db(clean)
        with _quiet():
            core.build_lexicon(_otzaria_spec(clean), self.cfg(), self.out)
        uidb.import_book_scan(self.out, book_scan.scan_book(
            self.out, 'db', '1', cfg=self.cfg(1), db_path=self.db))
        self.assertIsNotNone(self.notice())
        # a complete full scan that contains book 1 is the newer truth for it
        self.spec = _otzaria_spec(clean)
        self.full_scan(0)
        self.assertIsNone(self.notice())


class ScanPanelTest(_Case):
    """The UI's scan panel: the same option, per run, on the command line."""

    def test_stage_command_carries_the_option_only_when_set(self):
        base = scanner._stage_cmd('lexicon', self.out)
        self.assertNotIn('--allow-unread', base)
        self.assertEqual(scanner._stage_cmd('lexicon', self.out, 3),
                         base + ['--allow-unread', '3'])

    def test_field_is_offered_at_zero_whatever_run_config_says(self):
        with open(os.path.join(self.out, scanner.RUN_CONFIG), 'w',
                  encoding='utf-8') as f:
            json.dump({'corpus': {}, 'config': {'allow_unread': 5}}, f)
        field = next(f for f in scanner.scan_config(self.out)['fields']
                     if f['key'] == 'allow_unread')
        self.assertEqual((field['value'], field['default'], field['type']),
                         (0, 0, 'int'))
        # the CLI's messages send the user to this field by name
        self.assertTrue(field['hebrew'].startswith(core.ALLOW_UNREAD_UI))
        self.assertEqual(scanner._merge_config(self.out, {}).allow_unread, 0)

    def test_negative_value_is_refused(self):
        with self.assertRaises(ValueError):
            scanner._merge_config(self.out, {'allow_unread': -1})
        # a count, written as one
        for bad in ('1.5', 1.5, True, 'abc', float('inf')):
            with self.assertRaises(ValueError, msg=repr(bad)):
                scanner._merge_config(self.out, {'allow_unread': bad})
        self.assertEqual(
            scanner._merge_config(self.out, {'allow_unread': '2'})
            .allow_unread, 2)

    def test_full_scan_passes_it_per_run_and_never_saves_it(self):
        seen = {}

        def fake_run(outdir, stages, run, allow_unread=0):
            seen['allow_unread'] = allow_unread
            run.done()
            run.close()
            with scanner._lock:
                scanner._state.update(state='done')
        with mock.patch.object(scanner, '_run', fake_run):
            scanner.start_scan(self.out, ['lexicon'], {'allow_unread': 3},
                               {'mode': 'sqlite', 'db_path': self.db})
            scanner._thread.join(10)
        self.assertEqual(seen, {'allow_unread': 3})
        with open(os.path.join(self.out, scanner.RUN_CONFIG),
                  encoding='utf-8') as f:
            self.assertNotIn('allow_unread', json.load(f)['config'])


if __name__ == '__main__':
    unittest.main()
