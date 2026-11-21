import unittest
from unittest.mock import Mock

from fxxk_desk.interference import InputArbiter, UserInterference, DesktopUnavailable
from fxxk_desk.native_input import TaggedInputs, InputMonitor


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class InterferenceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = InputArbiter(idle_seconds=5, own_tag=1234, clock=self.clock)
        self.gate.begin_operation()

    def test_own_mouse_and_keyboard_do_not_pause(self):
        self.assertFalse(self.gate.observe('mouse', 1234))
        self.assertFalse(self.gate.observe('keyboard', 1234))
        self.gate.check()
        self.assertEqual(self.gate.status()['own_events'], 2)

    def test_other_injected_remote_input_is_not_ignored(self):
        self.assertTrue(self.gate.observe('wheel', 9876))
        with self.assertRaises(UserInterference):
            self.gate.check()

    def test_duplicate_rdp_cursor_notification_is_not_a_takeover(self):
        self.gate.observe_pointer('mouse_move', (100, 100), 1234)
        self.assertFalse(self.gate.observe_pointer('mouse_move', (100, 100), 0))
        self.gate.check()
        self.assertTrue(self.gate.observe_pointer('mouse_move', (101, 100), 0))
        with self.assertRaises(UserInterference):
            self.gate.check()

    def test_stationary_wheel_and_button_are_still_takeovers(self):
        self.gate.observe_pointer('mouse_move', (100, 100), 1234)
        self.assertTrue(self.gate.observe_pointer('wheel', (100, 100), 0))

    def test_repeated_human_input_extends_idle_wait(self):
        self.gate.observe('mouse', 0)
        self.clock.now += 4
        self.gate.observe('keyboard', 0)
        self.clock.now += 4
        self.assertFalse(self.gate.resumable())
        self.clock.now += 1
        self.assertTrue(self.gate.resumable())

    def test_idle_does_not_authorize_reusing_old_coordinates(self):
        self.gate.observe('mouse', 0)
        self.clock.now += 6
        with self.assertRaises(UserInterference):
            self.gate.check()
        self.gate.begin_operation()
        self.gate.check()

    def test_user_in_another_app_prevents_focus_stealing(self):
        self.gate.observe('keyboard', 0)
        self.clock.now += 6
        self.assertFalse(self.gate.resumable(target_foreground=False))
        with self.assertRaises(UserInterference):
            self.gate.begin_operation(target_foreground=False)
        self.gate.begin_operation(target_foreground=True)
        self.gate.check()

    def writer(self):
        writer = object.__new__(TaggedInputs)
        writer.arbiter = self.gate
        writer.monitor = Mock()
        writer.monitor.check.side_effect = self.gate.check
        writer.user = Mock()
        writer.user.GetAsyncKeyState.return_value = 0
        writer.user.SendInput.return_value = 2
        return writer

    def test_no_native_input_after_user_takes_over(self):
        writer = self.writer()
        self.gate.observe('mouse', 0)
        with self.assertRaises(UserInterference):
            writer.type_text('ABC', pause=0)
        writer.user.SendInput.assert_not_called()

    def test_typing_stops_after_the_interrupted_character(self):
        writer = self.writer()

        def send(*_):
            self.gate.observe('keyboard', 0)
            return 2

        writer.user.SendInput.side_effect = send
        with self.assertRaises(UserInterference):
            writer.type_text('ABC', pause=0)
        self.assertEqual(writer.user.SendInput.call_count, 1)

    def test_focus_guard_is_checked_between_characters(self):
        writer = self.writer()
        writer.context_guard = Mock(side_effect=[None, UserInterference('Focus changed')])
        with self.assertRaises(UserInterference):
            writer.type_text('ABC', pause=0)
        self.assertEqual(writer.user.SendInput.call_count, 1)

    def test_held_modifier_prevents_even_the_first_event(self):
        writer = self.writer()
        writer.user.GetAsyncKeyState.return_value = 0x8000
        with self.assertRaises(UserInterference):
            writer.type_text('ABC', pause=0)
        writer.user.SendInput.assert_not_called()

    def test_unavailable_rdp_desktop_pauses_before_injection(self):
        monitor = object.__new__(InputMonitor)
        monitor.arbiter = self.gate
        monitor.failure = None
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = True
        monitor.user = Mock()
        monitor.user.GetCursorPos.return_value = 0
        with self.assertRaises(DesktopUnavailable):
            monitor.check()
        self.assertEqual(self.gate.status()['reason'], 'desktop_unavailable')

    def test_dead_monitor_disallows_input(self):
        monitor = object.__new__(InputMonitor)
        monitor.failure = None
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = False
        with self.assertRaises(RuntimeError):
            monitor.ensure_healthy()

    def test_unicode_packets_keep_the_own_input_marker(self):
        writer = self.writer()
        packets = []

        def send(count, records, _):
            packets.extend((records[i].ki.wScan, records[i].ki.dwFlags, records[i].ki.dwExtraInfo) for i in range(count))
            return count

        writer.user.SendInput.side_effect = send
        writer.type_text('\U0001f642', pause=0)
        self.assertEqual([p[0] for p in packets], [0xD83D, 0xD83D, 0xDE42, 0xDE42])
        self.assertTrue(all(p[2] == 1234 for p in packets))
        self.assertFalse(self.gate.events)
