"""`GuiSession`: one sandboxed KiCad GUI (`pcbnew.exe` / `eeschema.exe` / `kicad.exe`) with its
API server bound to a project, for humans watching (docs/DISPLAY.md §7.4) and for `kw_live`.

What is verified (Windows 11, KiCad 10.0.6 and 10.99.0-4083 stock installs, 2026-09-20;
scratchpad phase2/kw_live/probe1.py, then tests/test_gui_session_live.py):
- The GUI API server listens on `ipc://<GetTempDir()>/kicad/api.sock` and `GetTempDir()` honours
  the process's TEMP/TMP (docs/VERIFICATION.md claim 10 (5); `KICAD_API_SERVER::StandardSocketPath`
  in common/api/api_server.cpp, unchanged on the 10.99 base): a GUI started with its own short
  TEMP (`<ConfigRoot.temp_dir>/g<6 hex>`, ~77 chars for the socket, under the 120-char pipe limit)
  binds its OWN pipe, so a session never contends with another KiCad on the machine for the
  fixed default pipe. Only `kicad-cli api-server` takes `--socket`; the GUI has no override.
- kicad-python 0.9 (kipy, the pinned client) talks to both GUIs over that pipe: `GetVersion`,
  `GetOpenDocuments`, `get_board()` (15 footprints of the ecc83 demo), `begin_commit` /
  `drop_commit`. A stock 10.0.6 answered 12 s after launch, the 10.99 nightly 35 s on a fresh
  sandbox root (library tables load, fp-info-cache is built); later starts are faster.
- A GUI launched with `STARTUPINFO.wShowWindow = SW_SHOWNOACTIVATE` (`STARTF_USESHOWWINDOW`) did
  NOT take the foreground on either version (`GetForegroundWindow` unchanged before, right after
  readiness and 2 s later); the frame is still a normal, visible, maximised `wxWindowNR` window
  that WGC can capture (docs/DISPLAY.md §12.5: cloaked or covered windows are composed, fully
  off-screen or minimised ones are not - the launcher never minimises or hides the frame).
- The GUI takes KiCad's project lock AND the document lock next to the file
  (`~<name>.kicad_pro.lck`, `~<name>.kicad_pcb.lck`) and `TerminateProcess` leaves both behind,
  so `close()` records the `~*.lck` files that appeared during start-up and deletes them once the
  process is confirmed dead (the sandbox is a scratch copy owned by kiwrap; a lock that predates
  the session is left alone, like `sessions._remove_stale_lock`).
- The product mode is `view` (docs/DECISIONS.md D20): the editor is a normal visible window on
  the desktop, started without activation, streamed into the VS Code tab, and the person
  interacts with it natively (`raise`). `KICAD_INPUT_FROM_EVENTS=cloak` (fork branch
  `mr/display-input`, docs/DISPLAY.md §12.5, parked) is exported ONLY when `mode="hidden"` is
  requested explicitly AND the install carries the patch (`input_from_events()` below: the
  manifest `builds[]` entry of the install's BUILT commit, the same reader as `kw_env`'s
  `gui.input_from_events`; the install's own `kiwrap-install.json` claim is not consulted, so a
  re-registered 6dd893f7d1-7 answers False everywhere); `cloaked` is then read back from
  `DwmGetWindowAttribute(DWMWA_CLOAKED)` on the frame after readiness so a build that silently
  lacks the patch is reported, not assumed. `auto` and `mirror` are accepted as spellings of
  `view` (older callers); neither ever cloaks.

Shape:
- Every process runs with `ConfigRoot.env()` of a root seeded with `enable_api_server=True`
  (claim 10 (2): a GUI ignores nothing - the flag must be true for it to start the server), its
  own TEMP, `CREATE_NEW_PROCESS_GROUP`, the kill-on-close Job Object of `kiwrap.platform.lifetime`
  (the OS kills the GUI with kiwrap, orderly exit or not) and a record under
  `<Platform.temp_dir>/gui-sessions/<pid>.json` for `sweep_orphans()`.
- Readiness = the pipe answers anything but AS_NOT_READY / connection refused: a standalone
  `eeschema.exe` on 9/10 has no `API_HANDLER_COMMON`, so `GetVersion` comes back as "no handler"
  (claim 10 (4)) - that reply still proves the server is up.
- At most ONE GUI session per kiwrap process (PROCESS HYGIENE: one GUI at a time per agent, and
  `kiwrap.backends.ipc.sessions` binds it to its project so no headless server is started there).
- Duck-compatible with `HeadlessSession` for the readers / twins: `install`, `capabilities`,
  `file`, `running`, `pid`, `socket_url`, `kicad()`, `client()`, `board()`, `schematic()`,
  `open_document()`, `load_libraries()` (a no-op: the editor loaded its tables at start).
- Screenshots without the helper: `PrintWindow(PW_RENDERFULLCONTENT)` of a frame (works on a
  cloaked window too, docs/DISPLAY.md §12.5) scaled by GDI `StretchBlt(HALFTONE)` to `max_px` and
  encoded as PNG with zlib (no image library in the dependency set). With the extension's helper
  attached (`attach_helper`), `/control {type: screenshot}` over a minimal RFC 6455 client.
- Two kiwrap servers, one GUI (docs/EXTENSION.md §6, the integrator's wiring): the VS Code
  extension's `kiwrap serve` launches the GUI; the agent's `kiwrap serve` (Claude Code, from
  `.mcp.json`) is another process, and its `kw_pcb_place` must land in that same window. The
  record under `<Platform.temp_dir>/gui-sessions/<pid>.json` therefore carries everything a second
  process needs (socket, token, install root, editor, mode, files, locks, the helper endpoint once
  attached), and `RemoteGuiSession` is the HeadlessSession-compatible view of such a foreign
  session: `sessions.bound_session_for()` discovers it through `discover_remote_session()`, so the
  agent's ipc11 provider binds to the extension's KiCad; `find_gui_session()` falls back to it so
  `kw_live status | screenshot | detach` work from either server. Both servers must share the
  storage home (`temp_dir` derives from `KIWRAP_HOME`): that is the product configuration
  (extension setting `kiwrap.storageHome` empty, `.mcp.json` without `KIWRAP_HOME`).
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import itertools
import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kiwrap.backends.ipc import ensure_kipy_importable
from kiwrap.backends.ipc.sessions import (
    CLIENT_NAME_PREFIX,
    STOP_GRACE_S,
    SessionError,
    SessionStartError,
    _command_line,
    _kill_tree,
    _pid_alive,
    bind_gui_session,
    unbind_gui_session,
)
from kiwrap.capability import Capabilities, build_carries_patch, capabilities_for
from kiwrap.installation import KiCadInstallation
from kiwrap.platform import current
from kiwrap.platform.lifetime import bind_child_lifetime, popen_lifetime_kwargs
from kiwrap.sandbox import ConfigRoot, ensure

__all__ = [
    "DISPLAY_INPUT_PATCH",
    "EDITORS",
    "MODES",
    "GuiBusyError",
    "GuiSession",
    "GuiSessionError",
    "GuiUnsupportedError",
    "HelperEndpoint",
    "RemoteGuiSession",
    "SurfaceInfo",
    "close_all_gui",
    "discover_remote_session",
    "find_gui_session",
    "helper_screenshot",
    "input_from_events",
    "launch",
    "live_gui_sessions",
    "png_encode",
    "remote_gui_sessions",
    "sweep_orphans",
]

log = logging.getLogger(__name__)

DISPLAY_INPUT_PATCH = "display-input"
GUI_CAPABILITY_KEY = "gui.input_from_events"
EDITORS: tuple[str, ...] = ("pcb", "sch", "project")
#: `view` is the product mode (D20); `auto` / `mirror` are accepted as its older spellings and
#: resolve to it; `hidden` is the experimental cloaked mode (explicit request + display-input patch)
MODES: tuple[str, ...] = ("view", "auto", "mirror", "hidden")
VIEW_MODE = "view"
RECORDS_DIRNAME = "gui-sessions"
DEFAULT_READY_TIMEOUT_S = 180.0  # fresh sandbox root on the nightly: 35 s measured; libraries scale it
DEFAULT_REQUEST_TIMEOUT_MS = 10_000
MAX_SCREENSHOT_PX = 1024
SW_SHOWNOACTIVATE = 4
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 0x00000002

#: window title suffixes KiCad gives its top-level frames -> surface kinds (helper contract)
_KINDS: tuple[tuple[str, str], ...] = (
    ("PCB Editor", "pcb"),
    ("Schematic Editor", "sch"),
    ("3D Viewer", "3d"),
    ("Footprint Editor", "fp"),
    ("Symbol Editor", "sym"),
)
_EXE: dict[str, str] = {"pcb": "pcbnew", "sch": "eeschema", "project": "kicad"}

_REGISTRY: dict[str, GuiSession] = {}
_REGISTRY_LOCK = threading.Lock()


class GuiSessionError(SessionError):
    """The GUI could not do what was asked (no window, capture failed, helper refused)."""

    code = "E_GUI_SESSION"


class GuiBusyError(GuiSessionError):
    """Another GUI session is live (one per agent) or another one is bound to the project."""

    code = "E_GUI_BUSY"


class GuiUnsupportedError(GuiSessionError):
    """The install cannot serve the requested mode / editor (no display-input patch, no exe)."""

    code = "E_GUI_UNSUPPORTED"


# --- capability -----------------------------------------------------------------------------------


def input_from_events(install: KiCadInstallation, caps: Capabilities | None = None) -> bool:
    """Whether this install's KiCad honours `KICAD_INPUT_FROM_EVENTS` (fork patch `display-input`).

    One rule, shared with `kw_env` (`capability.build_carries_patch`, the source of the key
    `gui.input_from_events`): the manifest's `builds[]` entry of the install's BUILT commit must
    list the patch. The patch has no CLI/IPC surface to probe, and the install's own
    `kiwrap-install.json` claim (`KiCadInstallation.kiwrap_patches`) is deliberately NOT consulted
    - the same "never the install's own claim" rule as `kiwrap.patches`, so a re-registered build
    whose manifest entry dropped the patch (D20: 6dd893f7d1-7) answers False here, in `kw_env`
    and in the launch receipt's `hidden_available` alike. A capability record that already
    carries the key (derived from the same function) short-circuits. Stock installs answer
    False."""
    if caps is not None:
        with contextlib.suppress(Exception):
            if caps.has(GUI_CAPABILITY_KEY):
                return True
    return build_carries_patch(install, DISPLAY_INPUT_PATCH)


# --- Win32 window helpers (ctypes only; reached only on Windows through `current().is_windows`) ----


def _user32() -> Any:
    """user32 with 64-bit-safe prototypes for the handle-returning calls this module uses (the
    ctypes default `c_int` restype truncates an HWND / HDC above 2**31 and a bare Python int
    argument above that raises OverflowError - seen once in the tool round trip)."""
    user32 = getattr(ctypes, "windll").user32  # noqa: B009 - Windows only (mypy on Linux)
    for name in ("GetWindow", "GetForegroundWindow", "GetDC"):
        getattr(user32, name).restype = ctypes.c_void_p
    user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    with contextlib.suppress(AttributeError):
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
    return user32


def _dwmapi() -> Any:
    return getattr(ctypes, "windll").dwmapi  # noqa: B009


def _gdi32() -> Any:
    gdi32 = getattr(ctypes, "windll").gdi32  # noqa: B009
    for name in ("CreateCompatibleDC", "SelectObject", "CreateDIBSection"):
        getattr(gdi32, name).restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.StretchBlt.argtypes = (
        [ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint32]
    )
    return gdi32


@contextlib.contextmanager
def _per_monitor_dpi() -> Any:
    """Per-monitor-v2 DPI awareness for THIS thread while enumerating (physical pixels, like the
    helper reports them); restored afterwards. No-op when the OS lacks the call."""
    user32 = _user32()
    previous = None
    try:
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        previous = None
    try:
        yield
    finally:
        if previous:
            with contextlib.suppress(Exception):
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))


def _is_cloaked(hwnd: int) -> bool:
    value = ctypes.c_uint32()
    dwmapi = _dwmapi()
    dwmapi.DwmGetWindowAttribute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    hr = dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
    )
    return hr == 0 and value.value != 0


def _window_text(hwnd: int, fn: Any) -> str:
    buf = ctypes.create_unicode_buffer(512)
    fn(ctypes.c_void_p(hwnd), buf, 512)
    return buf.value


def _dpi_for(hwnd: int) -> int:
    user32 = _user32()
    try:
        return int(user32.GetDpiForWindow(ctypes.c_void_p(hwnd))) or 96
    except (AttributeError, OSError):
        return 96


@dataclass
class SurfaceInfo:
    """One top-level window of the session (helper contract `surfaces` entry)."""

    id: str
    kind: str  # pcb | sch | 3d | mgr | fp | sym | other
    title: str
    hwnd: int
    rect_px: tuple[int, int, int, int]  # left, top, right, bottom, physical px
    dpi: int
    cloaked: bool
    owner: int = 0
    cls: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "hwnd": self.hwnd,
            "rect_px": list(self.rect_px),
            "dpi": self.dpi,
            "cloaked": self.cloaked,
        }


def _kind_of(title: str, cls: str, owner: int) -> str:
    for suffix, kind in _KINDS:
        if title.endswith(suffix):
            return kind
    if owner == 0 and cls == "wxWindowNR" and (title == "KiCad" or " KiCad " in f" {title} "):
        return "mgr"
    return "other"


def windows_of(pid: int, *, include_owned: bool = False) -> list[SurfaceInfo]:
    """Visible top-level windows of `pid` in z-order (top first), cloaked ones INCLUDED (they are
    the point of hidden mode); minimised and empty ones skipped. Without `include_owned` only
    unowned frames (KiCad's editor frames are unowned siblings, dialogs are owned:
    docs/DISPLAY.md §12.2)."""
    if not current().is_windows:
        return []
    user32 = _user32()
    out: list[SurfaceInfo] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    class _Rect(ctypes.Structure):
        _fields_ = [(n, ctypes.c_long) for n in ("left", "top", "right", "bottom")]

    def cb(hwnd: Any, _: Any) -> bool:
        h = int(hwnd or 0)
        owner_pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(owner_pid))
        if owner_pid.value != pid or not user32.IsWindowVisible(ctypes.c_void_p(h)):
            return True
        if user32.IsIconic(ctypes.c_void_p(h)):
            return True
        owner = int(user32.GetWindow(ctypes.c_void_p(h), 4) or 0)  # GW_OWNER
        if owner and not include_owned:
            return True
        rect = _Rect()
        user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(rect))
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True
        title = _window_text(h, user32.GetWindowTextW)
        cls = _window_text(h, user32.GetClassNameW)
        if cls in ("tooltips_class32", "#32768", "ComboLBox"):
            return True
        kind = _kind_of(title, cls, owner)
        out.append(
            SurfaceInfo(
                id=f"{kind}-{h:x}",
                kind=kind,
                title=title,
                hwnd=h,
                rect_px=(rect.left, rect.top, rect.right, rect.bottom),
                dpi=_dpi_for(h),
                cloaked=_is_cloaked(h),
                owner=owner,
                cls=cls,
            )
        )
        return True

    with _per_monitor_dpi():
        user32.EnumWindows(enum_proc(cb), 0)
    return out


def foreground_window() -> tuple[int, int]:
    """(hwnd, pid) of the foreground window, (0, 0) off Windows or when there is none."""
    if not current().is_windows:
        return 0, 0
    user32 = _user32()
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return 0, 0
    pid = ctypes.c_uint32()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return hwnd, int(pid.value)


def raise_window(hwnd: int) -> bool:
    """Bring a frame to the front from a background process (best effort): plain
    `SetForegroundWindow` is refused unless the caller owns the foreground, so the documented
    ALT-tap (a synthetic Alt press/release makes the OS grant the next request) is tried once.
    Returns whether the window is the foreground window afterwards."""
    if not current().is_windows:
        return False
    user32 = _user32()
    h = ctypes.c_void_p(hwnd)
    user32.ShowWindow(h, 5)  # SW_SHOW (restores nothing, activates nothing on its own)
    if user32.SetForegroundWindow(h) and int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
    user32.keybd_event(0x12, 0, 2, 0)  # KEYEVENTF_KEYUP
    user32.SetForegroundWindow(h)
    user32.BringWindowToTop(h)
    return int(user32.GetForegroundWindow() or 0) == hwnd


# --- PNG (zlib, stdlib only) ------------------------------------------------------------------------


def png_encode(width: int, height: int, bgra: bytes) -> bytes:
    """A 24-bit RGB PNG from a top-down BGRA buffer (filter 0 rows, zlib level 6)."""
    if len(bgra) < width * height * 4:
        raise ValueError("pixel buffer too small")
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2 : width * height * 4 : 4]
    rgb[1::3] = bgra[1 : width * height * 4 : 4]
    rgb[2::3] = bgra[0 : width * height * 4 : 4]
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride : (y + 1) * stride]) for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def capture_window(hwnd: int, *, max_px: int = MAX_SCREENSHOT_PX) -> tuple[bytes, int, int]:
    """`PrintWindow(PW_RENDERFULLCONTENT)` of `hwnd` into a DIB, GDI HALFTONE downscale to
    `max_px` on the longest edge, PNG bytes + final size. Works for covered and cloaked windows
    (docs/DISPLAY.md §12.5); a minimised or fully off-screen window yields a stale or black frame."""
    if not current().is_windows:
        raise GuiSessionError("window capture needs Windows", fix="use the kicad-cli render fallback")
    user32, gdi32 = _user32(), _gdi32()

    class _Rect(ctypes.Structure):
        _fields_ = [(n, ctypes.c_long) for n in ("left", "top", "right", "bottom")]

    class _BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class _BitmapInfo(ctypes.Structure):
        _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 3)]

    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_BitmapInfo),
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]

    def dib(w: int, h: int) -> tuple[Any, Any]:
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(None, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bmp:
            raise GuiSessionError("CreateDIBSection failed", fix="use the kicad-cli render fallback")
        return bmp, bits

    with _per_monitor_dpi():
        rect = _Rect()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            raise GuiSessionError(f"window {hwnd:#x} is gone", fix="kw_live status lists the live surfaces")
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            raise GuiSessionError("window has no client area (minimised?)", fix="raise the window first")
        scale = min(1.0, max_px / max(w, h))
        sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
        src_dc = gdi32.CreateCompatibleDC(None)
        dst_dc = gdi32.CreateCompatibleDC(None)
        src_bmp, _ = dib(w, h)
        dst_bmp, dst_bits = dib(sw, sh)
        try:
            gdi32.SelectObject(src_dc, src_bmp)
            gdi32.SelectObject(dst_dc, dst_bmp)
            # PrintWindow answers 0 while the frame is still being built right after launch
            # (seen once in the tool round trip on 10.0.6): a few retries over ~2 s cover it
            ok = False
            for _attempt in range(8):
                ok = bool(user32.PrintWindow(ctypes.c_void_p(hwnd), src_dc, PW_RENDERFULLCONTENT))
                if ok:
                    break
                time.sleep(0.25)
            if not ok:
                raise GuiSessionError("PrintWindow failed", fix="use the kicad-cli render fallback")
            gdi32.SetStretchBltMode(dst_dc, 4)  # HALFTONE
            gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
            gdi32.StretchBlt(dst_dc, 0, 0, sw, sh, src_dc, 0, 0, w, h, 0x00CC0020)  # SRCCOPY
            gdi32.GdiFlush()
            raw = ctypes.string_at(dst_bits, sw * sh * 4)
        finally:
            gdi32.DeleteDC(src_dc)
            gdi32.DeleteDC(dst_dc)
            gdi32.DeleteObject(src_bmp)
            gdi32.DeleteObject(dst_bmp)
    return png_encode(sw, sh, raw), sw, sh


# --- helper (Rust `kiwrap-live.exe`) control channel: minimal RFC 6455 client -----------------------


LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    return str(host).strip().strip("[]").lower() in LOOPBACK_HOSTS


@dataclass(frozen=True)
class HelperEndpoint:
    """What the extension registers with `kw_live attach` (helper protocol contract: the helper
    prints `{port, token, version}` on stdout; `/control` and `/stream` take `?token=`)."""

    port: int
    token: str
    version: str = ""
    host: str = "127.0.0.1"
    pid: int | None = None

    def __post_init__(self) -> None:
        # the helper contract is loopback-only: a record naming any other host would make BOTH
        # kiwrap servers open an outbound WebSocket to it with the caller's token in the URL
        # (reviewer finding 13, 2026-09-20)
        if not is_loopback_host(self.host):
            raise SessionError(
                f"helper host {self.host!r} is not a loopback address",
                why="the display helper listens on 127.0.0.1 only (helper protocol contract)",
                fix="pass host='127.0.0.1' (the default), 'localhost' or '::1'",
            )

    def url(self, path: str) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"ws://{host}:{self.port}{path}?token={self.token}"

    def to_json(self, *, reveal_token: bool = False) -> dict[str, Any]:
        """Receipts mask the token; the session record (same user, loopback only) keeps it so
        another kiwrap process can ask the helper for screenshots."""
        return {
            "port": self.port,
            "version": self.version,
            "host": self.host,
            "pid": self.pid,
            "token": self.token if reveal_token else "***",
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> HelperEndpoint:
        try:
            return cls(
                port=int(raw["port"]),
                token=str(raw["token"]),
                version=str(raw.get("version", "")),
                host=str(raw.get("host", "127.0.0.1")),
                pid=int(raw["pid"]) if raw.get("pid") else None,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise SessionError(
                f"helper endpoint needs port and token: {exc}",
                fix="pass helper={'port': <int>, 'token': '<str>', 'version': '<str>'} as the helper printed it",
            ) from exc


#: text frames the helper pushes on /control on its own (never an answer to a request)
_WS_PUSH_TYPES: frozenset[str] = frozenset(
    {"hello", "event", "surface", "surface_created", "surface_changed", "surface_destroyed", "probe", "log"}
)
_WS_IDS = itertools.count(1)


def _ws_reply_matches(request: dict[str, Any], reply: Any) -> bool:
    """Whether `reply` answers `request`: the echoed `id` when the helper echoes one (kiwrap-live
    does, `with_id`), else a frame of the request's type or a bare error that is not one of the
    helper's own pushes. The `hello` kiwrap-live sends on connect (reviewer finding 4,
    2026-09-20: it was taken for the answer, so every screenshot fell back to PrintWindow) and
    the surface events are skipped."""
    if not isinstance(reply, dict):
        return False
    rid = reply.get("id")
    if rid is not None:
        return rid == request.get("id")
    rtype = str(reply.get("type") or "")
    if rtype in _WS_PUSH_TYPES:
        return False
    return rtype in ("", "error", str(request.get("type") or ""))


def _ws_request(url: str, message: dict[str, Any], *, timeout_s: float = 10.0) -> dict[str, Any]:
    """Open a WebSocket, send one JSON text frame (with a fresh `id`), return the JSON text
    frame that answers it (`_ws_reply_matches`; the helper's hello and pushes are skipped,
    control frames are answered/skipped). Client frames are masked as the RFC requires; no
    extensions are negotiated (a VS Code webview client needs permessage-deflate off too,
    docs/DISPLAY.md §12.3)."""
    message = {**message, "id": next(_WS_IDS)} if message.get("id") is None else dict(message)
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise GuiSessionError("helper closed the connection during the handshake")
            header += chunk
        head, rest = header.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise GuiSessionError(
                f"helper refused the WebSocket upgrade: {status.decode('ascii', 'replace')}",
                fix="check the helper's port and token (kw_live attach registers them)",
            )
        payload = json.dumps(message).encode("utf-8")
        mask = os.urandom(4)
        frame = bytearray([0x81])
        n = len(payload)
        if n < 126:
            frame.append(0x80 | n)
        elif n < 65536:
            frame += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            frame += bytes([0x80 | 127]) + struct.pack(">Q", n)
        frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(frame))
        buf = bytearray(rest)

        def need(k: int) -> None:
            while len(buf) < k:
                chunk = sock.recv(65536)
                if not chunk:
                    raise GuiSessionError("helper closed the connection before answering")
                buf.extend(chunk)

        deadline = time.monotonic() + timeout_s
        text = bytearray()
        while time.monotonic() < deadline:
            need(2)
            fin, opcode, ln = buf[0] & 0x80, buf[0] & 0x0F, buf[1] & 0x7F
            masked = bool(buf[1] & 0x80)
            offset = 2
            if ln == 126:
                need(4)
                ln = struct.unpack(">H", bytes(buf[2:4]))[0]
                offset = 4
            elif ln == 127:
                need(10)
                ln = struct.unpack(">Q", bytes(buf[2:10]))[0]
                offset = 10
            if masked:
                need(offset + 4)
                fmask = bytes(buf[offset : offset + 4])
                offset += 4
            need(offset + ln)
            data = bytes(buf[offset : offset + ln])
            if masked:
                data = bytes(b ^ fmask[i % 4] for i, b in enumerate(data))
            del buf[: offset + ln]
            if opcode == 0x9:  # ping -> pong
                sock.sendall(
                    bytes([0x8A, 0x80 | len(data)])
                    + mask
                    + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                )
                continue
            if opcode == 0x8:
                raise GuiSessionError("helper closed the control channel")
            if opcode in (0x1, 0x0):
                text.extend(data)
                if fin:
                    try:
                        reply = json.loads(text.decode("utf-8"))
                    except ValueError:
                        reply = None
                    text = bytearray()
                    if not _ws_reply_matches(message, reply):
                        continue  # the hello, a surface event: keep waiting for our answer
                    with contextlib.suppress(OSError):
                        sock.sendall(bytes([0x88, 0x80]) + mask)  # close
                    return reply
            # binary frames on /control are not part of the contract: skip
        raise GuiSessionError("helper did not answer in time", fix="retry; check the helper log")


def helper_screenshot(endpoint: HelperEndpoint, surface: str, *, max_px: int = MAX_SCREENSHOT_PX) -> bytes:
    """`{type: 'screenshot', surface, max_px}` -> PNG bytes (the reply's base64 field is accepted
    under `png`, `data`, `image` or `base64`; an `error` field is a teaching error)."""
    reply = _ws_request(
        endpoint.url("/control"), {"type": "screenshot", "surface": surface, "max_px": max_px}
    )
    if reply.get("error"):
        raise GuiSessionError(
            f"helper screenshot failed: {reply['error']}", fix="kw_live status lists the surfaces"
        )
    for key in ("png", "data", "image", "base64"):
        value = reply.get(key)
        if isinstance(value, str) and value:
            if value.startswith("data:"):
                value = value.split(",", 1)[-1]
            return base64.b64decode(value)
    raise GuiSessionError("helper screenshot reply carries no image", details=json.dumps(reply)[:500])


def helper_surfaces(endpoint: HelperEndpoint) -> list[dict[str, Any]]:
    reply = _ws_request(endpoint.url("/control"), {"type": "surfaces"})
    surfaces = reply.get("surfaces", reply if isinstance(reply, list) else [])
    return [s for s in surfaces if isinstance(s, dict)]


def helper_surface_id(endpoint: HelperEndpoint, wanted: str | None, frame: SurfaceInfo | None) -> str | None:
    """The helper's id of the surface a caller means: `wanted` when it is one of the helper's own
    ids, else the entry whose `hwnd` is the frame's (our ids are `<kind>-<hwnd>`, the helper's
    `pcb`, `pcb-2`, ...: reviewer finding 4's second half - the mismatch fell back to
    PrintWindow), else the first entry of the wanted / frame's kind, else the first surface at
    all. None when the helper lists nothing (the caller then tries its own guess)."""
    surfaces = helper_surfaces(endpoint)
    if not surfaces:
        return None
    ids = {str(s.get("id")) for s in surfaces}
    if wanted and wanted in ids:
        return wanted
    hwnd = frame.hwnd if frame is not None else None
    for s in surfaces:
        if hwnd is not None and int(s.get("hwnd") or 0) == hwnd:
            return str(s.get("id"))
    kind = (
        wanted if wanted in {k for _, k in _KINDS} | {"mgr"} else (frame.kind if frame is not None else None)
    )
    for s in surfaces:
        if kind and str(s.get("kind")) == kind:
            return str(s.get("id"))
    return str(surfaces[0].get("id"))


# --- orphan records -------------------------------------------------------------------------------


def _records_dir(temp_dir: Path | None = None) -> Path:
    return (temp_dir if temp_dir is not None else current().temp_dir) / RECORDS_DIRNAME


def sweep_orphans(temp_dir: Path | None = None) -> list[int]:
    """Kill GUI sessions recorded under `<temp>/gui-sessions/` whose kiwrap parent is gone (the
    Job Object normally took them already; this covers a kiwrap that was itself started inside a
    job that forbids nesting) - identified by kiwrap's own record plus the recorded exe and
    document in the process's command line, never by image name alone - and remove the `~*.lck`
    files the record names. Never raises."""
    killed: list[int] = []
    try:
        d = _records_dir(temp_dir)
        if not d.is_dir():
            return killed
        live = {s.pid for s in live_gui_sessions() if s.pid is not None}
        for rec in sorted(d.glob("*.json")):
            try:
                info = json.loads(rec.read_text(encoding="utf-8"))
                pid, parent = int(info["pid"]), int(info.get("parent_pid", 0))
            except (OSError, ValueError, KeyError, TypeError):
                with contextlib.suppress(OSError):
                    rec.unlink()
                continue
            if _pid_alive(pid):
                orphan = not _pid_alive(parent) or (parent == os.getpid() and pid not in live)
                if not orphan:
                    continue
                if not _record_matches_process(info, pid):
                    continue  # the pid was recycled: leave it alone (and keep no record)
                _kill_tree(pid)
                for _ in range(50):
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.1)
                if _pid_alive(pid):
                    continue
                killed.append(pid)
            for lck in info.get("locks", []) or []:
                with contextlib.suppress(OSError):
                    Path(lck).unlink()
            with contextlib.suppress(OSError):
                rec.unlink()
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break a launch
        log.debug("gui orphan sweep failed: %s", exc)
    return killed


class _FramesMixin:
    """Window introspection shared by `GuiSession` and `RemoteGuiSession`: both know the GUI's
    `pid`, `editor`, `running`, `helper` and `warnings`; the frames are found through the OS, so a
    session another process launched is inspected exactly like one's own."""

    editor: str
    helper: HelperEndpoint | None
    warnings: list[str]

    @property
    def pid(self) -> int | None:  # pragma: no cover - the concrete classes provide it
        raise NotImplementedError

    @property
    def running(self) -> bool:  # pragma: no cover - the concrete classes provide it
        raise NotImplementedError

    def surfaces(self) -> list[SurfaceInfo]:
        if self.pid is None:
            return []
        return windows_of(self.pid)

    def frame(self, kind: str | None = None, *, wait_s: float = 0.0) -> SurfaceInfo | None:
        """The editor frame of `kind` (default: the session's editor kind, else the first frame),
        waiting up to `wait_s` for one to appear (the API answers before the frame is shown)."""
        deadline = time.monotonic() + wait_s
        while True:
            wins = self.surfaces()
            wanted = kind or {"pcb": "pcb", "sch": "sch", "project": "mgr"}[self.editor]
            for w in wins:
                if w.kind == wanted:
                    return w
            for w in wins:
                if w.kind != "other":
                    return w
            if wins or time.monotonic() >= deadline or not self.running:
                return wins[0] if wins else None
            time.sleep(0.2)

    def raise_frame(self, kind: str | None = None) -> dict[str, Any]:
        f = self.frame(kind)
        if f is None:
            raise GuiSessionError("no window to raise", fix="the GUI has no frame yet or exited")
        if f.cloaked:
            return {
                "raised": False,
                "surface": f.to_json(),
                "note": "cloaked window: the helper owns the view",
            }
        return {"raised": raise_window(f.hwnd), "surface": f.to_json()}

    def screenshot(
        self, *, surface: str | None = None, max_px: int = MAX_SCREENSHOT_PX
    ) -> tuple[bytes, dict[str, Any]]:
        """PNG bytes + summary: the helper's `/control` screenshot when one is attached, else a
        `PrintWindow` capture of the frame. Raises `GuiSessionError` when neither can answer
        (the caller falls back to a render of the saved file)."""
        max_px = max(64, min(int(max_px), MAX_SCREENSHOT_PX))
        if self.helper is not None:
            f = self.frame(surface if surface in {k for _, k in _KINDS} | {"mgr"} else None)
            try:
                # the helper has its own surface ids (`pcb`, `pcb-2`, ...): resolve ours (a kind,
                # or `<kind>-<hwnd>` from `windows_of`) through its list, by id, hwnd, then kind
                surface_id = (
                    helper_surface_id(self.helper, surface, f) or surface or (f.id if f else self.editor)
                )
                png = helper_screenshot(self.helper, surface_id, max_px=max_px)
                return png, {"source": "helper", "surface": surface_id, "max_px": max_px}
            except (GuiSessionError, OSError, ValueError) as exc:
                self.warnings.append(f"helper screenshot failed ({exc}); captured the window directly")
        f = self.frame(surface if surface in {k for _, k in _KINDS} | {"mgr"} else None, wait_s=5.0)
        if f is None:
            raise GuiSessionError("no window to capture", fix="the GUI has no frame yet or exited")
        png, w, h = capture_window(f.hwnd, max_px=max_px)
        return png, {"source": "window", "surface": f.to_json(), "width": w, "height": h}


# --- the session ----------------------------------------------------------------------------------


@dataclass
class GuiSession(_FramesMixin):
    """One sandboxed KiCad GUI bound to a project (module docstring)."""

    install: KiCadInstallation
    project_dir: Path
    editor: str = "pcb"
    mode: str = VIEW_MODE
    config_root: ConfigRoot | None = None
    capabilities_record: Capabilities | None = None
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    storage_home: Path | None = None
    session_id: str = field(default_factory=lambda: "g" + os.urandom(4).hex())
    #: KiCad's own instance token (`ApiResponse.header.kicad_token`, a KIID the server mints at
    #: start): learned from the readiness probe and sent back on every request, so a request of
    #: this session can never land in another KiCad that inherited the pipe path
    #: (AS_TOKEN_MISMATCH). NOT a secret and NOT an authentication: KiCad accepts requests that
    #: carry no token at all (common/api/api_server.cpp; reviewer finding 16, 2026-09-20).
    token: str = ""
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    temp_dir: Path | None = None
    socket_path: Path | None = None
    document: Path | None = None  # the file passed on the command line
    files: list[Path] = field(default_factory=list)  # every design file this GUI is expected to hold
    mode_used: str = ""
    cloak_requested: bool = False
    cloaked: bool | None = None
    in_job: bool = False
    started_epoch: float = 0.0
    ready_after_s: float | None = None
    record: Path | None = None
    helper: HelperEndpoint | None = None
    locks_taken: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: launch-time foreground grab (docs/DISPLAY.md 12.5 "one foreground grab at launch"): whether
    #: the GUI took the foreground although it was started with SW_SHOWNOACTIVATE, and whether the
    #: previous foreground window got it back
    foreground_taken: bool = False
    foreground_restored: bool | None = None
    _kicad: Any = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _locks_before: set[str] = field(default_factory=set, repr=False)

    #: a session this process launched (RemoteGuiSession says True)
    remote: bool = field(default=False, init=False)

    # -- HeadlessSession-compatible surface ------------------------------------------------------

    @property
    def config(self) -> ConfigRoot:
        if self.config_root is None:
            self.config_root = ensure(self.install, storage_home=self.storage_home, enable_api_server=True)
        return self.config_root

    @property
    def capabilities(self) -> Capabilities:
        if self.capabilities_record is None:
            self.capabilities_record = capabilities_for(self.install)
        return self.capabilities_record

    @property
    def file(self) -> Path | None:
        return self.document

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def socket_url(self) -> str | None:
        return f"ipc://{self.socket_path}" if self.socket_path else None

    @property
    def exe(self) -> Path:
        return self.install.root / "bin" / (_EXE[self.editor] + current().exe_suffix)

    def kicad(self) -> Any:
        if not self.running:
            raise GuiSessionError("GUI session is not running", fix="kw_live(action='launch') starts one")
        if self._kicad is None:
            from kipy.client import KiCadClient
            from kipy.kicad import KiCad

            client = KiCadClient(
                self.socket_url or "",
                f"{CLIENT_NAME_PREFIX}-live-{self.session_id}",
                self.token,
                self.request_timeout_ms,
            )
            self._kicad = KiCad.from_client(client)
        return self._kicad

    def client(self) -> Any:
        return self.kicad()._client

    def board(self) -> Any:
        return self.kicad().get_board()

    def schematic(self) -> Any:
        return self.kicad().get_schematic()

    def open_document(self, path: str | Path, kind: str) -> Any:
        from kipy.proto.common.types import DocumentType

        types = {
            "pcb": DocumentType.DOCTYPE_PCB,
            "schematic": DocumentType.DOCTYPE_SCHEMATIC,
            "project": DocumentType.DOCTYPE_PROJECT,
        }
        if kind not in types:
            raise SessionError(f"unknown document kind {kind!r}", fix=f"one of {sorted(types)}")
        return self.kicad().open_document(str(Path(path).resolve()), types[kind])

    def load_libraries(
        self, kinds: Any = ("symbol", "footprint"), *, timeout_s: float = 0.0
    ) -> dict[str, str]:
        """The editor loaded its tables at start-up (a GUI never runs `LoadAllLibraries`)."""
        return {}

    def version(self) -> str:
        return str(self.kicad().get_version().full_version)

    def serves(self, path: str | Path) -> bool:
        """Whether a request for `path` belongs to this session (the file is under the bound
        project directory)."""
        try:
            Path(path).resolve().relative_to(self.project_dir)
        except ValueError:
            return False
        return True

    # -- lifecycle ---------------------------------------------------------------------------------

    def _document_for(self) -> Path:
        pros = sorted(self.project_dir.glob("*.kicad_pro"))
        stem = pros[0].stem if pros else self.project_dir.name
        wanted = {
            "pcb": self.project_dir / f"{stem}.kicad_pcb",
            "sch": self.project_dir / f"{stem}.kicad_sch",
            "project": self.project_dir / f"{stem}.kicad_pro",
        }[self.editor]
        if not wanted.is_file():
            raise SessionStartError(
                f"{wanted.name} not found in {self.project_dir}",
                why="the editor opens the project's design file of its kind",
                fix="kw_project(action='status') lists the files; create the board / schematic first",
            )
        return wanted

    def _resolve_mode(self) -> tuple[str, bool]:
        """(mode_used, cloak): `view` (the product path, D20; `auto` / `mirror` are its older
        spellings) never cloaks; `hidden` is honoured only when requested explicitly AND the
        install carries the display-input patch, and refused honestly otherwise."""
        if self.mode not in MODES:
            raise SessionError(f"unknown mode {self.mode!r}", fix=f"one of {MODES}")
        if self.mode != "hidden":
            return VIEW_MODE, False
        if not input_from_events(self.install, self.capabilities):
            raise GuiUnsupportedError(
                f"KiCad {self.install.version} at {self.install.root} cannot run hidden: it lacks the "
                f"`{DISPLAY_INPUT_PATCH}` patch (KICAD_INPUT_FROM_EVENTS)",
                why="hidden mode cloaks the editor windows from inside KiCad (fork branch mr/display-input, "
                "experimental and parked under docs/DECISIONS.md D20)",
                fix="use mode='view' (the live view; the person interacts with the KiCad window on the desktop)",
            )
        return "hidden", True

    def start(self) -> GuiSession:
        with self._lock:
            if self.running:
                return self
            if not current().is_windows:
                raise GuiUnsupportedError("GUI sessions are Windows-only (docs/DECISIONS.md D18)")
            ensure_kipy_importable()
            _register_atexit()
            if self.editor not in EDITORS:
                raise SessionError(f"unknown editor {self.editor!r}", fix=f"one of {EDITORS}")
            other = [s for s in live_gui_sessions() if s is not self]
            if other:
                raise GuiBusyError(
                    f"a GUI session is already live: {other[0].session_id} on {other[0].project_dir}",
                    why="kiwrap runs one KiCad GUI at a time per agent (PROCESS HYGIENE)",
                    fix=f"kw_live(action='detach', session_id='{other[0].session_id}') first",
                )
            self.project_dir = Path(self.project_dir).resolve()
            foreign = discover_remote_session(self.project_dir)
            if foreign is not None:
                raise GuiBusyError(
                    f"a GUI session launched by another kiwrap process (pid {foreign.owner_pid}) is live on "
                    f"{self.project_dir}: {foreign.session_id} (KiCad pid {foreign.pid})",
                    why="one KiCad per project: its API server owns the project's files (docs/DISPLAY.md 7.4); "
                    "the other server (the VS Code extension's or the agent's) already shows it",
                    fix=f"use it (kw_live status / screenshot find it) or kw_live(action='detach', "
                    f"session_id='{foreign.session_id}') closes it from here",
                )
            self.document = self._document_for()
            pro = self.project_dir / (self.document.stem + ".kicad_pro")
            self.files = [p for p in (self.document, pro) if p.is_file()]
            if not self.exe.is_file():
                raise GuiUnsupportedError(
                    f"{self.exe} is missing",
                    why="this install has no GUI of that kind (a kicad-cli-only tree?)",
                    fix="pick another editor or install",
                )
            self.mode_used, self.cloak_requested = self._resolve_mode()
            sweep_orphans(current().temp_dir)
            config = self.config
            env = config.env()
            self.temp_dir = config.temp_dir(current()) / f"g{os.urandom(3).hex()}"
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            env["TEMP"] = env["TMP"] = str(self.temp_dir)
            self.socket_path = self.temp_dir / "kicad" / "api.sock"
            limit = current().max_socket_path
            if len(str(self.socket_path)) > limit:
                raise SessionError(
                    f"API socket path is {len(str(self.socket_path))} chars, over the {limit}-char pipe limit",
                    fix="set KIWRAP_HOME (or the platform temp dir) to a shorter path",
                )
            if self.cloak_requested:
                env["KICAD_INPUT_FROM_EVENTS"] = "cloak"
            else:
                env.pop("KICAD_INPUT_FROM_EVENTS", None)
            locks_before = {p.name for p in self.project_dir.glob("~*.lck")}
            prev_fg, _prev_fg_pid = foreground_window()
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = SW_SHOWNOACTIVATE
            argv = [str(self.exe), str(self.document)]
            self.process = subprocess.Popen(
                argv,
                env=env,
                cwd=str(self.project_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                **popen_lifetime_kwargs(),
            )
            self.in_job = bind_child_lifetime(self.process)
            self.started_epoch = time.time()
            with _REGISTRY_LOCK:
                _REGISTRY[self.session_id] = self
            self._write_record(locks_before)
            try:
                self._wait_ready()
            except BaseException:
                self.close()
                raise
            self.locks_taken = sorted(
                p for p in self.project_dir.glob("~*.lck") if p.name not in locks_before
            )
            self._write_record(locks_before)
            self.frame(wait_s=10.0)  # the API answers before the frame is shown; wait for it
            frames = [s for s in self.surfaces() if s.kind != "other"] or self.surfaces()
            self.cloaked = any(s.cloaked for s in frames) if frames else None
            self._give_foreground_back(prev_fg)
            if self.cloak_requested and not self.cloaked:
                self.warnings.append(
                    "cloak requested (KICAD_INPUT_FROM_EVENTS=cloak) but no frame reports DWMWA_CLOAKED: "
                    "this build does not honour the variable; the window is visible on the desktop"
                )
                self.mode_used = VIEW_MODE
            self._write_record()  # mode_used / cloaked are now known to a second process too
            bind_gui_session(self.project_dir, self)
            return self

    def _give_foreground_back(self, prev_fg: int) -> None:
        """Measured on the display-input build (extension/test/e2e, 2026-09-20): the frame takes the
        foreground once at launch even with SW_SHOWNOACTIVATE (docs/DISPLAY.md 12.5) - a cloaked
        foreground window would swallow the user's typing invisibly. Hand it back to whoever had
        it (best effort, the ALT-tap route of `raise_window`); reported in status()."""
        fg, fg_pid = foreground_window()
        if not fg or fg_pid != self.pid:
            return
        self.foreground_taken = True
        if not prev_fg or prev_fg == fg or not _user32().IsWindow(ctypes.c_void_p(prev_fg)):
            self.foreground_restored = False
            self.warnings.append(
                "the GUI took the foreground at launch and no previous window could take it back"
            )
            return
        self.foreground_restored = bool(raise_window(prev_fg))
        if not self.foreground_restored:
            self.warnings.append(
                "the GUI took the foreground at launch (docs/DISPLAY.md 12.5) and it could not be given back"
            )

    def _write_record(self, locks_before: set[str] | None = None) -> None:
        """The orphan-sweep record, which is also what another kiwrap process reads to bind to
        this GUI (`RemoteGuiSession`): everything below but `locks` / `parent_pid` is what the
        remote side needs to talk to the same API server and helper."""
        if locks_before is not None:
            self._locks_before = set(locks_before)
        d = _records_dir(current().temp_dir)
        d.mkdir(parents=True, exist_ok=True)
        assert self.process is not None
        self.record = d / f"{self.process.pid}.json"
        payload = {
            "pid": self.process.pid,
            "parent_pid": os.getpid(),
            "session_id": self.session_id,
            "token": self.token,
            "exe": str(self.exe),
            "editor": self.editor,
            "document": str(self.document),
            "project_dir": str(self.project_dir),
            "files": [str(p) for p in self.files],
            "socket": str(self.socket_path),
            "temp": str(self.temp_dir) if self.temp_dir else None,
            "install_root": str(self.install.root),
            "install_key": self.install.key(),
            "kicad": self.install.version,
            "mode": self.mode,
            "mode_used": self.mode_used,
            "cloak_requested": self.cloak_requested,
            "cloaked": self.cloaked,
            "started": self.started_epoch,
            "ready_after_s": self.ready_after_s,
            "helper": self.helper.to_json(reveal_token=True) if self.helper else None,
            "locks": [str(p) for p in self.project_dir.glob("~*.lck") if p.name not in self._locks_before],
        }
        with contextlib.suppress(OSError):
            self.record.write_text(json.dumps(payload), encoding="utf-8")

    def _wait_ready(self) -> None:
        """Poll the pipe until KiCad answers (any status but NOT_READY / refused, see docstring)."""
        from kipy.client import KiCadClient
        from kipy.errors import ApiError, ConnectionError
        from kipy.proto.common import commands

        deadline = time.monotonic() + self.ready_timeout_s
        last = ""
        while time.monotonic() < deadline:
            if not self.running:
                raise SessionStartError(
                    f"{self.exe.name} exited with status {self.process.returncode if self.process else '?'} "
                    "before its API answered",
                    why="the GUI could not start or could not load the document",
                    fix="open the file in KiCad by hand once to see its error; check the sandbox root",
                )
            probe = KiCadClient(self.socket_url or "", f"{CLIENT_NAME_PREFIX}.live-probe", "", 1000)
            try:
                probe.send(commands.GetVersion(), commands.GetVersionResponse)
                # kipy keeps the instance token of the first OK reply: this session's requests
                # carry it from now on (see `token`)
                self.token = str(getattr(probe, "_kicad_token", "") or "")
                probe.close()
                self.ready_after_s = round(time.time() - self.started_epoch, 1)
                return
            except ApiError as exc:
                probe.close()
                last = str(exc)
                text = last.lower()
                if "not ready" not in text and "not_ready" not in text:
                    # AS_UNHANDLED ("no handler available") from a standalone eeschema: the
                    # server is up (claim 10 (4))
                    self.ready_after_s = round(time.time() - self.started_epoch, 1)
                    self.warnings.append(f"API answered without GetVersion: {last[:120]}")
                    return
            except (ConnectionError, OSError) as exc:
                probe.close()
                last = str(exc)
            time.sleep(0.25)
        raise SessionStartError(
            f"timed out after {self.ready_timeout_s:.0f} s waiting for the {self.exe.name} API",
            why="the GUI is running but its API server never answered (api.enable_server off? a modal dialog?)",
            fix="raise ready_timeout_s for huge projects; kw_live screenshot / the desktop shows a blocking dialog",
            details=f"socket: {self.socket_url}; last error: {last}",
        )

    def close(self) -> None:
        """Terminate the GUI (idempotent, never raises): `CloseAllDocuments` first when the server
        handles it (releases the locks cleanly), then `TerminateProcess`, then a tree kill; the
        lock files this session took are removed once the process is dead, the seed restored."""
        with self._lock:
            unbind_gui_session(self.project_dir, self)
            with _REGISTRY_LOCK:
                _REGISTRY.pop(self.session_id, None)
            kicad, self._kicad = self._kicad, None
            proc = self.process
            if kicad is not None:
                with contextlib.suppress(Exception):
                    kicad._client.close()
            if proc is not None and proc.poll() is None:
                self._release_documents()
                with contextlib.suppress(OSError):
                    proc.terminate()
                with contextlib.suppress(subprocess.SubprocessError):
                    proc.wait(timeout=STOP_GRACE_S)
                if proc.poll() is None:
                    _kill_tree(proc.pid)
                    with contextlib.suppress(subprocess.SubprocessError):
                        proc.wait(timeout=STOP_GRACE_S)
                if proc.poll() is None:  # pragma: no cover - last resort
                    with contextlib.suppress(OSError):
                        proc.kill()
                    with contextlib.suppress(subprocess.SubprocessError):
                        proc.wait(timeout=STOP_GRACE_S)
            if proc is not None and proc.poll() is not None:
                self.process = None
                for lck in self.locks_taken:
                    with contextlib.suppress(OSError):
                        lck.unlink()
                if self.record is not None:
                    with contextlib.suppress(OSError):
                        self.record.unlink()
                    self.record = None
                if self.config_root is not None:
                    with contextlib.suppress(Exception), self.config_root.lock(timeout_s=STOP_GRACE_S):
                        self.config_root.restore_seed()
            elif proc is not None:
                log.warning(
                    "GUI session %s pid %s survived close(); it stays recorded", self.session_id, proc.pid
                )

    def _release_documents(self) -> None:
        from google.protobuf.empty_pb2 import Empty
        from kipy.client import KiCadClient
        from kipy.proto.common.commands import project_commands_pb2

        client: Any = None
        try:
            client = KiCadClient(self.socket_url or "", f"{CLIENT_NAME_PREFIX}.live-close", "", 2000)
            client.send(project_commands_pb2.CloseAllDocuments(), Empty)
        except Exception as exc:  # noqa: BLE001 - a GUI may not handle it; the process is killed next
            log.debug("CloseAllDocuments on the GUI failed: %s", exc)
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()

    def __enter__(self) -> GuiSession:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        alive = self.running
        return {
            "session_id": self.session_id,
            "alive": alive,
            "pid": self.pid,
            "exit_code": self.process.returncode if self.process is not None and not alive else None,
            "install": self.install.key(),
            "kicad": self.install.version,
            "editor": self.editor,
            "mode": self.mode,
            "mode_used": self.mode_used,
            "cloak_requested": self.cloak_requested,
            "cloaked": self.cloaked,
            "project": str(self.project_dir),
            "document": str(self.document) if self.document else None,
            "files": [str(p) for p in self.files],
            "api_socket": self.socket_url,
            "token": self.token,
            "temp": str(self.temp_dir) if self.temp_dir else None,
            "in_job": self.in_job,
            "ready_after_s": self.ready_after_s,
            "started": self.started_epoch,
            "editors": [s.to_json() for s in self.surfaces()] if alive else [],
            "helper": self.helper.to_json() if self.helper else None,
            "locks": [p.name for p in self.locks_taken],
            "warnings": list(self.warnings),
            "foreground_taken": self.foreground_taken,
            "foreground_restored": self.foreground_restored,
            "owner_pid": os.getpid(),
            "remote": False,
        }

    def attach_helper(self, endpoint: HelperEndpoint | dict[str, Any]) -> HelperEndpoint:
        if isinstance(endpoint, dict):
            endpoint = HelperEndpoint.from_json(endpoint)
        self.helper = endpoint
        if self.process is not None and self.running:
            self._write_record()  # the other server's screenshots go through the helper too
        return endpoint


# --- a session another kiwrap process launched ----------------------------------------------------


class RemoteGuiSession(_FramesMixin):
    """The GUI session of ANOTHER kiwrap process, seen through its record (module docstring: the
    two-server model). Same duck type as `GuiSession` for the readers / twins / `kw_live`, over
    the same API socket and helper; this process never started the GUI and does not own it:
    `close()` is `kw_live detach` of a foreign session (kill by pid, remove the recorded locks
    and the record - the owner notices the pid dying like any other exit)."""

    remote = True

    def __init__(self, record: Path, info: dict[str, Any], install: KiCadInstallation):
        self.record = record
        self.info = info
        self.install = install
        self.capabilities_record: Capabilities | None = None
        self.request_timeout_ms = DEFAULT_REQUEST_TIMEOUT_MS
        self.session_id = str(info.get("session_id", ""))
        self.token = str(info.get("token", ""))
        self.owner_pid = int(info.get("parent_pid", 0) or 0)
        self._pid = int(info["pid"])
        self.editor = str(info.get("editor") or _editor_of_exe(str(info.get("exe", ""))))
        self.project_dir = Path(str(info.get("project_dir", "")))
        self.document = Path(info["document"]) if info.get("document") else None
        self.files = [Path(f) for f in info.get("files", []) or []]
        self.socket_path = Path(info["socket"]) if info.get("socket") else None
        self.temp_dir = Path(info["temp"]) if info.get("temp") else None
        self.mode = str(info.get("mode", VIEW_MODE))
        self.mode_used = str(info.get("mode_used", ""))
        self.cloak_requested = bool(info.get("cloak_requested", False))
        self.cloaked: bool | None = info.get("cloaked")
        self.started_epoch = float(info.get("started", 0.0) or 0.0)
        self.ready_after_s = info.get("ready_after_s")
        self.locks_taken = [Path(x) for x in info.get("locks", []) or []]
        raw_helper = info.get("helper")
        self.helper: HelperEndpoint | None = HelperEndpoint.from_json(raw_helper) if raw_helper else None
        self.warnings: list[str] = []
        self.in_job = False
        self._kicad: Any = None
        self._closed = False

    # -- HeadlessSession-compatible surface ------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        if self.capabilities_record is None:
            self.capabilities_record = capabilities_for(self.install)
        return self.capabilities_record

    @property
    def file(self) -> Path | None:
        return self.document

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def running(self) -> bool:
        """Alive while the KiCad pid lives and the owner has not withdrawn the record (it deletes
        it on its own `close()`; a recycled pid without the record is not our GUI)."""
        return not self._closed and self.record.is_file() and _pid_alive(self._pid)

    @property
    def socket_url(self) -> str | None:
        return f"ipc://{self.socket_path}" if self.socket_path else None

    def refresh(self) -> None:
        """Re-read the record: the owner rewrites it when it learns the cloak state or attaches
        the helper."""
        try:
            info = json.loads(self.record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if int(info.get("pid", 0) or 0) != self._pid:
            return
        self.info = info
        self.mode_used = str(info.get("mode_used", self.mode_used))
        self.cloaked = info.get("cloaked", self.cloaked)
        self.locks_taken = [Path(x) for x in info.get("locks", []) or []]
        raw_helper = info.get("helper")
        if raw_helper:
            with contextlib.suppress(SessionError):
                self.helper = HelperEndpoint.from_json(raw_helper)

    def kicad(self) -> Any:
        if not self.running:
            raise GuiSessionError(
                "the GUI session of the other kiwrap process is not running",
                fix="kw_live(action='launch') starts one",
            )
        if self._kicad is None:
            from kipy.client import KiCadClient
            from kipy.kicad import KiCad

            client = KiCadClient(
                self.socket_url or "",
                f"{CLIENT_NAME_PREFIX}-live-{self.session_id}-{os.getpid()}",
                self.token,  # the owner's record carries KiCad's instance token (GuiSession.token)
                self.request_timeout_ms,
            )
            self._kicad = KiCad.from_client(client)
        return self._kicad

    def client(self) -> Any:
        return self.kicad()._client

    def board(self) -> Any:
        return self.kicad().get_board()

    def schematic(self) -> Any:
        return self.kicad().get_schematic()

    def open_document(self, path: str | Path, kind: str) -> Any:
        return GuiSession.open_document(self, path, kind)  # type: ignore[arg-type]

    def load_libraries(
        self, kinds: Any = ("symbol", "footprint"), *, timeout_s: float = 0.0
    ) -> dict[str, str]:
        return {}

    def version(self) -> str:
        return str(self.kicad().get_version().full_version)

    def serves(self, path: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.project_dir)
        except ValueError:
            return False
        return True

    # -- lifecycle ---------------------------------------------------------------------------------

    def close(self) -> bool:
        """Detach a foreign session: kill the GUI by pid (its owner's job object / liveness poll
        sees it go), remove the locks the record names once it is dead, drop the record. Returns
        whether OUR GUI is gone: True when the pid is dead or belongs to something else now (a
        recycled pid: the GUI it recorded is gone, its locks are stale); False when the process
        survived the kill - the record and the locks are then kept for the orphan sweep and the
        session stays discoverable (reviewer finding 24, 2026-09-20)."""
        kicad, self._kicad = self._kicad, None
        if kicad is not None:
            with contextlib.suppress(Exception):
                kicad._client.close()
        unbind_gui_session(self.project_dir, self)
        ours = True
        if _pid_alive(self._pid):
            ours = _record_matches_process(self.info, self._pid)
            if ours:
                _kill_tree(self._pid)
                for _ in range(int(STOP_GRACE_S * 10)):
                    if not _pid_alive(self._pid):
                        break
                    time.sleep(0.1)
        dead = not ours or not _pid_alive(self._pid)
        if dead:
            for lck in self.locks_taken:
                with contextlib.suppress(OSError):
                    lck.unlink()
            with contextlib.suppress(OSError):
                self.record.unlink()
            self._closed = True
            with _REMOTE_LOCK:
                _REMOTE.pop(self.record, None)
        else:
            log.warning(
                "foreign GUI session %s pid %s survived the kill; record kept", self.session_id, self._pid
            )
        return dead

    def status(self) -> dict[str, Any]:
        self.refresh()
        alive = self.running
        return {
            "session_id": self.session_id,
            "alive": alive,
            "pid": self._pid,
            "exit_code": None,
            "install": self.install.key(),
            "kicad": self.install.version,
            "editor": self.editor,
            "mode": self.mode,
            "mode_used": self.mode_used,
            "cloak_requested": self.cloak_requested,
            "cloaked": self.cloaked,
            "project": str(self.project_dir),
            "document": str(self.document) if self.document else None,
            "files": [str(p) for p in self.files],
            "api_socket": self.socket_url,
            "token": self.token,
            "temp": str(self.temp_dir) if self.temp_dir else None,
            "in_job": False,
            "ready_after_s": self.ready_after_s,
            "started": self.started_epoch,
            "editors": [s.to_json() for s in self.surfaces()] if alive else [],
            "helper": self.helper.to_json() if self.helper else None,
            "locks": [p.name for p in self.locks_taken],
            "warnings": list(self.warnings),
            "owner_pid": self.owner_pid,
            "remote": True,
        }

    def attach_helper(self, endpoint: HelperEndpoint | dict[str, Any]) -> HelperEndpoint:
        """A helper registered from this process for a foreign session: kept here and written
        into the record so the owner (and any third process) sees it too."""
        if isinstance(endpoint, dict):
            endpoint = HelperEndpoint.from_json(endpoint)
        self.helper = endpoint
        try:
            info = json.loads(self.record.read_text(encoding="utf-8"))
            info["helper"] = endpoint.to_json(reveal_token=True)
            self.record.write_text(json.dumps(info), encoding="utf-8")
        except (OSError, ValueError):
            pass
        return endpoint


def _editor_of_exe(exe: str) -> str:
    stem = Path(exe).stem.lower()
    for editor, name in _EXE.items():
        if stem == name:
            return editor
    return "pcb"


_REMOTE: dict[Path, RemoteGuiSession] = {}
_REMOTE_LOCK = threading.Lock()
#: (pid, record `started`) -> whether that pid's command line carried the recorded exe and
#: document when first checked: a pid's command line cannot change while it lives, so one
#: check per record identity is enough (reviewer finding 12/23, 2026-09-20)
_CMDLINE_VERDICTS: dict[tuple[int, float], bool] = {}


def _record_matches_process(info: dict[str, Any], pid: int) -> bool:
    """Whether `pid` still is the GUI `info` recorded (its command line carries the recorded
    exe and document; a recycled pid does not). Cached per (pid, started)."""
    key = (int(pid), float(info.get("started", 0.0) or 0.0))
    with _REMOTE_LOCK:
        verdict = _CMDLINE_VERDICTS.get(key)
    if verdict is not None:
        return verdict
    cmd = _command_line(pid).lower()
    exe, doc = str(info.get("exe", "")).lower(), str(info.get("document", "")).lower()
    verdict = bool(exe) and exe in cmd and (not doc or doc in cmd)
    with _REMOTE_LOCK:
        if len(_CMDLINE_VERDICTS) > 256:
            _CMDLINE_VERDICTS.clear()
        _CMDLINE_VERDICTS[key] = verdict
    return verdict


def _forget_dead_record(rec: Path, info: dict[str, Any]) -> None:
    """A record whose GUI pid is dead (its kiwrap owner died hard and the Job Object took the
    GUI, or the owner crashed before its own cleanup): remove the `~*.lck` files it names -
    the same identity rule `sweep_orphans` applies - and the record, so the next mutation is
    not refused E_FILE_LOCKED for locks nobody holds (reviewer finding 10, 2026-09-20)."""
    for lck in info.get("locks", []) or []:
        with contextlib.suppress(OSError):
            Path(lck).unlink()
    with contextlib.suppress(OSError):
        rec.unlink()
    with _REMOTE_LOCK:
        _REMOTE.pop(rec, None)


def _remote_from_record(rec: Path) -> RemoteGuiSession | None:
    """The live foreign session a record describes, or None (dead, ours, recycled pid, or an
    older record shape without the fields a remote needs). Cheap on the hot path: the cached
    session is returned before any command-line check, and that check runs once per record."""
    try:
        info = json.loads(rec.read_text(encoding="utf-8"))
        pid, parent = int(info["pid"]), int(info.get("parent_pid", 0) or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if parent == os.getpid():
        return None  # this process's own GuiSession (its close() cleans up)
    if not _pid_alive(pid):
        _forget_dead_record(rec, info)
        return None
    with _REMOTE_LOCK:
        cached = _REMOTE.get(rec)
    if cached is not None and cached.pid == pid and cached.running:
        cached.refresh()
        return cached
    if not info.get("socket") or not info.get("install_root"):
        return None
    if not _record_matches_process(info, pid):
        return None  # the pid was recycled by something else
    from kiwrap.installation import find

    try:
        install = find(str(info["install_root"]))
    except Exception as exc:  # noqa: BLE001 - an install that vanished cannot be bound
        log.debug("remote GUI session %s: install %s not found: %s", rec, info.get("install_root"), exc)
        return None
    session = RemoteGuiSession(rec, info, install)
    with _REMOTE_LOCK:
        _REMOTE[rec] = session
    return session


def remote_gui_sessions() -> list[RemoteGuiSession]:
    """Every live GUI session recorded by OTHER kiwrap processes sharing this temp dir."""
    out: list[RemoteGuiSession] = []
    d = _records_dir(current().temp_dir)
    if not d.is_dir():
        return out
    for rec in sorted(d.glob("*.json")):
        session = _remote_from_record(rec)
        if session is not None:
            out.append(session)
    return out


def discover_remote_session(
    path: str | Path | None = None, *, session_id: str | None = None
) -> RemoteGuiSession | None:
    """The foreign session by id, else the one whose project directory contains `path` (or is
    `path`), else the only live one when neither is given."""
    sessions = remote_gui_sessions()
    if session_id:
        return next((s for s in sessions if s.session_id == session_id), None)
    if path is not None:
        target = Path(path).resolve()
        for s in sessions:
            key = s.project_dir.resolve()
            if target == key:
                return s
            try:
                target.relative_to(key)
            except ValueError:
                continue
            return s
        return None
    return sessions[0] if len(sessions) == 1 else None


# --- registry -------------------------------------------------------------------------------------


def live_gui_sessions() -> list[GuiSession]:
    with _REGISTRY_LOCK:
        return [s for s in _REGISTRY.values() if s.running]


def find_gui_session(
    session_id: str | None = None, project_dir: str | Path | None = None
) -> GuiSession | RemoteGuiSession | None:
    """By id, else by project directory, else the only live one - this process's sessions first,
    then the ones other kiwrap processes recorded (the two-server model), so `kw_live status /
    screenshot / detach` answer from either server."""
    sessions = live_gui_sessions()
    found: GuiSession | RemoteGuiSession | None
    if session_id:
        found = next((s for s in sessions if s.session_id == session_id), None)
        return found if found is not None else discover_remote_session(session_id=session_id)
    if project_dir is not None:
        wanted = Path(project_dir).resolve()
        found = next((s for s in sessions if s.project_dir == wanted), None)
        return found if found is not None else discover_remote_session(wanted)
    if len(sessions) == 1:
        return sessions[0]
    if sessions:
        return None
    return discover_remote_session()


def close_all_gui() -> int:
    """Close every GUI session of this process (atexit hook; tests call it in teardown)."""
    with _REGISTRY_LOCK:
        sessions = list(_REGISTRY.values())
    for s in sessions:
        s.close()
    return len(sessions)


_ATEXIT_REGISTERED = False


def _register_atexit() -> None:
    """Like `sessions._register_atexit`: registered only once kipy/pynng are importable so the
    hook runs BEFORE pynng's `nng_fini` (atexit is LIFO) and the closing `CloseAllDocuments`
    dial cannot hang the interpreter exit."""
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    import atexit

    import pynng  # noqa: F401 - imported for its atexit registration order

    atexit.register(close_all_gui)
    _ATEXIT_REGISTERED = True


def launch(
    install: KiCadInstallation,
    project_dir: str | Path,
    *,
    editor: str = "pcb",
    mode: str = VIEW_MODE,
    config_root: ConfigRoot | None = None,
    capabilities: Capabilities | None = None,
    storage_home: Path | None = None,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
) -> GuiSession:
    """Start a GUI session (the `kw_live launch` backend). `config_root` must have been made with
    `enable_api_server=True`; when None one is ensured for the install (same root as the
    kicad-cli tools use - the GUI ignores the seed except the api flag, and the tools' runs
    restore the seed after every process)."""
    session = GuiSession(
        install=install,
        project_dir=Path(project_dir),
        editor=editor,
        mode=mode,
        config_root=config_root,
        capabilities_record=capabilities,
        storage_home=storage_home,
        ready_timeout_s=ready_timeout_s,
    )
    return session.start()


def session_digest(session: GuiSession) -> str:
    """A stable short id for receipts (never the token)."""
    return hashlib.sha1(f"{session.session_id}:{session.pid}".encode()).hexdigest()[:8]
