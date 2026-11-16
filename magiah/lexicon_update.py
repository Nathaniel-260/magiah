# -*- coding: utf-8 -*-
"""Incremental lexicon update: recount only the books that changed.

``magiah lexicon`` counts the whole corpus every time. When the library
changes a little (a new Otzaria release with a few books added or fixed), an
update recounts only those books and gives the very lexicon a full count
would give. Nothing here changes ``magiah lexicon`` itself.

``magiah lexicon --update``
    The first update makes a full count, and keeps each book's own counts in
    a side file, ``lexicon_books.pkl``. Every later update compares each
    book's stamp with the one recorded there, subtracts the old counts of the
    books that changed or are gone, and adds the new counts of the books that
    changed or are new. When nothing changed, nothing is written.
``magiah lexicon --status``
    What an update would do now (read only).
``magiah lexicon --rollback``
    Swaps the lexicon with the one the last update replaced. Running it again
    swaps back; nothing is deleted.

What an update rewrites: ``lexicon.pkl``, ``names.pkl`` (the name list, built
in the same pass), ``coverage_lexicon.json`` and the side file — the whole
output of the lexicon stage. Later outputs are derived from the lexicon like
after ``magiah lexicon``: rerun ``detect`` and ``locate`` (the realword
counts of a book scan check the lexicon's stamp and are recounted).

The stamp of a book:

* a seforim.db book — the ``contentHash`` Otzaria records for it, and its
  number of lines. A database without ``contentHash`` has no stamp: every
  update recounts every book (correct, only not faster);
* a text file (``--textdir``, ``--library`` and the files of a hybrid scan) —
  its size and modification time.

A plain SQLite table (``--sqlite``) has no book identity to rely on, so an
update there is a full count.

The side file is used only when it belongs to the lexicon on disk: the same
``lexicon_id`` (coverage_lexicon.json), the same ``lexicon.pkl`` (size and
modification time), the same corpus, the same counting rule, the name list
of that lexicon, and a complete lexicon (no ``--allow-unread`` gaps).
Otherwise — a plain ``magiah lexicon`` ran since, a copied or older folder —
the update makes a full count, and says why.

Safety (``_exchange``, used by ``--update`` and ``--rollback`` alike): the
new version is first written completely to ``lexicon_new/`` (same folder,
so the same volume). It is then put in place in two phases, with a journal
(``lexicon_txn.json``, written atomically before the first move): the
current files go one by one to ``lexicon_old/``, then the new ones go one by
one into the folder. The journal is deleted after the last move: that is
the commit. A failure before the commit (an OSError, Ctrl-C) puts every
file back where it was, so the folder holds the previous version complete;
a process killed before the commit is repaired the same way by the next
``--update``, ``--rollback`` or ``magiah lexicon`` (``recover``). After the
commit ``lexicon_old/`` becomes ``lexicon_prev/`` by renames, the former
``lexicon_prev/`` set aside first, so ``lexicon_prev/`` is always a whole
version or absent (a cut there is completed by ``recover``).

The guarantee: the lexicon folder is always a complete, consistent set of
the old version or of the new one, never a mix, except for the moments
between two renames of a killed process, which ``recover`` repairs before
anything reads or writes the set again. Single renames are atomic
(os.replace); the set as a whole is not, which is why the journal exists.
The files are not fsync-ed except the journal: a power loss (not just a
crash of the process) can still lose data the OS had not yet flushed. Any
OSError of an update or rollback is reported as a core.StageError, in
Hebrew.
"""
import json
import os
import pickle
import shutil
import time
import zlib
from collections import Counter

from . import core
from .corpus import OTZARIA_DB, TextDirCorpus, make_corpus
from .corpus_hybrid import SEFARIA_SOURCE, LibraryCorpus
from .normalize import tokenize
from .textsource import OtzariaDB, ReadStats, db_read_errors

BOOKS_F = 'lexicon_books.pkl'
NEW_DIR = 'lexicon_new'
PREV_DIR = 'lexicon_prev'
OLD_DIR = 'lexicon_old'              # the set being replaced, during a swap
TRASH_DIR = 'lexicon_trash'          # the dropped older version
TXN_F = 'lexicon_txn.json'           # the journal of a swap in progress
# bump when the side file's layout or the per-book counting changes
FORMAT = 1


def _coverage_f():
    return core.COVERAGE_F.format(stage='lexicon')


# the files of one lexicon version, in the order they are moved into place:
# the side file last (it vouches for the others)
VERSION_FILES = (core.LEXICON_F, core.NAMES_F, 'coverage_lexicon.json',
                 BOOKS_F)


class _Stale(Exception):
    """The side file does not add up with the lexicon: count in full."""


# ---------------------------------------------------------------------------
# book stamps
# ---------------------------------------------------------------------------

def _file_stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None                     # recounted (and then found gone)
    return [st.st_size, st.st_mtime_ns]


def _db_stamps(path, sefaria_only=False):
    """``{str(book id): [contentHash, lines] or None}`` of every book with
    lines — the books the corpus pass reads (``sefaria_only``: those a hybrid
    scan reads from the database)."""
    with OtzariaDB(path) as odb, db_read_errors(path):
        con = odb.con
        cols = {r[1] for r in con.execute('PRAGMA table_info(book)')}
        hashes = {}
        if 'contentHash' in cols:
            hashes = dict(con.execute('SELECT id, contentHash FROM book'))
        sql = 'SELECT bookId, COUNT(*) FROM line'
        args = ()
        if sefaria_only:
            # the rule of HybridCorpus.iter_texts_docs
            sql += (' WHERE bookId IN (SELECT b.id FROM book b JOIN source s '
                    'ON s.id = b.sourceId WHERE s.name = ?)')
            args = (SEFARIA_SOURCE,)
        out = {}
        for book, lines in con.execute(sql + ' GROUP BY bookId', args):
            h = hashes.get(book)
            out[str(book)] = None if h is None else [h, lines]
        return out


def doc_stamps(spec):
    """``{doc: stamp}`` for every document of the corpus `spec` — doc ids as
    the corpus pass names them — or None when the corpus has no documents an
    update can tell apart (a plain SQLite table). A stamp of None means
    "unknown": that document is recounted by every update."""
    kind = spec.get('type')
    if kind == 'textdir':
        corpus = TextDirCorpus(spec)
        corpus.check_root()
        return {os.path.relpath(fp, corpus.path): _file_stamp(fp)
                for fp in corpus._files()}
    if kind in ('library', 'hybrid'):
        lib = LibraryCorpus({'type': 'library', 'path': spec.get('path')})
        lib.check_root()
        out = {rel: _file_stamp(os.path.join(lib.path, *rel.split('/')))
               for rel in lib._files()}
        if kind == 'hybrid':
            out.update(_db_stamps(spec.get('db') or OTZARIA_DB,
                                  sefaria_only=True))
        return out
    if kind == 'sqlite' and spec.get('preset') == 'otzaria':
        return _db_stamps(spec['path'])
    return None


# ---------------------------------------------------------------------------
# counting, per book
# ---------------------------------------------------------------------------

def _pack(counts):
    """One book's counts, compact: ``word<TAB>count`` lines, sorted, zlib.
    A token never holds a tab or a newline (normalize.TOKEN_RE). Measured on
    the Otzaria library: 40% smaller than a compressed pickle."""
    text = '\n'.join(f'{w}\t{n}' for w, n in sorted(counts.items()))
    return zlib.compress(text.encode('utf-8'), 6)


def _unpack(blob):
    text = zlib.decompress(blob).decode('utf-8')
    if not text:
        return {}
    return {w: int(n) for w, n in (ln.split('\t') for ln in text.split('\n'))}


def _doc_texts(corpus, doc):
    """The texts of one document, read as the corpus pass reads them."""
    kind = corpus.spec.get('type')
    if kind == 'textdir':
        for _, _, text in corpus.iter_texts_docs(
                [os.path.join(corpus.path, doc)]):
            yield text
    elif kind == 'library' or (kind == 'hybrid' and not doc.isdigit()):
        lib = corpus if kind == 'library' else corpus.library
        for _, _, text in lib.iter_texts_docs([doc]):
            yield text
    else:                               # a seforim.db book
        for _, _, text in corpus._otzaria().book_lines(int(doc),
                                                       corpus.stats):
            yield text


def _count_task(task):
    """Worker: ``task`` is ``('chunk', chunk)`` (a corpus chunk, as the
    lexicon stage reads it) or ``('docs', [doc, ...])``. Returns the totals,
    each document's packed counts, and the read counters."""
    core._chunk_stats_begin()
    corpus = core._W['corpus']
    per = {}

    def add(doc, text):
        toks = tokenize(text)
        c, nc = per.setdefault(doc, (Counter(), Counter()))
        c.update(toks)
        core.count_names(toks, nc)

    if task[0] == 'chunk':
        for _, doc, text in corpus.iter_texts_docs(task[1]):
            add(doc, text)
    else:
        for doc in task[1]:
            for text in _doc_texts(corpus, doc):
                add(doc, text)
    total, names = Counter(), Counter()
    for c, nc in per.values():
        total.update(c)
        names.update(nc)
    packed = {doc: (_pack(c), _pack(nc)) for doc, (c, nc) in per.items()}
    return total, names, packed, core._chunk_stats_end()


def _run(spec, cfg, tasks, label):
    """Count `tasks` in the worker pool: ``(lex, names, packed, stats)``."""
    t0 = time.time()
    lex, names, packed = Counter(), Counter(), {}
    stats = ReadStats()
    if not tasks:
        return lex, names, packed, stats
    with core._pool(spec, cfg) as pool:
        for i, (c, nc, pk, st) in enumerate(
                pool.imap_unordered(_count_task, tasks), 1):
            lex.update(c)
            names.update(nc)
            for doc, (cb, nb) in pk.items():
                if doc in packed:
                    # a book split over two chunks (line-id ranges)
                    oc, on = packed[doc]
                    cb = _pack(Counter(_unpack(oc)) + Counter(_unpack(cb)))
                    nb = _pack(Counter(_unpack(on)) + Counter(_unpack(nb)))
                packed[doc] = (cb, nb)
            stats.add(ReadStats.from_dict(st))
            print(f'  [lexicon] {label} {i}/{len(tasks)}  '
                  f'types={len(lex):,}  ({time.time()-t0:.0f}s)', flush=True)
    return lex, names, packed, stats


def _doc_tasks(docs, n):
    """The documents to recount, in `n` tasks or fewer."""
    docs = sorted(docs)
    n = max(1, min(n, len(docs)))
    return [('docs', docs[i::n]) for i in range(n)]


# ---------------------------------------------------------------------------
# the side file
# ---------------------------------------------------------------------------

def _lexicon_stamp(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def _read_books(out_dir, counts=False):
    """``(head, packed)`` of the side file (`packed` only with `counts`)."""
    with open(os.path.join(out_dir, BOOKS_F), 'rb') as f:
        head = pickle.load(f)
        packed = pickle.load(f) if counts else None
    return head, packed


def books_problem(out_dir, spec):
    """``(head, None)`` when the side file belongs to the lexicon on disk
    and to `spec` — an update may then recount only what changed — else
    ``(None, reason)``, the reason in Hebrew."""
    path = os.path.join(out_dir, BOOKS_F)
    if not os.path.isfile(os.path.join(out_dir, core.LEXICON_F)):
        return None, 'אין מילון בתיקייה'
    if not os.path.isfile(path):
        return None, ('אין קובץ ספירות לפי ספר (נוצר בעדכון הראשון; המילון '
                      'הקיים נבנה בבנייה מלאה)')
    try:
        head, _ = _read_books(out_dir)
    except Exception:                   # noqa: BLE001 — any damage
        return None, 'קובץ הספירות לפי ספר פגום'
    if not isinstance(head, dict) or head.get('format') != FORMAT \
            or head.get('names_format') != core.NAMES_FORMAT \
            or not isinstance(head.get('stamps'), dict):
        return None, 'קובץ הספירות לפי ספר נכתב בגרסה אחרת של התוכנה'
    if not head.get('lexicon_id') \
            or head['lexicon_id'] != core._lexicon_id(out_dir) \
            or head.get('lexicon_stamp') != _lexicon_stamp(
                os.path.join(out_dir, core.LEXICON_F)):
        return None, ('קובץ הספירות לפי ספר שייך למילון אחר (המילון נבנה '
                      'מחדש או הוחלף מאז)')
    if core._latest_coverage(out_dir, 'lexicon')[0] != 'complete':
        return None, 'המילון הקיים חלקי (נבנה בלי חלק מהשורות)'
    if core._corpus_key(head.get('spec')) != core._corpus_key(spec):
        return None, 'המילון הקיים נבנה ממקור נתונים אחר'
    if core.load_names(out_dir) is None:
        return None, 'רשימת השמות של המילון חסרה או אינה שלו'
    return head, None


def diff_stamps(old, new):
    """``(added, changed, removed, unchanged)`` document ids. A document
    without a stamp (None) counts as changed every time."""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed, unchanged = [], 0
    for doc in set(new) & set(old):
        if new[doc] is None or new[doc] != old[doc]:
            changed.append(doc)
        else:
            unchanged += 1
    return added, sorted(changed), removed, unchanged


# ---------------------------------------------------------------------------
# status / update / rollback
# ---------------------------------------------------------------------------

def status(spec, out_dir):
    """What an update would do now — nothing is written. A dict:
    ``mode`` ('none', 'incremental', 'full' or 'unsupported'), ``reason``,
    the document lists and ``prev`` (is there a version to roll back to)."""
    out = {'mode': 'none', 'reason': None, 'added': [], 'changed': [],
           'removed': [], 'unchanged': 0,
           'prev': os.path.isfile(os.path.join(out_dir, PREV_DIR,
                                               core.LEXICON_F))}
    new = doc_stamps(spec)
    if new is None:
        out.update(mode='unsupported',
                   reason='למקור נתונים מסוג זה אין זיהוי ספרים (‎--sqlite‎)')
        return out
    head, why = books_problem(out_dir, spec)
    if head is None:
        out.update(mode='full', reason=why)
        return out
    added, changed, removed, unchanged = diff_stamps(head['stamps'], new)
    out.update(added=added, changed=changed, removed=removed,
               unchanged=unchanged)
    if added or changed or removed:
        out['mode'] = 'incremental'
    return out


def print_status(spec, out_dir):
    try:
        st = status(spec, out_dir)
    except OSError as e:
        raise core.StageError(
            f'קריאת מצב המילון נכשלה בגלל שגיאת קבצים: {e}') from e
    if st['mode'] == 'none':
        print(f'[lexicon] המילון מעודכן: אף ספר לא נוסף, השתנה או נמחק '
              f'({st["unchanged"]:,} ספרים).', flush=True)
    elif st['mode'] == 'incremental':
        print(f'[lexicon] עדכון ייספור מחדש רק את מה שהשתנה: נוספו '
              f'{len(st["added"]):,}, השתנו {len(st["changed"]):,}, נמחקו '
              f'{len(st["removed"]):,}, ללא שינוי {st["unchanged"]:,}.',
              flush=True)
    else:
        print(f'[lexicon] עדכון יבנה את המילון מחדש במלואו: {st["reason"]}.',
              flush=True)
    if st['prev']:
        print(f'[lexicon] יש גרסה קודמת לחזרה (‎--rollback‎): '
              f'{os.path.join(out_dir, PREV_DIR)}', flush=True)
    return 0


def _counts_from(out_dir, added, changed, removed, spec, cfg):
    """The current lexicon with `changed` and `removed` taken out and
    `added` and `changed` counted anew."""
    with open(os.path.join(out_dir, core.LEXICON_F), 'rb') as f:
        lex = Counter(pickle.load(f))
    rec = core.load_names(out_dir)
    names = Counter(rec['names'])
    seen = set(rec['docs'])
    _, packed = _read_books(out_dir, counts=True)

    def take(total, counts):
        for w, n in counts.items():
            left = total.get(w, 0) - n
            if left < 0:
                raise _Stale(w)
            if left:
                total[w] = left
            else:
                del total[w]

    for doc in changed + removed:
        if doc in packed:
            cb, nb = packed.pop(doc)
            take(lex, _unpack(cb))
            take(names, _unpack(nb))
        seen.discard(doc)
    c, nc, pk, stats = _run(spec, cfg, _doc_tasks(added + changed,
                                                  cfg.n_chunks), 'books')
    lex.update(c)
    names.update(nc)
    packed.update(pk)
    seen.update(pk)
    return lex, names, seen, packed, stats


def _file_error(e, what, out_dir):
    return core.StageError(
        f'{what} נכשל בגלל שגיאת קבצים: {e}\n'
        f'המילון בתיקייה {out_dir} נשאר שלם (אם הפעולה נקטעה באמצע, ההרצה '
        f'הבאה של ‎magiah lexicon --update‎ או ‎--rollback‎ תשחזר אותו). '
        f'אפשר להריץ שוב.')


def update(spec, cfg, out_dir):
    """``magiah lexicon --update``: recount the books that changed (a full
    count when the side file cannot be used). Returns the mode. A file
    error (OSError) is raised as core.StageError."""
    try:
        return _update(spec, cfg, out_dir)
    except OSError as e:
        raise _file_error(e, 'עדכון המילון', out_dir) from e


def _update(spec, cfg, out_dir):
    recover(out_dir)
    t0 = time.time()
    stamps = doc_stamps(spec)           # before reading: a change made
    if stamps is None:                  # meanwhile is seen next time
        print('[lexicon] למקור נתונים מסוג זה אין זיהוי ספרים — '
              'בונה את המילון במלואו.', flush=True)
        core.build_lexicon(spec, cfg, out_dir)
        return 'unsupported'
    head, why = books_problem(out_dir, spec)
    if head is not None:
        added, changed, removed, unchanged = diff_stamps(head['stamps'],
                                                         stamps)
        if not (added or changed or removed):
            print(f'[lexicon] המילון מעודכן: אף ספר לא נוסף, השתנה או נמחק '
                  f'({unchanged:,} ספרים). לא נכתב דבר.', flush=True)
            return 'none'
        print(f'[lexicon] update: added {len(added):,}, changed '
              f'{len(changed):,}, removed {len(removed):,}, unchanged '
              f'{unchanged:,}', flush=True)
        try:
            lex, names, seen, packed, stats = _counts_from(
                out_dir, added, changed, removed, spec, cfg)
            mode = 'incremental'
            summary = {'mode': mode, 'added': len(added),
                       'changed': len(changed), 'removed': len(removed),
                       'unchanged': unchanged}
            tasks = len(_doc_tasks(added + changed, cfg.n_chunks)) \
                if added or changed else 0
        except _Stale:
            head, why = None, 'קובץ הספירות לפי ספר אינו תואם את המילון'
    if head is None:
        print(f'[lexicon] בנייה מלאה: {why}.', flush=True)
        corpus = make_corpus(spec)
        chunks = corpus.chunks(cfg.n_chunks)
        lex, names, packed, stats = _run(
            spec, cfg, [('chunk', ch) for ch in chunks], 'chunk')
        seen = set(packed)
        mode = 'full'
        summary = {'mode': mode, 'books': len(stamps)}
        tasks = len(chunks)
    print(f'[lexicon] tokens={sum(lex.values()):,}  types={len(lex):,}  '
          f'time={time.time()-t0:.0f}s', flush=True)
    _save(spec, cfg, out_dir, lex, names, seen, packed, stamps, stats,
          tasks, summary)
    return mode


def _save(spec, cfg, out_dir, lex, names, seen, packed, stamps, stats,
          tasks, summary):
    """Write the new version to NEW_DIR, then move it into place — the
    files it replaces go to PREV_DIR."""
    lexicon_id = core._new_lexicon_id()
    extra = {'tokens': sum(lex.values()), 'types': len(lex),
             'chunks': tasks, 'lexicon_id': lexicon_id, 'update': summary}
    policy = core._policy(spec, cfg, out_dir)
    # the rules of build_lexicon: a read that missed rows stops here
    core._fail_if_partial('lexicon', stats, out_dir, extra, policy=policy)
    if not lex:
        core._require_text('lexicon', ReadStats())
    info = core._coverage_info('lexicon', stats, extra, policy=policy)
    new = os.path.join(out_dir, NEW_DIR)
    _fresh_dir(new)                     # a leftover of a failed update
    with open(os.path.join(new, core.LEXICON_F), 'wb') as f:
        pickle.dump(dict(lex), f, protocol=4)
    with open(os.path.join(new, core.NAMES_F), 'wb') as f:
        pickle.dump({'format': core.NAMES_FORMAT, 'lexicon_id': lexicon_id,
                     'spec': spec, 'docs': sorted(seen),
                     'names': dict(names)}, f, protocol=4)
    with open(os.path.join(new, _coverage_f()), 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    if info['complete']:
        # only a complete lexicon can be updated later (books_problem). A
        # document read but not stamped (it appeared during the read) gets
        # no stamp: the next update recounts it, or takes it out if gone
        stamps = dict(stamps)
        for doc in packed:
            stamps.setdefault(doc, None)
        head = {'format': FORMAT, 'names_format': core.NAMES_FORMAT,
                'lexicon_id': lexicon_id, 'spec': spec, 'stamps': stamps,
                'lexicon_stamp': _lexicon_stamp(
                    os.path.join(new, core.LEXICON_F))}
        with open(os.path.join(new, BOOKS_F), 'wb') as f:
            pickle.dump(head, f, protocol=4)
            pickle.dump(packed, f, protocol=4)
    _exchange(out_dir, new)
    shutil.rmtree(new, ignore_errors=True)
    print(core.coverage_line(info), flush=True)
    if info.get('accepted'):
        print(core._accepted_warning(
            info, os.path.join(out_dir, _coverage_f())), flush=True)


def _fresh_dir(path):
    """An empty directory at `path`; whatever was there is removed."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
        os.makedirs(path)
    except OSError as e:
        raise core.StageError(
            f'לא ניתן לנקות את התיקייה הזמנית {path}: {e}\n'
            f'סגרו תוכנה שמחזיקה בה קבצים או מחקו אותה ידנית, והריצו שוב. '
            f'המילון הקיים לא שונה.') from e


def _write_json_atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _undo(out_dir, incoming, had):
    """Put the set that was in the folder (`had`, now in OLD_DIR) back, and
    the files of `incoming` that were already moved into the folder back to
    `incoming`. Safe to repeat after being cut at any point."""
    old = os.path.join(out_dir, OLD_DIR)
    inc = os.path.join(out_dir, incoming)
    for name in VERSION_FILES:
        cur = os.path.join(out_dir, name)
        saved = os.path.join(old, name)
        if os.path.exists(saved) or name not in had:
            if os.path.exists(cur):     # a file of the incoming version
                os.makedirs(inc, exist_ok=True)
                os.replace(cur, os.path.join(inc, name))
        if os.path.exists(saved):
            os.replace(saved, cur)


def _exchange(out_dir, incoming):
    """Make the version in the folder `incoming` (NEW_DIR, or PREV_DIR for a
    rollback) the current one, and the current one the previous one
    (PREV_DIR), crash-safely; see the module docstring."""
    old = os.path.join(out_dir, OLD_DIR)
    txn = os.path.join(out_dir, TXN_F)
    had = [n for n in VERSION_FILES
           if os.path.exists(os.path.join(out_dir, n))]
    name_in = os.path.basename(incoming)
    _fresh_dir(old)
    try:
        _write_json_atomic(txn, {'incoming': name_in, 'had': had})
        for name in had:                # current -> aside
            os.replace(os.path.join(out_dir, name), os.path.join(old, name))
        for name in VERSION_FILES:      # incoming -> current
            src = os.path.join(incoming, name)
            if os.path.exists(src):
                os.replace(src, os.path.join(out_dir, name))
    except BaseException:
        _undo(out_dir, name_in, had)    # a failure here leaves the journal
        shutil.rmtree(old, ignore_errors=True)
        for p in (txn, txn + '.tmp'):
            if os.path.exists(p):
                os.remove(p)
        raise
    os.remove(txn)                      # the commit
    try:
        _finish(out_dir)
    except OSError:
        _finish(out_dir)                # once more; then recover() does it


def _finish(out_dir):
    """After the commit: OLD_DIR becomes PREV_DIR (the dropped one goes via
    TRASH_DIR, so PREV_DIR is whole or absent at every moment)."""
    old = os.path.join(out_dir, OLD_DIR)
    prev = os.path.join(out_dir, PREV_DIR)
    trash = os.path.join(out_dir, TRASH_DIR)
    if os.path.isfile(os.path.join(old, core.LEXICON_F)):
        shutil.rmtree(trash, ignore_errors=True)
        if os.path.isdir(prev):
            os.replace(prev, trash)
        os.replace(old, prev)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(trash, ignore_errors=True)


def recover(out_dir):
    """Repair what a swap killed half way left: before its commit, put the
    previous version back; after it, finish moving OLD_DIR to PREV_DIR. Also
    drops a leftover NEW_DIR. Called before any lexicon write; does nothing
    on a clean folder."""
    txn = os.path.join(out_dir, TXN_F)
    old = os.path.join(out_dir, OLD_DIR)
    if os.path.exists(txn + '.tmp'):
        os.remove(txn + '.tmp')
    if os.path.isfile(txn):
        with open(txn, encoding='utf-8') as f:
            rec = json.load(f)
        _undo(out_dir, rec['incoming'], rec['had'])
        shutil.rmtree(old, ignore_errors=True)
        os.remove(txn)
        print('[lexicon] עדכון קודם נקטע באמצע: הגרסה השלמה שהייתה לפניו '
              'שוחזרה.', flush=True)
    elif os.path.isdir(old) or os.path.isdir(
            os.path.join(out_dir, TRASH_DIR)):
        _finish(out_dir)
    shutil.rmtree(os.path.join(out_dir, NEW_DIR), ignore_errors=True)


def rollback_problem(out_dir):
    """Why there is nothing to roll back to (Hebrew), or None."""
    prev = os.path.join(out_dir, PREV_DIR)
    if os.path.isfile(os.path.join(prev, core.LEXICON_F)):
        return None
    return ('אין גרסה קודמת של המילון לחזור אליה (היא נשמרת בכל עדכון '
            'מצטבר, ‎magiah lexicon --update‎).\n'
            f'לא שונה דבר. תיקייה: {prev}')


def rollback(out_dir):
    """``magiah lexicon --rollback``: swap the lexicon with the version the
    last update replaced (again: swap back), crash-safely (``_exchange``).
    Raises core.StageError when there is none or on a file error."""
    try:
        _rollback(out_dir)
    except OSError as e:
        raise _file_error(e, 'החזרת הגרסה הקודמת', out_dir) from e


def _rollback(out_dir):
    recover(out_dir)
    problem = rollback_problem(out_dir)
    if problem:
        raise core.StageError(problem)
    prev = os.path.join(out_dir, PREV_DIR)
    _exchange(out_dir, prev)
    info = {}
    try:
        with open(os.path.join(out_dir, _coverage_f()), encoding='utf-8') as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    print(f'[lexicon] הוחזרה הגרסה הקודמת של המילון '
          f'(tokens={info.get("tokens", 0):,}  '
          f'types={info.get("types", 0):,}). הגרסה שהוחלפה נשמרה ב-{prev}; '
          f'הרצה נוספת של ‎--rollback‎ תחזיר אותה.', flush=True)
