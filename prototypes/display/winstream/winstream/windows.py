"""Win32 helpers: DPI awareness, window enumeration per process, DWM frame bounds, launch, focus.

Everything here is physical pixels (the process opts into per-monitor-v2 DPI awareness first).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import time
from dataclasses import dataclass, field

import win32con
import win32gui
import win32process

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


def set_dpi_aware() -> None:
    """Per-monitor v2 so every rect we read is in physical pixels (150% scaling on this box)."""
    # DPI_AWARENESS_CONTEXT is a pseudo-HANDLE (-4): pass it as a pointer-sized value or the call
    # fails silently and every GetWindowRect comes back DPI-virtualised (logical px).
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    ok = user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    if not ok:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


def frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """DWMWA_EXTENDED_FRAME_BOUNDS: the visible frame (no invisible resize borders) in physical px.
    This is what Windows.Graphics.Capture delivers for a window, so it is the coordinate origin of
    every frame we stream."""
    r = wt.RECT()
    hr = dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        return win32gui.GetWindowRect(hwnd)
    return r.left, r.top, r.right, r.bottom


def is_cloaked(hwnd: int) -> bool:
    v = wt.DWORD()
    hr = dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return hr == 0 and v.value != 0


@dataclass
class WinInfo:
    hwnd: int
    title: str
    cls: str
    rect: tuple[int, int, int, int]  # frame bounds, physical px
    owner: int
    z: int  # 0 = topmost in EnumWindows order
    exstyle: int
    style: int

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def as_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "cls": self.cls,
            "rect": list(self.rect),
            "owner": self.owner,
            "z": self.z,
            "menu": self.cls == "#32768",
            "tooltip": self.cls.startswith("tooltips_class"),
        }


def windows_of_pid(pid: int) -> list[WinInfo]:
    """Visible, non-cloaked, non-empty top-level windows of `pid` in z-order (top first).

    Includes owned dialogs, popup menus (class #32768), combo drop-downs and tooltips because
    they are separate top-level HWNDs that a per-window WGC capture of the frame does NOT contain.
    """
    out: list[WinInfo] = []

    def cb(h: int, _: object) -> bool:
        _tid, p = win32process.GetWindowThreadProcessId(h)
        if p != pid or not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
            return True
        if is_cloaked(h):
            return True
        rect = frame_bounds(h)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return True
        out.append(
            WinInfo(
                hwnd=h,
                title=win32gui.GetWindowText(h),
                cls=win32gui.GetClassName(h),
                rect=rect,
                owner=win32gui.GetWindow(h, win32con.GW_OWNER),
                z=len(out),
                exstyle=win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE),
                style=win32gui.GetWindowLong(h, win32con.GWL_STYLE),
            )
        )
        return True

    win32gui.EnumWindows(cb, None)
    return out


def launch_kicad(env_json: str, document: str, exe_key: str = "pcbnew") -> subprocess.Popen:
    """Start a KiCad GUI with the sandboxed env produced by kicad_env.py (never the user's config)."""
    with open(env_json, encoding="utf-8") as fh:
        spec = json.load(fh)
    exe = spec[exe_key]
    return subprocess.Popen([exe, document], env=spec["env"], cwd=os.path.dirname(document))


def wait_main_window(pid: int, title_substr: str, timeout_s: float = 90) -> WinInfo:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        for w in windows_of_pid(pid):
            if title_substr in w.title:
                return w
        time.sleep(0.2)
    raise TimeoutError(f"no window with {title_substr!r} for pid {pid}")


def resize_window(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """Un-maximise and place the window; (x, y, w, h) are physical pixels of the frame bounds."""
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    time.sleep(0.5)
    # SetWindowPos wants the full window rect (with invisible borders). Place, measure the delta
    # between the frame bounds and the window rect (only reliable once restored), then correct.
    for _ in range(2):
        win32gui.SetWindowPos(hwnd, 0, x, y, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        time.sleep(0.2)
        fl, ft, fr, fb = frame_bounds(hwnd)
        wl, wt_, wr, wb = win32gui.GetWindowRect(hwnd)
        dx, dy = fl - wl, ft - wt_
        dw, dh = (wr - wl) - (fr - fl), (wb - wt_) - (fb - ft)
        win32gui.SetWindowPos(hwnd, 0, x - dx, y - dy, w + dw, h + dh, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        time.sleep(0.2)
        if frame_bounds(hwnd) == (x, y, x + w, y + h):
            break


def foreground_pid() -> int:
    h = win32gui.GetForegroundWindow()
    if not h:
        return 0
    return win32process.GetWindowThreadProcessId(h)[1]


def bring_to_front(hwnd: int) -> bool:
    """SetForegroundWindow is refused for background processes. Order of attempts:
    1. plain call (works when our process last sent input); 2. AttachThreadInput to the current
    foreground thread (shares its input state, then the call is allowed); 3. the synthetic ALT tap.
    The ALT tap is last because a bare ALT press/release puts a wx frame's MENU BAR into keyboard
    focus mode and swallows the next click (observed: right-click context menu did not open)."""
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    with_alt = False
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass
    if win32gui.GetForegroundWindow() != hwnd:
        fg = win32gui.GetForegroundWindow()
        fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        my_tid = kernel32.GetCurrentThreadId()
        if fg_tid and fg_tid != my_tid:
            user32.AttachThreadInput(my_tid, fg_tid, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if fg_tid and fg_tid != my_tid:
                user32.AttachThreadInput(my_tid, fg_tid, False)
    if win32gui.GetForegroundWindow() != hwnd:
        from .inject import press_alt_tap  # local import: inject depends on this module too

        with_alt = True
        press_alt_tap()
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.03)
    ok = win32gui.GetForegroundWindow() == hwnd
    if with_alt and ok:
        from .inject import press_key_tap

        press_key_tap(0x1B)  # leave the menu-bar focus mode the ALT tap put wx into
    return ok


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the virtual desktop in physical pixels (SM_*VIRTUALSCREEN)."""
    return (
        user32.GetSystemMetrics(76),
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),
        user32.GetSystemMetrics(79),
    )


def dpi_for_window(hwnd: int) -> int:
    return int(user32.GetDpiForWindow(wt.HWND(hwnd)))


def focused_hwnd_of_thread(hwnd: int) -> int:
    """Focus window of the thread owning `hwnd`. GetGUIThreadInfo reports hwndFocus only while
    that thread is the active one; for a background KiCad we attach our input queue to its thread
    and call GetFocus (classic technique), then fall back to the process's topmost enabled window
    (the modal dialog, if any) so posted keys at least reach the right top-level window."""
    tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    my_tid = kernel32.GetCurrentThreadId()
    if tid != my_tid and user32.AttachThreadInput(my_tid, tid, True):
        try:
            h = user32.GetFocus()
        finally:
            user32.AttachThreadInput(my_tid, tid, False)
        if h:
            return int(h)

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("flags", wt.DWORD),
            ("hwndActive", wt.HWND),
            ("hwndFocus", wt.HWND),
            ("hwndCapture", wt.HWND),
            ("hwndMenuOwner", wt.HWND),
            ("hwndMoveSize", wt.HWND),
            ("hwndCaret", wt.HWND),
            ("rcCaret", wt.RECT),
        ]

    tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
        return int(gti.hwndFocus)
    for w in windows_of_pid(pid):  # z-order, top first: a modal dialog precedes the frame
        if user32.IsWindowEnabled(w.hwnd):
            return w.hwnd
    return 0
