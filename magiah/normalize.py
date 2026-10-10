# -*- coding: utf-8 -*-
"""Text normalization and tokenization for Hebrew/Aramaic corpora.

Strips HTML markup, nikud (vowel points) and teamim (cantillation marks),
and tokenizes into Hebrew words. Abbreviation tokens (containing geresh or
gershayim, e.g. רמב"ם) are preserved as single tokens.
"""
import html
import re
import unicodedata

# --- character stripping table ---------------------------------------------
_STRIP = {}
for _cp in range(0x0591, 0x05C8):        # nikud + teamim
    _STRIP[_cp] = None
_STRIP[0x05BE] = ' '                     # maqaf (Hebrew hyphen) -> space
for _cp in (0x05C0, 0x05C3, 0x05C6):     # paseq, sof pasuq, inverted nun
    _STRIP[_cp] = ' '
for _cp in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
            0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0xFEFF, 0x034F):
    _STRIP[_cp] = None                   # zero-width / bidi controls
for _cp in (0x0307, 0x0323):
    _STRIP[_cp] = None                   # combining dots (Judeo-Arabic ג̇ ד̇ כ̇)
# Curly/typographic quotes act as geresh/gershayim in many printings (לבבכ’)
for _cp in (0x2018, 0x2019, 0x05F3):
    _STRIP[_cp] = "'"
for _cp in (0x201C, 0x201D, 0x05F4):
    _STRIP[_cp] = '"'
# Alphabetic presentation forms (U+FB1D-U+FB4F): precomposed letter+point
# glyphs (וּ שׁ הּ) used by nikud-heavy books. Decompose to the base letter so
# words like טמנוּ do not break in the middle.
for _cp in range(0xFB1D, 0xFB50):
    _base = ''.join(c for c in unicodedata.normalize('NFKD', chr(_cp))
                    if 'א' <= c <= 'ת')
    if _base:
        _STRIP[_cp] = _base
_STRIP[0x00A0] = ' '
_STRIP[0x05F3] = "'"                     # geresh
_STRIP[0x05F4] = '"'                     # gershayim
# Yiddish ligatures (װ ױ ײ) are single code points outside [א-ת]; spelled out
# so a word like צװײ stays one token instead of breaking at the ligature.
_STRIP[0x05F0] = 'וו'
_STRIP[0x05F1] = 'וי'
_STRIP[0x05F2] = 'יי'

# Combining marks that belong to the letter before them (nikud, teamim, the
# Judeo-Arabic dots, CGJ). Maqaf, paseq, sof pasuq and nun hafukha are in the
# same block but separate words, so they are not marks.
MARKS = frozenset(chr(c) for c in range(0x0591, 0x05C8)
                  if c not in (0x05BE, 0x05C0, 0x05C3, 0x05C6)) \
    | frozenset((chr(0x0307), chr(0x0323), chr(0x034F)))
_JOINERS = frozenset((chr(0x200C), chr(0x200D)))      # ZWNJ, ZWJ

# Inline formatting tags are removed with no space so they never split a word
# (e.g. an enlarged first letter: <big>ב</big>ראשית). Structural tags become
# a space so adjacent blocks never merge into one word.
# `span` is inline: it only styles, so ה<span>ע</span>ולם is one word. `sup`
# is NOT: it carries footnote letters that would glue onto the next word.
INLINE_TAG_RE = re.compile(
    r'</?(?:b|i|u|em|strong|big|small|font|span)(?:\s[^>]*)?>',
    re.IGNORECASE)
TAG_RE = re.compile(r'<[^>]*>')
TOKEN_RE = re.compile(r'[א-ת]+(?:["\'][א-ת]+)*')

FINALS = 'םןץףך'
# Letters that legitimately stack as prefixes (Hebrew ו,ה,ב,ל,מ,ש,כ + Aramaic ד,א)
PREFIX_LETTERS = 'ובהלמשכדא'
# Letters that end legitimate inflected forms (plural/possessive/feminine)
SUFFIX_LETTERS = 'הויםןת'
TO_FINAL = {'כ': 'ך', 'מ': 'ם', 'נ': 'ן', 'פ': 'ף', 'צ': 'ץ'}
FROM_FINAL = {v: k for k, v in TO_FINAL.items()}

# Letter pairs that are visually similar or commonly confused in OCR/typing.
_CONF_PAIRS = [('ב', 'כ'), ('כ', 'נ'), ('ג', 'נ'), ('ד', 'ר'), ('ד', 'ך'),
               ('ר', 'ך'), ('ה', 'ח'), ('ה', 'ת'), ('ח', 'ת'), ('ו', 'י'),
               ('ו', 'ז'), ('ו', 'ן'), ('י', 'ן'), ('ם', 'ס'), ('ע', 'צ'),
               ('ט', 'מ'), ('ש', 'ת'), ('ז', 'י')]
CONFUSABLE = set()
for _a, _b in _CONF_PAIRS:
    CONFUSABLE.add((_a, _b))
    CONFUSABLE.add((_b, _a))
    _fa, _fb = TO_FINAL.get(_a), TO_FINAL.get(_b)
    if _fa:
        CONFUSABLE.add((_fa, _b))
        CONFUSABLE.add((_b, _fa))
    if _fb:
        CONFUSABLE.add((_a, _fb))
        CONFUSABLE.add((_fb, _a))


def clean(text):
    """Strip HTML tags, decode entities, remove nikud/teamim.

    NOTE: :func:`clean_mapped` reproduces this transform character by
    character in order to report offsets. The two MUST stay in sync — every
    change here needs the same change there, and the test suite asserts
    ``clean_mapped(t)[0] == clean(t)``.
    """
    if '<' in text:
        text = INLINE_TAG_RE.sub('', text)
        text = TAG_RE.sub(' ', text)
    if '&' in text:
        text = html.unescape(text)
    return text.translate(_STRIP)


def tokenize(text):
    """Hebrew tokens of cleaned text."""
    return TOKEN_RE.findall(clean(text))


# ---------------------------------------------------------------------------
# Offset-preserving variant, for editing the ORIGINAL file
# ---------------------------------------------------------------------------
# `clean` is lossy about position: it deletes nikud, drops inline tags and
# rewrites entities, so a token's index in clean(text) says nothing about where
# that word sits in the raw line (measured: 35 raw chars -> 22 clean ones). The
# fixer must write back into the raw file, so it needs the inverse map.
#
# `clean_mapped` therefore performs the SAME transform as `clean`, stage by
# stage on real strings, while recording for every character it emits the raw
# span that produced it. No character is reserved as a marker: a line may hold
# any code point (NUL, U+0001 and U+0002 do occur in library files), and a
# sentinel that collides with one silently desynchronizes the map from
# `clean`, which turns into refusals at best.

# `clean` decodes entities with html.unescape, which is more permissive than a
# strict entity regex (it also accepts some forms without a trailing ';'). To
# stay byte-identical we do not re-implement it: we find candidate runs, hand
# each to html.unescape, and keep only the ones it actually changes.
_ENTITY_RE = re.compile(r'&#?[0-9a-zA-Z]+;?')

# every character `clean` translates (removes, replaces or expands); runs of
# other characters are copied in one slice
_SPECIAL_RE = re.compile('[%s]' % ''.join(
    re.escape(chr(c)) for c in sorted(_STRIP)))


def _strip_mapped(s, a, b):
    """Translate `s` through _STRIP; ``a[i]``/``b[i]`` are the raw bounds of
    ``s[i]`` (None: ``s`` IS the raw text, so they are ``i``/``i + 1``)."""
    out, om, em = [], [], []
    pos = 0
    for m in _SPECIAL_RE.finditer(s):
        i = m.start()
        if i > pos:
            out.append(s[pos:i])
            if a is None:
                om.extend(range(pos, i))
                em.extend(range(pos + 1, i + 1))
            else:
                om.extend(a[pos:i])
                em.extend(b[pos:i])
        rep = _STRIP[ord(s[i])]
        if rep:
            out.append(rep)
            om.extend([i if a is None else a[i]] * len(rep))
            em.extend([i + 1 if a is None else b[i]] * len(rep))
        pos = i + 1
    if pos < len(s):
        out.append(s[pos:])
        if a is None:
            om.extend(range(pos, len(s)))
            em.extend(range(pos + 1, len(s) + 1))
        else:
            om.extend(a[pos:])
            em.extend(b[pos:])
    return ''.join(out), om, em


def clean_mapped(text):
    """:func:`clean` plus a map from each output character back to the input.

    Returns ``(clean_text, offmap, endmap)``. ``offmap[i]`` is the index in
    `text` where the raw unit behind ``clean_text[i]`` STARTS, and
    ``endmap[i]`` is the index just past where it ENDS. For an ordinary
    character the two are ``i`` and ``i + 1``; when one raw unit expands to
    several output characters (a presentation form decomposing to its base
    letters, ``&#1488;`` decoding to a letter) every produced character
    carries the whole unit's bounds. Slicing
    ``text[offmap[a]:endmap[b]]`` therefore always covers whole raw units —
    which is what makes a replacement safe.

    Invariant (asserted by the tests): ``clean_mapped(t)[0] == clean(t)``.
    """
    if '<' not in text and '&' not in text:
        return _strip_mapped(text, None, None)
    s, a, b = text, list(range(len(text))), list(range(1, len(text) + 1))
    if '<' in text:
        # inline tags go with NO space, then structural tags become ONE space
        # each — the order and the regexes `clean` uses, on the same strings
        for rx, rep in ((INLINE_TAG_RE, ''), (TAG_RE, ' ')):
            parts, na, nb, pos = [], [], [], 0
            for m in rx.finditer(s):
                parts.append(s[pos:m.start()])
                na.extend(a[pos:m.start()])
                nb.extend(b[pos:m.start()])
                if rep:
                    parts.append(rep)
                    na.append(a[m.start()])
                    nb.append(a[m.start()] + 1)
                pos = m.end()
            parts.append(s[pos:])
            na.extend(a[pos:])
            nb.extend(b[pos:])
            s, a, b = ''.join(parts), na, nb
    if '&' in s:
        # the decoded text is attributed to the whole raw entity
        parts, na, nb, pos = [], [], [], 0
        for m in _ENTITY_RE.finditer(s):
            dec = html.unescape(m.group())
            if dec == m.group():
                continue
            parts.append(s[pos:m.start()])
            na.extend(a[pos:m.start()])
            nb.extend(b[pos:m.start()])
            parts.append(dec)
            na.extend([a[m.start()]] * len(dec))
            nb.extend([b[m.end() - 1]] * len(dec))
            pos = m.end()
        parts.append(s[pos:])
        na.extend(a[pos:])
        nb.extend(b[pos:])
        s, a, b = ''.join(parts), na, nb
    return _strip_mapped(s, a, b)


def token_spans(text):
    """Tokens of `text` with the RAW slice each one occupies.

    Returns ``[(token, raw_start, raw_end), ...]`` with `raw_end` exclusive,
    in reading order; ``[t[0] for t in token_spans(x)] == tokenize(x)``.

    A token that spans markup (``ויל<big>ך</big>`` tokenizes to ``וילך``)
    yields ONE span covering the markup. Replacing that span removes the tags
    along with the word, which is correct for a whole-word correction — and
    callers that would rather not touch markup can detect it by looking for
    '<' inside the returned slice.

    The span also swallows the combining marks after the last letter (the
    dagesh in ``אמרוּ``): they emit nothing in the clean text, so without this
    a replacement would leave them orphaned on the following character.
    """
    return [s[:3] for s in token_spans_full(text)[1]]


def mark_end(text, j):
    """Index past the marks that follow ``text[j-1]`` (joiners only between
    marks, never trailing).

    A mark still belongs to the letter when it is written as an entity
    (``אמרו&#1468;``) or sits behind inline formatting tags, which clean()
    drops without a space (``<b>אמרו</b>ּ``). The span takes those along:
    left outside it, the mark would end up on the replacement's last letter.
    (The fixer then refuses a span holding a tag, and asks for a vocalized
    correction for one holding a mark.)
    """
    n, k = len(text), j
    while k < n:
        ch = text[k]
        if ch in MARKS or ch in _JOINERS:
            k += 1
            if ch in MARKS:
                j = k
        elif ch == '&':
            m = _ENTITY_RE.match(text, k)
            dec = html.unescape(m.group()) if m else ''
            if not m or dec == m.group() or not all(
                    c in MARKS or c in _JOINERS for c in dec):
                break
            k = m.end()
            if any(c in MARKS for c in dec):
                j = k
        elif ch == '<':
            m = INLINE_TAG_RE.match(text, k)
            if not m:
                break
            k = m.end()            # taken only if a mark follows it
        else:
            break
    return j


def token_spans_full(text):
    """``(clean_text, [(token, raw_start, raw_end, clean_start, clean_end)])``
    — :func:`token_spans` plus each token's position in the CLEAN text, which
    is the coordinate system the scanner's snippets were cut in."""
    clean_text, omap, emap = clean_mapped(text)
    spans = []
    for m in TOKEN_RE.finditer(clean_text):
        spans.append((m.group(), omap[m.start()],
                      mark_end(text, emap[m.end() - 1]),
                      m.start(), m.end()))
    return clean_text, spans


def is_mark(ch):
    return ch in MARKS


def has_marks(text):
    """True when `text` carries nikud/teamim, as marks (also written as
    entities, ``&#1468;``) or presentation forms (U+FB1D-FB4E, except the
    wide letters FB20-FB29 that carry none)."""
    if '&' in text:
        text = html.unescape(text)
    for ch in text:
        if ch in MARKS:
            return True
        cp = ord(ch)
        if 0xFB1D <= cp <= 0xFB4E and not 0xFB20 <= cp <= 0xFB29:
            return True
    return False


def phrase_spans(text, phrase):
    """Raw spans of `phrase`, which may cover SEVERAL tokens.

    The ``extra_space`` family reports a split word as one finding whose
    ``word`` contains a space (``'ל קוחה'`` -> ``'לקוחה'``), so matching a
    single token is not enough: a consecutive run of tokens is matched and the
    span runs from the first token's start to the last token's end, which
    swallows the erroneous space between them.
    """
    want = phrase.split()
    if not want:
        return []
    spans = token_spans(text)
    if len(want) == 1:
        return [s for s in spans if s[0] == want[0]]
    out = []
    for i in range(len(spans) - len(want) + 1):
        window = spans[i:i + len(want)]
        if [w[0] for w in window] == want:
            out.append((phrase, window[0][1], window[-1][2]))
    return out


def is_abbrev(token):
    """True for abbreviation tokens such as רמב"ם or ר' (contain a quote)."""
    return '"' in token or "'" in token
