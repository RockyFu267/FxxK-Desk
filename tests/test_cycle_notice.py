import threading
import time
import unittest
from unittest.mock import Mock
from contextlib import nullcontext
from pathlib import Path
import tempfile

from fxxk_desk.cycle_notice import CycleNotice, announced_cycle
from fxxk_desk.interference import UserInterference
from fxxk_desk.region_monitor import RegionMonitor


class CycleNoticeTests(unittest.TestCase):
    def test_close_cancels_pending_tick(self):
        root = Mock()
        root.after.return_value = 'pending-tick'
        notice = CycleNotice(root, 3)
        notice.close()
        root.after_cancel.assert_called_once_with('pending-tick')
        root.after.reset_mock()
        notice.tick()
        root.after.assert_not_called()

    def test_countdown_becomes_working_notice_then_finished_notice_before_hiding(self):
        root = Mock()
        notice = CycleNotice(root, 3)
        pane = Mock()
        notice._show = lambda: notice.panes.append(pane)
        finished = threading.Event()
        errors = []
        stop = threading.Event()
        def work():
            try:
                notice(stop)
                finished.set()
            except UserInterference as error:
                errors.append(str(error))
        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.monotonic()+1
        while notice.requests.empty() and time.monotonic()<deadline:
            time.sleep(.001)
        notice.tick()
        self.assertFalse(finished.is_set())
        self.assertTrue(notice.panes)
        notice.active['deadline'] = time.monotonic()-1
        notice.tick()
        self.assertTrue(finished.wait(1))
        thread.join(1)
        self.assertEqual(notice.active['phase'], 'working')
        self.assertTrue(notice.panes)
        pane.destroy.assert_not_called()
        finisher = threading.Thread(target=lambda: notice.finish_cycle())
        finisher.start()
        deadline = time.monotonic()+1
        while notice.requests.empty() and time.monotonic()<deadline:
            time.sleep(.001)
        notice.tick()
        self.assertEqual(notice.active['phase'], 'finished')
        self.assertTrue(notice.panes)
        notice.active['deadline'] = time.monotonic()-1
        notice.tick()
        finisher.join(1)
        self.assertFalse(finisher.is_alive())
        pane.destroy.assert_called_once()
        self.assertFalse(notice.panes)
        self.assertFalse(errors)
        notice.close()

    def test_completion_and_error_are_signalled_after_action_has_ended(self):
        for error, expected in ((None, 'completed'), (RuntimeError('failure'), 'error'),
                                (UserInterference('paused'), 'paused')):
            notice = Mock()
            order = []
            notice.side_effect = lambda stop: order.append('before')
            notice.finish_cycle.side_effect = lambda outcome: order.append(outcome)
            try:
                with announced_cycle(notice, threading.Event()):
                    order.append('action')
                    notice.finish_cycle.assert_not_called()
                    if error:
                        raise error
            except (RuntimeError, UserInterference):
                if error is None:
                    raise
            self.assertEqual(order, ['before', 'action', expected])

    def test_cancel_during_countdown_never_reaches_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()
            def countdown(stop):
                entered.set()
                stop.wait(1)
                raise UserInterference('cancelled')
            capture = Mock()
            monitor = RegionMonitor('unused', Path(directory)/'image.png', .1,
                                    Path(directory)/'status.json', capture, nullcontext, before_cycle=countdown)
            monitor.start()
            self.assertTrue(entered.wait(1))
            monitor.stop()
            monitor.thread.join(1)
            capture.assert_not_called()
            self.assertIsNone(monitor.error)

    def test_first_and_next_round_both_wait_for_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            order = []
            captured = threading.Event()
            def countdown(stop):
                order.append('notice')
            def capture(config, output):
                order.append('capture')
                Path(output).write_bytes(b'image')
                if order.count('capture') >= 2:
                    captured.set()
            monitor = RegionMonitor('unused', Path(directory)/'image.png', .1,
                                    Path(directory)/'status.json', capture, nullcontext, before_cycle=countdown)
            monitor.start()
            self.assertTrue(captured.wait(2))
            monitor.stop()
            monitor.thread.join(1)
            self.assertEqual(order[:4], ['notice', 'capture', 'notice', 'capture'])
