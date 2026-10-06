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
same per-word rules from :func:`magiah.core.detect` to just the types present
in it, scoring every candidate against the *global* lexicon. The lexicon is
never rebuilt — it is the sole source of frequencies, exactly as the user
requires. A book scan therefore costs seconds instead of half an hour.

Rule parity with the full pipeline
----------------------------------
The detection rules are imported from :mod:`magiah.core`, not re-implemented:
``_dld1``, ``_segment``, ``_frequent_stem`` and the CONFUSABLE/prefix/suffix
tables are the same objects the full scan uses. The per-word decision code
below mirrors ``core.detect``'s loop and ``core._locate_chunk``'s occurrence
filters (name triggers, editorial brackets, foreign-line skip, extra-space
pass). Where the full pipeline consults the whole corpus and a single book
cannot, the difference is explicit and recorded — never silently faked:

* **missing_space** — the full scan confirms a split by finding the same words
  *spaced* elsewhere in the corpus. Alone, a book can only confirm it inside
  itself; otherwise the split rests on the global part frequencies and is
  scored lower (and marked ``split_local``).
* **ctx_hits** — the full scan counts how often the correction appears beside
  the same neighbours corpus-wide. A book scan counts that inside the book; if
  the caller enables :func:`verify_context` it is then upgraded to the true
  corpus-wide count. That costs one full pass over the corpus (measured: ~10
  min on the hybrid corpus at the default 3 workers) regardless of how small
  the book is, so it is off by default.
* **book_repeat** — in a full scan this demotes a word whose every occurrence
  sits in one book (an author's idiosyncratic spelling). That test needs the
  rest of the corpus to be meaningful, so here it is derived from the lexicon
  instead: a word whose in-book count already accounts for its entire global
  frequency is the same "only ever used here" signal. See ``_book_repeat``.

One more deliberate deviation: ``core.detect`` iterates *the lexicon*, so every
word it scores has ``freq >= 1``. A book scan iterates *the book*, which may
contain a word that is absent from the lexicon entirely (``freq == 0``) — a
newly typed book is full of them. Every frequency-ratio denominator here is
therefore ``max(fw, 1)``; without it the first such word raises
ZeroDivisionError. The guard is also slightly stricter than core (an unseen
word must clear the absolute threshold), never looser.

Everything else — the ranking formula, the verified test, the report tables —
is shared with the full pipeline so the findings are directly comparable.
"""
import math
import os
import pickle
import time
from collections import Counter

from . import core
from .book_source import load_book
from .config import Config
from .normalize import (CONFUSABLE, FINALS, FROM_FINAL, PREFIX_LETTERS,
                        SUFFIX_LETTERS, TO_FINAL, TOKEN_RE, clean, is_abbrev,
                        tokenize)

# error types whose suggestion is a single word the book itself may also use
LOCAL_TYPES = ('edit1_sub', 'edit1_ins', 'edit1_del', 'edit1_swap',
               'nonfinal_end', 'spelling_variant', 'lost_quotes')


class BookScanError(Exception):
    """A book scan cannot run (Hebrew message ready to display)."""


def load_lexicon(out_dir):
    """The existing lexicon. Never rebuilt here — that is the whole point."""
    path = os.path.join(out_dir, core.LEXICON_F)
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        raise BookScanError(
            'סריקת ספר בודד מתבססת על המילון הקיים, והוא לא נמצא '
            f'({core.LEXICON_F}).\nיש להריץ פעם אחת סריקה מלאה (או את שלב '
            '"בניית מילון") לפני שאפשר לסרוק ספר בודד.')
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


def _load_learned(out_dir):
    """Letter-confusion weights from `calibrate`, if it has ever run."""
    path = os.path.join(out_dir, 'confusion_learned.json')
    if not os.path.exists(path):
        return {}
    import json
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _read_book_tokens(book, cfg, freq):
    """Tokenize the book once.

    Returns ``(occurrences, present, bigrams)`` where `occurrences` is a list
    of ``(unit, ref, idx, token, prev, next, snippet)`` for every token of
    every non-skipped line, `present` counts each type inside the book, and
    `bigrams` counts adjacent pairs (used to confirm splits and local context).

    Foreign / heavily garbled lines are skipped with the same rule as
    ``core._locate_chunk`` — otherwise a Judeo-Arabic passage produces a wall
    of meaningless "corrections".
    """
    occurrences, joins = [], []
    present, bigrams = Counter(), Counter()
    for unit, ref, content in book.lines:
        text = clean(content)
        toks = list(TOKEN_RE.finditer(text))
        if not toks:
            continue
        real = [m.group() for m in toks if len(m.group()) >= 3]
        if len(real) >= 5:
            uncommon = sum(1 for t in real if freq.get(t, 0) < cfg.common_min)
            if uncommon > cfg.foreign_ratio * len(real):
                continue
        words = [m.group() for m in toks]
        present.update(words)
        for i in range(len(words) - 1):
            bigrams[(words[i], words[i + 1])] += 1

        # -- extra-space pass (identical rule to core._locate_chunk) --------
        joined_idx = set()
        for k, m in enumerate(toks[:-1]):
            w = m.group()
            if is_abbrev(w):
                continue
            nx = toks[k + 1]
            b = nx.group()
            if (nx.start() - m.end() <= 1 and not is_abbrev(b)
                    and not text[m.end():nx.start()].strip()):
                mn = min(freq.get(w, 0), freq.get(b, 0))
                if mn <= 3:
                    fj = freq.get(w + b, 0)
                    if fj >= cfg.join_min and fj >= 30 * max(1, mn):
                        s = m.start()
                        joins.append((unit, ref, w, b, w + b, fj,
                                      text[max(0, s - 45):
                                           nx.end() + 45].strip()))
                        joined_idx.update((k, k + 1))

        for k, m in enumerate(toks):
            if k in joined_idx:
                continue
            w = m.group()
            s, e = m.start(), m.end()
            # trailing geresh = abbreviation; adjacent bracket = editorial mark
            if e < len(text) and text[e] in '\'"([{*&':
                continue
            if s > 0 and text[s - 1] in ')]}*&':
                continue
            prev = toks[k - 1].group() if k else ''
            nxt = toks[k + 1].group() if k + 1 < len(toks) else ''
            # core._locate_chunk tests the name trigger against BOTH the
            # previous token and the one before it (מוה"ר ר' פלוני), so the
            # second-back token has to travel with the occurrence
            prev2 = toks[k - 2].group() if k >= 2 else ''
            occurrences.append((unit, ref, k, w, prev, nxt,
                                text[max(0, s - 45):e + 45].strip(), prev2))
    return occurrences, joins, present, bigrams


def _book_repeat(word, in_book, freq):
    """Stand-in for the full scan's same-book-repeat demotion.

    The corpus-wide test ("every occurrence of this word sits in one book") is
    unavailable when only one book is read. The lexicon still answers the
    question that test really asks: if the word's global frequency is fully
    accounted for by this book's own occurrences, then nowhere else in the
    corpus uses it — the same "author's own spelling" signal. Requiring 2+
    occurrences matches the full pipeline's ``n >= 2`` condition.
    """
    return 1 if in_book >= 2 and freq.get(word, 0) <= in_book else 0


def detect_in_book(book, freq, cfg, out_dir, progress=None):
    """Flag the suspect word types that occur in `book`.

    Mirrors ``core.detect``'s per-word rules, but iterates only over the types
    present in this book and consults the global lexicon for every frequency.
    Returns ``(errors, occurrences, joins, present, bigrams)``.
    """
    whitelist = _load_whitelist(cfg, out_dir)
    learned = _load_learned(out_dir)
    N = sum(freq.values())

    occurrences, joins, present, bigrams = _read_book_tokens(book, cfg, freq)
    if progress:
        progress(f'[book] {len(book):,} שורות, {sum(present.values()):,} '
                 f'מילים, {len(present):,} צורות שונות')

    # Correction candidates come from the global lexicon, exactly as in a full
    # scan — but reached differently. core.detect builds a SymSpell deletion
    # index over all ~1.9M common types (~4 s and hundreds of MB) because it
    # then queries it a million times. A book asks far fewer questions, so here
    # each word probes the lexicon directly for its own edit-1 neighbourhood
    # (~0.9 ms/word, no index at all). The two were verified to produce
    # identical candidate sets on 4,000 real rare words.
    common_min, ed1_ratio = cfg.common_min, cfg.ed1_ratio

    def is_common(w):
        f = freq.get(w, 0)
        return f >= common_min and not is_abbrev(w) and 2 <= len(w) <= 18

    # abbreviations that lost their gershayim (רמבם -> רמב"ם). Built over the
    # whole lexicon, identically to core.detect — the map is keyed by the
    # de-punctuated form, so it has to see every abbreviation the corpus knows,
    # not just the ones this book happens to contain.
    abbrev_map = {}
    for a, fa in freq.items():
        if fa >= common_min and is_abbrev(a):
            k = a.replace('"', '').replace("'", '')
            if len(k) >= 2 and (k not in abbrev_map
                                or freq[abbrev_map[k]] < fa):
                abbrev_map[k] = a

    errors = {}

    def record(w, fw, errtype, sugg, fs, score):
        cur = errors.get(w)
        if cur is None or score > cur[4]:
            errors[w] = (fw, errtype, sugg, fs, score)

    split_cands = {}

    def add_split(w, parts, strong):
        cur = split_cands.get(w)
        if cur is None or (strong and not cur[1]):
            split_cands[w] = (tuple(parts), strong)

    for w in present:
        if w in whitelist:
            continue
        fw = freq.get(w, 0)

        # non-final letter at word end (checked up to medium frequency)
        if len(w) >= 3 and w[-1] in TO_FINAL and not is_abbrev(w) and fw <= 20:
            wf = w[:-1] + TO_FINAL[w[-1]]
            ff = freq.get(wf, 0)
            if ff >= cfg.part_min and ff >= 50 * max(fw, 1):
                record(w, fw, 'nonfinal_end', wf, ff,
                       4 + math.log10(ff / max(fw, 1)))

        if fw > cfg.rare_max or len(w) < 3 or is_abbrev(w):
            continue

        # 1) final-form letter in the middle of a word
        mid_final = [i for i, ch in enumerate(w[:-1]) if ch in FINALS]
        if mid_final:
            handled = False
            for i in mid_final:
                a, b = w[:i + 1], w[i + 1:]
                if freq.get(a, 0) >= 10 and freq.get(b, 0) >= 10 and len(b) >= 2:
                    add_split(w, (a, b), True)
                    handled = True
            if not handled:
                w2 = ''.join(FROM_FINAL.get(ch, ch) if i < len(w) - 1 else ch
                             for i, ch in enumerate(w))
                f2 = freq.get(w2, 0)
                if f2 >= common_min:
                    record(w, fw, 'final_midword', w2, f2, 5 + math.log10(f2))
                else:
                    record(w, fw, 'final_midword', '', 0, 3.0)

        # 2) missing space
        if len(w) >= 5 and w not in split_cands:
            seg = core._segment(w, freq, cfg)
            if seg:
                parts, _ = seg
                exp = N * math.prod(freq[p] / N for p in parts)
                if exp >= cfg.exp_prefilter:
                    add_split(w, parts, False)

        # 3) abbreviation that lost its gershayim
        ab = abbrev_map.get(w)
        if ab and freq[ab] >= ed1_ratio * max(fw, 1):
            record(w, fw, 'lost_quotes', ab, freq[ab],
                   5 + math.log10(freq[ab] / max(fw, 1)))

        # 4) frequent stem + inflection suffix = legitimate morphology
        if core._frequent_stem(w, freq, common_min):
            continue

        # 5) edit distance 1 from a frequent word.
        # Candidates are generated from the word itself (deletions, and the
        # words that share a deletion) by probing the lexicon — no global
        # deletion index needed.
        cands = set()
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            if len(d) >= 2 and is_common(d):
                cands.add(d)
            # insertions/substitutions: every letter at every position
            for ch in _HEB:
                if ch != w[i]:
                    sub = w[:i] + ch + w[i + 1:]
                    if is_common(sub):
                        cands.add(sub)
                ins = w[:i] + ch + w[i:]
                if is_common(ins):
                    cands.add(ins)
            # transposition
            if i + 1 < len(w) and w[i] != w[i + 1]:
                sw = w[:i] + w[i + 1] + w[i] + w[i + 2:]
                if is_common(sw):
                    cands.add(sw)
        for ch in _HEB:                      # append at the very end
            app = w + ch
            if is_common(app):
                cands.add(app)

        best_c, best_score, best_kind, best_ch = None, 0.0, '', ''
        for c in cands:
            if c == w:
                continue
            fc = freq.get(c, 0)
            if fc < common_min or fc < ed1_ratio * max(fw, 1):
                continue
            r = core._dld1(w, c)
            if r is None:
                continue
            kind, ch_a, ch_b, pos = r
            if kind == 'ins':
                doubled = ((pos > 0 and w[pos - 1] == ch_a)
                           or (pos + 1 < len(w) and w[pos + 1] == ch_a))
                if (not doubled and pos <= 2 and ch_a in PREFIX_LETTERS
                        and all(x in PREFIX_LETTERS for x in w[:pos])):
                    continue
                if not doubled and pos == len(w) - 1 and ch_a in SUFFIX_LETTERS:
                    continue
            elif kind == 'sub' and pos == 0 \
                    and ch_a in PREFIX_LETTERS and ch_b in PREFIX_LETTERS:
                continue
            score = math.log10(fc / max(fw, 1))
            if kind == 'sub':
                lc = learned.get(ch_a + ch_b, 0)
                if lc:
                    score += min(2.5, 0.5 + 0.8 * math.log10(1 + lc))
                elif not learned and (ch_a, ch_b) in CONFUSABLE:
                    score += 2
            elif kind in ('ins', 'del') and ch_a in 'וי':
                score += 1
            elif kind == 'swap':
                score += 1
            if score > best_score:
                best_c, best_score, best_kind, best_ch = c, score, kind, ch_a
        if best_c:
            if best_kind in ('ins', 'del') and best_ch in 'וי':
                errtype = 'spelling_variant'
            else:
                errtype = f'edit1_{best_kind}'
            record(w, fw, errtype, best_c, freq[best_c], best_score)

    # --- confirm split candidates ----------------------------------------
    # A full scan confirms a split by finding the same words *spaced* somewhere
    # in the 417M-token corpus, and scores it by how often that happens. One
    # book holds far too little text for that count to mean the same thing, so
    # the book-local observation is used as confirmation but NOT as corpus
    # evidence: the score stays below the band a corpus-confirmed split earns,
    # so a book-only split never outranks a corpus-verified finding for the
    # same word. `record` keeps the highest-scoring explanation per word.
    for w, (parts, strong) in split_cands.items():
        obs = _spaced_in_book(parts, bigrams)
        minlen = min(len(p) for p in parts)
        exp = N * math.prod(freq[p] / N for p in parts)
        if obs:
            # seen spaced inside this very book — the strongest evidence a
            # single book can give, but capped: log10 of a book-scale count
            # cannot be compared with a corpus-scale one
            score = 3.5 + min(1.0, math.log10(obs + 1)) \
                + (1.0 if strong else 0.0) + (0.5 if minlen >= 3 else 0.0)
        elif strong:
            # a final-form letter mid-word already proves the break point
            score = 3.5
        elif minlen >= 3 and exp >= 1:
            # unconfirmed: rests only on the parts being frequent globally
            score = 2.5
        else:
            continue
        record(w, freq.get(w, 0), 'missing_space', ' '.join(parts),
               obs, score)

    if progress:
        progress(f'[book] {len(errors):,} צורות חשודות')
    return errors, occurrences, joins, present, bigrams


_HEB = 'אבגדהוזחטיכלמנסעפצקרשתךםןףץ'


def _spaced_in_book(parts, bigrams):
    """How often the split's parts occur adjacent (spaced) inside the book."""
    n = bigrams.get((parts[0], parts[1]), 0)
    if len(parts) == 2:
        return n
    return min(n, bigrams.get((parts[1], parts[2]), 0))


# ---------------------------------------------------------------------------
# optional: upgrade ctx_hits to the true corpus-wide count
# ---------------------------------------------------------------------------

def verify_context(spec, cfg, ctx_pairs, book_need, progress=None):
    """Count the wanted (word, word) pairs and book-local words corpus-wide.

    This is the one part of a book scan that must read the whole corpus, so it
    is optional: without it ``ctx_hits`` reflects the book alone. It reuses
    ``core._ctx_count_chunk`` and the same worker pool the full pipeline uses,
    which keeps the counting semantics identical.

    Returns ``(ctx_counts, local_counts)``.
    """
    import tempfile
    ctx_counts, local_counts = Counter(), Counter()
    if not ctx_pairs and not book_need:
        return ctx_counts, local_counts
    corpus = core.make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)
    tmp = tempfile.mkdtemp(prefix='magiah_bookctx_')
    ctx_path = os.path.join(tmp, 'ctx_pairs.pkl')
    need_path = os.path.join(tmp, 'book_need.pkl')
    try:
        with open(ctx_path, 'wb') as f:
            pickle.dump(set(ctx_pairs), f, protocol=4)
        with open(need_path, 'wb') as f:
            pickle.dump(book_need, f, protocol=4)
        with core._pool(spec, cfg, {'ctx_pairs': ctx_path,
                                    'book_need': need_path}) as pool:
            for i, (c, lc, _st) in enumerate(
                    pool.imap_unordered(core._ctx_count_chunk, chunks), 1):
                ctx_counts.update(c)
                local_counts.update(lc)
                if progress:
                    progress(f'  [context] chunk {i}/{len(chunks)}')
    finally:
        for p in (ctx_path, need_path):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    return ctx_counts, local_counts


# ---------------------------------------------------------------------------
# the scan itself
# ---------------------------------------------------------------------------

def scan_book(out_dir, source, key, cfg=None, db_path=None, library_dir=None,
              verify_ctx=False, spec=None, progress=None):
    """Scan one book against the existing lexicon and return its findings.

    Returns a dict with the book's identity and two row lists ready to be
    written into the review database:

    ``errors``   word-level rows (word, freq, errtype, suggestion, sugg_freq,
                 score) — the same shape as report.db's ``errors`` table.
    ``occurrences`` per-occurrence rows carrying the identity columns the UI's
                 findings table needs.
    ``space_errors`` extra-space rows.
    """
    t0 = time.time()
    cfg = cfg or Config()
    say = progress or (lambda *_: None)

    # Resolve the book BEFORE loading the lexicon: the lexicon is ~33 MB and
    # takes a couple of seconds, and a mistyped book id should fail instantly
    # rather than after that wait (during which no other scan can start).
    book = load_book(source, key, db_path=db_path, library_dir=library_dir)
    say(f'[book] נסרק: {book.title}  ({book.origin})')

    freq = load_lexicon(out_dir)
    say(f'[book] מילון קיים: {len(freq):,} צורות, '
        f'{sum(freq.values()):,} מילים')

    errors, occurrences, joins, present, bigrams = detect_in_book(
        book, freq, cfg, out_dir, progress=say)

    # occurrences of flagged words only, with the full pipeline's name-trigger
    # filter applied
    occ_rows = []
    for unit, ref, _, w, prev, nxt, snip, prev2 in occurrences:
        fr = errors.get(w)
        if fr is None:
            continue
        # a rare word right after a title (ר' פלוני, אבד"ק פלוני) is a
        # person/place name, not a typo — same test as core._locate_chunk
        if fr[1] not in ('missing_space', 'final_midword') \
                and (prev in core.NAME_TRIGGERS
                     or prev2 in core.NAME_TRIGGERS):
            continue
        occ_rows.append((w, unit, ref, prev, nxt, snip))

    # --- verification signals --------------------------------------------
    ctx_pairs, need = set(), set()
    for w, unit, ref, prev, nxt, snip in occ_rows:
        fr = errors[w]
        if fr[1].startswith('edit1') or fr[1] == 'spelling_variant':
            if prev:
                ctx_pairs.add((prev, fr[2]))
            if nxt:
                ctx_pairs.add((fr[2], nxt))
        if fr[1] in LOCAL_TYPES and fr[2]:
            need.add(fr[2])

    ctx_counts, local_counts = Counter(), Counter()
    ctx_scope = 'book'
    if verify_ctx and spec is not None and (ctx_pairs or need):
        say('[book] אימות הקשר מול כל המאגר — זה השלב הארוך בסריקת ספר בודד')
        ctx_counts, local_counts = verify_context(
            spec, cfg, ctx_pairs, {book.doc: need}, progress=say)
        local_counts = Counter({w: c for (d, w), c in local_counts.items()
                                if d == book.doc})
        ctx_scope = 'corpus'
    else:
        # book-local fallback: the same two questions, asked of this book
        for (a, b), n in bigrams.items():
            if (a, b) in ctx_pairs:
                ctx_counts[(a, b)] += n
        for w in need:
            local_counts[w] = present.get(w, 0)

    rows = []
    for w, unit, ref, prev, nxt, snip in occ_rows:
        fw, errtype, sugg, fs, score = errors[w]
        hits = 0
        if errtype.startswith('edit1') or errtype == 'spelling_variant':
            hits = (ctx_counts.get((prev, sugg), 0)
                    + ctx_counts.get((sugg, nxt), 0))
        local = local_counts.get(sugg, 0) if errtype in LOCAL_TYPES else 0
        rows.append({
            'word': w, 'errtype': errtype, 'suggestion': sugg,
            'score': score, 'ctx_hits': hits, 'sugg_local': local,
            'book_repeat': _book_repeat(w, present.get(w, 0), freq),
            'tanach': 0, 'origin': book.origin, 'source': book.title,
            'ref': ref, 'unit': unit, 'doc': book.doc, 'snippet': snip,
        })

    space_rows = [{
        'part1': p1, 'part2': p2, 'joined': j, 'join_freq': jf,
        'origin': book.origin, 'source': book.title, 'ref': ref,
        'unit': unit, 'doc': book.doc, 'snippet': snip,
    } for unit, ref, p1, p2, j, jf, snip in joins]

    say(f'[book] הסתיים: {len(rows):,} ממצאים, '
        f'{len(space_rows):,} רווחים מיותרים  ({time.time() - t0:.1f} שניות)')
    return {
        'doc': book.doc, 'title': book.title, 'origin': book.origin,
        'kind': book.kind, 'path': book.path, 'lines': len(book),
        'ctx_scope': ctx_scope,
        'findings': rows, 'space_errors': space_rows,
        'seconds': round(time.time() - t0, 1),
    }
