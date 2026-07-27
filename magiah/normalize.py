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

# Inline formatting tags are removed with no space so they never split a word
# (e.g. an enlarged first letter: <big>ב</big>ראשית). Structural tags become
# a space so adjacent blocks never merge into one word.
INLINE_TAG_RE = re.compile(r'</?(?:b|i|u|em|strong|big|small|font)(?:\s[^>]*)?>',
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
# `clean_mapped` therefore performs the SAME transform as `clean` while
# recording, for every character it emits, the raw index that produced it.
# Tag ranges are blanked in place (rather than removed) so that indices stay
# raw-aligned throughout:
_DROP = '\x00'      # inline tag: removed with NO space (never splits a word)
_SPACE = '\x01'     # structural tag: ONE space per tag (matches TAG_RE.sub(' '))
_KEEP = '\x02'      # continuation of a structural tag: contributes nothing

# `clean` decodes entities with html.unescape, which is more permissive than a
# strict entity regex (it also accepts some forms without a trailing ';'). To
# stay byte-identical we do not re-implement it: we find candidate runs, hand
# each to html.unescape, and keep only the ones it actually changes.
_ENTITY_RE = re.compile(r'&#?[0-9a-zA-Z]+;?')


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
    buf = list(text)
    if '<' in text:
        # inline first, then structural — the order `clean` uses
        for m in INLINE_TAG_RE.finditer(text):
            for i in range(m.start(), m.end()):
                buf[i] = _DROP
        masked = ''.join(buf)
        for m in TAG_RE.finditer(masked):
            if _DROP in masked[m.start():m.end()]:
                continue        # already consumed as an inline tag
            # one space PER TAG: mark the opening char, blank the remainder,
            # so '<blockquote><p>' yields two spaces exactly as
            # TAG_RE.sub(' ') does
            buf[m.start()] = _SPACE
            for i in range(m.start() + 1, m.end()):
                buf[i] = _KEEP
    # entity decoding, still position-aligned: the decoded text is attributed
    # to the '&' that started the entity
    ents = {}
    if '&' in text:
        masked = ''.join(buf)
        for m in _ENTITY_RE.finditer(masked):
            frag = m.group()
            if _DROP in frag or _SPACE in frag or _KEEP in frag:
                continue        # the candidate sat inside a tag
            dec = html.unescape(frag)
            if dec != frag:
                ents[m.start()] = (m.end(), dec)

    out, omap, emap = [], [], []
    i, n = 0, len(buf)
    while i < n:
        ent = ents.get(i)
        if ent is not None:
            end, dec = ent
            for ch in dec:
                rep = _STRIP.get(ord(ch), ch)
                if rep is None:
                    continue
                for c in rep:
                    out.append(c)
                    omap.append(i)
                    emap.append(end)
            i = end
            continue
        ch = buf[i]
        if ch == _DROP or ch == _KEEP:
            i += 1
            continue
        if ch == _SPACE:
            out.append(' ')
            omap.append(i)
            emap.append(i + 1)
            i += 1
            continue
        rep = _STRIP.get(ord(ch), ch)
        if rep is None:
            i += 1
            continue
        for c in rep:
            out.append(c)
            omap.append(i)
            emap.append(i + 1)
        i += 1
    return ''.join(out), omap, emap


def token_spans(text):
    """Tokens of `text` with the RAW slice each one occupies.

    Returns ``[(token, raw_start, raw_end), ...]`` with `raw_end` exclusive,
    in reading order; ``[t[0] for t in token_spans(x)] == tokenize(x)``.

    A token that spans markup (``ויל<big>ך</big>`` tokenizes to ``וילך``)
    yields ONE span covering the markup. Replacing that span removes the tags
    along with the word, which is correct for a whole-word correction — and
    callers that would rather not touch markup can detect it by looking for
    '<' inside the returned slice.
    """
    clean_text, omap, emap = clean_mapped(text)
    spans = []
    for m in TOKEN_RE.finditer(clean_text):
        spans.append((m.group(), omap[m.start()], emap[m.end() - 1]))
    return spans


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
