import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from fxxk_desk.relay import Relay, RelayModels, SummaryWorker, ready_for_delivery, source_chat_cycle
from fxxk_desk.targets import Target
from fxxk_desk.workflow import Workflow
from fxxk_desk.interference import UserInterference


def message(text, sender="Alice", direction="incoming"):
    return {"text": text, "sender": sender, "direction": direction, "complete": True}


class RelayTests(unittest.TestCase):
    def test_long_original_bypasses_model_and_sends_full_text_once(self):
        body = ''.join(f'Original row {i} with details\n' for i in range(32))
        self.collect([message('old')], b'first')
        self.collect([message('old'), message(body)], b'second')
        draft = self.relay.drafts()[0]
        self.gateway.reply.assert_not_called()
        with patch('fxxk_desk.relay.open_target'):
            sent = self.relay.send(draft['id'])
        parts = [call.args[1] for call in self.backend.send_reply.call_args_list]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].split('\n', 1)[1], body)
        self.assertEqual(sent['state'], 'sent')
        self.assertEqual(self.source.db.execute('SELECT next_part FROM relay_outbox').fetchone()[0], len(parts))

    def test_legacy_partial_delivery_keeps_numbered_boundaries(self):
        body = ''.join(f'Original row {i} with details\n' for i in range(32))
        item = message(body)
        self.collect([message('old')], b'first')
        self.collect([message('old'), item], b'second')
        draft = self.relay.drafts()[0]
        expected = self.relay.original_parts(item, draft['id'], 'Alice', legacy=True)
        self.assertGreater(len(expected), 1)
        with self.source.db:
            self.source.db.execute('UPDATE relay_outbox SET next_part=1,delivery_format=NULL,text=? WHERE id=?',
                                   (expected[0], draft['id']))
        with patch('fxxk_desk.relay.open_target'):
            self.relay.send(draft['id'])
        self.assertEqual([call.args[1] for call in self.backend.send_reply.call_args_list], expected[1:])

    def test_cycle_restores_source_even_when_delivery_branch_continues(self):
        events = []
        source = object()

        class Desktop:
            passive_observation = False

            def return_to_latest(self):
                events.append('latest')

        backend = Desktop()
        with patch('fxxk_desk.relay.open_target', side_effect=lambda target, _: events.append(target)):
            for _ in range(1):
                with source_chat_cycle(backend, source, None) as cycle:
                    cycle['restore'] = True
                    events.append('destination')
                    continue
        self.assertEqual(events, ['destination', source, 'latest'])

    def test_cycle_does_not_move_mouse_after_interruption(self):
        class Desktop:
            passive_observation = False

            def return_to_latest(self):
                pass

        with patch('fxxk_desk.relay.open_target') as open_chat:
            with self.assertRaises(UserInterference):
                with source_chat_cycle(Desktop(), object(), None):
                    raise UserInterference('interrupted')
            open_chat.assert_not_called()

    def test_summary_receives_chat_content_without_capture_metadata(self):
        gateway = Mock()
        models = RelayModels(Mock(), gateway)
        models.reply(dict(message('hello'), bubble_rect=[1, 2, 3, 4], ocr_confidence=.99), 'summarize')
        gateway.reply.assert_called_once_with({'text': 'hello', 'sender': 'Alice', 'direction': 'incoming'}, 'summarize')

    def test_committed_capture_is_enqueued_without_recapturing(self):
        self.collect([message('old')], b'first')
        self.image.write_bytes(b'second')
        self.gateway.extract_messages.return_value = [message('old'), message('new')]
        self.source.collect()
        with patch.object(self.source, 'collect', side_effect=AssertionError('must not recapture')):
            self.assertEqual(self.relay.enqueue_captured()['relay_queued'], 1)
        self.assertEqual(len(self.relay.drafts()), 1)

    def test_test_delivery_allowlist_excludes_unrelated_drafts(self):
        self.collect([message('old')], b'first')
        self.collect([message('old'), message('test FD-abcdef1234-1'), message('unrelated')], b'second')
        self.relay.drafts()
        drafts = ready_for_delivery(self.relay, SimpleNamespace(send_markers=['FD-abcdef1234-1']))
        self.assertEqual(len(drafts), 1)
        payload = self.source.db.execute('SELECT m.payload FROM messages m JOIN relay_outbox o ON o.source_id=m.id WHERE o.id=?', (drafts[0]['id'],)).fetchone()[0]
        self.assertIn('FD-abcdef1234-1', payload)

    def test_uncertain_marker_summary_never_invents_damaged_body(self):
        gateway = Mock()
        models = RelayModels(Mock(), gateway)
        result = models.reply(dict(message('识别不确定 FD-abcdef1234-2'), source='ocr_uncertain_marker'), 'summary')
        self.assertIn('FD-abcdef1234-2', result)
        self.assertIn('识别不确定', result)
        gateway.reply.assert_not_called()
    def assert_interrupted_phase(self, phase, expected):
        self.queue()
        draft = self.relay.drafts()[0]

        def interrupt(*_):
            self.backend.send_phase = phase
            raise UserInterference('User moved mouse')

        self.backend.send_reply.side_effect = interrupt
        with patch('fxxk_desk.relay.open_target'):
            with self.assertRaises(UserInterference):
                self.relay.send(draft['id'])
        row = self.source.db.execute('SELECT state FROM relay_outbox').fetchone()
        self.assertEqual(row[0], expected)

    def test_takeover_before_typing_keeps_retryable_draft(self):
        self.assert_interrupted_phase('preparing', 'draft')

    def test_takeover_during_typing_requires_draft_review(self):
        self.assert_interrupted_phase('editing', 'blocked_input')

    def test_takeover_during_send_is_never_automatically_retried(self):
        self.assert_interrupted_phase('sending', 'unknown')
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "frame.png"
        self.backend, self.gateway = Mock(), Mock()
        self.backend.message_snapshot.return_value = self.image
        self.gateway.reply.return_value = "CPU usage exceeds 90%; test only."
        self.source = Workflow("source", Target("app.exe", "Chat", "Source"), self.backend, self.gateway,
                               str(Path(self.temp.name) / "relay.db"))
        self.relay = Relay(self.source, Target("app.exe", "Chat", "Destination"), self.backend, self.gateway, "route")
        self.navigation = patch("fxxk_desk.workflow.open_target")
        self.navigation.start()

    def tearDown(self):
        self.source.close()
        self.navigation.stop()
        self.temp.cleanup()

    def collect(self, items, frame):
        self.image.write_bytes(frame)
        self.gateway.extract_messages.return_value = items
        return self.relay.collect()

    def queue(self):
        self.collect([message("old")], b"first")
        return self.collect([message("old"), message("new", "Bot")], b"second")

    def test_baseline_does_not_forward_old_messages(self):
        self.collect([message("old")], b"first")
        self.assertEqual(self.relay.drafts(), [])

    def test_summary_keeps_source_and_sender_outside_model_output(self):
        self.queue()
        draft = self.relay.drafts()[0]
        self.assertIn("Source", draft["text"])
        self.assertIn("Bot", draft["text"])
        self.assertIn(self.gateway.reply.return_value, draft["text"])

    def test_every_new_message_is_queued_including_outgoing(self):
        self.collect([message("old")], b"first")
        result = self.collect([message("old"), message("one"), message("two", None, "outgoing")], b"second")
        self.assertEqual(result["relay_queued"], 2)
        self.assertEqual(len(self.relay.drafts()), 2)

    def test_identical_snapshot_does_not_forward_twice(self):
        self.queue()
        self.relay.collect()
        self.assertEqual(len(self.relay.drafts()), 1)

    @patch("fxxk_desk.relay.open_target")
    def test_send_only_targets_destination(self, navigation):
        self.queue()
        draft = self.relay.drafts()[0]
        self.relay.send(draft["id"])
        self.assertEqual(navigation.call_args.args[0].chat, "Destination")
        self.backend.send_reply.assert_called_once_with("Destination", draft["text"])
        with self.assertRaises(ValueError):
            self.relay.send(draft["id"])

    @patch("fxxk_desk.relay.open_target")
    def test_uncertain_send_is_not_retried(self, _):
        self.queue()
        draft = self.relay.drafts()[0]
        self.backend.send_reply.side_effect = RuntimeError("lost focus")
        with self.assertRaises(RuntimeError):
            self.relay.send(draft["id"])
        with self.assertRaises(ValueError):
            self.relay.send(draft["id"])

    @patch("fxxk_desk.relay.open_target")
    def test_recovery_requires_explicit_confirmation_and_records_audit(self, _):
        self.queue()
        draft = self.relay.drafts()[0]
        self.backend.send_reply.side_effect = RuntimeError("lost focus")
        with self.assertRaises(RuntimeError):
            self.relay.send(draft["id"])
        with self.assertRaises(ValueError):
            self.relay.recover(draft["id"])
        self.assertEqual(self.relay.recover(draft["id"], True)["state"], "pending")
        self.assertEqual(self.source.db.execute("SELECT COUNT(*) FROM relay_recoveries").fetchone()[0], 1)
        self.backend.send_reply.assert_called_once()

    def test_offline_rule_summary_preserves_test_facts(self):
        gateway = Mock()
        gateway.reply.side_effect = RuntimeError("offline")
        models = RelayModels(Mock(), gateway)
        result = models.reply(message("[FxxK-Desk闭环测试 FD-123456abcd] 模拟告警：测试主机 demo-node 的CPU使用率持续5分钟超过90%。这是模拟数据。"), "summary")
        self.assertIn("规则摘要", result)
        self.assertIn("demo-node", result)
        self.assertIn("FD-123456abcd", result)

    def test_crash_after_capture_before_enqueue_does_not_lose_message(self):
        self.collect([message('old')], b'first')
        self.image.write_bytes(b'second')
        self.gateway.extract_messages.return_value = [message('old'), message('new')]
        self.source.collect()  # Capture committed; enqueue did not happen.
        result = self.relay.collect()  # Next capture is unchanged.
        self.assertEqual(result['relay_queued'], 1)
        self.assertEqual(len(self.relay.drafts()), 1)

    def test_backpressure_keeps_cursor_before_unqueued_message(self):
        self.relay.max_pending = 1
        self.collect([message('old')], b'first')
        result = self.collect([message('old'), message('one'), message('two')], b'second')
        self.assertTrue(result['backpressure'])
        self.assertEqual(result['relay_queued'], 1)
        with self.source.db:
            self.source.db.execute("UPDATE relay_outbox SET state='sent'")
        result = self.relay.collect()
        self.assertEqual(result['relay_queued'], 1)
        self.assertEqual(self.source.db.execute('SELECT COUNT(*) FROM relay_outbox').fetchone()[0], 2)

    def test_slow_model_does_not_block_capture_or_hold_db_write_lock(self):
        started, release = threading.Event(), threading.Event()
        gateway = Mock()

        def slow_reply(*_):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test timed out')
            return 'Summary'

        gateway.reply.side_effect = slow_reply
        self.queue()
        database = str(Path(self.temp.name) / 'relay.db')

        def factory():
            workflow = Workflow('source', Target('app.exe', 'Chat', 'Source'), None, gateway, database)
            return Relay(workflow, Target('app.exe', 'Chat', 'Destination'), None, gateway, 'route')

        worker = SummaryWorker(factory)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            result = self.collect([message('old'), message('new', 'Bot'), message('arrived while model busy')], b'third')
            self.assertEqual(result['relay_queued'], 1)
            self.assertFalse(release.is_set())
            self.assertEqual(self.source.db.execute('SELECT COUNT(*) FROM relay_outbox').fetchone()[0], 2)
            self.backend.send_reply.assert_not_called()
        finally:
            release.set()
            worker.stop()
