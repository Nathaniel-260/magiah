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
    const err = new Error(msg);
    // the fixer refuses with a machine code and a per-finding failure list;
    // callers need both to explain WHY nothing was written
    err.status = res.status;
    err.code = data && data.code;
    err.data = data;
    throw err;
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
  // fixer — keyed on the FILE (fixKey), never on the book title: one title can
  // belong to many different files in the corpus
  fixKey: "",
  fixBooks: [],
  fixDoc: null,             // /api/fixer/doc response (lines + fingerprint)
  fixLines: new Map(),      // lineno -> {n, text, tokens?}
  fixInclude: false,
  fixRows: [],
  fixCurId: null,           // the selected finding, by id — survives filtering
  fixView: "",              // "" | blocked | st:<status> | et:<errtype>
  fixMode: "replace",       // per-book default
  fixModeOverride: new Map(),  // finding id -> mode (session only)
  fixPicked: new Set(),     // findings selected for the next apply
  fixEdits: [],             // file_edits log for this book
  fixDocStale: false,       // set when the server says the file changed
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
  if (S.fixKey) p.set("fk", S.fixKey);
  if (S.fixInclude) p.set("fi", "1");
  // written whenever a book is open, so that reloading a link cannot flip the
  // mode back to the book's stored default behind the corrector's back
  if (S.fixKey) p.set("fm", S.fixMode);
  if (S.fixView) p.set("fv", S.fixView);
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
  S.fixKey = p.get("fk") || "";
  S.fixInclude = p.get("fi") === "1";
  S.fixMode = p.get("fm") === "bracket" ? "bracket" : "replace";
  S.fixView = p.get("fv") || "";
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
  repaintStatuses(ids);
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
    repaintStatuses(ids);
    toast("שמירת הסטטוס נכשלה: " + e.message, "err");
    throw e;
  }
}

/* `ids`, when given, are the findings that just changed status. For the
 * fixer we patch just those rows/lines in place rather than rebuilding the
 * whole (possibly huge) worklist and document pane on every click — see
 * patchFixRowById / patchFixDocLineFor. A full rebuild only happens when the
 * current status filter means the change could have moved a row in/out of
 * view, or when no ids are given (bulk/unknown change). */
function repaintStatuses(ids) {
  if (S.view === "table") renderTableRows();
  if (S.view !== "fixer") return;
  const editable = !!(S.fixDoc && S.fixDoc.editable);
  if (ids && ids.length && canPatchFixRows()) {
    let ok = true;
    for (const id of ids) {
      if (!patchFixRowById(id, editable)) { ok = false; break; }
      patchFixDocLineFor(id);
    }
    if (ok) {
      syncFixViewSelect();   // keeps the per-status counts in the dropdown honest
      updateFixProgress();
      updateApplyButton();
      return;
    }
  }
  renderFixList(false);
  renderFixDoc();
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
    // The server flags an undo whose correction is already ON DISK. Undoing a
    // status does NOT rewrite the book, and a corrector who is not told that
    // will assume the file went back too — so say it loudly, and in the fixer
    // leave it on screen next to the button that WOULD revert the file.
    if (resp && resp.file_warning) {
      toast(resp.file_warning, "err", 12000);
      if (S.view === "fixer") setFixBanner(resp.file_warning, "err");
    }
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
  if ($("#fixBook")) $("#fixBook").value = S.fixKey;
  syncModeButtons();
  syncFixViewSelect();
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

// Each candidate correction with the evidence behind it: the detector's, and
// the reading of the aligned verse (works / independent sources / alignment).
function altList(word, alts) {
  const box = el("div", { class: "alt-list" });
  for (const a of alts) {
    if (!a || typeof a !== "object") continue;
    const by = a.by === "tanach" ? "נוסח המקרא" : "הגלאי";
    const parts = [];
    if (a.by === "tanach") {
      if (a.ref) parts.push(a.ref);
      if (a.works != null) parts.push("ספרים: " + a.works);
      if (a.independent_sources != null) parts.push("מקורות בלתי תלויים: " + a.independent_sources);
      if (a.occurrences != null) parts.push("מהדורות: " + a.occurrences);
      if (a.aligned_tokens != null) parts.push("מילים מיושרות: " + a.aligned_tokens);
    } else if (a.agrees_with_tanach) {
      parts.push("זהה לנוסח המקרא");
    }
    box.append(el("div", null,
      el("bdi", null, (word || "") + " ← " + (a.suggestion || "—")),
      " (" + by + ")", parts.length ? " · " + parts.join(" · ") : ""));
  }
  return box;
}

// Hebrew label of a Tanach evidence kind / reason code (from /api/meta);
// anything unknown — a user's own note included — is shown as-is.
function evLabel(code) {
  const m = (S.meta && S.meta.evidence_labels) || {};
  return (typeof code === "string" && m[code]) || code;
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
      if (k === "alternatives" && Array.isArray(v)) {
        dl.append(el("dt", null, "הצעות חלופיות"), el("dd", null, altList(r.word, v)));
        continue;
      }
      const txt = (v && typeof v === "object") ? JSON.stringify(v)
        : typeof v === "boolean" ? (v ? "כן" : "לא")
        : (k === "evidence_kind" || k === "reason") ? String(evLabel(v)) : String(v);
      const kl = ((S.meta && S.meta.extra_labels) || {})[k];
      dl.append(el("dt", null, kl || ("פרטים: " + k)), el("dd", null, el("bdi", null, txt)));
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
      el("time", null, h.ts || ""), " · ", from + " ← " + to, h.note ? " · " + evLabel(h.note) : ""));
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

/* ============================================================ fixer view
 * The corrector no longer leaves the tool to fix a book: the book's own .txt
 * is opened here, the findings are highlighted inside the real text, and the
 * approved corrections are written back into the file (after a backup).
 *
 * Every offset shown or sent comes from the SERVER. The browser never computes
 * where a word sits in the file — it can only echo back an anchor the server
 * already verified. That is what keeps a correction from ever landing on an
 * unrelated passage, which is the whole point of this screen.
 */
function fixBookLabel(b) {
  let s = b.title || b.key;
  if (b.folder) s += "  ·  " + b.folder;
  if (!b.editable) s += "  (מסד נתונים — ייצוא בלבד)";
  else if (!b.exists) s += "  (הקובץ חסר)";
  if (b.remaining != null) s += "  — נותרו " + fmtNum(b.remaining);
  return s;
}

async function loadFixerBooks() {
  try {
    const p = new URLSearchParams();
    if (S.filters.origin) p.set("origin", S.filters.origin);
    p.set("statuses", S.fixInclude ? "approved,unsure,pending" : "approved");
    const resp = await api("/api/fixer/books?" + p);
    S.fixBooks = resp.books || [];
  } catch (e) {
    toast("טעינת רשימת הספרים נכשלה: " + e.message, "err");
  }
  renderFixBookOptions();
}

/* The picker is filtered in the browser: the whole list is already here, and
 * a corrector hunting for one book among hundreds should not wait for a round
 * trip on every keystroke. The book currently open always stays listed, so
 * typing can never make the selection silently vanish. */
function renderFixBookOptions() {
  const sel = $("#fixBook");
  if (!sel) return;
  const q = ($("#fixBookSearch") ? $("#fixBookSearch").value : "").trim();
  const books = S.fixBooks || [];
  const hits = q
    ? books.filter(b => (b.title || "").indexOf(q) >= 0 ||
                        (b.folder || "").indexOf(q) >= 0)
    : books;
  // the open book stays listed even when it does not match, so typing can
  // never make the current selection disappear — but it is not counted as a
  // search result, which would be a lie
  const shown = hits.slice();
  if (q && S.fixKey && !shown.some(b => b.key === S.fixKey)) {
    const open = books.find(b => b.key === S.fixKey);
    if (open) shown.push(open);
  }
  sel.replaceChildren(el("option", { value: "" },
    q ? (hits.length ? "נמצאו " + fmtNum(hits.length) + " ספרים — בחר…"
                     : "לא נמצא ספר התואם לחיפוש")
      : "בחר ספר לתיקון…"));
  for (const b of shown) {
    sel.append(el("option", { value: b.key }, fixBookLabel(b)));
  }
  sel.value = S.fixKey || "";
}

function fixBookInfo(key) {
  return (S.fixBooks || []).find(b => b.key === (key || S.fixKey)) || null;
}

/* the write mode named in the URL, if any */
function hashMode() {
  const v = new URLSearchParams(location.hash.replace(/^#/, "")).get("fm");
  return v === "bracket" ? "bracket" : (v === "replace" ? "replace" : null);
}

async function loadFixDoc() {
  const list = $("#fixList");
  if (!S.fixKey) {
    S.fixDoc = null;
    S.fixRows = [];
    list.replaceChildren(el("div", { class: "fixer-empty" },
      "בחר ספר מהרשימה — הכלי יפתח את קובץ הטקסט עצמו ויסמן בו את המקומות לתיקון."));
    $("#fixDoc").replaceChildren();
    $("#fixDocPath").textContent = "";
    $("#fixDocMeta").textContent = "";
    setFixBanner(null);
    updateFixProgress();
    updateApplyButton();
    return;
  }
  list.replaceChildren(el("div", { class: "fixer-empty" }, "טוען את הספר…"));
  const p = new URLSearchParams({
    key: S.fixKey,
    statuses: S.fixInclude ? "approved,unsure,pending" : "approved",
  });
  if (S.filters.origin) p.set("origin", S.filters.origin);
  // resolving an ambiguous occurrence is manual, per-word work; a reload
  // rebuilds the rows from the server, so carry those choices across
  const manual = new Map((S.fixRows || [])
    .filter(r => r.explicit).map(r => [r.id, r.explicit]));
  try {
    const resp = await api("/api/fixer/doc?" + p);
    S.fixDoc = resp;
    S.fixRows = resp.items || [];
    for (const r of S.fixRows) {
      const e = manual.get(r.id);
      // only re-apply where the server still cannot place it itself; if it
      // now anchors on its own, its answer is the better one
      if (e && r.anchor && !r.anchor.ok) {
        const line = (resp.lines || []).find(l => l.n === r.lineno);
        const txt = line ? line.text : "";
        r.explicit = e;
        r.anchor = { ok: true, start: e.start, end: e.end,
                     confidence: "manual",
                     spans_markup: txt.slice(e.start, e.end).indexOf("<") >= 0 };
      }
    }
    S.fixLines = new Map((resp.lines || []).map(l => [l.n, l]));
    // an explicit #fm in the URL is a deliberate choice by whoever opened this
    // link, so it outranks the book's stored default
    S.fixMode = hashMode() || resp.default_mode || "replace";
    S.fixModeOverride = new Map();
    // Only APPROVED findings are pre-selected. A finding nobody has judged
    // yet must never be written into a book just because it was on screen —
    // the corrector opts in, per finding or with the quick-pick bar.
    S.fixPicked = new Set(S.fixRows
      .filter(r => canApply(r) && effStatus(r) === "approved")
      .map(r => r.id));
    S.fixDocStale = false;
    S.fixCurId = (S.fixRows[0] || {}).id ?? null;
    S.fixEdits = resp.edits || [];
    syncModeButtons();
    renderFixDoc();
    renderFixList(true);
    updateFixHead();
    if (!resp.editable) {
      setFixBanner(resp.message || "ספר זה אינו קובץ טקסט — אפשר לייצא את התיקונים בלבד.", "err");
    } else {
      showBlockedBanner();
    }
  } catch (e) {
    S.fixDoc = null;
    S.fixRows = [];
    list.replaceChildren(el("div", { class: "fixer-empty" }, "הטעינה נכשלה: " + e.message));
    setFixBanner(e.message, "err");
  }
  updateApplyButton();
}

/* ---- which findings the worklist shows -------------------------------
 * The order is ALWAYS the order of the book (by line number). A corrector
 * works a book front to back, and re-ordering the list would make them lose
 * their place — so "show me the pending ones" narrows the list instead of
 * shuffling it. Whatever is shown stays in reading order.
 */
function fixViewOptions() {
  const counts = new Map();
  for (const r of S.fixRows) {
    const st = effStatus(r);
    counts.set("st:" + st, (counts.get("st:" + st) || 0) + 1);
    const et = r.errtype || "";
    if (et) counts.set("et:" + et, (counts.get("et:" + et) || 0) + 1);
  }
  const opts = [{ key: "", label: "הכל", n: S.fixRows.length }];
  const blocked = S.fixRows.filter(r => r.anchor && !r.anchor.ok).length;
  if (blocked) {
    opts.push({ key: "blocked", label: "⚠ דורשים אישור ידני", n: blocked });
  }
  for (const s of statuses()) {
    const n = counts.get("st:" + s.key) || 0;
    if (n) opts.push({ key: "st:" + s.key, label: s.icon + " " + s.hebrew, n });
  }
  for (const [k, n] of counts) {
    if (!k.startsWith("et:")) continue;
    const et = k.slice(3);
    opts.push({ key: k, label: errtypeInfo(et).hebrew || et, n });
  }
  return opts;
}

function visibleFixRows() {
  const f = S.fixView;
  if (!f) return S.fixRows;
  if (f === "blocked") {
    return S.fixRows.filter(r => r.anchor && !r.anchor.ok);
  }
  if (f.startsWith("st:")) {
    const st = f.slice(3);
    return S.fixRows.filter(r => effStatus(r) === st);
  }
  if (f.startsWith("et:")) {
    const et = f.slice(3);
    return S.fixRows.filter(r => (r.errtype || "") === et);
  }
  return S.fixRows;
}

/* the selected finding, and how to move the selection within the view */
function currentFixRow() {
  const view = visibleFixRows();
  if (S.fixCurId != null) {
    const hit = view.find(r => r.id === S.fixCurId);
    if (hit) return hit;
  }
  return view[0] || null;
}

function moveFixSelection(delta) {
  const view = visibleFixRows();
  if (!view.length) return;
  const cur = currentFixRow();
  const at = Math.max(0, view.findIndex(r => cur && r.id === cur.id));
  const next = view[Math.min(view.length - 1, Math.max(0, at + delta))];
  if (next) selectFixRowById(next.id);
}

/* a row can be written only when the server managed to anchor it */
function canApply(r) {
  return !!(r && r.anchor && r.anchor.ok && (r.correction || effFix(r)) &&
            effStatus(r) !== "fixed");
}

function rowMode(r) {
  return S.fixModeOverride.get(r.id) || S.fixMode;
}

/* -------------------------------------------------- the document pane */
function renderFixDoc() {
  const box = $("#fixDoc");
  box.replaceChildren();
  if (!S.fixDoc || !S.fixDoc.editable) return;
  const byLine = new Map();
  for (const r of S.fixRows) {
    if (r.lineno == null) continue;
    if (!byLine.has(r.lineno)) byLine.set(r.lineno, []);
    byLine.get(r.lineno).push(r);
  }
  const nums = [...S.fixLines.keys()].sort((a, b) => a - b);
  let prev = null;
  for (const n of nums) {
    // the pane may be windowed around the findings; say so rather than
    // letting the corrector think the book is shorter than it is
    if (prev != null && n > prev + 1) {
      box.append(el("div", { class: "fdoc-gap" },
        "… דילוג על " + fmtNum(n - prev - 1) + " שורות …"));
    }
    prev = n;
    box.append(fixDocLine(n, byLine.get(n) || []));
  }
  if (!nums.length) box.append(el("div", { class: "fixer-empty" }, "הקובץ ריק."));
}

/* Patch the one book line a status change actually touched, instead of
 * rebuilding every line the document pane has loaded. Safe unconditionally:
 * unlike the worklist, the doc pane has no status filter, so a status change
 * never adds/removes/reorders lines — only what's drawn on the one line. */
function patchFixDocLineFor(id) {
  const box = $("#fixDoc");
  const r = S.fixRows.find(x => x.id === id);
  if (!r || r.lineno == null) return false;
  const old = box.querySelector('.fdoc-line[data-n="' + r.lineno + '"]');
  if (!old) return false;
  const rows = S.fixRows.filter(x => x.lineno === r.lineno);
  old.replaceWith(fixDocLine(r.lineno, rows));
  return true;
}

function fixDocLine(n, rows) {
  const info = S.fixLines.get(n);
  const text = info ? info.text : "";
  const cur = currentFixRow();
  const line = el("div", {
    class: "fdoc-line" + (rows.length ? " has-fix" : "") +
      (rows.some(r => effStatus(r) === "fixed") ? " edited" : "") +
      (cur && cur.lineno === n ? " current" : ""),
    dataset: { n: String(n) },
  });
  line.append(el("span", { class: "fdoc-num" }, String(n + 1)));
  const body = el("bdi", { class: "fdoc-text" });

  // anchored highlights, spliced in from RIGHT to LEFT (descending offset) —
  // the same rule the writer uses, so what is shown is what will be written
  const anchored = rows.filter(r => r.anchor && r.anchor.ok)
    .sort((a, b) => b.anchor.start - a.anchor.start);
  let tail = text.length;
  const pieces = [];
  for (const r of anchored) {
    const a = r.anchor;
    if (a.end > tail) continue;                 // overlapping: skip the later
    pieces.unshift(text.slice(a.end, tail));
    pieces.unshift(fixHitNode(r, text.slice(a.start, a.end),
                              cur && cur.id === r.id));
    tail = a.start;
  }
  pieces.unshift(text.slice(0, tail));

  // A row the server refused to place is resolved by pointing at the word,
  // which turns the whole line into clickable tokens. That mode is driven by
  // the SELECTED row, not by whichever blocked row happens to be first:
  // otherwise a second blocked finding on the same line could never be
  // resolved, and the line's other — correctly anchored — marks would lose
  // their previews for as long as any one row on it stayed blocked.
  const pick = rows.find(r => r.anchor && !r.anchor.ok && cur &&
                         r.id === cur.id && Array.isArray(info && info.tokens));
  if (pick && info.tokens.length) {
    body.append(...tokenPickLine(text, info.tokens, pick));
  } else {
    for (const p of pieces) {
      body.append(typeof p === "string" ? document.createTextNode(p) : p);
    }
  }
  line.append(body);
  return line;
}

/* One marked word inside the book text.
 *
 * The corrector's question at every mark is "what goes and what comes?", so
 * the mark answers it in place, using the printer's convention: the original
 * struck through, the correction underlined right after it. Nothing is hidden
 * behind a tooltip — the line reads as the corrected sentence would, with the
 * old word still visible for comparison.
 *
 * In bracket mode the mark shows the literal string that will be written,
 * "(תיקון) [שגיאה]", because there the original stays in the book too.
 */
function fixHitNode(r, rawSlice, isCurrent) {
  const st = effStatus(r);
  const fix = r.correction || effFix(r) || "";
  const a = r.anchor || {};
  const mode = rowMode(r);
  const done = st === "fixed";
  const wrap = el("span", {
    class: "fdoc-hit st-" + st + (isCurrent ? " current" : "") +
      (a.confidence === "weak" ? " weak" : ""),
    dataset: { id: String(r.id) },
    title: statusInfo(st).hebrew + " — " + (r.word || "") + " ⇐ " + (fix || "—"),
  });
  // status marker, so a glance over the page says what was decided where
  wrap.append(el("span", { class: "fdoc-badge" }, statusInfo(st).icon));

  // Nothing is going to be written here, so showing a correction would be a
  // lie: a rejected or ignored finding leaves the book exactly as it is, and
  // an already-applied one is the book as it is. Show the word plainly.
  const keeps = st === "not_error" || st === "ignored";
  if (done || keeps || !fix) {
    wrap.append(el("bdi", { class: "fdoc-word" }, rawSlice));
  } else if (mode === "bracket") {
    wrap.append(el("bdi", { class: "fdoc-bracket" }, "(" + fix + ") [" + rawSlice + "]"));
  } else {
    wrap.append(el("bdi", { class: "fdoc-old" }, rawSlice),
                el("bdi", { class: "fdoc-new" }, fix));
  }
  wrap.addEventListener("click", ev => {
    ev.stopPropagation();
    selectFixRowById(r.id);
  });
  return wrap;
}

/* every token clickable, so an ambiguous finding can be resolved by hand */
function tokenPickLine(text, tokens, row) {
  const out = [];
  let pos = 0;
  for (const [a, b] of tokens) {
    if (a < pos) continue;
    out.push(document.createTextNode(text.slice(pos, a)));
    const t = el("span", { class: "fdoc-tok", title: "לחיצה תסמן שזה המופע לתיקון" },
      text.slice(a, b));
    t.addEventListener("click", ev => {
      ev.stopPropagation();
      resolveOccurrence(row, a, b);
    });
    out.push(t);
    pos = b;
  }
  out.push(document.createTextNode(text.slice(pos)));
  return out;
}

/* The human pointed at a word. `start`/`end` are a token span the SERVER sent
 * with this line, echoed back unchanged — the browser still never measures the
 * file. The server re-verifies the span really is this word before writing
 * (patcher.plan_edit's `explicit` branch), so a stale pick is caught there. */
function resolveOccurrence(row, start, end) {
  const lineText = (S.fixLines.get(row.lineno) || {}).text || "";
  const picked = lineText.slice(start, end);
  row.explicit = { start, end };
  row.anchor = { ok: true, start, end, confidence: "manual",
                 spans_markup: picked.indexOf("<") >= 0 };
  // Pointing at a word says WHERE, not WHETHER. Arming an undecided finding
  // on a locational click would write into a book something nobody judged.
  if (effStatus(row) === "approved") S.fixPicked.add(row.id);
  toast(effStatus(row) === "approved"
    ? "המופע סומן: «" + picked + "» — התיקון יוחל כאן בלבד"
    : "המופע סומן: «" + picked + "». הממצא עדיין לא אושר — יש לאשר אותו כדי שייכלל בתיקון.",
    "ok", 6000);
  renderFixDoc();
  renderFixList(false);
  updateApplyButton();
  showBlockedBanner();
}

/* Bring the finding into view — the WORD, not the top of its paragraph.
 * Book lines can run for hundreds of characters, so scrolling to the line
 * often left the marked word far below the fold, which is the opposite of
 * the point. Scroll to the mark itself when it exists. */
function scrollDocToLine(n, id) {
  const doc = $("#fixDoc");
  if (!doc) return;
  const line = doc.querySelector(".fdoc-line[data-n='" + n + "']");
  if (!line) return;
  const hit = id != null
    ? line.querySelector(".fdoc-hit[data-id='" + id + "']")
    : line.querySelector(".fdoc-hit");
  (hit || line).scrollIntoView({ block: "center", inline: "center",
                                 behavior: "smooth" });
}

function updateFixHead() {
  const b = fixBookInfo();
  const d = S.fixDoc;
  $("#fixDocPath").textContent = (d && d.book && d.book.path) || (b && b.path) || "";
  if (!d || !d.editable) { $("#fixDocMeta").textContent = ""; return; }
  const bits = [fmtNum(d.line_count) + " שורות", d.encoding];
  if (d.windowed) bits.push("תצוגה מקוצרת סביב הממצאים");
  $("#fixDocMeta").textContent = bits.join(" · ");
}

/* -------------------------------------------------- banners */
function setFixBanner(node, kind) {
  const bn = $("#fixerBanner");
  if (!node) { bn.hidden = true; bn.replaceChildren(); return; }
  bn.hidden = false;
  bn.className = kind || "";
  bn.replaceChildren(typeof node === "string"
    ? el("div", { class: "bn-text" }, node) : node);
}

function showBlockedBanner() {
  const blocked = S.fixRows.filter(r => r.anchor && !r.anchor.ok &&
                                   effStatus(r) !== "fixed");
  if (!blocked.length) { setFixBanner(null); return; }
  const byCode = new Map();
  for (const r of blocked) {
    const c = r.anchor.code || "";
    if (!byCode.has(c)) byCode.set(c, { message: r.anchor.message, n: 0 });
    byCode.get(c).n++;
  }
  const ul = el("ul");
  for (const [, v] of byCode) {
    ul.append(el("li", null, v.message + " (" + fmtNum(v.n) + ")"));
  }
  setFixBanner(el("div", { class: "bn-text" },
    el("b", null, fmtNum(blocked.length) + " ממצאים לא יוחלו אוטומטית — "),
    "הכלי לא הצליח לאתר אותם בוודאות בקובץ, ולכן הוא לא ינחש. " +
    "אפשר ללחוץ על המילה הנכונה בטקסט כדי לסמן אותה ידנית.",
    ul));
}

/* -------------------------------------------------- worklist */
function renderFixList(scroll) {
  const box = $("#fixList");
  box.replaceChildren();
  if (!S.fixKey) {
    box.append(el("div", { class: "fixer-empty" },
      "בחר ספר מהרשימה — הכלי יפתח את קובץ הטקסט עצמו ויסמן בו את המקומות לתיקון."));
    updateFixProgress();
    return;
  }
  if (!S.fixRows.length) {
    box.append(el("div", { class: "fixer-empty" }, "אין ממצאים לתיקון בספר זה 🎉"));
    updateFixProgress();
    return;
  }
  const editable = !!(S.fixDoc && S.fixDoc.editable);
  // filtered, but never re-ordered: the list always runs down the book
  const view = visibleFixRows();
  syncFixViewSelect();
  if (!view.length) {
    box.append(el("div", { class: "fixer-empty" },
      "אין ממצאים מהסוג שנבחר בספר זה. אפשר לבחור «הכל» כדי לראות את השאר."));
    updateFixProgress();
    updateApplyButton();
    return;
  }
  let lastRef = null;
  view.forEach((r, idx) => {
    const ref = r.ref || "(ללא מראה מקום)";
    if (ref !== lastRef) {
      box.append(el("div", { class: "fix-refgroup" }, el("bdi", null, ref)));
      lastRef = ref;
    }
    box.append(fixRowNode(r, idx, editable));
  });
  updateFixProgress();
  updateApplyButton();
  if (scroll) scrollFixCurrent();
}

/* Patch a single already-rendered row in place instead of rebuilding the
 * whole (possibly huge) worklist. Only safe when the status change cannot
 * have moved the finding in or out of the current filter — callers must
 * check that first (see canPatchFixRows). Falls back to the caller doing a
 * full renderFixList() when the row isn't currently on screen. */
function patchFixRowById(id, editable) {
  const box = $("#fixList");
  const old = box.querySelector('.fix-row[data-id="' + id + '"]');
  if (!old) return false;
  const r = S.fixRows.find(x => x.id === id);
  if (!r) return false;
  const idx = Number(old.dataset.idx);
  const fresh = fixRowNode(r, idx, editable);
  old.replaceWith(fresh);
  return true;
}

/* A plain status/note/suggestion edit never changes which findings exist or
 * their order — it can only change which FILTERED VIEW they belong to (a
 * status filter, or "blocked" if anchoring info also changed). So patching
 * in place is safe exactly when the current view isn't filtered by status,
 * and isn't the "blocked" view (anchor.ok doesn't change here either way,
 * but keep the check for future-proofing). */
function canPatchFixRows() {
  return !S.fixView || S.fixView.startsWith("et:");
}

/* rebuild the view picker, showing how many findings each choice holds */
function syncFixViewSelect() {
  const sel = $("#fixView");
  if (!sel) return;
  const opts = fixViewOptions();
  sel.replaceChildren();
  for (const o of opts) {
    sel.append(el("option", { value: o.key },
      o.label + " (" + fmtNum(o.n) + ")"));
  }
  if (!opts.some(o => o.key === S.fixView)) S.fixView = "";
  sel.value = S.fixView;
}

function fixRowNode(r, idx, editable) {
  const done = effStatus(r) === "fixed";
  const blocked = !!(r.anchor && !r.anchor.ok) && !done;
  const cur = currentFixRow();
  const row = el("div", {
    class: "fix-row" + (cur && cur.id === r.id ? " current" : "") +
      (done ? " done-row" : "") + (blocked ? " row-blocked" : ""),
    dataset: { idx: String(idx), id: String(r.id) },
  });
  const main = el("div", { class: "fix-main" });
  const rfix = r.correction || effFix(r);
  const mode = rowMode(r);
  const st = effStatus(r);
  const sinfo = statusInfo(st);
  // the status of THIS finding, stated rather than implied by row colour
  const chip = el("span", { class: "chip st-" + st, title: "לחיצה לשינוי הסטטוס" },
    (sinfo.icon ? sinfo.icon + " " : "") + sinfo.hebrew);
  chip.addEventListener("click", ev => {
    ev.stopPropagation();
    openStatusMenu(ev.currentTarget, key => fixAct(r, key));
  });
  main.append(el("div", { class: "fix-wordline" },
    chip,
    wordDiffNode(r.word || "", rfix || "", "err", "w-err"),
    el("span", { class: "arr" }, "⇐"),
    rfix ? wordDiffNode(rfix, r.word || "", "fix", "w-fix")
         : el("bdi", { class: "w-fix" }, "—"),
    r.custom_suggestion ? el("span", { class: "et-tag" }, "תיקון ידני") : null,
    mode === "bracket" ? el("span", { class: "fix-mode-tag" }, "סוגריים") : null));
  main.append(el("div", { class: "fix-snip" },
    renderSnippet(r.snippet, r.word, null, rfix)));
  if (r.lineno != null && editable) {
    const loc = el("span", { class: "fix-loc" }, "📍 שורה " + fmtNum(r.lineno + 1));
    loc.addEventListener("click", ev => { ev.stopPropagation(); scrollDocToLine(r.lineno, r.id); });
    main.append(loc);
  }
  if (blocked) main.append(el("div", { class: "fix-warn" }, r.anchor.message || ""));
  else if (r.anchor && r.anchor.confidence === "weak") {
    // the surrounding text does not look like the text the finding was made
    // against — probably fine, but worth a human glance before writing
    main.append(el("div", { class: "fix-warn weak-warn" },
      "⚠ הקטע בקובץ אינו תואם במלואו לקטע שנסרק — כדאי לוודא לפני ההחלה."));
  }
  if (r.anchor && r.anchor.ok && r.anchor.spans_markup) {
    // replacing takes the tag with the word (fine); bracketing would wrap
    // half of it and mis-nest the markup, so that combination is refused
    main.append(el("div", { class: "fix-warn" },
      "⚠ המילה חצויה בקובץ על ידי תגית עיצוב — תיקון אוטומטי היה שובר אותה. יש לתקן ידנית."));
  }
  row.append(main);

  const side = el("div", { class: "fix-side" });
  // the whole decision set from the cards tab, so a corrector can also settle
  // findings that were never triaged instead of leaving the screen
  const acts = el("div", { class: "fix-acts" });
  const act = (label, title, fn, cls) => {
    const b = el("button", { title: title || label, class: cls || null }, label);
    b.addEventListener("click", ev => { ev.stopPropagation(); fn(); });
    return b;
  };
  acts.append(
    act("✅ אושר", "זו שגיאה — לאשר את התיקון", () => fixAct(r, "approved"), "act-approve"),
    act("❌ לא שגיאה", "המילה תקינה", () => fixAct(r, "not_error"), "act-reject"),
    act("❌ בכל מקום", "המילה תקינה בכל הספרים", () => fixAct(r, "not_error", { scope: "word" })),
    act("✏ תיקון ידני", "הקלדת תיקון משלך", () => toggleFixEdit(row, r)),
    act("❓ בירור", "דרוש בירור", () => fixAct(r, "unsure")),
    act("🚫 התעלם", "התעלם מהממצא", () => fixAct(r, "ignored")),
    act("🔧 תוקן", "סמן כתוקן בלי לכתוב לקובץ", () => fixAct(r, "fixed")));
  side.append(acts);

  if (editable) {
    const seg = el("div", { class: "seg mini" });
    for (const m of [["replace", "החלפה"], ["bracket", "סוגריים"]]) {
      const b = el("button", {
        class: "seg-btn" + (mode === m[0] ? " active" : ""),
        title: m[0] === "bracket" ? "(תיקון) [שגיאה]" : "החלפה מלאה",
      }, m[1]);
      b.addEventListener("click", ev => {
        ev.stopPropagation();
        S.fixModeOverride.set(r.id, m[0]);
        renderFixList(false);
      });
      seg.append(b);
    }
    side.append(seg);

    const pick = el("label", { class: "fix-pick" });
    const cb = el("input", { type: "checkbox" });
    cb.checked = S.fixPicked.has(r.id);
    cb.disabled = !canApply(r);
    cb.addEventListener("click", ev => ev.stopPropagation());
    cb.addEventListener("change", () => {
      if (cb.checked) S.fixPicked.add(r.id); else S.fixPicked.delete(r.id);
      updateApplyButton();
    });
    pick.append(cb, el("span", null, done ? "תוקן בקובץ" : "לכלול בהחלה"));
    side.append(pick);
  }
  row.append(side);

  // inline manual-correction box
  const edit = el("div", { class: "fix-edit" },
    el("input", { type: "text", dir: "rtl", value: rfix || "",
                  placeholder: "הקלד את התיקון הנכון…" }),
    el("button", { class: "btn primary" }, "שמור ואשר"));
  const inp = edit.querySelector("input");
  const save = async () => {
    const v = inp.value.trim();
    if (!v) { toast("יש להקליד תיקון תחילה", "err"); return; }
    r.correction = v;
    await fixAct(r, "approved", { custom_suggestion: v });
  };
  edit.querySelector("button").addEventListener("click", ev => { ev.stopPropagation(); save(); });
  inp.addEventListener("keydown", ev => {
    ev.stopPropagation();
    if (ev.key === "Enter") { ev.preventDefault(); save(); }
    if (ev.key === "Escape") { ev.preventDefault(); edit.classList.remove("visible"); }
  });
  row.append(edit);

  row.addEventListener("click", ev => {
    if (ev.target.closest("button, input, label, mark")) return;
    selectFixRowById(r.id);
  });
  return row;
}

function toggleFixEdit(row, r) {
  const box = row.querySelector(".fix-edit");
  if (!box) return;
  const open = !box.classList.contains("visible");
  box.classList.toggle("visible", open);
  if (open) { const i = box.querySelector("input"); i.focus(); i.select(); }
}

function selectFixRowById(id) {
  const r = S.fixRows.find(x => x.id === id);
  if (!r) return;
  const prevId = S.fixCurId;
  S.fixCurId = id;
  // Selection only moves the "current" highlight — it never changes which
  // findings exist, their filter/order, or their status, so the previous and
  // new current row/line are the only DOM the change can affect. Patching
  // just those two (instead of rebuilding the whole worklist + doc pane)
  // keeps clicking through a book with many findings snappy.
  const editable = !!(S.fixDoc && S.fixDoc.editable);
  const patched = canPatchFixRows() &&
    (prevId == null || patchFixRowById(prevId, editable)) &&
    patchFixRowById(id, editable);
  if (patched) {
    if (prevId != null) patchFixDocLineFor(prevId);
    patchFixDocLineFor(id);
  } else {
    renderFixList(false);
    renderFixDoc();
  }
  if (r.lineno != null) scrollDocToLine(r.lineno, r.id);
  scrollFixCurrent();
}

/* a status decision from inside the fixer — reuses the shared writer, so the
 * optimistic update, the rollback and Ctrl+Z all behave as everywhere else */
async function fixAct(r, status, opts) {
  // Only an approved finding stays armed. "Needs clarification" in
  // particular is the opposite of "write this now", so leaving it ticked
  // would apply the very thing the corrector just flagged as unresolved.
  // Set this BEFORE setStatus's optimistic repaint, so the row it patches
  // in place (see repaintStatuses/patchFixRowById) draws its checkbox from
  // the up-to-date pick state instead of a stale one.
  const wasPicked = S.fixPicked.has(r.id);
  if (status === "approved") S.fixPicked.add(r.id);
  else S.fixPicked.delete(r.id);
  try {
    await setStatus([r.id], status, opts || {});
  } catch (e) {
    // toast already shown; setStatus's own catch already repainted the row
    // with the rolled-back status, so undo the pick-set change to match.
    if (wasPicked) S.fixPicked.add(r.id); else S.fixPicked.delete(r.id);
  }
  updateApplyButton();
}

function scrollFixCurrent() {
  const cur = $("#fixList .fix-row.current");
  if (cur) cur.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* Mark this finding fixed and move on to the next one still needing work.
 * Takes the ROW, like every other action here — and advances through the
 * VISIBLE list, so a filtered view does not jump to a finding off screen. */
async function markFixed(r) {
  if (!r || effStatus(r) === "fixed") return;
  await fixAct(r, "fixed");
  const view = visibleFixRows();
  const at = view.findIndex(x => x.id === r.id);
  const next = view.slice(at + 1).find(x => effStatus(x) !== "fixed");
  if (next) selectFixRowById(next.id);
}

function updateFixProgress() {
  // progress is always about the BOOK, never the current filter — otherwise
  // narrowing the view would appear to change how much work is left
  const total = S.fixRows.length;
  const done = S.fixRows.filter(r => effStatus(r) === "fixed").length;
  const pct = total ? Math.round(done * 100 / total) : 0;
  let txt = "";
  if (S.fixKey) {
    txt = "תוקנו " + fmtNum(done) + " מתוך " + fmtNum(total) +
          " בספר זה (" + pct + "%)";
    const shown = visibleFixRows().length;
    if (S.fixView && shown !== total) txt += " · מוצגים " + fmtNum(shown);
  }
  $("#fixProgressText").textContent = txt;
  $("#fixProgressBar").style.width = pct + "%";
}

/* -------------------------------------------------- mode + apply */
function syncModeButtons() {
  $("#fixModeReplace").classList.toggle("active", S.fixMode !== "bracket");
  $("#fixModeBracket").classList.toggle("active", S.fixMode === "bracket");
}

async function setFixMode(mode) {
  S.fixMode = mode;
  syncModeButtons();
  renderFixList(false);
  writeHash();          // keep the URL honest: the mode changes what gets WRITTEN
  if (S.fixKey && S.fixDoc && S.fixDoc.editable) {
    try {
      await api("/api/fixer/mode", { method: "POST", body: { key: S.fixKey, mode } });
    } catch (e) { /* a failed preference is not worth interrupting for */ }
  }
}

function applicableRows() {
  return S.fixRows.filter(r => S.fixPicked.has(r.id) && canApply(r));
}

/* Quick selection.
 *
 * Writing into a book is a decision, so the set of corrections to write is
 * built explicitly. Findings nobody has judged are never pre-selected; these
 * buttons are how a corrector opts a whole group in, without ticking dozens
 * of boxes by hand.
 */
function fixPick(which) {
  if (which === "none") {
    S.fixPicked.clear();
  } else if (which === "approved") {
    S.fixPicked = new Set(S.fixRows
      .filter(r => canApply(r) && effStatus(r) === "approved")
      .map(r => r.id));
  } else if (which === "verified") {
    S.fixPicked = new Set(S.fixRows
      .filter(r => canApply(r) && effStatus(r) === "approved" && r.verified)
      .map(r => r.id));
  } else if (which === "view") {
    // whatever the current filter shows — the corrector can see exactly what
    // they are about to select
    S.fixPicked = new Set(visibleFixRows().filter(canApply).map(r => r.id));
  }
  renderFixList(false);
  renderFixDoc();
  updateApplyButton();
}

function updateFixPickBar() {
  const bar = $("#fixPickBar");
  if (!bar) return;
  const editable = !!(S.fixDoc && S.fixDoc.editable) && S.fixRows.length;
  bar.hidden = !editable;
  if (!editable) return;
  const n = applicableRows().length;
  const avail = S.fixRows.filter(canApply).length;
  const undecided = S.fixRows.filter(
    r => canApply(r) && effStatus(r) !== "approved").length;
  const parts = ["נבחרו " + fmtNum(n) + " מתוך " + fmtNum(avail) +
                 " הניתנים לתיקון"];
  if (undecided) {
    parts.push(fmtNum(undecided) + " טרם הוחלטו — לא ייכנסו אלא אם תבחר בהם");
  }
  $("#fixPickCount").textContent = parts.join(" · ");
}

function updateApplyButton() {
  updateFixPickBar();
  const btn = $("#fixApply");
  const editable = !!(S.fixDoc && S.fixDoc.editable) && !S.fixDocStale;
  const n = applicableRows().length;
  btn.disabled = !editable || !n;
  btn.textContent = n ? "✔ החלה על הקובץ (" + fmtNum(n) + ")" : "✔ החלה על הקובץ";
  const undo = $("#fixUndoFile");
  const last = (S.fixEdits || []).find(e => !e.undone_at);
  undo.hidden = !last;
  if (last) undo.dataset.editId = String(last.id);
}

async function applyFixes() {
  const rows = applicableRows();
  if (!rows.length || !S.fixDoc) return;
  const bracket = rows.filter(r => rowMode(r) === "bracket").length;
  const what = bracket
    ? "מתוכם " + fmtNum(bracket) + " ייכתבו במצב סוגריים «(תיקון) [שגיאה]».\n"
    : "";
  // This is the moment of commitment, and the corrector may have scrolled far
  // from the marks — so name the actual changes here, not just a count.
  const sample = rows.slice(0, 6).map(r => {
    const fix = r.correction || effFix(r) || "";
    return "  שורה " + fmtNum((r.lineno || 0) + 1) + ":  " + r.word + " ⇐ " +
           (rowMode(r) === "bracket" ? "(" + fix + ") [" + r.word + "]" : fix);
  }).join("\n");
  const more = rows.length > 6
    ? "\n  …ועוד " + fmtNum(rows.length - 6) + " תיקונים" : "";
  if (!confirm("להחיל " + fmtNum(rows.length) + " תיקונים על הקובץ?\n\n" +
               sample + more + "\n\n" + what +
               "גיבוי של הקובץ יישמר לפני הכתיבה.")) return;
  const btn = $("#fixApply");
  btn.disabled = true;
  const items = rows.map(r => {
    const it = { id: r.id, mode: rowMode(r) };
    if (r.correction && r.correction !== r.suggestion) it.correction = r.correction;
    if (r.explicit) {
      it.explicit_start = r.explicit.start;
      it.explicit_end = r.explicit.end;
    }
    return it;
  });
  try {
    const resp = await api("/api/fixer/apply", {
      method: "POST",
      body: { key: S.fixKey, fingerprint: S.fixDoc.fingerprint,
              default_mode: S.fixMode, items, mark_fixed: true },
    });
    S.fixDoc.fingerprint = resp.fingerprint || S.fixDoc.fingerprint;
    for (const l of resp.changed_lines || []) S.fixLines.set(l.n, l);
    for (const a of resp.applied || []) {
      const r = S.fixRows.find(x => x.id === a.id);
      if (r) { r.effective_status = "fixed"; r.applied = true; }
      S.fixPicked.delete(a.id);
    }
    if (resp.db_warning) toast(resp.db_warning, "err", 9000);
    toast(resp.message + " (" + fmtNum((resp.applied || []).length) + " תיקונים)", "ok", 6000);
    // the anchors of everything left now refer to the OLD text, so reload
    await loadFixDoc();
    await loadFixerBooks();
    $("#fixBook").value = S.fixKey;
  } catch (e) {
    handleApplyFailure(e);
  }
  updateApplyButton();
}

/* a refusal is informative, not a dead end: say which findings were refused
 * and why, and offer the reload that makes them applicable again */
function handleApplyFailure(e) {
  const failed = (e.data && e.data.failed) || [];
  if (failed.length) {
    const ul = el("ul");
    for (const f of failed.slice(0, 8)) {
      const r = S.fixRows.find(x => x.id === f.id);
      ul.append(el("li", null, (r ? "«" + (r.word || "") + "» — " : "") + f.message));
      if (r) { r.anchor = { ok: false, code: f.code, message: f.message }; S.fixPicked.delete(r.id); }
    }
    if (failed.length > 8) ul.append(el("li", null, "…ועוד " + fmtNum(failed.length - 8)));
    setFixBanner(el("div", { class: "bn-text" },
      el("b", null, "לא נכתב דבר לקובץ. "),
      "חלק מהתיקונים לא אושרו:", ul), "err");
    renderFixList(false);
    renderFixDoc();
  } else {
    setFixBanner(el("div", { class: "bn-text" }, e.message), "err");
  }
  if (e.code === "file_changed") {
    S.fixDocStale = true;
    const bn = $("#fixerBanner");
    const b = el("button", { class: "btn primary" }, "🔄 טען מחדש");
    b.addEventListener("click", () => loadFixDoc());
    bn.append(b);
  }
  toast(e.message, "err", 8000);
}

async function undoFileEdit() {
  const id = $("#fixUndoFile").dataset.editId;
  if (!id) return;
  if (!confirm("לשחזר את הקובץ מהגיבוי?\nהתיקונים שנכתבו בו יבוטלו, " +
               "והממצאים יחזרו לסטטוס «אושר».")) return;
  try {
    const resp = await api("/api/fixer/undo_file", {
      method: "POST", body: { edit_id: Number(id) } });
    toast(resp.message, "ok", 6000);
    await loadFixDoc();
  } catch (e) {
    toast(e.message, "err", 8000);
  }
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
    el("tr", null, el("td", null, kbd("↑")), el("td", null, "מעבר בין הממצאים ברשימה"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("1")), el("td", null, "סטטוסים 1‑6: אושר / לא שגיאה / בירור / התעלם / תוקן / טרם נבדק"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("ס")), el("td", null, "החלפת מצב הכתיבה של התיקון הנוכחי (החלפה ⇄ סוגריים)"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("פ")), el("td", null, "סימון / ביטול סימון התיקון להחלה"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("Home")), el("td", null, "מעבר לממצא הראשון / האחרון ברשימה"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("Ctrl+S")), el("td", null, "החלת התיקונים המסומנים על הקובץ"), el("td", null, "מצב מתקן")),
    el("tr", null, el("td", null, kbd("Ctrl+Z")), el("td", null, "ביטול הפעולה האחרונה"), el("td", null, "בכל מקום")),
    el("tr", null, el("td", null, kbd("Esc")), el("td", null, "סגירת חלונית / תפריט"), el("td", null, "בכל מקום")));
  body.append(el("div", { class: "help-block" }, el("h3", null, "⌨ קיצורי מקלדת"), shTable));
  // fixer mode — the file-writing screen needs its own explanation, above all
  // because there are two different "undo"s and they do different things
  body.append(el("div", { class: "help-block" },
    el("h3", null, "🛠 מצב מתקן — תיקון בתוך הקובץ"),
    el("p", null, "בוחרים ספר, והכלי פותח את קובץ הטקסט עצמו ומסמן בו את " +
      "המקומות לתיקון. אין צורך לפתוח את הספר בעורך חיצוני."),
    el("ul", null,
      el("li", null, el("b", null, "רשימת הספרים: "),
        "מזוהים לפי הקובץ, לא לפי השם. לכן שני ספרים בשם «פרק א» בתיקיות " +
        "שונות מופיעים בנפרד, ולצד כל אחד מוצגת התיקייה שלו."),
      el("li", null, el("b", null, "הסימון בטקסט: "),
        "כל מילה מסומנת מראה גם ", el("b", null, "מה ירד"),
        " (המילה המקורית, מחוקה בקו) וגם ", el("b", null, "מה יהיה"),
        " (התיקון, בקו תחתון ירוק) — כך רואים בדיוק מה ייכתב, בלי לרחף עם " +
        "העכבר. לצד כל סימון מופיע גם סמל הסטטוס שלו."),
      el("li", null, el("b", null, "הצגה לפי סוג: "),
        "התיבה שבסרגל מציגה רק ממצאים מסטטוס או מסוג מסוים (למשל רק «טרם " +
        "נבדק», או רק אלה שדורשים אישור ידני). ",
        el("b", null, "הסדר תמיד נשאר לפי מספר השורה בספר"),
        " — הסינון מצמצם את הרשימה ולא מערבב אותה, כדי לא לאבד את המקום."),
      el("li", null, el("b", null, "אופן הכתיבה: "),
        "«החלפה» — המילה השגויה מוחלפת בתיקון. «סוגריים» — נכתב " +
        "«(תיקון) [שגיאה]», כלומר התיקון בסוגריים עגולים ואחריו המילה " +
        "המקורית במרובעים. אפשר לקבוע ברירת מחדל לספר (בסרגל העליון) " +
        "ולדרוס אותה לכל תיקון בנפרד (הכפתורים בשורת התיקון)."),
      el("li", null, el("b", null, "כל פעולות הסקירה זמינות כאן: "),
        "אפשר גם לאשר, לדחות, להקליד תיקון ידני או לסמן «דרוש בירור» — " +
        "כך אפשר לטפל גם בממצאים שטרם נבדקו בלי לצאת מהמסך."),
      el("li", null, el("b", null, "מה נכנס לתיקון: "),
        el("b", null, "ממצא שטרם הוחלט לגביו לעולם אינו נכנס אוטומטית"),
        " — רק ממצאים ב«אושר» מסומנים מראש. בסרגל הבחירה אפשר לבחור במכה " +
        "אחת «רק מאושרים», «מאושרים ומאומתים», או «הנראים כעת»."),
      el("li", null, el("b", null, "גיבוי אוטומטי: "),
        "לפני כל כתיבה נשמר עותק מלא של הקובץ. שום דבר בקובץ אינו משתנה " +
        "מלבד המילים שתוקנו."),
      el("li", null, el("b", null, "אם הספר השתנה מאז הסריקה: "),
        "אין צורך בסריקה חדשה — הקובץ נקרא מהדיסק בכל פתיחה. אם שורות זזו, " +
        "הכלי מאתר את המילה במקומה החדש, אך ורק כשהוא מזהה בוודאות שזה " +
        "אותו קטע ממש — בספרות הזו נוסחאות חוזרות בפסוקים מקבילים, ודמיון " +
        "לבדו אינו מספיק. מילה שכבר תוקנה ידנית תסומן באדום ולא תיגע."),
      el("li", null, el("b", null, "ממצאים שלא יוחלו אוטומטית: "),
        "אם הכלי אינו יכול לקבוע בוודאות היכן המילה נמצאת (למשל כשהיא " +
        "מופיעה כמה פעמים באותה שורה, או שהקובץ השתנה מאז הסריקה) — הוא " +
        "לא ינחש. הממצא יסומן באדום, ואפשר ללחוץ על המילה הנכונה בטקסט " +
        "כדי לסמן אותה ידנית.")),
    el("p", null, el("b", null, "שימו לב — שני סוגי ביטול: ")),
    el("ul", null,
      el("li", null, el("b", null, "Ctrl+Z / «ביטול» "),
        "מבטל את ההחלטה (הסטטוס) בלבד — ", el("b", null, "הקובץ עצמו לא משתנה"), "."),
      el("li", null, el("b", null, "«↩ שחזור מגיבוי» "),
        "מחזיר את הקובץ עצמו למצבו לפני התיקון, ומחזיר את הממצאים " +
        "לסטטוס «אושר»."))));
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
/* Two prefixes carry a path + line number:
     file:<repo-relpath>:<lineno>   — a book inside the library repo
     local:<absolute-path>:<lineno> — a .txt scanned from anywhere on disk
   The absolute form already IS the full path, so it must not be joined onto
   the repo root. Both split from the RIGHT: a Windows path contains ':'. */
function parseFileUnit(unit) {
  if (typeof unit !== "string") return null;
  let abs = false, body;
  if (unit.indexOf("file:") === 0) body = unit.slice(5);
  else if (unit.indexOf("local:") === 0) { body = unit.slice(6); abs = true; }
  else return null;
  const i = body.lastIndexOf(":");
  if (i < 1) return null;
  const ln = body.slice(i + 1);
  if (!/^\d+$/.test(ln)) return null;
  return { rel: body.slice(0, i), lineno: parseInt(ln, 10), abs: abs };
}

function fileUnitFullPath(rel, abs) {
  const winRel = rel.replace(/\//g, "\\");
  if (abs) return winRel;                       // already an absolute path
  const root = SCAN.cfg && SCAN.cfg.corpus && SCAN.cfg.corpus.library_dir;
  return root ? root.replace(/[\\\/]+$/, "") + "\\" + winRel : winRel;
}

async function copyFilePath(rel, abs) {
  if (!abs && !SCAN.cfg) { try { await loadScanConfig(); } catch (e) {} }
  copyText(fileUnitFullPath(rel, abs));
}

/* path + 1-based line number + copy button, for findings scanned from files */
function fileUnitNode(pf) {
  const btn = el("button", { class: "copy-path", title: "העתקת נתיב הקובץ המלא" }, "📋 נתיב");
  btn.addEventListener("click", ev => { ev.stopPropagation(); copyFilePath(pf.rel, pf.abs); });
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
  bindBookScan();
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
const SCAN = { cfg: null, pollTimer: null, lastState: "idle", statusSeq: 0 };

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
  if (running && st.is_book) {
    // one indivisible stage — there is no "stage i of n" to report
    stageLine.textContent = "סורק ספר בודד מול המילון הקיים…";
  } else if (running && stageKey && total) {
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
  const cd = Number(st.chunk_done) || 0, ctot = Number(st.chunk_total) || 0;
  // A book scan reports no chunks, so there is no honest percentage to show:
  // use an indeterminate (animated) bar rather than a bar frozen at 0%.
  const indet = running && st.is_book && ctot === 0;
  bar.classList.toggle("indet", indet);
  // a book scan that failed/was cancelled has no meaningful percentage; let
  // the bar stay full and turn red instead of visibly draining to 0%
  const bookEnded = st.is_book && !running && st.state !== "idle";
  bar.style.width = (indet || bookEnded) ? "100%" : pct + "%";
  bar.classList.toggle("done", st.state === "done");
  bar.classList.toggle("err", st.state === "failed" || st.state === "cancelled");

  // chunk text (only when a chunk total is known)
  $("#scanChunkText").textContent = (running && ctot > 0)
    ? "נתח " + fmtNum(cd) + " מתוך " + fmtNum(ctot)
    : "";
  $("#scanPctText").textContent = (indet || bookEnded) ? "" : pct + "%";
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
  // the backend's precise Hebrew reason is far more useful than "נכשלה"
  if (st.state === "failed" && st.error) txt += " — " + st.error;
  line.textContent = txt;
  line.className = running ? "run" : (st.state === "done" ? "ok" : (st.state === "idle" ? "" : "err"));
  renderScanProgress(st);
  const log = $("#scanLog");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = (st.log_tail || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  $("#scanStart").hidden = running;
  $("#scanCancel").hidden = !running;
  // a book scan merges its findings itself, so there is nothing to refresh
  $("#scanRefreshAfter").hidden = st.state !== "done" || !!st.is_book;
  if (running) startScanPolling();
  else stopScanPolling();
  if (SCAN.lastState === "running" && !running) {
    if (st.state === "done" && st.is_book) {
      const r = (st.book && st.book.result) || {};
      const name = (st.book && st.book.title) || "הספר";
      // `added` is every merged row; `findings` counts only the error family
      toast("סריקת «" + name + "» הושלמה: " + fmtNum(r.added || 0) +
            " ממצאים" +
            (r.replaced ? " (הוחלפו " + fmtNum(r.replaced) + " קודמים)" : "") +
            (r.preserved ? ", " + fmtNum(r.preserved) + " החלטות נשמרו" : ""),
            "ok", 9000);
      // the findings are already merged — reload the views, but leave the
      // panel open so the user can read the log and scan another book
      reloadAfterBookScan();
    }
    else if (st.state === "done") toast("הסריקה הושלמה — אפשר לרענן את הממצאים", "ok", 8000);
    else if (st.state === "failed") toast("הסריקה נכשלה: " + (st.error || "ראו את יומן הריצה"), "err", 10000);
    else if (st.state === "cancelled") toast("הסריקה בוטלה", "", 5000);
  }
  SCAN.lastState = st.state;
}

/* The poller and syncScanStatusOnce race: a slow "running" response can land
   after a newer "done" one and reset SCAN.lastState, which re-fires the
   completion branch (duplicate toast + a second full reload). Drop any
   response older than the newest one already rendered. */
async function fetchScanStatus() {
  const seq = ++SCAN.statusSeq;
  let st;
  try { st = await api("/api/scan/status"); }
  catch (e) { return; }
  if (seq !== SCAN.statusSeq) return;
  renderScanStatus(st);
}

async function syncScanStatusOnce() { await fetchScanStatus(); }

function startScanPolling() {
  if (SCAN.pollTimer) return;
  SCAN.pollTimer = setInterval(fetchScanStatus, 2000);
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

/* -------------------------------------------- single-book scan (fast path) */
const BS = { source: "db", chosen: null, seq: 0 };

function bsSource() {
  const r = $("#bsSources input:checked");
  return r ? r.value : "db";
}

function bsUpdateRows() {
  const isFile = bsSource() === "file";
  $("#bsPickRow").hidden = isFile;
  $("#bsFileRow").hidden = !isFile;
}

function bsSetChosen(book) {
  BS.chosen = book;
  const box = $("#bsChosen");
  if (!book) { box.hidden = true; box.replaceChildren(); return; }
  box.hidden = false;
  box.replaceChildren(
    "📖 ייסרק: " + book.title,
    book.origin ? el("span", { class: "bs-org" }, "  · " + book.origin) : null,
    book.path ? el("span", { class: "bs-path" }, book.path) : null
  );
}

async function bsSearch() {
  // bump FIRST: an early return must still invalidate any in-flight request,
  // or a response for a query the user already cleared repaints the list
  const seq = ++BS.seq;
  const src = bsSource();
  if (src === "file") return;
  const q = $("#bsSearch").value.trim();
  const box = $("#bsResults");
  if (!q) { box.replaceChildren(el("div", { class: "empty" }, "הקלד כדי לחפש ספר…")); return; }
  box.replaceChildren(el("div", { class: "empty" }, "מחפש…"));
  // search the corpus the scan will actually use — the paths in the full-scan
  // panel above, not whatever run_config.json happens to hold
  const corpus = collectScanRequest().corpus || {};
  let data;
  try {
    data = await api("/api/scan/books?source=" + encodeURIComponent(src) +
                     "&q=" + encodeURIComponent(q) + "&limit=100" +
                     "&library_dir=" + encodeURIComponent(corpus.library_dir || "") +
                     "&db_path=" + encodeURIComponent(corpus.db_path || ""));
  } catch (e) {
    if (seq === BS.seq) box.replaceChildren(el("div", { class: "empty" }, "החיפוש נכשל: " + e.message));
    return;
  }
  if (seq !== BS.seq) return;
  const books = (data && data.books) || [];
  if (!books.length) { box.replaceChildren(el("div", { class: "empty" }, "לא נמצאו ספרים מתאימים")); return; }
  box.replaceChildren(...books.map(b => {
    // several library books can share a filename (three different ספר רשות),
    // so show the folder that tells them apart
    const dir = b.path ? b.path.replace(/\/[^/]*$/, "") : "";
    const row = el("div", { class: "bs-item", title: b.path || b.title },
      el("span", { class: "bs-ttl" }, b.title,
        dir ? el("span", { class: "bs-path" }, dir) : null),
      el("span", { class: "bs-org" }, b.origin || ""));
    row.addEventListener("click", () => {
      $$("#bsResults .bs-item").forEach(x => x.classList.remove("on"));
      row.classList.add("on");
      bsSetChosen(b);
    });
    return row;
  }));
}

function bindBookScan() {
  $$("#bsSources input").forEach(r => r.addEventListener("change", () => {
    bsUpdateRows();
    bsSetChosen(null);
    $("#bsResults").replaceChildren(el("div", { class: "empty" }, "הקלד כדי לחפש ספר…"));
    bsSearch();
  }));
  $("#bsSearch").addEventListener("input", debounce(bsSearch, 300));
  bsUpdateRows();

  $("#bsStart").addEventListener("click", async () => {
    const src = bsSource();
    let key = null, label = "";
    if (src === "file") {
      key = $("#bsFilePath").value.trim();
      label = key;
      if (!key) { toast("יש להזין נתיב לקובץ", "err"); return; }
    } else {
      if (!BS.chosen) { toast("יש לבחור ספר מהרשימה", "err"); return; }
      key = BS.chosen.key;
      label = BS.chosen.title;
    }
    const verify = $("#bsVerifyCtx").checked;
    if (!confirm("לסרוק את «" + label + "»?\n\n" +
                 "הסריקה מתבססת על המילון הקיים ואורכת שניות." +
                 (verify ? "\n\n⚠ סימנת «אימות הקשר מול כל המאגר» — הסריקה " +
                           "תימשך כ־10 דקות במקום שניות." : "") +
                 "\n\nממצאים קודמים של ספר זה יוחלפו; ההחלטות שלך עליהם יישמרו.")) return;
    const req = collectScanRequest();
    try {
      const r = await api("/api/scan/book", {
        method: "POST",
        body: { source: src, book: key, verify_ctx: verify,
                config: req.config, corpus: req.corpus },
      });
      toast((r && r.message) || "סריקת הספר הופעלה", "ok");
      $("#scanRunSection").setAttribute("open", "");
      renderScanStatus((r && r.status) || { state: "running", log_tail: [] });
    } catch (e) {
      toast("הפעלת סריקת הספר נכשלה: " + e.message, "err", 9000);
    }
  });
}

/* Same refresh as afterDataChanged, minus closing the modal: a book scan is
   short and usually repeated for the next book, so the panel stays open. */
async function reloadAfterBookScan() {
  S.lastActions = [];
  S.sel.clear();
  S.cardStale = true;
  try { S.meta = await api("/api/meta"); buildSidebar(); } catch (e) {}
  loadBooks();
  refreshCurrentView();
  updateProgress();
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
    case "fixer": loadFixerBooks().then(loadFixDoc); break;
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
    const cur = currentFixRow();
    if ((ev.ctrlKey || ev.metaKey) && (ev.code === "KeyS" || ev.key === "s")) {
      ev.preventDefault();                       // beats the browser's Save
      applyFixes();
    } else if (ev.key === "Enter" || keyIs(ev, "fixedBook")) {
      ev.preventDefault(); if (cur) markFixed(cur);
    } else if (ev.code === "ArrowDown") {
      ev.preventDefault(); moveFixSelection(1);
    } else if (ev.code === "ArrowUp") {
      ev.preventDefault(); moveFixSelection(-1);
    } else if (ev.code === "Home") {
      ev.preventDefault(); { const v = visibleFixRows(); if (v.length) selectFixRowById(v[0].id); }
    } else if (ev.code === "End") {
      ev.preventDefault(); { const v = visibleFixRows(); if (v.length) selectFixRowById(v[v.length - 1].id); }
    } else if (ev.key === "ס" || ev.code === "KeyX") {
      // toggle THIS correction between plain replace and (תיקון) [שגיאה]
      if (cur) {
        ev.preventDefault();
        S.fixModeOverride.set(cur.id, rowMode(cur) === "bracket" ? "replace" : "bracket");
        renderFixList(false);
      }
    } else if (ev.key === "פ" || ev.code === "KeyP") {
      if (cur && canApply(cur)) {
        ev.preventDefault();
        if (S.fixPicked.has(cur.id)) S.fixPicked.delete(cur.id);
        else S.fixPicked.add(cur.id);
        renderFixList(false);
      }
    } else if (ev.key >= "1" && ev.key <= "6" && !ev.ctrlKey && !ev.altKey) {
      const st = ["approved", "not_error", "unsure", "ignored", "fixed", "pending"][+ev.key - 1];
      if (cur && st) { ev.preventDefault(); fixAct(cur, st); }
    }
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
  $("#fixBook").addEventListener("change", () => { S.fixKey = $("#fixBook").value; writeHash(); loadFixDoc(); });
  $("#fixInclude").addEventListener("change", () => {
    S.fixInclude = $("#fixInclude").checked;
    writeHash();
    loadFixerBooks().then(loadFixDoc);
  });
  for (const b of $$("#fixPickBar .fp-btn")) {
    b.addEventListener("click", () => fixPick(b.dataset.pick));
  }
  $("#fixBookSearch").addEventListener("input", debounce(renderFixBookOptions, 120));
  $("#fixBookSearch").addEventListener("keydown", ev => {
    // Enter on a single match opens it, so searching never needs the mouse
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    const sel = $("#fixBook");
    const opts = [...sel.children].filter(o => o.value);
    if (opts.length === 1) {
      sel.value = opts[0].value;
      S.fixKey = sel.value;
      writeHash();
      loadFixDoc();
    }
  });
  $("#fixView").addEventListener("change", () => {
    S.fixView = $("#fixView").value;
    writeHash();
    renderFixList(true);
    renderFixDoc();
  });
  $("#fixModeReplace").addEventListener("click", () => setFixMode("replace"));
  $("#fixModeBracket").addEventListener("click", () => setFixMode("bracket"));
  $("#fixApply").addEventListener("click", applyFixes);
  $("#fixUndoFile").addEventListener("click", undoFileEdit);
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
