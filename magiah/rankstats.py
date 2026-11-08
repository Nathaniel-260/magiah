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
* ``rw_lr`` (``Config.rw_corpus``) -- the real-word language model of the
  book scan (:func:`magiah.realword.posterior`) asked of each edit-1 /
  spelling-variant finding: original against correction between the same
  neighbours, bigrams and trigrams (:func:`rw_lr`). The original's n-grams
  are counted outside its own book: an error copied within a book would
  otherwise vouch for itself.
* ``par_sugg`` / ``par_word`` / ``par_vote`` (``Config.par_corpus``) --
  parallel passages: the occurrence's window of up to two words on each
  side (:func:`par_window`) is looked up in the corpus with the correction
  in the middle (``par_sugg``: evidence of an error) and with the original
  word in another book (``par_word``: evidence the text is right), and
  ``par_vote`` is the smoothed log10 ratio of the two (:func:`par_vote`).
"""
import math

# columns of report.db's `occurrences` after occ_score, in insert order
OCC_STAT_COLS = ('ctx_exp', 'ctx_assoc', 'rw_lr', 'par_sugg', 'par_word',
                 'par_vote')
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


# ---------------------------------------------------------------------------
# (2) the real-word language model, asked of the corpus scan's corrections
# ---------------------------------------------------------------------------

def rw_needs(occ, flagged, uses_context):
    """The n-grams :func:`rw_lr` needs for the located occurrences `occ`
    (``(word, unit, doc, prev, next, snippet)``), as the dict
    :func:`count_rw` reads: ``own`` the bigrams and trigrams of the
    original words (``words``; counted per book, so its own book can be
    left out), ``sugg3`` the trigrams of the corrections (``suggs``; their
    bigrams are the context pairs, counted anyway)."""
    own, sugg3, words, suggs = set(), set(), set(), set()
    for w, _, _, prev, nxt, _ in occ:
        fr = flagged[w]
        if not (uses_context(fr[1]) and fr[2]) or not (prev or nxt):
            continue
        words.add(w)
        if prev:
            own.add((prev, w))
        if nxt:
            own.add((w, nxt))
        if prev and nxt:
            own.add((prev, w, nxt))
            sugg3.add((prev, fr[2], nxt))
            suggs.add(fr[2])
    return {'own': own, 'sugg3': sugg3, 'words': words, 'suggs': suggs}


def count_rw(toks, doc, rwc, own_counts, sugg3_counts):
    """Count one token list's n-grams of :func:`rw_needs`: the original
    word's per ``(ngram, doc)``, the correction's trigrams in total. Both
    are looked up from their middle word, a rare word (own) or a
    correction (sugg3)."""
    words, suggs = rwc['words'], rwc['suggs']
    own, sugg3 = rwc['own'], rwc['sugg3']
    n = len(toks)
    for i, t in enumerate(toks):
        if t in words:
            p = toks[i - 1] if i else None
            x = toks[i + 1] if i + 1 < n else None
            for g in ((p, t), (t, x), (p, t, x)):
                if None not in g and g in own:
                    own_counts[(g, doc)] += 1
        if t in suggs and 0 < i < n - 1:
            g = (toks[i - 1], t, toks[i + 1])
            if g in sugg3:
                sugg3_counts[g] += 1


class OccCounts:
    """The n-gram counts of one occurrence, in the interface
    :func:`magiah.realword.posterior` reads: the original word's n-grams
    without its own book, the correction's corpus-wide."""

    def __init__(self, pairs, sugg3, own_total, own_doc, doc):
        self.pairs, self.sugg3 = pairs, sugg3
        self.own_total, self.own_doc, self.doc = own_total, own_doc, doc

    def _own(self, g):
        return self.own_total.get(g, 0) - self.own_doc.get((g, self.doc), 0)

    def bi(self, a, b):
        g = (a, b)
        return self._own(g) if g in self.own_total else self.pairs.get(g, 0)

    def tri(self, a, b, c):
        g = (a, b, c)
        return self._own(g) if g in self.own_total else self.sugg3.get(g, 0)


def own_totals(own_counts):
    """Corpus totals of the per-book counts :func:`count_rw` made."""
    tot = {}
    for (g, _), c in own_counts.items():
        tot[g] = tot.get(g, 0) + c
    return tot


class _Lex:
    def __init__(self, freq, N):
        self.freq, self.N = freq, N


def rw_lr(freq, N, counts, prev, w, sugg, nxt, own_w=0):
    """How far the neighbours shift the odds from the original `w` to the
    correction `sugg`: log10 of (posterior odds in context) / (odds on
    frequency alone), by :func:`magiah.realword.posterior` with the single
    candidate `sugg`. The frequency ratio is already in the score, so only
    the context's share is returned. Positive: the context prefers the
    correction. `own_w`: occurrences of `w` in its own book, left out of
    its frequency as its n-grams are (`counts`: an :class:`OccCounts`)."""
    from .realword import posterior
    lex = _Lex(freq, N)
    own = {w: own_w}
    _, ctx = posterior(lex, counts, prev, w, nxt, [sugg], own)
    _, uni = posterior(lex, counts, '', w, '', [sugg], own)
    return round(((ctx[1] - ctx[0]) - (uni[1] - uni[0])) / math.log(10), 4)


# ---------------------------------------------------------------------------
# (3) parallel passages: the same five-word window elsewhere in the corpus
# ---------------------------------------------------------------------------

PAR_SHAPES = ((2, 2), (2, 1), (1, 2))   # words left / right of the centre
SNIPPET_PAD = 45      # characters each side of a word in its snippet


def par_window(w, prev, nxt, snippet):
    """``(left, right)`` -- up to two words on each side of `w`, at least
    three in all -- read from the occurrence's snippet, or None.

    The snippet is a character slice of the line, ``SNIPPET_PAD``
    characters each side of the word (magiah.core._snippet): where that
    many characters stand beside the word the slice may have cut into the
    edge token, which is then not used; fewer means the line (or a space)
    ended there. A snippet where the word stands twice between the same
    neighbours with different outer words is ambiguous."""
    if not prev or not nxt or not snippet:
        return None
    from .normalize import TOKEN_RE
    toks = list(TOKEN_RE.finditer(snippet))
    q = [m.group() for m in toks]
    n = len(q)
    found = set()
    for i in range(1, n - 1):
        if q[i] == w and q[i - 1] == prev and q[i + 1] == nxt:
            whole_l = i - 2 >= 1 or toks[i].start() < SNIPPET_PAD
            whole_r = (i + 2 <= n - 2
                       or len(snippet) - toks[i].end() < SNIPPET_PAD)
            left = (q[i - 2], prev) if i >= 2 and whole_l else (prev,)
            right = (nxt, q[i + 2]) if i + 2 < n and whole_r else (nxt,)
            found.add((left, right))
    if len(found) != 1:
        return None
    left, right = found.pop()
    if len(left) + len(right) < 3:
        return None
    return left, right


def par_needs(windows):
    """The windows :func:`count_par` counts, from ``{(left, right): ...}``
    pairs of (original, correction): a dict with ``word`` (the original's
    windows, counted per book), ``sugg`` (the correction's, counted in
    total) and their centre words."""
    word, sugg, wc, sc = set(), set(), set(), set()
    for (left, right), (w, s) in windows:
        word.add(left + (w,) + right)
        sugg.add(left + (s,) + right)
        wc.add(w)
        sc.add(s)
    return {'word': word, 'sugg': sugg, 'words': wc, 'suggs': sc}


def count_par(toks, doc, par, word_counts, sugg_counts):
    """Count one token list's windows of :func:`par_needs`, looked up from
    their centre word."""
    words, suggs = par['words'], par['suggs']
    n = len(toks)
    for i, t in enumerate(toks):
        is_w, is_s = t in words, t in suggs
        if not (is_w or is_s):
            continue
        for lw, rw in PAR_SHAPES:
            if i - lw < 0 or i + rw >= n:
                continue
            g = tuple(toks[i - lw:i + rw + 1])
            if is_w and g in par['word']:
                word_counts[(g, doc)] += 1
            if is_s and g in par['sugg']:
                sugg_counts[g] += 1


def par_counts(win, w, sugg, doc, sugg_counts, word_total, word_counts):
    """``(par_sugg, par_word)`` of one occurrence: its window with the
    correction, anywhere; with the original, outside its own book."""
    left, right = win
    g = left + (w,) + right
    return (sugg_counts.get(left + (sugg,) + right, 0),
            word_total.get(g, 0) - word_counts.get((g, doc), 0))


def par_vote(par_sugg, par_word):
    """log10 of the window seen with the correction against seen with the
    original (elsewhere), smoothed: positive -- parallel passages read the
    correction; negative -- they read the original."""
    return round(math.log10((par_sugg + 0.5) / (par_word + 0.5)), 4)
