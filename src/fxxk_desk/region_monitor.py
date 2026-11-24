"""Read-only saved-region sampler; never clicks, types, calls AI, or sends."""
from pathlib import Path
import threading

from .capture_area import capture_region
from .lease import desktop_lease
from .runtime_status import RuntimeStatus, DEFAULT_STATUS, utc_now


class RegionMonitor:
    def __init__(self, region_file, output='artifacts/region-latest.png', interval=5,
                 status_path=DEFAULT_STATUS, capture=capture_region, lease=desktop_lease, obstructed=lambda: False, before_cycle=None):
        if interval < 0.1:
            raise ValueError('Capture interval too small')
        self.region_file, self.output = str(region_file), str(output)
        self.interval, self.status_path = interval, status_path
        self.capture, self.lease = capture, lease
        self.obstructed = obstructed
        self.before_cycle = before_cycle
        self.stop_event, self.ready = threading.Event(), threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True, name='region-capture')

    def start(self):
        self.thread.start()
        if not self.ready.wait(3):
            raise RuntimeError('Capture task did not start')
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        try:
            with self.lease():
                with RuntimeStatus('region_capture', self.status_path) as status:
                    self.ready.set()
                    count = 0
                    while not self.stop_event.is_set():
                        if self.obstructed():
                            status.update('paused_for_panel', detail='状态窗口位于监控区内，请移开或最小化')
                            self.stop_event.wait(0.5)
                            continue
                        from .cycle_notice import announced_cycle
                        if self.before_cycle:
                            status.update('announcing', detail='即将工作，请留意各屏幕提示')
                        with announced_cycle(self.before_cycle, self.stop_event):
                            if self.stop_event.is_set():
                                break
                            status.update('capturing', detail='')
                            output = Path(self.output)
                            temporary = output.with_name(output.stem + '.capture.tmp.png')
                            self.capture(self.region_file, temporary)
                            if self.obstructed():
                                temporary.unlink(missing_ok=True)
                                status.update('paused_for_panel', detail='窗口进入监控区，本帧已丢弃')
                                continue
                            temporary.replace(output)
                            count += 1
                            status.update('watching', capture_count=count, last_capture_at=utc_now(),
                                          last_check_at=utc_now(), preview_path=str(output), interval_seconds=self.interval)
                        if self.stop_event.wait(self.interval):
                            break
        except Exception as error:
            if not self.stop_event.is_set():
                self.error = str(error)
            self.ready.set()

    def stop(self):
        self.stop_event.set()

    @property
    def alive(self):
        return self.thread.is_alive()
