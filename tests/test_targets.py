from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from fxxk_desk.targets import Target, load_target, matches_chat_header, open_target


class TargetTests(unittest.TestCase):
    def test_config_requires_adapter_and_nonempty_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[targets.a]\nadapter="desktop_chat"\nexecutable="a.exe"\nwindow_title="A"\nchat=""', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_target(path, "a")

    def test_header_is_exact_with_optional_member_count(self):
        self.assertTrue(matches_chat_header("Example Group (12)", "Example Group"))
        self.assertFalse(matches_chat_header("Example Group Backup", "Example Group"))

    def test_already_open_does_not_search_or_click(self):
        backend = Mock()
        backend.chat_is_open.return_value = True
        result = open_target(Target("a.exe", "A", "Group"), backend)
        self.assertEqual(result["status"], "chat_verified")
        backend.search.assert_not_called()
        backend.open_exact_result.assert_not_called()

    def test_open_requires_post_action_header(self):
        backend = Mock()
        backend.chat_is_open.side_effect = [False, True]
        backend.open_exact_result.side_effect = [False, True]
        target = Target("a.exe", "A", "Group")
        result = open_target(target, backend)
        backend.search.assert_called_once_with(target)
        self.assertFalse(result["already_open"])

    def test_ambiguity_stops_without_verification_claim(self):
        backend = Mock()
        backend.chat_is_open.return_value = False
        backend.open_exact_result.side_effect = RuntimeError("ambiguous")
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            open_target(Target("a.exe", "A", "Group"), backend)
