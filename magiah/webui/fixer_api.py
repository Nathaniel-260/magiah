# -*- coding: utf-8 -*-
"""Request handlers for מצב מתקן — the in-file corrector.

Kept out of server.py so the routing table there stays a routing table. Every
function takes an open connection and returns a plain dict for the JSON
encoder; refusals are raised as :class:`patcher.PatchError` and mapped to a
status code by the caller.

The write path is deliberately paranoid, in this order:

1. the client's fingerprint must still match the file on disk
2. every finding's own ``unit`` must resolve to the file being written —
   a finding from another book is refused, never applied
3. every edit is planned and verified; a single failure aborts the batch
4. only then is anything written, and a backup is taken first
5. the status change happens last, so a DB problem can never leave the file
   unwritten while the UI believes it was corrected

Step 2 is what makes the ``source``-collision impossible: in the real corpus
the filename stem 'פרק א' belongs to 35 different books, and this check is the
reason a correction for one of them can never reach another.
"""
import os

from . import db, hebrew, patcher, scanner

# how much of the book to send when it is too big to ship whole
WINDOW = 40
MAX_FULL_LINES = 4000


def _library_dir(outdir):
    try:
        return scanner.scan_config(outdir)['corpus'].get('library_dir')
    except Exception:                       # config unreadable -> the default
        return None


def _statuses(raw, default='approved'):
    if not raw:
        return [default]
    return [s for s in str(raw).split(',') if s] or [default]


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
    lib = _library_dir(outdir)
    statuses = _statuses(q.get('statuses'))
    items = db.get_fixer_items(con, key, statuses=statuses,
                               origin=q.get('origin'))

    if key.startswith('db:'):
        # a book with no file: the worklist is still useful for export
        return {'book': {'key': key, 'editable': False, 'kind': 'db'},
                'editable': False, 'items': items, 'lines': [],
                'line_count': 0, 'message': hebrew.FIXER_MESSAGES['db_book']}

    _kind, path = patcher.resolve_key(key, lib)
    fdoc = patcher.read_doc(path)
    patcher.anchor_rows(fdoc, items)

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

    # token spans only where an anchor is unresolved — that is the only case
    # the UI needs them for (click the right word), and they are not free
    need_tokens = {i['lineno'] for i in items
                   if not i['anchor'].get('ok')
                   and i.get('lineno') is not None}
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
    }


# ---------------------------------------------------------------------------
# POST /api/fixer/apply
# ---------------------------------------------------------------------------

def apply(con, outdir, body):
    key = (body.get('key') or '').strip()
    if not key:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    if key.startswith('db:'):
        raise patcher.PatchError('db_book')
    req = body.get('items') or []
    if not req:
        raise ValueError(hebrew.FIXER_MESSAGES['nothing_to_apply'])

    lib = _library_dir(outdir)
    _kind, path = patcher.resolve_key(key, lib)
    fp_before = patcher.check_fingerprint(path, body.get('fingerprint'))
    fdoc = patcher.read_doc(path)

    default_mode = body.get('default_mode') or patcher.MODE_REPLACE
    if default_mode not in patcher.MODES:
        default_mode = patcher.MODE_REPLACE

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

    # occurrence numbers are recomputed from the DB, never taken from the
    # client, so a stale tab cannot point an edit at the wrong occurrence
    live = {i['id']: i for i in db.get_fixer_items(
        con, key, statuses=['approved', 'fixed', 'unsure', 'pending',
                            'not_error', 'ignored'])}

    findings, modes, explicit = [], {}, {}
    for fid, r in by_id.items():
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
            'snippet': row.get('snippet'),
            'occurrence': ref.get('occurrence', 0),
            'expected_count': ref.get('expected_count')})
        if r.get('mode') in patcher.MODES:
            modes[fid] = r['mode']
        if r.get('explicit_start') is not None \
                and r.get('explicit_end') is not None:
            explicit[fid] = (int(r['explicit_start']), int(r['explicit_end']))

    plans, failures = patcher.plan_all(fdoc, findings, default_mode,
                                       modes, explicit)
    if failures:
        # nothing is written when anything is in doubt
        return {'ok': False, 'failed': failures,
                'error': hebrew.FIXER_MESSAGES['apply_refused']}, 409

    changed = patcher.apply_edits(fdoc, plans)
    res = patcher.write_doc(fdoc, outdir)

    ids = [p.finding_id for p in plans]
    edit_id = db.record_file_edit(
        con, path, key, res['backup'], default_mode, ids,
        patcher.detail_json(plans), fp_before, res['fingerprint'])

    out = {'ok': True, 'edit_id': edit_id,
           'applied': [p.to_dict() for p in plans], 'failed': [],
           'changed_lines': [{'n': n, 'text': fdoc.lines[n]} for n in changed],
           'backup': res['backup'], 'fingerprint': res['fingerprint'],
           'message': hebrew.FIXER_MESSAGES['applied']}
    if body.get('mark_fixed', True):
        try:
            # custom corrections are persisted per finding so the file and the
            # database never disagree about what was written
            for fid in ids:
                cs = by_id[fid].get('correction')
                db.set_status(con, outdir, [fid], 'fixed',
                              custom_suggestion=cs or None)
            out['fixed'] = len(ids)
        except Exception:
            # the file is already correct and backed up; re-marking is
            # idempotent, so this is a warning rather than a failure
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
    if rec.get('undone_at'):
        raise patcher.PatchError('backup_missing',
                                 hebrew.MESSAGES['nothing_to_undo'])
    patcher.restore_backup(outdir, rec['backup'], rec['path'],
                           rec.get('fp_after'))
    db.mark_edit_undone(con, rec['id'])
    ids = rec['finding_ids']
    restored = 0
    if ids:
        try:
            db.set_status(con, outdir, ids, 'approved')
            restored = len(ids)
        except Exception:
            pass
    return {'ok': True, 'restored': restored,
            'fingerprint': patcher.fingerprint(rec['path']),
            'message': hebrew.FIXER_MESSAGES['restored']}


# ---------------------------------------------------------------------------
# GET /api/fixer/edits  ·  POST /api/fixer/mode
# ---------------------------------------------------------------------------

def edits(con, q):
    return {'edits': db.get_file_edits(con, q.get('key'),
                                       limit=int(q.get('limit', 50)))}


def set_mode(con, body):
    key = (body.get('key') or '').strip()
    if not key:
        raise ValueError(hebrew.MESSAGES['bad_request'])
    mode = db.set_fixer_mode(con, key, body.get('mode') or 'replace')
    return {'ok': True, 'key': key, 'mode': mode}
