# -*- coding: utf-8 -*-
"""Corpus adapters.

A corpus spec is a small picklable dict, so worker processes can rebuild the
adapter themselves. Two adapters are provided:

* ``sqlite``  — any SQLite table with an integer id column and a text column.
  The ``otzaria`` preset points at the Otzaria seforim.db and enriches the
  report with book title and heRef.
* ``textdir`` — a directory tree of ``.txt`` files, one unit per line.
"""
import glob
import os
import sqlite3

from . import tanach
from .textsource import OtzariaDB, ReadStats, iter_file_lines

LEGACY_OTZARIA_DB = r'C:\ProgramData\otzaria\books\seforim.db'


def _read_library_path(path):
    """The library folder named in Otzaria's ``library_path.txt``, or ''.

    Runs at import time (through OTZARIA_DB), so it must never raise: a file
    saved as UTF-16 or cp1255 used to stop every command, ``ui`` included,
    with a UnicodeDecodeError. UTF-8 (with or without BOM) and UTF-16 with a
    BOM are read; anything else falls back to the default path. Only the
    first non-empty line counts, without surrounding quotes.
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read(64 * 1024)
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = raw.decode('utf-16')
        else:
            text = raw.decode('utf-8-sig')
    except (OSError, UnicodeError, ValueError):
        return ''
    for line in text.splitlines():
        line = line.strip().strip('"\'').strip()
        if line:
            return line
    return ''


def default_otzaria_db():
    """The seforim.db the Otzaria app actually uses on this machine.

    The app records its library folder in ``%APPDATA%\\otzaria\\library_path.txt``;
    the historical ProgramData path is only a last resort.
    """
    appdata = os.environ.get('APPDATA')
    if appdata:
        lib = _read_library_path(
            os.path.join(appdata, 'otzaria', 'library_path.txt'))
        try:
            cand = os.path.join(lib, 'seforim.db')
            if lib and os.path.isfile(cand):
                return cand
        except (OSError, ValueError):
            pass
    return LEGACY_OTZARIA_DB


OTZARIA_DB = default_otzaria_db()


def make_corpus(spec):
    if spec['type'] == 'sqlite':
        return SqliteCorpus(spec)
    if spec['type'] == 'textdir':
        return TextDirCorpus(spec)
    if spec['type'] in ('library', 'hybrid'):
        from .corpus_hybrid import make_hybrid_corpus
        return make_hybrid_corpus(spec)
    raise ValueError(f"unknown corpus type: {spec['type']}")


class SqliteCorpus:
    def __init__(self, spec):
        self.spec = spec
        self.path = spec['path']
        self.table = spec.get('table', 'line')
        self.id_col = spec.get('id_col', 'id')
        self.text_col = spec.get('text_col', 'content')
        self.stats = ReadStats()
        self._odb = None

    def _otzaria(self):
        """The schema-aware reader, for the otzaria preset."""
        if self._odb is None:
            self._odb = OtzariaDB(self.path)
        return self._odb

    def close(self):
        if self._odb is not None:
            self._odb.close()
            self._odb = None

    def chunks(self, n):
        if self.spec.get('preset') == 'otzaria':
            with OtzariaDB(self.path) as odb:
                lo, hi = odb.id_range()
        else:
            con = sqlite3.connect(self.path)
            lo, hi = con.execute(
                f'SELECT MIN({self.id_col}), MAX({self.id_col}) '
                f'FROM {self.table}').fetchone()
            con.close()
        if lo is None:
            return []
        step = (hi - lo) // n + 1
        return [(lo + i * step, min(lo + (i + 1) * step, hi + 1))
                for i in range(n)]

    def iter_texts(self, chunk):
        """Yield (unit_id, text) for one chunk. unit_id is a string."""
        for uid, _, text in self.iter_texts_docs(chunk):
            yield uid, text

    def _doc_col(self):
        return self.spec.get('doc_col') or (
            'bookId' if self.spec.get('preset') == 'otzaria' else None)

    def iter_texts_docs(self, chunk):
        """Yield (unit_id, doc_id, text); doc_id groups units into documents
        (books). Empty string when the table has no document column."""
        lo, hi = chunk
        if self.spec.get('preset') == 'otzaria':
            odb = self._otzaria()
            for uid, doc, text in odb.iter_range(lo, hi, self.stats):
                yield str(uid), str(doc), text
            self.stats.version_lines_skipped += odb.count_version_lines(lo, hi)
            return
        doc_col = self._doc_col()
        con = sqlite3.connect(self.path)
        try:
            doc_sel = doc_col if doc_col else "''"
            for uid, doc, text in con.execute(
                    f'SELECT {self.id_col}, {doc_sel}, {self.text_col} '
                    f'FROM {self.table} '
                    f'WHERE {self.id_col}>=? AND {self.id_col}<? '
                    f'AND {self.text_col} IS NOT NULL', (lo, hi)):
                if not isinstance(text, str):
                    # a BLOB here would tokenize to nothing and look like an
                    # empty line — count it as unreadable instead
                    self.stats.unread_row(uid, 'decode_errors',
                                          'non-text value in text column')
                    continue
                self.stats.lines += 1
                self.stats.chars += len(text)
                yield str(uid), str(doc), text
        finally:
            con.close()

    def enrich(self, con):
        """Add source metadata to the report database (best effort)."""
        if self.spec.get('preset') != 'otzaria':
            _default_enrich(con)
            return
        if not os.path.isfile(self.path):
            # ATTACH of a missing path silently creates an empty database
            raise FileNotFoundError(self.path)
        con.execute("ATTACH DATABASE ? AS src", (self.path,))
        con.execute(tanach.EVIDENCE_SCHEMA)
        con.executescript(f'''
            CREATE TABLE occurrences_full AS
              SELECT o.word, e.errtype, e.suggestion,
                     e.score, o.ctx_hits, o.sugg_local, o.book_repeat,
                     o.tanach,
                     b.title AS source, l.heRef AS ref, o.unit, o.snippet,
                     COALESCE(sr.name, 'Unknown') AS origin, o.doc AS doc,
                     {tanach.ENRICH_COLS}
              FROM occurrences o
              JOIN errors e ON e.word = o.word
              {tanach.ENRICH_JOIN}
              JOIN src.line l ON l.id = CAST(o.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId
              LEFT JOIN src.source sr ON sr.id = b.sourceId;
            CREATE TABLE space_errors_full AS
              SELECT s.part1, s.part2, s.joined, s.join_freq,
                     b.title AS source, l.heRef AS ref, s.unit, s.snippet,
                     COALESCE(sr.name, 'Unknown') AS origin
              FROM space_errors s
              JOIN src.line l ON l.id = CAST(s.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId
              LEFT JOIN src.source sr ON sr.id = b.sourceId;
            CREATE TABLE tanach_matches_full AS
              SELECT t.word, b.title AS source, l.heRef AS ref, t.unit,
                     t.snippet, COALESCE(sr.name, 'Unknown') AS origin,
                     t.evidence
              FROM tanach_matches t
              JOIN src.line l ON l.id = CAST(t.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId
              LEFT JOIN src.source sr ON sr.id = b.sourceId;
            CREATE TABLE tanach_errors_full AS
              SELECT t.word, t.canonical, b.title AS source, l.heRef AS ref,
                     t.unit, t.snippet,
                     COALESCE(sr.name, 'Unknown') AS origin, t.evidence
              FROM tanach_errors t
              JOIN src.line l ON l.id = CAST(t.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId
              LEFT JOIN src.source sr ON sr.id = b.sourceId;
            DROP TABLE occurrences;
            DROP TABLE space_errors;
            DROP TABLE tanach_matches;
            DROP TABLE tanach_errors;
            CREATE INDEX ix_occ_word_unit ON occurrences_full(word, unit);
        ''')
        con.commit()


class TextDirCorpus:
    def __init__(self, spec):
        self.spec = spec
        self.path = spec['path']
        self.pattern = spec.get('pattern', '**/*.txt')
        self.encoding = spec.get('encoding', 'utf-8')
        self.stats = ReadStats()

    def _files(self):
        return sorted(glob.glob(os.path.join(self.path, self.pattern),
                                recursive=True))

    def chunks(self, n):
        files = self._files()
        if not files:
            return []
        n = min(n, len(files))
        return [files[i::n] for i in range(n)]

    def iter_texts(self, chunk):
        for uid, _, text in self.iter_texts_docs(chunk):
            yield uid, text

    def iter_texts_docs(self, chunk):
        for fp in chunk:
            rel = os.path.relpath(fp, self.path)
            try:
                for lineno, text in enumerate(
                        iter_file_lines(fp, self.encoding), 1):
                    self.stats.lines += 1
                    self.stats.chars += len(text)
                    yield f'{rel}:{lineno}', rel, text
            except OSError as e:
                self.stats.unread_row(rel, 'decode_errors', repr(e)[:200])

    def enrich(self, con):
        _default_enrich(con)


def _default_enrich(con):
    con.execute(tanach.EVIDENCE_SCHEMA)
    con.executescript(f'''
        CREATE TABLE occurrences_full AS
          SELECT o.word, e.errtype, e.suggestion, e.score, o.ctx_hits,
                 o.sugg_local, o.book_repeat, o.tanach,
                 o.doc AS source, '' AS ref, o.unit, o.snippet,
                 '' AS origin, o.doc AS doc, {tanach.ENRICH_COLS}
          FROM occurrences o JOIN errors e ON e.word = o.word
          {tanach.ENRICH_JOIN};
        CREATE TABLE space_errors_full AS
          SELECT part1, part2, joined, join_freq,
                 '' AS source, '' AS ref, unit, snippet, '' AS origin
          FROM space_errors;
        CREATE TABLE tanach_matches_full AS
          SELECT word, doc AS source, '' AS ref, unit, snippet,
                 '' AS origin, evidence
          FROM tanach_matches;
        CREATE TABLE tanach_errors_full AS
          SELECT word, canonical, '' AS source, '' AS ref, unit, snippet,
                 '' AS origin, evidence
          FROM tanach_errors;
        DROP TABLE occurrences;
        DROP TABLE space_errors;
        DROP TABLE tanach_matches;
        DROP TABLE tanach_errors;
    ''')
    con.commit()
