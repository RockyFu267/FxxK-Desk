"""Visible desktop chat navigation, observation and verified message sending."""
import hashlib
from pathlib import Path
import time
from collections import deque

from .targets import matches_chat_header
from .windows import Desktop
from .interference import UserInterference, ActionDeferred, ClipboardChanged, DraftVerificationFailed


class ChatDesktop:
    supports_backfill = True
    supports_timestamp_anchor = True
    supports_input_arbitration = True
    def __init__(self):
        import numpy as np
        from rapidocr import RapidOCR
        from pywinauto import Desktop as UIADesktop
        from .interference import InputArbiter
        from .native_input import InputMonitor, TaggedInputs, system_idle_seconds

        self.desktop = Desktop()
        self.np = np
        self.engine = RapidOCR()
        self.uia = UIADesktop(backend="uia")
        self.interference = InputArbiter(initial_idle=system_idle_seconds())
        self.monitor = InputMonitor(self.interference)
        self.monitor.start()
        self.inputs = TaggedInputs(self.interference, self.monitor, self.action_guard)
        self.mouse = self.inputs
        self.desktop.input_guard = self.activation_guard
        self.desktop.foreground_fallback = self.focus_by_pointer
        self.send_phase = 'preparing'
        self.scroll_profiles = {}
        self.metrics = {"frame_captures": 0, "ocr_calls": 0, "ocr_seconds": 0.0, "scroll_actions": deque(maxlen=200)}

    def ocr(self, image):
        started = time.monotonic()
        # Desktop text is upright. Angle classification can rotate a line of
        # question marks and corrupt otherwise readable event identifiers.
        result = self.engine(image, use_cls=False)
        self.metrics["ocr_calls"] += 1
        self.metrics["ocr_seconds"] += time.monotonic() - started
        if result.boxes is None:
            return [], None
        return list(zip(result.boxes, result.txts, result.scores)), None

    def ensure_app(self, target):
        self.check_stopped()
        self.target = target
        self.interference.idle_seconds = target.human_idle_seconds
        self.monitor.ensure_healthy()
        self.monitor.require_desktop()
        foreground = self.desktop.u.GetForegroundWindow()
        visible = self.desktop.windows()
        passive = getattr(self, 'passive_observation', False)
        initial_focus = bool((getattr(self, 'startup_focus_pending', False) or self.interference.automation_priority) and self.interference.action_ready())
        if initial_focus:
            passive = self.passive_observation = False
        self.interference.begin_operation(
            target_foreground=any(w.id == foreground and w.title == target.window_title for w in visible),
            require_target_foreground=False if passive or initial_focus else target.resume_requires_target_foreground)
        if not any(w.title == target.window_title for w in self.desktop.windows()):
            if passive:
                raise ActionDeferred('等待软件打开；后台观察不会抢占当前窗口')
            self.desktop.launch(target.executable)
        deadline = time.monotonic() + target.timeout_seconds
        while time.monotonic() < deadline:
            candidates = [w for w in self.desktop.windows() if w.title == target.window_title]
            if len(candidates) > 1:
                raise RuntimeError("Multiple matching application windows")
            if candidates:
                window = candidates[0]
                # Bind to the configured executable, not only a lookalike title.
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x1000, False, window.pid)
                if not handle:
                    raise RuntimeError("Cannot verify application process")
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        raise RuntimeError("Cannot read application executable")
                    if Path(buffer.value).resolve() != Path(target.executable).resolve():
                        raise RuntimeError("Window executable does not match configuration")
                finally:
                    kernel.CloseHandle(handle)
                self.window = window
                self.monitor.target_hwnd = window.id
                if not passive:
                    if getattr(self, 'region_file', None) and not initial_focus:
                        self.action_guard()
                    if getattr(self, 'region_file', None) and foreground != window.id and not initial_focus:
                        raise ActionDeferred('等待软件回到前台，暂缓自动操作')
                    self.desktop.activate(window)
                    self.startup_focus_pending = False
                self.wrapper = self.uia.window(handle=window.id).wrapper_object()
                self.target = target
                self.guard()
                if getattr(self, 'pointer_initialization_pending', False) and not passive:
                    self.inputs.park()
                    self.pointer_initialization_pending = False
                return
            time.sleep(0.3)
        raise RuntimeError("Application did not expose its configured window; check login or tray state")

    def guard(self):
        self.check_stopped()
        self.monitor.check()
        self.check_region()
        if not self.desktop.u.IsWindow(self.window.id):
            raise UserInterference('软件窗口已关闭，等待重新打开')
        if self.interference.automation_priority and self.desktop.u.GetForegroundWindow() != self.window.id:
            self.desktop.activate(self.window)
        if getattr(self, 'passive_observation', False):
            self.check_unobscured()
        elif self.desktop.u.GetForegroundWindow() != self.window.id:
            self.interference.observe('window_or_focus_changed', None)
            raise UserInterference("Target window or focus changed; automation paused")

    def action_guard(self):
        self.guard()
        if self.desktop.u.GetForegroundWindow() != self.window.id or not self.interference.action_ready():
            raise ActionDeferred('本轮需要鼠标或键盘操作，等待输入空闲并将软件置于前台；本轮采集尚未完成')

    def activation_guard(self):
        self.check_stopped()
        self.monitor.check()
        if getattr(self, 'region_file', None) and not self.interference.action_ready():
            raise ActionDeferred('等待输入空闲后执行本次启动定位')

    def focus_by_pointer(self, window):
        if not self.interference.automation_priority:
            return
        import ctypes
        from ctypes import wintypes
        self.activation_guard()
        self.check_region()
        self.check_unobscured()
        bounds = wintypes.RECT()
        if not self.desktop.u.GetWindowRect(window.id, ctypes.byref(bounds)):
            raise ActionDeferred('无法核对软件标题栏位置')
        original = self.inputs.context_guard
        def safe_focus_click():
            self.activation_guard()
            self.check_region()
            self.check_unobscured()
        try:
            # Explicit priority permits an ordinary click on the verified,
            # unobscured blank caption when Windows refuses API activation.
            self.inputs.context_guard = safe_focus_click
            self.inputs.click(((bounds.left+bounds.right)//2, bounds.top+20))
            time.sleep(.1)
        finally:
            self.inputs.context_guard = original

    def check_unobscured(self):
        """Visible pixels are usable only while no other application's window overlaps."""
        import ctypes
        import os
        from ctypes import wintypes
        u = self.desktop.u
        if u.IsIconic(self.window.id):
            raise ActionDeferred('软件已最小化，等待恢复可见窗口')
        target = wintypes.RECT()
        if not u.GetWindowRect(self.window.id, ctypes.byref(target)):
            raise ActionDeferred('无法确认软件位置')
        outcome = {'found': False, 'blocked': False}
        target_pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(self.window.id, ctypes.byref(target_pid))
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        @self.desktop.callback_type
        def visit(hwnd, _):
            if hwnd == self.window.id:
                outcome['found'] = True
                return False
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid() or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
                return True
            # Mouse-transparent layered tool overlays (including the desktop
            # debugger's cursor surface) have screen-sized bounds but do not
            # obstruct input and are omitted by our non-layered screen grab.
            extended = u.GetWindowLongPtrW(hwnd, -20)
            if extended & (0x80000 | 0x20 | 0x08000000) == (0x80000 | 0x20 | 0x08000000):
                return True
            # WXWork exposes its non-client title bar as a separate owned
            # top-level window. It is decoration, not an occluding dialog.
            name = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, name, len(name))
            if (name.value == 'TitleBarWindow' and pid.value == target_pid.value
                    and u.GetAncestor(hwnd, 3) == self.window.id):
                return True
            bounds = wintypes.RECT()
            if u.GetWindowRect(hwnd, ctypes.byref(bounds)) and bounds.left < target.right and bounds.right > target.left and bounds.top < target.bottom and bounds.bottom > target.top:
                outcome['blocked'] = True
                return False
            return True
        u.EnumWindows(visit, 0)
        if outcome['blocked']:
            raise ActionDeferred('软件被其他窗口遮挡，等待恢复可见区域')
        if not outcome['found']:
            raise ActionDeferred('软件窗口暂不可见')

    def check_region(self):
        if getattr(self, 'obstructed', lambda: False)():
            raise UserInterference('请移开或最小化控制台，避免遮挡监控区域')
        if getattr(self, 'region_file', None) and hasattr(self, 'window'):
            import ctypes
            from ctypes import wintypes
            from .capture_area import Rect, load_region
            bounds = wintypes.RECT()
            if not self.desktop.u.GetWindowRect(self.window.id, ctypes.byref(bounds)):
                raise RuntimeError('Cannot read target window bounds')
            if not load_region(self.region_file).contains(Rect(bounds.left, bounds.top, bounds.right, bounds.bottom)):
                raise UserInterference('请把软件窗口完整放进监控区域')

    def check_stopped(self):
        if getattr(self, 'stop_event', None) is not None and self.stop_event.is_set():
            from .relay_monitor import TaskStopped
            raise TaskStopped('Task stopped from console')

    def close(self):
        self.monitor.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def wait_for_user(self):
        # Waiting itself never activates a window or sends input. The caller
        # must restart from ensure_app/open_target after this returns.
        while True:
            self.check_stopped()
            self.monitor.ensure_healthy()
            foreground = self.desktop.u.GetForegroundWindow()
            is_target = any(w.id == foreground and w.title == self.target.window_title for w in self.desktop.windows())
            if self.monitor.desktop_available() and self.interference.resumable(is_target, self.target.resume_requires_target_foreground):
                try:
                    self.check_region()
                except UserInterference:
                    time.sleep(0.2)
                    continue
                self.scroll_profiles.clear()
                return
            time.sleep(0.2)

    def regions(self):
        image, rect = self.frame()
        # OCR stays on this computer. Retain original pixel coordinates.
        result, _ = self.ocr(self.np.array(image)[:, :, ::-1].copy())
        items = []
        for box, text, score in result or []:
            if score >= 0.85:
                x = sum(p[0] for p in box) / 4
                y = sum(p[1] for p in box) / 4
                items.append((text.strip(), x, y))
        return rect, items

    def frame(self):
        import ctypes
        from ctypes import wintypes
        from PIL import ImageGrab
        from pywinauto.win32structures import RECT
        self.guard()
        if getattr(self, 'on_observing', None):
            self.on_observing()
        self.metrics["frame_captures"] += 1
        bounds = wintypes.RECT()
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        if dwm.DwmGetWindowAttribute(self.window.id, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)) != 0:
            raise RuntimeError("Cannot determine visible window bounds")
        try:
            image = ImageGrab.grab(bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom), all_screens=True)
        except OSError:
            image = ImageGrab.grab(window=self.window.id)
        self.guard()
        rect = RECT(bounds.left, bounds.top, bounds.right, bounds.bottom)
        if image.size != (rect.width(), rect.height()):
            raise RuntimeError("Screenshot dimensions do not match window coordinates")
        return image, rect

    def calibrated_frame(self):
        image, rect = self.frame()
        if image.size != (self.target.layout_width, self.target.layout_height):
            raise RuntimeError(f'软件当前大小 {image.width}×{image.height}，需要已校准的 {self.target.layout_width}×{self.target.layout_height}；请调整窗口大小或重新校准布局')
        return image, rect

    def message_snapshot(self, chat):
        if not self.chat_is_open(chat):
            raise RuntimeError("Target chat title not verified")
        image, _ = self.calibrated_frame()
        image = image.crop(tuple(self.target.message_rect))
        digest = hashlib.sha256(image.tobytes()).hexdigest()
        path = Path("artifacts/observations") / (digest + ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return path

    def reveal_message_time(self, page, bubble, *, capture_evidence=None):
        """Read a seconds timestamp from the sender row of the clicked bubble."""
        from PIL import Image
        from .timestamp_backfill import parse_message_time

        if getattr(self, 'passive_observation', False):
            return None
        left, top, right, bottom = self.target.message_rect
        x0, y0, x1, y1 = bubble
        if not (0 <= x0 < x1 <= right-left and 8 <= y0 < y1 <= bottom-top
                and x1-x0 >= 24 and y1-y0 >= 20):
            return None
        if not self.chat_is_open(self.target.chat):
            raise ActionDeferred('来源群已切换，暂缓读取消息时间')
        box = (left+x0, top+y0, left+x1, top+min(y1, y0+48))
        with Image.open(page.screenshot) as recorded:
            original = recorded.convert('RGB').crop((x0, y0, x1, min(y1, y0+48)))
        image, rect = self.calibrated_frame()
        if image.crop(box).convert('RGB').tobytes() != original.tobytes():
            return None
        # Click the right padding so the captured pointer lies mostly outside
        # the bubble. Its shadow and the hover tint can still change RGB values.
        x, y = left+x1-7, top+y0+min(16, (y1-y0)//2)
        stable_width = original.width - min(28, original.width//3)
        reference = self.np.min(self.np.asarray(original)[:, :stable_width], axis=2) < 105
        header_box = (max(left, left+x0-8), max(top, top+y0-43), right, top+y0-2)

        def timestamp_from(frame):
            header = frame.crop(header_box)
            enlarged = header.resize((header.width*2, header.height*2))
            return parse_message_time(self.recognize(enlarged))

        def save_timestamp(frame, timestamp):
            if timestamp and capture_evidence:
                capture_evidence(frame.crop((left, max(top, top+y0-43), right,
                                             top+min(y1, y0+240))))
            return timestamp

        def read_header():
            for delay in (.18, .22):
                time.sleep(delay)
                current_frame, fresh = self.calibrated_frame()
                if fresh != rect:
                    return None
                current = current_frame.crop(box).convert('RGB')
                if current.tobytes() != original.tobytes():
                    glyphs = self.np.min(self.np.asarray(current)[:, :stable_width], axis=2) < 105
                    ink = self.np.count_nonzero(reference | glyphs)
                    if ink < 12 or self.np.count_nonzero(reference ^ glyphs) > max(3, ink*.03):
                        continue
                timestamp = timestamp_from(current_frame)
                if timestamp:
                    return save_timestamp(current_frame, timestamp)
            return save_timestamp(image, timestamp_from(image))

        return self.mouse.click((rect.left+x, rect.top+y), observe=read_header)

    def observe_latest_message_time(self):
        """After delivery, get evidence for just the newest message."""
        from .backfill import Page
        from .timestamp_backfill import locate_latest_bubble_head

        self.return_to_latest(verify=False)

        def read():
            path = self.message_snapshot(self.target.chat)
            return Page(self.extract_local_messages(path), str(path), path.stem)

        page, message, moves = locate_latest_bubble_head(
            read, lambda: self.scroll_chat('up'),
            self.target.backfill_max_pages, self.target.backfill_max_seconds,
        )
        evidence = None

        def save(image):
            nonlocal evidence
            directory = Path('artifacts/timestamp-evidence')
            directory.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(image.tobytes()).hexdigest()
            evidence = directory / (digest + '.png')
            image.save(evidence)

        timestamp = self.reveal_message_time(page, message['bubble_rect'], capture_evidence=save) if message else None
        if moves and not self.return_to_latest(verify=False):
            self.return_to_latest()
        return {'time': timestamp, 'screenshot': str(evidence) if evidence else None, 'scrolls': moves}

    def jump_to_visible_new_messages(self):
        if getattr(self, 'passive_observation', False):
            return False
        rect, items = self.regions()
        import re
        matches = [(x, y) for text, x, y in items
                   if re.search(r'\d+\s*条新消息', text) and x > self.target.message_rect[0]
                   and self.target.message_rect[1] < y < self.target.message_rect[3]]
        if len(matches) == 1:
            x, y = matches[0]
            self.mouse.click((round(rect.left+x), round(rect.top+y)))
            time.sleep(.25)
            return True
        return False

    def return_to_latest(self, *, verify=True):
        from .interference import ActionDeferred
        from .scrolling import ScrollFeedbackUncertain

        rect, items = self.regions()
        left, top, right, bottom = self.target.message_rect
        controls = [(x, y) for label, x, y in items
                    if ''.join(label.split()) == '回到最新位置'
                    and left + (right-left)*.65 < x < right
                    and top + (bottom-top)*.8 < y < bottom]
        if len(controls) == 1:
            if not self.chat_is_open(self.target.chat):
                raise ActionDeferred('目标群已切换，无法回到最新位置')
            x, y = controls[0]
            self.mouse.click((round(rect.left+x), round(rect.top+y)))
            time.sleep(.3)
        if not verify:
            return bool(controls and len(controls) == 1)
        for _ in range(4):
            try:
                stats = self.scroll_chat('down', fast=True)
            except ScrollFeedbackUncertain:
                continue
            if stats.stopped_at_boundary:
                return
        raise ActionDeferred('尚未确认目标群已回到最新位置')

    def scroll_chat(self, direction, fast=False):
        import cv2
        from ctypes import wintypes
        from .scrolling import PixelScroller, ScrollProfile
        if direction not in ("up", "down"):
            raise ValueError("Invalid scroll direction")
        if not self.chat_is_open(self.target.chat):
            raise RuntimeError("Chat changed before scroll")
        image, rect = self.calibrated_frame()
        left, top, right, bottom = self.target.message_rect
        self.guard()
        # Cursor is placed strictly in the chat-history pane, not the sidebar
        # or composer. pywinauto uses positive wheel distance for upward scroll.
        coords = (rect.left + (left + right) // 2, rect.top + (top + bottom) // 2)
        crop = (left, top, right, bottom)

        def features(frame):
            gray = cv2.cvtColor(self.np.array(frame.crop(crop)), cv2.COLOR_RGB2GRAY)
            # Ignore pale stationary watermarks; correlate high-contrast text.
            return (gray < 180).astype(self.np.float32)

        def capture():
            frame, fresh = self.calibrated_frame()
            if fresh != rect:
                raise RuntimeError("Window moved during scroll")
            return features(frame)

        def wheel(count):
            for _ in range(count):
                self.guard()
                self.mouse.scroll(coords=coords, wheel_dist=self.target.scroll_notches if direction == "up" else -self.target.scroll_notches)
                time.sleep(0.04)
            # A batch leaves a smooth-scroll animation in flight. Capture only
            # after settling, otherwise its residual motion corrupts px/event.
            time.sleep(0.20 if self.target.scroll_mode == "batch" else 0.12)

        # Separate direction/client/layout caches; never reuse another chat's
        # calibration, and expire it after a minute in PixelScroller.
        get_dpi = getattr(self.desktop.u, "GetDpiForWindow", None)
        dpi = 96
        if get_dpi:
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            dpi = get_dpi(self.window.id)
        key = (self.window.id, self.target.chat, tuple(crop), image.size, rect.left, rect.top,
               dpi, self.target.scroll_notches, self.target.scroll_batch_events, direction)
        profile = self.scroll_profiles.get(key)
        if profile is None or time.monotonic() - profile.updated_at > 60:
            profile = ScrollProfile(max_batch_events=self.target.scroll_batch_events)
            self.scroll_profiles[key] = profile
        scroller = PixelScroller(capture, wheel, direction, bottom - top, profile, self.target.scroll_mode)
        try:
            scroller.run(features(image), (bottom - top) * (1.5 if fast else 0.4), 48 if fast else 16)
        finally:
            self.metrics["scroll_actions"].append(scroller.last_stats.to_dict())
        time.sleep(0.15)
        self.guard()
        return scroller.last_stats

    def recognize(self, image):
        result, _ = self.ocr(self.np.array(image)[:, :, ::-1].copy())
        return "".join(text for _, text, score in result or [] if score >= 0.85)

    def text_matches(self, image, text):
        import unicodedata
        normalize = lambda value: ''.join(unicodedata.normalize('NFKC', value).split())
        if normalize(self.recognize(image)) == normalize(text):
            return True
        enlarged = image.resize((image.width*3, image.height*3))
        return normalize(self.recognize(enlarged)) == normalize(text)

    def editor_text_matches(self, image, text):
        if self.text_matches(image, text):
            return True
        if '\n' not in text:
            return False
        import unicodedata
        from PIL import ImageOps

        def normalize(value):
            return ''.join(unicodedata.normalize('NFKC', value).split())

        # A dense multiline draft can be detected as one block, lose a row,
        # or reorder rows in the whole crop. Read separate visible rows.
        pixels = self.np.asarray(image.convert('L'))
        active = self.np.count_nonzero(pixels < 155, axis=1) >= 2
        edges = self.np.flatnonzero(self.np.diff(self.np.r_[False, active, False]))
        bands = []
        for start, end in zip(edges[::2], edges[1::2]):
            if bands and start - bands[-1][1] <= 3:
                bands[-1] = (bands[-1][0], end)
            else:
                bands.append((start, end))
        expected = normalize(text)
        offsets = {0}
        for start, end in bands:
            columns = self.np.flatnonzero(self.np.any(pixels[start:end] < 155, axis=0))
            row = image.crop((int(columns[0]), int(start), int(columns[-1])+1, int(end)))
            row = ImageOps.expand(row, border=8, fill='white')
            candidates = {normalize(self.recognize(row)),
                          normalize(self.recognize(row.resize((row.width*2, row.height*2))))}
            offsets = {offset + len(candidate) for offset in offsets for candidate in candidates
                       if candidate and expected.startswith(candidate, offset)}
            if not offsets:
                return False
        return len(expected) in offsets

    def editor_caret(self, frame):
        """Return the Windows caret rectangle relative to the verified editor."""
        import ctypes
        from ctypes import wintypes
        class Info(ctypes.Structure):
            _fields_ = [('cbSize',wintypes.DWORD),('flags',wintypes.DWORD),
                        ('active',wintypes.HWND),('focus',wintypes.HWND),('capture',wintypes.HWND),
                        ('menu',wintypes.HWND),('move',wintypes.HWND),('caret',wintypes.HWND),('rect',wintypes.RECT)]
        editor = tuple(self.target.editor_rect)
        u = self.desktop.u
        u.GetGUIThreadInfo.argtypes = [wintypes.DWORD,ctypes.POINTER(Info)]
        u.ClientToScreen.argtypes = [wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
        info = Info()
        info.cbSize = ctypes.sizeof(info)
        if u.GetGUIThreadInfo(0,ctypes.byref(info)) and info.caret:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(info.caret,ctypes.byref(pid))
            width, height = info.rect.right-info.rect.left, info.rect.bottom-info.rect.top
            point = wintypes.POINT(info.rect.left,info.rect.top)
            if pid.value == self.window.pid and 0 < width <= 3 and 8 <= height <= 60 and u.ClientToScreen(info.caret,ctypes.byref(point)):
                x,y = point.x-frame.left-editor[0],point.y-frame.top-editor[1]
                if 0 <= x and 0 <= y and x+width <= editor[2]-editor[0] and y+height <= editor[3]-editor[1]:
                    return x, y, x+width, y+height
        return None

    def editor_image(self, image, frame):
        """Remove only the caret rectangle reported by Windows, not text glyphs."""
        result = image.crop(tuple(self.target.editor_rect))
        caret = self.editor_caret(frame)
        if caret:
            result.paste('white', caret)
        return result

    def editor_empty(self, editor):
        # A blinking insertion caret is frequently recognized as "1". Require
        # a genuinely blank frame; a real typed character will not blink away.
        for _ in range(6):
            image, _ = self.calibrated_frame()
            rgb = self.np.array(image.crop(editor).convert("RGB"))
            dark = self.np.any(rgb < 245, axis=2)
            ys, xs = self.np.where(dark)
            if not xs.size:
                return True
            # Some remote sessions render a steady caret instead of blinking.
            # This is the calibrated 1×19 insertion bar at the empty origin,
            # not an OCR guess about the string "1".
            if (xs.min() == xs.max() == 10 and ys.min() == 5 and ys.max() == 23
                    and xs.size == 19):
                return True
            if (xs.min() == xs.max() == 14 and ys.min() == 11 and ys.max() == 35
                    and xs.size == 25):
                return True  # Calibrated high-DPI steady insertion bar.
            if (xs.max() - xs.min() > 1 or ys.max() - ys.min() > 28
                    or xs.min() > 20 or ys.min() > 40 or xs.size > 58):
                return False
            time.sleep(0.2)
        return False

    def extract_local_messages(self, image_path):
        """Group OCR lines inside visible light/dark-blue message bubbles."""
        import cv2
        from PIL import Image
        rgb = self.np.array(Image.open(image_path).convert("RGB"))
        lines, _ = self.ocr(rgb[:, :, ::-1].copy())
        bubbles = []
        for direction, color in (("incoming", (255, 255, 255)), ("incoming", (228, 231, 235)), ("outgoing", (201, 231, 255))):
            delta = self.np.abs(rgb.astype("int16") - self.np.array(color))
            mask = (self.np.max(delta, axis=2) <= 2).astype("uint8") * 255
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, width, height = cv2.boundingRect(contour)
                if width < 60 or height < 24 or cv2.contourArea(contour) < width * height * 0.7:
                    continue
                inside = []
                for box, text, score in lines:
                    cx, cy = self.np.mean(box, axis=0)
                    if x <= cx <= x + width and y <= cy <= y + height:
                        if y <= 1 and self.np.min(box[:, 1]) <= 2:
                            continue
                        if y+height >= rgb.shape[0]-1 and self.np.max(box[:, 1]) >= rgb.shape[0]-2:
                            continue
                        inside.append((float(cy), float(cx), text, float(score)))
                if not inside:
                    if y <= 1 or y+height >= rgb.shape[0]-1:
                        bubbles.append((y, {'text': '', 'sender': None, 'direction': direction, 'complete': False,
                                            'top_clipped': y <= 1, 'bottom_clipped': y+height >= rgb.shape[0]-1,
                                            'bubble_left': x, 'bubble_right': x+width, 'ocr_reliable': False,
                                            'source': 'unreadable_clipped_bubble'}))
                    continue
                # The client's floating history-navigation button is not a
                # chat bubble. Its fixed viewport position otherwise becomes
                # a false overlap anchor while the real messages scroll.
                navigation = ''.join(item[2] for item in inside)
                if (x > rgb.shape[1] * .65 and y > rgb.shape[0] * .8
                        and height < 80 and width < 240
                        and '回到最新位置' in navigation):
                    continue
                inside.sort()
                sender = None
                if direction == "incoming":
                    labels = [(float(self.np.mean(box, axis=0)[1]), text) for box, text, score in lines
                              if score >= 0.85 and x - 8 <= float(self.np.mean(box, axis=0)[0]) <= x + min(200, width)
                              and y - 35 < float(self.np.mean(box, axis=0)[1]) < y - 2]
                    if labels:
                        sender = max(labels)[1]
                geometric = y > 1 and y+height < rgb.shape[0]-1
                uncertain_id = None
                if geometric and not all(item[3] >= .85 for item in inside):
                    import re
                    enlarged = Image.fromarray(rgb[y:y+height, x:x+width]).resize((width*3, height*3))
                    reviewed = self.recognize(enlarged)
                    identifiers = set(re.findall(r'FD-[a-f0-9]{10}-\d+', reviewed))
                    if len(identifiers) == 1:
                        uncertain_id = next(iter(identifiers))
                # Unknown sender stays unknown; never reuse another bubble's sender.
                bubbles.append((y, {"text": ('识别不确定的测试消息 '+uncertain_id if uncertain_id else "\n".join(item[2] for item in inside)), "sender": sender,
                                    "direction": direction, "complete": geometric
                                    and (bool(uncertain_id) or all(item[3] >= 0.85 for item in inside)),
                                    "source": 'ocr_uncertain_marker' if uncertain_id else "local_ocr",
                                    'geometrically_complete': y > 1 and y+height < rgb.shape[0]-1,
                                    'top_clipped': y <= 1, 'bottom_clipped': y+height >= rgb.shape[0]-1,
                                    'bubble_left': x, 'bubble_right': x+width,
                                    'ocr_reliable': all(item[3] >= .85 for item in inside),
                                    'ocr_confidence': min(item[3] for item in inside),
                                    'bubble_rect': [x, y, x+width, y+height]}))
        import re
        ordered = sorted(bubbles, key=lambda entry: entry[0])
        for box, label, score in lines:
            label = ''.join(label.split()).replace('：', ':')
            x, y = self.np.mean(box, axis=0)
            if (score < .85 or abs(x - rgb.shape[1] / 2) > rgb.shape[1] * .08
                    or not re.fullmatch(r'(?:(?:昨天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日))?(?:[01]?\d|2[0-3]):[0-5]\d', label)):
                continue
            # A separator must be outside every bubble and immediately above
            # the next one. It is evidence for that occurrence only.
            if any(top <= y < item.get('bubble_rect', [0, 0, 0, top])[3] for top, item in ordered):
                continue
            following = next((item for top, item in ordered if 0 < top-y <= 125), None)
            if following is not None and not following.get('top_clipped'):
                following['visible_time_label'] = label
        return [item for _, item in ordered]

    def require_empty_relay_editor(self):
        from .interference import ActionDeferred

        if not self.editor_empty(tuple(self.target.editor_rect)):
            raise ActionDeferred('通知群输入框有未发送草稿。请核对并清空输入框，后续消息将在下一轮发送。')

    def send_reply(self, chat, text):
        self.send_phase = 'preparing'
        if not text or any(char in text for char in '\r\0'):
            raise ValueError("Invalid reply text")
        if not self.chat_is_open(chat):
            raise RuntimeError("Wrong chat")
        image, rect = self.calibrated_frame()
        editor = tuple(self.target.editor_rect)
        if not self.editor_empty(editor):
            raise RuntimeError("Editor contains a draft; refusing to overwrite it")
        self.guard()
        self.mouse.click(coords=(rect.left + (editor[0] + editor[2]) // 2,
                                 rect.top + (editor[1] + editor[3]) // 2))
        self.guard()
        self.send_phase = 'editing'
        if len(text) > 500 or any(char in text for char in '\n\t'):
            self.inputs.paste_text(text)
        else:
            self.inputs.type_text(text, pause=0.02)
        time.sleep(0.4)
        self.finish_verified_draft(chat, text)

    def finish_verified_draft(self, chat, text):
        import unicodedata
        self.send_phase = 'editing'
        editor = tuple(self.target.editor_rect)
        image, fresh = self.calibrated_frame()
        normalize = lambda value: ''.join(unicodedata.normalize('NFKC',value).split())
        full_readback = len(text) > 500 or text.count('\n') > 5
        if not full_readback and not self.editor_text_matches(self.editor_image(image, fresh), text):
            raise DraftVerificationFailed('通知群草稿与预期正文核验不一致，已保留草稿，未点击发送')
        if not self.chat_is_open(chat):
            raise RuntimeError("Chat changed before send")
        last_image, last_rect = self.calibrated_frame()
        if last_rect != fresh:
            raise RuntimeError("Window moved before send")
        if full_readback:
            try:
                copied = self.inputs.read_editor_text(lambda: self.editor_caret(last_rect) is not None, self.window.pid)
            except ClipboardChanged as error:
                raise DraftVerificationFailed('读取全文时剪贴板发生变化，已保留草稿，未点击发送') from error
            if copied is None or copied.replace('\r\n', '\n') != text:
                raise DraftVerificationFailed('无法从输入框读回一致的全文，已保留草稿，未点击发送')
            last_image, verified_rect = self.calibrated_frame()
            if verified_rect != last_rect:
                raise DraftVerificationFailed('全文核验后窗口位置发生变化，未点击发送')
        elif not self.editor_text_matches(self.editor_image(last_image,last_rect),text):
            raise DraftVerificationFailed('发送前重新核验草稿失败，已保留草稿，未点击发送')
        self.guard()
        # Use the freshly observed window position, never a saved desktop coordinate.
        x, y = self.target.send_point
        self.send_phase = 'sending'
        self.mouse.click(coords=(fresh.left + x, fresh.top + y))
        time.sleep(0.7)
        image, _ = self.calibrated_frame()
        if not self.editor_empty(editor):
            raise RuntimeError("Editor did not clear; send outcome unknown")
        # Bubble must be visible on the outgoing (right) side, not merely anywhere.
        result, _ = self.ocr(self.np.array(image)[:, :, ::-1].copy())
        message_left, message_top, message_right, message_bottom = self.target.message_rect
        visible = "".join(value for box, value, score in result or []
                          if score >= 0.85 and min(p[0] for p in box) > message_left + 70
                          and max(p[0] for p in box) < message_right
                          and message_top < min(p[1] for p in box) < message_bottom)
        # A full-size outgoing bubble can extend beyond the message viewport.
        # The exact editor check proved the full draft; validate its visible tail.
        expected = normalize(text)
        if full_readback:
            pane = tuple(self.target.message_rect)
            if last_image.crop(pane).tobytes() == image.crop(pane).tobytes():
                raise RuntimeError("No visible change in message area; send outcome unknown")
            expected = expected[-72:]
        if expected not in normalize(visible):
            crop = image.crop((message_left+70, message_top, message_right, message_bottom))
            enlarged = crop.resize((crop.width*3, crop.height*3))
            if expected not in normalize(self.recognize(enlarged)):
                raise RuntimeError("Sent text not visible in message area; send outcome unknown")
        self.guard()
        self.send_phase = 'verified'

    def chat_is_open(self, chat):
        rect, items = self.regions()
        return any(matches_chat_header(text, chat) and x > self.target.message_rect[0]
                   and y < min(110, rect.height() * 0.2) for text, x, y in items)

    def search(self, target):
        self.guard()
        self.inputs.hotkey('ctrl', 'f')
        time.sleep(0.4)
        self.guard()
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo
        from pywinauto.controls.uiawrapper import UIAWrapper
        focus = UIAWrapper(UIAElementInfo(IUIA().iuia.GetFocusedElement()))
        # Refuse to type unless Ctrl+F produced a search edit near the top-left.
        rect = self.wrapper.rectangle()
        edit_rect = focus.rectangle()
        if (focus.element_info.control_type != "Edit" or focus.element_info.process_id != self.window.pid
                or not rect.left <= edit_rect.left < rect.left + rect.width() * 0.55
                or not rect.top <= edit_rect.top < rect.top + min(160, rect.height() * 0.25)):
            # Recent clients expose a custom-painted search edit without UIA
            # Edit semantics. Require its unique focused blue outline first.
            import cv2
            image, rect = self.frame()
            rgb = self.np.array(image)
            blue = ((rgb[:, :, 2] > 220) & (rgb[:, :, 0] < 100) & (rgb[:, :, 1] > 80)).astype('uint8') * 255
            candidates = []
            for contour in cv2.findContours(blue, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
                x, y, width, height = cv2.boundingRect(contour)
                if 100 <= width <= image.width * .55 and 25 <= height <= 90 and y + height < 160 and x + width < image.width * .55:
                    candidates.append((x, y, width, height))
            if len(candidates) != 1:
                raise RuntimeError('Cannot verify unique focused search outline; no text entered')
            x, y, width, height = candidates[0]
            self.inputs.hotkey('ctrl', 'a')
            self.inputs.type_text(target.chat)
            time.sleep(.4)
            image, _ = self.frame()
            if target.chat not in self.recognize(image.crop((x+3, y+3, x+width-3, y+height-3))):
                raise RuntimeError('Search OCR does not match requested group; navigation stopped')
            return
        focus.set_edit_text(target.chat)
        self.guard()

    def open_exact_result(self, chat):
        rect, items = self.regions()
        matches = [(x, y) for text, x, y in items if matches_chat_header(text, chat)
                   and x < min(self.target.message_rect[0], rect.width() * 0.4)
                   and 65 < y < rect.height() * 0.8]
        if len(matches) > 1:
            raise RuntimeError("Multiple exact chat results; refusing ambiguous selection")
        if not matches:
            return False
        self.guard()
        _, fresh_rect = self.frame()
        if fresh_rect != rect:
            raise RuntimeError("Window moved during recognition; stopped")
        x, y = matches[0]
        self.mouse.click(coords=(round(rect.left + x), round(rect.top + y)))
        return True
