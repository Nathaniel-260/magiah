# -*- coding: utf-8 -*-
"""Held-out evaluation of the ranking against human review decisions.

Rules and calibration must be judged on data they were not chosen or learned
from. This harness takes the human decisions (ui_review.db: approved/fixed =
a real error; not_error, a word-wide not_error rule or a word-wide rejection
in decisions.db = a false alarm), splits them into two halves BY BOOK (a
book's decisions never straddle both halves), learns the confusion matrix
from the calibration half only and reports precision@k on the held-out half.

The stored score already contains the substitution bonus of the run that
produced it (a learned matrix, or the +2 for a hand-listed confusable pair
when no matrix exists). That bonus is removed first, then two rankings are
compared on the held-out half: ``held_out`` with the default bonus (no
calibration) and ``held_out_calibrated`` with the bonus a matrix learned on
the calibration half would give. If the run itself was calibrated on human
decisions, its candidate choice may already reflect held-out labels; the
result is then marked ``contaminated``.

    python -X utf8 -m magiah.eval --out results
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys

from . import core
from .normalize import CONFUSABLE

DEFAULT_KS = (10, 50, 100, 500)


def _global_rejections(out_dir, con):
    """Words rejected everywhere: word_rules not_error + decisions.db '*'."""
    words = {r[0] for r in con.execute(
        "SELECT word FROM word_rules WHERE status = 'not_error'")}
    dec = os.path.join(out_dir, 'decisions.db')
    if os.path.isfile(dec):
        dcon = sqlite3.connect(dec)
        try:
            words.update(r[0] for r in dcon.execute(
                "SELECT DISTINCT word FROM decisions "
                "WHERE verdict = 'reject' AND unit = '*'"))
        except sqlite3.OperationalError:
            pass
        finally:
            dcon.close()
    return words


def load_labels(out_dir):
    """Labelled findings: dicts with word, suggestion, machine_suggestion,
    errtype, doc, score, rank and label (1 = confirmed error, 0 = rejected).
    A finding without its own review row is labelled by a word-wide
    rejection, as the review UI shows it. Empty when nothing is labelled."""
    rows, _ = core.reviewed_findings(
        out_dir, core.REVIEW_POSITIVE + core.REVIEW_NEGATIVE)
    for r in rows:
        r['label'] = 1 if r['status'] in core.REVIEW_POSITIVE else 0
    path = os.path.join(out_dir, 'ui_review.db')
    if os.path.isfile(path):
        from .textsource import connect_ro
        con = connect_ro(path)
        try:
            words = sorted(_global_rejections(out_dir, con))
            for i in range(0, len(words), 500):
                batch = words[i:i + 500]
                ph = ','.join('?' * len(batch))
                for fid, w, sg, et, doc, rank, score in con.execute(
                        f"SELECT f.id, f.word, f.suggestion, f.errtype, "
                        f"COALESCE(f.doc, ''), f.rank, f.score "
                        f"FROM findings f LEFT JOIN review r "
                        f"ON r.finding_id = f.id WHERE r.finding_id IS NULL "
                        f"AND f.family = 'error' AND f.word IN ({ph})",
                        batch):
                    rows.append({'id': fid, 'word': w, 'suggestion': sg,
                                 'machine_suggestion': sg, 'errtype': et,
                                 'doc': doc, 'rank': rank, 'score': score,
                                 'status': 'not_error', 'label': 0})
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    for r in rows:
        r['rank'] = float(r['rank'] or 0.0)
    return rows


def _half(doc):
    return hashlib.sha1(str(doc).encode('utf-8')).digest()[0] & 1


def split_by_doc(rows):
    """Deterministic ``(calibration, held_out)`` split; disjoint by doc."""
    calib, held = [], []
    for r in rows:
        (held if _half(r['doc']) else calib).append(r)
    return calib, held


def _sub_pair(r):
    if r.get('errtype') != 'edit1_sub':
        return None
    d = core._dld1(r.get('word') or '',
                   r.get('machine_suggestion') or r.get('suggestion') or '')
    return (d[1], d[2]) if d and d[0] == 'sub' else None


def _sub_bonus(pair, learned):
    """The bonus evaluate_word gives a substitution under `learned`: the
    learned weight, or (only when no matrix exists) +2 for a confusable."""
    if pair is None:
        return 0.0
    lc = learned.get(pair[0] + pair[1], 0) if learned else 0
    if lc:
        return core.learned_bonus(lc)
    if not learned and pair in CONFUSABLE:
        return 2.0
    return 0.0


def precision_at(rows, ks, key='rank'):
    """Precision of the top k rows by `key` (k capped at the row count)."""
    order = sorted(rows, key=lambda r: -r[key])
    out = {}
    for k in ks:
        top = order[:k]
        out[k] = (sum(r['label'] for r in top) / len(top)) if top else None
    return out


def _summary(rows, ks, key):
    return {'n': len(rows), 'docs': len({r['doc'] for r in rows}),
            'positives': sum(r['label'] for r in rows),
            'precision_at': precision_at(rows, ks, key)}


def evaluate(rows, ks=DEFAULT_KS, learned_run=None):
    """`learned_run` is the matrix the run that scored `rows` used."""
    calib, held = split_by_doc(rows)
    pairs = core.learn_confusion(
        (r['word'], r['suggestion'])
        for r in core.calibration_rows([r for r in calib if r['label']]))
    for r in held:
        pair = _sub_pair(r)
        base = r['rank'] - _sub_bonus(pair, learned_run or {})
        r['rank_default'] = base + _sub_bonus(pair, {})
        r['rank_calibrated'] = base + _sub_bonus(pair, pairs)
    return {
        'status': 'ok',
        'calibration': {'n': len(calib),
                        'docs': len({r['doc'] for r in calib}),
                        'pairs': dict(pairs)},
        'held_out': _summary(held, ks, 'rank_default'),
        'held_out_calibrated': _summary(held, ks, 'rank_calibrated'),
        'held_out_rows': held,
    }


def run(out_dir, ks=DEFAULT_KS):
    rows = load_labels(out_dir)
    if not rows:
        print('[eval] no human-reviewed findings in ui_review.db - nothing '
              'to evaluate. Review findings in the UI first.', flush=True)
        return {'status': 'no_labels'}
    learned_run, _ = core.load_learned(out_dir)
    rep = evaluate(rows, ks, learned_run)
    rep['contaminated'] = core.calibration_source(out_dir) == 'human_review'
    if rep['contaminated']:
        print('[eval] the current run was calibrated on ALL human decisions; '
              'its candidate choice may reflect held-out labels '
              '(contaminated). Recalibrate on the calibration half only for '
              'a clean measurement.', flush=True)
    if not rep['held_out']['n'] or not rep['calibration']['n']:
        print('[eval] all labelled findings fall in one half (too few '
              'books) - the evaluation is not meaningful.', flush=True)
    printable = {k: v for k, v in rep.items() if k != 'held_out_rows'}
    print(json.dumps(printable, ensure_ascii=False, indent=1), flush=True)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(prog='magiah.eval', description=__doc__)
    ap.add_argument('--out', default='magiah_out')
    ap.add_argument('--k', type=int, action='append',
                    help='precision@k cut-offs (repeatable)')
    args = ap.parse_args(argv)
    run(args.out, tuple(args.k) if args.k else DEFAULT_KS)
    return 0


if __name__ == '__main__':
    sys.exit(main())
