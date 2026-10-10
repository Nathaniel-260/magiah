# -*- coding: utf-8 -*-
"""Request handlers for מצב מתקן — the in-file corrector.

Kept out of server.py so the routing table there stays a routing table. Every
function takes an open connection and returns a plain dict for the JSON
encoder; refusals are raised as :class:`patcher.PatchError` and mapped to a
status code by the caller.

The write path is deliberately paranoid, in this order:

1. every finding's own ``unit`` must resolve to the file being written —
   a finding from another book is refused, never applied
2. the file is resolved against the library root each finding was SCANNED
   from, which must also be the configured one (``source_mismatch``)
3. under a per-file lock, any interrupted earlier write is settled, the file
   is re-read and its fingerprint must still match the client's
4. every edit is planned and verified; a single failure aborts the batch
5. a backup is taken and a journal intent is made durable; only then is the
   file replaced, and the ``file_edits`` row and statuses follow

Step 1 is what makes the ``source``-collision impossible: in the real corpus
the filename stem 'פרק א' belongs to 35 different books, and this check is the
reason a correction for one of them can never reach another.
"""
import json
import os
import sqlite3
import stat
import traceback

from ..corpus_hybrid import DEFAULT_LIBRARY
from . import db, hebrew, journal, patcher, scanner

# how much of the book to send when it is too big to ship whole
WINDOW = 40
MAX_FULL_LINES = 4000


def _library_dir(outdir):
    try:
        return scanner.scan_config(outdir)['corpus'].get('library_dir')
    except Exception:                       # config unreadable -> the default
        return None


def _msg_not_approved(n):
    return hebrew.FIXER_MESSAGES['not_approved'].format(n=n)


def _statuses(raw, default='approved'):
    if not raw:
        return [default]
    return [s for s in str(raw).split(',') if s] or [default]


# ---------------------------------------------------------------------------
# which library root a finding was scanned from
# ---------------------------------------------------------------------------

def _norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _same_root(a, b):
    """Do two spellings name one library folder?

    First by normalized real path; then by file identity, because realpath
    leaves ``\\\\server\\share\\...`` and a drive letter for the same folder
    apart, while both report the same volume and file id. A folder that is
    missing, or a filesystem without file ids (id 0), compares by path only,
    so a doubt is always a mismatch, never a match.
    """
    if _norm(a) == _norm(b):
        return True
    try:
        sa, sb = os.stat(a), os.stat(b)
    except (OSError, ValueError):
        return False
    return (stat.S_ISDIR(sa.st_mode) and stat.S_ISDIR(sb.st_mode)
            and bool(sa.st_ino) and bool(sa.st_dev)
            and (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino))


def _row_scope(row):
    """'report' for full-scan rows, 'doc:<doc>' for single-book-scan rows."""
    extra = row.get('extra') or {}
    if isinstance(extra, str):              # db._rowdict usually parsed it
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = {}
    if isinstance(extra, dict) and extra.get('book_scan'):
        return 'doc:' + (row.get('doc') or '')
    return 'report'


def _report_scan_meta(outdir):
    """report.db's own record of the corpus it was built from (core.locate
    writes it); None for a report from an older version."""
    p = os.path.join(outdir, db.REPORT_DB_F)
    if not os.path.isfile(p):
        return None
    try:
        rep = sqlite3.connect(db._uri(p, ro=True), uri=True)
        try:
            return dict(rep.execute('SELECT key, value FROM scan_meta'))
        finally:
            rep.close()
    except sqlite3.Error:
        return None


def _report_root(con, outdir):
    """The library root the current full import was scanned from; None
    while it cannot be known.

    import_all records it in the same transaction as the rows (from
    report.db's own scan_meta), so neither a later scan written into this
    folder nor a single-book merge can change what it says — and nothing
    here is inferred from timestamps.

    An import made by an older version recorded nothing. Then report.db
    naming a root of its own means it was written AFTER that import (by
    this version), so it says nothing about the rows: unknown. Otherwise the
    configured library stands in, pinned to that import the first time the
    fixer sees it, so a later change of the setting cannot move it.
    """
    src = db.get_import_source(con)
    if src is not None:
        return src.get('root') or None
    stamp = db.full_import_stamp(con)
    rec = db.get_source_root(con, 'report')
    if rec is not None and rec['stamp'] == stamp:
        return rec['root']
    if _report_scan_meta(outdir) is not None:
        return None
    root = db.config_root_for_report(outdir)
    if root:
        db.record_source_root(con, 'report', root, stamp)
    return root


def _book_scan_scopes(rows):
    return {_row_scope(r) for r in rows} - {'report'}


def _mark_trusted(con, rows, fp, size):
    """Flag rows whose book file is byte-identical to the bytes their scan
    read (fingerprint AND size, both recorded at read time): their recorded
    line number is then evidence in its own right — together with an exact
    snippet window there (patcher._locate_line)."""
    seen = {}
    for r in rows:
        scope = _row_scope(r)
        if scope == 'report':
            r['trusted'] = False
            continue
        if scope not in seen:
            rec = db.get_source_root(con, scope) or {}
            seen[scope] = (rec.get('file_sha'), rec.get('file_size'))
        sha, rsize = seen[scope]
        r['trusted'] = (bool(sha) and rsize is not None and sha == fp
                        and int(rsize) == size)


def _resolve_book(con, outdir, key, rows):
    """``(kind, path, root)`` for a book key, resolved against the root its
    findings were scanned from — never a guess from the current setting.

    Every row is checked, but each scope's root is looked up once and each
    distinct root compared once: a book of thousands of rows is resolved on
    every page load, and comparing paths touches the disk (over a network
    share, slowly)."""
    if not key.startswith('file:'):
        kind, path = patcher.resolve_key(key)
        return kind, path, None
    configured = os.path.abspath(_library_dir(outdir) or DEFAULT_LIBRARY)
    by_scope = {}
    for row in rows:
        scope = _row_scope(row)
        if scope not in by_scope:
            if scope == 'report':
                by_scope[scope] = _report_root(con, outdir)
            else:
                rec = db.get_source_root(con, scope)
                by_scope[scope] = rec['root'] if rec else None
        if by_scope[scope] is None:
            raise patcher.PatchError('source_unknown', id=row.get('id'))
    roots = []
    for root in dict.fromkeys(by_scope.values()):    # distinct, in order
        if not any(_same_root(root, r) for r in roots):
            roots.append(root)
    root = roots[0] if roots else configured
    if len(roots) > 1 or not _same_root(root, configured):
        raise patcher.PatchError('source_mismatch', recorded=root,
                                 configured=configured)
    kind, path = patcher.resolve_key(key, root)
    return kind, path, root


def _conflicts(outdir, path):
    """Interrupted writes of this book that could not be settled because
    the file changed since: shown in the fixer until the user acknowledges
    them (resolve_conflict)."""
    keep = ('jid', 'ts', 'kind', 'backup', 'finding_ids', 'fp_before',
            'fp_after', 'fp_seen')
    return [dict({k: c.get(k) for k in keep},
                 message=hebrew.FIXER_MESSAGES['journal_conflict'])
            for c in journal.conflicts(outdir, path)]


# ---------------------------------------------------------------------------
# GET /api/fixer/books
# ---------------------------------------------------------------------------

def books(con, outdir, q):
    lib = _library_dir(outdir)
    out = db.get_fixer_books(con, origin=q.get('origin'),
                             statuses=_statuses(q.get('statuses')),
                             query=q.get('q'))
    for b in out:
        if not b['editable']:
            b['exists'] = True              # a DB book always "exists"
            b['path'] = ''
            continue
        try:
            _kind, path = patcher.resolve_key(b['key'], lib)
            b['path'] = path
            b['exists'] = os.path.isfile(path)
        except patcher.PatchError as e:
            b['path'] = ''
            b['exists'] = False
            b['error'] = str(e)
    return {'books': out, 'total': len(out),
            'library_dir': lib or '',
            'modes': hebrew.FIXER_MODES}


# ---------------------------------------------------------------------------
# GET /api/fixer/doc
# ---------------------------------------------------------------------------

def doc(con, outdir, q):
    key = (q.get('key') or '').strip()
    if not key:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    statuses = _statuses(q.get('statuses'))
    items = db.get_fixer_items(con, key, statuses=statuses,
                               origin=q.get('origin'))

    if key.startswith('db:'):
        # a book with no file: the worklist is still useful for export
        return {'book': {'key': key, 'editable': False, 'kind': 'db'},
                'editable': False, 'items': items, 'lines': [],
                'line_count': 0, 'message': hebrew.FIXER_MESSAGES['db_book']}

    _kind, path, _root = _resolve_book(con, outdir, key, items)
    # never wait on a busy file just to draw a page; a writer settles it
    journal.recover(con, outdir, path, wait=False)
    fdoc = patcher.read_doc(path)
    _mark_trusted(con, items, fdoc.fingerprint, fdoc.size)
    patcher.anchor_rows(fdoc, items, db.get_live_edit_entries(con, key))

    # lines the client needs: the ones carrying findings, plus context. Whole
    # books can be 190k lines, and shipping those to the browser would stall
    # the tab for something nobody reads.
    full = q.get('full') in ('1', 'true', 'yes')
    hit_lines = sorted({i['lineno'] for i in items
                        if i.get('lineno') is not None})
    if full or len(fdoc.lines) <= MAX_FULL_LINES:
        wanted = range(len(fdoc.lines))
    else:
        keep = set()
        for n in hit_lines:
            keep.update(range(max(0, n - WINDOW),
                              min(len(fdoc.lines), n + WINDOW + 1)))
        wanted = sorted(keep)

    # token spans only where an unresolved anchor may be resolved by a click:
    # the located line or the identified candidates, never an unproven line
    need_tokens = {n for i in items if not i['anchor'].get('ok')
                   for n in i['anchor'].get('manual_lines') or []}
    lines = []
    for n in wanted:
        row = {'n': n, 'text': fdoc.lines[n]}
        if n in need_tokens:
            row['tokens'] = patcher.line_tokens(fdoc, n)
        lines.append(row)

    counts = {}
    for i in items:
        st = i.get('effective_status') or 'pending'
        counts[st] = counts.get(st, 0) + 1
    return {
        'book': {'key': key, 'path': path, 'kind': _kind, 'editable': True,
                 'title': os.path.splitext(os.path.basename(path))[0]},
        'editable': True,
        'encoding': fdoc.encoding, 'bom': fdoc.bom,
        'fingerprint': fdoc.fingerprint,
        'line_count': len(fdoc.lines),
        'windowed': not (full or len(fdoc.lines) <= MAX_FULL_LINES),
        'lines': lines,
        'items': items,
        'counts': counts,
        'default_mode': db.get_fixer_mode(con, key),
        'modes': hebrew.FIXER_MODES,
        'edits': db.get_file_edits(con, key, limit=20),
        'journal_conflicts': _conflicts(outdir, path),
    }


# ---------------------------------------------------------------------------
# POST /api/fixer/apply
# ---------------------------------------------------------------------------

def _write_journaled(outdir, rec, path, data_before, data_after):
    """Backup, durable intent, atomic replace. Returns ``(jid, backup)``.

    If the replace fails the intent is closed as 'aborted' only when the file
    is provably untouched; otherwise it stays open for :func:`journal.recover`.
    """
    fp_before = patcher.fingerprint_bytes(data_before)
    bpath, bsha = patcher.write_backup(outdir, path, data_before)
    jid = journal.begin(outdir, dict(
        rec, path=path, backup=bpath, backup_sha=bsha, fp_before=fp_before,
        fp_after=patcher.fingerprint_bytes(data_after)))
    try:
        patcher.atomic_write(path, data_after)
    except BaseException:
        try:
            if patcher.fingerprint(path) == fp_before:
                journal.finish(outdir, jid, 'aborted')
        except OSError:
            pass
        raise
    return jid, bpath, bsha


def _settle_or_refuse(con, outdir, path):
    """Settle interrupted writes of this file; refuse while one stays open
    (e.g. the database is still locked), or its record would be lost."""
    journal.recover(con, outdir, path)
    if journal.pending(outdir, path):
        raise patcher.PatchError('journal_pending')


def _advance_trust(con, rows, fp_before, data_after):
    fp_after = patcher.fingerprint_bytes(data_after)
    for scope in _book_scan_scopes(rows):
        try:
            db.advance_scan_file_sha(con, scope, fp_before, fp_after,
                                     len(data_after))
        except Exception:
            traceback.print_exc()


def apply(con, outdir, body):
    key = (body.get('key') or '').strip()
    if not key:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    if key.startswith('db:'):
        raise patcher.PatchError('db_book')
    req = body.get('items') or []
    if not req:
        raise ValueError(hebrew.FIXER_MESSAGES['nothing_to_apply'])

    by_id = {int(r['id']): r for r in req if r.get('id') is not None}
    rows = {}
    for fid in by_id:
        row = db.get_finding(con, fid)
        if row is None:
            raise patcher.PatchError(
                'unit_mismatch', hebrew.MESSAGES['finding_not_found'], id=fid)
        # THE guard: this finding must belong to the file being written
        if patcher.book_key_of(row.get('unit')) != key:
            raise patcher.PatchError('unit_mismatch', id=fid)
        rows[fid] = row
    _kind, path, root = _resolve_book(con, outdir, key, rows.values())

    default_mode = body.get('default_mode') or patcher.MODE_REPLACE
    if default_mode not in patcher.MODES:
        default_mode = patcher.MODE_REPLACE

    # Everything from here to the write runs under the file's lock, against
    # bytes read under it: a second writer that read the same version must
    # find the fingerprint changed, not silently overwrite this one's edit.
    with journal.file_lock(path):
        _settle_or_refuse(con, outdir, path)
        data = patcher.read_bytes(path)
        fp_before = patcher.fingerprint_bytes(data)
        if not body.get('fingerprint') or body['fingerprint'] != fp_before:
            raise patcher.PatchError('file_changed')
        fdoc = patcher.doc_from_bytes(path, data)
        return _apply_locked(con, outdir, body, key, path, root, fdoc, data,
                             by_id, rows, default_mode)


def _apply_locked(con, outdir, body, key, path, root, fdoc, data, by_id,
                  rows, default_mode):
    # occurrence numbers are recomputed from the DB, never taken from the
    # client, so a stale tab cannot point an edit at the wrong occurrence
    # Every status, deliberately: assign_occurrences numbers findings that
    # share (unit, word), and it can only number them correctly if it sees ALL
    # the siblings on that line. Narrowing this list to the statuses being
    # written would renumber the occurrences and point an edit at the wrong
    # copy of the word.
    live = {i['id']: i for i in db.get_fixer_items(
        con, key, statuses=['approved', 'fixed', 'unsure', 'pending',
                            'not_error', 'ignored'])}

    # Nothing gets written into a book unless a human judged it an error.
    # The UI enforces this too, but the UI is not a security boundary: a stale
    # tab, a bookmarked request or a future code path must not be able to
    # commit an unreviewed finding to somebody's text.
    undecided = sorted(
        fid for fid in by_id
        if (live.get(fid, {}).get('effective_status')
            or 'pending') not in ('approved', 'fixed'))
    if undecided:
        raise patcher.PatchError('not_approved',
                                 _msg_not_approved(len(undecided)),
                                 ids=undecided)

    # A finding whose recorded edit is still in the file was already written
    # (e.g. the status update was lost): re-anchoring it would find the word
    # inside its own correction and apply it twice.
    recorded = db.get_live_edit_entries(con, key)
    _mark_trusted(con, list(rows.values()), fdoc.fingerprint, len(data))
    already = sorted(fid for fid in by_id if fid in recorded
                     and patcher.entry_in_place(fdoc, recorded[fid]))

    findings, modes, explicit = [], {}, {}
    for fid, r in by_id.items():
        if fid in already:
            continue
        row = rows[fid]
        ref = live.get(fid, {})
        correction = (r.get('correction')
                      or row.get('custom_suggestion')
                      or row.get('suggestion') or '')
        findings.append({
            'id': fid,
            'lineno': patcher.resolve_unit_lineno(row.get('unit')),
            'word': row.get('word'),
            'correction': correction,
            'suggestion': row.get('suggestion'),
            'snippet': row.get('snippet'),
            'occurrence': ref.get('occurrence', 0),
            'expected_count': ref.get('expected_count'),
            'errtype': row.get('errtype'),
            'trusted': row.get('trusted', False)})
        if r.get('mode') in patcher.MODES:
            modes[fid] = r['mode']
        if r.get('explicit_start') is not None \
                and r.get('explicit_end') is not None:
            explicit[fid] = (int(r['explicit_start']), int(r['explicit_end']))
            if r.get('explicit_lineno') is not None:
                findings[-1]['explicit_lineno'] = int(r['explicit_lineno'])

    plans, failures = patcher.plan_all(fdoc, findings, default_mode,
                                       modes, explicit,
                                       own_edits=list(recorded.values()))
    if failures:
        # nothing is written when anything is in doubt
        return {'ok': False, 'failed': failures,
                'error': hebrew.FIXER_MESSAGES['apply_refused']}, 409

    ids = [p.finding_id for p in plans]
    mark_fixed = body.get('mark_fixed', True)
    out = {'ok': True, 'edit_id': None, 'applied': [], 'failed': [],
           'already_applied': already, 'changed_lines': [],
           'fingerprint': fdoc.fingerprint,
           'message': hebrew.FIXER_MESSAGES['applied']}
    if plans:
        changed = patcher.apply_edits(fdoc, plans)
        new_data = fdoc.encode()
        detail = patcher.detail_json(plans)
        jid, bpath, bsha = _write_journaled(outdir, {
            'kind': 'apply', 'book_key': key, 'mode': default_mode,
            'finding_ids': ids, 'detail': detail, 'source_root': root,
            'mark_fixed': bool(mark_fixed),
            'corrections': {str(fid): by_id[fid].get('correction')
                            for fid in ids}}, path, data, new_data)
        fp_after = patcher.fingerprint_bytes(new_data)
        try:
            out['edit_id'] = db.record_file_edit(
                con, path, key, bpath, default_mode, ids, detail,
                fdoc.fingerprint, fp_after, journal_id=jid, backup_sha=bsha,
                source_root=root)
        except Exception:
            # the file is written and the journal intent is still open: the
            # next access (journal.recover) creates this row, so it stays
            # undoable; tell the user the bookkeeping lagged
            traceback.print_exc()
            out['db_warning'] = hebrew.FIXER_MESSAGES['db_warning']
        else:
            journal.finish(outdir, jid, 'committed', edit_id=out['edit_id'])
        _advance_trust(con, rows.values(), fdoc.fingerprint, new_data)
        out.update(applied=[p.to_dict() for p in plans],
                   changed_lines=[{'n': n, 'text': fdoc.lines[n]}
                                  for n in changed],
                   backup=bpath, fingerprint=fp_after)
    else:
        out['message'] = hebrew.FIXER_MESSAGES['already_applied'].format(
            n=recorded[already[0]]['lineno'] + 1)
    if mark_fixed:
        try:
            # custom corrections are persisted per finding so the file and the
            # database never disagree about what was written
            for fid in ids + already:
                cs = by_id[fid].get('correction')
                db.set_status(con, outdir, [fid], 'fixed',
                              custom_suggestion=cs or None)
            out['fixed'] = len(ids) + len(already)
        except Exception:
            # the file is already correct and backed up; re-marking is
            # idempotent, so this is a warning rather than a failure. It is
            # still a bug worth seeing, so it goes to the console — never
            # swallowed silently.
            traceback.print_exc()
            out['db_warning'] = hebrew.FIXER_MESSAGES['db_warning']
    return out, 200


# ---------------------------------------------------------------------------
# POST /api/fixer/undo_file
# ---------------------------------------------------------------------------

def undo_file(con, outdir, body):
    edit_id = body.get('edit_id')
    if edit_id is None:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    rec = db.get_file_edit(con, int(edit_id))
    if rec is None:
        raise FileNotFoundError(hebrew.MESSAGES['not_found'])
    path = rec['path']
    with journal.file_lock(path):
        _settle_or_refuse(con, outdir, path)
        rec = db.get_file_edit(con, rec['id'])
        if rec.get('undone_at'):
            raise patcher.PatchError('backup_missing',
                                     hebrew.MESSAGES['nothing_to_undo'])
        data = patcher.read_bytes(path)
        restored = None
        if patcher.fingerprint_bytes(data) == rec.get('fp_after'):
            # untouched since the write: the verified backup is exact
            try:
                restored = patcher.read_backup(
                    outdir, rec['backup'],
                    rec.get('backup_sha') or rec.get('fp_before'))
            except patcher.PatchError as e:
                if e.code not in ('backup_corrupt', 'backup_missing',
                                  'bad_backup'):
                    raise
        if restored is None:
            # changed since (or no usable backup): put back only the recorded
            # spans, each verified in place, so later changes survive
            fdoc = patcher.doc_from_bytes(path, data)
            patcher.reverse_entries(fdoc, rec.get('detail') or [])
            restored = fdoc.encode()
        ids = rec['finding_ids']
        jid, _b, _s = _write_journaled(outdir, {
            'kind': 'undo', 'edit_id': rec['id'], 'book_key': rec['book_key'],
            'finding_ids': ids}, path, data, restored)
        db.mark_edit_undone(con, rec['id'])
        journal.finish(outdir, jid, 'committed', edit_id=rec['id'])
        rows = [db.get_finding(con, fid) for fid in rec['finding_ids']]
        _advance_trust(con, [r for r in rows if r],
                       patcher.fingerprint_bytes(data), restored)
    out = {'ok': True, 'restored': 0,
           'fingerprint': patcher.fingerprint_bytes(restored),
           'message': hebrew.FIXER_MESSAGES['restored']}
    if ids:
        try:
            db.set_status(con, outdir, ids, 'approved')
            out['restored'] = len(ids)
        except Exception:
            # the FILE is already back, which is the half that mattered; a
            # failed status update is surfaced, never swallowed
            traceback.print_exc()
            out['db_warning'] = hebrew.FIXER_MESSAGES['db_warning']
    return out


# ---------------------------------------------------------------------------
# GET /api/fixer/edits  ·  POST /api/fixer/mode
# ---------------------------------------------------------------------------

def resolve_conflict(con, outdir, body):
    """The user checked a file whose interrupted write could not be settled
    automatically; stop reporting it."""
    jid = (body.get('jid') or '').strip()
    if not jid or jid not in {c['jid'] for c in journal.conflicts(outdir)}:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    journal.resolve(outdir, jid)
    return {'ok': True, 'jid': jid}


def edits(con, q):
    return {'edits': db.get_file_edits(con, q.get('key'),
                                       limit=int(q.get('limit', 50)))}


def set_mode(con, body):
    key = (body.get('key') or '').strip()
    if not key:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    mode = db.set_fixer_mode(con, key, body.get('mode') or 'replace')
    return {'ok': True, 'key': key, 'mode': mode}
