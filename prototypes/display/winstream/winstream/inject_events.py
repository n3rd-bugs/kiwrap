"""Message-only input for a KiCad built with the `KICAD_INPUT_FROM_EVENTS` patch (fork branch
`mr/display-input`): every pointer and key event becomes a WM_* message posted to the right HWND
of the KiCad process, nothing else. No SendInput, no SetForegroundWindow, no SetFocus, no
AttachThreadInput: the user's cursor, focus and keyboard are never touched, and the KiCad window
may be off-screen, covered or on another virtual desktop.

Stock KiCad cannot be driven this way (its canvas reads the OS cursor, wx reads the hardware key
state and wxEVT_CHAR_HOOK comes from a keyboard hook that posted messages never trigger); see
docs/research/display/win-input.md.

Targeting rules (mirroring what the OS does for real input):
  mouse   -> the deepest visible child under the point among the process's own top-level windows,
             z-order first (a popup menu / dialog above the frame wins); disabled top-levels (the
             frame behind a modal dialog) are skipped; coordinates in that HWND's client space.
  wheel   -> same HWND as the mouse, lParam in screen coordinates (WM_MOUSEWHEEL convention).
  keys    -> the focus window of the KiCad UI thread (GetGUIThreadInfo), falling back to the
             top-most enabled top-level window of the process.
  modifiers -> ordinary WM_KEYDOWN/WM_KEYUP of VK_SHIFT/VK_CONTROL/VK_MENU; the patched KiCad
             mirrors them into its thread key state so wx sees ControlDown()/ShiftDown().
  double-click -> a second button-down within GetDoubleClickTime() and the system double-click
             rectangle becomes WM_xBUTTONDBLCLK (the OS synthesises it only for real input).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import win32con
import win32gui
import win32process

from . import windows as W
from .inject import vk_for_code

user32 = ctypes.windll.user32
user32.ChildWindowFromPointEx.argtypes = [wt.HWND, wt.POINT, wt.UINT]
user32.ChildWindowFromPointEx.restype = wt.HWND
user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.c_void_p]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL

WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_MOUSELEAVE = 0x02A3
CWP_SKIPINVISIBLE, CWP_SKIPDISABLED, CWP_SKIPTRANSPARENT = 0x0001, 0x0002, 0x0004
MK_XBUTTON1, MK_XBUTTON2 = 0x0020, 0x0040

_BTN_WM = {
    0: (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK, win32con.MK_LBUTTON),
    1: (win32con.WM_MBUTTONDOWN, win32con.WM_MBUTTONUP, win32con.WM_MBUTTONDBLCLK, win32con.MK_MBUTTON),
    2: (win32con.WM_RBUTTONDOWN, win32con.WM_RBUTTONUP, win32con.WM_RBUTTONDBLCLK, win32con.MK_RBUTTON),
}
_MOD_VK = {0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11, 0xA4: 0x12, 0xA5: 0x12}  # L/R -> generic VK
_MOD_MK = {0x10: win32con.MK_SHIFT, 0x11: win32con.MK_CONTROL}


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT),
    ]


def thread_focus(tid: int) -> tuple[int, int, int]:
    """(hwndFocus, hwndActive, hwndCapture) of a UI thread, without attaching to it."""
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return 0, 0, 0
    return int(gti.hwndFocus or 0), int(gti.hwndActive or 0), int(gti.hwndCapture or 0)


def _lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _post(hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
    return bool(user32.PostMessageW(hwnd, msg, wparam & 0xFFFFFFFFFFFFFFFF, lparam & 0xFFFFFFFF))


class EventInjector:
    """Posted WM_* messages only; needs the KICAD_INPUT_FROM_EVENTS build of KiCad."""

    name = "events"

    def __init__(self, main_hwnd: int, pid: int) -> None:
        self.main_hwnd = main_hwnd
        self.pid = pid
        self.tid = win32process.GetWindowThreadProcessId(main_hwnd)[0]
        self._buttons = 0
        self._mods = 0            # MK_SHIFT | MK_CONTROL for mouse wParams
        self._alt = False
        self._last_down: dict[int, tuple[float, int, int]] = {}
        self._hover = 0  # HWND that last received a mouse message (gets WM_MOUSELEAVE on hand-over)
        self._dbl_ms = user32.GetDoubleClickTime()
        self._dbl_w = user32.GetSystemMetrics(36) // 2  # SM_CXDOUBLECLK
        self._dbl_h = user32.GetSystemMetrics(37) // 2  # SM_CYDOUBLECLK
        self._wins_cache: tuple[float, list[W.WinInfo]] = (0.0, [])
        self.foreground_fixes = 0
        self.obscured_fixes = 0
        self.surface_hwnds: set[int] = {main_hwnd}  # server bookkeeping shared with the other injectors
        self.posted = 0
        self.misses = 0
        self.log: list[tuple] = []

    # ---------------------------------------------------------------- targeting
    def _windows(self) -> list[W.WinInfo]:
        now = time.time()
        if now - self._wins_cache[0] > 0.04:
            self._wins_cache = (now, W.windows_of_pid(self.pid))
        return self._wins_cache[1]

    def _target(self, sx: int, sy: int) -> tuple[int, int, int]:
        """Deepest visible child of the process under the screen point, or (0, 0, 0)."""
        for w in self._windows():  # z-order, top first
            l, t, r, b = win32gui.GetWindowRect(w.hwnd)
            if not (l <= sx < r and t <= sy < b):
                continue
            if not user32.IsWindowEnabled(w.hwnd):
                return 0, 0, 0  # a disabled top-level window would not get real input either
            h = w.hwnd
            for _ in range(32):
                cx, cy = win32gui.ScreenToClient(h, (sx, sy))
                c = user32.ChildWindowFromPointEx(h, wt.POINT(cx, cy), CWP_SKIPINVISIBLE | CWP_SKIPDISABLED)
                if not c or c == h:
                    break
                h = int(c)
            cx, cy = win32gui.ScreenToClient(h, (sx, sy))
            return h, cx, cy
        return 0, 0, 0

    def _key_target(self) -> int:
        focus, _active, _capture = thread_focus(self.tid)
        if focus and win32gui.IsWindow(focus):
            return focus
        for w in self._windows():
            if w.cls != "#32768" and user32.IsWindowEnabled(w.hwnd):
                return w.hwnd
        return self.main_hwnd

    def ensure_foreground(self) -> None:
        return None  # never: that is the point of this injector

    # ---------------------------------------------------------------- mouse
    def _hand_over(self, h: int) -> None:
        """The OS only raises WM_MOUSELEAVE for the real cursor; tell the previous window that the
        (virtual) pointer left it so hover highlights and tooltips end like they would for real."""
        if self._hover and self._hover != h and win32gui.IsWindow(self._hover):
            _post(self._hover, WM_MOUSELEAVE, 0, 0)
        self._hover = h

    def move(self, sx: int, sy: int) -> None:
        h, cx, cy = self._target(sx, sy)
        if not h:
            self.misses += 1
            return
        self._hand_over(h)
        _post(h, win32con.WM_MOUSEMOVE, self._buttons | self._mods, _lparam(cx, cy))
        self.posted += 1

    def button(self, sx: int, sy: int, button: int, down: bool) -> None:
        h, cx, cy = self._target(sx, sy)
        if not h:
            self.misses += 1
            return
        msg_down, msg_up, msg_dbl, mk = _BTN_WM[button]
        now = time.time()
        self._hand_over(h)
        if down:
            msg = msg_down
            last = self._last_down.get(button)
            if last and (now - last[0]) * 1000 <= self._dbl_ms and abs(last[1] - sx) <= self._dbl_w \
                    and abs(last[2] - sy) <= self._dbl_h:
                msg = msg_dbl
                self._last_down.pop(button, None)
            else:
                self._last_down[button] = (now, sx, sy)
            self._buttons |= mk
            # A real click is preceded by a move to the same spot; make the canvas see it too.
            _post(h, win32con.WM_MOUSEMOVE, self._buttons | self._mods, _lparam(cx, cy))
        else:
            msg = msg_up
            self._buttons &= ~mk
        _post(h, msg, self._buttons | self._mods, _lparam(cx, cy))
        self.posted += 1
        self.log.append((round(now % 1000, 3), "btn", button, down, h, cx, cy, msg))

    def wheel(self, sx: int, sy: int, dx: int, dy: int) -> None:
        h, _cx, _cy = self._target(sx, sy)
        if not h:
            self.misses += 1
            return
        self._hand_over(h)
        if dy:
            _post(h, WM_MOUSEWHEEL, ((dy & 0xFFFF) << 16) | (self._buttons | self._mods), _lparam(sx, sy))
        if dx:
            _post(h, WM_MOUSEHWHEEL, ((dx & 0xFFFF) << 16) | (self._buttons | self._mods), _lparam(sx, sy))
        self.posted += 1

    # ---------------------------------------------------------------- keyboard
    def key(self, code: str, down: bool) -> bool:
        m = vk_for_code(code)
        if m is None:
            return False
        vk, ext = m
        h = self._key_target()
        scan = user32.MapVirtualKeyW(vk, 0)
        lp = 1 | (scan << 16) | ((1 << 24) if ext else 0)
        is_mod = vk in _MOD_VK
        generic = _MOD_VK.get(vk, vk)
        if generic == 0x12:  # Alt: WM_SYSKEY* with the context bit (KF_ALTDOWN)
            self._alt = down
            msg = WM_SYSKEYDOWN if down else win32con.WM_KEYUP
            lp |= (1 << 29) if down else 0
        else:
            msg = win32con.WM_KEYDOWN if down else win32con.WM_KEYUP
            if self._alt:
                msg = WM_SYSKEYDOWN if down else WM_SYSKEYUP
                lp |= 1 << 29
        if not down:
            lp |= (1 << 30) | (1 << 31)
        if is_mod and generic in _MOD_MK:
            self._mods = (self._mods | _MOD_MK[generic]) if down else (self._mods & ~_MOD_MK[generic])
        # Post the generic VK (VK_SHIFT, not VK_LSHIFT): wx and the patched KiCad key on it.
        _post(h, msg, generic, lp)
        self.posted += 1
        self.log.append((round(time.time() % 1000, 3), "key", code, down, h))
        return True

    def stats(self) -> dict:
        focus, active, capture = thread_focus(self.tid)
        return {"posted": self.posted, "misses": self.misses, "buttons": self._buttons, "mods": self._mods,
                "thread_focus": focus, "thread_active": active, "thread_capture": capture,
                "log": self.log[-30:]}
