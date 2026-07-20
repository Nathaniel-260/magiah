# -*- coding: utf-8 -*-
"""Text normalization and tokenization for Hebrew/Aramaic corpora.

Strips HTML markup, nikud (vowel points) and teamim (cantillation marks),
and tokenizes into Hebrew words. Abbreviation tokens (containing geresh or
gershayim, e.g. רמב"ם) are preserved as single tokens.
"""
import html
import re

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
    """Strip HTML tags, decode entities, remove nikud/teamim."""
    if '<' in text:
        text = INLINE_TAG_RE.sub('', text)
        text = TAG_RE.sub(' ', text)
    if '&' in text:
        text = html.unescape(text)
    return text.translate(_STRIP)


def tokenize(text):
    """Hebrew tokens of cleaned text."""
    return TOKEN_RE.findall(clean(text))


def is_abbrev(token):
    """True for abbreviation tokens such as רמב"ם or ר' (contain a quote)."""
    return '"' in token or "'" in token
