# -*- coding: utf-8 -*-
"""The four-stage detection pipeline.

1. ``lexicon`` — count the frequency of every word in the corpus.
2. ``detect``  — flag suspect word types (rare words close to frequent ones);
   missing-space candidates are verified against the corpus: the split is kept
   only if the same word sequence actually occurs *with* spaces elsewhere.
3. ``locate``  — find every occurrence of a flagged word, detect extra-space
   errors, and context-verify edit-distance-1 corrections: does the corrected
   word actually appear next to the same neighboring words elsewhere?
4. ``report``  — export ranked CSV files.

Everything is derived from the corpus itself — no external dictionaries, so
Aramaic, rabbinic Hebrew and abbreviations are handled naturally.
"""
import csv
import math
import os
import pickle
import re
import sqlite3
import time
from collections import Counter
from multiprocessing import Pool

from .config import Config
from . import tanach
from .corpus import make_corpus
from .textsource import ReadStats
from .normalize import (CONFUSABLE, FINALS, FROM_FINAL, PREFIX_LETTERS,
                        SUFFIX_LETTERS, TO_FINAL, is_abbrev, tokenize)

LEXICON_F = 'lexicon.pkl'
FLAGGED_F = 'flagged.pkl'
SPLITS_F = 'split_cands.pkl'
REPORT_DB_F = 'report.db'


class StageError(Exception):
    """A stage was run before the stage that produces its input.

    Carries a ready-to-print Hebrew message; the CLI prints it and exits 1
    instead of dumping a traceback, and the UI's scan log shows it as-is.
    """


# Hebrew name of the stage that produces each prerequisite, for the message.
_STAGE_OF = {
    LEXICON_F: ('lexicon', 'מילון'),
    FLAGGED_F: ('detect', 'איתור'),
    SPLITS_F: ('detect', 'איתור'),
    REPORT_DB_F: ('locate', 'מיקום'),
}


def _require(out_dir, filename, stage):
    """Fail cleanly when a prerequisite of `stage` is missing.

    First-run guard: every stage except `lexicon` consumes a file produced by
    an earlier stage. Without this the user gets a raw FileNotFoundError /
    "no such table" traceback in English — and, for the report.db cases, a
    0-byte report.db left behind by sqlite3.connect() that then blocks the UI
    from starting at all.
    """
    path = os.path.join(out_dir, filename)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    need_cmd, need_he = _STAGE_OF.get(filename, ('all', 'סריקה'))
    raise StageError(
        f'לא ניתן להריץ את שלב "{stage}": חסר הקובץ {filename}, '
        f'שנוצר בשלב "{need_he}".\n'
        f'יש להריץ קודם:  python -X utf8 -m magiah {need_cmd} '
        f'--out "{out_dir}"\n'
        f'או להריץ סריקה מלאה:  python -X utf8 -m magiah all '
        f'--out "{out_dir}"')

# ---------------------------------------------------------------------------
# worker plumbing
# ---------------------------------------------------------------------------
_W = {}


def _init(spec, cfg_dict, loads):
    """Pool initializer: rebuild the corpus adapter and load shared data."""
    _W.clear()
    _W['corpus'] = make_corpus(spec)
    _W['cfg'] = Config.from_dict(cfg_dict)
    for key, path in loads.items():
        with open(path, 'rb') as f:
            _W[key] = pickle.load(f)
    if 'split_cands' in _W:
        _W['seq'] = SeqCounter(parts for _, parts in _W.pop('split_cands'))
    if 'ctx_pairs' in _W:
        _W['ctx_first'] = {a for a, _ in _W['ctx_pairs']}


def _pool(spec, cfg, loads=None):
    return Pool(cfg.workers, initializer=_init,
                initargs=(spec, cfg.to_dict(), loads or {}))


def _chunk_stats_begin():
    """Fresh per-chunk read counters on the worker's corpus adapter."""
    _W['corpus'].stats = ReadStats()


def _chunk_stats_end():
    return _W['corpus'].stats.to_dict()


COVERAGE_F = 'coverage_{stage}.json'


class PartialRead(StageError):
    """Rows of the input could not be read; the stage output is partial."""


def _write_coverage(out_dir, stage, stats, extra=None):
    """Persist what a stage actually read (the run's coverage evidence)."""
    import json
    info = {'stage': stage, 'complete': stats.decode_errors == 0,
            **stats.to_dict(), **(extra or {})}
    with open(os.path.join(out_dir, COVERAGE_F.format(stage=stage)), 'w',
              encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(f'[{stage}] coverage: lines={stats.lines:,} chars={stats.chars:,} '
          f'decode_errors={stats.decode_errors:,} '
          f'version_lines_skipped={stats.version_lines_skipped:,}',
          flush=True)
    return info


def _fail_if_partial(stage, stats, out_dir):
    """A pass with unreadable rows must never be reported as complete."""
    if stats.decode_errors:
        raise PartialRead(
            f'שלב "{stage}" לא הצליח לקרוא {stats.decode_errors:,} שורות '
            f'מהקלט, ולכן התוצאה חלקית ואינה מוצגת כהצלחה.' + chr(10) +
            f'פרטים: {os.path.join(out_dir, COVERAGE_F.format(stage=stage))}')


# ---------------------------------------------------------------------------
# stage 1: lexicon
# ---------------------------------------------------------------------------

def _count_chunk(chunk):
    _chunk_stats_begin()
    c = Counter()
    for _, text in _W['corpus'].iter_texts(chunk):
        c.update(tokenize(text))
    return c, _chunk_stats_end()


def build_lexicon(spec, cfg, out_dir):
    t0 = time.time()
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)
    lex = Counter()
    stats = ReadStats()
    with _pool(spec, cfg) as pool:
        for i, (c, st) in enumerate(
                pool.imap_unordered(_count_chunk, chunks), 1):
            lex.update(c)
            stats.add(ReadStats.from_dict(st))
            print(f'  [lexicon] chunk {i}/{len(chunks)}  types={len(lex):,}  '
                  f'({time.time()-t0:.0f}s)', flush=True)
    with open(os.path.join(out_dir, LEXICON_F), 'wb') as f:
        pickle.dump(dict(lex), f, protocol=4)
    print(f'[lexicon] tokens={sum(lex.values()):,}  types={len(lex):,}  '
          f'time={time.time()-t0:.0f}s', flush=True)
    _write_coverage(out_dir, 'lexicon', stats,
                    {'tokens': sum(lex.values()), 'types': len(lex),
                     'chunks': len(chunks)})
    _fail_if_partial('lexicon', stats, out_dir)


# ---------------------------------------------------------------------------
# stage 2: type-level detection
# ---------------------------------------------------------------------------

def _dld1(a, b):
    """Damerau-Levenshtein distance-1 check.
    Returns (kind, ch_a, ch_b, pos) or None; pos is the edit position in the
    longer string (for sub/swap: in either)."""
    la, lb = len(a), len(b)
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            i = diff[0]
            return ('sub', a[i], b[i], i)
        if (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]):
            return ('swap', a[diff[0]], a[diff[1]], diff[0])
        return None
    if abs(la - lb) != 1:
        return None
    kind = 'ins' if la > lb else 'del'   # relative to a: extra / missing letter
    long, short = (a, b) if la > lb else (b, a)
    for i in range(len(short)):
        if long[i] != short[i]:
            return (kind, long[i], None, i) if long[i + 1:] == short[i:] else None
    return (kind, long[-1], None, len(long) - 1)


# Possessive/plural suffixes, longest first. A rare word that is a frequent
# stem plus one of these is legitimate inflection (שמחזקם = שמחזק+ם), not a
# typo.
_SUFFIXES = ('כם', 'כן', 'הם', 'הן', 'נו', 'יו', 'יה', 'ות', 'ים', 'ין',
             'ם', 'ן', 'ו', 'ה', 'י', 'ך')


def _frequent_stem(w, freq, thresh):
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            stem = w[:-len(suf)]
            if freq.get(stem, 0) >= thresh:
                return True
            if stem[-1] in TO_FINAL and \
                    freq.get(stem[:-1] + TO_FINAL[stem[-1]], 0) >= thresh:
                return True
    return False


# ---------------------------------------------------------------------------
# shared per-word rules: core.detect and book_scan call exactly these, so the
# two paths cannot drift. Only the edit-1 candidate SOURCE differs (a deletion
# index for the whole lexicon vs direct probing for one book); both feed the
# same evaluation and are tested to give identical results.
# ---------------------------------------------------------------------------

HEB_LETTERS = 'אבגדהוזחטיכלמנסעפצקרשתךםןףץ'

# error types whose suggestion is a single word the book itself may also use
LOCAL_TYPES = ('edit1_sub', 'edit1_ins', 'edit1_del', 'edit1_swap',
               'nonfinal_end', 'spelling_variant', 'lost_quotes')

CALIBRATION_META_F = 'calibration_meta.json'
LEARNED_F = 'confusion_learned.json'
OCR_PROFILES_F = 'ocr_profiles.pkl'
SPLIT_ALTS_F = 'split_alts.pkl'

# What actually supports a finding (report column `evidence`). Book-scope
# kinds are deliberately distinct from corpus-wide ones.
EVIDENCE_KINDS = ('tanach', 'context', 'context_book', 'book_local',
                  'split_observed', 'split_observed_book', 'structural',
                  'ocr_profile', 'suspicion', 'none')
STRUCTURAL_TYPES = ('nonfinal_end', 'final_midword', 'lost_quotes')


class Lexicon:
    """The global lexicon plus the derived lookups every rule needs."""

    def __init__(self, freq, cfg, learned=None):
        self.freq, self.cfg = freq, cfg
        self.learned = learned or {}
        self.N = sum(freq.values())
        # abbreviations that lost their gershayim: רמבם -> רמב"ם
        self.abbrev_map = {}
        for a, fa in freq.items():
            if fa >= cfg.common_min and is_abbrev(a):
                k = a.replace('"', '').replace("'", '')
                if len(k) >= 2 and (k not in self.abbrev_map
                                    or freq[self.abbrev_map[k]] < fa):
                    self.abbrev_map[k] = a

    def is_common(self, w):
        return (self.freq.get(w, 0) >= self.cfg.common_min
                and not is_abbrev(w) and 2 <= len(w) <= 18)

    def expected(self, parts):
        """Expected spaced count of `parts` if the words were independent."""
        N = self.N or 1
        return N * math.prod(self.freq.get(p, 0) / N for p in parts)


class Edit1Index:
    """Edit-1 candidates via a SymSpell deletion index over the common
    words: built once, then queried for every rare type of the lexicon."""

    def __init__(self, lex):
        self.lex = lex
        self.del_index = {}
        for w in lex.freq:
            if lex.is_common(w):
                for i in range(len(w)):
                    self.del_index.setdefault(w[:i] + w[i + 1:], []).append(w)

    def __call__(self, w):
        cands = set(self.del_index.get(w, ()))
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            cands.update(self.del_index.get(d, ()))
            if self.lex.is_common(d):
                cands.add(d)
        return cands


def edit1_probe(lex):
    """Edit-1 candidates by probing the lexicon directly — no index; cheaper
    when only a single book's types are asked about."""
    is_common = lex.is_common

    def cands(w):
        out = set()
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            if is_common(d):
                out.add(d)
            for ch in HEB_LETTERS:
                if ch != w[i]:
                    sub = w[:i] + ch + w[i + 1:]
                    if is_common(sub):
                        out.add(sub)
                ins = w[:i] + ch + w[i:]
                if is_common(ins):
                    out.add(ins)
            if i + 1 < len(w) and w[i] != w[i + 1]:
                sw = w[:i] + w[i + 1] + w[i] + w[i + 2:]
                if is_common(sw):
                    out.add(sw)
        for ch in HEB_LETTERS:
            if is_common(w + ch):
                out.add(w + ch)
        return out
    return cands


def segmentations(w, freq, cfg):
    """Plausible missing-space splits of `w`, each still to be verified.

    Every 2-part split whose parts are frequent, plus the best few 3-part
    ones. A single "best" split chosen by part frequency alone misses the
    real one when a shorter, more frequent prefix exists (בכ|למקום vs
    בכל|מקום), so the choice is left to the spaced-sequence evidence.
    """
    n, mp = len(w), cfg.max_part

    def ok(p):
        return 2 <= len(p) <= mp and freq.get(p, 0) >= cfg.part_min

    out = [(w[:i], w[i:]) for i in range(2, n - 1)
           if ok(w[:i]) and ok(w[i:])]
    if cfg.max_parts >= 3 and n <= 3 * mp:
        three = []
        for i in range(2, n - 3):
            if not ok(w[:i]):
                continue
            for j in range(i + 2, n - 1):
                if ok(w[i:j]) and ok(w[j:]):
                    three.append((w[:i], w[i:j], w[j:]))
        three.sort(key=lambda p: (-min(freq[x] for x in p), p))
        out.extend(three[:cfg.split_alts_3])
    return out


def learned_bonus(count):
    """Score bonus of a substitution learned `count` times by calibration."""
    return min(2.5, 0.5 + 0.8 * math.log10(1 + count))


def _frequency_penalty(fw, cfg):
    """Structural rules also run above the rarity cutoff; a more frequent
    spelling is less likely to be a typo, so it ranks lower."""
    rm = max(cfg.rare_max, 1)
    return math.log10(fw / rm) if fw > rm else 0.0


def evaluate_word(w, fw, lex, edit1_cands):
    """Every per-word rule for one word type.

    Returns ``(records, splits)``: records are ``(errtype, suggestion,
    sugg_freq, score)``; splits are ``(parts, strong)`` candidates that still
    need spaced-sequence evidence. `fw` may be 0 (a book word unknown to the
    lexicon), so ratios use ``max(fw, 1)``.
    """
    cfg, freq = lex.cfg, lex.freq
    recs, splits = [], []
    # geresh/gershayim mark abbreviations, which follow their own spelling
    if len(w) < 3 or is_abbrev(w):
        return recs, splits
    fw1 = max(fw, 1)

    # structural: non-final letter at word end
    if w[-1] in TO_FINAL and fw <= cfg.nonfinal_max:
        wf = w[:-1] + TO_FINAL[w[-1]]
        ff = freq.get(wf, 0)
        if ff >= cfg.part_min and ff >= 50 * fw1:
            recs.append(('nonfinal_end', wf, ff, 4 + math.log10(ff / fw1)))

    # structural: final-form letter mid-word — checked above the rarity
    # cutoff too (up to struct_max); a frequent form is an established one
    mid_final = [i for i, ch in enumerate(w[:-1]) if ch in FINALS]
    if mid_final and fw <= max(cfg.struct_max, cfg.rare_max):
        for i in mid_final:              # perhaps a missing space there
            a, b = w[:i + 1], w[i + 1:]
            if freq.get(a, 0) >= 10 and freq.get(b, 0) >= 10 and len(b) >= 2:
                splits.append(((a, b), True))
        if not splits:                   # perhaps final form for regular
            w2 = ''.join(FROM_FINAL.get(ch, ch) if i < len(w) - 1 else ch
                         for i, ch in enumerate(w))
            f2 = freq.get(w2, 0)
            pen = _frequency_penalty(fw, cfg)
            if f2 >= cfg.common_min:
                recs.append(('final_midword', w2, f2,
                             5 + math.log10(f2) - pen))
            else:
                # a suspicion only: no corpus word to propose
                recs.append(('final_midword', '', 0, 3.0 - pen))

    if fw > cfg.rare_max:
        return recs, splits

    # missing space — every plausible segmentation, verified later
    if len(w) >= 5 and not splits:
        for parts in segmentations(w, freq, cfg):
            if lex.expected(parts) >= cfg.exp_prefilter:
                splits.append((parts, False))

    # abbreviation that lost its gershayim (רמבם -> רמב"ם)
    ab = lex.abbrev_map.get(w)
    if ab and freq[ab] >= cfg.ed1_ratio * fw1:
        recs.append(('lost_quotes', ab, freq[ab],
                     5 + math.log10(freq[ab] / fw1)))

    # frequent stem + inflection suffix: legitimate morphology
    if _frequent_stem(w, freq, cfg.common_min):
        return recs, splits

    # edit distance 1 from a frequent word (sorted: ties break the same way
    # whichever candidate source produced the set)
    best_c, best_score, best_kind, best_ch = None, 0.0, '', ''
    for c in sorted(edit1_cands(w)):
        if c == w or not lex.is_common(c):
            continue
        fc = freq[c]
        if fc < cfg.ed1_ratio * fw1:
            continue
        r = _dld1(w, c)
        if r is None:
            continue
        kind, ch_a, ch_b, pos = r
        if kind == 'ins':
            # a doubled letter (למללך, נגמרר) is always suspicious
            doubled = ((pos > 0 and w[pos - 1] == ch_a)
                       or (pos + 1 < len(w) and w[pos + 1] == ch_a))
            # extra letter in the leading prefix cluster (דלאליעזר) or a
            # trailing inflection suffix (מזדעזעה): morphology, not a typo
            if (not doubled and pos <= 2 and ch_a in PREFIX_LETTERS
                    and all(x in PREFIX_LETTERS for x in w[:pos])):
                continue
            if not doubled and pos == len(w) - 1 and ch_a in SUFFIX_LETTERS:
                continue
        elif kind == 'sub' and pos == 0 \
                and ch_a in PREFIX_LETTERS and ch_b in PREFIX_LETTERS:
            # Hebrew vs Aramaic prefix variation (אידועים = אַ+ידועים)
            continue
        score = math.log10(fc / fw1)
        if kind == 'sub':
            lc = lex.learned.get(ch_a + ch_b, 0)
            if lc:
                score += learned_bonus(lc)
            elif not lex.learned and (ch_a, ch_b) in CONFUSABLE:
                score += 2
        elif kind in ('ins', 'del') and ch_a in 'וי':
            score += 1
        elif kind == 'swap':
            score += 1
        if score > best_score:
            best_c, best_score, best_kind, best_ch = c, score, kind, ch_a
    if best_c:
        # extra/missing ו or י is ktiv male/chaser variation: own class
        if best_kind in ('ins', 'del') and best_ch in 'וי':
            errtype = 'spelling_variant'
        else:
            errtype = f'edit1_{best_kind}'
        recs.append((errtype, best_c, freq[best_c], best_score))
    return recs, splits


def record_best(errors, w, fw, rec):
    """Keep the highest-scoring explanation per word."""
    errtype, sugg, fs, score = rec
    cur = errors.get(w)
    if cur is None or score > cur[4]:
        errors[w] = (fw, errtype, sugg, fs, score)


class SeqCounter:
    """Counts CONTIGUOUS occurrences of word sequences (split candidates)
    in token lists; shared by the corpus pass and the book scan."""

    def __init__(self, seqs):
        self.seqs = list(seqs)
        self.first, self.pairs = set(), {}
        for idx, parts in enumerate(self.seqs):
            self.first.add(parts[0])
            self.pairs.setdefault((parts[0], parts[1]), []).append(idx)

    def count(self, toks, counts):
        first, pairs, seqs = self.first, self.pairs, self.seqs
        n = len(toks)
        for i in range(n - 1):
            if toks[i] in first:
                for idx in pairs.get((toks[i], toks[i + 1]), ()):
                    parts = seqs[idx]
                    if i + len(parts) <= n and all(
                            toks[i + k] == parts[k]
                            for k in range(2, len(parts))):
                        counts[idx] = counts.get(idx, 0) + 1


def _split_score(strong, obs, exp, minlen, cfg, scope):
    """Score of a split given its spaced evidence, or None if rejected.

    'corpus': the full pipeline's rule. 'book': only one book was searched,
    so an observation there is capped below the corpus band, and an
    unobserved split may stand on part frequencies alone (low score).
    """
    if scope == 'corpus':
        if obs <= 0:
            return None
        # a final-form letter mid-word proves the break; otherwise the
        # spaced sequence must occur at least as often as chance predicts
        if strong:
            ok = obs >= 1
        elif obs < exp:
            ok = False
        elif minlen >= 3:
            ok = obs >= cfg.split_obs_min
        else:
            ok = obs >= cfg.split_obs_min_short and obs >= 2 * exp
        if not ok:
            return None
        return (4 + math.log10(obs) + (2 if strong else 0)
                + (1 if minlen >= 3 else 0))
    if obs:
        return 3.5 + min(1.0, math.log10(obs + 1)) \
            + (1.0 if strong else 0.0) + (0.5 if minlen >= 3 else 0.0)
    if strong:
        return 3.5          # a final-form letter mid-word proves the break
    if minlen >= 3 and exp >= 1:
        return 2.5
    return None


def resolve_splits(cands, observed, lex, scope):
    """Choose one word's segmentation by evidence.

    `cands` are ``(parts, strong)`` and `observed` their spaced counts.
    Returns ``(best, alts)``: best is ``(parts, obs, score, strong)`` or None
    — the accepted candidate with the highest score plus association (how
    far the spaced count exceeds chance, log10(obs/exp) capped at 3; corpus
    scope only, a book's count is not comparable with corpus expectation),
    then the fewest parts; alts lists every candidate as
    ``(parts, obs, exp, accepted)``.
    """
    best, best_key, alts = None, None, []
    for (parts, strong), obs in zip(cands, observed):
        exp = lex.expected(parts)
        score = _split_score(strong, obs, exp, min(len(p) for p in parts),
                             lex.cfg, scope)
        alts.append((parts, obs, exp, score is not None))
        if score is not None:
            key = (score + (_association(obs, exp) if scope == 'corpus'
                            else 0.0), -len(parts))
            if best_key is None or key > best_key:
                best, best_key = (parts, obs, score, strong), key
    return best, alts


def _association(obs, exp):
    if obs <= 0:
        return 0.0
    if exp <= 0:
        return 3.0
    return max(0.0, min(3.0, math.log10(obs / exp)))


def settle_split(errors, w, fw, best, lex, scope, strong=None):
    """Record a confirmed split, unless it would displace a correction.

    A word may also have an edit-1 style correction whose support (context,
    book) is only measured later. A split replaces such a correction only on
    strong evidence: a final-form letter break, or enough observations far
    above chance (corpus scope; in a book, enough in-book observations).
    Returns 'primary' (the split is the word's finding) or 'alternative'.
    """
    parts, obs, score = best[0], best[1], best[2]
    if strong is None:
        strong = best[3] if len(best) > 3 else False
    rec = ('missing_space', ' '.join(parts), obs, score)
    cur = errors.get(w)
    if cur is not None and (cur[1] in LOCAL_TYPES or uses_context(cur[1])):
        cfg = lex.cfg
        if scope == 'corpus':
            decisive = strong or (
                obs >= cfg.split_obs_min
                and obs >= cfg.split_override_ratio * lex.expected(parts))
        else:
            decisive = strong or obs >= cfg.split_obs_min
        if not decisive:
            return 'alternative'
        errors[w] = (fw, *rec)
        return 'primary'
    record_best(errors, w, fw, rec)
    return 'primary' if errors[w][1] == 'missing_space' else 'alternative'


def keep_structural_suspicion(errors, w, fw, cands, lex):
    """A final-form letter mid-word is wrong whether or not the split it
    suggests is ever seen spaced; record the suspicion without inventing a
    replacement when no split could be confirmed."""
    if w in errors or not any(strong for _, strong in cands):
        return
    cfg = lex.cfg
    record_best(errors, w, fw,
                ('final_midword', '', 0, 3.0 - _frequency_penalty(fw, cfg)))



def uses_context(errtype):
    """Error types whose correction is verified by neighbouring words."""
    return errtype.startswith('edit1') or errtype == 'spelling_variant'


def evidence_kind(errtype, sugg, sugg_freq, ctx_hits, sugg_local, tanach,
                  ctx_scope='corpus', split_scope='corpus'):
    """The strongest kind of support actually found for one occurrence."""
    if tanach == 3:              # verse reading confirmed by the reference
        return 'tanach'
    if errtype == 'missing_space':
        if sugg_freq and sugg_freq > 0:     # sugg_freq = spaced observations
            return ('split_observed' if split_scope == 'corpus'
                    else 'split_observed_book')
        return 'none'
    if ctx_hits > 0:
        return 'context' if ctx_scope == 'corpus' else 'context_book'
    if sugg_local >= 3:
        return 'book_local'
    if not sugg:
        return 'suspicion'
    if errtype in STRUCTURAL_TYPES:
        return 'structural'     # a deterministic orthographic rule
    if errtype == 'ocr_profile':
        return 'ocr_profile'    # the book's (reviewed) systematic confusion
    return 'none'


def book_repeat_flag(errtype, n_located, only_this_book):
    """All of a word's (2+) occurrences sit in one book: the author's own
    spelling rather than a typo. Corpus scope: located occurrences span one
    doc; book scope: the lexicon count is fully explained by this book."""
    return 1 if (errtype in LOCAL_TYPES and n_located >= 2
                 and only_this_book) else 0


# ---------------------------------------------------------------------------
# calibration provenance: learned weights are used only when they say where
# they came from
# ---------------------------------------------------------------------------

def _file_digest(path):
    import hashlib
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def write_calibration_meta(out_dir, info):
    """Write calibration_meta.json with the hashes of the learned files it
    vouches for; a file edited or replaced afterwards no longer matches."""
    import json
    info = dict(info)
    info['hashes'] = {
        fn: _file_digest(os.path.join(out_dir, fn))
        for fn in (LEARNED_F, OCR_PROFILES_F)} \
        if info.get('source') in ('human_review', 'machine_unreviewed') \
        else {}
    with open(os.path.join(out_dir, CALIBRATION_META_F), 'w',
              encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return info


def _calibration_meta(out_dir):
    import json
    try:
        with open(os.path.join(out_dir, CALIBRATION_META_F),
                  encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def calibration_source(out_dir):
    """'human_review' | 'machine_unreviewed' | None (absent, disabled by a
    calibrate with nothing to learn, or legacy)."""
    meta = _calibration_meta(out_dir)
    src = meta.get('source') if meta else None
    return src if src in ('human_review', 'machine_unreviewed') else None


def _vouched(out_dir, fn):
    """Source of a learned file whose hash matches the meta, else None."""
    meta = _calibration_meta(out_dir)
    src = meta.get('source') if meta else None
    if src not in ('human_review', 'machine_unreviewed'):
        return None
    path = os.path.join(out_dir, fn)
    want = (meta.get('hashes') or {}).get(fn)
    if not want or _file_digest(path) != want:
        return None
    return src


def load_learned(out_dir):
    """``(learned, source)``. A confusion_learned.json without provenance (an
    older `calibrate` learned it from the machine's own findings) or whose
    hash does not match calibration_meta.json is ignored, so unreviewed
    output never feeds back silently."""
    import json
    source = _vouched(out_dir, LEARNED_F)
    if source is None:
        return {}, None
    try:
        with open(os.path.join(out_dir, LEARNED_F), encoding='utf-8') as f:
            learned = json.load(f)
    except (OSError, ValueError):
        return {}, None
    return (learned if isinstance(learned, dict) else {}), source


def load_ocr_profiles(out_dir):
    """``(profiles, source)``, same provenance rule as :func:`load_learned`."""
    source = _vouched(out_dir, OCR_PROFILES_F)
    if source is None:
        return {}, None
    with open(os.path.join(out_dir, OCR_PROFILES_F), 'rb') as f:
        return pickle.load(f), source


def calibration_rows(rows):
    """Reviewed findings that can teach a letter substitution: the
    edit-distance-1 and OCR-profile classes (not structural rules)."""
    return [r for r in rows if r.get('errtype') and (
        r['errtype'].startswith('edit1') or r['errtype'] == 'ocr_profile')]


def learn_confusion(pairs_of_words):
    """Counter of letter substitutions in (word, correction) pairs."""
    pairs = Counter()
    for w, s in pairs_of_words:
        r = _dld1(w or '', s or '')
        if r and r[0] == 'sub':
            pairs[r[1] + r[2]] += 1
    return pairs


REVIEW_POSITIVE = ('approved', 'fixed')
REVIEW_NEGATIVE = ('not_error',)


def reviewed_findings(out_dir, statuses):
    """Human review decisions from ui_review.db as dicts, or ``([], why)``.

    Rows are limited to ``decided_by = 'human'`` when that column exists;
    without it every review row is a click in the review UI.
    """
    from .textsource import TextSourceError, connect_ro
    path = os.path.join(out_dir, 'ui_review.db')
    if not os.path.isfile(path):
        return [], 'ui_review.db not found'
    try:
        con = connect_ro(path)
    except (TextSourceError, sqlite3.Error) as e:
        return [], f'ui_review.db unreadable: {e}'
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'findings', 'review'} <= tables:
            return [], 'ui_review.db has no review tables'
        cols = {r[1] for r in con.execute('PRAGMA table_info(review)')}
        human = " AND r.decided_by = 'human'" if 'decided_by' in cols else ''
        ph = ','.join('?' * len(statuses))
        rows = [dict(zip(('id', 'word', 'suggestion', 'errtype', 'doc',
                          'rank', 'status', 'score', 'machine_suggestion'),
                         r)) for r in con.execute(
            f"SELECT f.id, f.word, COALESCE(NULLIF(r.custom_suggestion, ''), "
            f"f.suggestion), f.errtype, COALESCE(f.doc, ''), f.rank, "
            f"r.status, f.score, f.suggestion "
            f"FROM findings f JOIN review r ON r.finding_id = f.id "
            f"WHERE f.family = 'error' AND r.status IN ({ph}){human} "
            f"ORDER BY f.id", statuses)]
    finally:
        con.close()
    return rows, ('' if rows else 'no human-reviewed findings')


def calibrate(cfg, out_dir, from_machine=False):
    """Learn a letter-substitution confusion matrix and per-book OCR profiles
    from HUMAN-REVIEWED findings (ui_review.db: approved/fixed).

    Learning from the machine's own report would reinforce its unreviewed
    suggestions, so that happens only with ``from_machine=True`` and the
    result is labelled 'machine_unreviewed'. With neither, nothing is
    learned and the existing files are left as they are.
    """
    import json
    rows, why = reviewed_findings(out_dir, REVIEW_POSITIVE)
    rows = calibration_rows(rows)
    if rows:
        source = 'human_review'
        conf_rows = [(r['word'], r['suggestion']) for r in rows]
        prof_rows = [(r['word'], r['suggestion'], r['doc']) for r in rows]
    elif from_machine:
        source = 'machine_unreviewed'
        con = sqlite3.connect(_require(out_dir, REPORT_DB_F, 'כיול'))
        conf_rows = list(con.execute(
            "SELECT DISTINCT word, suggestion FROM occurrences_full "
            "WHERE errtype = 'edit1_sub' AND ctx_hits > 0 AND score >= 4"))
        try:
            prof_rows = list(con.execute(
                "SELECT word, suggestion, doc FROM occurrences_full "
                "WHERE errtype = 'edit1_sub' AND score >= 4 "
                "AND (ctx_hits > 0 OR sugg_local >= 3)"))
        except sqlite3.OperationalError:      # older report.db without doc
            prof_rows = []
        con.close()
    else:
        print(f'[calibrate] nothing learned: {why or "no approved findings"}.'
              ' Calibration learns only from human-reviewed findings; '
              'review findings in the UI first, or pass '
              '--calibrate-from-machine to learn (labelled unreviewed) from '
              'the machine report. Earlier learned files are now disabled.',
              flush=True)
        return write_calibration_meta(out_dir, {
            'source': 'none', 'rows': 0, 'reason': why,
            'created': time.strftime('%Y-%m-%d %H:%M:%S')})

    pairs = learn_confusion(conf_rows)
    with open(os.path.join(out_dir, LEARNED_F), 'w', encoding='utf-8') as f:
        json.dump(dict(pairs), f, ensure_ascii=False, indent=1)
    print(f'[calibrate] source={source}: {len(pairs)} substitution pairs '
          f'from {sum(pairs.values()):,} findings', flush=True)
    for p, c in pairs.most_common(15):
        print(f'    {p[0]} -> {p[1]}: {c:,}', flush=True)

    # per-book OCR profiles: many substitutions of one letter pair in one
    # book prove a systematic OCR fault; `locate` rescans those books
    profiles = {}
    for w, s, doc in prof_rows:
        r = _dld1(w or '', s or '')
        if r and r[0] == 'sub':
            profiles.setdefault(doc, Counter())[r[1] + r[2]] += 1
    profiles = {d: {p: c for p, c in cnt.items() if c >= cfg.ocr_pair_min}
                for d, cnt in profiles.items()}
    profiles = {d: ps for d, ps in profiles.items() if ps}
    with open(os.path.join(out_dir, OCR_PROFILES_F), 'wb') as f:
        pickle.dump(profiles, f, protocol=4)
    info = write_calibration_meta(out_dir, {
        'source': source, 'rows': len(conf_rows), 'pairs': len(pairs),
        'profiles': len(profiles),
        'created': time.strftime('%Y-%m-%d %H:%M:%S')})
    print(f'[calibrate] OCR profiles: {len(profiles)} books, '
          f'{sum(len(v) for v in profiles.values())} systematic pairs',
          flush=True)
    if source == 'machine_unreviewed':
        print('[calibrate] WARNING: learned from UNREVIEWED machine findings '
              '(labelled machine_unreviewed in calibration_meta.json)',
              flush=True)
    return info


def _split_verify_chunk(chunk):
    """Count how often each split candidate occurs *with* spaces."""
    _chunk_stats_begin()
    seq = _W['seq']
    counts = {}
    for _, text in _W['corpus'].iter_texts(chunk):
        seq.count(tokenize(text), counts)
    return counts, _chunk_stats_end()


def load_review_rejections(out_dir):
    """Words rejected in review with GLOBAL scope (decisions.db unit '*').

    A rejection of one occurrence, of one suggested replacement or of a
    book's own spelling is not a statement about the word elsewhere, so it
    must never become a corpus-wide whitelist entry.
    """
    dec_path = os.path.join(out_dir, 'decisions.db')
    if not os.path.exists(dec_path):
        return set()
    dcon = sqlite3.connect(dec_path, timeout=30.0)
    try:
        return {r[0] for r in dcon.execute(
            "SELECT DISTINCT word FROM decisions "
            "WHERE verdict='reject' AND unit='*'")}
    except sqlite3.OperationalError:
        return set()
    finally:
        dcon.close()


def detect(spec, cfg, out_dir):
    t0 = time.time()
    with open(_require(out_dir, LEXICON_F, 'איתור'), 'rb') as f:
        freq = pickle.load(f)
    N = sum(freq.values())
    print(f'[detect] lexicon: {len(freq):,} types, {N:,} tokens', flush=True)

    # optional whitelist: words listed here are never flagged (suppression
    # only — the corpus remains the sole source of correction candidates)
    whitelist = set()
    for path in (cfg.whitelist or ()):
        with open(path, encoding='utf-8') as f:
            whitelist.update(line.strip() for line in f if line.strip())
    if whitelist:
        print(f'[detect] whitelist: {len(whitelist):,} words', flush=True)

    # words the user rejected EVERYWHERE in review are never flagged again
    rejected = load_review_rejections(out_dir)
    if rejected:
        whitelist |= rejected
        print(f'[detect] review rejections honored: {len(rejected):,} '
              f'words', flush=True)

    # substitution weights learned by `magiah calibrate` (if it was run)
    learned, lsource = load_learned(out_dir)
    if lsource:
        print(f'[detect] learned confusion matrix: {len(learned)} pairs '
              f'(source: {lsource})', flush=True)
    elif os.path.exists(os.path.join(out_dir, LEARNED_F)):
        print(f'[detect] {LEARNED_F} has no provenance (learned from '
              f'unreviewed findings by an older version) - ignored',
              flush=True)

    lex = Lexicon(freq, cfg, learned)
    index = Edit1Index(lex)
    print(f'[detect] del-index={len(index.del_index):,}  '
          f'({time.time()-t0:.0f}s)', flush=True)

    errors = {}       # word -> (freq, errtype, suggestion, sugg_freq, score)
    split_cands = {}  # word -> [(parts, strong), ...]
    for n, (w, fw) in enumerate(freq.items(), 1):
        if w in whitelist:
            continue
        if n % 500000 == 0:
            print(f'  [detect] scanned {n:,} types ({time.time()-t0:.0f}s)',
                  flush=True)
        recs, splits = evaluate_word(w, fw, lex, index)
        for rec in recs:
            record_best(errors, w, fw, rec)
        if splits:
            split_cands[w] = splits
    del index

    # --- verify split candidates against the corpus -----------------------
    cand_list = [(w, parts) for w, cs in split_cands.items()
                 for parts, _ in cs]
    print(f'[detect] split candidates to verify: {len(cand_list):,} '
          f'({len(split_cands):,} words)  ({time.time()-t0:.0f}s)',
          flush=True)
    splits_path = os.path.join(out_dir, SPLITS_F)
    with open(splits_path, 'wb') as f:
        pickle.dump(cand_list, f, protocol=4)
    corpus = make_corpus(spec)
    counts = Counter()
    vstats = ReadStats()
    chunks = corpus.chunks(cfg.n_chunks)
    with _pool(spec, cfg, {'split_cands': splits_path}) as pool:
        for i, (c, st) in enumerate(
                pool.imap_unordered(_split_verify_chunk, chunks), 1):
            counts.update(c)
            vstats.add(ReadStats.from_dict(st))
            if i % 6 == 0:
                print(f'  [verify] chunk {i}/{len(chunks)} '
                      f'({time.time()-t0:.0f}s)', flush=True)
    n_ok, alts_out, idx = 0, {}, 0
    for w, cs in split_cands.items():
        obs = [counts.get(idx + k, 0) for k in range(len(cs))]
        idx += len(cs)
        best, alts = resolve_splits(cs, obs, lex, 'corpus')
        if not best:
            keep_structural_suspicion(errors, w, freq[w], cs, lex)
        if best:
            settle_split(errors, w, freq[w], best, lex, 'corpus')
            alts_out[w] = [(' '.join(p), o, e, ok and p == best[0])
                           for p, o, e, ok in alts]
            n_ok += 1
    print(f'[verify] confirmed {n_ok:,}/{len(split_cands):,} split words',
          flush=True)
    with open(os.path.join(out_dir, SPLIT_ALTS_F), 'wb') as f:
        pickle.dump(alts_out, f, protocol=4)
    _write_coverage(out_dir, 'detect', vstats,
                    {'calibration': lsource or 'none'})
    _fail_if_partial('detect', vstats, out_dir)

    with open(os.path.join(out_dir, FLAGGED_F), 'wb') as f:
        pickle.dump(errors, f, protocol=4)
    print(f'[detect] flagged={len(errors):,}  time={time.time()-t0:.0f}s',
          flush=True)
    for k, v in Counter(v[1] for v in errors.values()).most_common():
        print(f'    {k}: {v:,}', flush=True)


# ---------------------------------------------------------------------------
# Tanach reference (Otzaria corpora). magiah.tanach identifies the Bible books
# explicitly (category + title + verse heRefs), aligns their editions per
# (work, chapter, verse) and groups them into independent sources. A quotation
# counts as evidence only when it aligns with ONE verse position; nothing here
# replaces the detector's suggestion — a Tanach reading is an alternative.
# ---------------------------------------------------------------------------

def _build_verse_index(db_path):
    """The verified-editions index (see magiah.tanach)."""
    idx = tanach.build_index(db_path)
    if idx.stats.decode_errors:
        raise PartialRead(f'Tanach index: {idx.stats.decode_errors:,} rows '
                          f'could not be decoded')
    return idx


def _tanach_edition_errors(db_path, vidx):
    """Minority readings of one independent source against >= 2 agreeing
    independent sources, within the same work and verse."""
    rows, st = vidx.edition_errors()
    print(f'[tanach] edition disagreements: {st}', flush=True)
    return rows


def _tanach_check(db_path, all_occ, flagged):
    """Compare occurrences with the verse they quote.

    Returns ``(kept, tan_info, matches, edition_rows, evidence_rows)``.
    An occurrence leaves the main report only when >= 2 independent sources
    read exactly that word at the aligned verse position."""
    t1 = time.time()
    vidx = _build_verse_index(db_path)
    c = vidx.counts()
    print(f"[tanach] index: {c['works']} works, {c['verses']:,} verses, "
          f"{c['editions']} editions, {c['independent_sources']} independent "
          f"sources ({time.time()-t1:.0f}s)", flush=True)
    kept, tan_info, matches, evidence_rows = [], [], [], []
    kinds = Counter()
    cache = {}
    for oc in all_occ:
        w, uid, doc, prev, nxt, snip = oc
        key = (w, prev, nxt, snip)
        if key not in cache:
            cache[key] = vidx.evidence(w, prev, nxt, snip)
        ev = cache[key]
        code = tanach.TANACH_NONE
        if ev is not None:
            fr = flagged.get(w)
            code, kind = ev.decide(fr[2] if fr else '')
            kinds[kind] += 1
            evidence_rows.append(tanach.evidence_row(
                vidx, ev, w, uid, doc, snip,
                (fr[1], fr[2]) if fr else ('', '')))
            if ev.kind == tanach.MATCH:
                matches.append((w, uid, doc, snip, evidence_rows[-1][-1]))
                continue
        kept.append(oc)
        tan_info.append((code, ev.reading if code else ''))
    print(f'[tanach] aligned occurrences by kind: {dict(kinds)}', flush=True)
    edition_rows = _tanach_edition_errors(db_path, vidx)
    return kept, tan_info, matches, edition_rows, evidence_rows


# stage-3 helper: a rare word right after a title is a person/place name
NAME_TRIGGERS = frozenset((
    'ר', 'רב', 'רבי', 'הרב', 'הגאון', 'מו"ה', 'מוה"ר', 'מהר"ר', 'כמוהר"ר',
    'מהור"ר', 'אבד"ק', 'בק"ק', 'דק"ק', 'מק"ק', 'בכפר', 'בעיר', 'במדינת',
    'לעיר', 'העיר'))

# ---------------------------------------------------------------------------
# stage 3: locate occurrences + extra-space + context verification
# ---------------------------------------------------------------------------

def _snippet(text, s, e):
    """Bounded context of one token — never the whole (possibly huge) line."""
    return text[max(0, s - 45):e + 45].strip()


def _editorial_adjacent(text, s, e):
    """A trailing geresh marks an abbreviation (וכוננ' = וכוננה); an adjacent
    bracket/asterisk marks editorial notation (קיצ[ו]ר, תקינ(?))."""
    return ((e < len(text) and text[e] in '\'"([{*&')
            or (s > 0 and text[s - 1] in ')]}*&'))


def ocr_profile_hit(w, fw, prof, freq, cfg):
    """``(correction, freq)`` of `w` along a book's profiled letter pairs.

    In books with a proven systematic substitution, words above the global
    rarity cutoff are rescanned for exactly those pairs.
    """
    if not (3 <= len(w) <= 15) or is_abbrev(w) \
            or not (1 <= fw <= cfg.ocr_rare_max):
        return None
    hit = None
    for pair in prof:
        a, b = pair[0], pair[1]
        i0 = w.find(a)
        while i0 != -1:
            w2 = w[:i0] + b + w[i0 + 1:]
            f2 = freq.get(w2, 0)
            if (f2 >= cfg.common_min and f2 >= cfg.ocr_ratio * fw
                    and (hit is None or f2 > hit[1])):
                hit = (w2, f2)
            i0 = w.find(a, i0 + 1)
    return hit


def ocr_score(fw, fs):
    return 2 + math.log10(fs / max(fw, 1))


def locate_line(content, freq, cfg, flagged, prof=None):
    """All findings of one line; core.locate and the book scan both use it.

    Returns None for a skipped foreign/garbled line, else ``(occ, joins,
    ocr)``: occ ``[(word, prev, next, snippet)]`` for flagged words, joins
    ``[(part1, part2, joined, join_freq, snippet)]``, ocr ``[(word, freq,
    correction, corr_freq, snippet)]``. Snippets exist only for reported
    tokens and are bounded, so a multi-MB line stays linear in time and
    memory and is never truncated.
    """
    from .normalize import TOKEN_RE, clean
    text = clean(content)
    toks = list(TOKEN_RE.finditer(text))
    # foreign-language / heavily garbled lines: too many uncommon words for
    # point-fixes to mean anything. 1-2 letter tokens dilute the ratio.
    n_real = uncommon = 0
    for m in toks:
        t = m.group()
        if len(t) >= 3:
            n_real += 1
            if freq.get(t, 0) < cfg.common_min:
                uncommon += 1
    if n_real >= 5 and uncommon > cfg.foreign_ratio * n_real:
        return None

    # pass 1 — extra space: adjacent pair whose concatenation is a frequent
    # word. Such tokens are excluded from letter-level reporting below, so
    # one textual error yields one report row.
    joins, joined_idx = [], set()
    for k in range(len(toks) - 1):
        m, nx = toks[k], toks[k + 1]
        w, b = m.group(), nx.group()
        if is_abbrev(w):
            continue
        # the gap must be pure whitespace — a bracket, hyphen or asterisk
        # means editorial markup (בחשבונ(י)ך, ל-נקותם)
        if (nx.start() - m.end() <= 1 and not is_abbrev(b)
                and not text[m.end():nx.start()].strip()):
            mn = min(freq.get(w, 0), freq.get(b, 0))
            if mn <= 3:
                fj = freq.get(w + b, 0)
                if fj >= cfg.join_min and fj >= 30 * max(1, mn):
                    joins.append((w, b, w + b, fj,
                                  text[max(0, m.start() - 45):
                                       nx.end() + 45].strip()))
                    joined_idx.update((k, k + 1))

    # pass 2 — occurrences of flagged word types (+ OCR-profile layer)
    occ, ocr = [], []
    for k, m in enumerate(toks):
        w = m.group()
        if prof and w not in flagged:
            fw = freq.get(w, 0)
            hit = ocr_profile_hit(w, fw, prof, freq, cfg)
            if hit and not _editorial_adjacent(text, m.start(), m.end()):
                ocr.append((w, fw, hit[0], hit[1],
                            _snippet(text, m.start(), m.end())))
        fr = flagged.get(w)
        if fr is None or k in joined_idx:
            continue
        s, e = m.start(), m.end()
        if _editorial_adjacent(text, s, e):
            continue
        # a footnote marker glued to a word ('...א)') is notation, not an
        # extra last letter
        if (e < len(text) and text[e] == ')' and fr[1] == 'edit1_ins'
                and fr[2] == w[:-1]):
            continue
        prev = toks[k - 1].group() if k else ''
        nxt = toks[k + 1].group() if k + 1 < len(toks) else ''
        # a rare word right after a title (ר' פלוני, מוה"ר ר' פלוני) is a
        # person/place name, not a typo
        if fr[1] not in ('missing_space', 'final_midword') \
                and (prev in NAME_TRIGGERS
                     or (k >= 2 and toks[k - 2].group() in NAME_TRIGGERS)):
            continue
        occ.append((w, prev, nxt, _snippet(text, s, e)))
    return occ, joins, ocr


def _locate_chunk(chunk):
    _chunk_stats_begin()
    freq, flagged, cfg = _W['lexicon'], _W['flagged'], _W['cfg']
    profiles = _W.get('ocr_profiles') or {}
    occ, joins, ocr = [], [], []
    for uid, doc, content in _W['corpus'].iter_texts_docs(chunk):
        res = locate_line(content, freq, cfg, flagged, profiles.get(doc))
        if res is None:
            continue
        o, j, c = res
        occ.extend((w, uid, doc, p, n, s) for w, p, n, s in o)
        joins.extend((uid,) + x for x in j)
        ocr.extend((w, fw, sg, fs, uid, doc, s) for w, fw, sg, fs, s in c)
    return occ, joins, ocr, _chunk_stats_end()


def count_pairs(toks, pairs, first, counts):
    """Count the wanted adjacent (word, word) pairs in one token list."""
    for i in range(len(toks) - 1):
        t = toks[i]
        if t in first:
            p = (t, toks[i + 1])
            if p in pairs:
                counts[p] += 1


def _ctx_count_chunk(chunk):
    """One corpus pass that feeds the verification signals:
    * ctx: how often does (neighbor, correction) occur as an adjacent pair?
    * local: how often does each proposed correction occur in the same book
      as the flagged word?
    * seq (book scan only): spaced occurrences of split candidates."""
    _chunk_stats_begin()
    pairs, first = _W['ctx_pairs'], _W['ctx_first']
    book_need = _W['book_need']
    seq = _W.get('seq')
    counts, local, seq_counts = Counter(), Counter(), {}
    for _, doc, text in _W['corpus'].iter_texts_docs(chunk):
        toks = tokenize(text)
        need = book_need.get(doc)
        if need is not None:
            for t in toks:
                if t in need:
                    local[(doc, t)] += 1
        count_pairs(toks, pairs, first, counts)
        if seq is not None:
            seq.count(toks, seq_counts)
    return counts, local, seq_counts, _chunk_stats_end()


def write_scan_meta(con, spec):
    """Record in report.db which corpus produced it, so the fixer can write
    each finding into the library it was found in, not the current setting."""
    import json
    from datetime import datetime
    root = (os.path.abspath(spec['path'])
            if spec.get('type') in ('library', 'hybrid') and spec.get('path')
            else '')
    con.execute('CREATE TABLE IF NOT EXISTS scan_meta('
                'key TEXT PRIMARY KEY, value TEXT)')
    con.executemany('INSERT OR REPLACE INTO scan_meta VALUES(?,?)', [
        ('corpus', json.dumps(spec, ensure_ascii=False)),
        ('library_root', root),
        ('scanned_at', datetime.now().isoformat(timespec='microseconds'))])


def locate(spec, cfg, out_dir):
    t0 = time.time()
    with open(_require(out_dir, FLAGGED_F, 'מיקום'), 'rb') as f:
        flagged = pickle.load(f)
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)

    all_occ, all_joins, all_ocr = [], [], []
    lstats = ReadStats()
    loads = {'lexicon': os.path.join(out_dir, LEXICON_F),
             'flagged': os.path.join(out_dir, FLAGGED_F)}
    prof_path = os.path.join(out_dir, OCR_PROFILES_F)
    psource = calibration_source(out_dir)
    if os.path.exists(prof_path):
        if psource:
            loads['ocr_profiles'] = prof_path
            print(f'[locate] OCR profiles in use (source: {psource})',
                  flush=True)
        else:
            print(f'[locate] {OCR_PROFILES_F} has no provenance - ignored',
                  flush=True)
    with _pool(spec, cfg, loads) as pool:
        for i, (occ, joins, ocr, st) in enumerate(
                pool.imap_unordered(_locate_chunk, chunks), 1):
            lstats.add(ReadStats.from_dict(st))
            all_occ.extend(occ)
            all_joins.extend(joins)
            all_ocr.extend(ocr)
            print(f'  [locate] chunk {i}/{len(chunks)}  occ={len(all_occ):,}  '
                  f'space={len(all_joins):,}  ocr={len(all_ocr):,}  '
                  f'({time.time()-t0:.0f}s)', flush=True)

    # --- Tanach reference check (Otzaria only) ----------------------------
    # Verified verse matches go to a separate review file (the reference
    # itself might be wrong); a differing verse reading is stored next to the
    # detector's suggestion as an alternative, never in place of it.
    tanach_matches, tanach_errors_rows, tanach_evidence = [], [], []
    tan_info = None
    if spec.get('preset') == 'otzaria':
        (all_occ, tan_info, tanach_matches, tanach_errors_rows,
         tanach_evidence) = _tanach_check(spec['path'], all_occ, flagged)
        print(f'[tanach] verse matches (separate review file): '
              f'{len(tanach_matches):,}  edition variants: '
              f'{len(tanach_errors_rows):,}', flush=True)

    # words whose (very few) occurrences all sit in a single book are usually
    # the author's own idiosyncratic spelling, not typos
    w_count, w_docs = Counter(), {}
    for w, _, doc, _, _, _ in all_occ:
        w_count[w] += 1
        w_docs.setdefault(w, set()).add(doc)
    repeat_words = {w for w, n in w_count.items()
                    if book_repeat_flag(flagged[w][1], n, len(w_docs[w]) == 1)}
    print(f'[locate] same-book-repeat words suppressed: {len(repeat_words):,}',
          flush=True)

    # context verification for edit-distance-1 suggestions (does the corrected
    # word occur next to the same neighbors elsewhere?) + book-local counts
    # (does the corrected word occur in this very book?)
    ctx_pairs, book_need = set(), {}
    for w, uid, doc, prev, nxt, _ in all_occ:
        fr = flagged[w]
        if uses_context(fr[1]):
            sugg = fr[2]
            if prev:
                ctx_pairs.add((prev, sugg))
            if nxt:
                ctx_pairs.add((sugg, nxt))
        if fr[1] in LOCAL_TYPES and fr[2]:
            book_need.setdefault(doc, set()).add(fr[2])
    print(f'[locate] context pairs to verify: {len(ctx_pairs):,}', flush=True)
    ctx_counts, local_counts = Counter(), Counter()
    if ctx_pairs or book_need:
        ctx_path = os.path.join(out_dir, 'ctx_pairs.pkl')
        with open(ctx_path, 'wb') as f:
            pickle.dump(ctx_pairs, f, protocol=4)
        need_path = os.path.join(out_dir, 'book_need.pkl')
        with open(need_path, 'wb') as f:
            pickle.dump(book_need, f, protocol=4)
        with _pool(spec, cfg, {'ctx_pairs': ctx_path,
                               'book_need': need_path}) as pool:
            for i, (c, lc, _sc, _st) in enumerate(
                    pool.imap_unordered(_ctx_count_chunk, chunks), 1):
                ctx_counts.update(c)
                local_counts.update(lc)
                if i % 6 == 0:
                    print(f'  [context] chunk {i}/{len(chunks)} '
                          f'({time.time()-t0:.0f}s)', flush=True)

    # --- write the report database ---------------------------------------
    db_path = os.path.join(out_dir, REPORT_DB_F)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    # occurrences.occ_sugg/occ_score: an occurrence-level suggestion (OCR
    # profiles differ per book) that overrides the word-level one in enrich;
    # evidence: what actually supports the finding (see evidence_kind)
    con.executescript('''
        CREATE TABLE errors(word TEXT PRIMARY KEY, freq INT, errtype TEXT,
                            suggestion TEXT, sugg_freq INT, score REAL);
        CREATE TABLE occurrences(word TEXT, unit TEXT, doc TEXT, ctx_hits INT,
                                 sugg_local INT, book_repeat INT, tanach INT,
                                 tanach_sugg TEXT, snippet TEXT,
                                 evidence TEXT, occ_sugg TEXT,
                                 occ_score REAL);
        CREATE TABLE space_errors(unit TEXT, part1 TEXT, part2 TEXT,
                                  joined TEXT, join_freq INT, snippet TEXT);
        CREATE TABLE tanach_matches(word TEXT, unit TEXT, doc TEXT,
                                    snippet TEXT, evidence TEXT);
        CREATE TABLE tanach_errors(unit TEXT, word TEXT, canonical TEXT,
                                   snippet TEXT, evidence TEXT);
        CREATE TABLE split_alternatives(word TEXT, parts TEXT, observed INT,
                                        expected REAL, chosen INT,
                                        is_primary INT);
    ''')
    write_scan_meta(con, spec)
    con.executemany('INSERT OR REPLACE INTO errors VALUES(?,?,?,?,?,?)',
                    [(w, *v) for w, v in flagged.items()])
    rows = []
    for j, (w, uid, doc, prev, nxt, snip) in enumerate(all_occ):
        fr = flagged[w]
        hits = 0
        if uses_context(fr[1]):
            sugg = fr[2]
            hits = (ctx_counts.get((prev, sugg), 0)
                    + ctx_counts.get((sugg, nxt), 0))
        local = local_counts.get((doc, fr[2]), 0) if fr[1] in LOCAL_TYPES else 0
        tan, tsugg = tan_info[j] if tan_info else (0, '')
        ev = evidence_kind(fr[1], tsugg or fr[2], fr[3], hits, local, tan)
        rows.append((w, uid, doc, hits, local,
                     1 if w in repeat_words else 0, tan, tsugg, snip,
                     ev, None, None))
    con.executemany('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    rows)
    # OCR-profile findings: word-level entry + occurrence rows carrying their
    # own book's suggestion (the word-level one is only the first seen)
    for w, fw, sugg, fs, uid, doc, snip in all_ocr:
        if w not in flagged:
            score = ocr_score(fw, fs)
            con.execute('INSERT OR IGNORE INTO errors VALUES(?,?,?,?,?,?)',
                        (w, fw, 'ocr_profile', sugg, fs, score))
            con.execute('INSERT INTO occurrences '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (w, uid, doc, 0, 0, 0, 0, '', snip,
                         evidence_kind('ocr_profile', sugg, fs, 0, 0, 0),
                         sugg, score))
    if all_ocr:
        print(f'[locate] ocr-profile findings: {len(all_ocr):,}', flush=True)
    alts_path = os.path.join(out_dir, SPLIT_ALTS_F)
    if os.path.exists(alts_path):
        with open(alts_path, 'rb') as f:
            alts = pickle.load(f)
        # chosen: the best-evidenced segmentation; is_primary: that split is
        # the word's reported finding (else a correction kept precedence)
        con.executemany(
            'INSERT INTO split_alternatives VALUES(?,?,?,?,?,?)',
            [(w, p, o, e, 1 if ch else 0,
              1 if flagged[w][1] == 'missing_space' else 0)
             for w, lst in alts.items() if w in flagged
             for p, o, e, ch in lst])
    con.executemany('INSERT INTO tanach_matches VALUES(?,?,?,?,?)',
                    tanach_matches)
    con.executemany('INSERT INTO tanach_errors VALUES(?,?,?,?,?)',
                    tanach_errors_rows)
    tanach.write_evidence(con, tanach_evidence)
    con.executemany('INSERT INTO space_errors VALUES(?,?,?,?,?,?)', all_joins)
    con.commit()
    corpus.enrich(con)
    con.close()
    print(f'[locate] occurrences={len(rows):,}  space_errors={len(all_joins):,}'
          f'  time={time.time()-t0:.0f}s -> {db_path}', flush=True)
    _write_coverage(out_dir, 'locate', lstats,
                    {'ocr_profiles': psource or 'none'})
    _fail_if_partial('locate', lstats, out_dir)


# ---------------------------------------------------------------------------
# stage 4: report
# ---------------------------------------------------------------------------

# Ranking combines the base score with three corpus-evidence signals:
# * ctx_hits    — the correction was seen next to the same neighbors (edit1)
# * sugg_local  — the correction is used inside the very same book
# * book_repeat — all occurrences of the word sit in one book (idiosyncratic
#                 spelling, not a typo) — strong demotion
# plus the Tanach signal: tanach = 3 (an aligned verse whose reading, backed
# by >= 2 independent sources, differs from the word AND equals the detector's
# suggestion). tanach = 4 (the verse reads otherwise than the detector) and
# tanach = 2 (rows of the old trigram heuristic) earn nothing.
RANK_SQL = '''score
              + CASE WHEN errtype LIKE 'edit1%' THEN
                  CASE WHEN ctx_hits > 0 THEN 1.5 ELSE -1.0 END
                ELSE 0 END
              + CASE WHEN sugg_local >= 10 THEN 1.5
                     WHEN sugg_local >= 3 THEN 0.7 ELSE 0 END
              - CASE WHEN book_repeat = 1 THEN 3.0 ELSE 0 END
              + CASE WHEN tanach = 3 THEN 4.0 ELSE 0 END'''

# a finding is "verified" when the context or the book itself supports the
# proposed correction and the word is not an in-book spelling convention.
# The column `evidence` names WHICH support was found (and its scope).
VERIFIED_SQL = 'book_repeat = 0 AND (ctx_hits > 0 OR sugg_local >= 3)'


def _open_report(path):
    try:
        return open(path, 'w', newline='', encoding='utf-8-sig')
    except PermissionError:
        print(f'[report] SKIPPED (file open in another program): {path}',
              flush=True)
        return None


def _write_reports(con, dest_dir, extra_where, params, top):
    """Write the full set of report CSVs into dest_dir, optionally filtered
    (extra_where/params) to a single source repository."""
    limit = f'LIMIT {top}' if top else ''
    types = [r[0] for r in con.execute(
        'SELECT DISTINCT errtype FROM occurrences_full ORDER BY errtype')]
    # the Tanach reading sits next to the detector's suggestion, never in it
    have = {r[1] for r in con.execute('PRAGMA table_info(occurrences_full)')}
    ev = 'evidence' if 'evidence' in have else "''"      # older report.db
    tan_col = ("COALESCE(tanach_reading, '')" if 'tanach_reading' in have
               else "''")
    cols = ('word, suggestion, {tan}, ROUND({rank}, 2), ctx_hits, '
            'sugg_local, {ev}, source, ref, unit, snippet').format(
                rank=RANK_SQL, tan=tan_col, ev=ev)
    header = ['word', 'suggestion', 'tanach_reading', 'rank', 'ctx_hits',
              'sugg_local', 'evidence', 'source', 'ref', 'unit', 'snippet']
    for t in types:
        variants = [(f'errors_{t}.csv', f'errtype = ?{extra_where}')]
        if t.startswith('edit1'):
            # edit1 is the noisiest class — also export the high-precision
            # subset where corpus evidence supports the correction
            variants.append((f'errors_{t}_verified.csv',
                             f'errtype = ? AND {VERIFIED_SQL}{extra_where}'))
        for fname, where in variants:
            path = os.path.join(dest_dir, fname)
            out = _open_report(path)
            if out is None:
                continue
            with out as f:
                wr = csv.writer(f)
                wr.writerow(header)
                n = 0
                for row in con.execute(f'''
                        SELECT {cols} FROM occurrences_full WHERE {where}
                        ORDER BY {RANK_SQL} DESC {limit}''', (t, *params)):
                    wr.writerow(row)
                    n += 1
            print(f'[report] {n:,} rows -> {path}', flush=True)
    # Tanach review files (Otzaria corpora)
    for tbl, fname, sel, hdr in (
            ('tanach_matches_full', 'tanach_matches.csv',
             'word, source, ref, unit, snippet',
             ['word', 'source', 'ref', 'unit', 'snippet']),
            ('tanach_errors_full', 'tanach_edition_errors.csv',
             'word, canonical, source, ref, unit, snippet',
             ['word', 'canonical', 'source', 'ref', 'unit', 'snippet'])):
        try:
            con.execute(f'SELECT 1 FROM {tbl} LIMIT 1')
        except sqlite3.OperationalError:
            continue
        if 'evidence' in {r[1] for r in con.execute(
                f'PRAGMA table_info({tbl})')}:
            sel, hdr = sel + ', evidence', hdr + ['evidence']
        path = os.path.join(dest_dir, fname)
        out = _open_report(path)
        if out is None:
            continue
        with out as f:
            wr = csv.writer(f)
            wr.writerow(hdr)
            n = 0
            for row in con.execute(
                    f'SELECT {sel} FROM {tbl} WHERE 1=1{extra_where}',
                    params):
                wr.writerow(row)
                n += 1
        print(f'[report] {n:,} rows -> {path}', flush=True)

    path = os.path.join(dest_dir, 'space_errors.csv')
    out = _open_report(path)
    if out is not None:
        with out as f:
            wr = csv.writer(f)
            wr.writerow(['part1', 'part2', 'joined', 'join_freq',
                         'source', 'ref', 'unit', 'snippet'])
            n = 0
            for row in con.execute(f'''
                    SELECT part1, part2, joined, join_freq, source, ref,
                           unit, snippet
                    FROM space_errors_full WHERE 1=1{extra_where}
                    ORDER BY join_freq DESC {limit}''', params):
                wr.writerow(row)
                n += 1
        print(f'[report] {n:,} space errors -> {path}', flush=True)


def report(cfg, out_dir, top=0):
    con = sqlite3.connect(_require(out_dir, REPORT_DB_F, 'דוחות'))
    _write_reports(con, out_dir, '', (), top)
    # a separate folder per source repository (Sefaria, Dicta, wikisource...)
    try:
        origins = [r[0] for r in con.execute(
            "SELECT DISTINCT origin FROM occurrences_full "
            "WHERE origin IS NOT NULL AND origin != '' ORDER BY origin")]
    except sqlite3.OperationalError:
        origins = []
    for org in origins:
        safe = re.sub(r'[^\w.\-]+', '_', org)
        d = os.path.join(out_dir, 'by_source', safe)
        os.makedirs(d, exist_ok=True)
        print(f'[report] ===== {org} =====', flush=True)
        _write_reports(con, d, ' AND origin = ?', (org,), top)
    con.close()
