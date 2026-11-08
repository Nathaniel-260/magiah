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
        dict(score=2.0, errtype='edit1_sub', ctx_hits=1, sugg_local=0,
             book_repeat=0, tanach=0, ctx_assoc=0.4),
        dict(score=2.0, errtype='edit1_sub', ctx_hits=40, sugg_local=0,
             book_repeat=0, tanach=0, ctx_assoc=3.7),
        dict(score=2.0, errtype='edit1_ins', ctx_hits=0, sugg_local=0,
             book_repeat=0, tanach=0, ctx_assoc=-0.01),
        dict(score=2.0, errtype='edit1_ins', ctx_hits=0, sugg_local=0,
             book_repeat=0, tanach=0, ctx_assoc=-1.6),
        dict(score=2.0, errtype='edit1_sub', ctx_hits=1, sugg_local=0,
             book_repeat=0, tanach=0, ctx_assoc=0.4, rw_lr=2.2),
        dict(score=2.0, errtype='spelling_variant', ctx_hits=0,
             sugg_local=0, book_repeat=0, tanach=0, rw_lr=-5.0),
        dict(score=2.0, errtype='edit1_sub', ctx_hits=2, sugg_local=0,
             book_repeat=0, tanach=0, ctx_assoc=1.0, rw_lr=0.3,
             par_vote=1.3),
        dict(score=2.0, errtype='edit1_del', ctx_hits=0, sugg_local=0,
             book_repeat=0, tanach=0, par_vote=-3.0),
    ]

    def _sql(self, case):
        con = sqlite3.connect(':memory:')
        cols = list(case)
        con.execute(f'CREATE TABLE t({", ".join(cols)})')
        con.execute(f'INSERT INTO t VALUES({",".join("?" * len(cols))})',
                    [case.get(c) for c in cols])
        return con.execute(f'SELECT {core.RANK_SQL} FROM '
                           f'{core.rank_source(con, "t")}').fetchone()[0]

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


class ContextAgainstChance(unittest.TestCase):
    """(1) ctx_hits measured against the pairs chance would produce."""

    FREQ = {'של': 50000, 'ספר': 4000, 'גדול': 3000, 'רבנו': 20, 'זצל': 10}
    N = 1_000_000

    def _sql(self, sql, **row):
        con = sqlite3.connect(':memory:')
        con.execute(f'CREATE TABLE t({", ".join(row)})')
        con.execute(f'INSERT INTO t VALUES({",".join("?" * len(row))})',
                    list(row.values()))
        return con.execute(f'SELECT {sql} FROM '
                           f'{core.rank_source(con, "t")}').fetchone()[0]

    def test_expectation_is_per_side_product_over_n(self):
        from magiah import rankstats as rs
        e = rs.ctx_expected(self.FREQ, self.N, 'של', 'ספר', 'גדול')
        self.assertAlmostEqual(e, 50000 * 4000 / self.N
                               + 3000 * 4000 / self.N)
        self.assertAlmostEqual(
            rs.ctx_expected(self.FREQ, self.N, '', 'ספר', 'גדול'),
            3000 * 4000 / self.N)
        self.assertEqual(rs.ctx_stats(self.FREQ, self.N, '', 'ספר', '', 0),
                         (None, None))

    def test_association_separates_the_three_cases(self):
        from magiah import rankstats as rs
        rare = rs.ctx_expected(self.FREQ, self.N, 'רבנו', 'זצל', '')
        self.assertLess(rare, 0.01)
        # no hits, chance expected next to none: no information
        self.assertGreater(rs.ctx_association(0, rare), -0.01)
        # no hits where chance expected 200: real negative evidence
        self.assertLess(rs.ctx_association(0, 200.0), -2)
        # one hit where chance expected 200: below chance, not support
        self.assertLess(rs.ctx_association(1, 200.0), 0)
        # 3 hits where chance expected next to none: support
        self.assertGreater(rs.ctx_association(3, rare), 0.8)

    def test_penalty_only_when_absence_is_informative(self):
        no_info = self._sql(core.RANK_SQL, score=2.0, errtype='edit1_sub',
                            ctx_hits=0, sugg_local=0, book_repeat=0,
                            tanach=0, ctx_assoc=-0.004)
        expected = self._sql(core.RANK_SQL, score=2.0, errtype='edit1_sub',
                             ctx_hits=0, sugg_local=0, book_repeat=0,
                             tanach=0, ctx_assoc=-2.6)
        self.assertAlmostEqual(no_info, 2.0 - 0.004)
        self.assertAlmostEqual(expected, 1.0)

    def test_bonus_grows_with_association(self):
        r = [self._sql(core.RANK_SQL, score=0.0, errtype='edit1_sub',
                       ctx_hits=h, sugg_local=0, book_repeat=0, tanach=0,
                       ctx_assoc=a) for h, a in ((1, 0.0), (2, 1.0),
                                                 (50, 2.0), (500, 5.0))]
        self.assertEqual(r, sorted(r))
        self.assertAlmostEqual(r[0], 0.5)
        self.assertAlmostEqual(r[-1], 2.0)

    def test_unmeasured_rows_keep_the_flat_rule(self):
        for hits, want in ((3, 3.5), (0, 1.0)):
            self.assertAlmostEqual(self._sql(
                core.RANK_SQL, score=2.0, errtype='edit1_sub',
                ctx_hits=hits, sugg_local=0, book_repeat=0, tanach=0), want)

    def test_verified_needs_hits_above_chance(self):
        base = dict(book_repeat=0, sugg_local=0, ctx_hits=2)
        self.assertEqual(self._sql(core.VERIFIED_SQL, **base,
                                   ctx_assoc=0.6), 1)
        self.assertEqual(self._sql(core.VERIFIED_SQL, **base,
                                   ctx_assoc=-0.3), 0)
        self.assertEqual(self._sql(core.VERIFIED_SQL, **base), 1)  # legacy
        self.assertEqual(self._sql(core.VERIFIED_SQL, book_repeat=0,
                                   sugg_local=3, ctx_hits=1,
                                   ctx_assoc=-0.3), 1)


class RealWordInCorpusScan(unittest.TestCase):
    """(2) the real-word model weighs original vs correction in context."""

    FREQ = {'ראה': 300, 'ספר': 500, 'ספד': 2, 'גדול': 400, 'הלך': 300}
    N = 100_000

    def _counts(self, pairs=None, tri=None, own=None, doc='a'):
        from magiah import rankstats as rs
        own = own or {}
        return rs.OccCounts(pairs or {}, tri or {}, rs.own_totals(own), own,
                            doc)

    def test_context_seen_with_the_correction_favours_it(self):
        from magiah import rankstats as rs
        c = self._counts(pairs={('ראה', 'ספר'): 40, ('ספר', 'גדול'): 30},
                         tri={('ראה', 'ספר', 'גדול'): 20})
        lr = rs.rw_lr(self.FREQ, self.N, c, 'ראה', 'ספד', 'ספר', 'גדול')
        self.assertGreater(lr, 1.0)

    def test_context_never_seen_with_the_correction_disfavours_it(self):
        from magiah import rankstats as rs
        c = self._counts(own={(('ראה', 'ספד'), 'b'): 1,
                              (('ספד', 'גדול'), 'b'): 1,
                              (('ראה', 'ספד', 'גדול'), 'b'): 1})
        lr = rs.rw_lr(self.FREQ, self.N, c, 'ראה', 'ספד', 'ספר', 'גדול')
        self.assertLess(lr, 0)

    def test_the_originals_own_book_does_not_vouch_for_it(self):
        g = ('ראה', 'ספד', 'גדול')
        own = {(g, 'a'): 2, ((g[0], g[1]), 'a'): 2, ((g[1], g[2]), 'a'): 2}
        c = self._counts(own=own, doc='a')
        self.assertEqual((c.bi('ראה', 'ספד'), c.tri(*g)), (0, 0))
        other = self._counts(own=own, doc='b')
        self.assertEqual(other.tri(*g), 2)

    def test_count_rw_counts_per_book_from_the_middle_word(self):
        from collections import Counter
        from magiah import rankstats as rs
        flagged = {'ספד': (2, 'edit1_sub', 'ספר', 500, 2.0)}
        need = rs.rw_needs([('ספד', 'u', 'a', 'ראה', 'גדול', 's')], flagged,
                           core.uses_context)
        own, s3 = Counter(), Counter()
        rs.count_rw(['ראה', 'ספד', 'גדול'], 'a', need, own, s3)
        rs.count_rw(['ראה', 'ספר', 'גדול', 'ראה', 'ספר', 'גדול'], 'b',
                    need, own, s3)
        self.assertEqual(own[(('ראה', 'ספד', 'גדול'), 'a')], 1)
        self.assertEqual(own[(('ראה', 'ספד'), 'a')], 1)
        self.assertEqual(s3[('ראה', 'ספר', 'גדול')], 2)

    def test_pipeline_flag_is_rank_only(self):
        measured = flag_is_rank_only(self, 'rw_corpus', ('rw_lr',))
        # the slip ספד in "ראה ספד גדול": the corpus reads ספר there
        self.assertTrue(any(r['rw_lr'] > 0 for r in measured))


def flag_is_rank_only(case, flag, cols):
    """Run the detection fixture's library with `flag` off and on: only
    `cols` may differ, they are NULL when off and measured (for context
    types only) when on. Returns the measured rows."""
    import test_detection as td
    tmp = tempfile.mkdtemp(prefix='magiah_flag_')
    try:
        lib = os.path.join(tmp, 'lib')
        td.write_lib(lib, {td.BOOK_A: td.A_LINES, td.BOOK_D: td.D_LINES,
                           f'{td.LIB_TOP}/b.txt': td.B_LINES})
        spec = {'type': 'library', 'path': lib}
        rows = {}
        for on in (0, 1):
            out = os.path.join(tmp, f'out{on}')
            os.makedirs(out)
            td.run_full(spec, td.small_cfg(**{flag: on}), out)
            rows[on] = sorted(td.report_rows(out),
                              key=lambda r: (r['word'], r['unit']))
        case.assertTrue(rows[0])
        keep = [k for k in rows[0][0] if k not in cols]
        case.assertEqual([[r[k] for k in keep] for r in rows[0]],
                         [[r[k] for k in keep] for r in rows[1]])
        case.assertTrue(all(r[c] is None for r in rows[0] for c in cols))
        measured = [r for r in rows[1] if r[cols[0]] is not None]
        case.assertTrue(measured)
        case.assertTrue(all(core.uses_context(r['errtype'])
                            for r in measured))
        return measured
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ParallelPassages(unittest.TestCase):
    """(3) the occurrence's window voted on by its parallel passages."""

    def test_window_from_a_whole_line_snippet(self):
        from magiah import rankstats as rs
        self.assertEqual(rs.par_window('ספד', 'ראה', 'גדול',
                                       'הילד ראה ספד גדול'),
                         (('הילד', 'ראה'), ('גדול',)))
        # one word each side only: too little context
        self.assertIsNone(rs.par_window('ספד', 'ראה', 'גדול',
                                        'ראה ספד גדול'))

    def test_cut_edge_token_is_not_used(self):
        from magiah import rankstats as rs
        # a full pad of characters left of the word: the slice may have
        # cut into the edge token, which is then left out
        cut = 'ב' * (rs.SNIPPET_PAD - 4) + ' ראה ספד גדול טוב'
        self.assertEqual(rs.par_window('ספד', 'ראה', 'גדול', cut),
                         (('ראה',), ('גדול', 'טוב')))
        # fewer: the line starts there, the edge token is whole
        whole = 'ב' * 10 + ' ראה ספד גדול טוב'
        self.assertEqual(rs.par_window('ספד', 'ראה', 'גדול', whole),
                         (('ב' * 10, 'ראה'), ('גדול', 'טוב')))

    def test_ambiguous_snippet_gives_nothing(self):
        from magiah import rankstats as rs
        self.assertIsNone(rs.par_window(
            'ספד', 'ראה', 'גדול', 'אז ראה ספד גדול ואז הוא ראה ספד גדול שם'))

    def test_counts_and_vote(self):
        from collections import Counter
        from magiah import rankstats as rs
        win = (('הילד', 'ראה'), ('גדול',))
        need = rs.par_needs([(win, ('ספד', 'ספר'))])
        own, sugg = Counter(), Counter()
        rs.count_par('הילד ראה ספד גדול'.split(), 'a', need, own, sugg)
        for _ in range(3):
            rs.count_par('הילד ראה ספר גדול מאד'.split(), 'b', need, own,
                         sugg)
        rs.count_par('הילד ראה ספד גדול'.split(), 'c', need, own, sugg)
        ps, pw = rs.par_counts(win, 'ספד', 'ספר', 'a', sugg,
                               rs.own_totals(own), own)
        self.assertEqual((ps, pw), (3, 1))
        self.assertGreater(rs.par_vote(ps, pw), 0)
        self.assertLess(rs.par_vote(0, 4), 0)
        self.assertEqual(rs.par_vote(0, 0), 0)

    def test_pipeline_flag_is_rank_only(self):
        measured = flag_is_rank_only(
            self, 'par_corpus', ('par_vote', 'par_sugg', 'par_word'))
        # "הילד ראה ספד גדול": another book reads ספר in the same window
        self.assertTrue(any(r['par_sugg'] > 0 and r['par_vote'] > 0
                            for r in measured))


class RankModel(unittest.TestCase):
    """(4) the optional calibrated logistic ranking."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix='magiah_model_')

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def _label(self, n_docs=30):
        """Human decisions: real errors are the context-verified findings
        (with some noise), across `n_docs` books."""
        from magiah.webui import db as uidb
        con = uidb.connect(self.out)
        i = 0
        for d in range(n_docs):
            for k in range(4):
                i += 1
                real = k < 2
                hits = 3 if (real or k == 3 and d % 5 == 0) else 0
                status = 'approved' if real else 'not_error'
                con.execute(
                    'INSERT INTO findings(id, family, errtype, word, '
                    'suggestion, score, rank, ctx_hits, sugg_local, '
                    'book_repeat, tanach, doc, unit, verified) VALUES'
                    "(?, 'error', 'edit1_sub', ?, ?, ?, 0, ?, 0, 0, 0, ?, "
                    '?, 0)', (i, f'בות{i}', 'בית', 2.0 + (k % 2) * 0.1,
                              hits, f'd{d}', str(i)))
                con.execute('INSERT INTO review(finding_id, status, '
                            "updated_at) VALUES(?, ?, 'x')", (i, status))
        con.commit()
        con.close()
        return i

    def _cfg(self, **kw):
        from magiah.config import Config
        return Config(**{'rank_model': 1, 'rank_model_min': 50, **kw})

    def test_python_and_sql_features_agree(self):
        from magiah import rankmodel as rm
        model = {'bias': -0.3,
                 'weights': {k: 0.1 * (j + 1) - 0.6
                             for j, k in enumerate(rm.FEATURE_NAMES)}}
        rows = [
            dict(word='בות', errtype='edit1_sub', score=2.5, ctx_hits=2,
                 sugg_local=4, book_repeat=0, tanach=0, ctx_assoc=0.7,
                 rw_lr=None, par_vote=4.0),
            dict(word='אבגדהוזחטיכלמנ', errtype='final_midword', score=3.0,
                 ctx_hits=0, sugg_local=12, book_repeat=1, tanach=3),
            dict(word='אב', errtype='missing_space', score=None,
                 ctx_hits=None, sugg_local=None, book_repeat=None,
                 tanach=None, rw_lr=-7.0),
        ]
        con = sqlite3.connect(':memory:')
        cols = ('word', 'errtype', 'score', 'ctx_hits', 'sugg_local',
                'book_repeat', 'tanach', 'ctx_assoc', 'rw_lr', 'par_vote')
        con.execute(f'CREATE TABLE t({", ".join(cols)})')
        for r in rows:
            con.execute(f'INSERT INTO t VALUES({",".join("?" * len(cols))})',
                        [r.get(c) for c in cols])
        got = [v for v, in con.execute(
            f'SELECT {rm.logit_sql(model)} FROM t ORDER BY rowid')]
        for r, v in zip(rows, got):
            self.assertAlmostEqual(rm.logit(model, r), v, places=9)

    def test_fit_learns_the_signal(self):
        from magiah import rankmodel as rm
        rows = ([_row(i, 1, ctx_hits=2) for i in range(30)]
                + [_row(i, 0, ctx_hits=0) for i in range(30, 60)])
        m = rm.fit(rows, l2=0.1, iters=500)
        self.assertGreater(m['weights']['ctx_hit'], 0)
        self.assertGreater(rm.prob(m, rows[0]), 0.8)
        self.assertLess(rm.prob(m, rows[-1]), 0.2)

    def test_too_few_labels_fall_back(self):
        from magiah import rankmodel as rm
        rows = [_row(i, i % 2) for i in range(8)]
        model, info = rm.train(rows, 200)
        self.assertIsNone(model)
        self.assertIn('8 labels', info['reason'])

    def test_calibrate_with_few_labels_keeps_the_formula(self):
        from magiah import rankmodel as rm
        self._label(n_docs=2)
        info = core.calibrate(self._cfg(), self.out)
        self.assertEqual(info['rank_model']['state'], 'fallback')
        self.assertFalse(os.path.exists(os.path.join(self.out, rm.MODEL_F)))
        self.assertEqual(rm.rank_sql(self.out, core.RANK_SQL),
                         (core.RANK_SQL, None))

    def test_calibrate_fits_saves_and_vouches(self):
        from magiah import rankmodel as rm
        self._label()
        info = core.calibrate(self._cfg(), self.out)
        self.assertEqual(info['rank_model']['state'], 'fitted')
        self.assertIn('held_out', info['rank_model'])
        model = rm.load_model(self.out)
        self.assertIsNotNone(model)
        self.assertGreater(model['weights']['ctx_hit'], 0)
        expr, m = rm.rank_sql(self.out, core.RANK_SQL)
        self.assertIs(m is not None, True)
        self.assertNotEqual(expr, core.RANK_SQL)
        # edited afterwards: no longer vouched for
        path = os.path.join(self.out, rm.MODEL_F)
        with open(path, encoding='utf-8') as f:
            obj = f.read()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(obj.replace('"bias": ', '"bias": 1 + 0 * '))
        self.assertIsNone(rm.load_model(self.out))

    def test_calibrate_without_the_flag_removes_the_model(self):
        from magiah import rankmodel as rm
        self._label()
        core.calibrate(self._cfg(), self.out)
        self.assertIsNotNone(rm.load_model(self.out))
        info = core.calibrate(self._cfg(rank_model=0), self.out)
        self.assertEqual(info['rank_model'], {'state': 'off'})
        self.assertIsNone(rm.load_model(self.out))

    def test_reports_rank_by_the_model_with_prob(self):
        from magiah import rankmodel as rm
        self._label()
        core.calibrate(self._cfg(), self.out)
        expr, model = rm.rank_sql(self.out, core.RANK_SQL)
        con = sqlite3.connect(':memory:')
        con.execute('CREATE TABLE occurrences_full(word, errtype, '
                    'suggestion, score, ctx_hits, sugg_local, book_repeat, '
                    'tanach, source, ref, unit, snippet, origin, doc, '
                    'evidence)')
        con.execute('CREATE TABLE space_errors_full(part1, part2, joined, '
                    'join_freq, source, ref, unit, snippet, origin)')
        con.executemany(
            'INSERT INTO occurrences_full VALUES(' + ','.join('?' * 15) + ')',
            [('בות', 'edit1_sub', 'בית', 2.0, h, 0, 0, 0, 'b', 'r', str(h),
              's', '', 'd', 'none') for h in (0, 5)])
        for m, e in ((None, None), (model, expr)):
            dest = tempfile.mkdtemp(dir=self.out)
            core._write_reports(con, dest, '', (), 0, e, m)
            with open(os.path.join(dest, 'errors_edit1_sub.csv'),
                      encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            if m is None:
                self.assertEqual({r['prob'] for r in rows}, {''})
            else:
                probs = [float(r['prob']) for r in rows]
                self.assertEqual(probs, sorted(probs, reverse=True))
                self.assertEqual(rows[0]['ctx_hits'], '5')
                self.assertTrue(all(0 < p < 1 for p in probs))


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
