"""The JLCPCB profile, kw_fab_bom and the intent diff against real KiCads (docs/DECISIONS.md D9).

The Phase 1b blinker board is built by the same agent conversation as tests/test_e2e_authoring.py
(in-process this time, one build per KiCad, ~60 s), then:

    kw_fab_export(profile="jlcpcb")   gerber set + Protel names, Excellon PTH/NPTH + map, CPL with the
                                      rotation corrections (SOT-223 -> 180, 0603s untouched), BOM CSV,
                                      the upload zip - KiCad wrote every file, kiwrap only post-processed
    kw_fab_bom                        kicad-cli's `sch export bom` rows == the netlist-grouped rows
    kw_intent + kw_sch_check          the blinker's intended nets diff empty; a wrong pin is caught

plus the 4-layer `video` demo (In1/In2 gerbers in the set) and the ecc83 demo's BOM (cli vs
netlist). Every KiCad process runs sandboxed; the user's KiCad config hash is asserted by the
session fixture.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.core.fab import bom_from_netlist
from kiwrap.core.server import build_server
from tests._fakes import demo_project
from tests.test_e2e_authoring import FlowError, Log, flow

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

TARGETS = ("10.0", "10.99")


def _install_for(target: str):
    from kiwrap.installation import discover

    inst = next((i for i in discover() if i.major_minor == target and not i.is_bench_build), None)
    if inst is None:
        pytest.skip(f"KiCad {target} not installed")
    return inst


class Session:
    """The MCP server in-process (same tools, same receipts as over stdio; no transport)."""

    def __init__(self, home: Path):
        self.server = build_server(home)
        self.log = Log()

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = anyio.run(self.server.call_tool, tool, args)
        sc = result.structured_content or {}
        self.log.add(tool, sc, error=bool(result.is_error))
        if result.is_error:
            raise FlowError(f"{tool} -> {sc.get('code')}: {sc.get('message')}\n  fix: {sc.get('fix')}")
        return sc


@pytest.fixture(scope="module")
def blinkers(tmp_path_factory: pytest.TempPathFactory, storage_home: Path) -> dict[str, tuple[Session, Path]]:
    """One blinker project per installed primary KiCad, built by the e2e agent conversation."""
    out: dict[str, tuple[Session, Path]] = {}
    for target in TARGETS:
        try:
            _install_for(target)
        except pytest.skip.Exception:
            continue
        work = tmp_path_factory.mktemp(f"fab-{target.replace('.', '_')}")
        session = Session(storage_home)

        async def call(tool: str, args: dict[str, Any], _s: Session = session) -> dict[str, Any]:
            r = await _s.server.call_tool(tool, args)
            sc = r.structured_content or {}
            _s.log.add(tool, sc, error=bool(r.is_error))
            if r.is_error:
                raise FlowError(f"{tool} -> {sc.get('code')}: {sc.get('message')}\n  fix: {sc.get('fix')}")
            return sc

        anyio.run(flow, call, work, target, session.log)
        out[target] = (session, work / "blinky")
    if not out:
        pytest.skip("neither KiCad 10.0 nor 10.99 installed")
    return out


def _pick(blinkers: dict[str, tuple[Session, Path]], target: str) -> tuple[Session, Path]:
    if target not in blinkers:
        pytest.skip(f"KiCad {target} not installed")
    return blinkers[target]


def _names(files: dict[str, list[str]], target: str) -> set[str]:
    return {Path(f).name for f in files.get(target, [])}


# ---- kw_fab_export(profile="jlcpcb") on the blinker -------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
def test_jlcpcb_bundle_on_the_blinker(blinkers, target: str):
    s, proj = _pick(blinkers, target)
    state = json.loads((proj / ".kiwrap" / "state.json").read_text())
    force = (
        None
        if state["stage"] in ("drc_clean", "exported")
        else {"reason": "live test: zone refill unavailable"}
    )
    sc = s.call(
        "kw_fab_export",
        project=str(proj),
        profile="jlcpcb",
        targets=["gerbers", "drill", "pos", "bom"],
        force=force,
    )
    d = sc["data"]
    assert d["ok"] and d["unsupported"] == {} and d["profile"] == "jlcpcb", sc
    assert d.get("dropped_options", {}) == {}, d.get("dropped_options")  # 10.x knows every flag we set
    files = d["files"]
    # gerbers: the JLCPCB layer set with Protel extensions (KiCad's own naming) + the job file
    g = _names(files, "gerbers")
    expected = {
        "blinky-F_Cu.gtl",
        "blinky-B_Cu.gbl",
        "blinky-F_Paste.gtp",
        "blinky-B_Paste.gbp",
        "blinky-F_Silkscreen.gto",
        "blinky-B_Silkscreen.gbo",
        "blinky-F_Mask.gts",
        "blinky-B_Mask.gbs",
        "blinky-Edge_Cuts.gm1",
        "blinky-job.gbrjob",
    }
    assert expected <= g, sorted(g)
    assert not any(n.endswith(".gbr") for n in g), sorted(g)  # no KiCad-extension leftovers
    fcu = next(Path(f) for f in files["gerbers"] if f.endswith("blinky-F_Cu.gtl")).read_text(errors="replace")
    assert "%TF.GenerationSoftware,KiCad" in fcu and "%TF.FileFunction,Copper,L1,Top*%" in fcu  # X2 kept
    assert "%FSLAX46Y46*%" in fcu and "%MOMM*%" in fcu  # 4.6 mm, KiCad's default precision
    # drill: separate PTH / NPTH Excellon files, metric, decimal, plus the Gerber X2 map
    dr = _names(files, "drill")
    assert {"blinky-PTH.drl", "blinky-NPTH.drl"} <= dr, sorted(dr)
    assert any(n.endswith("-drl_map.gbr") or "map" in n for n in dr), sorted(dr)
    pth = next(Path(f) for f in files["drill"] if f.endswith("blinky-PTH.drl")).read_text(errors="replace")
    assert pth.startswith("M48") and "METRIC" in pth and re.search(r"^T1C0\.\d+$", pth, re.M), pth[:300]
    assert "; FORMAT={-:-/ absolute / metric / decimal}" in pth  # KiCad's Excellon header: our drill options
    # pos: KiCad's CSV + the corrected CPL; the SOT-223 regulator is the only corrected part
    pos = d["positions"]
    assert pos["rows"] == 11 and pos["skipped"] == [] and pos["bottom_mirrored"] == []
    corrected = {c["ref"]: c for c in pos["corrected"]}
    assert set(corrected) == {"U1"}, corrected  # DIP-8, 0603 R/C/LED, pin header: no rule
    assert corrected["U1"]["pattern"] == "^SOT-223" and corrected["U1"]["rotation_kicad"] == 0
    assert corrected["U1"]["rotation_out"] == 180 and corrected["U1"]["package"] == "SOT-223-3_TabPin2"
    cpl = Path(pos["file"])
    assert cpl.name == "blinky-cpl-jlcpcb.csv" and cpl.parent.name == "pos"
    rows = list(csv.reader(io.StringIO(cpl.read_text(encoding="utf-8"))))
    assert rows[0] == ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
    by_ref = {r[0]: r for r in rows[1:]}
    assert len(by_ref) == 11 and by_ref["U1"][5] == "180" and by_ref["U1"][6] == "Top"
    assert by_ref["R1"][5] == "270" and by_ref["C3"][5] == "270"  # placed at 270, untouched
    assert by_ref["R4"][5] == "180" and by_ref["U2"][5] == "0"
    # the CPL coordinates are KiCad's own pos-file numbers (mm, y up): R4 sits at (18, 19.08)
    assert (float(by_ref["R4"][3]), float(by_ref["R4"][4])) == (18.0, -19.08)
    kicad_csv = Path(pos["kicad_file"]).read_text(encoding="utf-8")
    assert kicad_csv.startswith("Ref,Val,Package,PosX,PosY,Rot,Side")
    # bom: the 11 parts through kicad-cli's exporter, grouped, no LCSC numbers yet
    bom = d["bom"]
    assert bom["source"] == "cli" and bom["components"] == 11 and bom["dnp_refs"] == []
    assert sorted(bom["missing_lcsc"]) == ["C1", "C2", "C3", "C4", "D1", "J1", "R1", "R2", "R4", "U1", "U2"]
    bom_file = next(Path(f) for f in files["bom"] if f.endswith("blinky-bom-jlcpcb.csv"))
    rows = list(csv.reader(io.StringIO(bom_file.read_text(encoding="utf-8"))))
    assert rows[0] == ["Comment", "Designator", "Footprint", "LCSC Part #", "Qty"]
    groups = {r[1]: r for r in rows[1:]}
    assert groups["C1,C2,C3"] == ["10u", "C1,C2,C3", "C_0603_1608Metric", "", "3"]
    assert groups["U2"][0] == "NE555P" and groups["U2"][2] == "DIP-8_W7.62mm"
    assert any("LCSC" in w for w in sc["warnings"])
    # the upload zip holds every gerber + drill file
    z = Path(files["bundle"][0])
    assert z.name == "blinky-jlcpcb-gerbers.zip" and z.parent == proj / "fab"
    assert set(zipfile.ZipFile(z).namelist()) == g | dr
    assert sc["stage"] == "exported"


# ---- kw_fab_bom: kicad-cli's grouping equals the netlist grouping ----------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
def test_fab_bom_cli_equals_netlist_on_the_blinker(blinkers, target: str):
    s, proj = _pick(blinkers, target)
    sc = s.call("kw_fab_bom", project=str(proj), fields=["Datasheet"])
    d = sc["data"]
    assert d["source"] == "cli" and d["components"] == 11 and d["groups"] == 9, d["groups"]
    assert sc["degraded"] == [] and d["file"] is None
    rows = {tuple(r["refs"]): r for r in d["rows"]}
    assert (
        rows[("C1", "C2", "C3")]["value"] == "10u"
        and rows[("C1", "C2", "C3")]["package"] == "C_0603_1608Metric"
    )
    assert rows[("U2",)]["footprint"] == "Package_DIP:DIP-8_W7.62mm" and rows[("U2",)]["cost"] is None
    assert "Datasheet" in rows[("U2",)]["fields"]
    # the same rows from KiCad's netlist (the fallback path on a KiCad without `sch export bom`)
    from kiwrap.backends.cli.netlist import export_netlist
    from kiwrap.sandbox import ensure

    install = _install_for(target)
    ctx = s.server.kiwrap_context
    nl = export_netlist(
        install, ensure(install, storage_home=ctx.services.storage_home), proj / "blinky.kicad_sch"
    )
    ref = bom_from_netlist(nl, extra_fields=["Datasheet"])
    assert [r.to_dict() for r in ref.rows] == d["rows"]


# ---- intent diff on the blinker -----------------------------------------------------------------------------------

BLINKER_NETS = {
    "+3V3": ["U1.2", "C2.1", "U2.8", "U2.4", "R1.1"],
    "GND": ["J1.2", "C1.2", "U1.1", "C2.2", "D1.1", "U2.1", "C3.2", "C4.1"],
    "VIN": ["J1.1", "C1.1", "U1.3"],
    "/blinker/BLINK": ["R4.1", "U2.3"],
    "THRES": ["U2.6", "U2.2", "R2.2", "C3.1"],
    "DISCH": ["U2.7", "R1.2", "R2.1"],
    "CONT": ["U2.5", "C4.2"],
    "LED": ["D1.2", "R4.2"],
}


@pytest.mark.parametrize("target", TARGETS)
def test_intent_diff_on_the_blinker(blinkers, target: str):
    s, proj = _pick(blinkers, target)
    sc = s.call(
        "kw_intent",
        project=str(proj),
        action="set",
        intent={"nets": BLINKER_NETS, "constraints": {"strict_nets": True, "fab": "jlcpcb", "layers": 2}},
    )
    assert sc["data"]["nets"] == 8 and (proj / ".kiwrap" / "intent.json").is_file()
    sc = s.call("kw_sch_check", project=str(proj))
    v = {x["name"]: x for x in sc["validators"]}
    assert v["erc"]["after"] == 0 and sc["data"]["ok"]
    diff = sc["data"]["intent_diff"]
    assert diff["ok"] and diff["count"] == 0, json.dumps(diff, indent=1)
    assert diff["extra"] == [] and diff["unmatched"] == []  # strict: the declaration is complete
    assert diff["nets_matched"]["VIN"] == "/VIN" and diff["nets_matched"]["THRES"] == "Net-(U2-THRES)"
    assert v["intent_diff"]["after"] == 0 and sc["stage"] == "exported"  # a clean check keeps the stage
    # a wrong pin (R2.1 instead of R2.2 on THRES) is exactly what ERC cannot see and the diff does
    wrong = {**BLINKER_NETS, "THRES": ["U2.6", "U2.2", "R2.1", "C3.1"]}
    s.call("kw_intent", project=str(proj), action="set", intent={"nets": wrong})
    sc = s.call("kw_intent", project=str(proj), action="diff")
    d = sc["data"]
    assert d["count"] == 1 and d["ok"] is False
    assert d["wrong_pin"] == [
        {
            "net": "THRES",
            "kicad_net": "Net-(U2-THRES)",
            "ref": "R2",
            "expected_pin": "1",
            "actual_pin": ["2"],
            "found_in": "Net-(U2-DISCH)",
        }
    ]
    assert sc["next_suggested"] == "kw_sch_connect"
    s.call("kw_intent", project=str(proj), action="set", intent={"nets": BLINKER_NETS})


# ---- demos: a 4-layer board and the ecc83 BOM -----------------------------------------------------------------------


def test_jlcpcb_gerbers_of_the_4_layer_video_demo(tmp_path: Path, storage_home: Path, newest):
    proj = demo_project(newest, tmp_path, "video")
    s = Session(storage_home)
    sc = s.call(
        "kw_fab_export",
        project=str(proj),
        profile="jlcpcb",
        targets=["gerbers", "drill"],
        force={"reason": "live test on a demo board"},
    )
    d = sc["data"]
    assert d["ok"] and d["unsupported"] == {}, sc
    g = _names(d["files"], "gerbers")
    # KiCad names the file after the layer's user name (F.Cu is "top_copper" in this demo) and
    # gives inner layers Protel's .g1 / .g2
    assert {
        "video-top_copper.gtl",
        "video-In1_Cu.g1",
        "video-In2_Cu.g2",
        "video-B_Cu.gbl",
        "video-Edge_Cuts.gm1",
    } <= g, sorted(g)
    assert {"video-F_Mask.gts", "video-B_Mask.gbs", "video-F_Paste.gtp", "video-B_Silkscreen.gbo"} <= g
    in1 = next(Path(f) for f in d["files"]["gerbers"] if f.endswith("In1_Cu.g1")).read_text(errors="replace")
    assert "%TF.FileFunction,Copper,L2,Inr*%" in in1
    assert {"video-PTH.drl", "video-NPTH.drl"} <= _names(d["files"], "drill")
    assert set(zipfile.ZipFile(d["files"]["bundle"][0]).namelist()) == g | _names(d["files"], "drill")


def test_ecc83_bom_cli_equals_netlist(tmp_path: Path, storage_home: Path, newest):
    proj = demo_project(newest, tmp_path, "ecc83")
    s = Session(storage_home)
    sc = s.call("kw_fab_bom", project=str(proj / "ecc83-pp.kicad_pro"), profile="jlcpcb")
    d = sc["data"]
    assert d["source"] == "cli" and d["components"] > 10 and d["groups"] >= 5, d["groups"]
    assert d["missing_lcsc"] and d["dnp_included"] is False
    from kiwrap.backends.cli.netlist import export_netlist
    from kiwrap.core.state.project import Project
    from kiwrap.core.tools.fab_bom import excluded_from_bom
    from kiwrap.sandbox import ensure

    ctx = s.server.kiwrap_context
    project = Project.from_path(str(proj / "ecc83-pp.kicad_pro"))
    nl = export_netlist(newest, ensure(newest, storage_home=ctx.services.storage_home), project.root_sch)
    # the demo's mounting holes are `in_bom no`: KiCad's exporter drops them, the netlist keeps
    # them, and the file reader is what tells the netlist path to drop them too
    excluded = excluded_from_bom(ctx, project)
    assert excluded and all(r.startswith("P") for r in excluded), excluded
    assert any(c.ref in excluded for c in nl.components)
    ref = bom_from_netlist(nl, exclude_refs=excluded)
    assert [r.to_dict() for r in ref.fitted] == d["rows"]
