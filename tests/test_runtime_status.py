from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fxxk_desk.runtime_status import RuntimeStatus, read_status
from fxxk_desk.region_monitor import RegionMonitor


class RuntimeStatusTests(unittest.TestCase):
    def test_start_refresh_keeps_timer_and_shows_running_worker(self):
        from fxxk_desk.region_status_ui import RegionStatusPanel

        panel = Mock()
        panel.poll_id = 'scheduled-poll'
        panel.worker.alive = True
        panel.worker.error = None
        panel.stopping = panel.closing = False
        panel.config_widgets = []
        with patch('fxxk_desk.region_status_ui.read_status', side_effect=[
                {'state': 'stopped'}, {'state': 'observing'}, {'state': 'watching'}]):
            RegionStatusPanel.poll(panel, schedule=False)
            panel.headline.set.assert_called_with('正在启动')
            panel.root.after_cancel.assert_not_called()
            RegionStatusPanel.poll(panel)
            panel.headline.set.assert_called_with('执行中 · 正在观察')
            RegionStatusPanel.poll(panel)
            panel.headline.set.assert_called_with('运行等待中 · 等待下一轮')

    def test_saved_config_without_runtime_does_not_mean_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(read_status(Path(directory) / 'missing.json')['state'], 'idle')

    def test_stale_and_dead_tasks_are_not_shown_as_running(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            path.write_text(json.dumps({'state': 'watching', 'pid': 123, 'heartbeat_at': 100}))
            self.assertEqual(read_status(path, now=120, alive=lambda _: True)['state'], 'stale')
            self.assertEqual(read_status(path, now=101, alive=lambda _: False)['state'], 'interrupted')

    def test_terminal_status_does_not_require_a_live_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            path.write_text(json.dumps({'state': 'stopped', 'pid': 123, 'heartbeat_at': 1}))
            self.assertEqual(read_status(path, now=500, alive=lambda _: False)['state'], 'stopped')

    def test_heartbeat_and_final_state_are_real(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            with RuntimeStatus('test', path) as status:
                status.update('watching', capture_count=2)
                self.assertEqual(read_status(path)['state'], 'watching')
                self.assertEqual(read_status(path)['capture_count'], 2)
            self.assertEqual(read_status(path)['state'], 'stopped')

    def test_task_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            with self.assertRaises(RuntimeError):
                with RuntimeStatus('test', path):
                    raise RuntimeError('test')
            self.assertEqual(read_status(path)['state'], 'error')
            self.assertEqual(read_status(path)['detail'], 'test')
            self.assertEqual(read_status(path)['error_type'], 'RuntimeError')

    def test_sampler_only_publishes_actual_completed_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = threading.Event()
            def capture(config, output):
                Path(output).write_bytes(b'test image')
                captured.set()
            monitor = RegionMonitor(root / 'region.json', root / 'latest.png', 0.2,
                                    root / 'status.json', capture, nullcontext)
            monitor.start()
            self.assertTrue(captured.wait(2))
            monitor.stop()
            monitor.thread.join(2)
            status = read_status(root / 'status.json')
            self.assertEqual(status['state'], 'stopped')
            self.assertGreaterEqual(status['capture_count'], 1)
            self.assertTrue((root / 'latest.png').exists())

    def test_obstructed_panel_pauses_instead_of_capturing_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def forbidden(*_):
                raise AssertionError('Should not capture an obstructed region')
            monitor = RegionMonitor(root / 'region.json', root / 'latest.png', 0.2,
                                    root / 'status.json', forbidden, nullcontext, lambda: True)
            monitor.start()
            monitor.stop()
            monitor.thread.join(2)
            self.assertFalse((root / 'latest.png').exists())
            self.assertEqual(read_status(root / 'status.json')['capture_count'], 0)
