# -*- coding: utf-8 -*-
"""Ranked alternatives for an edit-1 finding, judged by its neighbours.

Why this exists
---------------
The edit-1 rule (:func:`magiah.core.edit1_scored`) usually finds several
common words one edit away from a rare one, and keeps the one with the best
*word-level* score: the frequency ratio plus a bonus for a confusable pair,
an extra/missing ו or י or a swap. The same suggestion is then made at every
occurrence, whatever the words around it. Where two candidates are close,
the neighbours often tell them apart (בית/בין before המלך).

This module lists, for each occurrence of such a finding, up to
``cfg.alternatives`` of those candidates, best first:

* **candidates** — exactly the ones the edit-1 rule accepted for the word
  (same filters, same scores); no new candidate generator;
* **context** — :func:`magiah.realword.posterior`, the bigram/trigram model
  of the real-word check, over the candidates between the occurrence's
  previous and next word. The detector's bonus is added as the channel, so
  with no neighbours the order would be the detector's own. The detector's
  first choice keeps first place unless the context prefers another one by
  :data:`MARGIN` (log10 odds); the others follow the context;
* **fallback** — when there are no counts (a book-scope scan), no
  neighbour, the n-grams were not counted, or none of the candidates was
  ever seen next to these neighbours, the detector's own order is kept
  (``by: 'frequency'``): the neighbours then say nothing.

The first alternative in the detector's order is always the finding's
suggestion. ``cfg.alt_rerank`` (off by default) lets the context choose the
suggestion too: where the context ranking (``by: 'context'``) puts another
candidate first, the finding proposes that one, with that candidate's own
type and score.

Where the counts come from
--------------------------
The same pass as the real-word check (``--book-verify-ctx``), without the
rows of the scanned book itself: an error repeated in the book cannot vouch
for its own correction. A book-scope scan has no corpus counts and lists
the alternatives in the detector's order.

Book scan only, off by default (``Config.alternatives = 0``): the findings
then carry no ``alternatives`` and nothing else changes.
"""
import math

from .core import edit1_record, edit1_scored, uses_context
from .realword import posterior

BY_CONTEXT = 'context'
BY_FREQUENCY = 'frequency'
# log10 odds by which the context must prefer another candidate to put it
# before the detector's own choice (the rest follow the context). Chosen on
# injected errors in library text, where the detector's first choice is
# already right most of the time; below it, the context's overrides were
# about as often wrong as right
MARGIN = 2.0
_LN10 = math.log(10)


def candidates(w, fw, lex, edit1_cands):
    """The edit-1 corrections of `w` (``edit1_scored`` tuples), best first
    by the detector's score; on a tie in sorted order, as the detector
    picks."""
    return sorted(edit1_scored(w, fw, lex, edit1_cands),
                  key=lambda c: (-c[1], c[0]))


def eligible(errtype):
    """Findings whose suggestion came out of the edit-1 rule."""
    return uses_context(errtype)


def needs(items):
    """The (bigram, trigram) sets :func:`rank` needs for `items`, an
    iterable of ``(prev, word, next, candidate words)``."""
    n2, n3 = set(), set()
    for prev, w, nxt, cs in items:
        if not (prev or nxt):
            continue
        for x in [w] + list(cs):
            if prev:
                n2.add((prev, x))
            if nxt:
                n2.add((x, nxt))
            if prev and nxt:
                n3.add((prev, x, nxt))
    return n2, n3


def _hits(counts, prev, x, nxt):
    return ((counts.bi(prev, x) if prev else 0)
            + (counts.bi(x, nxt) if nxt else 0))


def scores(lex, counts, prev, w, nxt, cands, own=None):
    """Natural-log score of each of `cands`: the context posterior plus the
    detector's bonus as the channel (its score less the frequency ratio the
    context model already weighs)."""
    _, sc = posterior(lex, counts, prev, w, nxt, [c[0] for c in cands], own)
    fw1 = max(lex.freq.get(w, 0), 1)
    return [sc[i + 1] + _LN10 * (c[1] - math.log10(lex.freq[c[0]] / fw1))
            for i, c in enumerate(cands)]


def anchor(cands, sugg):
    """`cands` with the finding's own suggestion `sugg` first (the rest in
    the detector's order), or [] when `sugg` is not among them: the
    ranking is only defined relative to the suggestion actually made, so a
    finding whose candidates do not contain it gets no alternatives."""
    for i, c in enumerate(cands):
        if c[0] == sugg:
            return [c] + cands[:i] + cands[i + 1:]
    return []


def rank(lex, counts, prev, w, nxt, cands, show, own=None):
    """Up to `show` of `cands` (:func:`candidates`) for one occurrence of
    `w` between `prev` and `nxt` ('' — no neighbour), best first:
    ``[{'suggestion', 'by', 'ctx_hits'}]``. `counts` -- a
    :class:`magiah.realword.Counts` (None: none were made); `own` -- see
    :func:`magiah.realword.posterior`.

    ``by`` is ``'context'`` when the neighbours ranked the list, else
    ``'frequency'`` (the detector's order); ``ctx_hits`` is how often the
    candidate was seen beside these neighbours (None without counts)."""
    show = max(0, int(show))
    words = [c[0] for c in cands]
    order, by = list(range(len(words))), BY_FREQUENCY
    hits = [None] * len(words)
    if counts is not None and words and (prev or nxt) \
            and counts.covers(prev, nxt, [w] + words):
        hits = [_hits(counts, prev, x, nxt) for x in words]
        if any(hits):
            sc = scores(lex, counts, prev, w, nxt, cands, own)
            order = sorted(order, key=lambda i: (-sc[i], words[i]))
            if order[0] and sc[order[0]] - sc[0] < MARGIN * _LN10:
                # not sure enough to pass the detector's own choice
                order.remove(0)
                order.insert(0, 0)
            by = BY_CONTEXT
    return [{'suggestion': words[i], 'by': by, 'ctx_hits': hits[i]}
            for i in order[:show]]


def reranked(w, cands, alts, lex):
    """The finding ``(errtype, suggestion, sugg_freq, score)`` of the
    context's first choice, when the context put another candidate than
    the detector's first (`cands`[0], see :func:`anchor`); else None
    (``cfg.alt_rerank``)."""
    if not alts or not cands or alts[0]['by'] != BY_CONTEXT             or alts[0]['suggestion'] == cands[0][0]:
        return None
    best = next((c for c in cands if c[0] == alts[0]['suggestion']), None)
    return edit1_record(w, best, lex) if best else None
