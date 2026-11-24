"""Tagged SendInput + passive low-level hooks in the current Windows session.

Only our process-specific dwExtraInfo marker is ignored. Untagged RDP or other
injected input is treated conservatively as a user takeover. Hooks never block.
"""
import ctypes
from ctypes import wintypes
import os
import threading
import time
from contextlib import contextmanager

from .interference import ClipboardChanged, UserInterference, DesktopUnavailable


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
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.ready = threading.Event()
        self.failure = None
        self.target_hwnd = None
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
        return bool(self.user.GetCursorPos(ctypes.byref(point))) and self._input_desktop_is_default()

    def _input_desktop_is_default(self):
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = self.user.OpenInputDesktop(0, False, 1)
        if not desktop:
            return False
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            return bool(self.user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))) and name.value.casefold() == 'default'
        finally:
            self.user.CloseDesktop(desktop)

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
                self.arbiter.observe_keyboard(int(data.dwExtraInfo),
                                             target_foreground=not self.target_hwnd or user.GetForegroundWindow() == self.target_hwnd)
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
        self.anchor = None

    def park(self):
        anchor = getattr(self, 'anchor', None)
        if anchor is not None:
            self._send([self._move_record(anchor)], expected_after=anchor)

    @contextmanager
    def step(self):
        self.park()
        yield
        # Do not restore after an interrupted/failed action: context may have
        # changed or Windows may be locked. The next attempt reinitializes.
        self.park()

    def _send(self, records, expected_cursor=None, expected_after=None):
        self.monitor.check()
        if getattr(self, 'context_guard', None):
            self.context_guard()
        if expected_cursor is not None:
            point = wintypes.POINT()
            self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            if not self.user.GetCursorPos(ctypes.byref(point)):
                raise DesktopUnavailable('Cannot verify pointer position before click')
            if abs(point.x - expected_cursor[0]) > 2 or abs(point.y - expected_cursor[1]) > 2:
                from .interference import ActionDeferred
                raise ActionDeferred('鼠标在点击前偏离目标，重新定位后再试')
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
        if expected_after is not None:
            point = wintypes.POINT()
            self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            if not self.user.GetCursorPos(ctypes.byref(point)):
                raise DesktopUnavailable('Cannot verify initialized pointer')
            if abs(point.x-expected_after[0]) > 2 or abs(point.y-expected_after[1]) > 2:
                from .interference import ActionDeferred
                raise ActionDeferred('鼠标初始化位置校验失败，本轮操作已暂缓')

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

    def click(self, coords, *, observe=None):
        with self.step():
            self._click(coords)
            if observe is not None:
                return observe()

    def _click(self, coords):
        # RDP/mixed-DPI desktops may apply a move after a co-batched button.
        # Settle and verify the actual pointer before a separate button packet.
        self.move(coords)
        time.sleep(0.08)
        down, up = self._move_record(coords), self._move_record(coords)
        down.mi.dwFlags |= 0x0002
        up.mi.dwFlags |= 0x0004
        self._send([down, up], expected_cursor=coords)

    def scroll(self, coords, wheel_dist):
        with self.step():
            self.move(coords)
            wheel = self._move_record(coords)
            wheel.mi.dwFlags |= 0x0800
            wheel.mi.mouseData = int(wheel_dist * 120) & 0xFFFFFFFF
            self._send([wheel], expected_cursor=coords)
        # Preserve the previous controller's event pacing while changing tags.
        time.sleep(0.09)

    def hotkey(self, *keys):
        with self.step():
            self._hotkey(*keys)

    def _hotkey(self, *keys):
        codes = {'ctrl': 0x11, 'f': 0x46, 'a': 0x41, 'v': 0x56, 'c': 0x43}
        values = [codes[key] for key in keys]
        records = [INPUT(type=1, ki=KEYBDINPUT(code, 0, 0, 0, self.arbiter.own_tag)) for code in values]
        records += [INPUT(type=1, ki=KEYBDINPUT(code, 0, 0x0002, 0, self.arbiter.own_tag)) for code in reversed(values)]
        self._send(records)

    def type_text(self, text, pause=0.02):
        if any(char in text for char in '\r\n\t'):
            raise ValueError('Control characters require clipboard paste')
        with self.step():
            self._type_text(text, pause)

    def paste_text(self, text):
        """Paste a multiline draft without sending any Enter key events."""
        from concurrent.futures import ThreadPoolExecutor
        import win32clipboard as clipboard

        with self.step(), ThreadPoolExecutor(max_workers=1, thread_name_prefix='clipboard-sta') as executor:
            staged = self._staged_clipboard(text)
            owned = executor.submit(staged.__enter__).result()
            try:
                if clipboard.GetClipboardSequenceNumber() != owned[0]:
                    raise ClipboardChanged('剪贴板在粘贴前发生变化，本轮暂缓发送')
                self._hotkey('ctrl', 'v')
                time.sleep(.35)
            finally:
                executor.submit(staged.__exit__, None, None, None).result()

    def read_editor_text(self, verify_focus, expected_pid):
        """Copy a verified editor selection and read all text, including scrolled lines."""
        from concurrent.futures import ThreadPoolExecutor
        from uuid import uuid4
        import win32clipboard as clipboard
        import win32process

        with self.step(), ThreadPoolExecutor(max_workers=1, thread_name_prefix='clipboard-sta') as executor:
            staged = self._staged_clipboard(uuid4().hex)
            owned = executor.submit(staged.__enter__).result()
            try:
                if not verify_focus() or clipboard.GetClipboardSequenceNumber() != owned[0]:
                    return None
                self._hotkey('ctrl', 'a')
                self._hotkey('ctrl', 'c')
                for _ in range(8):
                    time.sleep(.05)
                    if clipboard.GetClipboardSequenceNumber() != owned[0]:
                        break
                owner = clipboard.GetClipboardOwner()
                if not owner or win32process.GetWindowThreadProcessId(owner)[1] != expected_pid:
                    return None
                clipboard.OpenClipboard()
                try:
                    sequence = clipboard.GetClipboardSequenceNumber()
                    if sequence == owned[0] or owner != clipboard.GetClipboardOwner():
                        return None
                    owned[0] = sequence
                    if clipboard.IsClipboardFormatAvailable(clipboard.CF_UNICODETEXT):
                        copied = clipboard.GetClipboardData(clipboard.CF_UNICODETEXT)
                        owned[0] = clipboard.GetClipboardSequenceNumber()
                    else:
                        return None
                finally:
                    clipboard.CloseClipboard()
                return copied
            finally:
                executor.submit(staged.__exit__, None, None, None).result()

    @staticmethod
    @contextmanager
    def _staged_clipboard(text):
        import pythoncom
        import pywintypes
        import win32clipboard as clipboard
        import win32gui

        pythoncom.OleInitialize()
        owned = [None]
        try:
            expected = text.replace('\n', '\r\n')
            foreground = win32gui.GetForegroundWindow()
            previous_sequence = clipboard.GetClipboardSequenceNumber()
            previous = pythoncom.OleGetClipboard()
            clipboard.OpenClipboard(foreground)
            try:
                if clipboard.GetClipboardSequenceNumber() != previous_sequence:
                    raise ClipboardChanged('剪贴板在准备粘贴时发生变化，本轮暂缓发送')
                clipboard.EmptyClipboard()
                owned[0] = clipboard.GetClipboardSequenceNumber()
                clipboard.SetClipboardText(expected, clipboard.CF_UNICODETEXT)
                owned[0] = clipboard.GetClipboardSequenceNumber()
            finally:
                clipboard.CloseClipboard()
            # Closing the clipboard can publish the write and advance the
            # sequence. Verify the published payload before recording it.
            clipboard.OpenClipboard(foreground)
            try:
                if (not clipboard.IsClipboardFormatAvailable(clipboard.CF_UNICODETEXT)
                        or clipboard.GetClipboardData(clipboard.CF_UNICODETEXT) != expected):
                    raise ClipboardChanged('剪贴板中的正文与待发送内容不一致，本轮暂缓发送')
                owned[0] = clipboard.GetClipboardSequenceNumber()
            finally:
                clipboard.CloseClipboard()
            yield owned
        finally:
            try:
                for attempt in range(3):
                    if owned[0] is None or clipboard.GetClipboardSequenceNumber() != owned[0]:
                        break
                    try:
                        pythoncom.OleSetClipboard(previous)
                        break
                    except pywintypes.com_error as error:
                        if attempt == 2:
                            import logging
                            logging.getLogger(__name__).warning('无法恢复剪贴板，继续核验已粘贴正文（HRESULT %s）', error.hresult)
                        else:
                            time.sleep(.05)
            finally:
                ctypes.WinDLL('ole32').OleUninitialize()

    def _type_text(self, text, pause):
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
