"""winstream server: capture a KiCad GUI process (all its top-level windows) with WGC, stream JPEG
frames over a WebSocket to a browser page, inject the page's input back into the windows.

    python -m winstream.server --env env.json --pcb <scratch>/ecc83/ecc83-pp.kicad_pcb \
        --size 1920x1080 --pos 100,100 --quality 70 --fps 30 --inject sendinput --port 8765

Endpoints (127.0.0.1 only, `?token=` required on /ws when --token is given):
    GET /            viewer page            GET /ws       stream + input
    GET /stats       JSON counters          GET /shot?hwnd=<n>   PNG of the last raw frame
    POST /quit       stop server (and the KiCad process it launched)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import psutil
from aiohttp import WSMsgType, web

from . import capture as C
from . import inject as I
from . import windows as W

HEADER_FMT = "<4sIIIIiiddII"  # magic, hwnd, seq, w, h, x, y, t_capture_ms, t_inject_ms, probe_id, flags
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAGIC = b"WSF1"
STATIC = Path(__file__).parent / "static"


class Streamer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: dict[web.WebSocketResponse, dict] = {}
        self.seq = 0
        self.probe: tuple[int, float] | None = None
        self.proc: subprocess.Popen | None = None
        self.pid = 0
        self.main_hwnd = 0
        self.tracker: C.ProcessTracker | None = None
        self.injector = None
        self.cpu_samples: deque = deque(maxlen=30)
        self.input_events = 0
        self.input_log: deque = deque(maxlen=50)
        self.sent_bytes = 0
        self.sent_frames = 0
        self.t_start = time.time()

    # ---------------------------------------------------------------- KiCad process + windows
    def start_kicad(self) -> None:
        a = self.args
        if a.attach_pid:
            self.pid = a.attach_pid
        else:
            self.proc = W.launch_kicad(a.env, a.pcb, a.exe)
            self.pid = self.proc.pid
        main = W.wait_main_window(self.pid, a.title, timeout_s=120)
        self.main_hwnd = main.hwnd
        time.sleep(a.settle)
        if a.size:
            w, h = (int(v) for v in a.size.lower().split("x"))
            x, y = (int(v) for v in a.pos.split(","))
            W.resize_window(self.main_hwnd, x, y, w, h)
            time.sleep(0.5)
        if a.topmost:
            import win32con, win32gui
            win32gui.SetWindowPos(self.main_hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        self.injector = I.make_injector(a.inject, self.main_hwnd, self.pid)
        self.tracker = C.ProcessTracker(
            self.pid, self.main_hwnd, self._sink, self._on_windows, a.quality, a.fps,
            capture_children=not a.no_children,
        )
        self.tracker.start()
        print(f"[winstream] pid={self.pid} hwnd={self.main_hwnd} rect={W.frame_bounds(self.main_hwnd)} "
              f"dpi={W.dpi_for_window(self.main_hwnd)} inject={self.injector.name}", flush=True)

    def stop_kicad(self) -> None:
        if self.tracker:
            self.tracker.stop()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ---------------------------------------------------------------- frames -> clients
    def _sink(self, hwnd: int, jpeg: bytes, w: int, h: int, t_cap: float, enc_ms: float) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._publish, hwnd, jpeg, w, h, t_cap)

    def _publish(self, hwnd: int, jpeg: bytes, w: int, h: int, t_cap: float) -> None:
        self.seq += 1
        mx, my = self.tracker.main_rect[0], self.tracker.main_rect[1]
        rect = None
        for win in self.tracker.windows:
            if win.hwnd == hwnd:
                rect = win.rect
        if rect is None:
            rect = W.frame_bounds(hwnd)
        probe_id, t_inj = 0, 0.0
        if self.probe and hwnd == self.main_hwnd and t_cap >= self.probe[1]:
            probe_id, t_inj = self.probe
            self.probe = None
        header = struct.pack(HEADER_FMT, MAGIC, hwnd, self.seq, w, h, rect[0] - mx, rect[1] - my, t_cap, t_inj,
                             probe_id, 0)
        msg = header + jpeg
        for _ws, st in self.clients.items():
            st["pending"][hwnd] = msg
            st["event"].set()

    def _on_windows(self, wins: list[W.WinInfo]) -> None:
        if self.loop is None:
            return
        mx, my = self.tracker.main_rect[0], self.tracker.main_rect[1]
        payload = json.dumps({
            "t": "windows",
            "main": self.main_hwnd,
            "list": [
                {**w.as_dict(), "rel": [w.rect[0] - mx, w.rect[1] - my, w.width, w.height]} for w in wins
            ],
        })
        self.loop.call_soon_threadsafe(self._broadcast_text, payload)

    def _broadcast_text(self, text: str) -> None:
        for ws in list(self.clients):
            asyncio.ensure_future(ws.send_str(text))

    async def _sender(self, ws: web.WebSocketResponse, st: dict) -> None:
        try:
            while not ws.closed:
                await st["event"].wait()
                st["event"].clear()
                pending, st["pending"] = st["pending"], {}
                for msg in pending.values():
                    await ws.send_bytes(msg)
                    self.sent_bytes += len(msg)
                    self.sent_frames += 1
                    st["frames"] += 1
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    # ---------------------------------------------------------------- input <- clients
    def _handle_input(self, ev: dict) -> None:
        inj = self.injector
        mx, my = self.tracker.main_rect[0], self.tracker.main_rect[1]
        t = ev.get("t")
        self.input_events += 1
        if t != "move":
            self.input_log.append((round(time.time() % 1000, 3), t, ev.get("code") or ev.get("button"), ev.get("down"), ev.get("x"), ev.get("y")))
        sx = mx + int(round(ev.get("x", 0)))
        sy = my + int(round(ev.get("y", 0)))
        if ev.get("probe"):
            self.probe = (int(ev["probe"]), time.time() * 1000)
        if t == "move":
            inj.move(sx, sy)
        elif t == "down":
            # no ensure_foreground here: the injected click itself activates the window under it
            inj.button(sx, sy, int(ev.get("button", 0)), True)
        elif t == "up":
            inj.button(sx, sy, int(ev.get("button", 0)), False)
        elif t == "wheel":
            inj.wheel(sx, sy, int(ev.get("dx", 0)), int(ev.get("dy", 0)))
        elif t == "key":
            inj.key(ev.get("code", ""), bool(ev.get("down")))
        elif t == "focus":
            inj.ensure_foreground()
        if ev.get("probe"):
            # inject time is more useful than the ws receive time for the latency split
            self.probe = (int(ev["probe"]), time.time() * 1000)

    # ---------------------------------------------------------------- http handlers
    def _auth(self, request: web.Request) -> bool:
        return not self.args.token or request.query.get("token") == self.args.token

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if not self._auth(request):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=20)
        await ws.prepare(request)
        st = {"pending": {}, "event": asyncio.Event(), "frames": 0}
        self.clients[ws] = st
        sender = asyncio.ensure_future(self._sender(ws, st))
        mr = self.tracker.main_rect
        await ws.send_str(json.dumps({
            "t": "hello", "main": self.main_hwnd, "w": mr[2] - mr[0], "h": mr[3] - mr[1],
            "dpi": W.dpi_for_window(self.main_hwnd), "inject": self.injector.name, "quality": self.args.quality,
            "fps_cap": self.args.fps, "t_server": time.time() * 1000,
        }))
        self._on_windows(self.tracker.windows)
        # re-send the latest frame of every window so a new client is not blank until a change
        for h, s in self.tracker.sessions.items():
            if s.last_bgra is not None:
                import simplejpeg
                jpeg = simplejpeg.encode_jpeg(s.last_bgra, quality=self.args.quality, colorspace="BGRA", fastdct=True)
                self._publish(h, jpeg, s.last_bgra.shape[1], s.last_bgra.shape[0], time.time() * 1000)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    ev = json.loads(msg.data)
                    if ev.get("t") == "ping":
                        await ws.send_str(json.dumps({"t": "pong", "id": ev.get("id"), "t_server": time.time() * 1000}))
                    else:
                        self._handle_input(ev)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.clients.pop(ws, None)
            sender.cancel()
        return ws

    async def index(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC / "index.html")

    async def stats(self, request: web.Request) -> web.Response:
        tr = self.tracker
        now = time.time() * 1000
        sessions = {}
        for h, s in tr.sessions.items():
            st = s.stats
            recent = [a for a in st.arrivals if a > now - 1000]
            sessions[str(h)] = {
                "frames": st.frames, "bytes": st.bytes, "fps_1s": len(recent),
                "encode_ms_avg": round(sum(st.encode_ms) / len(st.encode_ms), 2) if st.encode_ms else None,
                "size": st.last_size, "error": st.error,
            }
        gpu = None
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            gpu = out
        except Exception:  # noqa: BLE001
            pass
        return web.json_response({
            "pid": self.pid, "main_hwnd": self.main_hwnd, "main_rect": tr.main_rect, "uptime_s": round(time.time() - self.t_start, 1),
            "windows": [w.as_dict() for w in tr.windows], "failed_windows": tr.failed,
            "fragile_seen": tr.fragile_seen, "monitor_session": tr.monitor is not None,
            "monitor_frames": tr.monitor.stats.frames if tr.monitor else None,
            "sessions": sessions, "sent_frames": self.sent_frames, "sent_bytes": self.sent_bytes,
            "clients": len(self.clients), "input_events": self.input_events, "input_log": list(self.input_log),
            "foreground_fixes": getattr(self.injector, "foreground_fixes", 0),
            "obscured_fixes": getattr(self.injector, "obscured_fixes", 0),
            "cpu": {"server_pct": self._cpu("self"), "kicad_pct": self._cpu("kicad"), "cores": psutil.cpu_count()},
            "gpu": gpu, "inject": self.injector.name, "quality": self.args.quality, "fps_cap": self.args.fps,
        })

    def _cpu(self, which: str) -> float | None:
        vals = [s[which] for s in self.cpu_samples if s.get(which) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    async def _cpu_sampler(self) -> None:
        me = psutil.Process()
        try:
            kc = psutil.Process(self.pid)
        except psutil.Error:
            kc = None
        me.cpu_percent(None)
        if kc:
            kc.cpu_percent(None)
        while True:
            await asyncio.sleep(1.0)
            try:
                self.cpu_samples.append({"self": me.cpu_percent(None), "kicad": kc.cpu_percent(None) if kc else None})
            except psutil.Error:
                pass

    async def shot(self, request: web.Request) -> web.Response:
        h = int(request.query.get("hwnd", self.main_hwnd))
        s = self.tracker.sessions.get(h)
        if s is None or s.last_bgra is None:
            raise web.HTTPNotFound()
        import cv2
        ok, png = cv2.imencode(".png", s.last_bgra[:, :, :3])
        return web.Response(body=png.tobytes(), content_type="image/png")

    async def cursor(self, request: web.Request) -> web.Response:
        import win32gui
        x, y = win32gui.GetCursorPos()
        mr = self.tracker.main_rect
        return web.json_response({"screen": [x, y], "frame": [x - mr[0], y - mr[1]], "main_rect": mr,
                                  "foreground_pid": W.foreground_pid(), "kicad_pid": self.pid})

    async def clipboard(self, request: web.Request) -> web.Response:
        import win32clipboard
        try:
            win32clipboard.OpenClipboard()
            try:
                text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except TypeError:
                text = None
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)})
        return web.json_response({"text": text[:400] if text else None, "len": len(text) if text else 0})

    async def quit(self, request: web.Request) -> web.Response:
        asyncio.get_event_loop().call_later(0.2, self._shutdown)
        return web.json_response({"ok": True})

    def _shutdown(self) -> None:
        self.stop_kicad()
        os._exit(0)

    # ---------------------------------------------------------------- app
    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/stats", self.stats)
        app.router.add_get("/shot", self.shot)
        app.router.add_post("/quit", self.quit)
        app.router.add_get("/cursor", self.cursor)
        app.router.add_get("/clipboard", self.clipboard)
        app.router.add_static("/static", STATIC)

        async def on_startup(app: web.Application) -> None:
            self.loop = asyncio.get_running_loop()
            asyncio.ensure_future(self._cpu_sampler())

        app.on_startup.append(on_startup)
        return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, help="env JSON from kicad_env.py")
    ap.add_argument("--pcb", required=True, help="document to open (scratch copy!)")
    ap.add_argument("--exe", default="pcbnew", help="key in env JSON: pcbnew | eeschema")
    ap.add_argument("--title", default="PCB Editor", help="substring of the main window title")
    ap.add_argument("--attach-pid", type=int, default=0)
    ap.add_argument("--size", default="", help="WxH physical px of the window frame ('' = leave as is)")
    ap.add_argument("--pos", default="60,60")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--fps", type=int, default=30, help="WGC minimum update interval cap (0 = uncapped)")
    ap.add_argument("--inject", choices=["sendinput", "postmessage"], default="sendinput")
    ap.add_argument("--topmost", action="store_true", help="keep the KiCad main window above other apps")
    ap.add_argument("--no-children", action="store_true", help="capture the main window only")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="")
    args = ap.parse_args(argv)

    W.set_dpi_aware()
    s = Streamer(args)
    s.start_kicad()
    app = s.make_app()
    try:
        web.run_app(app, host="127.0.0.1", port=args.port, print=lambda *_: None, handle_signals=True)
    finally:
        s.stop_kicad()
    return 0


if __name__ == "__main__":
    sys.exit(main())
