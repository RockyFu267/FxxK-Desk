"""Click-through, capture-excluded running indicator, independent of panel minimization."""
import ctypes
from ctypes import wintypes


class LiveFrame:
    def __init__(self, root, rect):
        self.root, self.rect, self.panes = root, rect, []
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        self.user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.user.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]

    def show(self):
        if self.panes:
            return
        import tkinter as tk
        r = self.rect
        try:
            for x, y, width, height in ((r.left, r.top, r.width, 4), (r.left, r.bottom-4, r.width, 4),
                                       (r.left, r.top, 4, r.height), (r.right-4, r.top, 4, r.height)):
                pane = tk.Toplevel(self.root)
                self.panes.append(pane)
                pane.withdraw()
                pane.overrideredirect(True)
                pane.configure(bg='#00d8a0')
                pane.attributes('-alpha', 0.99)
                pane.update_idletasks()
                hwnd = self.user.GetParent(pane.winfo_id()) or pane.winfo_id()
                # No native owner: minimizing the settings panel must leave the indicator visible.
                self.user.SetWindowLongPtrW(hwnd, -8, 0)
                style = self.user.GetWindowLongPtrW(hwnd, -20)
                self.user.SetWindowLongPtrW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
                if not self.user.SetWindowDisplayAffinity(hwnd, 0x11):
                    raise RuntimeError('当前桌面不支持排除监控框截图，未启动显示')
                pane.deiconify()
                self.user.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, width, height, 0x0010 | 0x0040)
        except Exception:
            self.hide()
            raise

    def hide(self):
        for pane in self.panes:
            pane.destroy()
        self.panes.clear()
