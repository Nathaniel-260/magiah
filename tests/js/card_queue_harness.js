// Drives magiah/webui/static/cardqueue.js against a live review server.
// Usage: node card_queue_harness.js <base-url> <scenario>
// Prints one JSON object; used by tests/test_review_state.py.
"use strict";
const path = require("path");
const { CardQueue, writeChanged } = require(path.join(__dirname, "..", "..", "magiah", "webui", "static", "cardqueue.js"));

const [base, scenario] = process.argv.slice(2);
let saves = 0, conflicts = 0;

async function get(p) {
  const r = await fetch(base + p);
  if (!r.ok) throw new Error("GET " + p + " -> " + r.status);
  return r.json();
}
async function post(p, body) {
  const r = await fetch(base + p, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) { const e = new Error("POST " + p + " -> " + r.status); e.status = r.status; e.data = data; throw e; }
  return data;
}

const io = {
  fetchPage: (cursor) => get("/api/findings?status=pending&sort=rank&dir=desc&page_size=50&cursor=" + encodeURIComponent(cursor)),
  countRemaining: async () => (await get("/api/findings?status=pending&page_size=1")).total,
  save: async (row, status, opts) => {
    saves++;
    const body = { ids: [row.id], status, expect_status: row.effective_status || "pending" };
    if (opts && opts.scope) body.scope = opts.scope;
    try {
      return await post("/api/status", body);
    } catch (e) {
      if (e.status === 409) conflicts++;
      throw e;
    }
  },
  fetchRows: (ids) => get("/api/findings?status=pending&page_size=500&ids=" + ids.join(",")),
};

async function drain(q, onHead) {
  const visited = new Set();
  for (let guard = 0; guard < 10000; guard++) {
    await q.fill();
    const h = q.head();
    if (!h) break;
    visited.add(h.id);
    await onHead(h);
  }
  return visited;
}

async function main() {
  const q = new CardQueue(io);
  const out = {};
  if (scenario === "approve_all") {
    out.visited = (await drain(q, () => q.act("approved"))).size;
  } else if (scenario === "skip_first") {
    await q.fill();
    const first = q.head().id;
    q.skip();
    out.skipped_returned = false;
    out.visited = (await drain(q, h => {
      if (h.id === first) out.skipped_returned = true;
      return q.act("approved");
    })).size;
  } else if (scenario === "double") {
    await q.fill();
    const before = q.queue.length;
    const second = q.queue[1].id;
    await Promise.all([q.act("approved"), q.act("approved")]);
    out.shifted = before - q.queue.length;
    out.head_is_second = q.head().id === second;
  } else if (scenario === "reopen_behind") {
    await q.fill();
    const first = q.head();
    await q.act("approved");
    // another reviewer re-opens it after this client moved on
    await post("/api/status", { ids: [first.id], status: "pending" });
    out.visited = 1 + (await drain(q, () => q.act("approved"))).size;
  } else if (scenario === "reset_race") {
    // offline: a reset while a page is still loading must not leave two
    // fill loops running for the new queue
    let active = 0, maxc = 0, counting = false;
    const fake = {
      fetchPage: async (cursor) => {
        const mine = counting;
        if (mine) { active++; maxc = Math.max(maxc, active); }
        await new Promise(r => setTimeout(r, 30));
        if (mine) active--;
        const start = cursor ? Number(cursor) : 0;
        const rows = [];
        for (let i = start; i < Math.min(start + 5, 40); i++) rows.push({ id: i + 1 });
        return { rows, next_cursor: start + 5 < 40 ? String(start + 5) : null };
      },
      countRemaining: async () => 0,
      save: async () => ({ updated: 1 }),
    };
    const fq = new CardQueue(fake, { min: 30 });
    const p1 = fq.fill();
    await new Promise(r => setTimeout(r, 5));
    fq.reset();
    counting = true;
    const p2 = fq.fill();
    await p1;
    const p3 = fq.fill();
    await Promise.all([p2, p3]);
    const ids = fq.queue.map(r => r.id);
    out.max_concurrent = maxc;
    out.dupes = ids.length - new Set(ids).size;
    process.stdout.write(JSON.stringify(out));
    return;
  } else if (scenario === "restore_dup") {
    await q.fill();
    const h = q.head();
    q.restore(Object.assign({}, h));
    out.copies = q.queue.filter(r => r.id === h.id).length;
  } else if (scenario === "rule_settles") {
    // "not an error everywhere" on the first card: the word's other cards
    // leave the queue, and the rest of the session meets no conflict
    await q.fill();
    const word = q.head().word;
    out.word_cards_before = q.queue.filter(r => r.word === word).length;
    q.skip();                       // one of them waits in the skip pile
    const head = q.head();
    await q.act("not_error", { scope: head.word === word ? "word" : undefined });
    out.word_cards_after = q.queue.concat(q.skipped).filter(r => r.word === word).length;
    out.visited = (await drain(q, () => q.act("approved"))).size;
  } else if (scenario === "rule_elsewhere") {
    // another window sets a word rule; this queue still holds the word's
    // cards and acts on one: a 409 that names the rule, and the card goes
    await q.fill();
    const head = q.head();
    const other = q.queue.find(r => r.word === head.word && r.id !== head.id);
    await post("/api/status", { ids: [other.id], status: "not_error", scope: "word" });
    try {
      await q.act("approved");
      out.error = null;
    } catch (e) {
      out.error = e.status;
      out.code = e.data.code;
      out.cause = e.data.cause[String(head.id)];
      out.message = e.data.error;
    }
    out.head_left = !q.queue.some(r => r.id === head.id);
    out.visited = (await drain(q, () => q.act("approved"))).size;
  } else if (scenario === "noop_changed") {
    out.first = writeChanged(await post("/api/status", { ids: [1], status: "approved" }));
    out.second = writeChanged(await post("/api/status", { ids: [1], status: "approved" }));
  } else {
    throw new Error("unknown scenario " + scenario);
  }
  out.saves = saves;
  out.conflicts = conflicts;
  out.state = q.state();
  out.remaining = q.remaining;
  process.stdout.write(JSON.stringify(out));
}

main().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
