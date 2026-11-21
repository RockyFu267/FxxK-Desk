"""Native self-test in an isolated Tk window; never types in a chat client.

Simulates foreign tagged input as a remote-control-like event. The monitor
must recognize it, block subsequent own input, then permit a fresh operation.
"""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import tkinter as tk

from fxxk_desk.interference import InputArbiter, UserInterference
from fxxk_desk.native_input import InputMonitor, TaggedInputs, INPUT, KEYBDINPUT


def main():
    report = {}
    root = tk.Tk()
    root.title('FxxK-Desk input ownership self-test')
    root.geometry('520x170+300+200')
    root.attributes('-topmost', True)
    tk.Label(root, text='FxxK-Desk 人工接管自测\n仅在本窗口输入测试字符，不操作聊天软件').pack(pady=12)
    entry = tk.Entry(root, width=55)
    entry.pack(padx=20, pady=8)
    label = tk.Label(root, text='正在验证输入标记、暂停和恢复…')
    label.pack()
    root.update()
    arbiter = InputArbiter(idle_seconds=0.25)
    monitor = InputMonitor(arbiter)
    monitor.start()
    writer = TaggedInputs(arbiter, monitor)
    native = writer.user
    native.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    native.GetAncestor.restype = wintypes.HWND
    native.GetForegroundWindow.restype = wintypes.HWND
    window = native.GetAncestor(root.winfo_id(), 2)
    expected = 'FxxKDesk_selftest_123'
    finished = False
    stage = 'initializing'

    class WindowMonitor:
        def check(self):
            monitor.check()
            if native.GetForegroundWindow() != window:
                arbiter.observe('focus_changed', None)
                raise UserInterference('Self-test window lost focus')

    writer.monitor = WindowMonitor()

    def finish(error=None):
        nonlocal finished
        if finished:
            return
        finished = True
        if error:
            report['error'] = str(error)
        report['stage'] = stage
        report['input_status'] = arbiter.status()
        report['event_categories'] = sorted({kind for _, kind in arbiter.events})
        report['passed'] = (not error and all(report.get(name) for name in (
            'own_input_not_paused', 'own_text_received', 'foreign_wheel_detected',
            'foreign_keyboard_detected', 'input_blocked_while_paused', 'resume_requires_new_operation',
            'resumed_after_idle', 'resumed_text_received')))
        monitor.close()
        report['monitor_uninstalled'] = not monitor.thread.is_alive()
        path = Path('artifacts/human-takeover-native.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        root.destroy()

    def later(function, delay=80):
        def invoke():
            try:
                function()
            except Exception as error:
                finish(error)
        root.after(delay, invoke)

    def start():
        nonlocal stage
        stage = 'activating_test_window'
        root.lift()
        entry.focus_force()
        root.update()
        arbiter.begin_operation(require_target_foreground=False)
        x, y = entry.winfo_rootx() + 30, entry.winfo_rooty() + 10
        native.WindowFromPoint.argtypes = [wintypes.POINT]
        native.WindowFromPoint.restype = wintypes.HWND
        under_pointer = native.WindowFromPoint(wintypes.POINT(x, y))
        if native.GetAncestor(under_pointer, 2) != window:
            raise RuntimeError('Self-test entry is occluded; no click performed')
        # The first bounded click activates only our verified test window.
        writer.monitor = monitor
        writer.click((x, y))
        writer.monitor = WindowMonitor()
        later(type_own)

    def type_own():
        nonlocal stage
        stage = 'typing_own_tagged_text'
        writer.type_text(expected, pause=0.005)
        report['own_input_not_paused'] = not arbiter.interrupted and arbiter.own_events > 0
        later(foreign_mouse)

    def foreign_mouse():
        nonlocal stage
        stage = 'injecting_foreign_mouse'
        report['own_text_received'] = entry.get() == expected
        x, y = entry.winfo_rootx() + 20, entry.winfo_rooty() + 10
        move = writer._move_record((x, y))
        wheel = writer._mouse(0x0800, 120)
        for record in (move, wheel):
            record.mi.dwExtraInfo = arbiter.own_tag ^ 0x100
        array = (INPUT * 2)(move, wheel)
        if native.SendInput(2, array, ctypes.sizeof(INPUT)) != 2:
            raise RuntimeError('Foreign test input injection failed')
        later(paused)

    def paused():
        nonlocal stage
        stage = 'verifying_pause'
        report['foreign_wheel_detected'] = any(kind == 'wheel' for _, kind in arbiter.events)
        try:
            writer.type_text('must_not_type', pause=0)
        except UserInterference:
            report['input_blocked_while_paused'] = entry.get() == expected
        tag = arbiter.own_tag ^ 0x100
        # Shift down/up generates no text and remains inside this test window.
        array = (INPUT * 2)(INPUT(type=1, ki=KEYBDINPUT(0x10, 0, 0, 0, tag)),
                            INPUT(type=1, ki=KEYBDINPUT(0x10, 0, 2, 0, tag)))
        if native.SendInput(2, array, ctypes.sizeof(INPUT)) != 2:
            raise RuntimeError('Foreign keyboard test injection failed')
        later(resume, 400)

    def resume():
        nonlocal stage
        stage = 'resuming_after_idle'
        report['foreign_keyboard_detected'] = any(kind == 'keyboard' for _, kind in arbiter.events)
        try:
            arbiter.check()
        except UserInterference:
            report['resume_requires_new_operation'] = True
        arbiter.begin_operation(require_target_foreground=False)
        report['resumed_after_idle'] = not arbiter.interrupted
        writer.type_text('_resumed', pause=0.005)
        later(verify)

    def verify():
        nonlocal stage
        stage = 'verifying_resumed_text'
        report['resumed_text_received'] = entry.get() == expected + '_resumed'
        finish()

    root.protocol('WM_DELETE_WINDOW', lambda: finish('Test window closed by user'))
    root.after(15000, lambda: finish('Self-test timeout'))
    later(start, 300)
    root.mainloop()
    if not report.get('passed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
