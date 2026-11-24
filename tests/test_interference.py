import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from types import SimpleNamespace
from fxxk_desk.targets import Target

from fxxk_desk.interference import InputArbiter, UserInterference, DesktopUnavailable
from fxxk_desk.native_input import TaggedInputs, InputMonitor
from fxxk_desk.chat import ChatDesktop
from fxxk_desk.interference import ActionDeferred


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

    def test_other_monitor_pointer_does_not_interrupt_observation_but_defers_actions(self):
        self.gate.set_region((-2160, -815, 0, 3025))
        for kind in ('mouse_move', 'mouse', 'wheel'):
            self.assertFalse(self.gate.observe_pointer(kind, (100, 100), 0))
            self.gate.check()
        self.assertFalse(self.gate.action_ready())
        self.clock.now += 5
        self.assertTrue(self.gate.action_ready())

    def test_region_boundary_and_real_remote_input_still_pause(self):
        self.gate.set_region((-2160, -815, 0, 3025))
        self.assertFalse(self.gate.observe_pointer('wheel', (0, 100), 9876))
        self.assertTrue(self.gate.observe_pointer('wheel', (-1, 100), 9876))
        with self.assertRaises(UserInterference):
            self.gate.check()

    def test_keyboard_in_other_app_keeps_observation_and_blocks_actions(self):
        self.gate.set_region((-2160, -815, 0, 3025))
        self.assertFalse(self.gate.observe_keyboard(0, target_foreground=False))
        self.gate.check()
        self.assertFalse(self.gate.action_ready())
        self.assertTrue(self.gate.observe_keyboard(0, target_foreground=True))
        with self.assertRaises(UserInterference):
            self.gate.check()

    def test_own_events_outside_region_do_not_extend_human_idle(self):
        self.gate.set_region((-2160, -815, 0, 3025))
        self.gate.observe_pointer('mouse_move', (100, 100), 1234)
        self.gate.observe_keyboard(1234, target_foreground=False)
        self.assertTrue(self.gate.action_ready())

    def test_automation_priority_does_not_yield_to_pointer_or_keyboard(self):
        self.gate.set_region((-2160, -815, 0, 3025))
        self.gate.automation_priority = True
        self.assertFalse(self.gate.observe_pointer('mouse', (-100, 100), 0))
        self.assertFalse(self.gate.observe_pointer('wheel', (100, 100), 0))
        self.assertFalse(self.gate.observe_keyboard(0, target_foreground=True))
        self.gate.check()
        self.assertTrue(self.gate.action_ready())
        self.gate.observe('desktop_unavailable', None)
        with self.assertRaises(UserInterference):
            self.gate.check()

    def test_passive_read_permission_does_not_authorize_background_input(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.guard = Mock()
        backend.window = Mock(id=100)
        backend.desktop = Mock()
        backend.desktop.u.GetForegroundWindow.return_value = 200
        backend.interference = self.gate
        with self.assertRaises(ActionDeferred):
            backend.action_guard()
        backend.desktop.u.GetForegroundWindow.return_value = 100
        self.gate.observe_pointer('mouse_move', (300, 300), 0)
        with self.assertRaises(ActionDeferred):
            backend.action_guard()
        self.clock.now += 5
        backend.action_guard()

    def test_explicit_start_allows_only_one_idle_initial_activation(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.interference = self.gate
        backend.monitor = Mock()
        backend.desktop = Mock()
        backend.desktop.windows.return_value = [SimpleNamespace(id=100, pid=42, title='Chat')]
        backend.desktop.u.GetForegroundWindow.return_value = 200
        backend.desktop.activate.side_effect = lambda _: setattr(backend.desktop.u.GetForegroundWindow, 'return_value', 100)
        backend.uia = Mock()
        backend.guard = Mock()
        backend.region_file = 'region.json'
        backend.startup_focus_pending = True
        backend.passive_observation = True
        kernel = Mock()
        kernel.OpenProcess.return_value = 42
        def executable(handle, flags, buffer, size):
            buffer.value = str(Path('app.exe').resolve())
            return True
        kernel.QueryFullProcessImageNameW.side_effect = executable
        with patch('ctypes.WinDLL', return_value=kernel):
            backend.ensure_app(Target('app.exe', 'Chat', 'Group'))
            self.assertFalse(backend.startup_focus_pending)
            self.assertFalse(backend.passive_observation)
            backend.desktop.activate.assert_called_once()
            backend.desktop.u.GetForegroundWindow.return_value = 200
            with self.assertRaises(ActionDeferred):
                backend.ensure_app(Target('app.exe', 'Chat', 'Group'))
            backend.desktop.activate.assert_called_once()

    def test_input_just_before_initial_activation_blocks_focus_change(self):
        backend = ChatDesktop.__new__(ChatDesktop)
        backend.monitor = Mock()
        backend.interference = self.gate
        backend.region_file = 'region.json'
        self.gate.set_region((-2160, -815, 0, 3025))
        self.gate.observe_pointer('mouse', (100, 100), 0)
        with self.assertRaises(ActionDeferred):
            backend.activation_guard()

    def test_secure_desktop_prevents_even_read_only_capture(self):
        monitor = object.__new__(InputMonitor)
        monitor.arbiter = self.gate
        monitor.failure = None
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = True
        monitor.user = Mock()
        monitor.user.GetCursorPos.return_value = 1
        monitor._input_desktop_is_default = Mock(return_value=False)
        with self.assertRaises(DesktopUnavailable):
            monitor.check()

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

    def test_pointer_parks_before_and_after_a_complete_action(self):
        writer = self.writer()
        events = []
        writer.park = lambda: events.append('park')
        writer._hotkey = lambda *keys: events.append('hotkey')
        writer.hotkey('ctrl', 'f')
        self.assertEqual(events, ['park', 'hotkey', 'park'])

    def test_failed_action_does_not_force_pointer_restore(self):
        writer = self.writer()
        writer.park = Mock()
        writer._hotkey = Mock(side_effect=DesktopUnavailable('locked'))
        with self.assertRaises(DesktopUnavailable):
            writer.hotkey('ctrl', 'f')
        writer.park.assert_called_once()

    def test_click_stops_before_button_if_pointer_moved_away(self):
        writer = self.writer()
        def cursor(pointer):
            pointer._obj.x, pointer._obj.y = 1, 2
            return 1
        writer.user.GetCursorPos.side_effect = cursor
        with self.assertRaises(ActionDeferred):
            writer._send([writer._mouse(0x0002), writer._mouse(0x0004)], expected_cursor=(100, 100))
        writer.user.SendInput.assert_not_called()

    def test_remote_click_button_packets_keep_absolute_virtual_desktop_position(self):
        writer = self.writer()
        writer.user.GetSystemMetrics.side_effect = lambda key: {76: -2160, 77: -846, 78: 8160, 79: 3871}[key]
        def cursor(pointer):
            pointer._obj.x, pointer._obj.y = -1762, -544
            return 1
        writer.user.GetCursorPos.side_effect = cursor
        packets = []
        def send(count, records, size):
            packets.append([(records[i].mi.dx, records[i].mi.dy, records[i].mi.dwFlags) for i in range(count)])
            return count
        writer.user.SendInput.side_effect = send
        writer.click((-1762, -544))
        self.assertEqual([len(packet) for packet in packets], [1, 2])
        self.assertEqual(packets[0][0][:2], packets[1][0][:2])
        self.assertEqual(packets[1][0][:2], packets[1][1][:2])
        self.assertEqual(packets[1][0][2] & 0xC003, 0xC003)
        self.assertEqual(packets[1][1][2] & 0xC005, 0xC005)

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
