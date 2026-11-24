import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fxxk_desk.backfill import Page
from fxxk_desk.relay import Relay
from fxxk_desk.targets import Target
from fxxk_desk.timestamp_backfill import TimestampBackfill, locate_latest_bubble_head, parse_message_time
from fxxk_desk.workflow import Workflow, fingerprint


OLD_TIME = '05/19 09:37:13'
NEW_TIME = '05/19 09:38:03'


def bubble(text, y):
    return {'text': text, 'direction': 'incoming', 'sender': 'Bot',
            'complete': True, 'bubble_rect': [80, y, 280, y + 40]}


class TimestampBackfillTests(unittest.TestCase):
    def test_latest_head_shortcut_ignores_clipped_older_messages(self):
        page = Page([dict(bubble('old fragment', 0), top_clipped=True), bubble('newest', 100)],
                    'tail', 'tail')
        scroll = Mock(side_effect=AssertionError('unexpected scroll'))
        observed, message, moves = locate_latest_bubble_head(lambda: page, scroll, 4, 5)
        self.assertEqual((observed, message, moves), (page, page.messages[-1], 0))
        scroll.assert_not_called()

    def engine(self, pages=(), times=None):
        self.moves = []
        self.clicks = []
        pages = iter(pages)

        def reveal(page, box):
            ref = (page.screenshot, box[1])
            self.clicks.append(ref)
            return (times or {}).get(ref)

        return TimestampBackfill(lambda: next(pages), self.moves.append,
                                 reveal, fingerprint, 4, 5)

    def test_seconds_timestamp_and_strict_validation(self):
        self.assertEqual(parse_message_time('测试消息通知 05/19 09:37:13'), OLD_TIME)
        self.assertEqual(parse_message_time('测试消息通知05/1909:37:13'), OLD_TIME)
        self.assertIsNone(parse_message_time('05/19 09:37'))
        self.assertIsNone(parse_message_time('11/31 09:37:13'))
        self.assertIsNone(parse_message_time(f'{OLD_TIME} {NEW_TIME}'))

    def test_identical_text_at_different_seconds_is_a_new_occurrence(self):
        page = Page([bubble('654 321 876540', 40), bubble('654 321 876540', 100)], 'tail', 'tail')
        engine = self.engine(times={('tail', 40): OLD_TIME, ('tail', 100): NEW_TIME})
        anchor = {'direction': 'incoming', 'text': '654321876540', 'timestamp': OLD_TIME}
        result = engine.recover(page, anchor, had_checkpoint=True)
        self.assertEqual([item['bubble_rect'][1] for item in result.messages], [100])
        self.assertEqual(result.anchor['timestamp'], NEW_TIME)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.clicks.count(('tail', 100)), 1)

    def test_scroll_until_single_timestamped_anchor_is_found(self):
        tail = Page([bubble('bridge', 30), bubble('new', 90)], 'tail', 'tail')
        older = Page([bubble('previous', 50), bubble('bridge', 110)], 'older', 'older')
        engine = self.engine([older], {('older', 50): OLD_TIME, ('tail', 90): NEW_TIME})
        anchor = {'direction': 'incoming', 'text': 'previous', 'timestamp': OLD_TIME}
        result = engine.recover(tail, anchor, had_checkpoint=True)
        self.assertEqual(self.moves, ['up'])
        self.assertEqual([item['text'] for item in result.messages], ['bridge', 'new'])
        self.assertEqual(result.anchor['timestamp'], NEW_TIME)

    def test_legacy_migration_only_requires_the_latest_timed_message(self):
        previous = dict(bubble('previous', 40), visible_time_label='09:37')
        page = Page([previous, bubble('new', 100)], 'tail', 'tail')
        engine = self.engine(times={('tail', 40): OLD_TIME, ('tail', 100): NEW_TIME})
        result = engine.recover(page, None, previous, had_checkpoint=True)
        self.assertEqual([item['text'] for item in result.messages], ['new'])
        self.assertEqual(result.anchor['timestamp'], NEW_TIME)
        self.assertEqual(self.moves, [])

    def test_visible_older_minute_and_newer_tail_bound_unreadable_anchor_time(self):
        page = Page([dict(bubble('older', 40), visible_time_label='10:17'),
                     bubble('long original', 110), bubble('new', 200)], 'tail', 'tail')
        engine = self.engine(times={('tail', 200): '05/19 10:23:36'})
        anchor = {'direction': 'incoming', 'text': 'longoriginal', 'timestamp': '05/19 10:20:51'}
        result = engine.recover(page, anchor, had_checkpoint=True)
        self.assertEqual([item['text'] for item in result.messages], ['new'])
        self.assertEqual(result.reason, 'message_anchor_verified_between_timestamps')
        self.assertEqual(self.moves, [])
        self.assertNotIn(('tail', 40), self.clicks)

    def test_older_exact_timestamp_stops_scrolling_even_when_anchor_body_missing(self):
        page = Page([bubble('older', 40), bubble('new', 200)], 'tail', 'tail')
        engine = self.engine(times={('tail', 40): '05/19 10:18:47',
                                    ('tail', 200): '05/19 10:23:36'})
        anchor = {'direction': 'incoming', 'text': 'missing', 'timestamp': '05/19 10:20:51'}
        result = engine.recover(page, anchor, had_checkpoint=True)
        self.assertEqual(result.reason, 'older_timestamp_reached_anchor_unverified')
        self.assertEqual(self.moves, [])
        self.assertEqual(result.messages, [])
        self.assertIsNone(result.anchor)

    def test_unreadable_latest_time_does_not_discard_verified_newer_prefix(self):
        tail = Page([bubble('first new', 40), bubble('unreadable latest', 100)], 'tail', 'tail')
        older = Page([dict(bubble('older', 40), visible_time_label='10:17'),
                      bubble('long original', 100), bubble('first new', 160)], 'older', 'older')
        engine = self.engine([older], {('tail', 40): '05/19 10:23:36'})
        anchor = {'direction': 'incoming', 'text': 'longoriginal', 'timestamp': '05/19 10:20:51'}
        result = engine.recover(tail, anchor, had_checkpoint=True)
        self.assertEqual(self.moves, ['up'])
        self.assertEqual([item['text'] for item in result.messages], ['first new'])
        self.assertEqual(result.anchor['timestamp'], '05/19 10:23:36')
        self.assertFalse(result.caught_up)

    def test_first_nonempty_observation_after_empty_baseline_is_new(self):
        engine = self.engine(times={('tail', 40): NEW_TIME})
        empty = Page([], 'empty', 'empty')
        self.assertEqual(engine.recover(empty, None).status, 'baseline')
        page = Page([bubble('first', 40)], 'tail', 'tail')
        result = engine.recover(page, None, had_checkpoint=True)
        self.assertEqual([item['text'] for item in result.messages], ['first'])

    def test_two_identical_bubbles_in_same_second_do_not_advance_anchor(self):
        page = Page([bubble('same', 40), bubble('same', 100)], 'tail', 'tail')
        engine = self.engine(times={('tail', 40): OLD_TIME, ('tail', 100): OLD_TIME})
        anchor = {'direction': 'incoming', 'text': 'same', 'timestamp': OLD_TIME}
        result = engine.recover(page, anchor, had_checkpoint=True)
        self.assertEqual(result.reason, 'message_timestamp_anchor_ambiguous')
        self.assertEqual(result.messages, [])
        self.assertIsNone(result.anchor)

    def test_workflow_persists_anchor_and_enqueues_new_occurrence_once(self):
        class Desktop:
            supports_backfill = True
            supports_timestamp_anchor = True

            def ensure_app(self, target):
                pass

            def chat_is_open(self, chat):
                return True

            def message_snapshot(self, chat):
                return path

            def reveal_message_time(self, page, box):
                return OLD_TIME if box[1] == 40 else NEW_TIME

            scroll_chat = Mock(side_effect=AssertionError('unexpected scroll'))

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'frame'
            desktop = Desktop()
            reader = Mock()
            target = Target('app.exe', 'App', 'Source')
            workflow = Workflow('source', target, desktop, reader, str(Path(temp) / 'db'))
            relay = Relay(workflow, Target('app.exe', 'App', 'Destination'), desktop, reader, 'demo')
            try:
                path.write_bytes(b'old')
                reader.extract_messages.return_value = [bubble('same', 40)]
                self.assertEqual(relay.collect()['relay_queued'], 0)
                path.write_bytes(b'new')
                reader.extract_messages.return_value = [bubble('same', 40), bubble('same', 100)]
                self.assertEqual(relay.collect()['relay_queued'], 1)
                self.assertEqual(relay.collect()['relay_queued'], 0)
                self.assertEqual(relay.queue_counts(), {'pending': 1})
            finally:
                workflow.close()
