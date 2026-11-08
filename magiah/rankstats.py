# -*- coding: utf-8 -*-
"""Rank signals measured against chance, stored per occurrence in report.db.

Every column here is a *rank* signal: it moves a finding up or down in
RANK_SQL (:mod:`magiah.core`) and never adds or removes one. A column is NULL
where it was not measured (an older report.db, a book-scope scan, a signal
switched off), and RANK_SQL then treats the row as it did before the signal
existed.

Kept apart from :mod:`magiah.core` so the corpus adapters, which ``core``
imports, can name the columns too.

* ``ctx_exp`` / ``ctx_assoc`` -- the context check measured against chance.
  ``ctx_hits`` counts the pairs (previous word, correction) and (correction,
  next word) in the corpus; ``ctx_exp`` is how many such pairs independence
  would predict (``f(neighbour) * f(correction) / N`` per side) and
  ``ctx_assoc`` the smoothed log10 ratio of the two (:func:`ctx_association`).
"""
import math

# columns of report.db's `occurrences` after occ_score, in insert order
OCC_STAT_COLS = ('ctx_exp', 'ctx_assoc')
# ... as carried into occurrences_full by every corpus adapter's enrich
ENRICH_STAT_COLS = ', '.join('o.' + c for c in OCC_STAT_COLS)

CTX_SMOOTH = 0.5      # pseudo-count of the context association


def ctx_expected(freq, N, prev, sugg, nxt):
    """How often the pairs (prev, sugg) and (sugg, nxt) would occur in the
    corpus by chance, if neighbouring words were independent: per side
    ``f(neighbour) * f(sugg) / N``, summed over the sides that have a
    neighbour."""
    if not N:
        return 0.0
    fs = freq.get(sugg, 0)
    return sum(freq.get(a, 0) * fs / N for a in (prev, nxt) if a)


def ctx_association(hits, exp):
    """log10 of the context hits observed against chance, smoothed:
    ``log10((hits + 0.5) / (exp + 0.5))``. Positive: the correction is seen
    beside these neighbours more often than chance; negative: less often
    than chance would predict (real negative evidence); about 0 with no
    hits and a negligible expectation (no information either way)."""
    return round(math.log10((hits + CTX_SMOOTH) / (exp + CTX_SMOOTH)), 4)


def ctx_stats(freq, N, prev, sugg, nxt, hits):
    """``(ctx_exp, ctx_assoc)`` of one occurrence, or ``(None, None)`` when
    it has no neighbour to measure."""
    if not (prev or nxt):
        return None, None
    exp = ctx_expected(freq, N, prev, sugg, nxt)
    return round(exp, 6), ctx_association(hits, exp)
