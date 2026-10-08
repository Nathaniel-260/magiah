# Magiah (מַגִּיהַּ)

**Corpus-based typo detection for Hebrew and Aramaic texts — no dictionaries, no AI.**

[עברית](README.he.md)

Magiah finds typing and OCR errors in large Hebrew/Aramaic corpora such as the
[Otzaria](https://github.com/Sivan22/otzaria) Torah library (7,000+ books,
400M+ words). It uses **no external dictionaries and no machine learning** —
which is exactly why it works on rabbinic Hebrew, Aramaic, acronyms and
abbreviations that no dictionary covers.

## The idea: the corpus is its own dictionary

In a corpus of hundreds of millions of words, every real word — Hebrew,
Aramaic, or abbreviation — appears many times. A typo is almost always a
**very rare word that is a small perturbation of a very frequent word**.
Magiah exploits a second property of large religious corpora: massive internal
redundancy. The same phrases recur across books, editions and quotations, so a
suspected correction can be **verified against the corpus itself**.

### Error classes detected

| Class | Example | Verification |
|---|---|---|
| **Missing space** | `אתהשמים` → `את השמים` | the split sequence must actually occur *with* spaces elsewhere in the corpus (bigram evidence) |
| **Extra space** | `הימ נו` → `הימנו` | the joined form is frequent while a fragment is rare |
| **Wrong / missing / extra / swapped letter** | `היעמנו` → `הימנו` | Damerau-Levenshtein distance 1 from a word ≥50× more frequent, boosted by a **learned confusion matrix** (see Calibration), then **context-verified**: the corrected word must appear next to the same neighboring words elsewhere |
| **Final letter mid-word** | `שלוםעליכם` | deterministic rule of Hebrew orthography (ם ן ץ ף ך) |
| **Non-final letter at word end** | `אדמ` → `אדם` | the final-form variant must be ≥50× more frequent |
| **Abbreviation that lost its gershayim** | `רמבם` → `רמב"ם` | the quoted form must be a frequent abbreviation in the corpus |
| **Book-specific OCR errors** | ד↔ר confusion throughout one scanned book | per-book OCR profiles learned by calibration allow a sensitized rescan of books with a proven systematic confusion |
| **Spelling variant** (reported separately) | `חבותיו` ↔ `חובותיו` | an extra/missing ו or י is usually ktiv male/chaser variation, not a typo — exported to its own file so *you* decide the policy |
| **Deviation from Tanach** (self-validating) | a verse quoted with one word off | only verified Bible books take part (see *Tanach evidence* below); a quotation must align with one verse position, and the verse reading must be backed by 2+ independent sources — it is stored as an alternative next to the detector's suggestion, and ranks higher only when the two agree |

### Tanach evidence

* **Which texts are editions.** A book counts only when it sits under
  תנ"ך → תורה/נביאים/כתובים, its title is one of the 39 books, and its
  lines carry `<book>, <chapter>, <verse>` heRefs. `hasTeamim` is not a
  criterion (siddurim, haftara collections and commentaries carry
  cantillation too). Each book row and each `book_version` with its own
  reading is an edition; editions are grouped into **independent sources**
  by provenance (the host of `versionSource`), so several renderings of one
  upload count once.
* **Quotations.** The context must align with a single verse position:
  at least 5 consecutive context tokens (one on each side of the word) and at
  least 2 *distinctive* ones (3+ letters, fewer than 200 occurrences in the
  Tanach), so a run of common words cannot pin a rabbinic phrase to a verse.
  Qere/ketiv slots, a single inner ו/י (male/haser) and readings backed by one
  source are labelled, never counted.
* **Ranking.** `tanach = 3` (rank +4) only when the verse reading equals the
  detector's suggestion. `tanach = 4` means the verse reads otherwise: both
  candidates are kept (`alternatives`, `tanach_reading`) for a human to
  decide, with no bonus. `tanach = 2` marks findings of the old trigram
  heuristic: no bonus, flagged `tanach_legacy` for re-check in the review UI.
* **Edition errors need three independent sources.** A reading is reported as
  an edition error (`tanach_edition_variant`) only when one source stands
  against at least two independent sources that agree. With two sources a
  disagreement is one witness against another and nobody can be outvoted,
  so it is reported as `tanach_edition_unresolved` (rank 0, with a `reason`:
  one_against_one, intra_source, plene, qere_ketiv, …), as are disagreements
  between renderings of the same source. The current Otzaria database has two
  independent Tanach sources (Wikisource's Miqra al pi ha-Masorah and
  tanach.us), so it yields unresolved rows only.

Every finding gets a confidence score; reports are sorted so genuine errors
concentrate at the top, and each class also gets a high-precision
`*_verified.csv` subset (context-verified or repeated in the same book).

### False-positive suppression

Beyond the statistical verification above, Magiah automatically avoids the
common failure modes of naive edit-distance flagging:

* **Stacked prefixes** — `דלאליעזר` (= ד+ל+אליעזר) is legitimate morphology,
  so an "extra first letter" that is a valid prefix (ו ה ב ל מ ש כ ד א) is
  never proposed as an error; likewise an "extra last letter" that is a valid
  inflection suffix, and rare inflections of frequent stems.
* **Abbreviations** — tokens with gershayim (`רמב"ם`) and words followed by a
  geresh (`וכוננ'` = וכוננה) are recognized and skipped.
* **Foreign-language passages** — lines where too many words are uncommon
  (e.g. Judeo-Arabic in תפסיר רס"ג) are skipped entirely (`foreign_ratio`).
* **Names** — a rare word right after שם/רב/רבי/הרב… is likely a proper name
  and is suppressed.
* **Book-local conventions** — a "typo" repeated many times in the same book
  is probably that book's spelling convention; it is down-ranked, not flagged.
* **Optional whitelist** (`--whitelist words.txt`, repeatable) — words listed
  are never flagged. Suppression only: the whitelist never *creates* findings,
  so an incomplete dictionary can't hurt Aramaic or rabbinic vocabulary. Works
  well with the [hspell](http://hspell.ivrix.org.il/)-derived inflected word
  lists from [hebrew_wordlists](https://github.com/eyaler/hebrew_wordlists)
  (AGPL-licensed data, hence downloaded separately rather than bundled).
* **Your review decisions** — words you rejected in the review interface are
  automatically excluded from every future scan.

### Pipeline

```
1. lexicon    count every word's frequency                    (one corpus pass)
2. detect     flag rare words close to frequent ones,
              verify missing-space splits against bigrams     (one corpus pass)
3. locate     find occurrences, detect extra spaces,
              context-verify corrections, check Tanach quotes (two corpus passes)
4. report     ranked CSVs + SQLite report, split per source
5. calibrate  (optional, after a first run) learn a letter-confusion matrix
              and per-book OCR profiles from the verified findings, then
              rerun detect+locate for a sharper second pass
6. review     local web interface for accepting/rejecting findings
```

On a 4-core machine the full pipeline over 417M tokens (5.9M lines, 7,293
books) runs in roughly **45 minutes** using only the Python standard library.

## Installation

Requires Python ≥3.9. Otzaria databases from schema 6 on store each line
as a zstd frame; Python 3.14+ decodes them with the standard library, and
older interpreters install `zstandard` automatically (`pip install .`).

```bash
pip install .
# or just run from the source tree:
python -m magiah --help
```

On Windows always run with `python -X utf8 -m magiah …` to avoid console
encoding problems.

## Usage

**Otzaria library** (auto-detects the `seforim.db` of the folder recorded in
`%APPDATA%\otzaria\library_path.txt`, falling back to
`C:\ProgramData\otzaria\books\seforim.db`). Both the old layout
(`line.content`) and schema 6 (`line_content`, zstd with a stored
dictionary) are read. Alternative editions in `version_line` are not
scanned and are counted as skipped. Every stage writes
`coverage_<stage>.json`, and a stage that could not read some rows
stops with an error instead of reporting a partial pass as complete (see
*Unreadable rows* below):

```bash
magiah all --otzaria --out results
```

Stages can be run separately (`lexicon`, `detect`, `locate`, `report`) — the
corpus source and thresholds are remembered in `results/run_config.json`, so
after tuning thresholds you can rerun from `detect` without recounting the
lexicon.

A failed stage never replaces its previous output, so after a failed run the
folder still holds the last good results. Every run therefore records itself
in `results/run_state/` — when it started, the stage it reached, and how it
ended (done, failed, partial, cancelled). A run that never got to say how it
ended (killed, window closed, power cut) is recognized as *interrupted*: it
holds an OS file lock for as long as it lives. The review UI (`magiah ui`)
reads this record and, until a scan that rebuilds the results succeeds, shows
a warning above the findings that they come from the previous complete scan.
Two pipeline runs on one folder at a time are refused — they would overwrite
each other's files.

**Unreadable rows (`--allow-unread`).** `seforim.db` is used as downloaded,
and it may hold a row that cannot be read — a corrupt zstd frame, or a
`line` without its `line_content` row. By default any such row stops the
stage before it writes anything; the error lists the rows (book, reference,
line id) and says how to go on. If the database cannot be repaired, let the
scan skip them:

```bash
magiah all --otzaria --out results --allow-unread 3
```

* `N` is an absolute number of distinct rows (a row met by several passes
  counts once). The default, `0`, skips none, and there is no "unlimited".
  With more than N unreadable rows the stage stops as before, so a database
  that degrades further is not let through.
* With a folder of text files (`--textdir`, or the library of a hybrid scan)
  an unreadable *file* counts as one row, and the review UI's notice speaks
  of files, not of the database.
* Skipped rows are not scanned: errors in them are not found, and their
  words are missing from the lexicon frequencies. Every output built this way
  is marked partial, never complete: `coverage_<stage>.json` keeps
  `complete: false` and adds `accepted: true`, `allow_unread`, `unread_rows`,
  `unread_units` (the row ids) and `unread_refs` (where the first 20 are); a
  stage built on a partial output of an earlier stage records it under
  `inherited`. `report.db` carries the same record, and the review UI shows a
  persistent notice listing the rows. Such a run still counts as completed:
  its results are the latest, so the warning about a scan that did not
  finish does not appear — unless a later scan fails, and then both do.
* The option applies only to the run that names it and is not remembered in
  `run_config.json`. A later run — `detect`, `report`, a single-book scan —
  refuses partial outputs unless it is given an `--allow-unread` that covers
  as many rows, and its error says which value to give. A complete run (for
  example on a repaired database, without the option) replaces the partial
  outputs and clears every mark.
* A single-book scan follows the same rule: a book with at most N unreadable
  rows is scanned without them and marked partial; with more, it is refused.
* In the UI scan panel the option is the advanced setting «שורות לא קריאות
  מותרות»; it opens at 0 and is cleared after each scan.
* Deleting `coverage_*.json` is not a workaround: on a fresh folder it
  unblocks nothing, and on an old one it passes stale results off as current.

**Second, sharper pass** (recommended):

```bash
magiah calibrate --out results   # learn confusion matrix + OCR book profiles
magiah detect    --out results
magiah locate    --out results
magiah report    --out results
```

**Scan a single book** (seconds instead of an hour):

```bash
magiah book --book-list "Bartenura" --out results  # find a book
magiah book --book 593 --out results               # a seforim.db book id
magiah book --book "books/halacha/x.txt" --out results  # a library relpath
magiah book --book "C:\new book.txt" --out results      # any .txt on disk
```

Scans one book against the **already-built** lexicon — the lexicon is never
rebuilt, which is what makes this cost seconds. Every correction candidate is
still scored against whole-corpus frequencies, so detection quality matches a
full scan.

Findings are merged **additively** into `ui_review.db`. If the book was scanned
before, its rows are replaced in place (no duplicates) and your review
decisions on them are preserved; other books are untouched.

`--book-verify-ctx` additionally verifies each correction against the entire
corpus (more accurate, adds ~10 min). Without it, verification is book-local.

> Requires an existing `lexicon.pkl` — i.e. one prior `magiah lexicon` (or
> `all`) run.

**Review the findings** in your browser:

```bash
magiah review --out results      # opens http://127.0.0.1:8765/
```

Findings are shown one by one (filter by error type and source repository;
order by score or randomly shuffled). Keyboard: **י** accept, **נ** reject
this occurrence, **ד** reject the word everywhere, **ת** type your own
correction, **ע** ignore forever, **space** skip for now. Every decision is
stored immediately in `decisions.db`; the **export** button writes a
`to_send/` folder with one ready-to-send CSV of approved fixes per source
repository, plus `rejected_words.txt` (which future scans use as a
whitelist automatically).

## Running Magiah on *your own* corpus

Magiah is not tied to Otzaria. Any Hebrew/Aramaic text collection works, and
the whole pipeline needs just one thing: **a way to iterate over your lines of
text**. There are three built-in adapters, from simplest to most capable.

### Option 1 — a folder of text files (works for everything)

Put your books in a directory tree of UTF-8 `.txt` files (subfolders are fine,
one book per file is best):

```bash
magiah all --textdir path/to/books --out results
```

Each file is treated as a document, which enables book-local verification
(is this correction already used elsewhere in the same book?) and per-book
OCR profiles.

**If your corpus is in any other format** — Word documents, PDFs with a text
layer, JSON, CSV, HTML — the simplest route is to convert it to a folder of
`.txt` files. For example, from JSON:

```python
import json, os, pathlib
data = json.load(open('mybooks.json', encoding='utf-8'))
out = pathlib.Path('books_txt'); out.mkdir(exist_ok=True)
for book in data:
    safe = ''.join(c if c.isalnum() else '_' for c in book['title'])
    (out / f"{safe}.txt").write_text('\n'.join(book['lines']), encoding='utf-8')
```

Magiah's tokenizer already strips nikud, cantillation marks, HTML tags and
Unicode presentation forms — you don't need to clean the text first.

### Option 2 — any SQLite database

If your corpus is already a SQLite database with one row per line/paragraph:

```bash
magiah all --sqlite mycorpus.db \
    --table line --id-col id --text-col content --doc-col book_id \
    --out results
```

| Flag | Meaning | Default |
|---|---|---|
| `--table` | the table holding your text rows | `line` |
| `--id-col` | integer primary key of that table (used to chunk work across processes and to reference findings) | `id` |
| `--text-col` | the column with the actual text | `content` |
| `--doc-col` | *optional but recommended:* a column grouping rows into books/documents. Enables book-local verification and OCR profiles | none |

Any schema works as long as those columns exist. If your text lives in
multiple tables, create a view:

```sql
CREATE VIEW all_lines AS
  SELECT id, book_id, text AS content FROM mishna
  UNION ALL
  SELECT id + 1000000, book_id, text FROM talmud;
```

then `--table all_lines`.

### Option 3 — the Otzaria preset

`--otzaria` is just a preset of Option 2 (`table=line, id-col=id,
text-col=content, doc-col=bookId`) plus report enrichment that joins book
titles, references and source-repository names from Otzaria's schema. Use
`--db path` if your `seforim.db` lives elsewhere.

### Notes for custom corpora

* The corpus source is saved in `results/run_config.json` after the first
  command — subsequent stages don't need the flags again.
* **Corpus size matters.** The statistics need volume: below ~5M words,
  raise thresholds (`--common-min 10 --ed1-ratio 20`) and expect lower
  precision; below ~1M words the "corpus as dictionary" premise gets weak —
  consider adding more text of the same genre to the corpus (findings are
  reported per book anyway, so extra background text costs nothing).
* The review interface and per-type reports work identically for every
  adapter. Otzaria-specific extras (per-source-repository folders, heRef
  references) simply stay empty for other corpora.

### Outputs (in the `--out` directory)

| File | Contents |
|---|---|
| `errors_<type>.csv` | one ranked CSV per error class: word, suggested correction, the aligned verse's reading (`tanach_reading`, when 2+ sources back it), confidence rank, context-verification hits, book, snippet |
| `errors_<type>_verified.csv` | high-precision subset (context-verified or correction already used in the same book) |
| `spelling_variants.csv` | ktiv male/chaser ו/י differences — policy decisions, not typos |
| `space_errors.csv` | extra-space findings |
| `tanach_matches.csv` / `tanach_edition_errors.csv` | quotations confirmed by 2+ independent sources / edition disagreements (`evidence` JSON: variant vs. unresolved, witnesses) |
| `by_source/<origin>/…` | the same reports split per source repository (Otzaria corpora) |
| `report.db` | everything as a queryable SQLite database |
| `coverage_<stage>.json` | what each stage actually read — a partial read stops the stage and is recorded here; for a partial output accepted under `--allow-unread` also `accepted`, the rows not read and where they are |
| `run_state/` | how the latest runs ended (`scan.json`, `book.json`) — read by the review UI |
| `to_send/` | written by the review interface: approved fixes per source repository, ready to send upstream |

```sql
-- highest-confidence findings
SELECT * FROM occurrences_full ORDER BY score DESC LIMIT 100;
-- findings in one book
SELECT * FROM occurrences_full WHERE source LIKE '%תוספתא%';
```

### Tuning

All thresholds are CLI flags (see `magiah --help`). The important ones:

| Flag | Default | Meaning |
|---|---|---|
| `--rare-max` | 2 | a word is *suspect* if it occurs ≤N times in the whole corpus. Raise to 3–5 to find more errors at the cost of more false positives |
| `--common-min` | 30 | minimum frequency for a proposed correction |
| `--ed1-ratio` | 50 | how many times more frequent the correction must be |
| `--workers` | 3 | parallel processes |

## Why not a dictionary? Why not AI?

* **Dictionaries** fail on rabbinic Hebrew, Aramaic, Yiddish loanwords,
  acronyms, and the thousand spelling conventions of 1,000 years of printing.
  The corpus's own frequency distribution *is* the right dictionary for the
  corpus.
* **LLMs** over 400M words are slow, expensive, and hallucinate corrections.
  Statistical evidence (frequency ratios + bigram/context verification) is
  reproducible, explainable, and runs on a laptop.

## License

MIT
