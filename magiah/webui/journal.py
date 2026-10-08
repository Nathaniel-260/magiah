# -*- coding: utf-8 -*-
"""Per-file locking and a durable write journal for the in-file fixer.

The failure this exists for
---------------------------
A book write is two commits in two places: the file on disk and its
``file_edits`` row in ui_review.db. If the process dies, or SQLite fails,
between them, the book is changed with no record to undo it by. So before a
file is touched an *intent* is appended (and fsynced) to
``<outdir>/file_backups/journal.jsonl`` — outside SQLite, because SQLite
failing is one of the cases being protected against. The intent names the
file, its hash before and the hash the write will produce, and the backup.
After the write and the DB row, a terminal record closes it.

:func:`recover` settles every intent left open by comparing the file's hash
now with the recorded ones: the planned result, or the start of a later
write that committed on top of it -> finish the bookkeeping (``committed``);
the original -> nothing happened (``aborted``); anything else -> ``conflict``,
surfaced to the user until acknowledged (:func:`resolve`), never guessed at.
Writers refuse to touch a file that still has an open intent, so two
unrecorded writes can never stack up on one book.

Locking
-------
:func:`file_lock` serialises writers of one book: an in-process lock (the web
server is threaded) plus a ``<book>.magiah.lock`` file created with O_EXCL,
which also excludes a second server process. The holder refreshes the lock
file's mtime; one untouched for ``STALE_LOCK_SECONDS`` is a leftover from a
crash and is broken (renamed away first, so two breakers cannot both win).
"""
import json
import os
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime

from . import db, patcher

JOURNAL_F = 'journal.jsonl'
LOCK_SUFFIX = '.magiah.lock'
LOCK_TIMEOUT = 15.0
# the holder touches its lock file this often, so a long write is never
# mistaken for a dead process
LOCK_REFRESH_SECONDS = 20.0
STALE_LOCK_SECONDS = 120.0
# a journal larger than this is rewritten without its settled records
COMPACT_BYTES = 256 * 1024

TERMINAL = ('committed', 'aborted', 'conflict')

_guard = threading.Lock()
_locks = {}                      # _key(path) -> [RLock, depth, stop]


def _key(path):
    """One key per book FILE, however its path is spelled.

    realpath does not map a ``\\\\server\\share`` spelling onto the drive
    letter of the same folder (or back), and the fixer accepts both for one
    library, so the folder is keyed by its volume and file id. The file
    itself is not: every write replaces it, and with it its id.
    """
    p = os.path.realpath(os.path.abspath(path))
    folder, name = os.path.split(p)
    try:
        st = os.stat(folder)
    except (OSError, ValueError):
        st = None
    if st is not None and st.st_ino and st.st_dev:
        return 'id:%x:%x:%s' % (st.st_dev, st.st_ino, os.path.normcase(name))
    return os.path.normcase(p)


def _busy():
    return patcher.PatchError('file_busy', patcher._msg('file_busy'))


def _break_if_stale(lf):
    """Remove a dead holder's lock file; any failure just means 'still busy'."""
    try:
        if time.time() - os.path.getmtime(lf) <= STALE_LOCK_SECONDS:
            return
        aside = '%s.%s.stale' % (lf, uuid.uuid4().hex)
        os.rename(lf, aside)
    except OSError:
        return
    try:
        if time.time() - os.path.getmtime(aside) <= STALE_LOCK_SECONDS:
            # a live holder re-created it between our check and the rename
            if not os.path.exists(lf):
                os.rename(aside, lf)
                return
        os.remove(aside)
    except OSError:
        pass


def _acquire_lock_file(lf, deadline):
    if not os.path.isdir(os.path.dirname(lf) or '.'):
        return                        # the book's folder is gone: nothing to guard
    while True:
        try:
            fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # PermissionError: Windows reports a lock file that is being
            # deleted ("delete pending") this way — busy, not fatal
            _break_if_stale(lf)
            if time.time() >= deadline:
                raise _busy()
            time.sleep(0.05)
            continue
        try:
            os.write(fd, json.dumps({'pid': os.getpid(),
                                     'ts': time.time()}).encode('ascii'))
        finally:
            os.close(fd)
        return


def _keep_fresh(lf, stop):
    while not stop.wait(LOCK_REFRESH_SECONDS):
        try:
            os.utime(lf, None)
        except OSError:
            pass


@contextmanager
def file_lock(path, timeout=LOCK_TIMEOUT):
    """Exclusive access to one book file; re-entrant within a thread."""
    k = _key(path)
    with _guard:
        entry = _locks.setdefault(k, [threading.RLock(), 0, None])
    deadline = time.time() + timeout
    got = (entry[0].acquire(blocking=False) if timeout <= 0
           else entry[0].acquire(timeout=timeout))
    if not got:
        raise _busy()
    lf = path + LOCK_SUFFIX
    try:
        if entry[1] == 0:
            _acquire_lock_file(lf, deadline)
            entry[2] = threading.Event()
            threading.Thread(target=_keep_fresh, args=(lf, entry[2]),
                             daemon=True).start()
        entry[1] += 1
        try:
            yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                entry[2].set()
                try:
                    os.remove(lf)
                except OSError:
                    pass
    finally:
        entry[0].release()


# ---------------------------------------------------------------------------
# the journal file
# ---------------------------------------------------------------------------

def journal_path(outdir):
    return os.path.join(outdir, patcher.FIXER_BACKUP_DIR, JOURNAL_F)


@contextmanager
def _journal_locked(outdir):
    p = journal_path(outdir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with file_lock(p):
        yield p


def _append(outdir, rec):
    line = (json.dumps(rec, ensure_ascii=False) + '\n').encode('utf-8')
    with _journal_locked(outdir) as p:
        with open(p, 'ab') as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def _now():
    return datetime.now().isoformat(timespec='microseconds')


def begin(outdir, rec):
    """Durably record the intent to write; returns its journal id."""
    jid = uuid.uuid4().hex
    _append(outdir, dict(rec, op='intent', jid=jid, ts=_now()))
    return jid


def finish(outdir, jid, state, **extra):
    _append(outdir, dict(extra, op=state, jid=jid, ts=_now()))


def resolve(outdir, jid):
    """The user looked at a conflict and took responsibility for the file."""
    _append(outdir, {'op': 'resolved', 'jid': jid, 'ts': _now()})


def read_records(outdir):
    p = journal_path(outdir)
    if not os.path.isfile(p):
        return []
    out = []
    with open(p, 'rb') as f:
        for raw in f:
            try:
                rec = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                continue              # a torn final line from a crash
            if isinstance(rec, dict) and rec.get('jid'):
                out.append(rec)
    return out


def _settled(outdir, records=None):
    """``(intents in journal order, last terminal record, resolved jids)``."""
    intents, last, resolved = {}, {}, set()
    for rec in records if records is not None else read_records(outdir):
        op = rec.get('op')
        if op == 'intent':
            intents[rec['jid']] = rec
        elif op in TERMINAL:
            last[rec['jid']] = rec
        elif op == 'resolved':
            resolved.add(rec['jid'])
    return intents, last, resolved


def pending(outdir, path=None):
    intents, last, _r = _settled(outdir)
    k = _key(path) if path else None
    return [r for j, r in intents.items() if j not in last
            and (k is None or _key(r.get('path') or '') == k)]


def conflicts(outdir, path=None):
    intents, last, resolved = _settled(outdir)
    k = _key(path) if path else None
    return [dict(intents[j], fp_seen=t.get('fp_seen'))
            for j, t in last.items()
            if t.get('op') == 'conflict' and j in intents
            and j not in resolved
            and (k is None or _key(intents[j].get('path') or '') == k)]


def compact(outdir, min_bytes=COMPACT_BYTES):
    """Drop settled records; open intents and unacknowledged conflicts stay."""
    p = journal_path(outdir)
    try:
        if os.path.getsize(p) < min_bytes:
            return False
    except OSError:
        return False
    with _journal_locked(outdir):
        records = read_records(outdir)
        intents, last, resolved = _settled(outdir, records)
        keep = {j for j in intents
                if j not in last or (last[j].get('op') == 'conflict'
                                     and j not in resolved)}
        data = b''.join((json.dumps(r, ensure_ascii=False) + '\n')
                        .encode('utf-8')
                        for r in records if r['jid'] in keep)
        patcher.atomic_write(p, data)
    return True


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

def _set_statuses(con, outdir, ids, status, corrections=None):
    corrections = corrections or {}
    try:
        for fid in ids:
            db.set_status(con, outdir, [fid], status,
                          custom_suggestion=corrections.get(str(fid))
                          or None)
        return True
    except Exception:
        traceback.print_exc()
        return False


def complete(con, outdir, rec):
    """The DB side of a write whose file side is known to have landed."""
    if rec.get('kind') == 'undo':
        edit = db.get_file_edit(con, rec['edit_id'])
        if edit is not None and not edit.get('undone_at'):
            db.mark_edit_undone(con, rec['edit_id'])
        _set_statuses(con, outdir, rec.get('finding_ids') or [], 'approved')
        return rec['edit_id']
    edit_id = db.find_file_edit_by_journal(con, rec['jid'])
    if edit_id is None:
        edit_id = db.record_file_edit(
            con, rec['path'], rec['book_key'], rec.get('backup') or '',
            rec.get('mode') or patcher.MODE_REPLACE,
            rec.get('finding_ids') or [], rec.get('detail') or '[]',
            rec.get('fp_before'), rec.get('fp_after'),
            journal_id=rec['jid'], backup_sha=rec.get('backup_sha'),
            source_root=rec.get('source_root'))
    if rec.get('mark_fixed'):
        _set_statuses(con, outdir, rec.get('finding_ids') or [], 'fixed',
                      rec.get('corrections'))
    return edit_id


def _landed(order, i, last, fp_now, seen=None):
    """Did intent ``order[i]`` reach the file? Yes if the file is exactly its
    result, or a later write that started FROM its result committed (or
    landed itself)."""
    rec = order[i]
    after = rec.get('fp_after')
    if after is None:
        return False
    if fp_now == after:
        return True
    seen = seen or set()
    k = _key(rec.get('path') or '')
    for j in range(i + 1, len(order)):
        nxt = order[j]
        if j in seen or nxt.get('fp_before') != after \
                or _key(nxt.get('path') or '') != k:
            continue
        seen.add(j)
        if (last.get(nxt['jid']) or {}).get('op') == 'committed' \
                or _landed(order, j, last, fp_now, seen):
            return True
    return False


def recover(con, outdir, path=None, wait=True):
    """Settle every open intent (for one file, or all). Returns one dict per
    intent: ``{'jid', 'path', 'kind', 'state'}``; ``state`` stays 'pending'
    when the database is still failing, and is 'busy' when ``wait`` is False
    and another writer holds the file."""
    intents, last, _r = _settled(outdir)
    order = list(intents.values())
    k = _key(path) if path else None
    out = []
    for i, rec in enumerate(order):
        if rec['jid'] in last:
            continue
        p = rec.get('path') or ''
        if k is not None and _key(p) != k:
            continue
        state, extra = 'pending', {}
        try:
            with file_lock(p, timeout=LOCK_TIMEOUT if wait else 0):
                try:
                    fp = patcher.fingerprint(p)
                except OSError:
                    fp = None
                if fp is not None and _landed(order, i, last, fp):
                    extra['edit_id'] = complete(con, outdir, rec)
                    state = 'committed'
                elif fp is not None and fp == rec.get('fp_before'):
                    state = 'aborted'
                else:
                    state = 'conflict'
                finish(outdir, rec['jid'], state, fp_seen=fp, **extra)
                last[rec['jid']] = {'op': state}
        except patcher.PatchError as e:
            if e.code != 'file_busy':
                raise
            state = 'busy'
        except Exception:
            # the DB is still unavailable: leave the intent open; writers
            # refuse this file until it is settled
            traceback.print_exc()
        out.append({'jid': rec['jid'], 'path': p, 'kind': rec.get('kind'),
                    'state': state})
    if not pending(outdir):
        try:
            compact(outdir)
        except (OSError, patcher.PatchError):
            traceback.print_exc()
    return out
