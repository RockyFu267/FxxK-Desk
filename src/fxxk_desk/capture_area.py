"""Monitor-independent capture rectangle geometry and local settings."""
from dataclasses import dataclass, asdict
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self):
        if any(type(v) is not int for v in self.tuple()) or self.width <= 0 or self.height <= 0:
            raise ValueError('Invalid physical pixel rectangle')

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def contains(self, other):
        return self.left <= other.left and self.top <= other.top and self.right >= other.right and self.bottom >= other.bottom

    def tuple(self):
        return self.left, self.top, self.right, self.bottom


@dataclass(frozen=True)
class Monitor:
    device: str
    bounds: Rect
    primary: bool = False


@dataclass(frozen=True)
class Limits:
    min_width: int = 640
    min_height: int = 480
    max_pixels: int = 16_777_216

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.min_width, self.min_height, self.max_pixels)):
            raise ValueError('Region limits must be positive integers')
        if self.min_width * self.min_height > self.max_pixels:
            raise ValueError('Minimum region exceeds pixel budget')

    def size(self, width, height, monitor):
        bounds = monitor.bounds
        if bounds.width < self.min_width or bounds.height < self.min_height:
            raise ValueError('Monitor smaller than the minimum capture region')
        width = min(bounds.width, max(self.min_width, int(width)))
        height = min(bounds.height, max(self.min_height, int(height)))
        if width * height > self.max_pixels:
            factor = math.sqrt(self.max_pixels / (width * height))
            width = max(self.min_width, int(width * factor))
            height = max(self.min_height, int(height * factor))
            if width * height > self.max_pixels:
                width = min(width, self.max_pixels // self.min_height)
                height = min(height, self.max_pixels // width)
        return width, height


def monitor_at(monitors, x, y):
    if not monitors:
        raise ValueError('No monitors available')
    def distance(monitor):
        r = monitor.bounds
        dx = max(r.left - x, 0, x - r.right + 1)
        dy = max(r.top - y, 0, y - r.bottom + 1)
        return dx * dx + dy * dy
    return min(monitors, key=distance)


def fit_rect(rect, monitor, limits):
    width, height = limits.size(rect.width, rect.height, monitor)
    bounds = monitor.bounds
    left = min(max(rect.left, bounds.left), bounds.right - width)
    top = min(max(rect.top, bounds.top), bounds.bottom - height)
    return Rect(left, top, left + width, top + height)


def resize_rect(rect, edge, dx, dy, monitor, limits):
    if edge not in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
        raise ValueError('Invalid resize handle')
    left = max(monitor.bounds.left, rect.left + dx) if 'w' in edge else rect.left
    right = min(monitor.bounds.right, rect.right + dx) if 'e' in edge else rect.right
    top = max(monitor.bounds.top, rect.top + dy) if 'n' in edge else rect.top
    bottom = min(monitor.bounds.bottom, rect.bottom + dy) if 's' in edge else rect.bottom
    width, height = limits.size(max(1, right - left), max(1, bottom - top), monitor)
    left = rect.right - width if 'w' in edge else rect.left
    top = rect.bottom - height if 'n' in edge else rect.top
    return fit_rect(Rect(left, top, left + width, top + height), monitor, limits)


def load_region(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if value.get('coordinate_space') != 'physical_screen_pixels':
        raise ValueError('Unsupported capture coordinate space')
    rect = Rect(*(value[name] for name in ('left', 'top', 'right', 'bottom')))
    if value.get('width', rect.width) != rect.width or value.get('height', rect.height) != rect.height:
        raise ValueError('Region dimensions disagree with bounds')
    return rect


def save_region(path, rect, monitor, limits):
    if fit_rect(rect, monitor, limits) != rect:
        raise ValueError('Region exceeds screen or configured limits')
    value = {'version': 2, 'coordinate_space': 'physical_screen_pixels', **asdict(rect),
             'width': rect.width, 'height': rect.height, 'monitor': asdict(monitor),
             'limits': asdict(limits), 'layout_calibration_required': True}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return value


def list_monitors():
    import ctypes
    from ctypes import wintypes
    from .windows import Desktop
    user = Desktop().u
    class Info(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('monitor', wintypes.RECT), ('work', wintypes.RECT),
                    ('flags', wintypes.DWORD), ('device', wintypes.WCHAR * 32)]
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    found = []
    @callback_type
    def visit(handle, hdc, bounds, data):
        info = Info()
        info.cbSize = ctypes.sizeof(info)
        if user.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.monitor
            found.append(Monitor(info.device, Rect(r.left, r.top, r.right, r.bottom), bool(info.flags & 1)))
        return True
    user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    if not user.EnumDisplayMonitors(None, None, visit, 0) or not found:
        raise RuntimeError('Cannot enumerate current desktop monitors')
    return sorted(found, key=lambda m: (m.bounds.left, m.bounds.top))


def capture_region(region_file, output):
    from PIL import ImageGrab
    monitors = list_monitors()
    rect = load_region(region_file)
    if rect.width * rect.height > Limits().max_pixels:
        raise ValueError('Saved region exceeds capture pixel budget')
    if not any(m.bounds.contains(rect) for m in monitors):
        raise ValueError('Saved region no longer fits a current monitor; reopen the region editor')
    image = ImageGrab.grab(bbox=rect.tuple(), all_screens=True)
    if image.size != (rect.width, rect.height):
        raise RuntimeError('Captured dimensions differ from region settings')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format='PNG')
    return {'path': str(output), 'screen_bounds': rect.tuple(), 'size': image.size,
            'coordinate_space': 'physical_screen_pixels'}
