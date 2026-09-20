"""Unit tests for `kiwrap.formats.board_writer` and `kiwrap.formats.dru` (no KiCad needed).

The KiCad oracle runs (parity DRC, pos export, zone refill, upgrade re-save) live in
`tests/test_board_writer_live.py` and `conformance/tests/t8_board_writer.py`; what is checked
here is the CST shape the writer produces in each dialect and that edits stay local.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import (
    BoardWriter,
    BoardWriterError,
    MinimalFootprintResolver,
    dialect_for,
    flip_footprint,
    flip_layer_name,
    footprint_placement,
    read_fp_lib_table,
)
from kiwrap.formats.dru import DruRule, parse_dru, render_dru, write_dru
from kiwrap.formats.sexpr import Node, dumps, load, loads

CORPUS = Path(__file__).resolve().parent.parent / "conformance" / "corpus"
MOD_10 = CORPUS / "10.0" / "handwritten" / "R_0603_min.kicad_mod"
BOARD_10 = CORPUS / "10.0" / "headless_board" / "test.kicad_pcb"
BOARD_9 = CORPUS / "9.0" / "headless_board" / "multi_0.kicad_pcb"

EPOCH_8 = {"pcb": 20240108, "mod": 20240108, "pro": 1}
EPOCH_9 = {"pcb": 20241229, "mod": 20241229, "pro": 3}
EPOCH_10 = {"pcb": 20260206, "mod": 20260206, "pro": 3}
EPOCH_11 = {"pcb": 20260901, "mod": 20260901, "pro": 4}


class FakeLib:
    """A `FootprintSource`: every id resolves to the corpus 0603 footprint."""

    def __init__(self) -> None:
        self.node = load(MOD_10)
        self.requested: list[str] = []

    def footprint(self, fpid: str):
        self.requested.append(fpid)

        class Result:
            pass

        r = Result()
        r.node = self.node  # type: ignore[attr-defined]
        return r


def _writer(tmp_path: Path, epoch=EPOCH_10, name="b.kicad_pcb") -> BoardWriter:
    w = BoardWriter(tmp_path, epoch=epoch, lib_resolver=FakeLib())
    w.load(tmp_path / name)
    return w


# ---- dialect --------------------------------------------------------------------------------------


def test_dialect_thresholds():
    assert not dialect_for(EPOCH_9).net_names_only
    assert dialect_for(EPOCH_10).net_names_only
    assert dialect_for(EPOCH_11).net_names_only
    assert not dialect_for(EPOCH_8).layer_ids_v2
    assert dialect_for(EPOCH_9).layer_ids_v2
    assert dialect_for(EPOCH_11).has_exclude_from_sim and not dialect_for(EPOCH_10).has_exclude_from_sim
    with pytest.raises(BoardWriterError):
        dialect_for({"pcb": 20221018})
    with pytest.raises(BoardWriterError):
        dialect_for({"sch": 20260306})


@pytest.mark.parametrize(
    "epoch,b_cu,edge,net0", [(EPOCH_8, 31, 44, True), (EPOCH_9, 2, 25, True), (EPOCH_10, 2, 25, False)]
)
def test_fresh_skeleton_per_dialect(tmp_path, epoch, b_cu, edge, net0):
    w = _writer(tmp_path, epoch)
    text = w.text()
    root = loads(text)
    assert root.get("version") == str(epoch["pcb"])
    rows = {r.values()[1]: int(r.values()[0]) for r in root.find("layers").nodes}
    assert rows["F.Cu"] == 0 and rows["B.Cu"] == b_cu and rows["Edge.Cuts"] == edge
    assert (root.find("net") is not None) == net0
    board = BoardFile.loads(text)
    assert board.copper_layers == ("F.Cu", "B.Cu")


def test_setup_layers_four_copper(tmp_path):
    w = _writer(tmp_path, EPOCH_10)
    w.setup(layers=4)
    board = BoardFile.loads(w.text())
    assert board.copper_layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    ids = {layer.name: layer.id for layer in board.layers}
    assert ids["In1.Cu"] == 4 and ids["In2.Cu"] == 6 and ids["B.Cu"] == 2
    w8 = _writer(tmp_path, EPOCH_8, "b8.kicad_pcb")
    w8.setup(layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    ids8 = {layer.name: layer.id for layer in BoardFile.loads(w8.text()).layers}
    assert ids8["In1.Cu"] == 1 and ids8["In2.Cu"] == 2 and ids8["B.Cu"] == 31
    with pytest.raises(BoardWriterError):
        w.setup(layers=3)


def test_setup_outline_rules_netclasses_dru(tmp_path):
    w = _writer(tmp_path, EPOCH_10)
    applied = w.setup(
        rules={
            "clearance_mm": 0.15,
            "track_width_mm": 0.2,
            "via_size_mm": 0.6,
            "via_drill_mm": 0.3,
            "min_track_mm": 0.1,
        },
        netclasses={"Power": {"nets": ["GND", "+5V"], "clearance_mm": 0.3, "track_width_mm": 0.5}},
        outline={"rect": (30, 20), "origin": (10, 10)},
        custom_rules=[DruRule("edge", {"edge_clearance": 0.5})],
    )
    assert applied["rules"]["min_clearance"] == 0.15 and applied["rules"]["min_track_width"] == 0.1
    w.save()
    board = BoardFile.load(tmp_path / "b.kicad_pcb")
    bbox = board.outline_bbox()
    assert bbox is not None and (bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y) == (10, 10, 40, 30)
    pro = json.loads((tmp_path / "b.kicad_pro").read_text(encoding="utf-8"))
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_clearance"] == 0.15 and rules["min_track_width"] == 0.1
    classes = {c["name"]: c for c in pro["net_settings"]["classes"]}
    assert classes["Default"]["track_width"] == 0.2 and classes["Default"]["via_drill"] == 0.3
    assert (
        classes["Power"]["clearance"] == 0.3 and classes["Power"]["priority"] < classes["Default"]["priority"]
    )
    assert pro["net_settings"]["netclass_patterns"] == [
        {"netclass": "Power", "pattern": "GND"},
        {"netclass": "Power", "pattern": "+5V"},
    ]
    assert pro["meta"]["filename"] == "b.kicad_pro"
    dru = (tmp_path / "b.kicad_dru").read_text(encoding="utf-8")
    assert (
        dru.startswith("(version 1)")
        and '(rule "edge"' in dru
        and "(constraint edge_clearance (min 0.5mm))" in dru
    )
    with pytest.raises(BoardWriterError):
        w.setup(rules={"bogus": 1})


def test_setup_keeps_existing_project_json(tmp_path):
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(
        json.dumps(
            {
                "meta": {"version": 3},
                "custom": {"keep": True},
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.1}]},
            }
        )
    )
    w = _writer(tmp_path, EPOCH_10)
    w.setup(rules={"clearance_mm": 0.25})
    w.save()
    data = json.loads(pro.read_text(encoding="utf-8"))
    assert data["custom"] == {"keep": True} and data["meta"]["version"] == 3
    assert data["net_settings"]["classes"][0]["clearance"] == 0.25


# ---- footprints ----------------------------------------------------------------------------------


def test_instantiate_qualifies_name_and_links(tmp_path):
    w = _writer(tmp_path, EPOCH_10)
    fp = w.instantiate(
        load(MOD_10),
        "Resistor_SMD:R_0603_min",
        "R7",
        "4k7",
        at_mm=(12.5, 30),
        rotation=90,
        path="/abc",
        sheet_name="Root",
        sheet_file="x.kicad_sch",
        fields={"Datasheet": "ds", "MPN": "RC0603"},
        dnp=True,
    )
    assert fp.atoms[0].value == "Resistor_SMD:R_0603_min"
    assert fp.find("version") is None and fp.find("generator") is None
    heads = [n.head for n in fp.nodes]
    assert heads[:3] == ["layer", "uuid", "at"]
    assert heads.index("path") < heads.index("attr") < heads.index("fp_rect")
    assert fp.get("path") == "/abc" and fp.get("sheetname") == "Root" and fp.get("sheetfile") == "x.kicad_sch"
    props = {p.atoms[0].value: p.atoms[1].value for p in fp.find_all("property")}
    assert props["Reference"] == "R7" and props["Value"] == "4k7" and props["Datasheet"] == "ds"
    assert props["MPN"] == "RC0603" and props["Description"] == ""
    assert [a.value for a in fp.find("attr").atoms] == ["smd", "dnp"]
    assert fp.find("at").values() == ["12.5", "30", "90"]
    # absolute angles: pads and texts carry the footprint rotation, local xy untouched
    pads = fp.find_all("pad")
    assert [p.find("at").values() for p in pads] == [["-0.825", "0", "90"], ["0.825", "0", "90"]]
    ref = next(p for p in fp.find_all("property") if p.atoms[0].value == "Reference")
    assert ref.find("at").values() == ["0", "-1.43", "90"]
    assert all(p.find("uuid") is not None for p in pads)
    uuids = {n.get("uuid") for n in fp.walk() if n.get("uuid")}
    assert len(uuids) == len([n for n in fp.walk() if n.find("uuid") is not None])


def test_instantiate_exclude_from_sim_only_on_new_stamp(tmp_path):
    lib = load(MOD_10)
    w10 = _writer(tmp_path, EPOCH_10)
    w11 = _writer(tmp_path, EPOCH_11, "b11.kicad_pcb")
    f10 = w10.instantiate(lib, "L:R", "R1", "1", exclude_from_sim=True, exclude_from_bom=True)
    f11 = w11.instantiate(lib, "L:R", "R1", "1", exclude_from_sim=True, exclude_from_bom=True)
    assert [a.value for a in f10.find("attr").atoms] == ["smd", "exclude_from_bom"]
    assert [a.value for a in f11.find("attr").atoms] == ["smd", "exclude_from_bom", "exclude_from_sim"]


def test_place_rotation_side_and_lock(tmp_path):
    w = _writer(tmp_path, EPOCH_10)
    fp = w.instantiate(load(MOD_10), "L:R", "R1", "1k", at_mm=(10, 10))
    w._insert_footprint(fp)
    w.place("R1", (20, 30), rotation=45)
    node = w.footprint_node("R1")
    assert node.find("at").values() == ["20", "30", "45"]
    assert node.find_all("pad")[0].find("at").values() == ["-0.825", "0", "45"]
    w.place("R1", (20, 30), rotation=0, side="back", locked=True)
    node = w.footprint_node("R1")
    assert node.get("layer") == "B.Cu" and node.get("locked") == "yes"
    assert node.find("at").values() == ["20", "30"]
    pad1 = node.find_all("pad")[0]
    assert [a.value for a in pad1.find("layers").atoms] == ["B.Cu", "B.Paste", "B.Mask"]
    assert pad1.find("at").values() == ["-0.825", "0"]
    crtyd = next(g for g in node.find_all("fp_rect"))
    assert crtyd.get("layer") == "B.CrtYd" and crtyd.find("start").values() == ["-1.48", "0.73"]
    ref = next(p for p in node.find_all("property") if p.atoms[0].value == "Reference")
    assert ref.get("layer") == "B.SilkS" and ref.find("at").values() == ["0", "1.43"]
    assert [a.value for a in ref.find("effects").find("justify").atoms] == ["mirror"]
    board = BoardFile.loads(w.text())
    r1 = board.footprint("R1")
    assert r1.side == "back" and r1.locked
    # flipping back restores the front geometry
    w.place("R1", (20, 30), side="front", locked=False)
    node = w.footprint_node("R1")
    assert node.get("layer") == "F.Cu" and node.find("locked") is None
    assert crtyd.find("start").values() == ["-1.48", "-0.73"] and ref.find("effects").find("justify") is None
    w8 = _writer(tmp_path, EPOCH_8, "b8.kicad_pcb")
    w8._insert_footprint(w8.instantiate(load(MOD_10), "L:R", "R1", "1k"))
    w8.place("R1", (1, 1), locked=True)
    assert "locked" in [a.value for a in w8.footprint_node("R1").atoms]
    with pytest.raises(BoardWriterError):
        w.place("R99", (0, 0))


def test_flip_helpers():
    assert flip_layer_name("F.Cu") == "B.Cu" and flip_layer_name("B.SilkS") == "F.SilkS"
    assert flip_layer_name("In1.Cu") == "In1.Cu" and flip_layer_name("*.Cu") == "*.Cu"
    fp = loads(
        '(footprint "X" (layer "F.Cu") (at 5 5 30) (fp_arc (start 0 1) (mid 1 0) (end 0 -1) (layer "F.SilkS")) (pad "1" smd rect (at 1 2 30) (size 1 1) (layers "F.Cu")))'
    )
    flip_footprint(fp)
    assert fp.get("layer") == "B.Cu" and fp.find("at").values() == ["5", "5", "-30"]
    arc = fp.find("fp_arc")
    assert arc.find("start").values() == ["0", "1"] and arc.find("end").values() == ["0", "-1"]
    assert arc.get("layer") == "B.SilkS"
    pad = fp.find("pad")
    assert pad.find("at").values() == ["1", "-2", "330"]


# ---- tracks / vias / zones / nets ------------------------------------------------------------------


@pytest.mark.parametrize("epoch", [EPOCH_9, EPOCH_10])
def test_tracks_vias_zones_in_both_net_dialects(tmp_path, epoch):
    w = _writer(tmp_path, epoch)
    w.setup(layers=2)
    segs = w.add_track([(0, 0), (10, 0), (10, 5)], 0.25, "F.Cu", "GND")
    via = w.add_via((10, 5), 0.6, 0.3, "GND")
    zone = w.add_zone("GND", "B.Cu", [(0, 0), (20, 0), (20, 20), (0, 20)], priority=2, name="gnd")
    ko = w.add_zone("", "F.Cu", [(1, 1), (2, 1), (2, 2)], keepout={"tracks": True})
    assert len(segs) == 2
    board = BoardFile.loads(w.text())
    assert [t.net.name for t in board.tracks] == ["GND", "GND"] and board.vias[0].net.name == "GND"
    assert board.tracks[0].width == 0.25 and board.tracks[1].end.y == 5
    z = next(z for z in board.zones if z.uuid == zone)
    assert z.net.name == "GND" and z.layers == ("B.Cu",) and z.priority == 2 and z.name == "gnd"
    k = next(z for z in board.zones if z.uuid == ko)
    assert k.is_rule_area and k.layers == ("F.Cu",)
    root = loads(w.text())
    seg_net = root.find("segment").find("net").values()
    if dialect_for(epoch).net_names_only:
        assert seg_net == ["GND"] and root.find("net") is None
        assert root.find("zone").find("net_name") is None
    else:
        assert seg_net == ["1"]
        table = {r.values()[1]: r.values()[0] for r in root.find_all("net")}
        assert table == {"": "0", "GND": "1"}
        assert root.find("zone").get("net_name") == "GND"
    w.delete(via)
    assert not BoardFile.loads(w.text()).vias
    with pytest.raises(BoardWriterError):
        w.delete(via)
    with pytest.raises(BoardWriterError):
        w.add_track([(0, 0), (1, 1)], 0.2, "In1.Cu", "GND")
    with pytest.raises(BoardWriterError):
        w.add_zone("GND", "B.Cu", [(0, 0), (1, 1)])


def test_save_only_when_touched_and_edits_stay_local(tmp_path):
    src = BOARD_10.read_bytes()
    board_path = tmp_path / "test.kicad_pcb"
    board_path.write_bytes(src)
    w = BoardWriter(tmp_path, epoch=EPOCH_10)
    w.load(board_path)
    w.save()
    assert board_path.read_bytes() == src, "an untouched board is not rewritten"
    w.place("R1", (8, 10), rotation=90)
    w.save()
    before, after = src.decode("utf-8").splitlines(), board_path.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert 0 < len(changed) <= 10, (
        changed
    )  # (at …) of the footprint, its 5 properties, 1 text and 2 pads only
    assert BoardFile.load(board_path).footprint("R1").rotation == 90


def test_existing_board_dialect_wins_over_epoch(tmp_path):
    board_path = tmp_path / "multi_0.kicad_pcb"
    board_path.write_bytes(BOARD_9.read_bytes())
    w = BoardWriter(tmp_path, epoch=EPOCH_10)
    w.load(board_path)
    assert not w.dialect.net_names_only
    w.add_track([(0, 0), (1, 0)], 0.2, "F.Cu", "NEW")
    root = loads(w.text())
    codes = {r.values()[1]: int(r.values()[0]) for r in root.find_all("net")}
    assert codes["NEW"] == max(codes.values()) and codes["GND"] == 1
    assert root.find_all("segment")[-1].find("net").values() == [str(codes["NEW"])]


# ---- library resolution -------------------------------------------------------------------------


def test_minimal_resolver_tables_env_and_nesting(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "corpus.pretty").mkdir()
    (proj / "corpus.pretty" / "R_0603_min.kicad_mod").write_bytes(MOD_10.read_bytes())
    (proj / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "corpus") (type "KiCad") (uri "${KIPRJMOD}/corpus.pretty") (options "") (descr "")))'
    )
    share = tmp_path / "share"
    (share / "Resistor_SMD.pretty").mkdir(parents=True)
    (share / "Resistor_SMD.pretty" / "R_0603_min.kicad_mod").write_bytes(MOD_10.read_bytes())
    nested = tmp_path / "nested-table"
    nested.write_text(
        '(fp_lib_table (version 7) (lib (name "Nested") (type "KiCad") (uri "${MYLIBS}/Resistor_SMD.pretty") (options "") (descr "")))'
    )
    top = tmp_path / "fp-lib-table"
    top.write_text(
        f'(fp_lib_table (version 7) (lib (name "sub") (type "Table") (uri "{nested.as_posix()}") (options "") (descr "")))'
    )
    env = {"MYLIBS": share.as_posix()}
    rows = read_fp_lib_table(top, env)
    assert rows == {"Nested": share / "Resistor_SMD.pretty"}
    res = MinimalFootprintResolver(share, proj, env=env, tables=[top])
    assert res.footprint("corpus:R_0603_min").path == proj / "corpus.pretty" / "R_0603_min.kicad_mod"
    assert res.footprint("Nested:R_0603_min").pads[0].number == "1"
    assert res.footprint("Resistor_SMD:R_0603_min").path is not None  # <share>/<Nick>.pretty fallback
    with pytest.raises(BoardWriterError, match="not found"):
        res.footprint("Nope:R_0603_min")
    with pytest.raises(BoardWriterError, match="Nick:Name"):
        res.footprint("R_0603_min")


# ---- dru -------------------------------------------------------------------------------------------


def test_dru_render_parse_roundtrip(tmp_path):
    rules = [
        DruRule("hv", {"clearance": 1.5}, condition="A.NetClass == 'HV'", severity="error"),
        DruRule("no_vias", {"disallow": "via"}, layer="F.Cu"),
        '(rule "raw"\n\t(constraint track_width (min 0.2mm) (max 2mm))\n)',
    ]
    text = render_dru(rules)
    version, parsed = parse_dru(text)
    assert version == 1 and [r.name for r in parsed] == ["hv", "no_vias", "raw"]
    assert (
        parsed[0].constraints == {"clearance": "(min 1.5mm)"} and parsed[0].condition == "A.NetClass == 'HV'"
    )
    assert parsed[1].layer == "F.Cu" and parsed[1].constraints == {"disallow": "via"}
    assert parsed[2].constraints == {"track_width": "(min 0.2mm) (max 2mm)"}
    assert render_dru('(version 1)\n(rule "x" (constraint clearance (min 1mm)))').count("(version") == 1
    with pytest.raises(ValueError):
        render_dru('(rule "broken" (constraint clearance (min 1mm))')
    out = write_dru(tmp_path / "x.kicad_dru", rules)
    assert out.read_text(encoding="utf-8") == text
    assert dumps(loads(text)) == text


def test_place_on_affine_transform_dialect(tmp_path):
    """Boards the nightly writes (stamp 20260616+) carry `(transform …)` instead of `(at …)`;
    `place()` must update that block and never add a competing `(at)`."""
    w = _writer(tmp_path, EPOCH_11)
    fp = w.instantiate(load(MOD_10), "L:R", "R1", "1k", at_mm=(10, 10))
    fp.replace(fp.find("at"), loads("(transform (translate 10 10) (rotate 0) (scale 1 1))"))
    w._insert_footprint(fp)
    assert footprint_placement(w.footprint_node("R1")) == (10, 10, 0)
    w.place("R1", (33, 44), rotation=90, side="back")
    node = w.footprint_node("R1")
    assert node.find("at") is None
    assert node.find("transform").find("translate").values() == ["33", "44"]
    assert node.find("transform").find("rotate").values() == ["90"]
    assert node.get("layer") == "B.Cu"
    assert [p.find("at").values() for p in node.find_all("pad")] == [
        ["-0.825", "0", "90"],
        ["0.825", "0", "90"],
    ]


def _mod_with_rule_area() -> Node:
    """The corpus 0603 with a footprint rule area (a keep-out like the ESP32 module's antenna
    zone) in the library frame: x -24..24, y -27.75..-6.75 around the footprint origin."""
    mod = load(MOD_10)
    mod.append(
        loads(
            '(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid "11111111-1111-1111-1111-111111111111") '
            "(hatch full 0.508) (connect_pads (clearance 0)) (min_thickness 0.25) "
            "(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed)) "
            "(fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy -24 -6.75) (xy 24 -6.75) (xy 24 -27.75) (xy -24 -27.75))))"
        )
    )
    return mod


def _zone_pts(node: Node) -> list[list[str]]:
    zone = node.find("zone")
    return [xy.values() for xy in zone.find("polygon").find("pts").nodes if xy.head == "xy"]


def test_footprint_rule_area_follows_the_footprint_in_the_board_frame(tmp_path):
    """KiCad <= 10 stores a footprint's zones in BOARD coordinates and moves / rotates / flips
    them with the footprint (`FOOTPRINT::SetPosition` / `Rotate` / `Flip` on `m_zones`); the
    library file holds them around the footprint origin. Instantiating the ESP32-like module at
    (25, 6.75) must put its antenna keep-out at x 1..49 / y -21..0 on the board (Phase 1c
    finding: it sat at the library coordinates, off-board to the top-left), and every later
    placement change must carry it along exactly like the pads."""
    w = _writer(tmp_path, EPOCH_10)
    fp = w.instantiate(_mod_with_rule_area(), "L:U", "U1", "ESP", at_mm=(25, 6.75))
    w._insert_footprint(fp)
    assert _zone_pts(w.footprint_node("U1")) == [["1", "0"], ["49", "0"], ["49", "-21"], ["1", "-21"]]
    # rotate by 90 about the new position: KiCad's RotatePoint convention (x' = y, y' = -x)
    w.place("U1", (10, 10), rotation=90)
    assert _zone_pts(w.footprint_node("U1")) == [
        ["3.25", "34"],
        ["3.25", "-14"],
        ["-17.75", "-14"],
        ["-17.75", "34"],
    ]
    # back to 0 at the origin: the library coordinates come back exactly
    w.place("U1", (0, 0), rotation=0)
    assert _zone_pts(w.footprint_node("U1")) == [
        ["-24", "-6.75"],
        ["24", "-6.75"],
        ["24", "-27.75"],
        ["-24", "-27.75"],
    ]
    # a flip mirrors the outline about the footprint position (FOOTPRINT::Flip(m_pos, TOP_BOTTOM))
    w.place("U1", (25, 6.75), side="back")
    node = w.footprint_node("U1")
    assert node.get("layer") == "B.Cu"
    assert _zone_pts(node) == [["1", "13.5"], ["49", "13.5"], ["49", "34.5"], ["1", "34.5"]]
    w.place("U1", (25, 6.75), side="front")
    assert _zone_pts(w.footprint_node("U1")) == [["1", "0"], ["49", "0"], ["49", "-21"], ["1", "-21"]]
    # the sync path: instantiate + grid spread keeps the zone with the part
    w2 = _writer(tmp_path, EPOCH_10, "b2.kicad_pcb")
    fp2 = w2.instantiate(_mod_with_rule_area(), "L:U", "U2", "ESP", at_mm=(5, 5), rotation=180)
    assert _zone_pts(fp2) == [["29", "11.75"], ["-19", "11.75"], ["-19", "32.75"], ["29", "32.75"]]
    # the affine-transform dialect (the nightly's own files) keeps the library frame untouched
    w11 = _writer(tmp_path, EPOCH_11, "b11.kicad_pcb")
    fp11 = w11.instantiate(_mod_with_rule_area(), "L:U", "U1", "ESP", at_mm=(25, 6.75))
    assert _zone_pts(fp11) == [["-24", "-6.75"], ["24", "-6.75"], ["24", "-27.75"], ["-24", "-27.75"]]
    w11._insert_footprint(fp11)
    w11.place("U1", (10, 10), rotation=90, side="back")
    assert _zone_pts(w11.footprint_node("U1")) == [
        ["-24", "6.75"],
        ["24", "6.75"],
        ["24", "27.75"],
        ["-24", "27.75"],
    ]
