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

OTZARIA_DB = r'C:\ProgramData\otzaria\books\seforim.db'


def make_corpus(spec):
    if spec['type'] == 'sqlite':
        return SqliteCorpus(spec)
    if spec['type'] == 'textdir':
        return TextDirCorpus(spec)
    raise ValueError(f"unknown corpus type: {spec['type']}")


class SqliteCorpus:
    def __init__(self, spec):
        self.spec = spec
        self.path = spec['path']
        self.table = spec.get('table', 'line')
        self.id_col = spec.get('id_col', 'id')
        self.text_col = spec.get('text_col', 'content')

    def chunks(self, n):
        con = sqlite3.connect(self.path)
        lo, hi = con.execute(
            f'SELECT MIN({self.id_col}), MAX({self.id_col}) FROM {self.table}'
        ).fetchone()
        con.close()
        if lo is None:
            return []
        step = (hi - lo) // n + 1
        return [(lo + i * step, min(lo + (i + 1) * step, hi + 1))
                for i in range(n)]

    def iter_texts(self, chunk):
        """Yield (unit_id, text) for one chunk. unit_id is a string."""
        lo, hi = chunk
        con = sqlite3.connect(self.path)
        try:
            for uid, text in con.execute(
                    f'SELECT {self.id_col}, {self.text_col} FROM {self.table} '
                    f'WHERE {self.id_col}>=? AND {self.id_col}<? '
                    f'AND {self.text_col} IS NOT NULL', (lo, hi)):
                yield str(uid), text
        finally:
            con.close()

    def enrich(self, con):
        """Add source metadata to the report database (best effort)."""
        if self.spec.get('preset') != 'otzaria':
            _default_enrich(con)
            return
        con.execute("ATTACH DATABASE ? AS src", (self.path,))
        con.executescript('''
            CREATE TABLE occurrences_full AS
              SELECT o.word, e.errtype, e.suggestion, e.score, o.ctx_hits,
                     b.title AS source, l.heRef AS ref, o.unit, o.snippet
              FROM occurrences o
              JOIN errors e ON e.word = o.word
              JOIN src.line l ON l.id = CAST(o.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId;
            CREATE TABLE space_errors_full AS
              SELECT s.part1, s.part2, s.joined, s.join_freq,
                     b.title AS source, l.heRef AS ref, s.unit, s.snippet
              FROM space_errors s
              JOIN src.line l ON l.id = CAST(s.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId;
            DROP TABLE occurrences;
            DROP TABLE space_errors;
        ''')
        con.commit()


class TextDirCorpus:
    def __init__(self, spec):
        self.spec = spec
        self.path = spec['path']
        self.pattern = spec.get('pattern', '**/*.txt')
        self.encoding = spec.get('encoding', 'utf-8')

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
        for fp in chunk:
            rel = os.path.relpath(fp, self.path)
            with open(fp, encoding=self.encoding, errors='replace') as f:
                for lineno, text in enumerate(f, 1):
                    yield f'{rel}:{lineno}', text

    def enrich(self, con):
        _default_enrich(con)


def _default_enrich(con):
    con.executescript('''
        CREATE TABLE occurrences_full AS
          SELECT o.word, e.errtype, e.suggestion, e.score, o.ctx_hits,
                 '' AS source, '' AS ref, o.unit, o.snippet
          FROM occurrences o JOIN errors e ON e.word = o.word;
        CREATE TABLE space_errors_full AS
          SELECT part1, part2, joined, join_freq,
                 '' AS source, '' AS ref, unit, snippet
          FROM space_errors;
        DROP TABLE occurrences;
        DROP TABLE space_errors;
    ''')
    con.commit()
