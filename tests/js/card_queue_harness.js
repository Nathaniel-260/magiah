// Drives magiah/webui/static/cardqueue.js against a live review server.
// Usage: node card_queue_harness.js <base-url> <scenario>
// Prints one JSON object; used by tests/test_review_state.py.
"use strict";
const path = require("path");
const { CardQueue, writeChanged } = require(path.join(__dirname, "..", "..", "magiah", "webui", "static", "cardqueue.js"));

const [base, scenario] = process.argv.slice(2);
let saves = 0;

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
  save: async (row, status) => {
    saves++;
    return post("/api/status", { ids: [row.id], status, expect_status: row.effective_status || "pending" });
  },
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
  } else if (scenario === "noop_changed") {
    out.first = writeChanged(await post("/api/status", { ids: [1], status: "approved" }));
    out.second = writeChanged(await post("/api/status", { ids: [1], status: "approved" }));
  } else {
    throw new Error("unknown scenario " + scenario);
  }
  out.saves = saves;
  out.state = q.state();
  out.remaining = q.remaining;
  process.stdout.write(JSON.stringify(out));
}

main().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
