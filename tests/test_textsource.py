# -*- coding: utf-8 -*-
"""The seforim.db reader: layouts, zstd rows, versions, coverage, lines."""
import contextlib
import io
import json
import os
import pickle
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from magiah import book_scan, book_source, cli, core, corpus as corpus_mod
from magiah.config import Config
from magiah.corpus import make_corpus
from magiah.corpus_hybrid import HybridCorpus, LibraryCorpus
from magiah.textsource import (OtzariaDB, ReadStats, TextSourceError, ro_uri,
                               iter_file_lines, split_lines)
from magiah.webui import db as webui_db



class _ZstandardFixtures:
    """The slice of ``compression.zstd`` the fixtures use, on the
    ``zstandard`` package — the decoder Python < 3.14 reads seforim.db with
    (pyproject: ``zstandard; python_version < '3.14'``). Without it the
    zstd fixtures could not be written there, and the reader's only backend
    on those interpreters would go untested."""

    def __init__(self, zs):
        self._zs = zs

    def train_dict(self, samples, size):
        return self.ZstdDict(self._zs.train_dictionary(size, samples)
                             .as_bytes())

    def ZstdDict(self, content):
        d = types.SimpleNamespace(dict_content=bytes(content))
        d.compression_dict = self._zs.ZstdCompressionDict(d.dict_content)
        return d

    def compress(self, data, zstd_dict):
        return self._zs.ZstdCompressor(
            dict_data=zstd_dict.compression_dict).compress(data)


try:
    from compression import zstd as _zstd
except ImportError:                                  # Python < 3.14
    try:
        import zstandard
    except ImportError:          # neither backend: the reader cannot decode
        _zstd = None             # either, so the zstd tests are skipped
    else:
        _zstd = _ZstandardFixtures(zstandard)
NO_ZSTD = 'needs a zstd backend (compression.zstd, or zstandard before 3.14)'

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


def make_library_dir(path):
    """A library repo with one book: an empty one is refused as a source
    (corpus_hybrid.LibraryCorpus.chunks), so a hybrid corpus needs one even
    where only its database part is under test."""
    os.makedirs(os.path.join(path, 'DictaToOtzaria'), exist_ok=True)
    with open(os.path.join(path, 'DictaToOtzaria', 'ספר.txt'), 'w',
              encoding='utf-8') as f:
        f.write('שורה מקובץ\n')
    return path


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
        -- the real database's indexes: the query plans depend on them
        CREATE INDEX idx_line_book_index ON line(bookId, lineIndex);
        CREATE INDEX idx_version_line_line ON version_line(lineId);
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


@unittest.skipIf(_zstd is None, NO_ZSTD)
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

    def test_the_backend_this_interpreter_ships_with_is_tested(self):
        # before 3.14 the decoder is `zstandard`: the fixtures are written
        # with it there instead of skipping every zstd test
        with OtzariaDB(self._db()) as odb:
            self.assertEqual(odb.backend,
                             'compression.zstd' if sys.version_info >= (3, 14)
                             else 'zstandard')

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
        lib = make_library_dir(os.path.join(self.dir, 'lib'))
        corpus = HybridCorpus({'type': 'hybrid', 'path': lib, 'db': p})
        units = []
        for ch in corpus.chunks(1):
            if ch[0] == 'db':
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

    def _counting_walks(self):
        calls = []
        real = LibraryCorpus._files

        def files(corpus, dirs=None):
            calls.append(corpus.path)
            return real(corpus, dirs)
        return calls, mock.patch.object(LibraryCorpus, '_files', files)

    def _write(self, *parts):
        os.makedirs(os.path.dirname(os.path.join(*parts)), exist_ok=True)
        with open(os.path.join(*parts), 'w', encoding='utf-8') as f:
            f.write('שורה\n')

    def _age(self, lib):
        """Date every folder a minute back: a walk trusts only folders not
        modified around the time it ran (book_source._walk_library)."""
        past = time.time() - 60
        for d, _, _ in os.walk(lib):
            os.utime(d, (past, past))

    def test_picker_walks_the_library_once_while_it_is_unchanged(self):
        with tempfile.TemporaryDirectory() as lib:
            self._write(lib, 'MoreBooks', 'א', 'ספר א.txt')
            self._write(lib, 'MoreBooks', 'ב', 'ספר ב.txt')
            self._age(lib)
            calls, patch = self._counting_walks()
            with patch:
                for q in ('', 'ספר', 'א', 'ב', 'ספר א'):
                    book_source.list_library_books(lib, q)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                [b['key'] for b in book_source.list_library_books(lib, '')],
                ['MoreBooks/א/ספר א.txt', 'MoreBooks/ב/ספר ב.txt'])

    def test_picker_sees_every_kind_of_change(self):
        with tempfile.TemporaryDirectory() as lib:
            self._write(lib, 'MoreBooks', 'א', 'ספר א.txt')
            calls, patch = self._counting_walks()

            def after(change):
                """A trusted walk, then `change`: the next query must see
                it through the changed folder's modification time."""
                self._age(lib)
                book_source.list_library_books(lib, '')
                n = len(calls)
                change()
                keys = [b['key'] for b in
                        book_source.list_library_books(lib, '')]
                self.assertEqual(len(calls), n + 1)
                return keys
            with patch:
                # a book deep in a folder that already existed
                self.assertIn('MoreBooks/א/ספר ג.txt', after(
                    lambda: self._write(lib, 'MoreBooks', 'א', 'ספר ג.txt')))
                # a new folder, and a new origin at the top
                self.assertIn('MoreBooks/חדש/ספר ד.txt', after(
                    lambda: self._write(lib, 'MoreBooks', 'חדש',
                                        'ספר ד.txt')))
                self.assertIn('OtherRepo/ספר ה.txt', after(
                    lambda: self._write(lib, 'OtherRepo', 'ספר ה.txt')))
                # removed, renamed, a whole folder removed
                self.assertNotIn('MoreBooks/א/ספר ג.txt', after(
                    lambda: os.remove(os.path.join(lib, 'MoreBooks', 'א',
                                                   'ספר ג.txt'))))
                self.assertEqual(after(lambda: os.rename(
                    os.path.join(lib, 'OtherRepo', 'ספר ה.txt'),
                    os.path.join(lib, 'OtherRepo', 'ספר ו.txt'))),
                    ['MoreBooks/א/ספר א.txt', 'MoreBooks/חדש/ספר ד.txt',
                     'OtherRepo/ספר ו.txt'])
                self.assertNotIn('MoreBooks/חדש/ספר ד.txt', after(
                    lambda: shutil.rmtree(os.path.join(lib, 'MoreBooks',
                                                       'חדש'))))

    def test_a_walk_racing_a_change_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as lib:
            self._write(lib, 'MoreBooks', 'ספר א.txt')
            self._age(lib)
            calls, patch = self._counting_walks()
            # every folder looks modified after the walk began
            with patch, mock.patch.object(book_source.time, 'time_ns',
                                          return_value=0):
                book_source.list_library_books(lib, '')
                book_source.list_library_books(lib, '')
            self.assertEqual(len(calls), 2)


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


@unittest.skipIf(_zstd is None, NO_ZSTD)
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


@unittest.skipIf(_zstd is None, NO_ZSTD)
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


@unittest.skipIf(_zstd is None, NO_ZSTD)
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


@unittest.skipIf(_zstd is None, NO_ZSTD)
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

    def _make_bible_book(self):
        """Book 1 becomes a verified Bible book (category + title + heRefs);
        its line 2 is the corrupt row."""
        con = sqlite3.connect(self.db)
        con.executescript('''
            CREATE TABLE category(id INTEGER PRIMARY KEY, parentId INT,
                                  title TEXT, level INT);
            INSERT INTO category VALUES(1, NULL, 'תנ"ך', 0), (2, 1, 'תורה', 1);
            ALTER TABLE book ADD COLUMN categoryId INT;
            UPDATE book SET title = 'בראשית', categoryId = 2 WHERE id = 1;
            UPDATE line SET heRef = 'בראשית, א, א' WHERE id = 1;
            UPDATE line SET heRef = 'בראשית, א, ב' WHERE id = 2;
        ''')
        con.commit()
        con.close()

    def test_tanach_passes_count_and_release_the_db(self):
        # hasTeamim alone does not make a Bible edition: nothing is read
        st = ReadStats()
        core._build_verse_index(self.db, st)
        self.assertEqual((st.lines, st.decode_errors), (0, 0))
        # a verified Bible book with an unreadable row: counted
        self._make_bible_book()
        st = ReadStats()
        vidx = core._build_verse_index(self.db, st)
        self.assertEqual(st.decode_errors, 1)
        self.assertEqual(st.unread(), 1)
        os.remove(self.db)                      # closed: not locked
        # the edition comparison works on the index alone — it does not read
        # (or reopen) the database, so it has nothing further to count
        rows = core._tanach_edition_errors(vidx)
        self.assertEqual(rows, [])

    def test_tanach_index_counts_missing_line_content(self):
        self._make_bible_book()
        con = sqlite3.connect(self.db)
        con.execute('DELETE FROM line_content WHERE id = 1')
        con.commit()
        con.close()
        st = ReadStats()
        core._build_verse_index(self.db, st)
        self.assertEqual(st.missing, 1)
        self.assertEqual(st.unread(), 2)        # + the corrupt row 2

    def test_book_context_verification_refuses_partial_pass(self):
        with self.assertRaises(book_scan.BookScanError):
            book_scan.verify_context(
                _otzaria_spec(self.db), Config(workers=1, n_chunks=2),
                {('בראשית', 'ברא')}, {})

    def test_hybrid_counts_skipped_version_lines(self):
        db = os.path.join(self.tmp.name, 'clean.db')
        make_schema6_db(db)
        lib = make_library_dir(os.path.join(self.tmp.name, 'lib'))
        corpus = HybridCorpus({'type': 'hybrid', 'path': lib, 'db': db})
        for ch in corpus.chunks(2):
            list(corpus.iter_texts_docs(ch))
        corpus.close()
        # the version row belongs to book 1 (Sefaria), so hybrid skips it too
        self.assertEqual(corpus.stats.version_lines_skipped, 1)


# make_schema6_db + add_version_edge_cases: line id -> book id
_LINE_BOOKS = {1: 1, 2: 1, 3: 2, 4: 2, 6: 3, 7: 3, 9: 3}
# (versionId, lineId) of their version rows with content; no line 8 or 50
_VERSION_READINGS = [(1, 2), (4, 2), (2, 3), (3, 6), (3, 7), (3, 8), (3, 50)]
_BY_SOURCE = ('SELECT b.id FROM book b JOIN source s ON s.id = b.sourceId '
              'WHERE s.name = ?')
# name -> (book_ids_sql, params, the book ids it selects)
_BOOK_FILTERS = {
    'none': (None, (), None),
    'Sefaria': (_BY_SOURCE, ('Sefaria',), {1, 3}),
    'Dicta': (_BY_SOURCE, ('DictaToOtzaria',), {2}),
    'hasTeamim': ('SELECT id FROM book WHERE hasTeamim = 1', (), {3}),
    'no book': ('SELECT id FROM book WHERE 0', (), set()),
}


def add_version_edge_cases(path):
    """On top of make_schema6_db: a second Sefaria book (3, cantillated)
    with gaps in its line ids, a line with two versions, NULL rows in two
    books, and version rows whose line does not exist (8 and 50)."""
    con = sqlite3.connect(path)
    zd = _zstd.ZstdDict(
        con.execute('SELECT dict FROM zstd_dict').fetchone()[0])

    def z(text):
        return _zstd.compress(text.encode('utf-8'), zstd_dict=zd)

    con.execute("INSERT INTO book VALUES(3, 'ספר ג', 1, 1)")
    for i in (6, 7, 9):
        con.execute('INSERT INTO line VALUES(?,3,?,?,NULL,0)',
                    (i, i, f'ref {i}'))
        con.execute('INSERT INTO line_content VALUES(?,?)',
                    (i, z(f'שורה {i}')))
    con.executemany('INSERT INTO book_version VALUES(?,?,?,1)',
                    [(2, 2, 'v2'), (3, 3, 'v3'), (4, 1, 'v4')])
    con.executemany('INSERT INTO version_line VALUES(?,?,?,0)',
                    [(v, ln, z('נוסח אחר')) for v, ln in _VERSION_READINGS
                     if (v, ln) != (1, 2)]   # make_schema6_db's own row
                    + [(2, 4, None), (3, 9, None)])
    con.commit()
    con.close()


def _in_subquery_sql(lo, hi, book_ids_sql, params):
    """count_version_lines' query before the join, ``(sql, args)``: the
    reference for which rows count."""
    sql = 'SELECT COUNT(*) FROM version_line WHERE content IS NOT NULL'
    args = []
    if lo is not None:
        sql += ' AND lineId >= ? AND lineId < ?'
        args = [lo, hi]
    if book_ids_sql:
        sql += (' AND lineId IN (SELECT id FROM line '
                f'WHERE bookId IN ({book_ids_sql}))')
        args.extend(params)
    return sql, args


class _Recorder:
    """Stands in for a connection and keeps every statement run on it."""

    def __init__(self, con):
        self.con, self.calls = con, []

    def execute(self, sql, args=()):
        self.calls.append((sql, tuple(args)))
        return self.con.execute(sql, args)


@contextlib.contextmanager
def _recording(odb):
    """Record the statements `odb` runs; its connection is restored after."""
    rec = odb.con = _Recorder(odb.con)
    try:
        yield rec
    finally:
        odb.con = rec.con


def _plan(con, sql, args):
    """The EXPLAIN QUERY PLAN details of a statement, joined. Only the
    details are used: the other columns differ between SQLite versions."""
    return ' | '.join(str(r[-1]) for r in
                      con.execute('EXPLAIN QUERY PLAN ' + sql, args))


@unittest.skipIf(_zstd is None, NO_ZSTD)
class BookFilteredReadTest(unittest.TestCase):
    """Reads restricted to some books: the same rows, with a chunk's id range
    — not the list of the selected books' lines — driving the scan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'seforim.db')
        make_schema6_db(self.db)
        add_version_edge_cases(self.db)
        bounds = list(range(12)) + [50, 51]
        self.ranges = [(lo, hi) for lo in bounds for hi in bounds if lo <= hi]

    def tearDown(self):
        self.tmp.cleanup()

    def _hybrid(self):
        lib = make_library_dir(os.path.join(self.tmp.name, 'lib'))
        return HybridCorpus({'type': 'hybrid', 'path': lib, 'db': self.db})

    def test_version_counts_match_the_in_subquery_form(self):
        ranges = [(None, None)] + self.ranges       # None: the whole table
        with OtzariaDB(self.db) as odb:
            for name, (sql, params, books) in _BOOK_FILTERS.items():
                with self.subTest(name):
                    got = [odb.count_version_lines(lo, hi, sql, params)
                           for lo, hi in ranges]
                    self.assertEqual(got, [
                        odb.con.execute(*_in_subquery_sql(
                            lo, hi, sql, params)).fetchone()[0]
                        for lo, hi in ranges])
                    self.assertEqual(got, [
                        sum(1 for _, ln in _VERSION_READINGS
                            if (lo is None or lo <= ln < hi) and (
                                books is None
                                or _LINE_BOOKS.get(ln) in books))
                        for lo, hi in ranges])

    def test_hybrid_skipped_count_does_not_depend_on_chunking(self):
        for n in (1, 2, 3, 7):
            with self.subTest(chunks=n):
                corpus = self._hybrid()
                for ch in corpus.chunks(n):
                    list(corpus.iter_texts_docs(ch))
                corpus.close()
                # (1,2) (4,2) (3,6) (3,7): the Sefaria books' own readings
                self.assertEqual(corpus.stats.version_lines_skipped, 4)

    def test_range_reads_return_exactly_the_selected_books(self):
        with OtzariaDB(self.db) as odb:
            for name, (sql, params, books) in _BOOK_FILTERS.items():
                with self.subTest(name):
                    for lo, hi in self.ranges:
                        st = ReadStats()
                        got = sorted((lid, bid) for lid, bid, _ in
                                     odb.iter_range(lo, hi, st, sql, params))
                        self.assertEqual(got, sorted(
                            (i, b) for i, b in _LINE_BOOKS.items()
                            if lo <= i < hi
                            and (books is None or b in books)), (lo, hi))
                        self.assertEqual(st.unread(), 0)

    def test_hybrid_chunk_is_driven_by_its_id_range(self):
        corpus = self._hybrid()
        try:
            _, lo, hi = next(c for c in corpus.chunks(2) if c[0] == 'db')
            with _recording(corpus._otzaria()) as rec:
                list(corpus.iter_texts_docs(('db', lo, hi)))
            [read] = [c for c in rec.calls if 'line_content' in c[0]]
            [count] = [c for c in rec.calls if 'version_line' in c[0]]
            read_plan = _plan(rec.con, *read)
            count_plan = _plan(rec.con, *count)
            old_plan = _plan(rec.con, *_in_subquery_sql(
                lo, hi, _BY_SOURCE, ('Sefaria',)))
        finally:
            corpus.close()
        # driven by the book index, SQLite walked the lines of every
        # selected book on each chunk; the range must drive instead
        self.assertIn('rowid>?', read_plan)
        self.assertIn('lineId>?', count_plan)
        for plan in (read_plan, count_plan):
            self.assertNotIn('idx_line_book_index', plan)
        # and the check is not vacuous: the IN-subquery form fails it
        self.assertIn('idx_line_book_index', old_plan)


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


def make_report_db(path):
    """The smallest report.db import_all accepts: one finding."""
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE occurrences_full(errtype TEXT, word TEXT,
            suggestion TEXT, score REAL, ctx_hits INT, sugg_local INT,
            book_repeat INT, tanach INT, origin TEXT, source TEXT, ref TEXT,
            unit TEXT, doc TEXT, snippet TEXT);
        CREATE TABLE space_errors_full(part1 TEXT, part2 TEXT, joined TEXT,
            join_freq INT, source TEXT, ref TEXT, unit TEXT, snippet TEXT,
            origin TEXT);
        CREATE TABLE tanach_errors_full(word TEXT, canonical TEXT,
            source TEXT, ref TEXT, unit TEXT, snippet TEXT, origin TEXT);
        CREATE TABLE tanach_matches_full(word TEXT, source TEXT, ref TEXT,
            unit TEXT, snippet TEXT, origin TEXT);''')
    con.execute('INSERT INTO occurrences_full VALUES'
                '(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('edit1', 'בראשת', 'בראשית', 5.0, 1, 3, 0, 0, 'otzaria',
                 'ספר', 'ref 1', '1', 'ספר', 'בראשת ברא'))
    con.commit()
    con.close()


class WebUiUriTest(unittest.TestCase):
    """The review UI opens ui_review.db (and ATTACHes report.db) through the
    same URI builder: an output folder on a network share must work too."""

    @unittest.skipUnless(os.name == 'nt', 'Windows paths')
    def test_local_uri(self):
        self.assertEqual(webui_db._uri(r'C:\scan\ui_review.db'),
                         'file:///C:/scan/ui_review.db')
        self.assertEqual(webui_db._uri(r'C:\scan\report.db', ro=True),
                         'file:///C:/scan/report.db?mode=ro')

    @unittest.skipUnless(os.name == 'nt', 'Windows paths')
    def test_awkward_uri(self):
        self.assertEqual(webui_db._uri(r'C:\תוצאות\a b#c%d\ui_review.db'),
                         'file:///C:/%D7%AA%D7%95%D7%A6%D7%90%D7%95%D7%AA/'
                         'a%20b%23c%25d/ui_review.db')

    @unittest.skipUnless(os.name == 'nt', 'Windows paths')
    def test_unc_uri_keeps_server_and_share(self):
        # pathname2url gave file://server/share/... — "invalid uri authority"
        self.assertEqual(webui_db._uri(r'\\server\share\a b\ui_review.db'),
                         'file:////server/share/a%20b/ui_review.db')
        self.assertEqual(webui_db._uri(r'\\server\share\report.db', ro=True),
                         'file:////server/share/report.db?mode=ro')

    def _import_and_check(self, outdir):
        con = webui_db.connect(outdir)
        con.close()
        counts = webui_db.import_all(outdir)
        self.assertEqual(counts['error'], 1)
        con = webui_db.connect(outdir)
        try:
            self.assertEqual(con.execute(
                'SELECT word FROM findings').fetchall()[0][0], 'בראשת')
        finally:
            con.close()

    def test_awkward_local_outdir_imports(self):
        with tempfile.TemporaryDirectory(prefix='תוצאות # 100% ') as d:
            make_report_db(os.path.join(d, webui_db.REPORT_DB_F))
            self._import_and_check(d)

    @unittest.skipUnless(os.name == 'nt' and os.path.isdir(r'\\localhost\C$'),
                         'needs the \\\\localhost\\C$ admin share')
    def test_unc_outdir_imports(self):
        with tempfile.TemporaryDirectory(prefix='תוצאות # ') as d:
            drive, rest = os.path.splitdrive(os.path.abspath(d))
            if len(drive) != 2:
                self.skipTest('temp dir is not on a drive letter')
            unc = '\\\\localhost\\' + drive[0] + '$' + rest
            make_report_db(os.path.join(unc, webui_db.REPORT_DB_F))
            self._import_and_check(unc)
            self.assertTrue(os.path.isfile(
                os.path.join(d, webui_db.UI_DB_F)))


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
