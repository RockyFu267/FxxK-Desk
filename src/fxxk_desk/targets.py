"""Configuration and a bounded, verified chat-navigation workflow."""
from dataclasses import dataclass
from pathlib import Path
import re
import time
import tomllib


@dataclass(frozen=True)
class Target:
    executable: str
    window_title: str
    chat: str
    search_hotkey: str = "^f"
    timeout_seconds: float = 20
    layout_width: int = 1300
    layout_height: int = 1160
    message_rect: tuple = (400, 64, 1130, 900)
    editor_rect: tuple = (418, 940, 1120, 1100)
    send_point: tuple = (1090, 1123)
    auto_reply: bool = False
    backfill_enabled: bool = True
    backfill_max_pages: int = 30
    backfill_max_seconds: float = 45
    anchor_messages: int = 3
    scroll_notches: int = 3
    scroll_mode: str = "batch"
    scroll_batch_events: int = 2
    human_idle_seconds: float = 5
    resume_requires_target_foreground: bool = True


def load_target(path, name):
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    section = data.get("targets", {}).get(name)
    if not isinstance(section, dict):
        raise ValueError(f"Unknown target: {name}")
    if section.get("adapter") != "desktop_chat":
        raise ValueError("Unsupported target adapter")
    allowed = set(Target.__dataclass_fields__) | {"adapter"}
    if set(section) - allowed:
        raise ValueError("Unknown target configuration fields")
    if type(section.get("auto_reply", False)) is not bool:
        raise ValueError("auto_reply must be a boolean")
    if type(section.get("backfill_enabled", True)) is not bool:
        raise ValueError("backfill_enabled must be a boolean")
    if section.get("scroll_mode", "batch") not in ("batch", "stepwise"):
        raise ValueError("Invalid scroll_mode")
    if type(section.get("resume_requires_target_foreground", True)) is not bool:
        raise ValueError("Invalid resume_requires_target_foreground")
    idle = section.get("human_idle_seconds", 5)
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not 1 <= idle <= 300:
        raise ValueError("human_idle_seconds must be between 1 and 300")
    for key, default, low, high in (("backfill_max_pages", 30, 4, 200), ("anchor_messages", 3, 2, 10), ("scroll_notches", 3, 1, 6), ("scroll_batch_events", 2, 1, 12)):
        value = section.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid {key}")
    seconds = section.get("backfill_max_seconds", 45)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 2 <= seconds <= 120:
        raise ValueError("Invalid backfill_max_seconds")
    for key in ("executable", "window_title", "chat"):
        if not isinstance(section.get(key), str) or not section[key].strip():
            raise ValueError(f"Target requires nonempty {key}")
    # Only this tested navigation key is allowed; configuration is not a script.
    if section.get("search_hotkey", "^f") != "^f":
        raise ValueError("Only Ctrl+F search navigation is supported")
    timeout = section.get("timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise ValueError("timeout_seconds must be between 1 and 60")
    width, height = section.get("layout_width", 1300), section.get("layout_height", 1160)
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        raise ValueError("Layout dimensions must be positive integers")
    for key, fallback in (("message_rect", Target.message_rect), ("editor_rect", Target.editor_rect), ("send_point", Target.send_point)):
        values = section.get(key, fallback)
        if not isinstance(values, (tuple, list)) or len(values) != len(fallback) or any(type(v) is not int for v in values):
            raise ValueError(f"Invalid {key}")
        if any(not 0 <= value < (width if index % 2 == 0 else height) for index, value in enumerate(values)):
            raise ValueError(f"Out of bounds {key}")
        if len(values) == 4 and (values[0] >= values[2] or values[1] >= values[3]):
            raise ValueError(f"Invalid rectangle {key}")
    return Target(**{k: v for k, v in section.items() if k != "adapter"})


def matches_chat_header(text, chat):
    return re.fullmatch(re.escape(chat) + r"(?:\s*[（(]\d+[）)])?", text.strip()) is not None


def open_target(target, backend):
    backend.ensure_app(target)
    if backend.chat_is_open(target.chat):
        return {"status": "chat_verified", "chat": target.chat, "already_open": True}
    selected = backend.open_exact_result(target.chat)
    if not selected:
        backend.search(target)
    deadline = time.monotonic() + target.timeout_seconds
    while time.monotonic() < deadline:
        if selected or backend.open_exact_result(target.chat):
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("No unique exact chat result; navigation stopped")
    deadline = time.monotonic() + target.timeout_seconds
    while time.monotonic() < deadline:
        if backend.chat_is_open(target.chat):
            return {"status": "chat_verified", "chat": target.chat, "already_open": False}
        time.sleep(0.3)
    raise RuntimeError("Chat header could not be verified; no further action performed")
