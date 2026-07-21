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
import sqlite3
import time
from collections import Counter
from multiprocessing import Pool

from .config import Config
from .corpus import make_corpus
from .normalize import (CONFUSABLE, FINALS, FROM_FINAL, PREFIX_LETTERS,
                        SUFFIX_LETTERS, TO_FINAL, is_abbrev, tokenize)

LEXICON_F = 'lexicon.pkl'
FLAGGED_F = 'flagged.pkl'
SPLITS_F = 'split_cands.pkl'
REPORT_DB_F = 'report.db'

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
        pairs, first = {}, set()
        for idx, (_, parts) in enumerate(_W['split_cands']):
            first.add(parts[0])
            pairs.setdefault((parts[0], parts[1]), []).append(idx)
        _W['sc_pairs'], _W['sc_first'] = pairs, first
    if 'ctx_pairs' in _W:
        _W['ctx_first'] = {a for a, _ in _W['ctx_pairs']}


def _pool(spec, cfg, loads=None):
    return Pool(cfg.workers, initializer=_init,
                initargs=(spec, cfg.to_dict(), loads or {}))


# ---------------------------------------------------------------------------
# stage 1: lexicon
# ---------------------------------------------------------------------------

def _count_chunk(chunk):
    c = Counter()
    for _, text in _W['corpus'].iter_texts(chunk):
        c.update(tokenize(text))
    return c


def build_lexicon(spec, cfg, out_dir):
    t0 = time.time()
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)
    lex = Counter()
    with _pool(spec, cfg) as pool:
        for i, c in enumerate(pool.imap_unordered(_count_chunk, chunks), 1):
            lex.update(c)
            print(f'  [lexicon] chunk {i}/{len(chunks)}  types={len(lex):,}  '
                  f'({time.time()-t0:.0f}s)', flush=True)
    with open(os.path.join(out_dir, LEXICON_F), 'wb') as f:
        pickle.dump(dict(lex), f, protocol=4)
    print(f'[lexicon] tokens={sum(lex.values()):,}  types={len(lex):,}  '
          f'time={time.time()-t0:.0f}s', flush=True)


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


def _segment(w, freq, cfg):
    """Split w into a sequence of frequent parts (missing-space candidate).
    Dynamic programming, minimizing the number of parts."""
    n = len(w)
    best = [None] * (n + 1)              # best[i] = (nparts, -minfreq, parts)
    best[0] = (0, -math.inf, [])
    for i in range(n):
        if best[i] is None:
            continue
        for j in range(i + 2, min(i + cfg.max_part, n) + 1):
            f = freq.get(w[i:j], 0)
            if f < cfg.part_min:
                continue
            cand = (best[i][0] + 1, max(best[i][1], -f), best[i][2] + [w[i:j]])
            if best[j] is None or cand[:2] < best[j][:2]:
                best[j] = cand
    if best[n] is None or not (2 <= best[n][0] <= cfg.max_parts):
        return None
    return best[n][2], -best[n][1]


def _split_verify_chunk(chunk):
    """Count how often each split candidate occurs *with* spaces."""
    first, pairs, cands = _W['sc_first'], _W['sc_pairs'], _W['split_cands']
    counts = Counter()
    for _, text in _W['corpus'].iter_texts(chunk):
        toks = tokenize(text)
        for i in range(len(toks) - 1):
            if toks[i] in first:
                for idx in pairs.get((toks[i], toks[i + 1]), ()):
                    parts = cands[idx][1]
                    if len(parts) == 2 or (i + 2 < len(toks)
                                           and toks[i + 2] == parts[2]):
                        counts[idx] += 1
    return counts


def detect(spec, cfg, out_dir):
    t0 = time.time()
    with open(os.path.join(out_dir, LEXICON_F), 'rb') as f:
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

    common = {w: f for w, f in freq.items()
              if f >= cfg.common_min and not is_abbrev(w) and 2 <= len(w) <= 18}
    del_index = {}
    for w in common:                     # SymSpell-style deletion index
        for i in range(len(w)):
            del_index.setdefault(w[:i] + w[i + 1:], []).append(w)
    print(f'[detect] common={len(common):,}  del-index={len(del_index):,}  '
          f'({time.time()-t0:.0f}s)', flush=True)

    errors = {}       # word -> (freq, errtype, suggestion, sugg_freq, score)
    split_cands = {}  # word -> (parts_tuple, strong)

    def record(w, fw, errtype, sugg, fs, score):
        cur = errors.get(w)
        if cur is None or score > cur[4]:
            errors[w] = (fw, errtype, sugg, fs, score)

    def add_split(w, parts, strong):
        cur = split_cands.get(w)
        if cur is None or (strong and not cur[1]):
            split_cands[w] = (tuple(parts), strong)

    n_scanned = 0
    for w, fw in freq.items():
        if w in whitelist:
            continue
        # non-final letter at word end (checked up to medium frequency)
        if len(w) >= 3 and w[-1] in TO_FINAL and not is_abbrev(w) and fw <= 20:
            wf = w[:-1] + TO_FINAL[w[-1]]
            ff = freq.get(wf, 0)
            if ff >= cfg.part_min and ff >= 50 * fw:
                record(w, fw, 'nonfinal_end', wf, ff, 4 + math.log10(ff / fw))

        if fw > cfg.rare_max or len(w) < 3 or is_abbrev(w):
            continue
        n_scanned += 1
        if n_scanned % 200000 == 0:
            print(f'  [detect] scanned {n_scanned:,} rare types '
                  f'({time.time()-t0:.0f}s)', flush=True)

        # 1) final-form letter in the middle of a word
        mid_final = [i for i, ch in enumerate(w[:-1]) if ch in FINALS]
        if mid_final:
            handled = False
            for i in mid_final:          # perhaps a missing space at that point
                a, b = w[:i + 1], w[i + 1:]
                if freq.get(a, 0) >= 10 and freq.get(b, 0) >= 10 and len(b) >= 2:
                    add_split(w, (a, b), True)
                    handled = True
            if not handled:              # perhaps final form instead of regular
                w2 = ''.join(FROM_FINAL.get(ch, ch) if i < len(w) - 1 else ch
                             for i, ch in enumerate(w))
                f2 = freq.get(w2, 0)
                if f2 >= cfg.common_min:
                    record(w, fw, 'final_midword', w2, f2, 5 + math.log10(f2))
                else:
                    record(w, fw, 'final_midword', '', 0, 3.0)

        # 2) missing space — verified against corpus bigrams afterwards
        if len(w) >= 5 and w not in split_cands:
            seg = _segment(w, freq, cfg)
            if seg:
                parts, _ = seg
                exp = N * math.prod(freq[p] / N for p in parts)
                if exp >= cfg.exp_prefilter:
                    add_split(w, parts, False)

        # 3) edit distance 1 from a frequent word
        cands = set()
        if w in del_index:
            cands.update(del_index[w])
        for i in range(len(w)):
            d = w[:i] + w[i + 1:]
            if d in del_index:
                cands.update(del_index[d])
            if freq.get(d, 0) >= cfg.common_min and len(d) >= 2:
                cands.add(d)
        best_c, best_score, best_kind = None, 0.0, ''
        for c in cands:
            if c == w:
                continue
            fc = freq.get(c, 0)
            if fc < cfg.common_min or fc < cfg.ed1_ratio * fw:
                continue
            r = _dld1(w, c)
            if r is None:
                continue
            kind, ch_a, ch_b, pos = r
            if kind == 'ins':
                # a doubled letter (למללך, נגמרר) is always suspicious — the
                # morphology exemptions below never apply to it
                doubled = ((pos > 0 and w[pos - 1] == ch_a)
                           or (pos + 1 < len(w) and w[pos + 1] == ch_a))
                # "extra" letter within the leading prefix cluster (דלאליעזר =
                # ד+ל+אליעזר, ובהבעל = ו+ב+הבעל) or a trailing inflection
                # suffix (מזדעזעה) — legitimate morphology, not a typo
                if (not doubled and pos <= 2 and ch_a in PREFIX_LETTERS
                        and all(c in PREFIX_LETTERS for c in w[:pos])):
                    continue
                if not doubled and pos == len(w) - 1 and ch_a in SUFFIX_LETTERS:
                    continue
            elif kind == 'sub' and pos == 0 \
                    and ch_a in PREFIX_LETTERS and ch_b in PREFIX_LETTERS:
                # א/ה/ד... swapped at word start is usually Hebrew vs Aramaic
                # prefix variation (אידועים = אַ+ידועים), not a typo
                continue
            score = math.log10(fc / fw)
            if kind == 'sub' and (ch_a, ch_b) in CONFUSABLE:
                score += 2
            elif kind in ('ins', 'del') and ch_a in 'וי':
                score += 1
            elif kind == 'swap':
                score += 1
            if score > best_score:
                best_c, best_score, best_kind = c, score, kind
        if best_c:
            record(w, fw, f'edit1_{best_kind}', best_c, freq[best_c], best_score)

    # --- verify split candidates against the corpus -----------------------
    cand_list = [(w, parts) for w, (parts, _) in split_cands.items()]
    print(f'[detect] split candidates to verify: {len(cand_list):,}  '
          f'({time.time()-t0:.0f}s)', flush=True)
    splits_path = os.path.join(out_dir, SPLITS_F)
    with open(splits_path, 'wb') as f:
        pickle.dump(cand_list, f, protocol=4)
    corpus = make_corpus(spec)
    counts = Counter()
    chunks = corpus.chunks(cfg.n_chunks)
    with _pool(spec, cfg, {'split_cands': splits_path}) as pool:
        for i, c in enumerate(pool.imap_unordered(_split_verify_chunk, chunks), 1):
            counts.update(c)
            if i % 6 == 0:
                print(f'  [verify] chunk {i}/{len(chunks)} '
                      f'({time.time()-t0:.0f}s)', flush=True)
    n_ok = 0
    for idx, (w, parts) in enumerate(cand_list):
        obs = counts.get(idx, 0)
        if not obs:
            continue
        strong = split_cands[w][1]
        minlen = min(len(p) for p in parts)
        exp = N * math.prod(freq[p] / N for p in parts)
        if strong:
            ok = obs >= 1
        elif minlen >= 3:
            ok = obs >= cfg.split_obs_min
        else:
            ok = obs >= cfg.split_obs_min_short and obs >= 2 * exp
        if ok:
            record(w, freq[w], 'missing_space', ' '.join(parts), obs,
                   4 + math.log10(obs) + (2 if strong else 0)
                   + (1 if minlen >= 3 else 0))
            n_ok += 1
    print(f'[verify] confirmed {n_ok:,}/{len(cand_list):,} splits', flush=True)

    with open(os.path.join(out_dir, FLAGGED_F), 'wb') as f:
        pickle.dump(errors, f, protocol=4)
    print(f'[detect] flagged={len(errors):,}  time={time.time()-t0:.0f}s',
          flush=True)
    for k, v in Counter(v[1] for v in errors.values()).most_common():
        print(f'    {k}: {v:,}', flush=True)


# ---------------------------------------------------------------------------
# stage 3: locate occurrences + extra-space + context verification
# ---------------------------------------------------------------------------

def _locate_chunk(chunk):
    freq, flagged, cfg = _W['lexicon'], _W['flagged'], _W['cfg']
    occ, joins = [], []
    from .normalize import TOKEN_RE, clean
    for uid, doc, content in _W['corpus'].iter_texts_docs(chunk):
        text = clean(content)
        toks = list(TOKEN_RE.finditer(text))
        # skip foreign-language / heavily garbled lines: too many of their
        # words are uncommon for point-fixes to be meaningful. Tokens of 1-2
        # letters are ignored — they are frequent in any language and would
        # dilute the ratio.
        real = [m.group() for m in toks if len(m.group()) >= 3]
        if len(real) >= 5:
            uncommon = sum(1 for t in real if freq.get(t, 0) < cfg.common_min)
            if uncommon > cfg.foreign_ratio * len(real):
                continue
        for k, m in enumerate(toks):
            w = m.group()
            if w in flagged:
                s, e = m.start(), m.end()
                # a trailing geresh marks an abbreviation (וכוננ' = וכוננה);
                # an adjacent bracket/asterisk marks editorial notation
                # (קיצ[ו]ר, תקינ(?)) — neither is a broken word
                if e < len(text) and text[e] in '\'"([{*&':
                    continue
                if s > 0 and text[s - 1] in ')]}*&':
                    continue
                prev = toks[k - 1].group() if k else ''
                nxt = toks[k + 1].group() if k + 1 < len(toks) else ''
                occ.append((w, uid, doc, prev, nxt,
                            text[max(0, s - 45):e + 45].strip()))
            # extra space: adjacent pair whose concatenation is a frequent word
            if k + 1 < len(toks) and not is_abbrev(w):
                nx = toks[k + 1]
                b = nx.group()
                # the gap must be pure whitespace — a bracket, hyphen or
                # asterisk between the tokens means editorial markup
                # (בחשבונ(י)ך, ל-נקותם), not an accidental space
                if (nx.start() - m.end() <= 1 and not is_abbrev(b)
                        and not text[m.end():nx.start()].strip()):
                    mn = min(freq.get(w, 0), freq.get(b, 0))
                    if mn <= 3:
                        fj = freq.get(w + b, 0)
                        if fj >= cfg.join_min and fj >= 30 * max(1, mn):
                            s = m.start()
                            joins.append((uid, w, b, w + b, fj,
                                          text[max(0, s - 45):nx.end() + 45].strip()))
    return occ, joins


def _ctx_count_chunk(chunk):
    """One corpus pass that feeds both verification signals:
    * ctx: how often does (neighbor, correction) occur as an adjacent pair?
    * local: how often does each proposed correction occur in the same book
      as the flagged word?"""
    pairs, first = _W['ctx_pairs'], _W['ctx_first']
    book_need = _W['book_need']
    counts, local = Counter(), Counter()
    for _, doc, text in _W['corpus'].iter_texts_docs(chunk):
        toks = tokenize(text)
        need = book_need.get(doc)
        for i, t in enumerate(toks):
            if need is not None and t in need:
                local[(doc, t)] += 1
            if t in first and i + 1 < len(toks):
                p = (t, toks[i + 1])
                if p in pairs:
                    counts[p] += 1
    return counts, local


def locate(spec, cfg, out_dir):
    t0 = time.time()
    with open(os.path.join(out_dir, FLAGGED_F), 'rb') as f:
        flagged = pickle.load(f)
    corpus = make_corpus(spec)
    chunks = corpus.chunks(cfg.n_chunks)

    all_occ, all_joins = [], []
    loads = {'lexicon': os.path.join(out_dir, LEXICON_F),
             'flagged': os.path.join(out_dir, FLAGGED_F)}
    with _pool(spec, cfg, loads) as pool:
        for i, (occ, joins) in enumerate(
                pool.imap_unordered(_locate_chunk, chunks), 1):
            all_occ.extend(occ)
            all_joins.extend(joins)
            print(f'  [locate] chunk {i}/{len(chunks)}  occ={len(all_occ):,}  '
                  f'space={len(all_joins):,}  ({time.time()-t0:.0f}s)',
                  flush=True)

    # words whose (very few) occurrences all sit in a single book are usually
    # the author's own idiosyncratic spelling, not typos
    LOCAL_TYPES = ('edit1_sub', 'edit1_ins', 'edit1_del', 'edit1_swap',
                   'nonfinal_end')
    w_count, w_docs = Counter(), {}
    for w, _, doc, _, _, _ in all_occ:
        w_count[w] += 1
        w_docs.setdefault(w, set()).add(doc)
    repeat_words = {w for w, n in w_count.items()
                    if n >= 2 and len(w_docs[w]) == 1
                    and flagged[w][1] in LOCAL_TYPES}
    print(f'[locate] same-book-repeat words suppressed: {len(repeat_words):,}',
          flush=True)

    # context verification for edit-distance-1 suggestions (does the corrected
    # word occur next to the same neighbors elsewhere?) + book-local counts
    # (does the corrected word occur in this very book?)
    ctx_pairs, book_need = set(), {}
    for w, uid, doc, prev, nxt, _ in all_occ:
        fr = flagged[w]
        if fr[1].startswith('edit1'):
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
            for i, (c, lc) in enumerate(
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
    con.executescript('''
        CREATE TABLE errors(word TEXT PRIMARY KEY, freq INT, errtype TEXT,
                            suggestion TEXT, sugg_freq INT, score REAL);
        CREATE TABLE occurrences(word TEXT, unit TEXT, doc TEXT, ctx_hits INT,
                                 sugg_local INT, book_repeat INT,
                                 snippet TEXT);
        CREATE TABLE space_errors(unit TEXT, part1 TEXT, part2 TEXT,
                                  joined TEXT, join_freq INT, snippet TEXT);
    ''')
    con.executemany('INSERT OR REPLACE INTO errors VALUES(?,?,?,?,?,?)',
                    [(w, *v) for w, v in flagged.items()])
    rows = []
    for w, uid, doc, prev, nxt, snip in all_occ:
        fr = flagged[w]
        hits = 0
        if fr[1].startswith('edit1'):
            sugg = fr[2]
            hits = (ctx_counts.get((prev, sugg), 0)
                    + ctx_counts.get((sugg, nxt), 0))
        local = local_counts.get((doc, fr[2]), 0) if fr[1] in LOCAL_TYPES else 0
        rows.append((w, uid, doc, hits, local,
                     1 if w in repeat_words else 0, snip))
    con.executemany('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?)', rows)
    con.executemany('INSERT INTO space_errors VALUES(?,?,?,?,?,?)', all_joins)
    con.commit()
    corpus.enrich(con)
    con.close()
    print(f'[locate] occurrences={len(rows):,}  space_errors={len(all_joins):,}'
          f'  time={time.time()-t0:.0f}s -> {db_path}', flush=True)


# ---------------------------------------------------------------------------
# stage 4: report
# ---------------------------------------------------------------------------

# Ranking combines the base score with three corpus-evidence signals:
# * ctx_hits    — the correction was seen next to the same neighbors (edit1)
# * sugg_local  — the correction is used inside the very same book
# * book_repeat — all occurrences of the word sit in one book (idiosyncratic
#                 spelling, not a typo) — strong demotion
RANK_SQL = '''score
              + CASE WHEN errtype LIKE 'edit1%' THEN
                  CASE WHEN ctx_hits > 0 THEN 1.5 ELSE -1.0 END
                ELSE 0 END
              + CASE WHEN sugg_local >= 10 THEN 1.5
                     WHEN sugg_local >= 3 THEN 0.7 ELSE 0 END
              - CASE WHEN book_repeat = 1 THEN 3.0 ELSE 0 END'''

# a finding is "verified" when the context or the book itself supports the
# proposed correction and the word is not an in-book spelling convention
VERIFIED_SQL = 'book_repeat = 0 AND (ctx_hits > 0 OR sugg_local >= 3)'


def report(cfg, out_dir, top=0):
    con = sqlite3.connect(os.path.join(out_dir, REPORT_DB_F))
    limit = f'LIMIT {top}' if top else ''
    # one ranked CSV per error type
    types = [r[0] for r in con.execute(
        'SELECT DISTINCT errtype FROM occurrences_full ORDER BY errtype')]
    cols = ('word, suggestion, ROUND({rank}, 2), ctx_hits, sugg_local, '
            'source, ref, unit, snippet').format(rank=RANK_SQL)
    header = ['word', 'suggestion', 'rank', 'ctx_hits', 'sugg_local',
              'source', 'ref', 'unit', 'snippet']
    for t in types:
        variants = [(f'errors_{t}.csv', 'errtype = ?')]
        if t.startswith('edit1'):
            # edit1 is the noisiest class — also export the high-precision
            # subset where corpus evidence supports the correction
            variants.append((f'errors_{t}_verified.csv',
                             f'errtype = ? AND {VERIFIED_SQL}'))
        for fname, where in variants:
            path = os.path.join(out_dir, fname)
            try:
                out = open(path, 'w', newline='', encoding='utf-8-sig')
            except PermissionError:
                print(f'[report] SKIPPED (file open in another program): '
                      f'{path}', flush=True)
                continue
            with out as f:
                wr = csv.writer(f)
                wr.writerow(header)
                n = 0
                for row in con.execute(f'''
                        SELECT {cols} FROM occurrences_full WHERE {where}
                        ORDER BY {RANK_SQL} DESC {limit}''', (t,)):
                    wr.writerow(row)
                    n += 1
            print(f'[report] {n:,} rows -> {path}', flush=True)
    p2 = os.path.join(out_dir, 'space_errors.csv')
    with open(p2, 'w', newline='', encoding='utf-8-sig') as f:
        wr = csv.writer(f)
        wr.writerow(['part1', 'part2', 'joined', 'join_freq',
                     'source', 'ref', 'unit', 'snippet'])
        for row in con.execute(f'''
                SELECT part1, part2, joined, join_freq, source, ref, unit,
                       snippet
                FROM space_errors_full ORDER BY join_freq DESC {limit}'''):
            wr.writerow(row)
    n2 = con.execute('SELECT COUNT(*) FROM space_errors_full').fetchone()[0]
    con.close()
    print(f'[report] {n2:,} space errors -> {p2}', flush=True)
