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
import os
import pickle
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

from .normalize import tokenize

EXTRA_PREFIX = 'extra_'
EXTRA_SUFFIX = '.pkl'
FORMAT = 1

TEXT_EXTS = ('.txt',)
HTML_EXTS = ('.html', '.htm', '.xhtml')
ZIP_EXTS = ('.docx', '.epub')
EXTS = TEXT_EXTS + HTML_EXTS + ZIP_EXTS

# a name is part of a file name: letters (any script), digits, '-' and '_'
_NAME_RE = re.compile(r'[^\W][\w-]{0,63}')
# html parts that are not text; everything else goes to normalize.clean,
# which already drops tags and decodes entities the way the corpus does
_DROP_RE = re.compile(r'<(script|style|head)\b.*?</\1\s*>|<!--.*?-->',
                      re.DOTALL | re.IGNORECASE)
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_DOCX_PARTS = ('word/document.xml', 'word/footnotes.xml',
               'word/endnotes.xml')


class ExtraLexiconError(Exception):
    """A user error: bad name, missing lexicon, unreadable or unsupported
    file. The message is Hebrew and meant to be shown as it is."""


def check_name(name):
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ExtraLexiconError(
            f'שם מילון נוסף לא תקין: {name!r}. מותר להשתמש באותיות, ספרות, '
            '"-" ו-"_" (עד 64 תווים).')
    return name


def extra_path(out_dir, name):
    return os.path.join(out_dir, EXTRA_PREFIX + check_name(name)
                        + EXTRA_SUFFIX)


# --- reading documents -----------------------------------------------------

def decode_text(data):
    """Text of a plain-text file: a BOM decides, then strict UTF-8, then
    Windows-1255 (Hebrew text saved by older Windows editors)."""
    for bom, enc in ((b'\xef\xbb\xbf', 'utf-8-sig'), (b'\xff\xfe', 'utf-16'),
                     (b'\xfe\xff', 'utf-16')):
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


def _html_text(text):
    return _DROP_RE.sub(' ', text)


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
                text = decode_text(f.read())
            return [_html_text(text) if ext in HTML_EXTS else text]
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if ext == '.docx':
                return ['\n'.join(_docx_lines(z.read(n)))
                        for n in _DOCX_PARTS if n in names]
            # epub: every (x)html content document; order does not matter
            # for a word count
            return [_html_text(z.read(n).decode('utf-8', errors='replace'))
                    for n in sorted(names) if n.lower().endswith(HTML_EXTS)]
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as e:
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
    for n, fp in enumerate(files, 1):
        for text in read_document(fp):
            counts.update(tokenize(text))
        if n % 100 == 0:
            say(f'[extra] {n:,}/{len(files):,} קבצים')
    rec = {
        'format': FORMAT,
        'name': name,
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
    return sorted(n[len(EXTRA_PREFIX):-len(EXTRA_SUFFIX)] for n in names
                  if n.startswith(EXTRA_PREFIX) and n.endswith(EXTRA_SUFFIX))


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
               if rec.get('skipped_pdf') else ''))


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
