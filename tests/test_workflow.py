from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fxxk_desk.targets import Target
from fxxk_desk.workflow import Workflow, overlap


def message(text, direction="incoming"):
    return {"text": text, "direction": direction, "sender": None, "complete": True}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "frame.png"
        self.path.write_bytes(b"first")
        self.backend = Mock()
        self.backend.message_snapshot.return_value = self.path
        self.gateway = Mock()
        self.gateway.reply.return_value = "Received test."
        self.workflow = Workflow("test", Target("app.exe", "Chat", "Group"), self.backend, self.gateway,
                                 str(Path(self.directory.name) / "test.sqlite3"))
        self.navigation = patch("fxxk_desk.workflow.open_target")
        self.navigation.start()

    def tearDown(self):
        self.workflow.close()
        self.navigation.stop()
        self.directory.cleanup()

    def observe(self, messages, image):
        self.path.write_bytes(image)
        self.gateway.extract_messages.return_value = messages
        return self.workflow.collect()

    def create_draft(self):
        self.observe([message("old")], b"baseline")
        self.observe([message("old"), message("new")], b"new")
        return self.workflow.draft("Reply briefly")

    def test_baseline_never_replies_to_old_messages(self):
        self.observe([message("old")], b"old")
        self.assertEqual(self.workflow.draft("Reply")["status"], "no_new_incoming_message")

    def test_unchanged_image_does_not_call_model_again(self):
        self.observe([message("old")], b"old")
        self.assertEqual(self.workflow.collect()["status"], "unchanged")
        self.gateway.extract_messages.assert_called_once()

    def test_identical_repeated_bubbles_are_preserved_by_sequence(self):
        self.assertEqual(overlap(["a", "b"], ["b", "b"]), 1)
        self.observe([message("a"), message("b")], b"first")
        result = self.observe([message("b"), message("b")], b"second")
        self.assertEqual(result["new_messages"], 1)

    def test_gap_is_recorded_but_not_auto_replied(self):
        self.observe([message("old")], b"first")
        self.observe([message("unrelated")], b"second")
        self.assertEqual(self.workflow.draft("Reply")["status"], "no_new_incoming_message")

    def test_outgoing_does_not_trigger_reply(self):
        self.observe([message("old")], b"first")
        self.observe([message("old"), message("own", "outgoing")], b"second")
        self.assertEqual(self.workflow.draft("Reply")["status"], "no_new_incoming_message")

    def test_successful_send_cannot_be_repeated(self):
        draft = self.create_draft()
        self.assertEqual(self.workflow.send(draft["id"])["state"], "sent")
        with self.assertRaises(ValueError):
            self.workflow.send(draft["id"])
        self.backend.send_reply.assert_called_once()

    def test_unknown_send_is_not_retried(self):
        draft = self.create_draft()
        self.backend.send_reply.side_effect = RuntimeError("Lost desktop")
        with self.assertRaises(RuntimeError):
            self.workflow.send(draft["id"])
        row = self.workflow.db.execute("SELECT state FROM outbox").fetchone()
        self.assertEqual(row["state"], "unknown")
        with self.assertRaises(ValueError):
            self.workflow.send(draft["id"])
