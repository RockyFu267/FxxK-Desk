"""Display a local alignment frame using an existing window's physical bounds."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path

from .windows import Desktop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.toml")
    parser.add_argument("--target", default="ops")
    parser.add_argument("--output", default="artifacts/capture-region.local.json")
    args = parser.parse_args()
    from .targets import load_target
    target = load_target(args.config, args.target)
    desktop = Desktop()
    window = desktop.select(target.window_title)
    if desktop.u.IsIconic(window.id):
        raise RuntimeError("Restore the target window before defining a region")
    rect = wintypes.RECT()
    if not desktop.u.GetWindowRect(window.id, ctypes.byref(rect)):
        raise RuntimeError("Cannot read target window bounds")
    # DWM visible bounds exclude invisible resize margins where available.
    dwm = ctypes.WinDLL("dwmapi")
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    visible = wintypes.RECT()
    if dwm.DwmGetWindowAttribute(window.id, 9, ctypes.byref(visible), ctypes.sizeof(visible)) == 0:
        rect = visible
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("Invalid target bounds")
    data = {"coordinate_space": "physical_screen_pixels", "left": left, "top": top,
            "right": right, "bottom": bottom, "width": width, "height": height}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps(data), flush=True)

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    color = "#00d8a0"

    def panel(x, y, w, h):
        pane = tk.Toplevel(root)
        pane.overrideredirect(True)
        pane.attributes("-topmost", True)
        pane.configure(bg=color)
        pane.geometry(f"{w}x{h}{x:+d}{y:+d}")
        # Tk treats negative geometry offsets as right/bottom anchors. Use Win32
        # to place all overlays in physical virtual-desktop coordinates.
        pane.update_idletasks()
        user = desktop.u
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        hwnd = user.GetParent(pane.winfo_id()) or pane.winfo_id()
        user.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, w, h, 0x0010)
        pane.bind("<Escape>", lambda _: root.destroy())
        return pane

    # Border lies outside the saved capture rectangle; interior stays usable.
    panel(left - 3, top - 3, width + 6, 3)
    panel(left - 3, bottom, width + 6, 3)
    panel(left - 3, top, 3, height)
    panel(right, top, 3, height)
    bar = panel(left, max(0, top - 32), min(width, 840), 28)
    tk.Label(bar, text=f"固定截取范围  ({left}, {top}) → ({right}, {bottom})  |  {width} × {height} px",
             bg=color, fg="#102020", font=("Microsoft YaHei UI", 10)).pack(side="left", padx=8)
    tk.Button(bar, text="关闭定位框", command=root.destroy).pack(side="right")
    root.after(600_000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
