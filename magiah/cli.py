# -*- coding: utf-8 -*-
"""Command-line interface."""
import argparse
import json
import os
import sys

from . import core
from .config import Config
from .corpus import OTZARIA_DB

RUN_CONFIG = 'run_config.json'


def _build_spec(args):
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


def _save_run_config(out_dir, spec, cfg):
    with open(os.path.join(out_dir, RUN_CONFIG), 'w', encoding='utf-8') as f:
        json.dump({'corpus': spec, 'config': cfg.to_dict()}, f,
                  ensure_ascii=False, indent=2)


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
                             'report', 'review', 'all'])
    ap.add_argument('--port', type=int, default=8765,
                    help='review: local server port')
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
    ap.add_argument('--out', default='magiah_out', help='output directory')
    ap.add_argument('--top', type=int, default=0,
                    help='report: export only the N highest-ranked rows')
    ap.add_argument('--whitelist', action='append', metavar='FILE',
                    help='word-list file (one word per line); listed words are '
                         'never flagged. May be given multiple times.')
    tune = ap.add_argument_group('thresholds')
    for f in ('rare_max', 'common_min', 'part_min', 'join_min', 'ed1_ratio',
              'workers', 'n_chunks'):
        tune.add_argument(f'--{f.replace("_", "-")}', type=int, default=None)
    args = ap.parse_args(argv)

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    prev = _load_run_config(out_dir)
    spec = _build_spec(args) or (prev and prev['corpus'])
    if spec is None:
        ap.error('no corpus source: use --otzaria, --sqlite or --textdir')
    cfg = Config.from_dict(prev['config']) if prev else Config()
    for f in ('rare_max', 'common_min', 'part_min', 'join_min', 'ed1_ratio',
              'workers', 'n_chunks'):
        v = getattr(args, f)
        if v is not None:
            setattr(cfg, f, v)
    if args.whitelist:
        cfg.whitelist = tuple(os.path.abspath(p) for p in args.whitelist)
    _save_run_config(out_dir, spec, cfg)

    if args.command in ('lexicon', 'all'):
        core.build_lexicon(spec, cfg, out_dir)
    if args.command == 'calibrate':
        core.calibrate(cfg, out_dir)
    if args.command == 'review':
        from . import review
        review.serve(out_dir, port=args.port)
        return
    if args.command in ('detect', 'all'):
        core.detect(spec, cfg, out_dir)
    if args.command in ('locate', 'all'):
        core.locate(spec, cfg, out_dir)
    if args.command in ('report', 'all'):
        core.report(cfg, out_dir, top=args.top)


if __name__ == '__main__':
    main()
