"""Win32 helpers: DPI awareness, window enumeration per process, DWM frame bounds, launch, focus.

Everything here is physical pixels (the process opts into per-monitor-v2 DPI awareness first).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

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
    surface: str = ""  # set by group_surfaces(): pcb / sch / 3d / mgr / ...
    root: int = 0  # surface root HWND (the editor frame)

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
            "surface": self.surface,
            "root": self.root,
            "enabled": bool(user32.IsWindowEnabled(self.hwnd)),
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
    """Start a KiCad GUI with the sandboxed env produced by kicad_env.py (never the user's config).
    exe_key: pcbnew | eeschema (standalone) | kicad (project manager, document = .kicad_pro)."""
    with open(env_json, encoding="utf-8-sig") as fh:
        spec = json.load(fh)
    exe = spec.get(exe_key) or os.path.join(spec["root"], "bin", exe_key + ".exe")
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


# ------------------------------------------------------------------ SetWinEventHook (no polling)
EVENT_NAMES = {
    0x0003: "foreground", 0x0016: "minimizestart", 0x0017: "minimizeend",
    0x8000: "create", 0x8001: "destroy", 0x8002: "show", 0x8003: "hide", 0x800B: "locationchange",
    0x0006: "menupopupstart", 0x0007: "menupopupend", 0x000A: "movesizestart", 0x000B: "movesizeend",
}
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
_WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)


class WinEventHook:
    """Accessibility event hook for one process: window create/destroy/show/hide/move, foreground
    changes, minimise, menu popups. Replaces the 100 ms EnumWindows poll of the first prototype:
    the callback fires on the hook thread within ~1 ms of the event, and the tracker rescans then.
    Only top-level objects (idObject == OBJID_WINDOW, idChild == 0) are passed on."""

    def __init__(self, pid: int, on_event: Callable[[str, int], None]) -> None:
        self.pid = pid
        self.on_event = on_event
        self.count = 0
        self.log: deque = deque(maxlen=60)
        self._hooks: list[int] = []
        self._proc = _WinEventProc(self._cb)
        self._thread = threading.Thread(target=self._run, name="winevent", daemon=True)
        self._tid = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT

    def _cb(self, hook, event, hwnd, id_object, id_child, tid, t_ms) -> None:
        if id_object != 0 or id_child != 0 or not hwnd:  # OBJID_WINDOW only
            return
        name = EVENT_NAMES.get(event, hex(event))
        self.count += 1
        self.log.append((round(time.time() % 1000, 3), name, int(hwnd)))
        try:
            self.on_event(name, int(hwnd))
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        user32.SetWinEventHook.restype = wt.HANDLE
        ranges = [(0x0003, 0x0003), (0x0006, 0x0007), (0x000A, 0x000B), (0x0016, 0x0017), (0x8000, 0x8003), (0x800B, 0x800B)]
        for lo, hi in ranges:
            # EVENT_SYSTEM_FOREGROUND must be hooked for every process (pid 0): the event that matters
            # is another process taking the foreground away from KiCad.
            pid = 0 if lo == 0x0003 else self.pid
            h = user32.SetWinEventHook(lo, hi, None, self._proc, pid, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            if h:
                self._hooks.append(h)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for h in self._hooks:
            user32.UnhookWinEvent(h)


# ------------------------------------------------------------------ surfaces (one per editor frame)
#: title substring -> surface kind. A KiCad project session (kicad.exe) is ONE process and ONE GUI
#: thread whose editor frames are unowned siblings (class wxWindowNR); dialogs and the footprint
#: chooser are owned by their editor frame (GW_OWNER); the 3D viewer is another unowned frame.
SURFACE_KINDS = (
    ("PCB Editor", "pcb"), ("Schematic Editor", "sch"), ("3D Viewer", "3d"), ("Footprint Editor", "fp"),
    ("Symbol Editor", "sym"), ("Gerber Viewer", "gerber"), ("KiCad", "mgr"),
)


def surface_kind(title: str) -> str:
    for sub, kind in SURFACE_KINDS:
        if sub in title:
            return kind
    return ""


def group_surfaces(wins: list[WinInfo]) -> dict[int, dict]:
    """Assign every window to a surface root: the top of its GW_OWNER chain inside the process.
    Unowned popups (menus, tooltips, combo lists: class-based) go to the root whose rect contains
    their centre, else to the first (topmost) root. Returns {root_hwnd: {kind, root, hwnds[]}}
    and stamps `w.surface` / `w.root` on each WinInfo."""
    by_hwnd = {w.hwnd: w for w in wins}
    roots: dict[int, dict] = {}
    kinds_used: dict[str, int] = {}
    for w in wins:  # z-order: top first
        if w.owner == 0 and not w.cls.startswith(("#32768", "tooltips_class", "ComboLBox", "wxTipWindow")) and w.title:
            kind = surface_kind(w.title) or "win"
            n = kinds_used.get(kind, 0)
            kinds_used[kind] = n + 1
            roots[w.hwnd] = {"kind": kind if n == 0 else f"{kind}{n + 1}", "root": w.hwnd, "hwnds": [w.hwnd], "title": w.title}
    for w in wins:
        if w.hwnd in roots:
            continue
        h = w.hwnd
        seen = 0
        while h and h not in roots and seen < 16:
            h = by_hwnd[h].owner if h in by_hwnd else 0
            seen += 1
        if not h:
            cx, cy = (w.rect[0] + w.rect[2]) // 2, (w.rect[1] + w.rect[3]) // 2
            for r in roots:
                rr = by_hwnd[r].rect
                if rr[0] <= cx < rr[2] and rr[1] <= cy < rr[3]:
                    h = r
                    break
        # an unowned popup outside every root (e.g. a stray tooltip window parked on another
        # monitor) belongs to no surface: it is neither streamed nor allowed to grow a stage
        if h:
            roots[h]["hwnds"].append(w.hwnd)
    for r, info in roots.items():
        for h in info["hwnds"]:
            by_hwnd[h].surface = info["kind"]
            by_hwnd[h].root = r
    return roots
