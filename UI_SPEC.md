# UI_SPEC — מגיה: ממשק סקירה חדש (webui)

**Status: BINDING SPEC.** All build agents implement exactly this contract. Deviations require architect approval.
This replaces `magiah/review.py` (which stays untouched for backward compat) with a new subpackage `magiah/webui/`.

## 0. Goals (from user feedback — the 5 notes)

1. **Hebrew everywhere** — all column headers, error-type names, statuses, messages, help text in Hebrew. RTL layout.
2. **Book-centric workflow** — the user fixes books one at a time; all errors of a book must be viewable together, and Excel export must be one workbook per מאגר (origin) with multiple sheets.
3. **Single UI over a single DB** — filter by מאגר / ספר / סוג שגיאה / סטטוס / ציון / free text. One unified database, not 10 CSV files.
4. **Explanations** — every column and every error type has a Hebrew explanation (tooltip on header + full help page).
5. **Status tracking per finding** — mark each: טופל / אושר לתיקון / לא שגיאה / דרוש בירור / התעלם / טרם נבדק, with notes, bulk actions, undo, history.

Plus fixes for known defects (§8) and full feature parity with old review UI (§9).

## 1. File layout (new code only — do NOT modify existing modules except cli.py)

```
magiah/magiah/webui/
    __init__.py
    hebrew.py        # single source of truth: all Hebrew labels/mappings/help texts (dict constants)
    db.py            # importer (build ui_review.db from report.db etc.) + query layer + status writes + decisions.db sync
    xlsx.py          # pure-stdlib XLSX writer (zipfile + XML), RTL sheets
    export.py        # Excel-per-origin export + to_send/ legacy-compatible export
    server.py        # ThreadingHTTPServer + JSON API + static file serving
    static/
        index.html   # RTL Hebrew SPA
        app.js
        style.css
```

CLI: add command `ui` to `magiah/cli.py` (argparse choices + dispatch → `webui.server.serve(outdir, port)`). Default port **8766**. Keep `review` command working as-is. `python -X utf8 -m magiah ui --out <dir>` must work when run from the `magiah/` repo dir with `--out ..` pointing at the data dir (`c:\Users\onewr\Downloads\ניפוי שגיאות`).

**Zero third-party dependencies.** Python stdlib only (sqlite3, http.server, zipfile, xml, json, csv). Frontend: vanilla JS/CSS, no CDN (offline machine behind filtering proxy).

## 2. Unified database: `ui_review.db` (created in the out dir, next to report.db)

Built by `db.import_all(outdir)` — idempotent, re-runnable (refresh findings without losing review data). `report.db` stays **read-only**.

```sql
CREATE TABLE findings(
  id INTEGER PRIMARY KEY,
  family TEXT NOT NULL,        -- 'error' | 'extra_space' | 'tanach_error' | 'tanach_match' | 'tokdiag'
  errtype TEXT NOT NULL,       -- canonical key, see §4 (for family='error' the original errtype; else family-derived key)
  word TEXT, suggestion TEXT,
  score REAL, rank REAL,       -- rank = RANK_SQL formula precomputed at import (copy formula from core.py)
  ctx_hits INTEGER, sugg_local INTEGER, book_repeat INTEGER, tanach INTEGER,
  verified INTEGER NOT NULL DEFAULT 0,  -- 1 if book_repeat=0 AND (ctx_hits>0 OR sugg_local>=3)
  origin TEXT, source TEXT, ref TEXT, unit TEXT, doc TEXT, snippet TEXT,
  extra TEXT                   -- JSON for family-specific fields: part1,part2,joined,join_freq | canonical | category,freq
);
CREATE INDEX idx_f_origin ON findings(origin);
CREATE INDEX idx_f_source ON findings(source);
CREATE INDEX idx_f_errtype ON findings(errtype);
CREATE INDEX idx_f_word_unit ON findings(word, unit);
CREATE INDEX idx_f_rank ON findings(rank);

CREATE TABLE review(
  finding_id INTEGER PRIMARY KEY REFERENCES findings(id),
  status TEXT NOT NULL,        -- see §5
  note TEXT,
  custom_suggestion TEXT,      -- user-typed correction (overrides suggestion)
  updated_at TEXT NOT NULL     -- ISO timestamp
);

CREATE TABLE word_rules(       -- "reject everywhere" semantics (old unit='*')
  word TEXT PRIMARY KEY,
  status TEXT NOT NULL,        -- 'not_error' (typically)
  updated_at TEXT NOT NULL
);

CREATE TABLE history(          -- append-only audit log; powers undo
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,        -- 'set_status' | 'word_rule' | 'undo' | 'bulk'
  finding_id INTEGER, word TEXT,
  old_status TEXT, new_status TEXT,
  note TEXT
);

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);  -- import timestamps, source row counts
```

**Import sources → findings:**
- `report.db.occurrences_full` → family `error` (327,907 rows).
- `report.db.space_errors_full` → family `extra_space`, errtype `extra_space`; word=`joined`... actually: word = the erroneous split form `part1 + ' ' + part2`, suggestion = `joined`; extra = {part1,part2,joined,join_freq}; score/rank from join_freq (log-scaled is fine, document it).
- `report.db.tanach_errors_full` → family `tanach_error`, errtype `tanach_edition`; suggestion = `canonical`.
- `report.db.tanach_matches_full` → family `tanach_match`, errtype `tanach_match` (informational).
- `1784548105098-tokdiag_source_he.csv` (if present in outdir) → family `tokdiag`, errtype `tokdiag`; word=term, suggestion, snippet=context, source=book, ref=heRef, unit=line_id; extra={category,freq}. origin: resolve via unit→occurrences match if cheap, else 'לא ידוע'.
- **Migration:** existing `decisions.db` (114 rows): verdict `accept`→status `approved` (with suggestion→custom_suggestion if differs), `reject` with unit='*'→word_rules `not_error`, `reject` per-unit→`not_error`, `ignore`→`ignored`. Match on (word, unit) → finding_id.

**Effective status resolution (query layer):** review row wins; else word_rules for that word; else `pending`.

**decisions.db sync-back (critical for pipeline compat):** every status write ALSO writes the old-format row to `decisions.db` `decisions(word,unit,errtype,verdict,suggestion,source,ref)`: `approved`/`fixed` → verdict `accept`; `not_error` → `reject` (word-rule → unit='*'); `ignored` → `ignore`; `pending`/`unsure` → DELETE the row (so old detect feedback loop keeps working unchanged).

## 3. HTTP API (JSON, UTF-8; server binds 127.0.0.1)

- `GET /` and `/static/*` — SPA files.
- `GET /api/meta` — { origins:[{name, hebrew, count, done_count}], errtypes:[{key, hebrew, explanation, count, pending_count}], statuses:[...], columns:[{key, hebrew, explanation}] }.
- `GET /api/books?origin=&q=` — books (source values) with counts + pending counts, sorted by count desc; q = substring filter.
- `GET /api/findings?origin=&book=&errtype=&status=&verified=&min_rank=&q=&sort=rank|random|source|word&dir=&page=&page_size=` — paginated (default page_size 50, max 500). Returns rows with effective_status + total count. `q` searches word/suggestion/snippet/ref (LIKE).
- `GET /api/finding/<id>` — full row incl. extra JSON, history entries for it.
- `POST /api/status` — body {ids:[...], status, note?, custom_suggestion?, scope?:'occurrence'|'word'} ; scope 'word' writes word_rules for those words. Writes history + decisions.db sync. Returns updated counts.
- `POST /api/undo` — revert last history entry (incl. bulk as one step). Returns what was reverted.
- `GET /api/history?limit=100` — recent actions.
- `GET /api/stats` — progress matrix: per origin × status counts, per errtype × status, per book (top N + filtered).
- `POST /api/export/xlsx` — body {origin?} — export one/all origins; returns file paths + row counts.
- `POST /api/export/fixes` — legacy `to_send/` export (see §7). Returns counts.
- `POST /api/refresh` — re-run importer (after a new pipeline scan).
- Errors: JSON {error: "<Hebrew message>"} with proper HTTP status; **all user-facing messages in Hebrew**.

## 4. Hebrew mappings (hebrew.py — the single source of truth; frontend fetches via /api/meta)

Error types (key → Hebrew name → one-line explanation; full versions in hebrew.py with 2-3 sentence explanations + example):

| key | עברית | הסבר קצר |
|---|---|---|
| edit1_sub | החלפת אות | אות אחת הוחלפה באות אחרת (למשל דגש→רגש) |
| edit1_ins | אות מיותרת | נוספה אות שאינה שייכת למילה |
| edit1_del | אות חסרה | חסרה אות במילה |
| edit1_swap | היפוך אותיות | שתי אותיות סמוכות התחלפו במקומן |
| missing_space | רווח חסר | שתי מילים נדבקו יחד (אתהשמים→את השמים) |
| extra_space | רווח מיותר | מילה אחת נחתכה לשתיים (הימ נו→הימנו) |
| spelling_variant | כתיב מלא/חסר | תוספת/חוסר ו' או י' — לרוב לא שגיאה אלא סגנון כתיב |
| final_midword | אות סופית באמצע מילה | ם/ן/ץ/ף/ך באמצע מילה — רווח חסר או אות שגויה |
| nonfinal_end | אות רגילה בסוף מילה | כ/מ/נ/פ/צ בסוף מילה במקום אות סופית (אדמ→אדם) |
| lost_quotes | גרשיים חסרים | ראשי תיבות שאיבדו את הגרשיים (רמבם→רמב"ם) |
| ocr_profile | שגיאת סריקה (OCR) | בלבול אותיות שיטתי האופייני לספר סרוק מסוים (למשל ד↔ר) |
| tanach_edition | סטיה מנוסח המקרא | מהדורת תנ"ך אחת חורגת מנוסח שעליו מסכימות שאר המהדורות |
| tanach_match | ציטוט פסוק (אומת) | ציטוט פסוק שאומת מול מהדורות התנ"ך — לידיעה בלבד |
| tokdiag | אבחון אסימונים | ממצאי בדיקת תווים: אות לועזית דבוקה, שרידי HTML וכד' (הקטגוריה המדויקת בעמודת פרטים) |

Columns (key → header → tooltip explanation; write full explanations in hebrew.py):

| key | כותרת | הסבר |
|---|---|---|
| word | המילה במקור | המילה כפי שהיא מופיעה בטקסט, החשודה כשגויה |
| suggestion | הצעת תיקון | התיקון המוצע על סמך שכיחות במאגר |
| rank | ציון | ציון ביטחון משוקלל — גבוה יותר = סביר יותר שזו שגיאה אמיתית |
| score | ציון בסיס | ציון גולמי לפני שקלול ראיות |
| ctx_hits | אימות הקשר | בכמה מקומות במאגר מופיע התיקון באותו הקשר מילים (ראיה חזקה) |
| sugg_local | שכיחות בספר | כמה פעמים המילה המתוקנת מופיעה באותו ספר עצמו |
| book_repeat | חזרות בספר | כמה פעמים ה"שגיאה" עצמה חוזרת באותו ספר (חזרות רבות = כנראה מכוון) |
| verified | מאומת | ממצא ברמת ודאות גבוהה (אין חזרות + יש אימות הקשר או שכיחות בספר) |
| origin | מאגר | אוסף המקור (ספריא, דיקטה, אוריתא...) |
| source | ספר | שם הספר |
| ref | מראה מקום | מיקום מדויק בספר |
| unit | מזהה שורה | מספר השורה במסד הנתונים של אוצריא |
| snippet | קטע מהטקסט | הסביבה הטקסטואלית של המילה |
| status | סטטוס | מצב הטיפול בממצא |
| note | הערה | הערה חופשית שלך |
| errtype | סוג שגיאה | — |

Origin (מאגר) display names: Sefaria→ספריא, DictaToOtzaria→דיקטה, National-LibraryToOtzaria→הספריה הלאומית, OraytaToOtzaria→אוריתא, ToratEmetToOtzaria→תורת אמת, OnYourWayToOtzaria→ובלכתך בדרך, wikiJewishBooksToOtzaria→ויקי ספרים יהודיים, tashmaToOtzaria→תא שמע, pninimToOtzaria→פנינים, Ben-YehudaToOtzaria→פרויקט בן־יהודה, wikisourceToOtzaria→ויקיטקסט, MoreBooks→ספרים נוספים. (Keep raw key alongside for export file names.)

## 5. Statuses

| key | עברית | icon | decisions.db verdict |
|---|---|---|---|
| pending | טרם נבדק | ⬜ | (row deleted) |
| approved | אושר — זו שגיאה | ✅ | accept |
| fixed | תוקן בספר | 🔧 | accept |
| not_error | לא שגיאה | ❌ | reject |
| unsure | דרוש בירור | ❓ | (row deleted) |
| ignored | התעלם | 🚫 | ignore |

`fixed` exists so the user can distinguish "confirmed error" from "already fixed in the actual book". Export of fixes (§7) includes approved + fixed (fixed marked in a column).

## 6. Frontend (static/) — RTL Hebrew SPA, world-class but dependency-free

- `<html dir="rtl" lang="he">`; clean modern design (CSS variables, light theme default + dark toggle; system Hebrew fonts: "Segoe UI", "Noto Sans Hebrew", Arial).
- **Layout:** top bar (title מַגִּיהַּ, global search, export buttons, undo, stats link, help link) · right sidebar (filters: מאגר dropdown, ספר searchable list with pending-counts, סוג שגיאה checklist with counts, סטטוס, "מאומתים בלבד" toggle, ציון מינימלי slider, סדר: ציון/אקראי/לפי ספר) · main area with two view modes:
  1. **תצוגת טבלה** (default): paginated sortable table, Hebrew headers with ⓘ tooltip per header, row shows word (red) / suggestion (green) / rank / verified badge / ספר / מראה מקום / snippet (word highlighted) / status chip. Checkbox column + bulk action bar (set status for selected; "החל על המילה בכל מקום" option). Click row → detail drawer: full snippet before/after fix, all fields with Hebrew labels, note field, custom-correction input, history of this finding.
  2. **תצוגת כרטיסים** (like old UI, for fast keyboard triage): one finding, big text, snippet before/after, buttons + keyboard shortcuts **identical to old**: י=אושר, נ=לא שגיאה (מופע זה), ד=לא שגיאה בכל מקום, ת=הקלדת תיקון ידני, ע=התעלם, רווח=דלג, plus new: ב=דרוש בירור, ק=תוקן בספר, Ctrl+Z=ביטול.
  3. **מצב מתקן (Fixer mode)** — a third view for the person actually fixing books (user note #6). Pick a ספר → a worklist of that book's findings whose status is `approved` (default; toggle to include unsure/pending), **sorted by position in the book** (numeric `unit` ascending — reading order), grouped visually by ref. Each row: מראה מקום, המילה במקור, התיקון (custom_suggestion wins), snippet with highlight, one-click **📋 העתק** buttons (copy word / copy fix), and a big **תוקן ✓** button (keyboard: ק or Enter advances to next). Progress bar "תוקנו X מתוך Y בספר זה". Books dropdown in this mode shows only books having approved-not-yet-fixed findings, with remaining counts — so the fixer always knows which book to open next. Endpoint support: `GET /api/fixlist?book=&origin=&statuses=` returns the ordered worklist (server sorts by CAST(unit AS INTEGER)).
- **Progress:** header shows "טופלו X מתוך Y בסינון הנוכחי" + session counter; stats page: table origin×status, errtype×status, per-book progress bars.
- **Help page (עזרה):** full Hebrew documentation rendered from /api/meta explanations: every error type with example, every column, the statuses, keyboard shortcuts, workflow recommendation (verified-first), export explanation. This answers note #4 — user must never wonder what a column means.
- Snippet display must handle RTL text with embedded Latin/digits safely (use `<bdi>`).
- No page reloads; fetch-based; optimistic UI updates with error toast (Hebrew) on failure.

## 7. Exports (export.py + xlsx.py)

**A. Excel per מאגר (the user's primary ask):** `POST /api/export/xlsx` writes to `<outdir>/excel/שגיאות_<hebrew-origin-name>.xlsx`, one workbook per origin:
- Sheet 1 "סיכום": counts per errtype × status, per book top list, export timestamp.
- Sheet 2 "כל השגיאות — לפי ספר": ALL findings of the origin sorted by ספר, then errtype, then rank desc. Columns (Hebrew headers, this order): ספר, מראה מקום, סוג שגיאה (Hebrew name), המילה במקור, הצעת תיקון, ציון, מאומת (כן/ריק), סטטוס (Hebrew), הערה, קטע מהטקסט, מזהה שורה.
- One sheet per errtype (Hebrew sheet name, e.g. "רווח חסר"), same columns minus סוג שגיאה, sorted by ספר then rank.
- All sheets `rightToLeft`, frozen header row, bold header. Sheet names ≤31 chars, sanitized.
- xlsx.py = minimal correct OOXML writer with stdlib zipfile: [Content_Types].xml, _rels, workbook.xml, styles.xml (bold header style), worksheets with **inline strings** (`<is><t>`), `<sheetView rightToLeft="1">`, freeze pane. Must open cleanly in Excel with Hebrew intact. XML-escape everything; strip illegal XML chars (\x00-\x08 etc.) from snippets.
- Large sheets (Sefaria ~194k rows) must export in streaming fashion (write rows incrementally, no giant string concat) and stay under a few hundred MB memory.
- If target file is locked (open in Excel): return Hebrew error naming the locked file — never skip silently (fix for defect §8.3).

**B. Legacy fixes export (compat):** `POST /api/export/fixes` reproduces old `to_send/` exactly: `approved_fixes_all.csv` + `approved_fixes_<origin>.csv` (header `word,suggestion,errtype,book,ref,line_id,origin,snippet`, UTF-8-BOM) from statuses approved+fixed (custom_suggestion wins over suggestion), plus `rejected_words.txt` from not_error word_rules **and** per-occurrence not_error words. Fix defect §8.1: book/ref/origin/snippet come from the findings table directly (no lossy join → no more "Unknown" rows).

## 8. Defects in old tool that MUST be fixed in the new one

1. Export losing book/origin on reject-everywhere / custom accepts (LEFT JOIN nulls) → export from findings table (§7B).
2. Two conflicting approved_fixes formats → new tool only ever writes the 8-column format; ignore legacy root approved_fixes.csv.
3. Locked-file silent skip → explicit Hebrew error listing locked files; nothing silently dropped.
4. No undo / no bulk / no history → §3 undo+history endpoints, §6 bulk bar.
5. `ignore` verdict invisible → it's a first-class status, filterable, shown in stats, syncs verdict `ignore`.

## 9. Feature-parity checklist (QA gate — every item must pass)

- [ ] All 10 errtypes + extra_space + tanach_edition + tanach_match reviewable; counts match report.db (327,907 error occurrences + 291 + 55,111 + 25).
- [ ] Origin filter (12 origins), errtype filter, score & random ordering, RANK formula identical to core.py RANK_SQL.
- [ ] All six old actions incl. keyboard shortcuts י/נ/ד/ת/ע/רווח work in card view.
- [ ] Decisions persist in ui_review.db AND sync to decisions.db in old schema (verify old `magiah review` still sees them; verify detect whitelist feedback intact).
- [ ] Existing 114 decisions migrated correctly (112 accept → approved, rejects → not_error, `*` → word_rules).
- [ ] to_send/ export byte-format-compatible (same header, BOM, per-origin split).
- [ ] Excel export: valid xlsx opening in Excel, RTL sheets, Hebrew headers, one workbook per origin, "כל השגיאות לפי ספר" sheet sorted by book.
- [ ] Every column header shows Hebrew tooltip; help page complete.
- [ ] Status set/bulk/undo/history round-trip via API.
- [ ] Server starts: `python -X utf8 -m magiah ui --out ..` from magiah dir; auto-opens browser; port flag works.
- [ ] Filters combine correctly (origin+book+errtype+status+verified+min_rank+q).
- [ ] Importer idempotent: re-running import_all preserves review/history and refreshes findings.
- [ ] Fixer mode: per-book worklist ordered by unit asc, copy buttons, "תוקן" advances, per-book remaining counts.

## 11. Module interface contracts (for parallel build)

- `xlsx.write_workbook(path, sheets)` — pure, no DB access. `sheets` = list of dicts: `{"name": str, "headers": [str], "rows": iterable of lists (str|int|float|None)}`. Always: rightToLeft views, bold frozen header row, inline strings, XML-escaping + illegal-char stripping, streaming write. Raises `PermissionError` (with path in message) if the target is locked; caller turns that into the Hebrew error.
- `db.py` public functions (server imports these): `import_all(outdir) -> dict counts`, `connect(outdir)`, `get_meta(con)`, `get_books(con, origin, q)`, `query_findings(con, filters, sort, page, page_size) -> (rows, total)`, `get_finding(con, id)`, `set_status(con, outdir, ids, status, note, custom_suggestion, scope) -> counts` (writes history + decisions.db sync), `undo(con, outdir)`, `get_history(con, limit)`, `get_stats(con)`, `get_fixlist(con, book, origin, statuses)`.
- `export.py`: `export_xlsx(con, outdir, origin=None) -> [paths]`, `export_fixes(con, outdir) -> dict counts`.

## 9b. Scan lifecycle — not bound to one scan or to past decisions (user note #7)

The user must be able to re-run the detection pipeline and see fresh results in the UI, and must NOT be locked to previous decisions (the existing 114 decisions in decisions.db were an experiment).

1. **Legacy migration is OPT-IN, not automatic.** `import_all(outdir, migrate_legacy=False)` — the old decisions.db rows are imported into review/word_rules ONLY when explicitly requested (endpoint `POST /api/import_legacy` / UI button "ייבוא החלטות ישנות"). Default first-run state: everything `pending`.
2. **Refresh from a new scan:** `POST /api/refresh` re-runs import_all against the current report.db (after the user re-ran the pipeline). Findings are matched by stable identity (family, word, unit, errtype, ref): statuses of still-existing findings survive; vanished findings are removed; new findings arrive as pending. Response reports: נוספו X, הוסרו Y, נשמרו Z החלטות.
3. **Full reset:** `POST /api/reset` with body {scope: "statuses" | "all"} —
   - Always first writes a timestamped backup of review + word_rules + history to `<outdir>/backups/ui_backup_<ts>.json`.
   - scope "statuses": clears review, word_rules, history in ui_review.db.
   - scope "all": additionally clears decisions.db rows (so the next `detect` run is not influenced by experimental rejects — removes the whitelist feedback binding).
   - UI: in a "ניהול סריקה" panel, buttons: "רענן מסריקה חדשה", "אפס החלטות (נשמר גיבוי)", "אפס הכל כולל decisions.db", "ייבוא החלטות ישנות" — each with a Hebrew double-confirm dialog stating exactly what will happen; reset responses state the backup file path.
4. **Restore:** `POST /api/restore` {file} re-imports a backup JSON (best-effort match by finding identity). UI lists available backups from /backups.
5. decisions.db sync-back (§2) remains, but reset scope "all" is the escape hatch from it.

## 9c. Hybrid corpus — scan the book FILES, not only the DB (user note #8)

**REDEFINED by user 2026-07-22 (supersedes the C:\אוצריא details below, kept for history):** corpus = local clone of https://github.com/Otzaria/otzaria-library (top-level folder per origin: DictaToOtzaria, OraytaToOtzaria, ... — origin key = folder name; exclude non-book folders .claude/.github/ForDB/KSK/docs/library_csv/linker-eval/metadata/send_update/סקריפטים שונות; skip sefariaToOtzaria) **+ Sefaria books ONLY from seforim.db** (source.name='Sefaria'). C:\אוצריא is an old unrelated copy — not used. Clone target: C:\OTZ\otzaria-library (shallow, ~3.1GB). Enumeration rule inside source folders (ערוך vs לא ערוך etc.) decided by the build agent after inspecting the real clone; documented in corpus_hybrid.py.

The user updates books in their local copy of the otzaria-library repo (text files) and wants to re-scan them without waiting for a new seforim.db release. Sefaria books exist only in the DB.

- New corpus adapter `HybridCorpus` (new module `magiah/corpus_hybrid.py` — additive only, no changes to existing adapters/detection logic). **Verified structure facts (explored 2026-07-22):**
  - Text library root: `C:\אוצריא\אוצריא` — 16 category folders, nesting 1–4 levels, one book = one `.txt` (7,355 files, ~3.93GB). UTF-8 no BOM, no empty lines, line 0 = `<h1>Title</h1>`, sections as `<h2>`–`<h4>`, light inline HTML in body lines.
  - **Origin (מאגר) per file:** `C:\אוצריא\אוצריא\אודות התוכנה\SourcesBooks.csv` (7,389 rows: שם הקובץ, נתיב הקובץ, תיקיית המקור, מספר שורות) — primary mapping; fallback `C:\אוצריא\files_manifest.json` (repo path prefix e.g. `sefariaToOtzaria/` → origin). Map source-folder names to the canonical origin keys used in report.db (sefaria→Sefaria, Dicta→DictaToOtzaria, etc.).
  - **Sefaria books DO exist as files** (6,258 txt). The only DB-only origin is `National-LibraryToOtzaria` (271 books, no txt files) + ~small residual of DB books with no matching file.
  - Hybrid = walk library files (skip `אודות התוכנה` folder + non-.txt) **plus** seforim.db lines restricted to books with no file counterpart (match by normalized title: strip Windows-forbidden chars `"'׳״?:*<>|/\.,` ; National-Library entirely). `book.sourceId → source.name` gives DB-side origin.
  - **Line identity:** file line N (0-based) == DB `line.lineIndex` N (verified byte-identical when snapshots match; local edits drift — validate via SourcesBooks.csv line counts or manifest hash). File-based unit: `file:<library-relpath>:<lineno>`; enrich derives ref from the `<h1>`–`<h4>` headers tracked during scan (like the DB's heRef).
  - CLI: `--library DIR` (default `C:\אוצריא\אוצריא`) combinable with `--sqlite/--otzaria` for the DB-only remainder; persisted in run_config.json corpus block (type: "hybrid").
- The UI is agnostic (reads report.db as always), but fixer mode gains: for file-based findings (unit starts with `file:`), show the clickable file path + line number so the fixer opens the exact file directly.

## 9d. Run the scan from the UI with full settings (user note #9)

- New panel "הרצת סריקה" in the UI: corpus selection (library dir path, DB path, hybrid toggle), every Config threshold (rare_max, common_min, part_min, join_min, ed1_ratio, foreign_ratio, workers, ... — each with Hebrew label, explanation and its default), whitelist files, stage selection (הכל / כיול+ריצה שניה / שלב בודד).
- Backend: `POST /api/scan/start` (writes run_config.json, launches `python -X utf8 -m magiah <stages>` as a subprocess with the chosen flags), `GET /api/scan/status` (state + tail of captured log lines, polled by UI), `POST /api/scan/cancel`. Only one scan at a time; UI shows live log + progress; on completion offer "רענן ממצאים" (§9b refresh). Scan settings persist in run_config.json (single source of truth, same file the CLI uses).
- Hebrew explanations for every threshold go in hebrew.py (CONFIG_LABELS dict).

## 10. Non-goals

- No auth/multi-user; localhost only.
- Do not modify detection pipeline (core.py, normalize.py, corpus.py) or old review.py.
- No third-party packages, no network resources in the frontend.
