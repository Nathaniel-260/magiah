/* Card-queue state machine — no DOM, so it is tested directly against the
 * server (tests/test_review_state.py drives it through node).
 *
 * io.fetchPage(cursor, recheck) -> {rows, next_cursor}  keyset page; recheck
 *     is true on a final pass that should cover what is still open
 * io.countRemaining() -> number   open findings in the filter, per the SERVER
 * io.save(row, status, opts)      persists one decision (rejects on failure)
 * io.fetchRows(ids, recheck) -> {rows}  (optional) those of `ids` still in
 *     the filter, as the server has them now
 *
 * Paging is by server cursor, never by page number: a decision removes the
 * row from a status-filtered set, so OFFSET paging would silently skip rows.
 * A skipped card goes to a separate pile that comes back once the main queue
 * is exhausted; it is never counted as done. "Finished" is decided by the
 * server's count, not by the local queue being empty. */
(function (root) {
  "use strict";

  // a decision with one of these scopes decides other findings of the word
  const RULE_SCOPES = new Set(["word", "book", "replacement"]);

  class CardQueue {
    constructor(io, opts) {
      this.io = io;
      this.min = (opts && opts.min) || 10;
      this.gen = 0;
      this.reset();
    }

    reset() {
      this.gen++;
      this.queue = [];
      this.seen = new Set();
      this.done = new Set();     // ids decided in this session
      this.skipped = [];         // "later" pile, explicit and never lost
      this.cursor = "";
      this.exhausted = false;
      this.recheck = false;
      this.inflight = null;
      this.finished = false;
      this.remaining = null;     // server count once a pass is complete
      this._loading = null;
    }

    head() { return this.queue[0] || null; }
    loading() { return !!this._loading; }
    pendingCount() { return this.queue.length + this.skipped.length; }

    /* "loading" | "active" | "done" (server: nothing open) |
     * "remaining" (server still counts open findings this queue cannot show) */
    state() {
      if (this.head()) return "active";
      if (!this.finished) return "loading";
      return this.remaining ? "remaining" : "done";
    }

    fill() {
      if (!this._loading) {
        const p = this._fill().finally(() => {
          // a reset() may already have started a newer load: keep that one
          if (this._loading === p) this._loading = null;
        });
        this._loading = p;
      }
      return this._loading;
    }

    async _fill() {
      const gen = this.gen;
      for (;;) {
        let added = 0;
        while (this.queue.length < this.min && !this.exhausted) {
          const page = await this.io.fetchPage(this.cursor, this.recheck);
          if (gen !== this.gen) return;
          for (const r of (page && page.rows) || []) {
            if (this.seen.has(r.id) || this.done.has(r.id)) continue;
            this.seen.add(r.id);
            this.queue.push(r);
            added++;
          }
          this.cursor = (page && page.next_cursor) || "";
          if (!this.cursor) this.exhausted = true;
        }
        if (this.queue.length) { this.finished = false; return; }
        if (this.skipped.length) {
          this.queue = this.skipped;
          this.skipped = [];
          this.finished = false;
          return;
        }
        const remaining = await this.io.countRemaining();
        if (gen !== this.gen) return;
        this.remaining = remaining;
        // one more pass over whatever the server still counts as open; a
        // pass that brings nothing new ends the session
        if (remaining > 0 && (!this.recheck || added > 0)) {
          this.recheck = true;
          this.cursor = "";
          this.exhausted = false;
          this.seen = new Set();
          continue;
        }
        this.finished = true;
        return;
      }
    }

    /* Decide the head card. Ignored while another decision is in flight, so a
     * double click saves once and advances once. */
    async act(status, opts) {
      const r = this.head();
      if (!r || this.inflight !== null) return { ignored: true };
      this.inflight = r.id;
      opts = opts || {};
      try {
        let res;
        try {
          res = await this.io.save(r, status, opts);
        } catch (e) {
          // the finding moved on (another window, a rule): show what is true
          // now, or let the card go when it left the filter. A rule decided
          // the word's other cards as well.
          if (e && e.status === 409) {
            const cause = e.data && e.data.cause && e.data.cause[String(r.id)];
            await this.settle(RULE_SCOPES.has(cause) ? this.sameWord(r, true) : [r]);
          }
          throw e;
        }
        const i = this.queue.indexOf(r);
        if (i >= 0) this.queue.splice(i, 1);
        this.done.add(r.id);
        // a rule decides the word's other cards too; acting on one of them
        // as if it were still open would only meet a conflict
        if (RULE_SCOPES.has(opts.scope)) await this.settle(this.sameWord(r, false));
        return res;
      } finally {
        this.inflight = null;
      }
    }

    /* The waiting cards of `r`'s word (`r` itself first when `self`). */
    sameWord(r, self) {
      const others = this.queue.concat(this.skipped).filter(c => c !== r && c.word === r.word);
      return self ? [r].concat(others) : others;
    }

    /* Ask the server how `rows` stand now under the queue's filter: a card
     * no longer in it leaves the queue and the skip pile, the others take
     * their current state. Never fails the caller (the cards then stay, and
     * acting on one meets a conflict that names the rule). */
    async settle(rows) {
      if (!rows.length || !this.io.fetchRows) return 0;
      const gen = this.gen;
      let fresh;
      try {
        fresh = await this.io.fetchRows(rows.map(r => r.id), this.recheck);
      } catch (e) {
        return 0;
      }
      if (gen !== this.gen) return 0;
      const list = Array.isArray(fresh) ? fresh : ((fresh && fresh.rows) || []);
      const byId = new Map(list.map(f => [f.id, f]));
      const gone = new Set();
      for (const r of rows) {
        const f = byId.get(r.id);
        if (f) Object.assign(r, f);
        else gone.add(r.id);
      }
      if (gone.size) {
        this.queue = this.queue.filter(r => !gone.has(r.id));
        this.skipped = this.skipped.filter(r => !gone.has(r.id));
      }
      return gone.size;
    }

    skip() {
      if (this.inflight !== null || !this.head()) return false;
      this.skipped.push(this.queue.shift());
      return true;
    }

    /* Undo put this finding back: show it first again. */
    restore(row) {
      this.done.delete(row.id);
      this.skipped = this.skipped.filter(r => r.id !== row.id);
      this.queue = this.queue.filter(r => r.id !== row.id);
      this.queue.unshift(row);
      this.finished = false;
    }
  }

  /* Did a /api/status reply change anything? A no-op write (same decision
   * again) leaves no history entry, so it must not become a client undo
   * step either — otherwise the next undo reverts an older action. */
  function writeChanged(resp) {
    return !!resp && ((resp.updated || 0) + (resp.word_rules || 0)) > 0;
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { CardQueue, writeChanged };
  else { root.CardQueue = CardQueue; root.writeChanged = writeChanged; }
})(typeof window !== "undefined" ? window : this);
