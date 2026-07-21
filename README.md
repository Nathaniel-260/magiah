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
| **Deviation from Tanach** (self-validating) | a verse quoted with one word off | verse trigrams are indexed across ~100 Tanach editions; a reading is only "canonical" if 2+ editions agree, and a single edition deviating from 3+ agreeing editions is itself reported as a suspected *edition* error — the Tanach text is never blindly trusted |

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

Requires Python ≥3.9. No third-party dependencies.

```bash
pip install .
# or just run from the source tree:
python -m magiah --help
```

On Windows always run with `python -X utf8 -m magiah …` to avoid console
encoding problems.

## Usage

**Otzaria library** (auto-detects `C:\ProgramData\otzaria\books\seforim.db`):

```bash
magiah all --otzaria --out results
```

Stages can be run separately (`lexicon`, `detect`, `locate`, `report`) — the
corpus source and thresholds are remembered in `results/run_config.json`, so
after tuning thresholds you can rerun from `detect` without recounting the
lexicon.

**Second, sharper pass** (recommended):

```bash
magiah calibrate --out results   # learn confusion matrix + OCR book profiles
magiah detect    --out results
magiah locate    --out results
magiah report    --out results
```

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
| `errors_<type>.csv` | one ranked CSV per error class: word, suggested correction, confidence rank, context-verification hits, book, snippet |
| `errors_<type>_verified.csv` | high-precision subset (context-verified or correction already used in the same book) |
| `spelling_variants.csv` | ktiv male/chaser ו/י differences — policy decisions, not typos |
| `space_errors.csv` | extra-space findings |
| `tanach_errors.csv` / `tanach_matches.csv` / `tanach_edition_errors.csv` | deviations from multi-edition-verified Tanach text / silent confirmations / suspected errors in the editions themselves |
| `by_source/<origin>/…` | the same reports split per source repository (Otzaria corpora) |
| `report.db` | everything as a queryable SQLite database |
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
