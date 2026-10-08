# -*- coding: utf-8 -*-
"""Regression tests for anchoring and write safety in the in-file fixer.

Each class pins one audited failure (C = source identity / anchoring,
E = Unicode, F = writing / recovery). Every fixture is built under a temp dir;
nothing here touches a real library.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from magiah import normalize                                    # noqa: E402
from magiah.normalize import TOKEN_RE                           # noqa: E402
from magiah.webui import db, fixer_api, patcher                 # noqa: E402


def write(path, text, encoding='utf-8'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(text.encode(encoding))
    return path


def raw(path):
    with open(path, 'rb') as f:
        return f.read()


def scan_snippet(line, word, k=0):
    """The snippet exactly as the scanner stores it (core._locate_chunk /
    book_scan._read_book_tokens): a +-45 char window of the CLEANED line."""
    text = normalize.clean(line)
    want = word.split()
    toks = list(TOKEN_RE.finditer(text))
    hits = []
    for i in range(len(toks) - len(want) + 1):
        if [m.group() for m in toks[i:i + len(want)]] == want:
            hits.append((toks[i].start(), toks[i + len(want) - 1].end()))
    s, e = hits[k]
    return text[max(0, s - 45):e + 45].strip()


def finding(line, word, correction, lineno=0, fid=1, k=0, **kw):
    d = {'id': fid, 'lineno': lineno, 'word': word, 'correction': correction,
         'snippet': scan_snippet(line, word, k)}
    d.update(kw)
    return d


def make_report(outdir, lib, rows, scan_meta=True):
    """A report.db the way `magiah locate` writes it: scan_meta naming the
    library it read (unless `scan_meta` is False: a pre-scan_meta report),
    and one occurrences_full row per ``(word, suggestion, unit, snippet)``."""
    from magiah import core
    p = os.path.join(outdir, db.REPORT_DB_F)
    if os.path.exists(p):
        os.remove(p)
    con = sqlite3.connect(p)
    con.executescript('''
        CREATE TABLE occurrences_full(errtype TEXT, word TEXT,
            suggestion TEXT, score REAL, ctx_hits INT, sugg_local INT,
            book_repeat INT, tanach INT, origin TEXT, source TEXT, ref TEXT,
            unit TEXT, doc TEXT, snippet TEXT);
        CREATE TABLE space_errors_full(part1 TEXT, part2 TEXT, joined TEXT,
            join_freq INT, source TEXT, ref TEXT, unit TEXT, snippet TEXT,
            origin TEXT);
        CREATE TABLE tanach_errors_full(word TEXT, canonical TEXT,
            source TEXT, ref TEXT, unit TEXT, snippet TEXT, origin TEXT);
        CREATE TABLE tanach_matches_full(word TEXT, source TEXT, ref TEXT,
            unit TEXT, snippet TEXT, origin TEXT);''')
    if scan_meta:
        core.write_scan_meta(con, {'type': 'library', 'path': lib})
    con.executemany(
        'INSERT INTO occurrences_full VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [('edit1_sub', w, s, 5.0, 1, 3, 0, 0, 'o', 't', 'r', u,
          patcher.book_key_of(u).split(':', 1)[1], snip)
         for w, s, u, snip in rows])
    con.commit()
    con.close()
    return p


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_anchor_')
        self.outdir = os.path.join(self.tmp, 'scan')
        os.makedirs(self.outdir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def doc(self, text, name='b.txt'):
        return patcher.read_doc(write(os.path.join(self.tmp, name), text))


class Env(TempCase):
    """A scan folder + library + ui_review.db, driven through fixer_api."""

    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.tmp, 'libA')
        os.makedirs(self.lib)
        self.set_config(self.lib)
        db.connect(self.outdir).close()

    def set_config(self, lib):
        with open(os.path.join(self.outdir, 'run_config.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'corpus': {'type': 'library', 'path': lib}}, f)

    def con(self):
        return db.connect(self.outdir)

    def add(self, fid, word, sugg, unit, snippet, doc=None, extra=None,
            status='approved'):
        con = self.con()
        con.execute(
            'INSERT INTO findings(id, family, errtype, word, suggestion, '
            'rank, verified, origin, source, ref, unit, doc, snippet, extra) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (fid, 'error', 'edit1_sub', word, sugg, 5.0, 1, 'o', 't', 'r',
             unit, doc, snippet, extra))
        if status:
            con.execute("INSERT OR REPLACE INTO review "
                        "VALUES(?,?,NULL,NULL,'t')", (fid, status))
        con.commit()
        con.close()

    def open_doc(self, key):
        con = self.con()
        try:
            return fixer_api.doc(con, self.outdir, {'key': key})
        finally:
            con.close()

    def apply(self, key, items, fingerprint=None, **body):
        con = self.con()
        try:
            if fingerprint is None:
                fingerprint = self.open_doc(key)['fingerprint']
            body.update({'key': key, 'fingerprint': fingerprint,
                         'items': items})
            try:
                return fixer_api.apply(con, self.outdir, body)
            except patcher.PatchError as e:
                return dict({'code': e.code}, **e.extra), 409
        finally:
            con.close()

    def undo(self, edit_id):
        con = self.con()
        try:
            try:
                return fixer_api.undo_file(con, self.outdir,
                                           {'edit_id': edit_id}), 200
            except patcher.PatchError as e:
                return {'code': e.code}, 409
        finally:
            con.close()

    def status_of(self, fid):
        con = self.con()
        try:
            row = con.execute('SELECT status FROM review WHERE finding_id=?',
                              (fid,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()


# ---------------------------------------------------------------------------
# C1 - the library root a finding was scanned against
# ---------------------------------------------------------------------------

class TestSourceIdentity(Env):
    LINE = 'אמר רבי יותבת בן זומא'

    def setUp(self):
        super().setUp()
        self.libB = os.path.join(self.tmp, 'libB')
        # the same relative path, the same text, in two library roots
        self.pa = write(os.path.join(self.lib, 'ספר', 'פרק.txt'),
                        self.LINE + '\n')
        self.pb = write(os.path.join(self.libB, 'ספר', 'פרק.txt'),
                        self.LINE + '\n')
        self.key = 'file:ספר/פרק.txt'

    def book_scan_row(self, root):
        self.add(1, 'יותבת', 'יושבת', self.key + ':0',
                 scan_snippet(self.LINE, 'יותבת'), doc='ספר/פרק.txt',
                 extra=json.dumps({'book_scan': True, 'ctx_scope': 'book'}))
        db.record_book_scan_source(self.outdir, {
            'doc': 'ספר/פרק.txt', 'kind': 'library',
            'path': os.path.join(root, 'ספר', 'פרק.txt')})

    def test_book_scanned_in_B_is_never_written_into_A(self):
        self.book_scan_row(self.libB)          # configured library is A
        before = (raw(self.pa), raw(self.pb))
        res, code = self.apply(self.key, [{'id': 1}],
                               fingerprint=patcher.fingerprint(self.pa))
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'source_mismatch')
        self.assertEqual((raw(self.pa), raw(self.pb)), before)

    def test_matching_root_writes_the_recorded_root_only(self):
        self.book_scan_row(self.libB)
        self.set_config(self.libB)
        before_a = raw(self.pa)
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        self.assertEqual(raw(self.pa), before_a)
        self.assertIn('יושבת', raw(self.pb).decode('utf-8'))

    def test_full_scan_root_is_pinned_at_import(self):
        self.add(1, 'יותבת', 'יושבת', self.key + ':0',
                 scan_snippet(self.LINE, 'יותבת'))
        self.open_doc(self.key)                # records root A for the import
        self.set_config(self.libB)             # a new scan was started, not imported
        before = (raw(self.pa), raw(self.pb))
        res, code = self.apply(self.key, [{'id': 1}],
                               fingerprint=patcher.fingerprint(self.pb))
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'source_mismatch')
        self.assertEqual((raw(self.pa), raw(self.pb)), before)

    def test_book_scan_without_a_recorded_root_is_refused(self):
        self.add(1, 'יותבת', 'יושבת', self.key + ':0',
                 scan_snippet(self.LINE, 'יותבת'), doc='ספר/פרק.txt',
                 extra=json.dumps({'book_scan': True}))
        before = raw(self.pa)
        res, code = self.apply(self.key, [{'id': 1}],
                               fingerprint=patcher.fingerprint(self.pa))
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'source_unknown')
        self.assertEqual(raw(self.pa), before)

    def test_identical_titles_in_different_folders(self):
        other = write(os.path.join(self.lib, 'אחר', 'פרק.txt'),
                      self.LINE + '\n')
        self.add(1, 'יותבת', 'יושבת', self.key + ':0',
                 scan_snippet(self.LINE, 'יותבת'))
        before = raw(other)
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        self.assertEqual(raw(other), before)


class TestImportSource(Env):
    """The root of a full import is recorded BY the import, with the rows.

    It used to be inferred from report.db's scan time against last_import —
    and a single-book merge also moves last_import, so a scan of another
    library written into the same folder (not imported) became 'imported'.
    """
    LINE = 'אמר רבי יותבת בן זומא'

    def setUp(self):
        super().setUp()
        self.libB = os.path.join(self.tmp, 'libB')
        self.pa = write(os.path.join(self.lib, 'ספר', 'פרק.txt'),
                        self.LINE + '\n')
        self.pb = write(os.path.join(self.libB, 'ספר', 'פרק.txt'),
                        self.LINE + '\n')
        self.key = 'file:ספר/פרק.txt'
        self.rows = [('יותבת', 'יושבת', self.key + ':0',
                      scan_snippet(self.LINE, 'יותבת'))]

    def import_and_approve(self):
        db.import_all(self.outdir)
        con = self.con()
        try:
            fid = con.execute('SELECT id FROM findings').fetchone()[0]
            con.execute("INSERT OR REPLACE INTO review "
                        "VALUES(?,'approved',NULL,NULL,'t')", (fid,))
            con.commit()
        finally:
            con.close()
        return fid

    def source(self):
        con = self.con()
        try:
            return db.get_import_source(con)
        finally:
            con.close()

    def test_import_records_root_and_scan_time(self):
        rep = make_report(self.outdir, self.lib, self.rows)
        db.import_all(self.outdir)
        con = sqlite3.connect(rep)
        meta = dict(con.execute('SELECT key, value FROM scan_meta'))
        con.close()
        self.assertEqual(self.source(), {
            'root': os.path.abspath(self.lib),
            'scanned_at': meta['scanned_at'], 'basis': 'scan_meta'})

    def test_book_merge_after_an_unimported_scan_keeps_the_import_root(self):
        """QA's reproduction: scan A + refresh; a full scan of a copy B into
        the same folder, NOT imported; then any single-book merge. The rows
        are still A's, so writing them into B must be refused."""
        make_report(self.outdir, self.lib, self.rows)
        fid = self.import_and_approve()
        self.assertTrue(self.open_doc(self.key)['editable'])
        # a full scan of B into the same folder: report.db and run_config.json
        # are B's now, the database still holds A's rows
        make_report(self.outdir, self.libB, self.rows)
        self.set_config(self.libB)
        other = write(os.path.join(self.libB, 'ספר', 'אחר.txt'), 'שורה\n')
        db.merge_book_scan(self.outdir, {
            'doc': 'ספר/אחר.txt', 'title': 'אחר', 'kind': 'library',
            'path': other, 'findings': [], 'space_errors': []})
        self.assertEqual(self.source()['root'], os.path.abspath(self.lib))
        before = (raw(self.pa), raw(self.pb))
        with self.assertRaises(patcher.PatchError) as cm:
            self.open_doc(self.key)
        self.assertEqual(cm.exception.code, 'source_mismatch')
        res, code = self.apply(self.key, [{'id': fid}],
                               fingerprint=patcher.fingerprint(self.pb))
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'source_mismatch')
        self.assertEqual(res['recorded'], os.path.abspath(self.lib))
        self.assertEqual((raw(self.pa), raw(self.pb)), before)
        # importing B's report makes B the source, and the fix lands there
        db.import_all(self.outdir)
        res, code = self.apply(self.key, [{'id': fid}])
        self.assertEqual(code, 200, res)
        self.assertEqual(raw(self.pa), before[0])
        self.assertIn('יושבת', raw(self.pb).decode('utf-8'))

    def test_older_import_and_a_newer_report_is_unknown(self):
        """No recorded source (imported by an older version) while report.db
        names a root: report.db was written after that import."""
        make_report(self.outdir, self.lib, self.rows)
        self.add(1, 'יותבת', 'יושבת', self.key + ':0', self.rows[0][3])
        before = raw(self.pa)
        res, code = self.apply(self.key, [{'id': 1}],
                               fingerprint=patcher.fingerprint(self.pa))
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'source_unknown')
        self.assertEqual(raw(self.pa), before)

    def test_report_without_scan_meta_uses_the_configured_root(self):
        rep = make_report(self.outdir, self.lib, self.rows, scan_meta=False)
        old = time.time() - 3600
        os.utime(os.path.join(self.outdir, 'run_config.json'), (old, old))
        fid = self.import_and_approve()
        self.assertEqual(self.source()['root'], os.path.abspath(self.lib))
        res, code = self.apply(self.key, [{'id': fid}])
        self.assertEqual(code, 200, res)
        # a scan started after report.db rewrote run_config.json: the setting
        # may describe that scan, so it is not taken as this report's root
        os.utime(rep, (old, old))
        self.set_config(self.lib)
        db.import_all(self.outdir)
        self.assertIsNone(self.source()['root'])

    @unittest.skipUnless(os.name == 'nt' and os.path.isdir(r'\\localhost\C$'),
                         'needs the \\\\localhost\\C$ admin share')
    def test_unc_and_drive_spellings_name_one_root(self):
        from magiah.webui import journal
        drive, rest = os.path.splitdrive(os.path.abspath(self.lib))
        if len(drive) != 2:
            self.skipTest('temp dir is not on a drive letter')
        unc = '\\\\localhost\\' + drive[0] + '$' + rest
        self.assertTrue(fixer_api._same_root(unc, self.lib))
        self.assertFalse(fixer_api._same_root(unc, self.libB))
        unc_book = os.path.join(unc, 'ספר', 'פרק.txt')
        self.assertEqual(journal._key(unc_book), journal._key(self.pa))
        self.assertNotEqual(journal._key(unc_book), journal._key(self.pb))
        make_report(self.outdir, unc, self.rows)     # scanned over the share
        fid = self.import_and_approve()               # configured: drive path
        res, code = self.apply(self.key, [{'id': fid}])
        self.assertEqual(code, 200, res)
        self.assertIn('יושבת', raw(self.pa).decode('utf-8'))
        self.assertNotIn('יושבת', raw(self.pb).decode('utf-8'))


# ---------------------------------------------------------------------------
# C2 - an occurrence at the old line number is not proof of identity
# ---------------------------------------------------------------------------

class TestLineIdentity(TempCase):
    TARGET = 'אמר הרב ברכת שלם עליכם לתלמידיו'
    INSERTED = 'אמר הסוחר המחיר שלם ואין חוב'

    def test_inserted_line_with_the_same_word_is_not_corrected(self):
        f = finding(self.TARGET, 'שלם', 'שלום', lineno=1,
                    occurrence=0, expected_count=1)
        d = self.doc('כותרת\n%s\n%s\n' % (self.INSERTED, self.TARGET))
        plan = patcher.plan_edit(d, f)
        self.assertEqual(plan.lineno, 2)
        self.assertNotEqual(plan.confidence, 'exact')
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.lines[1], self.INSERTED)
        self.assertIn('שלום', d.lines[2])

    def test_no_identity_and_no_relocation_refuses(self):
        f = finding(self.TARGET, 'שלם', 'שלום', lineno=1)
        d = self.doc('כותרת\n%s\n' % self.INSERTED)
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'line_mismatch')

    def test_missing_snippet_is_never_exact(self):
        d = self.doc(self.TARGET + '\n')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, {'id': 1, 'lineno': 0, 'word': 'שלם',
                                  'correction': 'שלום'})
        self.assertEqual(cm.exception.code, 'line_mismatch')

    def test_duplicated_identical_lines_nearby_are_ambiguous(self):
        f = finding(self.TARGET, 'שלם', 'שלום', lineno=1)
        d = self.doc('כותרת\n%s\nאחר\n%s\n' % (self.TARGET, self.TARGET))
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'ambiguous_line')

    def test_snippet_matching_several_lines_on_drift_is_ambiguous(self):
        f = finding(self.TARGET, 'שלם', 'שלום', lineno=0)
        d = self.doc('כותרת\n%s\nאחר\n%s\n' % (self.TARGET, self.TARGET))
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'ambiguous_line')

    LONG = ('שלם ' + 'מילה ' * 20 + 'ועוד דברים רבים כאן ' * 2 + 'שלם סוף')

    def test_repeated_word_is_pinned_by_its_own_window(self):
        # the scan flagged the SECOND copy; a hand edit then added a third
        f = finding(self.LONG, 'שלם', 'שלום', k=1, occurrence=1,
                    expected_count=2)
        d = self.doc('שלם ' + self.LONG + '\n')
        plan = patcher.plan_edit(d, f)
        self.assertEqual(plan.confidence, 'exact')
        line = d.lines[0]
        self.assertEqual(plan.start, line.rindex('שלם'))

    def test_repeated_word_on_a_short_line_uses_the_original_order(self):
        line = 'שלם אמר שלם'
        f = finding(line, 'שלם', 'שלום', occurrence=1, expected_count=2)
        d = self.doc(line + '\n')
        plan = patcher.plan_edit(d, f)
        self.assertEqual(plan.start, line.rindex('שלם'))
        self.assertEqual(plan.confidence, 'indexed')


# ---------------------------------------------------------------------------
# E - Unicode, nikud and tags
# ---------------------------------------------------------------------------

class TestUnicodeSpans(TempCase):

    def test_trailing_mark_is_inside_the_span(self):
        line = 'הם אמרוּ דבר'
        tok = [s for s in normalize.token_spans(line) if s[0] == 'אמרו'][0]
        self.assertEqual(line[tok[1]:tok[2]], 'אמרוּ')

    def test_trailing_teamim_and_nikud_are_inside_the_span(self):
        line = 'אמר דָּבָר֙ אחד'
        tok = [s for s in normalize.token_spans(line) if s[0] == 'דבר'][0]
        self.assertEqual(line[tok[1]:tok[2]], 'דָּבָר֙')

    def test_vocalized_replace_leaves_no_orphan_mark(self):
        line = 'הם אמרוּ דבר'
        d = self.doc(line)
        plan = patcher.plan_edit(d, finding(line, 'אמרו', 'אָמַר'))
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.lines[0], 'הם אָמַר דבר')

    def test_bracket_keeps_the_whole_original_slice(self):
        line = 'הם אמרוּ דבר'
        d = self.doc(line)
        plan = patcher.plan_edit(d, finding(line, 'אמרו', 'אמר'),
                                 mode=patcher.MODE_BRACKET)
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.lines[0], 'הם (אמר) [אמרוּ] דבר')

    def test_unvocalized_correction_in_vocalized_word_needs_review(self):
        line = 'הם אמרוּ דבר'
        d = self.doc(line)
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, finding(line, 'אמרו', 'אמר'))
        self.assertEqual(cm.exception.code, 'needs_vocalization')

    def test_span_tag_does_not_split_a_word(self):
        line = 'ה<span class="x">ע</span>ולם הזה'
        self.assertEqual(normalize.tokenize(line), ['העולם', 'הזה'])
        self.assertEqual(normalize.clean_mapped(line)[0],
                         normalize.clean(line))

    def test_structural_tags_still_split(self):
        for tag in ('<br>', '<p>', '</div>', '<h2>', '<li>', '<blockquote>'):
            with self.subTest(tag=tag):
                self.assertEqual(normalize.tokenize('אבג%sדהו' % tag),
                                 ['אבג', 'דהו'])

    def test_whole_word_in_span_keeps_the_tags(self):
        line = 'אמר <span>שלם</span> לו'
        d = self.doc(line)
        plan = patcher.plan_edit(d, finding(line, 'שלם', 'שלום'))
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.lines[0], 'אמר <span>שלום</span> לו')

    def test_yiddish_ligatures_do_not_split_words(self):
        line = 'אַ צװײ מאָל'
        self.assertEqual(normalize.tokenize(line), ['א', 'צוויי', 'מאל'])
        self.assertEqual(normalize.clean_mapped(line)[0],
                         normalize.clean(line))
        tok = [s for s in normalize.token_spans(line) if s[0] == 'צוויי'][0]
        self.assertEqual(line[tok[1]:tok[2]], 'צװײ')

    def test_ligature_replacement_covers_the_whole_raw_char(self):
        line = 'אמר צװײ פעמים'
        d = self.doc(line)
        plan = patcher.plan_edit(d, finding(line, 'צוויי', 'שלוש'))
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.lines[0], 'אמר שלוש פעמים')


class TestGeresh(TempCase):

    def run_one(self, line, word, corr, mode=patcher.MODE_REPLACE):
        d = self.doc(line)
        plan = patcher.plan_edit(d, finding(line, word, corr), mode=mode)
        patcher.apply_edits(d, [plan])
        return d.lines[0]

    def test_geresh_kept_when_correction_has_none(self):
        self.assertEqual(self.run_one("אמר רבך' שלום", 'רבך', 'רבי'),
                         "אמר רבי' שלום")

    def test_geresh_not_doubled_ascii(self):
        self.assertEqual(self.run_one("אמר רבך' שלום", 'רבך', "רבי'"),
                         "אמר רבי' שלום")

    def test_geresh_not_doubled_hebrew_geresh(self):
        self.assertEqual(self.run_one('אמר רבך׳ שלום', 'רבך', 'רבי׳'),
                         'אמר רבי׳ שלום')

    def test_bracket_mode_keeps_the_geresh_inside(self):
        self.assertEqual(self.run_one('אמר רבך׳ שלום', 'רבך', 'רבי',
                                      mode=patcher.MODE_BRACKET),
                         'אמר (רבי׳) [רבך׳] שלום')

    def test_closing_quote_is_not_part_of_the_word(self):
        self.assertEqual(self.run_one('אמר "שלם" לו', 'שלם', 'שלום',
                                      mode=patcher.MODE_BRACKET),
                         'אמר "(שלום) [שלם]" לו')
        self.assertEqual(self.run_one('אמר ״שלם״ לו', 'שלם', 'שלום'),
                         'אמר ״שלום״ לו')

    def test_internal_gershayim_is_one_token(self):
        line = 'כתב רמב"ן כאן'
        self.assertEqual(self.run_one(line, 'רמב"ן', 'רמב"ם'),
                         'כתב רמב"ם כאן')


class TestKetivQere(TempCase):
    """'(x) [word]' is the layout of the fixer's bracket mode AND of ketiv/
    qere in the books. Only the fixer's own output is 'already applied'."""
    LINE = 'ויצא (הנער) [הנערח] אל השדה ותקח את הכד'

    def test_a_typo_in_the_qere_is_corrected(self):
        d = self.doc(self.LINE)
        plan = patcher.plan_edit(d, finding(self.LINE, 'הנערח', 'הנערה'))
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.lines[0],
                         'ויצא (הנער) [הנערה] אל השדה ותקח את הכד')

    def test_a_manual_pick_in_the_qere_is_corrected(self):
        d = self.doc(self.LINE)
        a, b = normalize.phrase_spans(self.LINE, 'הנערח')[0][1:]
        plan = patcher.plan_edit(d, finding(self.LINE, 'הנערח', 'הנערה'),
                                 explicit=(a, b))
        self.assertEqual((plan.start, plan.end, plan.confidence),
                         (a, b, 'manual'))

    def test_the_fixers_own_output_is_already_applied(self):
        for line, word, corr in (
                ('אמר רבי (יושבת) [יותבת] בן זומא', 'יותבת', 'יושבת'),
                # bracket mode carries the original's geresh onto the fix
                ('אמר (רבי׳) [רבך׳] שלום', 'רבך', 'רבי')):
            with self.subTest(line=line):
                d = self.doc(line)
                for mode in patcher.MODES:
                    with self.assertRaises(patcher.PatchError) as cm:
                        patcher.plan_edit(d, finding(line, word, corr),
                                          mode=mode)
                    self.assertEqual(cm.exception.code, 'already_applied')

    def test_a_recorded_bracket_edit_is_ours_whatever_the_correction(self):
        orig = 'אמר רבי יותבת בן זומא'
        d = self.doc(orig)
        plan = patcher.plan_edit(d, finding(orig, 'יותבת', 'יושבת'),
                                 mode=patcher.MODE_BRACKET)
        patcher.apply_edits(d, [plan])
        # re-applied with another correction once the finding's id changed
        # (a re-scan): only the record says these brackets are ours
        f = finding(orig, 'יותבת', 'ישבת', fid=2)
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f, own_edits=[plan.to_dict()])
        self.assertEqual(cm.exception.code, 'already_applied')


class TestOutsideTheSpanIsUntouched(TempCase):
    LINES = [
        'וַיֹּ֥אמֶר אֱלֹהִ֖ים יְהִ֣י א֑וֹר',
        '<b>אמר</b> רבי <span class="c">יוסי</span> בן חלפתא',
        'א&amp;ב &quot;ציטוט&quot; טקסט &#1488;&#1489;ג סוף',
        'אַ צװײ מאָל אױף דער װעלט',
        "ר' יוסי אמר וכו' ועוד",
        '<sup>4</sup> וְיֶהֱמוּ גַלָיו',
    ]

    def test_bytes_outside_the_span_are_identical(self):
        for line in self.LINES:
            for tok, a, b in normalize.token_spans(line):
                if '<' in line[a:b] or '&' in line[a:b]:
                    continue
                with self.subTest(line=line[:20], tok=tok):
                    d = self.doc(line)
                    plan = patcher.plan_edit(
                        d, {'id': 1, 'lineno': 0, 'word': tok,
                            'correction': 'שינוי',
                            'snippet': normalize.clean(line).strip()},
                        mode=patcher.MODE_BRACKET, explicit=(a, b))
                    patcher.apply_edits(d, [plan])
                    new = d.lines[0]
                    self.assertEqual(new[:plan.start], line[:plan.start])
                    self.assertEqual(new[plan.start + len(plan.new_text):],
                                     line[plan.end:])
                    # nothing of the word survives outside the brackets
                    rest = line[plan.end:]
                    self.assertFalse(rest[:1] and
                                     normalize.is_mark(rest[0]), rest[:3])


# ---------------------------------------------------------------------------
# F - writing, backups and recovery
# ---------------------------------------------------------------------------

class FixerEnv(Env):
    TEXT = ('כותרת הספר\n'
            'אמר רבי יותבת בן זומא\n'
            'ועוד אמר מחורז דבר אחר\n')

    def setUp(self):
        super().setUp()
        self.path = write(os.path.join(self.lib, 'ספר.txt'), self.TEXT)
        self.key = 'file:ספר.txt'
        lines = self.TEXT.splitlines()
        self.add(1, 'יותבת', 'יושבת', self.key + ':1',
                 scan_snippet(lines[1], 'יותבת'))
        self.add(2, 'מחורז', 'מחוז', self.key + ':2',
                 scan_snippet(lines[2], 'מחורז'))


class TestConcurrentWrites(FixerEnv):

    def test_two_racing_writes_do_not_lose_an_update(self):
        fp = self.open_doc(self.key)['fingerprint']
        barrier = threading.Barrier(2)
        real_plan_all = patcher.plan_all

        def slow_plan_all(*a, **kw):
            out = real_plan_all(*a, **kw)
            try:
                barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            return out

        patcher.plan_all = slow_plan_all
        results = {}
        try:
            ts = [threading.Thread(target=lambda i=i: results.__setitem__(
                i, self.apply(self.key, [{'id': i}], fingerprint=fp)))
                for i in (1, 2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(10)
        finally:
            patcher.plan_all = real_plan_all
        codes = sorted(results[i][1] for i in (1, 2))
        self.assertEqual(codes, [200, 409], results)
        loser = [r for r, c in results.values() if c == 409][0]
        self.assertEqual(loser['code'], 'file_changed')
        text = raw(self.path).decode('utf-8')
        # exactly one correction landed, and nothing was silently erased
        self.assertEqual(('יושבת' in text) + ('מחוז' in text), 1, text)

    def test_another_process_lock_file_blocks(self):
        from magiah.webui import journal
        write(self.path + journal.LOCK_SUFFIX, '{}')
        with self.assertRaises(patcher.PatchError) as cm:
            with journal.file_lock(self.path, timeout=0.2):
                pass
        self.assertEqual(cm.exception.code, 'file_busy')

    def test_stale_lock_file_is_broken(self):
        from magiah.webui import journal
        lf = write(self.path + journal.LOCK_SUFFIX, '{}')
        old = time.time() - journal.STALE_LOCK_SECONDS - 5
        os.utime(lf, (old, old))
        with journal.file_lock(self.path, timeout=1):
            self.assertTrue(os.path.exists(lf))
        self.assertFalse(os.path.exists(lf))


class TestBackups(FixerEnv):

    def test_backup_names_are_unique_within_a_second(self):
        names = {patcher.backup_path(self.outdir, self.path)
                 for _ in range(5)}
        self.assertEqual(len(names), 5)

    def test_two_writes_in_one_second_keep_both_backups(self):
        contents = []
        for word, corr in (('יותבת', 'יושבת'), ('מחורז', 'מחוז')):
            d = patcher.read_doc(self.path)
            contents.append(d.encode())
            ln = 1 if word == 'יותבת' else 2
            plan = patcher.plan_edit(d, finding(d.lines[ln], word, corr,
                                                lineno=ln))
            patcher.apply_edits(d, [plan])
            patcher.write_doc(d, self.outdir)
        bdir = os.path.join(self.outdir, patcher.FIXER_BACKUP_DIR)
        baks = sorted(raw(os.path.join(bdir, n)) for n in os.listdir(bdir)
                      if n.endswith('.bak'))
        self.assertEqual(baks, sorted(contents))

    def test_old_backup_names_are_still_restorable(self):
        self.assertTrue(patcher._BACKUP_RE.match(
            'ספר.20240101-120000.0123abcd.bak'))

    def test_temp_names_are_unique(self):
        a = patcher.temp_path_for(self.path)
        b = patcher.temp_path_for(self.path)
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.dirname(a), os.path.dirname(self.path))

    def test_corrupt_backup_is_not_restored(self):
        d = patcher.read_doc(self.path)
        plan = patcher.plan_edit(d, finding(d.lines[1], 'יותבת', 'יושבת',
                                            lineno=1))
        patcher.apply_edits(d, [plan])
        res = patcher.write_doc(d, self.outdir)
        with open(res['backup'], 'ab') as f:
            f.write(b'garbage')
        after = raw(self.path)
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.restore_backup(self.outdir, res['backup'], self.path,
                                   res['fingerprint'],
                                   backup_fingerprint=res['backup_sha'])
        self.assertEqual(cm.exception.code, 'backup_corrupt')
        self.assertEqual(raw(self.path), after)

    def test_edit_record_carries_hashes(self):
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        con = self.con()
        rec = db.get_file_edit(con, res['edit_id'])
        con.close()
        self.assertEqual(rec['backup_sha'], patcher.fingerprint_bytes(
            self.TEXT.encode('utf-8')))
        self.assertEqual(rec['fp_before'], rec['backup_sha'])
        self.assertEqual(rec['fp_after'], patcher.fingerprint(self.path))

    def test_undo_with_a_corrupt_backup_still_restores_by_spans(self):
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        with open(res['backup'], 'ab') as f:
            f.write(b'garbage')
        out, code = self.undo(res['edit_id'])
        self.assertEqual(code, 200, out)
        self.assertEqual(raw(self.path), self.TEXT.encode('utf-8'))


class TestJournal(FixerEnv):

    def test_db_failure_after_the_write_is_recoverable(self):
        from magiah.webui import journal
        real = db.record_file_edit

        def boom(*a, **kw):
            raise sqlite3.OperationalError('disk I/O error')

        db.record_file_edit = boom
        try:
            res, code = self.apply(self.key, [{'id': 1}])
        finally:
            db.record_file_edit = real
        self.assertIn('יושבת', raw(self.path).decode('utf-8'))
        # the next access recovers the record, so the edit can be undone
        con = self.con()
        try:
            out = journal.recover(con, self.outdir)
            edits = db.get_file_edits(con, self.key)
        finally:
            con.close()
        self.assertEqual([o['state'] for o in out], ['committed'])
        self.assertEqual(len(edits), 1)
        self.assertEqual(self.status_of(1), 'fixed')
        _o, code = self.undo(edits[0]['id'])
        self.assertEqual(code, 200)
        self.assertEqual(raw(self.path), self.TEXT.encode('utf-8'))

    def test_failed_replace_leaves_the_file_untouched(self):
        from magiah.webui import journal
        real = os.replace

        def boom(src, dst):
            raise PermissionError('locked')

        os.replace = boom
        try:
            with self.assertRaises(PermissionError):
                self.apply(self.key, [{'id': 1}])
        finally:
            os.replace = real
        self.assertEqual(raw(self.path), self.TEXT.encode('utf-8'))
        self.assertEqual([n for n in os.listdir(self.lib)
                          if n.endswith('.tmp')], [])
        self.assertEqual(journal.pending(self.outdir), [])

    def test_crash_between_write_and_commit(self):
        """A process killed after os.replace: the journal intent survives."""
        from magiah.webui import journal

        class Crash(BaseException):
            pass

        real = db.record_file_edit

        def crash(*a, **kw):
            raise Crash()

        db.record_file_edit = crash
        try:
            with self.assertRaises(Crash):
                self.apply(self.key, [{'id': 1}])
        finally:
            db.record_file_edit = real
        self.assertEqual(len(journal.pending(self.outdir)), 1)
        # a fresh page load recovers before showing anything
        d = self.open_doc(self.key)
        self.assertEqual(journal.pending(self.outdir), [])
        self.assertEqual(len(d['edits']), 1)
        _o, code = self.undo(d['edits'][0]['id'])
        self.assertEqual(code, 200)
        self.assertEqual(raw(self.path), self.TEXT.encode('utf-8'))

    def test_pending_intent_on_an_unwritten_file_is_aborted(self):
        from magiah.webui import journal
        fp = patcher.fingerprint(self.path)
        journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': fp, 'fp_after': 'sha256:other', 'finding_ids': [1]})
        con = self.con()
        try:
            out = journal.recover(con, self.outdir)
        finally:
            con.close()
        self.assertEqual([o['state'] for o in out], ['aborted'])

    def test_pending_intent_on_a_changed_file_is_a_conflict(self):
        from magiah.webui import journal
        journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': 'sha256:x', 'fp_after': 'sha256:y',
            'finding_ids': [1]})
        con = self.con()
        try:
            out = journal.recover(con, self.outdir)
        finally:
            con.close()
        self.assertEqual([o['state'] for o in out], ['conflict'])
        self.assertEqual(len(journal.conflicts(self.outdir)), 1)


class TestIdempotency(FixerEnv):

    def _reapprove(self, fid):
        con = self.con()
        con.execute("UPDATE review SET status='approved' WHERE finding_id=?",
                    (fid,))
        con.commit()
        con.close()

    def test_reapplying_in_bracket_mode_does_not_nest(self):
        res, code = self.apply(self.key, [{'id': 1}], default_mode='bracket')
        self.assertEqual(code, 200, res)
        once = raw(self.path)
        self._reapprove(1)                    # status lost, file written
        res, code = self.apply(self.key, [{'id': 1}], default_mode='bracket')
        self.assertEqual(code, 200, res)
        self.assertEqual(res.get('already_applied'), [1])
        self.assertEqual(raw(self.path), once)

    def test_reapplying_after_the_record_is_lost_does_not_nest(self):
        res, code = self.apply(self.key, [{'id': 1}], default_mode='bracket')
        self.assertEqual(code, 200, res)
        once = raw(self.path)
        con = self.con()
        con.execute('DELETE FROM file_edits')
        con.commit()
        con.close()
        self._reapprove(1)
        res, code = self.apply(self.key, [{'id': 1}], default_mode='bracket')
        self.assertEqual(code, 409, res)
        self.assertEqual([f['code'] for f in res['failed']],
                         ['already_applied'])
        self.assertEqual(raw(self.path), once)

    def test_ketiv_qere_is_written_through_the_api(self):
        line = 'ויצא (הנער) [הנערח] אל השדה'
        write(self.path, self.TEXT + line + '\n')
        self.add(3, 'הנערח', 'הנערה', self.key + ':3',
                 scan_snippet(line, 'הנערח'))
        res, code = self.apply(self.key, [{'id': 3}])
        self.assertEqual(code, 200, res)
        self.assertEqual(res['applied'][0]['confidence'], 'exact')
        self.assertEqual(raw(self.path).decode('utf-8').splitlines()[3],
                         'ויצא (הנער) [הנערה] אל השדה')

    def test_reapplying_in_replace_mode_reports_already_applied(self):
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        once = raw(self.path)
        self._reapprove(1)
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        self.assertEqual(res.get('already_applied'), [1])
        self.assertEqual(raw(self.path), once)
        self.assertEqual(self.status_of(1), 'fixed')

    def test_doc_view_marks_an_applied_finding(self):
        self.apply(self.key, [{'id': 1}])
        self._reapprove(1)
        d = self.open_doc(self.key)
        item = [i for i in d['items'] if i['id'] == 1][0]
        self.assertEqual(item['anchor'].get('code'), 'already_applied')


class TestSpanUndo(FixerEnv):

    def test_undo_keeps_a_later_unrelated_change(self):
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        text = raw(self.path).decode('utf-8').replace('כותרת הספר',
                                                      'כותרת חדשה')
        write(self.path, text)
        out, code = self.undo(res['edit_id'])
        self.assertEqual(code, 200, out)
        self.assertEqual(raw(self.path).decode('utf-8'),
                         self.TEXT.replace('כותרת הספר', 'כותרת חדשה'))
        self.assertEqual(self.status_of(1), 'approved')

    def test_undo_after_a_later_batch_on_another_line(self):
        res1, _ = self.apply(self.key, [{'id': 1}])
        res2, code = self.apply(self.key, [{'id': 2}])
        self.assertEqual(code, 200, res2)
        out, code = self.undo(res1['edit_id'])
        self.assertEqual(code, 200, out)
        text = raw(self.path).decode('utf-8')
        self.assertIn('יותבת', text)
        self.assertIn('מחוז', text)

    def test_undo_refuses_when_the_span_itself_changed(self):
        res, code = self.apply(self.key, [{'id': 1}])
        text = raw(self.path).decode('utf-8').replace('יושבת', 'יושבות')
        write(self.path, text)
        before = raw(self.path)
        out, code = self.undo(res['edit_id'])
        self.assertEqual(code, 409, out)
        self.assertEqual(out['code'], 'file_changed_since_edit')
        self.assertEqual(raw(self.path), before)


# ---------------------------------------------------------------------------
# review round: manual picks, open intents, lock robustness
# ---------------------------------------------------------------------------

class TestManualPickNeedsIdentity(Env):
    """A refused anchor must never turn an unidentified line into a click
    target: the human may only choose among snippet-identified lines."""
    SCANNED = 'אמר יותבת וגם יותבת שם'
    OTHER = 'ועוד יותבת אחר כך'

    def setUp(self):
        super().setUp()
        # the scan saw SCANNED at line 2 with one flagged copy; a line was
        # inserted above it since
        self.path = write(os.path.join(self.lib, 'ב.txt'),
                          'שורה ראשונה\nשורה חדשה\n%s\n%s\n'
                          % (self.OTHER, self.SCANNED))
        self.key = 'file:ב.txt'
        self.add(1, 'יותבת', 'יושבת', self.key + ':2',
                 scan_snippet(self.SCANNED, 'יותבת'))

    def test_refused_anchor_points_at_the_located_line(self):
        d = self.open_doc(self.key)
        it = d['items'][0]
        self.assertFalse(it['anchor']['ok'])
        self.assertEqual(it['lineno'], 3)
        self.assertEqual(it['anchor']['manual_lines'], [3])
        with_tokens = [ln['n'] for ln in d['lines'] if 'tokens' in ln]
        self.assertEqual(with_tokens, [3])

    def test_pick_on_an_unidentified_line_is_refused(self):
        d = self.open_doc(self.key)
        before = raw(self.path)
        a, b = normalize.phrase_spans(self.OTHER, 'יותבת')[0][1:]
        for extra in ({'explicit_lineno': 2}, {}):
            with self.subTest(extra=extra):
                item = dict({'id': 1, 'explicit_start': a,
                             'explicit_end': b}, **extra)
                res, code = self.apply(self.key, [item],
                                       fingerprint=d['fingerprint'])
                self.assertEqual(code, 409, res)
                self.assertEqual(raw(self.path), before)

    def test_pick_on_the_identified_line_is_written(self):
        d = self.open_doc(self.key)
        spans = normalize.phrase_spans(self.SCANNED, 'יותבת')
        res, code = self.apply(self.key, [{
            'id': 1, 'explicit_lineno': 3, 'explicit_start': spans[1][1],
            'explicit_end': spans[1][2]}], fingerprint=d['fingerprint'])
        self.assertEqual(code, 200, res)
        lines = raw(self.path).decode('utf-8').splitlines()
        self.assertEqual(lines[2], self.OTHER)
        self.assertEqual(lines[3], 'אמר יותבת וגם יושבת שם')

    def test_line_mismatch_offers_no_manual_choice(self):
        write(self.path, 'שורה ראשונה\nשורה חדשה\n%s\n' % self.OTHER)
        d = self.open_doc(self.key)
        it = d['items'][0]
        self.assertEqual(it['anchor']['code'], 'line_mismatch')
        self.assertEqual(it['anchor'].get('manual_lines'), [])
        self.assertFalse(any('tokens' in ln for ln in d['lines']))


class TestOpenIntents(FixerEnv):

    def _failing_record(self):
        real = db.record_file_edit

        def boom(*a, **kw):
            raise sqlite3.OperationalError('database is locked')
        db.record_file_edit = boom
        return real

    def test_second_write_waits_for_the_first_record(self):
        from magiah.webui import journal
        real = self._failing_record()
        try:
            self.apply(self.key, [{'id': 1}])
            res, code = self.apply(self.key, [{'id': 2}])
        finally:
            db.record_file_edit = real
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'journal_pending')
        self.assertNotIn('מחוז', raw(self.path).decode('utf-8'))
        # once the database answers again, the first write is recorded and
        # the next one goes through
        res, code = self.apply(self.key, [{'id': 2}])
        self.assertEqual(code, 200, res)
        con = self.con()
        try:
            self.assertEqual(len(db.get_file_edits(con, self.key)), 2)
        finally:
            con.close()
        self.assertEqual(journal.conflicts(self.outdir), [])

    def test_a_chained_intent_is_committed(self):
        from magiah.webui import journal
        x, y = patcher.fingerprint(self.path), 'sha256:mid'
        a = journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': 'sha256:start', 'fp_after': y, 'finding_ids': [1],
            'detail': '[]'})
        b = journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': y, 'fp_after': x, 'finding_ids': [2],
            'detail': '[]'})
        journal.finish(self.outdir, b, 'committed')
        con = self.con()
        try:
            out = journal.recover(con, self.outdir)
            self.assertEqual([(o['jid'], o['state']) for o in out],
                             [(a, 'committed')])
            self.assertIsNotNone(db.find_file_edit_by_journal(con, a))
        finally:
            con.close()

    def test_a_conflict_can_be_acknowledged(self):
        from magiah.webui import journal
        jid = journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': 'sha256:x', 'fp_after': 'sha256:y',
            'finding_ids': [1]})
        con = self.con()
        try:
            journal.recover(con, self.outdir)
            self.assertEqual(len(self.open_doc(self.key)['journal_conflicts']),
                             1)
            fixer_api.resolve_conflict(con, self.outdir, {'jid': jid})
        finally:
            con.close()
        self.assertEqual(self.open_doc(self.key)['journal_conflicts'], [])


class TestLockRobustness(FixerEnv):

    def _run_with_timeout(self, fn, limit):
        res = {}

        def run():
            try:
                fn()
                res['r'] = 'ok'
            except Exception as e:
                res['r'] = getattr(e, 'code', type(e).__name__)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(limit)
        self.assertFalse(t.is_alive(), 'lock acquisition ignored its deadline')
        return res['r']

    def test_undeletable_stale_lock_respects_the_deadline(self):
        from magiah.webui import journal
        lf = write(self.path + journal.LOCK_SUFFIX, '{}')
        old = time.time() - journal.STALE_LOCK_SECONDS - 50
        os.utime(lf, (old, old))
        real_remove, real_rename = journal.os.remove, journal.os.rename

        def deny(*a, **kw):
            raise PermissionError(13, 'denied')
        journal.os.remove = journal.os.rename = deny
        try:
            r = self._run_with_timeout(
                lambda: journal.file_lock(self.path, timeout=0.3).__enter__(),
                3)
        finally:
            journal.os.remove, journal.os.rename = real_remove, real_rename
        self.assertEqual(r, 'file_busy')

    def test_delete_pending_lock_is_busy_not_an_error(self):
        from magiah.webui import journal
        real_open = journal.os.open

        def pending(path, *a, **kw):
            if path.endswith(journal.LOCK_SUFFIX):
                raise PermissionError(13, 'delete pending')
            return real_open(path, *a, **kw)
        journal.os.open = pending
        try:
            r = self._run_with_timeout(
                lambda: journal.file_lock(self.path, timeout=0.3).__enter__(),
                3)
        finally:
            journal.os.open = real_open
        self.assertEqual(r, 'file_busy')

    def test_held_lock_is_kept_fresh(self):
        from magiah.webui import journal
        real = journal.LOCK_REFRESH_SECONDS
        journal.LOCK_REFRESH_SECONDS = 0.05
        try:
            with journal.file_lock(self.path):
                lf = self.path + journal.LOCK_SUFFIX
                old = time.time() - 1000
                os.utime(lf, (old, old))
                time.sleep(0.3)
                self.assertGreater(os.path.getmtime(lf), old + 900)
        finally:
            journal.LOCK_REFRESH_SECONDS = real

    def test_page_load_does_not_wait_for_a_busy_file(self):
        from magiah.webui import journal
        journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': 'sha256:x', 'fp_after': 'sha256:y',
            'finding_ids': [1]})
        held, release = threading.Event(), threading.Event()

        def holder():
            with journal.file_lock(self.path):
                held.set()
                release.wait(10)
        t = threading.Thread(target=holder, daemon=True)
        t.start()
        held.wait(5)
        try:
            con = self.con()
            t0 = time.time()
            try:
                out = journal.recover(con, self.outdir, wait=False)
            finally:
                con.close()
            self.assertLess(time.time() - t0, 2)
            self.assertEqual([o['state'] for o in out], ['busy'])
        finally:
            release.set()
            t.join(5)

    def test_journal_is_compacted(self):
        from magiah.webui import journal
        for _ in range(30):
            jid = journal.begin(self.outdir, {'kind': 'apply',
                                              'path': self.path,
                                              'detail': 'x' * 2000})
            journal.finish(self.outdir, jid, 'committed')
        keep = journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'fp_before': 'sha256:x', 'fp_after': 'sha256:y',
            'finding_ids': [1]})
        con = self.con()
        try:
            journal.recover(con, self.outdir)
        finally:
            con.close()
        size = os.path.getsize(journal.journal_path(self.outdir))
        journal.compact(self.outdir, min_bytes=0)
        self.assertLess(os.path.getsize(journal.journal_path(self.outdir)),
                        size / 10)
        self.assertEqual([c['jid'] for c in journal.conflicts(self.outdir)],
                         [keep])

    def test_own_stale_temp_files_are_cleaned(self):
        stale = write(self.path + '.' + 'a' * 32 + '.tmp', 'x')
        old = time.time() - 3600
        os.utime(stale, (old, old))
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        self.assertFalse(os.path.exists(stale))


class TestRecordedEvidence(Env):

    def test_entry_in_place_requires_its_context(self):
        d = self.doc('אמר יושבת כאן\n')
        e = {'lineno': 0, 'post_start': 4, 'new': 'יושבת',
             'ctx_l': 'שונה לגמרי ', 'ctx_r': ' אחר'}
        self.assertFalse(patcher.entry_in_place(d, e))

    def test_unchanged_file_trusts_the_line_number_over_twins(self):
        line = 'ויאמר יותבת לו'
        body = ['שורה %d' % i for i in range(80)]
        body[10] = body[40] = line
        path = write(os.path.join(self.lib, 'מקור', 'ד.txt'),
                     '\n'.join(body) + '\n')
        key = 'file:מקור/ד.txt'
        for fid, n in ((1, 10), (2, 40)):
            self.add(fid, 'יותבת', 'יושבת', '%s:%d' % (key, n),
                     scan_snippet(line, 'יותבת'), doc='מקור/ד.txt',
                     extra=json.dumps({'book_scan': True}))
        data = raw(path)
        db.record_book_scan_source(self.outdir, {
            'doc': 'מקור/ד.txt', 'kind': 'library', 'path': path,
            'file_sha': patcher.fingerprint_bytes(data),
            'file_size': len(data)})
        d = self.open_doc(key)
        self.assertEqual([i['anchor'].get('ok') for i in d['items']],
                         [True, True])
        res, code = self.apply(key, [{'id': 1}], fingerprint=d['fingerprint'])
        self.assertEqual(code, 200, res)
        # our own write never moves lines, so the evidence stays valid
        d = self.open_doc(key)
        two = [i for i in d['items'] if i['id'] == 2][0]
        self.assertTrue(two['anchor'].get('ok'), two['anchor'])
        # a change made elsewhere does invalidate it (twins are back too)
        write(path, '\n'.join(body) + '\nעוד שורה\n')
        d = self.open_doc(key)
        two = [i for i in d['items'] if i['id'] == 2][0]
        self.assertEqual(two['anchor'].get('code'), 'ambiguous_line')

    def test_report_root_comes_from_report_db(self):
        libB = os.path.join(self.tmp, 'libB')
        os.makedirs(libB)
        line = 'אמר רבי יותבת בן זומא'
        write(os.path.join(self.lib, 'ספר.txt'), line + '\n')
        make_report(self.outdir, self.lib, [
            ('יותבת', 'יושבת', 'file:ספר.txt:0',
             scan_snippet(line, 'יותבת'))])
        db.import_all(self.outdir)
        con = self.con()
        fid = con.execute('SELECT id FROM findings').fetchone()[0]
        con.execute("INSERT INTO review VALUES(?,'approved',NULL,NULL,'t')",
                    (fid,))
        con.commit()
        con.close()
        self.set_config(libB)             # run_config says B, the scan said A
        res, code = self.apply('file:ספר.txt', [{'id': fid}],
                               fingerprint='sha256:x')
        self.assertEqual(code, 409, res)
        self.assertEqual(res['code'], 'source_mismatch')
        self.assertEqual(res['recorded'], os.path.abspath(self.lib))


class TestScanTimeFingerprint(Env):
    """A book scan records the fingerprint of the bytes it READ.

    It used to hash the file when the result was merged. A book edited while
    it was being scanned (context verification can take minutes) then looked
    unchanged, its rows skipped the parallel-verse (twin) check, and a
    correction landed on the verse the user had judged not_error.
    """
    P = 'ולכן צריך לעמוד כעבד לפני רבו קדבן היא בבקר'    # parallel verse
    S = 'ולכן צריך לעמוד כעבד לפני רבו קדבן היא בערב'    # the scanned one
    REL = 'מקור/מקביל.txt'

    def setUp(self):
        super().setUp()
        import pickle
        from collections import Counter
        from magiah import core
        words = set(normalize.tokenize(
            ' '.join((self.P, self.S, 'ויאמר משה אל העם לאמר',
                      'ויהי ערב ויהי בקר יום אחד הקדמה חדשה קרבן'))))
        freq = Counter({w: 10 ** 6 for w in words})
        freq['קדבן'] = 2
        with open(os.path.join(self.outdir, core.LEXICON_F), 'wb') as f:
            pickle.dump(freq, f)
        self.path = write(os.path.join(self.lib, *self.REL.split('/')),
                          'ויאמר משה אל העם לאמר\n%s\n%s\n'
                          'ויהי ערב ויהי בקר יום אחד\n' % (self.P, self.S))
        self.key = 'file:' + self.REL

    def scan(self):
        from magiah import book_scan
        return book_scan.scan_book(self.outdir, 'library', self.REL,
                                   library_dir=self.lib)

    def judge_and_apply(self):
        """P is correct here (not_error); S is the typo (approved)."""
        con = self.con()
        try:
            ids = {r[1].rsplit(':', 1)[1]: r[0] for r in con.execute(
                "SELECT id, unit FROM findings WHERE word = 'קדבן'")}
        finally:
            con.close()
        self.assertEqual(sorted(ids), ['1', '2'])
        con = self.con()
        try:
            db.set_status(con, self.outdir, [ids['1']], 'not_error')
            db.set_status(con, self.outdir, [ids['2']], 'approved')
        finally:
            con.close()
        d = self.open_doc(self.key)
        self.trusted = {i['trusted'] for i in d['items']}
        return self.apply(self.key, [{'id': ids['2']}],
                          fingerprint=d['fingerprint'])

    def test_the_book_read_carries_its_fingerprint(self):
        from magiah import book_source
        book = book_source.load_book('library', self.REL,
                                     library_dir=self.lib)
        self.assertEqual(book.file_sha, patcher.fingerprint(self.path))
        self.assertEqual(book.file_size, len(raw(self.path)))
        result = self.scan()
        self.assertEqual((result['file_sha'], result['file_size']),
                         (book.file_sha, book.file_size))

    def test_a_book_edited_during_its_scan_is_not_trusted(self):
        result = self.scan()
        scanned = raw(self.path)
        # edited while the scan was still running: a line inserted on top
        write(self.path, 'הקדמה חדשה\n' + scanned.decode('utf-8'))
        db.merge_book_scan(self.outdir, result)
        con = self.con()
        try:
            rec = db.get_source_root(con, 'doc:' + self.REL)
        finally:
            con.close()
        self.assertEqual(rec['file_sha'], patcher.fingerprint_bytes(scanned))
        self.assertEqual(rec['file_size'], len(scanned))
        before = raw(self.path)
        res, code = self.judge_and_apply()
        self.assertEqual(self.trusted, {False})
        # the scanned line number now holds P: never written there
        self.assertEqual(code, 409, res)
        self.assertEqual([f['code'] for f in res['failed']],
                         ['ambiguous_line'])
        self.assertEqual(raw(self.path), before)

    def test_an_unchanged_book_still_trusts_its_line_numbers(self):
        db.merge_book_scan(self.outdir, self.scan())
        res, code = self.judge_and_apply()
        self.assertEqual(self.trusted, {True})
        self.assertEqual(code, 200, res)
        lines = raw(self.path).decode('utf-8').splitlines()
        self.assertEqual(lines[1], self.P)
        self.assertEqual(lines[2], self.S.replace('קדבן', 'קרבן'))

    def test_trust_needs_the_exact_window_at_the_line(self):
        """Even a byte-identical book: a token-level match at the recorded
        line is no proof, so the twin check still runs."""
        d = self.doc('פתיחה\n%s\n%s\n' % (self.P, self.S))
        f = finding(self.S, 'קדבן', 'קרבן', lineno=1, trusted=True)
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'ambiguous_line')
        self.assertEqual(cm.exception.extra['candidate_lines'], [1, 2])

    def test_a_merge_without_a_read_fingerprint_is_not_trusted(self):
        db.record_book_scan_source(self.outdir, {
            'doc': self.REL, 'kind': 'library', 'path': self.path})
        con = self.con()
        try:
            rec = db.get_source_root(con, 'doc:' + self.REL)
        finally:
            con.close()
        self.assertEqual((rec['file_sha'], rec['file_size']), (None, None))


class TestCliBookScanRecordsRoot(Env):

    def test_cli_book_scan_then_fix(self):
        import pickle
        from collections import Counter
        from magiah import cli, core
        common = 'אמר רבי יושבת בן זומא דבר טוב כאן ועוד שם'.split()
        freq = Counter({w: 10 ** 6 for w in common})
        freq['יותבת'] = 2
        with open(os.path.join(self.outdir, core.LEXICON_F), 'wb') as f:
            pickle.dump(freq, f)
        line = 'ועוד אמר יותבת בן זומא דבר טוב כאן שם'
        path = write(os.path.join(self.lib, 'מקור', 'ספר.txt'), line + '\n')
        os.remove(os.path.join(self.outdir, 'run_config.json'))
        rc = cli.main(['book', '--library', self.lib, '--book-source',
                       'library', '--book', 'מקור/ספר.txt',
                       '--out', self.outdir])
        self.assertEqual(rc, 0)
        key = 'file:מקור/ספר.txt'
        con = self.con()
        fid = con.execute("SELECT id FROM findings WHERE word='יותבת' "
                          "AND unit=?", (key + ':0',)).fetchone()
        self.assertIsNotNone(fid)
        con.execute("INSERT OR REPLACE INTO review "
                    "VALUES(?,'approved',NULL,NULL,'t')", (fid[0],))
        con.commit()
        con.close()
        res, code = self.apply(key, [{'id': fid[0]}])
        self.assertEqual(code, 200, res)
        self.assertIn('יושבת', raw(path).decode('utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
