"""Unit tests for the DSN writer, the rules reader and the DSN comparer (no KiCad needed).

Oracles that run offline: the DSN files KiCad 10.0.x's own `ExportSpecctraDSN` wrote for the
boards under `spikes/specctra-verify` and `spikes/autorouting/swigwd` (docs/VERIFICATION.md
claim 7), and hand-built boards whose expected padstacks follow `specctra_export.cpp` verbatim.
The live comparison against the SWIG export of the demo boards is conformance test T7.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kiwrap.formats.board import BoardFile
from kiwrap.formats.geometry import Vec2
from kiwrap.formats.specctra import (
    IGNORE_CLASS,
    DsnRules,
    DsnWriter,
    NetclassRule,
    SpecctraError,
    compare_dsn,
    parse_dsn,
    rules_from_project,
    via_padstack_id,
    write_dsn,
)
from kiwrap.formats.specctra.dsn_reader import polygon_distance
from kiwrap.formats.specctra.dsn_writer import _roundrect_polygon, kicad_arc_polyline
from kiwrap.formats.specctra.format import g6, g10, place_pt, pt, quote_id, um
from kiwrap.formats.specctra.rules import netclass_pattern_matches

REPO = Path(__file__).resolve().parent.parent
SPIKES = REPO / "spikes"
FIXTURES = REPO / "tests" / "fixtures" / "specctra"
TEST_LOCKED = SPIKES / "specctra-verify" / "test_locked.kicad_pcb"
TEST_LOCKED_DSN = SPIKES / "specctra-verify" / "out_10.0.dsn"
TINY2 = SPIKES / "autorouting" / "swigwd" / "tiny2.kicad_pcb"
TINY2_DSN = SPIKES / "autorouting" / "swigwd" / "tiny2.dsn"
BLINKER = FIXTURES / "blinker_10" / "blinky.kicad_pcb"

HEADER = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
{layers}
\t\t(25 "Edge.Cuts" user)
\t\t(39 "F.Mask" user)
\t\t(38 "B.Mask" user)
\t\t(35 "F.Paste" user)
\t\t(37 "F.SilkS" user "F.Silkscreen")
\t\t(47 "F.CrtYd" user "F.Courtyard")
\t\t(46 "B.CrtYd" user "B.Courtyard")
\t)
\t(setup (pad_to_mask_clearance 0))
"""
TWO_LAYERS = '\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)'
FOUR_LAYERS = '\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" power)\n\t\t(6 "In2.Cu" mixed)\n\t\t(2 "B.Cu" signal)'


def board_text(body: str, layers: str = TWO_LAYERS, outline: str | None = None) -> str:
    if outline is None:
        outline = (
            "\t(gr_rect (start 0 0) (end 40 30) (stroke (width 0.05) (type default)) (fill no)"
            ' (layer "Edge.Cuts") (uuid "e0"))\n'
        )
    return HEADER.format(layers=layers) + outline + body + ")\n"


def fp(ref: str, fpid: str, x: float, y: float, rot: float, layer: str, pads: str, extra: str = "") -> str:
    return (
        f'\t(footprint "{fpid}" (layer "{layer}") (uuid "u-{ref}") (at {x} {y} {rot})\n'
        f'\t\t(property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") (uuid "r-{ref}") (effects (font (size 1 1))))\n'
        f'\t\t(property "Value" "{ref}val" (at 0 2 0) (layer "F.Fab") (uuid "v-{ref}") (effects (font (size 1 1))))\n'
        f"{extra}{pads}\t)\n"
    )


def load_board(tmp_path: Path, text: str, pro: dict | None = None, name: str = "t.kicad_pcb") -> BoardFile:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    if pro is not None:
        path.with_suffix(".kicad_pro").write_text(json.dumps(pro), encoding="utf-8")
    return BoardFile.load(path)


# ------------------------------------------------------------------------------------------------
# formatting helpers
# ------------------------------------------------------------------------------------------------


def test_g6_matches_c_printf_conventions():
    assert g6(200.0) == "200"
    assert g6(1234.5678) == "1234.57"
    assert g6(-0.0) == "0"
    assert g6(0.0000001) == "1e-07"
    assert g6(1500000.0) == "1.5e+06"
    assert pt(21.5, 20.0) == "21500 -20000"
    assert pt(0.0, 0.0) == "0 0"


def test_quote_id_follows_kicad_get_quote_char():
    assert quote_id("R1") == "R1"
    assert quote_id("-R1") == "-R1"  # a leading dash needs no quotes
    assert quote_id("Net-(R1-Pad1)") == '"Net-(R1-Pad1)"'
    assert quote_id("Via[0-1]_600:300_um") == '"Via[0-1]_600:300_um"'
    assert quote_id("a b") == '"a b"'
    assert quote_id("#PWR") == '"#PWR"'
    assert quote_id("") == '""'
    assert quote_id("50%") == '"50%"'
    assert quote_id("{x}") == '"{x}"'
    assert quote_id("TQFP-32") == '"TQFP-32"'


# ------------------------------------------------------------------------------------------------
# offline oracles: KiCad's own export of the spike boards
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("board_path, dsn_path", [(TEST_LOCKED, TEST_LOCKED_DSN), (TINY2, TINY2_DSN)])
def test_codec_dsn_is_semantically_equal_to_kicads_export(board_path: Path, dsn_path: Path):
    ours = parse_dsn(write_dsn(BoardFile.load(board_path)))
    theirs = parse_dsn(dsn_path.read_text(encoding="utf-8"))
    diff = compare_dsn(ours, theirs, tol_um=5.0)
    assert diff.ok, diff.differences
    assert not diff.warnings
    assert diff.checked["placements"] > 0 and diff.checked["pins"] > 0


def test_locked_items_are_fix_and_bare_locked_list_is_honoured():
    doc = parse_dsn(write_dsn(BoardFile.load(TEST_LOCKED)))
    fixed = [w for w in doc.wires if w.type == "fix"]
    assert len(fixed) == 1 and fixed[0].net == "NET1"
    assert [v.type for v in doc.vias] == ["route", "fix"]
    assert doc.via_ids == ["Via[0-1]_600:300_um", "Via[0-1]_800:400_um"]


def test_roundrect_polygon_matches_kicads_vertices_for_the_0805_pad():
    """`RoundRect[T]Pad_1025x1400_250.951_um` in tiny2.dsn: 21 vertices, KiCad's numbers."""
    ring = _roundrect_polygon(1.025, 1.4, 1.025 * 0.243902, 0.005)
    assert len(ring) == 21 and ring[0] == ring[-1]
    ref = parse_dsn(TINY2_DSN.read_text(encoding="utf-8"))
    ps = ref.padstacks["RoundRect[T]Pad_1025.000000x1400.000000_250.951000_um_0.000000_0"]
    theirs = ps.shapes[0].points
    ours = [(p.x * 1000, -p.y * 1000) for p in ring]
    assert polygon_distance(ours, theirs) < 0.05  # 50 nm
    assert len(theirs) == len(ours)


def test_kicad_arc_polyline_straddles_the_arc_and_keeps_the_ends():
    start, mid, end = Vec2(10, 0), Vec2(10 * math.cos(math.pi / 4), 10 * math.sin(math.pi / 4)), Vec2(0, 10)
    pts = kicad_arc_polyline(start, mid, end, 0.005)
    assert pts[0] == start and pts[-1] == end
    radii = [math.hypot(p.x, p.y) for p in pts[1:-1]]
    assert all(10.0 < r < 10.005 for r in radii)  # r + effectiveError/2, outside the arc
    # GetArcToSegmentCount(10 mm, 5 um, 90 deg) = 25 interior points at the odd half-steps
    assert len(pts) == 27


# ------------------------------------------------------------------------------------------------
# pads, padstacks, images
# ------------------------------------------------------------------------------------------------


def _pad(
    num: str, kind: str, shape: str, x: float, y: float, rot: float, size: str, layers: str, extra: str = ""
) -> str:
    return f'\t\t(pad "{num}" {kind} {shape} (at {x} {y} {rot}) (size {size}) (layers {layers}) {extra}(uuid "p-{num}"))\n'


def test_every_pad_shape_gets_kicads_padstack(tmp_path: Path):
    pads = (
        _pad("1", "thru_hole", "circle", 0, 0, 0, "1.6 1.6", '"*.Cu" "*.Mask"', "(drill 0.8) ")
        + _pad("2", "smd", "rect", 2, 0, 0, "1.2 0.8", '"F.Cu" "F.Mask"')
        + _pad("3", "thru_hole", "oval", 4, 0, 0, "1.2 2", '"*.Cu"', "(drill 0.6) ")
        + _pad("4", "smd", "trapezoid", 6, 0, 0, "2 1", '"F.Cu"', "(rect_delta 0.4 0) ")
        + _pad("5", "smd", "roundrect", 8, 0, 0, "1 1", '"F.Cu"', "(roundrect_rratio 0.25) ")
        + _pad("6", "thru_hole", "circle", 10, 0, 0, "1 1", '"*.Cu"', "(drill 0.5 (offset 0.1 -0.2)) ")
        + _pad("7", "np_thru_hole", "circle", 12, 0, 0, "1.5 1.5", '"*.Cu" "*.Mask"', "(drill 1.5) ")
        + _pad("8", "smd", "rect", 14, 0, 0, "1 1", '"F.Paste"')  # copper-less: skipped
        + _pad("", "smd", "rect", 16, 0, 0, "0.5 0.5", '"F.Cu"')  # empty number -> @1
        + _pad("2", "smd", "rect", 18, 0, 0, "1.2 0.8", '"F.Cu"')  # duplicate number -> 2@1
        + _pad("9", "smd", "rect", 20, 0, 45, "1 0.5", '"B.Cu"')  # back copper of a front part
    )
    board = load_board(tmp_path, board_text(fp("U1", "Lib:Shapes", 10, 10, 0, "F.Cu", pads)))
    w = DsnWriter(board, rules=DsnRules.kicad_defaults())
    doc = parse_dsn(w.write())
    image = doc.image_of("U1")
    assert image is not None
    by_pin = {p.pin_id: p for p in image.pins}
    assert set(by_pin) == {"1", "2", "3", "4", "5", "6", "@1", "2@1", "9"}
    assert by_pin["1"].padstack == "Round[A]Pad_1600.000000_um"
    assert by_pin["2"].padstack == "Rect[T]Pad_1200.000000x800.000000_um"
    assert by_pin["3"].padstack == "Oval[A]Pad_1200.000000x2000.000000_um"
    assert by_pin["4"].padstack == "Trapz[T]Pad_2000.000000x1000.000000_p400.000000xp0.000000_um"
    assert by_pin["5"].padstack == "RoundRect[T]Pad_1000.000000x1000.000000_250.951000_um_0.000000_0"
    assert by_pin["6"].padstack == "Round[A][100.000000,200.000000]Pad_1000.000000_um"
    assert by_pin["9"].padstack == "Rect[B]Pad_1000.000000x500.000000_um"
    assert by_pin["9"].rotation == 45.0 and by_pin["2"].rotation == 0.0
    # positions: footprint-relative, y flipped
    assert (by_pin["3"].x, by_pin["3"].y) == (4000.0, 0.0)
    # NPTH becomes a round keepout of drill + 2 * hole clearance on every layer
    assert [k.kind for k in image.keepouts] == ["keepout", "keepout"]
    circle = image.keepouts[0].shape
    assert circle is not None and circle.kind == "circle"
    assert circle.values == (1500.0 + 2 * 250.0, 12000.0, 0.0)
    # shapes
    oval = doc.padstacks[by_pin["3"].padstack].shapes
    assert [s.layer for s in oval] == ["F.Cu", "B.Cu"]
    assert (
        oval[0].kind == "path" and oval[0].width == 1200.0 and oval[0].points == [(0.0, -400.0), (0.0, 400.0)]
    )
    rect = doc.padstacks[by_pin["2"].padstack].shapes[0]
    assert rect.kind == "rect" and rect.values == (-600.0, -400.0, 600.0, 400.0)
    circ = doc.padstacks[by_pin["6"].padstack].shapes[0]
    assert circ.values == (1000.0, 100.0, 200.0)
    trapz = doc.padstacks[by_pin["4"].padstack].shapes[0]
    assert trapz.kind == "polygon" and len(trapz.points) == 4
    assert doc.padstacks[by_pin["5"].padstack].shapes[0].kind == "polygon"
    assert "unsupported" not in " ".join(w.warnings)


def test_custom_pad_is_the_convex_hull_of_stroked_primitives(tmp_path: Path):
    pad = (
        '\t\t(pad "1" smd custom (at 0 0) (size 0.5 0.5) (layers "F.Cu")\n'
        "\t\t\t(options (clearance outline) (anchor circle))\n"
        "\t\t\t(primitives (gr_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) (width 0.2) (fill yes)))\n"
        '\t\t\t(uuid "p1"))\n'
    )
    board = load_board(tmp_path, board_text(fp("U1", "Lib:Cust", 5, 5, 0, "F.Cu", pad)))
    doc = parse_dsn(write_dsn(board, rules=DsnRules.kicad_defaults()))
    pin = doc.image_of("U1").pins[0]
    assert pin.padstack.startswith("Cust[T]Pad_500.000000x500.000000_2200.000000x2200.000000_")
    poly = doc.padstacks[pin.padstack].shapes[0]
    xs = [p[0] for p in poly.points]
    assert max(xs) == pytest.approx(1100.0, abs=0.5) and min(xs) == pytest.approx(-1100.0, abs=0.5)
    assert poly.points[0] == poly.points[-1]


def test_back_side_footprint_is_flipped_to_the_top_view(tmp_path: Path):
    # KiCad stores a back-side footprint with mirrored local coordinates; pad 2 sits at local (1.5, 0.5)
    pads = _pad("1", "smd", "rect", -1.5, 0.5, 90, "1 2", '"B.Cu" "B.Mask"') + _pad(
        "2", "smd", "rect", 1.5, 0.5, 90, "1 2", '"B.Cu" "B.Mask"'
    )
    text = board_text(
        fp("R1", "Lib:R", 20, 10, 90, "B.Cu", pads)
        + fp("R2", "Lib:R", 30, 10, 0, "F.Cu", pads.replace("B.", "F."))
    )
    board = load_board(tmp_path, text)
    doc = parse_dsn(write_dsn(board, rules=DsnRules.kicad_defaults()))
    r1 = {p.ref: p for p in doc.placements}["R1"]
    assert r1.side == "back" and r1.rotation == 270.0  # (180 + 90) mod 360
    assert (r1.x, r1.y) == (20000.0, -10000.0)
    image = doc.image_of("R1")
    pins = {p.pin_id: p for p in image.pins}
    assert (pins["2"].x, pins["2"].y) == (1500.0, 500.0)  # y keeps its sign: local mirrored back
    assert pins["2"].rotation == 0.0  # fp_rot - pad_rot = 90 - 90
    assert pins["2"].padstack == "Rect[T]Pad_1000.000000x2000.000000_um"  # B.Cu -> top after the flip
    r2 = {p.ref: p for p in doc.placements}["R2"]
    assert r2.side == "front" and r2.rotation == 0.0
    front_pins = {p.pin_id: p for p in doc.image_of("R2").pins}
    assert (front_pins["2"].x, front_pins["2"].y) == (1500.0, -500.0)
    assert front_pins["2"].rotation == 90.0


def test_identical_footprints_share_an_image_and_different_content_gets_a_suffix(tmp_path: Path):
    pads = _pad("1", "smd", "rect", -1, 0, 0, "1 1", '"F.Cu"') + _pad(
        "2", "smd", "rect", 1, 0, 0, "1 1", '"F.Cu"'
    )
    other = _pad("1", "smd", "rect", -1.5, 0, 0, "1 1", '"F.Cu"') + _pad(
        "2", "smd", "rect", 1.5, 0, 0, "1 1", '"F.Cu"'
    )
    text = board_text(
        fp("R1", "Lib:R", 10, 10, 0, "F.Cu", pads)
        + fp(
            "R2", "Lib:R", 20, 10, 180, "F.Cu", pads.replace(" 0) (size", " 180) (size")
        )  # pad angles are absolute
        + fp("R3", "Lib:R", 30, 10, 0, "F.Cu", other)
        + fp("C1", "Lib:C", 10, 20, 0, "F.Cu", pads)  # same content under another id: KiCad folds it
    )
    doc = parse_dsn(write_dsn(load_board(tmp_path, text), rules=DsnRules.kicad_defaults()))
    images = {p.ref: p.image for p in doc.placements}
    assert images["R1"] == images["R2"] == images["C1"] == "Lib:R"
    assert images["R3"] == "Lib:R::1"
    assert set(doc.images) == {"Lib:R", "Lib:R::1"}


def test_place_rotation_is_normalised_like_kicad(tmp_path: Path):
    pads = _pad("1", "smd", "rect", 0, 0, 270, "1 2", '"F.Cu"')
    text = board_text(
        fp("R1", "Lib:R", 10, 10, 270, "F.Cu", pads) + fp("R2", "Lib:R", 20, 10, -90, "B.Cu", pads)
    )
    doc = parse_dsn(write_dsn(load_board(tmp_path, text), rules=DsnRules.kicad_defaults()))
    rot = {p.ref: p.rotation for p in doc.placements}
    assert rot["R1"] == -90.0  # Normalize180
    assert rot["R2"] == 90.0  # (180 + (-90)) mod 360


def test_duplicate_and_empty_references_are_uniquified(tmp_path: Path):
    pads = _pad("1", "smd", "rect", 0, 0, 0, "1 1", '"F.Cu"')
    text = board_text(
        fp("R1", "Lib:R", 10, 10, 0, "F.Cu", pads)
        + fp("r1", "Lib:R", 20, 10, 0, "F.Cu", pads)
        + fp("", "Lib:R", 30, 10, 0, "F.Cu", pads).replace('(uuid "u-")', '(uuid "u-a")')
        + fp("", "Lib:R", 30, 20, 0, "F.Cu", pads).replace('(uuid "u-")', '(uuid "u-b")')
    )
    doc = parse_dsn(write_dsn(load_board(tmp_path, text), rules=DsnRules.kicad_defaults()))
    assert [p.ref for p in doc.placements] == ["R1", "r1_1", "REF**", "REF**_1"]


# ------------------------------------------------------------------------------------------------
# structure: layers, boundary, planes, keepouts, rules
# ------------------------------------------------------------------------------------------------


def test_layers_in_stack_order_with_user_names_types_and_directions(tmp_path: Path):
    layers = FOUR_LAYERS.replace('(4 "In1.Cu" power)', '(4 "In1.Cu" power "GND_PLANE")')
    board = load_board(tmp_path, board_text("", layers=layers))
    w = DsnWriter(
        board, rules=DsnRules.kicad_defaults(), layer_directions={"F.Cu": "horizontal", "B.Cu": "vertical"}
    )
    text = w.write()
    doc = parse_dsn(text)
    assert [(la.id, la.type, la.index) for la in doc.layers] == [
        ("F.Cu", "signal", 0),
        ("GND_PLANE", "power", 1),
        ("In2.Cu", "signal", 2),  # mixed -> signal (Freerouting knows only signal/power)
        ("B.Cu", "signal", 3),
    ]
    assert "(direction horizontal)" in text and "(direction vertical)" in text
    assert doc.via_ids == ["Via[0-3]_600:300_um"]
    with pytest.raises(SpecctraError):
        DsnWriter(board, rules=DsnRules.kicad_defaults(), layers=["F.Cu", "In9.Cu"])
    sub = parse_dsn(DsnWriter(board, rules=DsnRules.kicad_defaults(), layers=["B.Cu", "F.Cu"]).write())
    assert [la.id for la in sub.layers] == ["F.Cu", "B.Cu"]


def test_boundary_cutouts_planes_and_rule_areas(tmp_path: Path):
    outline = (
        '\t(gr_rect (start 0 0) (end 40 30) (stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts") (uuid "e0"))\n'
        '\t(gr_circle (center 10 10) (end 12 10) (stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts") (uuid "e1"))\n'
    )
    body = (
        '\t(zone (net "GND") (layers "F.Cu" "B.Cu") (uuid "z1") (name "pour") (hatch edge 0.5)\n'
        "\t\t(connect_pads (clearance 0.3)) (min_thickness 0.25) (fill yes)\n"
        "\t\t(polygon (pts (xy 1 1) (xy 39 1) (xy 39 29) (xy 1 29)))\n"
        "\t\t(polygon (pts (xy 20 20) (xy 25 20) (xy 25 25) (xy 20 25))))\n"
        '\t(zone (layers "F.Cu") (uuid "z2") (name "noroute") (hatch edge 0.5)\n'
        "\t\t(keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))\n"
        "\t\t(polygon (pts (xy 30 5) (xy 35 5) (xy 35 8) (xy 30 8))))\n"
        '\t(zone (layers "F.Cu" "B.Cu") (uuid "z3") (hatch edge 0.5)\n'
        "\t\t(keepout (tracks allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))\n"
        "\t\t(polygon (pts (xy 30 15) (xy 35 15) (xy 35 18) (xy 30 18))))\n"
        '\t(zone (layers "F.Cu") (uuid "z4") (hatch edge 0.5) (connect_pads (clearance 0.3)) (min_thickness 0.25)\n'
        "\t\t(fill yes) (polygon (pts (xy 2 2) (xy 5 2) (xy 5 5) (xy 2 5))))\n"
    )
    board = load_board(tmp_path, board_text(body, outline=outline))
    text = write_dsn(board, rules=DsnRules.kicad_defaults())
    doc = parse_dsn(text)
    assert len(doc.boundary) == 1 and doc.boundary[0].layer == "pcb"
    assert polygon_distance(doc.boundary[0].points, [(0, 0), (40000, 0), (40000, -30000), (0, -30000)]) < 1e-6
    cutouts = [k for k in doc.keepouts if k.shape is not None and k.shape.layer == "signal"]
    assert len(cutouts) == 1 and cutouts[0].kind == "keepout"
    radii = [math.hypot(x - 10000, y + 10000) for x, y in cutouts[0].shape.points]
    assert all(abs(r - 2000) < 5 for r in radii) and len(radii) > 16
    planes = {(p.name, p.shape.layer): p for p in doc.planes if p.shape is not None}
    assert set(planes) == {("GND", "F.Cu"), ("GND", "B.Cu"), ("@:no_net_0", "F.Cu")}
    assert len(planes[("GND", "F.Cu")].windows) == 1
    assert (
        polygon_distance(
            planes[("GND", "F.Cu")].windows[0].points,
            [(20000, -20000), (25000, -20000), (25000, -25000), (20000, -25000)],
        )
        < 1e-6
    )
    rule_areas = [
        (k.kind, k.shape.layer) for k in doc.keepouts if k.shape is not None and k.shape.layer != "signal"
    ]
    assert sorted(rule_areas) == [("via_keepout", "B.Cu"), ("via_keepout", "F.Cu"), ("wire_keepout", "F.Cu")]
    assert [n.name for n in doc.nets] == ["@:no_net_0"]  # the netless plane's bogus net
    assert doc.rules == {"width": 200.0, "clearance": 200.0, "clearance:smd_smd": 50.0}


def test_missing_outline_falls_back_to_a_bounding_box_with_a_warning(tmp_path: Path):
    pads = _pad("1", "smd", "rect", 0, 0, 0, "1 1", '"F.Cu"')
    board = load_board(tmp_path, board_text(fp("R1", "Lib:R", 10, 10, 0, "F.Cu", pads), outline=""))
    w = DsnWriter(board, rules=DsnRules.kicad_defaults())
    doc = parse_dsn(w.write())
    assert any("outline" in x for x in w.warnings)
    assert len(doc.boundary) == 1 and len(doc.boundary[0].points) == 5


# ------------------------------------------------------------------------------------------------
# network, classes, wiring
# ------------------------------------------------------------------------------------------------


def _two_parts(net_a: str = "NET1", net_b: str = "NET2") -> str:
    pads = (
        f'\t\t(pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "{net_a}") (uuid "pa"))\n'
        f'\t\t(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "{net_b}") (uuid "pb"))\n'
    )
    return fp("R1", "Lib:R", 10, 10, 0, "F.Cu", pads) + fp(
        "R2", "Lib:R", 30, 10, 0, "F.Cu", pads.replace('"pa"', '"pc"').replace('"pb"', '"pd"')
    )


def _wiring() -> str:
    return (
        '\t(segment (start 11 10) (end 29 10) (width 0.25) (layer "F.Cu") (net "NET1") (uuid "s1"))\n'
        '\t(segment (start 29 10) (end 29 15) (width 0.25) (layer "F.Cu") (net "NET1") (uuid "s2"))\n'
        '\t(segment (start 11 10) (end 11 14) (width 0.4) (layer "B.Cu") (locked yes) (net "NET2") (uuid "s3"))\n'
        '\t(via (at 11 14) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "NET2") (uuid "v1"))\n'
        '\t(arc (start 20 20) (mid 21 21) (end 22 20) (width 0.25) (layer "F.Cu") (net "NET2") (uuid "a1"))\n'
    )


def test_network_pins_classes_and_wiring(tmp_path: Path):
    pro = {
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "priority": 2147483647,
                },
                {
                    "name": "Power",
                    "clearance": 0.3,
                    "track_width": 0.5,
                    "via_diameter": 0.8,
                    "via_drill": 0.4,
                    "priority": 0,
                },
            ],
            "netclass_patterns": [{"pattern": "NET2", "netclass": "Power"}],
            "meta": {"version": 5},
        }
    }
    for cls in pro["net_settings"]["classes"]:
        cls.update(FULL_CLASS_EXTRAS)  # a KiCad-written class defines every parameter: bare names
    board = load_board(tmp_path, board_text(_two_parts() + _wiring()), pro=pro)
    w = DsnWriter(board)
    doc = parse_dsn(w.write())
    assert {n.name: n.pins for n in doc.nets} == {"NET1": ("R1-1", "R2-1"), "NET2": ("R1-2", "R2-2")}
    classes = {c.name: c for c in doc.classes}
    assert set(classes) == {"kicad_default", "Power"}
    assert classes["kicad_default"].nets == ("NET1",) and classes["kicad_default"].width == 250.0
    assert classes["Power"].nets == ("NET2",) and classes["Power"].use_via == "Via[0-1]_800:400_um"
    assert classes["Power"].clearance == 300.0
    assert doc.via_ids == ["Via[0-1]_600:300_um", "Via[0-1]_800:400_um"]
    assert doc.rules["width"] == 250.0 and doc.rules["clearance:smd_smd"] == 50.0
    # consecutive NET1 segments chained into one path; the locked NET2 segment is fix; arc = chord
    wires = {(w_.net, w_.type, w_.layer): w_ for w_ in doc.wires}
    assert wires[("NET1", "route", "F.Cu")].points == (
        (11000.0, -10000.0),
        (29000.0, -10000.0),
        (29000.0, -15000.0),
    )
    assert wires[("NET2", "fix", "B.Cu")].width == 400.0
    chord = [w_ for w_ in doc.wires if w_.net == "NET2" and w_.layer == "F.Cu"]
    assert len(chord) == 1 and chord[0].points == ((20000.0, -20000.0), (22000.0, -20000.0))
    assert doc.vias == [type(doc.vias[0])("Via[0-1]_800:400_um", 11000.0, -14000.0, "NET2", "route")]
    poly = parse_dsn(DsnWriter(board, arcs="polyline").write())
    arc_wire = [w_ for w_ in poly.wires if w_.net == "NET2" and w_.layer == "F.Cu"][0]
    assert len(arc_wire.points) > 2


def test_net_subset_drops_other_nets_from_the_network_and_fixes_their_copper(tmp_path: Path):
    """Same semantic as `kiwrap.backends.router.dsn_post.restrict_nets` (Freerouting 2.4.1
    ignores `-inc`): the other nets are not in the network, their pins stay in the images as
    net-less obstacles, their wiring is fixed."""
    board = load_board(tmp_path, board_text(_two_parts() + _wiring()))
    doc = parse_dsn(DsnWriter(board, rules=DsnRules.kicad_defaults(), nets=["NET2"]).write())
    classes = {c.name: c.nets for c in doc.classes}
    assert classes == {"kicad_default": ("NET2",)} and IGNORE_CLASS not in classes
    assert {n.name for n in doc.nets} == {"NET2"}
    assert doc.image_of("R1") is not None and len(doc.image_of("R1").pins) == 2  # pads stay obstacles
    assert all(w_.type == "fix" for w_ in doc.wires if w_.net == "NET1")
    assert any(w_.type == "route" for w_ in doc.wires if w_.net == "NET2")


def test_um_goes_through_kicads_integer_nanometres():
    """Phase 1c finding: `mm * 1000.0` printed 128082.50000000001 as `128083` where KiCad
    (128082500 nm / 1000.0 = 128082.5, `%.6g`) prints `128082`: 1 um off on every half-micrometre
    coordinate at or above 100 mm (jetson / vme-wren wiring)."""
    assert um(128.0825) == 128082.5 and g6(um(128.0825)) == "128082"
    assert pt(128.0825, 146.551) == "128082 -146551"
    assert um(-0.0) == 0.0 and um(0.0004) == 0.4 and um(-1.23456789) == -1234.568
    # placements print ten significant digits (FormatDouble2Str), everything else six
    assert place_pt(142.480532, 10.0) == "142480.532 -10000" and pt(142.480532, 10.0) == "142481 -10000"
    assert g10(90.0) == "90" and g10(45.5) == "45.5" and g10(-0.0) == "0" and g10(0.00001) == "0.00001"


def test_chamfered_rect_pads_get_kicads_polygon_and_padstack_id(tmp_path: Path):
    """A `roundrect` pad whose `(chamfer …)` lists a corner is KiCad's CHAMFERED_RECT (the file
    format has no other spelling); the exporter cuts `chamfer_ratio * min(w, h)` off those corners
    on the inflated size (`doChamfer`), verified against SWIG 10.0.6 on the blinker (0 differences,
    identical ids) and the RoyalBlue54L demo (8 chamfered THT pads, front and back)."""
    pads = (
        _pad(
            "1",
            "smd",
            "roundrect",
            0,
            0,
            0,
            "0.8 0.95",
            '"F.Cu"',
            "(roundrect_rratio 0) (chamfer_ratio 0.2) (chamfer top_left bottom_right) ",
        )
        + _pad(
            "2",
            "smd",
            "roundrect",
            2,
            0,
            0,
            "0.8 0.95",
            '"F.Cu"',
            "(roundrect_rratio 0.1) (chamfer_ratio 0.25) (chamfer top_left top_right bottom_left bottom_right) ",
        )
        + _pad("3", "smd", "roundrect", 4, 0, 0, "0.8 0.95", '"F.Cu"', "(roundrect_rratio 0.25) (chamfer) ")
    )
    board = load_board(tmp_path, board_text(fp("U1", "Lib:Chamfer", 10, 10, 0, "F.Cu", pads)))
    shapes = {p.number: p.effective_shape for p in board.footprints[0].pads}
    assert shapes == {"1": "chamfered_rect", "2": "chamfered_rect", "3": "roundrect"}
    w = DsnWriter(board, rules=DsnRules.kicad_defaults())
    doc = parse_dsn(w.write())
    assert not w.warnings
    by_pin = {p.pin_id: p for p in doc.image_of("U1").pins}
    # ids as SWIG writes them: `_<radius>_um_<chamfer ratio %.6f>_<positions hex>`
    assert by_pin["1"].padstack == "RoundRect[T]Pad_800.000000x950.000000_0.000000_um_0.200000_9"
    assert by_pin["2"].padstack == "RoundRect[T]Pad_800.000000x950.000000_80.304000_um_0.250000_F"
    assert by_pin["3"].padstack == "RoundRect[T]Pad_800.000000x950.000000_200.761000_um_0.000000_0"
    # pad 1: two sharp corners cut by 0.2 * 800 = 160 um, the other two untouched (rratio 0)
    poly = doc.padstacks[by_pin["1"].padstack].shapes[0].points
    xs = sorted({round(x) for x, _ in poly})
    ys = sorted({round(y) for _, y in poly})
    assert xs == [-400, -240, 240, 400] and ys == [-475, -315, 315, 475]
    corners = {(round(x), round(y)) for x, y in poly}
    # KiCad's top_left is (-x, -y) in its y-down frame: (-400, 315) / (-240, 475) with the DSN y up
    assert {(-400, 315), (-240, 475), (400, -315), (240, -475)} <= corners  # TL + BR cut
    assert {(400, 475), (-400, -475)} <= corners  # TR + BL sharp
    # a back-side part mirrors the chamfer positions (PAD::Flip: TL <-> BL, TR <-> BR)
    back = load_board(
        tmp_path, board_text(fp("U1", "Lib:Chamfer", 10, 10, 0, "B.Cu", pads)), name="b.kicad_pcb"
    )
    bdoc = parse_dsn(DsnWriter(back, rules=DsnRules.kicad_defaults()).write())
    bpin = {p.pin_id: p for p in bdoc.image_of("U1").pins}["1"]
    assert bpin.padstack.endswith("_um_0.200000_6")


def test_back_side_footprint_rule_area_is_written_on_the_mirrored_layer(tmp_path: Path):
    """Phase 1c finding (jetson demo BT1): `FOOTPRINT::Flip` flips a zone's layer set, so a
    B.Cu-only keepout of a back part is `F.Cu` in the (top-view) image; the codec kept `B.Cu`."""
    zone = (
        '\t\t(zone (net 0) (net_name "") (layer "B.Cu") (uuid "z1") (hatch edge 0.5)\n'
        "\t\t\t(keepout (tracks allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))\n"
        "\t\t\t(polygon (pts (xy 11 11) (xy 13 11) (xy 13 12) (xy 11 12)))\n\t\t)\n"
    )
    pads = _pad("1", "smd", "rect", 0, 0, 0, "1 1", '"B.Cu"')
    board = load_board(tmp_path, board_text(fp("BT1", "Lib:Cell", 10, 10, 0, "B.Cu", pads, extra=zone)))
    doc = parse_dsn(DsnWriter(board, rules=DsnRules.kicad_defaults()).write())
    keepouts = doc.image_of("BT1").keepouts
    assert len(keepouts) == 1 and keepouts[0].kind == "via_keepout" and keepouts[0].shape.layer == "F.Cu"
    # image coordinates: board -> footprint-relative, y kept for a back part (top view)
    pts = {(round(x), round(y)) for x, y in keepouts[0].shape.points}
    assert pts == {(1000, 1000), (3000, 1000), (3000, 2000), (1000, 2000)}
    # the same zone on a FRONT part stays on its layer with the y flipped
    front = load_board(
        tmp_path, board_text(fp("BT1", "Lib:Cell", 10, 10, 0, "F.Cu", pads, extra=zone)), name="f.kicad_pcb"
    )
    fdoc = parse_dsn(DsnWriter(front, rules=DsnRules.kicad_defaults()).write())
    fk = fdoc.image_of("BT1").keepouts[0]
    assert fk.shape.layer == "B.Cu"
    assert {(round(x), round(y)) for x, y in fk.shape.points} == {
        (1000, -1000),
        (3000, -1000),
        (3000, -2000),
        (1000, -2000),
    }


def test_library_frame_footprint_rule_area_on_the_nightly_dialect(tmp_path: Path):
    """Phase 1c finding: boards saved by the 10.99 nightly (stamp >= 20260616) hold footprint
    zones in the LIBRARY frame; the codec applied the board -> image transform to library
    coordinates and put the keepout ~42 mm off. The library outline IS the image geometry."""
    zone = (
        '\t\t(zone (net 0) (net_name "") (layer "F.Cu") (uuid "z1") (hatch edge 0.5)\n'
        "\t\t\t(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))\n"
        "\t\t\t(polygon (pts (xy -1.5 -1) (xy 1.5 -1) (xy 1.5 -2) (xy -1.5 -2)))\n\t\t)\n"
    )
    pads = _pad("1", "smd", "rect", 0, 0, 0, "1 1", '"F.Cu"')
    text = board_text(fp("R1", "Lib:R", 37.3, 14.6, -90, "F.Cu", pads, extra=zone)).replace(
        "(version 20260206)", "(version 20260901)"
    )
    board = load_board(tmp_path, text)
    assert board.stamp == 20260901
    doc = parse_dsn(DsnWriter(board, rules=DsnRules.kicad_defaults()).write())
    ko = doc.image_of("R1").keepouts[0]
    assert ko.kind == "keepout" and ko.shape.layer == "F.Cu"
    assert {(round(x), round(y)) for x, y in ko.shape.points} == {
        (-1500, 1000),
        (1500, 1000),
        (1500, 2000),
        (-1500, 2000),
    }
    # the SAME geometry stored in the board frame on the <= 10 dialect (rotated -90 about
    # (37.3, 14.6)) gives the same image keepout: what 10.0.6's SWIG export writes for it
    legacy_zone = zone.replace(
        "(xy -1.5 -1) (xy 1.5 -1) (xy 1.5 -2) (xy -1.5 -2)",
        "(xy 38.3 13.1) (xy 39.3 13.1) (xy 39.3 16.1) (xy 38.3 16.1)",
    )
    legacy = load_board(
        tmp_path,
        board_text(fp("R1", "Lib:R", 37.3, 14.6, -90, "F.Cu", pads, extra=legacy_zone)),
        name="legacy.kicad_pcb",
    )
    ldoc = parse_dsn(DsnWriter(legacy, rules=DsnRules.kicad_defaults()).write())
    assert {(round(x), round(y)) for x, y in ldoc.image_of("R1").keepouts[0].shape.points} == {
        (-1500, 1000),
        (1500, 1000),
        (1500, 2000),
        (-1500, 2000),
    }


def test_net_names_with_quotes_are_folded(tmp_path: Path):
    board = load_board(tmp_path, board_text(_two_parts('a\\"b', "c d")))
    text = write_dsn(board, rules=DsnRules.kicad_defaults())
    assert "(net a''b" in text and '(net "c d"' in text
    doc = parse_dsn(text)
    assert {n.name for n in doc.nets} == {"a''b", "c d"}


def test_via_padstack_id_format():
    assert via_padstack_id(0.6, 0.3, 0, 1) == "Via[0-1]_600:300_um"
    assert via_padstack_id(0.45, 0.25, 0, 3) == "Via[0-3]_450:250_um"


def test_blinker_fixture_writes_a_dsn_the_reader_round_trips():
    board = BoardFile.load(BLINKER)
    w = DsnWriter(board)
    doc = parse_dsn(w.write())
    assert len(doc.placements) == 11 and len(doc.nets) == 8
    assert [la.id for la in doc.layers] == ["F.Cu", "B.Cu"]
    assert {p.name for p in doc.planes} == {"GND"}
    # the project's net_settings.meta.version is 0: KiCad's migration wipes netclass_patterns
    assert [c.name for c in doc.classes] == ["kicad_default"]
    assert not w.warnings


# ------------------------------------------------------------------------------------------------
# rules
# ------------------------------------------------------------------------------------------------


#: the parameters a KiCad-written netclass always carries besides the four the DSN uses; a class
#: missing any of them exports as `<name>,Default` (NET_SETTINGS::addMissingDefaults)
FULL_CLASS_EXTRAS = {
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "diff_pair_width": 0.2,
    "diff_pair_gap": 0.25,
    "wire_width": 6,
    "bus_width": 12,
}


def test_rules_from_project_resolves_patterns_assignments_and_priorities(tmp_path: Path):
    pro = {
        "board": {
            "design_settings": {
                "rules": {"min_hole_clearance": 0.3, "min_copper_edge_clearance": 0.4, "max_error": 0.01}
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.15,
                    "track_width": 0.2,
                    "via_diameter": 0.5,
                    "via_drill": 0.25,
                },
                {"name": "Power", "clearance": 0.3, "track_width": 0.5, "priority": 1},
                {"name": "Fast", "clearance": 0.1, "track_width": 0.15, "priority": 0},
            ],
            "netclass_patterns": [
                {"pattern": "/PWR*", "netclass": "Power"},
                {"pattern": "^/CLK[0-9]$", "netclass": "Fast"},
            ],
            "netclass_assignments": {"/SPECIAL": ["Fast"], "/BOTH": ["Power", "Fast"]},
            "meta": {"version": 5},
        },
    }
    rules = rules_from_project(
        None, pro, net_names=["/PWR1", "/CLK1", "/CLK10", "/SPECIAL", "/BOTH", "/other", ""]
    )
    # Power and Fast leave parameters to Default, so KiCad's effective netclass is `<name>,Default`;
    # a net in two classes gets both, highest priority (lowest value) first: `Fast,Power,Default`
    assert rules.net_class == {
        "/PWR1": "Power,Default",
        "/CLK1": "Fast,Default",
        "/SPECIAL": "Fast,Default",
        "/BOTH": "Fast,Power,Default",
    }
    assert rules.class_of("/other").is_default and rules.class_of("/PWR1").via_diameter == 0.5  # inherited
    assert rules.class_of("/PWR1").base_name == "Power" and rules.class_of("/PWR1").clearance == 0.3
    both = rules.class_of("/BOTH")
    assert both.constituents == ("Fast", "Power", "Default") and both.clearance == 0.1  # Fast wins
    assert rules.hole_clearance == 0.3 and rules.copper_edge_clearance == 0.4 and rules.max_error == 0.01
    assert rules.by_name("Fast") is not None and rules.by_name("Nope") is None
    # a class that defines every parameter keeps its bare name (the jetson demo's six diff-pair
    # classes set only diff-pair values and export as `85Ohm-diff_DP,Default`; VERIFICATION claim 7)
    for cls in pro["net_settings"]["classes"]:
        if cls["name"] == "Power":
            cls.update({"via_diameter": 0.8, "via_drill": 0.4, **FULL_CLASS_EXTRAS})
    full = rules_from_project(None, pro, net_names=["/PWR1", "/BOTH"])
    assert full.net_class == {"/PWR1": "Power", "/BOTH": "Fast,Power"}
    assert full.class_of("/BOTH").via_diameter == 0.8  # Fast has none, Power supplies it
    # a label-assigned class the project never configured is an implicit netclass (no parameters)
    pro["net_settings"]["netclass_assignments"]["/LBL"] = ["Ghost"]
    ghost = rules_from_project(None, pro, net_names=["/LBL"])
    assert ghost.net_class == {"/LBL": "Ghost,Default"} and ghost.class_of("/LBL").clearance == 0.15


def test_rules_replay_kicads_net_settings_migrations():
    classes = [
        {"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "Power", "clearance": 0.3, "track_width": 0.5, "via_diameter": 0.6, "via_drill": 0.3},
    ]
    patterns = [{"pattern": "GND", "netclass": "Power"}]
    # version 0 (what the Phase 1b project template writes): migrateSchema2to3 wipes the patterns
    old = {"net_settings": {"classes": classes, "netclass_patterns": patterns, "meta": {"version": 0}}}
    assert rules_from_project(None, old, net_names=["GND"]).net_class == {}
    # legacy `nets` lists become patterns instead
    legacy = {
        "net_settings": {
            "classes": [classes[0], dict(classes[1], nets=["GND", "VCC"])],
            "meta": {"version": 2},
        }
    }
    assert rules_from_project(None, legacy, net_names=["GND", "VCC", "X"]).net_class == {
        "GND": "Power,Default",
        "VCC": "Power,Default",
    }
    # no meta at all: no migration, the patterns count
    fresh = {"net_settings": {"classes": classes, "netclass_patterns": patterns}}
    assert rules_from_project(None, fresh, net_names=["GND"]).net_class == {"GND": "Power,Default"}
    # version 3: priorities in file order (first non-default class wins)
    v3 = {
        "net_settings": {
            "classes": classes + [{"name": "Other", "clearance": 0.4, "track_width": 0.4}],
            "netclass_patterns": [
                {"pattern": "GND", "netclass": "Other"},
                {"pattern": "GND", "netclass": "Power"},
            ],
            "meta": {"version": 3},
        }
    }
    assert rules_from_project(None, v3, net_names=["GND"]).net_class == {"GND": "Power,Other,Default"}
    assert rules_from_project(None, v3, net_names=["GND"]).class_of("GND").clearance == 0.3  # Power wins


def test_netclass_pattern_matcher_is_anchored_wildcard_or_regex():
    assert netclass_pattern_matches("GND", "GND")
    assert not netclass_pattern_matches("GND", "GNDA")
    assert netclass_pattern_matches("/PWR*", "/PWR_3V3")
    assert netclass_pattern_matches("D?", "D1") and not netclass_pattern_matches("D?", "D10")
    assert netclass_pattern_matches("^/CLK[0-9]+$", "/CLK12")
    assert netclass_pattern_matches("Net-(R1-Pad1)", "Net-(R1-Pad1)")  # parens literal in wildcard mode


def test_defaults_without_a_project_file(tmp_path: Path):
    board = load_board(tmp_path, board_text(_two_parts()))
    rules = rules_from_project(board)
    assert rules.default == NetclassRule("Default", 0.2, 0.2, 0.6, 0.3)
    assert rules.classes == () and rules.net_class == {}


# ------------------------------------------------------------------------------------------------
# comparer
# ------------------------------------------------------------------------------------------------


def test_compare_dsn_detects_semantic_changes(tmp_path: Path):
    base = board_text(_two_parts() + _wiring())
    ref = parse_dsn(write_dsn(load_board(tmp_path, base), rules=DsnRules.kicad_defaults()))
    moved = parse_dsn(
        write_dsn(
            load_board(tmp_path, base.replace("(at 30 10 0)", "(at 30.01 10 0)")),
            rules=DsnRules.kicad_defaults(),
        )
    )
    d = compare_dsn(moved, ref)
    assert any(x.startswith("R2: position") for x in d.differences)
    renet = parse_dsn(
        write_dsn(
            load_board(tmp_path, base.replace('(net "NET1") (uuid "pc")', '(net "NET2") (uuid "pc")')),
            rules=DsnRules.kicad_defaults(),
        )
    )
    d = compare_dsn(renet, ref)
    assert any(x.startswith("net 'NET1'") for x in d.differences)
    same = compare_dsn(ref, parse_dsn(write_dsn(load_board(tmp_path, base), rules=DsnRules.kicad_defaults())))
    assert same.ok and same.summary().startswith("0 differences")
    assert (
        polygon_distance([(0, 0), (10, 0), (10, 10), (0, 10)], [(10, 10), (0, 10), (0, 0), (5, 0), (10, 0)])
        == 0.0
    )
