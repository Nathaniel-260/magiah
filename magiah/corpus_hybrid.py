# -*- coding: utf-8 -*-
"""Hybrid corpus adapters (UI_SPEC §9c, additive — no changes to detection).

Two new corpus types:

* ``library`` — a local clone of the otzaria-library GitHub repo: one book per
  ``.txt`` file, grouped in one top-level folder per source repository (מאגר).
  The top-level folder name IS the origin key (Ben-YehudaToOtzaria,
  DictaToOtzaria, ...). ``sefariaToOtzaria`` is skipped — Sefaria books come
  from seforim.db in hybrid mode. Unit ids are ``file:<repo-relpath>:<lineno>``
  (0-based line numbers, forward-slash relpaths).
* ``hybrid``  — the library repo plus seforim.db lines restricted to books
  whose source is 'Sefaria' (the books that exist only in the DB).

Both implement the exact interface core.py expects from a corpus adapter:
``chunks(n)`` (picklable chunk descriptors), ``iter_texts(chunk)``,
``iter_texts_docs(chunk)`` yielding ``(unit_id, doc_id, text)`` strings, and
``enrich(con)`` producing the same ``*_full`` report tables as corpus.py.
"""
import os
import re
import sqlite3

from .corpus import OTZARIA_DB

DEFAULT_LIBRARY = r'C:\OTZ\otzaria-library'
SEFARIA_SOURCE = 'Sefaria'
FILE_UNIT_PREFIX = 'file:'
FALLBACK_ORIGIN = 'קבצי אוצריא'

# repo top-level folders that hold no book texts (plus sefariaToOtzaria,
# whose books are scanned from seforim.db instead)
EXCLUDED_TOP = {
    '.claude', '.git', '.github', 'ForDB', 'KSK', 'docs', 'library_csv',
    'linker-eval', 'metadata', 'send_update', 'סקריפטים שונות',
    'sefariaToOtzaria',
}

# subfolder names (any level) that hold tooling/links, not books
_SKIP_DIR_RE = re.compile(r'^(סקריפטים|scripts?$|links$|linker_links$|'
                          r'archived_files$)', re.IGNORECASE)

# inside extraBooks: Sefaria dumps are scanned from seforim.db instead
# (verified: most titles exist in the DB with source=Sefaria), and
# 'ספריא מחוק' holds deleted Sefaria books
_EXTRA_SKIP = {'SefariaToOtzria', 'sefariaToOtzaria', 'ספריא מחוק'}

# exact names of curated/raw variant folders (NOT a substring match — book
# categories like 'שולחן ערוך' legitimately contain the word ערוך)
_CURATED_DIRS = {'ערוך', 'דיקטה ערוך'}
_RAW_DIRS = {'לא ערוך'}

_HDR_RE = re.compile(r'<h([1-4])[^>]*>(.*?)</h\1>', re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')


def make_hybrid_corpus(spec):
    if spec['type'] == 'library':
        return LibraryCorpus(spec)
    if spec['type'] == 'hybrid':
        return HybridCorpus(spec)
    raise ValueError(f"unknown corpus type: {spec['type']}")


def parse_file_unit(unit):
    """'file:<relpath>:<lineno>' -> (relpath, lineno) or None."""
    if not isinstance(unit, str) or not unit.startswith(FILE_UNIT_PREFIX):
        return None
    body = unit[len(FILE_UNIT_PREFIX):]
    rel, sep, ln = body.rpartition(':')
    if not sep or not ln.isdigit():
        return None
    return rel, int(ln)


def _header_text(m):
    return _TAG_RE.sub('', m.group(2)).strip()


class LibraryCorpus:
    """One book per .txt file under a repo clone of otzaria-library."""

    def __init__(self, spec):
        self.spec = spec
        self.path = spec.get('path') or DEFAULT_LIBRARY
        self.encoding = spec.get('encoding', 'utf-8')

    # -- enumeration -------------------------------------------------------
    @staticmethod
    def _is_curated(rel):
        """True when the path goes through a curated-variant folder
        (DictaToOtzaria/ערוך/..., extraBooks/דיקטה ערוך/...)."""
        return any(s in _CURATED_DIRS for s in rel.split('/'))

    def _files(self):
        """Sorted repo-relative paths (forward slashes) of all book files.

        Rule (verified against the repo layout, commit ca69c56):
        * one top-level folder per source (skip the non-book folders in
          EXCLUDED_TOP; sefariaToOtzaria holds only scripts — Sefaria is
          scanned from seforim.db);
        * inside a source, skip tooling/link folders (סקריפטים, scripts,
          links, linker_links, archived_files) at any depth;
        * inside extraBooks, also skip the Sefaria dumps (they duplicate the
          DB) and 'ספריא מחוק';
        * curated vs raw duplicates: when the same filename exists both under
          a curated (ערוך) path and a raw/plain path of the SAME source,
          only the curated copy is scanned.
        """
        root = self.path
        try:
            tops = sorted(os.listdir(root))
        except OSError:
            return []
        out = []
        for top in tops:
            if top in EXCLUDED_TOP or top.startswith('.'):
                continue
            top_path = os.path.join(root, top)
            if not os.path.isdir(top_path):
                continue
            rels = []
            for dirpath, dirnames, filenames in os.walk(top_path):
                dirnames[:] = sorted(d for d in dirnames
                                     if not _SKIP_DIR_RE.match(d)
                                     and not (top == 'extraBooks'
                                              and d in _EXTRA_SKIP))
                for fn in sorted(filenames):
                    if fn.endswith('.txt'):
                        rel = os.path.relpath(os.path.join(dirpath, fn),
                                              root)
                        rels.append(rel.replace(os.sep, '/'))
            curated_names = {rel.rsplit('/', 1)[-1] for rel in rels
                             if self._is_curated(rel)}
            for rel in rels:
                if not self._is_curated(rel) \
                        and rel.rsplit('/', 1)[-1] in curated_names:
                    continue          # raw/plain duplicate of a curated copy
                out.append(rel)
        return out

    def origin_of(self, rel):
        """Origin (מאגר) key of a repo-relative path: its top-level folder."""
        top = rel.split('/', 1)[0]
        return top or FALLBACK_ORIGIN

    # -- corpus interface --------------------------------------------------
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
        for rel in chunk:
            fp = os.path.join(self.path, *rel.split('/'))
            try:
                f = open(fp, encoding=self.encoding, errors='replace')
            except OSError:
                continue
            with f:
                for lineno, text in enumerate(f):
                    yield f'{FILE_UNIT_PREFIX}{rel}:{lineno}', rel, text

    # -- enrichment --------------------------------------------------------
    def file_unit_meta(self, units):
        """{unit -> (source, ref, origin)} for file:-units, derived by
        re-reading only the files that actually have findings and tracking
        their <h1>-<h4> headers."""
        by_file = {}
        for u in units:
            parsed = parse_file_unit(u)
            if parsed:
                by_file.setdefault(parsed[0], []).append((parsed[1], u))
        meta = {}
        for rel, wanted in by_file.items():
            wanted.sort()
            title = os.path.splitext(rel.rsplit('/', 1)[-1])[0]
            origin = self.origin_of(rel)
            fp = os.path.join(self.path, *rel.split('/'))
            refs = {}
            try:
                f = open(fp, encoding=self.encoding, errors='replace')
            except OSError:
                f = None
            if f is not None:
                with f:
                    need = {ln for ln, _ in wanted}
                    hdrs = {}          # level -> text
                    for lineno, line in enumerate(f):
                        for m in _HDR_RE.finditer(line):
                            lvl = int(m.group(1))
                            hdrs[lvl] = _header_text(m)
                            for deeper in range(lvl + 1, 5):
                                hdrs.pop(deeper, None)
                        if lineno in need:
                            parts = [hdrs[k] for k in (2, 3, 4)
                                     if hdrs.get(k)]
                            refs[lineno] = (title + (', ' + ' '.join(parts)
                                                     if parts else ''))
            for ln, unit in wanted:
                meta[unit] = (title, refs.get(ln, title), origin)
        return meta

    def enrich(self, con):
        _meta_enrich(con, self.file_unit_meta(_report_units(con)))


class HybridCorpus:
    """LibraryCorpus (repo files) + seforim.db lines of Sefaria-only books."""

    def __init__(self, spec):
        self.spec = spec
        self.library = LibraryCorpus({'type': 'library',
                                      'path': spec.get('path')})
        self.db = spec.get('db') or OTZARIA_DB
        self._db_book_ids = None

    def db_book_ids(self):
        """ids of the seforim.db books scanned from the DB (Sefaria only) —
        computed once per process and cached."""
        if self._db_book_ids is None:
            con = sqlite3.connect(self.db)
            try:
                self._db_book_ids = frozenset(r[0] for r in con.execute(
                    'SELECT b.id FROM book b JOIN source s '
                    'ON s.id = b.sourceId WHERE s.name = ?',
                    (SEFARIA_SOURCE,)))
            finally:
                con.close()
        return self._db_book_ids

    # -- corpus interface --------------------------------------------------
    def chunks(self, n):
        chunks = [('f', c) for c in self.library.chunks(n)]
        con = sqlite3.connect(self.db)
        try:
            lo, hi = con.execute('SELECT MIN(id), MAX(id) FROM line'
                                 ).fetchone()
        finally:
            con.close()
        if lo is not None:
            step = (hi - lo) // n + 1
            chunks.extend(('db', lo + i * step,
                           min(lo + (i + 1) * step, hi + 1))
                          for i in range(n))
        return chunks

    def iter_texts(self, chunk):
        for uid, _, text in self.iter_texts_docs(chunk):
            yield uid, text

    def iter_texts_docs(self, chunk):
        if chunk[0] == 'f':
            yield from self.library.iter_texts_docs(chunk[1])
            return
        _, lo, hi = chunk
        con = sqlite3.connect(self.db)
        try:
            # the uncorrelated IN-subquery is materialized once by SQLite,
            # so non-Sefaria rows are filtered before their content is read
            for uid, book_id, text in con.execute(
                    'SELECT id, bookId, content FROM line '
                    'WHERE id >= ? AND id < ? AND content IS NOT NULL '
                    'AND bookId IN (SELECT b.id FROM book b '
                    '  JOIN source s ON s.id = b.sourceId WHERE s.name = ?)',
                    (lo, hi, SEFARIA_SOURCE)):
                yield str(uid), str(book_id), text
        finally:
            con.close()

    # -- enrichment --------------------------------------------------------
    def enrich(self, con):
        units = _report_units(con)
        meta = self.library.file_unit_meta(units)
        db_units = [u for u in units
                    if u not in meta and u.lstrip('-').isdigit()]
        src = sqlite3.connect(self.db)
        try:
            for i in range(0, len(db_units), 500):
                batch = db_units[i:i + 500]
                ph = ','.join('?' * len(batch))
                for uid, title, ref, origin in src.execute(
                        f'SELECT l.id, b.title, l.heRef, s.name '
                        f'FROM line l JOIN book b ON b.id = l.bookId '
                        f'LEFT JOIN source s ON s.id = b.sourceId '
                        f'WHERE l.id IN ({ph})',
                        [int(u) for u in batch]):
                    meta[str(uid)] = (title or '', ref or '',
                                      origin or 'Unknown')
        finally:
            src.close()
        _meta_enrich(con, meta)


# ---------------------------------------------------------------------------
# shared enrichment: build the *_full tables from a unit -> meta mapping
# ---------------------------------------------------------------------------

def _report_units(con):
    """All distinct unit ids referenced by the raw report tables."""
    units = set()
    for tbl in ('occurrences', 'space_errors', 'tanach_matches',
                'tanach_errors'):
        try:
            units.update(r[0] for r in
                         con.execute(f'SELECT DISTINCT unit FROM {tbl}'))
        except sqlite3.OperationalError:
            pass
    return units


def _meta_enrich(con, meta):
    """Same output contract as corpus.py's otzaria enrich(): *_full tables
    with source/ref/origin columns, raw tables dropped."""
    con.execute('CREATE TEMP TABLE unit_meta('
                'unit TEXT PRIMARY KEY, source TEXT, ref TEXT, origin TEXT)')
    con.executemany('INSERT OR REPLACE INTO unit_meta VALUES(?,?,?,?)',
                    [(u, s, r, o) for u, (s, r, o) in meta.items()])
    con.executescript(f'''
        CREATE TABLE occurrences_full AS
          SELECT o.word, e.errtype,
                 CASE WHEN o.tanach_sugg != '' THEN o.tanach_sugg
                      ELSE e.suggestion END AS suggestion,
                 e.score, o.ctx_hits, o.sugg_local, o.book_repeat, o.tanach,
                 COALESCE(m.source, o.doc) AS source,
                 COALESCE(m.ref, '') AS ref, o.unit, o.snippet,
                 COALESCE(m.origin, '{FALLBACK_ORIGIN}') AS origin,
                 o.doc AS doc
          FROM occurrences o
          JOIN errors e ON e.word = o.word
          LEFT JOIN unit_meta m ON m.unit = o.unit;
        CREATE TABLE space_errors_full AS
          SELECT s.part1, s.part2, s.joined, s.join_freq,
                 COALESCE(m.source, '') AS source,
                 COALESCE(m.ref, '') AS ref, s.unit, s.snippet,
                 COALESCE(m.origin, '{FALLBACK_ORIGIN}') AS origin
          FROM space_errors s LEFT JOIN unit_meta m ON m.unit = s.unit;
        CREATE TABLE tanach_matches_full AS
          SELECT t.word, COALESCE(m.source, t.doc) AS source,
                 COALESCE(m.ref, '') AS ref, t.unit, t.snippet,
                 COALESCE(m.origin, '{FALLBACK_ORIGIN}') AS origin
          FROM tanach_matches t LEFT JOIN unit_meta m ON m.unit = t.unit;
        CREATE TABLE tanach_errors_full AS
          SELECT t.word, t.canonical, COALESCE(m.source, '') AS source,
                 COALESCE(m.ref, '') AS ref, t.unit, t.snippet,
                 COALESCE(m.origin, '{FALLBACK_ORIGIN}') AS origin
          FROM tanach_errors t LEFT JOIN unit_meta m ON m.unit = t.unit;
        DROP TABLE occurrences;
        DROP TABLE space_errors;
        DROP TABLE tanach_matches;
        DROP TABLE tanach_errors;
        CREATE INDEX ix_occ_word_unit ON occurrences_full(word, unit);
    ''')
    con.execute('DROP TABLE unit_meta')
    con.commit()
