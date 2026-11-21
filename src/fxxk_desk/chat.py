"""Visible desktop chat navigation, observation and verified message sending."""
import hashlib
from pathlib import Path
import time
from collections import deque

from .targets import matches_chat_header
from .windows import Desktop
from .interference import UserInterference


class ChatDesktop:
    supports_backfill = True
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
        self.inputs = TaggedInputs(self.interference, self.monitor, self.guard)
        self.mouse = self.inputs
        self.desktop.input_guard = self.monitor.check
        self.send_phase = 'preparing'
        self.scroll_profiles = {}
        self.metrics = {"frame_captures": 0, "ocr_calls": 0, "ocr_seconds": 0.0, "scroll_actions": deque(maxlen=200)}

    def ocr(self, image):
        started = time.monotonic()
        result = self.engine(image)
        self.metrics["ocr_calls"] += 1
        self.metrics["ocr_seconds"] += time.monotonic() - started
        if result.boxes is None:
            return [], None
        return list(zip(result.boxes, result.txts, result.scores)), None

    def ensure_app(self, target):
        self.target = target
        self.interference.idle_seconds = target.human_idle_seconds
        self.monitor.ensure_healthy()
        self.monitor.require_desktop()
        foreground = self.desktop.u.GetForegroundWindow()
        visible = self.desktop.windows()
        self.interference.begin_operation(
            target_foreground=any(w.id == foreground and w.title == target.window_title for w in visible),
            require_target_foreground=target.resume_requires_target_foreground)
        if not any(w.title == target.window_title for w in self.desktop.windows()):
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
                self.desktop.activate(window)
                self.wrapper = self.uia.window(handle=window.id).wrapper_object()
                self.target = target
                self.guard()
                return
            time.sleep(0.3)
        raise RuntimeError("Application did not expose its configured window; check login or tray state")

    def guard(self):
        self.monitor.check()
        if not self.desktop.u.IsWindow(self.window.id) or self.desktop.u.GetForegroundWindow() != self.window.id:
            self.interference.observe('window_or_focus_changed', None)
            raise UserInterference("Target window or focus changed; automation paused")

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
            self.monitor.ensure_healthy()
            foreground = self.desktop.u.GetForegroundWindow()
            is_target = any(w.id == foreground and w.title == self.target.window_title for w in self.desktop.windows())
            if self.monitor.desktop_available() and self.interference.resumable(is_target, self.target.resume_requires_target_foreground):
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
            raise RuntimeError("Window size changed; recalibrate configured layout before continuing")
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

    def recognize(self, image):
        result, _ = self.ocr(self.np.array(image)[:, :, ::-1].copy())
        return "".join(text for _, text, score in result or [] if score >= 0.85)

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
                        inside.append((float(cy), float(cx), text, float(score)))
                if not inside:
                    continue
                inside.sort()
                sender = None
                if direction == "incoming":
                    labels = [(float(self.np.mean(box, axis=0)[1]), text) for box, text, score in lines
                              if score >= 0.85 and x - 8 <= float(self.np.mean(box, axis=0)[0]) <= x + min(200, width)
                              and y - 35 < float(self.np.mean(box, axis=0)[1]) < y - 2]
                    if labels:
                        sender = max(labels)[1]
                # Unknown sender stays unknown; never reuse another bubble's sender.
                bubbles.append((y, {"text": "\n".join(item[2] for item in inside), "sender": sender,
                                    "direction": direction, "complete": y > 1 and y + height < rgb.shape[0] - 1
                                    and all(item[3] >= 0.85 for item in inside), "source": "local_ocr"}))
        return [item for _, item in sorted(bubbles, key=lambda entry: entry[0])]

    def send_reply(self, chat, text):
        self.send_phase = 'preparing'
        if not text or len(text) > 500 or any(c in text for c in "\r\n\t"):
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
        self.inputs.type_text(text, pause=0.02)
        time.sleep(0.4)
        image, fresh = self.calibrated_frame()
        normalize = lambda value: "".join(value.split())
        if normalize(self.recognize(image.crop(editor))) != normalize(text):
            raise RuntimeError("Editor OCR does not match intended reply; stopped before send")
        if not self.chat_is_open(chat):
            raise RuntimeError("Chat changed before send")
        _, last_rect = self.calibrated_frame()
        if last_rect != fresh:
            raise RuntimeError("Window moved before send")
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
        visible = "".join(value for box, value, score in result or []
                          if score >= 0.85 and min(p[0] for p in box) > 470
                          and 64 < min(p[1] for p in box) < 900)
        if normalize(text) not in normalize(visible):
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
            raise RuntimeError("Cannot verify focused search field; no text entered")
        focus.set_edit_text(target.chat)
        self.guard()

    def open_exact_result(self, chat):
        rect, items = self.regions()
        matches = [(x, y) for text, x, y in items if text == chat
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
