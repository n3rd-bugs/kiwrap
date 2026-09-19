"""winstream server: capture a KiCad GUI process (all its top-level windows) with WGC, stream JPEG
frames over a WebSocket to a browser page, inject the page's input back into the windows.

    python -m winstream.server --env env.json --pcb <scratch>/ecc83/ecc83-pp.kicad_pcb \
        --size 1920x1080 --pos 100,100 --quality 70 --fps 30 --inject sendinput --port 8765
    python -m winstream.server --env env.json --attach-pid <kicad.exe pid> --title KiCad   # full project session

Every top-level window of the process is captured and grouped into *surfaces* (one per editor
frame: pcb, sch, 3d, mgr, fp, sym; dialogs/chooser/menus ride with their owner frame). A client
subscribes to one surface with /ws?surface=pcb (no filter = everything relative to the main window).

Endpoints (127.0.0.1 only, `?token=` required on /ws when --token is given):
    GET /            viewer page (?surface=pcb|sch|3d|mgr)     GET /ws?surface=<kind>  stream + input
    GET /stats       JSON counters   GET /surfaces   surface list   GET /shot?hwnd=<n>   PNG of the last raw frame
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


def user32_is_enabled(hwnd: int) -> bool:
    import ctypes
    return bool(ctypes.windll.user32.IsWindowEnabled(hwnd))

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
        self.injectors: dict[int, object] = {}  # surface root hwnd -> injector
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
        if not a.attach_pid:
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
        self.injectors[self.main_hwnd] = self.injector
        self.tracker = C.ProcessTracker(
            self.pid, self.main_hwnd, self._sink, self._on_windows, a.quality, a.fps,
            capture_children=not a.no_children, use_hook=not a.no_hook, gather_dialogs=a.gather_dialogs,
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

    def _root_for_client(self, st: dict) -> int:
        kind = st.get("surface")
        if kind:
            return self.tracker.root_of_kind(kind) or 0
        return self.main_hwnd

    def _publish(self, hwnd: int, jpeg: bytes, w: int, h: int, t_cap: float) -> None:
        self.seq += 1
        tr = self.tracker
        rect = tr.rect_of(hwnd)
        kind = tr.surface_of_hwnd(hwnd)
        probe_id, t_inj = 0, 0.0
        if self.probe and (hwnd == self.main_hwnd or hwnd == self.probe[2]) and t_cap >= self.probe[1]:
            probe_id, t_inj = self.probe[0], self.probe[1]
            self.probe = None
        headers: dict[int, bytes] = {}
        for _ws, st in self.clients.items():
            if st.get("surface") and kind != st["surface"]:
                continue
            root = self._root_for_client(st)
            if root not in headers:
                rr = tr.rect_of(root) if root else (0, 0, 0, 0)
                headers[root] = struct.pack(HEADER_FMT, MAGIC, hwnd, self.seq, w, h, rect[0] - rr[0], rect[1] - rr[1],
                                            t_cap, t_inj, probe_id, 0) + jpeg
            st["pending"][hwnd] = headers[root]
            st["event"].set()

    def _surfaces_summary(self) -> list[dict]:
        tr = self.tracker
        out = []
        for root, info in tr.surfaces.items():
            rr = tr.rect_of(root)
            top_enabled = 0
            for w in tr.windows:
                if w.root == root and user32_is_enabled(w.hwnd):
                    top_enabled = w.hwnd
                    break
            out.append({"kind": info["kind"], "root": root, "title": info["title"], "rect": list(rr),
                        "hwnds": info["hwnds"], "focus_target": top_enabled,
                        "foreground": tr.foreground_hwnd in info["hwnds"]})
        return out

    def _windows_payload(self, st: dict, wins: list[W.WinInfo]) -> str:
        root = self._root_for_client(st)
        rr = self.tracker.rect_of(root) if root else (0, 0, 0, 0)
        kind = st.get("surface")
        sel = [w for w in wins if not kind or w.surface == kind]
        return json.dumps({
            "t": "windows", "main": root, "surface": kind or "", "root_rect": list(rr),
            "surfaces": self._surfaces_summary(),
            "list": [{**w.as_dict(), "rel": [w.rect[0] - rr[0], w.rect[1] - rr[1], w.width, w.height]} for w in sel],
        })

    def _on_windows(self, wins: list[W.WinInfo]) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._broadcast_windows, wins)

    def _broadcast_windows(self, wins: list[W.WinInfo]) -> None:
        for _ws, st in list(self.clients.items()):
            self._queue_text(st, self._windows_payload(st, wins))

    @staticmethod
    def _queue_text(st: dict, text: str) -> None:
        # One writer per client: an aiohttp WebSocketResponse is not safe for interleaved send_str /
        # send_bytes from different tasks (measured in VS Code: close code 1002 when a windows
        # payload raced a frame). Everything goes through the client's sender coroutine.
        st["texts"].append(text)
        st["event"].set()

    async def _sender(self, ws: web.WebSocketResponse, st: dict) -> None:
        try:
            while not ws.closed:
                await st["event"].wait()
                st["event"].clear()
                while st["texts"]:
                    await ws.send_str(st["texts"].popleft())
                pending, st["pending"] = st["pending"], {}
                for msg in pending.values():
                    await ws.send_bytes(msg)
                    self.sent_bytes += len(msg)
                    self.sent_frames += 1
                    st["frames"] += 1
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    # ---------------------------------------------------------------- input <- clients
    def _injector_for(self, root: int):
        """One injector per surface root; its `main_hwnd` (used for raise/foreground) is pointed at
        the surface's topmost ENABLED window so a modal dialog gets the keys, not the disabled frame."""
        inj = self.injectors.get(root)
        if inj is None:
            inj = I.make_injector(self.args.inject, root, self.pid)
            self.injectors[root] = inj
        target = root
        hwnds = set()
        for w in self.tracker.windows:  # z-order, top first
            if w.root == root:
                hwnds.add(w.hwnd)
                if target == root and user32_is_enabled(w.hwnd):
                    target = w.hwnd
        inj.main_hwnd = target
        inj.surface_hwnds = hwnds or {root}
        return inj

    def _handle_input(self, ev: dict, st: dict | None = None) -> None:
        root = self._root_for_client(st) if st else self.main_hwnd
        if not root:
            return
        inj = self._injector_for(root)
        mx, my = self.tracker.rect_of(root)[:2]
        t = ev.get("t")
        self.input_events += 1
        if t != "move":
            self.input_log.append((round(time.time() % 1000, 3), (st or {}).get("surface") or "main", t, ev.get("code") or ev.get("button"), ev.get("down"), ev.get("x"), ev.get("y")))
        sx = mx + int(round(ev.get("x", 0)))
        sy = my + int(round(ev.get("y", 0)))
        if ev.get("probe"):
            self.probe = (int(ev["probe"]), time.time() * 1000, root)
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
            self.probe = (int(ev["probe"]), time.time() * 1000, root)

    # ---------------------------------------------------------------- http handlers
    def _auth(self, request: web.Request) -> bool:
        return not self.args.token or request.query.get("token") == self.args.token

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if not self._auth(request):
            raise web.HTTPForbidden()
        # compress=False: a VS Code webview (Electron) sent permessage-deflate frames (RSV1 set) that
        # aiohttp rejected with 1002 "non-zero reserved bits" on the first larger input burst; JPEG
        # frames do not compress anyway, so the extension is refused at the handshake.
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=20, compress=False)
        await ws.prepare(request)
        st = {"pending": {}, "texts": deque(), "event": asyncio.Event(), "frames": 0, "surface": request.query.get("surface", "") or ""}
        self.clients[ws] = st
        sender = asyncio.ensure_future(self._sender(ws, st))
        root = self._root_for_client(st)
        mr = self.tracker.rect_of(root) if root else (0, 0, 0, 0)
        self._queue_text(st, json.dumps({
            "t": "hello", "main": root, "surface": st["surface"], "w": mr[2] - mr[0], "h": mr[3] - mr[1],
            "dpi": W.dpi_for_window(root) if root else 0, "inject": self.injector.name, "quality": self.args.quality,
            "fps_cap": self.args.fps, "t_server": time.time() * 1000, "pid": self.pid,
            "surfaces": self._surfaces_summary(),
        }))
        self._queue_text(st, self._windows_payload(st, self.tracker.windows))
        # re-send the latest frame of every window so a new client is not blank until a change
        for h, s in list(self.tracker.sessions.items()):
            if s.last_bgra is not None and (not st["surface"] or self.tracker.surface_of_hwnd(h) == st["surface"]):
                import simplejpeg
                jpeg = simplejpeg.encode_jpeg(s.last_bgra, quality=self.args.quality, colorspace="BGRA", fastdct=True)
                self._publish(h, jpeg, s.last_bgra.shape[1], s.last_bgra.shape[0], time.time() * 1000)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    ev = json.loads(msg.data)
                    if ev.get("t") == "ping":
                        self._queue_text(st, json.dumps({"t": "pong", "id": ev.get("id"), "t_server": time.time() * 1000}))
                    else:
                        self._handle_input(ev, st)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    print(f"[winstream] ws {st['surface'] or 'main'} {msg.type.name} exc={ws.exception()!r} data={msg.data!r}", file=sys.stderr, flush=True)
                    break
        except Exception as e:  # noqa: BLE001
            print(f"[winstream] ws {st['surface'] or 'main'} handler error: {e!r}", file=sys.stderr, flush=True)
            raise
        finally:
            print(f"[winstream] ws {st['surface'] or 'main'} closed code={ws.close_code} frames={st['frames']}", file=sys.stderr, flush=True)
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
                "surface": tr.surface_of_hwnd(h), "frames": st.frames, "bytes": st.bytes, "fps_1s": len(recent),
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
            "clients": [st.get("surface") or "" for st in self.clients.values()], "input_events": self.input_events, "input_log": list(self.input_log),
            "surfaces": self._surfaces_summary(), "foreground_hwnd": tr.foreground_hwnd,
            "foreground_log": list(tr.foreground_log), "hook_events": tr.hook_events, "rescans": tr.rescans,
            "hook_to_list_ms": list(tr.hook_to_list_ms), "gathered": tr.gathered,
            "hook_log": list(tr.hook.log) if tr.hook else [],
            "foreground_fixes": sum(getattr(i, "foreground_fixes", 0) for i in self.injectors.values()),
            "inject_timeouts": sum(getattr(i, "timeouts", 0) for i in self.injectors.values()),
            "obscured_fixes": sum(getattr(i, "obscured_fixes", 0) for i in self.injectors.values()),
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

    async def surfaces(self, request: web.Request) -> web.Response:
        return web.json_response({"pid": self.pid, "surfaces": self._surfaces_summary(),
                                  "windows": [w.as_dict() for w in self.tracker.windows]})

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
        app.router.add_get("/surfaces", self.surfaces)
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
    ap.add_argument("--pcb", default="", help="document to open (scratch copy!); not needed with --attach-pid")
    ap.add_argument("--exe", default="pcbnew", help="pcbnew | eeschema | kicad (project manager; --pcb is then the .kicad_pro)")
    ap.add_argument("--title", default="PCB Editor", help="substring of the main window title (kicad manager: 'KiCad')")
    ap.add_argument("--no-hook", action="store_true", help="poll EnumWindows only (no SetWinEventHook)")
    ap.add_argument("--gather-dialogs", action="store_true",
                    help="move an owned dialog/chooser that opens outside its editor frame onto the frame (once, on show)")
    ap.add_argument("--attach-pid", type=int, default=0)
    ap.add_argument("--size", default="", help="WxH physical px of the window frame ('' = leave as is)")
    ap.add_argument("--pos", default="60,60")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--fps", type=int, default=30, help="WGC minimum update interval cap (0 = uncapped)")
    ap.add_argument("--inject", choices=["sendinput", "postmessage", "hybrid"], default="sendinput",
                    help="hybrid = real cursor (SetCursorPos) + posted buttons/keys: no focus change")
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
