from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from fxxk_desk.interference import UserInterference
from fxxk_desk.relay import _run


class WatchTakeoverTests(unittest.TestCase):
    def test_watch_restarts_from_window_selection_after_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'test.toml'
            config.write_text('''
[targets.source]
adapter="desktop_chat"
executable="app.exe"
window_title="Chat"
chat="Source"
[targets.dest]
adapter="desktop_chat"
executable="app.exe"
window_title="Chat"
chat="Destination"
[relays.test]
source="source"
destination="dest"
''', encoding='utf-8')
            image = root / 'page.png'
            image.write_bytes(b'baseline')
            backend = Mock()
            backend.ensure_app.side_effect = [UserInterference('Human input'), None]
            backend.chat_is_open.return_value = True
            backend.message_snapshot.return_value = image
            backend.extract_local_messages.return_value = [{'text': 'old', 'sender': 'Alice', 'direction': 'incoming', 'complete': True}]
            backend.interference.status.return_value = {'paused': True}
            args = SimpleNamespace(config=str(config), route='test', action='watch', send=False,
                                   interval=5, database=str(root / 'test.db'))
            with patch('fxxk_desk.chat.ChatDesktop', return_value=backend), patch('fxxk_desk.gateway.Gateway'), \
                 patch('fxxk_desk.relay.SummaryWorker', return_value=Mock(last_error=None)), patch('fxxk_desk.relay.time.sleep', side_effect=[None, KeyboardInterrupt]) as sleep, \
                 patch('builtins.print'):
                with self.assertRaises(KeyboardInterrupt):
                    _run(args)
            backend.wait_for_user.assert_called_once()
            self.assertEqual(backend.ensure_app.call_count, 2)
            self.assertEqual(len(sleep.call_args_list), 2)
            for call in sleep.call_args_list:
                self.assertGreater(call.args[0], 4)
                self.assertLessEqual(call.args[0], 5)
            backend.send_reply.assert_not_called()
            backend.close.assert_called_once()
