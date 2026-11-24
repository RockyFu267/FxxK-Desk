from pathlib import Path
import tempfile
import unittest

from fxxk_desk.console_config import ConsoleConfig
from fxxk_desk.relay import load_route
from fxxk_desk.chat import ChatDesktop
from fxxk_desk.relay_monitor import TaskStopped
import threading
from unittest.mock import patch
from fxxk_desk.relay_monitor import RelayMonitor


class ConsoleConfigTests(unittest.TestCase):
    def fixture(self, directory):
        path = Path(directory) / 'config.local.toml'
        path.write_text(Path('config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        return path

    def test_update_groups_preserves_calibration_and_webhook_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            config = ConsoleConfig(path)
            config.save('监控群', '通知群', config.source.executable, config.source.window_title,
                        True, {'human_idle_seconds': 8, 'backfill_enabled': False})
            route, source, destination = load_route(path, config.route)
            self.assertEqual((source.chat, destination.chat), ('监控群', '通知群'))
            self.assertEqual(source.message_rect, [400, 64, 1130, 900])
            self.assertEqual(source.human_idle_seconds, 8)
            self.assertEqual(route['webhook_env'], 'SOURCE_WEBHOOK_URL')
            self.assertTrue(route['auto_send'])

    def test_invalid_update_leaves_original_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            config = ConsoleConfig(path)
            before = path.read_bytes()
            for source, destination, advanced in [('same', 'same', {}), ('one', 'two', {'backfill_max_pages': 0})]:
                with self.assertRaises(ValueError):
                    config.save(source, destination, config.source.executable, config.source.window_title, False, advanced)
                self.assertEqual(path.read_bytes(), before)

    def test_console_stop_interrupts_input_guard(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.stop_event = threading.Event()
        backend.stop_event.set()
        with self.assertRaises(TaskStopped):
            backend.guard()

    def test_relay_task_receives_ui_options_and_stops_cooperatively(self):
        seen = []
        ready = threading.Event()
        def fake_run(args):
            seen.append(args)
            ready.set()
            args.stop_event.wait(2)
        with patch('fxxk_desk.relay.run', fake_run):
            worker = RelayMonitor('private.toml', 'route', 7, False, 'region.json', 'status.json')
            worker.start()
            self.assertTrue(ready.wait(1))
            worker.stop()
            worker.thread.join(1)
        self.assertFalse(worker.alive)
        self.assertEqual(seen[0].interval, 7)
        self.assertFalse(seen[0].send)
        self.assertEqual(seen[0].region_file, 'region.json')
