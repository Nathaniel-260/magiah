# -*- coding: utf-8 -*-
"""Run the detection pipeline from the UI (UI_SPEC §9d).

A single-scan subprocess manager: writes run_config.json (the same file the
CLI uses — single source of truth), then runs the requested stages one after
another as ``python -X utf8 -m magiah <stage> --out <outdir>`` subprocesses,
capturing their combined stdout/stderr into a rolling in-memory buffer and
into ``<outdir>/scan_ui_log.txt``. Only one scan may run at a time.
"""
import dataclasses
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

from ..config import Config
from ..corpus import OTZARIA_DB
from ..corpus_hybrid import DEFAULT_LIBRARY
from . import hebrew

STAGE_ORDER = ['lexicon', 'calibrate', 'detect', 'locate', 'report']
# What a scan runs when the caller does not choose stages. Mirrors the CLI's
# `all`, which excludes calibrate: it learns from a previous scan's report.db,
# so it only makes sense as a deliberate second pass.
DEFAULT_STAGES = ['lexicon', 'detect', 'locate', 'report']
RUN_CONFIG = 'run_config.json'
LOG_FILE = 'scan_ui_log.txt'
LOG_TAIL_LINES = 300

# repo root: .../magiah (contains the `magiah` package) — cwd for subprocesses
REPO_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

# heavy stages print progress lines like "  [lexicon] chunk 12/48 types=..."
# (core.py owns these; we only read them). Parse the last such pair per stage.
_CHUNK_RE = re.compile(r'chunk\s+(\d+)\s*/\s*(\d+)')

class _Cancelled(Exception):
    """Internal: the user cancelled before anything was written."""


_lock = threading.Lock()
_state = {
    'state': 'idle',            # idle | running | done | failed | cancelled
    'stage': None,              # currently running stage
    'stages': [],               # full ordered stage list of this scan
    'stage_index': -1,
    'started_at': None,         # formatted local time string (display)
    'started_epoch': None,      # time.time() at scan start (for `elapsed`)
    'finished_at': None,
    'finished_epoch': None,     # time.time() at finish (freezes `elapsed`)
    'returncode': None,
    'error': None,
    'outdir': None,
    'chunk_done': 0,            # latest "done" for the current stage (0 if none)
    'chunk_total': 0,           # latest "total" for the current stage (0=unknown)
    'book': None,               # single-book scan: {source,key,title,result}
}
_log = deque(maxlen=LOG_TAIL_LINES)
_proc = None                    # current subprocess.Popen
_cancel = threading.Event()
_thread = None


def _log_line(text):
    _log.append(text.rstrip('\r\n'))


def _stage_cmd(stage, outdir):
    """Command that runs one pipeline stage as a subprocess.

    Frozen (PyInstaller) there is no interpreter to call: sys.executable IS
    the exe, which ignores `-m magiah <stage>` and would open a second UI
    instead of scanning. The exe's launcher accepts `<exe> <stage> --out <dir>`
    and dispatches to the CLI, so call it that way.
    """
    if getattr(sys, 'frozen', False):
        return [sys.executable, stage, '--out', outdir]
    return [sys.executable, '-X', 'utf8', '-m', 'magiah', stage,
            '--out', outdir]


def _parse_chunk(line):
    """Return (done, total) if `line` carries a `chunk i/N` marker, else None."""
    m = _CHUNK_RE.search(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _spec_from_corpus(corpus):
    """Build the run_config corpus spec from the UI's corpus block."""
    corpus = corpus or {}
    mode = corpus.get('mode') or 'hybrid'
    library = corpus.get('library_dir') or DEFAULT_LIBRARY
    db = corpus.get('db_path') or OTZARIA_DB
    if mode == 'hybrid':
        return {'type': 'hybrid', 'path': library, 'db': db}
    if mode == 'library':
        return {'type': 'library', 'path': library}
    if mode == 'sqlite':
        return {'type': 'sqlite', 'path': db, 'table': 'line',
                'id_col': 'id', 'text_col': 'content', 'preset': 'otzaria'}
    raise ValueError(hebrew.SCAN_MESSAGES['bad_corpus_mode'])


def load_run_config(outdir):
    p = os.path.join(outdir, RUN_CONFIG)
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return None


def _merge_config(outdir, overrides):
    prev = load_run_config(outdir)
    cfg = Config.from_dict(prev['config']) if prev and prev.get('config') \
        else Config()
    fields = {f.name for f in dataclasses.fields(Config)}
    for key, val in (overrides or {}).items():
        if key not in fields or val is None or val == '':
            continue
        cur = getattr(cfg, key)
        try:
            if key == 'whitelist':
                if isinstance(val, str):
                    val = [v.strip() for v in val.splitlines() if v.strip()]
                setattr(cfg, key, tuple(val))
            elif isinstance(cur, float):
                setattr(cfg, key, float(val))
            else:
                setattr(cfg, key, int(val))
        except (TypeError, ValueError):
            raise ValueError(
                hebrew.SCAN_MESSAGES['bad_config_value'] + str(key))
    return cfg


def start_scan(outdir, stages=None, config_overrides=None,
               corpus_overrides=None):
    """Validate, write run_config.json and launch the scan thread.
    Raises ValueError (Hebrew message) on any user error."""
    global _thread
    outdir = os.path.abspath(outdir)
    stages = [s for s in STAGE_ORDER if s in (stages or DEFAULT_STAGES)]
    if not stages:
        raise ValueError(hebrew.SCAN_MESSAGES['no_stages'])
    # calibrate learns from a previous scan's report.db; on a first run there
    # is nothing to learn from, so drop it rather than fail the whole scan.
    skipped_calibrate = False
    if 'calibrate' in stages and not os.path.isfile(
            os.path.join(outdir, 'report.db')):
        stages = [s for s in stages if s != 'calibrate']
        skipped_calibrate = True
        if not stages:
            raise ValueError(hebrew.SCAN_MESSAGES['calibrate_needs_report'])
    spec = _spec_from_corpus(corpus_overrides)
    if spec['type'] in ('library', 'hybrid') \
            and not os.path.isdir(spec['path']):
        raise ValueError(hebrew.SCAN_MESSAGES['library_missing']
                         + spec['path'])
    if spec['type'] in ('hybrid', 'sqlite'):
        db = spec.get('db') or spec.get('path')
        if not os.path.isfile(db):
            raise ValueError(hebrew.SCAN_MESSAGES['db_missing'] + str(db))
    cfg = _merge_config(outdir, config_overrides)
    with _lock:
        if _state['state'] == 'running':
            raise ValueError(hebrew.SCAN_MESSAGES['already_running'])
        with open(os.path.join(outdir, RUN_CONFIG), 'w',
                  encoding='utf-8') as f:
            json.dump({'corpus': spec, 'config': cfg.to_dict()}, f,
                      ensure_ascii=False, indent=2)
        _log.clear()
        if skipped_calibrate:
            _log.append('[webui] ' + hebrew.SCAN_MESSAGES['calibrate_skipped'])
        _cancel.clear()
        _state.update(state='running', stage=stages[0], stages=stages,
                      stage_index=0, returncode=None, error=None,
                      outdir=outdir, finished_at=None, finished_epoch=None,
                      chunk_done=0, chunk_total=0, book=None,
                      started_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                      started_epoch=time.time())
        _thread = threading.Thread(target=_run, args=(outdir, stages),
                                   daemon=True)
        _thread.start()
    return dict(get_status())


def _run(outdir, stages):
    global _proc
    log_path = os.path.join(outdir, LOG_FILE)
    rc = 0
    try:
        logf = open(log_path, 'w', encoding='utf-8')
    except OSError:
        logf = None

    def emit(line):
        _log_line(line)
        ct = _parse_chunk(line)
        if ct is not None:
            with _lock:
                _state['chunk_done'], _state['chunk_total'] = ct
        if logf:
            try:
                logf.write(line.rstrip('\r\n') + '\n')
                logf.flush()
            except OSError:
                pass

    try:
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        for i, stage in enumerate(stages):
            if _cancel.is_set():
                break
            with _lock:
                # new stage — its chunk counters start fresh
                _state.update(stage=stage, stage_index=i,
                              chunk_done=0, chunk_total=0)
            cmd = _stage_cmd(stage, outdir)
            emit(f'===== [{stage}] {" ".join(cmd)}')
            _proc = subprocess.Popen(
                cmd, cwd=REPO_DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess,
                                      'CREATE_NEW_PROCESS_GROUP', 0))
            for line in _proc.stdout:
                emit(line)
            rc = _proc.wait()
            _proc = None
            if _cancel.is_set():
                break
            if rc != 0:
                emit(f'===== [{stage}] failed (exit code {rc})')
                break
            emit(f'===== [{stage}] done')
    except Exception as e:                          # noqa: BLE001
        rc = -1
        emit(f'===== internal error: {e!r}')
        with _lock:
            _state['error'] = str(e)
    finally:
        if logf:
            try:
                logf.close()
            except OSError:
                pass
        with _lock:
            if _cancel.is_set():
                _state.update(state='cancelled', returncode=rc)
                _log_line('===== ' + hebrew.SCAN_MESSAGES['cancelled'])
            elif rc == 0:
                _state.update(state='done', returncode=0)
            else:
                _state.update(state='failed', returncode=rc)
            _state.update(stage=None,
                          finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                          finished_epoch=time.time())


def get_status():
    with _lock:
        st = dict(_state)
    st['log_tail'] = list(_log)
    st['hebrew_state'] = hebrew.SCAN_STATES.get(st['state'], st['state'])
    # a single-book scan is one indivisible stage with no chunk counters, so
    # the generic stage/chunk maths below would report a stuck 0% for it
    st['is_book'] = (st.get('stages') or []) == ['book']
    # --- derived progress (all backward-compatible additions) ---
    total_stages = len(st.get('stages') or [])
    st['total_stages'] = total_stages
    done = int(st.get('chunk_done') or 0)
    ctotal = int(st.get('chunk_total') or 0)
    frac = (done / ctotal) if ctotal > 0 else 0.0
    idx = st.get('stage_index')
    if st['state'] == 'running' and total_stages > 0 and idx is not None \
            and idx >= 0:
        # completed stages + fraction of the current one, over all stages
        pct = (idx + frac) / total_stages * 100.0
    elif st['state'] in ('done',):
        pct = 100.0
    elif st['state'] in ('failed', 'cancelled'):
        # freeze at wherever we got to (best-effort), no fake 100%
        base = (idx if (idx is not None and idx >= 0) else 0)
        pct = (base + frac) / total_stages * 100.0 if total_stages else 0.0
    else:
        pct = 0.0
    if pct != pct or pct < 0:          # NaN / negative guard
        pct = 0.0
    st['percent'] = round(min(100.0, pct), 1)
    # elapsed seconds: frozen at finish, live while running
    se = st.get('started_epoch')
    if se:
        end = st.get('finished_epoch') if st['state'] != 'running' \
            else time.time()
        st['elapsed'] = max(0, int((end or time.time()) - se))
    else:
        st['elapsed'] = 0
    return st


def cancel():
    """Terminate the running scan (kills the whole process tree).

    A single-book scan has no subprocess — it runs in `_thread` — so there is
    nothing to kill: setting the flag is the whole cancellation. The worker
    checks it once the (seconds-long) scan returns and discards the result
    instead of merging it.
    """
    with _lock:
        if _state['state'] != 'running':
            raise ValueError(hebrew.SCAN_MESSAGES['not_running'])
        _cancel.set()
        proc = _proc
    if proc is not None and proc.poll() is None:
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid),
                                '/T', '/F'],
                               capture_output=True, check=False)
            else:
                proc.terminate()
        except OSError:
            pass
    return {'ok': True, 'message': hebrew.SCAN_MESSAGES['cancel_sent']}


def start_book_scan(outdir, source, key, config_overrides=None,
                    corpus_overrides=None, verify_ctx=False):
    """Launch a single-book scan (UI: "סריקת ספר בודד").

    Runs in a thread rather than a subprocess: a book scan is seconds of work
    against the already-built lexicon, and keeping it in-process lets the
    findings be merged into ui_review.db the moment it finishes — no separate
    "refresh" step. It shares `_state` and `_lock` with the full scan, so the
    two can never run at once and the UI's existing status polling shows it
    with no changes.

    Raises ValueError (Hebrew) on any user error, before anything starts.
    """
    global _thread
    outdir = os.path.abspath(outdir)
    if source not in ('db', 'library', 'file'):
        raise ValueError(hebrew.SCAN_MESSAGES['book_bad_source'])
    if not str(key or '').strip():
        raise ValueError(hebrew.SCAN_MESSAGES['book_no_book'])
    # the whole feature rests on an existing lexicon — fail before starting,
    # with the Hebrew instruction, rather than inside the worker thread
    if not os.path.isfile(os.path.join(outdir, 'lexicon.pkl')):
        raise ValueError(hebrew.SCAN_MESSAGES['book_no_lexicon'])
    spec = _spec_from_corpus(corpus_overrides)
    cfg = _merge_config(outdir, config_overrides)
    db_path = spec.get('db') if spec['type'] != 'sqlite' else spec.get('path')
    library_dir = spec.get('path') if spec['type'] in ('hybrid', 'library') \
        else None
    if source == 'db' and not os.path.isfile(db_path or ''):
        raise ValueError(hebrew.SCAN_MESSAGES['db_missing'] + str(db_path))
    if source == 'library' and not os.path.isdir(library_dir or ''):
        raise ValueError(hebrew.SCAN_MESSAGES['library_missing']
                         + str(library_dir))

    with _lock:
        if _state['state'] == 'running':
            raise ValueError(hebrew.SCAN_MESSAGES['already_running'])
        _log.clear()
        _cancel.clear()
        _state.update(state='running', stage='book', stages=['book'],
                      stage_index=0, returncode=None, error=None,
                      outdir=outdir, finished_at=None, finished_epoch=None,
                      chunk_done=0, chunk_total=0,
                      started_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                      started_epoch=time.time())
        _state['book'] = {'source': source, 'key': key, 'title': None,
                          'result': None}
        _thread = threading.Thread(
            target=_run_book,
            args=(outdir, source, key, cfg, db_path, library_dir, verify_ctx,
                  spec),
            daemon=True)
        _thread.start()
    return dict(get_status())


def _run_book(outdir, source, key, cfg, db_path, library_dir, verify_ctx,
              spec):
    """Worker: scan one book, then merge it into ui_review.db."""
    from .. import book_scan
    from ..book_source import BookNotFound
    from . import db as uidb

    log_path = os.path.join(outdir, LOG_FILE)
    try:
        logf = open(log_path, 'w', encoding='utf-8')
    except OSError:
        logf = None

    def emit(line):
        _log_line(line)
        ct = _parse_chunk(line)
        if ct is not None:
            with _lock:
                _state['chunk_done'], _state['chunk_total'] = ct
        if logf:
            try:
                logf.write(line.rstrip('\r\n') + '\n')
                logf.flush()
            except OSError:
                pass

    rc, err = 0, None
    merged = False           # once the merge commits, it cannot be "cancelled"
    cancelled = False
    try:
        emit(f'===== [book] {source}: {key}')
        result = book_scan.scan_book(
            outdir, source, key, cfg=cfg, db_path=db_path,
            library_dir=library_dir, verify_ctx=verify_ctx, spec=spec,
            progress=emit)
        if _cancel.is_set():
            # cancelled before anything was written — discard the result
            cancelled = True
            emit('===== [book] ' + hebrew.SCAN_MESSAGES['cancelled'])
            raise _Cancelled()
        with _lock:
            _state['book']['title'] = result.get('title')
        counts = uidb.import_book_scan(outdir, result)
        merged = True
        emit(f"===== [book] נוספו {counts['added']:,} ממצאים, "
             f"הוחלפו {counts['replaced']:,}, "
             f"נשמרו {counts['preserved']:,} החלטות")
        with _lock:
            _state['book']['result'] = dict(counts, **{
                'lines': result.get('lines'),
                'origin': result.get('origin'),
                'ctx_scope': result.get('ctx_scope'),
                'seconds': result.get('seconds')})
    except _Cancelled:
        rc = 1
    except (book_scan.BookScanError, BookNotFound, ValueError) as e:
        rc, err = 1, str(e)
        emit(f'===== [book] שגיאה: {e}')
    except Exception as e:                          # noqa: BLE001
        rc, err = -1, str(e)
        emit(f'===== [book] שגיאה פנימית: {e!r}')
    finally:
        if logf:
            try:
                logf.close()
            except OSError:
                pass
        with _lock:
            # a cancel that arrived *during* the merge is too late: the rows
            # are committed, so report the truth (done), not "cancelled"
            if cancelled and not merged:
                _state.update(state='cancelled', returncode=rc or 1)
            elif rc == 0:
                _state.update(state='done', returncode=0)
            else:
                _state.update(state='failed', returncode=rc, error=err)
            _state.update(stage=None,
                          finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                          finished_epoch=time.time())


def scan_config(outdir):
    """Current run_config.json + defaults + Hebrew labels for the UI panel."""
    rc = load_run_config(outdir) or {}
    spec = rc.get('corpus') or {}
    mode = {'hybrid': 'hybrid', 'library': 'library'}.get(
        spec.get('type'), 'sqlite' if spec else 'hybrid')
    library_dir = spec.get('path') if spec.get('type') in ('hybrid', 'library') \
        else DEFAULT_LIBRARY
    if spec.get('type') == 'sqlite':
        db_path = spec.get('path') or OTZARIA_DB
    else:
        db_path = spec.get('db') or OTZARIA_DB
    defaults = Config().to_dict()
    current = dict(defaults)
    current.update(rc.get('config') or {})
    fields = []
    for key, default in defaults.items():
        lab = hebrew.CONFIG_LABELS.get(key, {})
        fields.append({
            'key': key,
            'hebrew': lab.get('hebrew', key),
            'explanation': lab.get('explanation', ''),
            'default': list(default) if isinstance(default, tuple) else default,
            'value': (list(current[key]) if isinstance(current[key], tuple)
                      else current[key]),
            'type': ('list' if key == 'whitelist'
                     else 'float' if isinstance(default, float) else 'int'),
        })
    have_report = os.path.isfile(os.path.join(outdir, 'report.db'))
    stages = [{'key': s,
               'hebrew': hebrew.STAGE_LABELS.get(s, {}).get('hebrew', s),
               'explanation': hebrew.STAGE_LABELS.get(s, {}).get(
                   'explanation', ''),
               # calibrate is a deliberate second pass: only pre-tick it once a
               # report.db from an earlier scan exists for it to learn from
               'default': s in DEFAULT_STAGES or (s == 'calibrate'
                                                  and have_report),
               'available': s != 'calibrate' or have_report}
              for s in STAGE_ORDER]
    return {
        'corpus': {'mode': mode, 'library_dir': library_dir or
                   DEFAULT_LIBRARY, 'db_path': db_path},
        'corpus_modes': hebrew.CORPUS_MODES,
        'fields': fields,
        'stages': stages,
        'run_config': rc or None,
    }
