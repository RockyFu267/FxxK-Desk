"""Cooperatively managed relay task for the visual console."""
import threading
from types import SimpleNamespace

from .interference import UserInterference


class TaskStopped(UserInterference):
    pass


class RelayMonitor:
    def __init__(self, config, route, interval, send, region_file, status_path, obstructed=lambda: False, automation_priority=False,
                 before_cycle=None, pointer_anchor=None):
        self.stop_event = threading.Event()
        self.error = None
        self.args = SimpleNamespace(action='watch', config=str(config), route=route,
                                    database='artifacts/chat.sqlite3', interval=interval, send=send,
                                    stop_event=self.stop_event, region_file=str(region_file), status_path=status_path,
                                    obstructed=obstructed, automation_priority=automation_priority,
                                    before_cycle=before_cycle, pointer_anchor=pointer_anchor)
        self.thread = threading.Thread(target=self._run, daemon=True, name='chat-relay')

    def _run(self):
        from .relay import run
        try:
            run(self.args)
        except TaskStopped:
            pass
        except Exception as error:
            self.error = str(error)
            import logging
            logging.getLogger(__name__).exception('转述任务停止：%s', error)

    def start(self):
        self.thread.start()

    def stop(self, reason='点击停止任务'):
        self.args.stop_reason = reason
        self.stop_event.set()

    @property
    def alive(self):
        return self.thread.is_alive()
