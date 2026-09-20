"""Unit tests (no KiCad) for the two-server model of `kw_live` (docs/EXTENSION.md §6): a GUI
session another kiwrap process launched is discovered through its record under the shared temp
dir, bound to the project in THIS process (`sessions.bound_session_for`), reported by
`find_gui_session` / `kw_live status`, exempted from the editor-lock refusal of the mutating
tools, and closed (`kw_live detach`) from here.

A stand-in process (this interpreter running `time.sleep`) plays the KiCad GUI: its command line
carries the recorded `exe` and `document` strings the pid-recycling guard looks for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.ipc import gui_sessions
from kiwrap.backends.ipc import sessions as ipc_sessions
from kiwrap.core.state import KiwrapError
from kiwrap.core.state.locks import refuse_if_editor_open
from kiwrap.core.state.project import Project
from kiwrap.core.tools import live as live_tool
from kiwrap.installation import KiCadInstallation


def _install(root: Path, version: str = "10.99.0") -> KiCadInstallation:
    return KiCadInstallation(
        root=root,
        kicad_cli=root / "bin" / "kicad-cli.exe",
        share=root / "share" / "kicad",
        version=version,
        source="explicit",
        extra={},
    )


def _project(tmp: Path) -> Path:
    d = tmp / "demo"
    d.mkdir()
    (d / "demo.kicad_pro").write_text("(kicad_project (version 1))", encoding="utf-8")
    (d / "demo.kicad_pcb").write_text("(kicad_pcb (version 20240108) (generator test))", encoding="utf-8")
    return d


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A record of a 'foreign' GUI session backed by a sleeping python child, the records dir
    redirected under tmp_path, and `installation.find` answering a fake install for its root."""
    project = _project(tmp_path)
    root = tmp_path / "kicad-bench"
    root.mkdir()
    install = _install(root)
    monkeypatch.setattr(gui_sessions, "_records_dir", lambda temp_dir=None: tmp_path / "gui-sessions")
    monkeypatch.setattr("kiwrap.installation.find", lambda spec, *a, **k: install)
    exe = str(root / "bin" / "pcbnew.exe")
    document = str(project / "demo.kicad_pcb")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", exe, document],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    locks = [project / "~demo.kicad_pcb.lck", project / "~demo.kicad_pro.lck"]
    for lck in locks:
        lck.write_text("held by the GUI", encoding="utf-8")
    records = tmp_path / "gui-sessions"
    records.mkdir()
    record = records / f"{child.pid}.json"
    payload = {
        "pid": child.pid,
        "parent_pid": child.pid,  # "another kiwrap process": anything but os.getpid()
        "session_id": "gremote01",
        "token": "tok-remote",
        "exe": exe,
        "editor": "pcb",
        "document": document,
        "project_dir": str(project),
        "files": [document, str(project / "demo.kicad_pro")],
        "socket": str(tmp_path / "g1" / "kicad" / "api.sock"),
        "temp": str(tmp_path / "g1"),
        "install_root": str(root),
        "install_key": install.key(),
        "kicad": "10.99.0",
        "mode": "auto",
        "mode_used": "hidden",
        "cloak_requested": True,
        "cloaked": True,
        "started": time.time(),
        "ready_after_s": 3.3,
        "helper": None,
        "locks": [str(p) for p in locks],
    }
    record.write_text(json.dumps(payload), encoding="utf-8")
    yield {"project": project, "record": record, "child": child, "install": install, "locks": locks}
    ipc_sessions.unbind_gui_session(project)
    gui_sessions._REMOTE.clear()
    if child.poll() is None:
        child.kill()
        child.wait(timeout=10)


def test_remote_session_is_discovered_and_bound_in_another_process(stand_in: dict[str, Any]):
    project, child = stand_in["project"], stand_in["child"]
    found = gui_sessions.discover_remote_session(project / "demo.kicad_pcb")
    assert isinstance(found, gui_sessions.RemoteGuiSession)
    assert found.remote is True and found.pid == child.pid and found.session_id == "gremote01"
    assert found.running and found.owner_pid == child.pid
    assert found.socket_url == f"ipc://{stand_in['record'].parent.parent / 'g1' / 'kicad' / 'api.sock'}"
    assert found.file == project / "demo.kicad_pcb" and found.serves(project / "demo.kicad_pro")
    assert not found.serves(project.parent / "elsewhere.kicad_pcb")
    assert found.install is stand_in["install"] and found.mode_used == "hidden" and found.cloaked is True
    assert [p.name for p in found.locks_taken] == ["~demo.kicad_pcb.lck", "~demo.kicad_pro.lck"]
    # the same object comes back (cached per record) and by id / as the only one
    assert gui_sessions.discover_remote_session(project) is found
    assert gui_sessions.discover_remote_session(session_id="gremote01") is found
    assert gui_sessions.discover_remote_session() is found
    assert gui_sessions.find_gui_session(project_dir=project) is found
    assert gui_sessions.find_gui_session(session_id="gremote01") is found
    assert gui_sessions.find_gui_session() is found
    # the pool binds it for every file under the project: no headless server is started
    bound = ipc_sessions.bound_session_for(project / "demo.kicad_pcb")
    assert bound is found
    handle = ipc_sessions.pooled_session(stand_in["install"], project / "demo.kicad_pcb")
    assert getattr(handle, "bound", False) is True and handle.pid == child.pid
    assert not ipc_sessions.live_sessions()
    info = ipc_sessions.release_project(project)
    assert info["bound"]["session_id"] == "gremote01"
    assert info["bound"]["ipc11"] is True, (
        "10.99 has the headless IPC provider: the tools write through the GUI"
    )
    assert sorted(Path(x).name for x in info["bound"]["locks"]) == [
        "~demo.kicad_pcb.lck",
        "~demo.kicad_pro.lck",
    ]
    status = found.status()
    assert status["remote"] is True and status["owner_pid"] == child.pid and status["alive"] is True
    assert status["token"] == "tok-remote" and status["helper"] is None


def test_remote_session_locks_are_exempt_from_the_editor_lock_refusal(stand_in: dict[str, Any]):
    project = Project.from_path(stand_in["project"])
    with pytest.raises(KiwrapError) as exc:
        refuse_if_editor_open(project)
    assert exc.value.code == "E_FILE_LOCKED"
    info = ipc_sessions.release_project(project.dir)
    refuse_if_editor_open(project, exempt=info["bound"]["locks"])  # the bound GUI's own locks
    # a third lock (a real foreign editor) is still refused
    (project.dir / "~other.kicad_sch.lck").write_text("x", encoding="utf-8")
    with pytest.raises(KiwrapError) as exc2:
        refuse_if_editor_open(project, exempt=info["bound"]["locks"])
    assert exc2.value.candidates == ["~other.kicad_sch.lck"]


def test_remote_session_helper_attach_and_record_refresh(stand_in: dict[str, Any]):
    project, record = stand_in["project"], stand_in["record"]
    found = gui_sessions.discover_remote_session(project)
    assert found is not None and found.helper is None
    # the owner attaches the helper and rewrites the record: the remote view refreshes on status()
    info = json.loads(record.read_text(encoding="utf-8"))
    info["helper"] = {"port": 5555, "token": "helper-tok", "version": "0.1.0", "host": "127.0.0.1", "pid": 77}
    record.write_text(json.dumps(info), encoding="utf-8")
    status = found.status()
    assert status["helper"] == {
        "port": 5555,
        "version": "0.1.0",
        "host": "127.0.0.1",
        "pid": 77,
        "token": "***",
    }
    assert found.helper is not None and found.helper.token == "helper-tok"
    # attaching from this process writes the endpoint back for the owner
    found.attach_helper({"port": 6666, "token": "t2", "version": "0.2.0"})
    assert json.loads(record.read_text(encoding="utf-8"))["helper"]["port"] == 6666


def test_remote_session_close_kills_the_gui_and_cleans_up(stand_in: dict[str, Any]):
    project, child, record = stand_in["project"], stand_in["child"], stand_in["record"]
    found = gui_sessions.discover_remote_session(project)
    assert found is not None and ipc_sessions.bound_session_for(project) is found
    found.close()
    assert child.wait(timeout=15) is not None
    assert not found.running and not record.exists()
    assert not any(project.glob("~*.lck")), "the recorded lock files are removed once the GUI is dead"
    assert ipc_sessions.bound_session_for(project) is None
    assert gui_sessions.discover_remote_session(project) is None


def test_kw_live_status_and_detach_reach_a_foreign_session(stand_in: dict[str, Any], monkeypatch):
    """The agent's server (this process) answers kw_live status / detach for the session the
    extension's server launched, through the default backends (no Services binding)."""
    from dataclasses import dataclass

    project, child = stand_in["project"], stand_in["child"]

    class BareServices:  # no gui_* bindings: kw_live imports the real supervisor functions
        storage_home = stand_in["record"].parent.parent

    @dataclass
    class Ctx:
        services: Any
        jobs: Any = None
        server_version: str = "test"

    ctx = Ctx(services=BareServices())
    r = live_tool.run(ctx, live_tool.LiveArgs(action="status", project=str(project)))
    assert r.data["session_id"] == "gremote01" and r.data["remote"] is True and r.data["pid"] == child.pid
    assert r.provider_used == "gui:10.99.0"
    r2 = live_tool.run(ctx, live_tool.LiveArgs(action="detach", session_id="gremote01"))
    assert r2.data["closed"] is True and r2.data["pid"] == child.pid
    assert child.wait(timeout=15) is not None
    with pytest.raises(KiwrapError) as exc:
        live_tool.run(ctx, live_tool.LiveArgs(action="status", project=str(project)))
    assert exc.value.code == "E_BAD_ARGS"


def test_stale_and_foreign_records_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gui_sessions, "_records_dir", lambda temp_dir=None: tmp_path / "gui-sessions")
    records = tmp_path / "gui-sessions"
    records.mkdir()
    # a dead pid, a record of THIS process, a recycled pid (command line does not match), garbage
    (records / "999999999.json").write_text(
        json.dumps(
            {
                "pid": 999999999,
                "parent_pid": 1,
                "socket": "s",
                "install_root": "r",
                "exe": "x",
                "project_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    (records / "1.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "parent_pid": os.getpid(),
                "socket": "s",
                "install_root": "r",
                "exe": "x",
                "project_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    (records / "2.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "parent_pid": 1,
                "socket": "s",
                "install_root": "r",
                "exe": "not-in-this-command-line.exe",
                "project_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    (records / "3.json").write_text("{not json", encoding="utf-8")
    assert gui_sessions.remote_gui_sessions() == []
    assert gui_sessions.discover_remote_session(tmp_path) is None
    assert ipc_sessions.bound_session_for(tmp_path / "x.kicad_pcb") is None


def test_locks_stay_refused_where_the_tools_cannot_write_through_the_gui(tmp_path: Path):
    """A KiCad without `ipc.headless` (10.0.x): the bound GUI holds the files, the planner never
    picks ipc11, so the mutating tools' prologue must NOT exempt its lock files (a file write
    under the GUI would be overwritten by its next save)."""
    from dataclasses import dataclass, field

    project = _project(tmp_path)
    lck = project / "~demo.kicad_pcb.lck"
    lck.write_text("held", encoding="utf-8")

    class Caps:
        def has(self, key: str) -> bool:
            return False

    @dataclass
    class Fake:
        project_dir: Path
        session_id: str = "g10"
        pid: int = 1
        running: bool = True
        socket_url: str = "ipc://x"
        locks_taken: list[Path] = field(default_factory=list)
        files: list[Path] = field(default_factory=list)
        capabilities: Any = field(default_factory=Caps)

    fake = Fake(project_dir=project, locks_taken=[lck])
    ipc_sessions.bind_gui_session(project, fake)
    try:
        info = ipc_sessions.release_project(project)
        assert info["bound"]["ipc11"] is False
        with pytest.raises(KiwrapError) as exc:
            refuse_if_editor_open(
                Project.from_path(project), exempt=info["bound"]["locks"] if info["bound"]["ipc11"] else []
            )
        assert exc.value.code == "E_FILE_LOCKED"
    finally:
        ipc_sessions.unbind_gui_session(project)
