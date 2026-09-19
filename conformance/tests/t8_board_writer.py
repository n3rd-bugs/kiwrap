"""T8 — board authoring engine certified by KiCad (docs/ARCHITECTURE.md §11, D17 tier 1).

For every installed KiCad whose kicad-cli has DRC and whose board epoch the writer supports
(8.0+; 7.0 has neither `pcb drc` nor the footprint-field dialect):

1. ecc83 schematic -> `sch export netlist` -> fresh board -> `BoardWriter.sync` ->
   `pcb drc --schematic-parity --format json`: zero violations, zero parity items. The netlist
   import is kiwrap's own (docs/VERIFICATION.md claim 5: no headless import exists on 7-10),
   so KiCad's parity checker is the only oracle that counts.
2. `place()` vs `pcb export pos`: every placed reference agrees in x/y (mm, board frame),
   rotation (mod 360) and side, including flipped and locked parts.
3. Incremental sync after a schematic edit (label rename, footprint swap, symbol deletion):
   parity clean again; swapped footprint kept its placement; the deleted part is gone; the
   tracks on the renamed net followed it.
4. Zones: created by the writer, filled by `pcb drc --refill-zones --save-board` where the
   version has the flags (`cli.drc_refill_flags`); the fill polygons are in the re-saved file.
   Board setup (4 copper layers, rules, netclass patterns, `.kicad_dru`) is accepted by DRC and
   the netclass/custom rules are demonstrably in effect. `pcb upgrade` (10+) re-saves the
   writer's board with the same footprints and zones.

Numbers are recorded as test properties (`-rP` shows them). Scenario helpers are shared with
`tests/test_board_writer_live.py`.
"""

from __future__ import annotations

import json

import pytest
from tests.test_board_writer_live import (
    PCB,
    SCH,
    assert_parity_clean,
    drc_sections,
    fresh_sync,
    mutate_schematic,
    project_copy,
    supports,
    writer_for,
)

from kiwrap.backends.cli.drc import run_drc
from kiwrap.backends.cli.netlist import export_netlist_file
from kiwrap.backends.cli.structured import export_position
from kiwrap.backends.cli.upgrade import upgrade_copy
from kiwrap.capability import capabilities_for
from kiwrap.formats.board import BoardFile
from kiwrap.formats.dru import DruRule

pytestmark = [pytest.mark.kicad, pytest.mark.slow]


@pytest.fixture
def target(install, config_root):
    if not supports(install):
        pytest.skip(f"{install.version}: no kicad-cli DRC or unsupported board epoch")
    return install, config_root(install)


def test_t8_fresh_sync_parity(target, tmp_path, record_property):
    inst, cfg = target
    proj = project_copy(inst, tmp_path / "fresh")
    _, report, _ = fresh_sync(inst, cfg, proj)
    assert not report.unresolved, report.unresolved
    doc = drc_sections(inst, cfg, proj / PCB)
    assert_parity_clean(doc, f"{inst.version} fresh")
    record_property(
        "t8_fresh",
        json.dumps(
            {"kicad": inst.version, "added": len(report.added), "unconnected": len(doc["unconnected_items"])}
        ),
    )


def test_t8_placement_matches_pos(target, tmp_path, record_property):
    inst, cfg = target
    proj = project_copy(inst, tmp_path / "place")
    w, _, _ = fresh_sync(inst, cfg, proj)
    expect = {
        "P1": (60, 60, 90, "top"),
        "R4": (80, 60, 0, "bottom"),
        "U1": (100, 90, 45, "top"),
        "R1": (120, 60, -90, "bottom"),
    }
    w.place("P1", (60, 60), rotation=90)
    w.place("R4", (80, 60), rotation=0, side="back")
    w.place("U1", (100, 90), rotation=45, locked=True)
    w.place("R1", (120, 60), rotation=-90, side="bottom")
    w.save()
    rows = {r.ref: r for r in export_position(inst, cfg, proj / PCB).rows}
    mismatches = []
    for ref, (x, y, rot, side) in expect.items():
        row = rows[ref]
        if (round(row.x_mm, 3), round(row.y_mm, 3), row.side) != (x, y, side) or (
            row.rotation_deg - rot
        ) % 360 != 0:
            mismatches.append((ref, row))
    assert not mismatches, mismatches
    doc = drc_sections(inst, cfg, proj / PCB)
    assert_parity_clean(doc, f"{inst.version} placed")
    record_property("t8_pos", json.dumps({"kicad": inst.version, "checked": len(expect)}))


def test_t8_incremental_sync(target, tmp_path, record_property):
    inst, cfg = target
    proj = project_copy(inst, tmp_path / "incr")
    w, _, _ = fresh_sync(inst, cfg, proj)
    w.place("C2", (70, 70), rotation=90, side="back")
    board = BoardFile.load(proj / PCB)
    p1, r4 = board.footprint("P1").pad("2").position, board.footprint("R4").pad("1").position
    w.add_track([(p1.x, p1.y), (r4.x, r4.y)], 0.25, "F.Cu", "Net-(P1-PM)")
    w.save()
    mutate_schematic(proj / SCH)
    net2 = export_netlist_file(inst, cfg, proj / SCH)
    w2 = writer_for(inst, cfg, proj)
    report = w2.sync(net2)
    w2.save()
    assert report.removed == ["R3"] and report.net_renames == {"Net-(P1-PM)": "/SIG_A"}
    assert [c[0] for c in report.footprint_changes] == ["C2"]
    doc = drc_sections(inst, cfg, proj / PCB)
    assert_parity_clean(doc, f"{inst.version} incremental")
    after = BoardFile.load(proj / PCB)
    assert after.footprint("R3") is None and {t.net.name for t in after.tracks} == {"/SIG_A"}
    c2 = after.footprint("C2")
    assert c2.side == "back" and (c2.position.x, c2.position.y) == (70, 70)
    record_property(
        "t8_incremental",
        json.dumps(
            {
                "kicad": inst.version,
                "updated": len(report.updated),
                "pad_net_changes": len(report.pad_net_changes),
            }
        ),
    )


def test_t8_setup_zones_rules_upgrade(target, tmp_path, record_property):
    inst, cfg = target
    caps = capabilities_for(inst)
    proj = project_copy(inst, tmp_path / "setup")
    net = export_netlist_file(inst, cfg, proj / SCH)
    w = writer_for(inst, cfg, proj)
    w.setup(
        layers=4,
        rules={"clearance_mm": 0.2, "track_width_mm": 0.25},
        netclasses={"Power": {"nets": ["GND"], "clearance_mm": 0.2}},
        outline={"rect": (100, 80), "origin": (50, 50)},
    )
    w.sync(net)
    w.place("C1", (70, 70))
    w.place("R1", (90, 70))
    gnd = w.add_zone("GND", "B.Cu", [(52, 52), (148, 52), (148, 128), (52, 128)])
    w.add_zone("GND", ["In1.Cu"], [(52, 52), (148, 52), (148, 128), (52, 128)], priority=1)
    w.add_zone("", "F.Cu", [(60, 60), (65, 60), (65, 65), (60, 65)], keepout={"tracks": True})
    board_path = w.save()
    refill = bool(caps.has("cli.drc_refill_flags"))
    doc = drc_sections(inst, cfg, board_path, refill_zones=refill, save_board=refill)
    assert_parity_clean(doc, f"{inst.version} setup")
    fills = 0
    if refill:
        filled = next(z for z in BoardFile.load(board_path).zones if z.uuid == gnd).filled
        fills = sum(len(p) for p in filled.values())
        assert fills > 0
    w = writer_for(inst, cfg, proj)
    w.setup(netclasses={"Power": {"nets": ["GND"], "clearance_mm": 3.0}})
    w.save()
    run_drc(inst, cfg, board_path, severity="all")
    doc = json.loads(board_path.with_suffix(".drc.json").read_text(encoding="utf-8"))
    netclass_hits = [v for v in doc["violations"] if v["type"] == "clearance" and "Power" in v["description"]]
    assert netclass_hits, inst.version
    w = writer_for(inst, cfg, proj)
    w.setup(
        netclasses={"Power": {"nets": ["GND"], "clearance_mm": 0.2}},
        custom_rules=[DruRule("wide", {"clearance": 3.0})],
    )
    w.save()
    run_drc(inst, cfg, board_path, severity="all")
    doc = json.loads(board_path.with_suffix(".drc.json").read_text(encoding="utf-8"))
    dru_hits = sum(1 for v in doc["violations"] if v["type"] == "clearance")
    assert dru_hits > len(netclass_hits), inst.version
    upgraded = False
    if caps.has("cli.has_sch_pcb_upgrade"):
        (proj / "ecc83-pp.kicad_dru").unlink()
        up = upgrade_copy(inst, cfg, board_path, tmp_path / "setup_up")
        resaved = BoardFile.load(up)
        assert {fp.reference for fp in resaved.footprints} == {
            fp.reference for fp in BoardFile.load(board_path).footprints
        }
        assert len(resaved.zones) == 3
        upgraded = True
    record_property(
        "t8_setup",
        json.dumps(
            {
                "kicad": inst.version,
                "refilled": refill,
                "fill_polygons": fills,
                "netclass_hits": len(netclass_hits),
                "dru_hits": dru_hits,
                "upgraded": upgraded,
            }
        ),
    )
