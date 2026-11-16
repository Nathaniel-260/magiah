# -*- coding: utf-8 -*-
"""Extra lexicons: word counts from the user's own documents.

The corpus is magiah's dictionary, so a book written in another register
(modern Hebrew, a technical field the library does not cover) gets many of
its ordinary words flagged as rare. An extra lexicon is a word count built
from documents the user chooses (`magiah extra --name modern --from DIR`);
selecting it for a scan (`--extra-lexicon modern`) makes every word it
holds at least ``cfg.extra_min`` times a known word.

It works like the whitelist: suppression only. An extra lexicon never
creates a finding and never becomes a correction candidate -- the corpus
stays the only source of suggestions.

Each lexicon is one pickle beside lexicon.pkl (``extra_<name>.pkl``): the
counts and a record of what they were built from. Building a name again
replaces it. Readers use the standard library only: .txt (UTF-8, UTF-16
with a BOM, or Windows-1255), .html/.htm/.xhtml, .docx and .epub. PDF is
not read (that needs a third-party library): a PDF named explicitly is
refused, and PDFs inside a folder are skipped and counted.

The text goes through :func:`magiah.normalize.tokenize`, the tokenizer of
the corpus passes, so a word in an extra lexicon matches the same word in a
scanned book.
"""
import codecs
import os
import pickle
import re
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter

from .normalize import tokenize

EXTRA_PREFIX = 'extra_'
EXTRA_SUFFIX = '.pkl'
FORMAT = 1

TEXT_EXTS = ('.txt',)
HTML_EXTS = ('.html', '.htm', '.xhtml')
ZIP_EXTS = ('.docx', '.epub')
EXTS = TEXT_EXTS + HTML_EXTS + ZIP_EXTS

# Decompression-bomb guard: the most bytes read from one file. For a zip
# (.docx/.epub) it is the total of all the members read from it, so it is
# also the cap on one member. Real books are a few MB of text.
MAX_BYTES = 200 * 1024 * 1024

# a name is part of a file name: letters (any script), digits, '-' and '_'
_NAME_RE = re.compile(r'[^\W][\w-]{0,63}')
# html parts that are not text; everything else goes to normalize.clean,
# which already drops tags and decodes entities the way the corpus does.
# Found by a forward scan (see _html_text), not one lazy regex: an unclosed
# <script made that regex quadratic.
_HTML_START_RE = re.compile(r'<!--|<(script|style|head)\b', re.IGNORECASE)
_HTML_CLOSE_RE = {t: re.compile(r'</' + t + r'\s*>', re.IGNORECASE)
                  for t in ('script', 'style', 'head')}
# the encoding an xml/html document declares about itself
_DECL_RE = re.compile(
    rb"""<\?xml[^>]{0,200}?encoding\s*=\s*["']([A-Za-z0-9._:-]+)"""
    rb"""|<meta[^>]{0,300}?charset\s*=\s*["']?([A-Za-z0-9._:-]+)""",
    re.IGNORECASE)
_BOMS = ((b'\xef\xbb\xbf', 'utf-8-sig'), (b'\xff\xfe', 'utf-16'),
         (b'\xfe\xff', 'utf-16'))
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_DOCX_PARTS = ('word/document.xml', 'word/footnotes.xml',
               'word/endnotes.xml')


class ExtraLexiconError(Exception):
    """A user error: bad name, missing lexicon, unreadable or unsupported
    file. The message is Hebrew and meant to be shown as it is."""


def norm_name(name):
    """The name as it is stored. Windows file names are case-insensitive,
    so "Modern" and "modern" would be one file: names are lower-cased, and
    they are one lexicon everywhere (build, scan, remove, list)."""
    return check_name(name).lower()


def check_name(name):
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ExtraLexiconError(
            f'שם מילון נוסף לא תקין: {name!r}. מותר להשתמש באותיות, ספרות, '
            '"-" ו-"_" (עד 64 תווים).')
    return name


def extra_path(out_dir, name):
    return os.path.join(out_dir, EXTRA_PREFIX + norm_name(name)
                        + EXTRA_SUFFIX)


# --- reading documents -----------------------------------------------------

def decode_text(data):
    """Text of a plain-text file: a BOM decides, then strict UTF-8, then
    Windows-1255 (Hebrew text saved by older Windows editors)."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data.decode(enc, errors='replace')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('cp1255')
    except UnicodeDecodeError:
        return data.decode('utf-8', errors='replace')


def decode_markup(data):
    """Text of an html/xhtml document: the encoding the document declares
    (<?xml encoding=...?> or <meta charset=...>) when it decodes the bytes,
    otherwise as :func:`decode_text` (BOM, UTF-8, Windows-1255)."""
    if not any(data.startswith(b) for b, _ in _BOMS):
        m = _DECL_RE.search(data[:2048])
        if m:
            label = (m.group(1) or m.group(2)).decode('ascii')
            try:
                name = codecs.lookup(label).name
            except LookupError:
                name = None
            # without a BOM a declared UTF-16/32 is a wrong declaration
            if name and not name.startswith(('utf-16', 'utf-32')):
                try:
                    return data.decode(name)
                except (UnicodeDecodeError, LookupError):
                    pass
    return decode_text(data)


def _html_text(text):
    """`text` without script/style/head elements and comments, in one
    forward pass (linear time). An element or comment that is never closed
    runs to the end of the text, as a browser reads it."""
    out, pos = [], 0
    while True:
        m = _HTML_START_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            return ''.join(out)
        out.append(text[pos:m.start()])
        out.append(' ')
        if m.group(1) is None:
            end = text.find('-->', m.end())
            pos = end + 3 if end >= 0 else len(text)
        else:
            c = _HTML_CLOSE_RE[m.group(1).lower()].search(text, m.end())
            pos = c.end() if c else len(text)


def _too_big(path, member=None):
    mb = MAX_BYTES // (1024 * 1024)
    return ExtraLexiconError(
        f'הקובץ גדול מדי לקריאה: {path}'
        + (f' (החלק {member})' if member else '') + '\n'
        f'המגבלה היא {mb:,} מ"ב של טקסט לכל קובץ (הגנה מפני קובץ דחוס '
        'בצורה חשודה). שום דבר לא נשמר. יש להוציא את הקובץ ולבנות שוב.')


def _read_capped(f, path):
    data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise _too_big(path)
    return data


def _docx_lines(xml_bytes):
    """One line per paragraph (<w:p>). Runs inside a paragraph are joined
    with no space: Word splits a word into several runs when only part of
    it is formatted, and it must stay one word."""
    lines = []

    def walk(el, buf):
        for ch in el:
            if ch.tag == _W + 'p':
                own = []
                walk(ch, own)
                lines.append(''.join(own))
            elif ch.tag == _W + 't':
                buf.append(ch.text or '')
            elif ch.tag in (_W + 'tab', _W + 'br', _W + 'cr'):
                buf.append(' ')
            else:
                walk(ch, buf)

    walk(ET.fromstring(xml_bytes), [])
    return lines


def read_document(path):
    """The text of one document, as a list of strings (one or more)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pdf':
        raise ExtraLexiconError(
            f'קובצי PDF אינם נתמכים במילון נוסף: {path}\n'
            'קריאת PDF דורשת ספרייה חיצונית, והמגיה משתמשת בספריית התקן '
            'של Python בלבד. אפשר לשמור את הקובץ כטקסט (txt) או כ-docx '
            'ולבנות ממנו.')
    if ext not in EXTS:
        raise ExtraLexiconError(
            f'סוג קובץ לא נתמך במילון נוסף: {path}\n'
            f'הסוגים הנתמכים: {", ".join(EXTS)}')
    try:
        if ext in TEXT_EXTS or ext in HTML_EXTS:
            with open(path, 'rb') as f:
                data = _read_capped(f, path)
            if ext in HTML_EXTS:
                return [_html_text(decode_markup(data))]
            return [decode_text(data)]
        with zipfile.ZipFile(path) as z:
            remaining = MAX_BYTES

            def member(n):
                # the declared size can lie: read at most what is left
                nonlocal remaining
                if z.getinfo(n).file_size > remaining:
                    raise _too_big(path, n)
                with z.open(n) as f:
                    data = f.read(remaining + 1)
                if len(data) > remaining:
                    raise _too_big(path, n)
                remaining -= len(data)
                return data

            names = z.namelist()
            if ext == '.docx':
                return ['\n'.join(_docx_lines(member(n)))
                        for n in _DOCX_PARTS if n in names]
            # epub: every (x)html content document; order does not matter
            # for a word count. Each is decoded as it declares itself.
            return [_html_text(decode_markup(member(n)))
                    for n in sorted(names) if n.lower().endswith(HTML_EXTS)]
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError,
            zlib.error, EOFError, NotImplementedError, RuntimeError) as e:
        raise ExtraLexiconError(
            f'לא ניתן לקרוא את הקובץ: {path} ({e})\n'
            'שום דבר לא נשמר. יש לתקן או להוציא את הקובץ ולבנות שוב.') from e


def collect_files(paths):
    """``(files, skipped_pdf)``: the documents named, and those found in
    the folders named (with their subfolders), in a stable order."""
    files, skipped_pdf = [], 0
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs.sort()
                for n in sorted(names):
                    ext = os.path.splitext(n)[1].lower()
                    if ext in EXTS:
                        files.append(os.path.join(root, n))
                    elif ext == '.pdf':
                        skipped_pdf += 1
        elif os.path.isfile(p):
            if os.path.splitext(p)[1].lower() not in EXTS:
                read_document(p)            # raises the Hebrew reason
            files.append(p)
        else:
            raise ExtraLexiconError(f'הקובץ או התיקייה לא נמצאו: {p}')
    return files, skipped_pdf


# --- build / list / remove -------------------------------------------------

def build(out_dir, name, paths, progress=None):
    """Count the words of every document under `paths` into
    ``extra_<name>.pkl``, replacing a lexicon of that name. Nothing is
    written unless every document was read."""
    say = progress or (lambda *_: None)
    path = extra_path(out_dir, name)
    if not paths:
        raise ExtraLexiconError(
            'יש לציין קבצים או תיקייה לבניית המילון הנוסף (‎--from‎).')
    files, skipped_pdf = collect_files(paths)
    if not files:
        raise ExtraLexiconError(
            'לא נמצאו קבצים נתמכים לבניית המילון הנוסף '
            f'({", ".join(EXTS)})'
            + (f'; דולגו {skipped_pdf:,} קובצי PDF' if skipped_pdf else '')
            + '.')
    counts = Counter()
    empty = []
    for n, fp in enumerate(files, 1):
        found = 0
        for text in read_document(fp):
            toks = tokenize(text)
            found += len(toks)
            counts.update(toks)
        if not found:
            empty.append(fp)
            say(f'[extra] אזהרה: לא נמצאו מילים בעברית בקובץ: {fp}')
        if n % 100 == 0:
            say(f'[extra] {n:,}/{len(files):,} קבצים')
    if not counts:
        raise ExtraLexiconError(
            f'לא נמצאו מילים בעברית באף אחד מ-{len(files):,} הקבצים, ולכן '
            'לא נבנה מילון נוסף. שום דבר לא נשמר.')
    rec = {
        'format': FORMAT,
        'name': norm_name(name),
        'empty_files': empty,
        'built': time.strftime('%Y-%m-%d %H:%M'),
        'sources': [os.path.abspath(p) for p in paths],
        'files': len(files),
        'skipped_pdf': skipped_pdf,
        'tokens': sum(counts.values()),
        'counts': dict(counts),
    }
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'wb') as f:
            pickle.dump(rec, f, protocol=4)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return rec


def load(out_dir, name):
    path = extra_path(out_dir, name)
    if not os.path.exists(path):
        have = list_names(out_dir)
        raise ExtraLexiconError(
            f'המילון הנוסף "{name}" לא נמצא ({path}).\n'
            + ('המילונים הנוספים הקיימים: ' + ', '.join(have) if have else
               'אין מילונים נוספים. בונים אחד כך: '
               f'magiah extra --name {name} --from <תיקייה או קבצים>'))
    try:
        with open(path, 'rb') as f:
            rec = pickle.load(f)
        if not isinstance(rec, dict) or rec.get('format') != FORMAT:
            raise ValueError('unknown format')
    except Exception as e:                          # noqa: BLE001
        raise ExtraLexiconError(
            f'לא ניתן לקרוא את המילון הנוסף {path} ({e}). '
            'יש לבנות אותו מחדש.') from e
    return rec


def list_names(out_dir):
    try:
        names = os.listdir(out_dir)
    except OSError:
        return []
    found = (n[len(EXTRA_PREFIX):-len(EXTRA_SUFFIX)] for n in names
             if n.startswith(EXTRA_PREFIX) and n.endswith(EXTRA_SUFFIX))
    return sorted(n for n in found if n == n.lower())


def remove(out_dir, name):
    path = extra_path(out_dir, name)
    if not os.path.exists(path):
        raise ExtraLexiconError(f'המילון הנוסף "{name}" לא נמצא ({path}).')
    os.remove(path)


def summary(rec, min_count):
    known = sum(1 for c in rec['counts'].values() if c >= min_count)
    return (f"{rec['name']}: {rec['files']:,} קבצים, {rec['tokens']:,} "
            f"מילים, {len(rec['counts']):,} צורות שונות ({known:,} מהן "
            f"לפחות {min_count} פעמים) — נבנה {rec['built']}"
            + (f", דולגו {rec['skipped_pdf']:,} קובצי PDF"
               if rec.get('skipped_pdf') else '')
            + (f", אזהרה: {len(rec['empty_files']):,} קבצים בלי מילים "
               'בעברית' if rec.get('empty_files') else ''))


# --- use in a scan ---------------------------------------------------------

def known_words(cfg, out_dir, progress=None):
    """Words of the selected extra lexicons (``cfg.extra_lexicon``) seen at
    least ``cfg.extra_min`` times in one of them. Empty when none is
    selected; a selected lexicon that is missing is an error, never an
    empty set."""
    names = tuple(getattr(cfg, 'extra_lexicon', ()) or ())
    words = set()
    if not names:
        return words
    min_count = max(1, int(cfg.extra_min))
    for name in names:
        rec = load(out_dir, name)
        own = {w for w, c in rec['counts'].items() if c >= min_count}
        words |= own
        if progress:
            progress(f'[extra] מילון נוסף "{name}": {len(own):,} מילים '
                     f'מוכרות (לפחות {min_count} פעמים)')
    return words
