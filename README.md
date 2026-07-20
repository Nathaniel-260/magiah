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

Every finding gets a confidence score; the report is sorted so genuine errors
concentrate at the top and you can stop reviewing when precision drops.

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
| `errors_ranked.csv` | every suspect occurrence: word, error type, suggested correction, confidence rank, context-verification hits, book/source, snippet |
| `space_errors.csv` | extra-space findings |
| `report.db` | the same as a queryable SQLite database |

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
