# Scan results — Otzaria library, Magiah v0.6

Full output of running Magiah v0.6 over the
[Otzaria](https://github.com/Sivan22/otzaria) Torah library
(7,293 books, ~417M words), scanned July 2026: **327,932 findings** —
**split into one zip per source repository**, so each project's maintainers
can download only the findings for their own texts:

תוצאות סריקה מלאה של ספריית אוצריא (7,293 ספרים, ~417 מיליון מילים),
יולי 2026: **327,932 ממצאים** — **מפוצל ל-ZIP נפרד לכל מאגר מקור**, כדי
שמתחזקי כל פרויקט יוכלו להוריד רק את הממצאים של הטקסטים שלהם:

| Zip | Source repository |
|---|---|
| `scan_v0.6_Sefaria.zip` | Sefaria |
| `scan_v0.6_DictaToOtzaria.zip` | Dicta |
| `scan_v0.6_MoreBooks.zip` | Otzaria — MoreBooks |
| `scan_v0.6_National-LibraryToOtzaria.zip` | National Library of Israel |
| `scan_v0.6_OraytaToOtzaria.zip` | Orayta |
| `scan_v0.6_ToratEmetToOtzaria.zip` | Torat Emet |
| `scan_v0.6_Ben-YehudaToOtzaria.zip` | Project Ben-Yehuda |
| `scan_v0.6_OnYourWayToOtzaria.zip` | On Your Way |
| `scan_v0.6_pninimToOtzaria.zip` / `_tashmaToOtzaria` / `_wikiJewishBooksToOtzaria` / `_wikisourceToOtzaria` | smaller collections |

Each zip holds the same 17 per-error-type CSVs, restricted to that
repository's books:

| CSV in each zip | Contents | Measured precision* |
|---|---|---|
| `errors_missing_space.csv` | missing space (`אתהשמים`) | ~100% |
| `errors_final_midword.csv` | final letter mid-word | ~97% |
| `errors_lost_quotes.csv` | abbreviation lost its gershayim | ~63% |
| `errors_nonfinal_end.csv` | non-final letter at word end | ~63% |
| `errors_edit1_swap.csv` | swapped letters | ~50% |
| `errors_edit1_del.csv` | missing letter | ~47% |
| `errors_ocr_profile.csv` | book-specific OCR confusions | ~43% |
| `errors_edit1_sub.csv` | wrong letter | ~25% |
| `errors_edit1_ins.csv` | extra letter | ~17% |
| `errors_*_verified.csv` | high-precision subsets (context-verified) | ≈2× the above |
| `errors_spelling_variant.csv` | ktiv male/chaser ו/י variants — policy, not typos | — |
| `space_errors.csv` | extra space | — |
| `tanach_edition_errors.csv` | suspected errors in Tanach *editions* (one edition deviating from 3+ agreeing ones) | — |
| `tanach_matches.csv` | quotes silently confirmed against multi-edition Tanach | — |

\* precision graded by random-sample manual review; every CSV is sorted by
confidence score, so precision near the top of each file is much higher than
the file-wide average.

Columns: word, suggested correction, score, context-verification hits,
same-book usage, book title, reference, line id, snippet, source repository.
