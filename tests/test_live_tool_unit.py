"""Unit tests (no KiCad) for `kw_live`, the GUI supervisor's pure parts and the bound-session
mechanics of `kiwrap.backends.ipc.sessions`: a fake `GuiSession` stands in for the process, a
tiny RFC 6455 server stands in for the display helper's `/control` channel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.ipc import gui_sessions
from kiwrap.backends.ipc import sessions as ipc_sessions
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.tools import live as live_tool
from kiwrap.installation import KiCadInstallation

# ---- fakes ---------------------------------------------------------------------------------------------


def _install(root: Path, version: str = "10.99.0", **extra: str) -> KiCadInstallation:
    return KiCadInstallation(
        root=root,
        kicad_cli=root / "bin" / "kicad-cli.exe",
        share=root / "share" / "kicad",
        version=version,
        source="explicit",
        extra=dict(extra),
    )


@dataclass
class FakeSession:
    install: Any
    project_dir: Path
    editor: str = "pcb"
    mode: str = "view"
    session_id: str = "gfake0001"
    token: str = "tok"
    running: bool = True
    pid: int | None = 4242
    mode_used: str = "view"
    cloaked: bool | None = False
    helper: Any = None
    locks_taken: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    closed: int = 0
    raised: list[str | None] = field(default_factory=list)
    shot: tuple[bytes, dict[str, Any]] | None = None

    @property
    def socket_url(self) -> str:
        return "ipc://fake/kicad/api.sock"

    @property
    def file(self) -> Path:
        return self.project_dir / "demo.kicad_pcb"

    @property
    def files(self) -> list[Path]:
        return [self.file]

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "alive": self.running,
            "pid": self.pid,
            "kicad": self.install.version,
            "install": self.install.key(),
            "editor": self.editor,
            "mode": self.mode,
            "mode_used": self.mode_used,
            "cloaked": self.cloaked,
            "project": str(self.project_dir),
            "api_socket": self.socket_url,
            "token": self.token,
            "editors": [
                {
                    "id": "pcb-1",
                    "kind": "pcb",
                    "title": "demo",
                    "hwnd": 1,
                    "rect_px": [0, 0, 8, 8],
                    "dpi": 96,
                    "cloaked": False,
                }
            ],
            "helper": self.helper.to_json() if self.helper else None,
            "locks": [p.name for p in self.locks_taken],
            "warnings": list(self.warnings),
        }

    def attach_helper(self, endpoint: dict[str, Any]) -> gui_sessions.HelperEndpoint:
        self.helper = gui_sessions.HelperEndpoint(port=int(endpoint["port"]), token=str(endpoint["token"]))
        return self.helper

    def raise_frame(self, kind: str | None = None) -> dict[str, Any]:
        self.raised.append(kind)
        return {"raised": True, "surface": self.status()["editors"][0]}

    def screenshot(self, *, surface: str | None = None, max_px: int = 1024) -> tuple[bytes, dict[str, Any]]:
        if self.shot is None:
            raise gui_sessions.GuiSessionError("no window to capture", fix="render")
        return self.shot

    def close(self) -> None:
        self.closed += 1
        self.running = False


class FakeServices:
    """Only what `kw_live` touches; the `gui_*` bindings are looked up by name on this object."""

    def __init__(self, tmp: Path, session: FakeSession | None = None):
        self.storage_home = tmp / "home"
        self.storage_home.mkdir(parents=True, exist_ok=True)
        self._session = session
        self.launched: list[dict[str, Any]] = []
        self.inst = _install(tmp / "kicad")

    # kw_live's lookups
    def gui_launch(self, install: Any, project_dir: Path, **kw: Any) -> FakeSession:
        self.launched.append({"install": install, "project_dir": project_dir, **kw})
        assert self._session is not None
        self._session.project_dir = Path(project_dir)
        self._session.running = True
        return self._session

    def gui_find(self, session_id: str | None, project_dir: Path | None) -> FakeSession | None:
        s = self._session
        if s is None or not s.running:
            return None
        if session_id and s.session_id != session_id:
            return None
        if project_dir is not None and Path(project_dir) != s.project_dir:
            return None
        return s

    def gui_sessions(self) -> list[FakeSession]:
        return [self._session] if self._session and self._session.running else []

    def gui_input_from_events(self, install: Any, caps: Any) -> bool:
        return False

    # the generic services kw_live's launch uses (effective_target, D21: the installs and their
    # format epochs; these caps name none, so the fake install counts as writing any format)
    def install(self, spec: str | None) -> Any:
        return self.inst

    def installs(self) -> list[Any]:
        return [self.inst]

    class _Caps:
        def has(self, key: str) -> bool:
            return key in ("ipc.headless", "ipc.schematic")

    def caps(self, install: Any) -> Any:
        return self._Caps()

    def sandbox_ensure(self, install: Any, **kw: Any) -> dict[str, Any]:
        assert kw.get("enable_api_server") is True, "the GUI root must have the API flag on"
        return {"install": install.version, **kw}


@dataclass
class Ctx:
    services: Any
    jobs: Any = None
    server_version: str = "test"


def _project(tmp: Path) -> Path:
    d = tmp / "demo"
    d.mkdir()
    (d / "demo.kicad_pro").write_text("(kicad_project (version 1))", encoding="utf-8")
    (d / "demo.kicad_pcb").write_text("(kicad_pcb (version 20240108) (generator test))", encoding="utf-8")
    return d


def _png(w: int = 4, h: int = 3) -> bytes:
    return gui_sessions.png_encode(w, h, bytes([10, 20, 30, 255]) * (w * h))


# ---- png / capability ---------------------------------------------------------------------------------


def test_png_encode_is_a_valid_rgb_png():
    png = _png(5, 2)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    length, tag = struct.unpack(">I4s", png[8:16])
    assert tag == b"IHDR" and length == 13
    w, h, depth, ctype = struct.unpack(">IIBB", png[16:26])
    assert (w, h, depth, ctype) == (5, 2, 8, 2)
    from kiwrap.core.state.receipts import png_size

    assert png_size(png) == (5, 2)
    idat_len = struct.unpack(">I", png[33:37])[0]
    assert png[37:41] == b"IDAT"
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert raw == (b"\x00" + bytes([30, 20, 10]) * 5) * 2  # BGRA -> RGB, filter byte per row
    with pytest.raises(ValueError):
        gui_sessions.png_encode(4, 4, b"\x00" * 8)


def test_input_from_events_is_presence_based(tmp_path, monkeypatch):
    """One reader for `gui.input_from_events`: the manifest's builds[] entry of the BUILT commit
    (capability.build_carries_patch). The install's own kiwrap-install.json claim is not a
    signal, so kw_env, kw_live mode=hidden and the receipt's hidden_available agree (D20)."""
    import kiwrap.capability as capability

    stock = _install(tmp_path / "stock")
    assert gui_sessions.input_from_events(stock) is False

    # the install's own claim alone (a re-registered bench build whose manifest entry dropped
    # the patch, like 6dd893f7d1-7 after D20) does not count
    claimed = _install(
        tmp_path / "bench", kiwrap_patches="ratsnest-export,display-input", commit="deadbeef00"
    )
    assert gui_sessions.input_from_events(claimed) is False
    assert capability.build_carries_patch(claimed, "display-input") is False

    class Caps:
        def has(self, key: str) -> bool:
            return key == gui_sessions.GUI_CAPABILITY_KEY

    assert gui_sessions.input_from_events(stock, Caps()) is True

    # a manifest build entry naming the patch for the BUILT commit is the signal
    manifest = {
        "schema": capability.PATCHES_SCHEMA,
        "base": "6dd893f7d1",
        "patches": [],
        "builds": [{"tag": "kiwrap-build/test-1", "commit": "deadbeef00", "patches": ["display-input"]}],
    }
    monkeypatch.setattr(capability, "load_patch_manifest", lambda: manifest)
    by_commit = _install(tmp_path / "bench2", commit="deadbeef00")
    assert gui_sessions.input_from_events(by_commit) is True
    assert gui_sessions.input_from_events(claimed) is True  # same commit, now listed
    assert gui_sessions.input_from_events(_install(tmp_path / "bench3", commit="0000000000")) is False

    # the live manifest: no registered build lists display-input (D20)
    monkeypatch.undo()
    live = capability.load_patch_manifest()
    assert not [b["tag"] for b in live.get("builds", []) if "display-input" in (b.get("patches") or [])]


def test_helper_endpoint_never_leaks_the_token():
    ep = gui_sessions.HelperEndpoint(port=5, token="secret", version="1")
    assert ep.url("/control") == "ws://127.0.0.1:5/control?token=secret"
    assert ep.to_json()["token"] == "***" and "secret" not in json.dumps(ep.to_json())


# ---- the helper's control channel: a fake RFC 6455 server ----------------------------------------------


class FakeHelper:
    """Accepts one WebSocket connection at a time on 127.0.0.1, records the request, answers it."""

    def __init__(self, reply: dict[str, Any] | None = None):
        self.reply = reply or {}
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    head += conn.recv(4096)
                request_line = head.split(b"\r\n", 1)[0].decode()
                self.paths.append(request_line.split(" ")[1])
                key = [
                    ln.split(b":", 1)[1].strip()
                    for ln in head.split(b"\r\n")
                    if ln.lower().startswith(b"sec-websocket-key")
                ][0]
                accept = base64.b64encode(
                    hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
                )
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                    + accept
                    + b"\r\n\r\n"
                )
                hdr = conn.recv(2)
                ln = hdr[1] & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", conn.recv(2))[0]
                mask = conn.recv(4)
                data = b""
                while len(data) < ln:
                    data += conn.recv(ln - len(data))
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                req = json.loads(payload)
                self.requests.append(req)
                reply = dict(self.reply)
                reply.setdefault("type", req.get("type"))
                body = json.dumps(reply).encode()
                # a ping first: the client must answer it and keep waiting for the text frame
                conn.sendall(bytes([0x89, 0x02]) + b"hi")
                if len(body) < 126:
                    conn.sendall(bytes([0x81, len(body)]) + body)
                else:
                    conn.sendall(bytes([0x81, 126]) + struct.pack(">H", len(body)) + body)
                try:
                    conn.settimeout(1.0)
                    conn.recv(64)  # the client's pong / close
                except OSError:
                    pass

    def close(self) -> None:
        self.sock.close()


def test_helper_screenshot_over_websocket():
    png = _png()
    helper = FakeHelper({"png": base64.b64encode(png).decode("ascii")})
    try:
        ep = gui_sessions.HelperEndpoint(port=helper.port, token="t0k", version="0.1")
        out = gui_sessions.helper_screenshot(ep, "pcb-1", max_px=640)
        assert out == png
        assert helper.paths == ["/control?token=t0k"]
        # every request carries a correlation id (the reply is matched by it: finding 4)
        assert [{k: v for k, v in r.items() if k != "id"} for r in helper.requests] == [
            {"type": "screenshot", "surface": "pcb-1", "max_px": 640}
        ]
        assert isinstance(helper.requests[0]["id"], int)
    finally:
        helper.close()


def test_helper_screenshot_errors_are_teaching_errors():
    helper = FakeHelper({"error": "no such surface"})
    try:
        ep = gui_sessions.HelperEndpoint(port=helper.port, token="t")
        with pytest.raises(gui_sessions.GuiSessionError, match="no such surface"):
            gui_sessions.helper_screenshot(ep, "nope")
    finally:
        helper.close()
    with pytest.raises((gui_sessions.GuiSessionError, OSError)):
        gui_sessions.helper_screenshot(gui_sessions.HelperEndpoint(port=1, token="t"), "pcb", max_px=100)


# ---- bound sessions in the pool -------------------------------------------------------------------------


def test_bound_session_replaces_headless_servers(tmp_path):
    project = _project(tmp_path)
    install = _install(tmp_path / "kicad")
    session = FakeSession(install=install, project_dir=project, locks_taken=[project / "~demo.kicad_pcb.lck"])
    assert ipc_sessions.bound_session_for(project / "demo.kicad_pcb") is None
    ipc_sessions.bind_gui_session(project, session)
    try:
        assert ipc_sessions.bound_session_for(project / "demo.kicad_pcb") is session
        assert ipc_sessions.bound_session_for(project) is session
        assert ipc_sessions.bound_session_for(tmp_path / "elsewhere.kicad_pcb") is None
        handle = ipc_sessions.pooled_session(install, project / "demo.kicad_pcb")
        assert getattr(handle, "bound", False) is True and handle.pid == 4242
        with handle as s:
            assert s is session
        handle.close()
        assert session.running and session.closed == 0, "the pool never closes a bound GUI"
        assert not ipc_sessions.live_sessions()
        info = ipc_sessions.release_project(project)
        assert info["bound"]["session_id"] == "gfake0001"
        assert info["bound"]["locks"] == [str(project / "~demo.kicad_pcb.lck")]
        assert session.closed == 0
        stats = ipc_sessions.SESSION_POOL.stats()
        assert stats["bound_hits"] >= 1 and stats["bound"][0]["pid"] == 4242
        # a second live session on the same project is refused
        other = FakeSession(install=install, project_dir=project, session_id="gother")
        with pytest.raises(ipc_sessions.SessionError):
            ipc_sessions.bind_gui_session(project, other)
        # a dead binding is dropped silently
        session.running = False
        assert ipc_sessions.bound_session_for(project) is None
        assert "bound" not in ipc_sessions.release_project(project)
    finally:
        ipc_sessions.unbind_gui_session(project)
    assert ipc_sessions.bound_session_for(project) is None


# ---- kw_live ---------------------------------------------------------------------------------------------


def test_kw_live_launch_status_attach_raise_detach(tmp_path):
    project = _project(tmp_path)
    session = FakeSession(install=_install(tmp_path / "kicad"), project_dir=project)
    svc = FakeServices(tmp_path, session)
    ctx = Ctx(services=svc)
    launched = live_tool.run(ctx, live_tool.LiveArgs(action="launch", project=str(project), editor="pcb"))
    assert launched.data["ok"] and launched.data["session_id"] == "gfake0001"
    assert launched.data["pid"] == 4242 and launched.data["api_socket"] == "ipc://fake/kicad/api.sock"
    assert launched.data["token"] == "tok" and launched.data["hidden_available"] is False
    assert launched.data["editors"][0]["kind"] == "pcb" and launched.provider_used == "gui:10.99.0"
    assert svc.launched[0]["editor"] == "pcb" and svc.launched[0]["mode"] == "view"
    assert svc.launched[0]["config_root"]["enable_api_server"] is True

    st = live_tool.run(ctx, live_tool.LiveArgs(action="status"))
    assert st.data["alive"] and st.data["session_id"] == "gfake0001"
    st2 = live_tool.run(ctx, live_tool.LiveArgs(action="status", project=str(project)))
    assert st2.data["alive"]

    with pytest.raises(KiwrapError) as exc:
        live_tool.run(ctx, live_tool.LiveArgs(action="attach", session_id="gfake0001"))
    assert exc.value.code == "E_BAD_ARGS"
    att = live_tool.run(
        ctx,
        live_tool.LiveArgs(
            action="attach", session_id="gfake0001", helper=live_tool.HelperArg(port=9, token="s3")
        ),
    )
    assert att.data["attached"] and att.data["helper"] == {
        "port": 9,
        "version": "",
        "host": "127.0.0.1",
        "pid": None,
        "token": "***",
    }
    assert "s3" not in json.dumps(att.data)

    raised = live_tool.run(ctx, live_tool.LiveArgs(action="raise", surface="pcb"))
    assert raised.data["raised"] is True and session.raised == ["pcb"]

    det = live_tool.run(ctx, live_tool.LiveArgs(action="detach", session_id="gfake0001"))
    assert det.data["closed"] and det.data["ok"] and session.closed == 1
    with pytest.raises(KiwrapError) as exc2:
        live_tool.run(ctx, live_tool.LiveArgs(action="status"))
    assert exc2.value.code == "E_BAD_ARGS" and "no live GUI session" in exc2.value.message


def test_kw_live_launch_needs_a_project(tmp_path):
    ctx = Ctx(
        services=FakeServices(tmp_path, FakeSession(install=_install(tmp_path / "k"), project_dir=tmp_path))
    )
    with pytest.raises(KiwrapError) as exc:
        live_tool.run(ctx, live_tool.LiveArgs(action="launch"))
    assert exc.value.code == "E_BAD_ARGS" and exc.value.example


def test_kw_live_screenshot_live_and_render_fallback(tmp_path, monkeypatch):
    project = _project(tmp_path)
    session = FakeSession(install=_install(tmp_path / "kicad"), project_dir=project)
    ctx = Ctx(services=FakeServices(tmp_path, session))
    session.shot = (_png(6, 4), {"source": "window", "surface": {"kind": "pcb"}})
    shot = live_tool.run(ctx, live_tool.LiveArgs(action="screenshot", max_px=256))
    assert shot.image == _png(6, 4) and shot.data["mode"] == "live" and shot.data["source"] == "window"
    assert shot.data["width"] == 6 and shot.data["height"] == 4 and shot.data["bytes"] == len(shot.image)

    # over-budget image: a warning, never an error
    big = _png(1024, 700)
    session.shot = (big, {"source": "helper", "surface": "pcb-1"})
    shot2 = live_tool.run(ctx, live_tool.LiveArgs(action="screenshot"))
    assert shot2.data["mode"] == "live" and (
        len(big) <= live_tool.TARGET_BYTES or any("bytes" in w for w in shot2.warnings)
    )

    # no live picture: the saved file is rendered (kw_pcb_render machinery, faked here)
    session.shot = None
    calls: list[Any] = []

    def fake_pcb_render(ctx_: Any, args: Any) -> Receipt:
        calls.append(args)
        return Receipt(provider_used="cli:10.99.0", data={"ok": True, "mode": "2d"}, image=_png(2, 2))

    from kiwrap.core.tools import render

    monkeypatch.setattr(render, "pcb_render", fake_pcb_render)
    fb = live_tool.run(ctx, live_tool.LiveArgs(action="screenshot", max_px=300))
    assert (
        "live_view_unavailable" in fb.degraded
        and fb.data["mode"] == "render"
        and fb.data["source"] == "render"
    )
    assert fb.image == _png(2, 2) and calls[0].max_px == 300 and calls[0].view == "2d"
    assert any("live view unavailable" in w for w in fb.warnings)
    assert fb.data["session_id"] == "gfake0001"


def test_kw_live_maps_backend_errors_to_catalogue_codes(tmp_path):
    project = _project(tmp_path)
    svc = FakeServices(tmp_path, FakeSession(install=_install(tmp_path / "k"), project_dir=project))

    def busy(*a: Any, **k: Any) -> Any:
        raise gui_sessions.GuiBusyError("a GUI session is already live", why="one per agent", fix="detach")

    svc.gui_launch = busy  # type: ignore[method-assign]
    ctx = Ctx(services=svc)
    with pytest.raises(KiwrapError) as exc:
        live_tool.run(ctx, live_tool.LiveArgs(action="launch", project=str(project)))
    assert (
        exc.value.code == "E_BUSY"
        and exc.value.fix == "detach"
        and exc.value.data["backend_code"] == "E_GUI_BUSY"
    )

    def unsupported(*a: Any, **k: Any) -> Any:
        raise gui_sessions.GuiUnsupportedError("cannot run hidden", fix="mode='view'")

    svc.gui_launch = unsupported  # type: ignore[method-assign]
    with pytest.raises(KiwrapError) as exc2:
        live_tool.run(ctx, live_tool.LiveArgs(action="launch", project=str(project), mode="hidden"))
    assert exc2.value.code == "E_UNSUPPORTED"

    def start_failed(*a: Any, **k: Any) -> Any:
        raise ipc_sessions.SessionStartError("pcbnew exited", details="stderr...")

    svc.gui_launch = start_failed  # type: ignore[method-assign]
    with pytest.raises(KiwrapError) as exc3:
        live_tool.run(ctx, live_tool.LiveArgs(action="launch", project=str(project)))
    assert exc3.value.code == "E_KICAD_FAILED" and exc3.value.data["details"] == "stderr..."


def test_kw_live_is_registered_with_a_help_page():
    from kiwrap.core.tools import TOOL_NAMES
    from kiwrap.core.tools import help as help_tool

    assert "kw_live" in TOOL_NAMES and TOOL_NAMES[-1] == "kw_live"
    assert "humans" in help_tool.TOOL_PAGES["kw_live"].lower()
    text, cands = help_tool.page("kw_live")
    assert text.startswith("# kw_live") and not cands
    assert live_tool.SPEC.annotations.destructive_hint is True  # it starts and kills processes
    fields = live_tool.LiveArgs.model_fields
    assert set(fields) >= {"action", "project", "session_id", "editor", "mode", "helper", "surface", "max_px"}
