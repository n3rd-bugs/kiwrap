"""Read-only mode with fakes (no KiCad): docs/DECISIONS.md D23, requests/kiwrap-feature-requests.review.md
item 2 (and theme T2, the `detached()` context).

The review found a pre-call snapshot, `.kiwrap/` and `<board>.drc.json` in a reviewed project; a
non-persisting call (`kiwrap serve --readonly` / `KIWRAP_READONLY=1`, or `persist=false` on one
call) must leave the project byte-identical: no snapshot, no state, outputs under the storage home,
anything unexpected undone by the project guard, and the tools that would write refused with
E_READONLY. The live counterpart on KiCad 10.0.6 / 10.99 is tests/test_readonly_live.py.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.core.cli_main import apply_readonly, build_parser
from kiwrap.core.state import KiwrapError, readonly
from kiwrap.core.state import snapshots as snap
from kiwrap.core.state.project import Project
from tests.test_core_fakes import FakeBackend
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")


def tree(d: Path) -> dict[str, str]:
    """sha256 of every file under `d` (the snapshot store included): the byte-identity oracle."""
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(d).rglob("*"))
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _clean_readonly(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No test leaks the env switch or another test's in-memory states."""
    monkeypatch.setenv(readonly.READONLY_ENV, "0")  # records the original, so teardown restores it
    readonly.forget_state()
    yield
    readonly.forget_state()


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _bare_copy(h: Harness, name: str = "third") -> Path:
    """A project kiwrap never touched: the created files without `.kiwrap/` (a reviewer's input)."""
    src = Path(h.create("src_" + name))
    dst = h.work / name
    dst.mkdir()
    for p in src.iterdir():
        if p.is_file():
            shutil.copy2(p, dst / p.name.replace("src_" + name, name))
    (dst / f"{name}.kicad_prl").write_text('{"meta": {"version": 5}}\n', encoding="utf-8")
    return dst


# ---- the common argument ---------------------------------------------------------------------------------


def test_every_tool_publishes_persist_with_a_description(h: Harness):
    import anyio

    tools = anyio.run(h.server.list_tools)
    assert tools
    for t in tools:
        if t.name == "kw_project_upgrade":
            # D24: a call the client asks the user about must never be one that can only fail
            assert "persist" not in t.input_schema["properties"]
            continue
        prop = t.input_schema["properties"].get("persist")
        assert prop is not None, t.name
        assert "read-only" in prop["description"] and "E_READONLY" in prop["description"], t.name
        assert "persist" not in t.input_schema.get("required", []), t.name


# ---- a read-only server leaves the project byte-identical ---------------------------------------------------


def test_readonly_server_validators_and_exports_write_nothing(h: Harness):
    proj = Path(h.create())
    h.ok("kw_pcb_drc", project=str(proj))  # a writable session first: state + snapshots exist
    before = tree(proj)
    snaps = len(snap.list_snapshots(proj))
    h.ctx.readonly = True  # type: ignore[attr-defined]  # what `serve --readonly` does per process
    for tool, args in (
        ("kw_project", {"action": "status"}),
        ("kw_sch_check", {}),
        ("kw_pcb_drc", {}),
        ("kw_fab_export", {"targets": ["gerbers", "pos", "bom"]}),
        ("kw_snapshot", {"action": "list"}),
        ("kw_snapshot", {"action": "diff", "ref": "HEAD"}),
        ("kw_intent", {"action": "get"}),
    ):
        sc = h.ok(tool, project=str(proj), **args)
        assert sc["snapshot"] is None, tool
        assert sc["data"]["readonly"]["mode"] == "server" and sc["data"]["readonly"]["persisted"] is False, (
            tool
        )
    assert tree(proj) == before
    assert len(snap.list_snapshots(proj)) == snaps
    # the kicad-cli reports went to the storage home, never beside the input files
    outs = [kw.get("out_json") for name, kw in h.backend.calls if name in ("run_erc", "run_drc")]
    ro_outs = [Path(o) for o in outs if o is not None]
    assert ro_outs and all(h.home in o.parents for o in ro_outs)


def test_readonly_server_env_switch(h: Harness, monkeypatch: pytest.MonkeyPatch):
    proj = Path(h.create())
    before = tree(proj)
    monkeypatch.setenv(readonly.READONLY_ENV, "1")
    sc = h.ok("kw_sch_check", project=str(proj))
    assert sc["data"]["readonly"]["mode"] == "server"
    h.fail("kw_sch_annotate", "E_READONLY", project=str(proj))
    assert tree(proj) == before
    monkeypatch.setenv(readonly.READONLY_ENV, "0")
    assert h.ok("kw_sch_check", project=str(proj))["snapshot"]


def test_persist_false_on_a_writable_server(h: Harness):
    proj = Path(h.create())
    before = tree(proj)
    snaps = len(snap.list_snapshots(proj))
    sc = h.ok("kw_pcb_drc", project=str(proj), persist=False)
    assert sc["snapshot"] is None and sc["data"]["readonly"]["mode"] == "call"
    assert (
        "read-only"
        in h.call("kw_sch_check", project=str(proj), persist=False)[2].content[0].text.splitlines()[0]
    )
    assert tree(proj) == before and len(snap.list_snapshots(proj)) == snaps
    # the same call persisting is snapshotted as before
    assert h.ok("kw_pcb_drc", project=str(proj), persist=True)["snapshot"]
    assert len(snap.list_snapshots(proj)) == snaps + 1


def test_a_project_kiwrap_never_touched_gets_no_kiwrap_dir(h: Harness):
    proj = _bare_copy(h)
    before = tree(proj)
    h.ctx.readonly = True  # type: ignore[attr-defined]
    h.ok("kw_project", action="status", project=str(proj))
    h.ok("kw_sch_check", project=str(proj))
    h.ok("kw_pcb_drc", project=str(proj))
    h.ok("kw_fab_export", project=str(proj), targets=["pdf"], force={"reason": "review"})
    assert tree(proj) == before and not (proj / ".kiwrap").exists()
    # the lock of a detached mutation lives under the storage home when the project has no .kiwrap/
    assert (h.home / "locks").is_dir()


# ---- refusals ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["server", "call"])
def test_mutating_tools_refuse_with_a_teaching_error(h: Harness, mode: str):
    proj = Path(h.create())
    h.ok("kw_intent", project=str(proj), action="set", intent={"nets": {"VCC": ["R1.1"]}})
    before = tree(proj)
    extra: dict[str, Any] = {}
    if mode == "server":
        h.ctx.readonly = True  # type: ignore[attr-defined]
    else:
        extra["persist"] = False
    refused: list[tuple[str, dict[str, Any]]] = [
        ("kw_sch_add_symbol", {"project": str(proj), "lib_id": "Device:R", "at_mm": [10, 10]}),
        ("kw_sch_annotate", {"project": str(proj)}),
        ("kw_pcb_sync", {"project": str(proj)}),
        ("kw_pcb_route_auto", {"project": str(proj)}),
        ("kw_project", {"action": "create", "dir": str(h.work / "new"), "name": "new"}),
        ("kw_project", {"action": "retarget", "project": str(proj), "target_kicad": "10"}),
        (
            "kw_project_upgrade",
            {"project": str(proj), "from_kicad": "10.0", "to_kicad": "10.0.0", "files": ["demo.kicad_sch"]},
        ),
        ("kw_snapshot", {"action": "create", "project": str(proj)}),
        ("kw_snapshot", {"action": "restore", "project": str(proj), "ref": "HEAD"}),
        ("kw_intent", {"action": "set", "project": str(proj), "intent": {"nets": {}}}),
        ("kw_intent", {"action": "clear", "project": str(proj)}),
        ("kw_pcb_drc", {"project": str(proj), "save_board": True}),
        ("kw_live", {"action": "launch", "project": str(proj)}),
    ]
    for tool, args in refused:
        sc = h.fail(tool, "E_READONLY", **args, **extra)
        assert sc["data"]["mode"] == mode and sc["data"]["tool"] == tool
        assert sc["fix"] and sc["candidates"] and sc["why"]
        if mode == "server":
            assert "writable kiwrap" in sc["fix"]
        else:
            assert "without persist=false" in sc["fix"]
    assert tree(proj) == before
    assert not (h.work / "new").exists()


def test_refusal_policy_table():
    class A:
        def __init__(self, **kw: Any):
            self.__dict__.update(kw)

    assert readonly.refusal("kw_project", A(action="status"), read_only_hint=False) is None
    assert readonly.refusal("kw_project", A(action="upgrade_preview"), read_only_hint=False) is None
    assert readonly.refusal("kw_project", A(action="retarget"), read_only_hint=False)
    assert readonly.refusal("kw_project_upgrade", A(project="p", files=["a"]), read_only_hint=False)
    assert readonly.READ_ALTERNATIVES["kw_project_upgrade"][0] == "kw_project(action='upgrade_preview')"
    assert (
        readonly.refusal("kw_pcb_drc", A(save_board=True, refill_zones=False), read_only_hint=False) is None
    )
    assert readonly.refusal("kw_pcb_drc", A(save_board=True, refill_zones=None), read_only_hint=False)
    assert readonly.refusal("kw_pcb_get", A(), read_only_hint=True) is None
    assert readonly.refusal("kw_future_writer", A(), read_only_hint=False)
    assert readonly.refusal("kw_live", A(action="status"), read_only_hint=False) is None
    assert readonly.refusal("kw_live", A(action="attach"), read_only_hint=False)


def test_persist_true_is_ignored_on_a_readonly_server(h: Harness):
    proj = Path(h.create())
    before = tree(proj)
    h.ctx.readonly = True  # type: ignore[attr-defined]
    sc = h.ok("kw_sch_check", project=str(proj), persist=True)
    assert sc["snapshot"] is None and any("persist=true is ignored" in w for w in sc["warnings"])
    h.fail("kw_sch_annotate", "E_READONLY", project=str(proj), persist=True)
    assert tree(proj) == before


def test_readonly_server_withholds_a_next_suggested_it_cannot_run(h: Harness):
    proj = Path(h.create())
    h.ctx.readonly = True  # type: ignore[attr-defined]
    sc = h.ok("kw_sch_check", project=str(proj))
    assert sc["stage"] == "erc_clean" and sc["next_suggested"] is None
    assert sc["data"]["readonly"]["next_suggested_withheld"] == "kw_pcb_sync"


# ---- the in-memory state ---------------------------------------------------------------------------------


def test_a_readonly_session_keeps_its_stage_in_memory(h: Harness):
    """A clean read-only kw_pcb_drc opens the kw_fab_export gate for that session only."""
    proj = Path(h.create())
    state_file = (proj / ".kiwrap" / "state.json").read_bytes()
    h.fail("kw_fab_export", "E_GATE", project=str(proj), persist=False)
    sc = h.ok("kw_pcb_drc", project=str(proj), persist=False)
    assert sc["stage"] == "drc_clean"
    assert (
        h.ok("kw_project", action="status", project=str(proj), persist=False)["data"]["stage"] == "drc_clean"
    )
    assert (proj / ".kiwrap" / "state.json").read_bytes() == state_file
    # the persisting view is the file's
    assert h.ok("kw_project", action="status", project=str(proj))["data"]["stage"] == "created"
    # validator deltas continue across read-only calls
    sc = h.ok("kw_pcb_drc", project=str(proj), persist=False)
    drc = next(v for v in sc["validators"] if v["name"] == "drc")
    assert drc["before"] == 0 and drc["after"] == 0


def test_the_memory_state_is_dropped_when_state_json_changes(h: Harness):
    proj = Path(h.create())
    h.ok("kw_pcb_drc", project=str(proj), persist=False)
    assert (
        h.ok("kw_project", action="status", project=str(proj), persist=False)["data"]["stage"] == "drc_clean"
    )
    time.sleep(0.02)
    h.ok("kw_sch_check", project=str(proj))  # a writable call rewrites .kiwrap/state.json
    assert (
        h.ok("kw_project", action="status", project=str(proj), persist=False)["data"]["stage"] == "erc_clean"
    )


# ---- outputs -----------------------------------------------------------------------------------------------


def test_fab_export_goes_to_the_results_dir_and_runs_synchronously(h: Harness):
    proj = Path(h.create())
    h.ctx.readonly = True  # type: ignore[attr-defined]
    sc = h.ok("kw_fab_export", project=str(proj), targets=["gerbers"], force={"reason": "r"}, run_async=True)
    assert "job_id" not in sc["data"] and any("synchronously" in w for w in sc["warnings"])
    out = Path(sc["data"]["out_dir"])
    results = Path(sc["data"]["readonly"]["results_dir"])
    assert results in out.parents and h.home in results.parents and out.is_dir()
    assert all(Path(f).is_file() and results in Path(f).parents for f in sc["data"]["files"]["gerbers"])
    assert not (proj / "fab").exists()


def test_a_caller_out_dir_is_honoured_even_inside_the_project(h: Harness):
    proj = Path(h.create())
    wanted = proj / "review-out"
    wanted.mkdir()
    sc = h.ok(
        "kw_fab_export",
        project=str(proj),
        targets=["pdf"],
        force={"reason": "r"},
        out_dir=str(wanted),
        persist=False,
    )
    assert Path(sc["data"]["out_dir"]) == wanted.resolve() and (wanted / "pdf").is_dir()
    assert "relocated" not in sc["data"]["readonly"]


def test_fab_bom_csv_goes_to_the_storage_home(h: Harness):
    proj = Path(h.create())
    before = tree(proj)
    h.ctx.readonly = True  # type: ignore[attr-defined]
    sc = h.ok("kw_fab_bom", project=str(proj), write=True)
    assert Path(sc["data"]["file"]).is_file() and h.home in Path(sc["data"]["file"]).parents
    assert tree(proj) == before and not (proj / "fab").exists()


def test_a_file_written_into_the_project_is_moved_and_the_receipt_follows(tmp_path: Path):
    """The guard relocates what a tool wrote into the project (review item 2: `<root>.net`,
    `.kiwrap/tmp`) and rewrites the receipt's paths to the new place."""

    class NetlistBeside(FakeBackend):
        def export_netlist(
            self, install: Any, config: Any, schematic: Path, out: Path | None = None, **kw: Any
        ) -> Any:
            beside = Path(schematic).with_suffix(".net")  # an old backend ignoring the redirected `out`
            beside.write_text("(export)", encoding="utf-8")
            self.written = beside
            return super().export_netlist(install, config, schematic, out, **kw)

    backend = NetlistBeside()
    h = Harness(tmp_path, backend)
    proj = Path(h.create())
    before = tree(proj)
    sc = h.ok("kw_sch_check", project=str(proj), persist=False)
    moved = sc["data"]["readonly"]["relocated"]
    assert [Path(m["from"]).name for m in moved] == ["demo.net"] and Path(moved[0]["to"]).is_file()
    assert tree(proj) == before


# ---- the project guard -------------------------------------------------------------------------------------


class WritingBackend(FakeBackend):
    """A kicad-cli that misbehaves: writes a report beside the board whatever `out_json` says,
    rewrites the `.kicad_prl` (review B2) and the board, and leaves a temp dir in `.kiwrap/`."""

    def run_drc(self, install: Any, config: Any, board: Path, **kw: Any) -> Any:
        board = Path(board)
        (board.parent / "stray.drc.json").write_text("{}", encoding="utf-8")
        prl = board.with_suffix(".kicad_prl")
        prl.write_text('{"meta": {"version": 99}}\n', encoding="utf-8")
        board.write_text(board.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        (board.parent / ".kiwrap" / "tmp" / "x").mkdir(parents=True, exist_ok=True)
        (board.parent / ".kiwrap" / "tmp" / "x" / "scratch.txt").write_text("x", encoding="utf-8")
        return super().run_drc(install, config, board, **kw)


def test_the_guard_undoes_what_a_misbehaving_backend_wrote(tmp_path: Path):
    h = Harness(tmp_path, WritingBackend())
    proj = Path(h.create())
    (proj / "demo.kicad_prl").write_text('{"meta": {"version": 5}}\n', encoding="utf-8")
    before = tree(proj)
    mtime = (proj / "demo.kicad_pcb").stat().st_mtime_ns
    sc = h.ok("kw_pcb_drc", project=str(proj), persist=False)
    ro = sc["data"]["readonly"]
    assert set(ro["restored"]) == {"demo.kicad_pcb", "demo.kicad_prl"}
    assert {Path(r["from"]).name for r in ro["relocated"]} == {"stray.drc.json", "scratch.txt"}
    assert all(Path(r["to"]).is_file() for r in ro["relocated"])
    assert ".kiwrap/tmp/x" in ro["removed_dirs"] and ".kiwrap/tmp" in ro["removed_dirs"]
    assert tree(proj) == before
    assert (proj / "demo.kicad_pcb").stat().st_mtime_ns == mtime
    assert any("put back" in w for w in sc["warnings"]) and any("moved to" in w for w in sc["warnings"])


def test_the_guard_leaves_a_project_an_editor_has_open(tmp_path: Path):
    h = Harness(tmp_path, WritingBackend())
    proj = Path(h.create())
    lock = proj / "~demo.kicad_pcb.lck"
    lock.write_text("someone", encoding="utf-8")
    board = (proj / "demo.kicad_pcb").read_bytes()
    sc = h.ok("kw_pcb_drc", project=str(proj), persist=False)
    left = {x["path"] for x in sc["data"]["readonly"]["left"]}
    assert "demo.kicad_pcb" in left and "stray.drc.json" in left
    assert (proj / "demo.kicad_pcb").read_bytes() != board  # the editor's business, reported not reverted
    assert lock.exists()


def test_a_failed_readonly_call_still_runs_the_guard(tmp_path: Path):
    class Failing(WritingBackend):
        def run_drc(self, install: Any, config: Any, board: Path, **kw: Any) -> Any:
            super().run_drc(install, config, board, **kw)
            raise RuntimeError("kicad-cli crashed")

    h = Harness(tmp_path, Failing())
    proj = Path(h.create())
    before = tree(proj)
    sc = h.fail("kw_pcb_drc", "E_INTERNAL", project=str(proj), persist=False)
    assert "demo.kicad_pcb" in sc["data"]["readonly"]["restored"]
    assert tree(proj) == before


# ---- lower layers ----------------------------------------------------------------------------------------


def test_snapshot_writes_refuse_inside_a_detached_call(h: Harness):
    proj = Path(h.create())
    project = Project.from_path(proj)
    fresh = _bare_copy(h, "fresh")
    with readonly.DetachedCall(h.ctx, "kw_test", mode="call", project=project):
        for fn in (
            lambda: snap.create(proj, "x"),
            lambda: snap.restore(proj, "HEAD"),
            lambda: snap.ensure_repo(fresh),
        ):
            with pytest.raises(KiwrapError) as exc:
                fn()
            assert exc.value.code == "E_READONLY"
        assert snap.list_snapshots(proj)  # reads still work
    assert not (fresh / ".kiwrap").exists()


def test_resolve_out_path_rules(h: Harness, tmp_path: Path):
    proj = Project.from_path(h.create())
    for bad in ("relative/dir", str(tmp_path / "x.kicad_pcb"), str(tmp_path / "missing" / "deeper" / "out")):
        with pytest.raises(KiwrapError) as exc:
            readonly.resolve_out_path(h.ctx, proj, bad, "fab")
        assert exc.value.code == "E_BAD_ARGS"
    good = tmp_path / "out"
    assert readonly.resolve_out_path(h.ctx, proj, str(good), "fab") == good.resolve()
    default = proj.dir / "fab"
    assert readonly.resolve_out_path(h.ctx, proj, None, "fab", default=default) == default
    auto = readonly.resolve_out_path(h.ctx, proj, "auto", "fab", default=default)
    assert h.home in auto.parents and auto.name == "fab" and auto.is_dir()
    with readonly.DetachedCall(h.ctx, "kw_test", mode="call", project=proj) as call:
        inside = readonly.resolve_out_path(h.ctx, proj, None, "bom", default=default)
        assert inside == call.results("bom") and inside.is_dir()
        chosen = readonly.resolve_out_path(h.ctx, proj, str(proj.dir), "bom")
        assert chosen in call.allowed


def test_results_are_swept(tmp_path: Path):
    root = tmp_path / "results" / "p-1234"
    now = time.time()
    dirs = []
    for i in range(25):
        d = root / f"202601{i + 1:02d}T000000-aaaaaa"
        d.mkdir(parents=True)
        age = 3600.0 if i < 24 else 10.0
        os.utime(d, (now - age, now - age))
        dirs.append(d)
    old = root / "20250101T000000-bbbbbb"
    old.mkdir()
    os.utime(old, (now - 2 * 86400, now - 2 * 86400))
    store = root / "drc"  # another store in the same per-project folder (the DRC result store)
    store.mkdir()
    os.utime(store, (now - 2 * 86400, now - 2 * 86400))
    gone = readonly.sweep_results(root, now=now)
    kept = sorted(p.name for p in root.iterdir() if p.name != "drc")
    assert old in gone and len(kept) == readonly.RESULTS_KEEP
    assert dirs[-1].name in kept  # the newest (and young) survives
    assert store.is_dir()  # never swept: not a call directory


def test_readonly_calls_on_one_project_take_turns(h: Harness):
    """Two guarded windows on one project never overlap in a process (one call's in-flight file
    must not look like the other call's leftover)."""
    import threading

    proj = Project.from_path(h.create())
    order: list[str] = []
    entered = threading.Event()

    def second() -> None:
        entered.wait(5)
        with readonly.DetachedCall(h.ctx, "kw_b", mode="call", project=proj):
            order.append("b")

    t = threading.Thread(target=second)
    t.start()
    with readonly.DetachedCall(h.ctx, "kw_a", mode="call", project=proj):
        entered.set()
        time.sleep(0.3)
        order.append("a")
    t.join(5)
    assert order == ["a", "b"]


def test_is_readonly_sources(monkeypatch: pytest.MonkeyPatch):
    class Ctx:
        pass

    ctx = Ctx()
    assert not readonly.is_readonly(ctx, {"persist": None})
    assert readonly.is_readonly(ctx, {"persist": False}) and readonly.is_readonly(ctx, False)
    monkeypatch.setenv(readonly.READONLY_ENV, "true")
    assert readonly.is_readonly(ctx, None)
    ctx.readonly = False  # type: ignore[attr-defined]  # an explicit context flag wins over the env
    assert not readonly.is_readonly(ctx, None)


def test_cli_serve_readonly_flag(monkeypatch: pytest.MonkeyPatch):
    args = build_parser().parse_args(["serve", "--readonly"])
    assert args.readonly is True
    assert apply_readonly(args) is True and os.environ[readonly.READONLY_ENV] == "1"
    monkeypatch.delenv(readonly.READONLY_ENV)
    assert apply_readonly(build_parser().parse_args(["serve"])) is False
    monkeypatch.setenv(readonly.READONLY_ENV, "1")
    assert apply_readonly(build_parser().parse_args(["serve"])) is True


def test_help_has_a_readonly_page(h: Harness):
    md = h.ok("kw_help", topic="readonly")["data"]["markdown"]
    assert md.startswith("# Read-only mode") and "persist=false" in md and "E_READONLY" in md
    assert "--readonly" in h.ok("kw_help", topic="E_READONLY")["data"]["markdown"]
    assert "readonly" in h.ok("kw_help")["data"]["markdown"]
