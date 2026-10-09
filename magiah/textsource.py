# -*- coding: utf-8 -*-
"""One reader for Otzaria's seforim.db, shared by every consumer.

Why a dedicated layer
---------------------
seforim.db has changed shape under Magiah's feet. Older databases kept the
text in ``line.content`` as plain TEXT. Schema 6 moved it to
``line_content.content`` and — once a ``zstd_dict`` table exists — stores each
row as a zstd frame compressed with that one dictionary. Code that queries
``line.content`` directly fails on such a database ("no such column"), and
code that reads the BLOB without decoding it tokenizes binary garbage into an
empty lexicon. Both used to be silent; the full scan, the single-book scan,
the Tanach index and the report enrichment each had their own query.

This module is the only place that knows the layout. It inspects the schema
at open time, decodes per row (BLOB -> zstd frame, TEXT -> as-is, exactly as
the Otzaria app does) and counts every row it could not read, so a caller can
refuse to present a partial pass as a complete one.

The zstd frames here are single-segment frames with a dictionary id; they do
NOT need ``--long``. (That flag concerns the outer ``seforim-schema6.db.zst``
release download, which is a different compression layer entirely.)

Versions
--------
``book_version`` / ``version_line`` hold alternative editions. A NULL
``version_line.content`` means "identical to the primary text", so only rows
with content carry a different reading. Those rows are NOT independent
witnesses by default — they are usually another digitisation of the same
edition — and scanning them would double-count words in the lexicon. They are
therefore excluded unless explicitly requested, and the exclusion is counted.
"""
import os
import sqlite3
import urllib.parse


class TextSourceError(Exception):
    """The database cannot be read as an Otzaria text source (Hebrew)."""


# ---------------------------------------------------------------------------
# zstd backend: stdlib on Python >= 3.14, the `zstandard` package before that
# ---------------------------------------------------------------------------

def _make_decoder(dict_bytes):
    """Return ``decode(bytes) -> bytes`` for frames made with `dict_bytes`."""
    try:
        from compression import zstd as _z          # Python 3.14+
        zd = _z.ZstdDict(dict_bytes)

        def decode(data):
            return _z.decompress(data, zstd_dict=zd)
        return decode, 'compression.zstd'
    except ImportError:
        pass
    try:
        import zstandard as _zs
    except ImportError:
        raise TextSourceError(
            'מסד הנתונים דחוס ב-zstd, ואין בסביבה מפענח zstd.\n'
            'יש להתקין את החבילה zstandard (pip install zstandard) '
            'או להשתמש ב-Python 3.14 ומעלה.')
    dctx = _zs.ZstdDecompressor(dict_data=_zs.ZstdCompressionDict(dict_bytes))

    def decode(data):
        # frames carry their content size; max_output_size guards the rare
        # frame written without it
        return dctx.decompress(data, max_output_size=64 * 1024 * 1024)
    return decode, 'zstandard'


def sqlite_uri(path, ro=False):
    """SQLite ``file:`` URI for `path`; read-only (``?mode=ro``) if `ro`.

    Built by hand rather than with ``pathname2url``: on Python 3.14 that turns
    ``\\\\server\\share\\...`` into ``//server/share/...``, so the URI becomes
    ``file://server/share/...`` and SQLite rejects ``server`` as an authority
    ("invalid uri authority") — a database on a network share could not be
    opened at all. SQLite wants an empty authority followed by the UNC path
    (``file:////server/share/``). Spaces, ``#``, ``%``, ``?`` and Hebrew are
    percent-encoded (UTF-8).
    """
    p = os.path.abspath(path)
    if os.name == 'nt':
        p = p.replace('\\', '/')
    p = urllib.parse.quote(p, safe='/:')
    if not p.startswith('/'):
        p = '/' + p                     # drive path: file:///C:/...
    u = 'file://' + p                   # UNC: file:////server/share/...
    return u + '?mode=ro' if ro else u


def ro_uri(path):
    """SQLite ``file:`` URI that opens `path` read-only."""
    return sqlite_uri(path, ro=True)


def connect_ro(path, timeout=30.0):
    """Read-only connection. Never creates a missing file (a plain
    ``sqlite3.connect`` would leave a 0-byte database behind)."""
    if not os.path.isfile(path):
        raise TextSourceError(f'קובץ מסד הנתונים לא נמצא: {path}')
    try:
        con = sqlite3.connect(ro_uri(path), uri=True, timeout=timeout)
    except sqlite3.Error as e:
        raise TextSourceError(f'לא ניתן לפתוח את מסד הנתונים: {path} ({e})')
    con.execute('PRAGMA busy_timeout=30000')
    return con


class ReadStats:
    """Counts of what a reader actually read — the coverage evidence."""

    # `missing`: a `line` row with no `line_content` row — unread, like a
    # decode error, so it too makes a pass incomplete
    FIELDS = ('lines', 'chars', 'empty', 'decode_errors', 'null_content',
              'version_lines', 'version_lines_skipped', 'missing')
    # Unread rows are also kept by unit id, up to this many, so that a row met
    # by several passes (or stages) is counted once. Past the cap, the count
    # alone remains, and a caller must assume the passes did not overlap.
    UNITS_CAP = 10000

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, 0)
        self.error_samples = []          # first few (unit, message)
        self.unread_units = set()

    def unread(self):
        """Rows that exist in the input but were not read."""
        return self.decode_errors + self.missing

    def unread_row(self, unit, kind, message):
        """Count one row that could not be read: `kind` is
        'decode_errors' or 'missing'."""
        setattr(self, kind, getattr(self, kind) + 1)
        unit = str(unit)
        if len(self.error_samples) < 20:
            self.error_samples.append((unit, message))
        if len(self.unread_units) < self.UNITS_CAP:
            self.unread_units.add(unit)

    def units_complete(self):
        """Whether every unread row is known by id (none past the cap)."""
        return len(self.unread_units) == self.unread()

    def add(self, other):
        for f in self.FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))
        room = 20 - len(self.error_samples)
        if room > 0:
            self.error_samples.extend(other.error_samples[:room])
        for u in other.unread_units:
            if len(self.unread_units) >= self.UNITS_CAP:
                break
            self.unread_units.add(u)
        return self

    def to_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d['error_samples'] = list(self.error_samples)
        if self.unread_units:            # absent from a complete read's dict
            d['unread_units'] = sorted(self.unread_units)
        return d

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for f in cls.FIELDS:
            setattr(s, f, int((d or {}).get(f, 0)))
        s.error_samples = list((d or {}).get('error_samples') or [])
        s.unread_units = {str(u) for u in (d or {}).get('unread_units') or ()}
        return s


class OtzariaDB:
    """Schema-aware, read-only access to the text of seforim.db."""

    def __init__(self, path):
        self.path = path
        self.con = connect_ro(path)
        # any failure past this point must release the connection: on Windows
        # an open handle keeps seforim.db locked (WinError 32) for the process
        try:
            self._inspect(path)
        except TextSourceError:
            self.con.close()
            raise
        except Exception as e:          # not a database, a corrupt dictionary
            self.con.close()
            raise TextSourceError(
                f'לא ניתן לקרוא את מסד הנתונים כמסד ספרים של אוצריא: {path}'
                f' ({e})') from e

    def _inspect(self, path):
        tables = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'line' not in tables or 'book' not in tables:
            raise TextSourceError(
                f'הקובץ אינו מסד ספרים של אוצריא (חסרות הטבלאות line/book): '
                f'{path}')
        line_cols = {r[1] for r in self.con.execute('PRAGMA table_info(line)')}
        # the 4th column says whether the content row exists at all: a LEFT
        # JOIN, so a `line` without its `line_content` row is counted as
        # missing instead of vanishing from the read unnoticed
        if 'line_content' in tables:
            self.layout = 'line_content'
            self._text_sql = ('SELECT l.id, l.bookId, c.content, '
                              'c.id IS NOT NULL FROM line l '
                              'LEFT JOIN line_content c ON c.id = l.id')
        elif 'content' in line_cols:
            self.layout = 'inline'
            self._text_sql = 'SELECT l.id, l.bookId, l.content, 1 FROM line l'
        else:
            raise TextSourceError(f'לא נמצאה עמודת תוכן במסד: {path}')
        self.has_versions = {'book_version', 'version_line'} <= tables
        self.decode_raw = None
        self.backend = None
        if 'zstd_dict' in tables:
            row = self.con.execute(
                'SELECT dict FROM zstd_dict ORDER BY id LIMIT 1').fetchone()
            if row is None:
                raise TextSourceError('טבלת zstd_dict ריקה — לא ניתן לפענח')
            self.decode_raw, self.backend = _make_decoder(row[0])
        self.schema_meta = {}
        if 'schema_meta' in tables:
            self.schema_meta = dict(self.con.execute(
                'SELECT key, value FROM schema_meta').fetchall())

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- decoding ------------------------------------------------------------
    def decode(self, value):
        """Row value -> str. BLOB is a zstd frame, TEXT is already text."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if self.decode_raw is None:
            # a BLOB in a database without a dictionary: not a format we know
            raise TextSourceError('תוכן בינארי במסד ללא מילון zstd')
        return self.decode_raw(bytes(value)).decode('utf-8')

    def _decode_counted(self, unit, value, stats, present=True):
        if not present:
            stats.unread_row(unit, 'missing', 'no line_content row')
            return None
        if value is None:
            stats.null_content += 1
            return None
        try:
            text = self.decode(value)
        except Exception as e:                      # noqa: BLE001
            stats.unread_row(unit, 'decode_errors', repr(e)[:200])
            return None
        stats.lines += 1
        stats.chars += len(text)
        if not text.strip():
            stats.empty += 1
        return text

    # -- enumeration ---------------------------------------------------------
    def id_range(self):
        return self.con.execute('SELECT MIN(id), MAX(id) FROM line').fetchone()

    def iter_range(self, lo, hi, stats, book_ids_sql=None, params=()):
        """Yield ``(line_id, book_id, text)`` for ``lo <= id < hi``.

        `book_ids_sql` optionally restricts to a sub-select of book ids; the
        range still drives the scan, the books are only checked per row.
        Unreadable rows are counted in `stats` and skipped — never yielded as
        empty text, which would make a broken read look like an empty book.
        """
        sql = self._text_sql + ' WHERE l.id >= ? AND l.id < ?'
        args = [lo, hi]
        if book_ids_sql:
            # unary '+' keeps idx_line_book_index from driving the scan:
            # through it, every chunk walked the lines of every selected book
            sql += f' AND +l.bookId IN ({book_ids_sql})'
            args.extend(params)
        for lid, bid, raw, present in self.con.execute(sql, args):
            text = self._decode_counted(lid, raw, stats, present)
            if text is not None:
                yield lid, bid, text

    def book_lines(self, book_id, stats=None):
        """``[(line_id, heRef, text)]`` of one book in reading order."""
        stats = stats if stats is not None else ReadStats()
        if self.layout == 'line_content':
            sql = ('SELECT l.id, l.heRef, c.content, c.id IS NOT NULL '
                   'FROM line l LEFT JOIN line_content c ON c.id = l.id '
                   'WHERE l.bookId = ? ORDER BY l.lineIndex, l.id')
        else:
            sql = ('SELECT l.id, l.heRef, l.content, 1 FROM line l '
                   'WHERE l.bookId = ? ORDER BY l.lineIndex, l.id')
        out = []
        for lid, ref, raw, present in self.con.execute(sql, (book_id,)):
            text = self._decode_counted(lid, raw, stats, present)
            if text is not None:
                out.append((lid, ref or '', text))
        return out

    def line_texts(self, line_ids, stats=None):
        """``{line_id: text}`` for the given ids (missing/unreadable omitted)."""
        stats = stats if stats is not None else ReadStats()
        out = {}
        ids = list(line_ids)
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            ph = ','.join('?' * len(batch))
            for lid, _bid, raw, present in self.con.execute(
                    self._text_sql + f' WHERE l.id IN ({ph})', batch):
                text = self._decode_counted(lid, raw, stats, present)
                if text is not None:
                    out[lid] = text
        return out

    def describe_lines(self, line_ids):
        """``{line_id: (book title, heRef)}`` — where a line is, for telling
        the user which rows could not be read (an id alone says nothing)."""
        out = {}
        ids = list(line_ids)
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            ph = ','.join('?' * len(batch))
            for lid, title, ref in self.con.execute(
                    'SELECT l.id, b.title, l.heRef FROM line l '
                    f'LEFT JOIN book b ON b.id = l.bookId WHERE l.id IN ({ph})',
                    batch):
                out[lid] = (title or '', ref or '')
        return out

    def count_version_lines(self, lo=None, hi=None, book_ids_sql=None,
                            params=()):
        """Version rows that carry their own reading (content NOT NULL),
        optionally only of the books `book_ids_sql` selects."""
        if not self.has_versions:
            return 0
        sql = 'SELECT COUNT(*) FROM version_line v'
        conds, args = ['v.content IS NOT NULL'], []
        if lo is not None:
            conds.append('v.lineId >= ? AND v.lineId < ?')
            args = [lo, hi]
        if book_ids_sql:
            # a join on line's primary key, not `lineId IN (SELECT id FROM
            # line WHERE bookId IN ...)`: SQLite drove that from the IN list,
            # building every selected book's line ids (~5M for Sefaria) on
            # each chunk. Same rows (a version row without its `line` row
            # matches neither); '+' keeps the book index from driving the
            # join too.
            sql += ' JOIN line l ON l.id = v.lineId'
            conds.append(f'+l.bookId IN ({book_ids_sql})')
            args.extend(params)
        sql += ' WHERE ' + ' AND '.join(conds)
        return self.con.execute(sql, args).fetchone()[0]

    def iter_version_range(self, lo, hi, stats):
        """Yield ``(version_id, line_id, book_id, text)`` for version rows
        with their own reading, ``lo <= line_id < hi``."""
        if not self.has_versions:
            return
        for vid, lid, bid, raw in self.con.execute(
                'SELECT v.versionId, v.lineId, l.bookId, v.content '
                'FROM version_line v JOIN line l ON l.id = v.lineId '
                'WHERE v.lineId >= ? AND v.lineId < ? '
                'AND v.content IS NOT NULL', (lo, hi)):
            text = self._decode_counted(f'ver:{vid}:{lid}', raw, stats)
            if text is not None:
                stats.version_lines += 1
                yield vid, lid, bid, text

    def identity(self):
        """What a run must record to be reproducible against this input."""
        st = os.stat(self.path)
        counts = {
            'books': self.con.execute('SELECT COUNT(*) FROM book').fetchone()[0],
            'lines': self.con.execute('SELECT COUNT(*) FROM line').fetchone()[0],
        }
        lo, hi = self.id_range()
        return {'path': os.path.abspath(self.path), 'bytes': st.st_size,
                'mtime': int(st.st_mtime), 'layout': self.layout,
                'compressed': self.decode_raw is not None,
                'zstd_backend': self.backend,
                'schema_meta': self.schema_meta,
                'line_id_range': [lo, hi], **counts}


# ---------------------------------------------------------------------------
# text files: one line-splitting rule for every reader
# ---------------------------------------------------------------------------

def split_lines(text):
    """Split on CR LF / CR / LF only — the same rule the file writer uses.

    ``str.splitlines`` also breaks on U+2028, U+2029, U+0085, VT, FF and
    U+001C-U+001E. Library files do contain U+2028 inside a line, and a reader
    that splits there numbers every later line differently from the full scan
    (which iterates the file object) and from the patcher, so a finding would
    point one line off. Kept here so all three agree by construction.
    """
    if not text:
        return []
    out = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if out and out[-1] == '':
        out.pop()                       # a trailing terminator ends the file
    return out


def iter_file_lines(path, encoding='utf-8'):
    """Yield the lines of a text file without terminators, numbered like
    :func:`split_lines` (and the patcher), streaming."""
    with open(path, encoding=encoding, errors='replace', newline='') as f:
        pending_cr = False
        buf = ''
        for chunk in iter(lambda: f.read(1 << 20), ''):
            if pending_cr and chunk.startswith('\n'):
                chunk = chunk[1:]
            pending_cr = chunk.endswith('\r')
            buf += chunk.replace('\r\n', '\n').replace('\r', '\n')
            parts = buf.split('\n')
            buf = parts.pop()
            yield from parts
        if buf:
            yield buf
