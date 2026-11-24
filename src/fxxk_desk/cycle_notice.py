"""Multi-monitor countdown, working, and completion notices with worker handshakes."""
import math
import queue
import threading
import time
from contextlib import contextmanager

from .interference import UserInterference


@contextmanager
def announced_cycle(notice, stop_event):
    outcome = 'completed'
    try:
        if notice:
            notice(stop_event)
        yield
    except UserInterference:
        outcome = 'paused'
        raise
    except BaseException:
        outcome = 'error'
        raise
    finally:
        if stop_event is not None and stop_event.is_set():
            outcome = 'stopped'
        finish = getattr(notice, 'finish_cycle', None)
        if finish:
            finish(outcome)


class CycleNotice:
    def __init__(self, root, seconds=3):
        self.root, self.seconds = root, seconds
        self.requests = queue.Queue()
        self.active = None
        self.panes = []
        self.closed = False
        self.after_id = root.after(40, self.tick)

    def __call__(self, stop_event):
        request = {'kind': 'begin', 'done': threading.Event(), 'stop': stop_event, 'seconds': self.seconds, 'error': None}
        self.requests.put(request)
        while not request['done'].wait(.05):
            if stop_event is not None and stop_event.is_set():
                raise UserInterference('启动倒计时已取消')
            if self.closed:
                raise UserInterference('控制台已关闭，未开始操作')
        if request['error']:
            raise RuntimeError(request['error'])
        if stop_event is not None and stop_event.is_set():
            raise UserInterference('启动倒计时已取消')

    def finish_cycle(self, outcome='completed'):
        if self.closed:
            return
        request = {'kind': 'finish', 'outcome': outcome, 'done': threading.Event(), 'error': None}
        self.requests.put(request)
        while not request['done'].wait(.05):
            if self.closed:
                return

    def _paint(self, text, color):
        for pane in self.panes:
            pane.configure(bg=color)
            pane.label.configure(text=text, bg=color)

    def _show(self):
        import tkinter as tk
        from ctypes import wintypes
        from .capture_area import list_monitors
        from .live_frame import LiveFrame
        self.native = LiveFrame(self.root, None)
        u = self.native.user
        for monitor in list_monitors():
            bounds = monitor.bounds
            width, height = min(760, bounds.width - 32), 156
            pane = tk.Toplevel(self.root)
            self.panes.append(pane)
            pane.withdraw()
            pane.overrideredirect(True)
            pane.attributes('-alpha', .98)
            pane.configure(bg='#172d46')
            label = tk.Label(pane, bg='#172d46', fg='white', font=('Microsoft YaHei UI', 14, 'bold'), wraplength=width-24)
            label.pack(fill='both', expand=True, padx=12, pady=12)
            pane.label = label
            pane.update_idletasks()
            hwnd = u.GetParent(pane.winfo_id()) or pane.winfo_id()
            u.SetWindowLongPtrW(hwnd, -8, 0)
            style = u.GetWindowLongPtrW(hwnd, -20)
            u.SetWindowLongPtrW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
            if not u.SetWindowDisplayAffinity(hwnd, 0x11):
                raise RuntimeError('无法创建排除截图的工作提示，已取消本轮操作')
            pane.deiconify()
            u.SetWindowPos(hwnd, wintypes.HWND(-1), bounds.left + (bounds.width-width)//2,
                           bounds.top+32, width, height, 0x0010 | 0x0040)

    def _finish(self, error=None):
        for pane in self.panes:
            pane.destroy()
        self.panes.clear()
        self.root.update_idletasks()
        if self.active:
            self.active['error'] = error
            self.active['done'].set()
            self.active = None

    def tick(self):
        if self.closed:
            return
        try:
            try:
                command = self.requests.get_nowait()
            except queue.Empty:
                command = None
            if command:
                if command['kind'] == 'finish':
                    if self.active:
                        self.active['done'].set()
                    self.active = command
                    if not self.panes:
                        self._show()
                    self.active['phase'] = 'finished'
                    self.active['deadline'] = time.monotonic()+2
                    outcome = command['outcome']
                    text = ('本轮操作完成 · 可以继续使用电脑' if outcome == 'completed' else
                            '本轮已暂停 · 鼠标键盘已释放' if outcome == 'paused' else
                            '任务已停止 · 可以继续使用电脑' if outcome == 'stopped' else
                            '本轮出错，已停止操作 · 可以继续使用电脑')
                    self._paint(text, '#176343' if outcome == 'completed' else '#825313')
                elif self.active:
                    command['error'] = '上轮操作提示尚未结束'
                    command['done'].set()
                else:
                    self.active = command
                    stop = command['stop']
                    if stop is not None and stop.is_set():
                        self._finish()
                    else:
                        self._show()
                        command['phase'] = 'countdown'
                        command['deadline'] = time.monotonic()+command['seconds']
            if self.active:
                phase = self.active['phase']
                if phase == 'finished' and time.monotonic() >= self.active['deadline']:
                    self._finish()
                elif phase == 'countdown':
                    stop = self.active['stop']
                    remaining = math.ceil(self.active['deadline'] - time.monotonic())
                    if stop is not None and stop.is_set():
                        self._finish()
                    elif remaining <= 0:
                        self.active['phase'] = 'working'
                        self._paint('程序正在工作\n正在采集 / 操作目标软件，请暂时不要使用鼠标键盘', '#174a80')
                        self.root.update_idletasks()
                        self.active['done'].set()  # Working notice remains visible throughout actions.
                    else:
                        self._paint(f'程序即将开始工作 · {remaining} 秒\n即将采集 / 操作目标软件，请暂时让出鼠标键盘', '#172d46')
        except Exception as error:
            self._finish(str(error))
        self.after_id = self.root.after(40, self.tick)

    def close(self):
        self.closed = True
        if getattr(self, 'after_id', None):
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self._finish('控制台已关闭')
