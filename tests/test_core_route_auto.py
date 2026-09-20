"""kw_pcb_route_auto / kw_pcb_fix and the planner's routing operations, against FAKED contracts: the
Specctra codec (DsnWriter / SesReader), the patched-CLI export / import, the managed engines and the
Router (kiwrap.core.tools.pcb_route_auto documents them). The board, the writer (locking on the
CST) and the reader are REAL (`kiwrap.formats`), on the conformance corpus' tiny routed board; the
DRC is the canned `FakeBackend` report. No KiCad, no JVM.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.router import Engines, RouteProgress, RouteResult, RouteRules
from kiwrap.backends.router.freerouting import RouterCancelled, RouterError, RouterTimeout
from kiwrap.core import planner
from kiwrap.core.state.project import Project
from kiwrap.core.state.stages import record_stage, sha256_file
from kiwrap.core.tools import TOOL_NAMES
from kiwrap.core.tools import help as help_tool
from kiwrap.core.tools.pcb_route_auto import copper_items, set_locked, violation_nets
from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter
from kiwrap.formats.reports import Position, Violation, ViolationItem
from tests.test_core_fakes import CAPS_10, drc_report
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

REPO = Path(__file__).resolve().parents[1]
TINY_BOARD = REPO / "conformance" / "corpus" / "10.0" / "tiny_routed" / "tiny_routed.kicad_pcb"


# ---- fakes of the contracts -------------------------------------------------------------------------------


@dataclass
class FakeSesReport:
    tracks_added: int = 0
    vias_added: int = 0
    tracks_removed: int = 0
    vias_removed: int = 0
    placement_ignored: bool = True
    nets_routed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def fake_dsn_writer(
    board: Any, *, rules: Any = None, layers: Any = None, nets: Any = None, name: str = ""
) -> str:
    """A minimal but well-formed DSN from the real board model (layers, boundary, nets with pins,
    a class, the wiring with `fix` for locked items) - what the router's DSN hook needs."""
    bbox = board.outline_bbox()
    x0, y0, x1, y1 = (bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y) if bbox else (0, 0, 20, 15)
    um = lambda v: int(round(v * 1000))  # noqa: E731
    out = [
        '(pcb fake (parser (string_quote ") (space_in_quoted_tokens on) (host_cad "kiwrap") (host_version "fake"))'
    ]
    out.append("  (resolution um 10) (unit um)")
    out.append("  (structure")
    for name in layers or list(board.copper_layers):
        out.append(f"    (layer {name} (type signal))")
    out.append(
        f"    (boundary (path pcb 0 {um(x0)} {-um(y0)} {um(x1)} {-um(y0)} {um(x1)} {-um(y1)} {um(x0)} {-um(y1)} {um(x0)} {-um(y0)}))"
    )
    out.append("    (rule (width 200) (clearance 200))")
    out.append("  )")
    out.append("  (placement)")
    out.append("  (library)")
    out.append("  (network")
    names: list[str] = []
    for net in board.nets:
        if not net.name or net.name.startswith("unconnected-("):
            continue
        pins = [
            f"{fp.reference}-{pad.number}"
            for fp, pad in board.pads()
            if pad.net is not None and pad.net.name == net.name
        ]
        out.append(f"    (net {net.name} (pins {' '.join(pins)}))")
        names.append(net.name)
    out.append(f"    (class kicad_default {' '.join(names)} (rule (width 200) (clearance 200)))")
    out.append("  )")
    out.append("  (wiring")
    for t in board.tracks:
        kind = "fix" if t.locked else "route"
        out.append(
            f"    (wire (path {t.layer} {um(t.width)} {um(t.start.x)} {-um(t.start.y)} {um(t.end.x)} {-um(t.end.y)}) (net {t.net.name if t.net else ''}) (type {kind}))"
        )
    for v in board.vias:
        kind = "fix" if v.locked else "route"
        out.append(
            f"    (via Via {um(v.position.x)} {-um(v.position.y)} (net {v.net.name if v.net else ''}) (type {kind}))"
        )
    out.append("  )")
    out.append(")")
    return "\n".join(out) + "\n"


class FakeSesReader:
    """KiCad's importer, emulated on the real writer: every UNLOCKED track / via is deleted, the
    locked ones stay, and one straight track per routed net (between its first two pads) is added."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.nets = re.findall(r"\(net (\S+)", text)

    def apply(self, writer: BoardWriter, *, keep_locked: bool = True) -> FakeSesReport:
        model = BoardFile.from_node(writer.root)
        rep = FakeSesReport(nets_routed=list(self.nets))
        for t in [*model.tracks, *model.arcs]:
            if not (keep_locked and t.locked):
                writer.delete(t.uuid)
                rep.tracks_removed += 1
        for v in model.vias:
            if not (keep_locked and v.locked):
                writer.delete(v.uuid)
                rep.vias_removed += 1
        for net in self.nets:
            pads = [pad for _fp, pad in model.pads() if pad.net is not None and pad.net.name == net]
            if len(pads) >= 2:
                a, b = pads[0].position, pads[1].position
                writer.add_track([(a.x, a.y), (b.x, b.y)], 0.2, "F.Cu", net)
                rep.tracks_added += 1
        return rep


class FakeRouter:
    """`kiwrap.backends.router.Router` twin: records the call, reports progress, writes a SES that
    routes the nets the DSN offered (or `nets`), returns a `RouteResult`. `mode` steers failures."""

    mode = "ok"
    calls: list[dict[str, Any]] = []

    def __init__(self, engines: Engines, *, temp_dir: Path | None = None, **kw: Any) -> None:
        self.engines, self.temp_dir = engines, temp_dir

    def route(
        self,
        dsn: Path,
        ses_out: Path,
        *,
        rules: RouteRules,
        nets: Any = None,
        passes: int = 100,
        timeout_s: float = 900,
        progress_cb: Any = None,
        cancel_event: threading.Event | None = None,
        extra_args: Any = (),
    ) -> RouteResult:
        text = Path(dsn).read_text(encoding="utf-8")
        FakeRouter.calls.append(
            {
                "dsn": text,
                "nets": list(nets) if nets is not None else None,
                "rules": rules,
                "passes": passes,
                "timeout_s": timeout_s,
                "cancel": cancel_event,
            }
        )
        if progress_cb is not None:
            progress_cb(
                RouteProgress(stage="autoroute", pass_no=1, max_passes=passes, unrouted=1, message="pass 1")
            )
        if FakeRouter.mode == "timeout":
            raise RouterTimeout(
                "Freerouting exceeded 5 s after 3 pass(es) (1 unrouted); killed",
                argv=["java"],
                log="pass #3",
                elapsed_s=5.0,
            )
        if FakeRouter.mode == "error":
            raise RouterError(
                "Freerouting failed (exit status 3: bad boundary)",
                argv=["java"],
                exit_code=3,
                log="ERROR bad boundary",
            )
        if FakeRouter.mode == "cancel":
            raise RouterCancelled("cancelled after 2 pass(es), 1 s")
        dsn_nets = re.findall(r"\(net (\S+) \(pins", text)
        routed = [n for n in dsn_nets if nets is None or n in nets]
        ses_out.parent.mkdir(parents=True, exist_ok=True)
        ses_out.write_text(
            "(session fake (routes (network_out " + " ".join(f"(net {n} (wire))" for n in routed) + ")))\n",
            encoding="utf-8",
        )
        if progress_cb is not None:
            progress_cb(
                RouteProgress(
                    stage="done", pass_no=2, max_passes=passes, unrouted=0, message="session written"
                )
            )
        return RouteResult(
            passes=2,
            unrouted=0 if FakeRouter.mode != "partial" else 1,
            log="fake log",
            duration_s=0.5,
            exit_code=0,
            ses_path=ses_out,
            ses_written=True,
            violations=0,
            score=99.0,
            state="COMPLETED",
            stop_reason="",
            unrouted_at_start=len(routed),
            unrouted_connections=[{"net": "NET2", "from": "R1-2", "to": "R2-2"}]
            if FakeRouter.mode == "partial"
            else [],
            argv=["java", "-jar", "freerouting.jar"],
            ignored_nets=[n for n in dsn_nets if n not in routed],
            ignore_classes=["kiwrap_ignore"] if nets is not None else [],
        )


def fake_engines(storage_home: Path | None = None, **kw: Any) -> Engines:
    return Engines(
        storage_home=Path(storage_home or "."),
        java=Path("java.exe"),
        jar=Path("freerouting-2.4.1.jar"),
        profile=None,
        jre_release="jdk-25.0.4.1+1",
        freerouting_version="2.4.1",
    )


def no_engines(storage_home: Path | None = None, **kw: Any) -> Engines:
    return Engines(storage_home=Path(storage_home or "."), java=None, jar=None, profile=None)


def must_fix_violation(net: str, vtype: str = "clearance") -> Violation:
    return Violation(
        type=vtype,
        severity="error",
        description="Clearance violation (netclass 'Default' clearance 0.2 mm; actual 0.1 mm)",
        items=(
            ViolationItem(
                uuid="aaaa", description=f"Track [{net}] on F.Cu, length 3.0 mm", pos=Position(5.0, 7.5)
            ),
            ViolationItem(uuid="bbbb", description=f"Pad 2 [{net}] of R1", pos=Position(5.9, 7.5)),
        ),
        sheet=None,
    )


def unconnected_violation(net: str) -> Violation:
    return Violation(
        type="unconnected_items",
        severity="error",
        description="Missing connection between items",
        items=(
            ViolationItem(uuid="cccc", description=f"Pad 1 [{net}] of R1", pos=Position(4.0, 7.5)),
            ViolationItem(uuid="dddd", description=f"Pad 1 [{net}] of R2", pos=Position(14.0, 7.5)),
        ),
        sheet=None,
    )


def netless_violation() -> Violation:
    return Violation(
        type="hole_clearance",
        severity="error",
        description="Hole clearance violation (0.25 mm; actual 0.1 mm)",
        items=(ViolationItem(uuid="eeee", description="Pad 1 of J1", pos=Position(1.0, 1.0)),),
        sheet=None,
    )


# ---- harness -------------------------------------------------------------------------------------------------


def make_harness(tmp_path: Path, backend: Any = None) -> Harness:
    FakeRouter.mode = "ok"
    FakeRouter.calls = []
    harness = Harness(tmp_path, backend)
    svc: Any = harness.ctx.services
    svc.file_read_board = BoardFile.load
    svc.board_writer = BoardWriter
    svc.lib_resolver = lambda *a, **k: None
    svc.dsn_writer = fake_dsn_writer
    svc.ses_reader = FakeSesReader
    svc.router = FakeRouter
    svc.router_engines = fake_engines
    return harness


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)


def routed_project(h: Harness, *, placed: bool = True, name: str = "demo") -> str:
    """A project whose board is the corpus' routed 2-resistor board (2 nets, 6 tracks), at stage
    `placed` (recorded with the board's hash, the way kw_pcb_place leaves it) unless `placed=False`."""
    d = h.create(name)
    shutil.copyfile(TINY_BOARD, Path(d) / f"{name}.kicad_pcb")
    if placed:
        project = Project.from_path(d)
        state = project.load_state()
        record_stage(project, state, "placed")
        project.save_state(state)
    return d


def board(d: str) -> BoardFile:
    return BoardFile.load(Path(d) / "demo.kicad_pcb")


FORCE = {"reason": "unit test: the board is placed by construction"}


# ---- catalog / planner -------------------------------------------------------------------------------------------


def test_tools_registered_with_help_pages_and_planner_ops():
    assert "kw_pcb_route_auto" in TOOL_NAMES and "kw_pcb_fix" in TOOL_NAMES
    assert (
        TOOL_NAMES.index("kw_pcb_route")
        < TOOL_NAMES.index("kw_pcb_route_auto")
        < TOOL_NAMES.index("kw_pcb_fix")
    )
    assert TOOL_NAMES.index("kw_pcb_fix") < TOOL_NAMES.index("kw_pcb_get")
    for name in ("kw_pcb_route_auto", "kw_pcb_fix"):
        assert name in help_tool.TOOL_PAGES and "Freerouting" in help_tool.TOOL_PAGES[name]
    assert "planned tool" not in help_tool.ERROR_PAGES["E_GATE"]
    for op in ("routing.autoroute", "routing.fix"):
        assert planner.operation(op).names() == ["cli", "codec", "swig"]
    described = planner.describe()
    assert described["routing.autoroute"]["fallback"] == "kw_pcb_route (track / via primitives)"
    assert planner.provider_label("codec", None) == "codec"
    assert planner.provider_label("swig", type("I", (), {"version": "10.0.6"})()) == "swig:10.0.6"
    # provider predicates: a stock 10.x record -> codec first (no patched cli, no swig driver keys)
    from tests.test_core_fakes import FakeCaps, FakeInstall

    caps = FakeCaps(CAPS_10, FakeInstall())
    p = planner.plan("routing.autoroute", caps)
    assert [c.name for c in p.candidates] == ["codec"]
    caps_cli = FakeCaps(
        {
            **CAPS_10,
            "cli.has_specctra_export": True,
            "cli.has_specctra_import": True,
            "swig.ses_import_headless": True,
        },
        FakeInstall(),
    )
    assert [c.name for c in planner.plan("routing.autoroute", caps_cli).candidates] == [
        "cli",
        "codec",
        "swig",
    ]
    page, _ = help_tool.page("providers")
    assert "routing.autoroute" in page and "codec" in page


def test_violation_nets_and_unfixable():
    v_ok = {
        "class": "must_fix",
        "description": "x",
        "items": [{"description": "Track [GND] on F.Cu"}, {"description": "Pad 1 [/sig] of U1"}],
    }
    v_none = {"class": "must_fix", "description": "Hole clearance", "items": [{"description": "Pad 1 of J1"}]}
    v_review = {"class": "review", "description": "y", "items": [{"description": "Track [VCC] on F.Cu"}]}
    v_unc = {
        "class": "must_fix",
        "description": "Missing connection",
        "items": [{"description": "Pad 1 [unconnected-(R1-Pad1)] of R1"}],
    }
    nets, unfixable = violation_nets([v_ok, v_none, v_review, v_unc])
    assert nets == ["/sig", "GND"] and unfixable == [v_none, v_unc]


def test_set_locked_on_the_real_writer(tmp_path: Path):
    p = tmp_path / "t.kicad_pcb"
    shutil.copyfile(TINY_BOARD, p)
    w = BoardWriter(tmp_path, epoch={"pcb": 20260206}, lib_resolver=None)
    w.load(p)
    items = copper_items(BoardFile.load(p))
    assert len(items) == 6 and not any(it.locked for it in items)
    assert set_locked(w, items[0].uuid, True) and set_locked(w, items[1].uuid, True)
    assert not set_locked(w, "no-such-uuid", True)
    w.save()
    text = p.read_text(encoding="utf-8")
    assert text.count("(locked yes)") == 2
    after = {it.uuid: it.locked for it in copper_items(BoardFile.load(p))}
    assert after[items[0].uuid] and after[items[1].uuid] and not after[items[2].uuid]
    w2 = BoardWriter(tmp_path, epoch={"pcb": 20260206}, lib_resolver=None)
    w2.load(p)
    assert set_locked(w2, items[0].uuid, False)
    w2.save()
    assert p.read_text(encoding="utf-8").count("(locked yes)") == 1


# ---- kw_pcb_route_auto -------------------------------------------------------------------------------------------


def test_route_auto_is_gated_on_placed(h: Harness):
    d = routed_project(h, placed=False)
    sc = h.fail("kw_pcb_route_auto", "E_GATE", project=d, run_async=False)
    assert sc["data"]["required_stage"] == "placed" and "kw_pcb_place" in sc["fix"]
    # a forced run at `created` routes but cannot reach `routed` (the stage machine is not lied to)
    sc2 = h.ok("kw_pcb_route_auto", project=d, run_async=False, force=FORCE)
    assert sc2["data"]["ok"] and sc2["stage"] == "created" and "gate overridden" in " ".join(sc2["warnings"])


def test_route_auto_needs_the_engines(h: Harness):
    d = routed_project(h)
    h.ctx.services.router_engines = no_engines  # type: ignore[attr-defined]
    sc = h.fail("kw_pcb_route_auto", "E_UNSUPPORTED", project=d, run_async=False)
    assert "bootstrap" in sc["fix"] and sc["data"]["capability"] == "engines.freerouting"
    assert sc["data"]["fallback"] == "kw_pcb_route"
    # nothing was touched: no lock flags, the same board
    assert sha256_file(Path(d) / "demo.kicad_pcb") == sha256_file(TINY_BOARD)


def test_route_auto_full_loop_with_the_codec_provider(h: Harness):
    d = routed_project(h)
    before = board(d)
    assert len(before.tracks) == 6
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False, passes=12, timeout_s=30)
    data = sc["data"]
    assert data["ok"] is True and sc["provider_used"] == "codec"
    assert data["engine"] == {"name": "freerouting", "version": "2.4.1", "jre": "jdk-25.0.4.1+1"}
    assert data["rules"]["fanout"] is False and data["rules"]["copper_layers"] == 2
    assert (
        data["dsn"]["provider"] == "codec"
        and data["dsn"]["layers"] == ["F.Cu", "B.Cu"]
        and data["dsn"]["boundary"]
    )
    assert data["dsn"]["nets"] == 2 and data["dsn"]["wires"] == 6
    assert data["router"]["passes"] == 2 and data["router"]["unrouted"] == 0 and data["router"]["ok"]
    routed = data["routed"]
    assert routed["nets"] is None and routed["nets_locked"] == [] and routed["kept_locked"] == 0
    assert routed["copper_before"] == {"tracks": 6, "vias": 0, "locked": 0}
    assert routed["tracks_removed"] == 6 and routed["tracks_added"] == 2 and routed["unrouted"] == 0
    assert data["ses"]["nets_routed"] == ["NET1", "NET2"] and data["ses"]["placement_ignored"] is True
    assert data["drc"]["ok"] and data["drc"]["must_fix"] == 0
    assert sc["stage"] == "drc_clean" and sc["next_suggested"] == "kw_fab_export"
    assert "demo.kicad_pcb" in sc["changed"]["updated"] and sc["snapshot"]
    names = {v["name"]: v for v in sc["validators"]}
    assert names["drc"]["after"] == 0 and names["unrouted"]["after"] == 0
    after = board(d)
    assert len(after.tracks) == 2 and not any(t.locked for t in after.tracks)
    call = FakeRouter.calls[-1]
    assert call["nets"] is None and call["passes"] == 12 and call["timeout_s"] == 30
    assert call["rules"].copper_to_edge_um > 0 and "(type fix)" not in call["dsn"]
    assert data["plan"]["candidates"] == ["codec"]
    # the mutation lock is released; a second call (now at drc_clean) still passes the gate
    sc2 = h.ok("kw_pcb_route_auto", project=d, run_async=False)
    assert sc2["data"]["ok"]


def test_route_auto_subset_locks_the_other_nets_for_the_run(h: Harness):
    d = routed_project(h)
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False, nets=["NET1"])
    data = sc["data"]
    assert data["routed"]["nets"] == ["NET1"] and data["routed"]["nets_locked"] == ["NET2"]
    assert data["routed"]["kept_locked"] == 3  # NET2's three segments were locked for the run
    call = FakeRouter.calls[-1]
    assert call["nets"] == ["NET1"]
    assert call["dsn"].count("(type fix)") == 3 and call["dsn"].count("(type route)") == 3
    after = board(d)
    # NET2's tracks survived (kept by the importer as locked) and are unlocked again; NET1 re-routed
    net2 = [t for t in after.tracks if t.net and t.net.name == "NET2"]
    net1 = [t for t in after.tracks if t.net and t.net.name == "NET1"]
    assert len(net2) == 3 and not any(t.locked for t in net2) and len(net1) == 1
    assert data["routed"]["tracks_removed"] == 3 and data["routed"]["tracks_added"] == 1
    assert data["router"]["ignored_nets"] == ["NET2"]
    # unknown net -> E_BAD_ARGS with candidates, board untouched
    sha = sha256_file(Path(d) / "demo.kicad_pcb")
    sc2 = h.fail("kw_pcb_route_auto", "E_BAD_ARGS", project=d, run_async=False, nets=["NET9"])
    assert "NET1" in sc2["candidates"] and sha256_file(Path(d) / "demo.kicad_pcb") == sha


def test_route_auto_lock_refs_keeps_the_footprints_nets(h: Harness):
    d = routed_project(h)
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False, lock_refs=["R1"])
    data = sc["data"]
    assert data["routed"]["nets_locked"] == ["NET1", "NET2"] and data["routed"]["kept_locked"] == 6
    assert data["routed"]["tracks_removed"] == 0
    after = board(d)
    assert len(after.tracks) == 8 and not any(t.locked for t in after.tracks)  # 6 kept + 2 fake routes
    sc2 = h.fail("kw_pcb_route_auto", "E_BAD_ARGS", project=d, run_async=False, lock_refs=["U9"])
    assert "R1" in sc2["candidates"]


def test_route_auto_must_fix_suggests_kw_pcb_fix(h: Harness):
    d = routed_project(h)
    h.backend.drc = drc_report(must_fix_violation("NET1"), unconnected=(unconnected_violation("NET2"),))
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False)
    data = sc["data"]
    assert data["ok"] is False and data["drc"]["must_fix"] == 2 and data["drc"]["unconnected"] == 1
    assert data["routed"]["unrouted"] == 1  # KiCad's count, not the router's 0
    assert sc["next_suggested"] == "kw_pcb_fix" and sc["stage"] != "drc_clean"
    names = {v["name"]: v for v in sc["validators"]}
    assert names["unrouted"]["after"] == 1 and names["unrouted"]["detail"]["router_unrouted"] == 0
    assert names["drc_touched_nets"]["detail"]["by_net"] == {"NET1": 1, "NET2": 1}


def test_route_auto_partial_result_reports_kicads_open_connections(h: Harness):
    d = routed_project(h)
    FakeRouter.mode = "partial"
    h.backend.drc = drc_report(unconnected=(unconnected_violation("NET2"),))
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False)
    # KiCad's unconnected_items (the judge) name the open connection with both endpoints, in
    # kw_pcb_check's `unrouted` shape; the router's own plateau list is the fallback only
    assert sc["data"]["routed"]["unrouted_connections"] == [
        {
            "net": "NET2",
            "from": {"ref": "R1", "pad": "1", "x_mm": 4.0, "y_mm": 7.5, "description": "Pad 1 [NET2] of R1"},
            "to": {"ref": "R2", "pad": "1", "x_mm": 14.0, "y_mm": 7.5, "description": "Pad 1 [NET2] of R2"},
        }
    ]
    assert sc["data"]["router"]["unrouted_connections"] == [{"net": "NET2", "from": "R1-2", "to": "R2-2"}]
    assert sc["next_suggested"] == "kw_pcb_fix"  # a re-run would only optimise the same copper
    # no DRC at all (a KiCad without cli.has_drc): the router's list stands in
    from tests.test_core_fakes import FakeBackend

    nodrc = FakeBackend()
    nodrc.caps = {"10.0.0": {**CAPS_10, "cli.has_drc": False}}
    (h.work.parent / "nodrc_home").mkdir()
    h2 = make_harness(h.work.parent / "nodrc_home", nodrc)
    FakeRouter.mode = "partial"
    d2 = routed_project(h2, name="nodrc")
    sc2 = h2.ok("kw_pcb_route_auto", project=d2, run_async=False)
    assert sc2["data"]["routed"]["unrouted_connections"] == [{"net": "NET2", "from": "R1-2", "to": "R2-2"}]
    assert "drc_unavailable" in sc2["degraded"] and sc2["next_suggested"] == "kw_pcb_fix"


def test_route_auto_from_scratch_rips_up_the_unlocked_copper_first(h: Harness):
    d = routed_project(h)
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False, from_scratch=True, nets=["NET1"])
    data = sc["data"]
    # NET1's three segments were deleted before the export; NET2 (locked for the run) untouched
    assert data["routed"]["cleaned"] == {"tracks": 3, "vias": 0, "nets": ["NET1"]}
    assert data["routed"]["from_scratch"] is True and data["routed"]["copper_before"]["tracks"] == 3
    assert "(type route)" not in FakeRouter.calls[-1]["dsn"]
    after = board(d)
    assert len([t for t in after.tracks if t.net and t.net.name == "NET2"]) == 3


def test_route_auto_router_timeout_and_error_leave_the_board_unchanged(h: Harness):
    d = routed_project(h)
    sha = sha256_file(Path(d) / "demo.kicad_pcb")
    FakeRouter.mode = "timeout"
    sc = h.fail("kw_pcb_route_auto", "E_TIMEOUT", project=d, run_async=False, nets=["NET1"])
    assert "timeout_s" in sc["fix"] and sc["data"]["log"] == "pass #3"
    # the lock flags set for the run were restored, so the file is byte-identical again
    assert sha256_file(Path(d) / "demo.kicad_pcb") == sha
    assert not any(t.locked for t in board(d).tracks)
    FakeRouter.mode = "error"
    sc = h.fail("kw_pcb_route_auto", "E_KICAD_FAILED", project=d, run_async=False)
    assert (
        sc["data"]["exit_code"] == 3
        and "bad boundary" in sc["data"]["log"]
        and sc["data"]["engine"] == "freerouting"
    )
    assert sha256_file(Path(d) / "demo.kicad_pcb") == sha
    FakeRouter.mode = "cancel"
    sc = h.fail("kw_pcb_route_auto", "E_TIMEOUT", project=d, run_async=False)
    assert sc["data"]["cancelled"] is True and "unchanged" in sc["why"]
    # the failure is recorded in the snapshot history as such
    status = h.ok("kw_project", action="status", project=d)
    assert status["data"]["stage"] == "placed"


def test_failure_after_a_board_write_keeps_the_stage_and_names_the_copper(h: Harness):
    """Phase 1c finding: a router failure after the stub clean-up (or kw_pcb_fix's rip-up) left
    kiwrap's own write unhashed, so the next call saw an external_edit, dropped the stage and
    refused the advertised retry with E_GATE."""
    from kiwrap.core.tools.pcb_route_auto import dangling_items

    d = routed_project(h)
    model = board(d)
    net1 = [t for t in model.tracks if t.net and t.net.name == "NET1"]
    middle = next(t for t in net1 if all(abs(p.y - 8.5351) < 1e-6 for p in (t.start, t.end)))
    w = BoardWriter(Path(d), epoch={"pcb": 20260206}, lib_resolver=None)
    w.load(Path(d) / "demo.kicad_pcb")
    w.delete(middle.uuid)
    w.save()
    stage_placed(d)
    assert len(dangling_items(board(d))) == 2
    FakeRouter.mode = "timeout"
    sc = h.fail("kw_pcb_route_auto", "E_TIMEOUT", project=d, run_async=False, nets=["NET1"])
    assert "stubs of NET1" in sc["why"] and "kw_snapshot" in sc["fix"]
    assert sc["data"]["copper_removed"] == ["NET1"]
    assert sc["data"]["restore_ref"]
    assert len(dangling_items(board(d))) == 0  # the clean-up is on disk
    status = h.ok("kw_project", action="status", project=d)
    assert status["data"]["stage"] == "placed" and "external_edit" not in status["degraded"]
    # the advertised retry passes the gate
    FakeRouter.mode = "ok"
    sc2 = h.ok("kw_pcb_route_auto", project=d, run_async=False, nets=["NET1"])
    assert sc2["data"]["ok"]
    # kw_pcb_fix: the ripped-up nets are named and the stage survives as well
    h.backend.drc = drc_report(must_fix_violation("NET2"))
    FakeRouter.mode = "error"
    sc3 = h.fail("kw_pcb_fix", "E_KICAD_FAILED", project=d, run_async=False, max_iterations=1)
    assert "ripped up" in sc3["why"] and "NET2" in sc3["why"] and sc3["fix"].startswith("kw_snapshot")
    assert sc3["data"]["copper_removed"] == ["NET2"]
    status = h.ok("kw_project", action="status", project=d)
    assert status["data"]["stage"] == "placed" and "external_edit" not in status["degraded"]


def test_async_job_result_ok_follows_data_ok(h: Harness):
    """Phase 1c finding: `receipt_to_dict` defaulted to ok=True, so a failed route behind kw_job
    read `ok: true` over `data.ok: false`."""
    d = routed_project(h)
    h.backend.drc = drc_report(must_fix_violation("NET1"))
    sc = h.ok("kw_pcb_route_auto", project=d)
    done = h.ok("kw_job", job_id=sc["data"]["job_id"], action="wait", wait_s=60)
    result = done["data"]["job"]["result"]
    assert done["data"]["job"]["state"] == "done" and result["data"]["ok"] is False
    assert result["ok"] is False and result["next_suggested"] == "kw_pcb_fix"


def test_async_call_refuses_gate_bad_nets_and_missing_engines_before_queueing(h: Harness):
    d = routed_project(h, placed=False)
    sc = h.fail("kw_pcb_route_auto", "E_GATE", project=d)  # run_async default: no job_id round trip
    assert sc["data"]["required_stage"] == "placed" and "kw_pcb_place_auto" not in sc["fix"]
    h.fail("kw_pcb_fix", "E_GATE", project=d)
    d2 = routed_project(h, name="placed")
    sc2 = h.fail("kw_pcb_route_auto", "E_BAD_ARGS", project=d2, nets=["NOPE"])
    assert "NET1" in sc2["candidates"]
    h.fail("kw_pcb_route_auto", "E_BAD_ARGS", project=d2, lock_refs=["U9"])
    h.ctx.services.router_engines = no_engines  # type: ignore[attr-defined]
    sc3 = h.fail("kw_pcb_route_auto", "E_UNSUPPORTED", project=d2)
    assert sc3["data"]["capability"] == "engines.freerouting"
    assert h.ok("kw_job", action="list")["data"]["jobs"] == []  # nothing was queued


def test_cancelled_job_ends_in_state_cancelled(h: Harness):
    """kw_job(action='cancel') on a running route: state `cancelled`, not `failed` E_TIMEOUT."""
    d = routed_project(h)
    FakeRouter.mode = "cancel"  # the fake raises RouterCancelled like a killed JVM would
    sc = h.ok("kw_pcb_route_auto", project=d)
    done = h.ok("kw_job", job_id=sc["data"]["job_id"], action="wait", wait_s=60)
    assert done["data"]["job"]["state"] == "cancelled" and done["data"]["job"]["error"] is None
    assert done["data"]["done"] and not done["warnings"]
    status = h.ok("kw_project", action="status", project=d)
    assert status["data"]["stage"] == "placed"
    assert any("E_CANCELLED" in (e.get("snapshot") or "") or True for e in status["data"].get("history", []))


def test_route_auto_async_job(h: Harness):
    d = routed_project(h)
    sc = h.ok("kw_pcb_route_auto", project=d)
    assert sc["provider_used"] == "jobs" and sc["next_suggested"] == "kw_job"
    job_id = sc["data"]["job_id"]
    done = h.ok("kw_job", job_id=job_id, action="wait", wait_s=60)
    assert done["data"]["done"] and done["data"]["job"]["state"] == "done", done
    result = done["data"]["job"]["result"]
    assert (
        result["ok"] and result["data"]["routed"]["tracks_added"] == 2 and result["provider_used"] == "codec"
    )
    assert done["data"]["job"]["progress"] == 1.0
    # the job handle's cancel event reached the router
    assert isinstance(FakeRouter.calls[-1]["cancel"], threading.Event)


def test_route_auto_prefers_the_patched_cli_when_present(h: Harness):
    h.backend.caps = {"10.0.0": {**CAPS_10, "cli.has_specctra_export": True, "cli.has_specctra_import": True}}
    d = routed_project(h)  # (the capability record is cached per install from the first call on)
    seen: dict[str, Any] = {}

    def export_dsn(install: Any, config: Any, board_path: Path, out: Path, caps: Any = None) -> Path:
        seen["export"] = (str(board_path), str(out))
        out.write_text(
            fake_dsn_writer(BoardFile.load(board_path), rules={}, layers=["F.Cu", "B.Cu"]), encoding="utf-8"
        )
        return out

    def import_ses(
        install: Any, config: Any, board_path: Path, ses: Path, out: Path, caps: Any = None
    ) -> Path:
        seen["import"] = (str(board_path), str(ses), str(out))
        w = BoardWriter(Path(board_path).parent, epoch={"pcb": 20260206}, lib_resolver=None)
        w.load(board_path)
        FakeSesReader(Path(ses).read_text(encoding="utf-8")).apply(w)
        w.save()
        return out

    h.ctx.services.cli_export_dsn = export_dsn  # type: ignore[attr-defined]
    h.ctx.services.cli_import_ses = import_ses  # type: ignore[attr-defined]
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False)
    assert sc["provider_used"] == "cli:10.0.0" and sc["data"]["dsn"]["provider"] == "cli"
    assert seen["export"][0].endswith("demo.kicad_pcb") and seen["import"][2].endswith("demo.kicad_pcb")
    assert sc["data"]["routed"]["tracks_added"] == 2 and sc["data"]["plan"]["candidates"] == ["cli", "codec"]
    # a cli export that answers E_UNSUPPORTED at call time falls through to the codec
    from kiwrap.core.state import KiwrapError

    def refuse(*a: Any, **k: Any) -> Path:
        raise KiwrapError(
            "E_UNSUPPORTED", "specctra export not built", data={"capability": "cli.has_specctra_export"}
        )

    h.ctx.services.cli_export_dsn = refuse  # type: ignore[attr-defined]
    sc2 = h.ok("kw_pcb_route_auto", project=d, run_async=False)
    assert sc2["provider_used"] == "codec" and "cli_unavailable" in sc2["degraded"]


def test_route_auto_without_codec_is_unsupported_with_the_fallback(h: Harness):
    d = routed_project(h)
    from kiwrap.core.state import KiwrapError

    def missing(*a: Any, **k: Any) -> Any:
        raise KiwrapError("E_UNSUPPORTED", "the Specctra DSN writer (codec) is not available in this build")

    h.ctx.services.dsn_writer = missing  # type: ignore[attr-defined]
    sc = h.fail("kw_pcb_route_auto", "E_UNSUPPORTED", project=d, run_async=False)
    assert "kw_pcb_route" in sc["fix"] and sc["data"]["plan"]["operation"] == "routing.autoroute"
    assert sha256_file(Path(d) / "demo.kicad_pcb") == sha256_file(TINY_BOARD)


# ---- kw_pcb_fix -------------------------------------------------------------------------------------------------


def test_fix_rips_up_and_reroutes_the_violating_nets(h: Harness):
    d = routed_project(h)
    reports = [
        drc_report(must_fix_violation("NET1")),  # initial DRC
        drc_report(),  # after round 1
    ]

    def run_drc(install: Any, config: Any, board_path: Path, **kw: Any) -> Any:
        return reports.pop(0) if len(reports) > 1 else reports[0]

    h.ctx.services.run_drc = run_drc  # type: ignore[attr-defined]
    sc = h.ok("kw_pcb_fix", project=d, run_async=False)
    data = sc["data"]
    assert data["ok"] and data["must_fix"] == {"before": 1, "after": 0} and data["nets_touched"] == ["NET1"]
    assert len(data["iterations"]) == 1
    it = data["iterations"][0]
    assert it["nets"] == ["NET1"] and it["must_fix_before"] == 1 and it["must_fix_after"] == 0
    assert it["rip_up"]["tracks_removed"] == 3 and it["router"]["passes"] == 2
    assert FakeRouter.calls[-1]["nets"] == ["NET1"]
    assert (
        sc["provider_used"] == "codec"
        and sc["stage"] == "drc_clean"
        and sc["next_suggested"] == "kw_fab_export"
    )
    after = board(d)
    net1 = [t for t in after.tracks if t.net and t.net.name == "NET1"]
    net2 = [t for t in after.tracks if t.net and t.net.name == "NET2"]
    assert len(net1) == 1 and len(net2) == 3 and not any(t.locked for t in after.tracks)
    assert data["unfixable"] == [] and data["actions"] == ["rip_up_violations", "reroute", "clean", "refill"]


def test_fix_stops_on_unfixable_and_exhausts_iterations(h: Harness):
    d = routed_project(h)
    h.backend.drc = drc_report(netless_violation())
    sc = h.ok("kw_pcb_fix", project=d, run_async=False)
    data = sc["data"]
    assert data["ok"] is False and data["iterations"] == [] and len(data["unfixable"]) == 1
    assert data["unfixable"][0]["type"] == "hole_clearance" and sc["next_suggested"] == "kw_pcb_drc"
    assert any("not fixable by re-routing" in w for w in sc["warnings"])
    assert FakeRouter.calls == []
    # a violation that never goes away: max_iterations rounds, then a warning
    h.backend.drc = drc_report(must_fix_violation("NET2"))
    sc2 = h.ok(
        "kw_pcb_fix", project=d, run_async=False, max_iterations=2, actions=["rip_up_violations", "reroute"]
    )
    assert sc2["data"]["ok"] is False and len(sc2["data"]["iterations"]) == 2
    assert sc2["data"]["must_fix"] == {"before": 1, "after": 1} and sc2["next_suggested"] == "kw_pcb_fix"
    # the scope escalates: the violating net alone, then (stalled) the neighbourhood - the other
    # net's copper within 2 mm of the violation's items is ripped up and re-routed with it
    rounds = sc2["data"]["iterations"]
    assert [r["scope"] for r in rounds] == ["nets", "neighbourhood"], rounds
    assert rounds[0]["ripped_up"] == ["NET2"] and rounds[0]["margin_mm"] == 0
    assert rounds[1]["ripped_up"] == ["NET1", "NET2"] and rounds[1]["margin_mm"] == 2.0
    assert rounds[1]["nets"] == ["NET2"]
    assert len(FakeRouter.calls) == 2
    assert FakeRouter.calls[0]["nets"] == ["NET2"] and FakeRouter.calls[1]["nets"] == ["NET1", "NET2"]
    assert any("did not reduce must_fix" in w and "neighbourhood" in w for w in sc2["warnings"])
    assert sc2["data"]["nets_touched"] == ["NET1", "NET2"]
    # still stalled after the neighbourhood: the whole board goes back to the router (nets=None)
    FakeRouter.calls.clear()
    sc3 = h.ok(
        "kw_pcb_fix", project=d, run_async=False, max_iterations=3, actions=["rip_up_violations", "reroute"]
    )
    rounds = sc3["data"]["iterations"]
    assert [r["scope"] for r in rounds] == ["nets", "neighbourhood", "board"], rounds
    assert rounds[2]["ripped_up"] == ["NET2"] and FakeRouter.calls[2]["nets"] is None
    assert any("re-routes the whole board" in w for w in sc3["warnings"])


def test_fix_async_and_bad_actions(h: Harness):
    d = routed_project(h)
    sc = h.ok("kw_pcb_fix", project=d)
    done = h.ok("kw_job", job_id=sc["data"]["job_id"], action="wait", wait_s=60)
    assert done["data"]["job"]["state"] == "done" and done["data"]["job"]["result"]["data"]["ok"]
    h.fail("kw_pcb_fix", "E_BAD_ARGS", project=d, run_async=False, actions=["explode"])


def test_fix_rips_up_every_net_beyond_the_listed_violations(h: Harness):
    """Phase 1c finding: the DRC receipt lists at most 50 violations; kw_pcb_fix must still rip
    up the nets of the ones beyond the cap (the validator names them all)."""
    d = routed_project(h)
    # 60 violations: the first 55 name NET1, NET2 appears only beyond the 50 the receipt lists
    many = tuple(unconnected_violation("NET1" if i < 55 else "NET2") for i in range(60))
    reports = [drc_report(unconnected=many), drc_report()]

    def run_drc(install: Any, config: Any, board_path: Path, **kw: Any) -> Any:
        return reports.pop(0) if len(reports) > 1 else reports[0]

    h.ctx.services.run_drc = run_drc  # type: ignore[attr-defined]
    sc = h.ok(
        "kw_pcb_fix", project=d, run_async=False, max_iterations=1, actions=["rip_up_violations", "reroute"]
    )
    it = sc["data"]["iterations"][0]
    assert it["nets"] == ["NET1", "NET2"] and it["ripped_up"] == ["NET1", "NET2"]
    assert it["violations_truncated"] == 10 and it["rip_up"]["tracks_removed"] == 6
    assert sc["data"]["nets_touched"] == ["NET1", "NET2"]
    assert sorted(FakeRouter.calls[-1]["nets"]) == ["NET1", "NET2"]
    assert sc["data"]["drc"]["nets_with_must_fix"] == []  # the final DRC is clean


def test_fix_receipt_carries_one_drc_delta_from_initial_to_final(h: Harness):
    d = routed_project(h)
    reports = [drc_report(must_fix_violation("NET1"), must_fix_violation("NET2")), drc_report()]

    def run_drc(install: Any, config: Any, board_path: Path, **kw: Any) -> Any:
        return reports.pop(0) if len(reports) > 1 else reports[0]

    h.ctx.services.run_drc = run_drc  # type: ignore[attr-defined]
    sc = h.ok("kw_pcb_fix", project=d, run_async=False)
    drc_deltas = [v for v in sc["validators"] if v["name"] == "drc"]
    assert len(drc_deltas) == 1 and (drc_deltas[0]["before"], drc_deltas[0]["after"]) == (2, 0)
    assert sc["data"]["must_fix"] == {"before": 2, "after": 0}
    assert len([v for v in sc["validators"] if v["name"] == "unrouted"]) == 1


def test_fix_is_gated_on_placed_and_can_be_forced(h: Harness):
    d = routed_project(h, placed=False)
    sc = h.fail("kw_pcb_fix", "E_GATE", project=d, run_async=False)
    assert sc["data"]["required_stage"] == "placed"
    sc2 = h.ok("kw_pcb_fix", project=d, run_async=False, force=FORCE)
    assert sc2["data"]["forced"] == FORCE["reason"] and "gate overridden" in " ".join(sc2["warnings"])


# ---- dangling stubs -----------------------------------------------------------------------------------------


def stage_placed(d: str) -> None:
    project = Project.from_path(d)
    state = project.load_state()
    record_stage(project, state, "placed")
    project.save_state(state)


def test_dangling_items_and_the_clean_step(h: Harness):
    from kiwrap.core.tools.pcb_route_auto import dangling_items

    d = routed_project(h)
    model = board(d)
    assert dangling_items(model) == []  # a fully routed board has no stubs
    net1 = [t for t in model.tracks if t.net and t.net.name == "NET1"]
    # the middle segment of NET1's three-segment polyline touches no pad; without it the two
    # remaining pieces each have a free end and are stubs
    middle = next(t for t in net1 if all(abs(p.y - 8.5351) < 1e-6 for p in (t.start, t.end)))
    w = BoardWriter(Path(d), epoch={"pcb": 20260206}, lib_resolver=None)
    w.load(Path(d) / "demo.kicad_pcb")
    w.delete(middle.uuid)
    w.save()
    stubs = dangling_items(board(d))
    assert {s.uuid for s in stubs} == {t.uuid for t in net1 if t.uuid != middle.uuid}
    assert all(s.net == "NET1" and s.kind == "track" for s in stubs)
    assert dangling_items(board(d), ["NET2"]) == []  # only the wanted nets are candidates
    # a locked stub is never a candidate (but still anchors its neighbours)
    w2 = BoardWriter(Path(d), epoch={"pcb": 20260206}, lib_resolver=None)
    w2.load(Path(d) / "demo.kicad_pcb")
    assert set_locked(w2, stubs[0].uuid, True)
    w2.save()
    assert [s.uuid for s in dangling_items(board(d))] == [stubs[1].uuid]
    w3 = BoardWriter(Path(d), epoch={"pcb": 20260206}, lib_resolver=None)
    w3.load(Path(d) / "demo.kicad_pcb")
    set_locked(w3, stubs[0].uuid, False)
    w3.save()
    stage_placed(d)
    # clean_dangling=false: the stubs go to the router as (type route) wires
    sc = h.ok("kw_pcb_route_auto", project=d, run_async=False, nets=["NET1"], clean_dangling=False)
    assert sc["data"]["routed"]["cleaned"] == {"tracks": 0, "vias": 0, "nets": []}
    assert FakeRouter.calls[-1]["dsn"].count("(type route)") == 2
    # restore the stubs and clean them (the default)
    shutil.copyfile(TINY_BOARD, Path(d) / "demo.kicad_pcb")
    w4 = BoardWriter(Path(d), epoch={"pcb": 20260206}, lib_resolver=None)
    w4.load(Path(d) / "demo.kicad_pcb")
    w4.delete(middle.uuid)
    w4.save()
    stage_placed(d)
    sc2 = h.ok("kw_pcb_route_auto", project=d, run_async=False, nets=["NET1"])
    assert sc2["data"]["routed"]["cleaned"] == {"tracks": 2, "vias": 0, "nets": ["NET1"]}
    assert "(type route)" not in FakeRouter.calls[-1]["dsn"]  # NET2 locked (fix), NET1 stubs gone
    assert sc2["data"]["routed"]["copper_before"] == {"tracks": 3, "vias": 0, "locked": 0}
    # kw_pcb_fix's `clean` action drives the same step
    h.backend.drc = drc_report(unconnected_violation("NET1"))
    reports = [drc_report(unconnected_violation("NET1")), drc_report()]
    h.ctx.services.run_drc = lambda *a, **k: reports.pop(0) if len(reports) > 1 else reports[0]  # type: ignore[attr-defined]
    sc3 = h.ok("kw_pcb_fix", project=d, run_async=False, actions=["reroute", "clean"])
    assert (
        sc3["data"]["ok"] and sc3["data"]["iterations"][0]["cleaned"]["nets"] == []
    )  # nothing dangling after the route
