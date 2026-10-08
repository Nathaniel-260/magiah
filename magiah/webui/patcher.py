# -*- coding: utf-8 -*-
"""Writing approved corrections back into the book's own .txt file.

The one rule this module exists to enforce
------------------------------------------
**A correction must never land on an unrelated passage.** Everything below is
an identity check; whenever identity is uncertain the module REFUSES and the
human is asked. Silence is never an option, and a partial write never happens:
:func:`plan_all` validates every edit before :func:`write_doc` touches a byte.

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
from datetime import datetime

from .. import book_source, normalize
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
    'source_unknown', 'file_busy', 'already_applied', 'backup_corrupt',
    'journal_conflict', 'journal_pending',
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
        self._clean = {}                # lineno -> clean(line), for scans

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
    with open(path, 'rb') as f:
        return f.read()


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


def check_fingerprint(path, expected):
    if not expected:
        raise PatchError('file_changed', _msg('file_changed'))
    actual = fingerprint(path)
    if actual != expected:
        raise PatchError('file_changed', _msg('file_changed'))
    return actual


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


def _snippet_identifies(line, snippet):
    """Strict token-overlap identity, for snippets that are not a window.

    Hebrew religious texts repeat long formulas verbatim across many
    verses ("וידבר ה' אל משה לאמר דבר אל בני ישראל ואמרת אלהם…"), and a
    tolerant overlap test passes on every parallel verse. Identity therefore
    needs most of the snippet's tokens: requiring a large majority is what
    separates "the same verse, moved" from "its twin, three verses down".
    """
    if not snippet:
        return False           # cannot identify anything without evidence
    toks = [t for t in normalize.tokenize(snippet) if len(t) > 1]
    if len(toks) < 3:
        return False           # too little to identify a line by
    line_toks = set(normalize.tokenize(line))
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


def _occurrences(line, word):
    """``(clean_text, [(raw_s, raw_e, clean_s, clean_e), ...])`` of `word`,
    which may span several tokens (the extra_space family)."""
    want = word.split()
    clean_text, spans = normalize.token_spans_full(line)
    out = []
    for i in range(len(spans) - len(want) + 1):
        win = spans[i:i + len(want)]
        if [t[0] for t in win] == want:
            out.append((win[0][1], win[-1][2], win[0][3], win[-1][4]))
    return clean_text, out


def _identify(line, word, snippet):
    """``(occurrences, identified_indexes, level)`` for one candidate line.

    ``level`` is None when the snippet does not identify the line at all."""
    clean_text, occs = _occurrences(line, word)
    if not snippet or not occs:
        return occs, [], None
    r = SNIPPET_RADIUS
    ident = [i for i, o in enumerate(occs)
             if clean_text[max(0, o[2] - r):o[3] + r].strip() == snippet]
    if ident:
        return occs, ident, LEVEL_WINDOW
    # an older scan (or another family) may have cut the snippet differently
    if _snippet_identifies(line, snippet):
        return occs, list(range(len(occs))), LEVEL_TOKENS
    return occs, [], None


def _clean_of(doc, n):
    c = doc._clean.get(n)
    if c is None:
        c = doc._clean[n] = normalize.clean(doc.lines[n])
    return c


def _candidates(doc, lineno, word, snippet, skip):
    """Lines within DRIFT_WINDOW (except `skip`) that the snippet identifies,
    as ``{level: [(n, occs, ident), ...]}``. Cheap substring prefilters keep
    this affordable when a whole book is anchored at page load."""
    out = {LEVEL_WINDOW: [], LEVEL_TOKENS: []}
    parts = word.split()
    lo = max(0, lineno - DRIFT_WINDOW)
    hi = min(len(doc.lines), lineno + DRIFT_WINDOW + 1)
    for n in range(lo, hi):
        if n == skip:
            continue
        cl = _clean_of(doc, n)
        if snippet not in cl and (
                not all(p in cl for p in parts)
                or not _snippet_identifies(doc.lines[n], snippet)):
            continue
        occs, ident, level = _identify(doc.lines[n], word, snippet)
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
        occs, ident, level = _identify(doc.lines[lineno], word, snippet)
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

    if lineno < len(doc.lines) and not _occurrences(doc.lines[lineno],
                                                    word)[1] \
            and _snippet_identifies(doc.lines[lineno], snippet):
        # The scanned passage is still sitting where it was and only the
        # word is gone (a human fixed it): never relocate onto a twin verse.
        raise PatchError('token_not_found', _msg(
            'token_not_found', word=word, n=lineno + 1), id=fid)

    cands = (_candidates(doc, lineno, word, snippet, skip=lineno)
             if snippet else {LEVEL_WINDOW: [], LEVEL_TOKENS: []})
    strong = cands[LEVEL_WINDOW]
    # a legacy (token-overlap) match is only trusted when the word occurs
    # once there; a window match pins the occurrence by itself
    weak = [c for c in cands[LEVEL_TOKENS] if len(c[1]) == 1]
    pool = strong or weak
    if len(pool) == 1:
        n, occs, ident = pool[0]
        return (n, occs, ident,
                LEVEL_WINDOW if strong else LEVEL_TOKENS, True)
    if len(pool) > 1:
        raise _ambiguous_line(word, lineno, [c[0] for c in pool], fid)
    if lineno >= len(doc.lines):
        raise PatchError('line_gone', _msg('line_gone', n=lineno + 1),
                         id=fid)
    if not _occurrences(doc.lines[lineno], word)[1]:
        raise PatchError('token_not_found', _msg(
            'token_not_found', word=word, n=lineno + 1), id=fid)
    # the word is here, but nothing proves this is the scanned sentence
    raise PatchError('line_mismatch', _msg('line_mismatch', word=word,
                                           n=lineno + 1), id=fid)


# geresh-like marks that end an abbreviation (ר' / וכו׳). Gershayim and double
# quotes AFTER a word are closing quotation marks, never part of the word.
_GERESH = "'’׳"

# "(תיקון) [" right before the span and "]" after it: the span is the original
# half of an earlier bracket-mode correction, and editing it would nest.
_BRACKETED_RE = re.compile(r'\([^()\[\]]*\) \[$')


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
              check_vocalization=True):
    """Verify one finding against the file and return an :class:`EditPlan`.

    ``finding`` is a dict with ``id, lineno, word, correction, snippet`` and
    the server-computed ``occurrence``/``expected_count``. ``explicit`` is an
    ``(start, end)`` pair supplied only when the human pointed at the word
    themselves, which resolves an ambiguity that the automatic rules refused.
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

    drifted = False
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
        spans = _occurrences(line, word)[1]
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
        expected = finding.get('expected_count')
        occurrence = finding.get('occurrence') or 0
        if level == LEVEL_WINDOW and len(ident) == 1:
            # the window pins the occurrence, whatever else changed
            occurrence, confidence = ident[0], 'exact'
        else:
            # Several equally-identified copies (a short line: every window is
            # the whole line). Only the scan's own order can choose, and only
            # while the line still has the count the scan saw — "only one is
            # left" says nothing about WHICH one the finding meant.
            if expected is not None and expected != total:
                raise PatchError(
                    'occurrence_count_changed',
                    _msg('occurrence_count_changed', word=word,
                         n=lineno + 1),
                    id=fid, candidates=[[s[0], s[1]] for s in spans],
                    located_line=lineno)
            if total == 1:
                occurrence = 0
            elif not (0 <= occurrence < total) or occurrence not in ident:
                raise PatchError(
                    'ambiguous_occurrence',
                    _msg('ambiguous_occurrence', word=word, n=lineno + 1,
                         k=total),
                    id=fid, candidates=[[s[0], s[1]] for s in spans],
                    located_line=lineno)
            confidence = 'indexed' if level == LEVEL_WINDOW else 'weak'
        start, end = spans[occurrence][0], spans[occurrence][1]

    close = end + 1 if line[end:end + 1] and line[end] in _GERESH else end
    if _BRACKETED_RE.search(line[:start]) and line[close:close + 1] == ']':
        raise PatchError('already_applied', _msg('already_applied',
                                                 n=lineno + 1), id=fid)

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
             explicit=None):
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
                                   explicit.get(fid)))
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
        os.makedirs(os.path.dirname(bpath), exist_ok=True)
        try:
            _write_new(bpath, data)
            return bpath, fingerprint_bytes(data)
        except FileExistsError:
            continue
    raise FileExistsError(bpath)


# temp files are only alive for the length of one write
STALE_TEMP_SECONDS = 600


def _clean_stale_temps(path):
    """Remove this book's own temp files that a crash left behind."""
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
                if now - os.path.getmtime(full) > STALE_TEMP_SECONDS:
                    os.remove(full)
            except OSError:
                pass


def atomic_write(path, data):
    """Temp file in the same directory, fsync, then ``os.replace``: a reader
    sees the old book or the new one, never half of either."""
    _clean_stale_temps(path)
    tmp = temp_path_for(path)
    try:
        _write_new(tmp, data)
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return fingerprint_bytes(data)


def write_doc(doc, outdir):
    """Back the file up, then replace it atomically.

    Order matters: the backup is taken FIRST, so any later failure — a locked
    file, a full disk, a crash — leaves the user with both the untouched
    original and a copy. The fixer's own write path (fixer_api.apply) adds a
    lock and a journal around these same two steps.
    """
    with open(doc.path, 'rb') as f:
        before = f.read()
    bpath, bsha = write_backup(outdir, doc.path, before)  # PermissionError -> 423
    data = doc.encode()
    atomic_write(doc.path, data)
    doc.raw = doc.text()
    doc.fingerprint = fingerprint_bytes(data)
    return {'backup': bpath, 'backup_sha': bsha,
            'fingerprint': doc.fingerprint, 'bytes': len(data)}


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


def restore_backup(outdir, backup, path, expect_fingerprint=None,
                   backup_fingerprint=None):
    """Put a backup back, refusing if the file changed after we wrote it.

    If the corrector edited the book by hand since the fix was applied,
    restoring would destroy that work, so a fingerprint mismatch refuses
    instead (fixer_api.undo_file then tries a span-level undo).
    """
    full = backup_file(outdir, backup)
    if not os.path.isfile(path):
        raise PatchError('not_a_file', _msg('not_a_file', path=path))
    data = read_backup(outdir, full, backup_fingerprint)
    if expect_fingerprint and fingerprint(path) != expect_fingerprint:
        raise PatchError('file_changed_since_edit',
                         _msg('file_changed_since_edit'))
    return {'restored': path, 'fingerprint': atomic_write(path, data)}


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
                'snippet': r.get('snippet'),
                'occurrence': r.get('occurrence'),
                'expected_count': r.get('expected_count'),
                'trusted': r.get('trusted', False)},
                check_vocalization=False)
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
