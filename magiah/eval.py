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

Beyond precision@k the harness reports, on the held-out half:

* ``by_errtype`` / ``by_evidence`` -- precision and recall per error class
  and per kind of support (context, book_local, tanach, ...);
* ``thresholds`` -- for each rank cut-off, how many confirmed errors would
  be lost below it and how many false alarms removed;
* ``reliability`` -- a calibration table: rows binned by rank (or by a
  probability column), the observed share of real errors per bin;
* ``ablation`` -- precision@k with each RANK_SQL term removed in turn
  (:data:`magiah.core.RANK_TERMS`; any ``rank_fn`` can be plugged in).

Selection bias. The labels are what a reviewer happened to look at, and a
reviewer looks at the top of the ranking. Every figure here is conditional
on being reviewed: "recall" is the share of the *reviewed* real errors a
cut-off keeps, not of all errors in the corpus, and errors ranked where
nobody looked are missing from both precision and recall. Precision of the
labelled set overstates precision lower down the ranking. For an unbiased
estimate, export a stratified random sample (``--sample-out``: every error
class x rank band sampled uniformly, each row carrying its stratum weight),
fill its ``label`` column (1 = real error, 0 = not) and read it back with
``--sample-labels``: weighted estimates per class follow.

    python -X utf8 -m magiah.eval --out results
    python -X utf8 -m magiah.eval --out results --sample-out sample.csv
    python -X utf8 -m magiah.eval --out results --sample-labels sample.csv
"""
import argparse
import csv
import hashlib
import random
import json
import os
import sqlite3
import sys

from . import core
from .normalize import CONFUSABLE

DEFAULT_KS = (10, 50, 100, 500)
DEFAULT_THRESHOLDS = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
# per-occurrence evidence columns of the review UI's findings table
FINDING_COLS = ('ctx_hits', 'sugg_local', 'book_repeat', 'tanach', 'unit')


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
        _attach_finding_cols(path, rows)
    for r in rows:
        r['rank'] = float(r['rank'] or 0.0)
    attach_report_features(out_dir, rows)
    return rows


def _attach_finding_cols(path, rows):
    """The evidence columns (ctx_hits, ...) of each labelled finding."""
    from .textsource import connect_ro
    con = connect_ro(path)
    try:
        have = {r[1] for r in con.execute('PRAGMA table_info(findings)')}
        cols = [c for c in FINDING_COLS if c in have]
        if not cols:
            return
        by_id = {r['id']: r for r in rows if r.get('id') is not None}
        ids = sorted(by_id)
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            ph = ','.join('?' * len(batch))
            for fid, *vals in con.execute(
                    f"SELECT id, {', '.join(cols)} FROM findings "
                    f"WHERE id IN ({ph})", batch):
                for c, v in zip(cols, vals):
                    by_id[fid].setdefault(c, v)
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()


def attach_report_features(out_dir, rows):
    """Copy the columns report.db holds for each labelled occurrence (its
    ``evidence`` and every rank feature) onto the row, matched by
    ``(word, unit)``. Rows without a match, or with no report.db, are left
    as they are. Returns the number of rows matched."""
    path = os.path.join(out_dir, core.REPORT_DB_F)
    want = {(r.get('word'), r.get('unit')) for r in rows if r.get('unit')}
    if not want or not os.path.isfile(path):
        return 0
    from .textsource import connect_ro
    con = connect_ro(path)
    con.row_factory = sqlite3.Row
    found = {}
    try:
        words = sorted({w for w, _ in want if w})
        for i in range(0, len(words), 500):
            batch = words[i:i + 500]
            ph = ','.join('?' * len(batch))
            for row in con.execute(f'SELECT * FROM occurrences_full '
                                   f'WHERE word IN ({ph})', batch):
                key = (row['word'], row['unit'])
                if key in want and key not in found:
                    found[key] = dict(row)
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()
    n = 0
    for r in rows:
        rep = found.get((r.get('word'), r.get('unit')))
        if rep is None:
            continue
        n += 1
        for k, v in rep.items():
            if k not in r or r[k] is None:
                r[k] = v
    return n


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


def evidence_of(r):
    """The row's evidence kind: report.db's own column when known, else
    derived from the review UI's columns the way evidence_kind does."""
    ev = r.get('evidence')
    if ev:
        return ev
    if (r.get('tanach') or 0) == 3:
        return 'tanach'
    if (r.get('ctx_hits') or 0) > 0:
        return 'context'
    if (r.get('sugg_local') or 0) >= 3:
        return 'book_local'
    return 'none'


def _ratio(a, b):
    return (a / b) if b else None


def by_group(rows, group, key='rank', threshold=None):
    """Per group (``group(row)``): n, positives, and -- when `threshold`
    is given -- precision and recall of keeping ``row[key] >= threshold``.
    Without a threshold precision is the group's share of real errors."""
    out = {}
    for r in rows:
        g = out.setdefault(group(r), {'n': 0, 'positives': 0,
                                      'kept': 0, 'kept_pos': 0})
        g['n'] += 1
        g['positives'] += r['label']
        if threshold is None or r[key] >= threshold:
            g['kept'] += 1
            g['kept_pos'] += r['label']
    for g in out.values():
        g['precision'] = _ratio(g['kept_pos'], g['kept'])
        g['recall'] = _ratio(g['kept_pos'], g['positives'])
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def threshold_report(rows, thresholds=DEFAULT_THRESHOLDS, key='rank'):
    """For each cut-off: what keeping ``row[key] >= t`` keeps and loses --
    ``lost_errors`` are confirmed errors that fall below the cut."""
    pos = sum(r['label'] for r in rows)
    out = []
    for t in thresholds:
        kept = [r for r in rows if r[key] >= t]
        kp = sum(r['label'] for r in kept)
        out.append({'threshold': t, 'kept': len(kept), 'kept_errors': kp,
                    'lost_errors': pos - kp,
                    'removed_false_alarms': (len(rows) - pos)
                    - (len(kept) - kp),
                    'precision': _ratio(kp, len(kept)),
                    'recall': _ratio(kp, pos)})
    return out


def reliability(rows, key='rank', bins=5):
    """Calibration table: rows sorted by ``row[key]`` into `bins` bins of
    (nearly) equal size; each bin's range, mean value and observed share of
    real errors. For a probability column the mean should match the
    share; for a rank it shows whether higher really means likelier."""
    order = sorted(rows, key=lambda r: r[key])
    n = len(order)
    out = []
    if not n:
        return out
    bins = max(1, min(bins, n))
    for b in range(bins):
        part = order[b * n // bins:(b + 1) * n // bins]
        if not part:
            continue
        out.append({'lo': part[0][key], 'hi': part[-1][key], 'n': len(part),
                    'mean': sum(r[key] for r in part) / len(part),
                    'observed': sum(r['label'] for r in part) / len(part)})
    return out


def ablation(rows, ks=DEFAULT_KS, rank_fn=None, terms=None):
    """precision@k of the full ranking and of the ranking without each
    term. ``rank_fn(row, drop)`` gives a row's rank without the terms in
    `drop` (default :func:`magiah.core.rank_value`, the Python mirror of
    RANK_SQL); `terms` defaults to every term of RANK_TERMS. Rows must
    carry the columns the terms read (see :func:`attach_report_features`).
    """
    rank_fn = rank_fn or core.rank_value
    terms = list(terms if terms is not None else core.RANK_TERMS)
    tmp = [dict(r) for r in rows]
    for r in tmp:
        r['_full'] = rank_fn(r, ())
    full = precision_at(tmp, ks, '_full')
    out = {'full': full}
    for t in terms:
        for r in tmp:
            r['_abl'] = rank_fn(r, (t,))
        p = precision_at(tmp, ks, '_abl')
        out['without_' + t] = {
            k: {'precision': v,
                'delta': (None if v is None or full[k] is None
                          else v - full[k])}
            for k, v in p.items()}
    return out


def diagnostics(rows, ks=DEFAULT_KS, key='rank',
                thresholds=DEFAULT_THRESHOLDS):
    """Every per-row diagnostic of one labelled set."""
    return {
        'by_errtype': by_group(rows, lambda r: r.get('errtype') or '',
                               key),
        'by_evidence': by_group(rows, evidence_of, key),
        'thresholds': threshold_report(rows, thresholds, key),
        'reliability': reliability(rows, key),
        'ablation': ablation(rows, ks),
    }


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
        'held_out_diagnostics': diagnostics(held, ks, 'rank_default'),
        'held_out_rows': held,
    }


# ---------------------------------------------------------------------------
# an unbiased sample for a reviewer to label
# ---------------------------------------------------------------------------

SAMPLE_COLS = ('stratum', 'stratum_size', 'sample_size', 'weight', 'label',
               'word', 'suggestion', 'errtype', 'rank', 'evidence',
               'ctx_hits', 'sugg_local', 'source', 'ref', 'unit', 'doc',
               'snippet')


def report_rows(out_dir):
    """Every error occurrence of report.db as dicts, with its ``rank``."""
    from .textsource import connect_ro
    con = connect_ro(os.path.join(out_dir, core.REPORT_DB_F))
    con.row_factory = sqlite3.Row
    try:
        src = core.rank_source(con, 'occurrences_full')
        rows = [dict(r) for r in con.execute(
            f'SELECT *, {core.RANK_SQL} AS rank FROM {src}')]
    finally:
        con.close()
    return rows


def stratified_sample(rows, per_stratum=20, bands=5, seed=1):
    """A uniform random sample within each stratum = error class x rank
    band (the class's rows cut into `bands` equal-size bands by rank, band
    1 = lowest). Each sampled row carries ``stratum``, ``stratum_size``,
    ``sample_size`` and ``weight`` = rows it stands for (size / sample),
    so labelled sample rows give unbiased estimates (:func:`sample_estimates`).
    Deterministic for a given `seed`."""
    rng = random.Random(seed)
    by_type = {}
    for r in rows:
        by_type.setdefault(r.get('errtype') or '', []).append(r)
    out = []
    for et in sorted(by_type):
        order = sorted(by_type[et], key=lambda r: (r['rank'] or 0.0))
        n = len(order)
        nb = max(1, min(bands, n))
        for b in range(nb):
            part = order[b * n // nb:(b + 1) * n // nb]
            if not part:
                continue
            pick = rng.sample(part, min(per_stratum, len(part)))
            for r in pick:
                out.append({**r, 'stratum': f'{et}|band{b + 1}',
                            'stratum_size': len(part),
                            'sample_size': len(pick),
                            'weight': len(part) / len(pick), 'label': ''})
    return out


def export_sample(out_dir, path, per_stratum=20, bands=5, seed=1):
    rows = stratified_sample(report_rows(out_dir), per_stratum, bands, seed)
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        wr = csv.writer(f)
        wr.writerow(SAMPLE_COLS)
        for r in rows:
            wr.writerow([r.get(c, '') for c in SAMPLE_COLS])
    print(f'[eval] {len(rows):,} sampled rows -> {path} (fill the "label" '
          f'column: 1 = real error, 0 = not; leave empty to skip)',
          flush=True)
    return len(rows)


def load_sample_labels(path):
    """Labelled rows of an exported sample (empty labels skipped)."""
    out = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            lab = (r.get('label') or '').strip()
            if lab not in ('0', '1'):
                continue
            r['label'] = int(lab)
            r['weight'] = float(r.get('weight') or 1.0)
            r['rank'] = float(r.get('rank') or 0.0)
            out.append(r)
    return out


def sample_estimates(rows):
    """Weighted (stratum-weight) estimates from labelled sample rows: per
    error class and overall, the estimated share of real errors and the
    estimated number of real errors among the rows the sample stands for.
    Strata with no labelled row do not count; their rows are reported as
    ``unlabelled_strata``."""
    groups = {}
    for r in rows:
        for g in (r.get('errtype') or '', '*'):
            d = groups.setdefault(g, {'labelled': 0, 'w': 0.0, 'wpos': 0.0})
            d['labelled'] += 1
            d['w'] += r['weight']
            d['wpos'] += r['weight'] * r['label']
    out = {}
    for g, d in sorted(groups.items()):
        out[g] = {'labelled': d['labelled'],
                  'precision': _ratio(d['wpos'], d['w']),
                  'est_errors': round(d['wpos'], 1),
                  'represents': round(d['w'], 1)}
    return out


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
    print('[eval] note: labels cover only what reviewers looked at (mostly '
          'the top of the ranking); recall is relative to the reviewed '
          'errors. For unbiased figures label a --sample-out sample.',
          flush=True)
    printable = {k: v for k, v in rep.items() if k != 'held_out_rows'}
    print(json.dumps(printable, ensure_ascii=False, indent=1), flush=True)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(prog='magiah.eval', description=__doc__)
    ap.add_argument('--out', default='magiah_out')
    ap.add_argument('--k', type=int, action='append',
                    help='precision@k cut-offs (repeatable)')
    ap.add_argument('--sample-out', metavar='CSV',
                    help='export a stratified random sample of report.db '
                         'to label (error class x rank band)')
    ap.add_argument('--sample-n', type=int, default=20,
                    help='rows per stratum of --sample-out (default 20)')
    ap.add_argument('--sample-seed', type=int, default=1)
    ap.add_argument('--sample-labels', metavar='CSV',
                    help='weighted precision estimates from a labelled '
                         '--sample-out file')
    args = ap.parse_args(argv)
    if args.sample_out:
        export_sample(args.out, args.sample_out, args.sample_n,
                      seed=args.sample_seed)
        return 0
    if args.sample_labels:
        est = sample_estimates(load_sample_labels(args.sample_labels))
        print(json.dumps(est, ensure_ascii=False, indent=1), flush=True)
        return 0
    run(args.out, tuple(args.k) if args.k else DEFAULT_KS)
    return 0


if __name__ == '__main__':
    sys.exit(main())
