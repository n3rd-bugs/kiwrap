"""Unit tests for kiwrap.formats.analysis (courtyards, off-board, bus labels, netlist diff,
ratsnest estimate) on hand-written boards; no KiCad needed."""

from __future__ import annotations

import pytest

from kiwrap.formats.analysis import (
    compare_netlists,
    courtyard_overlaps,
    missing_courtyards,
    off_board,
    ratsnest_estimate,
)
from kiwrap.formats.analysis.connectivity import BusMember, bus_members, escape_net_name, split_bus_label
from kiwrap.formats.board import BoardFile
from kiwrap.formats.geometry import Vec2
from kiwrap.formats.reports import Net, Netlist, NetNode


def _fp(
    ref: str,
    x: float,
    y: float,
    rot: float = 0,
    layer: str = "F.Cu",
    w: float = 4,
    h: float = 2,
    nets=("GND", "VCC"),
    courtyard: bool = True,
) -> str:
    side = "F" if layer == "F.Cu" else "B"
    crt = (
        f'\t\t(fp_rect (start {-w / 2} {-h / 2}) (end {w / 2} {h / 2}) (stroke (width 0.05) (type solid)) (layer "{side}.CrtYd") (uuid "{ref}crt"))\n'
        if courtyard
        else ""
    )
    return (
        f'\t(footprint "Lib:R" (layer "{layer}") (uuid "{ref}-uuid")\n'
        f"\t\t(at {x} {y} {rot})\n"
        f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "{side}.SilkS") (uuid "{ref}ref") (effects (font (size 1 1))))\n'
        f'\t\t(property "Value" "R" (at 0 0 0) (layer "{side}.Fab") (uuid "{ref}val") (effects (font (size 1 1))))\n'
        "\t\t(attr smd)\n"
        + crt
        + f'\t\t(pad "1" smd rect (at -1 0) (size 1 0.5) (layers "{side}.Cu") (net "{nets[0]}") (uuid "{ref}p1"))\n'
        f'\t\t(pad "2" smd rect (at 1 0) (size 1 0.5) (layers "{side}.Cu") (net "{nets[1]}") (uuid "{ref}p2"))\n'
        "\t)\n"
    )


_LAYERS_2 = '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user) (31 "F.CrtYd" user) (29 "B.CrtYd" user))\n'


def _board(body: str, tracks: str = "", layers: str = _LAYERS_2) -> BoardFile:
    text = (
        '(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")\n'
        '\t(general (thickness 1.6))\n\t(paper "A4")\n'
        + layers
        + "\t(setup (pad_to_mask_clearance 0))\n"
        + body
        + '\t(gr_rect (start 0 0) (end 50 30) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "edge"))\n'
        + tracks
        + ")\n"
    )
    return BoardFile.loads(text)


def test_courtyard_overlaps_and_missing():
    b = _board(
        _fp("R1", 10, 10)
        + _fp("R2", 13, 10)  # 4 mm wide courtyards 3 mm apart: 1 mm overlap
        + _fp("R3", 14.0, 10)  # touches R1 exactly at x = 12 -> legal
        + _fp("R4", 30, 10, layer="B.Cu")
        + _fp("R5", 31, 10, layer="B.Cu")  # back side overlap
        + _fp("R6", 30, 10)  # front, same xy as R4: different side -> no overlap
        + _fp("R7", 40, 20, courtyard=False)
    )
    pairs = {(o.a, o.b, o.side): o for o in courtyard_overlaps(b)}
    assert set(pairs) == {("R1", "R2", "front"), ("R2", "R3", "front"), ("R4", "R5", "back")}
    o = pairs[("R1", "R2", "front")]
    assert (o.bbox.min_x, o.bbox.max_x) == (11, 12) and o.bbox.min_y == 9 and o.position == Vec2(11.5, 10)
    assert missing_courtyards(b) == ["R7"]
    # a courtyard clearance rule turns near misses into violations
    assert ("R1", "R3", "front") in {(o.a, o.b, o.side) for o in courtyard_overlaps(b, clearance=0.5)}


def test_off_board():
    b = _board(
        _fp("R1", 10, 10)  # inside
        + _fp("R2", 49, 10)  # courtyard sticks out on the right (max x 51)
        + _fp("R3", 25, 0.5, courtyard=False)  # no courtyard: pads (y +-0.25) inside
        + _fp("R4", 25, -5, courtyard=False)  # pads outside
    )
    items = off_board(b)
    assert [(i.reference, i.kind, i.detail) for i in items] == [
        ("R2", "courtyard", ""),
        ("R2", "pad", "2"),  # pad 2 spans x 49.5..50.5: copper outside, not just the courtyard
        ("R4", "pad", "1"),
        ("R4", "pad", "2"),
    ]
    assert items[0].position.x > 50
    assert items[0].outside_by == 1.0 and items[0].bbox is not None and items[0].bbox.max_x == 51.0
    assert items[1].outside_by == 0.5 and items[1].bbox is not None
    # a courtyard overhang alone (pads inside): one review-grade item, no pad item
    only_crt = off_board(_board(_fp("R5", 48.5, 10)))
    assert [(i.reference, i.kind, i.outside_by) for i in only_crt] == [("R5", "courtyard", 0.5)]
    # boards without a closed outline give nothing rather than false positives
    nb = BoardFile.loads(
        '(kicad_pcb (version 20260206) (generator "pcbnew") (layers (0 "F.Cu" signal)) (setup)\n'
        + _fp("R1", 10, 10)
        + ")"
    )
    assert nb.outline == () and off_board(nb) == []


def test_bus_labels():
    assert bus_members("D[0..3]") == ["D0", "D1", "D2", "D3"]
    assert bus_members("D[3..0]") == ["D0", "D1", "D2", "D3"]
    assert bus_members("A[1..2]+") == ["A1+", "A2+"]
    assert bus_members("{A B}") == ["A", "B"] and bus_members("{A, B}") == ["A", "B"]
    assert bus_members("PWR{A B[0..1]}") == ["PWR.A", "PWR.B0", "PWR.B1"]
    assert bus_members("I2C_{SYS}{SCL SDA}") == ["I2C_{SYS}.SCL", "I2C_{SYS}.SDA"]
    assert bus_members("~{RST}") is None and bus_members("D0") is None and bus_members("D[2..2]") is None
    assert bus_members("USB{X}", {"X": ("D_P", "D_N")}) == ["USB.D_P", "USB.D_N"]
    kind, members = split_bus_label("D[0..1]")
    assert kind == "vector" and members == [BusMember("D0", 0), BusMember("D1", 1)]
    kind, members = split_bus_label("P{A B}")
    assert kind == "group" and members[1] == BusMember("P.B", "B")
    assert escape_net_name("a/b\n") == "a{slash}b"


def test_compare_netlists_classes():
    ours = Netlist(
        "",
        "",
        (),
        (
            Net(1, "GND", (NetNode("R1", "1"),)),
            Net(2, "/X", (NetNode("R1", "2"), NetNode("R2", "1"))),
            Net(3, "extra", (NetNode("R9", "1"),)),
        ),
    )
    theirs = Netlist(
        "",
        "",
        (),
        (
            Net(1, "GND", (NetNode("R1", "1"),)),
            Net(2, "/Y", (NetNode("R1", "2"), NetNode("R2", "1"))),
            Net(3, "Net-(R3-Pad1)", (NetNode("R3", "1"),)),
            Net(4, "/Z", (NetNode("R2", "2"),)),
        ),
    )
    cmp = compare_netlists(ours, theirs)
    assert cmp.exact == 1 and cmp.renamed == 1 and cmp.renames == [("/Y", "/X")]
    assert cmp.missing == ["Net-(R3-Pad1)", "/Z"] and cmp.extra == ["extra"]
    assert cmp.agreement == pytest.approx(0.5) and cmp.exact_agreement == pytest.approx(0.25)
    assert cmp.classes == {"other": 2, "auto_name": 1}
    assert "1/4 exact" in cmp.summary()


def test_ratsnest_estimate():
    # R1.2 and R2.1 share VCC: R1.2 at (11,10), R2.1 at (29,10); a track joins R1.2 to a via at
    # (15,10) only, so one airwire remains between the via group and R2.1.
    b = _board(
        _fp("R1", 10, 10, nets=("GND", "VCC")) + _fp("R2", 30, 10, nets=("VCC", "N2")),
        tracks=(
            '\t(segment (start 11 10) (end 15 10) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "t1"))\n'
            '\t(via (at 15 10) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "VCC") (uuid "v1"))\n'
        ),
    )
    r = ratsnest_estimate(b)
    assert r.unrouted_by_net["VCC"] == 1 and r.unrouted == 1
    aw = r.airwires[0]
    assert (
        aw.net == "VCC" and {aw.start_ref, aw.end_ref} == {"R1.2", "R2.1"} and aw.length == pytest.approx(18)
    )
    assert r.unrouted_by_net["GND"] == 0 and "N2" in r.unrouted_by_net
    # a full route removes the airwire
    b2 = _board(
        _fp("R1", 10, 10, nets=("GND", "VCC")) + _fp("R2", 30, 10, nets=("VCC", "N2")),
        tracks='\t(segment (start 11 10) (end 29 10) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "t1"))\n',
    )
    assert ratsnest_estimate(b2).unrouted == 0
    # with a netlist, pad nets come from the netlist: R1.1 and R2.2 become one net
    nl = Netlist(
        "",
        "",
        (),
        (Net(1, "NEW", (NetNode("R1", "1"), NetNode("R2", "2"))), Net(2, "ONLY", (NetNode("R5", "1"),))),
    )
    r3 = ratsnest_estimate(b2, nl)
    assert r3.unrouted_by_net == {"NEW": 1} and r3.netlist_only_nets == ["ONLY"]
    assert set(r3.pads_without_net_items) == {"R1.2", "R2.1"}
    # tracks ending inside a pad (not at its centre) still connect when pad_hit is on
    b3 = _board(
        _fp("R1", 10, 10, nets=("GND", "VCC")) + _fp("R2", 30, 10, nets=("VCC", "N2")),
        tracks='\t(segment (start 11.3 10.1) (end 28.7 10) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "t1"))\n',
    )
    assert ratsnest_estimate(b3).unrouted == 0
    assert ratsnest_estimate(b3, pad_hit=False).unrouted == 2  # the track becomes a third group


# ---- ratsnest: what KiCad's connectivity connects that end points alone do not -----------------------
# (each rule below was found by comparing the estimate with `kicad-cli pcb drc` unconnected_items on
# the demo boards of the installed KiCads on 2026-09-19; the integrator's ratsnest_vs_drc run reached
# a per-board match on every demo of 10.99 / 10.0 once they were in)


def _zone(net: str, layer: str, outline: str, fills: str, name: str = "z1") -> str:
    return (
        f'\t(zone (net 1) (net_name "{net}") (layer "{layer}") (uuid "{name}") (name "{name}") (hatch edge 0.5)\n'
        "\t\t(connect_pads (clearance 0.5))\n\t\t(min_thickness 0.25)\n\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        f"\t\t(polygon (pts {outline}))\n" + fills + "\t)\n"
    )


def _filled(layer: str, pts: str) -> str:
    return f'\t\t(filled_polygon (layer "{layer}") (pts {pts}))\n'


def _xy(*pts: tuple[float, float]) -> str:
    return " ".join(f"(xy {x} {y})" for x, y in pts)


def test_ratsnest_filled_zone_connects_by_stored_fill_not_by_outline():
    """R1.1 and R2.1 share GND; the zone outline covers both, but the stored fill reaches only R1
    (a solid cover) and R2 through a thermal spoke that ends inside the pad copper. A third pad on an
    island polygon of the same zone stays unconnected; `zones_connect=True` uses the outline; False
    ignores zones."""
    body = _fp("R1", 10, 10) + _fp("R2", 30, 10) + _fp("R3", 10, 25)
    # solid cover over R1.1 (pad centre 9,10), a spoke into R2.1 (pad centre 29,10; copper 28.5..29.5 x 9.75..10.25)
    main = _xy((5, 5), (20, 5), (20, 9.8), (29.2, 9.8), (29.2, 10.2), (20, 10.2), (20, 15), (5, 15))
    island = _xy((8, 23), (12, 23), (12, 27), (8, 27))
    zone = _zone(
        "GND",
        "F.Cu",
        _xy((4, 4), (35, 4), (35, 28), (4, 28)),
        _filled("F.Cu", main) + _filled("F.Cu", island),
    )
    b = _board(body + zone)
    r = ratsnest_estimate(b)
    assert r.unrouted_by_net["GND"] == 1  # R3.1 on the island
    assert {r.airwires[0].start_ref, r.airwires[0].end_ref} & {"R3.1"}
    assert ratsnest_estimate(b, zones_connect=True).unrouted_by_net["GND"] == 0  # outline covers all three
    assert ratsnest_estimate(b, zones_connect=False).unrouted_by_net["GND"] == 2


def test_ratsnest_track_width_and_t_junction_connect():
    """A 0.8 mm track stopping 0.2 mm short of a pad edge still overlaps the pad with its width (KiCad
    routes it that way on the ecc83 demo: P1.2 / P4.1); a track ending on the body of another track
    is a T-junction."""
    body = _fp("R1", 10, 10, nets=("GND", "VCC")) + _fp("R2", 30, 10, nets=("VCC", "N2"))
    # R1.2 pad copper x 10.5..11.5; the track starts at 11.7 (0.2 outside) with half-width 0.4
    tracks = (
        '\t(segment (start 11.7 10) (end 20 10) (width 0.8) (layer "F.Cu") (net "VCC") (uuid "t1"))\n'
        '\t(segment (start 20 5) (end 20 15) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "t2"))\n'
        '\t(segment (start 20 12) (end 29 10) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "t3"))\n'
    )
    assert ratsnest_estimate(_board(body, tracks)).unrouted_by_net["VCC"] == 0
    thin = tracks.replace(
        "(start 11.7 10) (end 20 10) (width 0.8)", "(start 11.7 10) (end 20 10) (width 0.2)"
    )
    assert ratsnest_estimate(_board(body, thin)).unrouted_by_net["VCC"] == 1  # 0.1 half-width misses by 0.1


def test_ratsnest_jumper_pads_net_ties_and_copper_graphics():
    """KiCad's `updateJumperPads` rules (verified with kicad-cli DRC on 10.0.6 and the nightly,
    scratchpad fixer/probe_stacked.py): two pads numbered "1" in one footprint are one node only
    with `duplicate_pad_numbers_are_jumpers`; `jumper_pad_groups` joins the listed pads;
    `net_tie_pad_groups` joins NOTHING (DRC reports the tied pads unconnected); a board-level
    copper rectangle bridges the pads it covers (openair-max USB shield pads)."""
    jumper = (
        '\t(footprint "Lib:JP" (layer "F.Cu") (uuid "jp-uuid")\n\t\t(at 10 20)\n'
        '\t\t(property "Reference" "JP1" (at 0 0 0) (layer "F.SilkS") (uuid "jpref") (effects (font (size 1 1))))\n'
        '\t\t(property "Value" "JP" (at 0 0 0) (layer "F.Fab") (uuid "jpval") (effects (font (size 1 1))))\n'
        '\t\t(attr smd)\n\t\t(jumper_pad_groups ("1" "2"))\n'
        '\t\t(pad "1" smd rect (at -1 0) (size 1 0.5) (layers "F.Cu") (net "SIG") (uuid "jp1"))\n'
        '\t\t(pad "2" smd rect (at 1 0) (size 1 0.5) (layers "F.Cu") (net "SIG") (uuid "jp2"))\n\t)\n'
    )
    stacked = (
        '\t(footprint "Lib:J" (layer "F.Cu") (uuid "j-uuid")\n\t\t(at 30 20)\n'
        '\t\t(property "Reference" "J1" (at 0 0 0) (layer "F.SilkS") (uuid "jref") (effects (font (size 1 1))))\n'
        '\t\t(property "Value" "J" (at 0 0 0) (layer "F.Fab") (uuid "jval") (effects (font (size 1 1))))\n'
        "\t\t(attr smd)\n\t\t(duplicate_pad_numbers_are_jumpers yes)\n"
        '\t\t(pad "1" smd rect (at -1 0) (size 1 0.5) (layers "F.Cu") (net "SIG") (uuid "j1a"))\n'
        '\t\t(pad "1" smd rect (at 1 0) (size 1 0.5) (layers "F.Cu") (net "SIG") (uuid "j1b"))\n\t)\n'
    )
    shield = _fp("U1", 10, 10, nets=("SH", "SH")) + _fp("U2", 30, 10, nets=("SH", "SH"))
    bridge = '\t(gr_rect (start 8 9.5) (end 32 10.5) (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu") (uuid "cu-bridge"))\n'
    b = _board(jumper + stacked + shield + bridge)
    r = ratsnest_estimate(b)
    assert r.unrouted_by_net["SIG"] == 1  # JP1 (grouped) <-> J1 (jumpered): exactly one airwire
    assert r.unrouted_by_net["SH"] == 0  # four pads under one copper rectangle
    plain = jumper.replace('\t\t(jumper_pad_groups ("1" "2"))\n', '\t\t(net_tie_pad_groups "1, 2")\n')
    plain += stacked.replace("\t\t(duplicate_pad_numbers_are_jumpers yes)\n", "") + shield
    r2 = ratsnest_estimate(_board(plain))
    assert r2.unrouted_by_net["SIG"] == 3 and r2.unrouted_by_net["SH"] == 3  # what kicad-cli DRC reports


def test_ratsnest_blind_via_spans_the_layers_between():
    """A blind via `(layers "F.Cu" "In1.Cu")` joins F.Cu and In1.Cu; a through via every copper layer."""
    layers = '\t(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user) (31 "F.CrtYd" user) (29 "B.CrtYd" user))\n'
    body = _fp("R1", 10, 10, nets=("GND", "VCC")) + _fp("R2", 30, 10, layer="B.Cu", nets=("VCC", "N2"))
    tracks = (
        '\t(segment (start 11 10) (end 20 10) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "t1"))\n'
        '\t(via blind (at 20 10) (size 0.8) (drill 0.4) (layers "F.Cu" "In1.Cu") (net "VCC") (uuid "v1"))\n'
        '\t(segment (start 20 10) (end 29 10) (width 0.25) (layer "In1.Cu") (net "VCC") (uuid "t2"))\n'
        '\t(via blind (at 29 10) (size 0.8) (drill 0.4) (layers "In1.Cu" "B.Cu") (net "VCC") (uuid "v2"))\n'
    )
    four = _board(body, tracks, layers=layers)
    assert four.copper_layers == ("F.Cu", "In1.Cu", "B.Cu")
    assert ratsnest_estimate(four).unrouted_by_net["VCC"] == 0
    # the second via only reaching In1.Cu leaves R2.1 (B.Cu) alone
    broken = _board(
        body, tracks.replace('(layers "In1.Cu" "B.Cu")', '(layers "F.Cu" "In1.Cu")'), layers=layers
    )
    assert ratsnest_estimate(broken).unrouted_by_net["VCC"] == 1
