# -*- coding: utf-8 -*-
"""Regression tests for the fixer follow-ups: one class per audited item.

Every fixture is built under a temp dir; nothing here touches a real
library. The shared fixtures come from test_anchor_write.
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from magiah import normalize                                    # noqa: E402
from magiah.webui import journal, patcher                       # noqa: E402
from test_anchor_write import (FixerEnv, TempCase, finding, raw,  # noqa: E402
                               scan_snippet, write)


# ---------------------------------------------------------------------------
# the mapped tokenizer and per-line work
# ---------------------------------------------------------------------------

class TestControlCharactersInALine(TempCase):
    """NUL, U+0001 and U+0002 used to be the mapper's own markers: a line
    holding one was mapped differently from clean(), so its findings were
    refused (or anchored by a map that was not clean's)."""

    LINES = ('אמר\x00 רבי יותבת בן זומא',
             '\x01אמר רבי יותבת\x02 בן <b>זומא</b>',
             'אמר רבי\x00<b>\x01</b> יותבת &amp;\x02 בן זומא')

    def test_the_map_is_cleans_own(self):
        for line in self.LINES:
            with self.subTest(line=repr(line)):
                got, omap, emap = normalize.clean_mapped(line)
                self.assertEqual(got, normalize.clean(line))
                self.assertEqual(len(omap), len(got))
                for tok, a, b in normalize.token_spans(line):
                    self.assertEqual(normalize.clean(line[a:b]).strip(), tok)

    def test_a_finding_on_such_a_line_is_written_exactly(self):
        for line in self.LINES:
            with self.subTest(line=repr(line)):
                d = self.doc(line + '\n')
                plan = patcher.plan_edit(d, finding(line, 'יותבת', 'יושבת'))
                self.assertEqual(plan.confidence, 'exact')
                patcher.apply_edits(d, [plan])
                self.assertEqual(d.encode(), (line.replace(
                    'יותבת', 'יושבת') + '\n').encode('utf-8'))


class TestPerLineWorkIsShared(TempCase):
    """Anchoring cost (findings on a line) x (line length): 50 findings on
    one 200 KB line took 22-26 s. Each line is now tokenized once."""

    def test_a_long_line_is_tokenized_once_for_all_its_findings(self):
        words = []
        typos = ['קדבנ' + chr(0x05D0 + i) for i in range(20)]
        for i in range(3000):
            words.append(typos[i // 150] if i % 150 == 75 else 'שלום')
        line = ' '.join(words)
        d = self.doc('פתיחה\n' + line + '\nסוף\n')
        rows = [dict(finding(line, t, 'קרבן', lineno=1, fid=i + 1),
                     occurrence=0, expected_count=1)
                for i, t in enumerate(typos)]
        calls = []
        real = normalize.token_spans_full

        def counting(text):
            calls.append(len(text))
            return real(text)
        normalize.token_spans_full = counting
        try:
            patcher.anchor_rows(d, rows)
        finally:
            normalize.token_spans_full = real
        self.assertEqual([r['anchor'].get('code') for r in rows
                          if not r['anchor']['ok']], [])
        self.assertEqual([r['anchor']['start'] for r in rows],
                         [line.index(t) for t in typos])
        self.assertEqual(calls.count(len(line)), 1, calls)

    def test_twins_are_still_found_past_an_entity_newline(self):
        """Nearby lines are searched as one joined text; an entity that
        decodes to a newline must not shift the line numbers found."""
        line = 'אמר רבי יותבת בן זומא'
        for first in ('פתיחה', 'א&#10;ב&#x0A;ג'):
            with self.subTest(first=first):
                d = self.doc('\n'.join((first, line, 'כותרת', line)) + '\n')
                with self.assertRaises(patcher.PatchError) as cm:
                    patcher.plan_edit(d, finding(line, 'יותבת', 'יושבת',
                                                 lineno=1))
                self.assertEqual(cm.exception.code, 'ambiguous_line')
                self.assertEqual(cm.exception.extra['candidate_lines'],
                                 [1, 3])

    def test_an_edited_line_is_not_answered_from_the_cache(self):
        line = 'אמר רבי יותבת בן זומא'
        d = self.doc(line + '\n')
        f = finding(line, 'יותבת', 'יושבת')
        plan = patcher.plan_edit(d, f)
        patcher.apply_edits(d, [plan])
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, f)
        self.assertEqual(cm.exception.code, 'token_not_found')


# ---------------------------------------------------------------------------
# identity of the line and of the copy
# ---------------------------------------------------------------------------

class TestNoRelocationOntoAParallelVerse(TempCase):
    """The scanned verse S is deleted, or moved beyond the drift window;
    its parallel verse P (most words shared, the same typo once) is close
    by. The fix used to be written on P, marked only 'moved'."""

    P = 'ולכן צריך לעמוד כעבד לפני רבו קדבן היא בבקר'
    S = 'ולכן צריך לעמוד כעבד לפני רבו קדבן היא בערב'

    def plan(self, lines):
        d = self.doc('\n'.join(lines) + '\n')
        return d, {'id': 2, 'lineno': 2, 'word': 'קדבן', 'correction': 'קרבן',
                   'snippet': scan_snippet(self.S, 'קדבן'), 'occurrence': 0,
                   'expected_count': 1}

    def test_deleted_or_moved_far_is_refused_with_a_pick(self):
        for label, lines in (
                ('deleted', ['פתיחה', self.P, 'ויהי ערב ויהי בקר יום אחד']),
                ('moved 70 lines down', ['פתיחה', self.P, 'ויהי ערב'] +
                 ['מילוי %d' % i for i in range(70)] + [self.S])):
            with self.subTest(label):
                d, f = self.plan(lines)
                with self.assertRaises(patcher.PatchError) as cm:
                    patcher.plan_edit(d, f)
                self.assertEqual(cm.exception.code, 'moved_unproven')
                self.assertIn(cm.exception.code, patcher.CONFLICT_CODES)
                self.assertEqual(patcher.manual_lines(d, f), [1])
                rows = patcher.anchor_rows(d, [dict(f, unit='x')])
                self.assertFalse(rows[0]['anchor']['ok'])
                self.assertEqual(rows[0]['anchor']['manual_lines'], [1])

    def test_the_same_sentence_moved_is_still_followed(self):
        # its window is exact there, so the parallel verse nearby does not
        # get in the way
        d, f = self.plan([self.P, 'פתיחה', 'שורה שנוספה', 'ויהי ערב',
                          self.S])
        plan = patcher.plan_edit(d, f)
        self.assertEqual((plan.lineno, plan.confidence), (4, 'moved'))
        self.assertTrue(plan.drifted)


class TestSingleCopyNeedsItsNeighbours(TempCase):
    """The scanned copy was fixed by hand and the typo typed again: one copy
    is left, the scan counted one, and the line still shares the words. The
    'weak' path wrote on the new copy."""

    W, FIX = 'לשמיס', 'לשמים'
    L0 = 'והלכה כדברי האומר שהתפלה לשמיס במקום קרבן היא ולכן צריך לעמוד'

    def plan(self, line, **kw):
        d = self.doc(line + '\n')
        return d, patcher.plan_edit(d, dict(finding(self.L0, self.W, self.FIX),
                                            occurrence=0, expected_count=1),
                                    **kw)

    def test_a_retyped_copy_is_refused(self):
        for line in (self.L0.replace(self.W, self.FIX + ' ' + self.W),
                     self.L0.replace(self.W, self.FIX).replace(
                         'ולכן', 'ולכן ' + self.W)):
            with self.subTest(line=line):
                with self.assertRaises(patcher.PatchError) as cm:
                    self.plan(line)
                self.assertEqual(cm.exception.code, 'copy_moved')
                self.assertEqual(cm.exception.extra['located_line'], 0)
                # a human who looked may still point at it
                a = line.index(self.W)
                _d, p = self.plan(line, explicit=(a, a + len(self.W)))
                self.assertEqual(p.confidence, 'manual')

    def test_the_scanned_copy_with_other_edits_nearby_is_written(self):
        line = self.L0.replace('האומר', 'האמר').replace('ולכן', 'לכן')
        d, p = self.plan(line)
        self.assertEqual(p.confidence, 'weak')
        self.assertEqual(p.start, line.index(self.W))


class TestCopiesTheScannerSkips(TempCase):
    """A flagged word with a second copy the scanner passes over (after a
    title, before a geresh, next to a bracket) has one finding; counting the
    skipped copy refused it even in an unchanged file."""

    W, FIX = 'לשמיס', 'לשמים'
    PAD = ' '.join(['והלכה כדברי האומר שהתפלה במקום קרבן היא'] * 3)
    CASES = ('אמר רבי {w} כעבד לפני {w} ולכן ',
             "כעבד {w}' לפני רבו {w} ולכן ",
             'כעבד {w}(?) לפני רבו {w} ולכן ')

    def scanned(self, line):
        """The findings the real scanner rule (core.locate_line) makes."""
        from collections import Counter
        from magiah import core
        from magiah.config import Config
        freq = Counter({t: 10 ** 6 for t in normalize.tokenize(line)})
        freq[self.W] = 1
        occ = core.locate_line(line, freq, Config(),
                               {self.W: ('error', 'edit1_sub', self.FIX)})[0]
        return [{'id': i + 1, 'lineno': 0, 'word': w, 'correction': self.FIX,
                 'suggestion': self.FIX, 'errtype': 'edit1_sub',
                 'snippet': snip, 'occurrence': i, 'expected_count': len(occ)}
                for i, (w, _p, _n, snip) in enumerate(occ)]

    def test_the_reported_copy_is_written(self):
        for case in self.CASES:
            line = case.format(w=self.W) + self.PAD
            [f] = self.scanned(line)
            reported = line.rindex(self.W)
            for now in (line, line + ' סוף הדבר'):
                with self.subTest(line=now[:30], edited=now != line):
                    p = patcher.plan_edit(self.doc(now + '\n'), dict(f))
                    self.assertEqual((p.start, p.confidence),
                                     (reported, 'indexed'))

    def test_the_proof_still_holds(self):
        """The reported copy fixed by hand and the word typed again: still
        one copy to count, but not the scanned one's window — refused."""
        for case in self.CASES:
            line = case.format(w=self.W) + self.PAD
            [f] = self.scanned(line)
            k = line.rindex(self.W)
            now = line[:k] + self.FIX + line[k + len(self.W):] + ' ' + self.W
            with self.subTest(line=now[:30]):
                with self.assertRaises(patcher.PatchError):
                    patcher.plan_edit(self.doc(now + '\n'), dict(f))


class TestBracketsWithACustomCorrection(TempCase):
    """Bracket output written with a custom correction, re-scanned with its
    record lost or its context broken by a hand edit, was overwritten:
    "(בורכת) [בדכת]" became "(בורכת) [ברכת]"."""

    T, S = 'בדכת', 'ברכת'
    BASE = 'ויאמר משה בדכת שלום עליכם לתלמידיו'

    def written(self):
        d = self.doc(self.BASE)
        p = patcher.plan_edit(d, finding(self.BASE, self.T, 'בורכת',
                                         suggestion=self.S),
                              mode=patcher.MODE_BRACKET)
        patcher.apply_edits(d, [p])
        self.assertEqual(d.lines[0],
                         'ויאמר משה (בורכת) [בדכת] שלום עליכם לתלמידיו')
        return d.lines[0], p.to_dict()

    def refused(self, line, own, snippet_from, corr):
        d = self.doc(line)
        for mode in patcher.MODES:
            with self.assertRaises(patcher.PatchError) as cm:
                patcher.plan_edit(d, dict(finding(snippet_from, self.T, corr),
                                          suggestion=self.S),
                                  mode=mode, own_edits=own)
            yield cm.exception.code

    def test_a_live_record_is_matched_by_its_text_alone(self):
        line, rec = self.written()
        broken = line.replace('ויאמר משה', 'ויאמר אהרן')    # hand edit
        for snip in (broken, self.BASE):
            with self.subTest(snippet=snip):
                self.assertEqual(set(self.refused(broken, [rec], snip,
                                                  self.S)),
                                 {'already_applied'})

    def test_without_a_record_only_a_click_writes(self):
        line, _rec = self.written()
        for corr in (self.S, 'בורכות'):        # re-scanned in a new folder
            with self.subTest(correction=corr):
                self.assertEqual(set(self.refused(line, [], line, corr)),
                                 {'bracket_unproven'})
        d = self.doc(line)
        a = line.index('[') + 1
        p = patcher.plan_edit(d, dict(finding(line, self.T, self.S),
                                      suggestion=self.S), explicit=(a, a + 4))
        self.assertEqual(p.confidence, 'manual')


class TestFileEdgeCases(TempCase):

    def test_a_bom_only_file_is_numbered_like_the_scanner(self):
        from magiah import textsource
        for data in (b'\xef\xbb\xbf', b'\xef\xbb\xbf\n', b'', b'\n',
                     b'\xef\xbb\xbf\xd7\x90'):
            with self.subTest(data=data):
                p = os.path.join(self.tmp, 'b.txt')
                with open(p, 'wb') as f:
                    f.write(data)
                d = patcher.read_doc(p)
                scanned = textsource.split_lines(data.decode('utf-8'))
                self.assertEqual(len(d.lines), len(scanned))
                self.assertEqual(d.encode(), data)

    def test_teamim_in_a_cp1255_book_are_refused_in_hebrew(self):
        line = 'אמר רבי יותבת בן זומא'
        p = os.path.join(self.tmp, 'b.txt')
        with open(p, 'wb') as f:
            f.write((line + '\n').encode('cp1255'))
        d = patcher.read_doc(p)
        self.assertEqual(d.encoding, 'cp1255')
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.plan_edit(d, finding(line, 'יותבת', 'יוֹשֶׁ֣בֶת'))
        self.assertEqual(cm.exception.code, 'unencodable')
        self.assertTrue(any('א' <= c <= 'ת' for c in str(cm.exception)))
        # nikud alone is in the code page and is written
        plan = patcher.plan_edit(d, finding(line, 'יותבת', 'יוֹשֶׁבֶת'))
        patcher.apply_edits(d, [plan])
        self.assertEqual(d.encode().decode('cp1255'),
                         line.replace('יותבת', 'יוֹשֶׁבֶת') + '\n')


# ---------------------------------------------------------------------------
# recorded edits after lines moved
# ---------------------------------------------------------------------------

class TestRecordsFollowMovedLines(FixerEnv):
    """Lines inserted above a fixer edit used to break its undo and the
    "already applied" check: records were looked up by line number only."""

    TOP = 'שורה חדשה בראש הספר\nועוד אחת\n'

    def test_undo_and_already_applied_after_a_shift(self):
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        write(self.path, self.TOP + raw(self.path).decode('utf-8'))
        d = self.open_doc(self.key)
        [row] = [i for i in d['items'] if i['id'] == 1]
        self.assertEqual(row['anchor']['code'], 'already_applied')
        self.assertEqual(row['lineno'], 3)
        # applying it again writes nothing
        res2, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual((code, res2['already_applied']), (200, [1]), res2)
        self.assertEqual(res2['applied'], [])
        # and its undo puts back exactly its span, on the moved line
        out, code = self.undo(res['edit_id'])
        self.assertEqual(code, 200, out)
        self.assertEqual(raw(self.path).decode('utf-8'),
                         self.TOP + self.TEXT)

    def test_a_twin_record_is_never_taken_for_another(self):
        """Two identical lines fixed by two writes; one fix reverted by hand
        and lines inserted above. Neither record can tell which remaining
        copy is its own: the undo is refused rather than undo the twin."""
        line = 'אמר רבי יותבת בן זומא'
        d = self.doc('כותרת\n%s\n%s\n' % (line, line))
        recs = []
        for n in (1, 2):
            f = dict(finding(line, 'יותבת', 'יושבת', lineno=n, fid=n),
                     trusted=True)
            p = patcher.plan_edit(d, f)
            patcher.apply_edits(d, [p])
            recs.append(p.to_dict())
        d.lines[1:2] = ['חדשה', 'כותרת', line]     # hand edits, then shift
        del d.lines[0]
        self.assertEqual(patcher.locate_entries(d, recs), [None, None])
        with self.assertRaises(patcher.PatchError) as cm:
            patcher.reverse_entries(d, [recs[0]], [recs[1]])
        self.assertEqual(cm.exception.code, 'file_changed_since_edit')

    def test_a_unique_record_is_found_far_from_its_line(self):
        line = 'אמר רבי יותבת בן זומא'
        d = self.doc('כותרת\n%s\n' % line)
        p = patcher.plan_edit(d, finding(line, 'יותבת', 'יושבת', lineno=1))
        patcher.apply_edits(d, [p])
        rec = p.to_dict()
        d.lines[0:0] = ['מילוי %d' % i for i in range(500)]
        self.assertEqual(patcher.locate_entries(d, [rec]),
                         [(501, p.post_start)])
        patcher.reverse_entries(d, [rec])
        self.assertEqual(d.lines[501], line)


# ---------------------------------------------------------------------------
# recovery, backups and the journal
# ---------------------------------------------------------------------------

class TestRecoveryDuringABriefLock(FixerEnv):
    """A crashed write whose book is momentarily locked while recovery runs
    is retried later, never recorded as a permanent conflict."""

    def _crashed_write(self):
        d = patcher.read_doc(self.path)
        before = d.encode()
        plans, failures = patcher.plan_all(d, [{
            'id': 1, 'lineno': 1, 'word': 'יותבת', 'correction': 'יושבת',
            'snippet': d.lines[1]}])
        self.assertEqual(failures, [])
        patcher.apply_edits(d, plans)
        after = d.encode()
        journal.begin(self.outdir, {
            'kind': 'apply', 'path': self.path, 'book_key': self.key,
            'mode': 'replace', 'finding_ids': [1], 'mark_fixed': True,
            'detail': patcher.detail_json(plans), 'backup': '',
            'fp_before': patcher.fingerprint_bytes(before),
            'fp_after': patcher.fingerprint_bytes(after)})
        write(self.path, after.decode('utf-8'))

    def _recover(self, wait):
        con = self.con()
        try:
            return journal.recover(con, self.outdir, wait=wait)
        finally:
            con.close()

    def test_a_locked_book_leaves_the_intent_open(self):
        self._crashed_write()
        real = patcher.fingerprint

        def locked(path):
            raise PermissionError(13, 'The process cannot access the file')
        patcher.fingerprint = locked
        try:
            out = self._recover(wait=False)
            self.assertEqual([o['state'] for o in out], ['denied'])
            # a writer is told why it cannot go on, in Hebrew
            with self.assertRaises(patcher.AccessDenied) as cm:
                self._recover(wait=True)
            self.assertEqual(cm.exception.code, 'access_denied')
        finally:
            patcher.fingerprint = real
        self.assertEqual(len(journal.pending(self.outdir)), 1)
        self.assertEqual(journal.conflicts(self.outdir), [])
        # the lock is gone: the write is settled as what it was
        self.assertEqual([o['state'] for o in self._recover(wait=False)],
                         ['committed'])
        self.assertEqual(journal.conflicts(self.outdir), [])
        self.assertEqual(self.status_of(1), 'fixed')


class TestRefusedWriteLeavesNoBackup(FixerEnv):

    def _backups(self):
        bdir = os.path.join(self.outdir, patcher.FIXER_BACKUP_DIR)
        if not os.path.isdir(bdir):
            return []
        return [n for n in os.listdir(bdir) if n.endswith('.bak')]

    def test_a_locked_or_read_only_book_leaves_no_backup(self):
        real = os.replace

        def refuse(src, dst):
            raise PermissionError(13, 'Access is denied')
        os.replace = refuse
        try:
            for _ in range(3):
                with self.assertRaises(patcher.AccessDenied):
                    self.apply(self.key, [{'id': 1}])
        finally:
            os.replace = real
        self.assertEqual(self._backups(), [])
        self.assertEqual(journal.pending(self.outdir), [])
        self.assertEqual(raw(self.path), self.TEXT.encode('utf-8'))
        # and a write that goes through still keeps its backup
        res, code = self.apply(self.key, [{'id': 1}])
        self.assertEqual(code, 200, res)
        self.assertEqual(len(self._backups()), 1)


class TestJournalTornLine(FixerEnv):

    def test_a_torn_last_line_does_not_swallow_the_next_record(self):
        jid = journal.begin(self.outdir, {'kind': 'apply', 'path': self.path})
        p = journal.journal_path(self.outdir)
        with open(p, 'ab') as f:
            f.write(b'{"op": "committed", "jid": "torn')   # a crash mid-line
        journal.finish(self.outdir, jid, 'committed')
        ops = [(r['jid'], r['op']) for r in journal.read_records(self.outdir)]
        self.assertEqual(ops, [(jid, 'intent'), (jid, 'committed')])
        self.assertEqual(journal.pending(self.outdir), [])


class TestLockOwnership(FixerEnv):

    def test_a_share_clock_behind_ours_does_not_break_a_live_lock(self):
        lf = write(self.path + journal.LOCK_SUFFIX, '{}')
        stamp = time.time() - journal.STALE_LOCK_SECONDS - 60
        os.utime(lf, (stamp, stamp))
        real = journal._server_now
        # the share's own clock says the holder touched it a moment ago
        journal._server_now = lambda folder: stamp + 5
        try:
            with self.assertRaises(patcher.PatchError) as cm:
                with journal.file_lock(self.path, timeout=0.2):
                    pass
        finally:
            journal._server_now = real
        self.assertEqual(cm.exception.code, 'file_busy')
        self.assertTrue(os.path.exists(lf))
        # by both clocks it is dead: broken as before
        with journal.file_lock(self.path, timeout=1):
            pass
        self.assertFalse(os.path.exists(lf))

    def test_a_holder_whose_lock_was_taken_does_not_write(self):
        """A writer stalled past the stale age; another one broke its lock
        and wrote. The woken writer must not replace the book."""
        fp = self.open_doc(self.key)['fingerprint']
        lf = self.path + journal.LOCK_SUFFIX
        real = patcher.plan_all
        thief = json.dumps({'pid': 1, 'token': 'another-writer'})

        def stall(*a, **kw):
            out = real(*a, **kw)
            with open(lf, 'w') as f:
                f.write(thief)
            return out
        patcher.plan_all = stall
        try:
            res, code = self.apply(self.key, [{'id': 1}], fingerprint=fp)
        finally:
            patcher.plan_all = real
        self.assertEqual((code, res.get('code')), (409, 'file_busy'))
        self.assertEqual(raw(self.path), self.TEXT.encode('utf-8'))
        # and its release leaves the other writer's lock alone
        with open(lf) as f:
            self.assertEqual(f.read(), thief)
        os.remove(lf)

    def test_the_book_changed_under_a_stalled_holder(self):
        fp = self.open_doc(self.key)['fingerprint']
        real = patcher.plan_all

        def stall(*a, **kw):
            out = real(*a, **kw)
            write(self.path, self.TEXT + 'שורה שנכתבה בינתיים\n')
            return out
        patcher.plan_all = stall
        try:
            res, code = self.apply(self.key, [{'id': 1}], fingerprint=fp)
        finally:
            patcher.plan_all = real
        self.assertEqual((code, res.get('code')), (409, 'file_changed'))
        self.assertNotIn('יושבת', raw(self.path).decode('utf-8'))
        self.assertEqual(journal.pending(self.outdir), [])

    def test_a_lock_being_deleted_on_fat_is_busy_not_unwritable(self):
        """FAT reports a lock file that is being deleted as access denied
        and already gone; the folder still takes other files."""
        real_open = journal.os.open

        def deleting(path, *a, **kw):
            if path.endswith(journal.LOCK_SUFFIX):
                raise PermissionError(13, 'Access is denied', path)
            return real_open(path, *a, **kw)
        journal.os.open = deleting
        try:
            with self.assertRaises(patcher.PatchError) as cm:
                with journal.file_lock(self.path, timeout=0.3):
                    pass
        finally:
            journal.os.open = real_open
        self.assertEqual(cm.exception.code, 'file_busy')
        self.assertEqual(sorted(os.listdir(self.lib)), ['ספר.txt'])


if __name__ == '__main__':
    unittest.main()
