# -*- coding: utf-8 -*-
"""Command-line interface."""
import argparse
import json
import os
import sqlite3
import sys

from . import core, runstate
from .config import Config
from .corpus import OTZARIA_DB, check_source
from .corpus_hybrid import DEFAULT_LIBRARY
from .textsource import TextSourceError, db_error_message

RUN_CONFIG = 'run_config.json'
# the stages each pipeline command runs, in order
PIPELINE = {'all': ('lexicon', 'detect', 'locate', 'report')}


# integer Config fields settable from the command line (remembered in
# run_config.json like every setting)
TUNABLE = ('rare_max', 'common_min', 'part_min', 'join_min', 'ed1_ratio',
           'workers', 'n_chunks', 'rw_corpus', 'par_corpus', 'rank_model',
           'rank_model_min', 'extra_min', 'alternatives', 'alt_rerank',
           'swapped_quotes')


def _build_spec(args):
    library = getattr(args, 'library', None)
    if library:
        library = os.path.abspath(library)
        if getattr(args, 'sqlite', None) or getattr(args, 'otzaria', False) \
                or getattr(args, 'db', None):
            # hybrid: repo text files + Sefaria-only books from seforim.db
            return {'type': 'hybrid', 'path': library,
                    'db': args.sqlite or args.db or OTZARIA_DB}
        return {'type': 'library', 'path': library}
    if getattr(args, 'otzaria', False):
        return {'type': 'sqlite', 'path': args.db or OTZARIA_DB,
                'table': 'line', 'id_col': 'id', 'text_col': 'content',
                'preset': 'otzaria'}
    if getattr(args, 'sqlite', None):
        return {'type': 'sqlite', 'path': args.sqlite, 'table': args.table,
                'id_col': args.id_col, 'text_col': args.text_col,
                'doc_col': args.doc_col}
    if getattr(args, 'textdir', None):
        return {'type': 'textdir', 'path': args.textdir}
    return None


def _load_run_config(out_dir):
    p = os.path.join(out_dir, RUN_CONFIG)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return None


def _save_run_config(out_dir, spec, cfg, prev=None):
    new = {'corpus': spec, 'config': cfg.to_run_config()}
    # an unchanged setting is not rewritten: the file's age tells the review
    # UI whether a scan was started after report.db (webui.db
    # .config_root_for_report), and `magiah book` or `report` start none
    if prev is not None and json.dumps(prev, sort_keys=True) == json.dumps(
            new, sort_keys=True):
        return
    with open(os.path.join(out_dir, RUN_CONFIG), 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, indent=2)


def _non_negative(text):
    """argparse type of --allow-unread: a count, never negative."""
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(
            f'expected a non-negative number of rows, got {text!r}')
    return n


def _guess_book_source(key):
    """db id | existing file path | library relpath — in that order."""
    key = str(key).strip()
    if key.isdigit():
        return 'db'
    if os.path.isfile(key):
        return 'file'
    return 'library'


def _run_book_cmd(args, spec, cfg, out_dir):
    """`magiah book` — scan one book, or list the books to choose from."""
    from . import book_scan, book_source

    db_path = spec.get('db') if spec.get('type') != 'sqlite' \
        else spec.get('path')
    library_dir = spec.get('path') if spec.get('type') in ('hybrid',
                                                           'library') else None

    if args.book_list is not None:
        src = args.book_source or 'db'
        try:
            if src == 'db':
                books = book_source.list_db_books(db_path, args.book_list)
            else:
                books = book_source.list_library_books(library_dir,
                                                       args.book_list)
        except book_source.BookNotFound as e:
            print(str(e), file=sys.stderr, flush=True)
            return 1
        for b in books:
            print(f"{b['key']}\t{b['title']}\t{b['origin']}", flush=True)
        print(f'-- {len(books)} books', flush=True)
        return 0

    if not args.book:
        print('יש לציין ספר לסריקה: --book <מזהה/נתיב>  '
              '(או --book-list כדי לראות את הרשימה)',
              file=sys.stderr, flush=True)
        return 1
    source = args.book_source or _guess_book_source(args.book)
    if args.book_real_word:
        cfg.real_word = 1
    if args.book_domain_max is not None:
        cfg.book_domain_max = args.book_domain_max
    from .webui import db as uidb
    # recorded apart from the pipeline's runs: a book scan never touches the
    # full scan's results, so it must neither raise nor clear their warning
    with runstate.BookRun(out_dir, source, args.book,
                          library_dir=library_dir) as run:
        try:
            result = book_scan.scan_book(
                out_dir, source, args.book, cfg=cfg, db_path=db_path,
                library_dir=library_dir, verify_ctx=args.book_verify_ctx,
                spec=spec, progress=lambda s: print(s, flush=True))
            counts = uidb.merge_book_scan(out_dir, result)
        except (book_scan.BookScanError, book_source.BookNotFound,
                uidb.DecisionsLocked, TextSourceError) as e:
            run.fail(str(e))
            print(str(e), file=sys.stderr, flush=True)
            return 1
        run.done(title=counts['title'])
    print(f"[book] «{counts['title']}»: נוספו {counts['added']:,} ממצאים, "
          f"הוחלפו {counts['replaced']:,}, "
          f"נשמרו {counts['preserved']:,} החלטות", flush=True)
    return 0


def _run_extra_cmd(args, out_dir, prev=None):
    """`magiah extra` — build (--name + --from), remove (--remove) or list
    the extra lexicons of the output folder. Needs no corpus."""
    from . import extralex
    min_count = (Config.from_run_config(prev.get('config')) if prev
                 else Config()).extra_min
    try:
        if args.remove:
            extralex.remove(out_dir, args.remove)
            print(f'[extra] המילון הנוסף "{args.remove}" נמחק', flush=True)
            return 0
        if args.name or args.extra_from:
            if not args.name:
                raise extralex.ExtraLexiconError(
                    'יש לתת שם למילון הנוסף: --name <שם>')
            rec = extralex.build(out_dir, args.name, args.extra_from or (),
                                 progress=lambda s: print(s, flush=True))
            print('[extra] ' + extralex.summary(rec, min_count),
                  flush=True)
            print(f'[extra] לשימוש בסריקה: --extra-lexicon {args.name}',
                  flush=True)
            return 0
        names = extralex.list_names(out_dir)
        for name in names:
            print(extralex.summary(extralex.load(out_dir, name),
                                   min_count), flush=True)
        print(f'-- {len(names)} extra lexicons', flush=True)
        return 0
    except extralex.ExtraLexiconError as e:
        print(str(e), file=sys.stderr, flush=True)
        return 1
def _run_guard_cmd(args, prev, out_dir):
    """`magiah guard` -- judge every word a correction changed (magiah.guard).

    Reads the lexicon of --out; writes nothing, not even run_config.json."""
    from . import guard
    if not args.before or not args.after:
        print('יש לציין את שני הקבצים: --before <לפני התיקון> '
              '--after <אחרי התיקון>', file=sys.stderr, flush=True)
        return 1
    cfg = Config.from_run_config(prev['config']) if prev else Config()
    cfg.allow_unread = args.allow_unread
    for f in TUNABLE:
        v = getattr(args, f)
        if v is not None:
            setattr(cfg, f, v)
    try:
        guard.run(out_dir, args.before, args.after, cfg,
                  show_all=args.guard_all)
    except guard.GuardError as e:
        print(str(e), file=sys.stderr, flush=True)
        return 1
    except OSError as e:
        print(f'לא ניתן לקרוא את הקובץ: {e}', file=sys.stderr, flush=True)
        return 1
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog='magiah',
        description='Corpus-based typo detection for Hebrew/Aramaic texts. '
                    'No external dictionaries, no AI — the corpus is its own '
                    'dictionary.')
    ap.add_argument('command',
                    choices=['lexicon', 'calibrate', 'detect', 'locate',
                             'report', 'review', 'ui', 'all', 'book',
                             'extra',
                             'guard'])
    bk = ap.add_argument_group(
        'book: scan a single book against the existing lexicon')
    bk.add_argument('--book', metavar='KEY',
                    help='the book to scan: a seforim.db book id, a path '
                         'relative to the library repo, or a .txt file path '
                         '(see --book-source)')
    bk.add_argument('--book-source', choices=['db', 'library', 'file'],
                    default=None,
                    help='where --book comes from (default: guessed — a '
                         'number is a db id, an existing path is a file, '
                         'anything else a library relpath)')
    bk.add_argument('--book-list', metavar='QUERY', nargs='?', const='',
                    help='list matching books instead of scanning; combine '
                         'with --book-source (default: db)')
    bk.add_argument('--book-verify-ctx', action='store_true',
                    help='verify corrections against the whole corpus '
                         '(slower, matches a full scan more closely)')
    bk.add_argument('--book-real-word', action='store_true',
                    help='also look for one-letter slips that produced '
                         'another existing word, judged by the neighbouring '
                         'words; the corpus counts are made by a '
                         '--book-verify-ctx scan and kept for later scans')
    lx = ap.add_argument_group(
        'lexicon: update the existing lexicon instead of recounting it')
    lxm = lx.add_mutually_exclusive_group()
    lxm.add_argument('--update', action='store_true',
                     help='recount only the books added, changed or removed '
                          'since the last update (the first one counts all '
                          'and keeps per-book counts in lexicon_books.pkl); '
                          'same lexicon as a full count')
    lxm.add_argument('--status', action='store_true',
                     help='say what --update would do now (writes nothing)')
    lxm.add_argument('--rollback', action='store_true',
                     help='swap the lexicon with the one the last --update '
                          'replaced (again: swap back)')
    ex = ap.add_argument_group(
        'extra: an extra lexicon built from your own documents (.txt, '
        '.html, .docx, .epub; not PDF). Its words are never flagged when a '
        'scan selects it with --extra-lexicon. Without --name/--remove: '
        'list them')
    ex.add_argument('--name', metavar='NAME',
                    help='build (or rebuild) the extra lexicon NAME from '
                         '--from')
    ex.add_argument('--from', dest='extra_from', action='append',
                    metavar='PATH',
                    help='a document, or a folder read with its subfolders; '
                         'may be given multiple times')
    ex.add_argument('--remove', metavar='NAME',
                    help='delete the extra lexicon NAME')
    ex.add_argument('--extra-lexicon', action='append', metavar='NAME',
                    help='detect/all/book: words this extra lexicon holds '
                         'at least --extra-min times (default 3) are never '
                         'flagged. May be given multiple times; remembered '
                         'in run_config.json like --whitelist. Off by '
                         'default: in rabbinic text it hides some real '
                         'errors, so select it for the books that need it')
    ex.add_argument('--no-extra-lexicon', action='store_true',
                    help='stop using the remembered extra lexicons')
    bk.add_argument('--book-domain-max', type=float, default=None,
                    metavar='SHARE',
                    help='leave out the rarity-based findings of a book in '
                         'which more than this share of the words (e.g. '
                         '0.05) is unknown to the lexicon: modern Hebrew, '
                         'another language or spelling (default 0: off)')

    gd = ap.add_argument_group(
        'guard: check a corrected text against the lexicon (changes nothing)')
    gd.add_argument('--before', metavar='FILE',
                    help='the text before the correction')
    gd.add_argument('--after', metavar='FILE',
                    help='the same text after the correction')
    gd.add_argument('--guard-all', action='store_true',
                    help='list every change, not only the suspect ones')
    ap.add_argument('--port', type=int, default=None,
                    help='review/ui: local server port '
                         '(default: review 8765, ui 8766)')
    src = ap.add_argument_group('corpus source (remembered in run_config.json)')
    src.add_argument('--otzaria', action='store_true',
                     help=f'use the Otzaria library ({OTZARIA_DB})')
    src.add_argument('--db', help='override the Otzaria database path')
    src.add_argument('--sqlite', metavar='PATH',
                     help='any SQLite database with a text table')
    src.add_argument('--table', default='line')
    src.add_argument('--id-col', default='id')
    src.add_argument('--text-col', default='content')
    src.add_argument('--doc-col', default=None,
                     help='column grouping rows into documents/books '
                          '(enables book-local verification)')
    src.add_argument('--textdir', metavar='DIR',
                     help='directory tree of UTF-8 .txt files')
    src.add_argument('--library', metavar='DIR', nargs='?',
                     const=DEFAULT_LIBRARY, default=None,
                     help='otzaria-library repo clone (one book per .txt); '
                          'combined with --sqlite/--otzaria/--db it becomes '
                          'a hybrid corpus: repo files + Sefaria books from '
                          f'seforim.db (default dir: {DEFAULT_LIBRARY})')
    ap.add_argument('--out', default='magiah_out', help='output directory')
    ap.add_argument('--calibrate-from-machine', action='store_true',
                    help='calibrate: learn from the machine report.db when no '
                         'human-reviewed findings exist (output labelled '
                         'machine_unreviewed)')
    ap.add_argument('--top', type=int, default=0,
                    help='report: export only the N highest-ranked rows')
    ap.add_argument('--whitelist', action='append', metavar='FILE',
                    help='word-list file (one word per line); listed words are '
                         'never flagged. May be given multiple times.')
    ap.add_argument('--allow-unread', type=_non_negative, default=0,
                    metavar='N',
                    help='go on when at most N input rows cannot be read '
                         '(corrupt or missing in the database; with text '
                         'files, each file that cannot be read): they are '
                         'skipped and every output built on them is marked '
                         'partial. Also needed to USE such outputs in a '
                         'later run. Default 0: any unreadable row stops the '
                         'stage. Applies to this run only — never remembered '
                         'in run_config.json.')
    tune = ap.add_argument_group('thresholds')
    for f in TUNABLE:
        tune.add_argument(f'--{f.replace("_", "-")}', type=int, default=None)
    args = ap.parse_args(argv)
    lex_mode = args.update or args.status or args.rollback
    if lex_mode and args.command != 'lexicon':
        ap.error('--update, --status and --rollback go with the lexicon '
                 'command')

    out_dir = os.path.abspath(args.out)
    if args.command == 'guard':         # writes nothing: no --out is created
        return _run_guard_cmd(args, _load_run_config(out_dir), out_dir)
    os.makedirs(out_dir, exist_ok=True)

    if args.command == 'ui':
        from .webui import server as webui_server
        webui_server.serve(out_dir, port=args.port or 8766)
        return

    prev = _load_run_config(out_dir)
    if args.command == 'extra':
        return _run_extra_cmd(args, out_dir, prev)
    spec = _build_spec(args) or (prev and prev['corpus'])
    if spec is None:
        ap.error('no corpus source: use --otzaria, --sqlite or --textdir')
    cfg = Config.from_run_config(prev['config']) if prev else Config()
    cfg.allow_unread = args.allow_unread
    for f in TUNABLE:
        v = getattr(args, f)
        if v is not None:
            setattr(cfg, f, v)
    if args.whitelist:
        cfg.whitelist = tuple(os.path.abspath(p) for p in args.whitelist)
    if args.status or args.rollback:
        # read the lexicon folder only: the remembered settings stay as they
        # are
        from . import lexicon_update
        try:
            if args.status:
                return lexicon_update.print_status(spec, out_dir)
            lexicon_update.recover(out_dir)
            # nothing to roll back to is not a failed lexicon run
            problem = lexicon_update.rollback_problem(out_dir)
            if problem:
                print(problem, file=sys.stderr, flush=True)
                return 1
            with runstate.track_scan(out_dir, ('lexicon',)) as run:
                run.enter('lexicon')
                lexicon_update.rollback(out_dir)
            return 0
        except (core.StageError, TextSourceError) as e:
            print(str(e), file=sys.stderr, flush=True)
            return 1
    if args.no_extra_lexicon:
        cfg.extra_lexicon = ()
    if args.extra_lexicon:
        cfg.extra_lexicon = tuple(args.extra_lexicon)
    if args.extra_lexicon and args.command in ('detect', 'all', 'book'):
        # a name that is not there is refused before it is remembered
        from . import extralex
        try:
            for name in cfg.extra_lexicon:
                extralex.load(out_dir, name)
        except extralex.ExtraLexiconError as e:
            print(str(e), file=sys.stderr, flush=True)
            return 1
    if args.command in ('lexicon', 'detect', 'locate', 'all'):
        # a folder that is not there is refused before run_config.json is
        # rewritten with it, and before any stage starts
        try:
            check_source(spec)
        except TextSourceError as e:
            print(str(e), file=sys.stderr, flush=True)
            return 1
    _save_run_config(out_dir, spec, cfg, prev)

    if args.command == 'review':
        from . import review
        review.serve(out_dir, port=args.port or 8765)
        return

    def lexicon_step():
        if args.update:
            from . import lexicon_update
            lexicon_update.update(spec, cfg, out_dir)
        else:
            from . import lexicon_update
            lexicon_update.recover(out_dir)   # a cut-short swap, if any
            core.build_lexicon(spec, cfg, out_dir)

    steps = {
        'lexicon': lexicon_step,
        'calibrate': lambda: core.calibrate(
            cfg, out_dir, from_machine=args.calibrate_from_machine),
        'detect': lambda: core.detect(spec, cfg, out_dir),
        'locate': lambda: core.locate(spec, cfg, out_dir),
        'report': lambda: core.report(cfg, out_dir, top=args.top),
    }
    try:
        if args.command == 'book':
            return _run_book_cmd(args, spec, cfg, out_dir)
        stages = PIPELINE.get(args.command, (args.command,))
        # the run records itself (run_state/), so a run that fails or never
        # finishes cannot leave the previous results looking current
        with runstate.track_scan(out_dir, stages) as run:
            for stage in stages:
                run.enter(stage)
                steps[stage]()
    except (core.StageError, TextSourceError) as e:
        # a stage was run before its prerequisite, or the database cannot be
        # read (missing, not Otzaria's, no zstd decoder): print the Hebrew
        # guidance (no traceback — this is a user error, not a crash)
        print(str(e), file=sys.stderr, flush=True)
        return 1
    except sqlite3.DatabaseError as e:
        # a database the readers do not wrap (report.db, ui_review.db, a
        # query outside textsource) failed: still Hebrew, exit code 1
        print(db_error_message('', e), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
