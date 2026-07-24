/* ============ מגיה — app.js (vanilla JS, RTL Hebrew SPA, zero deps) ============
 * All server data is rendered via textContent / createElement — never innerHTML
 * with raw data (XSS-safe). All user-facing text is Hebrew.
 */
"use strict";

/* ---------------------------------------------------------------- helpers */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") n.className = v;
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    n.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return n;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function fmtNum(n) {
  if (n == null || isNaN(n)) return "";
  return Number(n).toLocaleString("he-IL");
}
function fmtRank(n) {
  if (n == null || n === "" || isNaN(n)) return "";
  return (Math.round(Number(n) * 100) / 100).toString();
}

function toast(msg, type, ms) {
  const box = $("#toasts");
  const t = el("div", { class: "toast " + (type || "") }, msg);
  box.append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 320); }, ms || 4000);
}

async function api(path, opts) {
  opts = opts || {};
  let res;
  try {
    res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (e) {
    throw new Error("אין תקשורת עם השרת — ודא שהשרת פועל");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const msg = data && data.error ? data.error : "שגיאת שרת (HTTP " + res.status + ")";
    throw new Error(msg);
  }
  return data;
}

/* clipboard with fallback */
function copyText(text) {
  const done = () => toast("הועתק: " + text, "ok", 1600);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => copyFallback(text, done));
  } else copyFallback(text, done);
}
function copyFallback(text, done) {
  const ta = el("textarea", { style: "position:fixed;opacity:0" }, text);
  document.body.append(ta);
  ta.select();
  try { document.execCommand("copy"); done(); }
  catch (e) { toast("ההעתקה נכשלה", "err"); }
  ta.remove();
}

/* ------------------------------------------------------------ constants */
/* Fallbacks only — real labels come from /api/meta (§4-§5). */
const FALLBACK_STATUSES = [
  { key: "pending", hebrew: "טרם נבדק", icon: "⬜" },
  { key: "approved", hebrew: "אושר — זו שגיאה", icon: "✅" },
  { key: "fixed", hebrew: "תוקן בספר", icon: "🔧" },
  { key: "not_error", hebrew: "לא שגיאה", icon: "❌" },
  { key: "unsure", hebrew: "דרוש בירור", icon: "❓" },
  { key: "ignored", hebrew: "התעלם", icon: "🚫" },
];
const STATUS_ICONS = { pending: "⬜", approved: "✅", fixed: "🔧", not_error: "❌", unsure: "❓", ignored: "🚫" };

/* ------------------------------------------------------------ state */
const S = {
  meta: null,
  view: "table",
  filters: { origin: "", book: "", errtypes: [], statuses: [], verified: false, min_rank: 0, q: "", sort: "rank", dir: "desc" },
  page: 1,
  pageSize: 50,
  tableRows: [],
  tableTotal: 0,
  sel: new Set(),
  sessionActions: 0,
  // cards
  cardQueue: [],
  cardSeen: new Set(),
  cardPage: 1,
  cardExhausted: false,
  cardLoading: false,
  cardStale: true,
  cardFixOpen: false,
  // fixer
  fixBook: "",
  fixInclude: false,
  fixRows: [],
  fixIdx: 0,
  fixAlready: 0,
  fixSession: 0,
  // misc
  lastActions: [],           // local stack for card-restore on undo
  hashLock: false,
  bookList: [],
};

/* metadata accessors */
function statuses() {
  const raw = (S.meta && S.meta.statuses) || FALLBACK_STATUSES;
  return raw.map(s => {
    if (typeof s === "string") {
      const f = FALLBACK_STATUSES.find(x => x.key === s);
      return f || { key: s, hebrew: s, icon: "" };
    }
    return { key: s.key, hebrew: s.hebrew || s.name || s.key, icon: s.icon || STATUS_ICONS[s.key] || "" };
  });
}
function statusInfo(key) {
  return statuses().find(s => s.key === key) || { key, hebrew: key, icon: "" };
}
function colInfo(key) {
  const c = ((S.meta && S.meta.columns) || []).find(x => x.key === key);
  return c || { key, hebrew: key, explanation: "" };
}
function errtypeInfo(key) {
  const e = ((S.meta && S.meta.errtypes) || []).find(x => x.key === key);
  return e || { key, hebrew: key, explanation: "" };
}
function originInfo(name) {
  const o = ((S.meta && S.meta.origins) || []).find(x => x.name === name);
  return o || { name, hebrew: name };
}
function effStatus(row) {
  return row.effective_status || row.status || "pending";
}
function effFix(row) {
  return row.custom_suggestion || row.suggestion || "";
}

/* ---------------------------------------------------------- URL hash */
function writeHash() {
  const f = S.filters, p = new URLSearchParams();
  p.set("v", S.view);
  if (f.origin) p.set("o", f.origin);
  if (f.book) p.set("b", f.book);
  if (f.errtypes.length) p.set("e", f.errtypes.join(","));
  if (f.statuses.length) p.set("s", f.statuses.join(","));
  if (f.verified) p.set("vf", "1");
  if (f.min_rank) p.set("mr", String(f.min_rank));
  if (f.q) p.set("q", f.q);
  if (f.sort !== "rank" || f.dir !== "desc") p.set("sort", f.sort + ":" + f.dir);
  if (S.page > 1) p.set("p", String(S.page));
  if (S.fixBook) p.set("fb", S.fixBook);
  if (S.fixInclude) p.set("fi", "1");
  S.hashLock = true;
  location.hash = p.toString();
  setTimeout(() => { S.hashLock = false; }, 0);
}
function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ""));
  const f = S.filters;
  S.view = p.get("v") || "table";
  f.origin = p.get("o") || "";
  f.book = p.get("b") || "";
  f.errtypes = (p.get("e") || "").split(",").filter(Boolean);
  f.statuses = (p.get("s") || "").split(",").filter(Boolean);
  f.verified = p.get("vf") === "1";
  f.min_rank = parseFloat(p.get("mr") || "0") || 0;
  f.q = p.get("q") || "";
  const so = (p.get("sort") || "rank:desc").split(":");
  f.sort = so[0] || "rank";
  f.dir = so[1] || "desc";
  S.page = parseInt(p.get("p") || "1", 10) || 1;
  S.fixBook = p.get("fb") || "";
  S.fixInclude = p.get("fi") === "1";
}
window.addEventListener("hashchange", () => {
  if (S.hashLock) return;
  readHash();
  syncFilterControls();
  showView(S.view, true); // showView triggers refreshCurrentView
});

/* build query params from current filters */
function filterParams(overrides) {
  const f = S.filters, p = new URLSearchParams();
  const o = overrides || {};
  const val = (k, d) => (k in o ? o[k] : d);
  const origin = val("origin", f.origin), book = val("book", f.book);
  const errtypes = val("errtypes", f.errtypes), sts = val("statuses", f.statuses);
  if (origin) p.set("origin", origin);
  if (book) p.set("book", book);
  if (errtypes && errtypes.length) p.set("errtype", errtypes.join(","));
  if (sts && sts.length) p.set("status", sts.join(","));
  if (val("verified", f.verified)) p.set("verified", "1");
  const mr = val("min_rank", f.min_rank);
  if (mr) p.set("min_rank", String(mr));
  const q = val("q", f.q);
  if (q) p.set("q", q);
  p.set("sort", val("sort", f.sort));
  p.set("dir", val("dir", f.dir));
  p.set("page", String(val("page", S.page)));
  p.set("page_size", String(val("page_size", S.pageSize)));
  return p;
}

/* tolerate several server response shapes */
function rowsOf(resp) {
  if (Array.isArray(resp)) return resp;
  return resp.rows || resp.findings || resp.results || resp.items || [];
}
function totalOf(resp) {
  if (Array.isArray(resp)) return resp.length;
  const t = resp.total != null ? resp.total : resp.count;
  return t != null ? t : rowsOf(resp).length;
}

/* ---------------------------------------------------------- snippets */

/* Locate the reviewed word inside its snippet.
 * The scanner builds snippets with ~45 chars of context on each side, so when a
 * word occurs more than once the *central* occurrence is the reviewed one —
 * plain indexOf() would highlight the wrong copy (~1% of findings). We also
 * prefer occurrences that stand as whole words over ones inside a longer word. */
const HEB_WORDCHAR = /[֐-׿‏‎'"׳״]/;
function locateWord(s, w) {
  if (!w) return -1;
  const mid = s.length / 2;
  let best = -1, bestScore = Infinity;
  for (let i = s.indexOf(w); i >= 0; i = s.indexOf(w, i + 1)) {
    const leftOk = i === 0 || !HEB_WORDCHAR.test(s[i - 1]);
    const rightOk = i + w.length >= s.length || !HEB_WORDCHAR.test(s[i + w.length]);
    // distance of the occurrence's centre from the snippet's centre
    let score = Math.abs(i + w.length / 2 - mid);
    if (!(leftOk && rightOk)) score += 1000; // whole-word matches win outright
    if (score < bestScore) { bestScore = score; best = i; }
  }
  return best;
}

/* Character-level alignment of the wrong word against its correction.
 * Almost every finding here is a one-letter Hebrew confusion (ז/ח, ו/י, ר/ד,
 * ב/כ …), so showing only "red word → green word" makes the reviewer re-read
 * both words letter by letter. Aligning them lets us mark *just* the letters
 * that differ, which is what the eye should land on.
 *
 * Classic LCS backtrace. Words are short (< 20 chars), so the O(n·m) table is
 * free, and unlike a greedy scan it never mis-pairs a shifted insertion.
 * Returns two arrays of {ch, same} — one per word, in logical (not visual)
 * order; the browser's bidi algorithm handles RTL presentation. */
function diffChars(a, b) {
  a = a == null ? "" : String(a);
  b = b == null ? "" : String(b);
  const n = a.length, m = b.length;
  // Guard: pathological input shouldn't build a huge table.
  if (!n || !m || n * m > 40000) {
    return [[{ ch: a, same: false }], [{ ch: b, same: false }]];
  }
  const L = [];
  for (let i = 0; i <= n; i++) L.push(new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    }
  }
  const A = [], B = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { A.push({ ch: a[i], same: true }); B.push({ ch: b[j], same: true }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) { A.push({ ch: a[i], same: false }); i++; }
    else { B.push({ ch: b[j], same: false }); j++; }
  }
  while (i < n) { A.push({ ch: a[i], same: false }); i++; }
  while (j < m) { B.push({ ch: b[j], same: false }); j++; }
  return [A, B];
}

/* Collapse a diff array into <span>s, merging runs so the DOM stays small and
 * — more importantly — so adjacent changed letters read as one blob rather
 * than a row of separately-boxed characters. */
function diffSpans(parts, kind) {
  const out = [];
  let buf = "", bufSame = null;
  const flush = () => {
    if (!buf) return;
    if (bufSame) {
      out.push(document.createTextNode(buf));
    } else {
      // A changed run that is pure whitespace would be invisible, so show the
      // open-box glyph instead — that is the whole finding for the 56k
      // missing_space / extra_space rows.
      const blank = /^\s+$/.test(buf);
      out.push(el("span", { class: "dc dc-" + kind + (blank ? " dc-space" : "") },
                  blank ? "␣" : buf));
    }
    buf = "";
  };
  for (const p of parts) {
    if (p.same !== bufSame) { flush(); bufSame = p.same; }
    buf += p.ch;
  }
  flush();
  return out;
}

/* A word rendered with its differing letters marked, against `other`.
 * `kind` only picks the colour ("err" | "fix") — the marked letters always
 * come from `word` itself, which is diffChars' FIRST return value. (Taking the
 * second for "fix" marks the other word's letters: for יותבת→יושבת that
 * highlighted ת in the correction instead of ש.) */
function wordDiffNode(word, other, kind, cls) {
  const parts = diffChars(word, other)[0];
  const b = el("bdi", { class: cls || null });
  b.append(...diffSpans(parts, kind));
  return b;
}

/* `repl` = replacement to show instead of the word (the "after" rendering).
 * `counter` = the other side of the pair, used only to mark differing letters. */
function renderSnippet(snippet, word, repl, counter) {
  const b = el("bdi");
  const s = snippet == null ? "" : String(snippet);
  if (!s) { b.textContent = "—"; return b; }
  const w = word == null ? "" : String(word);
  const i = locateWord(s, w);
  if (i < 0) { b.textContent = s; return b; }
  b.append(s.slice(0, i));
  const shown = repl != null ? repl : w;
  const kind = repl != null ? "fix" : "err";
  const other = counter != null ? counter : (repl != null ? w : "");
  if (other && other !== shown) {
    b.append(wordDiffNode(shown, other, kind, "hl-" + kind));
  } else {
    b.append(el("span", { class: "hl-" + kind }, shown));
  }
  b.append(s.slice(i + w.length));
  return b;
}

/* ---- the focus line: sentence with the swap stacked in place -------------
 * The reviewer's real task is "is this one letter wrong?", so the sentence must
 * stay a single continuous reading line. At the error we open one shared column:
 * the original word sits raised above the baseline, the correction drops below
 * it, both centred on the same horizontal slot. The eye tracks the sentence
 * straight through and takes the swap in without leaving the line — far faster
 * than comparing two separate before/after paragraphs.
 *
 * The two stacked words are width-matched by the grid, so their letters line up
 * vertically and the differing letter shows up as a break in the column. */
function renderFocusLine(row) {
  const s = row.snippet == null ? "" : String(row.snippet);
  const w = row.word == null ? "" : String(row.word);
  const fix = effFix(row) || "";
  const line = el("div", { class: "focus-line" });
  if (!s) {
    line.append(el("bdi", { class: "fl-text" }, w || "—"));
    return line;
  }
  const i = locateWord(s, w);
  if (i < 0 || !w) {
    line.append(el("bdi", { class: "fl-text" }, s));
    return line;
  }
  const before = s.slice(0, i);
  const after = s.slice(i + w.length);
  const [A, B] = diffChars(w, fix);

  const swap = el("span", { class: "fl-swap" + (fix ? "" : " no-fix") });
  const errW = el("bdi", { class: "fl-word fl-err" });
  errW.append(...diffSpans(A, "err"));
  swap.append(errW);
  if (fix) {
    swap.append(el("span", { class: "fl-rule" }));
    const fixW = el("bdi", { class: "fl-word fl-fix" });
    fixW.append(...diffSpans(B, "fix"));
    swap.append(fixW);
  }
  line.append(el("bdi", { class: "fl-text" }, before), swap, el("bdi", { class: "fl-text" }, after));
  return line;
}

/* Shrink the focus line until the sentence fits on a single row.
 * Wrapping would defeat the whole design, and horizontal scrolling would make
 * the reviewer drag the sentence around, so we scale the type down instead —
 * down to a floor where it is still comfortably larger than body text. */
const FL_MAX = 25, FL_MIN = 13;
window.addEventListener("resize", debounce(() => {
  for (const line of $$(".focus-line")) fitFocusLine(line);
}, 120));
function fitFocusLine(line, max) {
  if (!line || !line.isConnected || !line.clientWidth) return;
  let size = max || Number(line.dataset.flMax) || FL_MAX;
  line.style.setProperty("--fl-size", size + "px");
  // scrollWidth > clientWidth means the nowrap line overflows its box
  let guard = 0;
  while (line.scrollWidth > line.clientWidth && size > FL_MIN && guard++ < 40) {
    size -= 1;
    line.style.setProperty("--fl-size", size + "px");
  }
}

/* ---------------------------------------------------------- status writes */
/* Optimistic update with rollback (§6 robustness). */
function localRowsById(ids) {
  const set = new Set(ids);
  const found = [];
  for (const r of S.tableRows) if (set.has(r.id)) found.push(r);
  for (const r of S.cardQueue) if (set.has(r.id)) found.push(r);
  for (const r of S.fixRows) if (set.has(r.id)) found.push(r);
  return found;
}

async function setStatus(ids, status, opts) {
  opts = opts || {};
  const rows = localRowsById(ids);
  const snapshot = rows.map(r => [r, effStatus(r), r.custom_suggestion, r.note]);
  // optimistic
  for (const r of rows) {
    r.effective_status = status;
    if (opts.custom_suggestion !== undefined) r.custom_suggestion = opts.custom_suggestion;
    if (opts.note !== undefined) r.note = opts.note;
  }
  repaintStatuses();
  const body = { ids, status };
  if (opts.note !== undefined) body.note = opts.note;
  if (opts.custom_suggestion !== undefined) body.custom_suggestion = opts.custom_suggestion;
  if (opts.scope) body.scope = opts.scope;
  try {
    const resp = await api("/api/status", { method: "POST", body });
    S.sessionActions += ids.length;
    S.lastActions.push({ ids, rows: rows.slice(), snapshot, status });
    if (S.lastActions.length > 60) S.lastActions.shift();
    updateSessionCounter();
    updateProgress();
    if (resp && resp.warnings) for (const w of resp.warnings) toast(w, "err", 8000);
    return resp;
  } catch (e) {
    for (const [r, st, cs, nt] of snapshot) { r.effective_status = st; r.custom_suggestion = cs; r.note = nt; }
    repaintStatuses();
    toast("שמירת הסטטוס נכשלה: " + e.message, "err");
    throw e;
  }
}

function repaintStatuses() {
  if (S.view === "table") renderTableRows();
  if (S.view === "fixer") renderFixList(false);
}

async function doUndo() {
  try {
    const resp = await api("/api/undo", { method: "POST" });
    const last = S.lastActions.pop();
    if (last) {
      for (const [r, st, cs, nt] of last.snapshot) { r.effective_status = st; r.custom_suggestion = cs; r.note = nt; }
      // put the finding back at the head of the card queue
      if (S.view === "cards" && last.rows.length === 1 && !S.cardQueue.includes(last.rows[0])) {
        S.cardQueue.unshift(last.rows[0]);
        renderCard();
      }
    }
    let msg = "הפעולה האחרונה בוטלה";
    if (resp && resp.reverted != null) msg += " (" + fmtNum(resp.reverted) + " ממצאים)";
    toast(msg, "ok");
    repaintStatuses();
    updateProgress();
    if (S.view === "table") loadTable();
  } catch (e) {
    toast("הביטול נכשל: " + e.message, "err");
  }
}

/* ---------------------------------------------------------- progress */
const updateProgress = debounce(async function () {
  try {
    const base = filterParams({ statuses: [], page: 1, page_size: 1 });
    const pend = filterParams({ statuses: ["pending"], page: 1, page_size: 1 });
    const [allR, pendR] = await Promise.all([api("/api/findings?" + base), api("/api/findings?" + pend)]);
    const total = totalOf(allR), pending = totalOf(pendR);
    const done = Math.max(0, total - pending);
    $("#progressPill").textContent = "טופלו " + fmtNum(done) + " מתוך " + fmtNum(total);
    $("#progressPill").title = "בסינון הנוכחי (ללא סינון סטטוס)";
  } catch (e) { /* silent */ }
}, 400);

function updateSessionCounter() {
  $("#sessionCounter").textContent = S.sessionActions ? "פעולות במושב זה: " + fmtNum(S.sessionActions) : "";
}

/* ---------------------------------------------------------- sidebar */
function syncFilterControls() {
  const f = S.filters;
  $("#fOrigin").value = f.origin;
  $("#fVerified").checked = f.verified;
  $("#fMinRank").value = f.min_rank;
  $("#fMinRankVal").value = f.min_rank;
  $("#fSort").value = f.sort + ":" + f.dir;
  $("#globalSearch").value = f.q;
  $$("#fErrtypes input[type=checkbox]").forEach(c => { c.checked = f.errtypes.includes(c.value); });
  $$("#fStatuses input[type=checkbox]").forEach(c => { c.checked = f.statuses.includes(c.value); });
  $$("#fBookList .book-item").forEach(b => b.classList.toggle("selected", b.dataset.name === f.book));
  $("#fixInclude").checked = S.fixInclude;
}

function buildSidebar() {
  const m = S.meta;
  // origins
  const sel = $("#fOrigin");
  sel.replaceChildren(el("option", { value: "" }, "כל המאגרים"));
  for (const o of (m.origins || [])) {
    sel.append(el("option", { value: o.name }, (o.hebrew || o.name) + (o.count != null ? " (" + fmtNum(o.count) + ")" : "")));
  }
  // errtypes
  const et = $("#fErrtypes");
  et.replaceChildren();
  for (const e of (m.errtypes || [])) {
    const cnt = e.pending_count != null ? fmtNum(e.pending_count) + " ממתינים" : (e.count != null ? fmtNum(e.count) : "");
    const lb = el("label", { class: "chk-row", title: e.explanation || "" },
      el("input", { type: "checkbox", value: e.key }),
      el("span", null, e.hebrew || e.key),
      el("span", { class: "cnt" }, cnt));
    lb.querySelector("input").addEventListener("change", onErrtypeChange);
    et.append(lb);
  }
  // statuses
  const st = $("#fStatuses");
  st.replaceChildren();
  for (const s of statuses()) {
    const lb = el("label", { class: "chk-row" },
      el("input", { type: "checkbox", value: s.key }),
      el("span", null, s.icon + " " + s.hebrew));
    lb.querySelector("input").addEventListener("change", onStatusFilterChange);
    st.append(lb);
  }
  syncFilterControls();
}

function onErrtypeChange() {
  S.filters.errtypes = $$("#fErrtypes input:checked").map(c => c.value);
  filtersChanged();
}
function onStatusFilterChange() {
  S.filters.statuses = $$("#fStatuses input:checked").map(c => c.value);
  filtersChanged();
}
function filtersChanged() {
  S.page = 1;
  S.sel.clear();
  S.cardStale = true;
  writeHash();
  refreshCurrentView();
  updateProgress();
}

/* books list */
const loadBooks = debounce(async function () {
  const q = $("#fBookSearch").value.trim();
  const p = new URLSearchParams();
  if (S.filters.origin) p.set("origin", S.filters.origin);
  if (q) p.set("q", q);
  try {
    const resp = await api("/api/books?" + p);
    const books = Array.isArray(resp) ? resp : (resp.books || resp.rows || []);
    S.bookList = books;
    const box = $("#fBookList");
    box.replaceChildren();
    if (!books.length) { box.append(el("div", { class: "empty" }, "לא נמצאו ספרים")); return; }
    for (const b of books.slice(0, 300)) {
      const name = b.name || b.source || String(b);
      const pending = b.pending_count != null ? b.pending_count : b.pending;
      const cnt = (pending != null ? fmtNum(pending) + " ממתינים / " : "") + (b.count != null ? fmtNum(b.count) : "");
      const item = el("div", { class: "book-item" + (S.filters.book === name ? " selected" : ""), dataset: { name } },
        el("span", { class: "bname", title: name }, el("bdi", null, name)),
        el("span", { class: "bcount" }, cnt));
      item.addEventListener("click", () => {
        S.filters.book = S.filters.book === name ? "" : name;
        syncFilterControls();
        filtersChanged();
      });
      box.append(item);
    }
  } catch (e) {
    $("#fBookList").replaceChildren(el("div", { class: "empty" }, "טעינת הספרים נכשלה: " + e.message));
  }
}, 250);

/* ---------------------------------------------------------- table view */
const TABLE_COLS = [
  { key: "word", sortable: true },
  { key: "suggestion" },
  { key: "rank", sortable: true },
  { key: "verified" },
  { key: "errtype" },
  { key: "source", sortable: true },
  { key: "ref" },
  { key: "snippet" },
  { key: "status" },
];

function renderTableHead() {
  const tr = el("tr");
  const all = el("input", { type: "checkbox", title: "בחירת כל העמוד" });
  all.addEventListener("change", () => {
    if (all.checked) S.tableRows.forEach(r => S.sel.add(r.id));
    else S.tableRows.forEach(r => S.sel.delete(r.id));
    renderTableRows(); updateBulkBar();
  });
  tr.append(el("th", null, all));
  for (const c of TABLE_COLS) {
    const info = colInfo(c.key);
    const th = el("th", { class: c.sortable ? "sortable" : "" }, info.hebrew);
    if (info.explanation) th.append(el("span", { class: "info-i", dataset: { tip: info.explanation } }, "ⓘ"));
    if (c.sortable) {
      if (S.filters.sort === c.key) th.append(el("span", { class: "arrow" }, S.filters.dir === "desc" ? " ▼" : " ▲"));
      th.addEventListener("click", () => {
        if (S.filters.sort === c.key) S.filters.dir = S.filters.dir === "desc" ? "asc" : "desc";
        else { S.filters.sort = c.key; S.filters.dir = c.key === "rank" ? "desc" : "asc"; }
        syncFilterControls();
        filtersChanged();
      });
    }
    tr.append(th);
  }
  $("#tblHead").replaceChildren(tr);
}

function statusChip(row) {
  const st = effStatus(row);
  const info = statusInfo(st);
  const chip = el("span", { class: "chip st-" + st, title: "לחיצה לשינוי סטטוס" }, (info.icon ? info.icon + " " : "") + info.hebrew);
  chip.addEventListener("click", ev => {
    ev.stopPropagation();
    openStatusMenu(ev.currentTarget, sKey => setStatus([row.id], sKey).catch(() => {}));
  });
  return chip;
}

function renderTableRows() {
  const tb = $("#tblBody");
  tb.replaceChildren();
  if (!S.tableRows.length) {
    tb.append(el("tr", null, el("td", { class: "empty-row", colspan: String(TABLE_COLS.length + 1) }, "אין ממצאים בסינון הנוכחי")));
    return;
  }
  for (const r of S.tableRows) {
    const tr = el("tr", { class: S.sel.has(r.id) ? "selrow" : "" });
    const cb = el("input", { type: "checkbox" });
    cb.checked = S.sel.has(r.id);
    cb.addEventListener("click", ev => {
      ev.stopPropagation();
      if (cb.checked) S.sel.add(r.id); else S.sel.delete(r.id);
      tr.classList.toggle("selrow", cb.checked);
      updateBulkBar();
    });
    tr.append(el("td", null, cb));
    // letter-level diff here too, so scanning the table shows *what* changed
    tr.append(el("td", null, wordDiffNode(r.word || "", effFix(r), "err", "w-err")));
    tr.append(el("td", null, effFix(r)
      ? wordDiffNode(effFix(r), r.word || "", "fix", "w-fix")
      : el("bdi", { class: "w-fix" }, "")));
    tr.append(el("td", { class: "num" }, fmtRank(r.rank)));
    tr.append(el("td", null, r.verified ? el("span", { class: "vbadge" }, "מאומת") : ""));
    tr.append(el("td", null, el("span", { class: "et-tag", title: errtypeInfo(r.errtype).explanation || "" }, errtypeInfo(r.errtype).hebrew)));
    tr.append(el("td", { class: "bookcell", title: r.source || "" }, el("bdi", null, r.source || "")));
    tr.append(el("td", { class: "refcell", title: r.ref || "" }, el("bdi", null, r.ref || "")));
    tr.append(el("td", { class: "snip" }, renderSnippet(r.snippet, r.word, null, effFix(r))));
    tr.append(el("td", null, statusChip(r)));
    tr.addEventListener("click", () => openDrawer(r.id));
    tb.append(tr);
  }
}

async function loadTable() {
  const tb = $("#tblBody");
  tb.replaceChildren(el("tr", null, el("td", { class: "loading-row", colspan: String(TABLE_COLS.length + 1) }, "טוען ממצאים…")));
  renderTableHead();
  try {
    const resp = await api("/api/findings?" + filterParams());
    S.tableRows = rowsOf(resp);
    S.tableTotal = totalOf(resp);
    renderTableRows();
    renderPager();
    updateBulkBar();
  } catch (e) {
    tb.replaceChildren(el("tr", null, el("td", { class: "empty-row", colspan: String(TABLE_COLS.length + 1) }, "הטעינה נכשלה: " + e.message)));
    toast(e.message, "err");
  }
}

function renderPager() {
  const pages = Math.max(1, Math.ceil(S.tableTotal / S.pageSize));
  if (S.page > pages) S.page = pages;
  const go = p => { S.page = p; writeHash(); loadTable(); };
  const mk = (label, p, dis) => {
    const b = el("button", null, label);
    b.disabled = !!dis;
    b.addEventListener("click", () => go(p));
    return b;
  };
  const sizeSel = el("select", { title: "שורות בעמוד" });
  for (const n of [25, 50, 100, 200, 500]) sizeSel.append(el("option", { value: String(n) }, String(n) + " בעמוד"));
  sizeSel.value = String(S.pageSize);
  sizeSel.addEventListener("change", () => { S.pageSize = parseInt(sizeSel.value, 10); S.page = 1; loadTable(); });
  $("#pager").replaceChildren(
    mk("« ראשון", 1, S.page <= 1),
    mk("‹ הקודם", S.page - 1, S.page <= 1),
    el("span", { class: "pinfo" }, "עמוד " + fmtNum(S.page) + " מתוך " + fmtNum(pages)),
    mk("הבא ›", S.page + 1, S.page >= pages),
    mk("אחרון »", pages, S.page >= pages),
    sizeSel,
    el("span", { class: "total" }, "סה״כ " + fmtNum(S.tableTotal) + " ממצאים"),
  );
}

/* bulk bar */
function updateBulkBar() {
  const bar = $("#bulkBar");
  bar.classList.toggle("visible", S.sel.size > 0);
  bar.querySelector(".bulk-count").textContent = "נבחרו " + fmtNum(S.sel.size) + " ממצאים";
  const act = bar.querySelector(".bulk-actions");
  if (!act.childElementCount) {
    for (const s of statuses()) {
      if (s.key === "pending") continue;
      const b = el("button", { class: "stbtn" }, s.icon + " " + s.hebrew);
      b.addEventListener("click", async () => {
        const scope = $("#bulkWordScope").checked ? "word" : "occurrence";
        try {
          await setStatus([...S.sel], s.key, { scope });
          toast("עודכנו " + fmtNum(S.sel.size) + " ממצאים ל: " + s.hebrew, "ok");
          S.sel.clear();
          updateBulkBar();
          loadTable();
        } catch (e) { /* toast already shown */ }
      });
      act.append(b);
    }
  }
}

/* floating status menu */
function openStatusMenu(anchor, onPick) {
  const menu = $("#stMenu");
  menu.replaceChildren();
  for (const s of statuses()) {
    const b = el("button", null, s.icon + " " + s.hebrew);
    b.addEventListener("click", () => { closeStatusMenu(); onPick(s.key); });
    menu.append(b);
  }
  const r = anchor.getBoundingClientRect();
  menu.style.display = "block";
  const mw = 180;
  menu.style.top = Math.min(window.innerHeight - 250, r.bottom + 4) + "px";
  menu.style.left = Math.max(8, Math.min(window.innerWidth - mw - 8, r.left)) + "px";
  setTimeout(() => document.addEventListener("click", closeStatusMenu, { once: true }), 0);
}
function closeStatusMenu() { $("#stMenu").style.display = "none"; }

/* ---------------------------------------------------------- drawer */
async function openDrawer(id) {
  $("#drawer").classList.add("visible");
  $("#drawerScrim").classList.add("visible");
  const body = $("#drawerBody");
  body.replaceChildren(el("div", { class: "loading-row" }, "טוען…"));
  let resp;
  try {
    resp = await api("/api/finding/" + id);
  } catch (e) {
    body.replaceChildren(el("div", { class: "loading-row" }, "הטעינה נכשלה: " + e.message));
    toast(e.message, "err");
    return;
  }
  const r = resp.finding || resp.row || resp;
  const history = resp.history || r.history || [];
  // keep local caches in sync if this row is on screen
  const local = localRowsById([r.id])[0];
  if (local) { r.effective_status = effStatus(r) || effStatus(local); }
  renderDrawer(r, history);
}

function renderDrawer(r, history) {
  const body = $("#drawerBody");
  body.replaceChildren();
  const dfix = effFix(r);
  // word line — letter-diffed like everywhere else
  body.append(el("div", { class: "d-word-line" },
    wordDiffNode(r.word || "—", dfix || "", "err", "w-err"),
    el("span", { class: "arr" }, "⇐"),
    dfix ? wordDiffNode(dfix, r.word || "", "fix", "w-fix") : el("bdi", { class: "w-fix" }, "—")));
  // the swap shown in place, then the two full readings for careful checking
  const sec = el("div", { class: "d-section" }, el("h4", null, "קטע מהטקסט — התיקון במקומו"));
  const dfocus = renderFocusLine(r);
  dfocus.dataset.flMax = "19"; // the drawer is narrow
  sec.append(dfocus);
  requestAnimationFrame(() => fitFocusLine(dfocus));
  sec.append(el("div", { class: "d-snippet", style: "margin-top:8px" }, renderSnippet(r.snippet, r.word, null, dfix)));
  if (dfix) sec.append(el("div", { class: "d-snippet", style: "margin-top:6px;border-color:var(--green)" }, renderSnippet(r.snippet, r.word, dfix)));
  body.append(sec);
  // status buttons
  const stSec = el("div", { class: "d-section" }, el("h4", null, "סטטוס"));
  const acts = el("div", { class: "d-actions" });
  for (const s of statuses()) {
    const cur = effStatus(r) === s.key;
    const b = el("button", { class: cur ? "btn primary" : "btn" }, s.icon + " " + s.hebrew);
    b.addEventListener("click", async () => {
      try {
        await setStatus([r.id], s.key);
        r.effective_status = s.key;
        renderDrawer(r, history);
      } catch (e) {}
    });
    acts.append(b);
  }
  stSec.append(acts);
  body.append(stSec);
  // custom correction
  const fixSec = el("div", { class: "d-section" }, el("h4", null, "תיקון ידני (גובר על ההצעה)"));
  const fixIn = el("input", { type: "text", value: r.custom_suggestion || "", placeholder: "הקלד תיקון משלך…" });
  const fixBtn = el("button", { class: "btn primary", style: "margin-top:6px" }, "✅ שמור תיקון ואשר");
  fixBtn.addEventListener("click", async () => {
    const v = fixIn.value.trim();
    if (!v) { toast("יש להקליד תיקון תחילה", "err"); return; }
    try {
      await setStatus([r.id], "approved", { custom_suggestion: v });
      r.custom_suggestion = v; r.effective_status = "approved";
      toast("התיקון נשמר והממצא אושר", "ok");
      renderDrawer(r, history);
    } catch (e) {}
  });
  fixSec.append(fixIn, fixBtn);
  body.append(fixSec);
  // note
  const noteSec = el("div", { class: "d-section" }, el("h4", null, "הערה"));
  const noteIn = el("textarea", { rows: "2", placeholder: "הערה חופשית…" });
  noteIn.value = r.note || "";
  const noteBtn = el("button", { class: "btn", style: "margin-top:6px" }, "💾 שמירת הערה");
  noteBtn.addEventListener("click", async () => {
    try {
      await setStatus([r.id], effStatus(r), { note: noteIn.value });
      toast("ההערה נשמרה", "ok");
    } catch (e) {}
  });
  noteSec.append(noteIn, noteBtn);
  body.append(noteSec);
  // fields
  const fSec = el("div", { class: "d-section" }, el("h4", null, "כל הפרטים"));
  const dl = el("dl", { class: "d-fields" });
  const addField = (key, valNode) => {
    if (valNode == null || valNode === "") return;
    const info = colInfo(key);
    const dt = el("dt", { title: info.explanation || "" }, info.hebrew);
    dl.append(dt, el("dd", null, valNode));
  };
  addField("errtype", errtypeInfo(r.errtype).hebrew);
  addField("origin", originInfo(r.origin).hebrew || r.origin);
  addField("source", el("bdi", null, r.source || ""));
  addField("ref", el("bdi", null, r.ref || ""));
  const pfu = parseFileUnit(r.unit);
  if (pfu) addField("unit", fileUnitNode(pfu));
  else addField("unit", r.unit != null ? String(r.unit) : "");
  addField("rank", fmtRank(r.rank));
  addField("score", fmtRank(r.score));
  addField("ctx_hits", r.ctx_hits != null ? String(r.ctx_hits) : "");
  addField("sugg_local", r.sugg_local != null ? String(r.sugg_local) : "");
  addField("book_repeat", r.book_repeat != null ? String(r.book_repeat) : "");
  addField("verified", r.verified ? "כן ✓" : "לא");
  // family-specific extra JSON
  let extra = r.extra;
  if (typeof extra === "string" && extra) { try { extra = JSON.parse(extra); } catch (e) { extra = null; } }
  if (extra && typeof extra === "object") {
    for (const [k, v] of Object.entries(extra)) {
      dl.append(el("dt", null, "פרטים: " + k), el("dd", null, el("bdi", null, String(v))));
    }
  }
  fSec.append(dl);
  body.append(fSec);
  // history
  const hSec = el("div", { class: "d-section d-history" }, el("h4", null, "היסטוריית הממצא"));
  if (!history.length) hSec.append(el("div", { class: "h-item" }, "אין פעולות קודמות על ממצא זה"));
  for (const h of history) {
    const from = h.old_status ? statusInfo(h.old_status).hebrew : "—";
    const to = h.new_status ? statusInfo(h.new_status).hebrew : "—";
    hSec.append(el("div", { class: "h-item" },
      el("time", null, h.ts || ""), " · ", from + " ← " + to, h.note ? " · " + h.note : ""));
  }
  body.append(hSec);
}

function closeDrawer() {
  $("#drawer").classList.remove("visible");
  $("#drawerScrim").classList.remove("visible");
}

/* ---------------------------------------------------------- card view */
async function ensureCardQueue() {
  if (S.cardLoading || S.cardExhausted) return;
  if (S.cardQueue.length >= 10) return;
  S.cardLoading = true;
  try {
    const resp = await api("/api/findings?" + filterParams({ page: S.cardPage, page_size: 50 }));
    const rows = rowsOf(resp);
    let added = 0;
    for (const r of rows) {
      if (!S.cardSeen.has(r.id)) { S.cardSeen.add(r.id); S.cardQueue.push(r); added++; }
    }
    S.cardPage++;
    if (!rows.length || (S.cardPage - 1) * 50 >= totalOf(resp)) S.cardExhausted = true;
    if (!added && rows.length) {
      // page contained only seen rows — advance further
      S.cardLoading = false;
      return ensureCardQueue();
    }
  } catch (e) {
    toast("טעינת הכרטיסים נכשלה: " + e.message, "err");
    S.cardExhausted = true;
  }
  S.cardLoading = false;
  renderCard();
}

function resetCardQueue() {
  S.cardQueue = [];
  S.cardSeen = new Set();
  S.cardPage = 1;
  S.cardExhausted = false;
  S.cardStale = false;
  S.cardFixOpen = false;
  renderCard();
  ensureCardQueue();
}

function currentCard() { return S.cardQueue[0] || null; }

function renderCard() {
  const box = $("#cardBox");
  const meta = $("#cardMeta");
  const r = currentCard();
  meta.replaceChildren(
    el("span", null, "בתור: " + fmtNum(S.cardQueue.length) + (S.cardExhausted ? "" : "+")),
    el("span", null, "קיצורים: י=אושר · נ=לא שגיאה · ד=לא בכל מקום · ת=תיקון · ע=התעלם · ב=בירור · ק=תוקן · רווח=דלג"));
  box.replaceChildren();
  if (!r) {
    box.append(el("div", { class: "card-done" },
      el("div", { class: "big" }, S.cardLoading ? "⏳" : "🎉"),
      S.cardLoading ? "טוען ממצאים…" : "אין עוד ממצאים בסינון הנוכחי — כל הכבוד!"));
    return;
  }
  const info = errtypeInfo(r.errtype);
  const fix = effFix(r);

  // 1. the reading line — sentence intact, swap stacked in place at the error
  const focus = renderFocusLine(r);
  box.append(focus);
  // must run after the node is in the document to measure it
  requestAnimationFrame(() => fitFocusLine(focus));

  // 2. the isolated pair, letter-diffed, for when the stack alone isn't enough
  const pair = el("div", { class: "card-pair" },
    wordDiffNode(r.word || "—", fix || "", "err", "w-err"),
    el("span", { class: "arr" }, "⇦"),
    fix ? wordDiffNode(fix, r.word || "", "fix", "w-fix") : el("bdi", { class: "w-fix" }, "—"));
  const cp = el("button", { class: "pair-copy", title: "העתקת התיקון" }, "⧉");
  cp.addEventListener("click", () => copyText(fix || r.word || ""));
  pair.append(cp);
  box.append(pair);

  // 3. provenance / confidence, secondary
  box.append(el("div", { class: "card-sub" },
    el("span", { class: "et-tag", title: info.explanation || "" }, info.hebrew),
    el("span", null, "📖 ", el("bdi", null, r.source || "")),
    el("span", null, "📍 ", el("bdi", null, r.ref || "")),
    el("span", null, "ציון: " + fmtRank(r.rank)),
    r.verified ? el("span", { class: "vbadge" }, "מאומת") : null,
    el("span", { class: "chip st-" + effStatus(r) }, statusInfo(effStatus(r)).icon + " " + statusInfo(effStatus(r)).hebrew)));
  /* Decisions — same eight actions and same keys as always, but ranked by how
   * often they are actually used. In triage ~90% of cards end in one of the
   * first two, so those get large, colour-coded primary buttons; the rest stay
   * one click away on a quieter secondary row. Nothing is hidden. */
  const mkBtn = (label, kbd, fn, cls) => {
    const b = el("button", { class: cls || null },
      el("span", { class: "lbl" }, label), el("kbd", null, kbd));
    b.addEventListener("click", fn);
    return b;
  };
  box.append(el("div", { class: "card-actions primary-row" },
    mkBtn("✅ אושר — זו שגיאה", "י", () => cardAct("approved"), "act-approve"),
    mkBtn("❌ לא שגיאה", "נ", () => cardAct("not_error"), "act-reject"),
    mkBtn("⏭ דלג", "רווח", () => cardSkip(), "act-skip")));
  box.append(el("div", { class: "card-actions second-row" },
    mkBtn("❌ לא שגיאה בכל מקום", "ד", () => cardAct("not_error", { scope: "word" })),
    mkBtn("✏ תיקון ידני", "ת", () => toggleCardFix(true)),
    mkBtn("🚫 התעלם", "ע", () => cardAct("ignored")),
    mkBtn("❓ דרוש בירור", "ב", () => cardAct("unsure")),
    mkBtn("🔧 תוקן בספר", "ק", () => cardAct("fixed"))));
  const fixRow = el("div", { id: "cardFixRow", class: S.cardFixOpen ? "visible" : "" },
    el("input", { id: "cardFixInput", type: "text", placeholder: "הקלד את התיקון הנכון ולחץ Enter…", dir: "rtl" }),
    el("button", { class: "btn primary" }, "שמור ואשר"));
  fixRow.querySelector("button").addEventListener("click", submitCardFix);
  fixRow.querySelector("input").addEventListener("keydown", ev => {
    if (ev.key === "Enter") { ev.preventDefault(); submitCardFix(); }
    if (ev.key === "Escape") { ev.preventDefault(); toggleCardFix(false); }
    ev.stopPropagation();
  });
  box.append(fixRow);
  if (S.cardFixOpen) setTimeout(() => { const i = $("#cardFixInput"); if (i) { i.value = effFix(r); i.focus(); i.select(); } }, 0);
}

function toggleCardFix(open) {
  S.cardFixOpen = open;
  const row = $("#cardFixRow");
  if (row) row.classList.toggle("visible", open);
  if (open) { const i = $("#cardFixInput"); if (i) { i.focus(); i.select(); } }
}
async function submitCardFix() {
  const r = currentCard();
  const i = $("#cardFixInput");
  if (!r || !i) return;
  const v = i.value.trim();
  if (!v) { toast("יש להקליד תיקון תחילה", "err"); return; }
  S.cardFixOpen = false;
  await cardAct("approved", { custom_suggestion: v });
}
async function cardAct(status, opts) {
  const r = currentCard();
  if (!r) return;
  try {
    await setStatus([r.id], status, opts || {});
    S.cardQueue.shift();
    S.cardFixOpen = false;
    renderCard();
    ensureCardQueue();
  } catch (e) { /* stays on card; toast shown */ }
}
function cardSkip() {
  if (!currentCard()) return;
  S.cardQueue.push(S.cardQueue.shift());
  S.cardFixOpen = false;
  renderCard();
  ensureCardQueue();
}

/* ---------------------------------------------------------- fixer view */
async function loadFixerBooks() {
  const sel = $("#fixBook");
  const keep = S.fixBook;
  sel.replaceChildren(el("option", { value: "" }, "בחר ספר לתיקון…"));
  let entries = [];
  try {
    const p = new URLSearchParams();
    if (S.filters.origin) p.set("origin", S.filters.origin);
    const resp = await api("/api/fixlist" + (p.toString() ? "?" + p : ""));
    const books = resp.books || resp.rows || [];
    for (const b of books) {
      const remaining = b.remaining != null ? b.remaining : b.count;
      if (remaining > 0) entries.push({ name: b.source || b.name, remaining });
    }
  } catch (e) { /* fall through to /api/books */ }
  if (!entries.length) {
    try {
      const resp = await api("/api/books" + (S.filters.origin ? "?origin=" + encodeURIComponent(S.filters.origin) : ""));
      const books = Array.isArray(resp) ? resp : (resp.books || []);
      entries = books
        .filter(b => (b.approved_count != null ? b.approved_count : 1) > 0)
        .map(b => ({ name: b.name || b.source, remaining: b.approved_count }));
    } catch (e) {
      toast("טעינת רשימת הספרים נכשלה: " + e.message, "err");
    }
  }
  entries.sort((a, b) => (b.remaining || 0) - (a.remaining || 0));
  for (const en of entries) {
    sel.append(el("option", { value: en.name }, en.name + (en.remaining != null ? " — נותרו " + fmtNum(en.remaining) : "")));
  }
  if (keep) sel.value = keep;
}

async function loadFixlist() {
  const box = $("#fixList");
  if (!S.fixBook) {
    box.replaceChildren(el("div", { class: "fixer-empty" }, "בחר ספר מהרשימה — מוצגים רק ספרים שיש בהם ממצאים מאושרים שטרם תוקנו."));
    updateFixProgress();
    return;
  }
  box.replaceChildren(el("div", { class: "fixer-empty" }, "טוען רשימת עבודה…"));
  const sts = S.fixInclude ? "approved,unsure,pending" : "approved";
  const p = new URLSearchParams({ book: S.fixBook, statuses: sts });
  if (S.filters.origin) p.set("origin", S.filters.origin);
  try {
    const [resp, fixedR] = await Promise.all([
      api("/api/fixlist?" + p),
      api("/api/findings?" + new URLSearchParams({ book: S.fixBook, status: "fixed", page: "1", page_size: "1", sort: "rank", dir: "desc" })),
    ]);
    S.fixRows = rowsOf(resp);
    S.fixAlready = totalOf(fixedR);
    S.fixSession = 0;
    S.fixIdx = 0;
    renderFixList(true);
  } catch (e) {
    box.replaceChildren(el("div", { class: "fixer-empty" }, "הטעינה נכשלה: " + e.message));
    toast(e.message, "err");
  }
}

function renderFixList(scroll) {
  const box = $("#fixList");
  box.replaceChildren();
  if (!S.fixRows.length) {
    box.append(el("div", { class: "fixer-empty" }, "אין ממצאים לתיקון בספר זה 🎉"));
    updateFixProgress();
    return;
  }
  let lastRef = null;
  S.fixRows.forEach((r, idx) => {
    const ref = r.ref || "(ללא מראה מקום)";
    if (ref !== lastRef) {
      box.append(el("div", { class: "fix-refgroup" }, el("bdi", null, ref)));
      lastRef = ref;
    }
    const done = effStatus(r) === "fixed";
    const row = el("div", { class: "fix-row" + (idx === S.fixIdx ? " current" : "") + (done ? " done-row" : ""), dataset: { idx: String(idx) } });
    const main = el("div", { class: "fix-main" });
    const rfix = effFix(r);
    main.append(el("div", { class: "fix-wordline" },
      wordDiffNode(r.word || "", rfix || "", "err", "w-err"),
      el("span", { class: "arr" }, "⇐"),
      rfix ? wordDiffNode(rfix, r.word || "", "fix", "w-fix") : el("bdi", { class: "w-fix" }, "—"),
      r.custom_suggestion ? el("span", { class: "et-tag", style: "margin-inline-start:8px" }, "תיקון ידני") : null));
    main.append(el("div", { class: "fix-snip" }, renderSnippet(r.snippet, r.word, null, rfix)));
    const pf = parseFileUnit(r.unit);
    if (pf) main.append(el("div", { class: "fix-file" }, fileUnitNode(pf)));
    row.append(main);
    const side = el("div", { class: "fix-side" });
    const cp = el("div", { class: "copybtns" });
    const b1 = el("button", { title: "העתקת המילה השגויה" }, "📋 מילה");
    b1.addEventListener("click", () => copyText(r.word || ""));
    const b2 = el("button", { title: "העתקת התיקון" }, "📋 תיקון");
    b2.addEventListener("click", () => copyText(effFix(r)));
    cp.append(b1, b2);
    side.append(cp);
    const fx = el("button", { class: "btn-fixed" }, done ? "✓ תוקן" : "תוקן ✓");
    fx.disabled = done;
    fx.addEventListener("click", () => markFixed(idx));
    side.append(fx);
    row.append(side);
    row.addEventListener("click", ev => {
      if (ev.target.tagName === "BUTTON") return;
      S.fixIdx = idx;
      renderFixList(false);
    });
    box.append(row);
  });
  updateFixProgress();
  if (scroll) scrollFixCurrent();
}

function scrollFixCurrent() {
  const cur = $("#fixList .fix-row.current");
  if (cur) cur.scrollIntoView({ block: "center", behavior: "smooth" });
}

async function markFixed(idx) {
  const r = S.fixRows[idx];
  if (!r || effStatus(r) === "fixed") return;
  try {
    await setStatus([r.id], "fixed");
    S.fixSession++;
    // advance to next unfixed
    let next = idx + 1;
    while (next < S.fixRows.length && effStatus(S.fixRows[next]) === "fixed") next++;
    if (next >= S.fixRows.length) {
      next = S.fixRows.findIndex(x => effStatus(x) !== "fixed");
      if (next < 0) next = idx;
    }
    S.fixIdx = next;
    renderFixList(true);
  } catch (e) { /* toast shown */ }
}

function updateFixProgress() {
  const total = S.fixRows.length + S.fixAlready;
  const done = S.fixAlready + S.fixRows.filter(r => effStatus(r) === "fixed").length;
  const pct = total ? Math.round(done * 100 / total) : 0;
  $("#fixProgressText").textContent = S.fixBook
    ? "תוקנו " + fmtNum(done) + " מתוך " + fmtNum(total) + " בספר זה (" + pct + "%)"
    : "";
  $("#fixProgressBar").style.width = pct + "%";
}

/* ---------------------------------------------------------- stats view */
const KNOWN_STATUS_KEYS = ["pending", "approved", "fixed", "not_error", "unsure", "ignored"];

function normalizeMatrix(data) {
  /* Accepts: [{origin/errtype/book/name/label, statuses:{k:n}}] | [{...flat status keys...}]
     | [{label, status, count}] triplets | {label:{status:count}}. Returns [{label, counts}]. */
  const out = new Map();
  const get = label => {
    if (!out.has(label)) out.set(label, { label, counts: {} });
    return out.get(label);
  };
  if (Array.isArray(data)) {
    for (const item of data) {
      if (item == null) continue;
      const label = item.hebrew || item.origin || item.errtype || item.book || item.source || item.name || item.label || item.key || "";
      const e = get(String(label));
      if (item.statuses && typeof item.statuses === "object") {
        Object.assign(e.counts, item.statuses);
      } else if (item.status != null && item.count != null) {
        e.counts[item.status] = (e.counts[item.status] || 0) + item.count;
      } else {
        for (const k of KNOWN_STATUS_KEYS) if (typeof item[k] === "number") e.counts[k] = item[k];
        if (typeof item.total === "number") e.total = item.total;
      }
      if (typeof item.total === "number") e.total = item.total;
    }
  } else if (data && typeof data === "object") {
    for (const [label, v] of Object.entries(data)) {
      const e = get(label);
      if (v && typeof v === "object") Object.assign(e.counts, v);
    }
  }
  return [...out.values()];
}

function matrixTable(entries, firstColTitle, labelFn) {
  const stList = statuses();
  const table = el("table", { class: "stats" });
  const hr = el("tr", null, el("th", null, firstColTitle));
  for (const s of stList) hr.append(el("th", { title: s.hebrew }, s.icon + " " + s.hebrew));
  hr.append(el("th", { class: "rowtotal" }, "סה״כ"));
  table.append(el("thead", null, hr));
  const tb = el("tbody");
  for (const e of entries) {
    const tr = el("tr", null, el("td", null, el("bdi", null, labelFn ? labelFn(e.label) : e.label)));
    let sum = 0;
    for (const s of stList) {
      const n = e.counts[s.key] || 0;
      sum += n;
      tr.append(el("td", { class: "num" + (n ? "" : " zero") }, n ? fmtNum(n) : "·"));
    }
    tr.append(el("td", { class: "num rowtotal" }, fmtNum(e.total != null ? e.total : sum)));
    tb.append(tr);
  }
  table.append(tb);
  return el("div", { class: "stats-table-wrap" }, table);
}

async function loadStats() {
  const body = $("#statsBody");
  body.replaceChildren(el("div", null, "טוען…"));
  let st;
  try {
    st = await api("/api/stats");
  } catch (e) {
    body.replaceChildren(el("div", null, "טעינת הסטטיסטיקה נכשלה: " + e.message));
    toast(e.message, "err");
    return;
  }
  body.replaceChildren();
  const origins = normalizeMatrix(st.origins || st.by_origin || st.origin_status || []);
  const errts = normalizeMatrix(st.errtypes || st.by_errtype || st.errtype_status || []);
  const books = normalizeMatrix(st.books || st.by_book || st.per_book || []);
  if (origins.length) {
    body.append(el("h3", null, "לפי מאגר"));
    body.append(matrixTable(origins, colInfo("origin").hebrew, l => originInfo(l).hebrew || l));
  }
  if (errts.length) {
    body.append(el("h3", null, "לפי סוג שגיאה"));
    body.append(matrixTable(errts, colInfo("errtype").hebrew, l => errtypeInfo(l).hebrew || l));
  }
  if (books.length) {
    body.append(el("h3", null, "התקדמות לפי ספר"));
    const bars = el("div", { class: "bookbars" });
    for (const b of books) {
      const total = b.total != null ? b.total : Object.values(b.counts).reduce((a, n) => a + n, 0);
      const pending = b.counts.pending || 0;
      const done = Math.max(0, total - pending);
      const pct = total ? Math.round(done * 100 / total) : 0;
      bars.append(el("div", { class: "bookbar" },
        el("span", { class: "bb-name", title: b.label }, el("bdi", null, b.label)),
        el("div", { class: "pbar" }, el("div", { style: "width:" + pct + "%" })),
        el("span", { class: "bb-nums" }, fmtNum(done) + " / " + fmtNum(total) + " (" + pct + "%)")));
    }
    body.append(bars);
  }
  if (!origins.length && !errts.length && !books.length) {
    body.append(el("div", null, "אין נתוני סטטיסטיקה להצגה."));
  }
}

/* ---------------------------------------------------------- help view */
function renderHelp() {
  const body = $("#helpBody");
  body.replaceChildren();
  const m = S.meta || {};
  // workflow (hardcoded UI chrome)
  body.append(el("div", { class: "help-block" },
    el("h3", null, "🚀 סדר עבודה מומלץ"),
    el("ol", null,
      el("li", null, "התחילו עם המסנן «מאומתים בלבד» — אלו הממצאים בעלי הוודאות הגבוהה ביותר."),
      el("li", null, "מיינו לפי ציון (מהגבוה לנמוך) — ממצאים עם ציון גבוה הם כמעט תמיד שגיאות אמיתיות."),
      el("li", null, "עבדו במצב כרטיסים עם קיצורי המקלדת לסקירה מהירה, או בטבלה לפעולות מרוכזות."),
      el("li", null, "לאחר אישור השגיאות — עברו ל«מצב מתקן» כדי לתקן ספר־ספר, לפי סדר הופעה בספר."),
      el("li", null, "בסיום — ייצאו ל־Excel (למסירה) או ל־to_send (לצינור העבודה הישן)."))));
  // shortcuts
  const kbd = (k) => el("kbd", null, k);
  const shTable = el("table", null,
    el("tr", null, el("th", null, "מקש"), el("th", null, "פעולה"), el("th", null, "תצוגה")),
    el("tr", null, el("td", null, kbd("י")), el("td", null, "אושר — זו שגיאה"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("נ")), el("td", null, "לא שגיאה (מופע זה בלבד)"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("ד")), el("td", null, "לא שגיאה — בכל מקום שבו מופיעה המילה"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("ת")), el("td", null, "הקלדת תיקון ידני (Enter לשמירה)"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("ע")), el("td", null, "התעלם"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("ב")), el("td", null, "דרוש בירור"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("ק")), el("td", null, "תוקן בספר"), el("td", null, "כרטיסים / מצב מתקן")),
    el("tr", null, el("td", null, kbd("רווח")), el("td", null, "דילוג לממצא הבא (ללא החלטה)"), el("td", null, "כרטיסים")),
    el("tr", null, el("td", null, kbd("Enter")), el("td", null, "סימון «תוקן» ומעבר לבא"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("Ctrl+Z")), el("td", null, "ביטול הפעולה האחרונה"), el("td", null, "בכל מקום")),
    el("tr", null, el("td", null, kbd("Esc")), el("td", null, "סגירת חלונית / תפריט"), el("td", null, "בכל מקום")));
  body.append(el("div", { class: "help-block" }, el("h3", null, "⌨ קיצורי מקלדת"), shTable));
  // statuses (labels from API; usage guidance is chrome)
  const stGuide = {
    pending: "מצב ההתחלה של כל ממצא — טרם התקבלה החלטה.",
    approved: "אישרתם שזו שגיאת דפוס אמיתית. ייכלל בייצוא התיקונים.",
    fixed: "השגיאה כבר תוקנה בפועל בספר עצמו. נכלל בייצוא ומסומן בעמודה נפרדת.",
    not_error: "המילה תקינה — אינה שגיאה. משמש גם ללימוד הסורק (רשימה לבנה).",
    unsure: "דרושה בדיקה נוספת — למשל התייעצות או השוואה למקור.",
    ignored: "לא רלוונטי לטיפול — לא שגיאה ולא נדרש בירור.",
  };
  const stTable = el("table", null, el("tr", null, el("th", null, "סטטוס"), el("th", null, "מתי להשתמש")));
  for (const s of statuses()) {
    stTable.append(el("tr", null, el("td", null, s.icon + " " + s.hebrew), el("td", null, stGuide[s.key] || "")));
  }
  body.append(el("div", { class: "help-block" }, el("h3", null, "🏷 הסטטוסים"), stTable));
  // error types from meta
  const etTable = el("table", null, el("tr", null, el("th", null, "סוג שגיאה"), el("th", null, "הסבר")));
  for (const e of (m.errtypes || [])) {
    etTable.append(el("tr", null, el("td", null, e.hebrew || e.key), el("td", null, e.explanation || "")));
  }
  body.append(el("div", { class: "help-block" }, el("h3", null, "🔤 סוגי השגיאות"), etTable));
  // columns from meta
  const colTable = el("table", null, el("tr", null, el("th", null, "עמודה"), el("th", null, "הסבר")));
  for (const c of (m.columns || [])) {
    colTable.append(el("tr", null, el("td", null, c.hebrew || c.key), el("td", null, c.explanation || "")));
  }
  body.append(el("div", { class: "help-block" }, el("h3", null, "📊 העמודות"), colTable));
  // exports (hardcoded chrome)
  body.append(el("div", { class: "help-block" },
    el("h3", null, "⬇ מה מייצא כל כפתור?"),
    el("ul", null,
      el("li", null, el("b", null, "Excel — למאגר הנוכחי / כל המאגרים: "),
        "חוברת Excel אחת לכל מאגר (שגיאות_<שם המאגר>.xlsx) עם גיליון סיכום, גיליון «כל השגיאות — לפי ספר», וגיליון נפרד לכל סוג שגיאה. כל הגיליונות מימין לשמאל עם כותרות בעברית."),
      el("li", null, el("b", null, "ייצוא תיקונים ל־to_send: "),
        "קבצי CSV בפורמט הישן (approved_fixes_all.csv + קובץ לכל מאגר) הכוללים את כל הממצאים שאושרו או תוקנו — תיקון ידני גובר על ההצעה. בנוסף rejected_words.txt עם המילים שסומנו «לא שגיאה»."))));
  // scan lifecycle (chrome)
  body.append(el("div", { class: "help-block" },
    el("h3", null, "⚙ ניהול סריקה"),
    el("ul", null,
      el("li", null, el("b", null, "רענן מסריקה חדשה: "), "לאחר הרצה מחודשת של הסורק — טוען את הממצאים העדכניים. החלטות על ממצאים שעדיין קיימים נשמרות."),
      el("li", null, el("b", null, "ייבוא החלטות ישנות: "), "ייבוא ההחלטות מהכלי הישן (decisions.db) — פעולה מפורשת בלבד, לא אוטומטית."),
      el("li", null, el("b", null, "אפס החלטות: "), "ניקוי כל הסטטוסים בממשק זה; גיבוי נשמר אוטומטית וניתן לשחזור."),
      el("li", null, el("b", null, "אפס הכל: "), "בנוסף מוחק את decisions.db כדי שסריקות הבאות לא יושפעו מהחלטות ניסיוניות."))));
}

/* ------------------------------------------------ file-based units (§9c) */
function parseFileUnit(unit) {
  if (typeof unit !== "string" || unit.indexOf("file:") !== 0) return null;
  const body = unit.slice(5);
  const i = body.lastIndexOf(":");
  if (i < 1) return null;
  const ln = body.slice(i + 1);
  if (!/^\d+$/.test(ln)) return null;
  return { rel: body.slice(0, i), lineno: parseInt(ln, 10) };
}

function fileUnitFullPath(rel) {
  const root = SCAN.cfg && SCAN.cfg.corpus && SCAN.cfg.corpus.library_dir;
  const winRel = rel.replace(/\//g, "\\");
  return root ? root.replace(/[\\\/]+$/, "") + "\\" + winRel : winRel;
}

async function copyFilePath(rel) {
  if (!SCAN.cfg) { try { await loadScanConfig(); } catch (e) {} }
  copyText(fileUnitFullPath(rel));
}

/* path + 1-based line number + copy button, for findings scanned from files */
function fileUnitNode(pf) {
  const btn = el("button", { class: "copy-path", title: "העתקת נתיב הקובץ המלא" }, "📋 נתיב");
  btn.addEventListener("click", ev => { ev.stopPropagation(); copyFilePath(pf.rel); });
  return el("span", { class: "file-unit" },
    el("bdi", { class: "file-path", title: pf.rel }, pf.rel),
    el("span", { class: "file-line" }, " · שורה " + fmtNum(pf.lineno + 1)),
    btn);
}

/* ---------------------------------------------------------- scan modal */
function openScanModal() {
  $("#scanModal").classList.add("visible");
  $("#scanScrim").classList.add("visible");
  loadBackups();
  loadScanConfig().catch(e => toast("טעינת הגדרות הסריקה נכשלה: " + e.message, "err"));
  syncScanStatusOnce();
}
function closeScanModal() {
  $("#scanModal").classList.remove("visible");
  $("#scanScrim").classList.remove("visible");
}

async function loadBackups() {
  const box = $("#backupsList");
  box.replaceChildren(el("div", null, "טוען…"));
  try {
    const resp = await api("/api/backups");
    const list = Array.isArray(resp) ? resp : (resp.backups || resp.files || []);
    box.replaceChildren();
    if (!list.length) { box.append(el("div", { style: "color:var(--faint)" }, "אין גיבויים עדיין")); return; }
    for (const item of list) {
      const file = typeof item === "string" ? item : (item.file || item.name || item.path || "");
      const extraTxt = typeof item === "object" && item.ts ? " · " + item.ts : "";
      const btn = el("button", { class: "btn" }, "↩ שחזר");
      btn.addEventListener("click", async () => {
        if (!confirm("לשחזר את הגיבוי?\n" + file + "\n\nההחלטות הנוכחיות יוחלפו בתוכן הגיבוי (התאמה לפי זהות הממצא).")) return;
        try {
          const r = await api("/api/restore", { method: "POST", body: { file } });
          toast(hebrewResult(r, "הגיבוי שוחזר בהצלחה"), "ok", 6000);
          afterDataChanged();
        } catch (e) { toast("השחזור נכשל: " + e.message, "err"); }
      });
      box.append(el("div", { class: "bk-item" }, el("code", null, file + extraTxt), btn));
    }
  } catch (e) {
    box.replaceChildren(el("div", null, "טעינת הגיבויים נכשלה: " + e.message));
  }
}

function hebrewResult(resp, fallback) {
  if (!resp) return fallback;
  if (resp.message) return resp.message;
  const parts = [];
  if (resp.added != null) parts.push("נוספו " + fmtNum(resp.added));
  if (resp.removed != null) parts.push("הוסרו " + fmtNum(resp.removed));
  if (resp.kept != null) parts.push("נשמרו " + fmtNum(resp.kept) + " החלטות");
  if (resp.migrated != null) parts.push("יובאו " + fmtNum(resp.migrated) + " החלטות");
  if (resp.restored != null) parts.push("שוחזרו " + fmtNum(resp.restored) + " החלטות");
  if (resp.backup) parts.push("גיבוי נשמר: " + resp.backup);
  return parts.length ? parts.join(", ") : fallback;
}

function bindScanModal() {
  $("#btnScan").addEventListener("click", openScanModal);
  bindScanRun();
  $("#scanClose").addEventListener("click", closeScanModal);
  $("#scanScrim").addEventListener("click", closeScanModal);
  $("#scanRefresh").addEventListener("click", async () => {
    if (!confirm("לרענן את הממצאים מהסריקה הנוכחית (report.db)?\n\nהחלטות על ממצאים שעדיין קיימים — יישמרו. ממצאים שנעלמו — יוסרו. ממצאים חדשים יתווספו כ«טרם נבדק».")) return;
    try {
      toast("מרענן מסריקה חדשה — נא להמתין…");
      const r = await api("/api/refresh", { method: "POST", body: {} });
      toast("הרענון הושלם: " + hebrewResult(r, "בוצע"), "ok", 7000);
      afterDataChanged();
    } catch (e) { toast("הרענון נכשל: " + e.message, "err"); }
  });
  $("#scanImportLegacy").addEventListener("click", async () => {
    if (!confirm("לייבא את ההחלטות מהכלי הישן (decisions.db)?\n\nהחלטות accept יהפכו ל«אושר», reject ל«לא שגיאה» (כולל חוקי «בכל מקום»), ignore ל«התעלם». ההחלטות ישויכו לממצאים לפי מילה ומזהה שורה.")) return;
    try {
      const r = await api("/api/import_legacy", { method: "POST", body: {} });
      toast("הייבוא הושלם: " + hebrewResult(r, "בוצע"), "ok", 7000);
      afterDataChanged();
    } catch (e) { toast("הייבוא נכשל: " + e.message, "err"); }
  });
  $("#scanResetStatuses").addEventListener("click", async () => {
    if (!confirm("לאפס את כל ההחלטות בממשק זה?\n\nיימחקו: כל הסטטוסים, ההערות, התיקונים הידניים, חוקי «בכל מקום» וההיסטוריה.\nגיבוי מלא יישמר אוטומטית לפני האיפוס.")) return;
    if (!confirm("אישור נוסף: האם אתם בטוחים? כל הממצאים יחזרו למצב «טרם נבדק».")) return;
    try {
      const r = await api("/api/reset", { method: "POST", body: { scope: "statuses" } });
      toast("האיפוס הושלם. " + (r && r.backup ? "גיבוי נשמר: " + r.backup : hebrewResult(r, "")), "ok", 8000);
      afterDataChanged();
    } catch (e) { toast("האיפוס נכשל: " + e.message, "err"); }
  });
  $("#scanResetAll").addEventListener("click", async () => {
    if (!confirm("אזהרה! לאפס הכל — כולל decisions.db?\n\nבנוסף לניקוי כל ההחלטות בממשק, יימחקו גם השורות ב־decisions.db, ובכך תבוטל השפעת ההחלטות הקודמות על סריקות detect עתידיות (הרשימה הלבנה).\nגיבוי יישמר לפני המחיקה.")) return;
    if (!confirm("אישור אחרון: פעולה זו משפיעה גם על צינור הסריקה הישן. להמשיך?")) return;
    try {
      const r = await api("/api/reset", { method: "POST", body: { scope: "all" } });
      toast("האיפוס המלא הושלם. " + (r && r.backup ? "גיבוי נשמר: " + r.backup : hebrewResult(r, "")), "ok", 8000);
      afterDataChanged();
    } catch (e) { toast("האיפוס נכשל: " + e.message, "err"); }
  });
}

/* ------------------------------------------------ §9d — run scan from UI */
const SCAN = { cfg: null, pollTimer: null, lastState: "idle" };

/* fallback Hebrew stage names — real ones come from /api/scan/config */
const STAGE_HEBREW = {
  lexicon: "בניית מילון",
  calibrate: "כיול",
  detect: "איתור",
  locate: "מיקום",
  report: "דוחות",
};

function fmtElapsed(secs) {
  const s = Math.max(0, Math.floor(Number(secs) || 0));
  const mm = Math.floor(s / 60), ss = s % 60;
  return String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}

async function loadScanConfig(force) {
  if (SCAN.cfg && !force) return SCAN.cfg;
  SCAN.cfg = await api("/api/scan/config");
  renderScanForm();
  return SCAN.cfg;
}

function scanMode() {
  const r = $("#scanModes input:checked");
  return r ? r.value : "hybrid";
}

function updateScanPathRows() {
  const m = scanMode();
  $("#scanLibRow").style.display = (m === "sqlite") ? "none" : "";
  $("#scanDbRow").style.display = (m === "library") ? "none" : "";
}

function renderScanForm() {
  const cfg = SCAN.cfg;
  if (!cfg) return;
  // corpus mode radios
  const modes = $("#scanModes");
  modes.replaceChildren();
  for (const m of (cfg.corpus_modes || [])) {
    const rb = el("input", { type: "radio", name: "scanMode", value: m.key });
    rb.checked = (cfg.corpus && cfg.corpus.mode) === m.key;
    rb.addEventListener("change", updateScanPathRows);
    modes.append(el("label", { class: "chk-row" }, rb,
      el("span", null, m.hebrew, el("span", { class: "mode-desc" }, m.explanation || ""))));
  }
  if (!$("#scanModes input:checked")) {
    const first = $("#scanModes input");
    if (first) first.checked = true;
  }
  $("#scanLibDir").value = (cfg.corpus && cfg.corpus.library_dir) || "";
  $("#scanDbPath").value = (cfg.corpus && cfg.corpus.db_path) || "";
  updateScanPathRows();
  // stages
  const stBox = $("#scanStages");
  stBox.replaceChildren();
  for (const s of (cfg.stages || [])) {
    const cb = el("input", { type: "checkbox", value: s.key });
    // calibrate needs a previous scan's report.db, so it is off by default
    cb.checked = s.default !== false;
    stBox.append(el("label", { class: "chk-row", title: s.explanation || "" }, cb, el("span", null, s.hebrew)));
  }
  // advanced config fields
  const fBox = $("#scanFields");
  fBox.replaceChildren();
  for (const f of (cfg.fields || [])) {
    const row = el("div", { class: "scan-field", dataset: { key: f.key, ftype: f.type } });
    row.append(el("span", { class: "sf-label" }, f.hebrew));
    if (f.type === "list") {
      const ta = el("textarea", { placeholder: "נתיב קובץ בכל שורה…" });
      ta.value = Array.isArray(f.value) ? f.value.join("\n") : "";
      row.append(el("span"), ta);
    } else {
      const inp = el("input", { type: "number", step: f.type === "float" ? "any" : "1" });
      inp.value = f.value != null ? String(f.value) : "";
      row.append(inp);
    }
    const defTxt = Array.isArray(f.default) ? (f.default.length ? f.default.join(", ") : "ללא") : String(f.default);
    row.append(el("span", { class: "sf-exp" }, (f.explanation || "") + " (ברירת מחדל: " + defTxt + ")"));
    fBox.append(row);
  }
}

function collectScanRequest() {
  const stages = $$("#scanStages input:checked").map(c => c.value);
  const config = {};
  for (const row of $$("#scanFields .scan-field")) {
    const key = row.dataset.key, type = row.dataset.ftype;
    if (type === "list") {
      const ta = row.querySelector("textarea");
      config[key] = ta.value.split("\n").map(s => s.trim()).filter(Boolean);
    } else {
      const v = row.querySelector("input").value.trim();
      if (v !== "") config[key] = type === "float" ? parseFloat(v) : parseInt(v, 10);
    }
  }
  return {
    stages,
    config,
    corpus: {
      mode: scanMode(),
      library_dir: $("#scanLibDir").value.trim(),
      db_path: $("#scanDbPath").value.trim(),
    },
  };
}

function stageHebrew(key) {
  const s = ((SCAN.cfg && SCAN.cfg.stages) || []).find(x => x.key === key);
  return s ? s.hebrew : (STAGE_HEBREW[key] || key);
}

function renderScanProgress(st) {
  const wrap = $("#scanProgress");
  const running = st.state === "running";
  const known = ["running", "done", "failed", "cancelled"].includes(st.state);
  // show the block whenever a scan is running or has produced a result
  wrap.hidden = !known;
  if (!known) return;

  const total = (st.total_stages != null ? st.total_stages : (st.stages || []).length) || 0;
  const idx = (st.stage_index != null && st.stage_index >= 0) ? st.stage_index : 0;
  const stageKey = st.stage || (st.stages || [])[idx] || "";

  // stage line: "שלב X מתוך Y: <name>"
  const stageLine = $("#scanStageLine");
  if (running && stageKey && total) {
    stageLine.textContent = "שלב " + fmtNum(idx + 1) + " מתוך " + fmtNum(total) + ": " + stageHebrew(stageKey);
  } else if (st.state === "done") {
    stageLine.textContent = "כל השלבים הושלמו ✓";
  } else if (st.state === "failed") {
    stageLine.textContent = "הסריקה נעצרה עקב שגיאה";
  } else if (st.state === "cancelled") {
    stageLine.textContent = "הסריקה בוטלה";
  } else {
    stageLine.textContent = "";
  }

  // percent (0/NaN -> 0), bar fill + color
  let pct = Number(st.percent);
  if (isNaN(pct) || pct < 0) pct = 0;
  if (st.state === "done") pct = 100;
  pct = Math.min(100, Math.round(pct * 10) / 10);
  const bar = $("#scanBar");
  bar.style.width = pct + "%";
  bar.classList.toggle("done", st.state === "done");
  bar.classList.toggle("err", st.state === "failed" || st.state === "cancelled");

  // chunk text (only when a chunk total is known)
  const cd = Number(st.chunk_done) || 0, ctot = Number(st.chunk_total) || 0;
  $("#scanChunkText").textContent = (running && ctot > 0)
    ? "נתח " + fmtNum(cd) + " מתוך " + fmtNum(ctot)
    : "";
  $("#scanPctText").textContent = pct + "%";
  $("#scanElapsed").textContent = "זמן שחלף: " + fmtElapsed(st.elapsed);

  // lexicon-is-longest note — only during the lexicon stage
  $("#scanLexiconNote").hidden = !(running && stageKey === "lexicon");
}

function renderScanStatus(st) {
  const panel = $("#scanStatusPanel");
  const line = $("#scanStateLine");
  const running = st.state === "running";
  panel.hidden = st.state === "idle" && !(st.log_tail || []).length;
  let txt = st.hebrew_state || st.state;
  if (st.started_at) txt += " · התחילה: " + st.started_at;
  line.textContent = txt;
  line.className = running ? "run" : (st.state === "done" ? "ok" : (st.state === "idle" ? "" : "err"));
  renderScanProgress(st);
  const log = $("#scanLog");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = (st.log_tail || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  $("#scanStart").hidden = running;
  $("#scanCancel").hidden = !running;
  $("#scanRefreshAfter").hidden = st.state !== "done";
  if (running) startScanPolling();
  else stopScanPolling();
  if (SCAN.lastState === "running" && !running) {
    if (st.state === "done") toast("הסריקה הושלמה — אפשר לרענן את הממצאים", "ok", 8000);
    else if (st.state === "failed") toast("הסריקה נכשלה — ראו את יומן הריצה", "err", 8000);
    else if (st.state === "cancelled") toast("הסריקה בוטלה", "", 5000);
  }
  SCAN.lastState = st.state;
}

async function syncScanStatusOnce() {
  try { renderScanStatus(await api("/api/scan/status")); } catch (e) {}
}

function startScanPolling() {
  if (SCAN.pollTimer) return;
  SCAN.pollTimer = setInterval(async () => {
    try { renderScanStatus(await api("/api/scan/status")); }
    catch (e) { /* keep polling; server may be busy */ }
  }, 2000);
}
function stopScanPolling() {
  if (SCAN.pollTimer) { clearInterval(SCAN.pollTimer); SCAN.pollTimer = null; }
}

function bindScanRun() {
  $("#scanStart").addEventListener("click", async () => {
    const req = collectScanRequest();
    if (!req.stages.length) { toast("יש לבחור לפחות שלב אחד להרצה", "err"); return; }
    if (!confirm("להתחיל סריקה חדשה?\n\nשלבים: " + req.stages.map(stageHebrew).join(", ") + "\nהסריקה עשויה להימשך זמן רב; אפשר לעקוב אחרי ההתקדמות ביומן.")) return;
    try {
      const r = await api("/api/scan/start", { method: "POST", body: req });
      toast((r && r.message) || "הסריקה הופעלה", "ok");
      $("#scanRunSection").setAttribute("open", "");
      renderScanStatus((r && r.status) || { state: "running", log_tail: [] });
    } catch (e) {
      toast("הפעלת הסריקה נכשלה: " + e.message, "err", 8000);
    }
  });
  $("#scanCancel").addEventListener("click", async () => {
    if (!confirm("לבטל את הסריקה הרצה?")) return;
    try {
      const r = await api("/api/scan/cancel", { method: "POST", body: {} });
      toast((r && r.message) || "בקשת הביטול נשלחה", "ok");
      syncScanStatusOnce();
    } catch (e) { toast("הביטול נכשל: " + e.message, "err"); }
  });
  $("#scanRefreshAfter").addEventListener("click", async () => {
    try {
      toast("מרענן ממצאים מהסריקה החדשה — נא להמתין…");
      const r = await api("/api/refresh", { method: "POST", body: {} });
      toast(hebrewResult(r, "הרענון הושלם"), "ok", 8000);
      $("#scanRefreshAfter").hidden = true;
      afterDataChanged();
    } catch (e) { toast("הרענון נכשל: " + e.message, "err", 8000); }
  });
}

async function afterDataChanged() {
  closeScanModal();
  S.lastActions = [];
  S.sel.clear();
  S.cardStale = true;
  try { S.meta = await api("/api/meta"); buildSidebar(); } catch (e) {}
  loadBooks();
  refreshCurrentView();
  updateProgress();
}

/* ---------------------------------------------------------- exports */
function bindExports() {
  $("#expXlsxCurrent").addEventListener("click", async () => {
    closeExportMenu();
    if (!S.filters.origin) { toast("בחרו מאגר בסינון תחילה, או השתמשו ב«כל המאגרים»", "err"); return; }
    await runExport("/api/export/xlsx", { origin: S.filters.origin }, "ייצוא Excel למאגר " + (originInfo(S.filters.origin).hebrew || S.filters.origin));
  });
  $("#expXlsxAll").addEventListener("click", async () => {
    closeExportMenu();
    await runExport("/api/export/xlsx", {}, "ייצוא Excel לכל המאגרים");
  });
  $("#expFixes").addEventListener("click", async () => {
    closeExportMenu();
    await runExport("/api/export/fixes", {}, "ייצוא תיקונים ל־to_send");
  });
}
function closeExportMenu() { $("#exportMenu").removeAttribute("open"); }

async function runExport(path, body, label) {
  toast(label + " — מתבצע, נא להמתין…");
  try {
    const r = await api(path, { method: "POST", body });
    let msg = label + " הושלם.";
    if (r) {
      if (Array.isArray(r.files) && r.files.length) {
        msg += " נכתבו " + fmtNum(r.files.length) + " קבצים.";
      } else if (Array.isArray(r.paths) && r.paths.length) {
        msg += " נכתבו " + fmtNum(r.paths.length) + " קבצים.";
      }
      if (r.rows != null) msg += " " + fmtNum(r.rows) + " שורות.";
      if (r.approved != null) msg += " " + fmtNum(r.approved) + " תיקונים מאושרים.";
      if (r.rejected != null) msg += " " + fmtNum(r.rejected) + " מילים דחויות.";
      if (r.message) msg = r.message;
    }
    toast(msg, "ok", 8000);
  } catch (e) {
    toast(label + " נכשל: " + e.message, "err", 8000);
  }
}

/* ---------------------------------------------------------- views */
function showView(v, skipHash) {
  S.view = v;
  $$("#viewtabs button[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $("#btnStats").classList.toggle("active", v === "stats");
  $("#btnHelp").classList.toggle("active", v === "help");
  $$("section.view").forEach(s => s.classList.toggle("visible", s.id === "view-" + v));
  if (!skipHash) writeHash();
  refreshCurrentView();
}

function refreshCurrentView() {
  switch (S.view) {
    case "table": loadTable(); break;
    case "cards": if (S.cardStale) resetCardQueue(); else { renderCard(); ensureCardQueue(); } break;
    case "fixer": loadFixerBooks(); loadFixlist(); break;
    case "stats": loadStats(); break;
    case "help": renderHelp(); break;
  }
}

/* ---------------------------------------------------------- keyboard */
const HEB_KEYS = {
  // Hebrew char / physical code → action
  approve: ["י", "KeyH"],
  reject: ["נ", "KeyB"],
  rejectAll: ["ד", "KeyS"],
  fixTyped: ["ת", "Comma"],
  ignore: ["ע", "KeyG"],
  unsure: ["ב", "KeyC"],
  fixedBook: ["ק", "KeyE"],
};
function keyIs(ev, action) {
  const [heb, code] = HEB_KEYS[action];
  return ev.key === heb || ev.code === code;
}

document.addEventListener("keydown", ev => {
  // Ctrl+Z anywhere
  if ((ev.ctrlKey || ev.metaKey) && (ev.code === "KeyZ" || ev.key === "z" || ev.key === "Z")) {
    ev.preventDefault();
    doUndo();
    return;
  }
  if (ev.key === "Escape") {
    closeDrawer(); closeStatusMenu(); closeScanModal(); closeExportMenu();
    if (S.cardFixOpen) toggleCardFix(false);
    return;
  }
  const t = ev.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  if ($("#scanModal").classList.contains("visible") || $("#drawer").classList.contains("visible")) return;

  if (S.view === "cards") {
    if (keyIs(ev, "approve")) { ev.preventDefault(); cardAct("approved"); }
    else if (keyIs(ev, "reject")) { ev.preventDefault(); cardAct("not_error"); }
    else if (keyIs(ev, "rejectAll")) { ev.preventDefault(); cardAct("not_error", { scope: "word" }); }
    else if (keyIs(ev, "fixTyped")) { ev.preventDefault(); toggleCardFix(true); }
    else if (keyIs(ev, "ignore")) { ev.preventDefault(); cardAct("ignored"); }
    else if (keyIs(ev, "unsure")) { ev.preventDefault(); cardAct("unsure"); }
    else if (keyIs(ev, "fixedBook")) { ev.preventDefault(); cardAct("fixed"); }
    else if (ev.code === "Space" || ev.key === " ") { ev.preventDefault(); cardSkip(); }
  } else if (S.view === "fixer") {
    if (ev.key === "Enter" || keyIs(ev, "fixedBook")) { ev.preventDefault(); markFixed(S.fixIdx); }
    else if (ev.code === "ArrowDown") { ev.preventDefault(); S.fixIdx = Math.min(S.fixRows.length - 1, S.fixIdx + 1); renderFixList(true); }
    else if (ev.code === "ArrowUp") { ev.preventDefault(); S.fixIdx = Math.max(0, S.fixIdx - 1); renderFixList(true); }
  }
});

/* ---------------------------------------------------------- tooltips */
document.addEventListener("mouseover", ev => {
  const t = ev.target.closest && ev.target.closest("[data-tip]");
  const tip = $("#tooltip");
  if (!t) { tip.style.display = "none"; return; }
  tip.textContent = t.dataset.tip;
  tip.style.display = "block";
  const r = t.getBoundingClientRect();
  const tw = Math.min(290, tip.offsetWidth);
  let x = r.left + r.width / 2 - tw / 2;
  x = Math.max(8, Math.min(window.innerWidth - tw - 8, x));
  let y = r.bottom + 7;
  if (y + tip.offsetHeight > window.innerHeight - 8) y = r.top - tip.offsetHeight - 7;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
});

/* ---------------------------------------------------------- theme */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#btnTheme").textContent = t === "dark" ? "☀" : "🌙";
  try { localStorage.setItem("magiah_theme", t); } catch (e) {}
}
function initTheme() {
  let t = null;
  try { t = localStorage.getItem("magiah_theme"); } catch (e) {}
  if (!t) t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  applyTheme(t);
  $("#btnTheme").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
}

/* ---------------------------------------------------------- init */
function bindControls() {
  $("#fOrigin").addEventListener("change", () => {
    S.filters.origin = $("#fOrigin").value;
    S.filters.book = "";
    loadBooks();
    filtersChanged();
  });
  $("#fBookSearch").addEventListener("input", loadBooks);
  $("#fVerified").addEventListener("change", () => { S.filters.verified = $("#fVerified").checked; filtersChanged(); });
  const mrSync = v => {
    S.filters.min_rank = parseFloat(v) || 0;
    $("#fMinRank").value = S.filters.min_rank;
    $("#fMinRankVal").value = S.filters.min_rank;
  };
  $("#fMinRank").addEventListener("input", () => { mrSync($("#fMinRank").value); });
  $("#fMinRank").addEventListener("change", () => { filtersChanged(); });
  $("#fMinRankVal").addEventListener("change", () => { mrSync($("#fMinRankVal").value); filtersChanged(); });
  $("#fSort").addEventListener("change", () => {
    const [s, d] = $("#fSort").value.split(":");
    S.filters.sort = s; S.filters.dir = d || "desc";
    filtersChanged();
  });
  $("#btnClearFilters").addEventListener("click", () => {
    S.filters = { origin: "", book: "", errtypes: [], statuses: [], verified: false, min_rank: 0, q: "", sort: "rank", dir: "desc" };
    $("#fBookSearch").value = "";
    syncFilterControls();
    loadBooks();
    filtersChanged();
  });
  $("#globalSearch").addEventListener("input", debounce(() => {
    S.filters.q = $("#globalSearch").value.trim();
    filtersChanged();
  }, 350));
  $("#btnUndo").addEventListener("click", doUndo);
  $$("#topbar [data-goview]").forEach(b => b.addEventListener("click", () => showView(b.dataset.goview)));
  $$("#viewtabs button[data-view]").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerScrim").addEventListener("click", closeDrawer);
  $("#bulkClear").addEventListener("click", () => { S.sel.clear(); renderTableRows(); updateBulkBar(); });
  $("#btnSidebar").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#fixBook").addEventListener("change", () => { S.fixBook = $("#fixBook").value; writeHash(); loadFixlist(); });
  $("#fixInclude").addEventListener("change", () => { S.fixInclude = $("#fixInclude").checked; writeHash(); loadFixlist(); });
  document.addEventListener("click", ev => {
    const em = $("#exportMenu");
    if (em.hasAttribute("open") && !em.contains(ev.target)) em.removeAttribute("open");
  });
  bindExports();
  bindScanModal();
}

async function init() {
  initTheme();
  readHash();
  bindControls();
  try {
    S.meta = await api("/api/meta");
  } catch (e) {
    toast("טעינת הנתונים מהשרת נכשלה: " + e.message, "err", 10000);
    S.meta = { origins: [], errtypes: [], statuses: FALLBACK_STATUSES, columns: [] };
  }
  buildSidebar();
  loadBooks();
  showView(S.view, true);
  updateProgress();
  updateSessionCounter();
  syncScanStatusOnce();   // resume live scan status if a scan is running
  if (S.meta && S.meta.no_scan) showNoScanScreen();
}

/* No findings yet: invite the user to run a scan instead of showing an
   empty table. Dismissing it leaves the normal (empty) UI behind. */
function showNoScanScreen() {
  if ($("#noScan")) return;
  const box = el("div", { id: "noScan", class: "no-scan" },
    el("div", { class: "no-scan-card" },
      el("h2", null, "עדיין אין סריקה בתיקייה הזו"),
      el("p", null,
        "הממשק מציג ממצאים מסריקה קיימת, ובתיקייה שנבחרה עדיין אין קובץ " +
        "report.db. אפשר להריץ סריקה עכשיו — בסיומה הממצאים ייטענו לממשק " +
        "ללא צורך בהפעלה מחדש."),
      el("div", { class: "no-scan-actions" },
        el("button", { class: "ns-btn ns-primary", onclick: () => {
          hideNoScanScreen(); openScanModal();
          const d = $("#scanRunSection"); if (d) d.open = true;
        } }, el("bdi", null, "▶"), " הרצת סריקה חדשה"),
        el("button", { class: "ns-btn", onclick: hideNoScanScreen },
          "סגירה"))));
  document.body.appendChild(box);
}
function hideNoScanScreen() {
  const b = $("#noScan"); if (b) b.remove();
}

init();
