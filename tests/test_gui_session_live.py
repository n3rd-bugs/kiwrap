"""Live GUI session tests (`kiwrap.backends.ipc.gui_sessions` + `kw_live`): a sandboxed
`pcbnew.exe` on a scratch copy of the ecc83 demo, its API bound to the project, a footprint moved
through the bound session (the tool path on a KiCad with `ipc.headless`, the IPC twin directly on
older ones - the planner's ipc11 predicate does not know about bound sessions yet, see the module
docstring of `kw_live`), the screenshot paths, and a clean detach.

Marked `gui`: one KiCad GUI at a time (a cross-process lock under the platform temp dir serialises
every module that launches one), always sandboxed (`kiwrap.sandbox.ensure(enable_api_server=True)`,
a per-session TEMP, no activation), always killed by PID; the user's `%APPDATA%/kicad` hash is
asserted unchanged. Run with `-p no:xdist`.

The hidden-mode test is opt-in (`KIWRAP_E2E_HIDDEN=1`; docs/DECISIONS.md D20 parked the cloaked
mode) and needs a bench build carrying the `display-input` patch (the manifest's `builds[]` entry
of its commit must list it - `src/kiwrap/data/kiwrap_patches.json`, the only presence signal; the
install's own `kiwrap-install.json` claim does not count - and `KIWRAP_KICAD_ROOTS` or
`<storage>/installs.json` must name the root): the frame's
`DWMWA_CLOAKED` is read with ctypes here, independently of the supervisor.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.ipc import gui_sessions
from kiwrap.backends.ipc import sessions as ipc_sessions
from kiwrap.capability import capabilities_for
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.locks import ProjectLock
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.tools import live as live_tool
from kiwrap.core.tools._base import ToolContext
from kiwrap.platform import current
from kiwrap.sandbox import user_config_hash

pytestmark = [pytest.mark.kicad, pytest.mark.gui, pytest.mark.slow]


# ---- serialisation + hygiene ----------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _one_gui_at_a_time():
    """Cross-process lock: two pytest sessions (or xdist workers) never drive two GUIs at once."""
    if not current().is_windows:
        pytest.skip("GUI sessions are Windows-only (D18)")
    lock = ProjectLock(current().temp_dir / "gui-tests.lock", timeout_s=900, stale_after_s=1800)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _installs_for(installs, major_minor: str) -> list[Any]:
    return [i for i in installs if i.major_minor == major_minor and i.source != "explicit"]


@pytest.fixture(scope="module", params=["10.0", "10.99"])
def gui_install(request, installs):
    found = _installs_for(installs, request.param)
    if not found:
        pytest.skip(f"KiCad {request.param} not installed")
    return found[0]


@pytest.fixture(scope="module")
def display_install(installs):
    if os.environ.get("KIWRAP_E2E_HIDDEN", "") not in ("1", "true", "yes"):
        pytest.skip("hidden mode is experimental (D20): set KIWRAP_E2E_HIDDEN=1 to run it")
    found = [i for i in installs if gui_sessions.input_from_events(i)]
    if not found:
        pytest.skip("no installed KiCad carries the display-input patch")
    return found[0]


@pytest.fixture
def _hygiene(request):
    installs = request.getfixturevalue("installs")
    before = {i.key(): user_config_hash(i) for i in installs}
    yield
    gui_sessions.close_all_gui()
    ipc_sessions.close_all()
    for i in installs:
        assert user_config_hash(i) == before[i.key()], f"user config of {i.version} changed"


def _scratch_ecc83(install, tmp_path: Path) -> Project:
    src = install.dir("demos") / "ecc83"
    if not src.is_dir():
        pytest.skip("ecc83 demo not shipped")
    dst = tmp_path / "ecc83"
    shutil.copytree(src, dst)
    for stray in dst.glob("*_v2.*"):  # the demo ships a second project (ecc83-pp_v2); one per directory
        stray.unlink()
    project = Project.from_path(dst / "ecc83-pp.kicad_pro")
    project.save_state(ProjectState(target_kicad=str(install.root)))
    return project


def _ctx(storage_home: Path) -> ToolContext:
    from kiwrap.core.services import Services

    return ToolContext(services=Services.real(storage_home), jobs=JobQueue(storage_home / "jobs"))


def _cloaked(hwnd: int) -> bool:
    value = ctypes.c_uint32()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(  # type: ignore[attr-defined]
        ctypes.c_void_p(hwnd), 14, ctypes.byref(value), ctypes.sizeof(value)
    )
    return hr == 0 and value.value != 0


def _pid_alive(pid: int) -> bool:
    return ipc_sessions._pid_alive(pid)


def _file_position(board: Path, ref: str) -> tuple[float, float]:
    from kiwrap.formats.board import BoardFile

    model = BoardFile.load(board)
    for fp in model.footprints:
        if fp.reference == ref:
            return (float(fp.position.x), float(fp.position.y))
    raise AssertionError(f"{ref} not in {board}")


def _api_position(session: Any, ref: str) -> tuple[float, float]:
    for fp in session.board().get_footprints():
        if fp.reference_field.text.value == ref:
            return (fp.position.x / 1e6, fp.position.y / 1e6)
    raise AssertionError(f"{ref} not on the live board")


def _file_rotation(board: Path, ref: str) -> float:
    from kiwrap.formats.board import BoardFile

    for fp in BoardFile.load(board).footprints:
        if fp.reference == ref:
            return float(fp.rotation) % 360.0
    raise AssertionError(f"{ref} not in {board}")


def _api_rotation(session: Any, ref: str) -> float:
    for fp in session.board().get_footprints():
        if fp.reference_field.text.value == ref:
            return float(fp.orientation.degrees) % 360.0
    raise AssertionError(f"{ref} not on the live board")


# ---- the supervisor -------------------------------------------------------------------------------------


def test_launch_bind_place_screenshot_detach(gui_install, storage_home, tmp_path, monkeypatch, _hygiene):
    project = _scratch_ecc83(gui_install, tmp_path)
    caps = capabilities_for(gui_install, storage_home=storage_home)
    session = gui_sessions.launch(
        gui_install, project.dir, editor="pcb", mode="auto", storage_home=storage_home
    )
    try:
        # 1. a live, sandboxed GUI whose API answers on its own pipe under the session's TEMP
        assert session.running and session.pid is not None
        assert session.socket_path is not None and session.temp_dir is not None
        assert session.socket_path.parent.parent == session.temp_dir
        assert len(str(session.socket_path)) <= current().max_socket_path
        assert session.version().startswith(gui_install.version.split(".")[0])
        assert session.mode_used == "view" and session.cloaked is False  # the product path: visible
        assert session.in_job, "the GUI must die with kiwrap (Job Object)"
        status = session.status()
        assert status["alive"] and status["api_socket"] == session.socket_url
        kinds = [e["kind"] for e in status["editors"]]
        assert "pcb" in kinds, status["editors"]
        assert status["locks"], "pcbnew takes ~<name>.kicad_pcb.lck / .kicad_pro.lck next to the file"
        assert session.record is not None and session.record.is_file()

        # 2. bound: the pool hands the GUI out, no headless server is started for the project
        assert ipc_sessions.bound_session_for(project.board) is session
        assert ipc_sessions.bound_session_for(project.dir) is session
        handle = ipc_sessions.pooled_session(gui_install, project.board)
        assert getattr(handle, "bound", False) is True
        with handle as s:
            assert s is session
            assert len(s.board().get_footprints()) == 15
        assert not ipc_sessions.live_sessions(), "no HeadlessSession while the GUI is bound"
        info = ipc_sessions.release_project(project.dir)
        assert info["bound"]["pid"] == session.pid and info["bound"]["locks"]
        assert session.running, "release_project() must not close the bound GUI"

        # 3. move C1 through the bound session; KiCad saves; the file and the API agree
        before_file = _file_position(project.board, "C1")
        target = (round(before_file[0] + 7.5, 3), round(before_file[1] - 3.25, 3))
        # the mutation prologue exempts the bound GUI's own ~*.lck files (core/tools/_base.mutation)
        if caps.has("ipc.headless"):
            from kiwrap.core.tools import pcb_author

            ctx = _ctx(storage_home)
            receipt = pcb_author.pcb_place(
                ctx,
                pcb_author.PcbPlaceArgs(
                    project=str(project.dir), placements=[pcb_author.Placement(ref="C1", at_mm=list(target))]
                ),
            )
            assert receipt.provider_used.startswith("ipc11"), (receipt.provider_used, receipt.warnings)
            # `ok` is the placement verdict (a courtyard overlap after a blind 7.5 mm move is
            # legitimate); what matters here is that the move went through the live GUI
            assert receipt.data["placed"][0]["ref"] == "C1"
            assert "after" in receipt.data and "ipc11" in receipt.data["plan"]["candidates"]
            assert not ipc_sessions.live_sessions(), "the tool used the bound GUI, not a headless server"
        else:
            from kiwrap.backends.ipc import pcb_writes

            with ipc_sessions.pooled_session(gui_install, project.board) as s:
                pcb_writes.pcb_place(s, "C1", target)
                pcb_writes.pcb_save(s)
        assert session.running, "the GUI survives a commit + save through its API"
        api_after = _api_position(session, "C1")
        assert api_after == pytest.approx(target, abs=1e-3)
        file_after = _file_position(project.board, "C1")
        assert file_after == pytest.approx(target, abs=1e-3), "SaveDocument wrote the move to disk"

        if caps.has("ipc.headless"):
            # 3b. reviewer finding 2 (2026-09-20): a write that lands on DISK (a rotation is
            # routed to the file writer by the G16 guard on every 10.99 build) must reach the
            # GUI too - it reloads the file - and the next commit through the GUI must not
            # write the old board back over it
            rot_before = _api_rotation(session, "C1")
            rot_target = (rot_before + 90.0) % 360.0
            r2 = pcb_author.pcb_place(
                ctx,
                pcb_author.PcbPlaceArgs(
                    project=str(project.dir),
                    placements=[pcb_author.Placement(ref="C1", at_mm=list(target), rotation=rot_target)],
                ),
            )
            if r2.provider_used.startswith("file"):
                assert r2.data.get("live_view") == {"reloaded": ["board"]}, (r2.warnings, r2.degraded)
                assert "live_view_stale" not in r2.degraded, r2.warnings
            assert _api_rotation(session, "C1") == pytest.approx(rot_target, abs=1e-3), (
                "the GUI shows the rotation written on disk"
            )
            # a commit through the GUI (R1 moves) keeps C1's rotation on disk
            r1_before = _file_position(project.board, "R1")
            r3 = pcb_author.pcb_place(
                ctx,
                pcb_author.PcbPlaceArgs(
                    project=str(project.dir),
                    placements=[pcb_author.Placement(ref="R1", at_mm=[r1_before[0] + 2.0, r1_before[1]])],
                ),
            )
            assert r3.provider_used.startswith("ipc11"), (r3.provider_used, r3.warnings)
            assert _file_rotation(project.board, "C1") == pytest.approx(rot_target, abs=1e-3), (
                "the GUI's SaveDocument must not undo the rotation written on disk"
            )
            # 3c. reviewer finding 9: this pcbnew holds no schematic - the schematic is answered
            # by the file engine, never with an E_INTERNAL from the GUI's API
            with pytest.raises(ipc_sessions.BoundElsewhere):
                ipc_sessions.pooled_session(gui_install, project.dir / "ecc83-pp.kicad_sch")
            from kiwrap.core.tools import sch_stats

            stats = sch_stats.sch_stats(ctx, sch_stats.SchStatsArgs(project=str(project.dir)))
            assert stats.data.get("ok") is not None and not ipc_sessions.live_sessions()

        # 4. screenshot without a helper: a PrintWindow capture, <= 1024 px, a real PNG
        png, summary = session.screenshot(max_px=800)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and summary["source"] == "window"
        from kiwrap.core.state.receipts import png_size

        size = png_size(png)
        assert size and max(size) <= 800 and min(size) >= 64, size
        assert len(png) > 2000, "a black or empty frame compresses to almost nothing"

        # 5. the helper path degrades to the window capture when the helper is unreachable
        session.attach_helper({"port": 1, "token": "nope", "version": "test"})
        png2, summary2 = session.screenshot(max_px=512)
        assert summary2["source"] == "window" and png2[:8] == b"\x89PNG\r\n\x1a\n"
        assert any("helper screenshot failed" in w for w in session.warnings)
        session.helper = None
    finally:
        pid = session.pid
        locks = list(session.locks_taken)
        session.close()
    # 6. detach leaves nothing: no process, no lock files, no binding, no record
    assert not session.running and pid is not None
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    assert not _pid_alive(pid)
    assert not project.editor_locks(), [p.name for p in project.editor_locks()]
    assert all(not p.exists() for p in locks)
    assert ipc_sessions.bound_session_for(project.board) is None
    assert session.record is None
    assert not gui_sessions.live_gui_sessions()


def test_one_gui_at_a_time(gui_install, storage_home, tmp_path, _hygiene):
    project = _scratch_ecc83(gui_install, tmp_path)
    session = gui_sessions.launch(gui_install, project.dir, editor="pcb", storage_home=storage_home)
    try:
        other = _scratch_ecc83(gui_install, tmp_path / "second")
        with pytest.raises(gui_sessions.GuiBusyError):
            gui_sessions.launch(gui_install, other.dir, editor="pcb", storage_home=storage_home)
        # hidden mode is refused honestly on a stock install; the older spellings resolve to view
        if not gui_sessions.input_from_events(gui_install):
            with pytest.raises(gui_sessions.GuiUnsupportedError):
                gui_sessions.GuiSession(
                    install=gui_install, project_dir=other.dir, mode="hidden", storage_home=storage_home
                )._resolve_mode()
        for spelling in ("view", "auto", "mirror"):
            resolved = gui_sessions.GuiSession(
                install=gui_install, project_dir=other.dir, mode=spelling, storage_home=storage_home
            )._resolve_mode()
            assert resolved == ("view", False), spelling
    finally:
        session.close()
    assert not gui_sessions.live_gui_sessions()


# ---- kw_live through the tool ------------------------------------------------------------------------


def test_kw_live_tool_round_trip(gui_install, storage_home, tmp_path, monkeypatch, _hygiene):
    project = _scratch_ecc83(gui_install, tmp_path)
    ctx = _ctx(storage_home)
    launched = live_tool.run(ctx, live_tool.LiveArgs(action="launch", project=str(project.dir), editor="pcb"))
    sid = launched.data["session_id"]
    try:
        assert launched.data["ok"] and launched.data["pid"] and launched.data["api_socket"]
        assert launched.data["token"] and launched.data["install"] == gui_install.key()
        assert launched.provider_used == f"gui:{gui_install.version}"
        assert [e for e in launched.data["editors"] if e["kind"] == "pcb"]
        assert launched.data["mode_used"] == "view" and launched.data["cloaked"] is False
        # status by id, by project, and by "the only one"
        for args in (
            live_tool.LiveArgs(action="status", session_id=sid),
            live_tool.LiveArgs(action="status", project=str(project.dir)),
            live_tool.LiveArgs(action="status"),
        ):
            st = live_tool.run(ctx, args)
            assert st.data["alive"] and st.data["session_id"] == sid
        # screenshot: the live window (PrintWindow), then the render fallback when capture fails
        shot = live_tool.run(ctx, live_tool.LiveArgs(action="screenshot", session_id=sid, max_px=640))
        assert shot.data["mode"] == "live", shot.warnings
        assert shot.image and shot.image[:8] == b"\x89PNG\r\n\x1a\n"
        assert shot.data["width"] <= 640 and shot.data["height"] <= 640

        def boom(*a: Any, **k: Any) -> Any:
            raise gui_sessions.GuiSessionError("capture disabled for the test")

        monkeypatch.setattr(gui_sessions.GuiSession, "screenshot", boom)
        fallback = live_tool.run(ctx, live_tool.LiveArgs(action="screenshot", session_id=sid, max_px=512))
        assert "live_view_unavailable" in fallback.degraded
        assert fallback.data.get("mode") == "render" and fallback.data["session_id"] == sid
        assert fallback.image is not None or "render_unavailable" in fallback.degraded
        monkeypatch.undo()
        # attach + raise
        att = live_tool.run(
            ctx,
            live_tool.LiveArgs(
                action="attach", session_id=sid, helper=live_tool.HelperArg(port=2, token="t", version="0")
            ),
        )
        assert (
            att.data["attached"] and att.data["helper"]["port"] == 2 and att.data["helper"]["token"] == "***"
        )
        raised = live_tool.run(ctx, live_tool.LiveArgs(action="raise", session_id=sid))
        assert "raised" in raised.data and raised.data["surface"]["kind"] == "pcb"
    finally:
        det = live_tool.run(ctx, live_tool.LiveArgs(action="detach", session_id=sid))
    assert det.data["closed"] and det.data["ok"]
    assert not project.editor_locks()
    with pytest.raises(Exception) as exc:
        live_tool.run(ctx, live_tool.LiveArgs(action="status", session_id=sid))
    assert getattr(exc.value, "code", "") == "E_BAD_ARGS"


# ---- hidden mode on a build carrying mr/display-input (experimental, opt-in: KIWRAP_E2E_HIDDEN=1) ---------


def test_hidden_mode_cloaks_the_editor(display_install, storage_home, tmp_path, _hygiene):
    project = _scratch_ecc83(display_install, tmp_path)
    session = gui_sessions.launch(
        display_install, project.dir, editor="pcb", mode="hidden", storage_home=storage_home
    )
    try:
        assert session.cloak_requested and session.mode_used == "hidden"
        assert session.cloaked is True, session.warnings
        frame = session.frame("pcb")
        assert frame is not None and frame.cloaked
        assert _cloaked(frame.hwnd), "DWMWA_CLOAKED read independently with ctypes"
        # the cloaked window is still composed: PrintWindow returns a real picture
        png, summary = session.screenshot(max_px=640)
        assert summary["source"] == "window" and len(png) > 2000
        # raise is a no-op on a cloaked frame
        assert session.raise_frame()["raised"] is False
        # the API is bound and answers
        assert ipc_sessions.bound_session_for(project.board) is session
        assert len(session.board().get_footprints()) == 15
        record = json.loads(session.record.read_text(encoding="utf-8"))
        assert record["pid"] == session.pid and record["locks"]
        # D20: even on this build the default (and every non-hidden spelling) stays the view
        for spelling in ("view", "auto", "mirror"):
            other = gui_sessions.GuiSession(
                install=display_install, project_dir=project.dir, mode=spelling, storage_home=storage_home
            )
            assert other._resolve_mode() == ("view", False), spelling
    finally:
        pid = session.pid
        session.close()
    assert pid is not None and not _pid_alive(pid)
    assert not project.editor_locks()
