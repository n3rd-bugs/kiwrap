"""`kiwrap.formats.netlist_sync`: the planner (pure) and `BoardWriter.sync` on an in-memory board.

No KiCad is needed here; `tests/test_board_writer_live.py` and `conformance/tests/t8_board_writer.py`
run the same sync against `kicad-cli pcb drc --schematic-parity`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter
from kiwrap.formats.netlist_sync import (
    FootprintState,
    SyncComponent,
    SyncNetlist,
    netlist_from_any,
    pad_net_assignment,
    plan_sync,
    read_sync_netlist,
)
from kiwrap.formats.reports.netlist import parse_netlist
from kiwrap.formats.sexpr import load, loads

CORPUS = Path(__file__).resolve().parent.parent / "conformance" / "corpus"
NET_10 = CORPUS / "10.0" / "led_d10" / "netlist_10.0.net"
MOD_10 = CORPUS / "10.0" / "handwritten" / "R_0603_min.kicad_mod"
EPOCH_10 = {"pcb": 20260206, "mod": 20260206, "pro": 3}
EPOCH_9 = {"pcb": 20241229, "mod": 20241229, "pro": 3}


def comp(ref, value="1k", fpid="L:R", pins=None, path="", **kw) -> SyncComponent:
    return SyncComponent(
        ref=ref,
        value=value,
        fpid=fpid,
        path=path or f"/{ref}-uuid",
        uuids=(f"{ref}-uuid",),
        pins=pins or {},
        **kw,
    )


def state(ref, fpid="L:R", pad_nets=None, path="", **kw) -> FootprintState:
    pad_nets = pad_nets or {}
    return FootprintState(
        uuid=f"fp-{ref}",
        ref=ref,
        value=kw.pop("value", "1k"),
        fpid=fpid,
        path=path or f"/{ref}-uuid",
        pad_numbers=tuple(pad_nets),
        pad_nets=dict(pad_nets),
        **kw,
    )


def netlist(*comps: SyncComponent) -> SyncNetlist:
    nets: dict[str, list[tuple[str, str]]] = {}
    for c in comps:
        for pin, net in c.pins.items():
            nets.setdefault(net, []).append((c.ref, pin))
    return SyncNetlist(components=comps, nets={k: tuple(v) for k, v in nets.items()})


# ---- reading -------------------------------------------------------------------------------------


def test_read_kicad_netlist_paths_pins_fields():
    nl = read_sync_netlist(NET_10.read_text(encoding="utf-8"))
    refs = [c.ref for c in nl.components]
    assert "D1" in refs and "R1" in refs
    d1 = nl.component("D1")
    assert d1.fpid == "LED_SMD:LED_0603_1608Metric" and d1.path == "/44141c57-aff0-4f76-854b-ffff37fb8be9"
    assert d1.uuids == ("44141c57-aff0-4f76-854b-ffff37fb8be9",)
    assert d1.sheet_path == "/" and d1.sheet_file.endswith(".kicad_sch")
    assert "Footprint" not in d1.fields and d1.fields.get("Description") == "Light emitting diode"
    assert set(d1.pins) == {"1", "2"} and all(d1.pins[p] for p in d1.pins)
    assert all(n for n in nl.nets) and sum(len(v) for v in nl.nets.values()) == sum(
        len(c.pins) for c in nl.components
    )
    # the report-model fallback carries no uuids -> empty paths, same pins
    rep = parse_netlist(NET_10.read_text(encoding="utf-8"))
    nl2 = netlist_from_any(rep)
    assert nl2.component("D1").path == "" and nl2.component("D1").pins == d1.pins
    assert netlist_from_any(NET_10).component("R1").path == nl.component("R1").path
    assert netlist_from_any(rep, text=NET_10.read_text(encoding="utf-8")).component("D1").path == d1.path
    with pytest.raises(ValueError):
        read_sync_netlist("(kicad_pcb)")


def test_flag_properties_and_multi_unit_paths():
    text = """(export (version "E")
      (components
        (comp (ref "U1") (value "OPA") (footprint "P:SO8")
          (property (name "dnp")) (property (name "exclude_from_bom") (value "0")) (property (name "exclude_from_sim"))
          (sheetpath (names "/amp/") (tstamps "/aaaa/")) (tstamps "u1a" "u1b"))
        (comp (ref "J9") (value "x") (footprint "P:X") (property (name "exclude_from_board")) (sheetpath (names "/") (tstamps "/")) (tstamps "j9")))
      (nets (net (code "1") (name "N") (node (ref "U1") (pin "1") (pinfunction "IN") (pintype "input")))))"""
    nl = read_sync_netlist(text)
    u1 = nl.component("U1")
    assert u1.dnp and not u1.exclude_from_bom and u1.exclude_from_sim
    assert u1.path == "/aaaa/u1a" and u1.uuids == ("u1a", "u1b") and u1.sheet_path == "/amp/"
    assert u1.pin_functions == {"1": "IN"} and u1.pin_types == {"1": "input"}
    assert nl.component("J9").exclude_from_board


# ---- planning ------------------------------------------------------------------------------------


def test_pad_net_assignment_repeats_and_unnumbered():
    assert pad_net_assignment(["1", "2", "3", "3", ""], {"1": "A", "3": "GND"}) == {
        "1": "A",
        "2": "",
        "3": "GND",
        "": "",
    }


def test_plan_add_update_swap_remove_locked():
    board = [
        state("R1", pad_nets={"1": "A", "2": "B"}),
        state("R2", pad_nets={"1": "A", "2": "C"}, fpid="L:Old"),
        state("R3", pad_nets={"1": "X", "2": "Y"}),
        state("R4", pad_nets={"1": "X", "2": "Y"}, locked=True),
    ]
    nl = netlist(
        comp("R1", pins={"1": "A", "2": "B"}),
        comp("R2", pins={"1": "A", "2": "C"}, fpid="L:New", value="2k"),
        comp("R5", pins={"1": "A", "2": "Z"}),
        comp("R6", fpid=None, pins={"1": "A"}),
    )
    plan = plan_sync(board, nl)
    assert [c.ref for c in plan.add] == ["R5"]
    assert plan.unresolved == [("R6", "", "no footprint assigned in the schematic")]
    upd = {u.ref: u for u in plan.update}
    assert not upd["R1"].changed
    assert (
        upd["R2"].fpid_change == ("L:Old", "L:New") and upd["R2"].value == "2k" and upd["R2"].pad_nets == {}
    )
    assert [s.ref for s in plan.remove] == ["R3"] and [s.ref for s in plan.kept_locked] == ["R4"]
    assert plan.nets == ("A", "B", "C", "Z")
    kept = plan_sync(board, nl, remove_orphans=False)
    assert not kept.remove and any("R3" in w for w in kept.warnings)


def test_plan_rename_by_path_and_pad_changes():
    board = [state("R1", pad_nets={"1": "A", "2": "B", "3": ""}, path="/same")]
    nl = netlist(comp("R9", pins={"1": "A", "2": "NEWB", "4": "Q"}, path="/same"))
    plan = plan_sync(board, nl)
    assert not plan.add and not plan.remove
    upd = plan.update[0]
    assert upd.matched_by == "path" and upd.old_ref == "R1" and upd.ref == "R9"
    assert upd.pad_nets == {"2": "NEWB"}
    assert any("no pad found for pin 4" in w for w in plan.warnings)
    # the whole pad set of B moved to NEWB and B is gone: a rename
    assert plan.net_renames == {"B": "NEWB"}


def test_plan_swapped_references_rename_by_path_and_keep_pad_nets():
    """R1 / R2 swap references in the schematic (uuids unchanged): KiCad's uuid mode renames the
    footprints and leaves value / pad nets alone; matching by reference first would rewrite the
    value and the pad nets onto the other footprint (routed copper on the wrong net)."""
    board = [
        state("R1", value="1k", pad_nets={"1": "/A", "2": "GND"}, path="/u1"),
        state("R2", value="10k", pad_nets={"1": "/B", "2": "GND"}, path="/u2"),
    ]
    nl = netlist(
        comp("R2", value="1k", pins={"1": "/A", "2": "GND"}, path="/u1"),
        comp("R1", value="10k", pins={"1": "/B", "2": "GND"}, path="/u2"),
    )
    plan = plan_sync(board, nl)
    assert not plan.add and not plan.remove and not plan.warnings
    by_uuid = {u.uuid: u for u in plan.update}
    assert by_uuid["fp-R1"].matched_by == "path" and by_uuid["fp-R1"].old_ref == "R1"
    assert by_uuid["fp-R1"].ref == "R2" and by_uuid["fp-R1"].value is None and by_uuid["fp-R1"].pad_nets == {}
    assert (
        by_uuid["fp-R2"].old_ref == "R2" and by_uuid["fp-R2"].ref == "R1" and by_uuid["fp-R2"].pad_nets == {}
    )
    # the GUI's re-link-by-reference box: reference first, so both footprints are rewritten
    ref_plan = plan_sync(board, nl, match="reference")
    by_uuid = {u.uuid: u for u in ref_plan.update}
    assert by_uuid["fp-R1"].matched_by == "reference" and by_uuid["fp-R1"].old_ref is None
    assert by_uuid["fp-R1"].value == "10k" and by_uuid["fp-R1"].pad_nets == {"1": "/B"}
    with pytest.raises(ValueError):
        plan_sync(board, nl, match="uuid")


def test_plan_recreated_symbol_is_readded_and_pathless_sides_match_by_reference():
    # same reference, new uuid: KiCad's uuid mode removes + re-adds (position lost, warned)
    board = [state("R1", pad_nets={"1": "A"}, path="/old")]
    plan = plan_sync(board, netlist(comp("R1", pins={"1": "A"}, path="/new")))
    assert [c.ref for c in plan.add] == ["R1"] and [s.ref for s in plan.remove] == ["R1"]
    assert any("uuid changed" in w for w in plan.warnings)
    # a footprint without a path (hand-made board) or a netlist without tstamps: by reference
    for board_path, comp_path in (("", "/new"), ("/old", ""), ("", "")):
        board = [replace(state("R1", pad_nets={"1": "A"}), path=board_path)]
        plan = plan_sync(board, netlist(replace(comp("R1", pins={"1": "B"}), path=comp_path)))
        assert not plan.add and not plan.remove, (board_path, comp_path)
        assert plan.update[0].matched_by == "reference" and plan.update[0].pad_nets == {"1": "B"}


def test_plan_net_rename_needs_consensus_and_disconnects_stray_pads():
    board = [state("R1", pad_nets={"1": "OLD", "2": "K"}), state("R2", pad_nets={"1": "OLD", "2": "K"})]
    nl = netlist(comp("R1", pins={"1": "N1"}), comp("R2", pins={"1": "N2", "2": "K"}))
    plan = plan_sync(board, nl)
    assert plan.net_renames == {}  # OLD split into N1 and N2
    upd = {u.ref: u for u in plan.update}
    assert upd["R1"].pad_nets == {"1": "N1", "2": ""}  # pin 2 no longer in the netlist -> disconnected
    assert upd["R2"].pad_nets == {"1": "N2"}


def test_plan_flags_fields_sheet_and_exclude_from_board():
    board = [
        state("R1", pad_nets={"1": "A"}, dnp=True, properties={"Datasheet": "old"}),
        state("R2", pad_nets={}),
    ]
    nl = netlist(
        comp(
            "R1",
            pins={"1": "A"},
            dnp=False,
            exclude_from_bom=True,
            exclude_from_sim=True,
            exclude_from_pos_files=True,
            fields={"Datasheet": "new", "MPN": "X"},
            sheet_name="Top",
            sheet_file="t.kicad_sch",
        ),
        comp("R2", pins={}, exclude_from_board=True),
    )
    plan = plan_sync(board, nl)
    upd = plan.update[0]
    assert (
        upd.dnp is False
        and upd.exclude_from_bom is True
        and upd.exclude_from_sim is True
        and upd.exclude_from_pos_files is True
    )
    assert (
        upd.fields == {"Datasheet": "new", "MPN": "X"}
        and upd.sheet_name == "Top"
        and upd.sheet_file == "t.kicad_sch"
    )
    assert [s.ref for s in plan.remove] == ["R2"]  # excluded from board -> orphan
    assert not plan_sync(board, nl, update_fields=False).update[0].fields


# ---- executing on a board ----------------------------------------------------------------------


class Lib:
    def __init__(self):
        self.node = load(MOD_10)

    def footprint(self, fpid):
        if fpid.startswith("Missing:"):
            raise LookupError(f"no such footprint {fpid}")
        r = type("R", (), {})()
        r.node = self.node
        return r


def _sync_fresh(tmp_path, epoch):
    w = BoardWriter(tmp_path, epoch=epoch, lib_resolver=Lib())
    w.load(tmp_path / "s.kicad_pcb")
    w.setup(outline={"rect": (20, 10)})
    nl = netlist(
        comp(
            "R1",
            pins={"1": "VCC", "2": "SIG"},
            fields={"Datasheet": "", "Description": "res"},
            sheet_name="Root",
            sheet_file="s.kicad_sch",
        ),
        comp("R2", pins={"1": "SIG", "2": "GND"}, dnp=True),
        comp("R3", pins={"1": "GND"}, fpid="Missing:R"),
    )
    return w, w.sync(nl)


@pytest.mark.parametrize("epoch", [EPOCH_9, EPOCH_10])
def test_sync_fresh_board(tmp_path, epoch):
    w, rep = _sync_fresh(tmp_path, epoch)
    assert rep.added == ["R1", "R2"] and rep.removed == [] and rep.updated == []
    assert rep.unresolved and rep.unresolved[0][0] == "R3" and "Missing:R" in rep.unresolved[0][1]
    assert {(r, p, n) for r, p, _, n in rep.pad_net_changes} == {
        ("R1", "1", "VCC"),
        ("R1", "2", "SIG"),
        ("R2", "1", "SIG"),
        ("R2", "2", "GND"),
    }
    assert set(rep.placed) == {"R1", "R2"}
    board = BoardFile.loads(w.text())
    r1, r2 = board.footprint("R1"), board.footprint("R2")
    assert r1.pad("1").net.name == "VCC" and r2.pad("2").net.name == "GND" and r2.dnp and not r1.dnp
    assert r1.path == "/R1-uuid" and r1.node.get("sheetname") == "Root"
    assert r1.properties.get("Description") == "res"
    # placed in a row below the outline, not overlapping each other
    outline = board.outline_bbox()
    assert r1.position.y > outline.max_y and r2.position.y > outline.max_y
    assert abs(r1.position.x - r2.position.x) > 3
    assert {n.name for n in board.nets if n.name} == {"VCC", "SIG", "GND"}
    assert board.net_codes_in_file == (epoch is EPOCH_9)
    pad = r1.node.find_all("pad")[0]
    assert pad.find("net").values() == (["1", "VCC"] if epoch is EPOCH_9 else ["VCC"])
    w.save()
    assert (tmp_path / "s.kicad_pcb").is_file()


def test_sync_incremental_swap_rename_remove_and_track_renet(tmp_path):
    w, _ = _sync_fresh(tmp_path, EPOCH_10)
    w.place("R2", (5, 5), rotation=90, side="back", locked=True)
    w.add_track([(0, 0), (1, 1)], 0.2, "F.Cu", "SIG")
    w.add_via((2, 2), 0.6, 0.3, "SIG")
    zone = w.add_zone("GND", "B.Cu", [(0, 0), (20, 0), (20, 10), (0, 10)])
    nl = netlist(
        comp("R1", pins={"1": "VCC", "2": "SIG2"}, value="4k7"),
        comp(
            "R2", pins={"1": "SIG2", "2": "GND"}, fpid="L:R_swapped"
        ),  # swap keeps placement; also no longer dnp
        comp("R7", pins={"1": "GND"}),
    )
    rep = w.sync(nl)
    assert rep.footprint_changes == [("R2", "L:R", "L:R_swapped")]
    assert sorted(rep.updated) == ["R1", "R2"] and rep.added == ["R7"]
    assert rep.net_renames == {"SIG": "SIG2"}
    board = BoardFile.loads(w.text())
    r2 = board.footprint("R2")
    assert r2.fpid == "L:R_swapped" and r2.side == "back" and r2.rotation == 90
    assert [p.find("at").values() for p in r2.node.find_all("pad")] == [
        ["-0.825", "0", "90"],
        ["0.825", "0", "90"],
    ]
    assert r2.locked and not r2.dnp and r2.position.x == 5 and r2.position.y == 5
    assert r2.pad("1").net.name == "SIG2" and board.footprint("R1").value == "4k7"
    assert {t.net.name for t in board.tracks} == {"SIG2"} and {v.net.name for v in board.vias} == {"SIG2"}
    assert next(z for z in board.zones if z.uuid == zone).net.name == "GND"
    # remove R7 again: its footprint goes, the zone stays
    rep2 = w.sync(
        netlist(
            comp("R1", pins={"1": "VCC", "2": "SIG2"}, value="4k7"),
            comp("R2", pins={"1": "SIG2", "2": "GND"}, fpid="L:R_swapped"),
        )
    )
    assert rep2.removed == ["R7"] and rep2.updated == [] and rep2.added == []
    assert BoardFile.loads(w.text()).footprint("R7") is None


def test_sync_locked_orphan_and_net_table_pruning(tmp_path):
    w, _ = _sync_fresh(tmp_path, EPOCH_9)
    w.place("R2", (1, 1), locked=True)
    rep = w.sync(netlist(comp("R1", pins={"1": "VCC", "2": "SIG"})))
    assert rep.kept_locked == ["R2"] and rep.removed == []
    root = loads(w.text())
    table = {r.values()[1] for r in root.find_all("net")}
    assert table == {"", "VCC", "SIG", "GND"}  # GND still referenced by R2's pad
    w.place("R2", (1, 1), locked=False)
    rep = w.sync(netlist(comp("R1", pins={"1": "VCC", "2": "SIG"})))
    assert rep.removed == ["R2"]
    table = {r.values()[1] for r in loads(w.text()).find_all("net")}
    assert table == {"", "VCC", "SIG"}
