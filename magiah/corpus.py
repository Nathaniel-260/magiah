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
        for uid, _, text in self.iter_texts_docs(chunk):
            yield uid, text

    def _doc_col(self):
        return self.spec.get('doc_col') or (
            'bookId' if self.spec.get('preset') == 'otzaria' else None)

    def iter_texts_docs(self, chunk):
        """Yield (unit_id, doc_id, text); doc_id groups units into documents
        (books). Empty string when the table has no document column."""
        lo, hi = chunk
        doc_col = self._doc_col()
        con = sqlite3.connect(self.path)
        try:
            if doc_col:
                for uid, doc, text in con.execute(
                        f'SELECT {self.id_col}, {doc_col}, {self.text_col} '
                        f'FROM {self.table} '
                        f'WHERE {self.id_col}>=? AND {self.id_col}<? '
                        f'AND {self.text_col} IS NOT NULL', (lo, hi)):
                    yield str(uid), str(doc), text
            else:
                for uid, text in con.execute(
                        f'SELECT {self.id_col}, {self.text_col} '
                        f'FROM {self.table} '
                        f'WHERE {self.id_col}>=? AND {self.id_col}<? '
                        f'AND {self.text_col} IS NOT NULL', (lo, hi)):
                    yield str(uid), '', text
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
              SELECT o.word, e.errtype,
                     CASE WHEN o.tanach_sugg != '' THEN o.tanach_sugg
                          ELSE e.suggestion END AS suggestion,
                     e.score, o.ctx_hits, o.sugg_local, o.book_repeat,
                     o.tanach,
                     b.title AS source, l.heRef AS ref, o.unit, o.snippet,
                     COALESCE(sr.name, 'Unknown') AS origin
              FROM occurrences o
              JOIN errors e ON e.word = o.word
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
                     t.snippet, COALESCE(sr.name, 'Unknown') AS origin
              FROM tanach_matches t
              JOIN src.line l ON l.id = CAST(t.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId
              LEFT JOIN src.source sr ON sr.id = b.sourceId;
            CREATE TABLE tanach_errors_full AS
              SELECT t.word, t.canonical, b.title AS source, l.heRef AS ref,
                     t.unit, t.snippet,
                     COALESCE(sr.name, 'Unknown') AS origin
              FROM tanach_errors t
              JOIN src.line l ON l.id = CAST(t.unit AS INTEGER)
              JOIN src.book b ON b.id = l.bookId
              LEFT JOIN src.source sr ON sr.id = b.sourceId;
            DROP TABLE occurrences;
            DROP TABLE space_errors;
            DROP TABLE tanach_matches;
            DROP TABLE tanach_errors;
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
        for uid, _, text in self.iter_texts_docs(chunk):
            yield uid, text

    def iter_texts_docs(self, chunk):
        for fp in chunk:
            rel = os.path.relpath(fp, self.path)
            with open(fp, encoding=self.encoding, errors='replace') as f:
                for lineno, text in enumerate(f, 1):
                    yield f'{rel}:{lineno}', rel, text

    def enrich(self, con):
        _default_enrich(con)


def _default_enrich(con):
    con.executescript('''
        CREATE TABLE occurrences_full AS
          SELECT o.word, e.errtype, e.suggestion, e.score, o.ctx_hits,
                 o.sugg_local, o.book_repeat, o.tanach,
                 o.doc AS source, '' AS ref, o.unit, o.snippet,
                 '' AS origin
          FROM occurrences o JOIN errors e ON e.word = o.word;
        CREATE TABLE space_errors_full AS
          SELECT part1, part2, joined, join_freq,
                 '' AS source, '' AS ref, unit, snippet, '' AS origin
          FROM space_errors;
        CREATE TABLE tanach_matches_full AS
          SELECT word, doc AS source, '' AS ref, unit, snippet,
                 '' AS origin
          FROM tanach_matches;
        CREATE TABLE tanach_errors_full AS
          SELECT word, canonical, '' AS source, '' AS ref, unit, snippet,
                 '' AS origin
          FROM tanach_errors;
        DROP TABLE occurrences;
        DROP TABLE space_errors;
        DROP TABLE tanach_matches;
        DROP TABLE tanach_errors;
    ''')
    con.commit()
