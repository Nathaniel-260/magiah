# -*- coding: utf-8 -*-
"""Writing approved corrections back into the book's own .txt file.

The one rule this module exists to enforce
------------------------------------------
**A correction must never land on an unrelated passage.** Everything below is
an identity check; whenever identity is uncertain the module REFUSES and the
human is asked. Silence is never an option, and a partial write never happens:
:func:`plan_all` validates every edit before a byte is written — and the only
writer, ``fixer_api``, writes under the book's lock and its journal.

Why a plain ``line.replace(word, fix)`` would corrupt books
----------------------------------------------------------
``normalize.clean`` — which produced the ``word`` stored in the report — drops
nikud and teamim, deletes inline HTML tags without a space, turns structural
tags into spaces and rewrites maqaf and curly quotes. Real library lines look
like::

    <sup style="color: #2563eb; font-weight: bold;">4</sup> וְיֶהֱמוּ גַלָיו …

so the stored ``word`` (``יהמו``) does not occur literally in the raw line at
all, while a short ``word`` may well occur *inside the markup*. ``snippet`` is
a window of the CLEANED line, not a slice of the file, so it cannot locate
anything in the raw text — but it can prove which line and which occurrence a
finding means (see :func:`_identify`).

The anchor is therefore computed by re-tokenizing the raw line with
:func:`normalize.token_spans`, which reports the raw span each token occupies.
The span is the only thing ever replaced, so nikud, tags and punctuation
outside it survive byte for byte.

Identity, and the ways it can go wrong
--------------------------------------
* the file changed since it was opened  -> ``file_changed`` (fingerprint)
* the line is gone / shorter file        -> ``line_gone``
* the word is no longer there            -> ``token_not_found``
* the word is there, the sentence is not -> ``line_mismatch``
* the sentence is on several lines       -> ``ambiguous_line``
* the word occurs N times                -> ``occurrence_count_changed`` or
  ``ambiguous_occurrence``; the human clicks the right one and the offsets
  then come from the server, never from the client

Callers must key on ``unit`` (which carries the path AND the line number), NOT
on ``source``: ``source`` is only the filename stem, and in the real corpus the
stem ``'פרק א'`` belongs to 35 different books. Writing by ``source`` would
scatter edits across all of them.
"""
import hashlib
import json
import os
import re
import uuid
from collections import OrderedDict
from datetime import datetime

from .. import book_source, core, normalize
from ..corpus_hybrid import DEFAULT_LIBRARY, parse_file_unit
from . import hebrew

FIXER_BACKUP_DIR = 'file_backups'

MODE_REPLACE = 'replace'
MODE_BRACKET = 'bracket'
MODES = (MODE_REPLACE, MODE_BRACKET)

# Conflicts: the file (or our knowledge of it) moved under the user's feet.
# They are recoverable by reloading, so they get 409 rather than 400.
CONFLICT_CODES = frozenset((
    'file_changed', 'line_gone', 'token_not_found',
    'occurrence_count_changed', 'ambiguous_occurrence', 'overlapping_edits',
    'unit_mismatch', 'file_changed_since_edit', 'word_spans_markup',
    'not_approved', 'line_mismatch', 'ambiguous_line', 'source_mismatch',
    'source_unknown', 'file_busy', 'already_applied', 'already_bracketed',
    'backup_corrupt', 'journal_conflict', 'journal_pending',
    'moved_unproven', 'copy_moved',
))

# Encodings tried in order. Decoding is STRICT: a lossy read (errors='replace')
# would write U+FFFD into somebody's book, so an undecodable file is refused.
# 'utf-8-sig' is NOT in this list: it decodes BOM-less UTF-8 happily and then
# re-encodes WITH a BOM, which would prepend three bytes to every plain UTF-8
# book we touch. The BOM is detected from the bytes instead (see read_doc).
ENCODINGS = ('utf-8', 'cp1255')

UTF8_BOM = '﻿'

# old names: <stem>.<YYYYmmdd-HHMMSS>.<pathtag>.bak; new names add microseconds
# and a random suffix, so two backups in one second can never collide
_BACKUP_RE = re.compile(
    r'^[\w.\- ]+\.\d{8}-\d{6}(?:-\d{6})?\.[0-9a-f]{8}(?:\.[0-9a-f]{8})?\.bak$')


class PatchError(ValueError):
    """A refusal, carrying a machine code and a Hebrew message.

    Subclasses ValueError so server.py's existing handler already maps an
    unhandled one to HTTP 400; codes in CONFLICT_CODES are mapped to 409.
    """

    def __init__(self, code, message=None, **extra):
        self.code = code
        self.extra = extra
        super().__init__(message or hebrew.FIXER_MESSAGES.get(code, code))


class AccessDenied(PermissionError):
    """A file or folder the fixer must read or write refuses access: the book
    is locked by another program or read-only, or its folder does not allow
    new files. Waiting cannot help, so it is reported at once (HTTP 423),
    with a Hebrew message and a machine code, like a :class:`PatchError`.
    """

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def denied_folder(folder):
    return AccessDenied('folder_not_writable',
                        _msg('folder_not_writable', folder=folder))


def denied_file(path):
    return AccessDenied('access_denied', _msg('access_denied', path=path))


def _msg(code, **fmt):
    text = hebrew.FIXER_MESSAGES.get(code, code)
    return text.format(**fmt) if fmt else text


# ---------------------------------------------------------------------------
# locating the file
# ---------------------------------------------------------------------------

def resolve_unit(unit, library_dir=None):
    """``unit`` -> ``(kind, abspath, lineno0)``; kind is 'library' | 'local'.

    ``unit`` is the only trustworthy locator (see the module docstring). DB
    units carry no file at all and raise ``db_book`` so the caller can offer
    export instead of editing.
    """
    library_dir = library_dir or DEFAULT_LIBRARY
    parsed = parse_file_unit(unit)
    if parsed is not None:
        rel, lineno = parsed
        rel = book_source.canonical_rel(rel)
        # '.', '..' and 'x.txt/..' all normalize to something that names a
        # DIRECTORY (often the library root itself) rather than a book
        if not rel or rel in ('.', '..') or rel.split('/')[-1] in ('.', '..'):
            raise PatchError('bad_unit', _msg('bad_unit', unit=unit))
        path = os.path.join(library_dir, *rel.split('/'))
        # a crafted '../..' relpath must never read or write outside the repo
        if book_source._rel_within(path, library_dir) is None:
            raise PatchError('outside_library', _msg('outside_library'))
        return 'library', path, lineno
    parsed = book_source.parse_local_unit(unit)
    if parsed is not None:
        path, lineno = parsed
        if not path.strip():
            raise PatchError('bad_unit', _msg('bad_unit', unit=unit))
        return 'local', book_source.canonical_path(path), lineno
    # a bare line id -> a book that lives in seforim.db, not on disk
    raise PatchError('db_book', _msg('db_book'))


def book_key_of(unit):
    """The stable per-FILE identity a worklist is grouped by.

    ``'file:<rel>'`` / ``'local:<abs>'`` — the unit minus its line number, so
    every finding in one file shares one key while the 35 distinct books named
    'פרק א' stay 35 distinct keys.
    """
    parsed = parse_file_unit(unit)
    if parsed is not None:
        return 'file:' + book_source.canonical_rel(parsed[0])
    parsed = book_source.parse_local_unit(unit)
    if parsed is not None:
        return 'local:' + book_source.canonical_path(parsed[0])
    return None


def resolve_unit_lineno(unit):
    """The 0-based line number inside a file unit, or None for a DB unit."""
    parsed = parse_file_unit(unit)
    if parsed is not None:
        return parsed[1]
    parsed = book_source.parse_local_unit(unit)
    return parsed[1] if parsed is not None else None


def resolve_key(key, library_dir=None):
    """A book key from :func:`book_key_of` -> ``(kind, abspath)``."""
    if not isinstance(key, str) or ':' not in key:
        raise PatchError('bad_unit', _msg('bad_unit', unit=key))
    kind, _, rest = key.partition(':')
    if kind not in ('file', 'local'):
        raise PatchError('db_book', _msg('db_book'))
    # deliberately round-trips through resolve_unit with a dummy line number,
    # so key resolution inherits exactly the same containment and
    # names-a-file checks as unit resolution — one place to get right
    _kind, path, _ln = resolve_unit('%s:%s:0' % (kind, rest), library_dir)
    return _kind, path


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

class FileDoc:
    """One book file in memory, with everything needed to write it back.

    ``lines`` holds the text WITHOUT terminators and ``line_ends`` the exact
    terminator that followed each one ('' for a final line with no newline),
    so serializing is a plain interleave and a file with mixed or unusual line
    endings round-trips unchanged.
    """

    def __init__(self, path, raw, encoding, lines, line_ends, bom=False,
                 fingerprint=None, size=None):
        self.path = path
        self.raw = raw                  # decoded text, BOM excluded
        self.encoding = encoding
        self.lines = lines
        self.line_ends = line_ends
        self.bom = bom                  # re-emitted verbatim on write
        self.fingerprint = fingerprint or fingerprint_bytes(self.encode())
        # bytes on disk when read (BOM included); with the fingerprint, what a
        # book scan's record is compared against
        self.size = size
        # Per-line work shared by every finding of the book (anchoring a page
        # costs findings x line length otherwise). Each entry is checked
        # against the line's current text, so an edited line is recomputed.
        self._clean = {}                # lineno -> (line, clean, token set)
        self._info = OrderedDict()      # line text -> _LineInfo, LRU
        self._cleans = None             # clean() of every line

    def __len__(self):
        return len(self.lines)

    def text(self):
        parts = []
        for i, line in enumerate(self.lines):
            parts.append(line)
            parts.append(self.line_ends[i] if i < len(self.line_ends) else '')
        return ''.join(parts)

    def encode(self):
        data = self.text().encode(self.encoding)
        return (b'\xef\xbb\xbf' + data) if self.bom else data


_SPLIT_RE = re.compile(r'(\r\n|\r|\n)')


def read_bytes(path):
    """The book's exact bytes, after the is-a-book checks."""
    if not os.path.isfile(path):
        raise PatchError('not_a_file', _msg('not_a_file', path=path))
    if not path.lower().endswith('.txt'):
        raise PatchError('not_txt', _msg('not_txt'))
    size = os.path.getsize(path)
    if size > book_source.MAX_BOOK_BYTES:
        raise PatchError('too_big', _msg(
            'too_big', mb=size / 1e6,
            limit=book_source.MAX_BOOK_BYTES / 1e6))
    try:
        with open(path, 'rb') as f:
            return f.read()
    except PermissionError as e:       # opened exclusively by another program
        raise denied_file(path) from e


def read_doc(path):
    """Read a book file, preserving everything needed to write it back."""
    return doc_from_bytes(path, read_bytes(path))


def doc_from_bytes(path, data):
    """Parse bytes already read, so the fingerprint and the text a plan is
    made against are guaranteed to be the same version of the file."""
    fp, size = fingerprint_bytes(data), len(data)
    # the BOM is a property of the file, kept aside so it is neither lost nor
    # invented on write
    bom = data.startswith(b'\xef\xbb\xbf')
    if bom:
        data = data[3:]
    raw = encoding = None
    for enc in ENCODINGS:
        try:
            raw = data.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise PatchError('undecodable', _msg('undecodable', path=path))
    # split keeping the terminators, so CRLF/LF/CR survive per line
    parts = _SPLIT_RE.split(raw)
    lines, ends = [], []
    for i in range(0, len(parts), 2):
        lines.append(parts[i])
        ends.append(parts[i + 1] if i + 1 < len(parts) else '')
    # a trailing terminator produces one empty final chunk; drop it so the
    # line numbering matches the scanner's (which used splitlines())
    if len(lines) > 1 and lines[-1] == '' and ends[-1] == '':
        lines.pop()
        ends.pop()
    elif not raw:
        lines, ends = [], []           # an empty file has no lines at all
    doc = FileDoc(path, raw, encoding, lines, ends, bom, fingerprint=fp,
                  size=size)
    if doc.encode() != ((b'\xef\xbb\xbf' + data) if bom else data):
        # a decode/encode round trip that is not byte-exact would rewrite
        # bytes outside every span; refuse rather than write such a file
        raise PatchError('undecodable', _msg('undecodable', path=path))
    return doc


def fingerprint_bytes(text_or_bytes):
    data = (text_or_bytes.encode('utf-8')
            if isinstance(text_or_bytes, str) else text_or_bytes)
    # one definition with the book scan, which records the fingerprint of the
    # bytes it read for _locate_line's "unchanged since the scan" test
    return book_source.file_fingerprint(data)


def fingerprint(path):
    """Content hash of the file on disk. mtime is deliberately not trusted:
    editors preserve it and FAT rounds it to two seconds."""
    with open(path, 'rb') as f:
        return fingerprint_bytes(f.read())


# ---------------------------------------------------------------------------
# planning one edit — the anchoring core
# ---------------------------------------------------------------------------

class EditPlan:
    """One verified replacement: exactly which raw span becomes what."""

    def __init__(self, finding_id, lineno, start, end, old_raw, new_text,
                 mode, occurrence, total, confidence, spans_markup):
        self.finding_id = finding_id
        self.lineno = lineno
        self.start = start
        self.end = end
        self.old_raw = old_raw
        self.new_text = new_text
        self.mode = mode
        self.occurrence = occurrence
        self.total = total
        self.confidence = confidence
        self.spans_markup = spans_markup
        self.drifted = False        # set when the line moved since the scan
        self.needs_vocalization = False
        # filled by apply_edits: where new_text sits in the WRITTEN line and
        # the text around it, which is what undo and idempotency verify
        self.post_start = None
        self.ctx_l = self.ctx_r = None

    def to_dict(self):
        d = {'id': self.finding_id, 'lineno': self.lineno,
             'start': self.start, 'end': self.end, 'old': self.old_raw,
             'new': self.new_text, 'mode': self.mode,
             'occurrence': self.occurrence,
             'confidence': self.confidence,
             'spans_markup': self.spans_markup}
        if self.post_start is not None:
            d.update(post_start=self.post_start, ctx_l=self.ctx_l,
                     ctx_r=self.ctx_r)
        return d


def render_replacement(mode, wrong_raw, correction):
    """The text that replaces the anchored span.

    ``MODE_BRACKET`` writes ``(תיקון) [שגיאה]`` — the correction first in round
    parentheses, then the original in square brackets. ``wrong_raw`` is the RAW
    slice taken from the file, never the DB's ``word``: the stored word is
    normalized, so writing it back would silently strip the nikud off a
    vocalized book.
    """
    if mode == MODE_BRACKET:
        return '(%s) [%s]' % (correction, wrong_raw)
    return correction


def _snippet_identifies(line_toks, snippet):
    """Strict token-overlap identity, for snippets that are not a window.

    ``line_toks`` is the set of the line's tokens. Hebrew religious texts
    repeat long formulas verbatim across many verses ("וידבר ה' אל משה
    לאמר דבר אל בני ישראל ואמרת אלהם…"), and a tolerant overlap test passes
    on every parallel verse. Identity therefore needs most of the snippet's
    tokens: requiring a large majority is what separates "the same verse,
    moved" from "its twin, three verses down".
    """
    if not snippet:
        return False           # cannot identify anything without evidence
    toks = [t for t in normalize.tokenize(snippet) if len(t) > 1]
    if len(toks) < 3:
        return False           # too little to identify a line by
    hits = sum(1 for t in toks if t in line_toks)
    return hits >= max(3, int(len(toks) * 0.75))


# The scanner stores ``clean(line)[s - 45:e + 45].strip()`` around the flagged
# word (core._locate_chunk, book_scan._read_book_tokens). Re-cutting that
# window at a candidate occurrence and comparing it EXACTLY is the identity
# proof: it pins the line and the occurrence on it, not just shared words.
SNIPPET_RADIUS = 45

# How far to look for a line that drifted. Books get lines inserted and
# removed between a scan and a fix; a few dozen lines covers ordinary editing
# while staying far too small to reach an unrelated chapter.
DRIFT_WINDOW = 60

LEVEL_WINDOW = 'window'        # the snippet IS this occurrence's window
LEVEL_TOKENS = 'tokens'        # legacy: most snippet tokens are on the line


class _LineInfo:
    """One line's clean text and token spans, computed once and shared by
    every finding that looks at the line."""
    __slots__ = ('clean', 'spans', '_by_tok', '_at')

    def __init__(self, text):
        self.clean, self.spans = normalize.token_spans_full(text)
        self._by_tok = self._at = None

    def index_at(self, clean_start):
        """Index of the token starting at `clean_start`, or None."""
        if self._at is None:
            self._at = {sp[3]: i for i, sp in enumerate(self.spans)}
        return self._at.get(clean_start)

    def by_tok(self):
        """token -> indexes of its spans, in order."""
        if self._by_tok is None:
            idx = {}
            for i, sp in enumerate(self.spans):
                idx.setdefault(sp[0], []).append(i)
            self._by_tok = idx
        return self._by_tok

    def occurrences(self, word):
        """``[(raw_s, raw_e, clean_s, clean_e), ...]`` of `word`, which may
        span several tokens (the extra_space family)."""
        want = word.split()
        if not want:
            return []
        spans, k, out = self.spans, len(want), []
        for i in self.by_tok().get(want[0], ()):
            if i + k <= len(spans) and all(
                    spans[i + j][0] == want[j] for j in range(1, k)):
                out.append((spans[i][1], spans[i + k - 1][2], spans[i][3],
                            spans[i + k - 1][4]))
        return out


# lines kept per document: anchoring walks the book in reading order, so a
# small window of recent lines catches every repeat
_INFO_CACHE = 256


def _line_info(doc, text):
    """The :class:`_LineInfo` of `text`, from `doc`'s cache when it has one."""
    cache = getattr(doc, '_info', None)
    if cache is None:
        return _LineInfo(text)
    info = cache.get(text)
    if info is None:
        info = cache[text] = _LineInfo(text)
        if len(cache) > _INFO_CACHE:
            cache.popitem(last=False)
    else:
        cache.move_to_end(text)
    return info


def _occurrences(line, word, doc=None):
    """``(clean_text, [(raw_s, raw_e, clean_s, clean_e), ...])`` of `word`,
    which may span several tokens (the extra_space family)."""
    info = _line_info(doc, line)
    return info.clean, info.occurrences(word)


def _identify(line, word, snippet, doc=None):
    """``(occurrences, identified_indexes, level)`` for one candidate line.

    ``level`` is None when the snippet does not identify the line at all."""
    info = _line_info(doc, line)
    occs = info.occurrences(word)
    if not snippet or not occs:
        return occs, [], None
    clean_text, r = info.clean, SNIPPET_RADIUS
    ident = [i for i, o in enumerate(occs)
             if clean_text[max(0, o[2] - r):o[3] + r].strip() == snippet]
    if ident:
        return occs, ident, LEVEL_WINDOW
    # an older scan (or another family) may have cut the snippet differently
    if _snippet_identifies(info.by_tok(), snippet):
        return occs, list(range(len(occs))), LEVEL_TOKENS
    return occs, [], None


def _neighbours(text, word):
    """``{(token before, token after), ...}`` of each copy of `word` in
    `text` (None at either end)."""
    want = word.split()
    toks = normalize.tokenize(text or '')
    k = len(want)
    return {(toks[i - 1] if i else None,
             toks[i + k] if i + k < len(toks) else None)
            for i in range(len(toks) - k + 1) if toks[i:i + k] == want}


def _same_neighbour(a, b, before):
    """Two neighbouring tokens agree; at a window's edge one may be cut
    short (the end of the word before, the start of the word after)."""
    if a == b:
        return True
    if a is None or b is None:
        return False
    return (a.endswith(b) or b.endswith(a)) if before else \
        (a.startswith(b) or b.startswith(a))


def _neighbours_agree(clean_text, occ, snippet, word):
    """Does the copy at `occ` stand between the same words as a copy of the
    word in the scan's snippet? Cheap evidence for the single-copy path,
    where the line is identified by its words only."""
    r = SNIPPET_RADIUS
    here = _neighbours(clean_text[max(0, occ[2] - r):occ[3] + r], word)
    then = _neighbours(snippet, word)
    return any(_same_neighbour(p, q, True) and _same_neighbour(n, m, False)
               for p, n in here for q, m in then)


def _copies(text, word):
    """How many times `word` (one token or several) occurs in `text`."""
    want = word.split()
    toks = normalize.tokenize(text or '')
    return sum(1 for i in range(len(toks) - len(want) + 1)
               if toks[i:i + len(want)] == want)


def _clean_of(doc, n):
    """``(clean_text, token set)`` of line `n`, cached while the line is
    unchanged."""
    line = doc.lines[n]
    c = doc._clean.get(n)
    if c is None or c[0] is not line:
        cl = normalize.clean(line)
        c = doc._clean[n] = (line, cl, frozenset(
            normalize.TOKEN_RE.findall(cl)))
    return c[1], c[2]


def _cleans(doc):
    """clean() of every line, computed once per document (apply_edits and
    reverse_entries, the only writers of ``doc.lines``, drop it)."""
    if doc._cleans is None or len(doc._cleans) != len(doc.lines):
        doc._cleans = [normalize.clean(line) for line in doc.lines]
    return doc._cleans


NL = chr(10)


def _lines_with(doc, lo, hi, text):
    """Lines in ``[lo, hi)`` whose clean text contains `text`, in order: one
    C-speed search over the window instead of a test per line."""
    cleans = _cleans(doc)
    win = NL.join(cleans[lo:hi])
    if win.count(NL) != max(0, hi - lo - 1):
        # an entity (&#10;) decoded to a newline: count lines one by one
        return [n for n in range(lo, hi) if text in cleans[n]]
    out, n, last = [], lo, 0
    pos = win.find(text)
    while pos >= 0:
        n += win.count(NL, last, pos)
        out.append(n)
        last = win.find(NL, pos)
        if last < 0:
            break
        n += 1
        last += 1
        pos = win.find(text, last)
    return out


def _candidates(doc, lineno, word, snippet, skip):
    """Lines within DRIFT_WINDOW (except `skip`) that the snippet identifies,
    as ``{level: [(n, occs, ident), ...]}``. Cheap prefilters keep this
    affordable when a whole book is anchored at page load."""
    out = {LEVEL_WINDOW: [], LEVEL_TOKENS: []}
    parts = word.split()
    if not parts:
        return out
    lo = max(0, lineno - DRIFT_WINDOW)
    hi = min(len(doc.lines), lineno + DRIFT_WINDOW + 1)
    for n in _lines_with(doc, lo, hi, parts[0]):
        if n == skip:
            continue
        cl, toks = _clean_of(doc, n)
        # the word must be a token there, and the snippet must be on the
        # line or at least most of its words
        if not all(p in toks for p in parts) or (
                snippet not in cl and not _snippet_identifies(toks, snippet)):
            continue
        occs, ident, level = _identify(doc.lines[n], word, snippet, doc)
        if level:
            out[level].append((n, occs, ident))
    return out


def _ambiguous_line(word, n, lines, fid):
    return PatchError('ambiguous_line',
                      _msg('ambiguous_line', word=word, n=n + 1), id=fid,
                      candidate_lines=sorted(lines))


def _locate_line(doc, lineno, word, snippet, fid, trusted=False):
    """``(lineno, occs, ident, level, drifted)`` — the one line this finding
    provably refers to, or a refusal.

    An occurrence of the word at the old line number proves nothing: a line
    inserted above shifts a DIFFERENT sentence that happens to contain the
    word into that slot. So the line must be identified by the snippet; if it
    is not, the finding is looked for nearby (unambiguous matches only).
    ``trusted`` means the file is byte-identical to the bytes the scan read
    (fingerprint and size recorded when the scan read them), so the line
    number itself is evidence and identical twins nearby do not matter — but
    only when the snippet window matches that line EXACTLY as well. A token-
    level match there is a parallel verse as easily as the scanned one.
    """
    here = None
    if lineno < len(doc.lines):
        occs, ident, level = _identify(doc.lines[lineno], word, snippet,
                                       doc)
        if level:
            here = (lineno, occs, ident, level, False)
    if here is not None and not (trusted and here[3] == LEVEL_WINDOW):
        # an identical twin nearby means the line number alone is deciding,
        # and a line number is exactly what an insertion invalidates
        twins = _candidates(doc, lineno, word, snippet, skip=lineno)
        same = [c[0] for c in twins[here[3]]]
        if here[3] == LEVEL_TOKENS:
            same += [c[0] for c in twins[LEVEL_WINDOW]]
        if same:
            raise _ambiguous_line(word, lineno, [lineno] + same, fid)
    if here is not None:
        return here

    if lineno < len(doc.lines) \
            and not _occurrences(doc.lines[lineno], word, doc)[1] \
            and _snippet_identifies(_clean_of(doc, lineno)[1], snippet):
        # The scanned passage is still sitting where it was and only the
        # word is gone (a human fixed it): never relocate onto a twin verse.
        raise PatchError('token_not_found', _msg(
            'token_not_found', word=word, n=lineno + 1), id=fid)

    cands = (_candidates(doc, lineno, word, snippet, skip=lineno)
             if snippet else {LEVEL_WINDOW: [], LEVEL_TOKENS: []})
    strong = cands[LEVEL_WINDOW]
    if len(strong) == 1:
        # the scan's exact window around the word, on another line: the
        # same sentence, moved
        n, occs, ident = strong[0]
        return n, occs, ident, LEVEL_WINDOW, True
    if len(strong) > 1:
        raise _ambiguous_line(word, lineno, [c[0] for c in strong], fid)
    weak = cands[LEVEL_TOKENS]
    if weak:
        # Only most of the words are shared. When the scanned verse was
        # deleted or moved out of reach, that is exactly what its parallel
        # verse looks like, so nothing is relocated on it: a human may
        # point at the word there, if it really is the same place.
        raise PatchError('moved_unproven', _msg(
            'moved_unproven', word=word, n=lineno + 1), id=fid,
            candidate_lines=sorted(c[0] for c in weak))
    if lineno >= len(doc.lines):
        raise PatchError('line_gone', _msg('line_gone', n=lineno + 1),
                         id=fid)
    if not _occurrences(doc.lines[lineno], word, doc)[1]:
        raise PatchError('token_not_found', _msg(
            'token_not_found', word=word, n=lineno + 1), id=fid)
    # the word is here, but nothing proves this is the scanned sentence
    raise PatchError('line_mismatch', _msg('line_mismatch', word=word,
                                           n=lineno + 1), id=fid)


# geresh-like marks that end an abbreviation (ר' / וכו׳). Gershayim and double
# quotes AFTER a word are closing quotation marks, never part of the word.
_GERESH = "'’׳"


# "(x) [" right before the span and "]" after it: the exact layout the fixer's
# bracket mode writes, "(correction) [original]". A plain ketiv/qere pair,
# "(הנער) [הנערה]", looks the same, while Otzaria's Tanach writes ketiv/qere
# inside mam-kq spans or as "ketiv [qere]", neither of which matches here.
def _bracket_opened(line, start):
    """Where "(x) [" ending exactly at `start` begins, or None. Looked up
    backwards from `start` alone, so a long line costs nothing per word."""
    if start < 4 or line[start - 3:start] != ') [':
        return None
    k = line.rfind('(', 0, start - 3)
    if k < 0 or any(c in line[k + 1:start - 3] for c in '()[]'):
        return None
    return k


def _bare(text):
    """Text compared without marks, spacing or a trailing geresh."""
    return normalize.clean(text or '').strip().rstrip("'\"")


def _bracket_origin(line, a, b, snippet, corrections, own_edits):
    """Where ``line[a:b]`` — "(x) [word]" — came from: ``'record'`` or
    ``'own'`` (the fixer's output), ``'later'`` (unexplained brackets that
    came after the scan), or ``'scanned'`` (the book's own ketiv/qere).

    The text alone cannot tell the fixer's output from a ketiv/qere pair,
    and the two mistakes are not alike: taking a real pair for the fixer's
    output only refuses a correction, while taking the fixer's output for a
    pair nests brackets into it or overwrites the original half — data lost.
    So every doubt counts as the fixer's, in this order:

    1. a live record of the fixer sitting exactly there, matched by its text
       and context on whatever line it is now (lines move; matching a twin
       line by mistake only refuses) — ``'record'``;
    2. the parenthesized text is what this finding writes, or the detector's
       suggestion for it — ``'own'``. Bracket output keeps the original typo
       inside "[...]", so every later scan flags it again, and a scan in a new
       folder has no record of it. A ketiv equal to the correction is refused
       here too: a refusal is the price of never nesting into the fixer's
       brackets;
    3. the layout is missing from the scan's snippet: brackets that came
       after the scan with nothing to explain them — ``'later'``;
    4. otherwise the book had this pair when it was scanned and nothing ties
       it to the fixer: ketiv/qere, corrected like any word — ``'scanned'``.
       (Left open: a pair the fixer wrote with a custom correction, re-scanned
       in a new folder, then re-applied with yet another correction.)
    """
    text = line[a:b]
    for e in own_edits or ():
        if e.get('new') == text and _locate_entry(line, e) == a:
            return 'record'
    paren = _bare(text[1:text.index(') [')])
    if paren and any(paren == _bare(c) for c in corrections if c):
        return 'own'
    layout = normalize.clean(text).strip()
    if not (snippet and layout and layout in snippet):
        return 'later'
    return 'scanned'


def _as_scanned(line, lineno, own_edits):
    """The line with the fixer's own live edits on it undone: as the scan
    saw it, as far as the fixer had a hand in it.

    Returns ``(text, undone)`` with each undone edit as ``(now_start,
    now_end, then_start, then_end)``. An edit no longer found where its
    record says (edited around by hand since) is left as it is: wherever it
    matters, the exact checks made on the result then fail, which is the
    point. None when two records overlap — nothing is proven then.
    """
    found = []
    for e in own_edits or ():
        if e.get('lineno') != lineno:
            continue
        pos = _locate_entry(line, e)
        if pos is not None:
            found.append((pos, pos + len(e.get('new') or ''),
                          e.get('old') or ''))
    if not found:
        return line, []            # the very string: its spans are cached
    found.sort()
    parts, undone, prev, then = [], [], 0, 0
    for a, b, old in found:
        if a < prev:
            return None
        parts.append(line[prev:a])
        then += a - prev
        undone.append((a, b, then, then + len(old)))
        parts.append(old)
        then += len(old)
        prev = b
    parts.append(line[prev:])
    return ''.join(parts), undone


def _scanner_skips(text, clean_text, occ, word, finding, doc=None):
    """Would the scan have passed over this copy without a finding? The
    same rules as core.locate_line, on the same clean text (a join of two
    words, which needs the lexicon, is not modelled: such a copy is
    counted, and a count that then disagrees refuses)."""
    if len(word.split()) != 1:
        return False                 # a split word is not reported per token
    s, e = occ[2], occ[3]
    if core._editorial_adjacent(clean_text, s, e):
        return True
    errtype = finding.get('errtype')
    if (e < len(clean_text) and clean_text[e] == ')'
            and errtype == 'edit1_ins'
            and (finding.get('suggestion') or '') == word[:-1]
            and word[-1] in core.NUMERAL_LETTERS):
        depth = 0                    # a footnote marker unless '(' is open
        for ch in clean_text[:e]:
            if ch == '(':
                depth += 1
            elif ch == ')' and depth:
                depth -= 1
        if not depth:
            return True
    if errtype in ('missing_space', 'final_midword'):
        return False
    info = _line_info(doc, text)
    k = info.index_at(s)
    return k is not None and any(
        0 <= k - j and info.spans[k - j][0] in core.NAME_TRIGGERS
        for j in (1, 2))


def _scanned_copy(line, lineno, word, finding, spans, own_edits, doc=None):
    """Index in `spans` (the copies of `word` on the line now) of the copy
    the finding means, chosen by the scan's order — and proven, or refused.

    Counting the copies that are left proves nothing: a copy fixed by hand
    and another one typed in keep the count while shifting every copy after
    them. So the line is rebuilt as the scan saw it (the fixer's own edits on
    it undone); there the word must occur as often as the scan counted, and
    the copy at the finding's place in that order must have the scan's
    snippet window EXACTLY. Only then is it mapped back onto the line as it
    is now. Anything less is refused, with the copies offered for a click.
    """
    fid = finding.get('id')
    expected = finding.get('expected_count')
    occurrence = finding.get('occurrence') or 0
    snippet = finding.get('snippet')

    def refuse(code):
        raise PatchError(code, _msg(code, word=word, n=lineno + 1,
                                    k=len(spans)),
                         id=fid, candidates=[[s[0], s[1]] for s in spans],
                         located_line=lineno)

    rebuilt = _as_scanned(line, lineno, own_edits)
    if rebuilt is None:
        refuse('ambiguous_occurrence')
    then, undone = rebuilt
    clean_then, occs = _occurrences(then, word, doc)
    # the scan numbered only the copies it reports; one it skips (after a
    # title, before a geresh, next to a bracket) has no finding and no
    # number, so it must not be counted — the exact window below is still
    # what proves the copy
    occs = [o for o in occs if not _scanner_skips(then, clean_then, o, word,
                                                 finding, doc)]
    if expected is not None and expected != len(occs):
        refuse('occurrence_count_changed')
    if not 0 <= occurrence < len(occs):
        refuse('ambiguous_occurrence')
    o, r = occs[occurrence], SNIPPET_RADIUS
    if not snippet or \
            clean_then[max(0, o[2] - r):o[3] + r].strip() != snippet:
        refuse('ambiguous_occurrence')
    shift = 0
    for a, b, ta, tb in undone:
        if tb <= o[0]:
            shift += (b - a) - (tb - ta)
        elif ta < o[1]:
            # the fixer already rewrote this very copy (for another finding)
            raise PatchError('already_applied', _msg(
                'already_applied', n=lineno + 1), id=fid)
    hit = [i for i, s in enumerate(spans)
           if (s[0], s[1]) == (o[0] + shift, o[1] + shift)]
    if len(hit) != 1:
        refuse('ambiguous_occurrence')
    return hit[0]


def manual_lines(doc, finding):
    """The lines on which a human may point at this finding's word: the
    located line, or the snippet-identified candidates of an ambiguous one.
    Empty when no line is identified — a click cannot supply that proof."""
    lineno = finding.get('lineno')
    word = (finding.get('word') or '').strip()
    if lineno is None or lineno < 0 or not word:
        return []
    try:
        return [_locate_line(doc, lineno, word, finding.get('snippet'),
                             finding.get('id'),
                             finding.get('trusted', False))[0]]
    except PatchError as e:
        return list(e.extra.get('candidate_lines') or [])


def plan_edit(doc, finding, mode=MODE_REPLACE, explicit=None,
              check_vocalization=True, own_edits=None):
    """Verify one finding against the file and return an :class:`EditPlan`.

    ``finding`` is a dict with ``id, lineno, word, correction, snippet`` and
    the server-computed ``occurrence``/``expected_count``. ``explicit`` is an
    ``(start, end)`` pair supplied only when the human pointed at the word
    themselves, which resolves an ambiguity that the automatic rules refused.
    ``own_edits`` are this book's live recorded edits (file_edits detail
    entries): proof of which text in the file is the fixer's own output.
    """
    fid = finding.get('id')
    lineno = finding.get('lineno')
    word = (finding.get('word') or '').strip()
    correction = (finding.get('correction') or '').strip()
    if not correction:
        raise PatchError('no_correction', _msg('no_correction'), id=fid)
    if '\n' in correction or '\r' in correction:
        raise PatchError('bad_correction', _msg('bad_correction'), id=fid)
    if not word:
        raise PatchError('token_not_found', _msg(
            'token_not_found', word='', n=(lineno or 0) + 1), id=fid)
    if mode not in MODES:
        mode = MODE_REPLACE
    if lineno is None or lineno < 0:
        raise PatchError('line_gone', _msg(
            'line_gone', n=(lineno or 0) + 1), id=fid)

    drifted = copy_moved = False
    if explicit is not None:
        # A human click resolves WHICH occurrence, never WHICH sentence: it is
        # only accepted on a line the snippet identifies.
        allowed = manual_lines(doc, finding)
        want = finding.get('explicit_lineno')
        if want is None and len(allowed) == 1:
            want = allowed[0]
        if want is None or want not in allowed:
            raise PatchError('line_mismatch', _msg(
                'line_mismatch', word=word, n=lineno + 1), id=fid,
                manual_lines=allowed)
        lineno = want
        line = doc.lines[lineno]
        spans = _occurrences(line, word, doc)[1]
        start, end = explicit
        if not (0 <= start < end <= len(line)):
            raise PatchError('bad_offsets', _msg('bad_offsets'), id=fid)
        # even a human-pointed span must actually BE this word
        if normalize.clean(line[start:end]).strip() != word:
            raise PatchError('token_not_found', _msg(
                'token_not_found', word=word, n=lineno + 1), id=fid)
        # a click on the letters alone still takes the word's trailing marks
        end = normalize.mark_end(line, end)
        occurrence = next((i for i, s in enumerate(spans)
                           if s[0] == start and s[1] == end), -1)
        total = len(spans)
        confidence = 'manual'
    else:
        lineno, spans, ident, level, drifted = _locate_line(
            doc, lineno, word, finding.get('snippet'), fid,
            finding.get('trusted', False))
        line = doc.lines[lineno]
        total = len(spans)
        if level == LEVEL_WINDOW and len(ident) == 1 \
                and _copies(finding.get('snippet'), word) == 1:
            # The window pins the occurrence, whatever else changed — when
            # the window holds this one copy only. A window holding several
            # copies can equal the snippet around ANOTHER of them once the
            # line shifts: on a short line every window is clipped at the
            # line's ends, so a word typed at the start makes the last
            # copy's window equal the old whole line. Then the scan's order
            # must decide, with proof (_scanned_copy).
            occurrence, confidence = ident[0], 'exact'
        elif total == 1 and finding.get('expected_count') in (None, 1):
            # the one copy, on a line identified by its words: as scanned —
            # if it still stands between the words it stood between. A
            # copy fixed by hand and the typo typed again elsewhere leaves
            # one copy too, on a line that still shares the words.
            # (Refused below, after the bracket checks: the fixer's own
            # brackets around the copy change its neighbours too.)
            copy_moved = not _neighbours_agree(
                _line_info(doc, line).clean, spans[0],
                finding.get('snippet'), word)
            occurrence, confidence = 0, 'weak'
        else:
            # Several copies to choose from (on a short line every window is
            # the whole line), or copies gone or added since the scan: only
            # the scan's own order can choose, and only with proof.
            occurrence = _scanned_copy(line, lineno, word, finding, spans,
                                       own_edits, doc)
            confidence = 'indexed'
        start, end = spans[occurrence][0], spans[occurrence][1]

    close = end + 1 if line[end:end + 1] and line[end] in _GERESH else end
    opened = _bracket_opened(line, start)
    if opened is not None and line[close:close + 1] == ']':
        origin = _bracket_origin(
            line, opened, close + 1, finding.get('snippet'),
            (correction, finding.get('suggestion')), own_edits)
        if origin in ('record', 'own'):
            raise PatchError('already_applied', _msg('already_applied',
                                                     n=lineno + 1), id=fid)
        if origin == 'later':
            raise PatchError('already_bracketed', _msg(
                'already_bracketed', word=word, n=lineno + 1), id=fid)
    if copy_moved:
        raise PatchError('copy_moved', _msg(
            'copy_moved', word=word, n=lineno + 1), id=fid,
            candidates=[[s[0], s[1]] for s in spans], located_line=lineno)

    # A trailing geresh belongs to the abbreviation. It stays attached to the
    # correction; a correction that brings its own replaces it (never two).
    if end < len(line) and line[end] in _GERESH:
        if correction[-1] in _GERESH:
            end += 1
        elif mode == MODE_BRACKET:
            correction += line[end]
            end += 1

    old_raw = line[start:end]
    # A word can span markup: ויל<big>ך</big> tokenizes to the single token
    # וילך, whose raw span is 'ויל<big>ך' — it contains the OPENING tag but
    # not the closing one, because the tag opens mid-word and closes after it.
    # Neither mode can write that safely: replacing yields 'וילכו</big>' (an
    # orphaned close tag) and bracketing yields '[ויל<big>ך]</big>' (mis-nested
    # brackets). Both corrupt the book's markup, so refuse and let the human
    # decide — this is rare, and a wrong guess here is silent damage.
    if '<' in old_raw or '>' in old_raw:
        raise PatchError('word_spans_markup',
                         _msg('word_spans_markup', word=word,
                              n=lineno + 1), id=fid)
    # Nikud is never invented: a plain correction would silently de-vocalize a
    # vocalized word, so replace mode needs a vocalized correction (or brackets).
    needs_voc = (mode == MODE_REPLACE and normalize.has_marks(old_raw)
                 and not normalize.has_marks(correction))
    if needs_voc and check_vocalization:
        raise PatchError('needs_vocalization',
                         _msg('needs_vocalization', word=word, n=lineno + 1),
                         id=fid)
    if drifted:
        # the line moved since the scan; the corrector should be told, even
        # though the match itself was corroborated
        confidence = 'moved'
    plan = EditPlan(fid, lineno, start, end, old_raw,
                    render_replacement(mode, old_raw, correction), mode,
                    occurrence, total, confidence, '<' in old_raw)
    plan.drifted = drifted
    plan.needs_vocalization = needs_voc
    return plan


def plan_all(doc, findings, default_mode=MODE_REPLACE, modes=None,
             explicit=None, own_edits=None):
    """Plan every edit, collecting failures instead of raising on the first.

    Returns ``(plans, failures)``. The caller writes ONLY when `failures` is
    empty — a half-applied batch is never acceptable.
    """
    modes = modes or {}
    explicit = explicit or {}
    plans, failures = [], []
    for f in findings:
        fid = f.get('id')
        try:
            plans.append(plan_edit(doc, f, modes.get(fid, default_mode),
                                   explicit.get(fid), own_edits=own_edits))
        except PatchError as e:
            failures.append(dict({'id': fid, 'code': e.code,
                                  'message': str(e)}, **e.extra))
    # two edits fighting over the same characters cannot both be right
    for a, b in _overlaps(plans):
        failures.append({
            'id': b.finding_id, 'code': 'overlapping_edits',
            'message': _msg('overlapping_edits', n=b.lineno + 1),
            'conflicts_with': a.finding_id})
    return plans, failures


def _overlaps(plans):
    by_line = {}
    for p in plans:
        by_line.setdefault(p.lineno, []).append(p)
    out = []
    for group in by_line.values():
        group.sort(key=lambda p: p.start)
        for i in range(1, len(group)):
            if group[i].start < group[i - 1].end:
                out.append((group[i - 1], group[i]))
    return out


# characters of context recorded on each side of a written span
CTX_CHARS = 24


def apply_edits(doc, plans):
    """Splice every plan into ``doc.lines``.

    Within a line the edits are applied by DESCENDING start offset, so an
    earlier replacement never shifts the offsets of a later one. This is not a
    micro-optimisation: 25,700 lines in the real corpus carry more than one
    finding, and left-to-right application would silently mis-place every edit
    after the first whenever the replacement length differed.
    """
    by_line = {}
    for p in plans:
        by_line.setdefault(p.lineno, []).append(p)
    changed = []
    for lineno, group in sorted(by_line.items()):
        line = doc.lines[lineno]
        for p in sorted(group, key=lambda p: p.start, reverse=True):
            line = line[:p.start] + p.new_text + line[p.end:]
        doc.lines[lineno] = line
        doc._clean.pop(lineno, None)
        doc._cleans = None
        delta = 0
        for p in sorted(group, key=lambda p: p.start):
            p.post_start = ps = p.start + delta
            delta += len(p.new_text) - (p.end - p.start)
            pe = ps + len(p.new_text)
            p.ctx_l = line[max(0, ps - CTX_CHARS):ps]
            p.ctx_r = line[pe:pe + CTX_CHARS]
        changed.append(lineno)
    return changed


# ---------------------------------------------------------------------------
# recorded edits: idempotency and span-level undo
# ---------------------------------------------------------------------------

def _post_starts(entries):
    """Fill ``post_start`` for records written before it was stored, from the
    pre-edit offsets of the edits that shared each line."""
    by_line = {}
    for e in entries:
        by_line.setdefault(e.get('lineno'), []).append(e)
    for group in by_line.values():
        delta = 0
        for e in sorted(group, key=lambda e: e.get('start') or 0):
            if e.get('post_start') is None and e.get('start') is not None:
                e['post_start'] = e['start'] + delta
            delta += len(e.get('new') or '') - (
                (e.get('end') or 0) - (e.get('start') or 0))
    return entries


def _locate_entry(line, e):
    """Where the recorded new text sits now, verified by its context; None
    when it cannot be pinned down unambiguously."""
    new, ps = e.get('new') or '', e.get('post_start')
    cl, cr = e.get('ctx_l'), e.get('ctx_r')
    if ps is None or cl is None or cr is None or not new:
        return None
    if ps >= len(cl) and line[ps - len(cl):ps + len(new) + len(cr)] \
            == cl + new + cr:
        return ps
    # a later edit on the same line shifted it: accept a unique match
    needle = cl + new + cr
    hits = [m.start() for m in re.finditer(re.escape(needle), line)]
    return hits[0] + len(cl) if len(hits) == 1 else None


def entry_in_place(doc, e):
    """Is this recorded (live) edit still visibly applied in `doc`?"""
    n = e.get('lineno')
    if n is None or not 0 <= n < len(doc.lines):
        return False
    return _locate_entry(doc.lines[n], e) is not None


def reverse_entries(doc, entries):
    """Put each recorded edit's original text back, touching nothing else.

    Every span must still hold exactly what was written, with the same
    context on both sides; if any one does not, nothing is changed and
    ``file_changed_since_edit`` is raised.
    """
    entries = _post_starts([dict(e) for e in entries])
    by_line = {}
    for e in entries:
        by_line.setdefault(e.get('lineno'), []).append(e)
    new_lines = {}
    for n, group in by_line.items():
        if n is None or not 0 <= n < len(doc.lines):
            raise PatchError('file_changed_since_edit',
                             _msg('file_changed_since_edit'))
        line = doc.lines[n]
        located = []
        for e in group:
            pos = _locate_entry(line, e)
            if pos is None:
                raise PatchError('file_changed_since_edit',
                                 _msg('file_changed_since_edit'))
            located.append((pos, e))
        located.sort(key=lambda x: x[0], reverse=True)
        for i in range(1, len(located)):
            if located[i][0] + len(located[i][1]['new']) > located[i - 1][0]:
                raise PatchError('file_changed_since_edit',
                                 _msg('file_changed_since_edit'))
        for pos, e in located:
            line = line[:pos] + e['old'] + line[pos + len(e['new']):]
        new_lines[n] = line
    for n, line in new_lines.items():
        doc.lines[n] = line
        doc._clean.pop(n, None)
        doc._cleans = None
    return sorted(new_lines)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def backup_path(outdir, path):
    """A backup name that keeps same-named books AND same-second writes apart.

    The stem alone is not unique — 'פרק א' names 35 different books — so the
    path's hash is part of the filename; microseconds plus a random suffix
    keep two backups of one book taken in the same second apart.
    """
    bdir = os.path.join(outdir, FIXER_BACKUP_DIR)
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = re.sub(r'[^\w.\- ]+', '_', stem)[:60] or 'book'
    tag = hashlib.sha256(os.path.abspath(path).encode('utf-8')).hexdigest()[:8]
    ts = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    return os.path.join(bdir, '%s.%s.%s.%s.bak' % (safe, ts, tag,
                                                  uuid.uuid4().hex[:8]))


def temp_path_for(path):
    """A temp name next to `path` (os.replace is atomic only within one
    filesystem) that no other thread or process will pick."""
    return '%s.%s.tmp' % (path, uuid.uuid4().hex)


def _write_new(path, data):
    """Create `path` exclusively and make its bytes durable."""
    with open(path, 'xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def write_backup(outdir, path, data):
    """Store `data` (the book as it is before a write) under a fresh backup
    name; returns ``(backup_path, sha)``."""
    for _ in range(5):
        bpath = backup_path(outdir, path)
        try:
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            _write_new(bpath, data)
            return bpath, fingerprint_bytes(data)
        except FileExistsError:
            continue
        except PermissionError as e:
            raise denied_folder(os.path.dirname(bpath)) from e
    raise FileExistsError(bpath)


# temp files are only alive for the length of one write
STALE_TEMP_SECONDS = 600


def _clean_stale_temps(path, max_age=STALE_TEMP_SECONDS):
    """Remove this book's own temp files that a crash left behind: those
    older than `max_age` seconds, or all of them with ``max_age=0`` — which
    only a holder of the book's lock may ask for: every writer of these
    files holds it, so then none of them is still being written."""
    d, base = os.path.split(path)
    pat = re.compile(re.escape(base) + r'\.[0-9a-f]{32}\.tmp$')
    try:
        names = os.listdir(d or '.')
    except OSError:
        return
    now = datetime.now().timestamp()
    for n in names:
        if pat.match(n):
            full = os.path.join(d, n)
            try:
                if max_age <= 0 or now - os.path.getmtime(full) > max_age:
                    os.remove(full)
            except OSError:
                pass


def atomic_write(path, data):
    """Temp file in the same directory, fsync, then ``os.replace``: a reader
    sees the old book or the new one, never half of either."""
    _clean_stale_temps(path)
    tmp = temp_path_for(path)
    placed = False
    try:
        _write_new(tmp, data)
        placed = True
        os.replace(tmp, path)
    except BaseException as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, PermissionError) and not isinstance(e, AccessDenied):
            # the raw error names the temp file, in English; say what it means
            if placed:
                raise denied_file(path) from e        # locked or read-only
            raise denied_folder(os.path.dirname(path) or '.') from e
        raise
    return fingerprint_bytes(data)


# There is deliberately no "write this doc" or "restore this backup" helper
# here: a write that skipped the book's lock and its journal would reopen the
# lost-update and no-undo holes. fixer_api._write_journaled is the only writer.


def backup_file(outdir, backup):
    """Absolute path of a backup NAME, refusing anything outside the dir."""
    base = os.path.basename(backup)
    if not _BACKUP_RE.match(base):
        raise PatchError('bad_backup', _msg('bad_backup'))
    bdir = os.path.abspath(os.path.join(outdir, FIXER_BACKUP_DIR))
    full = os.path.abspath(os.path.join(bdir, base))
    if os.path.dirname(full) != bdir or not os.path.isfile(full):
        raise PatchError('backup_missing', _msg('backup_missing'))
    return full


def read_backup(outdir, backup, expect_sha=None):
    """A backup's bytes, verified against the hash recorded when it was
    taken — a truncated or replaced backup is never restored."""
    with open(backup_file(outdir, backup), 'rb') as f:
        data = f.read()
    if expect_sha and fingerprint_bytes(data) != expect_sha:
        raise PatchError('backup_corrupt', _msg('backup_corrupt'))
    return data


# ---------------------------------------------------------------------------
# building a worklist for one file
# ---------------------------------------------------------------------------

def assign_occurrences(rows):
    """Number findings that share ``(unit, word)`` so each names one occurrence.

    The report has no character offset, so when the same word occurs twice on
    one line the only thing distinguishing the two findings is their order —
    2,249 such pairs exist in the real corpus. Rows are numbered in ascending
    id order (the importer's own deterministic order) and also told how many
    siblings they have, so the writer can detect that the line changed.
    """
    groups = {}
    for r in rows:
        groups.setdefault((r.get('unit'), r.get('word')), []).append(r)
    for group in groups.values():
        group.sort(key=lambda r: r.get('id') or 0)
        for i, r in enumerate(group):
            r['occurrence'] = i
            r['expected_count'] = len(group)
    return rows


def anchor_rows(doc, rows, applied=None):
    """Attach an ``anchor`` to each row: where it points, or why it does not.

    Computed on the SERVER for every row at load time, so the browser never
    invents an offset — it can only echo one back. ``applied`` maps a finding
    id to its live recorded edit, so an already-written finding is shown as
    such instead of being re-anchored inside its own correction.
    """
    applied = applied or {}
    own = list(applied.values())
    for r in rows:
        e = applied.get(r.get('id'))
        if e is not None and entry_in_place(doc, e):
            r['anchor'] = {'ok': False, 'code': 'already_applied',
                           'message': _msg('already_applied',
                                           n=e['lineno'] + 1),
                           'manual_lines': []}
            r['lineno'] = e['lineno']
            continue
        try:
            plan = plan_edit(doc, {
                'id': r.get('id'), 'lineno': r.get('lineno'),
                'word': r.get('word'),
                'correction': r.get('correction') or r.get('word'),
                'suggestion': r.get('suggestion'),
                'snippet': r.get('snippet'),
                'occurrence': r.get('occurrence'),
                'expected_count': r.get('expected_count'),
                'errtype': r.get('errtype'),
                'trusted': r.get('trusted', False)},
                check_vocalization=False, own_edits=own)
            r['anchor'] = {'ok': True, 'start': plan.start, 'end': plan.end,
                           'confidence': plan.confidence,
                           'spans_markup': plan.spans_markup,
                           'needs_vocalization': plan.needs_vocalization,
                           'total_occurrences': plan.total,
                           'moved_from': (r.get('lineno')
                                          if plan.drifted else None)}
            # the row now describes where the word REALLY is, so the document
            # pane highlights the right line
            r['lineno'] = plan.lineno
        except PatchError as e:
            r['anchor'] = dict({'ok': False, 'code': e.code,
                                'message': str(e)}, **e.extra)
            # the pane must show the line the refusal is ABOUT, and offer a
            # click only where the snippet identified the sentence
            lines = e.extra.get('candidate_lines')
            if e.extra.get('located_line') is not None:
                lines = [e.extra['located_line']]
            lines = list(lines or [])
            r['anchor']['manual_lines'] = lines
            if lines and r.get('lineno') not in lines:
                r['lineno'] = lines[0]
    return rows


def line_tokens(doc, lineno):
    """Raw spans of every token on a line, for click-to-resolve in the UI."""
    if lineno is None or lineno < 0 or lineno >= len(doc.lines):
        return []
    return [[a, b] for _t, a, b in normalize.token_spans(doc.lines[lineno])]


def detail_json(plans):
    return json.dumps([p.to_dict() for p in plans], ensure_ascii=False)
