"""Fabrication profiles without KiCad: the JLCPCB presets and options, the rotation-correction
table and its bottom-side convention, the BOM grouping from both sources, and kw_fab_export /
kw_fab_bom / kw_pcb_setup(preset=...) against the faked lower layers (tests/test_core_fakes)."""

from __future__ import annotations

import csv
import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from kiwrap.core.fab import (
    PRESET_NAMES,
    PROFILE_NAMES,
    RotationRule,
    RotationTable,
    bom_csv,
    bom_from_cli_csv,
    bom_from_netlist,
    correct_positions,
    get_profile,
    preset_rules,
)
from kiwrap.core.fab.bom import cli_bom_options
from kiwrap.core.fab.jlcpcb import PRESET_2LAYER, PRESET_4LAYER, ROTATIONS_PATH, JlcpcbProfile
from kiwrap.core.fab.profiles import filter_options
from kiwrap.formats.board_writer import _NETCLASS_KEYS, _RULE_KEYS
from kiwrap.formats.reports import Component, Net, Netlist, NetNode
from kiwrap.formats.reports.position import parse_pos_csv
from tests.test_core_fakes import CAPS_10, FakeBackend, FakeCaps, FakeInstall
from tests.test_core_tools import Harness

# ---- presets ----------------------------------------------------------------------------------------------


def test_presets_use_only_board_writer_rule_keys():
    """Every preset key must be one `BoardWriter.setup(rules=...)` accepts, else kw_pcb_setup would refuse it."""
    known = set(_RULE_KEYS) | set(_NETCLASS_KEYS)
    for name in PRESET_NAMES:
        p = preset_rules(name)
        assert set(p.rules) <= known, set(p.rules) - known
        assert p.layers in (2, 4) and p.source and p.note
    assert preset_rules("JLCPCB-2Layer") is PRESET_2LAYER
    with pytest.raises(ValueError):
        preset_rules("oshpark")


def test_presets_are_above_the_vendor_minimums_and_consistent():
    for p in (PRESET_2LAYER, PRESET_4LAYER):
        r = p.rules
        assert r["min_track_mm"] >= 0.09 and r["track_width_mm"] >= r["min_track_mm"]
        assert r["via_drill_mm"] >= r["min_hole_mm"] >= 0.15
        assert r["via_size_mm"] >= r["min_via_mm"] >= r["min_hole_mm"] + 2 * r["min_via_annular_mm"] - 1e-9
        assert r["copper_edge_clearance_mm"] >= 0.2
        assert r["clearance_mm"] >= r["min_track_mm"] or r["clearance_mm"] >= 0.1
    assert (
        PRESET_4LAYER.layers == 4
        and PRESET_4LAYER.rules["min_track_mm"] < PRESET_2LAYER.rules["min_track_mm"]
    )


# ---- profiles + option filtering ----------------------------------------------------------------------------


def test_profile_registry_and_jlcpcb_options():
    assert PROFILE_NAMES == ("generic", "jlcpcb")
    assert get_profile("generic").name == "generic" and get_profile("JLCPCB").name == "jlcpcb"
    with pytest.raises(ValueError):
        get_profile("pcbway")
    p = get_profile("jlcpcb")
    assert isinstance(p, JlcpcbProfile) and p.exclude_dnp
    g = p.gerber_options(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert g["layers"][:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert {"F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"} <= set(g["layers"])
    assert g["subtract_soldermask"] and g["check_zones"] and not g["no_protel_ext"]
    assert "no_x2" not in g  # X2 kept (accepted by the vendor)
    d = p.drill_options()
    assert (
        d["format"] == "excellon" and d["excellon_units"] == "mm" and d["excellon_zeros_format"] == "decimal"
    )
    assert d["excellon_oval_format"] == "alternate" and d["drill_origin"] == "absolute"
    assert d["excellon_separate_th"] is True and d["generate_map"] is True
    pos = p.pos_options()
    assert pos["format"] == "csv" and pos["units"] == "mm" and pos["exclude_dnp"] and pos["side"] == "both"
    assert p.file_name("pos", "blinky") == "blinky-cpl-jlcpcb.csv"
    assert p.file_name("bom", "blinky") == "blinky-bom-jlcpcb.csv"
    assert p.bundle_name("blinky") == "blinky-jlcpcb-gerbers.zip"
    assert (
        get_profile("generic").bundle_name("x") is None
        and get_profile("generic").file_name("pos", "x") is None
    )
    desc = p.describe()
    assert desc["rotation_rules"] > 40 and "jlcpcb-2layer" in desc["presets"] and desc["notes"]


def test_filter_options_drops_flags_the_kicad_lacks_and_reports_them():
    caps = FakeCaps(
        {**CAPS_10, "cli.flags.pcb export pos": ["--output", "--format", "--units", "--side"]}, FakeInstall()
    )
    f = get_profile("jlcpcb").options_for("pos", caps)
    assert f.options == {"format": "csv", "units": "mm", "side": "both"}
    # exclude_dnp (True) is reported; switches that were off (use_drill_file_origin, smd_only) are not
    assert f.dropped == ["exclude_dnp (--exclude-dnp)"]
    # no flags list at all -> keep everything (backends.cli.export is the judge)
    f2 = get_profile("jlcpcb").options_for("pos", FakeCaps(CAPS_10, FakeInstall()))
    assert f2.dropped == [] and f2.options["exclude_dnp"] is True
    assert filter_options({"layers": ["F.Cu"]}, None, "pcb export gerbers").options == {"layers": ["F.Cu"]}


# ---- rotation corrections -------------------------------------------------------------------------------------

POS_CSV = """Ref,Val,Package,PosX,PosY,Rot,Side
"U1","AMS1117-3.3","SOT-223-3_TabPin2",12.000000,-8.000000,0.000000,top
"Q1","BSS138","SOT-23",5.000000,-5.000000,90.000000,top
"Q2","BSS138","SOT-23",6.000000,-5.000000,90.000000,bottom
"R1","330","R_0603_1608Metric",18.000000,-19.080000,180.000000,top
"C9","10u","C_0603_1608Metric",1.000000,-2.000000,270.000000,bottom
"U2","MCU","QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm",20.000000,-20.000000,0.000000,top
"U3","OPAMP","SOIC-8_3.9x4.9mm_P1.27mm",30.000000,-20.000000,90.000000,top
"J1","USB","USB_C_Receptacle_XKB_U262-16XN-4BVC11",40.000000,-10.000000,0.000000,top
"""


def test_rotation_table_loads_and_longest_match_wins():
    table = RotationTable.load(ROTATIONS_PATH)
    assert table.source and len(table.rules) > 40
    raw = json.loads(ROTATIONS_PATH.read_text(encoding="utf-8"))
    assert "_source" in raw and "matthewlai/JLCKicadTools" in raw["_source"]
    assert table.match("SOT-223-3_TabPin2").rotation == 180
    assert table.match("SOT-23-5").rotation == -90
    assert table.match("R_0603_1608Metric") is None
    assert table.match("QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm").rotation == 270
    assert table.match("SOP-4_4.4x2.6mm_P1.27mm").rotation == 0  # ^SOP-4_ beats ^SOP-(?!18_) (longer match)
    assert table.match("SOP-8_3.9x4.9mm_P1.27mm").rotation == 270
    t = RotationTable(rules=(RotationRule("SOT-23", 10), RotationRule("SOT-23-3", 20)))
    assert t.match("SOT-23-3").rotation == 20  # longest match, not first
    assert t.match("SOT-23").rotation == 10


def test_correct_positions_conventions_and_report():
    table = RotationTable.load(ROTATIONS_PATH)
    pos = parse_pos_csv(POS_CSV, units="mm")
    out = correct_positions(pos, table)
    rows = {r.ref: r for r in out.rows}
    # top: kicad + correction
    assert rows["U1"].rotation_deg == 180 and rows["Q1"].rotation_deg == 0  # 90 + (-90)
    assert rows["U2"].rotation_deg == 270 and rows["U3"].rotation_deg == 0  # 90 + 270 = 360 -> 0
    # no rule: unchanged on top
    assert rows["R1"].rotation_deg == 180 and rows["R1"].rule is None
    # bottom: mirrored (180 - kicad) first, then the correction
    assert rows["C9"].rotation_deg == 270  # (180 - 270) % 360 = 270, no rule
    assert rows["Q2"].rotation_deg == 0  # (180 - 90) - 90 = 0
    assert set(out.bottom_mirrored) == {"Q2", "C9"}
    # pos-file coordinates are kept (y up as KiCad wrote them)
    assert (rows["U1"].x_mm, rows["U1"].y_mm) == (12.0, -8.0)
    # xy offset rule (USB-C XKB: +1.44 mm along x at rotation 0)
    assert (rows["J1"].x_mm, rows["J1"].y_mm) == (41.44, -10.0)
    corrected = {c.ref: c for c in out.corrections}
    assert set(corrected) == {"U1", "Q1", "Q2", "U2", "U3", "J1"}
    assert corrected["Q1"].rotation_kicad == 90 and corrected["Q1"].rotation_out == 0
    assert corrected["Q1"].pattern == "^SOT-23"
    d = out.to_dict()
    assert d["rows"] == 8 and len(d["corrected"]) == 6 and d["skipped"] == []
    # skip refs (DNP handled outside kicad-cli)
    out2 = correct_positions(pos, table, skip_refs=["R1", "nope"])
    assert out2.skipped_refs == ["R1"] and len(out2.rows) == 7
    # no table: only the bottom mirroring
    out3 = correct_positions(pos, None)
    assert out3.corrections == [] and {r.ref: r.rotation_deg for r in out3.rows}["Q2"] == 90


def test_bottom_offset_is_mirrored():
    t = RotationTable(rules=(RotationRule("^X", 0, (1.0, 0.5)),))
    text = 'Ref,Val,Package,PosX,PosY,Rot,Side\n"A1","v","X",10,-10,0,top\n"A2","v","X",10,-10,0,bottom\n'
    out = correct_positions(parse_pos_csv(text, units="mm"), t)
    top, bottom = out.rows
    # top at rotation 0: dx = +1.0, dy(kicad y-down) = +0.5 -> pos-file y (up) goes down by 0.5
    assert (top.x_mm, top.y_mm) == (11.0, -10.5)
    # bottom: angle mirrored to 180, x negated: dx = -(1.0*cos180) = +1.0, dy = -(1.0*sin180) + 0.5*cos180 = -0.5
    assert (bottom.x_mm, bottom.y_mm) == (11.0, -9.5)


def test_jlc_cpl_csv_columns():
    p = get_profile("jlcpcb")
    out = p.process_positions(parse_pos_csv(POS_CSV, units="mm"))
    text = p.cpl_csv(out)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
    by_ref = {r[0]: r for r in rows[1:]}
    assert by_ref["U1"] == ["U1", "AMS1117-3.3", "SOT-223-3_TabPin2", "12.0000", "-8.0000", "180", "Top"]
    assert by_ref["Q2"][6] == "Bottom" and by_ref["Q2"][5] == "0"


# ---- BOM ---------------------------------------------------------------------------------------------------------


def _nl() -> Netlist:
    def comp(ref: str, value: str, fp: str, dnp: bool = False, **props: str) -> Component:
        return Component(
            ref=ref, value=value, footprint=fp, lib_id="x:y", sheet_path="/", properties=props, dnp=dnp
        )

    return Netlist(
        source="blink.kicad_sch",
        kicad_version="10.0.6",
        components=(
            comp("R2", "10k", "Resistor_SMD:R_0603_1608Metric", **{"LCSC Part #": "C25804"}),
            comp("R1", "10k", "Resistor_SMD:R_0603_1608Metric", **{"LCSC Part #": "C25804"}),
            comp("R3", "10k", "Resistor_SMD:R_0603_1608Metric", dnp=True, **{"LCSC Part #": "C25804"}),
            comp("R4", "10k", "Resistor_SMD:R_0603_1608Metric", LCSC="C99999"),  # alias field, other part
            comp("C1", "10u", "Capacitor_SMD:C_0603_1608Metric", MPN="CL10A106", Manufacturer="Samsung"),
            comp("U1", "NE555P", "Package_DIP:DIP-8_W7.62mm"),
            comp("#PWR01", "GND", ""),
            comp("H1", "Hole", "MountingHole:MountingHole_3.2mm_M3"),
        ),
        nets=(Net(code=1, name="GND", nodes=(NetNode("R1", "1"),)),),
    )


def test_bom_from_netlist_groups_by_value_footprint_part_and_dnp():
    t = bom_from_netlist(_nl(), extra_fields=["MPN"], exclude_refs=["H1"])
    assert t.source == "netlist" and t.kicad_version == "10.0.6"
    keys = [(r.refs, r.value, r.lcsc, r.dnp) for r in t.rows]
    assert keys == [  # natural reference order, DNP groups last
        (["C1"], "10u", None, False),
        (["R1", "R2"], "10k", "C25804", False),
        (["R4"], "10k", "C99999", False),
        (["U1"], "NE555P", None, False),
        (["R3"], "10k", "C25804", True),  # DNP twin grouped apart, sorted last
    ]
    assert not any("#" in r for row in t.rows for r in row.refs) and not any("H1" in r.refs for r in t.rows)
    c1 = next(r for r in t.rows if r.refs == ["C1"])
    assert c1.mpn == "CL10A106" and c1.manufacturer == "Samsung" and c1.fields == {"MPN": "CL10A106"}
    assert c1.package == "C_0603_1608Metric" and c1.cost is None and c1.stock is None
    assert t.missing_lcsc() == ["C1", "U1"]
    d = t.to_dict()
    assert (
        d["groups"] == 5 and d["components"] == 6 and d["fitted_components"] == 5 and d["dnp_refs"] == ["R3"]
    )


def test_bom_from_cli_csv_matches_the_netlist_grouping():
    opts = cli_bom_options(extra_fields=["MPN"])
    assert opts["fields"][:5] == ["Reference", "Value", "Footprint", "${DNP}", "${QUANTITY}"]
    assert (
        "LCSC Part #" in opts["fields"] and opts["labels"][3] == "DNP" and opts["ref_range_delimiter"] == ""
    )
    assert opts["group_by"][:3] == ["Value", "Footprint", "${DNP}"] and opts["exclude_dnp"] is False
    header = opts["labels"]
    idx = {h: i for i, h in enumerate(header)}

    def row(refs: str, value: str, fp: str, dnp: str, qty: str, **fields: str) -> list[str]:
        cells = [""] * len(header)
        cells[idx["Reference"]], cells[idx["Value"]], cells[idx["Footprint"]] = refs, value, fp
        cells[idx["DNP"]], cells[idx["QUANTITY"]] = dnp, qty
        for k, v in fields.items():
            cells[idx[k]] = v
        return cells

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerow(row("R1,R2", "10k", "Resistor_SMD:R_0603_1608Metric", "", "2", **{"LCSC Part #": "C25804"}))
    w.writerow(row("R3", "10k", "Resistor_SMD:R_0603_1608Metric", "DNP", "1", **{"LCSC Part #": "C25804"}))
    w.writerow(row("R4", "10k", "Resistor_SMD:R_0603_1608Metric", "", "1", LCSC="C99999"))
    w.writerow(
        row("C1", "10u", "Capacitor_SMD:C_0603_1608Metric", "", "1", MPN="CL10A106", Manufacturer="Samsung")
    )
    w.writerow(row("U1", "NE555P", "Package_DIP:DIP-8_W7.62mm", "", "1"))
    t = bom_from_cli_csv("﻿" + buf.getvalue(), extra_fields=["MPN"], kicad_version="10.0.6")
    ref = bom_from_netlist(_nl(), extra_fields=["MPN"], exclude_refs=["H1"])
    assert [r.to_dict() for r in t.rows] == [r.to_dict() for r in ref.rows]
    assert t.source == "cli" and t.warnings == []


def test_bom_csv_per_profile():
    t = bom_from_netlist(_nl(), exclude_refs=["H1"])
    generic = get_profile("generic")
    rows = list(csv.reader(io.StringIO(generic.bom_csv(t))))
    assert rows[0] == ["Reference", "Qty", "Value", "Footprint", "LCSC Part #", "DNP", "MPN", "Manufacturer"]
    assert rows[2][:5] == ["R1,R2", "2", "10k", "Resistor_SMD:R_0603_1608Metric", "C25804"]
    assert [r[5] for r in rows[1:]].count("DNP") == 1  # generic keeps DNP rows, flagged
    jlc = get_profile("jlcpcb")
    rows = list(csv.reader(io.StringIO(jlc.bom_csv(t))))
    assert rows[0] == ["Comment", "Designator", "Footprint", "LCSC Part #", "Qty"]
    assert rows[2] == ["10k", "R1,R2", "R_0603_1608Metric", "C25804", "2"]
    assert not any("R3" in r[1].split(",") for r in rows[1:])  # DNP dropped
    rows = list(csv.reader(io.StringIO(jlc.bom_csv(t, include_dnp=True))))
    assert any(r[1] == "R3" for r in rows[1:])
    assert bom_csv([], columns=(("A", "refs"),)).splitlines() == ["A"]


# ---- the tools against fakes ------------------------------------------------------------------------------------


class ProfileBackend(FakeBackend):
    """The fake exporter, but `pos` writes a real KiCad-shaped CSV and `bom` a kicad-cli-shaped CSV
    so the profile post-processing runs end to end."""

    def export_pcb(
        self, install: Any, config: Any, board: Path, target: str, out_dir: Path, **opts: Any
    ) -> list[Path]:
        if target != "pos":
            return super().export_pcb(install, config, board, target, out_dir, **opts)
        self._log("export_pcb", target=target, out_dir=str(out_dir), **opts)
        out = Path(out_dir) / f"{Path(board).stem}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(POS_CSV, encoding="utf-8")
        return [out]

    def export_sch(
        self, install: Any, config: Any, schematic: Path, target: str, out: Path, **opts: Any
    ) -> list[Path]:
        if target != "bom" or "fields" not in opts:
            return super().export_sch(install, config, schematic, target, out, **opts)
        self._log("export_sch", target=target, out=str(out), **opts)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(opts["labels"])
        n = len(opts["labels"])
        r1 = ["U1,U2", "NE555P", "Package_DIP:DIP-8_W7.62mm", "", "2", "C7593"] + [""] * (n - 6)
        r2 = ["R1", "330", "Resistor_SMD:R_0603_1608Metric", "DNP", "1", ""] + [""] * (n - 6)
        r3 = ["C1", "10u", "Capacitor_SMD:C_0603_1608Metric", "", "1", ""] + [""] * (n - 6)
        w.writerows([r1, r2, r3])
        out.write_text(buf.getvalue(), encoding="utf-8")
        return [out]


@pytest.fixture
def hp(tmp_path: Path) -> Harness:
    backend = ProfileBackend(
        caps={"10.0.0": {**CAPS_10, "cli.subcommands": ["sch erc", "pcb drc", "sch export bom"]}}
    )
    return Harness(tmp_path, backend)


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")


def test_fab_export_jlcpcb_profile_end_to_end(hp: Harness):
    proj = hp.create()
    sc = hp.fail("kw_fab_export", "E_BAD_ARGS", project=proj, profile="oshpark", force={"reason": "x"})
    assert "jlcpcb" in sc["candidates"]
    sc = hp.ok(
        "kw_fab_export",
        project=proj,
        profile="jlcpcb",
        targets=["gerbers", "drill", "pos", "bom"],
        force={"reason": "unit test"},
    )
    d = sc["data"]
    assert (
        d["ok"]
        and d["profile"] == "jlcpcb"
        and set(d["files"]) == {"gerbers", "drill", "pos", "bom", "bundle"}
    )
    assert sc["stage"] == "exported" and d["profile_notes"]
    # the options reached the exporter
    calls = {(n, kw.get("target")): kw for n, kw in hp.backend.calls if n in ("export_pcb", "export_sch")}
    g = calls[("export_pcb", "gerbers")]
    assert g["subtract_soldermask"] is True and g["check_zones"] is True and "Edge.Cuts" in g["layers"]
    assert g["layers"][:2] == ["F.Cu", "B.Cu"]  # no board reader wired -> the 2-layer default
    dr = calls[("export_pcb", "drill")]
    assert dr["excellon_separate_th"] is True and dr["excellon_units"] == "mm"
    assert calls[("export_pcb", "pos")]["exclude_dnp"] is True
    b = calls[("export_sch", "bom")]
    assert "LCSC Part #" in b["fields"] and b["ref_range_delimiter"] == ""
    # the CPL: corrected rotations reported, vendor columns, DNP handling
    pos = d["positions"]
    assert pos["rows"] == 8 and {c["ref"] for c in pos["corrected"]} == {"U1", "Q1", "Q2", "U2", "U3", "J1"}
    assert pos["bottom_mirrored"] == ["Q2", "C9"]
    cpl = Path(pos["file"])
    assert cpl.name == "demo-cpl-jlcpcb.csv" and cpl.read_text().startswith("Designator,Val,Package,Mid X")
    assert any("rotation correction" in w for w in sc["warnings"])
    # the BOM: kicad-cli source, DNP dropped, missing LCSC listed
    bom = d["bom"]
    assert bom["source"] == "cli" and bom["groups"] == 2 and bom["components"] == 3
    assert bom["dnp_refs"] == ["R1"] and bom["missing_lcsc"] == ["C1"]
    bom_file = next(f for f in d["files"]["bom"] if f.endswith("demo-bom-jlcpcb.csv"))
    rows = list(csv.reader(io.StringIO(Path(bom_file).read_text())))
    assert rows[0] == ["Comment", "Designator", "Footprint", "LCSC Part #", "Qty"]
    assert rows[2] == ["NE555P", "U1,U2", "DIP-8_W7.62mm", "C7593", "2"] and len(rows) == 3
    # the upload bundle holds gerbers + drill files and nothing else
    z = Path(d["files"]["bundle"][0])
    assert z.name == "demo-jlcpcb-gerbers.zip"
    names = sorted(zipfile.ZipFile(z).namelist())
    assert names == sorted(Path(f).name for t in ("gerbers", "drill") for f in d["files"][t])
    # include_dnp=true keeps DNP parts: kicad-cli's --exclude-dnp is not passed and the BOM keeps R1
    hp.backend.calls.clear()
    sc = hp.ok("kw_fab_export", project=proj, profile="jlcpcb", targets=["pos", "bom"], include_dnp=True)
    pos_call = next(kw for n, kw in hp.backend.calls if n == "export_pcb" and kw.get("target") == "pos")
    assert "exclude_dnp" not in pos_call and sc["data"]["bom"]["components"] == 4
    # generic keeps kicad-cli's options: no bundle, no CPL / corrections; since review item 4 the
    # positions report (rows KiCad wrote, excluded refs with the reason, BOM diff) is there too
    hp.backend.calls.clear()
    sc = hp.ok("kw_fab_export", project=proj, targets=["pos", "bom"])
    assert "bundle" not in sc["data"]["files"] and "corrected" not in sc["data"]["positions"]
    assert {"rows", "refs", "excluded", "flags", "bom_diff"} <= set(sc["data"]["positions"])
    assert hp.backend.calls[-2][1].keys() >= {"target", "out_dir"} and "format" not in hp.backend.calls[-2][1]


def test_fab_export_jlcpcb_drops_missing_flags_and_falls_back_to_the_netlist(tmp_path: Path):
    caps = {
        **CAPS_10,
        "cli.subcommands": ["sch erc", "pcb drc", "pcb export pos"],  # no `sch export bom`
        "cli.flags.pcb export pos": ["--output", "--format", "--units", "--side"],
    }
    h = Harness(tmp_path, ProfileBackend(caps={"10.0.0": caps}))
    proj = h.create()
    sc = h.ok("kw_fab_export", project=proj, profile="jlcpcb", targets=["pos", "bom"], force={"reason": "t"})
    d = sc["data"]
    assert d["dropped_options"] == {"pos": ["exclude_dnp (--exclude-dnp)"]}
    assert "bom_from_netlist" in sc["degraded"] and d["bom"]["source"] == "netlist"
    assert d["bom"]["components"] == 1 and d["bom"]["missing_lcsc"] == ["R1"]  # the fake netlist's R1
    assert any("dropped" in w for w in sc["warnings"])


def test_fab_bom_tool(hp: Harness):
    proj = hp.create()
    h = hp
    sc = h.ok("kw_fab_bom", project=proj)
    d = sc["data"]
    assert d["ok"] and d["source"] == "cli" and d["profile"] == "generic" and d["file"] is None
    assert d["groups"] == 3 and d["components"] == 4 and d["dnp_refs"] == ["R1"] and d["dnp_included"] is True
    assert d["rows"][0] == {
        "refs": ["C1"],
        "qty": 1,
        "value": "10u",
        "footprint": "Capacitor_SMD:C_0603_1608Metric",
        "package": "C_0603_1608Metric",
        "lcsc": None,
        "dnp": False,
        "mpn": None,
        "manufacturer": None,
        "fields": {},
        "cost": None,
        "stock": None,
    }
    assert sc["snapshot"] is None  # a read: no snapshot, no stage change
    sc = h.ok("kw_fab_bom", project=proj, profile="jlcpcb", write=True, fields=["Datasheet"])
    d = sc["data"]
    assert d["dnp_included"] is False and d["missing_lcsc"] == ["C1"] and d["columns"][-1] == "Datasheet"
    assert any("LCSC" in w for w in sc["warnings"])
    assert Path(d["file"]).name == "demo-bom-jlcpcb.csv" and Path(d["file"]).read_text().startswith(
        "Comment,"
    )
    assert not (Path(proj) / ".kiwrap" / "tmp" / "bom").exists()  # intermediate output removed
    h.fail("kw_fab_bom", "E_BAD_ARGS", project=proj, profile="nope")
    (Path(proj) / "demo.kicad_sch").unlink()
    h.fail("kw_fab_bom", "E_PROJECT_NOT_FOUND", project=proj)


def test_pcb_setup_preset(h: Harness):
    proj = h.create()
    sc = h.fail("kw_pcb_setup", "E_BAD_ARGS", project=proj, preset="oshpark")
    assert sc["candidates"] == list(PRESET_NAMES)
    sc = h.ok("kw_pcb_setup", project=proj, preset="jlcpcb-2layer", rules={"track_width_mm": 0.3}, drc=False)
    d = sc["data"]
    assert d["layers"] == 2 and d["preset"]["name"] == "jlcpcb-2layer"
    assert d["rules"]["track_width_mm"] == 0.3  # explicit key wins
    assert d["rules"]["min_track_mm"] == PRESET_2LAYER.rules["min_track_mm"]
    pro = json.loads((Path(proj) / "demo.kicad_pro").read_text(encoding="utf-8"))
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_track_width"] == 0.127 and rules["min_copper_edge_clearance"] == 0.3
    sc = h.ok("kw_pcb_setup", project=proj, preset="jlcpcb-4layer", drc=False)
    assert sc["data"]["layers"] == 4 and sc["data"]["applied"]["layers"] == [
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
    ]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
