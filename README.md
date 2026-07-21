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
| **Wrong / missing / extra / swapped letter** | `היעמנו` → `הימנו` | Damerau-Levenshtein distance 1 from a word ≥50× more frequent, boosted for visually-confusable letter pairs (ד/ר, ה/ח, ו/י…), then **context-verified**: the corrected word must appear next to the same neighboring words elsewhere |
| **Final letter mid-word** | `שלוםעליכם` | deterministic rule of Hebrew orthography (ם ן ץ ף ך) |
| **Non-final letter at word end** | `אדמ` → `אדם` | the final-form variant must be ≥50× more frequent |
| **Abbreviation that lost its gershayim** | `רמבם` → `רמב"ם` | the quoted form must be a frequent abbreviation in the corpus |
| **Spelling variant** (reported separately) | `חבותיו` ↔ `חובותיו` | an extra/missing ו or י is usually ktiv male/chaser variation, not a typo — exported to its own file so *you* decide the policy |

Every finding gets a confidence score; the report is sorted so genuine errors
concentrate at the top and you can stop reviewing when precision drops.

### False-positive suppression

Beyond the statistical verification above, Magiah automatically avoids the
common failure modes of naive edit-distance flagging:

* **Stacked prefixes** — `דלאליעזר` (= ד+ל+אליעזר) is legitimate morphology,
  so an "extra first letter" that is a valid prefix (ו ה ב ל מ ש כ ד א) is
  never proposed as an error; likewise an "extra last letter" that is a valid
  inflection suffix (ה ו י ם ן ת).
* **Abbreviations** — tokens with gershayim (`רמב"ם`) and words followed by a
  geresh (`וכוננ'` = וכוננה) are recognized and skipped.
* **Foreign-language passages** — lines where too many words are uncommon
  (e.g. Judeo-Arabic in תפסיר רס"ג) are skipped entirely (`foreign_ratio`).
* **Optional whitelist** (`--whitelist words.txt`, repeatable) — words listed
  are never flagged. Suppression only: the whitelist never *creates* findings,
  so an incomplete dictionary can't hurt Aramaic or rabbinic vocabulary. Works
  well with the [hspell](http://hspell.ivrix.org.il/)-derived inflected word
  lists from [hebrew_wordlists](https://github.com/eyaler/hebrew_wordlists)
  (AGPL-licensed data, hence downloaded separately rather than bundled).

### Pipeline

```
1. lexicon   count every word's frequency                    (one corpus pass)
2. detect    flag rare words close to frequent ones,
             verify missing-space splits against bigrams     (one corpus pass)
3. locate    find occurrences, detect extra spaces,
             context-verify letter-level corrections         (two corpus passes)
4. report    ranked CSV + SQLite report
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

## Usage

**Otzaria library** (auto-detects `C:\ProgramData\otzaria\books\seforim.db`):

```bash
magiah all --otzaria --out results
```

**Any SQLite corpus:**

```bash
magiah all --sqlite mycorpus.db --table line --id-col id --text-col content --out results
```

**A folder of UTF-8 text files:**

```bash
magiah all --textdir path/to/books --out results
```

Stages can be run separately (`lexicon`, `detect`, `locate`, `report`) — the
corpus source and thresholds are remembered in `results/run_config.json`, so
after tuning thresholds you can rerun from `detect` without recounting the
lexicon.

### Outputs (in the `--out` directory)

| File | Contents |
|---|---|
| `errors_<type>.csv` | one ranked CSV per error class (`missing_space`, `edit1_sub`, `edit1_ins`, `edit1_del`, `edit1_swap`, `nonfinal_end`, `final_midword`): word, suggested correction, confidence rank, context-verification hits, book/source, snippet |
| `space_errors.csv` | extra-space findings |
| `report.db` | everything as a queryable SQLite database |

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
