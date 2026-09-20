"""Unit tests for kiwrap.formats.board on hand-written boards in both net dialects (no KiCad needed).

The geometry facts asserted here (pad/graphic transform, absolute pad angle, courtyard chaining)
are the ones conformance/tests/t2_file_models.py verifies against kicad-cli on every installed
KiCad; the fixtures below are minimal boards that exercise each dialect branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiwrap.formats.board import BoardFile, Net, read_bool
from kiwrap.formats.geometry import Vec2

# KiCad 9-style board: numeric net table, (net N "name") on pads, (net N) on tracks/zones,
# uuid tokens, property-based reference, a flipped footprint, a rotated footprint.
BOARD_V9 = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(generator_version "9.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t\t(copper_finish "None")
\t\t)
\t\t(pad_to_mask_clearance 0.05)
\t\t(pcbplotparams (layerselection 0x00000000_00000000_00000000_000000af) (usegerberextensions no))
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VCC")
\t(footprint "Lib:R" (layer "F.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t\t(at 10 20 90)
\t\t(property "Reference" "R1" (at 0 0 90) (layer "F.SilkS") (uuid "r1ref") (effects (font (size 1 1))))
\t\t(property "Value" "10k" (at 0 0 90) (layer "F.Fab") (uuid "r1val") (effects (font (size 1 1))))
\t\t(property "Sheetfile" "test.kicad_sch")
\t\t(path "/root/r1")
\t\t(attr smd)
\t\t(fp_line (start -2 -1) (end 2 -1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "l1"))
\t\t(fp_line (start 2 -1) (end 2 1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "l2"))
\t\t(fp_line (start 2 1) (end -2 1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "l3"))
\t\t(fp_line (start -2 1) (end -2 -1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "l4"))
\t\t(pad "1" smd rect (at -1 0 90) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "p1"))
\t\t(pad "2" smd rect (at 1 0 90) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "VCC") (pintype "passive") (uuid "p2"))
\t)
\t(footprint "Lib:C" (layer "B.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000002")
\t\t(at 30 40)
\t\t(property "Reference" "C1" (at 0 0 0) (layer "B.SilkS") (uuid "c1ref") (effects (font (size 1 1))))
\t\t(property "Value" "100n" (at 0 0 0) (layer "B.Fab") (uuid "c1val") (effects (font (size 1 1))))
\t\t(attr smd exclude_from_pos_files)
\t\t(fp_rect (start -1.5 1.5) (end 1.5 -1.5) (stroke (width 0.05) (type solid)) (layer "B.CrtYd") (uuid "cr"))
\t\t(fp_circle (center 0 0) (end 0.4 0) (stroke (width 0.1) (type solid)) (layer "B.SilkS") (uuid "cc"))
\t\t(pad "1" smd roundrect (at -0.8 0) (size 0.9 1) (layers "B.Cu" "B.Paste" "B.Mask") (roundrect_rratio 0.25) (net 1 "GND") (uuid "cp1"))
\t\t(pad "2" smd oval (at 0.8 0) (size 0.9 1) (layers "B.Cu" "B.Paste" "B.Mask") (net 2 "VCC") (uuid "cp2"))
\t)
\t(footprint "Lib:TH" (layer "F.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000003")
\t\t(at 50 50 45)
\t\t(property "Reference" "J1" (at 0 0 0) (layer "F.SilkS") (uuid "j1ref") (effects (font (size 1 1))))
\t\t(property "Value" "Conn" (at 0 0 0) (layer "F.Fab") (uuid "j1val") (effects (font (size 1 1))))
\t\t(attr through_hole)
\t\t(fp_arc (start -1 0) (mid 0 -1) (end 1 0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "a1"))
\t\t(fp_line (start 1 0) (end -1 0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "a2"))
\t\t(fp_poly (pts (xy -3 -3) (xy 3 -3) (xy 3 3) (xy -3 3)) (stroke (width 0.05) (type solid)) (fill no) (layer "F.Fab") (uuid "pp"))
\t\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net 1 "GND") (uuid "jp1"))
\t\t(pad "2" np_thru_hole circle (at 2 0 45) (size 1.6 1.6) (drill 1.2 2.0 (offset 0.1 0)) (layers "*.Cu" "*.Mask") (uuid "jp2"))
\t\t(pad "3" thru_hole custom (at 4 0) (size 1 1) (drill 0.5) (layers "*.Cu") (options (clearance outline) (anchor rect))
\t\t\t(primitives (gr_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) (width 0.1) (fill yes)))
\t\t\t(uuid "jp3"))
\t)
\t(gr_line (start 0 0) (end 100 0) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e1"))
\t(gr_line (start 100 0) (end 100 80) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e2"))
\t(gr_line (start 100 80) (end 0 80) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e3"))
\t(gr_line (start 0 80) (end 0 0) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e4"))
\t(gr_circle (center 80 60) (end 82 60) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "hole"))
\t(segment (start 9 20) (end 9 30) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))
\t(segment locked (start 11 20) (end 11 30) (width 0.25) (layer "F.Cu") (net 2) (uuid "s2"))
\t(arc (start 20 20) (mid 25 15) (end 30 20) (width 0.25) (layer "F.Cu") (net 2) (uuid "arc1"))
\t(via blind (at 9 30) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
\t(via (at 11 30) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (free yes) (net 2) (uuid "v2"))
\t(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1") (hatch edge 0.5) (connect_pads (clearance 0.5)) (min_thickness 0.25)
\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon (pts (xy 1 1) (xy 99 1) (xy 99 79) (xy 1 79)))
\t\t(filled_polygon (layer "B.Cu") (pts (xy 2 2) (xy 98 2) (xy 98 78) (xy 2 78)))
\t)
\t(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid "z2") (hatch edge 0.5) (keepout (tracks not_allowed) (vias not_allowed))
\t\t(polygon (pts (xy 60 60) (xy 70 60) (xy 70 70) (xy 60 70)))
\t)
\t(embedded_fonts no)
)
"""

# KiCad 7-style: tstamp, fp_text reference/value, true/false, 2-word layerselection, 7 layer ids.
BOARD_V7 = """(kicad_pcb (version 20221018) (generator pcbnew)

  (general
    (thickness 1.6)
  )

  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
  )

  (setup
    (pad_to_mask_clearance 0)
    (pcbplotparams
      (layerselection 0x00010fc_ffffffff)
      (disableapertmacros false)
      (usegerberextensions false)
      (plotreference true)
    )
  )

  (net 0 "")
  (net 1 "Net-(R1-Pad1)")

  (footprint "Lib:R" (layer "F.Cu")
    (tstamp 00000000-0000-0000-0000-000000000001)
    (at 10 10)
    (attr smd)
    (fp_text reference "R1" (at 0 -2) (layer "F.SilkS")
        (effects (font (size 1 1) (thickness 0.15)))
      (tstamp 00000000-0000-0000-0000-000000000011)
    )
    (fp_text value "1k" (at 0 2) (layer "F.Fab")
        (effects (font (size 1 1) (thickness 0.15)))
      (tstamp 00000000-0000-0000-0000-000000000012)
    )
    (fp_rect (start -2 -1) (end 2 1)
      (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (tstamp 00000000-0000-0000-0000-000000000013))
    (pad "1" smd rect (at -1 0) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "Net-(R1-Pad1)") (tstamp 00000000-0000-0000-0000-000000000014))
    (pad "2" smd rect (at 1 0) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (tstamp 00000000-0000-0000-0000-000000000015))
  )

  (gr_rect (start 0 0) (end 50 30) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (tstamp 00000000-0000-0000-0000-000000000020))
  (segment (start 9 10) (end 9 20) (width 0.25) (layer "F.Cu") (net 1) (tstamp 00000000-0000-0000-0000-000000000030))
)
"""

# KiCad 10.0.6-style (20260206): no net table, (net "name") everywhere, dnp as a separate token.
BOARD_V10 = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t)
\t(setup (pad_to_mask_clearance 0))
\t(footprint "Lib:R" (layer "F.Cu") (uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t\t(at 10 10)
\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "r1ref") (effects (font (size 1 1))))
\t\t(property "Value" "1k" (at 0 0 0) (layer "F.Fab") (uuid "r1val") (effects (font (size 1 1))))
\t\t(attr smd)
\t\t(dnp yes)
\t\t(fp_rect (start -2 -1) (end 2 1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd") (uuid "cr"))
\t\t(pad "1" smd rect (at -1 0) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net "GND") (uuid "p1"))
\t\t(pad "2" smd rect (at 1 0) (size 1 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net "VCC") (uuid "p2"))
\t)
\t(gr_rect (start 0 0) (end 50 30) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e"))
\t(segment (start 9 10) (end 9 20) (width 0.25) (layer "F.Cu") (net "GND") (uuid "s1"))
\t(via (at 9 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "GND") (uuid "v1"))
\t(zone (net "GND") (layer "B.Cu") (uuid "z1") (hatch edge 0.5) (min_thickness 0.25) (fill yes)
\t\t(polygon (pts (xy 1 1) (xy 49 1) (xy 49 29) (xy 1 29)))
\t)
)
"""


@pytest.fixture
def board9() -> BoardFile:
    return BoardFile.loads(BOARD_V9)


def test_read_bool_dialects():
    assert read_bool("yes") and read_bool("true") and read_bool("1")
    assert not read_bool("no") and not read_bool("false") and not read_bool("0")
    assert read_bool(None, True) and read_bool("maybe", False) is False


def test_header_layers_and_nets(board9: BoardFile):
    assert board9.stamp == 20241229 and board9.generator == "pcbnew" and board9.generator_version == "9.0"
    assert [layer.name for layer in board9.layers] == ["F.Cu", "B.Cu", "Edge.Cuts", "F.CrtYd", "B.CrtYd"]
    assert board9.copper_layers == ("F.Cu", "B.Cu")
    assert board9.layer("B.CrtYd").user_name == "B.Courtyard" and board9.layer("B.CrtYd").id == 29
    assert board9.nets == (Net(0, ""), Net(1, "GND"), Net(2, "VCC")) and board9.net_codes_in_file
    assert board9.net("GND") == Net(1, "GND") and board9.net("nope") is None


def test_footprint_transform_and_pads(board9: BoardFile):
    r1 = board9.footprint("R1")
    assert r1 is not None and r1.value == "10k" and r1.fpid == "Lib:R" and r1.side == "front"
    assert r1.position == Vec2(10, 20) and r1.rotation == 90 and r1.is_smd and not r1.dnp
    assert r1.path == "/root/r1" and r1.properties["Sheetfile"] == "test.kicad_sch"
    # local (-1, 0) rotated by 90 with KiCad's convention -> (0, +1): board (10, 21)
    p1 = r1.pad("1")
    assert p1.position == Vec2(10, 21) and p1.local_position == Vec2(-1, 0) and p1.rotation == 90
    assert r1.pad("2").position == Vec2(10, 19)
    assert p1.net == Net(1, "GND") and r1.pad("2").pin_type == "passive"
    assert (
        p1.expand_layers() == ("F.Cu", "F.Paste", "F.Mask")
        and p1.on_layer("F.Cu")
        and not p1.on_layer("B.Cu")
    )
    # the courtyard rectangle rotates with the footprint: 4 x 2 becomes 2 x 4 around (10, 20)
    assert len(r1.courtyard_front) == 1 and not r1.courtyard_back and not r1.courtyard_open
    box = r1.courtyard_front[0].bbox
    assert (box.min_x, box.min_y, box.max_x, box.max_y) == (9, 18, 11, 22)
    assert r1.courtyard == r1.courtyard_front
    # flipped footprint: local coordinates are stored already mirrored; back courtyard from fp_rect
    c1 = board9.footprint("C1")
    assert c1.side == "back" and c1.exclude_from_pos_files and c1.courtyard == c1.courtyard_back
    assert c1.courtyard_back[0].bbox.min_x == pytest.approx(28.5) and c1.courtyard_back[
        0
    ].bbox.max_y == pytest.approx(41.5)
    assert c1.pad("1").shape == "roundrect" and c1.pad("1").roundrect_rratio == 0.25
    polys = c1.pad("1").polygons()
    assert len(polys) == 1 and polys[0].bbox.width == pytest.approx(0.9, abs=1e-6)
    assert c1.pad("2").polygons()[0].bbox.height == pytest.approx(1.0, abs=0.01)  # arc sampling error < 5 um
    assert c1.pad("1").expand_layers(board9.copper_layers) == ("B.Cu", "B.Paste", "B.Mask")
    # through-hole with wildcard layers, slot drill with offset, custom pad primitives
    j1 = board9.footprint("J1")
    assert j1.rotation == 45 and j1.pad("1").drill.diameter == 0.8 and j1.pad("1").is_through_hole
    assert j1.pad("1").expand_layers(board9.copper_layers) == ("F.Cu", "B.Cu", "F.Mask", "B.Mask")
    d = j1.pad("2").drill
    assert d.is_slot and d.width == 2.0 and d.offset == Vec2(0.1, 0) and j1.pad("2").type == "np_thru_hole"
    p2 = j1.pad("2").position
    assert p2.x == pytest.approx(50 + 2 * 0.7071067811865476) and p2.y == pytest.approx(
        50 - 2 * 0.7071067811865476
    )
    assert j1.pad("3").shape == "custom" and len(j1.pad("3").primitives) == 1
    assert len(j1.pad("3").polygons()) == 2
    # arc + line close into a half-disc courtyard
    assert len(j1.courtyard_front) == 1 and len(j1.courtyard_front[0]) > 10
    assert j1.pad("1").bbox().width == pytest.approx(1.6, abs=0.01)
    assert j1.bbox().contains(Vec2(50, 50))
    graphics = [g for g in j1.graphics if g.kind == "poly"]
    assert graphics and graphics[0].closed and graphics[0].polygon() is not None


def test_tracks_vias_zones_outline(board9: BoardFile):
    assert len(board9.tracks) == 2 and board9.tracks[1].locked and board9.tracks[0].net == Net(1, "GND")
    assert board9.tracks[0].length() == 10 and board9.tracks[0].uuid == "s1"
    assert len(board9.arcs) == 1 and board9.arcs[0].mid == Vec2(25, 15) and len(board9.arcs[0].points()) > 3
    assert [v.kind for v in board9.vias] == ["blind", "through"] and board9.vias[1].free
    assert board9.vias[0].layers == ("F.Cu", "B.Cu") and board9.vias[0].net.name == "GND"
    z1, z2 = board9.zones
    assert z1.net == Net(1, "GND") and z1.layers == ("B.Cu",) and len(z1.outline) == 1 and "B.Cu" in z1.filled
    assert z1.filled["B.Cu"][0].bbox.max_x == 98 and not z1.is_rule_area
    assert z2.is_rule_area and z2.layers == ("F.Cu", "B.Cu") and z2.net == Net(0, "")
    # outline: rectangle plus a circular cutout nested as a hole
    assert len(board9.outline) == 1 and not board9.outline_open
    outer = board9.board_polygon()
    assert outer is not None and len(outer.holes) == 1 and outer.contains(Vec2(50, 40))
    assert not outer.contains(Vec2(80, 60))  # inside the cutout
    assert board9.outline_bbox().max_x == 100 and board9.outline_bbox().max_y == 80
    items = board9.items_by_net("GND")
    assert (
        len(items["pads"]) == 3
        and len(items["tracks"]) == 1
        and len(items["vias"]) == 1
        and len(items["zones"]) == 1
    )


def test_design_rules(board9: BoardFile):
    dr = board9.design_rules
    assert (
        dr.pad_to_mask_clearance == 0.05 and dr.scalar("missing") is None and dr.scalar("missing", 1.0) == 1.0
    )
    assert [s.name for s in dr.stackup] == ["F.Cu", "dielectric 1", "B.Cu"]
    assert (
        dr.stackup[1].material == "FR4" and dr.stackup[1].epsilon_r == 4.5 and dr.stackup[1].thickness == 1.51
    )
    assert dr.values["stackup.copper_finish"] == ("None",)
    assert dr.plot_params["layerselection"] == ("0x00000000_00000000_00000000_000000af",)
    assert dr.project_rules is None


def test_v7_dialect():
    b = BoardFile.loads(BOARD_V7)
    assert b.stamp == 20221018 and b.generator == "pcbnew" and b.generator_version is None
    r1 = b.footprint("R1")
    assert r1 is not None and r1.value == "1k" and r1.uuid == "00000000-0000-0000-0000-000000000001"
    assert r1.pad("1").net == Net(1, "Net-(R1-Pad1)") and r1.pad("2").net is None
    assert r1.pad("1").uuid == "00000000-0000-0000-0000-000000000014"
    assert b.layer("B.Cu").id == 31 and b.copper_layers == ("F.Cu", "B.Cu")
    assert b.design_rules.plot_params["layerselection"] == ("0x00010fc_ffffffff",)
    assert b.design_rules.plot_params["plotreference"] == ("true",)
    assert len(b.outline) == 1 and b.outline[0].bbox.max_x == 50
    assert len(r1.courtyard_front) == 1 and r1.courtyard_front[0].bbox.min_x == 8
    assert b.tracks[0].net.name == "Net-(R1-Pad1)"


def test_v10_name_only_nets(tmp_path: Path):
    p = tmp_path / "t.kicad_pcb"
    p.write_text(BOARD_V10, encoding="utf-8")
    (tmp_path / "t.kicad_pro").write_text(
        json.dumps(
            {"board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.15}}}}
        ),
        encoding="utf-8",
    )
    b = BoardFile.load(p)
    assert not b.net_codes_in_file and b.nets == (Net(None, "GND"), Net(None, "VCC"))
    r1 = b.footprint("R1")
    assert r1.pad("1").net == Net(None, "GND") and r1.dnp and "dnp" in r1.attributes
    assert b.tracks[0].net is b.nets[0] and b.vias[0].net.name == "GND" and b.zones[0].net.name == "GND"
    assert b.design_rules.project_rules == {"min_clearance": 0.2, "min_track_width": 0.15}
    assert b.path == p and b.root.head == "kicad_pcb"
    assert BoardFile.load(p, project_rules=False).design_rules.project_rules is None


def test_nightly_transform_placement_reads_like_at():
    """The 10.99 nightly (stamp 20260616+) saves a footprint's placement as `(transform (translate
    x y) (rotate r) (scale 1 1))` instead of `(at x y r)`; the reader takes either (module
    docstring), so boards the api-server saved keep their positions in kw_pcb_get / the placement
    checks. Pad angles stay absolute in both forms."""
    transform = "\t\t(transform\n\t\t\t(translate 10 20)\n\t\t\t(rotate 90)\n\t\t\t(scale 1 1)\n\t\t)\n"
    text = BOARD_V9.replace("(version 20241229)", "(version 20260901)").replace(
        "\t\t(at 10 20 90)\n", transform
    )
    assert "(translate 10 20)" in text
    b = BoardFile.loads(text)
    r1 = b.footprint("R1")
    assert r1.position == Vec2(10, 20) and r1.rotation == 90
    assert r1.pad("1").position == Vec2(10, 21) and r1.pad("2").position == Vec2(10, 19)
    box = r1.courtyard_front[0].bbox
    assert (box.min_x, box.min_y, box.max_x, box.max_y) == (9, 18, 11, 22)
    # `(at …)` wins when both are present, as in KiCad's parser
    both = text.replace(transform, "\t\t(at 1 2 0)\n" + transform)
    assert BoardFile.loads(both).footprint("R1").position == Vec2(1, 2)


def test_copper_layers_are_the_leading_typed_block_like_kicads_parser():
    """Phase 1c finding: KiCad 8.99 / 9.0.0 wrote `User.N` rows typed `signal` AFTER the `user`
    rows (10.0.6's multichannel_mixer-unrouted demo); `parseLayers` stops at the first `user` row,
    so those are user layers, not copper - the codec, the writer and the fab export must agree."""
    from kiwrap.formats.board_writer import BoardWriter

    text = BOARD_V9.replace(
        '\t\t(29 "B.CrtYd" user "B.Courtyard")',
        '\t\t(29 "B.CrtYd" user "B.Courtyard")\n\t\t(39 "User.1" signal)\n\t\t(41 "User.2" power)',
    )
    board = BoardFile.loads(text)
    assert [layer.name for layer in board.layers][-2:] == ["User.1", "User.2"]
    assert board.copper_layers == ("F.Cu", "B.Cu")
    user1 = board.layer("User.1")
    assert user1 is not None and not user1.is_copper and user1.has_copper_type and user1.kind == "signal"
    assert board.layer("F.Cu").is_copper and board.layer("Edge.Cuts").has_copper_type is False
    # the writer's own copper list follows the same rule, and setting the stack leaves User.N alone
    w = BoardWriter(Path(), epoch={"pcb": 20241229}, lib_resolver=None)
    w.root = BoardFile.loads(text).root
    w._copper = w._copper_layers()
    assert w._copper == ["F.Cu", "B.Cu"]
    assert w.setup(layers=4)["layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    names = [row.values()[1] for row in w.root.find("layers").nodes]
    assert names[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"] and names[-2:] == ["User.1", "User.2"]
    assert BoardFile.from_node(w.root).copper_layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def test_rejects_non_board():
    with pytest.raises(ValueError):
        BoardFile.loads("(kicad_sch (version 1))")


def test_cst_is_preserved(board9: BoardFile):
    """The semantic view keeps the CST: unknown tokens survive and edits go through the nodes."""
    assert board9.root.find("embedded_fonts") is not None
    r1 = board9.footprint("R1")
    assert r1.node is not None and r1.node.get("path") == "/root/r1"
    at = r1.node.find("at")
    at.atoms[0].value = 11.5
    from kiwrap.formats.sexpr import dumps

    reparsed = BoardFile.loads(dumps(board9.root))
    assert reparsed.footprint("R1").position == Vec2(11.5, 20)
    assert reparsed.footprint("R1").pad("1").position == Vec2(11.5, 21)
