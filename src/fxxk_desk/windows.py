"""Win32 window discovery, foreground activation and visible-screen capture."""
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
import os
import subprocess
import time


@dataclass
class Window:
    id: int
    title: str
    pid: int

    def to_dict(self):
        return asdict(self)


class Desktop:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Desktop control requires Windows")
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.input_guard = None
        self.u.GetForegroundWindow.restype = wintypes.HWND
        self.u.IsWindow.argtypes = [wintypes.HWND]
        self.u.IsWindowVisible.argtypes = [wintypes.HWND]
        self.u.IsIconic.argtypes = [wintypes.HWND]
        self.u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.u.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        try:
            self.u.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            self.u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            self.u.SetProcessDPIAware()

    def windows(self):
        found = []

        @self.callback_type
        def visit(hwnd, _):
            if self.u.IsWindowVisible(hwnd):
                size = self.u.GetWindowTextLengthW(hwnd)
                if size:
                    title = ctypes.create_unicode_buffer(size + 1)
                    self.u.GetWindowTextW(hwnd, title, size + 1)
                    pid = wintypes.DWORD()
                    self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    found.append(Window(int(hwnd), title.value, pid.value))
            return True

        if not self.u.EnumWindows(visit, 0):
            raise RuntimeError("Could not enumerate desktop windows")
        return found

    def select(self, title):
        candidates = [w for w in self.windows() if title.casefold() in w.title.casefold()]
        exact = [w for w in candidates if w.title.casefold() == title.casefold()]
        candidates = exact or candidates
        if len(candidates) != 1:
            raise ValueError(f"Expected one window, found {len(candidates)}; use windows to inspect titles")
        return candidates[0]

    def activate(self, window):
        if self.input_guard:
            self.input_guard()
        if not self.u.IsWindow(window.id):
            raise RuntimeError("Window no longer exists")
        if self.u.IsIconic(window.id):
            self.u.ShowWindow(window.id, 9)
        if self.input_guard:
            self.input_guard()
        self.u.SetForegroundWindow(window.id)
        time.sleep(0.4)
        if self.input_guard:
            self.input_guard()
        if self.u.GetForegroundWindow() != window.id:
            raise RuntimeError("Windows denied foreground activation; select the window manually")

    def launch(self, executable):
        if self.input_guard:
            self.input_guard()
        path = Path(executable).resolve(strict=True)
        if path.suffix.lower() != ".exe":
            raise ValueError("Provide an explicit .exe path")
        subprocess.Popen([str(path)], shell=False)

    def capture(self, window, output):
        from PIL import ImageGrab

        self.activate(window)
        rect = wintypes.RECT()
        if not self.u.GetWindowRect(window.id, ctypes.byref(rect)):
            raise RuntimeError("Cannot read window bounds")
        bounds = (rect.left, rect.top, rect.right, rect.bottom)
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise RuntimeError("Window has invalid bounds")
        try:
            image = ImageGrab.grab(window=window.id)
            capture_method = "window"
        except (OSError, TypeError):
            image = ImageGrab.grab(bbox=bounds, all_screens=True)
            capture_method = "screen_region"
        if self.u.GetForegroundWindow() != window.id:
            raise RuntimeError("Focus changed during capture; discard screenshot")
        image.thumbnail((1024, 1024))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG")
        return {"window": window.to_dict(), "screen_bounds": bounds, "capture_method": capture_method,
                "uploaded_size": image.size, "captured_at": time.time(), "path": str(output)}
