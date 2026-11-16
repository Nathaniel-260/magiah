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
never ``suspect``.

Nothing else in Magiah calls this: it runs only through ``magiah guard`` or
an explicit call, and changes no scan, finding or score.
"""
import difflib
import os

from .normalize import tokenize

SUSPECT, SUPPORTED, NEUTRAL = 'suspect', 'supported', 'neutral'


def _tokens_by_line(text):
    """``[(token, line_number)]`` in reading order (line numbers from 1)."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        out.extend((t, n) for t in tokenize(line))
    return out


def _least(freq, words):
    return min((freq.get(w, 0) for w in words), default=0)


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
    insertion: the line of the word it follows). Changes of equal length
    are paired word by word; any other change is one unit.
    """
    a = _tokens_by_line(before)
    b = _tokens_by_line(after)
    wa, wb = [t for t, _ in a], [t for t, _ in b]
    out = []
    sm = difflib.SequenceMatcher(a=wa, b=wb, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            continue
        old, new = wa[i1:i2], wb[j1:j2]
        if i1 < len(a):
            line = a[i1][1]
        else:
            line = a[-1][1] if a else 1
        if len(old) == len(new):
            units = [([o], [n]) for o, n in zip(old, new)]
            lines = [a[i1 + k][1] for k in range(len(old))]
        else:
            units = [(old, new)]
            lines = [line]
        for (o, n), ln in zip(units, lines):
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


def run(out_dir, before_path, after_path, cfg, show_all=False,
        load_lexicon=None):
    """``magiah guard``: print the judged changes, return the counts."""
    if load_lexicon is None:
        from .book_scan import load_lexicon
    freq = load_lexicon(out_dir, cfg.allow_unread)
    rows = compare(freq, read_text(before_path), read_text(after_path), cfg)
    for line in format_rows(rows, show_all):
        print(line, flush=True)
    counts = summary(rows)
    print(f'[guard] {os.path.basename(after_path)}: {len(rows):,} changes -- '
          f'{counts[SUSPECT]:,} suspect, {counts[SUPPORTED]:,} supported, '
          f'{counts[NEUTRAL]:,} neutral', flush=True)
    return counts
