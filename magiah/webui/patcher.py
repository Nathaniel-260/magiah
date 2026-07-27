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
no better: it is ``' '.join(tokens)`` of NORMALIZED tokens, not a slice of the
file. Neither can be used to locate anything.

The anchor is therefore computed by re-tokenizing the raw line with
:func:`normalize.token_spans`, which reports the raw span each token occupies.
The span is the only thing ever replaced, so nikud, tags and punctuation
outside it survive byte for byte.

Identity, and the four ways it can go wrong
-------------------------------------------
* the file changed since it was opened  -> ``file_changed`` (fingerprint)
* the line is gone / shorter file        -> ``line_gone``
* the word is no longer there            -> ``token_not_found``
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
import shutil
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
    'not_approved',
))

# Encodings tried in order. Decoding is STRICT: a lossy read (errors='replace')
# would write U+FFFD into somebody's book, so an undecodable file is refused.
# 'utf-8-sig' is NOT in this list: it decodes BOM-less UTF-8 happily and then
# re-encodes WITH a BOM, which would prepend three bytes to every plain UTF-8
# book we touch. The BOM is detected from the bytes instead (see read_doc).
ENCODINGS = ('utf-8', 'cp1255')

UTF8_BOM = '﻿'

_BACKUP_RE = re.compile(r'^[\w.\- ]+\.\d{8}-\d{6}\.[0-9a-f]{8}\.bak$')


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

    def __init__(self, path, raw, encoding, lines, line_ends, bom=False):
        self.path = path
        self.raw = raw                  # decoded text, BOM excluded
        self.encoding = encoding
        self.lines = lines
        self.line_ends = line_ends
        self.bom = bom                  # re-emitted verbatim on write
        self.fingerprint = fingerprint_bytes(self.encode())

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


def read_doc(path):
    """Read a book file, preserving everything needed to write it back."""
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
        data = f.read()
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
    return FileDoc(path, raw, encoding, lines, ends, bom)


def fingerprint_bytes(text_or_bytes):
    data = (text_or_bytes.encode('utf-8')
            if isinstance(text_or_bytes, str) else text_or_bytes)
    return 'sha256:' + hashlib.sha256(data).hexdigest()[:32]


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

    def to_dict(self):
        return {'id': self.finding_id, 'lineno': self.lineno,
                'start': self.start, 'end': self.end, 'old': self.old_raw,
                'new': self.new_text, 'mode': self.mode,
                'occurrence': self.occurrence,
                'confidence': self.confidence,
                'spans_markup': self.spans_markup}


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


def _snippet_supports(line_clean, snippet, tok_index_hint=None):
    """Does the report's snippet corroborate this line?

    The snippet is a join of normalized tokens, so it will not match the line
    literally; we only ask whether a few of its tokens are present. A negative
    answer NEVER refuses — it downgrades confidence, because false negatives
    are expected (the snippet spans neighbouring lines' tokens too).
    """
    if not snippet:
        return True
    toks = [t for t in normalize.tokenize(snippet) if len(t) > 1]
    if not toks:
        return True
    line_toks = set(normalize.tokenize(line_clean))
    hits = sum(1 for t in toks if t in line_toks)
    return hits >= max(2, len(toks) // 4)


def _snippet_identifies(line, snippet):
    """Strict variant of :func:`_snippet_supports`, for RELOCATING a finding.

    The tolerant test above is right for downgrading confidence, but far too
    weak to prove "this is the same passage". Hebrew religious texts repeat
    long formulas verbatim across many verses ("וידבר ה' אל משה לאמר דבר אל
    בני ישראל ואמרת אלהם…"), and that shared formula alone clears the tolerant
    threshold — so every parallel verse looks like a match and the finding
    lands on whichever one happens to be nearest.

    Identity therefore rests on what the verses do NOT share: most of the
    snippet's tokens must be present, and the distinctive ones — those absent
    from the passage's neighbours — matter most. Requiring a large majority is
    what separates "the same verse, moved" from "its twin, three verses down".
    """
    if not snippet:
        return False           # cannot identify anything without evidence
    toks = [t for t in normalize.tokenize(snippet) if len(t) > 1]
    if len(toks) < 3:
        return False           # too little to identify a line by
    line_toks = set(normalize.tokenize(line))
    hits = sum(1 for t in toks if t in line_toks)
    return hits >= max(3, int(len(toks) * 0.75))


# How far to look for a line that drifted. Books get lines inserted and
# removed between a scan and a fix; a few dozen lines covers ordinary editing
# while staying far too small to reach an unrelated chapter.
DRIFT_WINDOW = 60


def _find_drifted_line(doc, lineno, word, snippet):
    """The line this finding now sits on, after the file was edited.

    Two situations look identical from the word's absence alone, and they need
    opposite answers:

    * the passage MOVED (a line was inserted above it) -> relocate
    * the passage is still there and only the WORD is gone, because a human
      already corrected it -> there is nothing to do, refuse

    Telling them apart is not optional. Hebrew religious texts are full of
    parallel verses sharing a formula ("וידבר ה' אל משה לאמר…"), so in the
    second case the nearest surviving occurrence of the word is typically a
    DIFFERENT verse that satisfies every other test — and correcting it would
    be exactly the unrelated-passage edit this module exists to prevent.
    The discriminator is the scanned line itself: if the snippet still matches
    it, the passage never moved, so nothing may be relocated.

    Beyond that, a candidate is only accepted when it is unambiguous: the word
    occurs there exactly once, exactly one nearby line qualifies, and the
    snippet corroborates it. Returns None whenever anything is in doubt.
    """
    if not snippet:
        return None            # nothing to corroborate with: do not guess
    # the scanned passage is still sitting where it was -> it did not drift
    if lineno < len(doc.lines) and _snippet_identifies(doc.lines[lineno],
                                                       snippet):
        return None
    lo = max(0, lineno - DRIFT_WINDOW)
    hi = min(len(doc.lines), lineno + DRIFT_WINDOW + 1)
    hits = []
    for n in range(lo, hi):
        if n == lineno:
            continue
        line = doc.lines[n]
        if len(normalize.phrase_spans(line, word)) != 1:
            continue
        if not _snippet_identifies(line, snippet):
            continue
        hits.append(n)
        if len(hits) > 1:
            return None        # several candidates -> ambiguous, refuse
    return hits[0] if len(hits) == 1 else None


def plan_edit(doc, finding, mode=MODE_REPLACE, explicit=None):
    """Verify one finding against the file and return an :class:`EditPlan`.

    ``finding`` is a dict with ``id, unit/lineno, word, correction`` and the
    server-computed ``occurrence``/``expected_count``. ``explicit`` is an
    ``(start, end)`` pair supplied only when the human pointed at the word
    themselves, which resolves an ambiguity that the automatic rules refused.
    """
    fid = finding.get('id')
    lineno = finding.get('lineno')
    word = (finding.get('word') or '').strip()
    correction = (finding.get('correction') or '').strip()
    if not correction:
        raise PatchError('no_correction', _msg('no_correction'), id=fid)
    if not word:
        raise PatchError('token_not_found', _msg(
            'token_not_found', word='', n=(lineno or 0) + 1), id=fid)
    if mode not in MODES:
        mode = MODE_REPLACE
    if lineno is None or lineno < 0:
        raise PatchError('line_gone', _msg(
            'line_gone', n=(lineno or 0) + 1), id=fid)

    # The book may have been edited since the scan — lines inserted or removed
    # shift every finding below them. Rather than refuse everything (which
    # would force a full re-scan for one added line), look for the line the
    # finding drifted to. _find_drifted_line only accepts an unambiguous,
    # snippet-corroborated match, so a failure to relocate still refuses.
    drifted = False
    if lineno >= len(doc.lines) or not normalize.phrase_spans(
            doc.lines[lineno], word):
        moved = None
        if explicit is None:
            moved = _find_drifted_line(doc, lineno, word,
                                       finding.get('snippet'))
        if moved is None:
            if lineno >= len(doc.lines):
                raise PatchError('line_gone', _msg(
                    'line_gone', n=lineno + 1), id=fid)
        else:
            lineno, drifted = moved, True

    line = doc.lines[lineno]
    spans = normalize.phrase_spans(line, word)

    if explicit is not None:
        start, end = explicit
        if not (0 <= start < end <= len(line)):
            raise PatchError('bad_offsets', _msg('bad_offsets'), id=fid)
        # even a human-pointed span must actually BE this word
        if normalize.clean(line[start:end]).strip() != word:
            raise PatchError('token_not_found', _msg(
                'token_not_found', word=word, n=lineno + 1), id=fid)
        occurrence = next((i for i, s in enumerate(spans)
                           if s[1] == start and s[2] == end), -1)
        total = len(spans)
        confidence = 'manual'
    else:
        total = len(spans)
        if total == 0:
            raise PatchError('token_not_found', _msg(
                'token_not_found', word=word, n=lineno + 1), id=fid)
        expected = finding.get('expected_count')
        occurrence = finding.get('occurrence') or 0
        # A changed count means the line is not the line the finding was made
        # against — someone edited it in between. This is checked BEFORE the
        # single-candidate shortcut on purpose: if the scan saw two copies of
        # the word and only one is left, the survivor is not necessarily the
        # one this finding meant, and "there is only one now" is exactly the
        # kind of reasoning that silently corrects the wrong occurrence.
        if expected is not None and expected != total:
            raise PatchError(
                'occurrence_count_changed',
                _msg('occurrence_count_changed', word=word, n=lineno + 1),
                id=fid,
                candidates=[[s[1], s[2]] for s in spans])
        if total == 1:
            occurrence, confidence = 0, 'exact'
        else:
            if not (0 <= occurrence < total):
                raise PatchError(
                    'ambiguous_occurrence',
                    _msg('ambiguous_occurrence', word=word, n=lineno + 1,
                         k=total),
                    id=fid,
                    candidates=[[s[1], s[2]] for s in spans])
            confidence = 'indexed'
        start, end = spans[occurrence][1], spans[occurrence][2]

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
    if confidence != 'manual' and not _snippet_supports(
            line, finding.get('snippet')):
        confidence = 'weak'
    if drifted:
        # the line moved since the scan; the corrector should be told, even
        # though the match itself was corroborated
        confidence = 'moved'
    plan = EditPlan(fid, lineno, start, end, old_raw,
                    render_replacement(mode, old_raw, correction), mode,
                    occurrence, total, confidence, '<' in old_raw)
    plan.drifted = drifted
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
        changed.append(lineno)
    return changed


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def backup_path(outdir, path):
    """A backup name that keeps same-named books apart.

    The stem alone is not unique — 'פרק א' names 35 different books — so the
    path's hash is part of the filename.
    """
    bdir = os.path.join(outdir, FIXER_BACKUP_DIR)
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = re.sub(r'[^\w.\- ]+', '_', stem)[:60] or 'book'
    tag = hashlib.sha256(os.path.abspath(path).encode('utf-8')).hexdigest()[:8]
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    return os.path.join(bdir, '%s.%s.%s.bak' % (safe, ts, tag))


def write_doc(doc, outdir):
    """Back the file up, then replace it atomically.

    Order matters: the backup is taken FIRST, so any later failure — a locked
    file, a full disk, a crash — leaves the user with both the untouched
    original and a copy. The new text goes to a temp file in the same
    directory (``os.replace`` is only atomic within one filesystem), is
    fsynced, and only then replaces the original, so no reader ever observes a
    half-written book.
    """
    bpath = backup_path(outdir, doc.path)
    os.makedirs(os.path.dirname(bpath), exist_ok=True)
    shutil.copy2(doc.path, bpath)          # PermissionError -> 423 upstream

    data = doc.encode()
    tmp = '%s.%d.tmp' % (doc.path, os.getpid())
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, doc.path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    doc.raw = doc.text()
    doc.fingerprint = fingerprint_bytes(data)
    return {'backup': bpath, 'fingerprint': doc.fingerprint,
            'bytes': len(data)}


def restore_backup(outdir, backup, path, expect_fingerprint=None):
    """Put a backup back, refusing if the file changed after we wrote it.

    If the corrector edited the book by hand since the fix was applied,
    restoring would destroy that work, so a fingerprint mismatch refuses
    instead.
    """
    base = os.path.basename(backup)
    if not _BACKUP_RE.match(base):
        raise PatchError('bad_backup', _msg('bad_backup'))
    bdir = os.path.abspath(os.path.join(outdir, FIXER_BACKUP_DIR))
    full = os.path.abspath(os.path.join(bdir, base))
    if os.path.dirname(full) != bdir or not os.path.isfile(full):
        raise PatchError('backup_missing', _msg('backup_missing'))
    if not os.path.isfile(path):
        raise PatchError('not_a_file', _msg('not_a_file', path=path))
    if expect_fingerprint and fingerprint(path) != expect_fingerprint:
        raise PatchError('file_changed_since_edit',
                         _msg('file_changed_since_edit'))
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        shutil.copyfile(full, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return {'restored': path, 'fingerprint': fingerprint(path)}


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


def anchor_rows(doc, rows):
    """Attach an ``anchor`` to each row: where it points, or why it does not.

    Computed on the SERVER for every row at load time, so the browser never
    invents an offset — it can only echo one back.
    """
    for r in rows:
        try:
            plan = plan_edit(doc, {
                'id': r.get('id'), 'lineno': r.get('lineno'),
                'word': r.get('word'),
                'correction': r.get('correction') or r.get('word'),
                'snippet': r.get('snippet'),
                'occurrence': r.get('occurrence'),
                'expected_count': r.get('expected_count')})
            r['anchor'] = {'ok': True, 'start': plan.start, 'end': plan.end,
                           'confidence': plan.confidence,
                           'spans_markup': plan.spans_markup,
                           'total_occurrences': plan.total,
                           'moved_from': (r.get('lineno')
                                          if plan.drifted else None)}
            # the row now describes where the word REALLY is, so the document
            # pane highlights the right line
            r['lineno'] = plan.lineno
        except PatchError as e:
            r['anchor'] = dict({'ok': False, 'code': e.code,
                                'message': str(e)}, **e.extra)
    return rows


def line_tokens(doc, lineno):
    """Raw spans of every token on a line, for click-to-resolve in the UI."""
    if lineno is None or lineno < 0 or lineno >= len(doc.lines):
        return []
    return [[a, b] for _t, a, b in normalize.token_spans(doc.lines[lineno])]


def detail_json(plans):
    return json.dumps([p.to_dict() for p in plans], ensure_ascii=False)
