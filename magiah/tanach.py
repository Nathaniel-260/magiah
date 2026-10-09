# -*- coding: utf-8 -*-
"""Tanach reference: verified Bible editions, compared verse by verse.

What counts as a Bible edition
------------------------------
Only books that are *proven* to be a biblical book take part:

* the book sits directly under a תורה / נביאים / כתובים category whose parent
  is the top-level תנ"ך category,
* its title names one of the 39 books of the Tanach, and
* at least 90% of its referenced lines carry a ``<title>, <chapter>, <verse>``
  heRef, so every line resolves to an explicit (work, chapter, verse).

``hasTeamim`` is NOT a criterion: siddurim, haftara collections and
commentaries quoting verses carry cantillation too, and treating them as
editions made any shared word trigram look like an agreeing witness.

Editions and independence
-------------------------
An *edition* is one text of a verified book: the book row itself or one of its
``book_version`` rows (a version row with NULL content is "identical to the
primary text" and is skipped — counting it would count the primary twice).
A version whose lines do not track the primary text (a translation, a
different verse division) is rejected. Editions are grouped into *independent
sources* by provenance (the host of ``versionSource``, else the source name);
several renderings of one upload (with teamim / with nikud / letters only) are
one source, and a version without a stated source is never assumed
independent of the primary text. Every count of witnesses is a count of
independent sources, not of editions or of rows.

Quotations
----------
A quotation in another book is evidence only when its context aligns with ONE
verse position: at least ``MIN_CONTEXT`` context tokens (with at least one on
each side of the word) must match the verse consecutively, compared on a
plene-insensitive skeleton. Two candidate positions -> ambiguous, no evidence.
Qere/ketiv positions, plene/defective (ו/י) differences and readings backed by
a single independent source never produce evidence; they are only labelled.
"""
import json
import re
from array import array
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from fractions import Fraction
from urllib.parse import urlsplit

from .normalize import TOKEN_RE, clean
from .textsource import OtzariaDB, ReadStats, db_read_errors

# --- thresholds --------------------------------------------------------------
MIN_CONTEXT = 5            # aligned context tokens around the word
# A context token is *distinctive* when its skeleton has 3+ letters and occurs
# fewer than COMMON_FREQ times in the Tanach stream (~top 1% of types, which
# covers particles, divine names and narrative formulae). Rabbinic prose
# strings such words together constantly, so a run of them can sit on one
# verse by chance; MIN_DISTINCT distinctive words pin the verse down.
COMMON_FREQ = 200
MIN_DISTINCT = 2
AMBIGUITY_MARGIN = 2       # the best verse must beat the runner-up by this
MIN_INDEPENDENT = 2        # independent sources that must agree on a reading
# Shares are exact fractions: `count < SHARE * total` is then an exact
# rational comparison, so exactly 90% passes for every size.
REF_SHARE = Fraction(9, 10)  # a book's referenced lines that must parse
VERSION_LINE_RATIO = 0.6   # token similarity of a version line to the primary
VERSION_ACCEPT_SHARE = Fraction(9, 10)  # a version's lines that must track it

# --- evidence kinds ----------------------------------------------------------
VARIANT = 'tanach_verse_variant'     # verse reading == detector suggestion
DISAGREES = 'tanach_disagrees'       # verse reading != detector suggestion
MATCH = 'tanach_verse_match'         # word is the verse's reading
EDITION_VARIANT = 'tanach_edition_variant'
EDITION_UNRESOLVED = 'tanach_edition_unresolved'   # informational only
PLENE = 'tanach_plene'               # differs only in ו/י (male/haser)
QERE_KETIV = 'tanach_qere_ketiv'
SINGLE_SOURCE = 'tanach_single_source'
DISPUTED = 'tanach_disputed'         # the independent sources disagree
UNRELATED = 'tanach_unrelated'       # aligned, but not a near-spelling
AMBIGUOUS = 'tanach_ambiguous'       # context fits several verse positions
LEGACY = 'tanach_legacy'             # produced by the pre-verse heuristic

# values of the report's `tanach` column
TANACH_NONE = 0
TANACH_LEGACY = 2      # old (prev, next)-trigram mechanism: re-check required
TANACH_VERIFIED = 3    # verse reading (>= 2 sources) == detector suggestion
TANACH_DISAGREES = 4   # verse reading != detector suggestion: no bonus,
                       # both kept as alternatives for a human to decide

ROOT_TITLES = {'תנ"ך', 'תנך'}
SECTION_TITLES = {'תורה', 'נביאים', 'כתובים'}
WORKS = (
    'בראשית', 'שמות', 'ויקרא', 'במדבר', 'דברים',
    'יהושע', 'שופטים', 'שמואל א', 'שמואל ב', 'מלכים א', 'מלכים ב',
    'ישעיהו', 'ירמיהו', 'יחזקאל', 'הושע', 'יואל', 'עמוס', 'עובדיה', 'יונה',
    'מיכה', 'נחום', 'חבקוק', 'צפניה', 'חגי', 'זכריה', 'מלאכי',
    'תהילים', 'משלי', 'איוב', 'שיר השירים', 'רות', 'איכה', 'קהלת', 'אסתר',
    'דניאל', 'עזרא', 'נחמיה', 'דברי הימים א', 'דברי הימים ב')
_WORK_ALIASES = {'תהלים': 'תהילים', 'ישעיה': 'ישעיהו', 'ירמיה': 'ירמיהו'}

# report.db side table: one row per evidence decision, joined by enrich()
EVIDENCE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS tanach_evidence(
  word TEXT, unit TEXT, doc TEXT, snippet TEXT,
  evidence_kind TEXT, ref TEXT, reading TEXT, alternatives TEXT,
  evidence TEXT,
  PRIMARY KEY(word, unit, snippet))'''


def _norm_title(t):
    t = (t or '').replace('״', '"').replace('׳', "'")
    t = re.sub(r"[\"'׳]", '', t) if t not in ROOT_TITLES else t
    t = ' '.join(t.split())
    return _WORK_ALIASES.get(t, t)


def _norm_root(t):
    return ' '.join((t or '').replace('״', '"').split())


_FINAL_TO_REG = str.maketrans('ךםןףץ', 'כמנפצ')


_SK_CACHE = {}


def skeleton(tok):
    """Plene-insensitive matching key: final forms folded, ו/י dropped."""
    s = _SK_CACHE.get(tok)
    if s is None:
        s = tok.translate(_FINAL_TO_REG).replace('ו', '').replace('י', '')
        s = s or tok
        if len(_SK_CACHE) < 500000:
            _SK_CACHE[tok] = s
    return s


def plene_equal(a, b):
    """True when `a` and `b` differ by ONE inserted ו or י that is not the
    first letter (male/haser). A substitution, a transposition or a leading
    ו/י (conjunction, verb prefix) is a different word, not a spelling."""
    if len(a) == len(b) + 1:
        longer, shorter = a, b
    elif len(b) == len(a) + 1:
        longer, shorter = b, a
    else:
        return False
    return any(longer[i] in 'וי' and longer[:i] + longer[i + 1:] == shorter
               for i in range(1, len(longer)))


def within2(a, b):
    """Levenshtein distance <= 2 (adjacent transposition counted as 1)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 2:
        return False
    prev2, prev = None, list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + c)
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[lb] <= 2


# ---------------------------------------------------------------------------
# verse text -> tokens, with qere/ketiv marked
# ---------------------------------------------------------------------------
# Markup seen in Otzaria's Tanach (inspected by structure only):
# * primary text ("Miqra according to the Masorah"): a leading "(N)" verse
#   number; <span class="mam-kq-k">(ketiv)</span> <span class="mam-kq-q">
#   [qere]</span>; <span class="mam-kq-trivial">; {פ}/{ס} paragraph spans;
#   footnotes as <sup class="footnote-marker">*</sup><i class="footnote">(..)</i>
# * tanach.us versions: "(N)" verse number, the ketiv as a plain word followed
#   by "[qere]", and "(פ)"/"(ס)" paragraph markers.
_FOOT_RE = re.compile(r'<sup class="footnote-marker">.*?</sup>'
                      r'|<i class="footnote">.*?</i>', re.S)
_SPI_RE = re.compile(r'<span class="mam-spi-[^"]*">.*?</span>', re.S)
_TRIV_RE = re.compile(r'<span class="mam-kq-trivial">(.*?)</span>', re.S)
_KET_RE = re.compile(r'<span class="mam-kq-k">(.*?)</span>', re.S)
_QRE_RE = re.compile(r'<span class="mam-kq-q">(.*?)</span>', re.S)
_PIECE_RE = re.compile(
    r'\x04(?P<k>[^\x05]*)\x05\s*(?:\x06(?P<q>[^\x07]*)\x07)?'
    r'|\x06(?P<qo>[^\x07]*)\x07'
    r'|\x02(?P<triv>[^\x03]*)\x03'
    r'|\((?P<pk>[^()\[\]]*)\)\s*\[(?P<pq>[^\[\]]*)\]'
    r'|\[(?P<bq>[^\[\]]*)\]'
    r'|\([^()]*\)|\{[^{}]*\}'
    r'|(?P<tok>[א-ת]+(?:["\'][א-ת]+)*)')


def _emit_kq(out, ket, qer):
    if not qer:                       # ketiv without qere
        for t in ket:
            out.append((t, True, ()))
        return
    for i, t in enumerate(qer):
        out.append((t, True, tuple(ket) if i == 0 else ()))


def verse_tokens(raw):
    """``[(token, is_qere_ketiv, ketiv_tokens)]`` of one edition's verse.

    For a qere/ketiv pair the slot holds the qere; the ketiv is kept aside so
    a quotation that follows the ketiv still aligns."""
    t = _FOOT_RE.sub(' ', raw)
    t = _SPI_RE.sub(' ', t)
    t = _TRIV_RE.sub(lambda m: '\x02' + m.group(1) + '\x03', t)
    t = _KET_RE.sub(lambda m: '\x04' + m.group(1) + '\x05', t)
    t = _QRE_RE.sub(lambda m: '\x06' + m.group(1) + '\x07', t)
    t = clean(t)
    out = []
    for m in _PIECE_RE.finditer(t):
        g = m.groupdict()
        if g['tok'] is not None:
            out.append((g['tok'], False, ()))
        elif g['k'] is not None:
            _emit_kq(out, TOKEN_RE.findall(g['k']),
                     TOKEN_RE.findall(g['q'] or ''))
        elif g['qo'] is not None:
            _emit_kq(out, (), TOKEN_RE.findall(g['qo']))
        elif g['triv'] is not None:
            for tok in TOKEN_RE.findall(g['triv']):
                out.append((tok, True, ()))
        elif g['pk'] is not None:
            _emit_kq(out, TOKEN_RE.findall(g['pk']),
                     TOKEN_RE.findall(g['pq']))
        elif g['bq'] is not None:
            # "ketiv [qere]": the plain word just before is the ketiv
            ket = ()
            if out and not out[-1][1]:
                ket = (out.pop()[0],)
            _emit_kq(out, ket, TOKEN_RE.findall(g['bq']))
        # anything else: verse number, (פ)/(ס), {פ}/{ס} — not text
    return out


def _align(prim_sk, ed_sk):
    """primary position -> edition position (None where not 1:1), ratio."""
    sm = SequenceMatcher(None, prim_sk, ed_sk, autojunk=False)
    mapping = [None] * len(prim_sk)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal' or (tag == 'replace' and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                mapping[i1 + k] = j1 + k
    return mapping, sm.ratio()


def provenance_group(version_source, fallback):
    """Independent-source key: the host a text was taken from."""
    if version_source:
        try:
            host = urlsplit(version_source.strip()).netloc.lower()
        except ValueError:
            host = ''
        if host.startswith('www.'):
            host = host[4:]
        if host:
            return 'host:' + host
    return fallback


# ---------------------------------------------------------------------------
# identifying the Bible books
# ---------------------------------------------------------------------------

def _cols(con, table):
    return {r[1] for r in con.execute(f'PRAGMA table_info({table})')}


def _ref_re(title):
    return re.compile(r'^\s*' + re.escape(title) +
                      r'''[\s,]+(?P<c>[א-ת"'׳״]+)[\s,:]+(?P<v>[א-ת"'׳״]+)\s*$''')


def find_bible_books(con):
    """``(books, report)``: verified Bible book rows and why others failed.

    `books` is a list of dicts (book_id, work, title, source, n_verses)."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    report = {'has_teamim_books': 0, 'has_teamim_not_bible': {},
              'rejected': []}
    if 'category' not in tables:
        return [], report
    bcols = _cols(con, 'book')
    cats = {cid: (pid, title) for cid, pid, title in con.execute(
        'SELECT id, parentId, title FROM category')}
    roots = {cid for cid, (pid, t) in cats.items()
             if pid is None and _norm_root(t) in ROOT_TITLES}
    sections = {cid for cid, (pid, t) in cats.items()
                if pid in roots and _norm_title(t) in SECTION_TITLES}
    src_names = dict(con.execute('SELECT id, name FROM source')) \
        if 'source' in tables else {}
    dep = 'dependenceType' if 'dependenceType' in bcols else 'NULL'
    teamim = 'hasTeamim' if 'hasTeamim' in bcols else '0'
    works = set(WORKS)

    def top_path(cid):
        chain = []
        while cid in cats and len(chain) < 20:
            chain.append(cats[cid][1])
            cid = cats[cid][0]
        return ' / '.join(reversed(chain[-2:])) if chain else '?'

    books = []
    for bid, title, cid, sid, ht, dtype in con.execute(
            f'SELECT id, title, categoryId, sourceId, {teamim}, {dep} '
            f'FROM book ORDER BY id'):
        work = _norm_title(title)
        is_cand = cid in sections and work in works and not dtype
        if ht:
            report['has_teamim_books'] += 1
        if not is_cand:
            if ht:
                k = top_path(cid)
                report['has_teamim_not_bible'][k] = \
                    report['has_teamim_not_bible'].get(k, 0) + 1
            continue
        rx = _ref_re(title)
        n_ref = n_ok = 0
        for (ref,) in con.execute(
                'SELECT heRef FROM line WHERE bookId = ? AND heRef IS NOT NULL'
                " AND heRef != ''", (bid,)):
            n_ref += 1
            if rx.match(ref):
                n_ok += 1
        if n_ref == 0 or n_ok < REF_SHARE * n_ref:
            report['rejected'].append((bid, 'heRef', n_ok, n_ref))
            if ht:
                report['has_teamim_not_bible']['(unverified heRef)'] = \
                    report['has_teamim_not_bible'].get(
                        '(unverified heRef)', 0) + 1
            continue
        books.append({'book_id': bid, 'work': work, 'title': title,
                      'source': src_names.get(sid, 'Unknown'),
                      'n_verses': n_ok})
    return books, report


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

class Verse:
    __slots__ = ('work', 'ch', 'v', 'line_id', 'toks', 'kq', 'readings',
                 'members')

    def __init__(self, work, ch, v, line_id, toks, kq):
        self.work, self.ch, self.v, self.line_id = work, ch, v, line_id
        self.toks = toks             # primary tokens (qere in kq slots)
        self.kq = kq                 # positions touched by qere/ketiv
        self.readings = {}           # group -> tuple aligned to self.toks
        self.members = {}            # group -> edition ids that read it

    @property
    def ref(self):
        return f'{self.work} {self.ch}:{self.v}'


class Evidence:
    __slots__ = ('kind', 'verse', 'pos', 'reading', 'groups', 'editions',
                 'aligned')

    def __init__(self, kind, verse=None, pos=None, reading='', groups=(),
                 editions=(), aligned=0):
        self.kind, self.verse, self.pos = kind, verse, pos
        self.reading, self.groups, self.editions = reading, groups, editions
        self.aligned = aligned

    def decide(self, detector_suggestion):
        """``(tanach code, evidence kind)`` given the detector's suggestion.

        The rank bonus needs both signals to name the same correction; when
        the verse reads otherwise, the row is marked for a human decision."""
        if self.kind != VARIANT:
            return TANACH_NONE, self.kind
        if detector_suggestion == self.reading:
            return TANACH_VERIFIED, VARIANT
        return TANACH_DISAGREES, DISAGREES

    def to_dict(self, index):
        d = {'evidence_kind': self.kind}
        if self.verse is not None:
            d['ref'] = self.verse.ref
            d['verse_line'] = self.verse.line_id
        if self.reading:
            d['reading'] = self.reading
        if self.aligned:
            d['aligned_tokens'] = self.aligned
        # three different counts — never conflate them
        d['occurrences'] = len(self.editions)          # agreeing editions
        d['works'] = 1 if self.verse is not None else 0
        d['independent_sources'] = len(self.groups)
        if self.groups:
            d['witnesses'] = [
                {'source': g, 'editions': [index.edition_label(e)
                                           for e in self.editions
                                           if index.editions[e]['group'] == g]}
                for g in self.groups]
        return d


class TanachIndex:
    """(work, chapter, verse) -> readings of verified editions, plus an
    anchor index over the primary text for aligning quotations."""

    def __init__(self):
        self.verses = []
        self.editions = {}           # eid -> metadata
        self.report = {}
        self.stats = ReadStats()
        self.sk = []                 # global skeleton stream (None = break)
        self.loc_v = array('i')
        self.loc_p = array('i')
        self.kq_alt = {}             # stream index -> ketiv skeletons
        self.anchors = defaultdict(list)

    # -- building ------------------------------------------------------------
    @classmethod
    def build(cls, db_path, stats=None):
        """Read the verified editions. Every line and version row read is
        counted in `stats` (``idx.stats``): a row that could not be decoded,
        or a ``line`` without its ``line_content`` row, is unread, and the
        caller must not present an index built from a partial read as
        complete. The database is released even when the build fails."""
        idx = cls()
        if stats is not None:
            idx.stats = stats
        with OtzariaDB(db_path) as odb, db_read_errors(db_path):
            idx._build(odb)
        return idx

    def edition_label(self, eid):
        e = self.editions[eid]
        return e['version_title'] or f"{e['title']} ({e['source']})"

    def _build(self, odb):
        con = odb.con
        books, report = find_bible_books(con)
        self.report = report
        by_work = defaultdict(list)
        for b in books:
            by_work[b['work']].append(b)
        has_versions = odb.has_versions
        vcols = _cols(con, 'book_version') if has_versions else set()
        vsrc = 'versionSource' if 'versionSource' in vcols else 'NULL'
        rejected_versions = []
        verse_at = {}                # (work, ch, v) -> verse index
        line_verse = {}              # line id -> verse index
        ed_tokens = defaultdict(dict)  # verse idx -> eid -> token list
        kq_alts = {}                 # verse idx -> {pos: ketiv skeletons}
        for work in WORKS:
            rows = sorted(by_work.get(work, ()),
                          key=lambda b: (-b['n_verses'], b['book_id']))
            group_of_source = {}     # a second import from one source is
                                     # the same source, not a new witness
            for rank, b in enumerate(rows):
                bid = b['book_id']
                rx = _ref_re(b['title'])
                versions = []
                if has_versions:
                    versions = con.execute(
                        f'SELECT id, versionTitle, {vsrc} FROM book_version '
                        f'WHERE bookId = ? ORDER BY id', (bid,)).fetchall()
                lines = odb.book_lines(bid, self.stats)
                verse_lines = []
                for lid, ref, text in lines:
                    m = rx.match(ref) if ref else None
                    if m:
                        verse_lines.append((lid, m.group('c'), m.group('v'),
                                            text))
                # which version IS the primary text (all rows "identical")
                prim_ver = None
                for vid, vtitle, vs in versions:
                    n, nn = con.execute(
                        'SELECT COUNT(*), COUNT(content) FROM version_line '
                        'WHERE versionId = ?', (vid,)).fetchone()
                    if n and nn == 0 and n >= REF_SHARE * len(verse_lines):
                        prim_ver = (vid, vtitle, vs)
                        break
                peid = f'b{bid}'
                pgroup = group_of_source.get(b['source']) or provenance_group(
                    prim_ver[2] if prim_ver else None, 'source:' + b['source'])
                group_of_source.setdefault(b['source'], pgroup)
                self.editions[peid] = {
                    'book_id': bid, 'version_id': None, 'title': b['title'],
                    'source': b['source'], 'work': work,
                    'version_title': prim_ver[1] if prim_ver else '',
                    'version_source': prim_ver[2] if prim_ver else '',
                    'group': pgroup, 'role': 'primary' if rank == 0
                    else 'book'}
                for lid, ch, v, text in verse_lines:
                    toks = verse_tokens(text)
                    key = (work, ch, v)
                    if rank == 0 and key not in verse_at:
                        vi = len(self.verses)
                        verse_at[key] = vi
                        kq = frozenset(i for i, t in enumerate(toks) if t[1])
                        self.verses.append(Verse(
                            work, ch, v, lid, tuple(t[0] for t in toks), kq))
                        alt = {i: frozenset(skeleton(k) for k in t[2])
                               for i, t in enumerate(toks) if t[2]}
                        if alt:
                            kq_alts[vi] = alt
                    vi = verse_at.get(key)
                    if vi is None:
                        continue
                    line_verse[lid] = vi
                    ed_tokens[vi][peid] = (toks, None)
                # alternative versions with their own reading
                for vid, vtitle, vs in versions:
                    if prim_ver and vid == prim_ver[0]:
                        continue
                    eid = f'v{vid}'
                    got, ok = {}, 0
                    for lid, raw in con.execute(
                            'SELECT lineId, content FROM version_line '
                            'WHERE versionId = ? AND content IS NOT NULL',
                            (vid,)):
                        vi = line_verse.get(lid)
                        if vi is None:
                            continue
                        try:
                            text = odb.decode(raw)
                        except Exception as e:      # noqa: BLE001
                            self.stats.unread_row(f'ver:{vid}:{lid}',
                                                  'decode_errors',
                                                  repr(e)[:200])
                            continue
                        self.stats.version_lines += 1
                        toks = verse_tokens(text)
                        prim = self.verses[vi]
                        mapping, ratio = _align(
                            [skeleton(t) for t in prim.toks],
                            [skeleton(t[0]) for t in toks])
                        if ratio >= VERSION_LINE_RATIO:
                            ok += 1
                            got[vi] = (toks, mapping)
                    total = con.execute(
                        'SELECT COUNT(content) FROM version_line '
                        'WHERE versionId = ?', (vid,)).fetchone()[0]
                    if not total or ok < VERSION_ACCEPT_SHARE * total:
                        rejected_versions.append((bid, vid, vtitle, ok, total))
                        continue
                    # no stated provenance -> not independent of the primary
                    self.editions[eid] = {
                        'book_id': bid, 'version_id': vid, 'title': b['title'],
                        'source': b['source'], 'work': work,
                        'version_title': vtitle or '', 'version_source': vs
                        or '', 'group': provenance_group(vs, pgroup),
                        'role': 'version'}
                    for vi, item in got.items():
                        ed_tokens[vi][eid] = item
        self.report['rejected_versions'] = rejected_versions
        self.report['bible_books'] = len(books)
        self._finalize(ed_tokens, kq_alts)

    def _finalize(self, ed_tokens, kq_alts):
        """Per verse: align every edition to the primary tokens, fold the
        editions of one provenance group into a single reading."""
        self.intra = []              # (verse idx, pos, group, {reading: eids})
        for vi, verse in enumerate(self.verses):
            prim_sk = [skeleton(t) for t in verse.toks]
            kq = set(verse.kq)
            per_group = defaultdict(list)
            for eid, (toks, mapping) in ed_tokens.get(vi, {}).items():
                if mapping is None:
                    if [t[0] for t in toks] == list(verse.toks):
                        mapping = range(len(toks))
                    else:
                        mapping, _ = _align(prim_sk,
                                            [skeleton(t[0]) for t in toks])
                aligned = tuple(toks[j][0] if j is not None else None
                                for j in mapping)
                for i, j in enumerate(mapping):
                    if j is not None and toks[j][1]:
                        kq.add(i)
                per_group[self.editions[eid]['group']].append((eid, aligned))
            for g, members in per_group.items():
                folded = []
                for i in range(len(verse.toks)):
                    vals = {a[i] for _, a in members if a[i] is not None}
                    # a source that disagrees with itself casts no vote, but
                    # the disagreement is kept for the edition report
                    if len(vals) > 1:
                        by = defaultdict(list)
                        for e, a in members:
                            if a[i] is not None:
                                by[a[i]].append(e)
                        self.intra.append((vi, i, g, dict(by)))
                    folded.append(vals.pop() if len(vals) == 1 else None)
                verse.readings[g] = tuple(folded)
                verse.members[g] = tuple(e for e, _ in members)
            verse.kq = frozenset(kq)
            alt = kq_alts.get(vi)
            # stream for quotation alignment
            if self.sk and (self.verses[vi - 1].work != verse.work):
                self._push_break()
            for i, t in enumerate(verse.toks):
                s = len(self.sk)
                self.sk.append(skeleton(t))
                self.loc_v.append(vi)
                self.loc_p.append(i)
                if alt and i in alt:
                    self.kq_alt[s] = alt[i]
        self._push_break()
        sk = self.sk
        self.freq = Counter(x for x in sk if x is not None)
        for s in range(len(sk) - 1):
            if sk[s] is not None and sk[s + 1] is not None:
                self.anchors[(sk[s], sk[s + 1])].append(s)
        self.anchors = dict(self.anchors)

    def _push_break(self):
        self.sk.append(None)
        self.loc_v.append(-1)
        self.loc_p.append(-1)

    # -- summary ---------------------------------------------------------------
    def counts(self):
        groups = {e['group'] for e in self.editions.values()}
        works = {v.work for v in self.verses}
        per_work_groups = defaultdict(set)
        for e in self.editions.values():
            per_work_groups[e['work']].add(e['group'])
        return {
            'works': len(works), 'verses': len(self.verses),
            'editions': len(self.editions),
            'independent_sources': len(groups),
            'max_independent_per_work': max(
                (len(g) for g in per_work_groups.values()), default=0),
            'stream_tokens': sum(1 for x in self.sk if x is not None),
            'anchor_keys': len(self.anchors),
        }

    # -- quotations ------------------------------------------------------------
    def _match(self, q_sk, s):
        x = self.sk[s]
        return x is not None and (q_sk == x or q_sk in self.kq_alt.get(s, ()))

    def _positions(self, qsk, c, lo, hi):
        hyps = set()
        if c - 2 >= lo:
            for p in self.anchors.get((qsk[c - 2], qsk[c - 1]), ()):
                hyps.add(p + 2)
        if c + 2 <= hi:
            for p in self.anchors.get((qsk[c + 1], qsk[c + 2]), ()):
                hyps.add(p - 1)
        n = len(self.sk)
        out = []
        for s in hyps:
            if s < 0 or s >= n or self.sk[s] is None:
                continue
            left = 0
            while c - 1 - left >= lo and s - 1 - left >= 0 and \
                    self._match(qsk[c - 1 - left], s - 1 - left):
                left += 1
            right = 0
            while c + 1 + right <= hi and s + 1 + right < n and \
                    self._match(qsk[c + 1 + right], s + 1 + right):
                right += 1
            if left >= 1 and right >= 1 and left + right >= MIN_CONTEXT \
                    and self._distinct(s - left, s + right) >= MIN_DISTINCT:
                out.append((s, left + right))
        return out

    def _distinct(self, a, b):
        """Distinctive context words in stream[a..b], the word excluded."""
        n = 0
        for s in range(a, b + 1):
            x = self.sk[s]
            if x is not None and len(x) >= 3 and \
                    self.freq.get(x, 0) < COMMON_FREQ:
                n += 1
        return n

    def evidence(self, word, prev, nxt, snippet):
        """Evidence for one occurrence, or None when no verse aligns."""
        if not prev or not nxt or not snippet:
            return None
        q = TOKEN_RE.findall(clean(snippet))
        n = len(q)
        cands = [i for i in range(1, n - 1)
                 if q[i] == word and q[i - 1] == prev and q[i + 1] == nxt]
        if not cands:
            return None
        qsk = [skeleton(t) for t in q]
        found = {}
        for c in cands:
            # the snippet is a character slice: its edge tokens may be cut
            lo, hi = min(1, c - 1), max(n - 2, c + 1)
            for s, score in self._positions(qsk, c, lo, hi):
                found[s] = max(score, found.get(s, 0))
        if not found:
            return None
        ranked = sorted(found.values(), reverse=True)
        # a second position aligned about as well -> which verse is unknown
        if len(ranked) > 1 and ranked[0] - ranked[1] < AMBIGUITY_MARGIN:
            return Evidence(AMBIGUOUS)
        s = max(found, key=found.get)
        ev = self._judge(word, s)
        ev.aligned = found[s]
        return ev

    def _judge(self, word, s):
        verse, pos = self.verses[self.loc_v[s]], self.loc_p[s]
        if pos in verse.kq:
            return Evidence(QERE_KETIV, verse, pos)
        by = defaultdict(list)
        for g, toks in verse.readings.items():
            if toks[pos] is not None:
                by[toks[pos]].append(g)
        if not by:
            return Evidence(SINGLE_SOURCE, verse, pos)

        def eds(groups, reading):
            return tuple(e for g in groups for e in verse.members[g]
                         if self._reads(verse, e, pos, reading))
        if word in by:
            gs = tuple(sorted(by[word]))
            if len(by) > 1:
                kind = DISPUTED
            elif len(gs) >= MIN_INDEPENDENT:
                kind = MATCH
            else:
                kind = SINGLE_SOURCE
            return Evidence(kind, verse, pos, word, gs, eds(gs, word))
        if any(plene_equal(word, t) for t in by):
            r = max(by, key=lambda t: len(by[t]))
            gs = tuple(sorted(by[r]))
            return Evidence(PLENE, verse, pos, r, gs, eds(gs, r))
        ranked = sorted(by.items(), key=lambda kv: -len(kv[1]))
        r, gs = ranked[0][0], tuple(sorted(ranked[0][1]))
        total = sum(len(v) for v in by.values())
        if len(gs) < MIN_INDEPENDENT or 2 * len(gs) <= total:
            return Evidence(SINGLE_SOURCE if len(by) == 1 else DISPUTED,
                            verse, pos, r, gs, eds(gs, r))
        if len(word) < 2 or not within2(word, r):
            return Evidence(UNRELATED, verse, pos, r, gs, eds(gs, r))
        return Evidence(VARIANT, verse, pos, r, gs, eds(gs, r))

    def _reads(self, verse, eid, pos, reading):
        # an edition supports a reading when its group's folded reading is it;
        # folding already required every member of the group to agree
        return verse.readings[self.editions[eid]['group']][pos] == reading

    # -- edition disagreements -------------------------------------------------
    def edition_errors(self):
        """``(rows, stats)`` for the edition report.

        * ``tanach_edition_variant`` — one independent source against >=
          MIN_INDEPENDENT agreeing independent sources (needs >= 3 sources in
          all, so with two sources there are none);
        * ``tanach_edition_unresolved`` — every other disagreement, labelled
          with a `reason` (one_against_one, no_majority, intra_source, plene,
          qere_ketiv, unrelated). Informational: nobody is outvoted.

        Rows: (unit, word, canonical, snippet, evidence_json).

        Location: `unit` is the PRIMARY text's line id, which is also the
        ``lineId`` of every version's row for that verse; `snippet` is the
        minority edition's own text. When the minority is a version, `word`
        is in that version (named by `minority_editions` / `readings` in the
        evidence), not in the primary line, so an exported fix row names the
        right line but not the edition to correct. Unresolved rows carry no
        canonical reading (empty suggestion): they inform, nobody is
        outvoted. Neither can be applied by the fixer, which does not edit
        database books."""
        rows = []
        st = Counter()

        def snippet_of(verse, g):
            return ' '.join(t if t is not None else '_'
                            for t in verse.readings[g])

        def unresolved(verse, pos, word, reason, readings, extra=None):
            st[reason] += 1
            d = {'evidence_kind': EDITION_UNRESOLVED, 'reason': reason,
                 'ref': verse.ref, 'verse_line': verse.line_id,
                 'readings': readings}
            d.update(extra or {})
            rows.append((str(verse.line_id), word, '',
                         ' '.join(verse.toks),
                         json.dumps(d, ensure_ascii=False)))

        def labels(verse, by):
            return {t: {g: [self.edition_label(e) for e in verse.members[g]]
                        for g in gs} for t, gs in by.items()}

        for verse in self.verses:
            for pos in range(len(verse.toks)):
                by = defaultdict(list)
                for g, toks in verse.readings.items():
                    if toks[pos] is not None:
                        by[toks[pos]].append(g)
                if len(by) < 2:
                    continue
                st['disagreements'] += 1
                ranked = sorted(by.items(), key=lambda kv: -len(kv[1]))
                maj, maj_g = ranked[0]
                prim = verse.toks[pos]
                other = next((t for t, _ in ranked if t != prim), ranked[1][0])
                if pos in verse.kq:
                    unresolved(verse, pos, other, 'qere_ketiv',
                               labels(verse, by))
                    continue
                if len(maj_g) < MIN_INDEPENDENT or \
                        len(ranked[1][1]) == len(maj_g):
                    reason = 'one_against_one' if len(by) == 2 and \
                        all(len(v) == 1 for v in by.values()) \
                        else 'no_majority'
                    if any(plene_equal(maj, m) for m, _ in ranked[1:]):
                        st['unresolved_plene'] += 1
                    unresolved(verse, pos, other, reason, labels(verse, by))
                    continue
                for m, mg in ranked[1:]:
                    if plene_equal(m, maj):
                        unresolved(verse, pos, m, 'plene', labels(verse, by))
                        continue
                    if not within2(m, maj):
                        unresolved(verse, pos, m, 'unrelated',
                                   labels(verse, by))
                        continue
                    for g in mg:
                        st['reported'] += 1
                        ev = Evidence(EDITION_VARIANT, verse, pos, maj,
                                      tuple(sorted(maj_g)),
                                      tuple(e for x in maj_g
                                            for e in verse.members[x]))
                        d = ev.to_dict(self)
                        d['minority_source'] = g
                        d['minority_editions'] = [
                            self.edition_label(e) for e in verse.members[g]]
                        rows.append((str(verse.line_id), m, maj,
                                     snippet_of(verse, g),
                                     json.dumps(d, ensure_ascii=False)))
        for vi, pos, g, by in self.intra:
            verse = self.verses[vi]
            prim = verse.toks[pos]
            word = next((t for t in by if t != prim), next(iter(by)))
            unresolved(verse, pos, word, 'intra_source',
                       {t: [self.edition_label(e) for e in eids]
                        for t, eids in by.items()}, {'source': g})
        return rows, dict(st)


def build_index(db_path, stats=None):
    return TanachIndex.build(db_path, stats)


# ---------------------------------------------------------------------------
# report.db helpers
# ---------------------------------------------------------------------------

def evidence_row(index, ev, word, unit, doc, snippet, detector):
    """Row for ``tanach_evidence``. `detector` is (errtype, suggestion).

    The detector's suggestion is never replaced: a Tanach reading is stored
    next to it as an alternative, each with its own evidence."""
    errtype, sugg = detector
    _, kind = ev.decide(sugg)
    d = ev.to_dict(index)
    d['evidence_kind'] = kind
    alts = None
    if kind in (VARIANT, DISAGREES):
        alts = [{'suggestion': sugg or '', 'by': 'detector',
                 'errtype': errtype},
                {'suggestion': ev.reading, 'by': 'tanach', **d}]
        if sugg == ev.reading:
            alts[0]['agrees_with_tanach'] = True
    return (word, unit, doc, snippet, kind, d.get('ref', ''),
            ev.reading if alts else None,
            json.dumps(alts, ensure_ascii=False) if alts else None,
            json.dumps(d, ensure_ascii=False))


def write_evidence(con, rows):
    con.execute(EVIDENCE_SCHEMA)
    con.executemany('INSERT OR IGNORE INTO tanach_evidence '
                    'VALUES(?,?,?,?,?,?,?,?,?)', rows)


# enrich(): what occurrences_full takes from tanach_evidence. The suggestion
# column is always the detector's; the Tanach reading lives in `alternatives`.
ENRICH_COLS = ('te.evidence_kind AS evidence_kind, '
               'te.alternatives AS alternatives, '
               'te.reading AS tanach_reading')
ENRICH_JOIN = ('LEFT JOIN tanach_evidence te ON te.word = o.word '
               'AND te.unit = o.unit AND te.snippet = o.snippet')
