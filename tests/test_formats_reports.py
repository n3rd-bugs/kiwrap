"""Unit tests for the ERC/DRC report parsers (JSON and text), incl. the ×100 ERC JSON fix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiwrap.formats.reports import Position, Report, Violation, ViolationItem
from kiwrap.formats.reports._common import rescale_description, unit_factor
from kiwrap.formats.reports.drc import parse_drc_json, parse_drc_text
from kiwrap.formats.reports.erc import parse_erc_json, parse_erc_text

CORPUS = Path(__file__).resolve().parents[1] / "conformance" / "corpus"


def _read(rel: str) -> str:
    return (CORPUS / rel).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------
# ERC JSON: the x100 bug (docs/VERIFICATION.md claim 1)
# ----------------------------------------------------------------------------------------------

ERC_JSON = {
    "$schema": "https://schemas.kicad.org/erc.v1.json",
    "coordinate_units": "mm",
    "date": "2026-09-18T22:02:03",
    "kicad_version": "10.0.0",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "/11111111-2222-3333-4444-555555555555",
            "violations": [
                {
                    "description": "Wires not connected to anything",
                    "items": [
                        {
                            "description": "Horizontal Wire, length 0.1270 mm",
                            "pos": {"x": 0.762, "y": 0.508},
                            "uuid": "aaaaaaaa-0000-0000-0000-000000000002",
                        }
                    ],
                    "severity": "error",
                    "type": "wire_dangling",
                },
                {
                    "description": "Excluded thing",
                    "items": [{"description": "Symbol R1 [R]", "pos": {"x": 0.635, "y": 0.635}, "uuid": "u"}],
                    "severity": "exclusion",
                    "type": "lib_symbol_issues",
                    "excluded": True,
                },
                {
                    "description": "No position",
                    "items": [{"description": "thing", "uuid": "v"}],
                    "severity": "warning",
                    "type": "odd",
                },
            ],
        },
        {"path": "/sub/", "uuid_path": "/x/y", "violations": []},
    ],
}


def test_erc_json_rescales_positions_and_descriptions():
    r = parse_erc_json(
        json.dumps(ERC_JSON), position_scale=100, source="mini.kicad_sch", kicad_version="10.0.0"
    )
    assert r.kind == "erc" and r.source == "mini.kicad_sch" and r.kicad_version == "10.0.0"
    v = r.violations[0]
    assert v.type == "wire_dangling" and v.severity == "error" and v.sheet == "/"
    assert v.pos == Position(76.2, 50.8)
    assert v.items[0].uuid == "aaaaaaaa-0000-0000-0000-000000000002"
    assert v.items[0].description == "Horizontal Wire, length 12.7000 mm"
    assert r.errors == 1 and r.warnings == 1 and not r.ok
    ex = r.violations[1]
    assert ex.excluded and ex.severity == "exclusion" and ex.pos == Position(63.5, 63.5)
    assert r.count("exclusion") == 0  # excluded violations never count
    assert r.violations[2].pos is None
    assert r.raw["sheets"][1]["path"] == "/sub/"


def test_erc_json_scale_one_leaves_values_alone():
    r = parse_erc_json(json.dumps(ERC_JSON))
    assert r.violations[0].pos == Position(0.762, 0.508)
    assert r.violations[0].items[0].description == "Horizontal Wire, length 0.1270 mm"
    assert r.kicad_version == "10.0.0"  # falls back to the JSON


def test_erc_json_inch_units_convert_to_mm():
    data = json.loads(json.dumps(ERC_JSON))
    data["coordinate_units"] = "in"
    data["sheets"][0]["violations"][0]["items"][0]["pos"] = {"x": 0.0345, "y": 0.0185}
    data["sheets"][0]["violations"][0]["items"][0]["description"] = "Horizontal Wire, length 0.0050 in"
    r = parse_erc_json(json.dumps(data), position_scale=100)
    assert r.violations[0].pos == pytest.approx(Position(87.63, 46.99), abs=1e-9) or (
        abs(r.violations[0].pos.x_mm - 87.63) < 1e-9 and abs(r.violations[0].pos.y_mm - 46.99) < 1e-9
    )
    assert r.violations[0].items[0].description == "Horizontal Wire, length 0.5000 in"


@pytest.mark.parametrize(
    ("text", "scale", "expected"),
    [
        ("Horizontal Wire, length 0.1270 mm", 100, "Horizontal Wire, length 12.7000 mm"),
        ("@(0.0635 mm, 0.0254 mm): Symbol R1", 100, "@(6.3500 mm, 2.5400 mm): Symbol R1"),
        ("length 0.2032 mm", 1.0, "length 0.2032 mm"),
        ("Pin 2 in sheet 3", 100, "Pin 2 in sheet 3"),  # integers are not dimensions
        ("0.5 mils and 0.01 in", 100, "50.0 mils and 1.00 in"),
        ("", 100, ""),
        # Library / footprint names embed "N.Nmm" with no space (real 9.0.5 / 10.0.0 ERC output,
        # spikes/versioning-strategy/out/erc_fresh.json): never a dimension, never rescaled.
        (
            "The current configuration does not include the footprint library 'Connector_PinSocket_2.54mm'",
            100,
            "The current configuration does not include the footprint library 'Connector_PinSocket_2.54mm'",
        ),
        (
            "Footprint 'DSUB-9_Female_Horizontal_P2.77x2.84mm_EdgePinOffset7.70mm_Housed_"
            "MountingHolesOffset9.12mm' not found in library 'Connector_Dsub'.",
            100,
            "Footprint 'DSUB-9_Female_Horizontal_P2.77x2.84mm_EdgePinOffset7.70mm_Housed_"
            "MountingHolesOffset9.12mm' not found in library 'Connector_Dsub'.",
        ),
        ("Text 'gap 0.5mm' near 1.2.5 mm and v1.5 mm", 100, "Text 'gap 0.5mm' near 1.2.5 mm and v1.5 mm"),
    ],
)
def test_rescale_description(text, scale, expected):
    assert rescale_description(text, scale) == expected


def test_unit_factor():
    assert unit_factor("mm") == 1.0 and unit_factor("in") == 25.4 and unit_factor("mils") == 0.0254
    assert unit_factor(None) == 1.0 and unit_factor("furlongs") == 1.0


@pytest.mark.parametrize("version", ["8.0", "9.0", "10.0"])
def test_erc_json_matches_text_report_after_rescale(version):
    """Real kicad-cli outputs from the spikes: JSON×100 == text report positions (claim 1)."""
    folder = "8.0/mini" if version == "8.0" else f"{version}/mini_erc"
    j = parse_erc_json(_read(f"{folder}/erc.json"), position_scale=100)
    t = parse_erc_text(_read(f"{folder}/erc.rpt"))
    assert j.kicad_version.startswith(version)
    assert (len(j.violations), j.errors, j.warnings) == (len(t.violations), t.errors, t.warnings)

    def key(v: Violation) -> tuple:
        return (v.type, round(v.pos.x_mm, 4), round(v.pos.y_mm, 4))

    assert sorted(map(key, j.violations)) == sorted(map(key, t.violations))
    assert all(v.sheet == "/" for v in j.violations + t.violations)


# ----------------------------------------------------------------------------------------------
# ERC text
# ----------------------------------------------------------------------------------------------

ERC_TEXT = """ERC report (2026-09-18T22:02:03, Encoding UTF8)
Report includes: Errors, Warnings, Exclusions

***** Sheet /
[label_dangling]: Label not connected
    ; error
    @(50.80 mm, 50.80 mm): Global Label 'FOO'
[pin_not_connected]: Pin not connected
    ; warning (excluded)
    @(63.50 mm, 59.69 mm): Symbol R1 Pin 1 [Passive, Line]
***** Sheet /Sub/
[wire_dangling]: Wires not connected to anything
    ; error
    @(2.0000 in, 1.0000 in): Horizontal Wire, length 12.70 mm
    @(76.20 mm, 50.80 mm): second item

 ** ERC messages: 3  Errors 2  Warnings 1

 ** Ignored checks:
    - Global label only appears once in the schematic
"""


def test_erc_text_parsing():
    r = parse_erc_text(ERC_TEXT, source="s.kicad_sch", kicad_version="10.0.0")
    assert r.kind == "erc" and len(r.violations) == 3
    a, b, c = r.violations
    assert (a.type, a.severity, a.sheet, a.pos) == ("label_dangling", "error", "/", Position(50.8, 50.8))
    assert a.items[0].description == "Global Label 'FOO'" and a.items[0].uuid == ""
    assert b.excluded and b.severity == "warning"
    assert c.sheet == "/Sub/" and len(c.items) == 2
    assert c.items[0].pos == Position(50.8, 25.4)  # inches converted
    assert c.items[1].description == "second item"
    assert r.errors == 2 and r.warnings == 0  # the excluded warning does not count
    assert r.raw["format"] == "text"


def test_erc_text_v8_severity_spelling_and_four_decimals():
    text = "ERC report (2026-09-18T22:02:01+0500, Encoding UTF8)\n\n***** Sheet /\n[x]: Desc\n    ; error\n    @(76.2000 mm, 50.8000 mm): Horizontal Wire, length 12.7000 mm\n\n ** ERC messages: 1  Errors 1  Warnings 0\n"
    r = parse_erc_text(text)
    assert r.violations[0].pos == Position(76.2, 50.8) and r.errors == 1


# ----------------------------------------------------------------------------------------------
# DRC JSON / text
# ----------------------------------------------------------------------------------------------


def test_drc_json_buckets_and_counts():
    r = parse_drc_json(_read("10.0/headless_board/drc_medium_unrouted.json"))
    assert r.kind == "drc" and r.source == "medium.kicad_pcb" and r.kicad_version == "10.0.0"
    assert len(r.violations) == 0 and len(r.unconnected) == 46
    assert r.errors == 46 and r.count("error", include_unconnected=False) == 0 and not r.ok
    u = r.unconnected[0]
    assert u.type == "unconnected_items" and u.sheet is None and len(u.items) == 2
    assert u.pos == Position(18.8, 19.1625)  # DRC JSON is already in mm (claim 1)
    assert r.raw["$schema"].endswith("drc.v1.json")


def test_drc_json_schematic_parity_and_source_override():
    r = parse_drc_json(_read("10.0/headless_board/drc_pic_programmer_parity.json"), source="x.kicad_pcb")
    assert r.source == "x.kicad_pcb"
    assert len(r.schematic_parity) == 8 and r.schematic_parity[0].type == "net_conflict"
    assert r.schematic_parity[0].pos == Position(107.95, 121.92)
    assert r.ok  # parity items are warnings and never gate


def test_drc_json_position_scale_is_applied_when_asked():
    r = parse_drc_json(_read("10.0/headless_board/drc_medium_unrouted.json"), position_scale=2)
    assert r.unconnected[0].pos == Position(37.6, 38.325)


def test_drc_text_sections_and_v7_severity_spelling():
    r = parse_drc_text(
        _read("7.0/tiny_routed/drc.rpt"), source="tiny_routed.kicad_pcb", kicad_version="7.0.6"
    )
    assert r.kind == "drc" and r.kicad_version == "7.0.6"
    assert len(r.violations) == 2 and r.warnings == 2 and r.errors == 0 and r.ok
    v = r.violations[0]
    assert v.type == "lib_footprint_issues" and v.severity == "warning"
    assert v.pos == Position(5.0, 7.5) and v.items[0].description == "Footprint R1"
    assert r.raw["counts"] == {"drc violations": 2, "unconnected pads": 0, "footprint errors": 0}
    assert r.unconnected == () and r.schematic_parity == ()


def test_drc_text_multi_item_violations_and_rules():
    r = parse_drc_text(_read("9.0/headless_board/drc_swig.rpt"))
    assert len(r.violations) == 6 and r.errors == 6
    first = r.violations[0]
    assert first.type == "tracks_crossing" and len(first.items) == 2
    assert first.items[1] == ViolationItem(
        uuid="", description="Track [GND] on F.Cu, length 8.1505 mm", pos=Position(22.0, 9.175)
    )


DRC_TEXT_FULL = """** Drc report for board.kicad_pcb **
** Created on 2026-09-18 20:49:48 **

** Found 1 DRC violations **
[clearance]: Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1000 mm)
    Rule: netclass 'Default'; error
    @(1.0000 mm, 2.0000 mm): Track [A] on F.Cu, length 3.0000 mm
    @(4.0000 mm, 5.0000 mm): Pad 1 [B] of R1 on F.Cu

** Found 1 unconnected pads **
[unconnected_items]: Missing connection between items
    ; error (excluded)
    @(6.0000 mm, 7.0000 mm): Pad 2 [C] of R1 on F.Cu
    @(8.0000 mm, 9.0000 mm): Pad 1 [C] of R2 on F.Cu

** Found 1 Footprint errors **
[net_conflict]: Pad net (X) doesn't match net given by schematic (Y)
    ; warning
    @(10.0000 mm, 11.0000 mm): PTH pad 1 [X] of D3

** End of Report **
"""


def test_drc_text_all_sections():
    r = parse_drc_text(DRC_TEXT_FULL)
    assert [v.type for v in r.violations] == ["clearance"]
    assert [v.type for v in r.unconnected] == ["unconnected_items"]
    assert [v.type for v in r.schematic_parity] == ["net_conflict"]
    assert r.violations[0].items[1].pos == Position(4.0, 5.0)
    assert r.unconnected[0].excluded and r.errors == 1 and r.ok is False
    assert r.raw["counts"] == {"drc violations": 1, "unconnected pads": 1, "footprint errors": 1}


def test_report_model_helpers():
    r = Report(kind="drc", source="s", kicad_version="10.0.0", violations=())
    assert r.ok and r.errors == 0 and r.warnings == 0
    v = Violation(type="t", severity="error", description="d", items=())
    assert v.pos is None
