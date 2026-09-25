"""Unit tests (no KiCad) for the reviewer findings of 2026-09-20 on the live GUI session:

- finding 2: a file written on disk behind the bound GUI (the G16-guarded file writer, a SES
  import, a refill save) is reloaded into the editor (`RevertDocument`) before the next ipc11
  read or commit, so neither answers from a stale board and the editor's next save cannot undo
  the write; a save through the bound handle re-stamps the disk state;
- finding 9: a pcbnew session serves boards only (its API has no schematic handlers); the other
  document kind is "ipc11 unavailable" for the planner, and kipy's "no handler available" maps
  to E_UNSUPPORTED instead of E_INTERNAL;
- finding 4: `/control` replies are matched by the echoed request id, so kiwrap-live's `hello`
  (the first text frame on every connection) is never taken for the screenshot reply;
- finding 13: helper endpoints are loopback-only;
- finding 10 / 24: a record whose GUI pid is dead is forgotten with its lock files on discovery,
  and `kw_live detach` reports what really happened to the process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.ipc import gui_sessions
from kiwrap.backends.ipc import sessions as ipc_sessions
from kiwrap.core.tools import live as live_tool
from kiwrap.core.tools._base import to_kiwrap_error
from tests.test_live_tool_unit import Ctx, FakeHelper, FakeServices, FakeSession, _install, _png, _project


class _RevertingDoc:
    def __init__(self, log: list[str], name: str, fail: bool = False):
        self._log, self._name, self._fail = log, name, fail

    def revert(self) -> None:
        if self._fail:
            raise RuntimeError("a commit is open")
        self._log.append(self._name)


@dataclass
class BoundFake(FakeSession):
    """A FakeSession whose `board()` / `schematic()` record `RevertDocument` calls."""

    reverts: list[str] = field(default_factory=list)
    revert_fails: bool = False

    def board(self) -> Any:
        return _RevertingDoc(self.reverts, "board", self.revert_fails)

    def schematic(self) -> Any:
        return _RevertingDoc(self.reverts, "schematic", self.revert_fails)


def _write(path: Path, text: str) -> None:
    time.sleep(0.02)  # a distinct mtime on coarse file systems
    path.write_text(text, encoding="utf-8")


def test_bound_session_reloads_a_file_written_behind_the_gui(tmp_path):
    project = _project(tmp_path)
    install = _install(tmp_path / "kicad")
    session = BoundFake(install=install, project_dir=project)
    board = project / "demo.kicad_pcb"
    ipc_sessions.bind_gui_session(project, session)
    try:
        # the GUI just loaded the file: nothing to revert
        with ipc_sessions.pooled_session(install, board) as s:
            assert s is session
        assert session.reverts == []
        assert ipc_sessions.resync_bound_session(board) == {"changed": False, "reverted": []}
        # something wrote the board on disk behind the editor
        _write(board, "(kicad_pcb (version 20240108) (generator test) (footprint rotated))")
        r = ipc_sessions.resync_bound_session(board)
        assert r == {"changed": True, "reverted": ["board"]} and session.reverts == ["board"]
        assert ipc_sessions.SESSION_POOL.stats()["bound_reverts"] >= 1
        # stamped: the same disk state does not revert again
        assert ipc_sessions.resync_bound_session(board)["changed"] is False
        assert session.reverts == ["board"]
        # the pool's handle does the same check on acquire
        _write(board, "(kicad_pcb (version 20240108) (generator test) (footprint again))")
        with ipc_sessions.pooled_session(install, board):
            # the GUI "saves" through the handle: the disk changes under our own handle
            _write(board, "(kicad_pcb (version 20240108) (generator gui-save))")
        assert session.reverts == ["board", "board"]
        # the save through the handle was ours: no revert on the next acquire
        with ipc_sessions.pooled_session(install, board):
            pass
        assert session.reverts == ["board", "board"]
        # a refused revert is "ipc11 unavailable" for the planner (the file engine answers)
        session.revert_fails = True
        _write(board, "(kicad_pcb (version 20240108) (generator other))")
        with pytest.raises(ipc_sessions.BoundStale) as exc:
            ipc_sessions.pooled_session(install, board)
        err = to_kiwrap_error(exc.value)
        assert err.code == "E_UNSUPPORTED" and "file engine" in err.fix
    finally:
        ipc_sessions.unbind_gui_session(project)


def test_file_provider_write_resyncs_the_bound_gui(tmp_path):
    """`apply_outcome` after a file-provider write (a G16-guarded rotation) makes the bound GUI
    reload the board and says so in the receipt."""
    from kiwrap.core import planner
    from kiwrap.core.tools import _author
    from kiwrap.core.tools._base import Mutation

    project_dir = _project(tmp_path)
    install = _install(tmp_path / "kicad")
    session = BoundFake(install=install, project_dir=project_dir)
    board = project_dir / "demo.kicad_pcb"
    ipc_sessions.bind_gui_session(project_dir, session)
    try:
        from kiwrap.core.state.project import Project

        project = Project.from_path(project_dir)
        state = project.load_state()

        class _Ext:
            external_edit = False
            changed_files: list[str] = []

        m = Mutation(
            project=project,
            state=state,
            tool="kw_pcb_place",
            external=_Ext(),  # type: ignore[arg-type]
            bound={"session_id": "gfake0001"},
        )

        class Caps:
            def has(self, key: str) -> bool:
                return key in ("ipc.headless", "ipc.footprint_update_rotation_noise")

        _write(board, "(kicad_pcb (version 20240108) (generator file-writer) (footprint rotated))")
        outcome = planner.run("board.place", Caps(), {planner.FILE: lambda: "written"}, install=install)
        _author.apply_outcome(m, outcome, install, Ctx(services=FakeServices(tmp_path, session)))
        assert session.reverts == ["board"], "the file write went behind the GUI: it reloaded the board"
        assert m.receipt.data["live_view"] == {"reloaded": ["board"]}
        assert any("reloaded the file" in w for w in m.receipt.warnings)
        # an ipc11 outcome (the GUI wrote it itself) triggers nothing
        outcome2 = planner.run("board.place", Caps(), {planner.IPC11: lambda: "committed"}, install=install)
        _author.apply_outcome(m, outcome2, install, Ctx(services=FakeServices(tmp_path, session)))
        assert session.reverts == ["board"]
        # no binding: nothing happens either
        m.bound = None
        _write(board, "(kicad_pcb (version 20240108) (generator again))")
        _author.apply_outcome(m, outcome, install, Ctx(services=FakeServices(tmp_path, session)))
        assert session.reverts == ["board"]
    finally:
        ipc_sessions.unbind_gui_session(project_dir)


def test_bound_session_serves_only_its_own_document_kind(tmp_path):
    project = _project(tmp_path)
    (project / "demo.kicad_sch").write_text("(kicad_sch (version 20240101))", encoding="utf-8")
    install = _install(tmp_path / "kicad")
    session = BoundFake(install=install, project_dir=project, editor="pcb")
    ipc_sessions.bind_gui_session(project, session)
    try:
        assert ipc_sessions.bound_serves_kind(session, "pcb")
        assert not ipc_sessions.bound_serves_kind(session, "sch")
        assert ipc_sessions.bound_serves_kind(session, None)
        with pytest.raises(ipc_sessions.BoundElsewhere) as exc:
            ipc_sessions.pooled_session(install, project / "demo.kicad_sch")
        from kiwrap.core import planner

        err = to_kiwrap_error(exc.value)
        assert err.code == "E_UNSUPPORTED" and "schematic" in err.message and "file engine" in err.fix
        assert err.code in planner.UNAVAILABLE_CODES[planner.IPC11]
        assert not ipc_sessions.live_sessions(), "no headless server was started next to the GUI"
        # the board itself is still served by the GUI
        assert getattr(ipc_sessions.pooled_session(install, project / "demo.kicad_pcb"), "bound", False)
        # the mutation prologue still sees the binding for the directory (lock exemption)
        assert ipc_sessions.release_project(project)["bound"]["session_id"] == "gfake0001"
        session.editor = "project"
        assert ipc_sessions.bound_serves_kind(session, "sch")
    finally:
        ipc_sessions.unbind_gui_session(project)


def test_kipy_api_errors_map_to_planner_codes():
    from kipy.errors import ApiError

    unhandled = to_kiwrap_error(
        ApiError(
            "KiCad returned error: no handler available for request of type "
            "kiapi.common.commands.GetOpenDocuments"
        )
    )
    assert unhandled.code == "E_UNSUPPORTED" and "file engine" in unhandled.fix
    failed = to_kiwrap_error(ApiError("KiCad returned error: busy"))
    assert failed.code == "E_KICAD_FAILED"


class HelloFirstHelper(FakeHelper):
    """kiwrap-live's /control: a `hello` (carrying the surfaces) arrives before any reply, then
    a pushed event, then the answer echoing the request's id."""

    def _serve(self) -> None:  # type: ignore[override]
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    head += conn.recv(4096)
                key = [
                    ln.split(b":", 1)[1].strip()
                    for ln in head.split(b"\r\n")
                    if ln.lower().startswith(b"sec-websocket-key")
                ][0]
                accept = base64.b64encode(
                    hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
                )
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                )

                def text(o: dict[str, Any], c: Any = conn) -> None:
                    body = json.dumps(o).encode()
                    if len(body) < 126:
                        c.sendall(bytes([0x81, len(body)]) + body)
                    else:
                        c.sendall(bytes([0x81, 126]) + struct.pack(">H", len(body)) + body)

                text({"type": "hello", "version": "0.1.0", "inject": "none", "surfaces": [{"id": "pcb-1"}]})
                hdr = conn.recv(2)
                ln = hdr[1] & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", conn.recv(2))[0]
                mask = conn.recv(4)
                data = b""
                while len(data) < ln:
                    data += conn.recv(ln - len(data))
                req = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                self.requests.append(req)
                text({"type": "event", "event": "rect", "surface": {"id": "pcb-1", "kind": "pcb"}})
                text({**self.reply, "type": req.get("type"), "id": req.get("id")})
                try:
                    conn.settimeout(1.0)
                    conn.recv(64)
                except OSError:
                    pass


def test_ws_request_skips_the_hello_and_matches_the_reply_id():
    png = _png(3, 2)
    helper = HelloFirstHelper({"png": base64.b64encode(png).decode("ascii"), "ok": True})
    try:
        ep = gui_sessions.HelperEndpoint(port=helper.port, token="t")
        assert gui_sessions.helper_screenshot(ep, "pcb-1", max_px=200) == png
        assert helper.requests[-1]["type"] == "screenshot" and isinstance(helper.requests[-1]["id"], int)
    finally:
        helper.close()
    req = {"type": "screenshot", "id": 7}
    assert not gui_sessions._ws_reply_matches(req, {"type": "hello", "surfaces": []})
    assert not gui_sessions._ws_reply_matches(req, {"type": "screenshot", "id": 6})
    assert gui_sessions._ws_reply_matches(req, {"type": "screenshot", "id": 7})
    assert gui_sessions._ws_reply_matches(req, {"type": "screenshot", "png": "..."})  # a helper without ids
    assert gui_sessions._ws_reply_matches(req, {"type": "error", "error": "bad request"})
    assert not gui_sessions._ws_reply_matches(req, {"type": "event", "event": "create"})


def test_helper_endpoint_is_loopback_only():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert gui_sessions.HelperEndpoint(port=1, token="t", host=host).url("/control").startswith("ws://")
    assert gui_sessions.HelperEndpoint(port=1, token="t", host="::1").url("/x") == "ws://[::1]:1/x?token=t"
    with pytest.raises(ipc_sessions.SessionError):
        gui_sessions.HelperEndpoint(port=80, token="x", host="203.0.113.5")
    with pytest.raises(ipc_sessions.SessionError):
        gui_sessions.HelperEndpoint.from_json({"port": 80, "token": "x", "host": "example.com"})
    with pytest.raises(ValueError):
        live_tool.HelperArg(port=80, token="x", host="203.0.113.5")
    assert live_tool.HelperArg(port=80, token="x").host == "127.0.0.1"


def test_dead_gui_record_is_forgotten_with_its_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Finding 10: the extension's server died hard, the Job Object took the GUI, its record and
    ~*.lck stayed. Discovery (every tool call) and the mutation prologue clean it up."""
    monkeypatch.setattr(gui_sessions, "_records_dir", lambda temp_dir=None: tmp_path / "gui-sessions")
    project = _project(tmp_path)
    locks = [project / "~demo.kicad_pcb.lck", project / "~demo.kicad_pro.lck"]
    for lck in locks:
        lck.write_text("held by a dead GUI", encoding="utf-8")
    records = tmp_path / "gui-sessions"
    records.mkdir()
    rec = records / "999999997.json"
    rec.write_text(
        json.dumps(
            {
                "pid": 999999997,
                "parent_pid": 999999998,
                "session_id": "gdead",
                "exe": str(tmp_path / "bin" / "pcbnew.exe"),
                "document": str(project / "demo.kicad_pcb"),
                "project_dir": str(project),
                "socket": str(tmp_path / "s"),
                "install_root": str(tmp_path),
                "started": time.time(),
                "locks": [str(p) for p in locks],
            }
        ),
        encoding="utf-8",
    )
    assert gui_sessions.discover_remote_session(project) is None
    assert not rec.exists() and not any(p.exists() for p in locks)
    # and through the mutation prologue (release_project sweeps the GUI records too)
    rec.write_text(
        json.dumps({"pid": 999999997, "parent_pid": 1, "locks": [str(locks[0])]}), encoding="utf-8"
    )
    locks[0].write_text("again", encoding="utf-8")
    info = ipc_sessions.release_project(project)
    assert "bound" not in info and not rec.exists() and not locks[0].exists()
    from kiwrap.core.state.locks import refuse_if_editor_open
    from kiwrap.core.state.project import Project

    refuse_if_editor_open(Project.from_path(project))  # nothing left to refuse


def test_detach_reports_a_survivor_and_a_recycled_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Finding 24: the receipt's `closed` follows the process, not the object's `running`."""
    monkeypatch.setattr(gui_sessions, "_records_dir", lambda temp_dir=None: tmp_path / "gui-sessions")
    project = _project(tmp_path)
    install = _install(tmp_path / "kicad")
    monkeypatch.setattr("kiwrap.installation.find", lambda spec, *a, **k: install)
    records = tmp_path / "gui-sessions"
    records.mkdir()
    lck = project / "~demo.kicad_pcb.lck"
    lck.write_text("x", encoding="utf-8")
    # a live pid that is NOT our GUI (recycled: this interpreter's command line carries no pcbnew)
    rec = records / f"{os.getpid()}.json"
    info = {
        "pid": os.getpid(),
        "parent_pid": 1,
        "session_id": "grecycled",
        "exe": str(tmp_path / "bin" / "pcbnew.exe"),
        "document": str(project / "demo.kicad_pcb"),
        "project_dir": str(project),
        "socket": str(tmp_path / "s"),
        "install_root": str(tmp_path),
        "started": time.time(),
        "locks": [str(lck)],
    }
    rec.write_text(json.dumps(info), encoding="utf-8")
    remote = gui_sessions.RemoteGuiSession(rec, info, install)
    assert remote.close() is True, "a recycled pid means our GUI is gone: locks and record dropped"
    assert not rec.exists() and not lck.exists()

    class Survivor(gui_sessions.RemoteGuiSession):
        def close(self) -> bool:  # type: ignore[override]
            return False

    # kw_live detach on a session whose kill failed says so
    rec.write_text(json.dumps(info), encoding="utf-8")
    survivor = Survivor(rec, info, install)
    svc = FakeServices(tmp_path, None)
    svc.gui_find = lambda session_id, project_dir: survivor  # type: ignore[assignment]
    receipt = live_tool.run(Ctx(services=svc), live_tool.LiveArgs(action="detach", session_id="grecycled"))
    assert receipt.data["closed"] is False and receipt.data["ok"] is False
    assert any("survived" in w for w in receipt.warnings)


def test_helper_surface_id_resolves_kiwrap_ids_to_the_helpers():
    """Finding 4 (second half): kiwrap names frames `<kind>-<hwnd>`, the helper `pcb` / `pcb-2`;
    the screenshot must ask the helper with ITS id (matched by hwnd, then kind)."""
    from tests.test_live_tool_unit import FakeHelper

    helper = FakeHelper(
        {"surfaces": [{"id": "pcb", "kind": "pcb", "hwnd": 4660}, {"id": "3d", "kind": "3d", "hwnd": 99}]}
    )
    try:
        ep = gui_sessions.HelperEndpoint(port=helper.port, token="t")
        frame = gui_sessions.SurfaceInfo(
            id="pcb-1234", kind="pcb", title="x", hwnd=4660, rect_px=(0, 0, 1, 1), dpi=96, cloaked=False
        )
        assert gui_sessions.helper_surface_id(ep, None, frame) == "pcb"
        assert gui_sessions.helper_surface_id(ep, "pcb-1234", frame) == "pcb"
        assert gui_sessions.helper_surface_id(ep, "3d", None) == "3d"
        assert gui_sessions.helper_surface_id(ep, "pcb", None) == "pcb"
        other = gui_sessions.SurfaceInfo(
            id="sch-1", kind="sch", title="y", hwnd=1, rect_px=(0, 0, 1, 1), dpi=96, cloaked=False
        )
        assert gui_sessions.helper_surface_id(ep, None, other) == "pcb", "no match: the first surface"
    finally:
        helper.close()


def test_the_bound_record_names_the_guis_install_and_formats(tmp_path):
    """Review finding 7: the mutation prologue's `bound` record carries the GUI's install key and
    `formats.{sch,pcb}`, which the planner's ipc11 epoch precondition judges (the pool hands the
    GUI out whatever install a call asked for); a record without formats is not judged (None)."""

    @dataclass
    class WithCaps(FakeSession):
        capabilities: Any = None

    install = _install(tmp_path / "kicad")
    session = WithCaps(
        install=install, project_dir=_project(tmp_path), capabilities={"formats.sch": 20260830}
    )
    session.capabilities["formats.pcb"] = 20260901
    assert ipc_sessions._bound_install_key(session) == install.key()
    assert ipc_sessions._bound_formats(session) == {"sch": 20260830, "pcb": 20260901}
    assert ipc_sessions._bound_formats(WithCaps(install=install, project_dir=tmp_path)) is None
    assert (
        ipc_sessions._bound_formats(WithCaps(install=install, project_dir=tmp_path, capabilities={})) is None
    )
    assert ipc_sessions._bound_install_key(WithCaps(install=None, project_dir=tmp_path)) is None
