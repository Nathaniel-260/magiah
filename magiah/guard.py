# -*- coding: utf-8 -*-
"""Guard: check a correction against the lexicon, after the fact.

Magiah finds suspicious words; something else -- a person, an OCR
post-corrector, a language model -- then changes the text. A corrector can
also break a good word: it "fixes" a word the corpus knows well into one it
has hardly ever seen. :func:`compare` lines up the text before and after the
correction word by word and judges every change by the corpus frequencies:

* ``suspect``   -- a common word (>= ``Config.common_min``) became a rare or
  unknown one (<= ``Config.rare_max``): the correction probably broke it;
* ``supported`` -- the reverse: a rare word became a common one;
* ``neutral``   -- anything else (the lexicon has no opinion).

A split, a join, an insertion or a deletion is judged as one unit, by the
least frequent word on each side (``אמר ו`` -> ``אמרו``: min(f(אמר), f(ו))
against f(אמרו)). A word that was deleted outright has no new side and is
never ``suspect``. Inside such a unit, words that clearly correspond (the
same word with a letter or two changed: similar spelling and length) are
first paired off and judged one by one, so a real single-word break is not
hidden by a neighbouring split or join; only the words left over form
units.

Nothing else in Magiah calls this: it runs only through ``magiah guard`` or
an explicit call, and changes no scan, finding or score.
"""
import difflib
import os
import pickle
from collections import Counter

from .normalize import tokenize

SUSPECT, SUPPORTED, NEUTRAL = 'suspect', 'supported', 'neutral'

# difflib's cost grows with the number of equal-word pairs (one word that
# repeats n times on both sides costs n*n). After the common beginning and
# end are cut off, more pairs than this are refused instead of hanging.
MAX_PAIRS = 60_000_000
# a unit is split by similarity only up to this many words per side
MAX_ALIGN = 40
PAIR_RATIO = 0.6        # spelling similarity of two words that correspond
PAIR_LEN = 1.5          # ... and the longer may be at most this much longer


class GuardError(Exception):
    """A reason (in Hebrew) why ``magiah guard`` cannot run."""


def _tokens_by_line(text):
    """``[(token, line_number)]`` in reading order (line numbers from 1)."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        out.extend((t, n) for t in tokenize(line))
    return out


def _least(freq, words):
    return min((freq.get(w, 0) for w in words), default=0)


def _alike(x, y):
    """How alike two different words are (0 = not a pair): one word with a
    small change."""
    if x == y or not x or not y:
        return 0.0
    if max(len(x), len(y)) > PAIR_LEN * min(len(x), len(y)):
        return 0.0
    r = difflib.SequenceMatcher(a=x, b=y, autojunk=False).ratio()
    return r if r >= PAIR_RATIO else 0.0


def _pairs(old, new):
    """Monotone pairing of the words of `old` and `new` that correspond:
    ``[(i, j)]``, maximizing total spelling similarity."""
    n, m = len(old), len(new)
    if not n or not m or n > MAX_ALIGN or m > MAX_ALIGN:
        return []
    sim = [[_alike(x, y) for y in new] for x in old]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = best[i + 1][j + 1] + sim[i][j] if sim[i][j] else 0.0
            best[i][j] = max(best[i + 1][j], best[i][j + 1], take)
    out, i, j = [], 0, 0
    while i < n and j < m:
        if sim[i][j] and best[i][j] == best[i + 1][j + 1] + sim[i][j]:
            out.append((i, j))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return out


def _opcodes(wa, wb):
    """difflib opcodes, with the common beginning and end cut off first
    (cheap, and the result is the same); refuses an input difflib would
    take forever on."""
    lo = 0
    top = min(len(wa), len(wb))
    while lo < top and wa[lo] == wb[lo]:
        lo += 1
    ea, eb = len(wa), len(wb)
    while ea > lo and eb > lo and wa[ea - 1] == wb[eb - 1]:
        ea, eb = ea - 1, eb - 1
    mid_a, mid_b = wa[lo:ea], wb[lo:eb]
    if mid_a and mid_b:
        ca, cb = Counter(mid_a), Counter(mid_b)
        pairs = sum(n * cb[w] for w, n in ca.items() if w in cb)
        if pairs > MAX_PAIRS:
            raise GuardError(
                'הטקסטים שונים זה מזה בקטע ארוך וחוזר על עצמו, והשוואה '
                'מילה-מילה שלהם תימשך זמן בלתי סביר. יש לפצל את הקבצים '
                'לחלקים קטנים יותר ולהריץ את הבדיקה על כל חלק.')
    ops = []
    if lo:
        ops.append(('equal', 0, lo, 0, lo))
    sm = difflib.SequenceMatcher(a=mid_a, b=mid_b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        ops.append((op, i1 + lo, i2 + lo, j1 + lo, j2 + lo))
    if ea < len(wa):
        ops.append(('equal', ea, len(wa), eb, len(wb)))
    return ops


def judge(f_old, f_new, has_new, cfg):
    """The verdict for one change, from the two sides' frequencies."""
    if has_new and f_new <= cfg.rare_max and f_old >= cfg.common_min:
        return SUSPECT
    if has_new and f_new >= cfg.common_min and f_old <= cfg.rare_max:
        return SUPPORTED
    return NEUTRAL


def compare(freq, before, after, cfg):
    """Every word-level change between ``before`` and ``after``, judged.

    ``freq`` is the lexicon (word -> corpus count, as in lexicon.pkl).
    Returns a list of dicts in reading order::

        {'line': 3, 'old': 'שמר', 'new': 'שמד', 'f_old': 912, 'f_new': 1,
         'verdict': 'suspect'}

    ``line`` is the line in ``before`` where the change starts (for a pure
    insertion: the line of the word that comes next). Changes of equal
    length are paired word by word; in any other change, words that clearly
    correspond are paired and what is left forms units.
    """
    a = _tokens_by_line(before)
    b = _tokens_by_line(after)
    wa, wb = [t for t, _ in a], [t for t, _ in b]
    out = []
    for op, i1, i2, j1, j2 in _opcodes(wa, wb):
        if op == 'equal':
            continue
        old, new = wa[i1:i2], wb[j1:j2]
        if len(old) == len(new):
            units = [(i1 + k, [o], [n])
                     for k, (o, n) in enumerate(zip(old, new))]
        else:
            units = []
            ci = cj = 0                 # the part not yet turned into units
            for i, j in _pairs(old, new) + [(len(old), len(new))]:
                if (ci, cj) != (i, j):
                    units.append((i1 + ci, old[ci:i], new[cj:j]))
                if i < len(old):
                    units.append((i1 + i, [old[i]], [new[j]]))
                ci, cj = i + 1, j + 1
        for k, o, n in units:
            if k < len(a):
                ln = a[k][1]
            else:
                ln = a[-1][1] if a else 1
            fo, fn = _least(freq, o), _least(freq, n)
            out.append({'line': ln, 'old': ' '.join(o), 'new': ' '.join(n),
                        'f_old': fo, 'f_new': fn,
                        'verdict': judge(fo, fn, bool(n), cfg)})
    return out


def read_text(path):
    """A text file as Magiah reads one: UTF-8 (BOM allowed), else cp1255."""
    with open(path, 'rb') as f:
        data = f.read()
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1255')


def format_rows(rows, show_all=False):
    """Tab-separated lines for the command line: line, verdict, old, new,
    and the two frequencies. Only ``suspect`` changes unless ``show_all``."""
    out = []
    for r in rows:
        if show_all or r['verdict'] == SUSPECT:
            out.append('\t'.join((str(r['line']), r['verdict'], r['old'],
                                  r['new'], str(r['f_old']),
                                  str(r['f_new']))))
    return out


def summary(rows):
    counts = {SUSPECT: 0, SUPPORTED: 0, NEUTRAL: 0}
    for r in rows:
        counts[r['verdict']] += 1
    return counts


def load_freq(out_dir, allow_unread=0):
    """The lexicon of `out_dir`, or GuardError (Hebrew) saying why not."""
    from . import core
    path = os.path.join(out_dir, core.LEXICON_F)
    if not os.path.isfile(path):
        raise GuardError(
            f'לא נמצא מילון ({core.LEXICON_F}) בתיקייה {out_dir}.\n'
            'הבדיקה משווה את התיקון למילון קיים: יש להריץ קודם '
            '‎magiah lexicon‎ (או ‎magiah all‎) על אותה תיקיית --out, '
            'או לציין ב---out תיקייה שבה כבר נבנה מילון.')
    if os.path.getsize(path) == 0:
        raise GuardError(
            f'קובץ המילון ({path}) ריק.\nיש לבנות את המילון מחדש: '
            '‎magiah lexicon‎ על אותה תיקיית --out.')
    problem = core.coverage_problem(out_dir, 'lexicon', allow_unread)
    if problem:
        raise GuardError(problem)
    try:
        with open(path, 'rb') as f:
            freq = pickle.load(f)
        if not isinstance(freq, dict):
            raise ValueError('not a dict')
    except (pickle.UnpicklingError, EOFError, ValueError, AttributeError,
            ImportError, IndexError, TypeError) as e:
        raise GuardError(
            f'קובץ המילון ({path}) פגום ואי אפשר לקרוא אותו '
            f'({type(e).__name__}).\nיש לבנות את המילון מחדש: '
            '‎magiah lexicon‎ על אותה תיקיית --out.') from e
    return freq


def run(out_dir, before_path, after_path, cfg, show_all=False,
        load_lexicon=None):
    """``magiah guard``: print the judged changes, return the counts."""
    if load_lexicon is None:
        load_lexicon = load_freq
    freq = load_lexicon(out_dir, cfg.allow_unread)
    rows = compare(freq, read_text(before_path), read_text(after_path), cfg)
    for line in format_rows(rows, show_all):
        print(line, flush=True)
    counts = summary(rows)
    print(f'[guard] {os.path.basename(after_path)}: {len(rows):,} changes -- '
          f'{counts[SUSPECT]:,} suspect, {counts[SUPPORTED]:,} supported, '
          f'{counts[NEUTRAL]:,} neutral', flush=True)
    return counts
