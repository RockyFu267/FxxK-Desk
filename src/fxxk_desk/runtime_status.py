"""Local status heartbeat. A saved configuration is never a running task."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid

DEFAULT_STATUS = 'artifacts/runtime-status.local.json'
TERMINAL = {'stopped', 'error', 'completed'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_status(path=DEFAULT_STATUS, now=None, alive=process_alive):
    path = Path(path)
    if not path.exists():
        return {'state': 'idle', 'detail': '区域已保存，尚未启动任务'}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('state') in TERMINAL:
            return value
        age = (time.time() if now is None else now) - value['heartbeat_at']
        if not alive(value.get('pid')):
            return dict(value, state='interrupted', detail='任务进程已退出')
        if age < -5 or age > 10:
            return dict(value, state='stale', detail='状态心跳中断，不能确认正在运行')
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'detail': '暂时无法读取任务状态'}


class RuntimeStatus:
    def __init__(self, mode, path=DEFAULT_STATUS, task=None, heartbeat_interval=2):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.interval = heartbeat_interval
        self.value = {'run_id': uuid.uuid4().hex, 'pid': os.getpid(), 'mode': mode,
                      'task': task, 'state': 'starting', 'started_at': utc_now(),
                      'capture_count': 0, 'new_messages_total': 0, 'sent_total': 0}
        self.thread = threading.Thread(target=self._heartbeat, daemon=True, name='status-heartbeat')
        self.last_report = None

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.value['heartbeat_at'] = time.time()
        temporary = self.path.with_name(self.path.name + '.' + self.value['run_id'] + '.tmp')
        temporary.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows readers can briefly hold a handle without delete sharing.
        for attempt in range(10):
            try:
                temporary.replace(self.path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)

    def update(self, state=None, **fields):
        with self.lock:
            self.value.update(fields)
            if state is not None:
                self.value['state'] = state
            self._write()
            report = (self.value.get('state'), self.value.get('detail'), self.value.get('stop_reason'))
            if report != self.last_report:
                self.last_report = report
                # Diagnostic state only; never log screenshots or message bodies.
                print('[任务状态] ' + json.dumps({'time': utc_now(), 'state': report[0],
                      'detail': report[1] or '', 'stop_reason': report[2] or ''}, ensure_ascii=False), flush=True)

    def _heartbeat(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.update()
            except OSError:
                # The UI will explicitly mark a missing heartbeat stale.
                pass

    def __enter__(self):
        self.update()
        self.thread.start()
        return self

    def __exit__(self, kind, error, traceback):
        from .interference import UserInterference
        self.stop_event.set()
        self.thread.join(timeout=3)
        if error is not None and not isinstance(error, (KeyboardInterrupt, UserInterference)):
            self.update('error', error_type=type(error).__name__, detail=str(error), ended_at=utc_now())
        elif self.value['state'] != 'completed':
            self.update('stopped', ended_at=utc_now())
        return False
