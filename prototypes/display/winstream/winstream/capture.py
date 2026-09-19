"""Windows.Graphics.Capture sessions (via the `windows-capture` package, MIT, Rust) per HWND, plus
a tracker that follows every visible top-level window of the KiCad process (dialogs, menus,
tooltips, combo drop-downs) and starts/stops sessions as they appear.

WGC facts measured on this machine (see docs/research/display/winstream.md):
- a per-window capture contains ONLY that HWND's surface: owned dialogs / menus are separate;
- frames are delivered only when the window's content changes (idle windows produce nothing);
- the GL canvas (GAL) is present because WGC reads the DWM-composed surface of the window;
- size of a frame == DWMWA_EXTENDED_FRAME_BOUNDS of the window (physical pixels).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import simplejpeg
from windows_capture import WindowsCapture

from . import windows as W

FrameSink = Callable[[int, bytes, int, int, float, float], None]
# (hwnd, jpeg_bytes, width, height, t_capture_ms, encode_ms)


@dataclass
class SessionStats:
    frames: int = 0
    bytes: int = 0
    encode_ms: deque = field(default_factory=lambda: deque(maxlen=120))
    arrivals: deque = field(default_factory=lambda: deque(maxlen=240))
    last_size: tuple[int, int] = (0, 0)
    error: str = ""


class WindowSession:
    """One WGC capture of one HWND; encodes JPEG on the capture thread (simplejpeg releases the GIL)."""

    def __init__(self, hwnd: int, sink: FrameSink, quality: int, fps_cap: int, raw_sink=None) -> None:
        self.hwnd = hwnd
        self.sink = sink
        self.raw_sink = raw_sink
        self.quality = quality
        self.stats = SessionStats()
        self.closed = False
        self.last_bgra: np.ndarray | None = None
        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            window_hwnd=hwnd,
            minimum_update_interval=max(1, int(1000 / fps_cap)) if fps_cap else None,
            dirty_region=False,
        )
        cap.frame_handler = self._on_frame
        cap.closed_handler = self._on_closed
        self._cap = cap
        self._control = None

    def start(self) -> None:
        self._control = self._cap.start_free_threaded()

    def stop(self) -> None:
        self.closed = True
        try:
            if self._control is not None:
                self._control.stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_frame(self, frame, control) -> None:  # capture thread
        if self.closed:
            control.stop()
            return
        t_cap = time.time() * 1000
        t0 = time.perf_counter()
        buf = frame.frame_buffer
        if not buf.flags["C_CONTIGUOUS"]:
            buf = np.ascontiguousarray(buf)
        try:
            jpeg = simplejpeg.encode_jpeg(buf, quality=self.quality, colorspace="BGRA", fastdct=True)
        except Exception as e:  # noqa: BLE001
            self.stats.error = f"encode: {e}"
            return
        enc_ms = (time.perf_counter() - t0) * 1000
        st = self.stats
        st.frames += 1
        st.bytes += len(jpeg)
        st.encode_ms.append(enc_ms)
        st.arrivals.append(t_cap)
        st.last_size = (frame.width, frame.height)
        if self.raw_sink is not None:
            self.last_bgra = buf.copy()
        self.sink(self.hwnd, jpeg, frame.width, frame.height, t_cap, enc_ms)

    def _on_closed(self) -> None:
        self.closed = True


#: Window classes that a per-window WGC session must never touch: creating a capture item for a
#: popup menu dismisses it (measured: the menu closes 50-100 ms after the tracker's poll, and never
#: when child capture is off). Same treatment for tooltips and combo drop-downs (also transient
#: popups owned by the thread). They are on-screen anyway, so they are cropped from a monitor capture.
FRAGILE_CLASSES = ("#32768", "tooltips_class32", "ComboLBox", "wxTipWindow")


def is_fragile(cls: str) -> bool:
    return any(cls.startswith(f) for f in FRAGILE_CLASSES)


class MonitorCropSession:
    """One WGC *monitor* capture, cropped to the rects of the fragile windows currently shown.
    Monitor capture does not disturb menus. Started lazily and stopped when none is left."""

    def __init__(self, monitor_index: int, origin: tuple[int, int], sink: FrameSink, quality: int, fps_cap: int) -> None:
        self.origin = origin
        self.sink = sink
        self.quality = quality
        self.rects: dict[int, tuple[int, int, int, int]] = {}
        self.stats = SessionStats()
        self.closed = False
        cap = WindowsCapture(cursor_capture=False, draw_border=False, monitor_index=monitor_index,
                             minimum_update_interval=max(1, int(1000 / fps_cap)) if fps_cap else None,
                             dirty_region=False)
        cap.frame_handler = self._on_frame
        cap.closed_handler = self._on_closed
        self._cap = cap
        self._control = None
        self.last_bgra = None

    def start(self) -> None:
        self._control = self._cap.start_free_threaded()

    def stop(self) -> None:
        self.closed = True
        try:
            if self._control is not None:
                self._control.stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_frame(self, frame, control) -> None:
        if self.closed:
            control.stop()
            return
        t_cap = time.time() * 1000
        buf = frame.frame_buffer
        ox, oy = self.origin
        for hwnd, (l, t, r, b) in list(self.rects.items()):
            x0, y0 = max(0, l - ox), max(0, t - oy)
            x1, y1 = min(frame.width, r - ox), min(frame.height, b - oy)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = np.ascontiguousarray(buf[y0:y1, x0:x1])
            t0 = time.perf_counter()
            try:
                jpeg = simplejpeg.encode_jpeg(crop, quality=self.quality, colorspace="BGRA", fastdct=True)
            except Exception as e:  # noqa: BLE001
                self.stats.error = f"encode: {e}"
                continue
            self.stats.frames += 1
            self.stats.bytes += len(jpeg)
            self.stats.encode_ms.append((time.perf_counter() - t0) * 1000)
            self.stats.arrivals.append(t_cap)
            self.stats.last_size = (x1 - x0, y1 - y0)
            self.sink(hwnd, jpeg, x1 - x0, y1 - y0, t_cap, 0.0)

    def _on_closed(self) -> None:
        self.closed = True


def monitor_index_for(hwnd: int) -> tuple[int, tuple[int, int]]:
    """(1-based WGC monitor index, monitor origin in physical px) of the monitor showing `hwnd`.
    windows-capture enumerates monitors with EnumDisplayMonitors, so the order matches win32api's."""
    import win32api

    target = win32api.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    for i, (hm, _hdc, _rect) in enumerate(win32api.EnumDisplayMonitors(), start=1):
        if int(hm) == int(target):
            info = win32api.GetMonitorInfo(hm)["Monitor"]
            return i, (info[0], info[1])
    return 1, (0, 0)


class ProcessTracker:
    """Polls the process's top-level windows and keeps one WindowSession per visible window."""

    def __init__(self, pid: int, main_hwnd: int, sink: FrameSink, on_windows, quality: int, fps_cap: int,
                 poll_s: float = 0.1, capture_children: bool = True) -> None:
        self.pid = pid
        self.main_hwnd = main_hwnd
        self.sink = sink
        self.on_windows = on_windows  # callback(list[WinInfo]) when the set/geometry changes
        self.quality = quality
        self.fps_cap = fps_cap
        self.poll_s = poll_s
        self.capture_children = capture_children
        self.sessions: dict[int, WindowSession] = {}
        self.failed: dict[int, str] = {}
        self.monitor: MonitorCropSession | None = None
        self.fragile_seen = 0
        self.windows: list[W.WinInfo] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tracker", daemon=True)
        self.main_rect = W.frame_bounds(main_hwnd)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for s in list(self.sessions.values()):
            s.stop()

    def _run(self) -> None:
        last_sig = None
        while not self._stop.is_set():
            try:
                wins = W.windows_of_pid(self.pid)
            except Exception:  # noqa: BLE001
                wins = []
            if not self.capture_children:
                wins = [w for w in wins if w.hwnd == self.main_hwnd]
            for w in wins:
                if w.hwnd == self.main_hwnd:
                    self.main_rect = w.rect
            present = {w.hwnd for w in wins}
            fragile = {w.hwnd: w.rect for w in wins if is_fragile(w.cls)}
            if fragile and self.monitor is None:
                try:
                    idx, origin = monitor_index_for(self.main_hwnd)
                    self.monitor = MonitorCropSession(idx, origin, self.sink, self.quality, self.fps_cap)
                    self.monitor.start()
                except Exception as e:  # noqa: BLE001
                    self.failed[-1] = f"monitor capture: {e}"
                    self.monitor = None
            if self.monitor is not None:
                self.fragile_seen += len(set(fragile) - set(self.monitor.rects))
                self.monitor.rects = fragile
                if not fragile:
                    self.monitor.stop()
                    self.monitor = None
            for w in wins:
                if is_fragile(w.cls):
                    continue
                if w.hwnd not in self.sessions and w.hwnd not in self.failed:
                    s = WindowSession(w.hwnd, self.sink, self.quality, self.fps_cap, raw_sink=True)
                    try:
                        s.start()
                        self.sessions[w.hwnd] = s
                    except Exception as e:  # noqa: BLE001
                        self.failed[w.hwnd] = f"{w.cls} {w.title!r}: {e}"
            for h in list(self.sessions):
                if h not in present or self.sessions[h].closed:
                    self.sessions[h].stop()
                    del self.sessions[h]
            for h in list(self.failed):
                if h not in present:
                    del self.failed[h]
            sig = tuple((w.hwnd, w.rect, w.z) for w in wins)
            if sig != last_sig:
                last_sig = sig
                self.windows = wins
                try:
                    self.on_windows(wins)
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.poll_s)
