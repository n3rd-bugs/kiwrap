"""Parser unit tests for the structured kicad-cli exports (no KiCad needed).

Fixtures under tests/fixtures/structured/ are real kicad-cli outputs captured on 2026-09-19
(10.0.6, 8.0.9 and the 10.99.0-4083 nightly against the ecc83 / pic_programmer / jetson demos;
the IPC-2581 files are pruned to two components each, see scratchpad structured/). Every
expected number below was cross-checked against the demo board file or the `--format text`
output of the same command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.formats.reports import ipc2581, ipcd356, position, schdiff, stackup, stats

FIXTURES = Path(__file__).parent / "fixtures" / "structured"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------------------------


class TestStats:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("52.0700 mm", (52.07, "mm")),
            ("2413.705 mm²", (2413.705, "mm2")),
            ("2413.705 mmÂ²", (2413.705, "mm2")),  # UTF-8 "²" read through cp1252
            ("1.5 mm^2", (1.5, "mm2")),
            ("2.0 mm2", (2.0, "mm2")),
            ("1.0 in", (25.4, "mm")),
            ("1.0 in²", (645.16, "mm2")),
            ("10 mils", (0.254, "mm")),
            ("51.80", (51.8, "")),
            (12, (12.0, "")),
            ("3.5 furlong", (3.5, "furlong")),
        ],
    )
    def test_parse_quantity(self, text, expected):
        value, unit = stats.parse_quantity(text)
        assert unit == expected[1]
        assert value == pytest.approx(expected[0])

    def test_parse_quantity_rejects_non_numbers(self):
        with pytest.raises(ValueError):
            stats.parse_quantity("mm")
        with pytest.raises(ValueError):
            stats.parse_quantity(True)

    def test_ecc83_stats_10_0(self):
        s = stats.parse_stats_json(fixture("stats_ecc83_10.0.json"), source="ecc83-pp.kicad_pcb")
        assert s.source == "ecc83-pp.kicad_pcb" and s.kicad_version == "KiCad 10.0.6"
        assert s.project == "ecc83-pp" and s.has_outline
        # the Edge.Cuts rectangle of the demo is 121.285..173.355 x 90.17..136.525
        assert s.width_mm == pytest.approx(52.07) and s.height_mm == pytest.approx(46.355)
        assert s.area_mm2 == pytest.approx(2413.705)
        assert s.board_thickness_mm == pytest.approx(1.6)
        assert s.front_component_density_pct == pytest.approx(51.8)
        assert s.min_track_width_mm == pytest.approx(0.8) and s.min_drill_diameter_mm == pytest.approx(0.8)
        assert s.pads.through_hole == 33 and s.pads.total == 33 and s.vias.total == 0
        assert s.components["tht"].front == 15 and s.component_count == 15
        assert len(s.drill_holes) == 5 and sum(h.count for h in s.drill_holes) == 33
        h = s.drill_holes[0]
        assert (h.shape, h.x_size_mm, h.plated, h.source) == ("Round", 0.8, True, "Pad")
        assert s.copper_layers_used == ("top_cu", "bottom_cu")  # the demo's renamed F.Cu / B.Cu
        assert s.raw["metadata"]["board_name"] == "ecc83-pp"

    def test_units_in_are_converted(self):
        text = fixture("stats_ecc83_10.0.json").replace('"width": "52.0700 mm"', '"width": "2.05 in"')
        s = stats.parse_stats_json(text)
        assert s.width_mm == pytest.approx(52.07)

    def test_missing_sections_are_tolerated(self):
        s = stats.parse_stats_json('{"metadata": {}, "board": {"width": ""}}')
        assert s.width_mm is None and s.pads.total == 0 and s.components == {} and s.drill_holes == ()


# --------------------------------------------------------------------------------------------
# stackup
# --------------------------------------------------------------------------------------------

JETSON_BOARD = Path("C:/Program Files/KiCad/10.99/share/kicad/demos/jetson-agx-thor-baseboard")


class TestStackup:
    def test_proto_layer_names(self):
        f = stackup.proto_layer_name
        assert f("BL_F_Cu") == "F.Cu" and f("BL_In12_Cu") == "In12.Cu" and f("BL_B_SilkS") == "B.SilkS"
        assert f("BL_UNDEFINED") == "" and f("BL_UNKNOWN") == "" and f("") == ""

    def test_ecc83_json(self):
        s = stackup.parse_stackup_json(fixture("stackup_ecc83_nightly.json"))
        assert s.source == "json" and s.copper_count == 2 and s.finish == "None"
        assert s.total_thickness_mm == pytest.approx(1.6)  # 0.01 + 0.035 + 1.51 + 0.035 + 0.01
        names = [layer.name for layer in s.layers]
        assert names == ["F.SilkS", "F.Paste", "F.Mask", "F.Cu", "", "B.Cu", "B.Mask", "B.Paste", "B.SilkS"]
        cu = s.layer("F.Cu")
        assert cu is not None and cu.user_name == "top_cu" and cu.thickness_mm == pytest.approx(0.035)
        assert cu.material == "copper" and s.layer("top_cu") is cu
        core = s.layers[4]
        assert core.kind == "dielectric" and core.dielectric_type == "core" and core.material == "FR4"
        assert (
            core.epsilon_r == 4.5 and core.loss_tangent == 0.02 and core.thickness_mm == pytest.approx(1.51)
        )
        mask = s.layer("F.Mask")
        assert (
            mask.kind == "soldermask" and mask.epsilon_r == 3.3 and mask.thickness_mm == pytest.approx(0.01)
        )
        # named colours ("Green") come through the proto as {"a": 1} only -> unknown, not black
        assert mask.color is None
        assert not s.impedance_controlled and s.edge_connector is None and not s.edge_plating

    def test_jetson_json_sublayers(self):
        s = stackup.parse_stackup_json(fixture("stackup_jetson_nightly.json"))
        assert s.copper_count == 10 and s.finish == "ENIG"
        d3 = s.layers[8]
        assert d3.kind == "dielectric" and d3.dielectric_type == "prepreg" and len(d3.sublayers) == 2
        # the slot's own thickness is the first sublayer's (70 um); the slot total is 2 x 0.07
        assert d3.thickness_mm == pytest.approx(0.14) and d3.material == "PR1080" and d3.epsilon_r == 3.96
        assert [sub.thickness_mm for sub in d3.sublayers] == [pytest.approx(0.07)] * 2
        assert d3.sublayers[1].loss_tangent is None  # proto3 omits the default 0
        assert s.total_thickness_mm == pytest.approx(1.711)

    def test_jetson_csv_matches_json(self):
        c = stackup.parse_stackup_csv(fixture("stackup_jetson_nightly.csv"))
        j = stackup.parse_stackup_json(fixture("stackup_jetson_nightly.json"))
        assert c.source == "csv" and c.finish == "ENIG" and c.copper_count == j.copper_count
        assert c.total_thickness_mm == pytest.approx(j.total_thickness_mm)
        assert [layer.kind for layer in c.layers] == [layer.kind for layer in j.layers]
        assert [layer.thickness_mm for layer in c.layers] == [
            pytest.approx(x) for x in [layer.thickness_mm for layer in j.layers]
        ]
        d3 = c.layers[8]
        assert (
            d3.user_name == "Dielectric 3" and len(d3.sublayers) == 2 and d3.sublayers[1].loss_tangent == 0.0
        )
        assert c.layer("F.Mask").color == "Black"  # the CSV keeps the colour name

    def test_board_file_with_sublayers(self):
        board = JETSON_BOARD / "jetson-agx-thor-baseboard.kicad_pcb"
        if not board.is_file():
            pytest.skip("nightly jetson demo not installed")
        b = stackup.parse_stackup_board(board.read_text(encoding="utf-8"))
        j = stackup.parse_stackup_json(fixture("stackup_jetson_nightly.json"))
        assert b.source == "board" and b.has_stackup_section and b.finish == "ENIG"
        assert b.copper_count == j.copper_count and b.total_thickness_mm == pytest.approx(
            j.total_thickness_mm
        )
        assert [layer.name for layer in b.copper_layers] == [layer.name for layer in j.copper_layers]
        assert len(b.layers[8].sublayers) == 2 and b.layers[8].thickness_mm == pytest.approx(0.14)
        assert not b.impedance_controlled

    def test_board_file_minimal_and_without_section(self):
        text = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal "bottom") (36 "B.SilkS" user "B.Silkscreen"))
  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "core") (thickness 1.51 locked) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
    (layer "B.Cu" (type "copper") (thickness 0.035))
    (copper_finish "HAL SnPb") (dielectric_constraints yes) (edge_connector bevelled) (castellated_pads yes)))
)"""
        s = stackup.parse_stackup_board(text)
        assert s.copper_count == 2 and s.finish == "HAL SnPb" and s.impedance_controlled
        assert s.edge_connector == "bevelled" and s.castellated_pads and not s.edge_plating
        assert s.layers[1].thickness_locked and s.layers[1].thickness_mm == 1.51
        assert s.total_thickness_mm == pytest.approx(1.58)
        bare = stackup.parse_stackup_board(
            '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal "bottom")) (setup))'
        )
        assert not bare.has_stackup_section and [layer.name for layer in bare.layers] == ["F.Cu", "B.Cu"]
        assert bare.layers[1].user_name == "bottom" and bare.total_thickness_mm is None


# --------------------------------------------------------------------------------------------
# sch diff
# --------------------------------------------------------------------------------------------


class TestSchDiff:
    def test_modified_symbols(self):
        d = schdiff.parse_sch_diff_json(
            fixture("schdiff_modified_nightly.json"), reference="a.kicad_sch", exit_code=5
        )
        assert d.reference == "a.kicad_sch" and d.comparison.endswith("mod3.kicad_sch")
        assert d.doc_type == "kicad_sch" and not d.identical and d.exit_code == 5
        assert d.counts == {"added": 0, "removed": 0, "modified": 2, "collision": 0, "duplicate_uuid": 0}
        r1, r2 = d.modified
        assert (r1.type_name, r1.refdes, r1.kind) == ("SCH_SYMBOL", "R1", "modified")
        assert r1.uuid == "00000000-0000-0000-0000-00004549f38a"
        assert r1.sheet_path == "/28f865a0-4433-4a53-bbd7-b62f276848e4"
        value = r1.property("Value")
        assert value is not None and value.before.value == "1.5K" and value.after.value == "2.2K"
        assert not value.is_distance and value.describe() == "Value: 1.5K -> 2.2K"
        # R2 was moved from x=154.94 mm to 165.1 mm in the board file (schematic IU = 1/10000 mm)
        pos = r2.property("Position X")
        assert (
            pos.is_distance
            and pos.before_mm == pytest.approx(154.94)
            and pos.after_mm == pytest.approx(165.1)
        )
        assert pos.before.value == 1549400 and pos.describe() == "Position X: 154.94 mm -> 165.1 mm"
        assert (
            r2.bbox is not None
            and r2.bbox.x_mm == pytest.approx(151.0491)
            and r2.bbox.h_mm == pytest.approx(39.6853)
        )
        assert "modified SCH_SYMBOL R1" in d.summary()

    def test_removed_wire(self):
        d = schdiff.parse_sch_diff_json(fixture("schdiff_removed_nightly.json"))
        assert len(d.removed) == 1 and d.added == () and d.modified == ()
        wire = d.removed[0]
        assert (
            wire.type_name == "SCH_LINE"
            and wire.refdes is None
            and wire.describe().startswith("removed SCH_LINE 06655b90")
        )
        # every property of a removed item has `after: none`; the wire ran 185.42 -> 198.12 mm
        assert all(not p.after.exists for p in wire.properties)
        assert wire.property("Start X").before_mm == pytest.approx(185.42)
        assert wire.property("End X").before_mm == pytest.approx(198.12)
        assert wire.property("Length").before_mm == pytest.approx(12.7)
        style = wire.property("Wire Style")
        assert (
            style.before.type == "enum"
            and style.before.label == "Solid"
            and style.before.display() == "Solid"
        )
        assert style.after.display() == "<none>"
        color = wire.property("Color")
        assert color.before.type == "color" and color.before.value == [0.0, 0.0, 0.0, 0.0]
        assert wire.property("Locked").before.display() == "false"

    def test_identical(self):
        d = schdiff.parse_sch_diff_json(
            '{"changes": [], "docType": "kicad_sch", "path": "x.kicad_sch"}', exit_code=0
        )
        assert d.identical and d.comparison == "x.kicad_sch" and d.summary() == "0 change(s)"

    def test_distance_property_names(self):
        f = schdiff.is_distance_property
        assert all(f(n) for n in ("Position X", "End Y", "Length", "Line Width", "Text Size", "Pin Length"))
        assert not any(f(n) for n in ("Value", "Reference", "Wire Style", "Locked", "Color", "Unit"))


# --------------------------------------------------------------------------------------------
# IPC-D-356
# --------------------------------------------------------------------------------------------


class TestIpcd356:
    def test_ecc83_10_0(self):
        t = ipcd356.parse_ipcd356(fixture("ipcd356_ecc83_10.0.d356"), source="board.d356")
        assert t.units_code == "0" and t.aliases == {} and t.job == ""
        assert len(t.points) == 33 and t.vias == () and len(t.pads) == 33
        c1 = t.of("C1")
        assert [p.pin for p in c1] == ["1", "2"]
        p = c1[0]
        # C1 pad 1 sits at the footprint origin (141.605, 99.695) in the board file: decimils, y up
        assert (p.x_mm, p.y_mm) == (pytest.approx(141.605), pytest.approx(99.695))
        assert p.y_raw_mm == pytest.approx(-99.695)
        assert (
            p.drill_mm == pytest.approx(1.0, abs=0.002)
            and p.plated
            and p.record == "317"
            and p.kind == "pad_th"
        )
        assert p.width_mm == pytest.approx(2.0, abs=0.002) and p.rotation_deg == 270.0 and p.access == 0
        # 10.0 upper-cases and truncates net names to the last 14 characters
        assert p.net == "NET-(P3-P1)" and "CTED-(P5-PAD1)" in t.nets
        assert p.soldermask == 0  # 10.0 writes "R270S0" with no separating space

    def test_ecc83_nightly_aliases(self):
        t = ipcd356.parse_ipcd356(fixture("ipcd356_ecc83_nightly.d356"))
        assert t.job == "ecc83-pp" and t.aliases["M0000"] == "unconnected-(P5-Pad1)"
        assert t.of("P5")[0].net == "unconnected-(P5-Pad1)"  # alias resolved
        # names that fit in 14 columns are still upper-cased (sanitize_d356_name(.., aUpper=true))
        assert set(t.nets) >= {"GND", "NET-(P1-PM)", "NET-(U1A-G)"}
        assert len(t.points) == 33

    def test_pic_excerpt_vias_smd_npth(self):
        t = ipcd356.parse_ipcd356(fixture("ipcd356_pic_excerpt_nightly.d356"), aux_origin_mm=(10.0, 20.0))
        vias = t.vias
        assert len(vias) == 2 and all(v.midpoint and v.pin == "" and v.kind == "via" for v in vias)
        v = vias[0]
        assert v.net == "/CLOCK-RB6" and v.drill_mm == pytest.approx(0.6, abs=0.002) and v.soldermask == 3
        # X+050250 Y+017000 decimils relative to the aux origin (10, 20), y up
        assert v.x_mm == pytest.approx(10 + 127.635) and v.y_mm == pytest.approx(20 - 43.18)
        smd = [p for p in t.points if p.record == "327"]
        assert len(smd) == 2 and smd[0].ref == "JP1" and smd[0].kind == "pad_smd"
        assert smd[0].drill_mm is None and smd[0].plated is None and smd[0].access == 2  # bottom side
        assert smd[0].net == "/pic_sockets/VCC_PIC"  # alias M0001 resolved through the NNAME table
        npth = [p for p in t.points if p.record == "367"][0]
        assert npth.ref == "P101" and npth.net == "N/C" and npth.plated is False and npth.kind == "hole_npth"
        assert t.of("U1")[0].width_mm == pytest.approx(2.4, abs=0.002) and t.of("U1")[
            0
        ].height_mm == pytest.approx(1.6, abs=0.002)

    def test_negative_rotation_and_blind_via(self):
        # laid out with KiCad's own format string: "%03d%-14.14s   %-6.6s%c%-4.4s%c"
        pad = f"317{'NET1':<14}   {'R1':<6}-{'1':<4} D0315PA00X+010000Y-020000X0630Y0000R-90 S0"
        bb = f"307{'NET1':<14}   {'VIA':<6} {'':<4}MD0200PA01X+010000Y-020000{'':14} S0L01L02"
        t = ipcd356.parse_ipcd356(f"P  UNITS CUST 0\n{pad}\n{bb}\n999\n")
        assert t.points[0].rotation_deg == 270.0 and t.points[0].x_mm == pytest.approx(25.4)
        bb = t.points[1]
        assert bb.record == "307" and bb.kind == "via" and bb.start_layer == 1 and bb.end_layer == 2
        assert bb.width_mm is None and bb.access == 1


# --------------------------------------------------------------------------------------------
# position files
# --------------------------------------------------------------------------------------------


class TestPosition:
    def test_ecc83_csv(self):
        p = position.parse_pos(fixture("pos_ecc83_nightly.csv"), source="pos.csv")
        assert p.format == "csv" and len(p.rows) == 11 and p.refs[:3] == ("C1", "C2", "P1")
        c1 = p.row("C1")
        # `(at 141.605 99.695 90)` in the board file -> csv 141.605000,-99.695000 (y up)
        assert (c1.x_mm, c1.y_mm, c1.rotation_deg, c1.side) == (141.605, 99.695, 90.0, "top")
        assert c1.y_raw == -99.695 and c1.value == "10uF" and c1.package == "CP_Radial_D10.0mm_P5.00mm"
        assert p.side("bottom") == ()

    def test_bottom_rows_and_unicode(self):
        p = position.parse_pos(fixture("pos_pic_bottom_nightly.csv"))
        jp1 = p.row("JP1")
        assert jp1.side == "bottom" and jp1.x_mm == 148.082 and jp1.y_mm == 97.79 and jp1.rotation_deg == 0.0
        assert p.row("C1").value == "100µF"

    def test_units_origin_and_negate_x(self):
        text = 'Ref,Val,Package,PosX,PosY,Rot,Side\n"J1","V","P",-1.000000,2.000000,0.0,bottom\n'
        p = position.parse_pos_csv(text, units="in", aux_origin_mm=(100.0, 50.0), bottom_negate_x=True)
        j1 = p.row("J1")
        assert j1.x_raw == pytest.approx(-25.4) and j1.y_raw == pytest.approx(50.8)
        assert j1.x_mm == pytest.approx(100 + 25.4) and j1.y_mm == pytest.approx(50 - 50.8)

    def test_ascii(self):
        text = (
            "### Footprint positions - created on 2026-09-19 ###\n"
            "## Unit = in, Angle = deg.\n"
            "## Side : top\n"
            "# Ref  Val  Package  PosX  PosY  Rot  Side\n"
            'R1  "10 k"  R_0603  1.0000  -2.0000  90.0000  top\n'
            "## End\n"
        )
        p = position.parse_pos(text)
        assert p.format == "ascii" and p.units == "in"
        r1 = p.row("R1")
        assert r1.value == "10 k" and r1.x_mm == pytest.approx(25.4) and r1.y_mm == pytest.approx(50.8)


# --------------------------------------------------------------------------------------------
# IPC-2581
# --------------------------------------------------------------------------------------------


class TestIpc2581:
    @pytest.mark.parametrize("name", ["ipc2581_ecc83_nightly.xml", "ipc2581_ecc83_8.0.xml"])
    def test_ecc83_components_pads_nets(self, name):
        b = ipc2581.parse_ipc2581(fixture(name), source_name=name)
        assert b.source == name and b.name == "ecc83-pp" and b.units == "mm"
        assert b.kicad_version in ("10.99.0", "8.0.9")
        assert b.copper_layers == ("top_cu", "bottom_cu")
        assert [layer.function for layer in b.layers if layer.name == "Edge.Cuts"] == ["BOARD_OUTLINE"]
        # outline: the Edge.Cuts rectangle in board coordinates (y down)
        assert b.bbox == (ipc2581.Point(121.285, 90.17), ipc2581.Point(173.355, 136.525))
        assert len(b.outline) == 1 and b.outline[0][0] == ipc2581.Point(173.355, 136.525)
        c1 = b.component("C1")
        assert (c1.x_mm, c1.y_mm, c1.rotation_deg, c1.side, c1.mount_type) == (
            141.605,
            99.695,
            90.0,
            "top",
            "THMT",
        )
        assert c1.value == "10uF" and c1.populate and not c1.mirror and c1.package.startswith("CP_Radial")
        assert b.component("R1").rotation_deg == 270.0 and b.component("R1").value == "1.5K"
        pads = b.pads_of("C1")
        assert {p.pin for p in pads} == {"1", "2"}
        p2 = next(p for p in pads if p.pin == "2")
        assert (p2.net, p2.x_mm, p2.y_mm, p2.through_hole) == ("GND", 141.605, 94.695, True)
        assert p2.layers == ("top_cu", "bottom_cu") and p2.primitive == "CIRCLE_2"
        gnd = b.net("GND")
        assert ("C1", "2") in gnd.pins and gnd.layers == ("bottom_cu", "top_cu")
        # tracks: two kept per net on bottom_cu, width 0.8 mm
        t = b.net("Net-(P1-PM)")
        assert t.track_count == 2 and t.track_length_mm > 0 and b.tracks[0].width_mm == pytest.approx(0.8)
        assert b.tracks[0].layer == "bottom_cu" and b.tracks[0].length_mm == pytest.approx(4.9746, abs=1e-3)
        holes = [h for h in b.holes if h.padstack == "PADSTACK_2"]
        assert holes and holes[0].plating == "PLATED" and holes[0].diameter_mm == pytest.approx(1.0)
        ps = b.padstacks["PADSTACK_2"]
        assert ps.hole_diameter_mm == pytest.approx(1.0) and ps.plated and "top_cu" in ps.layers
        pkg = b.packages[c1.package]
        assert [pin.number for pin in pkg.pins] == ["1", "2"] and pkg.pins[1].x_mm == pytest.approx(5.0)
        assert len(pkg.outline) == 5

    def test_pic_bottom_component_vias_tracks(self):
        b = ipc2581.parse_ipc2581(fixture("ipc2581_pic_nightly.xml"))
        assert b.name == "pic_programmer" and b.copper_layers == ("top_layer", "bottom_layer")
        jp1 = b.component("JP1")
        # `(at 148.082 97.79)` on B.Cu with orientation 0: the exporter writes rotation 180 + mirror
        assert (
            jp1.side == "bottom" and jp1.mirror and jp1.ipc_rotation_deg == 180.0 and jp1.rotation_deg == 0.0
        )
        assert (jp1.x_mm, jp1.y_mm) == (148.082, 97.79) and jp1.mount_type == "OTHER" and not jp1.populate
        u1 = b.component("U1")
        assert u1.side == "top" and u1.rotation_deg == 0.0 and u1.value == "24Cxx"
        assert len(b.pads_of("U1")) == 8 and b.pads_of("JP1")[0].layers == ("bottom_layer",)
        assert not b.pads_of("JP1")[0].through_hole
        clock = b.net("/CLOCK-RB6")
        assert clock.via_count == 1 and ("U1", "6") in clock.pins and clock.track_count == 4
        via = b.vias[0]
        assert via.net == "/CLOCK-RB6" and via.layers == ("top_layer", "bottom_layer")
        assert (via.x_mm, via.y_mm) == (189.865, 110.49)
        assert all(t.width_mm == pytest.approx(0.5) for t in b.tracks if t.net == "/CLOCK-RB6")
        assert b.stackup_thickness_mm == pytest.approx(1.6)

    def test_kicad_rotation_inversion(self):
        f = ipc2581._kicad_rotation
        assert f(90.0, False) == 90.0 and f(180.0, True) == 0.0 and f(90.0, True) == 90.0
        assert f(0.0, True) == 180.0 and f(45.0, True) == 135.0

    def test_prefix_stripping(self):
        s = ipc2581._strip
        assert s("CMP:C1") == "C1" and s("PIN:2") == "2" and s("NET:/CLOCK") == "/CLOCK"
        assert s("Net-(P1-PM)") == "Net-(P1-PM)" and s(None) == ""
