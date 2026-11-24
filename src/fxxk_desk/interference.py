"""Cooperative desktop ownership; no keystroke or pointer-content recording."""
from collections import deque
import secrets
import threading
import time


class UserInterference(RuntimeError):
    """The current visual/action context must be discarded before resuming."""


class DesktopUnavailable(UserInterference):
    """The Windows input desktop is unavailable (for example an RDP pause)."""


class ActionDeferred(UserInterference):
    """Observation may continue, but shared mouse/keyboard are currently busy."""


class InputArbiter:
    def __init__(self, idle_seconds=5, own_tag=None, clock=time.monotonic, initial_idle=float('inf')):
        self.idle_seconds = idle_seconds
        self.own_tag = own_tag or (secrets.randbits(30) | 0x40000000)
        self.clock = clock
        self.lock = threading.RLock()
        self.last_human = clock() - initial_idle
        self.generation = self.expected_generation = 0
        self.interrupted = False
        self.armed = False
        self.reason = None
        self.own_events = 0
        self.events = deque(maxlen=128)
        self._last_pointer = None
        self.duplicate_pointer_notifications = 0
        self.region = None
        self.last_external_input = self.last_human
        self.outside_events = 0
        self.automation_priority = False

    def set_region(self, bounds):
        with self.lock:
            self.region = tuple(bounds)
            self.last_human = float('-inf')

    def action_ready(self):
        with self.lock:
            return self.automation_priority or self.clock() - self.last_external_input >= self.idle_seconds

    def observe_keyboard(self, extra_info, target_foreground=True):
        with self.lock:
            if extra_info != self.own_tag:
                self.last_external_input = self.clock()
                if self.automation_priority or self.region is not None and not target_foreground:
                    self.outside_events += 1
                    return False
            return self.observe('keyboard', extra_info)

    def observe_pointer(self, kind, position, extra_info):
        with self.lock:
            previous = self._last_pointer
            self._last_pointer = position
            if kind == 'mouse_move' and previous == position and extra_info != self.own_tag:
                self.duplicate_pointer_notifications += 1
                return False
            if extra_info != self.own_tag:
                self.last_external_input = self.clock()
                if self.automation_priority:
                    self.outside_events += 1
                    return False
                if self.region is not None:
                    left, top, right, bottom = self.region
                    if not (left <= position[0] < right and top <= position[1] < bottom):
                        self.outside_events += 1
                        return False
            # RDP may echo a cursor-position notification after our own move.
            # A zero-displacement move is not a takeover. Never suppress
            # buttons, wheels, or an actual one-pixel position change.
            return self.observe('mouse' if kind == 'mouse_move' else kind, extra_info)

    def observe(self, kind, extra_info):
        with self.lock:
            if extra_info == self.own_tag:
                self.own_events += 1
                return False
            self.generation += 1
            self.last_external_input = self.clock()
            self.last_human = self.clock()
            self.interrupted = True
            self.reason = kind
            # Only event category and monotonic time. Never key values/text.
            self.events.append((self.last_human, kind))
            return True

    def begin_operation(self, target_foreground=True, require_target_foreground=True):
        with self.lock:
            if self.clock() - self.last_human < self.idle_seconds:
                raise UserInterference('User is active; desktop automation paused')
            if self.interrupted and require_target_foreground and not target_foreground:
                raise UserInterference('Return to the target application before automatic resume')
            self.expected_generation = self.generation
            self.interrupted = False
            self.armed = True
            self.reason = None

    def check(self):
        with self.lock:
            if not self.armed or self.interrupted or self.generation != self.expected_generation:
                raise UserInterference('Human input detected; discard the current action context')

    def resumable(self, target_foreground=True, require_target_foreground=True):
        with self.lock:
            return (self.clock() - self.last_human >= self.idle_seconds
                    and (not self.interrupted or not require_target_foreground or target_foreground))

    def status(self):
        with self.lock:
            return {'paused': self.interrupted or not self.armed, 'generation': self.generation,
                    'reason': self.reason, 'idle_remaining_seconds': max(0, self.idle_seconds - (self.clock() - self.last_human)),
                    'own_events': self.own_events}


class ClipboardChanged(ActionDeferred):
    """Paste was deferred before any text was entered."""


class DraftVerificationFailed(ActionDeferred):
    """The draft needs review and the send button was never clicked."""


def interrupted_send_state(backend):
    phase = getattr(backend, 'send_phase', 'preparing')
    if phase == 'preparing':
        return 'draft'
    if phase == 'editing':
        return 'blocked_input'
    return 'unknown'
