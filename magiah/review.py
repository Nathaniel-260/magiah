# -*- coding: utf-8 -*-
"""Local review interface.

``magiah review`` starts a small local web server over report.db. Findings
are shown one after another, ranked; every accept/reject is stored in
decisions.db immediately. Rejected words feed the next ``detect`` run as a
whitelist, and accepted fixes can be exported to approved_fixes.csv.

Keyboard: י/Y = accept, נ/N = reject occurrence, ד/D = reject the word
everywhere, space = skip.
"""
import json
import os
import sqlite3
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import RANK_SQL, REPORT_DB_F

DECISIONS_F = 'decisions.db'

PAGE = '''<!DOCTYPE html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>מגיה — סקירת ממצאים</title>
<style>
 body{font-family:"Segoe UI",Arial,sans-serif;margin:0;background:#f4f2ec;color:#222}
 header{background:#2c3e50;color:#fff;padding:10px 18px;display:flex;gap:14px;
        align-items:center;flex-wrap:wrap}
 header h1{font-size:18px;margin:0 0 0 12px}
 select,button{font-size:15px;padding:5px 10px;border-radius:6px;border:1px solid #bbb}
 #stats{margin-inline-start:auto;font-size:14px}
 main{max-width:880px;margin:22px auto;padding:0 14px}
 .card{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.12);
       padding:18px 22px;margin-bottom:14px}
 .word{color:#c0392b;font-weight:bold}
 .sugg{color:#1e7e34;font-weight:bold}
 .meta{color:#666;font-size:13px;margin-top:6px}
 .snippet{font-size:17px;line-height:1.7;background:#faf8f2;border-radius:6px;
          padding:10px 12px;margin-top:10px}
 .btns{margin-top:12px;display:flex;gap:10px}
 .btns button{font-size:15px;padding:8px 18px;cursor:pointer;border:none;color:#fff}
 .ok{background:#28a745}.no{background:#dc3545}.noall{background:#8b0000}
 .skip{background:#6c757d}
 .done{color:#28a745;font-size:22px;text-align:center;margin-top:40px}
 kbd{background:#eee;border-radius:3px;padding:1px 5px;font-size:12px;color:#333}
 .keys{color:#555;font-size:13px;margin-top:8px}
</style></head><body>
<header><h1>מַגִּיהַּ — סקירה</h1>
 סוג: <select id="errtype"></select>
 מקור: <select id="origin"></select>
 <button onclick="doExport()">יצוא החלטות</button>
 <span id="stats"></span>
</header>
<main><div id="card"></div>
<div class="keys">קיצורים: <kbd>י</kbd>/<kbd>Y</kbd> אישור &nbsp;
<kbd>נ</kbd>/<kbd>N</kbd> דחיית מופע &nbsp; <kbd>ד</kbd>/<kbd>D</kbd> דחיית
המילה בכל מקום &nbsp; <kbd>ת</kbd>/<kbd>T</kbd> תיקון אחר &nbsp;
<kbd>רווח</kbd> דילוג</div></main>
<script>
let queue=[], cur=null, decided=0;
async function meta(keepSelection){
  const et=document.getElementById('errtype'), og=document.getElementById('origin');
  const prevEt=et.value, prevOg=og.value;
  const m=await (await fetch('api/meta?origin='+encodeURIComponent(prevOg||''))).json();
  et.innerHTML=m.errtypes.map(t=>`<option value="${t[0]}">${t[0]} (${t[1].toLocaleString()})</option>`).join('');
  if(keepSelection&&prevEt&&[...et.options].some(o=>o.value===prevEt))et.value=prevEt;
  if(og.options.length<=1){
    og.innerHTML='<option value="">הכול</option>'+m.origins.map(o=>`<option>${o}</option>`).join('');
    if(keepSelection)og.value=prevOg;
  }
  et.onchange=()=>{queue=[];next();};
  og.onchange=async()=>{queue=[];await meta(true);next();};
}
async function fill(){
  const et=document.getElementById('errtype').value,
        og=document.getElementById('origin').value;
  const r=await fetch(`api/rows?errtype=${encodeURIComponent(et)}&origin=${encodeURIComponent(og)}`);
  queue=await r.json();
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;');}
async function next(){
  if(!queue.length) await fill();
  cur=queue.shift();
  const c=document.getElementById('card');
  document.getElementById('stats').textContent=`הוחלטו בסשן: ${decided}`;
  if(!cur){c.innerHTML='<div class="done">✔ אין עוד ממצאים בסינון הזה</div>';return;}
  const snip=esc(cur.snippet).replace(new RegExp(esc(cur.word).replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'),'g'),
      `<span class="word">${esc(cur.word)}</span>`);
  c.innerHTML=`<div class="card">
   <div style="font-size:20px"><span class="word">${esc(cur.word)}</span>
     ← <span class="sugg">${esc(cur.suggestion||'?')}</span>
     <span style="color:#888;font-size:14px">(ציון ${cur.rank})</span></div>
   <div class="meta">${esc(cur.source)} · ${esc(cur.ref||'')} · ${esc(cur.errtype)}
     · הקשר מאומת: ${cur.ctx_hits} · בספר: ${cur.sugg_local}</div>
   <div class="snippet">${snip}</div>
   <div class="btns">
    <button class="ok" onclick="decide('accept')">✔ שגיאה — אשר (י)</button>
    <button class="no" onclick="decide('reject')">✘ לא שגיאה (נ)</button>
    <button class="noall" onclick="decide('reject_word')">✘✘ דחה מילה בכל מקום (ד)</button>
    <button class="skip" onclick="decide('skip')">דלג (רווח)</button>
   </div>
   <div class="btns">
    <input id="alt" placeholder="תיקון אחר... (ת ואז Enter)" dir="rtl"
      onkeydown="if(event.key==='Enter'){event.preventDefault();decideAlt();}"
      style="flex:1;font-size:16px;padding:7px 10px;border:1px solid #bbb;border-radius:6px">
    <button class="ok" style="background:#0b6e4f" onclick="decideAlt()">✔ אשר עם התיקון שלי</button>
   </div></div>`;
}
async function decide(v){
  if(!cur)return;
  if(v!=='skip'){
    decided++;
    await fetch('api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({word:cur.word,unit:v==='reject_word'?'*':cur.unit,
                           errtype:cur.errtype,verdict:v==='accept'?'accept':'reject',
                           suggestion:cur.suggestion,source:cur.source,ref:cur.ref})});
    if(decided%10===0)meta(true);
  }
  next();
}
async function decideAlt(){
  const inp=document.getElementById('alt');
  const v=inp?inp.value.trim():'';
  if(!cur||!v)return;
  decided++;
  await fetch('api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({word:cur.word,unit:cur.unit,errtype:cur.errtype,
                         verdict:'accept',suggestion:v,
                         source:cur.source,ref:cur.ref})});
  next();
}
async function doExport(){
  const r=await (await fetch('api/export')).json();
  alert(`נוצרה תיקיית to_send:\\n`+
        `${r.fixes} תיקונים מאושרים — קובץ מאוחד + קובץ לכל מאגר `+
        `(${r.origins.join(', ')})\\n`+
        `${r.rejected} מילים דחויות -> rejected_words.txt`);
}
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='SELECT'||e.target.tagName==='INPUT')return;
  const k=e.key.toLowerCase();
  if(k==='y'||k==='י')decide('accept');
  else if(k==='n'||k==='נ')decide('reject');
  else if(k==='d'||k==='ד')decide('reject_word');
  else if(k==='t'||k==='ת'){e.preventDefault();
    const i=document.getElementById('alt');if(i)i.focus();}
  else if(k===' '){e.preventDefault();decide('skip');}
});
meta().then(next);
</script></body></html>'''


class _Handler(BaseHTTPRequestHandler):
    out_dir = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _db(self):
        con = sqlite3.connect(os.path.join(self.out_dir, REPORT_DB_F))
        con.execute("ATTACH DATABASE ? AS dec",
                    (os.path.join(self.out_dir, DECISIONS_F),))
        con.execute('''CREATE TABLE IF NOT EXISTS dec.decisions(
            word TEXT, unit TEXT, errtype TEXT, verdict TEXT,
            suggestion TEXT, source TEXT, ref TEXT,
            PRIMARY KEY(word, unit))''')
        return con

    UNDECIDED = '''NOT EXISTS(SELECT 1 FROM dec.decisions d
                   WHERE d.word = o.word AND (d.unit = o.unit OR d.unit='*'))'''

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ('/', '/index.html'):
            body = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        con = self._db()
        try:
            if url.path == '/api/meta':
                q = urllib.parse.parse_qs(url.query)
                og = q.get('origin', [''])[0]
                owhere, oparams = ('', ())
                if og:
                    owhere, oparams = ' AND origin = ?', (og,)
                ets = con.execute(f'''
                    SELECT errtype, COUNT(*) FROM occurrences_full o
                    WHERE {self.UNDECIDED}{owhere} GROUP BY errtype
                    ORDER BY COUNT(*) DESC''', oparams).fetchall()
                origins = [r[0] for r in con.execute(
                    "SELECT DISTINCT origin FROM occurrences_full "
                    "WHERE origin != '' ORDER BY origin")]
                self._json({'errtypes': ets, 'origins': origins})
            elif url.path == '/api/rows':
                q = urllib.parse.parse_qs(url.query)
                et = q.get('errtype', [''])[0]
                og = q.get('origin', [''])[0]
                where = f'errtype = ? AND {self.UNDECIDED}'
                params = [et]
                if og:
                    where += ' AND origin = ?'
                    params.append(og)
                rows = con.execute(f'''
                    SELECT word, suggestion, ROUND({RANK_SQL},2) AS rank,
                           ctx_hits, sugg_local, errtype, source, ref, unit,
                           snippet
                    FROM occurrences_full o WHERE {where}
                    ORDER BY {RANK_SQL} DESC LIMIT 50''', params)
                cols = ['word', 'suggestion', 'rank', 'ctx_hits', 'sugg_local',
                        'errtype', 'source', 'ref', 'unit', 'snippet']
                self._json([dict(zip(cols, r)) for r in rows])
            elif url.path == '/api/export':
                import csv
                import re as _re
                con.execute('CREATE INDEX IF NOT EXISTS ix_occ_word_unit '
                            'ON occurrences_full(word, unit)')
                # enrich each approved decision with origin + snippet so the
                # fix can be routed to the right source repository
                fixes = con.execute('''
                    SELECT d.word, d.suggestion, o.errtype, o.source, o.ref,
                           d.unit, COALESCE(o.origin, ''), o.snippet
                    FROM dec.decisions d
                    LEFT JOIN occurrences_full o
                      ON o.word = d.word AND o.unit = d.unit
                    WHERE d.verdict = 'accept'
                    ORDER BY COALESCE(o.origin, ''), o.source''').fetchall()
                hdr = ['word', 'suggestion', 'errtype', 'book', 'ref',
                       'line_id', 'origin', 'snippet']
                send_dir = os.path.join(self.out_dir, 'to_send')
                os.makedirs(send_dir, exist_ok=True)
                p1 = os.path.join(send_dir, 'approved_fixes_all.csv')
                with open(p1, 'w', newline='', encoding='utf-8-sig') as f:
                    wr = csv.writer(f)
                    wr.writerow(hdr)
                    wr.writerows(fixes)
                by_origin = {}
                for row in fixes:
                    by_origin.setdefault(row[6] or 'Unknown', []).append(row)
                for org, rows in by_origin.items():
                    safe = _re.sub(r'[^\w.\-]+', '_', org)
                    p = os.path.join(send_dir, f'approved_fixes_{safe}.csv')
                    with open(p, 'w', newline='', encoding='utf-8-sig') as f:
                        wr = csv.writer(f)
                        wr.writerow(hdr)
                        wr.writerows(rows)
                rej = [r[0] for r in con.execute(
                    "SELECT DISTINCT word FROM dec.decisions "
                    "WHERE verdict='reject'")]
                p2 = os.path.join(send_dir, 'rejected_words.txt')
                with open(p2, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(rej))
                self._json({'fixes': len(fixes), 'rejected': len(rej),
                            'origins': sorted(by_origin),
                            'dir': send_dir})
            else:
                self._json({'error': 'not found'}, 404)
        finally:
            con.close()

    def do_POST(self):
        if self.path != '/api/decide':
            self._json({'error': 'not found'}, 404)
            return
        n = int(self.headers.get('Content-Length', 0))
        d = json.loads(self.rfile.read(n).decode('utf-8'))
        con = self._db()
        try:
            con.execute('INSERT OR REPLACE INTO dec.decisions '
                        'VALUES(?,?,?,?,?,?,?)',
                        (d['word'], d['unit'], d.get('errtype', ''),
                         d['verdict'], d.get('suggestion', ''),
                         d.get('source', ''), d.get('ref', '')))
            con.commit()
            self._json({'ok': True})
        finally:
            con.close()


def serve(out_dir, port=8765):
    _Handler.out_dir = os.path.abspath(out_dir)
    srv = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    url = f'http://127.0.0.1:{port}/'
    print(f'[review] serving {url}  (Ctrl+C to stop)', flush=True)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[review] stopped', flush=True)
