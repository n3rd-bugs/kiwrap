"""Unit tests for kiwrap.formats.schematic on hand-written schematics (no KiCad needed).

The pin transform cases are the 12 orientation combinations of docs/VERIFICATION.md claim 3 with
the same library pin (2.54, 5.08) and the expected sheet positions the ERC report gave.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.formats.geometry import Vec2
from kiwrap.formats.schematic import (
    Hierarchy,
    SchematicFile,
    is_bus_label,
    parse_lib_symbols,
    pin_sheet_position,
    unit_letter,
)
from kiwrap.formats.sexpr import loads

LIB_SYMBOLS = """(lib_symbols
\t(symbol "Device:R" (pin_numbers (hide yes)) (pin_names (offset 0)) (exclude_from_sim no) (in_bom yes) (on_board yes)
\t\t(property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "" (at -1.778 0 90) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(symbol "R_0_1"
\t\t\t(rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none)))
\t\t)
\t\t(symbol "R_1_1"
\t\t\t(pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t\t(pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
\t(symbol "Device:R_Small" (extends "R")
\t\t(property "Value" "R_Small" (at 0 0 90) (effects (font (size 1.27 1.27))))
\t)
\t(symbol "power:GND" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)
\t\t(property "Reference" "#PWR" (at 0 -6.35 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Value" "GND" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "GND_1_1"
\t\t\t(pin power_in line (at 0 0 270) (length 0) (hide yes) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
\t(symbol "power:PWR_FLAG" (power) (pin_names (offset 0)) (in_bom yes) (on_board yes)
\t\t(property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "PWR_FLAG_1_1"
\t\t\t(pin power_out line (at 0 0 90) (length 0) (name "pwr" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
\t(symbol "T:PINT" (pin_names (offset 1.016)) (in_bom yes) (on_board yes)
\t\t(property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "PINT" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "PINT_1_1"
\t\t\t(pin input line (at 2.54 5.08 180) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
\t(symbol "T:DUAL" (pin_names (offset 1.016)) (in_bom yes) (on_board yes)
\t\t(property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "DUAL" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "DUAL_0_1"
\t\t\t(pin power_in line (at 0 7.62 270) (length 2.54) hide (name "VCC" (effects (font (size 1.27 1.27)))) (number "8" (effects (font (size 1.27 1.27)))))
\t\t)
\t\t(symbol "DUAL_1_1"
\t\t\t(pin input line (at -5.08 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t)
\t\t(symbol "DUAL_2_1"
\t\t\t(pin input line (at -5.08 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
)"""


def _symbol(
    lib_id: str,
    ref: str,
    x: float,
    y: float,
    rot: float = 0,
    mirror: str | None = None,
    unit: int = 1,
    extra: str = "",
) -> str:
    m = f"(mirror {mirror})" if mirror else ""
    return f"""\t(symbol (lib_id "{lib_id}") (at {x} {y} {rot}) {m} (unit {unit}) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
\t\t(uuid "{ref.lower()}-uuid")
\t\t(property "Reference" "{ref}" (at {x} {y} 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "{lib_id.split(":")[1]}" (at {x} {y} 0) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "Lib:FP" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t{extra}
\t\t(instances (project "t" (path "/root-uuid" (reference "{ref}") (unit {unit}))))
\t)
"""


def _wire(x1, y1, x2, y2) -> str:
    return f'\t(wire (pts (xy {x1} {y1}) (xy {x2} {y2})) (stroke (width 0) (type default)) (uuid "w{x1}{y1}{x2}{y2}"))\n'


def _label(kind: str, text: str, x: float, y: float, shape: str | None = None) -> str:
    sh = f"(shape {shape})" if shape else ""
    return (
        f'\t({kind} "{text}" {sh} (at {x} {y} 0) (effects (font (size 1.27 1.27))) (uuid "{kind}-{text}"))\n'
    )


ORIENTATIONS = [
    # (rot, mirror, X, Y) -> expected pin position, from claim 3's test points
    (0, None, 50.8, 50.8, (53.34, 45.72)),
    (90, "x", 50.8, 76.2, (45.72, 78.74)),
    (270, "y", 127, 101.6, (121.92, 104.14)),
    (90, None, 50.8, 81.28, (45.72, 78.74)),
]


def test_pin_sheet_position_claim3():
    lib = Vec2(2.54, 5.08)
    for rot, mirror, x, y, (ex, ey) in ORIENTATIONS:
        p = pin_sheet_position(Vec2(x, y), rot, mirror, lib)
        assert (round(p.x, 4), round(p.y, 4)) == (ex, ey), (rot, mirror)
    # the four formulas and the mirror sign rules
    o = Vec2(0, 0)
    assert pin_sheet_position(o, 0, None, lib) == Vec2(2.54, -5.08)
    assert pin_sheet_position(o, 90, None, lib) == Vec2(-5.08, -2.54)
    assert pin_sheet_position(o, 180, None, lib) == Vec2(-2.54, 5.08)
    assert pin_sheet_position(o, 270, None, lib) == Vec2(5.08, 2.54)
    assert pin_sheet_position(o, 0, "x", lib) == Vec2(2.54, 5.08)
    assert pin_sheet_position(o, 0, "y", lib) == Vec2(-2.54, -5.08)
    assert pin_sheet_position(o, 180, "x", lib) == Vec2(-2.54, -5.08)


def test_lib_symbols_extends_units_power():
    libs = parse_lib_symbols(loads(LIB_SYMBOLS))
    r = libs["Device:R"]
    assert [p.number for p in r.pins] == ["1", "2"] and r.unit_count == 1 and r.power is None
    assert r.pins[0].position == Vec2(0, 3.81) and r.pins[0].shown_name == "" and r.pins[0].name == "~"
    small = libs["Device:R_Small"]
    assert small.extends == "R" and [p.number for p in small.pins] == ["1", "2"]
    assert small.properties["Value"] == "R_Small" and small.properties["Reference"] == "R"
    gnd = libs["power:GND"]
    assert gnd.is_power and gnd.power == "global" and gnd.pins[0].hidden and gnd.pins[0].type == "power_in"
    dual = libs["T:DUAL"]
    assert dual.unit_count == 2 and [p.unit for p in dual.pins] == [0, 1, 2]
    assert [p.number for p in dual.pins_for(2)] == ["8", "3"] and dual.pins[0].hidden  # bare `hide` flag
    assert (
        unit_letter(1) == "A" and unit_letter(26) == "Z" and unit_letter(27) == "AA" and unit_letter(0) == ""
    )
    assert (
        is_bus_label("D[0..7]")
        and is_bus_label("{A B}")
        and is_bus_label("PWR{A B}")
        and not is_bus_label("D0")
    )


def _schematic_text(body: str, uuid: str = "root-uuid", sheets: str = "") -> str:
    return (
        '(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")\n'
        f'\t(uuid "{uuid}")\n\t(paper "A4")\n'
        + LIB_SYMBOLS
        + "\n"
        + body
        + sheets
        + '\t(sheet_instances (path "/" (page "1")))\n)\n'
    )


@pytest.fixture
def flat() -> SchematicFile:
    body = (
        _symbol("Device:R", "R1", 50.8, 50.8)  # pins at (50.8, 47) and (50.8, 54.61)
        + _symbol(
            "Device:R", "R2", 63.5, 50.8, rot=90
        )  # rot 90: pin1 (63.5-3.81, 50.8) = (59.69, 50.8)? see below
        + _symbol("power:GND", "#PWR01", 50.8, 60)
        + _symbol("power:PWR_FLAG", "#FLG01", 50.8, 60)
        + _symbol("T:PINT", "U1", 50.8, 76.2, rot=90, mirror="x")
        + _symbol("T:DUAL", "U2", 100, 100, unit=1)
        + _symbol("T:DUAL", "U2", 120, 100, unit=2)
        + _wire(50.8, 54.61, 50.8, 60)  # R1.2 -> GND
        + _wire(50.8, 46.99, 50.8, 40)  # R1.1 -> label NET_A
        + _label("label", "NET_A", 50.8, 40)
        + _wire(45.72, 78.74, 40, 78.74)  # U1.IN -> global label
        + _label("global_label", "GLOB", 40, 78.74, "input")
        + _wire(94.92, 100, 90, 100)  # U2A.1 -> local label with a wire passing a junction
        + _wire(90, 100, 90, 90)
        + _label("label", "NET_B", 90, 95)  # mid-wire label: connected
        + _wire(114.92, 100, 110, 100)  # U2B.3 alone with a no-connect
        + '\t(no_connect (at 110 100) (uuid "nc1"))\n'
        + _wire(200, 200, 210, 200)  # dangling wire
        + _label("hierarchical_label", "OUT", 210, 200, "output")
    )
    return SchematicFile.loads(_schematic_text(body))


def test_schematic_items(flat: SchematicFile):
    assert flat.stamp == 20250114 and flat.uuid == "root-uuid" and flat.generator == "eeschema"
    assert (
        len(flat.symbols) == 7
        and len(flat.wires) == 7
        and len(flat.junctions) == 0
        and len(flat.no_connects) == 1
    )
    assert [label.kind for label in flat.labels] == ["label", "global_label", "label", "hierarchical_label"]
    r1 = flat.symbol("R1")
    assert r1 is not None and r1.lib_symbol is flat.lib_symbols["Device:R"] and r1.footprint == "Lib:FP"
    assert [p.position for p in r1.pins] == [Vec2(50.8, 46.99), Vec2(50.8, 54.61)]
    r2 = flat.symbol("R2")
    assert r2.pins[0].position == Vec2(59.69, 50.8) and r2.pins[1].position == Vec2(67.31, 50.8)
    u1 = flat.symbol("U1")
    assert u1.mirror == "x" and u1.rotation == 90 and u1.pins[0].position.rounded(4) == Vec2(45.72, 78.74)
    gnd = flat.symbol("#PWR01")
    assert gnd.is_power and gnd.power_kind == "global" and gnd.pins[0].hidden
    assert flat.symbol("#FLG01").is_power  # PWR_FLAG is a power symbol too (its pin is power_out)
    assert {s.reference for s in flat.power_symbols()} == {"#PWR01", "#FLG01"}
    units = [s for s in flat.symbols if s.reference == "U2"]
    assert [s.unit for s in units] == [1, 2] and units[0].unit_count == 2
    assert [p.number for p in units[0].pins_for("/root-uuid")] == ["8", "1"]
    assert [p.number for p in units[1].pins_for("/root-uuid")] == ["8", "3"]
    assert units[0].instance_for("/root-uuid").reference == "U2" and units[0].reference_for("/other") == "U2"
    assert flat.labels_of("global_label")[0].shape == "input"


def test_connectivity_and_netlist(flat: SchematicFile):
    from kiwrap.formats.analysis import netlist_from_file, pin_connectivity

    conn = pin_connectivity(flat)
    names = conn.net_by_name()
    assert "GND" in names and {(p.reference, p.number) for p in names["GND"].pins} >= {
        ("R1", "2"),
        ("#PWR01", "1"),
    }
    assert names["GND"].driver_kind == "global_power"
    assert "/NET_A" in names and [(p.reference, p.number) for p in names["/NET_A"].pins] == [("R1", "1")]
    assert "GLOB" in names and names["GLOB"].driver_kind == "global_label"
    assert [(p.reference, p.number) for p in names["GLOB"].pins] == [("U1", "1")]
    # mid-wire label connects (KiCad behaviour verified on 10.0.6)
    assert "/NET_B" in names and [(p.reference, p.number) for p in names["/NET_B"].pins] == [("U2", "1")]
    # hidden power pin on a non-power symbol -> global net named after the pin (both units share it)
    assert "VCC" in names and {(p.reference, p.number) for p in names["VCC"].pins} == {("U2", "8")}
    # no-connect single pin: unconnected- naming with the unit letter and -Pad number
    nc = [n for n in conn.nets if n.no_connect]
    assert len(nc) == 1 and nc[0].name == "unconnected-(U2B-A-Pad3)"
    assert [(p.reference, p.number) for p in conn.no_connect_pins] == [("U2", "3")]
    # R2 pins are alone: weakly driven single pins get unconnected- names without a unit letter
    assert "unconnected-(R2-Pad1)" in names and "unconnected-(R2-Pad2)" in names
    # unconnected_pins = KiCad's pin_not_connected: nothing touches the pin end. The NC-marked
    # U2.3 is not one (the marker touches it), nor is R1.2 (a power symbol's pin sits on it).
    assert {(p.reference, p.number) for p in conn.unconnected_pins} == {("R2", "1"), ("R2", "2")}
    # the free end of the NET_B stub and the free end of the OUT stub are dangling
    assert sorted(w.position.as_tuple() for w in conn.dangling_wire_ends) == [(90.0, 90.0), (200.0, 200.0)]
    assert [n.name for n in conn.label_only_nets] == ["/OUT"]
    assert conn.bus_count == 0 and conn.bus_entry_count == 0 and conn.warnings == []

    nl = netlist_from_file(flat)
    refs = [c.ref for c in nl.components]
    assert refs == ["R1", "R2", "U1", "U2"]  # power symbols and flags excluded, multi-unit once
    assert nl.component("U2").footprint == "Lib:FP" and nl.component("U2").lib_id == "T:DUAL"
    by = nl.net_by_name()
    assert set(by) >= {"GND", "/NET_A", "GLOB", "/NET_B", "VCC", "unconnected-(U2B-A-Pad3)"}
    assert "/OUT" not in by  # no nodes -> not exported
    gnd = by["GND"]
    assert [(n.ref, n.pin) for n in gnd.nodes] == [("R1", "2")]  # #PWR / #FLG never listed
    nc_node = by["unconnected-(U2B-A-Pad3)"].nodes[0]
    # KiCad 10+ writes `<name>_<number>`; 9 and older the bare name (finding 15)
    assert nc_node.pin_type == "input+no_connect" and nc_node.pin_function == "A_3"
    legacy = netlist_from_file(flat, pin_function_style="name").net_by_name()["unconnected-(U2B-A-Pad3)"]
    assert legacy.nodes[0].pin_function == "A"
    with pytest.raises(ValueError):
        netlist_from_file(flat, pin_function_style="bogus")
    assert by["/NET_A"].nodes[0].pin_function is None  # "~" pin name is shown as empty
    assert [n.code for n in nl.nets] == list(range(1, len(nl.nets) + 1))


def test_hierarchy_walk_and_sheet_pins(tmp_path: Path):
    child_body = (
        _symbol("Device:R", "R3", 50.8, 50.8)
        + _wire(50.8, 46.99, 50.8, 40)
        + _label("hierarchical_label", "IN", 50.8, 40, "input")
        + _wire(50.8, 54.61, 50.8, 60)
        + _label("label", "LOCAL", 50.8, 60)
    )
    child = _schematic_text(child_body, uuid="child-uuid")
    (tmp_path / "child.kicad_sch").write_text(child, encoding="utf-8")
    sheets = (
        "\t(sheet (at 100 100) (size 20 10) (fields_autoplaced yes) (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000))\n"
        '\t\t(uuid "sheet-uuid")\n'
        '\t\t(property "Sheetname" "sub" (at 100 99 0) (effects (font (size 1.27 1.27)) (justify left bottom)))\n'
        '\t\t(property "Sheetfile" "child.kicad_sch" (at 100 111 0) (effects (font (size 1.27 1.27)) (justify left top)))\n'
        '\t\t(pin "IN" input (at 100 105 180) (effects (font (size 1.27 1.27)) (justify right)) (uuid "sp-in"))\n'
        '\t\t(instances (project "t" (path "/root-uuid" (page "2"))))\n'
        "\t)\n"
        "\t(sheet (at 140 100) (size 20 10) (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000))\n"
        '\t\t(uuid "sheet2-uuid")\n'
        '\t\t(property "Sheetname" "sub2" (at 140 99 0) (effects (font (size 1.27 1.27)) (justify left bottom)))\n'
        '\t\t(property "Sheetfile" "child.kicad_sch" (at 140 111 0) (effects (font (size 1.27 1.27)) (justify left top)))\n'
        '\t\t(pin "IN" input (at 140 105 180) (effects (font (size 1.27 1.27)) (justify right)) (uuid "sp2-in"))\n'
        '\t\t(instances (project "t" (path "/root-uuid" (page "3"))))\n'
        "\t)\n"
        "\t(sheet (at 180 100) (size 20 10) (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000))\n"
        '\t\t(uuid "sheet3-uuid")\n'
        '\t\t(property "Sheetname" "missing" (at 180 99 0) (effects (font (size 1.27 1.27))))\n'
        '\t\t(property "Sheetfile" "nope.kicad_sch" (at 180 111 0) (effects (font (size 1.27 1.27))))\n'
        "\t)\n"
    )
    root_body = (
        _symbol("Device:R", "R1", 80, 105, rot=90)  # pin1 at (76.19, 105), pin2 at (83.81, 105)
        + _wire(83.81, 105, 100, 105)  # R1.2 -> sheet pin IN of sub
        + _wire(76.19, 105, 70, 105)
        + _label("label", "DRIVE", 70, 105)
        + _wire(140, 105, 130, 105)  # sub2 IN fed only by a wire + local label
        + _label("label", "OTHER", 130, 105)
    )
    root = _schematic_text(root_body, sheets=sheets)
    (tmp_path / "root.kicad_sch").write_text(root, encoding="utf-8")
    h = Hierarchy.load(tmp_path / "root.kicad_sch")
    assert [i.names_path for i in h.instances] == ["/", "/sub/", "/sub2/"]
    assert h.instances[1].path == "/root-uuid/sheet-uuid" and h.instances[1].tstamps_path == "/sheet-uuid/"
    assert h.instances[1].page == "2" and h.instances[2].page == "3" and h.instances[0].page == "1"
    assert h.instances[1].file is h.instances[2].file  # same file, two instances
    assert h.missing == [("/root-uuid", "nope.kicad_sch")]
    assert h.by_path()["/root-uuid/sheet2-uuid"].name == "sub2"
    assert len(list(h.symbols())) == 3 and len(list(h.children(h.instances[0]))) == 2
    assert h.root.sheets[0].pins[0].name == "IN" and h.root.sheets[0].pins[0].shape == "input"

    from kiwrap.formats.analysis import netlist_from_file, pin_connectivity

    conn = pin_connectivity(h)
    names = conn.net_by_name()
    # R1.1 carries the root local label; R1.2 reaches sub's sheet pin: the parent side is only a
    # weak sheet-pin driver, so the child's hierarchical label (strong) names the net "/sub/IN"
    assert "/DRIVE" in names and {(p.reference, p.number) for p in names["/DRIVE"].pins} == {("R1", "1")}
    assert "/sub/IN" in names and {(p.reference, p.number) for p in names["/sub/IN"].pins} == {
        ("R1", "2"),
        ("R3", "1"),
    }
    assert {p.path for p in names["/sub/IN"].pins} == {"/root-uuid", "/root-uuid/sheet-uuid"}
    # sub2's pin is driven from the root by a local label (priority 4 beats the hier label's 3)
    assert "/OTHER" in names and {(p.path, p.reference) for p in names["/OTHER"].pins} == {
        ("/root-uuid/sheet2-uuid", "R3")
    }
    assert "/sub/LOCAL" in names and "/sub2/LOCAL" in names  # local labels stay per sheet path
    assert "/sub2/IN" not in names
    nl = netlist_from_file(h)
    assert [c.ref for c in nl.components] == ["R1", "R3"] and nl.component("R3").sheet_path == "/sub/"
    # the same reference R3 in both instances collapses to one component (as kicad-cli does for
    # un-annotated repeated sheets); nets keep one node per (reference, pin)
    assert [(n.ref, n.pin) for n in nl.net_by_name()["/sub/IN"].nodes] == [("R1", "2"), ("R3", "1")]
    assert [(n.ref, n.pin) for n in nl.net_by_name()["/OTHER"].nodes] == [("R3", "1")]
