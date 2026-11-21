"""Tagged SendInput + passive low-level hooks in the current Windows session.

Only our process-specific dwExtraInfo marker is ignored. Untagged RDP or other
injected input is treated conservatively as a user takeover. Hooks never block.
"""
import ctypes
from ctypes import wintypes
import os
import threading
import time

from .interference import UserInterference, DesktopUnavailable


ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', wintypes.DWORD), ('data', INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class InputMonitor:
    def __init__(self, arbiter):
        if os.name != 'nt':
            raise RuntimeError('Native input monitoring requires Windows')
        self.arbiter = arbiter
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.ready = threading.Event()
        self.failure = None
        self.thread_id = None
        self.thread = threading.Thread(target=self._run, name='desktop-input-monitor', daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or self.failure or not self.thread.is_alive():
            self.close()
            raise RuntimeError('Cannot establish input monitoring; no desktop input allowed')

    def check(self):
        self.ensure_healthy()
        self.require_desktop()
        self.arbiter.check()

    def desktop_available(self):
        point = wintypes.POINT()
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        return bool(self.user.GetCursorPos(ctypes.byref(point)))

    def require_desktop(self):
        if not self.desktop_available():
            self.arbiter.observe('desktop_unavailable', None)
            raise DesktopUnavailable('Windows input desktop unavailable; waiting for the user session')

    def ensure_healthy(self):
        if self.failure or not self.thread.is_alive():
            raise RuntimeError('Input monitor unavailable; desktop automation stopped')

    def _run(self):
        hooks = []
        user, kernel = self.user, self.kernel
        callback_type = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user.CallNextHookEx.restype = LRESULT
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetCurrentThreadId.restype = wintypes.DWORD

        def mouse_callback(code, message, pointer):
            if code >= 0:
                data = ctypes.cast(pointer, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                kind = 'wheel' if message in (0x020A, 0x020E) else ('mouse_move' if message == 0x0200 else 'mouse')
                self.arbiter.observe_pointer(kind, (int(data.pt.x), int(data.pt.y)), int(data.dwExtraInfo))
            return user.CallNextHookEx(None, code, message, pointer)

        def keyboard_callback(code, message, pointer):
            if code >= 0:
                data = ctypes.cast(pointer, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                self.arbiter.observe('keyboard', int(data.dwExtraInfo))
            return user.CallNextHookEx(None, code, message, pointer)

        try:
            self.thread_id = kernel.GetCurrentThreadId()
            msg = wintypes.MSG()
            user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # Establish thread queue.
            self.callbacks = (callback_type(mouse_callback), callback_type(keyboard_callback))
            module = kernel.GetModuleHandleW(None)
            for hook_type, callback in zip((14, 13), self.callbacks):
                hook = user.SetWindowsHookExW(hook_type, callback, module, 0)
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(hook)
            self.ready.set()
            while True:
                code = user.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if code == 0:
                    break
                if code == -1:
                    raise RuntimeError('Input monitor message loop failed')
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
        except Exception as error:
            self.failure = type(error).__name__
            self.ready.set()
        finally:
            for hook in hooks:
                user.UnhookWindowsHookEx(hook)

    def close(self):
        if self.thread_id and self.thread.is_alive():
            self.user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            self.user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            self.thread.join(timeout=2)


class TaggedInputs:
    def __init__(self, arbiter, monitor, context_guard=None):
        self.arbiter, self.monitor = arbiter, monitor
        self.context_guard = context_guard
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user.SendInput.restype = wintypes.UINT
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = wintypes.SHORT

    def _send(self, records):
        self.monitor.check()
        if getattr(self, 'context_guard', None):
            self.context_guard()
        # Do not move the cursor during a human drag or type through held keys.
        if any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 0x10, 0x11, 0x12)):
            self.arbiter.observe('held_button_or_modifier', None)
            raise UserInterference('Human button/modifier is held; automation paused')
        array = (INPUT * len(records))(*records)
        sent = self.user.SendInput(len(records), array, ctypes.sizeof(INPUT))
        if sent != len(records):
            raise RuntimeError(f'SendInput failed or partially completed ({sent}/{len(records)}, Windows error {ctypes.get_last_error()})')
        time.sleep(0.002)
        self.monitor.check()
        if getattr(self, 'context_guard', None):
            self.context_guard()

    def _mouse(self, flags, data=0, x=0, y=0):
        return INPUT(type=0, mi=MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, self.arbiter.own_tag))

    def _move_record(self, coords):
        x, y = coords
        left, top = self.user.GetSystemMetrics(76), self.user.GetSystemMetrics(77)
        width, height = self.user.GetSystemMetrics(78), self.user.GetSystemMetrics(79)
        if width <= 1 or height <= 1 or not left <= x < left + width or not top <= y < top + height:
            raise ValueError('Pointer target outside the virtual desktop')
        return self._mouse(0x0001 | 0x8000 | 0x4000,
                           x=round((x - left) * 65535 / (width - 1)),
                           y=round((y - top) * 65535 / (height - 1)))

    def move(self, coords):
        self._send([self._move_record(coords)])

    def click(self, coords):
        self._send([self._move_record(coords), self._mouse(0x0002), self._mouse(0x0004)])

    def scroll(self, coords, wheel_dist):
        self.move(coords)
        self._send([self._mouse(0x0800, int(wheel_dist * 120))])
        # Preserve the previous controller's event pacing while changing tags.
        time.sleep(0.09)

    def hotkey(self, *keys):
        codes = {'ctrl': 0x11, 'f': 0x46}
        values = [codes[key] for key in keys]
        records = [INPUT(type=1, ki=KEYBDINPUT(code, 0, 0, 0, self.arbiter.own_tag)) for code in values]
        records += [INPUT(type=1, ki=KEYBDINPUT(code, 0, 0x0002, 0, self.arbiter.own_tag)) for code in reversed(values)]
        self._send(records)

    def type_text(self, text, pause=0.02):
        for character in text:
            units = character.encode('utf-16-le')
            records = []
            for offset in range(0, len(units), 2):
                unit = int.from_bytes(units[offset:offset + 2], 'little')
                records += [INPUT(type=1, ki=KEYBDINPUT(0, unit, flags, 0, self.arbiter.own_tag)) for flags in (0x0004, 0x0006)]
            self._send(records)
            time.sleep(pause)


def system_idle_seconds():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
    user = ctypes.WinDLL('user32', use_last_error=True)
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    if not user.GetLastInputInfo(ctypes.byref(info)):
        raise RuntimeError('Cannot determine desktop idle state')
    kernel = ctypes.WinDLL('kernel32')
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    return ((kernel.GetTickCount64() - info.dwTime) & 0xFFFFFFFF) / 1000.0
