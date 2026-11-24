"""Saved-region dashboard. Saving never silently starts background work."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone, timedelta
import os
import json
from pathlib import Path

from .capture_area import load_region, list_monitors
from .region_monitor import RegionMonitor
from .live_frame import LiveFrame
from .runtime_status import DEFAULT_STATUS, read_status

LABELS = {'idle': '区域已保存 · 尚未启动监控', 'starting': '正在启动',
          'checking_window': '执行中 · 正在校验窗口和目标群',
          'announcing': '即将工作 · 全屏幕倒计时',
          'history_paused': '运行等待中 · 已暂停自动回翻',
          'waiting_for_action': '观察等待中 · 自动操作暂缓',
          'capturing': '执行中 · 正在采集', 'watching': '运行等待中 · 等待下一轮',
          'observing': '执行中 · 正在观察', 'summarizing': '执行中 · 正在生成摘要',
          'sending': '执行中 · 正在发送', 'paused_for_user': '已暂停 · 用户正在操作',
          'stopping': '正在停止',
          'paused_for_panel': '已暂停 · 请移开或最小化状态窗口',
          'stopped': '已停止', 'completed': '单次任务已完成 · 未持续监控',
          'error': '任务出错，已停止', 'interrupted': '任务已退出',
          'stale': '心跳中断 · 运行状态无法确认', 'unknown': '运行状态暂时无法读取'}
ACTIVE = {'starting', 'capturing', 'watching', 'observing', 'summarizing', 'sending', 'paused_for_user', 'paused_for_panel'}


class RegionStatusPanel:
    def __init__(self, root, region_file, status_path=DEFAULT_STATUS, preview='artifacts/region-latest.png', config_file='config.local.toml'):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.tk, self.ttk = root, tk, ttk
        self.region_file, self.status_path, self.preview = Path(region_file), status_path, preview
        self.rect = load_region(region_file)
        self.worker = None
        self.edit_requested = False
        self.closing = False
        self.poll_id = None
        self.stopping = False
        self.live_frame = LiveFrame(root, self.rect)
        self.settings_file = self.region_file.with_name(self.region_file.stem + ".settings.local.json")
        try:
            settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        self.interval = tk.StringVar(value=str(settings.get("interval_seconds", 5)))
        self.task_kind = tk.StringVar(value=settings.get('task_kind', '群消息转发'))
        self.config_vars = {}
        from .cycle_notice import CycleNotice
        self.cycle_notice = CycleNotice(root)
        monitors = list_monitors()
        anchor_monitor = next((m for m in monitors if m.bounds.contains(self.rect)), monitors[0])
        default_anchor = (anchor_monitor.bounds.left+16, anchor_monitor.bounds.top+16)
        anchor = settings.get('pointer_anchor', default_anchor) or default_anchor
        self.config_widgets = []
        self.config = None
        self.config_error = None
        try:
            from .console_config import ConsoleConfig
            self.config = ConsoleConfig(config_file)
        except (OSError, ValueError) as error:
            self.config_error = str(error)
        self.preview_lease = None
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetParent.argtypes = [wintypes.HWND]
        self.user.GetParent.restype = wintypes.HWND
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.root.title('FxxK-Desk · 可视化控制台')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Escape>', lambda _: self.close())
        self.root.unbind('<Control-s>')
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        self.headline = tk.StringVar()
        self.indicator = ttk.Label(frame, textvariable=self.headline, font=('Microsoft YaHei UI', 15, 'bold'))
        self.indicator.pack(anchor='w', pady=(0, 10))
        r = self.rect
        ttk.Label(frame, text=f'监控范围：({r.left}, {r.top})  ·  {r.width} × {r.height} 像素').pack(anchor='w')
        self.mode = tk.StringVar()
        self.metrics = tk.StringVar()
        ttk.Label(frame, textvariable=self.mode).pack(anchor='w', pady=8)
        ttk.Label(frame, textvariable=self.metrics, justify='left').pack(anchor='w', pady=6)
        self.notice = tk.StringVar(value='保存配置不等于启动。点击下方按钮才会开始区域采集。')
        ttk.Label(frame, textvariable=self.notice, wraplength=650).pack(anchor='w', pady=8)
        controls = ttk.Frame(frame)
        controls.pack(fill='x', pady=10)
        self.start_button = ttk.Button(controls, text='开始任务', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(controls, text='停止任务', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        ttk.Button(controls, text='最小化', command=root.iconify).pack(side='left')
        secondary = ttk.Frame(frame)
        secondary.pack(fill='x')
        ttk.Label(secondary, text='绿框 = 正在采集').pack(side='left')
        ttk.Button(secondary, text='重新调整区域', command=self.edit).pack(side='left', padx=8)
        ttk.Button(secondary, text='查看最近截图', command=self.open_preview).pack(side='left')
        tabs = ttk.Notebook(frame)
        tabs.pack(fill='both', expand=True, pady=(12, 0))
        settings_row = ttk.Frame(tabs, padding=10)
        advanced_row = ttk.Frame(tabs, padding=10)
        tabs.add(settings_row, text='任务配置')
        tabs.add(advanced_row, text='高级设置')
        def field(parent, row, label, key, value, choices=None, boolean=False):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=4)
            var = tk.BooleanVar(value=value) if boolean else tk.StringVar(value=str(value))
            self.config_vars[key] = var
            widget = (ttk.Checkbutton(parent, variable=var) if boolean else
                      ttk.Combobox(parent, textvariable=var, values=choices, state='readonly') if choices else
                      ttk.Entry(parent, textvariable=var, width=48))
            widget.grid(row=row, column=1, sticky='ew', padx=10, pady=4)
            self.config_widgets.append((widget, 'readonly' if choices else 'normal'))
            parent.columnconfigure(1, weight=1)
        field(settings_row, 0, '支持的软件', 'software', '企业微信', ['企业微信'])
        ttk.Label(settings_row, text='任务类型').grid(row=1, column=0, sticky='w')
        selector = ttk.Combobox(settings_row, textvariable=self.task_kind, values=['群消息转发', '仅区域采集'], state='readonly')
        selector.grid(row=1, column=1, sticky='ew', padx=10, pady=4)
        self.config_widgets.append((selector, 'readonly'))
        source = self.config.source if self.config else None
        field(settings_row, 2, '监控的目标群', 'source_chat', source.chat if source else '')
        field(settings_row, 3, '接收转发的通知群', 'destination_chat', self.config.destination.chat if self.config else '')
        field(settings_row, 4, '自动发送到通知群', 'auto_send', self.config.routing.get('auto_send', False) if self.config else False, boolean=True)
        field(advanced_row, 8, '自动化优先（允许抢回焦点）', 'automation_priority', settings.get('automation_priority', False), boolean=True)
        field(advanced_row, 9, '每轮开始提示（秒，1～30）', 'warning_seconds', settings.get('warning_seconds', 3))
        field(advanced_row, 10, '每个动作前后复位鼠标', 'reset_pointer', settings.get('reset_pointer', True), boolean=True)
        field(advanced_row, 11, '鼠标准备位置 X', 'anchor_x', anchor[0])
        field(advanced_row, 12, '鼠标准备位置 Y', 'anchor_y', anchor[1])
        ttk.Label(settings_row, text='轮询间隔（秒）').grid(row=5, column=0, sticky='w')
        self.interval_entry = ttk.Entry(settings_row, textvariable=self.interval, width=8)
        self.interval_entry.grid(row=5, column=1, sticky='w', padx=10)
        self.save_button = ttk.Button(settings_row, text='保存配置', command=self.save_settings)
        self.save_button.grid(row=6, column=1, sticky='w', padx=10, pady=8)
        field(advanced_row, 0, '程序路径', 'executable', source.executable if source else '')
        field(advanced_row, 1, '窗口标题', 'window_title', source.window_title if source else '')
        field(advanced_row, 2, '刷屏时回翻历史', 'backfill_enabled', source.backfill_enabled if source else True, boolean=True)
        field(advanced_row, 3, '回翻最多页数', 'backfill_max_pages', source.backfill_max_pages if source else 30)
        field(advanced_row, 4, '回翻时限（秒）', 'backfill_max_seconds', source.backfill_max_seconds if source else 45)
        field(advanced_row, 5, '用户停止操作后等待（秒）', 'human_idle_seconds', source.human_idle_seconds if source else 5)
        field(advanced_row, 6, '恢复时要求软件在前台', 'resume_requires_target_foreground', source.resume_requires_target_foreground if source else True, boolean=True)
        field(advanced_row, 7, '滚动方式', 'scroll_mode', source.scroll_mode if source else 'batch', ['batch', 'stepwise'])
        advanced_save = ttk.Button(advanced_row, text='保存全部设置', command=self.save_settings)
        advanced_save.grid(row=13, column=1, sticky='w', padx=10, pady=8)
        self.config_widgets.append((advanced_save, 'normal'))
        self.footer = ttk.Label(frame, text='所有屏幕同步提示：开始前倒计时 → 操作中持续提示 → 本轮结束后可继续使用电脑。\n提示秒数和鼠标复位位置可在高级设置调整。绿框表示监控任务运行，最小化后保留。\n自动化优先模式允许抢回焦点；软件必须完整放入监控框并符合布局校准。', foreground='#666666', wraplength=650)
        self.footer.pack(anchor='w', pady=(12, 0))
        self.root.update_idletasks()
        self.root.geometry(f'{max(720, frame.winfo_reqwidth())}x{max(480, frame.winfo_reqheight())}')
        self.hwnd = self.user.GetParent(root.winfo_id()) or root.winfo_id()
        self.poll()

    def obstructed(self):
        if not self.user.IsWindowVisible(self.hwnd) or self.user.IsIconic(self.hwnd):
            return False
        r = wintypes.RECT()
        if not self.user.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return True
        area = self.rect
        return r.left < area.right and r.right > area.left and r.top < area.bottom and r.bottom > area.top

    def start(self):
        if self.worker and self.worker.alive:
            return
        if not self.save_settings():
            return
        self.stopping = False
        self.root.update_idletasks()
        if self.task_kind.get() == '群消息转发':
            from .relay_monitor import RelayMonitor
            self.worker = RelayMonitor(self.config.path, self.config.route, float(self.interval.get()),
                                       self.config_vars['auto_send'].get(), self.region_file, self.status_path, self.obstructed,
                                       self.config_vars['automation_priority'].get(), self.cycle_notice,
                                       self.pointer_anchor)
        else:
            self.worker = RegionMonitor(self.region_file, self.preview, interval=float(self.interval.get()), status_path=self.status_path,
                                        obstructed=self.obstructed, before_cycle=self.cycle_notice)
        try:
            self.worker.start()
            self.notice.set('任务已启动。请移开或最小化面板，并将软件放入监控框；用户操作时自动暂停。')
        except RuntimeError as error:
            self.notice.set(f'未能启动：{error}')
        self.poll(schedule=False)

    def stop(self):
        self.stopping = True
        self.hide_outline()
        if self.worker:
            from .relay_monitor import RelayMonitor
            if isinstance(self.worker, RelayMonitor):
                self.worker.stop('关闭控制台' if self.closing else '点击停止任务')
            else:
                self.worker.stop()
            self.notice.set('正在停止；当前这一帧结束后退出。')

    def save_settings(self):
        try:
            if self.worker and self.worker.alive:
                raise ValueError('请先停止任务再保存配置')
            interval = float(self.interval.get())
            minimum = 2 if self.task_kind.get() == '群消息转发' else 0.1
            if not minimum <= interval <= 3600:
                raise ValueError(f'间隔应为 {minimum}～3600 秒')
            warning_seconds = int(self.config_vars['warning_seconds'].get())
            if not 1 <= warning_seconds <= 30:
                raise ValueError('开始提示应为 1～30 秒')
            anchor = [int(self.config_vars['anchor_x'].get()), int(self.config_vars['anchor_y'].get())]
            if not any(m.bounds.left <= anchor[0] < m.bounds.right and m.bounds.top <= anchor[1] < m.bounds.bottom for m in list_monitors()):
                raise ValueError('鼠标准备位置必须位于当前某块屏幕内')
            self.pointer_anchor = tuple(anchor) if self.config_vars['reset_pointer'].get() else None
            self.cycle_notice.seconds = warning_seconds
            if self.task_kind.get() == '群消息转发':
                if not self.config:
                    raise ValueError(f'无法读取聊天配置：{self.config_error}')
                v = {key: var.get() for key, var in self.config_vars.items()}
                advanced = {key: v[key] for key in ('backfill_enabled', 'resume_requires_target_foreground', 'scroll_mode')}
                advanced['backfill_max_pages'] = int(v['backfill_max_pages'])
                for key in ('backfill_max_seconds', 'human_idle_seconds'):
                    advanced[key] = float(v[key])
                self.config.save(v['source_chat'], v['destination_chat'], v['executable'], v['window_title'], v['auto_send'], advanced)
            temporary = self.settings_file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'interval_seconds': interval, 'task_kind': self.task_kind.get(),
                                             'warning_seconds': warning_seconds, 'pointer_anchor': anchor,
                                             'reset_pointer': self.config_vars['reset_pointer'].get(),
                                             'automation_priority': self.config_vars['automation_priority'].get()}), encoding='utf-8')
            temporary.replace(self.settings_file)
            self.notice.set('配置已保存，点击开始监控后生效。')
            return True
        except (ValueError, OSError) as error:
            self.notice.set(f'保存失败：{error}')
            return False

    def hide_outline(self):
        self.live_frame.hide()

    def edit(self):
        if self.worker and self.worker.alive:
            self.notice.set('请先停止采集，再调整区域。')
            return
        self.hide_outline()
        from .lease import desktop_lease
        try:
            with desktop_lease():
                pass
        except RuntimeError:
            self.notice.set('另一个桌面任务正在运行，请先停止该任务。')
            return
        self.edit_requested = True
        self.close()

    def open_preview(self):
        if Path(self.preview).exists():
            os.startfile(str(Path(self.preview).resolve()))
        else:
            self.notice.set('还没有采集成功的截图，请先启动区域采集。')

    def poll(self, schedule=True):
        if schedule and self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        status = read_status(self.status_path)
        state = status.get('state', 'unknown')
        running = bool(self.worker and self.worker.alive)
        if running and self.stopping:
            state = 'stopping'
        elif running and state in ('idle', 'stopped', 'completed'):
            state = 'starting'
        elif self.worker and self.worker.error:
            state = 'error'
        if state == 'history_paused':
            self.notice.set('不再重复回翻：'+str(status.get('coverage_reason', ''))+'。处理后停止再开始可重试。')
        if (state.startswith('paused') or state == 'waiting_for_action') and status.get('detail'):
            self.notice.set(status['detail'])
        elif status.get('observation_mode') == 'passive' and state in ('observing', 'watching'):
            self.notice.set(status.get('detail') or '后台只读观察，自动操作等待空闲')
        elif state == 'watching' and self.worker and self.worker.alive:
            self.notice.set('本轮历史尚未核验完整：' + status.get('coverage_reason', '') if status.get('caught_up') is False
                            else '任务运行中；其他屏幕的操作不影响只读观察，自动操作等待输入空闲。')
        self.headline.set(LABELS.get(state, '状态未知'))
        self.indicator.configure(foreground='#16834a' if state in {'capturing', 'watching', 'observing', 'summarizing', 'sending'}
                                 else '#a46600' if state.startswith('paused') or state in ('starting', 'waiting_for_action', 'announcing', 'checking_window') else '#555555')
        self.mode.set('任务：只读区域截图' if status.get('mode') == 'region_capture'
                      else '任务：聊天适配器（使用独立的窗口配置）' if status.get('mode') in ('chat_relay', 'chat')
                      else '任务：单次区域截图' if status.get('mode') == 'region_once'
                      else '尚未运行持续任务')
        stamp = status.get('last_check_at') or status.get('last_capture_at')
        try:
            checked = datetime.fromisoformat(stamp).astimezone(timezone(timedelta(hours=8))).strftime('%H:%M:%S') if stamp else '尚无'
        except (TypeError, ValueError):
            checked = '未知'
        counter = '采集帧数' if status.get('mode') == 'region_capture' else '检查轮次'
        self.metrics.set(f"最近检查：{checked}\n{counter}：{status.get('capture_count', 0)}  ·  新消息：{status.get('new_messages_total', 0)}  ·  已发送：{status.get('sent_total', 0)}")
        if self.worker and self.worker.error:
            self.notice.set(f'任务已停止：{self.worker.error}')
        visible = running and not self.stopping and not self.closing and state in {'capturing', 'watching', 'observing', 'summarizing', 'sending'} and not self.obstructed()
        if visible:
            try:
                self.live_frame.show()
            except RuntimeError as error:
                self.notice.set(str(error))
                self.worker.stop()
        else:
            self.hide_outline()
        self.interval_entry.configure(state='disabled' if running else 'normal')
        self.save_button.configure(state='disabled' if running else 'normal')
        for widget, normal in self.config_widgets:
            widget.configure(state='disabled' if running else normal)
        self.start_button.configure(state='disabled' if running else 'normal')
        self.stop_button.configure(state='normal' if running else 'disabled')
        if self.closing and not running:
            if self.poll_id is not None:
                self.root.after_cancel(self.poll_id)
                self.poll_id = None
            self.hide_outline()
            self.cycle_notice.close()
            self.root.destroy()
            return
        if schedule:
            self.poll_id = self.root.after(500, self.poll)

    def close(self):
        self.closing = True
        self.stop()
        self.poll(schedule=False)
