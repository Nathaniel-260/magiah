# -*- coding: utf-8 -*-
"""Scan ONE book against the lexicon built by a previous full scan.

Why this exists
---------------
The full pipeline flags word *types* over the entire 417M-token corpus and then
locates them (~30-40 min). When the user has already built `lexicon.pkl` and
only wants to check a single book — a newly typed one, a file just corrected,
one book out of the library — nearly all of that work is wasted: only the word
types that actually occur in that book can possibly yield findings there.

So this module inverts the pipeline: read the book first, then apply the very
same per-word rules to just the types present in it, scoring every candidate
against the *global* lexicon. The lexicon is never rebuilt — it is the sole
source of frequencies. A book scan therefore costs seconds instead of half an
hour.

Rule parity with the full pipeline
----------------------------------
Nothing here re-implements a rule. Per-word detection is
:func:`magiah.core.evaluate_word`, split choice is
:func:`magiah.core.resolve_splits` (sequences counted by
:class:`magiah.core.SeqCounter`), every line is located by
:func:`magiah.core.locate_line` (foreign-line skip, extra-space pass,
editorial brackets, name triggers, OCR profiles) and the evidence label is
:func:`magiah.core.evidence_kind`. Only the edit-1 candidate *source* differs
(direct probing instead of a deletion index over the whole lexicon); both feed
the same evaluation and are tested to agree.

Evidence scope — the one deliberate difference
----------------------------------------------
The full pipeline verifies corrections against the whole corpus. A book scan
does so too when ``verify_ctx`` is on (one corpus pass, ~10 min on the hybrid
corpus at 3 workers); its findings then equal the full pipeline's for this
book. By default it verifies inside the book only, and says so:

* ``ctx_scope`` / ``split_scope`` in the result are ``'book'`` or ``'corpus'``;
* each row's ``evidence`` uses book-scope kinds (``context_book``,
  ``split_observed_book``) that are never confused with corpus-wide ones;
* in book scope an unobserved split may still be reported on part frequencies
  alone (low score, evidence ``none``), and an observed one is scored below
  the corpus band.

``book_repeat`` (all occurrences in one book = the author's own spelling) is
derived from the lexicon: the word's global count is fully explained by this
book. The full pipeline asks the same question of its located occurrences.

The Tanach reference check is an Otzaria full-pipeline feature and is not run
here (``tanach`` is always 0). Words absent from the lexicon (``freq == 0``)
are scored with ``max(freq, 1)`` as denominator by the shared rules.
"""
import os
import pickle
import tempfile
import time
from collections import Counter

from . import core
from .book_source import load_book
from .config import Config
from .corpus import OTZARIA_DB
from .normalize import tokenize
from .textsource import ReadStats

LOCAL_TYPES = core.LOCAL_TYPES


class BookScanError(Exception):
    """A book scan cannot run (Hebrew message ready to display)."""


def load_lexicon(out_dir, allow_unread=0):
    """The existing lexicon. Never rebuilt here — that is the whole point."""
    path = os.path.join(out_dir, core.LEXICON_F)
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        raise BookScanError(
            'סריקת ספר בודד מתבססת על המילון הקיים, והוא לא נמצא '
            f'({core.LEXICON_F}).\nיש להריץ פעם אחת סריקה מלאה (או את שלב '
            '"בניית מילון") לפני שאפשר לסרוק ספר בודד.')
    # the lexicon's latest build must have read its whole input — an older
    # lexicon left behind by a failed build is refused too; one built partial
    # under --allow-unread only when this scan allows as many rows
    problem = core.coverage_problem(out_dir, 'lexicon', allow_unread)
    if problem:
        raise BookScanError(problem)
    with open(path, 'rb') as f:
        return pickle.load(f)


def _load_whitelist(cfg, out_dir):
    """Whitelist files + words rejected everywhere in review (as detect)."""
    words = set()
    for path in (cfg.whitelist or ()):
        try:
            with open(path, encoding='utf-8') as f:
                words.update(line.strip() for line in f if line.strip())
        except OSError:
            continue
    words.update(core.load_review_rejections(out_dir))
    return words


def _book_tokens(book):
    """Tokens of every line, read exactly as the corpus passes read text
    (no foreign-line skip), so in-book counts mean what corpus counts mean."""
    for _, _, content in book.lines:
        yield tokenize(content)


def detect_in_book(book, freq, cfg, out_dir, progress=None):
    """Evaluate every word type of `book` with the shared per-word rules.

    Returns ``(lex, errors, split_cands, present, learned_source)``; splits
    are not yet resolved (that needs spaced-sequence evidence).
    """
    whitelist = _load_whitelist(cfg, out_dir)
    learned, lsource = core.load_learned(out_dir)
    present = Counter()
    for toks in _book_tokens(book):
        present.update(toks)
    if progress:
        progress(f'[book] {len(book):,} שורות, {sum(present.values()):,} '
                 f'מילים, {len(present):,} צורות שונות')
    lex = core.Lexicon(freq, cfg, learned)
    cands = core.edit1_probe(lex)
    errors, split_cands = {}, {}
    for w in present:
        if w in whitelist:
            continue
        fw = freq.get(w, 0)
        recs, splits = core.evaluate_word(w, fw, lex, cands)
        for rec in recs:
            core.record_best(errors, w, fw, rec)
        if splits:
            split_cands[w] = splits
    return lex, errors, split_cands, present, lsource


def _resolve_splits(errors, split_cands, freq, lex, seq_counts, scope):
    """Record each word's best-evidenced split; return all alternatives."""
    alts_out, idx = {}, 0
    for w, cs in split_cands.items():
        obs = [seq_counts.get(idx + k, 0) for k in range(len(cs))]
        idx += len(cs)
        best, alts = core.resolve_splits(cs, obs, lex, scope)
        if not best:
            core.keep_structural_suspicion(errors, w, freq.get(w, 0), cs, lex)
        if best:
            core.settle_split(errors, w, freq.get(w, 0), best, lex, scope)
            alts_out[w] = [(' '.join(p), o, e, ok and p == best[0])
                           for p, o, e, ok in alts]
    return alts_out


def _locate(book, freq, cfg, flagged, prof):
    occ, joins, ocr = [], [], []
    for unit, ref, content in book.lines:
        res = core.locate_line(content, freq, cfg, flagged, prof)
        if res is None:
            continue
        o, j, c = res
        occ.extend((unit, ref, w, p, n, s) for w, p, n, s in o)
        joins.extend((unit, ref) + x for x in j)
        ocr.extend((unit, ref) + x for x in c)
    return occ, joins, ocr


def _context_needs(occ, errors):
    """The (neighbour, correction) pairs and correction words to count."""
    ctx_pairs, need = set(), set()
    for _, _, w, prev, nxt, _ in occ:
        fr = errors[w]
        if core.uses_context(fr[1]):
            if prev:
                ctx_pairs.add((prev, fr[2]))
            if nxt:
                ctx_pairs.add((fr[2], nxt))
        if fr[1] in LOCAL_TYPES and fr[2]:
            need.add(fr[2])
    return ctx_pairs, need


# ---------------------------------------------------------------------------
# optional: the corpus-wide evidence pass
# ---------------------------------------------------------------------------

def verify_context(spec, cfg, ctx_pairs, book_need, seqs=(), progress=None,
                   stats=None):
    """Count the wanted pairs, book-local words and split sequences over the
    whole corpus, in ONE pass with the full pipeline's worker.

    A pass that missed more rows than ``cfg.allow_unread`` is refused; what
    it read is added to `stats`, so a caller can mark a result partial.

    Returns ``(ctx_counts, local_counts, seq_counts)``; seq_counts is indexed
    like `seqs`.
    """
    ctx_counts, local_counts, seq_counts = Counter(), Counter(), Counter()
    read = ReadStats()
    if not ctx_pairs and not book_need and not seqs:
        return ctx_counts, local_counts, seq_counts
    corpus = core.make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)
    tmp = tempfile.mkdtemp(prefix='magiah_bookctx_')
    paths = {'ctx_pairs': os.path.join(tmp, 'ctx_pairs.pkl'),
             'book_need': os.path.join(tmp, 'book_need.pkl'),
             'split_cands': os.path.join(tmp, 'split_cands.pkl')}
    try:
        for key, obj in (('ctx_pairs', set(ctx_pairs)),
                         ('book_need', book_need),
                         ('split_cands', [('', p) for p in seqs])):
            with open(paths[key], 'wb') as f:
                pickle.dump(obj, f, protocol=4)
        with core._pool(spec, cfg, paths) as pool:
            for i, (c, lc, sc, st) in enumerate(
                    pool.imap_unordered(core._ctx_count_chunk, chunks), 1):
                ctx_counts.update(c)
                local_counts.update(lc)
                seq_counts.update(sc)
                read.add(ReadStats.from_dict(st))
                if progress:
                    progress(f'  [context] chunk {i}/{len(chunks)}')
    finally:
        for p in paths.values():
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    limit = core.unread_limit(cfg)
    if read.unread() > limit:
        # counts from a partial pass would understate ctx_hits silently
        gaps = core.gap_record({'context': read}, {}, limit,
                               core.spec_db(spec))
        msg = [f'אימות ההקשר מול המאגר לא הצליח לקרוא {read.unread():,} '
               f'שורות; הסריקה בוטלה כדי לא להציג תוצאה חלקית.']
        if limit:
            msg.append(f'הריצה אישרה לדלג על {limit:,} שורות לכל היותר.')
        msg += core.refs_lines(gaps['unread_refs'], read.unread())
        msg.append(core.proceed_hint(read.unread()))
        raise BookScanError('\n'.join(msg))
    if stats is not None:
        stats.add(read)
    return ctx_counts, local_counts, seq_counts


# ---------------------------------------------------------------------------
# the scan itself
# ---------------------------------------------------------------------------

def scan_book(out_dir, source, key, cfg=None, db_path=None, library_dir=None,
              verify_ctx=False, spec=None, progress=None):
    """Scan one book against the existing lexicon and return its findings.

    Returns a dict with the book's identity and row lists ready to be written
    into the review database:

    ``findings``     per-occurrence rows (word, errtype, suggestion, score,
                     ctx_hits, sugg_local, book_repeat, tanach, evidence and
                     the identity columns the UI's findings table needs).
    ``space_errors`` extra-space rows.
    ``ctx_scope`` / ``split_scope``  'book' or 'corpus' — where the evidence
                     was looked for.
    ``split_alternatives``  every segmentation tried for a reported split,
                     with its spaced count.
    ``coverage`` None, or — when ``cfg.allow_unread`` let the scan skip
                 unreadable rows of the book or of the context pass, or let it
                 rest on a lexicon built partial — the gap record
                 (:func:`magiah.core.gap_record`) that marks it partial.
    """
    t0 = time.time()
    cfg = cfg or Config()
    say = progress or (lambda *_: None)
    limit = core.unread_limit(cfg)

    # Resolve the book BEFORE loading the lexicon: the lexicon is ~33 MB and
    # takes a couple of seconds, and a mistyped book id should fail instantly
    # rather than after that wait (during which no other scan can start).
    book = load_book(source, key, db_path=db_path, library_dir=library_dir,
                     allow_unread=limit)
    say(f'[book] נסרק: {book.title}  ({book.origin})')

    freq = load_lexicon(out_dir, limit)
    say(f'[book] מילון קיים: {len(freq):,} צורות, '
        f'{sum(freq.values()):,} מילים')
    return scan_loaded_book(book, freq, cfg, out_dir, verify_ctx=verify_ctx,
                            spec=spec, progress=say, t0=t0, db_path=db_path)


def scan_loaded_book(book, freq, cfg, out_dir, verify_ctx=False, spec=None,
                     progress=None, t0=None, db_path=None):
    """:func:`scan_book` for a book and lexicon already in memory."""
    t0 = t0 or time.time()
    say = progress or (lambda *_: None)
    limit = core.unread_limit(cfg)
    ctx_stats = ReadStats()
    lex, errors, split_cands, present, lsource = detect_in_book(
        book, freq, cfg, out_dir, progress=say)
    profiles, psource = core.load_ocr_profiles(out_dir)
    prof = profiles.get(book.doc)
    seqs = [parts for cs in split_cands.values() for parts, _ in cs]
    scope = 'corpus' if (verify_ctx and spec is not None) else 'book'

    if scope == 'corpus':
        # One corpus pass counts everything. Context pairs are taken from the
        # findings BEFORE split resolution: a confirmed split only replaces a
        # word's explanation, so the final pairs are a subset of these.
        pre_occ, _, _ = _locate(book, freq, cfg, errors, None)
        ctx_pairs, need = _context_needs(pre_occ, errors)
        say('[book] אימות הקשר מול כל המאגר — זה השלב הארוך בסריקת ספר בודד')
        ctx_counts, lc, seq_counts = verify_context(
            spec, cfg, ctx_pairs, {book.doc: need}, seqs, progress=say,
            stats=ctx_stats)
        local_counts = Counter({w: c for (d, w), c in lc.items()
                                if d == book.doc})
    else:
        seq_counts = {}
        seq = core.SeqCounter(seqs)
        for toks in _book_tokens(book):
            seq.count(toks, seq_counts)

    alts = _resolve_splits(errors, split_cands, freq, lex, seq_counts, scope)
    say(f'[book] {len(errors):,} צורות חשודות')
    occ, joins, ocr = _locate(book, freq, cfg, errors, prof)

    if scope == 'book':
        # the same two questions as the corpus pass, asked of this book
        ctx_pairs, need = _context_needs(occ, errors)
        ctx_counts = Counter()
        first = {a for a, _ in ctx_pairs}
        for toks in _book_tokens(book):
            core.count_pairs(toks, ctx_pairs, first, ctx_counts)
        local_counts = Counter({w: present.get(w, 0) for w in need})

    n_located = Counter(w for _, _, w, _, _, _ in occ)
    base = {'tanach': 0, 'origin': book.origin, 'source': book.title,
            'doc': book.doc}
    rows = []
    for unit, ref, w, prev, nxt, snip in occ:
        fw, errtype, sugg, fs, score = errors[w]
        hits = 0
        if core.uses_context(errtype):
            hits = (ctx_counts.get((prev, sugg), 0)
                    + ctx_counts.get((sugg, nxt), 0))
        local = local_counts.get(sugg, 0) if errtype in LOCAL_TYPES else 0
        rows.append({
            **base, 'word': w, 'errtype': errtype, 'suggestion': sugg,
            'score': score, 'ctx_hits': hits, 'sugg_local': local,
            'book_repeat': core.book_repeat_flag(
                errtype, n_located[w], freq.get(w, 0) <= present.get(w, 0)),
            'evidence': core.evidence_kind(errtype, sugg, fs, hits, local, 0,
                                           ctx_scope=scope,
                                           split_scope=scope),
            'ref': ref, 'unit': unit, 'snippet': snip,
        })
    for unit, ref, w, fw, sugg, fs, snip in ocr:
        rows.append({
            **base, 'word': w, 'errtype': 'ocr_profile', 'suggestion': sugg,
            'score': core.ocr_score(fw, fs), 'ctx_hits': 0, 'sugg_local': 0,
            'book_repeat': 0,
            'evidence': core.evidence_kind('ocr_profile', sugg, fs, 0, 0, 0),
            'ref': ref, 'unit': unit, 'snippet': snip,
        })

    space_rows = [{
        'part1': p1, 'part2': p2, 'joined': j, 'join_freq': jf,
        'origin': book.origin, 'source': book.title, 'ref': ref,
        'unit': unit, 'doc': book.doc, 'snippet': snip,
    } for unit, ref, p1, p2, j, jf, snip in joins]

    split_alts = [{'word': w, 'parts': p, 'observed': o, 'expected': e,
                   'chosen': bool(ch),
                   'is_primary': errors[w][1] == 'missing_space'}
                  for w, lst in alts.items()
                  for p, o, e, ch in lst]

    # same rule as a full scan: accepted is not complete
    passes = {'book': book.stats or ReadStats(), 'context': ctx_stats}
    coverage = core.gap_record(
        passes, core.accepted_gaps(out_dir, 'lexicon'), limit,
        db_path or (spec and core.spec_db(spec)) or OTZARIA_DB)
    if coverage and not coverage['accepted']:
        # each part was within the limit, their union is not: the book's
        # rows, the context pass and the lexicon missed different rows
        raise BookScanError('\n'.join(
            [f'הסריקה של «{book.title}» חסרה {coverage["unread_rows"]:,} '
             f'שורות בסך הכול (בספר, באימות ההקשר ובמילון), יותר מ-'
             f'{limit:,} שהותרו; היא בוטלה כדי לא להציג תוצאה חלקית.']
            + core.refs_lines(coverage['unread_refs'],
                              coverage['unread_rows'])
            + [core.proceed_hint(coverage['unread_rows'])]))
    say(f'[book] הסתיים: {len(rows):,} ממצאים, '
        f'{len(space_rows):,} רווחים מיותרים  ({time.time() - t0:.1f} שניות)')
    if coverage:
        say(book_coverage_warning(book.title, coverage))
    return {
        'doc': book.doc, 'title': book.title, 'origin': book.origin,
        'kind': book.kind, 'path': book.path, 'lines': len(book),
        # the bytes the rows above were read from (context verification can
        # run for minutes; the file may change meanwhile)
        'file_sha': book.file_sha, 'file_size': book.file_size,
        'ctx_scope': scope, 'split_scope': scope,
        'calibration': {'learned': lsource or 'none',
                        'ocr_profiles': psource or 'none'},
        'findings': rows, 'space_errors': space_rows,
        'split_alternatives': split_alts,
        'coverage': coverage,
        'seconds': round(time.time() - t0, 1),
    }


def book_coverage_warning(title, coverage):
    """The Hebrew notice for a book scan marked partial."""
    rows = coverage['unread_rows']
    lines = [f'[book] אזהרה: הסריקה של «{title}» חלקית — {rows:,} שורות לא '
             f'נקראו ולא נכללו בה (אושר במפורש ב-'
             f'‎--allow-unread {coverage["allow_unread"]}‎).']
    if coverage.get('inherited'):
        lines.append('היא מבוססת על מילון שנבנה מקריאה חלקית של המאגר.')
    return '\n'.join(lines + core.refs_lines(coverage['unread_refs'], rows))
