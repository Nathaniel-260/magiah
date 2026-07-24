# -*- coding: utf-8 -*-
"""HTTP server for the new review UI.

ThreadingHTTPServer bound to 127.0.0.1 only. JSON API per UI_SPEC §3 + §9b,
static SPA files from webui/static/ (if the frontend is not built yet, `/`
returns a simple Hebrew placeholder page). All user-facing messages are in
Hebrew; every response is UTF-8.
"""
import json
import mimetypes
import os
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, export, hebrew, scanner


def _static_dir():
    """Locate webui/static both as a normal package and when frozen.

    Under a PyInstaller one-file build __file__ points into the temporary
    _MEIPASS extract dir; the data files are unpacked at the same relative
    path (magiah/webui/static), so the __file__-relative path is correct.
    We keep an explicit _MEIPASS fallback in case __file__ resolution is
    unavailable, so a missing static dir never silently falls back to the
    Hebrew placeholder page in a frozen build."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    if os.path.isdir(here):
        return here
    base = getattr(sys, '_MEIPASS', None)
    if base:
        cand = os.path.join(base, 'magiah', 'webui', 'static')
        if os.path.isdir(cand):
            return cand
    return here


STATIC_DIR = _static_dir()

PLACEHOLDER = '''<!DOCTYPE html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>מגיה — ממשק סקירה</title></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;margin:40px auto;
max-width:640px;text-align:center">
<h1>מַגִּיהַּ — ממשק הסקירה</h1>
<p>קבצי הממשק (static/index.html) עדיין לא הותקנו.</p>
<p>שרת ה־API פעיל: אפשר לגשת אל <code>/api/meta</code>,
<code>/api/findings</code> וכו'.</p>
</body></html>'''

_import_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    outdir = None
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(code, body, 'application/json; charset=utf-8')

    def _error(self, msg, code=400):
        self._json({'error': msg}, code)

    def _static(self, relpath):
        if relpath in ('', '/'):
            relpath = 'index.html'
        path = os.path.normpath(os.path.join(STATIC_DIR, relpath))
        base = os.path.normpath(STATIC_DIR)
        if path != base and not path.startswith(base + os.sep):
            self._error(hebrew.MESSAGES['not_found'], 404)
            return
        if not os.path.isfile(path):
            if relpath == 'index.html':
                self._send(200, PLACEHOLDER.encode('utf-8'),
                           'text/html; charset=utf-8')
                return
            self._error(hebrew.MESSAGES['not_found'], 404)
            return
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in (
                'application/javascript', 'application/json'):
            ctype += '; charset=utf-8'
        with open(path, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ValueError(hebrew.MESSAGES['bad_json'])

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path
        try:
            if not path.startswith('/api/'):
                if path == '/':
                    self._static('index.html')
                elif path.startswith('/static/'):
                    self._static(path[len('/static/'):])
                else:
                    self._error(hebrew.MESSAGES['not_found'], 404)
                return
            if path == '/api/scan/status':
                self._json(scanner.get_status())
                return
            if path == '/api/scan/config':
                self._json(scanner.scan_config(self.outdir))
                return
            con = db.connect(self.outdir)
            try:
                self._api_get(path, q, con)
            finally:
                con.close()
        except Exception:
            traceback.print_exc()
            self._error(hebrew.MESSAGES['server_error'], 500)

    def _api_get(self, path, q, con):
        if path == '/api/meta':
            self._json(db.get_meta(con))
        elif path == '/api/books':
            books = db.get_books(con, q.get('origin'), q.get('q'))
            self._json({'books': books, 'rows': books, 'total': len(books)})
        elif path == '/api/findings':
            filters = {k: q.get(k) for k in
                       ('origin', 'book', 'errtype', 'status', 'verified',
                        'min_rank', 'q')}
            rows, total = db.query_findings(
                con, filters, sort=q.get('sort', 'rank'),
                direction=q.get('dir', ''), page=q.get('page', 1),
                page_size=q.get('page_size', 50))
            self._json({'rows': rows, 'total': total,
                        'page': int(q.get('page', 1) or 1)})
        elif path.startswith('/api/finding/'):
            try:
                fid = int(path.rsplit('/', 1)[1])
            except ValueError:
                self._error(hebrew.MESSAGES['bad_request'], 400)
                return
            row = db.get_finding(con, fid)
            if row is None:
                self._error(hebrew.MESSAGES['finding_not_found'], 404)
            else:
                self._json(row)
        elif path == '/api/history':
            self._json({'history': db.get_history(con,
                                                  q.get('limit', 100))})
        elif path == '/api/stats':
            self._json(db.get_stats(con))
        elif path == '/api/fixlist':
            self._json(db.get_fixlist(con, q.get('book'), q.get('origin'),
                                      q.get('statuses')))
        elif path == '/api/backups':
            self._json({'backups': db.list_backups(self.outdir)})
        else:
            self._error(hebrew.MESSAGES['not_found'], 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as e:
            self._error(str(e), 400)
            return
        try:
            if path == '/api/scan/start':
                res = scanner.start_scan(
                    self.outdir, body.get('stages'),
                    body.get('config'), body.get('corpus'))
                self._json({'ok': True, 'status': res,
                            'message': hebrew.SCAN_MESSAGES['started']})
                return
            if path == '/api/scan/cancel':
                self._json(scanner.cancel())
                return
            if path == '/api/refresh':
                with _import_lock:
                    counts = db.import_all(self.outdir)
                self._json({'ok': True, 'counts': counts,
                            'added': counts['added'],
                            'removed': counts['removed'],
                            'preserved': counts['preserved'],
                            'message': 'הרענון הושלם: נוספו '
                                       f"{counts['added']:,}, הוסרו "
                                       f"{counts['removed']:,}, נשמרו "
                                       f"{counts['preserved']:,} החלטות"})
                return
            con = db.connect(self.outdir)
            try:
                self._api_post(path, body, con)
            finally:
                con.close()
        except PermissionError as e:
            self._error(str(e), 423)
        except (ValueError, FileNotFoundError) as e:
            self._error(str(e), 400)
        except Exception:
            traceback.print_exc()
            self._error(hebrew.MESSAGES['server_error'], 500)

    def _api_post(self, path, body, con):
        if path == '/api/status':
            res = db.set_status(
                con, self.outdir, body.get('ids') or [],
                body.get('status', ''), body.get('note'),
                body.get('custom_suggestion'),
                body.get('scope', 'occurrence'))
            self._json({'ok': True, **res})
        elif path == '/api/undo':
            res = db.undo(con, self.outdir)
            if res is None:
                self._error(hebrew.MESSAGES['nothing_to_undo'], 409)
            else:
                self._json({'ok': True, **res})
        elif path == '/api/export/xlsx':
            paths = export.export_xlsx(con, self.outdir, body.get('origin'))
            self._json({'ok': True, 'files': paths,
                        'message': hebrew.MESSAGES['export_done']})
        elif path == '/api/export/fixes':
            res = export.export_fixes(con, self.outdir)
            self._json({'ok': True, **res,
                        'message': hebrew.MESSAGES['export_done']})
        elif path == '/api/import_legacy':
            res = db.migrate_legacy_decisions(con, self.outdir)
            self._json({'ok': True, **res,
                        'message': 'יובאו החלטות ישנות: '
                                   f"{res['review']} ממצאים, "
                                   f"{res['word_rules']} כללי מילים"})
        elif path == '/api/reset':
            res = db.reset(con, self.outdir, body.get('scope', 'statuses'))
            self._json({'ok': True, **res,
                        'message': 'האיפוס בוצע. גיבוי נשמר בקובץ: '
                                   + res['backup']})
        elif path == '/api/restore':
            res = db.restore_backup(con, self.outdir, body.get('file', ''))
            self._json({'ok': True, **res,
                        'message': 'השחזור הושלם: '
                                   f"{res['review']} ממצאים, "
                                   f"{res['word_rules']} כללי מילים"})
        else:
            self._error(hebrew.MESSAGES['not_found'], 404)


def serve(outdir, port=8766, open_browser=True):
    outdir = os.path.abspath(outdir)
    Handler.outdir = outdir
    ui_db = os.path.join(outdir, db.UI_DB_F)
    fresh = not os.path.exists(ui_db)
    if fresh:
        print('[webui] ' + hebrew.MESSAGES['db_missing'], flush=True)
        try:
            counts = db.import_all(outdir)
            print(f"[webui] import done: {counts.get('total', 0):,} findings "
                  f"in {counts.get('seconds', 0)}s", flush=True)
        except FileNotFoundError:
            # No scan in this folder yet. Still start the server: the UI opens
            # in "no scan" mode where the user can launch one from the scan
            # panel, then load the findings without restarting.
            print('[webui] ' + hebrew.MESSAGES['no_scan_console'], flush=True)
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{port}/'
    print(f'[webui] serving {url}  (Ctrl+C to stop)', flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[webui] stopped', flush=True)
    finally:
        srv.server_close()
