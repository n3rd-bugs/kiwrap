"""Every kw_* tool against faked lower layers: happy paths, every KiwrapError code, gates,
external-edit detection, snapshots and jobs - no KiCad involved."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.core.server import build_context, build_server
from kiwrap.core.state import STAGES, KiwrapError
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools import ALL_TOOLS, TOOL_NAMES, templates
from kiwrap.core.tools import help as help_tool
from kiwrap.core.tools._base import ToolContext, to_kiwrap_error
from kiwrap.core.tools.checks import classify_drc
from tests.test_core_fakes import (
    CAPS_7,
    CAPS_10,
    CliLoadError,
    FakeBackend,
    FakeInstall,
    Unsupported,
    drc_report,
    erc_report,
    violation,
)

ERROR_CODES = (
    "E_NO_KICAD",
    "E_UNSUPPORTED",
    "E_PROJECT_NOT_FOUND",
    "E_BAD_ARGS",
    "E_KICAD_FAILED",
    "E_FILE_LOCKED",
    "E_BUSY",
    "E_GATE",
    "E_EXTERNAL_EDIT",
    "E_TIMEOUT",
    "E_CANCELLED",
    "E_INTERNAL",
    "E_LIB_NOT_FOUND",
    "E_LIB_UNSUPPORTED",
    "E_LIB",
    "E_READONLY",
    "E_FORMAT_UPGRADE_REQUIRED",
    "E_FORMAT_NEWER",
    "E_STALE",
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")


class Harness:
    """A server with faked services plus a synchronous `call()` returning (is_error, structured)."""

    def __init__(self, tmp_path: Path, backend: FakeBackend | None = None):
        self.home = tmp_path / "home"
        self.backend = backend or FakeBackend()
        self.ctx = ToolContext(
            services=self.backend.services(self.home),
            jobs=JobQueue(self.home / "jobs"),
            server_version="test",
        )
        self.server = build_server(context=self.ctx)
        self.work = tmp_path / "work"
        self.work.mkdir()

    def call(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any], Any]:
        """Like the wire: a schema violation the SDK rejects before our code runs is an isError text result."""
        import anyio
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import CallToolResult, TextContent

        try:
            result = anyio.run(self.server.call_tool, tool, args)
        except ToolError as exc:
            result = CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
        return result.is_error, result.structured_content, result

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        err, sc, _ = self.call(tool, **args)
        assert not err, sc
        ReceiptOut.model_validate(sc)
        return sc

    def fail(self, tool: str, code: str, **args: Any) -> dict[str, Any]:
        err, sc, _ = self.call(tool, **args)
        assert err, sc
        assert sc["code"] == code, sc
        return sc

    def create(self, name: str = "demo", **extra: Any) -> str:
        sc = self.ok("kw_project", action="create", dir=str(self.work / name), name=name, **extra)
        return sc["data"]["dir"]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# ---- catalog ------------------------------------------------------------------------------------------


def test_catalog_is_deterministic_and_annotated():
    assert TOOL_NAMES == (
        "kw_env",
        "kw_project",
        "kw_project_upgrade",
        "kw_snapshot",
        "kw_job",
        "kw_help",
        "kw_intent",
        "kw_lib_search",
        "kw_lib_info",
        "kw_sch_add_symbol",
        "kw_sch_connect",
        "kw_sch_add_item",
        "kw_sch_edit",
        "kw_sch_annotate",
        "kw_sch_assign_footprints",
        "kw_sch_get",
        "kw_sch_check",
        "kw_sch_stats",
        "kw_sch_diff",
        "kw_sch_render",
        "kw_pcb_setup",
        "kw_pcb_sync",
        "kw_pcb_place",
        "kw_pcb_zones",
        "kw_pcb_route",
        "kw_pcb_route_auto",
        "kw_pcb_fix",
        "kw_pcb_get",
        "kw_pcb_check",
        "kw_pcb_drc",
        "kw_pcb_render",
        "kw_fab_bom",
        "kw_fab_export",
        "kw_live",
    )
    for spec in ALL_TOOLS:
        assert spec.name.startswith("kw_") and spec.description
        assert spec.annotations.read_only_hint is not None and spec.annotations.destructive_hint is not None


def test_list_tools_schemas(h: Harness):
    import anyio

    tools = anyio.run(h.server.list_tools)
    assert [t.name for t in tools] == list(TOOL_NAMES)
    for t in tools:
        assert t.input_schema["type"] == "object"
        assert t.output_schema and "changed" in t.output_schema["properties"]
        assert t.annotations is not None
    env = next(t for t in tools if t.name == "kw_env")
    assert env.annotations.read_only_hint is True
    assert "refresh" in env.input_schema["properties"]
    fab = next(t for t in tools if t.name == "kw_fab_export")
    assert fab.input_schema["required"] == ["project"]
    # numeric constraints from the pydantic models reach the wire (finding: they were dropped)
    render = next(t for t in tools if t.name == "kw_pcb_render")
    max_px = render.input_schema["properties"]["max_px"]
    assert max_px["minimum"] == 128 and max_px["maximum"] == 1024 and max_px["description"]
    job = next(t for t in tools if t.name == "kw_job")
    assert job.input_schema["properties"]["wait_s"]["minimum"] == 0
    snapshot = next(t for t in tools if t.name == "kw_snapshot")
    assert snapshot.input_schema["properties"]["limit"]["maximum"] == 500
    sch = next(t for t in tools if t.name == "kw_sch_check")
    assert sch.input_schema["properties"]["severity"]["enum"] == ["all", "error", "warning", "exclusions"]
    # D24: kw_project reads and pins; converting is kw_project_upgrade; no tool publishes `confirm`
    project = next(t for t in tools if t.name == "kw_project")
    assert project.input_schema["properties"]["action"]["enum"] == [
        "create",
        "status",
        "retarget",
        "upgrade_preview",
    ]
    assert project.annotations is not None and project.annotations.destructive_hint is False
    for t in tools:
        assert "confirm" not in t.input_schema["properties"], t.name


# ---- kw_env / kw_help ---------------------------------------------------------------------------------


def test_env_lists_installs_and_capabilities(h: Harness):
    sc = h.ok("kw_env")
    d = sc["data"]
    assert d["installs"][0]["version"] == "10.0.0"
    assert d["installs"][0]["capabilities"]["cli.has_drc"] is True
    assert d["installs"][0]["capabilities"]["formats.sch"] == 20260306
    assert d["default_install"] == "10.0.0" and d["storage"]["home"] == str(h.home)
    assert set(d["engines"]) == {"freerouting", "jre", "krt", "jlcparts", "autorouter"}
    # the resolved autorouter (JRE + jar the routing tools start), not a bare directory flag
    assert set(d["engines"]["autorouter"]) >= {"available"}
    if d["engines"]["autorouter"]["available"]:
        assert d["engines"]["autorouter"]["freerouting_version"] and d["engines"]["autorouter"]["java"]
    n = len([c for c in h.backend.calls if c[0] == "discover"])
    h.ok("kw_env")
    assert len([c for c in h.backend.calls if c[0] == "discover"]) == n  # cached
    h.ok("kw_env", refresh=True)
    assert len([c for c in h.backend.calls if c[0] == "discover"]) == n + 1


def test_env_without_kicad_warns(tmp_path: Path):
    h = Harness(tmp_path, FakeBackend(installs=[]))
    sc = h.ok("kw_env")
    assert sc["data"]["installs"] == [] and any("E_NO_KICAD" in w for w in sc["warnings"])


def test_help_has_every_error_code_and_tool(h: Harness):
    assert set(help_tool.ERROR_PAGES) == set(ERROR_CODES)
    assert set(help_tool.TOOL_PAGES) == set(TOOL_NAMES)
    for code in ERROR_CODES:
        md = h.ok("kw_help", topic=code)["data"]["markdown"]
        assert md.startswith(f"# {code}")
    assert "kw_env" in h.ok("kw_help")["data"]["markdown"]
    sc = h.ok("kw_help", topic="fab")
    assert "kw_fab_export" in sc["data"]["candidates"] and sc["warnings"]
    assert "E_GATE" in h.ok("kw_help", topic="errors")["data"]["markdown"]


# ---- kw_project -------------------------------------------------------------------------------------


def test_help_topic_lookup_is_forgiving_and_consistent(h: Harness):
    """Finding: ' E_GATE ' returned the right page but warned 'unknown topic'; 'e_gate' returned the
    overview. The warning and the page now come from one normalisation."""
    for topic in (" E_GATE ", "e_gate", "E_GATE"):
        sc = h.ok("kw_help", topic=topic)
        assert sc["data"]["markdown"].startswith("# E_GATE") and sc["warnings"] == [], topic
        assert sc["data"]["topic"] == "E_GATE"
    sc = h.ok("kw_help", topic="KW_PCB_RENDER")
    assert sc["data"]["markdown"].startswith("# kw_pcb_render")
    md = sc["data"]["markdown"]
    assert "pypdfium2" in md and "layers" in md and "max_px" in md and "SVG fallback" not in md
    sc = h.ok("kw_help", topic="kw_sch_render")
    assert "sch export pdf" in sc["data"]["markdown"] and "SVG" not in sc["data"]["markdown"]
    sc = h.ok("kw_help", topic="nonsense")
    assert sc["warnings"] and sc["data"]["markdown"].startswith("# kiwrap")


def test_project_create_status_and_upgrade(h: Harness):
    sc = h.ok(
        "kw_project",
        action="create",
        dir=str(h.work / "demo"),
        name="demo",
        target_kicad="10",
        board_size_mm=[50, 30],
    )
    assert sc["stage"] == "created" and sc["snapshot"] and sc["next_suggested"] == "kw_sch_check"
    assert sc["changed"]["added"] == ["demo.kicad_pro", "demo.kicad_sch", "demo.kicad_pcb"]
    d = sc["data"]
    assert d["target_kicad"] == "10.0.0" and d["epochs"] == {"sch": 20260306, "pcb": 20260206}
    proj = Path(d["dir"])
    sch = (proj / "demo.kicad_sch").read_text()
    pcb = (proj / "demo.kicad_pcb").read_text()
    assert "(version 20260306)" in sch and '(generator_version "10.0")' in sch and "(lib_symbols)" in sch
    assert "(version 20260206)" in pcb and '(2 "B.Cu" signal)' in pcb and pcb.count("gr_line") == 4
    assert json.loads((proj / "demo.kicad_pro").read_text())["meta"]["filename"] == "demo.kicad_pro"
    assert (proj / ".kiwrap" / "state.json").exists() and (proj / ".kiwrap" / "snapshots.git").is_dir()
    assert not (proj / ".git").exists()
    st = h.ok("kw_project", action="status", project=str(proj))["data"]
    assert (
        st["stage"] == "created" and st["target_installed"] is True and st["snapshot_head"] == sc["snapshot"]
    )
    assert st["epochs"] == {"sch": 20260306, "pcb": 20260206} and st["files"]["sch"] == ["demo.kicad_sch"]
    # D21: a created project runs on its own KiCad, which writes its format
    assert (
        st["target_effective"] == "10.0.0" and st["target_source"] == "created" and st["epoch_match"] is True
    )
    # D24: no write needs an upgrade - status has no upgrade_args, the preview converts nothing and
    # kw_project_upgrade refuses with a teaching error naming retarget (nothing is written)
    assert st["upgrade_args"] is None and st["retarget_candidates"] == []
    pre = h.ok("kw_project", action="upgrade_preview", project=str(proj))
    assert pre["data"]["upgrade_args"] is None and "nothing to convert" in pre["warnings"][0]
    design_before = {f.name: f.read_bytes() for f in proj.glob("demo.kicad_*")}
    sc = h.fail(
        "kw_project_upgrade",
        "E_BAD_ARGS",
        project=str(proj / "demo.kicad_pro"),
        from_kicad="10.0",
        to_kicad="10",
        files=["demo.kicad_sch"],
    )
    assert "nothing to convert" in sc["message"] and "kw_project(action='retarget')" in sc["candidates"]
    assert sc["data"]["upgrade_args"]["files"] == []
    assert {f.name: f.read_bytes() for f in proj.glob("demo.kicad_*")} == design_before


def test_project_create_old_dialect(tmp_path: Path):
    h = Harness(tmp_path, FakeBackend(installs=[FakeInstall(version="7.0.6")], caps={"7.0.6": CAPS_7}))
    d = h.ok("kw_project", action="create", dir=str(h.work / "old"), name="old")["data"]
    sch = (Path(d["dir"]) / "old.kicad_sch").read_text()
    pcb = (Path(d["dir"]) / "old.kicad_pcb").read_text()
    assert "generator_version" not in sch and "(version 20230121)" in sch
    assert '(31 "B.Cu" signal)' in pcb and "(version 20221018)" in pcb and "gr_line" not in pcb


def test_project_create_errors(h: Harness):
    h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(h.work / "x"))
    h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(h.work / "x"), name="bad name")
    h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(h.work / "x"), name="x", board_size_mm=[1])
    h.fail("kw_project", "E_NO_KICAD", action="create", dir=str(h.work / "x"), name="x", target_kicad="99")
    h.create("dup")
    sc = h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(h.work / "dup"), name="dup2")
    assert sc["candidates"] == ["dup.kicad_pro"]
    h.fail("kw_project", "E_BAD_ARGS", action="status")
    h.fail("kw_project", "E_PROJECT_NOT_FOUND", action="status", project=str(h.work / "nope"))
    # SDK schema validation (bad enum, missing required field, wrong type) and unknown tool names
    # are rejected before our tool function runs; KiwrapServer still answers a teaching error.
    sc = h.fail("kw_project", "E_BAD_ARGS", action="explode", project="x")
    assert "action" in sc["message"] and sc["fix"]
    sc = h.fail("kw_project", "E_BAD_ARGS")
    assert "action" in sc["message"] and "required" in sc["message"].lower()
    sc = h.fail("kw_fab_export", "E_BAD_ARGS", project="x", targets="gerbers")
    assert "targets" in sc["message"]
    sc = h.fail("kw_report", "E_BAD_ARGS")
    assert "unknown tool" in sc["message"] and sc["candidates"] == list(TOOL_NAMES)


def test_templates_capability_keys():
    class C(dict):
        pass

    # The dialect switches come from the capability record only (no epoch arithmetic in core);
    # a record without the key selects the older dialect every newer reader accepts (claim 9).
    assert templates.layer_ids_v2_for(C({"formats.pcb_layer_ids_v2": False})) is False
    assert templates.layer_ids_v2_for(C({"formats.pcb_layer_ids_v2": True})) is True
    assert templates.layer_ids_v2_for(C()) is False and templates.layer_ids_v2_for(None) is False
    inst = FakeInstall(version="9.0.5")
    assert templates.generator_version_for(C({"formats.generator_version_token": True}), inst) == "9.0"
    assert templates.generator_version_for(C({"formats.generator_version_token": False}), inst) is None
    assert templates.generator_version_for(C(), inst) is None
    assert templates.project_text("x", None).startswith("{") and templates.project_text(
        "x", Path("C:/nowhere")
    )


# ---- kw_sch_check -----------------------------------------------------------------------------------


def test_sch_check_ok_advances_to_erc_clean(h: Harness):
    proj = h.create()
    sc = h.ok("kw_sch_check", project=proj)
    assert sc["ok"] and sc["stage"] == "erc_clean" and sc["provider_used"] == "cli:10.0.0"
    assert sc["validators"] == [{"name": "erc", "before": None, "after": 0, "detail": {"warnings": 0}}]
    assert sc["data"]["netlist"]["components"] == 1 and sc["data"]["netlist"]["nets"] == 1
    assert sc["next_suggested"] == "kw_pcb_sync" and sc["snapshot"]
    run = next(c for c in h.backend.calls if c[0] == "run_erc")
    assert run[1]["units"] == "mm" and run[1]["schematic"].endswith("demo.kicad_sch")


def test_sch_check_surfaces_a_corrected_erc_position_scale(h: Harness):
    """`kiwrap.backends.cli.erc.run_erc` records `degraded: ['erc_pos_scale_corrected']` and a
    warning in `report.raw['_cli']` when the schematic's anchors contradict the matrix's
    `cli.erc_json_pos_scale` (upstream fixed the x100 in 10.0 6d8e1fea / master 407f6293, #25582);
    kw_sch_check's receipt carries both, once, on every run until the matrix is corrected."""
    import dataclasses

    proj = h.create()
    note = "ERC JSON position scale: the matrix says 100, the schematic anchors say 1 (3/3 items)"
    rep = erc_report(violation("pin_not_connected", "error", 12.7, 25.4))
    h.backend.erc = dataclasses.replace(
        rep,
        raw={
            "_cli": {"degraded": ["erc_pos_scale_corrected", "erc_pos_scale_corrected"], "warnings": [note]}
        },
    )
    for _ in range(2):
        sc = h.ok("kw_sch_check", project=proj)
        assert sc["degraded"].count("erc_pos_scale_corrected") == 1 and sc["warnings"].count(note) == 1
    # a report without the backend's notes (or a malformed one) adds nothing
    h.backend.erc = dataclasses.replace(rep, raw={"_cli": "not a dict"})
    sc = h.ok("kw_sch_check", project=proj)
    assert "erc_pos_scale_corrected" not in sc["degraded"] and note not in sc["warnings"]


def test_sch_check_errors_and_warnings(h: Harness):
    proj = h.create()
    h.backend.erc = erc_report(
        violation("pin_not_connected", "error", 12.7, 25.4), violation("lib_symbol_mismatch", "warning")
    )
    sc = h.ok("kw_sch_check", project=proj)
    # a failing check suggests the FIXING tool, never itself (pin_not_connected -> a wire)
    assert not sc["ok"] and sc["stage"] == "created" and sc["next_suggested"] == "kw_sch_connect"
    v = sc["data"]["violations"][0]
    assert v["pos"] == {"x_mm": 12.7, "y_mm": 25.4} and v["items"][0]["uuid"].startswith("1111")
    assert sc["data"]["errors"] == 1 and sc["data"]["warnings"] == 1
    # warnings alone never gate
    h.backend.erc = erc_report(violation("lib_symbol_mismatch", "warning"))
    sc = h.ok("kw_sch_check", project=proj)
    assert sc["ok"] and sc["stage"] == "erc_clean"
    assert sc["validators"][0]["before"] == 1 and sc["validators"][0]["after"] == 0


def test_sch_check_netlist_failure_degrades(h: Harness):
    proj = h.create()
    h.backend.net = Unsupported("netlist", FakeInstall())
    sc = h.ok("kw_sch_check", project=proj)
    assert sc["ok"] and "netlist_unavailable" in sc["degraded"] and sc["data"]["netlist"] is None


def test_sch_check_kicad_errors(h: Harness):
    proj = h.create()
    h.backend.erc = CliLoadError("Failed to load schematic")
    sc = h.fail("kw_sch_check", "E_KICAD_FAILED", project=proj)
    assert sc["data"]["returncode"] == 3
    h.backend.erc = Unsupported(
        "cli.has_erc", FakeInstall("7.0.6"), oldest_kicad_with_it="8.0.0", fallback=None
    )
    sc = h.fail("kw_sch_check", "E_UNSUPPORTED", project=proj)
    assert sc["data"]["oldest_kicad_with_it"] == "8.0.0"
    # finding: candidates name the installed KiCads that HAVE the capability and the fix is a
    # kw_* call, not "open the schematic in eeschema" / "use the fallback (swig ...)"
    assert sc["candidates"] == ["10.0.0"] and "kw_project(action='create', target_kicad='8')" in sc["fix"]
    assert "fallback" not in sc["fix"]
    h.backend.erc = subprocess.TimeoutExpired(["kicad-cli"], 300)
    h.fail("kw_sch_check", "E_TIMEOUT", project=proj)
    h.backend.erc = RuntimeError("boom")
    sc = h.fail("kw_sch_check", "E_INTERNAL", project=proj)
    assert "traceback" in sc["data"]
    h.fail("kw_sch_check", "E_PROJECT_NOT_FOUND", project=str(h.work / "none"))


# ---- kw_pcb_drc -------------------------------------------------------------------------------------


def test_classify_drc_tables():
    kw = {"must_fix": ("clearance",), "review": ("courtyards_overlap",), "cosmetic_prefix": "silk_"}
    assert classify_drc(violation("clearance", "error"), **kw) == "must_fix"
    assert classify_drc(violation("clearance", "warning"), **kw) == "review"
    assert classify_drc(violation("courtyards_overlap", "error"), **kw) == "review"
    assert classify_drc(violation("silk_over_copper", "error"), **kw) == "cosmetic"
    assert classify_drc(violation("invalid_outline", "error"), **kw) == "must_fix"
    assert classify_drc(violation("footprint_type_mismatch", "warning"), **kw) == "review"


def test_pcb_drc_classification_and_stage(h: Harness):
    proj = h.create()
    h.backend.drc = drc_report(
        violation("clearance", "error", sheet=None),
        violation("courtyards_overlap", "error", sheet=None),
        violation("silk_overlap", "warning", sheet=None),
        unconnected=(violation("unconnected_items", "error", sheet=None),),
    )
    sc = h.ok("kw_pcb_drc", project=proj, schematic_parity=True)
    d = sc["data"]
    assert (
        not d["ok"]
        and d["must_fix"] == 2
        and d["review"] == 1
        and d["cosmetic"] == 1
        and d["unconnected"] == 1
    )
    assert [v["class"] for v in d["violations"]] == ["must_fix", "must_fix", "review", "cosmetic"]
    assert sc["stage"] == "created" and sc["validators"][0]["after"] == 2
    assert next(c for c in h.backend.calls if c[0] == "run_drc")[1]["schematic_parity"] is True
    h.backend.drc = drc_report(violation("silk_overlap", "warning", sheet=None))
    sc = h.ok("kw_pcb_drc", project=proj)
    assert sc["ok"] and sc["stage"] == "drc_clean" and sc["next_suggested"] == "kw_fab_export"
    assert sc["validators"][0]["before"] == 2 and sc["validators"][0]["after"] == 0


def test_pcb_drc_failure_downgrades(h: Harness):
    proj = h.create()
    h.ok("kw_sch_check", project=proj)
    h.ok("kw_pcb_drc", project=proj)
    assert h.ok("kw_project", action="status", project=proj)["data"]["stage"] == "drc_clean"
    h.backend.drc = drc_report(violation("clearance", "error", sheet=None))
    sc = h.ok("kw_pcb_drc", project=proj)
    assert sc["stage"] == "erc_clean" and sc["next_suggested"] == "kw_pcb_route"
    h.backend.drc = drc_report(
        violation("copper_edge_clearance", "error", sheet=None), violation("clearance", "error", sheet=None)
    )
    assert h.ok("kw_pcb_drc", project=proj)["next_suggested"] == "kw_pcb_place"
    h.backend.drc = drc_report(parity=(violation("footprint_type_mismatch", "error", sheet=None),))
    assert h.ok("kw_pcb_drc", project=proj, schematic_parity=True)["next_suggested"] == "kw_pcb_sync"
    h.backend.erc = erc_report(violation("power_pin_not_driven", "error"))
    assert h.ok("kw_sch_check", project=proj)["next_suggested"] == "kw_sch_add_item"


def test_pcb_drc_saving_refill_is_not_an_external_edit(h: Harness):
    """kw_pcb_drc(refill_zones, save_board) lets kicad-cli rewrite the board: the board-derived
    stage hashes must follow, or the next call downgrades the project to erc_clean with
    `external_edit` (reviewer finding: kw_fab_export was then gated out)."""
    proj = h.create()
    h.ok("kw_sch_check", project=proj)
    h.ok("kw_pcb_drc", project=proj)
    assert h.ok("kw_project", action="status", project=proj)["data"]["stage"] == "drc_clean"
    board = h.work / "demo" / "demo.kicad_pcb"
    real_run_drc = h.backend.run_drc

    def run_drc_that_saves(install: Any, config: Any, board_path: Path, **kw: Any) -> Any:
        if kw.get("save_board"):
            Path(board_path).write_text(
                Path(board_path).read_text(encoding="utf-8") + "\n(zone (filled_polygon))\n", encoding="utf-8"
            )
        return real_run_drc(install, config, board_path, **kw)

    h.ctx.services.run_drc = run_drc_that_saves  # type: ignore[method-assign]
    sha_before = __import__("hashlib").sha256(board.read_bytes()).hexdigest()
    sc = h.ok("kw_pcb_drc", project=proj, refill_zones=True, save_board=True)
    assert (
        sc["stage"] == "drc_clean" and sc["changed"]["updated"] == ["demo.kicad_pcb"] and sc["degraded"] == []
    )
    assert __import__("hashlib").sha256(board.read_bytes()).hexdigest() != sha_before
    status = h.ok("kw_project", action="status", project=proj)
    assert status["data"]["stage"] == "drc_clean" and "external_edit" not in status["degraded"], status
    # a DRC that does not save leaves the receipt's changed list empty
    sc = h.ok("kw_pcb_drc", project=proj)
    assert sc["changed"]["updated"] == [] and sc["stage"] == "drc_clean"


def test_pcb_drc_refill_unsupported(h: Harness):
    proj = h.create()
    h.backend.drc = Unsupported(
        "cli.drc_refill_flags", FakeInstall("9.0.5"), oldest_kicad_with_it="10.0.0", fallback="swig fill"
    )
    sc = h.fail("kw_pcb_drc", "E_UNSUPPORTED", project=proj, refill_zones=True)
    assert sc["data"]["fallback"] == "swig fill"
    assert "swig fill" not in sc["fix"] and sc["candidates"] == ["10.0.0"]


def test_old_dialect_project_gets_no_impossible_suggestion(tmp_path: Path):
    """Finding: a KiCad 7 project kept suggesting kw_sch_check, which fails identically every time."""
    backend = FakeBackend(
        installs=[FakeInstall("7.0.6"), FakeInstall("10.0.0")], caps={"7.0.6": CAPS_7, "10.0.0": CAPS_10}
    )
    h = Harness(tmp_path, backend)
    proj = h.create("old", target_kicad="7")
    sc = h.ok("kw_project", action="status", project=proj)
    assert sc["data"]["target_kicad"] == "7.0.6" and sc["next_suggested"] is None
    assert h.ok("kw_snapshot", project=proj, action="list")["next_suggested"] is None
    new = h.create("new", target_kicad="10")
    assert h.ok("kw_project", action="status", project=new)["next_suggested"] == "kw_sch_check"


# ---- renders ----------------------------------------------------------------------------------------


def test_pcb_render_3d_returns_image(h: Harness):
    proj = h.create()
    err, sc, res = h.call("kw_pcb_render", project=proj)
    assert not err and sc["image_info"]["width"] == 64 and sc["data"]["mode"] == "3d"
    img = [c for c in res.content if type(c).__name__ == "ImageContent"]
    assert len(img) == 1 and img[0].mime_type == "image/png"
    assert base64.b64decode(img[0].data)[:8] == b"\x89PNG\r\n\x1a\n"
    assert h.ok("kw_project", action="status", project=proj)["data"]["stage"] == "created"  # read-only


def test_pcb_render_falls_back_to_2d_without_cli_render(tmp_path: Path):
    h = Harness(
        tmp_path,
        FakeBackend(
            installs=[FakeInstall(version="8.0.1")], caps={"8.0.1": {**CAPS_10, "cli.has_render": False}}
        ),
    )
    proj = h.create()
    sc = h.ok("kw_pcb_render", project=proj)
    assert (
        sc["data"]["mode"] == "2d"
        and "render_3d_unavailable" in sc["degraded"]
        and sc["image_info"]["width"] == 32
    )
    assert [c[0] for c in h.backend.calls if c[0].startswith("render")] == ["render_pcb_2d"]


def test_render_without_rasteriser_degrades(h: Harness):
    proj = h.create()
    h.backend.rasterise_raises = Unsupported(
        "render.pypdfium2", FakeInstall(), fallback="uv sync --extra render"
    )
    sc = h.ok("kw_pcb_render", project=proj, view="2d")
    assert "render_unavailable" in sc["degraded"] and sc["image_info"] is None and sc["data"]["files"]
    sc = h.ok("kw_sch_render", project=proj)
    assert "render_unavailable" in sc["degraded"] and sc["data"]["pdf"].endswith(".pdf")


def test_sch_render_page_out_of_range_is_bad_args(h: Harness):
    """Finding: an out-of-range page surfaced as E_INTERNAL with a pypdfium2 traceback."""
    proj = h.create()
    h.backend.pdf_pages = 2
    sc = h.fail("kw_sch_render", "E_BAD_ARGS", project=proj, page=5)
    assert "page 5 does not exist" in sc["message"] and sc["candidates"] == ["1", "2"]
    assert h.ok("kw_sch_render", project=proj, page=2)["data"]["page"] == 2


def test_pcb_render_3d_below_smallest_preset_still_renders_3d(h: Harness):
    """Finding: max_px < 480 skipped every 3d size, fell through to the 2d plot and reported mode='3d'."""
    proj = h.create()
    sc = h.ok("kw_pcb_render", project=proj, max_px=300)
    assert sc["data"]["mode"] == "3d" and sc["degraded"] == []
    render_calls = [kw for name, kw in h.backend.calls if name == "render_pcb"]
    assert render_calls[-1]["width"] == 300 and render_calls[-1]["height"] == 225
    assert not any(name == "render_pcb_2d" for name, _ in h.backend.calls)


def test_sch_render_image(h: Harness):
    proj = h.create()
    sc = h.ok("kw_sch_render", project=proj)
    assert sc["image_info"] == {
        "width": 40,
        "height": 30,
        "bytes": sc["data"]["bytes"],
        "mime_type": "image/png",
    }
    assert sc["data"]["sheet"] == "demo.kicad_sch"
    h.fail("kw_sch_render", "E_PROJECT_NOT_FOUND", project=proj, sheet="other.kicad_sch")


# ---- kw_fab_export + gates ------------------------------------------------------------------------------


def test_fab_export_gate_force_and_files(h: Harness):
    proj = h.create()
    sc = h.fail("kw_fab_export", "E_GATE", project=proj)
    assert sc["data"] == {"required_stage": "drc_clean", "stage": "created"}
    h.fail("kw_fab_export", "E_BAD_ARGS", project=proj, force={})
    h.fail("kw_fab_export", "E_BAD_ARGS", project=proj, targets=["gerbers", "hologram"])
    h.fail("kw_fab_export", "E_BAD_ARGS", project=proj, profile="oshpark", force={"reason": "x"})
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers", "bom"], force={"reason": "prototype"})
    assert sc["ok"] and sc["stage"] == "exported" and sc["data"]["forced"] == "prototype"
    assert set(sc["data"]["files"]) == {"gerbers", "bom"} and len(sc["changed"]["added"]) == 3
    assert all(Path(f).exists() for f in sc["changed"]["added"])
    assert Path(sc["data"]["out_dir"]) == Path(proj) / "fab"
    msgs = [s["message"] for s in h.ok("kw_snapshot", project=proj, action="list")["data"]["snapshots"]]
    assert msgs[0].startswith("[kiwrap] kw_fab_export gerbers,bom") and "(forced: prototype)" in msgs[0]


def test_fab_export_clean_only_deletes_kiwrap_output(h: Harness, tmp_path: Path):
    """Finding: with a caller-supplied out_dir, `clean` used to rmtree any pre-existing
    `<out_dir>/<target>` (e.g. ~/Documents/pdf). Only directories carrying kiwrap's marker go."""
    proj = h.create()
    h.ok("kw_pcb_drc", project=proj)
    docs = tmp_path / "Documents"
    (docs / "pdf").mkdir(parents=True)
    (docs / "pdf" / "thesis.pdf").write_text("precious")
    # default for an explicit out_dir is clean=false: nothing is deleted, output written alongside
    sc = h.ok("kw_fab_export", project=proj, targets=["pdf"], out_dir=str(docs))
    assert (docs / "pdf" / "thesis.pdf").read_text() == "precious" and (
        docs / "pdf" / ".kiwrap-export"
    ).is_file()
    # explicit clean=true on a directory that kiwrap did not write is refused
    (docs / "svg").mkdir()
    (docs / "svg" / "logo.svg").write_text("<svg/>")
    sc = h.fail("kw_fab_export", "E_BAD_ARGS", project=proj, targets=["svg"], out_dir=str(docs), clean=True)
    assert "not written by kiwrap" in sc["message"] and (docs / "svg" / "logo.svg").exists()
    # ...but a directory with the marker is kiwrap's own previous output and is replaced
    stale = docs / "pdf" / "old-output.pdf"
    stale.write_text("old")
    sc = h.ok("kw_fab_export", project=proj, targets=["pdf"], out_dir=str(docs), clean=True)
    assert not stale.exists() and sc["data"]["files"]["pdf"]
    # the project's own fab/ dir defaults to clean=true
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers"])
    stale = Path(proj) / "fab" / "gerbers" / "old.gbr"
    stale.write_text("old")
    h.ok("kw_fab_export", project=proj, targets=["gerbers"])
    assert not stale.exists()


def test_fab_export_after_drc_clean_and_partial_unsupported(h: Harness):
    proj = h.create()
    h.ok("kw_pcb_drc", project=proj)
    h.backend.export_raises["step"] = Unsupported("pcb export step", FakeInstall(), fallback=None)
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers", "step"])
    assert sc["ok"] and "step_unsupported" in sc["degraded"] and "step" in sc["data"]["unsupported"]
    assert sc["stage"] == "drc_clean"  # incomplete bundle does not count as exported
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers"])
    assert sc["stage"] == "exported"


def test_fab_export_async_job(h: Harness):
    proj = h.create()
    h.ok("kw_pcb_drc", project=proj)
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers"], run_async=True)
    job_id = sc["data"]["job_id"]
    sc = h.ok("kw_job", job_id=job_id, action="wait", wait_s=30)
    assert sc["data"]["done"] and sc["data"]["job"]["state"] == "done"
    assert sc["data"]["job"]["result"]["data"]["files"]["gerbers"]
    assert h.ok("kw_job", job_id=job_id)["data"]["job"]["progress"] == 1.0
    assert h.ok("kw_job", action="list")["data"]["jobs"][0]["job_id"] == job_id
    h.fail("kw_job", "E_BAD_ARGS", job_id="nope")
    h.fail("kw_job", "E_BAD_ARGS", action="wait")


def test_job_failure_is_reported(h: Harness):
    proj = h.create()
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers"], run_async=True)  # gate fails inside the job
    sc = h.ok("kw_job", job_id=sc["data"]["job_id"], action="wait", wait_s=30)
    assert (
        sc["data"]["job"]["state"] == "failed"
        and sc["data"]["job"]["error"]["code"] == "E_GATE"
        and not sc["data"]["ok"]
    )


# ---- external edits, locks, snapshots -------------------------------------------------------------------


def test_external_edit_downgrades_and_regates(h: Harness):
    proj = h.create()
    h.ok("kw_sch_check", project=proj)
    h.ok("kw_pcb_drc", project=proj)
    sch = Path(proj) / "demo.kicad_sch"
    sch.write_text(sch.read_text().replace("A4", "A3"))
    sc = h.ok("kw_project", action="status", project=proj)
    assert "external_edit" in sc["degraded"] and sc["stage"] == "sch_written"
    assert sc["data"]["external_edit"] == {
        "files": ["demo.kicad_sch"],
        "from": "drc_clean",
        "to": "sch_written",
    }
    h.fail("kw_fab_export", "E_GATE", project=proj)
    # a mutating tool reports the edit too
    sc = h.ok("kw_sch_check", project=proj)
    assert (
        sc["stage"] == "erc_clean" and "external_edit" not in sc["degraded"]
    )  # already settled by the status call
    Path(proj, "demo.kicad_pcb").write_text("(kicad_pcb (version 20260206))")
    sc = h.ok("kw_pcb_drc", project=proj)
    assert "external_edit" not in sc["degraded"]  # drc_clean had already been invalidated by the sch edit
    h.ok("kw_fab_export", project=proj, targets=["gerbers"])
    Path(proj, "demo.kicad_pcb").write_text("(kicad_pcb (version 20260206) (x))")
    sc = h.ok("kw_pcb_render", project=proj)
    assert sc["stage"] == "erc_clean"
    sc = h.ok("kw_sch_check", project=proj)
    assert sc["stage"] == "erc_clean"


def test_editor_lock_and_project_lock(h: Harness):
    proj = h.create()
    Path(proj, "~demo.kicad_sch.lck").write_text("{}")
    sc = h.fail("kw_sch_check", "E_FILE_LOCKED", project=proj)
    assert sc["candidates"] == ["~demo.kicad_sch.lck"]
    Path(proj, "~demo.kicad_sch.lck").unlink()
    lock = Path(proj, ".kiwrap", "lock")
    lock.write_text("pid=0\n")
    from kiwrap.core.state import locks

    old = locks.ProjectLock.__init__

    def fast(self, lock_path, *, timeout_s=60.0, stale_after_s=3600.0, poll_s=0.1):
        old(self, lock_path, timeout_s=0.2, stale_after_s=stale_after_s, poll_s=poll_s)

    locks.ProjectLock.__init__ = fast
    try:
        h.fail("kw_sch_check", "E_FILE_LOCKED", project=proj)
    finally:
        locks.ProjectLock.__init__ = old
    lock.unlink()


def test_mutation_prologue_releases_own_ipc_sessions_and_reports_busy_reads(h: Harness):
    """Finding 19: a pooled headless api-server holds KiCad's `~<name>.kicad_pro.lck`; the mutating
    tools close kiwrap's OWN servers on the project before the editor-lock check, and answer E_BUSY
    (not E_FILE_LOCKED with a 'delete the lock' fix) while a non-pooled read is in flight."""
    proj = h.create()
    calls: list[Path] = []
    lck = Path(proj, "~demo.kicad_pro.lck")

    def release(directory):
        calls.append(Path(directory))
        lck.unlink(missing_ok=True)  # what CloseAllDocuments + stop() do for a pooled server
        return {"closed": [str(Path(proj, "demo.kicad_pcb"))], "busy": []}

    lck.write_text("{}")
    h.ctx.services.ipc_release_project = release
    sc = h.ok("kw_sch_check", project=proj)
    assert calls == [Path(proj).resolve()] and not lck.exists() and sc["ok"] is True

    h.ctx.services.ipc_release_project = lambda directory: {
        "closed": [],
        "busy": [str(Path(proj, "demo.kicad_pcb"))],
    }
    sc = h.fail("kw_sch_check", "E_BUSY", project=proj)
    assert sc["candidates"] == ["demo.kicad_pcb"] and "retry" in sc["fix"]

    def unsupported(directory):
        raise KiwrapError(
            "E_UNSUPPORTED", "no IPC backend in this build", data={"service": "ipc_release_project"}
        )

    h.ctx.services.ipc_release_project = unsupported
    assert h.ok("kw_sch_check", project=proj)["ok"] is True  # a build without IPC just skips the step


def test_snapshot_tool(h: Harness):
    proj = h.create()
    first = h.ok("kw_project", action="status", project=proj)["data"]["snapshot_head"]
    h.ok("kw_sch_check", project=proj)
    snaps = h.ok("kw_snapshot", project=proj, action="list")["data"]["snapshots"]
    assert len(snaps) == 2 and snaps[-1]["sha"] == first
    sc = h.ok("kw_snapshot", project=proj, action="create", message="checkpoint")
    assert sc["snapshot"]
    d = h.ok("kw_snapshot", project=proj, action="diff", ref=first)["data"]
    assert any(f["path"] == ".kiwrap/state.json" for f in d["files"])
    sc = h.ok("kw_snapshot", project=proj, action="restore", ref=first)
    assert sc["data"]["restored"] == first and sc["stage"] == "created"  # state.json travelled back
    assert h.ok("kw_project", action="status", project=proj)["data"]["stage"] == "created"
    h.fail("kw_snapshot", "E_BAD_ARGS", project=proj, action="restore")
    h.fail("kw_snapshot", "E_BAD_ARGS", project=proj, action="restore", ref="deadbeef")
    h.fail("kw_snapshot", "E_BAD_ARGS", project=proj, action="diff")


def test_next_suggested_only_names_published_tools(h: Harness):
    """Finding: `next_suggested='kw_report'` after an export pointed at a tool that does not exist."""
    from kiwrap.core.state.stages import NEXT_SUGGESTED

    assert set(NEXT_SUGGESTED) == set(STAGES)
    for stage, tool in NEXT_SUGGESTED.items():
        assert tool is None or tool in TOOL_NAMES, stage
    proj = h.create()
    seen: list[str | None] = []
    for tool, args in (
        ("kw_sch_check", {}),
        ("kw_pcb_drc", {}),
        ("kw_fab_export", {"targets": ["gerbers"]}),
        ("kw_project", {"action": "status"}),
        ("kw_snapshot", {"action": "list"}),
    ):
        sc = h.ok(tool, project=proj, **args)
        seen.append(sc["next_suggested"])
    assert all(t is None or t in TOOL_NAMES for t in seen), seen
    assert seen[-3:] == [None, None, None]  # exported: the Phase 0 flow ends, nothing is suggested
    h.backend.export_raises["step"] = Unsupported("pcb export step", FakeInstall(), fallback=None)
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers", "step"])
    assert sc["next_suggested"] == "kw_fab_export"


def test_target_kicad_path_never_probes_an_arbitrary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Finding: a planted script handed in as target_kicad was executed as 'kicad-cli'. Through
    the MCP surface a path only selects an install discovery already found (`probe_paths=False`)."""
    from kiwrap import installation
    from kiwrap.core.services import Services

    calls: list[dict] = []

    def fake_find(spec=None, installs=None, *, probe_paths=True):
        calls.append({"spec": spec, "probe_paths": probe_paths})
        raise installation.NoKiCadError(spec, installs or [])

    monkeypatch.setattr(installation, "find", fake_find)
    svc = Services.real(tmp_path / "home")
    svc._installs = []
    planted = tmp_path / "notkicad.cmd"
    planted.write_text("@echo 10.0.0")
    with pytest.raises(KiwrapError) as e:
        svc.install(str(planted))
    assert e.value.code == "E_NO_KICAD" and calls == [{"spec": str(planted), "probe_paths": False}]


def test_project_create_refuses_home_and_drive_root(h: Harness):
    home = Path.home()
    for bad in (home, home.parent, Path(home.anchor)):
        sc = h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(bad), name="t")
        assert "refusing to create" in sc["message"] and sc["example"]


def test_external_edit_is_snapshotted_before_the_mutation(h: Harness):
    """Finding: the first mutating call after a hand edit must not destroy the only copy of that
    state; a pre-call snapshot records it and `kw_snapshot restore` can reach it."""
    from kiwrap.core.state import snapshots as snap

    proj = h.create()
    pcb = Path(proj, "demo.kicad_pcb")
    edited = pcb.read_text().replace("(kicad_pcb", "(kicad_pcb (hand-edit yes)", 1)
    pcb.write_text(edited)
    sc = h.ok("kw_pcb_drc", project=proj)
    pre = sc["data"]["pre_snapshot"]
    assert pre and any(w.startswith("pre-call snapshot") for w in sc["warnings"])
    messages = [s.message for s in snap.list_snapshots(proj)]
    assert messages[1] == "[kiwrap] pre-kw_pcb_drc (state before this call)" and messages[0].startswith(
        "[kiwrap] kw_pcb_drc"
    )
    assert snap._git(proj, "show", f"{pre}:demo.kicad_pcb").stdout == edited
    # no edit -> no extra snapshot
    n = len(snap.list_snapshots(proj))
    sc = h.ok("kw_sch_check", project=proj)
    assert "pre_snapshot" not in sc["data"] and len(snap.list_snapshots(proj)) == n + 1


def test_failed_call_is_marked_in_the_snapshot_message(h: Harness):
    from kiwrap.core.state import snapshots as snap

    proj = h.create()
    h.fail("kw_sch_check", "E_BAD_ARGS", project=proj, severity="bogus")
    messages = [s.message for s in snap.list_snapshots(proj)]
    assert not any("kw_sch_check" in m and "FAILED" not in m for m in messages)
    h.backend.erc = CliLoadError("boom")
    h.fail("kw_sch_check", "E_KICAD_FAILED", project=proj)
    messages = [s.message for s in snap.list_snapshots(proj)]
    assert messages[0].endswith("FAILED E_KICAD_FAILED"), messages


def test_every_error_code_is_producible(h: Harness):
    """E_EXTERNAL_EDIT is reserved (tools downgrade instead of raising); the mapping still exists."""
    assert to_kiwrap_error(KiwrapError("E_EXTERNAL_EDIT", "x")).code == "E_EXTERNAL_EDIT"
    assert to_kiwrap_error(PermissionError("locked")).code == "E_FILE_LOCKED"
    assert to_kiwrap_error(FileNotFoundError("gone")).code == "E_PROJECT_NOT_FOUND"
    assert to_kiwrap_error(TimeoutError("slow")).code == "E_TIMEOUT"
    assert to_kiwrap_error(ValueError("unknown severity")).code == "E_BAD_ARGS"
    assert to_kiwrap_error(RuntimeError("?")).code == "E_INTERNAL"


def test_writer_and_library_errors_keep_their_teaching_shape(h: Harness):
    """An unknown lib_id in kw_sch_add_symbol is the library tools' E_LIB_NOT_FOUND with the
    resolver's candidates and a search-first fix - not a generic E_BAD_ARGS pointing at the
    schema (WriterError is a ValueError; reviewer finding, 2026-09-20)."""
    from kiwrap.formats.libs import NotFound
    from kiwrap.formats.schematic_writer import SchematicWriter, WriterError

    err = to_kiwrap_error(
        WriterError(
            "E_SYMBOL_NOT_FOUND", "cannot resolve 'Device:Resistor'", fix="search first", candidates=["R"]
        )
    )
    assert err.code == "E_LIB_NOT_FOUND" and err.candidates == ["R"] and err.fix == "search first"
    assert err.data["writer_code"] == "E_SYMBOL_NOT_FOUND"
    other = to_kiwrap_error(WriterError("E_REF_TAKEN", "R1 is taken", fix="pass ref=None"))
    assert (
        other.code == "E_BAD_ARGS"
        and other.fix == "pass ref=None"
        and other.data["writer_code"] == "E_REF_TAKEN"
    )
    lib = to_kiwrap_error(
        NotFound("no symbol R_0603 in Device", fix="search with kw_lib_search first", candidates=["R"])
    )
    assert lib.code == "E_LIB_NOT_FOUND" and lib.candidates == ["R"] and "kw_lib_search" in lib.fix

    class Resolver:
        def symbol(self, lib_id: str):
            raise NotFound(
                f"no symbol {lib_id}", fix="search with kw_lib_search first", candidates=["R", "R_Small"]
            )

    w = SchematicWriter(h.work, epoch={"sch": 20260306, "sym": 20251024}, lib_resolver=Resolver())
    w.load(h.work / "x.kicad_sch")
    with pytest.raises(WriterError) as info:
        w.add_symbol("Device:Resistor", None, None, (0, 0))
    assert info.value.code == "E_SYMBOL_NOT_FOUND" and info.value.candidates == ["R", "R_Small"]
    mapped = to_kiwrap_error(info.value)
    assert mapped.code == "E_LIB_NOT_FOUND" and mapped.candidates == ["R", "R_Small"]
    assert "kw_lib_search" in mapped.fix


def test_build_context_uses_storage_home(tmp_path: Path):
    ctx = build_context(tmp_path / "sh")
    assert ctx.services.storage_home == (tmp_path / "sh").resolve() and (tmp_path / "sh").exists()


def test_lazy_services_report_missing_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kiwrap.core.services import _lazy

    fn = _lazy("kiwrap.does_not_exist", "nope", capability_hint="thing")
    with pytest.raises(KiwrapError) as e:
        fn()
    assert e.value.code == "E_UNSUPPORTED"

    def stub(*a, **k):
        raise NotImplementedError

    import kiwrap.core.tools.help as mod

    monkeypatch.setattr(mod, "overview", stub, raising=True)
    fn = _lazy("kiwrap.core.tools.help", "overview", capability_hint="thing")
    with pytest.raises(KiwrapError) as e:
        fn()
    assert e.value.code == "E_UNSUPPORTED" and "not implemented" in e.value.message


def test_tool_call_timing_is_recorded_in_history(h: Harness):
    proj = h.create()
    t0 = time.time()
    h.ok("kw_sch_check", project=proj)
    hist = h.ok("kw_project", action="status", project=proj)["data"]["history"]
    assert (
        hist[-1]["tool"] == "kw_sch_check" and hist[-1]["validators"] == {"erc": 0} and hist[-1]["snapshot"]
    )
    assert time.time() - t0 < 60
