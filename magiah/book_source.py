# -*- coding: utf-8 -*-
"""Locating and reading ONE book, for the single-book scan.

Three sources, each producing the *same* identity triple the full pipeline
produces, so a single-book finding is indistinguishable from a full-scan one
and replaces it cleanly:

======  ==========================  ==========================================
source  doc (book identity)         unit (line identity)
======  ==========================  ==========================================
db      str(bookId)                 str(line.id)
library <repo-relpath>              ``file:<repo-relpath>:<lineno>``  (0-based)
file    ``local:<abspath>``         ``local:<abspath>:<lineno>``      (0-based)
======  ==========================  ==========================================

`library` reuses LibraryCorpus's own relpath/origin/header conventions rather
than re-deriving them, so a book scanned alone lands on exactly the rows a
hybrid full scan would have produced for it.

A `local` book is one that is NOT inside the library repo — an arbitrary .txt
on disk. When the given path *is* inside the configured repo it is silently
promoted to a `library` book, so scanning "the file I just edited" updates the
right rows instead of creating a parallel `local:` copy of the same book.
"""
import hashlib
import os
import sqlite3

from . import core
from .corpus import OTZARIA_DB
from .textsource import (OtzariaDB, ReadStats, TextSourceError, ro_uri,
                         split_lines)
from .corpus_hybrid import (DEFAULT_LIBRARY, FILE_UNIT_PREFIX, LibraryCorpus,
                            _HDR_RE, _header_text)

LOCAL_UNIT_PREFIX = 'local:'
LOCAL_ORIGIN = 'קבצים מקומיים'

# A single book is read whole into memory. Guard against a user pointing at a
# multi-GB file by mistake: 200 MB of Hebrew text is ~2.5x the largest real
# book in the corpus, so anything above it is not a book.
MAX_BOOK_BYTES = 200 * 1024 * 1024


class BookNotFound(Exception):
    """The requested book does not exist / cannot be read (Hebrew message)."""


def file_fingerprint(data):
    """Content identity of a book file's bytes, in the form the fixer
    compares (``webui.patcher.fingerprint_bytes`` is this function)."""
    return 'sha256:' + hashlib.sha256(data).hexdigest()[:32]


def parse_local_unit(unit):
    """'local:<abspath>:<lineno>' -> (abspath, lineno) or None.

    The path itself contains ':' on Windows (C:\\...), so split from the right
    exactly like parse_file_unit does.
    """
    if not isinstance(unit, str) or not unit.startswith(LOCAL_UNIT_PREFIX):
        return None
    body = unit[len(LOCAL_UNIT_PREFIX):]
    path, sep, ln = body.rpartition(':')
    if not sep or not ln.isdigit():
        return None
    return path, int(ln)


# ---------------------------------------------------------------------------
# listing (for the UI's book picker)
# ---------------------------------------------------------------------------

def list_db_books(db_path=None, query='', limit=200):
    """Books in seforim.db matching `query`, newest-irrelevant, title order."""
    db_path = db_path or OTZARIA_DB
    if not os.path.isfile(db_path):
        raise BookNotFound(f'קובץ מסד הנתונים לא נמצא: {db_path}')
    sql = ('SELECT b.id, b.title, COALESCE(s.name, \'\'), b.totalLines '
           'FROM book b LEFT JOIN source s ON s.id = b.sourceId')
    params = []
    if query:
        sql += ' WHERE b.title LIKE ?'
        params.append('%' + query + '%')
    sql += ' ORDER BY b.title LIMIT ?'
    params.append(int(limit))
    try:
        con = _connect_ro(db_path)
        try:
            return [{'key': str(r[0]), 'title': r[1],
                     'origin': r[2] or 'Unknown', 'lines': r[3],
                     'source': 'db'}
                    for r in con.execute(sql, params)]
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        # not SQLite at all ("file is not a database"), not Otzaria's ("no
        # such table: book"), or damaged: the CLI and the UI's book picker
        # show this, never a traceback
        raise BookNotFound(
            f'לא ניתן לקרוא את רשימת הספרים מהקובץ {db_path}: {e}\n'
            'ייתכן שאינו מסד הספרים של אוצריא (seforim.db) או שהוא '
            'פגום.') from e


def list_library_books(library_dir=None, query='', limit=200):
    """Book files in the library repo whose filename matches `query`."""
    library_dir = library_dir or DEFAULT_LIBRARY
    if not os.path.isdir(library_dir):
        raise BookNotFound(f'תיקיית ריפו הספרייה לא נמצאה: {library_dir}')
    lib = LibraryCorpus({'type': 'library', 'path': library_dir})
    q = (query or '').strip()
    out = []
    for rel in sorted(_scanned_files(library_dir, lib, refresh=True)):
        title = os.path.splitext(rel.rsplit('/', 1)[-1])[0]
        if q and q not in title and q not in rel:
            continue
        out.append({'key': rel, 'title': title,
                    'origin': lib.origin_of(rel), 'path': rel,
                    'source': 'library'})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# reading one book
# ---------------------------------------------------------------------------

_scanned_cache = {}


def _scanned_files(library_dir, lib, refresh=False):
    """The repo files a scan actually reads, cached per library dir.

    ``LibraryCorpus._files()`` walks ~17k files; a book scan asks for this
    twice, so the walk is cached. The repo DOES change mid-session (a user
    adds a book and scans it), so the picker always refreshes and a cache
    miss is re-checked before a book is refused.
    """
    key = os.path.abspath(library_dir)
    hit = None if refresh else _scanned_cache.get(key)
    if hit is None:
        hit = frozenset(lib._files())
        _scanned_cache[key] = hit
    return hit


def _connect_ro(path):
    con = sqlite3.connect(ro_uri(path), uri=True, timeout=30.0)
    con.execute('PRAGMA busy_timeout=30000')
    return con


class BookText:
    """One book's lines, plus the identity/metadata the report rows need.

    `lines` is a list of ``(unit, ref, text)``; `ref` is the per-line reference
    shown in the UI (heRef in the DB, the running <h2>-<h4> header trail in a
    file). A book read from a file also carries ``file_sha`` / ``file_size``:
    the fingerprint of the very bytes these lines were decoded from. `stats`
    is what reading a DB book counted — including the rows skipped under
    ``allow_unread`` — and None for a file, which is read whole or not at
    all.
    """

    def __init__(self, doc, title, origin, lines, kind, path=None,
                 file_sha=None, file_size=None, stats=None):
        self.doc = doc
        self.title = title
        self.origin = origin
        self.lines = lines
        self.kind = kind          # 'db' | 'library' | 'file'
        self.path = path
        self.file_sha = file_sha
        self.file_size = file_size
        self.stats = stats

    def __len__(self):
        return len(self.lines)


def _read_text_file(path, encoding='utf-8'):
    """``(lines, sha, size)`` of one read of the file.

    The fingerprint is taken from the bytes that were decoded, never from a
    second read: a book edited while it is being scanned must not look
    unchanged afterwards (the fixer trusts line numbers of an unchanged book).
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise BookNotFound(f'לא ניתן לקרוא את הקובץ: {path} ({e})')
    if size > MAX_BOOK_BYTES:
        raise BookNotFound(
            f'הקובץ גדול מדי לסריקת ספר בודד ({size / 1e6:.0f} MB). '
            f'המגבלה היא {MAX_BOOK_BYTES / 1e6:.0f} MB.')
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        raise BookNotFound(f'לא ניתן לקרוא את הקובץ: {path} ({e})')
    # decoded the way text mode with newline='' decodes: no newline
    # translation. NOT str.splitlines(): it also breaks on U+2028 and
    # friends, which would number lines differently from the full scan and
    # from the patcher
    return (split_lines(data.decode(encoding, errors='replace')),
            file_fingerprint(data), len(data))


def _file_refs(raw_lines, title):
    """Per-line reference from the running <h2>-<h4> header trail.

    Same rule LibraryCorpus.file_unit_meta uses, so a file book scanned alone
    gets byte-identical `ref` values to a full hybrid scan of the same file.
    """
    refs = []
    hdrs = {}
    for line in raw_lines:
        for m in _HDR_RE.finditer(line):
            lvl = int(m.group(1))
            hdrs[lvl] = _header_text(m)
            for deeper in range(lvl + 1, 5):
                hdrs.pop(deeper, None)
        parts = [hdrs[k] for k in (2, 3, 4) if hdrs.get(k)]
        refs.append(title + (', ' + ' '.join(parts) if parts else ''))
    return refs


def _rel_within(path, root):
    """Repo-relative forward-slash path if `path` is inside `root`, else None."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:                     # different drive on Windows
        return None
    if rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        return None
    return rel.replace(os.sep, '/')


def canonical_path(path):
    """Absolute path in a stable form, for use as a book identity.

    Collapses ``.``/``..``/duplicate separators. On Windows the *filesystem* is
    case-insensitive, so ``C:\\x\\Book.txt`` and ``c:\\X\\BOOK.TXT`` are one
    file and must yield one ``doc`` — but the case is NOT folded here: `doc`
    values written by a full scan carry the real on-disk spelling, and folding
    would stop a book scan from matching them (duplicating the very book it
    means to replace). Case is unified instead by asking the filesystem for the
    real spelling; if that fails, the input spelling is kept.
    """
    p = os.path.abspath(path)
    try:                                    # real on-disk casing (Py3.6+)
        return os.path.realpath(p)
    except OSError:
        return p


def canonical_rel(rel):
    """Library-relative path in a stable form (see :func:`canonical_path`).

    Collapses ``.``/``..``/duplicate separators so ``a//b.txt``, ``./a/b.txt``
    and ``a/c/../b.txt`` all name one book. Case is preserved, for the reason
    given in :func:`canonical_path`.
    """
    rel = str(rel).replace('\\', '/').strip('/')
    if not rel:
        return ''
    return os.path.normpath(rel).replace(os.sep, '/')


def book_identity(source, key, library_dir=None):
    """``(source, key)`` naming the book that ``load_book(source, key)``
    reads, resolved the way the loaders resolve it — without reading it, so
    a scan that fails before (or because) the book cannot be read still has
    one. A file inside the library is that library book; ``./a//b.txt`` is
    ``a/b.txt``; a database id loses its leading zeros. Keys are folded to
    the file system's case, which makes them identities, not display text.
    """
    key = str(key).strip()
    if source == 'db':
        return source, str(int(key)) if key.isdigit() else key
    lib_dir = library_dir or DEFAULT_LIBRARY
    if source == 'file':
        path = key.strip('"')
        if not path:
            return source, ''
        real = canonical_path(path)
        if os.path.isdir(lib_dir):          # as _load_file_book decides
            rel = _rel_within(real, lib_dir)
            if rel is not None and rel.lower().endswith('.txt'):
                return book_identity('library', rel, lib_dir)
        return source, os.path.normcase(real)
    if source == 'library':
        rel = canonical_rel(key)
        if rel:                             # the repo's own spelling
            rel = _rel_within(canonical_path(
                os.path.join(lib_dir, *rel.split('/'))), lib_dir) or rel
        return source, os.path.normcase(rel).replace(os.sep, '/')
    return source, key


def load_book(source, key, db_path=None, library_dir=None, allow_unread=0):
    """Read one book. `source` is 'db' | 'library' | 'file'.

    A DB book with unreadable rows is refused, unless there are at most
    `allow_unread` of them: then the rest of the book is returned and the
    skipped rows are counted in ``BookText.stats``, for the scan to mark its
    result partial — the same rule the full scan applies.

    Returns a BookText. Raises BookNotFound with a Hebrew message.
    """
    if source == 'db':
        return _load_db_book(key, db_path, allow_unread)
    if source == 'library':
        return _load_library_book(key, library_dir)
    if source == 'file':
        return _load_file_book(key, library_dir)
    raise BookNotFound(f'מקור ספר לא מוכר: {source}')


def _load_db_book(key, db_path, allow_unread=0):
    db_path = db_path or OTZARIA_DB
    if not os.path.isfile(db_path):
        raise BookNotFound(f'קובץ מסד הנתונים לא נמצא: {db_path}')
    try:
        book_id = int(str(key).strip())
    except (TypeError, ValueError):
        raise BookNotFound(f'מזהה ספר לא תקין: {key}')
    try:
        odb = OtzariaDB(db_path)
    except TextSourceError as e:
        raise BookNotFound(str(e))
    stats = ReadStats()
    try:
        row = odb.con.execute(
            "SELECT b.title, COALESCE(s.name, '') FROM book b "
            'LEFT JOIN source s ON s.id = b.sourceId WHERE b.id = ?',
            (book_id,)).fetchone()
        if row is None:
            raise BookNotFound(f'הספר לא נמצא במסד הנתונים (מזהה {book_id})')
        title, origin = row[0], row[1] or 'Unknown'
        lines = [(str(uid), ref, text)
                 for uid, ref, text in odb.book_lines(book_id, stats)]
    finally:
        odb.close()
    unread = stats.unread()
    if unread > max(allow_unread, 0):
        # a book with unreadable (or missing) rows must not be scanned as if
        # complete
        gaps = core.gap_record({'book': stats}, {}, allow_unread, db_path)
        msg = [f'{unread:,} שורות בספר "{title}" לא נקראו (פגומות או חסרות '
               f'במסד הנתונים); הסריקה בוטלה כדי לא להציג תוצאה חלקית.']
        if allow_unread > 0:
            msg.append(f'הריצה אישרה לדלג על {allow_unread:,} שורות לכל '
                       f'היותר.')
        msg += core.refs_lines(gaps['unread_refs'], unread)
        msg.append(core.proceed_hint(unread))
        raise BookNotFound('\n'.join(msg))
    if not lines:
        if unread:
            raise BookNotFound(f'אף אחת מ-{unread:,} השורות בספר "{title}" '
                               f'אינה קריאה במסד הנתונים — אין מה לסרוק')
        raise BookNotFound(f'לא נמצאו שורות טקסט בספר "{title}"')
    return BookText(str(book_id), title, origin, lines, 'db', stats=stats)


def _load_library_book(rel, library_dir):
    library_dir = library_dir or DEFAULT_LIBRARY
    if not os.path.isdir(library_dir):
        raise BookNotFound(f'תיקיית ריפו הספרייה לא נמצאה: {library_dir}')
    rel = canonical_rel(rel)
    if not rel:
        raise BookNotFound('לא נבחר ספר')
    path = os.path.join(library_dir, *rel.split('/'))
    # the repo's own spelling wins, so 'ABC/x.txt' and 'abc/x.txt' become the
    # one doc a full scan would have written
    real_rel = _rel_within(canonical_path(path), library_dir)
    if real_rel:
        rel = real_rel
        path = os.path.join(library_dir, *rel.split('/'))
    # containment check: a crafted '../..' relpath must not read outside the repo
    if _rel_within(path, library_dir) is None:
        raise BookNotFound(f'נתיב הספר חורג מתיקיית הספרייה: {rel}')
    if not os.path.isfile(path):
        raise BookNotFound(f'קובץ הספר לא נמצא: {path}')
    lib = LibraryCorpus({'type': 'library', 'path': library_dir})
    # Only ~10k of the repo's ~17k .txt files are books a scan reads: the rest
    # are scripts, link tables, Sefaria dumps, and the RAW copies of books
    # whose curated (ערוך) version is what gets scanned. Scanning one of those
    # would create a second, phantom `doc` for a book already reviewed under
    # its real path — so refuse, and point at the curated twin when there is
    # one.
    scanned = _scanned_files(library_dir, lib)
    if rel not in scanned:
        scanned = _scanned_files(library_dir, lib, refresh=True)
    if rel not in scanned:
        name = rel.rsplit('/', 1)[-1]
        twin = next((r for r in scanned
                     if r.rsplit('/', 1)[-1] == name), None)
        msg = (f'הקובץ אינו חלק מהספרים שהסריקה קוראת: {rel}\n'
               'ייתכן שזו גרסה "לא ערוך" של ספר, תיקיית סקריפטים, או עותק '
               'של ספר ספריא שנסרק מתוך מסד הנתונים.')
        if twin:
            msg += f'\nהגרסה שנסרקת בפועל היא: {twin}'
        raise BookNotFound(msg)
    raw, sha, size = _read_text_file(path)
    title = os.path.splitext(rel.rsplit('/', 1)[-1])[0]
    origin = lib.origin_of(rel)
    refs = _file_refs(raw, title)
    lines = [(f'{FILE_UNIT_PREFIX}{rel}:{i}', refs[i], t)
             for i, t in enumerate(raw)]
    return BookText(rel, title, origin, lines, 'library', path=path,
                    file_sha=sha, file_size=size)


def _load_file_book(path, library_dir):
    path = str(path).strip().strip('"')
    if not path:
        raise BookNotFound('לא נבחר קובץ')
    if not os.path.isfile(path):
        raise BookNotFound(f'הקובץ לא נמצא: {path}')
    # inside the configured repo -> treat as that library book, so a re-scan
    # updates the same rows a full hybrid scan produced
    lib_dir = library_dir or DEFAULT_LIBRARY
    if os.path.isdir(lib_dir):
        rel = _rel_within(canonical_path(path), lib_dir)
        if rel is not None and rel.lower().endswith('.txt'):
            return _load_library_book(rel, lib_dir)
    abspath = canonical_path(path)
    raw, sha, size = _read_text_file(abspath)
    title = os.path.splitext(os.path.basename(abspath))[0]
    refs = _file_refs(raw, title)
    lines = [(f'{LOCAL_UNIT_PREFIX}{abspath}:{i}', refs[i], t)
             for i, t in enumerate(raw)]
    return BookText(LOCAL_UNIT_PREFIX + abspath, title, LOCAL_ORIGIN,
                    lines, 'file', path=abspath, file_sha=sha, file_size=size)
