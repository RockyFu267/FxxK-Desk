"""Interactive multi-monitor capture-region editor; independent of target apps."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path

from .capture_area import Rect, Limits, list_monitors, monitor_at, fit_rect, resize_rect, load_region, save_region


class RegionEditor:
    def __init__(self, output, initial=None, limits=None, on_saved=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.output = Path(output)
        self.on_saved = on_saved
        self.saved = False
        self.status_view = None
        self.limits = limits or Limits()
        self.monitors = list_monitors()
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        if initial is None and self.output.exists():
            initial = load_region(self.output)
        primary = next((m for m in self.monitors if m.primary), self.monitors[0])
        if initial is None:
            r = primary.bounds
            width, height = self.limits.size(r.width * 0.6, r.height * 0.65, primary)
            x, y = r.left + (r.width - width) // 2, r.top + (r.height - height) // 2
            initial = Rect(x, y, x + width, y + height)
        self.monitor = monitor_at(self.monitors, (initial.left + initial.right) // 2, (initial.top + initial.bottom) // 2)
        self.rect = fit_rect(initial, self.monitor, self.limits)
        self.drag_state = None
        self.overlay = []
        self.positions = []
        self.root = tk.Tk()
        self.root.title('FxxK-Desk · 监控区域设置')
        self.root.attributes('-topmost', True)
        self.root.geometry('720x350')
        self.root.minsize(640, 330)
        self.root.protocol('WM_DELETE_WINDOW', self.cancel)
        self.root.bind('<Escape>', lambda _: self.cancel())
        self.root.bind('<Control-s>', lambda _: self.save())
        container = ttk.Frame(self.root, padding=16)
        self.container = container
        container.pack(fill='both', expand=True)
        ttk.Label(container, text='选择你的监控区域', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(container, text='拖动蓝色配置框移动 · 拖动八个方块缩放 · 此时尚未监控').pack(anchor='w', pady=(6, 12))
        self.labels = [f'屏幕 {i + 1}  {m.bounds.width}×{m.bounds.height}' + ('（主屏）' if m.primary else '')
                       for i, m in enumerate(self.monitors)]
        row = ttk.Frame(container)
        row.pack(fill='x')
        ttk.Label(row, text='所在屏幕').pack(side='left')
        self.screen = ttk.Combobox(row, values=self.labels, state='readonly', width=30)
        self.screen.pack(side='left', padx=10)
        self.screen.bind('<<ComboboxSelected>>', self.switch_screen)
        ttk.Button(row, text='铺满该屏', command=self.fill_screen).pack(side='left')
        fields = ttk.Frame(container)
        fields.pack(fill='x', pady=12)
        self.values = {}
        for i, (key, label) in enumerate((('left', 'X'), ('top', 'Y'), ('width', '宽'), ('height', '高'))):
            ttk.Label(fields, text=label).grid(row=0, column=i * 2, padx=(0, 4))
            self.values[key] = tk.StringVar()
            ttk.Entry(fields, textvariable=self.values[key], width=9).grid(row=0, column=i * 2 + 1, padx=(0, 10))
        ttk.Button(fields, text='应用', command=self.apply_fields).grid(row=0, column=8)
        self.dimensions = tk.StringVar()
        ttk.Label(container, textvariable=self.dimensions).pack(anchor='w')
        ttk.Label(container, text='尺寸范围表示可采集范围；文字识别和自动点击还需按软件界面校准。', foreground='#666666', wraplength=670).pack(anchor='w', pady=(8, 4))
        self.status = tk.StringVar(value='保存后进入状态面板，不会自动开始监控。取消不修改原配置。')
        ttk.Label(container, textvariable=self.status, wraplength=670).pack(anchor='w', pady=6)
        actions = ttk.Frame(container)
        actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='保存并查看状态', command=self.save).pack(side='right')
        ttk.Button(actions, text='取消', command=self.cancel).pack(side='right', padx=8)
        for _ in range(4):
            self.overlay.append(self.make_panel('move', '#3789eb', 'fleur'))
        for edge in ('nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'):
            self.overlay.append(self.make_panel(edge, '#20509b', 'sizing'))
        self.root.update_idletasks()
        self.root.geometry(f'{max(720, self.root.winfo_reqwidth())}x{max(350, self.root.winfo_reqheight())}+60+60')
        self.render()
        self.root.after(300, self.write_state)

    def make_panel(self, action, color, cursor):
        pane = self.tk.Toplevel(self.root)
        pane.overrideredirect(True)
        pane.attributes('-topmost', True)
        try:
            pane.configure(bg=color, cursor=cursor)
        except self.tk.TclError:
            pane.configure(bg=color, cursor='crosshair')
        pane.bind('<ButtonPress-1>', lambda event, value=action: self.start_drag(event, value))
        pane.bind('<B1-Motion>', self.drag)
        pane.bind('<ButtonRelease-1>', self.end_drag)
        pane.bind('<Escape>', lambda _: self.cancel())
        pane.update_idletasks()
        return pane

    def cursor(self):
        point = wintypes.POINT()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise RuntimeError('Desktop pointer is unavailable')
        return point.x, point.y

    def place(self, pane, x, y, width, height):
        hwnd = self.user.GetParent(pane.winfo_id()) or pane.winfo_id()
        if not self.user.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, width, height, 0x0010 | 0x0040):
            raise RuntimeError('Cannot position capture-region overlay')

    def render(self):
        r = self.rect
        positions = [(r.left, r.top, r.width, 5), (r.left, r.bottom - 5, r.width, 5),
                     (r.left, r.top, 5, r.height), (r.right - 5, r.top, 5, r.height)]
        size = 16
        mx, my = (r.left + r.right - size) // 2, (r.top + r.bottom - size) // 2
        positions += [(r.left, r.top, size, size), (mx, r.top, size, size), (r.right - size, r.top, size, size),
                      (r.right - size, my, size, size), (r.right - size, r.bottom - size, size, size),
                      (mx, r.bottom - size, size, size), (r.left, r.bottom - size, size, size), (r.left, my, size, size)]
        self.positions = positions
        for pane, position in zip(self.overlay, positions):
            self.place(pane, *position)
        self.screen.current(self.monitors.index(self.monitor))
        for key in self.values:
            self.values[key].set(str(getattr(r, key)))
        max_w, max_h = self.limits.size(self.monitor.bounds.width, self.monitor.bounds.height, self.monitor)
        self.dimensions.set(f'当前 {r.width}×{r.height} px  ·  最小 {self.limits.min_width}×{self.limits.min_height}  ·  本屏可铺满至 {max_w}×{max_h}')

    def start_drag(self, event, action):
        self.drag_state = (self.cursor(), self.rect, self.monitor, action)
        event.widget.grab_set()

    def drag(self, _):
        if not self.drag_state:
            return
        (x, y), original, monitor, action = self.drag_state
        px, py = self.cursor()
        dx, dy = px - x, py - y
        if action == 'move':
            self.monitor = monitor_at(self.monitors, px, py)
            self.rect = fit_rect(Rect(original.left + dx, original.top + dy, original.right + dx, original.bottom + dy), self.monitor, self.limits)
        else:
            self.rect = resize_rect(original, action, dx, dy, monitor, self.limits)
        self.render()

    def end_drag(self, event):
        self.drag_state = None
        event.widget.grab_release()
        self.write_state()

    def switch_screen(self, _=None):
        self.monitor = self.monitors[self.screen.current()]
        r = self.monitor.bounds
        width, height = self.limits.size(self.rect.width, self.rect.height, self.monitor)
        x, y = r.left + (r.width - width) // 2, r.top + (r.height - height) // 2
        self.rect = Rect(x, y, x + width, y + height)
        self.render()
        self.write_state()

    def fill_screen(self):
        self.rect = fit_rect(self.monitor.bounds, self.monitor, self.limits)
        self.render()
        self.write_state()

    def apply_fields(self):
        try:
            x, y, width, height = (int(self.values[key].get()) for key in ('left', 'top', 'width', 'height'))
            proposed = Rect(x, y, x + width, y + height)
            self.monitor = monitor_at(self.monitors, x + width // 2, y + height // 2)
            self.rect = fit_rect(proposed, self.monitor, self.limits)
            self.render()
            self.status.set('已应用；超出屏幕或尺寸上限的部分已限制。')
            self.write_state()
        except ValueError:
            self.status.set('请输入整数坐标及正数尺寸。')

    def write_state(self):
        if self.saved:
            return
        verified = True
        for pane, (x, y, width, height) in zip(self.overlay, self.positions):
            hwnd = self.user.GetParent(pane.winfo_id()) or pane.winfo_id()
            r = wintypes.RECT()
            ok = self.user.GetWindowRect(hwnd, ctypes.byref(r))
            verified = verified and bool(ok) and (r.left, r.top, r.right, r.bottom) == (x, y, x + width, y + height)
        state = {'status': 'editing', 'bounds': self.rect.tuple(), 'size': [self.rect.width, self.rect.height],
                 'monitor_count': len(self.monitors), 'overlay_verified': verified, 'unsaved': True}
        path = Path('artifacts/region-editor-state.local.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding='utf-8')

    def save(self):
        try:
            save_region(self.output, self.rect, self.monitor, self.limits)
        except (OSError, ValueError) as error:
            self.status.set(f'保存失败：{error}')
            return
        print(json.dumps({'status': 'saved', 'bounds': self.rect.tuple(), 'path': str(self.output)}), flush=True)
        self.saved = True
        for pane in self.overlay:
            pane.destroy()
        self.overlay.clear()
        if self.on_saved:
            self.on_saved()  # Editing lease is released before sampling can start.
        self.container.destroy()
        from .region_status_ui import RegionStatusPanel
        self.status_view = RegionStatusPanel(self.root, self.output)

    def cancel(self):
        print(json.dumps({'status': 'cancelled'}), flush=True)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def edit_region(output='artifacts/capture-region.local.json', bounds=None, min_width=640, min_height=480, status_only=False):
    from contextlib import ExitStack
    from .lease import desktop_lease
    while True:
        if status_only:
            import tkinter as tk
            from .region_status_ui import RegionStatusPanel
            list_monitors()  # Physical DPI context before creating Tk.
            root = tk.Tk()
            view = RegionStatusPanel(root, output)
            root.mainloop()
            if view.edit_requested:
                status_only = False
                continue
            return
        with ExitStack() as stack:
            stack.enter_context(desktop_lease())
            editor = RegionEditor(output, Rect(*bounds) if bounds else None, Limits(min_width, min_height), on_saved=stack.close)
            editor.run()
        if editor.status_view and editor.status_view.edit_requested:
            bounds = None
            continue
        return


def main():
    parser = argparse.ArgumentParser(description='Visually move and resize a capture region')
    parser.add_argument('--output', default='artifacts/capture-region.local.json')
    parser.add_argument('--bounds', nargs=4, type=int)
    parser.add_argument('--min-width', type=int, default=640)
    parser.add_argument('--min-height', type=int, default=480)
    parser.add_argument('--status', action='store_true', help='Open saved-region status without editing')
    args = parser.parse_args()
    edit_region(args.output, args.bounds, args.min_width, args.min_height, args.status)


if __name__ == '__main__':
    main()
