"""Input injection: SendInput (real OS input, needs the window on-screen + foreground for keys)
and PostMessage (WM_* straight to the HWND under the point; no focus/cursor side effects).

Browser events arrive as JSON: {t: "move"|"down"|"up"|"wheel"|"key", x, y (frame px of the main
window), button: 0|1|2, dx, dy (wheel), code (KeyboardEvent.code), key, down: bool,
ctrl/shift/alt: bool}. The server converts frame px -> screen px before calling here.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import win32con
import win32gui
import win32process

from . import windows as W

user32 = ctypes.windll.user32

# --- SendInput structures -------------------------------------------------------------------
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E

_BTN_FLAGS = {
    0: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    1: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}
_BTN_WM = {
    0: (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP, win32con.MK_LBUTTON),
    1: (win32con.WM_MBUTTONDOWN, win32con.WM_MBUTTONUP, win32con.MK_MBUTTON),
    2: (win32con.WM_RBUTTONDOWN, win32con.WM_RBUTTONUP, win32con.MK_RBUTTON),
}

# KeyboardEvent.code -> (VK, extended). Letters/digits map 1:1 to ASCII VKs.
_CODE_VK: dict[str, tuple[int, bool]] = {
    "Escape": (0x1B, False), "Tab": (0x09, False), "Enter": (0x0D, False), "NumpadEnter": (0x0D, True),
    "Backspace": (0x08, False), "Space": (0x20, False), "Delete": (0x2E, True), "Insert": (0x2D, True),
    "Home": (0x24, True), "End": (0x23, True), "PageUp": (0x21, True), "PageDown": (0x22, True),
    "ArrowLeft": (0x25, True), "ArrowUp": (0x26, True), "ArrowRight": (0x27, True), "ArrowDown": (0x28, True),
    "ShiftLeft": (0xA0, False), "ShiftRight": (0xA1, True), "ControlLeft": (0xA2, False),
    "ControlRight": (0xA3, True), "AltLeft": (0xA4, False), "AltRight": (0xA5, True),
    "CapsLock": (0x14, False), "Minus": (0xBD, False), "Equal": (0xBB, False), "BracketLeft": (0xDB, False),
    "BracketRight": (0xDD, False), "Backslash": (0xDC, False), "Semicolon": (0xBA, False),
    "Quote": (0xDE, False), "Comma": (0xBC, False), "Period": (0xBE, False), "Slash": (0xBF, False),
    "Backquote": (0xC0, False), "NumpadAdd": (0x6B, False), "NumpadSubtract": (0x6D, False),
    "NumpadMultiply": (0x6A, False), "NumpadDivide": (0x6F, True), "NumpadDecimal": (0x6E, False),
    "PrintScreen": (0x2C, True), "ContextMenu": (0x5D, True),
}
for _i in range(1, 25):
    _CODE_VK[f"F{_i}"] = (0x70 + _i - 1, False)
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _CODE_VK[f"Key{_c}"] = (ord(_c), False)
for _d in "0123456789":
    _CODE_VK[f"Digit{_d}"] = (ord(_d), False)
    _CODE_VK[f"Numpad{_d}"] = (0x60 + int(_d), False)


def vk_for_code(code: str) -> tuple[int, bool] | None:
    return _CODE_VK.get(code)


def _send(inputs: list[INPUT]) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse_input(dx: int = 0, dy: int = 0, flags: int = 0, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def _key_input(vk: int, extended: bool, up: bool) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def press_key_tap(vk: int) -> None:
    _send([_key_input(vk, False, False), _key_input(vk, False, True)])


def press_alt_tap() -> None:
    """A synthetic ALT press/release: makes the calling process eligible for SetForegroundWindow."""
    _send([_key_input(0xA4, False, False), _key_input(0xA4, False, True)])


class SendInputInjector:
    """Real input through the OS queue. Mouse goes to whatever is under the (moved) cursor, so
    dialogs/menus/tooltips work for free; keys go to the foreground window, so we make KiCad
    foreground first. Side effect: the user's physical cursor moves and KiCad steals focus."""

    name = "sendinput"

    def __init__(self, main_hwnd: int, pid: int) -> None:
        self.main_hwnd = main_hwnd
        self.pid = pid
        self._vx, self._vy, self._vw, self._vh = W.virtual_screen()
        self.foreground_fixes = 0
        self.obscured_fixes = 0

    def _abs(self, sx: int, sy: int) -> tuple[int, int]:
        # MOUSEEVENTF_ABSOLUTE|VIRTUALDESK: 0..65535 across the virtual screen (pixel centre).
        nx = int((sx - self._vx + 0.5) * 65535 / self._vw)
        ny = int((sy - self._vy + 0.5) * 65535 / self._vh)
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def ensure_foreground(self) -> None:
        if W.foreground_pid() != self.pid:
            self.foreground_fixes += 1
            W.bring_to_front(self.main_hwnd)

    def ensure_unobscured(self, sx: int, sy: int) -> None:
        """SendInput hits whatever HWND is under the point: if another app (e.g. VS Code itself)
        overlaps KiCad there, raise KiCad first. Counted so the report shows how often it happens."""
        h = win32gui.WindowFromPoint((sx, sy))
        if h and win32process.GetWindowThreadProcessId(h)[1] != self.pid:
            self.obscured_fixes += 1
            W.bring_to_front(self.main_hwnd)

    def move(self, sx: int, sy: int) -> None:
        nx, ny = self._abs(sx, sy)
        _send([_mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)])

    def button(self, sx: int, sy: int, button: int, down: bool) -> None:
        if down:
            self.ensure_unobscured(sx, sy)
        nx, ny = self._abs(sx, sy)
        flag = _BTN_FLAGS[button][0 if down else 1]
        _send([
            _mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
            _mouse_input(nx, ny, flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
        ])

    def wheel(self, sx: int, sy: int, dx: int, dy: int) -> None:
        self.ensure_unobscured(sx, sy)
        nx, ny = self._abs(sx, sy)
        inputs = [_mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)]
        if dy:
            inputs.append(_mouse_input(0, 0, MOUSEEVENTF_WHEEL, dy))
        if dx:
            inputs.append(_mouse_input(0, 0, MOUSEEVENTF_HWHEEL, dx))
        _send(inputs)

    def key(self, code: str, down: bool) -> bool:
        m = vk_for_code(code)
        if m is None:
            return False
        self.ensure_foreground()
        _send([_key_input(m[0], m[1], not down)])
        return True


class PostMessageInjector:
    """WM_* messages posted to the child window under the point (mouse) or the thread's focus
    window (keys). No cursor movement, no focus stealing, works for an off-screen window; but
    modifier keys are read by wx through GetKeyState, so Ctrl/Shift combos need the modifier to
    be genuinely down (we still SendInput the modifiers) and hover/tooltips/menus are unreliable."""

    name = "postmessage"

    def __init__(self, main_hwnd: int, pid: int) -> None:
        self.main_hwnd = main_hwnd
        self.pid = pid
        self._buttons = 0
        self._mods = 0
        self.foreground_fixes = 0

    def _target(self, sx: int, sy: int) -> tuple[int, int, int]:
        h = win32gui.WindowFromPoint((sx, sy))
        if not h:
            h = self.main_hwnd
        cx, cy = win32gui.ScreenToClient(h, (sx, sy))
        return h, cx, cy

    @staticmethod
    def _lparam(x: int, y: int) -> int:
        return (y & 0xFFFF) << 16 | (x & 0xFFFF)

    def ensure_foreground(self) -> None:
        return None

    def move(self, sx: int, sy: int) -> None:
        h, cx, cy = self._target(sx, sy)
        win32gui.PostMessage(h, win32con.WM_MOUSEMOVE, self._buttons | self._mods, self._lparam(cx, cy))

    def button(self, sx: int, sy: int, button: int, down: bool) -> None:
        h, cx, cy = self._target(sx, sy)
        msg_down, msg_up, mk = _BTN_WM[button]
        if down:
            self._buttons |= mk
            # wx checks focus for keyboard afterwards; a real click would activate the child.
            try:
                user32.SetFocus(h)
            except Exception:  # noqa: BLE001
                pass
        else:
            self._buttons &= ~mk
        win32gui.PostMessage(h, msg_down if down else msg_up, self._buttons | self._mods, self._lparam(cx, cy))

    def wheel(self, sx: int, sy: int, dx: int, dy: int) -> None:
        h, _cx, _cy = self._target(sx, sy)
        if dy:
            win32gui.PostMessage(h, WM_MOUSEWHEEL, (dy & 0xFFFF) << 16 | (self._buttons | self._mods), self._lparam(sx, sy))
        if dx:
            win32gui.PostMessage(h, WM_MOUSEHWHEEL, (dx & 0xFFFF) << 16 | (self._buttons | self._mods), self._lparam(sx, sy))

    def key(self, code: str, down: bool) -> bool:
        m = vk_for_code(code)
        if m is None:
            return False
        vk, ext = m
        if vk in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
            # modifiers: wx reads GetKeyState, which PostMessage cannot fake -> real key event
            _send([_key_input(vk, ext, not down)])
            bit = {0xA0: win32con.MK_SHIFT, 0xA1: win32con.MK_SHIFT, 0xA2: win32con.MK_CONTROL, 0xA3: win32con.MK_CONTROL}.get(vk, 0)
            self._mods = (self._mods | bit) if down else (self._mods & ~bit)
            return True
        h = W.focused_hwnd_of_thread(self.main_hwnd) or self.main_hwnd
        scan = user32.MapVirtualKeyW(vk, 0)
        lp = 1 | (scan << 16) | ((1 << 24) if ext else 0)
        if down:
            win32gui.PostMessage(h, win32con.WM_KEYDOWN, vk, lp)
            # No WM_CHAR here: wx's message loop runs TranslateMessage on the posted WM_KEYDOWN and
            # produces the WM_CHAR itself (posting one too doubled every character: "rR00880055").
        else:
            win32gui.PostMessage(h, win32con.WM_KEYUP, vk, lp | (1 << 30) | (1 << 31))
        return True


def make_injector(kind: str, main_hwnd: int, pid: int):
    if kind == "postmessage":
        return PostMessageInjector(main_hwnd, pid)
    return SendInputInjector(main_hwnd, pid)


def sleep_ms(ms: float) -> None:
    time.sleep(ms / 1000)
