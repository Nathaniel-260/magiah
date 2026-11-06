# -*- coding: utf-8 -*-
"""Real-word errors in a single-book scan, judged by the neighbouring words.

Why this exists
---------------
Every rule in :mod:`magiah.core` starts from a *rare* word type. A slip that
turns one existing word into another (one letter misread or mistyped, and the
result is itself a word) is invisible to them: the word is common, so nothing
flags it. Roughly half of the one-letter OCR errors are of this kind.

This module asks the neighbours instead. For every common word of the book it
weighs the word against its one-letter neighbours in the lexicon, using how
often each of them occurs between the same two words in the corpus. It is a
small noisy-channel model (Mays, Damerau & Mercer, 1991):

* **candidates** — one substitution along :data:`magiah.normalize.CONFUSABLE`
  or a final <-> regular letter swap, giving a common word (``common_min``);
* **channel** — a fixed penalty for the substitution (:data:`CH_SUB`);
* **context** — the bigram with the previous word and the bigram with the
  next one (Dirichlet smoothing toward the word's corpus frequency,
  ``MU2``; like the n-gram counts, the frequencies leave out the book's own
  occurrences); when any candidate was seen between the same two words, the
  trigram counts take over (``MU3``, the bigram model as their prior);
* the **hint** — the log10 odds that the word is wrong, against the best
  candidate and the word itself, reach ``cfg.real_word_odds``. The default,
  5.49, was tuned on OCR output from outside the corpus; a corpus of another
  kind may want another value.

Tokens another rule already explains on the same line (an extra-space join,
an OCR-profile hit) and words in a name position (one or two words after a
title, :data:`magiah.core.NAME_TRIGGERS`, as for the located findings) are
not judged.

A book's own convention is not an error: a word the book uses at least
``cfg.real_word_rep`` times, and more often than the suggestion, gets no
hint. The comparison matters — repeated OCR slips repeat too, and an
absolute "seen 3 times in the book" skips most of them in a whole book.

Where the counts come from
--------------------------
The pairs and triples are counted in the corpus pass that
``--book-verify-ctx`` already makes (one pass, the same worker), without the
rows of the scanned book itself. They are kept in ``real_word/`` in the
output directory together with the set of n-grams that were asked for, so a
later book-scope scan of the same book reuses them. A word whose neighbours
were not counted — the book was edited since — is not judged (``stale``)
until the next corpus-scope scan. The folder holds one file per book scanned
this way and is never pruned; deleting it only costs a corpus-scope rescan.

Off by default (``Config.real_word``). The findings use their own
``errtype`` (``real_word``); no existing finding changes.
"""
import hashlib
import math
import os
import pickle

from .normalize import CONFUSABLE, FROM_FINAL, TO_FINAL, TOKEN_RE, clean

ERRTYPE = 'real_word'
EVIDENCE = 'context_lm'    # a language-model judgement, not verification
COUNTS_DIR = 'real_word'
FORMAT = 1

MU2, MU3 = 50.0, 3.0               # Dirichlet smoothing: bigrams, trigrams
CH_SUB = math.log(1e-3) + 2.0      # log channel weight of one substitution
_LN10 = math.log(10)

# letter -> the letters it may have been read or typed for
_SUBS = {}
for _a, _b in CONFUSABLE:
    _SUBS.setdefault(_a, set()).add(_b)
for _a, _b in list(TO_FINAL.items()) + list(FROM_FINAL.items()):
    _SUBS.setdefault(_a, set()).add(_b)


def checked(w, lex):
    """Words this module judges: common enough that no rare-word rule saw
    them, 2+ letters, no abbreviation."""
    return (len(w) >= 2 and "'" not in w and '"' not in w
            and lex.freq.get(w, 0) > lex.cfg.rare_max)


def candidates(w, lex):
    """Common words one confusable substitution away from `w`, sorted."""
    out = set()
    for i, a in enumerate(w):
        for b in _SUBS.get(a, ()):
            x = w[:i] + b + w[i + 1:]
            if lex.is_common(x):
                out.add(x)
    out.discard(w)
    return sorted(out)


def judged(book, lex, flagged, editorial_adjacent, is_foreign_line,
           covered=frozenset(), name_triggers=frozenset()):
    """Every word occurrence of `book` this module judges.

    Yields ``(unit, ref, text, match, prev, word, next, candidates)``. Lines
    the locate pass skips as foreign are skipped here too, and so are words
    already explained by another rule (`flagged`, or ``(unit, word)`` in
    `covered`: the tokens of an extra-space join or of an OCR-profile hit on
    that line), words next to editorial marks, and words one or two words
    after a title in `name_triggers` (a name, as in the locate pass).
    """
    memo = {}
    for unit, ref, content in book.lines:
        text = clean(content)
        toks = list(TOKEN_RE.finditer(text))
        words = [m.group() for m in toks]
        if is_foreign_line(words, lex.freq, lex.cfg):
            continue
        for k, w in enumerate(words):
            if w in flagged or not checked(w, lex) \
                    or (unit, w) in covered:
                continue
            if (k >= 1 and words[k - 1] in name_triggers) \
                    or (k >= 2 and words[k - 2] in name_triggers):
                continue
            cs = memo.get(w)
            if cs is None:
                cs = memo[w] = candidates(w, lex)
            if not cs:
                continue
            m = toks[k]
            if editorial_adjacent(text, m.start(), m.end()):
                continue
            prev = words[k - 1] if k else ''
            nxt = words[k + 1] if k + 1 < len(words) else ''
            yield unit, ref, text, m, prev, w, nxt, cs


def needs(items):
    """The (bigram, trigram) sets the judged occurrences `items` need."""
    n2, n3 = set(), set()
    for _, _, _, _, prev, w, nxt, cs in items:
        for x in [w] + cs:
            if prev:
                n2.add((prev, x))
            if nxt:
                n2.add((x, nxt))
            if prev and nxt:
                n3.add((prev, x, nxt))
    return n2, n3


def count_ngrams(toks, n2, first2, n3, first3, c2, c3):
    """Count the wanted bigrams and trigrams of one token list.

    Every wanted trigram starts with a wanted bigram (:func:`needs`), so a
    trigram is only looked up where its first two words were wanted."""
    n = len(toks)
    for i in range(n - 1):
        t = toks[i]
        if t not in first2:
            continue
        p = (t, toks[i + 1])
        if p not in n2:
            continue
        c2[p] += 1
        if t in first3 and i + 2 < n:
            q = (t, toks[i + 1], toks[i + 2])
            if q in n3:
                c3[q] += 1


# ---------------------------------------------------------------------------
# the counts of one book
# ---------------------------------------------------------------------------

class Counts:
    """Corpus counts of the n-grams one book asked for.

    ``need2`` / ``need3`` are the n-grams that were counted; a missing count
    is a real zero only for those.
    """

    def __init__(self, c2, c3, need2, need3, meta=None):
        self.c2, self.c3 = dict(c2), dict(c3)
        self.need2, self.need3 = set(need2), set(need3)
        self.meta = dict(meta or {})

    def bi(self, a, b):
        return self.c2.get((a, b), 0)

    def tri(self, a, b, c):
        return self.c3.get((a, b, c), 0)

    def covers(self, prev, nxt, xs):
        """Were all n-grams of `xs` between `prev` and `nxt` counted?"""
        for x in xs:
            if prev and (prev, x) not in self.need2:
                return False
            if nxt and (x, nxt) not in self.need2:
                return False
            if prev and nxt and (prev, x, nxt) not in self.need3:
                return False
        return True


def lexicon_stamp(out_dir):
    """Identity of the lexicon file the counts were made against."""
    from .core import LEXICON_F
    try:
        st = os.stat(os.path.join(out_dir, LEXICON_F))
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def counts_path(out_dir, doc):
    key = hashlib.sha1(str(doc).encode('utf-8')).hexdigest()[:20]
    return os.path.join(out_dir, COUNTS_DIR, key + '.pkl')


def save_counts(out_dir, doc, counts):
    from .core import _dump_pickle, _replace_atomically
    path = counts_path(out_dir, doc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    obj = {'format': FORMAT, 'doc': str(doc),
           'lexicon': lexicon_stamp(out_dir), 'meta': counts.meta,
           'c2': counts.c2, 'c3': counts.c3,
           'need2': counts.need2, 'need3': counts.need3}
    _replace_atomically(path, _dump_pickle(obj))
    return path


def load_counts(out_dir, doc):
    """``(counts, None)``, or ``(None, reason)`` when there are none to use:
    ``'none'`` (never counted), ``'lexicon'`` (counted against another
    lexicon) or ``'unreadable'``."""
    path = counts_path(out_dir, doc)
    if not os.path.isfile(path):
        return None, 'none'
    try:
        with open(path, 'rb') as f:
            obj = pickle.load(f)
    except Exception:
        return None, 'unreadable'
    if not isinstance(obj, dict) or obj.get('format') != FORMAT \
            or obj.get('doc') != str(doc):
        return None, 'unreadable'
    if obj.get('lexicon') != lexicon_stamp(out_dir):
        return None, 'lexicon'
    try:
        return Counts(obj['c2'], obj['c3'], obj['need2'], obj['need3'],
                      obj.get('meta')), None
    except (KeyError, TypeError, ValueError):
        return None, 'unreadable'


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def _lse(xs):
    mx = max(xs)
    return mx + math.log(sum(math.exp(x - mx) for x in xs))


def posterior(lex, counts, prev, w, nxt, cands, own=None):
    """``(words, log scores)``: `w` first, then `cands`; channel included.

    `own` -- the book's own word counts, left out of the corpus frequencies
    as the n-gram counts leave out the book's rows (None: the book is not
    part of the corpus). The total ``N`` keeps them; one book is a
    negligible share of it."""
    xs = [w] + list(cands)
    freq, N = lex.freq, lex.N or 1
    own = own or {}

    def f(x):
        return max(freq.get(x, 0) - own.get(x, 0), 0)

    def uni(x):
        return (f(x) + 0.5) / N
    lms = []
    for x in xs:
        if prev:
            s = math.log((counts.bi(prev, x) + MU2 * uni(x))
                         / (f(prev) + MU2))
        else:
            s = math.log(uni(x))
        if nxt:
            s += math.log((counts.bi(x, nxt) + MU2 * uni(nxt))
                          / (f(x) + MU2))
        lms.append(s)
    if prev and nxt:
        cs = [counts.tri(prev, x, nxt) for x in xs]
        tot = sum(cs)
        if tot:
            z = _lse(lms)
            lms = [math.log((c + MU3 * math.exp(s - z)) / (tot + MU3))
                   for c, s in zip(cs, lms)]
    return xs, [s + (0.0 if i == 0 else CH_SUB) for i, s in enumerate(lms)]


def check_config(cfg):
    """The name of the first invalid real-word setting, or None."""
    try:
        if not math.isfinite(float(cfg.real_word_odds)):
            return 'real_word_odds'
    except (TypeError, ValueError):
        return 'real_word_odds'
    try:
        if int(cfg.real_word_rep) < 1:
            return 'real_word_rep'
    except (TypeError, ValueError):
        return 'real_word_rep'
    return None


def judge(lex, counts, prev, w, nxt, cands, present, cfg, own=None):
    """The verdict on one occurrence: a dict with ``state`` — ``'stale'``
    (its n-grams were not counted), ``'convention'`` (the book's own
    spelling: used at least ``real_word_rep`` times and more often than the
    suggestion), ``'hint'`` or ``'ok'`` — plus ``sugg`` and ``odds`` (log10
    odds that `w` is wrong) when it was judged. `own`: see
    :func:`posterior`."""
    if not counts.covers(prev, nxt, [w] + cands):
        return {'state': 'stale'}
    xs, sc = posterior(lex, counts, prev, w, nxt, cands, own)
    j = max(range(1, len(xs)), key=lambda i: sc[i])
    odds = (_lse(sc[1:]) - sc[0]) / _LN10
    out = {'sugg': xs[j], 'odds': odds, 'state': 'ok'}
    if odds >= cfg.real_word_odds:
        rep = present.get(w, 0)
        if rep >= max(1, cfg.real_word_rep) and present.get(xs[j], 0) < rep:
            out['state'] = 'convention'
        else:
            out['state'] = 'hint'
    return out


def score(odds):
    """The finding's score: half the log10 odds, capped at 5."""
    return round(min(odds, 10.0) / 2, 3)
