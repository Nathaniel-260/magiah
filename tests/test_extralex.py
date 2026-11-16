# -*- coding: utf-8 -*-
"""Extra lexicons (magiah.extralex): built from the user's own documents,
selected per scan, suppression only. Every word here is invented."""
import contextlib
import io
import json
import os
import pickle
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from magiah import book_scan, cli, core, extralex
from magiah.config import Config
from magiah.webui import scanner

TUNE = ['--workers', '1', '--n-chunks', '1']
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

# invented words: TYPO is one letter away from COMMON, which the library
# repeats often enough to make it a confident correction
COMMON, TYPO = 'גלפרון', 'גלפדון'


def write(path, text, encoding='utf-8'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding=encoding) as f:
        f.write(text)


def make_docx(path, paragraphs):
    """A minimal .docx: each paragraph a list of runs (strings)."""
    body = ''.join(
        '<w:p>' + ''.join(f'<w:r><w:t>{r}</w:t></w:r>' for r in runs)
        + '</w:p>' for runs in paragraphs)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document '
           f'xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('word/document.xml', xml)


def make_epub(path, chapters):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        for i, text in enumerate(chapters):
            z.writestr(f'OEBPS/ch{i}.xhtml',
                       f'<html><head><title>כותרתון</title></head>'
                       f'<body><p>{text}</p></body></html>')


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='magiah_extra_t_')
        self.root = self._tmp.name
        self.out = os.path.join(self.root, 'out')
        os.makedirs(self.out)

    def tearDown(self):
        self._tmp.cleanup()

    def counts(self, *paths):
        return extralex.build(self.out, 't', list(paths))['counts']


class Readers(_Tmp):
    def test_txt_encodings(self):
        for enc in ('utf-8', 'utf-8-sig', 'cp1255', 'utf-16'):
            with self.subTest(encoding=enc):
                p = os.path.join(self.root, enc, 'a.txt')
                write(p, 'שמרפוט וזלנקה שמרפוט\n', enc)
                c = self.counts(p)
                self.assertEqual(c.get('שמרפוט'), 2)
                self.assertEqual(c.get('וזלנקה'), 1)

    def test_text_is_tokenized_like_the_corpus(self):
        p = os.path.join(self.root, 'a.txt')
        # nikud is dropped, a maqaf separates, an abbreviation stays whole
        write(p, 'שְׁמַרְפּוּט טרבל־קנדו רזב"ם\n')
        c = self.counts(p)
        self.assertEqual(set(c), {'שמרפוט', 'טרבל', 'קנדו', 'רזב"ם'})

    def test_html(self):
        p = os.path.join(self.root, 'a.html')
        write(p, '<html><head><title>כותרתון</title><style>p{}</style>'
                 '</head><body><script>var x="סקריפטון";</script>'
                 '<p>שמ<b>רפ</b>וט</p><p>וזלנקה</p><!-- הערתון --></body>'
                 '</html>')
        c = self.counts(p)
        self.assertEqual(set(c), {'שמרפוט', 'וזלנקה'})

    def test_docx_word_split_between_runs_stays_one_word(self):
        p = os.path.join(self.root, 'a.docx')
        make_docx(p, [['שמר', 'פוט'], ['וזלנקה'], ['קנדו']])
        c = self.counts(p)
        self.assertEqual(set(c), {'שמרפוט', 'וזלנקה', 'קנדו'})

    def test_epub(self):
        p = os.path.join(self.root, 'a.epub')
        make_epub(p, ['שמרפוט וזלנקה', 'שמרפוט'])
        c = self.counts(p)
        self.assertEqual(c, {'שמרפוט': 2, 'וזלנקה': 1})

    def test_folder_with_subfolders_skips_pdf(self):
        d = os.path.join(self.root, 'docs')
        write(os.path.join(d, 'a.txt'), 'שמרפוט')
        write(os.path.join(d, 'sub', 'deeper', 'b.txt'), 'וזלנקה')
        make_docx(os.path.join(d, 'sub', 'c.docx'), [['קנדו']])
        write(os.path.join(d, 'scan.pdf'), '%PDF-1.4')
        write(os.path.join(d, 'notes.md'), 'טרבל')     # not a supported type
        rec = extralex.build(self.out, 't', [d])
        self.assertEqual(set(rec['counts']), {'שמרפוט', 'וזלנקה', 'קנדו'})
        self.assertEqual((rec['files'], rec['skipped_pdf']), (3, 1))

    def test_files_and_folders_together(self):
        a = os.path.join(self.root, 'a.txt')
        write(a, 'שמרפוט')
        d = os.path.join(self.root, 'd')
        write(os.path.join(d, 'b.txt'), 'שמרפוט')
        self.assertEqual(self.counts(a, d), {'שמרפוט': 2})

    def test_pdf_named_explicitly_is_refused(self):
        p = os.path.join(self.root, 'a.pdf')
        write(p, '%PDF-1.4')
        with self.assertRaises(extralex.ExtraLexiconError) as cm:
            extralex.build(self.out, 't', [p])
        self.assertIn('PDF', str(cm.exception))
        self.assertEqual(extralex.list_names(self.out), [])

    def test_unsupported_missing_and_broken_files(self):
        odd = os.path.join(self.root, 'a.rtf')
        write(odd, 'x')
        bad = os.path.join(self.root, 'b.docx')
        write(bad, 'not a zip')
        for p in (odd, os.path.join(self.root, 'none.txt'), bad):
            with self.subTest(path=p), \
                    self.assertRaises(extralex.ExtraLexiconError):
                extralex.build(self.out, 't', [p])
        self.assertEqual(extralex.list_names(self.out), [])

    def test_folder_with_nothing_readable(self):
        d = os.path.join(self.root, 'pdfs')
        write(os.path.join(d, 'a.pdf'), '%PDF-1.4')
        with self.assertRaises(extralex.ExtraLexiconError) as cm:
            extralex.build(self.out, 't', [d])
        self.assertIn('PDF', str(cm.exception))


class Store(_Tmp):
    def test_build_list_load_remove(self):
        p = os.path.join(self.root, 'a.txt')
        write(p, 'שמרפוט ' * 3)
        rec = extralex.build(self.out, 'modern-1', [p])
        self.assertEqual(rec['tokens'], 3)
        self.assertTrue(os.path.exists(os.path.join(
            self.out, 'extra_modern-1.pkl')))
        self.assertEqual(extralex.list_names(self.out), ['modern-1'])
        self.assertEqual(extralex.load(self.out, 'modern-1')['counts'],
                         {'שמרפוט': 3})
        # a rebuild replaces it
        write(p, 'וזלנקה')
        extralex.build(self.out, 'modern-1', [p])
        self.assertEqual(extralex.load(self.out, 'modern-1')['counts'],
                         {'וזלנקה': 1})
        extralex.remove(self.out, 'modern-1')
        self.assertEqual(extralex.list_names(self.out), [])
        with self.assertRaises(extralex.ExtraLexiconError):
            extralex.remove(self.out, 'modern-1')

    def test_names(self):
        for ok in ('modern', 'מודרני', 'a_b-2'):
            self.assertEqual(extralex.check_name(ok), ok)
        for bad in ('', '../x', 'a b', '-x', 'a/b', 'x' * 65):
            with self.subTest(name=bad), \
                    self.assertRaises(extralex.ExtraLexiconError):
                extralex.check_name(bad)

    def test_known_words_threshold_and_union(self):
        a = os.path.join(self.root, 'a.txt')
        write(a, 'שמרפוט ' * 3 + 'וזלנקה ' * 2)
        extralex.build(self.out, 'one', [a])
        b = os.path.join(self.root, 'b.txt')
        write(b, 'קנדו ' * 5)
        extralex.build(self.out, 'two', [b])
        cfg = Config(extra_lexicon=('one',))
        self.assertEqual(extralex.known_words(cfg, self.out), {'שמרפוט'})
        cfg.extra_min = 2
        self.assertEqual(extralex.known_words(cfg, self.out),
                         {'שמרפוט', 'וזלנקה'})
        cfg.extra_lexicon = ('one', 'two')
        self.assertEqual(extralex.known_words(cfg, self.out),
                         {'שמרפוט', 'וזלנקה', 'קנדו'})

    def test_off_by_default(self):
        self.assertEqual(Config().extra_lexicon, ())
        self.assertEqual(extralex.known_words(Config(), self.out), set())

    def test_selected_but_missing_is_an_error(self):
        with self.assertRaises(extralex.ExtraLexiconError):
            extralex.known_words(Config(extra_lexicon=('none',)), self.out)


class Hardening(_Tmp):
    def zip_with(self, name, members):
        p = os.path.join(self.root, name)
        with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as z:
            for n, data in members.items():
                z.writestr(n, data)
        return p

    # --- 1. decompression bomb, html regex ---
    def test_zip_member_cap_names_the_file_and_writes_nothing(self):
        good = os.path.join(self.root, 'good.txt')
        write(good, 'שמרפוט וזלנקה\n')
        big = os.path.join(self.root, 'big.docx')
        make_docx(big, [['שמרפוט ' * 400]])
        with mock.patch.object(extralex, 'MAX_BYTES', 2000):
            with self.assertRaises(extralex.ExtraLexiconError) as cm:
                extralex.build(self.out, 't', [good, big])
        self.assertIn(big, str(cm.exception))
        self.assertIn('document.xml', str(cm.exception))
        self.assertEqual(extralex.list_names(self.out), [])
        self.assertEqual(
            [n for n in os.listdir(self.out) if n.startswith('extra_')], [])

    def test_zip_total_cap_over_members(self):
        part = '<html><body><p>' + 'שמרפוט ' * 100 + '</p></body></html>'
        p = self.zip_with('b.epub', {f'c{i}.xhtml': part for i in range(5)})
        with mock.patch.object(extralex, 'MAX_BYTES', 2500):
            with self.assertRaises(extralex.ExtraLexiconError) as cm:
                extralex.read_document(p)
        self.assertIn(p, str(cm.exception))
        # under the cap it is read
        self.assertEqual(len(extralex.read_document(p)), 5)

    def test_plain_file_cap(self):
        p = os.path.join(self.root, 'a.txt')
        write(p, 'שמרפוט ' * 1000)
        with mock.patch.object(extralex, 'MAX_BYTES', 100):
            with self.assertRaises(extralex.ExtraLexiconError) as cm:
                extralex.read_document(p)
        self.assertIn(p, str(cm.exception))

    def test_unclosed_script_is_linear(self):
        text = '<p>שמרפוט</p>' + '<script>x ' * 200000 + 'וזלנקה'
        t0 = time.time()
        out = extralex._html_text(text)
        self.assertLess(time.time() - t0, 5)
        self.assertIn('שמרפוט', out)
        self.assertNotIn('וזלנקה', out)       # runs to the end, like a browser
        t0 = time.time()
        extralex._html_text('<!--' * 200000 + 'x')
        extralex._html_text('<style ' * 200000)
        self.assertLess(time.time() - t0, 5)

    def test_html_parts_dropped(self):
        out = extralex._html_text(
            'א <SCRIPT>ב</script > ג <!-- ד --> ה <style>ו</style>ז '
            '<head><title>ח</title></head>ט <header>י</header>')
        for w in 'אגהזטי':
            self.assertIn(w, out)
        for w in 'בדוח':
            self.assertNotIn(w, out)

    # --- 2. empty files ---
    def test_zero_token_file_warns_and_all_empty_refuses(self):
        good = os.path.join(self.root, 'good.txt')
        write(good, 'שמרפוט וזלנקה\n')
        nodoc = self.zip_with('nodoc.docx', {'[Content_Types].xml': '<Types/>'})
        latin = os.path.join(self.root, 'latin.txt')
        write(latin, 'only latin words here\n')
        msgs = []
        rec = extralex.build(self.out, 't', [good, nodoc, latin],
                             progress=msgs.append)
        self.assertEqual(rec['empty_files'], [nodoc, latin])
        warn = [m for m in msgs if 'אזהרה' in m]
        self.assertEqual(len(warn), 2)
        self.assertIn(nodoc, warn[0])
        self.assertIn('אזהרה', extralex.summary(rec, 3))
        self.assertEqual(rec['tokens'], 2)
        # nothing at all: refused, and an older lexicon of the name survives
        before = extralex.load(self.out, 't')
        with self.assertRaises(extralex.ExtraLexiconError) as cm:
            extralex.build(self.out, 't', [nodoc, latin])
        self.assertIn('2', str(cm.exception))
        self.assertEqual(extralex.load(self.out, 't'), before)
        with self.assertRaises(extralex.ExtraLexiconError):
            extralex.build(self.out, 'fresh', [latin])
        self.assertEqual(extralex.list_names(self.out), ['t'])

    # --- 3. case-insensitive names ---
    def test_names_differing_by_case_are_one_lexicon(self):
        a = os.path.join(self.root, 'a.txt')
        b = os.path.join(self.root, 'b.txt')
        write(a, 'שמרפוט ' * 3)
        write(b, 'וזלנקה ' * 3)
        extralex.build(self.out, 'Modern', [a])
        rec = extralex.build(self.out, 'MODERN', [b])
        self.assertEqual(rec['name'], 'modern')
        self.assertEqual(extralex.list_names(self.out), ['modern'])
        self.assertEqual(
            [n for n in os.listdir(self.out) if n.startswith('extra_')],
            ['extra_modern.pkl'])
        self.assertEqual(set(extralex.load(self.out, 'mOdErN')['counts']),
                         {'וזלנקה'})
        words = extralex.known_words(
            Config(extra_lexicon=('Modern',)), self.out)
        self.assertEqual(words, {'וזלנקה'})
        extralex.remove(self.out, 'MODERN')
        self.assertEqual(extralex.list_names(self.out), [])

    # --- 4. encodings ---
    def test_epub_declared_encodings(self):
        def page(decl, word, enc):
            return (decl + '<html><body><p>' + word + '</p></body></html>'
                    ).encode(enc)
        p = self.zip_with('e.epub', {
            'a.xhtml': page('<?xml version="1.0" encoding="windows-1255"?>',
                            'שמרפוט', 'cp1255'),
            'b.html': page('<head><meta charset="iso-8859-8"></head>',
                           'וזלנקה', 'iso-8859-8'),
            'c.xhtml': page('', 'קנדו', 'utf-8'),
            # declares UTF-8 but is Windows-1255: falls back, not garbage
            'd.xhtml': page('<?xml version="1.0" encoding="utf-8"?>',
                            'טרבל', 'cp1255'),
            'e.xhtml': page('<?xml version="1.0" encoding="no-such-enc"?>',
                            'גלפרון', 'utf-8'),
        })
        c = self.counts(p)
        for w in ('שמרפוט', 'וזלנקה', 'קנדו', 'טרבל', 'גלפרון'):
            self.assertEqual(c.get(w), 1, w)

    def test_html_file_declared_encoding(self):
        p = os.path.join(self.root, 'a.html')
        with open(p, 'wb') as f:
            f.write('<meta http-equiv="Content-Type" content="text/html; '
                    'charset=windows-1255"><p>שמרפוט</p>'.encode('cp1255'))
        self.assertEqual(self.counts(p).get('שמרפוט'), 1)

    def test_broken_zip_members_are_a_hebrew_error(self):
        p = os.path.join(self.root, 'bad.docx')
        with open(p, 'wb') as f:
            f.write(b'PK\x03\x04 not really a zip')
        with self.assertRaises(extralex.ExtraLexiconError):
            extralex.read_document(p)


class Pipeline(unittest.TestCase):
    """The full scan and the single-book scan, with and without one."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='magiah_extra_p_')
        root = cls._tmp.name
        cls.lib = os.path.join(root, 'lib')
        write(os.path.join(cls.lib, 'Origin', 'ספר.txt'),
              f'{COMMON} שמרפוט וזלנקה קנדו טרבל\n' * 60
              + f'{TYPO} שמרפוט וזלנקה קנדו טרבל\n')
        cls.book = os.path.join(root, 'book.txt')
        write(cls.book, f'שמרפוט {TYPO} וזלנקה\nקנדו {COMMON} טרבל\n')
        cls.docs = os.path.join(root, 'docs')
        write(os.path.join(cls.docs, 'a.txt'), f'{TYPO} ' * 3)
        cls.base = os.path.join(root, 'base')
        rc, _, err = run_cli('all', '--library', cls.lib, '--out', cls.base,
                             *TUNE)
        assert rc in (0, None), err
        with open(os.path.join(cls.base, core.FLAGGED_F), 'rb') as f:
            cls.base_flagged = pickle.load(f)
        assert TYPO in cls.base_flagged, cls.base_flagged

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        # a copy of the scanned folder: each test starts from the same scan
        self.out = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), 'out')
        shutil.copytree(self.base, self.out)

    def flagged(self):
        with open(os.path.join(self.out, core.FLAGGED_F), 'rb') as f:
            return pickle.load(f)

    def run_config(self):
        with open(os.path.join(self.out, cli.RUN_CONFIG),
                  encoding='utf-8') as f:
            return json.load(f)['config']

    def build(self, name='modern'):
        rc, out, err = run_cli('extra', '--name', name, '--from', self.docs,
                               '--out', self.out)
        self.assertEqual(rc, 0, err)
        return out

    def test_extra_command_builds_lists_removes_without_a_corpus(self):
        out = self.build()
        self.assertIn('--extra-lexicon modern', out)
        rc, out, _ = run_cli('extra', '--out', self.out)
        self.assertEqual(rc, 0)
        self.assertIn('modern:', out)
        rc, _, _ = run_cli('extra', '--remove', 'modern', '--out', self.out)
        self.assertEqual(rc, 0)
        self.assertEqual(extralex.list_names(self.out), [])
        rc, _, err = run_cli('extra', '--from', self.docs, '--out', self.out)
        self.assertEqual(rc, 1)
        self.assertIn('--name', err)

    def test_built_but_not_selected_changes_nothing(self):
        self.build()
        rc, _, err = run_cli('detect', '--out', self.out)
        self.assertIn(rc, (0, None), err)
        self.assertEqual(self.flagged(), self.base_flagged)
        self.assertEqual(self.run_config()['extra_lexicon'], [])

    def test_selected_word_is_not_flagged_and_is_remembered(self):
        self.build()
        rc, out, err = run_cli('detect', '--extra-lexicon', 'modern',
                               '--out', self.out)
        self.assertIn(rc, (0, None), err)
        self.assertIn('modern', out)
        flagged = self.flagged()
        self.assertNotIn(TYPO, flagged)
        self.assertEqual({k: v for k, v in self.base_flagged.items()
                          if k != TYPO}, flagged)
        self.assertEqual(self.run_config()['extra_lexicon'], ['modern'])
        # remembered like --whitelist, until --no-extra-lexicon
        rc, _, err = run_cli('detect', '--no-extra-lexicon', '--out',
                             self.out)
        self.assertIn(rc, (0, None), err)
        self.assertEqual(self.flagged(), self.base_flagged)
        self.assertEqual(self.run_config()['extra_lexicon'], [])

    def test_threshold(self):
        self.build()
        rc, _, err = run_cli('detect', '--extra-lexicon', 'modern',
                             '--extra-min', '4', '--out', self.out)
        self.assertIn(rc, (0, None), err)
        self.assertIn(TYPO, self.flagged())

    def test_missing_lexicon_is_refused_and_not_remembered(self):
        before = self.run_config()
        rc, _, err = run_cli('detect', '--extra-lexicon', 'none', '--out',
                             self.out)
        self.assertEqual(rc, 1)
        self.assertIn('none', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(self.run_config(), before)
        # one removed after it was remembered fails the stage in Hebrew
        self.build()
        run_cli('detect', '--extra-lexicon', 'modern', '--out', self.out)
        extralex.remove(self.out, 'modern')
        rc, _, err = run_cli('detect', '--out', self.out)
        self.assertEqual(rc, 1)
        self.assertIn('modern', err)
        self.assertNotIn('Traceback', err)

    def book_words(self, cfg):
        res = book_scan.scan_book(self.out, 'file', self.book, cfg=cfg)
        return {r['word'] for r in res['findings']}

    def test_book_scan(self):
        self.build()
        self.assertIn(TYPO, self.book_words(Config()))
        self.assertNotIn(TYPO, self.book_words(
            Config(extra_lexicon=('modern',))))
        with self.assertRaises(book_scan.BookScanError):
            self.book_words(Config(extra_lexicon=('none',)))

    def test_book_command(self):
        self.build()
        rc, out, err = run_cli('book', '--book', self.book,
                               '--extra-lexicon', 'modern', '--out', self.out)
        self.assertEqual(rc, 0, err)
        self.assertIn('modern', out)

    def test_book_command_hides_the_word_and_reports_it(self):
        self.build()
        base, _, _ = run_cli('book', '--book', self.book, '--out', self.out)
        rc, out, err = run_cli('book', '--book', self.book,
                               '--extra-lexicon', 'MODERN', '--out',
                               self.out)
        self.assertEqual((base, rc), (0, 0), err)
        self.assertEqual(self.run_config()['extra_lexicon'], ['MODERN'])
        self.assertIn('נוספו 0 ממצאים', out)
        rc, _, err = run_cli('book', '--book', self.book,
                             '--extra-lexicon', 'none', '--out', self.out)
        self.assertEqual(rc, 1)
        self.assertNotIn('Traceback', err)

    def test_ui_book_scan_uses_the_field(self):
        self.build()
        corpus = {'mode': 'library', 'library_dir': self.lib}

        def run(over):
            scanner.start_book_scan(self.out, 'file', self.book,
                                    config_overrides=over,
                                    corpus_overrides=corpus)
            scanner._thread.join(120)
            st = scanner.get_status()
            self.assertEqual(st['state'], 'done', st)
            return st['book']['result']['added']
        plain = run({})
        self.assertGreaterEqual(plain, 1)
        self.assertEqual(run({'extra_lexicon': 'modern'}), plain - 1)
        with self.assertRaises(ValueError) as cm:
            scanner.start_book_scan(
                self.out, 'file', self.book,
                config_overrides={'extra_lexicon': 'none'},
                corpus_overrides=corpus)
        self.assertIn('none', str(cm.exception))
        with self.assertRaises(ValueError):
            scanner.start_scan(self.out, ['detect'],
                               config_overrides={'extra_lexicon': 'none'},
                               corpus_overrides=corpus)


class WebUI(unittest.TestCase):
    def test_scan_panel_field_is_a_list(self):
        with tempfile.TemporaryDirectory() as d:
            fields = {f['key']: f for f in scanner.scan_config(d)['fields']}
            self.assertEqual(fields['extra_lexicon']['type'], 'list')
            self.assertEqual(fields['extra_lexicon']['value'], [])
            self.assertEqual(fields['extra_min']['type'], 'int')
            cfg = scanner._merge_config(d, {'extra_lexicon': 'a\n\nb\n'})
            self.assertEqual(cfg.extra_lexicon, ('a', 'b'))
            cfg = scanner._merge_config(d, {'extra_lexicon': ['a']})
            self.assertEqual(cfg.extra_lexicon, ('a',))


if __name__ == '__main__':
    unittest.main()
