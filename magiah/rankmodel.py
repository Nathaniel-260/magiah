# -*- coding: utf-8 -*-
"""An optional calibrated ranking: logistic regression on review labels.

RANK_SQL (:mod:`magiah.core`) is a hand-made sum. Once enough findings have
been reviewed, ``calibrate`` (with ``Config.rank_model``) can instead fit a
logistic model on the same evidence -- the features below -- and the rank
becomes the model's log-odds that a finding is a real error, with the
probability itself in the reports' ``prob`` column.

* **Training** -- the human labels of :func:`magiah.eval.load_labels`
  (approved/fixed = 1, not_error and word-wide rejections = 0), with the
  report.db columns of each labelled occurrence. Plain-Python batch gradient
  descent with L2 on standardised features (:func:`fit`). The labels are
  split by book as in :mod:`magiah.eval`: a model fitted on one half is
  measured on the other (``held_out`` in the provenance), then the saved
  model is fitted on all labels.
* **Fallback** -- fewer than ``Config.rank_model_min`` labels, or fewer than
  :data:`MIN_PER_CLASS` of either class, and no model is written: the
  ranking stays RANK_SQL and ``prob`` stays empty. With today's handful of
  decisions this is the normal case.
* **Provenance** -- ``rank_model.json`` is hashed into calibration_meta.json
  like confusion_learned.json and is used only while the hash matches and
  the source is human review (:func:`load_model`). A calibrate that does not
  fit a model removes the old one.

Every feature is a column expression that SQLite evaluates without optional
math functions, and the same value in Python (:data:`FEATURES`); tests keep
the two equal. Columns a report.db lacks read as NULL (core.rank_source).
"""
import json
import math
import os
import time

MODEL_F = 'rank_model.json'
FORMAT = 1
MIN_PER_CLASS = 10


def _n(r, k):
    v = r.get(k)
    return 0.0 if v is None else float(v)


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _et(r):
    return str(r.get('errtype') or '')


_STRUCT = ('nonfinal_end', 'final_midword', 'lost_quotes')

# name -> (Python value of a row dict, SQL expression over a report row)
FEATURES = {
    'score': (lambda r: _n(r, 'score'), 'COALESCE(score, 0)'),
    'ctx_hit': (lambda r: 1.0 if _n(r, 'ctx_hits') > 0 else 0.0,
                'CASE WHEN ctx_hits > 0 THEN 1.0 ELSE 0.0 END'),
    'ctx_assoc': (lambda r: _clip(_n(r, 'ctx_assoc'), -2.0, 3.0),
                  'MAX(-2.0, MIN(3.0, COALESCE(ctx_assoc, 0)))'),
    'rw_lr': (lambda r: _clip(_n(r, 'rw_lr'), -2.0, 3.0),
              'MAX(-2.0, MIN(3.0, COALESCE(rw_lr, 0)))'),
    'par_vote': (lambda r: _clip(_n(r, 'par_vote'), -2.0, 2.0),
                 'MAX(-2.0, MIN(2.0, COALESCE(par_vote, 0)))'),
    'local3': (lambda r: 1.0 if _n(r, 'sugg_local') >= 3 else 0.0,
               'CASE WHEN sugg_local >= 3 THEN 1.0 ELSE 0.0 END'),
    'local10': (lambda r: 1.0 if _n(r, 'sugg_local') >= 10 else 0.0,
                'CASE WHEN sugg_local >= 10 THEN 1.0 ELSE 0.0 END'),
    'book_repeat': (lambda r: 1.0 if _n(r, 'book_repeat') == 1 else 0.0,
                    'CASE WHEN book_repeat = 1 THEN 1.0 ELSE 0.0 END'),
    'tanach': (lambda r: 1.0 if _n(r, 'tanach') == 3 else 0.0,
               'CASE WHEN tanach = 3 THEN 1.0 ELSE 0.0 END'),
    'edit1': (lambda r: 1.0 if _et(r).startswith('edit1') else 0.0,
              "CASE WHEN errtype LIKE 'edit1%' THEN 1.0 ELSE 0.0 END"),
    'variant': (lambda r: 1.0 if _et(r) == 'spelling_variant' else 0.0,
                "CASE WHEN errtype = 'spelling_variant' THEN 1.0 "
                "ELSE 0.0 END"),
    'split': (lambda r: 1.0 if _et(r) == 'missing_space' else 0.0,
              "CASE WHEN errtype = 'missing_space' THEN 1.0 ELSE 0.0 END"),
    'structural': (lambda r: 1.0 if _et(r) in _STRUCT else 0.0,
                   "CASE WHEN errtype IN ('nonfinal_end', 'final_midword', "
                   "'lost_quotes') THEN 1.0 ELSE 0.0 END"),
    'word_len': (lambda r: min(len(str(r.get('word') or '')), 12) / 12.0,
                 "MIN(LENGTH(COALESCE(word, '')), 12) / 12.0"),
}
FEATURE_NAMES = tuple(FEATURES)


def features(r):
    return [FEATURES[k][0](r) for k in FEATURE_NAMES]


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def fit(rows, l2=1.0, iters=2000, lr=0.5):
    """Logistic regression on ``row['label']`` by batch gradient descent
    with an L2 penalty (`l2`, on the weights, not the bias), on features
    standardised over `rows`. Returns ``{'bias', 'weights'}`` folded back
    to the raw features, so the model is one linear expression."""
    X = [features(r) for r in rows]
    y = [float(r['label']) for r in rows]
    n, d = len(X), len(FEATURE_NAMES)
    mu = [sum(x[j] for x in X) / n for j in range(d)]
    sd = [math.sqrt(sum((x[j] - mu[j]) ** 2 for x in X) / n) or 1.0
          for j in range(d)]
    Z = [[(x[j] - mu[j]) / sd[j] for j in range(d)] for x in X]
    w, b = [0.0] * d, 0.0
    for _ in range(iters):
        gw, gb = [0.0] * d, 0.0
        for z, t in zip(Z, y):
            e = _sigmoid(b + sum(wj * zj for wj, zj in zip(w, z))) - t
            gb += e
            for j in range(d):
                gw[j] += e * z[j]
        b -= lr * gb / n
        w = [wj - lr * (g / n + l2 * wj / n) for wj, g in zip(w, gw)]
    weights = {k: w[j] / sd[j] for j, k in enumerate(FEATURE_NAMES)}
    bias = b - sum(w[j] * mu[j] / sd[j] for j in range(d))
    return {'bias': bias, 'weights': weights}


def logit(model, r):
    return model['bias'] + sum(model['weights'][k] * FEATURES[k][0](r)
                               for k in FEATURE_NAMES)


def prob(model, r):
    return _sigmoid(logit(model, r))


def logit_sql(model):
    """The model's log-odds as an SQL expression over a report row."""
    terms = [repr(float(model['bias']))]
    for k in FEATURE_NAMES:
        w = float(model['weights'][k])
        if w:
            terms.append(f'({w!r}) * ({FEATURES[k][1]})')
    return '(' + ' + '.join(terms) + ')'


def _log_loss(model, rows):
    eps = 1e-12
    return sum(-math.log(max(eps, p if r['label'] else 1 - p))
               for r in rows for p in [prob(model, r)]) / len(rows)


def _precision_at(rows, key, k):
    top = sorted(rows, key=key, reverse=True)[:k]
    return (sum(r['label'] for r in top) / len(top)) if top else None


def train(rows, min_labels, l2=1.0):
    """``(model, info)``: model None (with info['reason']) when the labels
    are too few. `rows`: labelled rows carrying the feature columns."""
    from .eval import split_by_doc
    from .core import rank_value
    pos = sum(1 for r in rows if r['label'])
    neg = len(rows) - pos
    info = {'labels': len(rows), 'positives': pos, 'negatives': neg,
            'min_labels': min_labels, 'min_per_class': MIN_PER_CLASS}
    if len(rows) < max(1, min_labels) or min(pos, neg) < MIN_PER_CLASS:
        info['reason'] = (f'{len(rows)} labels ({pos} errors, {neg} false '
                          f'alarms); a model needs {min_labels} and '
                          f'{MIN_PER_CLASS} of each')
        return None, info
    calib, held = split_by_doc(rows)
    if held and calib and len({r['label'] for r in calib}) == 2:
        m = fit(calib, l2)
        k = max(1, len(held) // 4)
        info['held_out'] = {
            'n': len(held), 'calibration_n': len(calib),
            'log_loss': round(_log_loss(m, held), 4),
            'precision_at': k,
            'model_precision': _precision_at(
                held, lambda r: logit(m, r), k),
            'formula_precision': _precision_at(held, rank_value, k)}
    model = fit(rows, l2)
    return model, info


def save(out_dir, model, info):
    obj = {'format': FORMAT, 'features': list(FEATURE_NAMES),
           'bias': model['bias'], 'weights': model['weights'],
           'trained': info, 'created': time.strftime('%Y-%m-%d %H:%M:%S')}
    with open(os.path.join(out_dir, MODEL_F), 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def remove(out_dir):
    try:
        os.remove(os.path.join(out_dir, MODEL_F))
    except FileNotFoundError:
        pass


def load_model(out_dir):
    """The vouched model of `out_dir` (``{'bias', 'weights'}``), or None:
    no file, no matching hash in calibration_meta.json, a source other than
    human review, or features this version does not compute."""
    from .core import _vouched
    if _vouched(out_dir, MODEL_F) != 'human_review':
        return None
    try:
        with open(os.path.join(out_dir, MODEL_F), encoding='utf-8') as f:
            obj = json.load(f)
        if obj.get('format') != FORMAT \
                or tuple(obj.get('features') or ()) != FEATURE_NAMES:
            return None
        weights = {k: float(obj['weights'][k]) for k in FEATURE_NAMES}
        return {'bias': float(obj['bias']), 'weights': weights}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def rank_sql(out_dir, default):
    """``(expression, model)``: the vouched model's log-odds, or `default`
    (RANK_SQL) and None."""
    model = load_model(out_dir) if out_dir else None
    return (logit_sql(model), model) if model else (default, None)
