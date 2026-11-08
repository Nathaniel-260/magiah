# -*- coding: utf-8 -*-
"""False-positive statistics: the evaluation harness (per-class precision,
cut-offs, calibration table, ablation, stratified sample) and the
rank signals measured against chance."""
import csv
import os
import shutil
import sqlite3
import tempfile
import unittest

from magiah import core
from magiah import eval as ev


def _row(i, label, errtype='edit1_sub', **kw):
    r = {'doc': f'd{i % 9}', 'word': f'w{i}', 'suggestion': f's{i}',
         'errtype': errtype, 'label': label, 'score': 2.0, 'rank': 2.0,
         'ctx_hits': 0, 'sugg_local': 0, 'book_repeat': 0, 'tanach': 0}
    r.update(kw)
    return r


class RankMirror(unittest.TestCase):
    """core.rank_value is RANK_SQL in Python, term for term."""

    CASES = [
        dict(score=2.0, errtype='edit1_sub', ctx_hits=3, sugg_local=0,
             book_repeat=0, tanach=0),
        dict(score=1.2, errtype='edit1_del', ctx_hits=0, sugg_local=4,
             book_repeat=0, tanach=0),
        dict(score=3.0, errtype='missing_space', ctx_hits=0, sugg_local=12,
             book_repeat=1, tanach=3),
        dict(score=0.5, errtype='spelling_variant', ctx_hits=0,
             sugg_local=0, book_repeat=0, tanach=4),
    ]

    def _sql(self, case):
        con = sqlite3.connect(':memory:')
        cols = list(case)
        con.execute(f'CREATE TABLE t({", ".join(cols)})')
        con.execute(f'INSERT INTO t VALUES({",".join("?" * len(cols))})',
                    [case.get(c) for c in cols])
        return con.execute(f'SELECT {core.RANK_SQL} FROM t').fetchone()[0]

    def test_python_mirror_equals_sql(self):
        for case in self.CASES:
            self.assertAlmostEqual(core.rank_value(case), self._sql(case),
                                   msg=case)

    def test_drop_removes_one_term(self):
        c = self.CASES[2]
        self.assertAlmostEqual(core.rank_value(c) - core.rank_value(
            c, drop=('tanach',)), 4.0)

    def test_webui_copy_is_verbatim(self):
        from magiah.webui import db as uidb
        self.assertEqual(uidb.RANK_SQL, core.RANK_SQL)
        self.assertEqual(uidb.VERIFIED_SQL, core.VERIFIED_SQL)


class Diagnostics(unittest.TestCase):

    def test_by_group_precision_and_recall(self):
        rows = [_row(0, 1, rank=5.0), _row(1, 0, rank=1.0),
                _row(2, 1, 'missing_space', rank=0.5)]
        g = ev.by_group(rows, lambda r: r['errtype'], threshold=2.0)
        self.assertEqual(g['edit1_sub']['precision'], 1.0)
        self.assertEqual(g['edit1_sub']['recall'], 1.0)
        self.assertEqual(g['missing_space']['recall'], 0.0)
        self.assertIsNone(g['missing_space']['precision'])

    def test_evidence_of_prefers_report_column(self):
        self.assertEqual(ev.evidence_of({'evidence': 'tanach'}), 'tanach')
        self.assertEqual(ev.evidence_of({'ctx_hits': 2}), 'context')
        self.assertEqual(ev.evidence_of({'sugg_local': 5}), 'book_local')
        self.assertEqual(ev.evidence_of({}), 'none')

    def test_threshold_report_counts_lost_errors(self):
        rows = [_row(0, 1, rank=5.0), _row(1, 1, rank=1.0),
                _row(2, 0, rank=0.5), _row(3, 0, rank=3.0)]
        t = {d['threshold']: d for d in ev.threshold_report(rows, (2.0,))}
        self.assertEqual(t[2.0]['lost_errors'], 1)
        self.assertEqual(t[2.0]['removed_false_alarms'], 1)
        self.assertEqual(t[2.0]['precision'], 0.5)
        self.assertEqual(t[2.0]['recall'], 0.5)

    def test_reliability_bins(self):
        rows = [_row(i, 1 if i >= 5 else 0, rank=float(i))
                for i in range(10)]
        tab = ev.reliability(rows, bins=2)
        self.assertEqual([b['n'] for b in tab], [5, 5])
        self.assertEqual([b['observed'] for b in tab], [0.0, 1.0])

    def test_ablation_measures_each_term(self):
        # real errors are exactly the context-verified rows
        rows = ([_row(i, 1, ctx_hits=2, score=1.0) for i in range(5)]
                + [_row(i, 0, ctx_hits=0, score=1.2) for i in range(5, 10)])
        ab = ev.ablation(rows, ks=(5,))
        self.assertEqual(ab['full'][5], 1.0)
        self.assertLess(ab['without_ctx'][5]['delta'], 0)
        self.assertEqual(ab['without_tanach'][5]['delta'], 0)

    def test_ablation_takes_any_rank_function(self):
        rows = [_row(i, i % 2, score=float(i)) for i in range(6)]
        ab = ev.ablation(rows, ks=(3,), rank_fn=lambda r, d: -r['score'],
                         terms=['x'])
        self.assertEqual(set(ab), {'full', 'without_x'})

    def test_evaluate_carries_diagnostics(self):
        rows = [_row(i, i % 2, rank=float(i % 4)) for i in range(40)]
        rep = ev.evaluate(rows, ks=(5,))
        d = rep['held_out_diagnostics']
        self.assertIn('edit1_sub', d['by_errtype'])
        self.assertTrue(d['thresholds'] and d['reliability'])
        self.assertIn('without_ctx', d['ablation'])


class StratifiedSample(unittest.TestCase):

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix='magiah_sample_')
        con = sqlite3.connect(os.path.join(self.out, core.REPORT_DB_F))
        con.execute('CREATE TABLE occurrences_full(word, errtype, '
                    'suggestion, score, ctx_hits, sugg_local, book_repeat, '
                    'tanach, source, ref, unit, snippet, origin, doc, '
                    'evidence)')
        rows = []
        for i in range(100):
            rows.append((f'w{i}', 'edit1_sub', 's', float(i % 10), 0, 0, 0,
                         0, 'b', 'r', str(i), 'x', '', 'd', 'none'))
        for i in range(10):
            rows.append((f'm{i}', 'missing_space', 'a b', 4.0, 0, 0, 0, 0,
                         'b', 'r', str(200 + i), 'x', '', 'd', 'none'))
        con.executemany('INSERT INTO occurrences_full VALUES(' +
                        ','.join('?' * 15) + ')', rows)
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_strata_are_class_by_rank_band_with_weights(self):
        rows = ev.stratified_sample(ev.report_rows(self.out), per_stratum=4,
                                    bands=5, seed=3)
        strata = {r['stratum'] for r in rows}
        self.assertEqual(len([s for s in strata
                              if s.startswith('edit1_sub|')]), 5)
        for r in rows:
            if r['errtype'] == 'edit1_sub':
                self.assertEqual(r['stratum_size'], 20)
                self.assertEqual(r['weight'], 5.0)
        again = ev.stratified_sample(ev.report_rows(self.out),
                                     per_stratum=4, bands=5, seed=3)
        self.assertEqual([r['word'] for r in rows],
                         [r['word'] for r in again])

    def test_export_label_and_estimate(self):
        path = os.path.join(self.out, 'sample.csv')
        n = ev.export_sample(self.out, path, per_stratum=2, bands=2, seed=1)
        with open(path, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), n)
        for r in rows:          # the reviewer labels every class-A row
            r['label'] = '1' if r['errtype'] == 'edit1_sub' else '0'
        rows[-1]['label'] = ''  # an unlabelled row is skipped
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        est = ev.sample_estimates(ev.load_sample_labels(path))
        self.assertEqual(est['edit1_sub']['precision'], 1.0)
        self.assertAlmostEqual(est['edit1_sub']['represents'], 100.0)
        self.assertEqual(est['missing_space']['precision'], 0.0)
        self.assertEqual(est['*']['labelled'], n - 1)


if __name__ == '__main__':
    unittest.main()
