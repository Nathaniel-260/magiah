# -*- coding: utf-8 -*-
"""The seforim.db reader: layouts, zstd rows, versions, coverage, lines."""
import contextlib
import io
import json
import os
import pickle
import sqlite3
import tempfile
import unittest
from unittest import mock

from magiah import book_scan, book_source, cli, core, corpus as corpus_mod
from magiah.config import Config
from magiah.corpus import make_corpus
from magiah.corpus_hybrid import HybridCorpus, LibraryCorpus
from magiah.textsource import (OtzariaDB, ReadStats, TextSourceError, ro_uri,
                               iter_file_lines, split_lines)

try:
    from compression import zstd as _zstd
except ImportError:                                  # Python < 3.14
    _zstd = None

LINES = [
    (1, 1, 'בראשית ברא אלהים את השמים ואת הארץ'),
    (2, 1, '<b>והארץ</b> היתה תהו ובהו'),
    (3, 2, 'אמר הרב ברכת שלום עליכם לתלמידיו'),
    (4, 2, 'אמר הסוחר המחיר שלם ואין חוב'),
]


def _base_schema(con):
    con.executescript('''
        CREATE TABLE source(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE book(id INTEGER PRIMARY KEY, title TEXT, sourceId INT,
                          hasTeamim INT DEFAULT 0);
        INSERT INTO source VALUES(1, 'Sefaria'), (2, 'DictaToOtzaria');
        INSERT INTO book VALUES(1, 'ספר א', 1, 0), (2, 'ספר ב', 2, 0);
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta VALUES('db_schema_version', '6');
    ''')


def make_inline_db(path):
    """The pre-schema-6 layout: text in line.content."""
    con = sqlite3.connect(path)
    _base_schema(con)
    con.execute('CREATE TABLE line(id INTEGER PRIMARY KEY, bookId INT, '
                'lineIndex INT, heRef TEXT, content TEXT)')
    con.executemany('INSERT INTO line VALUES(?,?,?,?,?)',
                    [(i, b, i, f'ref {i}', t) for i, b, t in LINES])
    con.commit()
    con.close()


def _train_dict():
    samples = [(t + ' ' + str(i)).encode('utf-8')
               for i in range(400) for _, _, t in LINES]
    return _zstd.train_dict(samples, 4096)


def make_schema6_db(path, corrupt_id=None, plain_id=None, missing_id=None):
    """Schema 6: text in line_content, zstd frames with a stored dict, and
    one alternative version with its own reading. `missing_id` gets a `line`
    row but no `line_content` row."""
    zd = _train_dict()
    con = sqlite3.connect(path)
    _base_schema(con)
    con.executescript('''
        CREATE TABLE line(id INTEGER PRIMARY KEY, bookId INT, lineIndex INT,
                          heRef TEXT, tocEntryId INT, charCount INT);
        CREATE TABLE line_content(id INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE zstd_dict(id INTEGER PRIMARY KEY, dict BLOB NOT NULL);
        CREATE TABLE book_version(id INTEGER PRIMARY KEY, bookId INT,
                                  versionTitle TEXT, hasContent INT);
        CREATE TABLE version_line(versionId INT, lineId INT, content TEXT,
                                  charCount INT,
                                  PRIMARY KEY(versionId, lineId));
    ''')
    con.execute('INSERT INTO zstd_dict VALUES(1, ?)', (zd.dict_content,))
    for i, b, t in LINES:
        con.execute('INSERT INTO line VALUES(?,?,?,?,NULL,?)',
                    (i, b, i, f'ref {i}', len(t)))
        if i == missing_id:
            continue
        if i == plain_id:
            val = t                                  # stored uncompressed
        elif i == corrupt_id:
            val = b'\x28\xb5\x2f\xfd' + b'\x00' * 9  # broken frame
        else:
            val = _zstd.compress(t.encode('utf-8'), zstd_dict=zd)
        con.execute('INSERT INTO line_content VALUES(?,?)', (i, val))
    con.execute("INSERT INTO book_version VALUES(1, 1, 'v1', 1)")
    con.execute('INSERT INTO version_line VALUES(1, 1, NULL, 0)')
    con.execute('INSERT INTO version_line VALUES(1, 2, ?, 0)',
                (_zstd.compress('נוסח אחר'.encode('utf-8'), zstd_dict=zd),))
    con.commit()
    con.close()


class InlineLayoutTest(unittest.TestCase):
    def test_old_layout_still_reads(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 's.db')
            make_inline_db(p)
            with OtzariaDB(p) as odb:
                self.assertEqual(odb.layout, 'inline')
                st = ReadStats()
                got = list(odb.iter_range(1, 5, st))
            self.assertEqual([t for _, _, t in got], [t for _, _, t in LINES])
            self.assertEqual(st.lines, 4)
            self.assertEqual(st.decode_errors, 0)

    def test_missing_db_is_not_created(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'nope.db')
            with self.assertRaises(TextSourceError):
                OtzariaDB(p)
            self.assertFalse(os.path.exists(p))


@unittest.skipIf(_zstd is None, 'needs compression.zstd (Python 3.14+)')
class Schema6Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self, **kw):
        p = os.path.join(self.dir, 'seforim.db')
        make_schema6_db(p, **kw)
        return p

    def test_compressed_rows_decode(self):
        p = self._db()
        with OtzariaDB(p) as odb:
            self.assertEqual(odb.layout, 'line_content')
            self.assertIsNotNone(odb.backend)
            st = ReadStats()
            got = {lid: t for lid, _, t in odb.iter_range(1, 5, st)}
        self.assertEqual(got, {i: t for i, _, t in LINES})
        self.assertEqual(st.decode_errors, 0)

    def test_text_row_in_compressed_db_passes_through(self):
        p = self._db(plain_id=3)
        with OtzariaDB(p) as odb:
            got = {lid: t for lid, _, t in odb.iter_range(1, 5, ReadStats())}
        self.assertEqual(got[3], LINES[2][2])

    def test_corrupt_row_is_counted_never_yielded(self):
        p = self._db(corrupt_id=2)
        with OtzariaDB(p) as odb:
            st = ReadStats()
            got = [lid for lid, _, _ in odb.iter_range(1, 5, st)]
        self.assertNotIn(2, got)
        self.assertEqual(st.decode_errors, 1)
        self.assertEqual(st.error_samples[0][0], '2')

    def test_otzaria_preset_corpus_reads_schema6(self):
        p = self._db()
        corpus = make_corpus({'type': 'sqlite', 'path': p, 'table': 'line',
                              'id_col': 'id', 'text_col': 'content',
                              'preset': 'otzaria'})
        rows = []
        for ch in corpus.chunks(2):
            rows.extend(corpus.iter_texts_docs(ch))
        corpus.close()
        self.assertEqual(sorted((u, d) for u, d, _ in rows),
                         [('1', '1'), ('2', '1'), ('3', '2'), ('4', '2')])
        # the one version row with its own reading is excluded, and counted
        self.assertEqual(corpus.stats.version_lines_skipped, 1)

    def test_hybrid_db_part_reads_only_sefaria_books(self):
        p = self._db()
        lib = os.path.join(self.dir, 'lib')
        os.makedirs(lib)
        corpus = HybridCorpus({'type': 'hybrid', 'path': lib, 'db': p})
        units = []
        for ch in corpus.chunks(1):
            units.extend(u for u, _, _ in corpus.iter_texts_docs(ch))
        corpus.close()
        self.assertEqual(sorted(units), ['1', '2'])     # book 1 = Sefaria

    def test_book_scan_loader_reads_schema6(self):
        p = self._db()
        b = book_source.load_book('db', '2', db_path=p)
        self.assertEqual([t for _, _, t in b.lines],
                         [LINES[2][2], LINES[3][2]])
        self.assertEqual(b.lines[0][1], 'ref 3')

    def test_book_with_unreadable_row_is_refused(self):
        p = self._db(corrupt_id=4)
        with self.assertRaises(book_source.BookNotFound):
            book_source.load_book('db', '2', db_path=p)

    def test_lexicon_stage_refuses_partial_input(self):
        p = self._db(corrupt_id=2)
        out = os.path.join(self.dir, 'out')
        os.makedirs(out)
        spec = {'type': 'sqlite', 'path': p, 'table': 'line', 'id_col': 'id',
                'text_col': 'content', 'preset': 'otzaria'}
        cfg = Config(workers=1, n_chunks=2)
        with self.assertRaises(core.PartialRead):
            core.build_lexicon(spec, cfg, out)
        import json
        with open(os.path.join(out, 'coverage_lexicon.json'),
                  encoding='utf-8') as f:
            cov = json.load(f)
        self.assertFalse(cov['complete'])
        self.assertEqual(cov['decode_errors'], 1)
        self.assertEqual(cov['lines'], 3)

    def test_lexicon_stage_counts_full_coverage(self):
        p = self._db()
        out = os.path.join(self.dir, 'out')
        os.makedirs(out)
        spec = {'type': 'sqlite', 'path': p, 'table': 'line', 'id_col': 'id',
                'text_col': 'content', 'preset': 'otzaria'}
        core.build_lexicon(spec, Config(workers=1, n_chunks=2), out)
        import pickle
        with open(os.path.join(out, core.LEXICON_F), 'rb') as f:
            lex = pickle.load(f)
        self.assertEqual(lex.get('שלום'), 1)
        self.assertEqual(lex.get('והארץ'), 1)        # inline tag dropped


class LineSplittingTest(unittest.TestCase):
    """Full scan, single-book scan and the patcher must number lines alike."""

    TEXT = 'שורה א המשך\r\nשורה ב\rשורה ג\nשורה ד\n'

    def test_split_lines_ignores_unicode_separators(self):
        self.assertEqual(split_lines(self.TEXT),
                         ['שורה א המשך', 'שורה ב', 'שורה ג', 'שורה ד'])

    def test_streaming_reader_agrees_across_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'b.txt')
            # a CRLF straddling the 1 MiB read boundary
            body = 'א' * ((1 << 20) - 1) + '\r\nב\n'
            with open(p, 'w', encoding='utf-8', newline='') as f:
                f.write(self.TEXT + body)
            with open(p, encoding='utf-8', newline='') as f:
                whole = split_lines(f.read())
            self.assertEqual(list(iter_file_lines(p)), whole)

    def test_book_scan_units_match_full_scan_units(self):
        with tempfile.TemporaryDirectory() as lib:
            os.makedirs(os.path.join(lib, 'DictaToOtzaria'))
            p = os.path.join(lib, 'DictaToOtzaria', 'ספר.txt')
            with open(p, 'w', encoding='utf-8', newline='') as f:
                f.write(self.TEXT)
            corpus = LibraryCorpus({'type': 'library', 'path': lib})
            full = [(u, t) for ch in corpus.chunks(1)
                    for u, _, t in corpus.iter_texts_docs(ch)]
            book = book_source.load_book('library', 'DictaToOtzaria/ספר.txt',
                                         library_dir=lib)
            self.assertEqual(full, [(u, t) for u, _, t in book.lines])

    def test_new_library_file_is_found_without_restart(self):
        with tempfile.TemporaryDirectory() as lib:
            os.makedirs(os.path.join(lib, 'MoreBooks'))
            with open(os.path.join(lib, 'MoreBooks', 'א.txt'), 'w',
                      encoding='utf-8') as f:
                f.write('שורה\n')
            self.assertEqual(len(book_source.list_library_books(lib)), 1)
            with open(os.path.join(lib, 'MoreBooks', 'ב.txt'), 'w',
                      encoding='utf-8') as f:
                f.write('שורה\n')
            self.assertEqual(len(book_source.list_library_books(lib)), 2)
            b = book_source.load_book('library', 'MoreBooks/ב.txt',
                                      library_dir=lib)
            self.assertEqual(len(b), 1)


BROKEN_FRAME = b'\x28\xb5\x2f\xfd' + b'\x00' * 9


def _corrupt_row(db_path, line_id):
    con = sqlite3.connect(db_path)
    con.execute('UPDATE line_content SET content = ? WHERE id = ?',
                (BROKEN_FRAME, line_id))
    con.commit()
    con.close()


def _otzaria_spec(db_path):
    return {'type': 'sqlite', 'path': db_path, 'table': 'line',
            'id_col': 'id', 'text_col': 'content', 'preset': 'otzaria'}


def _read(path):
    with open(path, 'rb') as f:
        return f.read()


def _coverage(out, stage):
    with open(os.path.join(out, f'coverage_{stage}.json'),
              encoding='utf-8') as f:
        return json.load(f)


@unittest.skipIf(_zstd is None, 'needs compression.zstd (Python 3.14+)')
class PartialOutputTest(unittest.TestCase):
    """A stage that could not read its whole input must not replace its last
    good output, and nothing downstream may consume output whose latest
    build was partial."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'seforim.db')
        make_schema6_db(self.db)
        self.out = os.path.join(self.tmp.name, 'out')
        os.makedirs(self.out)
        self.spec = _otzaria_spec(self.db)
        self.cfg = Config(workers=1, n_chunks=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_lexicon_keeps_old_one_and_is_refused(self):
        core.build_lexicon(self.spec, self.cfg, self.out)
        lex_path = os.path.join(self.out, core.LEXICON_F)
        good = _read(lex_path)
        self.assertTrue(_coverage(self.out, 'lexicon')['complete'])
        self.assertIsNotNone(book_scan.load_lexicon(self.out))

        _corrupt_row(self.db, 2)
        with self.assertRaises(core.PartialRead):
            core.build_lexicon(self.spec, self.cfg, self.out)
        self.assertEqual(_read(lex_path), good)          # not replaced
        self.assertFalse(os.path.exists(lex_path + '.tmp'))
        self.assertFalse(_coverage(self.out, 'lexicon')['complete'])
        # the stale lexicon is refused by every consumer
        with self.assertRaises(book_scan.BookScanError) as cm:
            book_scan.load_lexicon(self.out)
        self.assertIn('מילון', str(cm.exception))
        with self.assertRaises(core.PartialRead):
            core.detect(self.spec, self.cfg, self.out)
        with self.assertRaises(book_scan.BookScanError):
            book_scan.scan_book(self.out, 'db', '2', db_path=self.db)

    def test_first_partial_lexicon_writes_no_lexicon(self):
        _corrupt_row(self.db, 2)
        with self.assertRaises(core.PartialRead):
            core.build_lexicon(self.spec, self.cfg, self.out)
        self.assertFalse(os.path.exists(
            os.path.join(self.out, core.LEXICON_F)))

    def test_partial_locate_keeps_old_report_and_is_refused(self):
        core.build_lexicon(self.spec, self.cfg, self.out)
        core.detect(self.spec, self.cfg, self.out)
        core.locate(self.spec, self.cfg, self.out)
        rep = os.path.join(self.out, core.REPORT_DB_F)
        good = _read(rep)
        core.report(self.cfg, self.out)                  # complete: accepted

        _corrupt_row(self.db, 3)
        with self.assertRaises(core.PartialRead):
            core.locate(self.spec, self.cfg, self.out)
        self.assertEqual(_read(rep), good)               # not replaced
        self.assertFalse(os.path.exists(rep + '.tmp'))
        cov = _coverage(self.out, 'locate')
        self.assertFalse(cov['complete'])
        self.assertEqual(cov['unread'], 1)
        with self.assertRaises(core.PartialRead) as cm:
            core.report(self.cfg, self.out)
        self.assertIn('מיקום', str(cm.exception))
        # calibrate only learns from the last complete report, and must not
        # block the rescan that repairs it
        core.calibrate(self.cfg, self.out)

    def test_failed_lexicon_also_blocks_the_older_report(self):
        # report.db is derived from the lexicon: once the latest lexicon
        # build is partial, the report from an earlier run is refused too
        core.build_lexicon(self.spec, self.cfg, self.out)
        core.detect(self.spec, self.cfg, self.out)
        core.locate(self.spec, self.cfg, self.out)
        _corrupt_row(self.db, 2)
        with self.assertRaises(core.PartialRead):
            core.build_lexicon(self.spec, self.cfg, self.out)
        with self.assertRaises(core.PartialRead) as cm:
            core.report(self.cfg, self.out)
        self.assertIn('מילון', str(cm.exception))
        # a complete rebuild clears it
        make_schema6_db(os.path.join(self.tmp.name, 'fixed.db'))
        os.replace(os.path.join(self.tmp.name, 'fixed.db'), self.db)
        core.build_lexicon(self.spec, self.cfg, self.out)
        core.report(self.cfg, self.out)

    def test_tanach_pass_failure_is_recorded_in_hebrew(self):
        core.build_lexicon(self.spec, self.cfg, self.out)
        core.detect(self.spec, self.cfg, self.out)

        def broken_index(db_path, stats):
            stats.decode_errors += 2
            return {}
        with mock.patch.object(core, '_build_verse_index', broken_index):
            with self.assertRaises(core.PartialRead) as cm:
                core.locate(self.spec, self.cfg, self.out)
        self.assertIn('לא הצליח לקרוא 2 שורות', str(cm.exception))
        cov = _coverage(self.out, 'locate')
        self.assertFalse(cov['complete'])
        self.assertEqual(cov['passes']['tanach_index']['decode_errors'], 2)
        self.assertFalse(os.path.exists(
            os.path.join(self.out, core.REPORT_DB_F)))

    def test_output_without_coverage_file_is_accepted(self):
        # outputs written before coverage was recorded keep working
        self.assertIsNone(core.coverage_problem(self.out, 'lexicon'))

    def test_cli_report_exits_nonzero_on_refused_report(self):
        core.build_lexicon(self.spec, self.cfg, self.out)
        core.detect(self.spec, self.cfg, self.out)
        core.locate(self.spec, self.cfg, self.out)
        _corrupt_row(self.db, 3)
        with self.assertRaises(core.PartialRead):
            core.locate(self.spec, self.cfg, self.out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(['report', '--otzaria', '--db', self.db,
                           '--out', self.out])
        self.assertEqual(rc, 1)
        self.assertIn('מיקום', err.getvalue())


@unittest.skipIf(_zstd is None, 'needs compression.zstd (Python 3.14+)')
class MissingContentRowTest(unittest.TestCase):
    """A `line` row without its `line_content` row is unread, not absent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'seforim.db')
        make_schema6_db(self.db, missing_id=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_range_read_counts_missing_row(self):
        with OtzariaDB(self.db) as odb:
            st = ReadStats()
            got = [lid for lid, _, _ in odb.iter_range(1, 5, st)]
        self.assertEqual(got, [1, 2, 4])
        self.assertEqual(st.missing, 1)
        self.assertEqual(st.unread(), 1)

    def test_stage_with_missing_row_is_partial(self):
        out = os.path.join(self.tmp.name, 'out')
        os.makedirs(out)
        with self.assertRaises(core.PartialRead):
            core.build_lexicon(_otzaria_spec(self.db),
                               Config(workers=1, n_chunks=2), out)
        cov = _coverage(out, 'lexicon')
        self.assertFalse(cov['complete'])
        self.assertEqual(cov['missing'], 1)

    def test_book_with_missing_row_is_refused(self):
        with self.assertRaises(book_source.BookNotFound):
            book_source.load_book('db', '2', db_path=self.db)


@unittest.skipIf(_zstd is None, 'needs compression.zstd (Python 3.14+)')
class OpenFailureTest(unittest.TestCase):
    """A database that fails to open is released and reported in Hebrew."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'seforim.db')
        make_schema6_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _set_dict(self, value):
        con = sqlite3.connect(self.db)
        if value is None:
            con.execute('DELETE FROM zstd_dict')
        else:
            con.execute('UPDATE zstd_dict SET dict = ?', (value,))
        con.commit()
        con.close()

    def _assert_released(self):
        # on Windows a leaked connection keeps the file locked (WinError 32)
        os.remove(self.db)
        self.assertFalse(os.path.exists(self.db))

    def test_empty_dictionary_table(self):
        self._set_dict(None)
        with self.assertRaises(TextSourceError):
            OtzariaDB(self.db)
        self._assert_released()

    def test_bad_dictionary_is_text_source_error(self):
        self._set_dict(b'not a zstd dictionary at all')
        with self.assertRaises(TextSourceError):
            OtzariaDB(self.db)
        self._assert_released()

    def test_missing_decoder(self):
        def no_decoder(_):
            raise TextSourceError('אין מפענח zstd')
        with mock.patch('magiah.textsource._make_decoder', no_decoder):
            with self.assertRaises(TextSourceError):
                OtzariaDB(self.db)
        self._assert_released()

    def test_not_a_database(self):
        with open(self.db, 'wb') as f:
            f.write(b'this is not sqlite' * 100)
        with self.assertRaises(TextSourceError):
            OtzariaDB(self.db)
        self._assert_released()

    def test_cli_prints_hebrew_not_traceback(self):
        out = os.path.join(self.tmp.name, 'out')
        missing = os.path.join(self.tmp.name, 'nope.db')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(['lexicon', '--otzaria', '--db', missing,
                           '--out', out])
        self.assertEqual(rc, 1)
        self.assertIn('לא נמצא', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        self.assertFalse(os.path.exists(missing))


@unittest.skipIf(_zstd is None, 'needs compression.zstd (Python 3.14+)')
class ReadStatsCountedTest(unittest.TestCase):
    """Every pass that reads the corpus counts what it could not read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'seforim.db')
        make_schema6_db(self.db, corrupt_id=2)
        con = sqlite3.connect(self.db)
        con.execute('UPDATE book SET hasTeamim = 1 WHERE id = 1')
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tanach_passes_count_and_release_the_db(self):
        st = ReadStats()
        vidx = core._build_verse_index(self.db, st)
        self.assertEqual(st.decode_errors, 1)
        st2 = ReadStats()
        core._tanach_edition_errors(self.db, vidx, st2)
        self.assertEqual(st2.decode_errors, 1)
        os.remove(self.db)                      # closed: not locked

    def test_book_context_verification_refuses_partial_pass(self):
        with self.assertRaises(book_scan.BookScanError):
            book_scan.verify_context(
                _otzaria_spec(self.db), Config(workers=1, n_chunks=2),
                {('בראשית', 'ברא')}, {})


class ReadOnlyUriTest(unittest.TestCase):
    """UNC paths, and paths with Hebrew, spaces, '#' and '%'."""

    @unittest.skipUnless(os.name == 'nt', 'Windows paths')
    def test_unc_uri_keeps_server_and_share(self):
        self.assertEqual(ro_uri(r'\\server\share\a b\seforim.db'),
                         'file:////server/share/a%20b/seforim.db?mode=ro')

    @unittest.skipUnless(os.name == 'nt', 'Windows paths')
    def test_drive_uri(self):
        self.assertEqual(ro_uri(r'C:\ספרים\a#b%c\seforim.db'),
                         'file:///C:/%D7%A1%D7%A4%D7%A8%D7%99%D7%9D/'
                         'a%23b%25c/seforim.db?mode=ro')

    def test_awkward_local_path_opens_read_only(self):
        with tempfile.TemporaryDirectory(prefix='ספר # 100% ') as d:
            p = os.path.join(d, 'seforim.db')
            make_inline_db(p)
            with OtzariaDB(p) as odb:
                self.assertEqual(len(list(odb.iter_range(1, 5,
                                                         ReadStats()))), 4)
                with self.assertRaises(sqlite3.OperationalError):
                    odb.con.execute('DELETE FROM line')
            missing = os.path.join(d, 'nope.db')
            with self.assertRaises(TextSourceError):
                OtzariaDB(missing)
            self.assertFalse(os.path.exists(missing))

    @unittest.skipUnless(os.name == 'nt' and os.path.isdir(r'\\localhost\C$'),
                         'needs the \\\\localhost\\C$ admin share')
    def test_unc_path_opens(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'seforim.db')
            make_inline_db(p)
            drive, rest = os.path.splitdrive(os.path.abspath(p))
            if len(drive) != 2:
                self.skipTest('temp dir is not on a drive letter')
            unc = '\\\\localhost\\' + drive[0] + '$' + rest
            with OtzariaDB(unc) as odb:
                self.assertEqual(odb.layout, 'inline')


class LibraryPathFileTest(unittest.TestCase):
    """%APPDATA%\\otzaria\\library_path.txt in any encoding must not stop the
    tool from starting (it is read at import time)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.appdata = self.tmp.name
        os.makedirs(os.path.join(self.appdata, 'otzaria'))
        self.lib = os.path.join(self.tmp.name, 'ספריית אוצריא')
        os.makedirs(self.lib)
        open(os.path.join(self.lib, 'seforim.db'), 'wb').close()
        self.want = os.path.join(self.lib, 'seforim.db')

    def tearDown(self):
        self.tmp.cleanup()

    def _resolve(self, data):
        with open(os.path.join(self.appdata, 'otzaria', 'library_path.txt'),
                  'wb') as f:
            f.write(data)
        with mock.patch.dict(os.environ, {'APPDATA': self.appdata}):
            return corpus_mod.default_otzaria_db()

    def test_utf8_with_bom(self):
        self.assertEqual(self._resolve(self.lib.encode('utf-8-sig')),
                         self.want)

    def test_utf16_with_bom(self):
        self.assertEqual(self._resolve(self.lib.encode('utf-16')), self.want)

    def test_quotes_and_extra_lines(self):
        data = f'\r\n  "{self.lib}"  \r\nsecond line\r\n'.encode('utf-8')
        self.assertEqual(self._resolve(data), self.want)

    def test_cp1255_falls_back_instead_of_crashing(self):
        self.assertEqual(self._resolve(self.lib.encode('cp1255')),
                         corpus_mod.LEGACY_OTZARIA_DB)

    def test_garbage_falls_back(self):
        self.assertEqual(self._resolve(b'\xff\x00\x81\x00\xfe'),
                         corpus_mod.LEGACY_OTZARIA_DB)


if __name__ == '__main__':
    unittest.main()
