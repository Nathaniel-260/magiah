# -*- coding: utf-8 -*-
"""Run-state markers: did the latest scan of this output folder finish?

Why this exists
---------------
A stage that fails never replaces its last good output, so after a failed
run the folder still holds the previous run's ``report.db`` — complete and
readable, but not the result of the scan the user last asked for. The
coverage files (:func:`magiah.core.coverage_problem`) catch one way of
failing, a partial read. They cannot catch the others: an exception, a killed
process, a closed window, a power cut. Each of those leaves the previous,
*complete* coverage file in place, so the old results look current.

So every run records itself, in ``<out>/run_state/``:

``scan.json``  pipeline runs — ``magiah lexicon|calibrate|detect|locate|
               report|all`` and scans started from the review UI
``book.json``  single-book scans (``magiah book``, the UI's book scan)

A record is written when the run starts (``running``) and finalized when it
ends (``done`` / ``failed`` / ``partial`` / ``cancelled``). A run that never
reaches its end — killed, crashed, the machine turned off — is recognized by
its lock: the run holds ``<slot>.lock`` with an OS file lock for its whole
lifetime, and the OS drops that lock when the process dies, however it dies.
A ``running`` record whose lock is free was therefore *interrupted*.

When are the results stale?
---------------------------
The results are derived from the chain lexicon -> detect -> locate (the same
chain :func:`magiah.core.coverage_problem` checks). ``scan.json`` remembers,
for each stage of the chain, the run that last attempted it — or planned to:
a run that stops at ``calibrate`` never reaches its ``detect`` and
``locate``, and those count as not done. The results are stale when one of
the three was not completed by its run. This is deliberately per stage, not
"did the latest run succeed": a later run of just ``calibrate`` or ``report``
does not rebuild the results, so it must not clear the warning left by an
interrupted ``lexicon``. Nor does a failed ``report`` (CSV export) or
``calibrate``-only run make the results in report.db stale.

A single-book scan never writes the pipeline's outputs (it reads the lexicon
and merges one book into ui_review.db in a single transaction), so it has its
own record and can neither raise nor clear the warning about the full scan.
That record is kept per book: a failed scan of one book says nothing about
another, so ``book.json`` lists every book whose latest scan failed or was
interrupted, and a successful scan of a book takes only that book off the
list (:func:`book_problems`). A full scan that succeeds reads every book of
the corpus again, so it supersedes the failures of those books that ended
before it began (not of a file outside the library, which it never reads).

Concurrency
-----------
Every write is an atomic replace (:func:`magiah.core._replace_atomically`),
so a reader sees the old record or the new one, never half of one. Under the
review UI a scan's stages run as subprocesses: the UI's scanner owns the run
(and its lock) and hands the run id to each stage in ``MAGIAH_RUN_ID``; a
stage that fails records why in that run instead of starting its own. Two
pipeline runs on the same folder at once would overwrite each other's files,
so the second one is refused.

Folders written before this module existed have no ``run_state/``: nothing
is known about their runs and nothing is reported (the coverage files still
are — see webui/result_status.py).
"""
import errno
import json
import os
import sys
import time
import uuid

from . import core
from .book_source import book_identity
from .textsource import TextSourceError

STATE_DIR = 'run_state'
ENV_RUN_ID = 'MAGIAH_RUN_ID'
VERSION = 1
# the stages the results are derived from, in order (as coverage_problem)
RESULT_CHAIN = core._READ_STAGES

# Windows refuses to replace a file another process has open, and a reader
# holds the marker for a moment; a run waits that out instead of failing.
_RETRIES = 40
_RETRY_S = 0.05
# books whose failed scan book.json remembers; past it the oldest is dropped
MAX_BOOK_FAILURES = 20


class RunBusy(core.StageError):
    """Another run of the same kind already holds this output folder."""


class LockUnsupported(OSError):
    """The file system cannot lock files (some network or FUSE mounts)."""


# what a lock attempt raises when another handle holds the lock; anything
# else means the file system cannot lock at all
_CONTENDED = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
              getattr(errno, 'EDEADLK', None)} - {None}

if os.name == 'nt':
    import msvcrt

    def _lock_once(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_once(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


def _try_lock(fd):
    """True: locked; False: another handle holds the lock. Raises
    LockUnsupported when the file system cannot lock — which must not read
    as "held" (every run would be refused as busy) nor as "free" (every run
    in progress would read as interrupted)."""
    try:
        _lock_once(fd)
        return True
    except OSError as e:
        if e.errno in _CONTENDED:
            return False
        raise LockUnsupported(e.errno, f'cannot lock {e.filename or fd}: '
                                       f'{e.strerror or e}') from e


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


class _Broken:
    """A marker that exists but cannot be read."""


BROKEN = _Broken()
# how a run the results answer to can have ended without completing them
STATES = ('failed', 'partial', 'cancelled', 'interrupted')


class _Slot:
    def __init__(self, out_dir, name):
        self.dir = os.path.join(out_dir, STATE_DIR)
        self.path = os.path.join(self.dir, name + '.json')
        self.lock_path = os.path.join(self.dir, name + '.lock')

    def load(self):
        """The record: a dict, None when there is none, or BROKEN."""
        for _ in range(_RETRIES):
            try:
                with open(self.path, encoding='utf-8') as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else BROKEN
            except FileNotFoundError:
                return None
            except PermissionError:      # being replaced this very moment
                time.sleep(_RETRY_S)
            except (OSError, ValueError):
                return BROKEN
        return BROKEN

    def store(self, data):
        def write(tmp):
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        os.makedirs(self.dir, exist_ok=True)
        for attempt in range(_RETRIES):
            try:
                core._replace_atomically(self.path, write)
                return
            except PermissionError:
                if attempt == _RETRIES - 1:
                    raise
                time.sleep(_RETRY_S)

    def hold(self, busy_message):
        """Take the slot's lock for the caller's lifetime; returns the fd.

        Retries briefly: a reader probing the lock holds it for a moment. On
        a file system that cannot lock, the run goes on unlocked (and says
        so): it then cannot refuse a concurrent run, nor be told from a dead
        one — but refusing every run would be worse.
        """
        os.makedirs(self.dir, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            for _ in range(_RETRIES):
                if _try_lock(fd):
                    return fd
                time.sleep(_RETRY_S)
        except LockUnsupported as e:
            print(f'[run_state] {e}', file=sys.stderr, flush=True)
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        raise RunBusy(busy_message)

    def settled(self):
        """The record, with a ``running`` run whose owner is gone shown as
        ``interrupted``. Never writes."""
        data = self.load()
        if not isinstance(data, dict) or not _running(data):
            return data
        fd = None
        # read-write first (NFS emulates flock with fcntl locks, which an
        # exclusive probe needs a writable handle for), read-only for a
        # folder the reader may not write to
        for mode in (os.O_RDWR, os.O_RDONLY):
            try:
                fd = os.open(self.lock_path, mode)
                break
            except FileNotFoundError:    # no lock file: nobody holds it
                return _interrupted(data)
            except OSError:
                continue
        if fd is None:                   # cannot tell: assume it runs
            return data
        try:
            try:
                if not _try_lock(fd):
                    return data          # the owner is alive: in progress
            except LockUnsupported:
                return data              # cannot tell: assume it runs
            try:
                # re-read under the lock: the owner may have finished between
                # the first read and the probe — and no run can start now
                data = self.load()
                return _interrupted(data) if isinstance(data, dict) else data
            finally:
                _unlock(fd)
        finally:
            os.close(fd)


def _runs(data):
    """The run records of a slot's data (scan: many, book: one)."""
    if 'runs' in data:
        runs = data.get('runs')
        runs = list(runs.values()) if isinstance(runs, dict) else []
    else:
        runs = [data.get('run')]
    # a hand-edited or damaged entry is skipped, never a crash
    return [r for r in runs if isinstance(r, dict)]


def _running(data):
    return any(r.get('state') == 'running' for r in _runs(data))


def _interrupted(data):
    for r in _runs(data):
        if r.get('state') == 'running':
            r['state'] = 'interrupted'
    return data


def _reason(exc):
    """A stored failure reason: our own messages as they are (Hebrew, ready
    for a person), anything else with its type."""
    if isinstance(exc, (core.StageError, TextSourceError)):
        return str(exc)
    return f'{type(exc).__name__}: {exc}'


class _Run:
    """A run this process owns. Finalizing is first-writer-wins: once the
    run is no longer ``running`` (a stage subprocess recorded its own
    failure, say) a later finalize keeps what is there."""

    kind = None
    busy = None

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self._slot = _Slot(out_dir, self.kind)
        self.id = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        self.stage = None
        self._fd = self._slot.hold(self.busy.format(out_dir=out_dir))
        try:
            self._update(self._begin)
        except BaseException:
            self.close()
            raise

    # -- record plumbing ----------------------------------------------------
    def _update(self, change):
        data = self._slot.load()
        if not (isinstance(data, dict) and data.get('version') == VERSION
                and self._valid(data)):
            data = self._fresh()        # unreadable: start a clean record
        change(data)
        self._slot.store(data)

    def _mine(self, data):
        return None

    def _ended(self, data, run):
        """Hook: `run` (in `data`) was just finalized."""

    def _finish(self, state, **fields):
        def change(data):
            run = self._mine(data)
            if run is None or run.get('state') != 'running':
                return
            run.update(fields, state=state, finished_at=_now())
            self._ended(data, run)
        try:
            self._update(change)
        except OSError as e:
            # the run itself is over; an unwritable marker must not turn its
            # result into a crash. Left `running`, it reads as interrupted.
            print(f'[run_state] {e}', file=sys.stderr, flush=True)

    # -- public ---------------------------------------------------------------
    def done(self, **fields):
        self._finish('done', **fields)

    def fail(self, reason, partial=False):
        self._finish('partial' if partial else 'failed', reason=reason)

    def cancel(self):
        self._finish('cancelled')

    def close(self):
        """Release the lock. A run still ``running`` now reads as
        interrupted."""
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                _unlock(fd)
            except OSError:
                pass
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self.done()
            elif issubclass(et, KeyboardInterrupt):
                self.cancel()
            else:
                self.fail(_reason(ev), partial=isinstance(ev, core.PartialRead))
        finally:
            self.close()
        return False


class ScanRun(_Run):
    """A pipeline run (one or more stages) owned by this process."""

    kind = 'scan'
    busy = ('סריקה אחרת כבר רצה על תיקיית התוצאות הזו ({out_dir}). שתי '
            'סריקות במקביל היו דורסות זו את הקבצים של זו — יש להמתין '
            'לסיומה (או לבטל אותה) ולהריץ שוב.')

    def __init__(self, out_dir, stages, via='cli'):
        self.stages = list(stages)
        self.via = via
        # it rebuilds the results from the whole corpus
        self.full = set(RESULT_CHAIN) <= set(self.stages)
        super().__init__(out_dir)

    @staticmethod
    def _fresh():
        return {'version': VERSION, 'runs': {}, 'stages': {}}

    @staticmethod
    def _valid(data):
        runs, stages = data.get('runs'), data.get('stages')
        # damaged entries would break the bookkeeping below: start afresh
        return (isinstance(runs, dict) and isinstance(stages, dict)
                and all(isinstance(r, dict) for r in runs.values())
                and all(isinstance(v, str) for v in stages.values()))

    def _mine(self, data):
        return data['runs'].get(self.id)

    def _begin(self, data):
        # we hold the lock, so any run still marked running died without
        # saying so
        _interrupted(data)
        data['runs'][self.id] = {
            'id': self.id, 'via': self.via, 'stages': self.stages,
            'state': 'running', 'stage': None, 'started_at': _now(),
            'finished_at': None, 'reason': None}
        if self.full:
            # the failed book scans it will supersede if it succeeds: those
            # that ended before it began, of books it reads
            data['runs'][self.id]['supersedes'] = [
                r.get('id') for r in _book_failures(self.out_dir)
                if str(r.get('book')).startswith(_CORPUS_BOOKS)]
        for st in self.stages:
            if st in RESULT_CHAIN:
                data['stages'][st] = self.id
        # only the runs some stage still answers to are worth keeping
        keep = set(data['stages'].values()) | {self.id}
        data['runs'] = {k: v for k, v in data['runs'].items() if k in keep}

    def enter(self, stage):
        """`stage` starts now (where the run stopped, if it never ends)."""
        self.stage = stage

        def change(data):
            run = self._mine(data)
            if run is not None and run.get('state') == 'running':
                run['stage'] = stage
        try:
            self._update(change)
        except OSError as e:
            # bookkeeping must not stop the scan; the stage is recorded
            # again if the run ends without completing
            print(f'[run_state] {e}', file=sys.stderr, flush=True)

    def done(self, **fields):
        self._finish('done', stage=None, **fields)

    def _finish(self, state, **fields):
        if state != 'done' and self.stage:
            fields.setdefault('stage', self.stage)
        super()._finish(state, **fields)

    def _ended(self, data, run):
        if not (self.full and run['state'] == 'done'):
            return
        # book.json is the book scans' to write; the superseded ones are
        # listed here and left out by book_problems. Ids no longer listed
        # there are dropped, so the list stays as short as book.json's.
        listed = {r.get('id') for r in _book_failures(self.out_dir)}
        old = data.get('books_superseded')
        old = old if isinstance(old, list) else []
        data['books_superseded'] = sorted(
            i for i in listed & set(old + (run.get('supersedes') or []))
            if isinstance(i, str))


class _ChildRun:
    """A stage subprocess of a run its parent (the UI's scanner) owns.

    It neither locks nor finalizes — the parent does both — it only records
    why it failed, which the parent cannot know: it sees just an exit code.
    """

    def __init__(self, out_dir, run_id):
        self._slot = _Slot(out_dir, 'scan')
        self.id = run_id
        self.stage = None

    def enter(self, stage):
        self.stage = stage

    def fail(self, reason, partial=False):
        def change(data):
            run = data['runs'].get(self.id)
            if run is not None and run.get('state') == 'running':
                run.update(state='partial' if partial else 'failed',
                           stage=self.stage or run.get('stage'),
                           reason=reason, finished_at=_now())
        try:
            data = self._slot.load()
            if isinstance(data, dict) and isinstance(data.get('runs'), dict):
                change(data)
                self._slot.store(data)
        except OSError as e:
            print(f'[run_state] {e}', file=sys.stderr, flush=True)

    def done(self, **fields):
        pass

    def cancel(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is not None and not issubclass(et, KeyboardInterrupt):
            self.fail(_reason(ev), partial=isinstance(ev, core.PartialRead))
        return False


def track_scan(out_dir, stages, via='cli'):
    """The run of `stages` this process is about to do.

    A stage subprocess of a UI scan (``MAGIAH_RUN_ID`` set) joins its
    parent's run; anything else starts its own run (raises
    :class:`RunBusy` when another pipeline run holds the folder).
    """
    run_id = os.environ.get(ENV_RUN_ID)
    if run_id:
        return _ChildRun(out_dir, run_id)
    return ScanRun(out_dir, stages, via)


def book_id(source, key, library_dir=None):
    """One book's identity in book.json, however it was asked for: resolved
    as the book loader resolves it (book_source.book_identity), so a failure
    recorded for ``./a//b.txt`` is cleared by a scan of ``a/b.txt`` — and one
    recorded for a file inside the library by a scan of that library book."""
    return '%s:%s' % book_identity(source, key, library_dir)


# books a full scan reads, and so supersedes a failed scan of (a file outside
# the library is no part of it: the import even keeps its rows apart)
_CORPUS_BOOKS = ('db:', 'library:')


def _file_failure(data, run):
    """Remember `run` as its book's latest, unsuccessful, scan."""
    failed = data.setdefault('failed', {})
    book = run.get('book') or book_id(run.get('source'), run.get('key'))
    failed.pop(book, None)
    failed[book] = dict(run)
    # newest last (insertion order): the oldest go first past the cap
    for old in list(failed)[:-MAX_BOOK_FAILURES]:
        del failed[old]


class BookRun(_Run):
    """A single-book scan owned by this process.

    ``book.json`` = ``{version, run: the latest book scan, failed: {book:
    run}}``. A book scan that ends ``done`` takes its book out of `failed`,
    one that fails (or never ends: interrupted) puts it there, and one that
    is cancelled changed nothing, so it leaves the book as it was.
    """

    kind = 'book'
    busy = ('סריקת ספר בודד אחרת כבר רצה על תיקיית התוצאות הזו '
            '({out_dir}) — יש להמתין לסיומה ולנסות שוב.')

    def __init__(self, out_dir, source, key, via='cli', library_dir=None):
        self.source, self.key, self.via = source, str(key), via
        self.book = book_id(source, key, library_dir)
        super().__init__(out_dir)

    @staticmethod
    def _fresh():
        return {'version': VERSION, 'run': None, 'failed': {}}

    @staticmethod
    def _valid(data):
        return ((data.get('run') is None or isinstance(data.get('run'), dict))
                and isinstance(data.get('failed', {}), dict))

    def _mine(self, data):
        run = data.get('run')
        return run if isinstance(run, dict) and run.get('id') == self.id \
            else None

    def fail(self, reason, partial=False):
        # a book scan's result is all or nothing: a partial one is a failure
        self._finish('failed', reason=reason)

    def _ended(self, data, run):
        if run['state'] == 'done':
            data.setdefault('failed', {}).pop(self.book, None)
        elif run['state'] != 'cancelled':
            _file_failure(data, run)

    def _begin(self, data):
        prev = data.get('run')
        if isinstance(prev, dict) and prev.get('state') == 'running':
            # we hold the lock, so it died without saying so
            prev['state'] = 'interrupted'
            _file_failure(data, prev)
        data['run'] = {
            'id': self.id, 'via': self.via, 'source': self.source,
            'key': self.key, 'book': self.book, 'title': None,
            'state': 'running', 'started_at': _now(), 'finished_at': None,
            'reason': None}


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def _completed(run, stage):
    """Did `run` complete `stage` (one of its own stages)? A run still in
    progress is not held against the results."""
    state = run.get('state')
    if state in ('done', 'running'):
        return True
    stages, stop = run.get('stages'), run.get('stage')
    if not isinstance(stages, list):
        stages = []
    if stage in stages and stop in stages:
        return stages.index(stage) < stages.index(stop)
    return False


def scan_problem(out_dir):
    """Why the results on disk are not from the latest scan attempt — None
    if they are, or if nothing is known (no marker: a folder from before
    run states were recorded).

    Returns ``{'state', 'stage', 'stages', 'started_at', 'finished_at',
    'reason', 'id', 'path'}``; `state` is failed / partial / cancelled /
    interrupted, or ``unknown`` when the marker cannot be read (unreadable
    evidence is no evidence — as with the coverage files).
    """
    slot = _Slot(out_dir, 'scan')
    data = slot.settled()
    if data is None:
        return None
    runs, owners = (data.get('runs'), data.get('stages')) \
        if data is not BROKEN else (None, None)
    if not isinstance(runs, dict) or not isinstance(owners, dict):
        return {'state': 'unknown', 'stage': None, 'stages': [],
                'started_at': None, 'finished_at': None, 'id': None,
                'reason': None, 'path': slot.path}
    for st in RESULT_CHAIN:
        owner = owners.get(st)
        run = runs.get(owner) if isinstance(owner, str) else None
        if isinstance(run, dict) and not _completed(run, st):
            stages = run.get('stages')
            stages = list(stages) if isinstance(stages, list) else []
            state = run.get('state')
            return {'state': state if state in STATES else 'unknown',
                    'stage': run.get('stage') or (stages[0] if stages
                                                  else st),
                    'stages': stages,
                    'started_at': run.get('started_at'),
                    'finished_at': run.get('finished_at'),
                    'reason': run.get('reason'), 'id': run.get('id'),
                    'path': slot.path}
    return None


def _book_failures(out_dir):
    """The book scans book.json holds as failed or interrupted (one per
    book, oldest first), superseded or not."""
    data = _Slot(out_dir, 'book').settled()
    if not isinstance(data, dict):
        return []
    failed = data.get('failed')
    runs = [r for r in failed.values() if isinstance(r, dict)]         if isinstance(failed, dict) else []
    run = data.get('run')
    if isinstance(run, dict) and run.get('state') == 'interrupted':
        # found dead by this read: it replaces its book's older entry
        runs = [r for r in runs if r.get('book') != run.get('book')] + [run]
    # `failed` is in the order the scans ended, newest last
    return [r for r in runs if r.get('state') in ('failed', 'interrupted')]


def book_problems(out_dir):
    """The books whose latest single-book scan failed or was interrupted,
    newest first — ``[]`` if none. A scan the user cancelled changed nothing
    and is not reported; a book scanned successfully since is not either,
    nor one a full scan has read since (ScanRun: ``books_superseded``).

    Each is ``{'state', 'source', 'key', 'book', 'title', 'started_at',
    'finished_at', 'reason', 'id'}``.
    """
    scan = _Slot(out_dir, 'scan').load()
    gone = scan.get('books_superseded') if isinstance(scan, dict) else None
    gone = {i for i in gone if isinstance(i, str)}         if isinstance(gone, list) else set()
    return [{k: r.get(k) for k in ('state', 'source', 'key', 'book', 'title',
                                   'started_at', 'finished_at', 'reason',
                                   'id')}
            for r in reversed(_book_failures(out_dir))
            if not (isinstance(r.get('id'), str) and r['id'] in gone)]
