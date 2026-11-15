# -*- coding: utf-8 -*-
"""`magiah lexicon --update`: recount only the books that changed, and get
the very lexicon a full count gives."""
import contextlib
import io
import os
import pickle
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from magiah import cli, core, lexicon_update as lu, runstate
from magiah.config import Config

# Invented words only. ר' makes the next words count in the name list.
BOOKS = {
    'Src/a.txt': ['אמר ר\' פלמוני דברים טובים', 'דברים טובים וקשים'] * 3,
    'Src/b.txt': ['שמעתי מר\' זלמוני דבר חדש', 'כלבו גבלו טובים'],
    'Other/c.txt': ['חרבון גדלון מזרון', 'אמר ר\' פלמוני הכל'],
}


def small_cfg(**kw):
    base = dict(workers=1, n_chunks=2)
    base.update(kw)
    return Config(**base)


def write_book(root, rel, lines):
    p = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    # a change within the clock's resolution must still change the stamp
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


class InlinePool:
    """core._pool without processes: the same initializer and task
    functions, run here (a pool per count costs seconds on Windows). One
    test, Update.test_with_worker_processes, uses the real pool."""

    def __init__(self, spec, cfg, loads=None):
        core._init(spec, cfg.to_dict(), loads or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        close = getattr(core._W.get('corpus'), 'close', None)
        if close:
            close()
        core._W.clear()

    def imap_unordered(self, fn, items):
        return map(fn, items)


def lexicon_of(out):
    """What a later stage reads: the counts, the name list and its docs."""
    with open(os.path.join(out, core.LEXICON_F), 'rb') as f:
        lex = pickle.load(f)
    rec = core.load_names(out)
    return lex, (rec or {}).get('names'), (rec or {}).get('docs')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='magiah_lexupd_')
        self.lib = os.path.join(self.tmp, 'lib')
        for rel, lines in BOOKS.items():
            write_book(self.lib, rel, lines)
        self.spec = {'type': 'library', 'path': self.lib}
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        patcher = mock.patch.object(core, '_pool', InlinePool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def update(self, spec=None):
        return quiet(lu.update, spec or self.spec, small_cfg(), self.out)

    def full(self, spec=None):
        """A plain `magiah lexicon` of the same corpus, in its own folder."""
        out = tempfile.mkdtemp(prefix='full_', dir=self.tmp)
        quiet(core.build_lexicon, spec or self.spec, small_cfg(), out)
        return lexicon_of(out)

    def assertSameAsFull(self, spec=None):
        lex, names, docs = lexicon_of(self.out)
        flex, fnames, fdocs = self.full(spec)
        self.assertEqual(lex, flex)
        self.assertEqual(names, fnames)
        self.assertEqual(docs, fdocs)
        self.assertIsNotNone(names)


class Update(Base):
    def test_first_update_counts_everything(self):
        self.assertEqual(self.update(), 'full')
        self.assertSameAsFull()
        self.assertTrue(os.path.isfile(os.path.join(self.out, lu.BOOKS_F)))
        self.assertIsNone(lu.books_problem(self.out, self.spec)[1])

    def test_added_changed_removed_books_give_the_full_lexicon(self):
        self.update()
        write_book(self.lib, 'Other/d.txt', ['ספר חדש ר\' גבלון כאן'])
        write_book(self.lib, 'Src/b.txt', ['שמעתי מר\' זלמוני דבר ישן'])
        os.remove(os.path.join(self.lib, 'Other', 'c.txt'))
        st = lu.status(self.spec, self.out)
        self.assertEqual(st['mode'], 'incremental')
        self.assertEqual((st['added'], st['changed'], st['removed']),
                         (['Other/d.txt'], ['Src/b.txt'], ['Other/c.txt']))
        self.assertEqual(self.update(), 'incremental')
        self.assertSameAsFull()
        # and again: the side file it wrote serves the next update
        write_book(self.lib, 'Src/a.txt', ['טובים טובים'])
        self.assertEqual(self.update(), 'incremental')
        self.assertSameAsFull()

    def test_a_word_that_leaves_the_corpus_leaves_the_lexicon(self):
        self.update()
        os.remove(os.path.join(self.lib, 'Other', 'c.txt'))
        self.update()
        lex, names, docs = lexicon_of(self.out)
        self.assertNotIn('חרבון', lex)
        self.assertNotIn('Other/c.txt', docs)
        self.assertSameAsFull()

    def test_with_worker_processes(self):
        mock.patch.stopall()            # the real core._pool
        self.update()
        write_book(self.lib, 'Other/d.txt', ["ספר חדש ר' גבלון כאן"])
        write_book(self.lib, 'Src/b.txt', ["שמעתי מר' זלמוני דבר ישן"])
        os.remove(os.path.join(self.lib, 'Other', 'c.txt'))
        self.assertEqual(self.update(), 'incremental')
        self.assertSameAsFull()

    def test_an_emptied_book_is_not_a_counted_document(self):
        self.update()
        with open(os.path.join(self.lib, 'Other', 'c.txt'), 'w'):
            pass                        # no line at all
        st = os.stat(os.path.join(self.lib, 'Other', 'c.txt'))
        os.utime(os.path.join(self.lib, 'Other', 'c.txt'),
                 ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.update()
        self.assertSameAsFull()
        self.assertNotIn('Other/c.txt', lexicon_of(self.out)[2])

    def test_nothing_changed_writes_nothing(self):
        self.update()
        before = {n: os.stat(os.path.join(self.out, n)).st_mtime_ns
                  for n in lu.VERSION_FILES}
        self.assertEqual(lu.status(self.spec, self.out)['mode'], 'none')
        with mock.patch.object(lu, '_run') as run:
            self.assertEqual(self.update(), 'none')
        run.assert_not_called()
        after = {n: os.stat(os.path.join(self.out, n)).st_mtime_ns
                 for n in lu.VERSION_FILES}
        self.assertEqual(before, after)

    def test_a_plain_lexicon_run_retires_the_side_file(self):
        self.update()
        quiet(core.build_lexicon, self.spec, small_cfg(), self.out)
        head, why = lu.books_problem(self.out, self.spec)
        self.assertIsNone(head)
        self.assertIn('מילון אחר', why)
        write_book(self.lib, 'Src/b.txt', ['מילה אחרת לגמרי'])
        self.assertEqual(self.update(), 'full')
        self.assertSameAsFull()

    def test_another_corpus_is_counted_in_full(self):
        self.update()
        other = os.path.join(self.tmp, 'lib2')
        shutil.copytree(self.lib, other)
        spec = {'type': 'library', 'path': other}
        self.assertEqual(lu.status(spec, self.out)['mode'], 'full')
        self.assertEqual(self.update(spec), 'full')
        self.assertSameAsFull(spec)

    def test_a_side_file_that_does_not_add_up_is_not_trusted(self):
        self.update()
        path = os.path.join(self.out, lu.BOOKS_F)
        with open(path, 'rb') as f:
            head, packed = pickle.load(f), pickle.load(f)
        # counts the lexicon never had: subtracting them would go negative
        cb, nb = packed['Src/b.txt']
        packed['Src/b.txt'] = (lu._pack({'זלמוני': 999}), nb)
        st = os.stat(os.path.join(self.out, core.LEXICON_F))
        with open(path, 'wb') as f:
            pickle.dump(head, f, protocol=4)
            pickle.dump(packed, f, protocol=4)
        os.utime(os.path.join(self.out, core.LEXICON_F),
                 ns=(st.st_atime_ns, st.st_mtime_ns))
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        self.assertEqual(self.update(), 'full')
        self.assertSameAsFull()

    def test_a_damaged_side_file_means_a_full_count(self):
        self.update()
        with open(os.path.join(self.out, lu.BOOKS_F), 'wb') as f:
            f.write(b'not a pickle')
        self.assertIsNone(lu.books_problem(self.out, self.spec)[0])
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        self.assertEqual(self.update(), 'full')
        self.assertSameAsFull()


class Interrupted(Base):
    def snapshot(self):
        out = {}
        for n in lu.VERSION_FILES:
            p = os.path.join(self.out, n)
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    out[n] = f.read()
        return out

    def test_a_failed_count_changes_nothing(self):
        self.update()
        before = self.snapshot()
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        with mock.patch.object(lu, '_run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.update()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.update(), 'incremental')
        self.assertSameAsFull()

    def test_cut_while_moving_files_into_place(self):
        # stop at every move of the swap: whatever is left, the lexicon is
        # readable and the next update gives the full lexicon
        for k in range(len(lu.VERSION_FILES) * 2):
            with self.subTest(k=k):
                shutil.rmtree(self.out)
                os.makedirs(self.out)
                write_book(self.lib, 'Src/b.txt', BOOKS['Src/b.txt'])
                self.update()
                write_book(self.lib, 'Src/b.txt', [f'שמעתי דבר {k}'])
                real, calls = os.replace, []

                def flaky(src, dst):
                    if os.path.dirname(os.path.abspath(dst)) == \
                            os.path.abspath(self.out) or \
                            os.path.basename(os.path.dirname(dst)) == \
                            lu.PREV_DIR:
                        calls.append(dst)
                        if len(calls) > k:
                            raise OSError('cut')
                    return real(src, dst)
                with mock.patch.object(lu.os, 'replace', flaky):
                    try:
                        self.update()
                    except OSError:
                        pass
                lex = os.path.join(self.out, core.LEXICON_F)
                if os.path.exists(lex):
                    with open(lex, 'rb') as f:
                        self.assertIsInstance(pickle.load(f), dict)
                self.update()
                self.assertSameAsFull()


class Rollback(Base):
    def test_rollback_swaps_and_swaps_back(self):
        self.update()
        old = lexicon_of(self.out)
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר חדש לגמרי'])
        self.update()
        new = lexicon_of(self.out)
        self.assertNotEqual(old[0], new[0])
        quiet(lu.rollback, self.out)
        self.assertEqual(lexicon_of(self.out), old)
        # the version it restored can be updated again
        self.assertIsNotNone(lu.books_problem(self.out, self.spec)[0])
        quiet(lu.rollback, self.out)
        self.assertEqual(lexicon_of(self.out), new)
        self.assertEqual(self.update(), 'none')

    def test_the_previous_version_of_a_first_update_is_the_plain_one(self):
        quiet(core.build_lexicon, self.spec, small_cfg(), self.out)
        plain = lexicon_of(self.out)
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        self.update()
        quiet(lu.rollback, self.out)
        self.assertEqual(lexicon_of(self.out), plain)
        self.assertFalse(os.path.exists(os.path.join(self.out, lu.BOOKS_F)))

    def test_nothing_to_roll_back_to(self):
        quiet(core.build_lexicon, self.spec, small_cfg(), self.out)
        with self.assertRaises(core.StageError):
            lu.rollback(self.out)


class OtherSources(Base):
    def make_db(self, path, books, hashes=True):
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE book(id INTEGER PRIMARY KEY, title TEXT'
                    + (', contentHash INTEGER' if hashes else '') + ')')
        con.execute('CREATE TABLE line(id INTEGER PRIMARY KEY, bookId '
                    'INTEGER, lineIndex INTEGER, heRef TEXT, content TEXT)')
        lid = 0
        for bid, (h, lines) in books.items():
            con.execute('INSERT INTO book VALUES (?, ?' + (', ?' if hashes
                                                          else '') + ')',
                        (bid, f'b{bid}', h)[:3 if hashes else 2])
            con.execute('DELETE FROM line WHERE bookId = ?', (bid,))
            for i, text in enumerate(lines):
                lid += 1
                con.execute('INSERT INTO line VALUES (?, ?, ?, ?, ?)',
                            (lid, bid, i, f'r{i}', text))
        con.commit()
        con.close()

    def db_spec(self, path):
        return {'type': 'sqlite', 'path': path, 'table': 'line',
                'id_col': 'id', 'text_col': 'content', 'preset': 'otzaria'}

    def test_otzaria_db_books_by_content_hash(self):
        db = os.path.join(self.tmp, 'seforim.db')
        books = {1: (11, BOOKS['Src/a.txt']), 2: (22, BOOKS['Src/b.txt']),
                 3: (33, BOOKS['Other/c.txt'])}
        self.make_db(db, books)
        spec = self.db_spec(db)
        self.assertEqual(self.update(spec), 'full')
        self.assertSameAsFull(spec)
        os.remove(db)
        books[2] = (23, ['שמעתי דבר אחר'])
        del books[3]
        books[4] = (44, ['ספר רביעי ר\' גבלון'])
        self.make_db(db, books)
        st = lu.status(spec, self.out)
        self.assertEqual((st['added'], st['changed'], st['removed'],
                          st['unchanged']), (['4'], ['2'], ['3'], 1))
        self.assertEqual(self.update(spec), 'incremental')
        self.assertSameAsFull(spec)

    def test_db_without_content_hash_recounts_every_book(self):
        db = os.path.join(self.tmp, 'old.db')
        self.make_db(db, {1: (0, ['אחת שתיים']), 2: (0, ['שלש ארבע'])},
                     hashes=False)
        spec = self.db_spec(db)
        self.update(spec)
        st = lu.status(spec, self.out)
        self.assertEqual(st['changed'], ['1', '2'])
        self.assertEqual(self.update(spec), 'incremental')
        self.assertSameAsFull(spec)

    def test_hybrid_library_and_db(self):
        db = os.path.join(self.tmp, 'hybrid.db')
        self.make_db(db, {1: (11, ['ספריא אחת שתיים']),
                          2: (22, ['לא מספריא כלל'])})
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE source(id INTEGER PRIMARY KEY, name TEXT)')
        con.executemany('INSERT INTO source VALUES (?, ?)',
                        [(1, 'Sefaria'), (2, 'Other')])
        con.execute('ALTER TABLE book ADD COLUMN sourceId INTEGER')
        con.execute('UPDATE book SET sourceId = id')
        con.commit()
        con.close()
        spec = {'type': 'hybrid', 'path': self.lib, 'db': db}
        self.update(spec)
        self.assertEqual(sorted(lu.doc_stamps(spec)),
                         ['1', 'Other/c.txt', 'Src/a.txt', 'Src/b.txt'])
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        con = sqlite3.connect(db)
        con.execute("UPDATE line SET content = 'ספריא חדשה' WHERE bookId = 1")
        con.execute('UPDATE book SET contentHash = 12 WHERE id = 1')
        con.commit()
        con.close()
        st = lu.status(spec, self.out)
        self.assertEqual(st['changed'], ['1', 'Src/b.txt'])
        self.assertEqual(self.update(spec), 'incremental')
        self.assertSameAsFull(spec)

    def test_textdir(self):
        spec = {'type': 'textdir', 'path': self.lib}
        self.update(spec)
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        st = lu.status(spec, self.out)
        self.assertEqual(st['changed'], [os.path.join('Src', 'b.txt')])
        self.assertEqual(self.update(spec), 'incremental')
        self.assertSameAsFull(spec)

    def test_plain_sqlite_has_no_books_to_compare(self):
        db = os.path.join(self.tmp, 'plain.db')
        self.make_db(db, {1: (0, ['אחת שתיים'])}, hashes=False)
        spec = {'type': 'sqlite', 'path': db, 'table': 'line',
                'id_col': 'id', 'text_col': 'content', 'doc_col': 'bookId'}
        self.assertEqual(lu.status(spec, self.out)['mode'], 'unsupported')
        self.assertEqual(self.update(spec), 'unsupported')
        self.assertSameAsFull(spec)
        self.assertFalse(os.path.exists(os.path.join(self.out, lu.BOOKS_F)))


class Cli(Base):
    def run_cli(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(err):
            code = cli.main(list(argv) + ['--out', self.out])
        return code, out.getvalue(), err.getvalue()

    def test_plain_lexicon_is_unchanged(self):
        code, _, _ = self.run_cli('lexicon', '--library', self.lib,
                                  '--workers', '1', '--n-chunks', '2')
        self.assertFalse(code)
        self.assertEqual(sorted(os.listdir(self.out)),
                         ['coverage_lexicon.json', 'lexicon.pkl',
                          'names.pkl', 'run_config.json', 'run_state'])

    def test_update_status_rollback(self):
        args = ('--library', self.lib, '--workers', '1', '--n-chunks', '2')
        self.assertFalse(self.run_cli('lexicon', '--update', *args)[0])
        code, _, err = self.run_cli('lexicon', '--rollback')
        self.assertEqual(code, 1)          # nothing to roll back to
        self.assertIn('אין גרסה קודמת', err)
        # ... which is no failed lexicon run
        self.assertIsNone(runstate.scan_problem(self.out))
        write_book(self.lib, 'Src/b.txt', ['שמעתי דבר'])
        code, out, _ = self.run_cli('lexicon', '--status')
        self.assertFalse(code)
        self.assertIn('השתנו 1', out)
        self.assertFalse(self.run_cli('lexicon', '--update')[0])
        self.assertSameAsFull()
        self.assertFalse(self.run_cli('lexicon', '--rollback')[0])
        self.assertIsNone(runstate.scan_problem(self.out))

    def test_flags_belong_to_the_lexicon_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(['detect', '--update', '--out', self.out])
            with self.assertRaises(SystemExit):
                cli.main(['lexicon', '--update', '--rollback',
                          '--out', self.out])


if __name__ == '__main__':
    unittest.main()
