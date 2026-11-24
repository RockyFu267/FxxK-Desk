import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fxxk_desk.backfill import Backfill, Page, find_anchor, find_identified_anchor
from fxxk_desk.targets import Target
from fxxk_desk.workflow import Workflow, fingerprint
from fxxk_desk.interference import UserInterference


def msg(number):
    return {"text": f"message-{number:04}", "sender": "Bot", "direction": "incoming", "complete": True}


class Transcript:
    supports_backfill = True

    def __init__(self, items, size=12, step=6, directory=None):
        self.items, self.size, self.step = items, size, step
        self.position = max(0, len(items) - size)
        self.directory = Path(directory) if directory else None
        self.moves = []
        self.on_up = None

    def read(self):
        items = self.items[self.position:self.position + self.size]
        raw = json.dumps(items).encode()
        return Page(items, f"page-{self.position}", hashlib.sha256(raw).hexdigest())

    def scroll(self, direction, fast=False):
        self.moves.append(direction)
        if direction == "up":
            self.position = max(0, self.position - self.step)
            if self.on_up:
                callback, self.on_up = self.on_up, None
                callback()
        else:
            self.position = max(0, len(self.items) - self.size) if fast else min(max(0, len(self.items) - self.size), self.position + self.step)

    scroll_chat = scroll

    def ensure_app(self, _):
        pass

    def chat_is_open(self, _):
        return True

    def message_snapshot(self, _):
        page = self.read()
        path = self.directory / (page.image_hash + '.json')
        path.write_text(json.dumps(page.messages), encoding='utf-8')
        return path


class Reader:
    extractor_version = 'test-pages-v1'

    def extract_messages(self, path):
        return json.loads(Path(path).read_text(encoding='utf-8'))


class BackfillTests(unittest.TestCase):
    def test_checkpoint_requires_all_five_in_order(self):
        previous = ['older', 'a', 'b', 'c', 'd', 'e']
        self.assertEqual(find_anchor(previous, ['a', 'b', 'c', 'd', 'e', 'new']), 5)
        self.assertIsNone(find_anchor(previous, ['x', 'b', 'c', 'd', 'e', 'new']))
        self.assertIsNone(find_anchor(previous, ['a', 'c', 'b', 'd', 'e']))
        self.assertEqual(find_anchor(['a', 'b'], ['a', 'b', 'new']), 2)

    def test_same_sender_and_text_at_a_later_visible_time_is_new(self):
        earlier = dict(msg(1), visible_time_label='11:10')
        later = dict(earlier, visible_time_label='11:12')
        previous = [msg(0), earlier]
        page = Page(previous + [later, later], 'current.png', 'current')
        capture = Backfill(lambda: self.fail('unexpected capture'),
                           lambda *args: self.fail('unexpected scroll'), fingerprint)
        result = capture.recover([fingerprint(item) for item in previous], page, had_checkpoint=True)
        self.assertEqual([fingerprint(item) for item in result.messages], [fingerprint(later)] * 2)

    def test_explicit_event_anchor_can_verify_prefix_when_previous_message_is_clipped(self):
        previous = [fingerprint(msg(1)), fingerprint(dict(msg(2), text='测试事件 SCROLL-7c78f4-08'))]
        current = [previous[-1], fingerprint(msg(3)), fingerprint(msg(4))]
        self.assertEqual(find_identified_anchor(previous, current), 1)
        self.assertIsNone(find_identified_anchor(previous, [previous[-1], previous[-1], current[-1]]))
        self.assertIsNone(find_identified_anchor([fingerprint(msg(1)), fingerprint(msg(2))], [fingerprint(msg(2)), fingerprint(msg(3))]))
    def test_visible_new_messages_are_returned_before_any_scroll(self):
        desktop = Transcript([msg(i) for i in range(16)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover(
            [fingerprint(msg(i)) for i in range(12)], desktop.read(), had_checkpoint=True)
        self.assertEqual([m['text'] for m in result.messages], [msg(i)['text'] for i in range(12,16)])
        self.assertEqual(desktop.moves, [])
        self.assertFalse(result.caught_up)

    def test_uncertain_pixel_speed_still_reads_page_and_proves_ocr_overlap(self):
        from fxxk_desk.scrolling import ScrollFeedbackUncertain
        desktop = Transcript([msg(i) for i in range(64)])
        def uncertain_scroll(direction, fast=False):
            desktop.scroll(direction, fast)
            raise ScrollFeedbackUncertain('speed uncertain')
        result = Backfill(desktop.read, uncertain_scroll, fingerprint).recover(
            [fingerprint(msg(i)) for i in range(12)], desktop.read(), had_checkpoint=True)
        self.assertTrue(result.caught_up, result.reason)
        self.assertEqual([m['text'] for m in result.messages], [msg(i)['text'] for i in range(12,64)])
    def test_passive_prefix_collection_does_not_scroll_or_claim_latest(self):
        desktop = Transcript([msg(i) for i in range(16)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover(
            [fingerprint(msg(i)) for i in range(12)], desktop.read(), had_checkpoint=True, passive=True)
        self.assertEqual([m['text'] for m in result.messages], [msg(i)['text'] for i in range(12, 16)])
        self.assertEqual(result.status, 'overlap')
        self.assertFalse(result.caught_up)
        self.assertEqual(desktop.moves, [])

    def test_passive_missing_anchor_preserves_checkpoint_and_waits(self):
        desktop = Transcript([msg(i) for i in range(100, 160)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover(
            [fingerprint(msg(i)) for i in range(12)], desktop.read(), had_checkpoint=True, passive=True)
        self.assertFalse(result.checkpoint)
        self.assertFalse(result.messages)
        self.assertFalse(result.caught_up)
        self.assertEqual(desktop.moves, [])

    def test_matching_visible_tail_finishes_without_probe_scroll(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop = Transcript([msg(i) for i in range(12)], directory=directory)
            target = Target('app.exe', 'App', 'Group')
            workflow = Workflow('group', target, desktop, Reader(), str(Path(directory) / 'chat.db'))
            try:
                self.assertTrue(workflow.collect()['caught_up'])
                desktop.moves.clear()
                desktop.items.extend([msg(12), msg(13)])
                desktop.position = 2
                desktop.passive_observation = True
                result = workflow.collect()
                self.assertEqual(result['new_messages'], 2)
                self.assertFalse(result['caught_up'])
                self.assertEqual(desktop.moves, [])
                self.assertFalse(workflow.collect()['caught_up'])
                desktop.passive_observation = False
                self.assertTrue(workflow.collect()['caught_up'])
                self.assertEqual(desktop.moves, [])
                self.assertFalse(workflow.db.execute('SELECT 1 FROM deferred_tail').fetchone())
            finally:
                workflow.close()
    def test_user_interruption_does_not_trigger_restore_scroll(self):
        desktop = Transcript([msg(i) for i in range(60)])

        def interrupt():
            raise UserInterference('User moved mouse')

        desktop.on_up = interrupt
        with self.assertRaises(UserInterference):
            Backfill(desktop.read, desktop.scroll, fingerprint).recover([fingerprint(msg(i)) for i in range(12)])
        self.assertEqual(desktop.moves[-1], 'up')

    def test_recovers_1000_burst_messages_in_order(self):
        items = [msg(i) for i in range(1024)]
        desktop = Transcript(items, size=24, step=12)
        result = Backfill(desktop.read, desktop.scroll, fingerprint, max_pages=150).recover(
            [fingerprint(m) for m in items[:24]])
        self.assertTrue(result.caught_up, result.reason)
        self.assertEqual([m['text'] for m in result.messages], [m['text'] for m in items[24:]])
        self.assertGreater(result.scrolls, 1)
        self.assertEqual(desktop.position, 1000)

    def test_no_anchor_reports_gap_without_claiming_new_messages(self):
        desktop = Transcript([msg(i) for i in range(100, 160)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover([fingerprint(msg(i)) for i in range(10)])
        self.assertEqual(result.status, 'gap')
        self.assertFalse(result.caught_up)
        self.assertEqual(result.messages, [])

    def test_budget_exhaustion_is_explicit(self):
        desktop = Transcript([msg(i) for i in range(100)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint, max_pages=4).recover([fingerprint(msg(i)) for i in range(10)])
        self.assertEqual(result.reason, 'recovery_budget_exhausted')
        self.assertEqual(result.checkpoint, [])

    def test_repeated_anchor_is_not_guessed(self):
        self.assertIsNone(find_anchor(['same', 'same'], ['same', 'same', 'same']))

    def test_scroll_that_skips_overlap_stops_without_committing(self):
        desktop = Transcript([msg(i) for i in range(60)], size=12, step=12)
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover([fingerprint(msg(i)) for i in range(12)])
        self.assertEqual(result.reason, 'page_overlap_missing')
        self.assertEqual(result.checkpoint, [])

    def test_empty_ocr_does_not_replace_existing_anchor(self):
        desktop = Transcript([])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover([fingerprint(msg(1))])
        self.assertEqual(result.status, 'gap')
        self.assertEqual(result.checkpoint, [])

    def test_partial_anchor_does_not_allow_single_message_guess(self):
        partial = dict(msg(0), complete=False)
        desktop = Transcript([partial, msg(1), msg(2)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover([fingerprint(msg(0)), fingerprint(msg(1))])
        self.assertEqual(result.status, 'gap')
        self.assertEqual(result.messages, [])

    def test_arrivals_during_backfill_do_not_get_skipped(self):
        desktop = Transcript([msg(i) for i in range(60)])
        desktop.on_up = lambda: desktop.items.extend(msg(i) for i in range(60, 90))
        engine = Backfill(desktop.read, desktop.scroll, fingerprint)
        first = engine.recover([fingerprint(msg(i)) for i in range(12)])
        self.assertFalse(first.caught_up)
        self.assertEqual(first.reason, 'tail_advanced_during_recovery')
        second = engine.recover([fingerprint(m) for m in first.checkpoint])
        self.assertTrue(second.caught_up, second.reason)
        self.assertEqual([m['text'] for m in first.messages + second.messages], [msg(i)['text'] for i in range(12, 90)])

    def test_empty_group_baseline_then_new_messages(self):
        desktop = Transcript([msg(1)])
        result = Backfill(desktop.read, desktop.scroll, fingerprint).recover([], had_checkpoint=True)
        self.assertEqual([m['text'] for m in result.messages], [msg(1)['text']])

    def test_gap_preserves_durable_checkpoint_and_retry_resolves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop = Transcript([msg(i) for i in range(12)], directory=directory)
            workflow = Workflow('source', Target('app.exe', 'Chat', 'Group'), desktop, Reader(), str(Path(directory) / 'test.db'))
            try:
                workflow.collect()
                before = workflow.db.execute('SELECT messages FROM snapshots').fetchone()[0]
                desktop.items = [msg(i) for i in range(100, 124)]
                desktop.position = 12
                self.assertEqual(workflow.collect()['status'], 'gap')
                self.assertEqual(workflow.db.execute('SELECT messages FROM snapshots').fetchone()[0], before)
                desktop.items = [msg(i) for i in range(36)]
                desktop.position = 24
                result = workflow.collect()
                self.assertTrue(result['caught_up'], result)
                self.assertEqual(result['new_messages'], 24)
                self.assertEqual(workflow.db.execute('SELECT COUNT(*) FROM collection_gaps WHERE resolved_at IS NULL').fetchone()[0], 0)
            finally:
                workflow.close()

    def test_recovered_messages_survive_restart_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop = Transcript([msg(i) for i in range(12)], directory=directory)
            path = str(Path(directory) / 'test.db')
            target = Target('app.exe', 'Chat', 'Group')
            workflow = Workflow('source', target, desktop, Reader(), path)
            workflow.collect()
            desktop.items.extend(msg(i) for i in range(12, 84))
            desktop.position = 72
            self.assertEqual(workflow.collect()['new_messages'], 72)
            workflow.close()
            workflow = Workflow('source', target, desktop, Reader(), path)
            try:
                self.assertEqual(workflow.collect()['new_messages'], 0)
                self.assertEqual(len(workflow.history()), 84)
            finally:
                workflow.close()
