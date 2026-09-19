"""The board writer against KiCad itself (ARCHITECTURE §7/§11: KiCad is the judge).

Scenarios (each installed KiCad with `cli.has_drc` whose board epoch the writer supports; the
newest install and 10.0 are the primaries, 9.0 and 8.0 best effort, 7.0 has no DRC):

1. ecc83 schematic -> `sch export netlist` -> fresh board -> `BoardWriter.sync` ->
   `pcb drc --schematic-parity`: zero violations and zero parity items (the only DRC output is
   the unconnected ratsnest, which is the point of a fresh board).
2. `place()` (rotation, back side, lock) vs `pcb export pos`: positions, rotations and sides
   must agree; tracks/vias/zones the writer adds load and DRC accepts them.
3. Incremental sync after editing the *schematic* (a label renames a net, C2 gets another
   footprint, R3 is deleted) and re-exporting the netlist: parity clean again, the swapped
   footprint kept its position, the deleted part is gone, tracks moved to the renamed net.
4. `setup()`: 4 copper layers, netclass + custom rules (`.kicad_dru`) change DRC results the
   way KiCad documents, zones fill through `pcb drc --refill-zones --save-board` (10+) and the
   fill polygons appear in the file; `pcb upgrade` (10+) re-saves the writer's board.

The helpers are shared with `conformance/tests/t8_board_writer.py`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli.drc import run_drc
from kiwrap.backends.cli.netlist import export_netlist_file
from kiwrap.backends.cli.structured import export_position
from kiwrap.backends.cli.upgrade import upgrade_copy
from kiwrap.capability import capabilities_for
from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import MIN_STAMP, BoardWriter
from kiwrap.formats.dru import DruRule
from kiwrap.formats.libs import LibResolver
from kiwrap.formats.schematic import SchematicFile
from kiwrap.formats.sexpr import Atom, build, dump, load

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

DEMO = "ecc83"
SCH = "ecc83-pp.kicad_sch"
PCB = "ecc83-pp.kicad_pcb"


def supports(install) -> bool:
    caps = capabilities_for(install)
    try:
        return bool(caps.has("cli.has_drc")) and int(caps["formats.pcb"]) >= MIN_STAMP
    except (KeyError, TypeError, ValueError):
        return False


def primary_targets(installs):
    """The newest install and the 10.0 one (when different), both supported."""
    picked = []
    for inst in installs:
        if supports(inst) and (inst is installs[0] or (inst.major_minor == "10.0" and inst not in picked)):
            picked.append(inst)
    return picked


def epoch_of(install) -> dict[str, int]:
    caps = capabilities_for(install)
    return {k: int(caps[f"formats.{k}"]) for k in ("pcb", "mod", "pro")}


def project_copy(install, dst: Path) -> Path:
    """A scratch copy of the ecc83 demo without its board."""
    src = install.dir("demos") / DEMO
    if not (src / SCH).is_file():
        pytest.skip(f"{install.version} ships no {DEMO} demo")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.lck", "*-backups", "*.kicad_pcb"))
    return dst


def writer_for(install, config, proj: Path) -> BoardWriter:
    """The production pairing: the writer over `kiwrap.formats.libs.LibResolver` (the sandbox
    root's fp-lib-table of the target install, project tables, `${KIPRJMOD}`)."""
    w = BoardWriter(
        proj,
        epoch=epoch_of(install),
        lib_resolver=LibResolver(install, config, proj),
        generator_version=install.major_minor,
    )
    w.load(proj / PCB)
    return w


def drc_sections(install, config, board: Path, **kw) -> dict:
    run_drc(install, config, board, schematic_parity=True, severity="all", **kw)
    return json.loads(board.with_suffix(".drc.json").read_text(encoding="utf-8"))


def assert_parity_clean(doc: dict, where: str) -> None:
    assert doc["violations"] == [], f"{where}: DRC violations {[v['description'] for v in doc['violations']]}"
    assert doc["schematic_parity"] == [], (
        f"{where}: parity {[v['description'] for v in doc['schematic_parity']]}"
    )


def fresh_sync(install, config, proj: Path):
    """Steps 1: netlist export, fresh board, sync, save. Returns (writer, report, netlist path)."""
    net = export_netlist_file(install, config, proj / SCH)
    w = writer_for(install, config, proj)
    w.setup(
        layers=2,
        rules={"clearance_mm": 0.2, "track_width_mm": 0.25, "via_size_mm": 0.8, "via_drill_mm": 0.4},
        outline={"rect": (100, 80), "origin": (50, 50)},
    )
    report = w.sync(net)
    w.save()
    return w, report, net


def mutate_schematic(sch: Path) -> None:
    """Label `SIG_A` on P1 pin 2's net, C2 -> C_Disc_D5.0mm, delete R3 (through the CST)."""
    root = load(sch)
    model = SchematicFile.from_node(root)
    p1 = model.symbol("P1")
    assert p1 is not None
    pin2 = next(p for p in p1.pins if p.number == "2")
    root.append(
        build(
            "label",
            "SIG_A",
            build("at", pin2.position.x, pin2.position.y, 0),
            build(
                "effects",
                build("font", build("size", 1.27, 1.27)),
                build("justify", Atom.symbol("left"), Atom.symbol("bottom")),
            ),
            build("uuid", "11111111-2222-3333-4444-555555555555"),
        )
    )
    for sym in root.find_all("symbol"):
        props = {p.atoms[0].value: p for p in sym.find_all("property") if p.atoms}
        ref = props.get("Reference")
        if ref is None or len(ref.atoms) < 2:
            continue
        if ref.atoms[1].value == "C2":
            props["Footprint"].atoms[1].value = "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm"
        elif ref.atoms[1].value == "R3":
            root.remove(sym)
    dump(root, sch)


@pytest.fixture(scope="module")
def targets(installs):
    picked = primary_targets(installs)
    if not picked:
        pytest.skip("no supported install with kicad-cli DRC")
    return picked


def test_fresh_sync_parity_clean(targets, config_root, tmp_path):
    for inst in targets:
        proj = project_copy(inst, tmp_path / inst.version / "fresh")
        w, report, net = fresh_sync(inst, config_root(inst), proj)
        assert not report.unresolved, report.unresolved
        assert set(report.added) >= {"C1", "C2", "P1", "R1", "U1"} and not report.removed
        board = BoardFile.load(proj / PCB)
        assert {fp.reference for fp in board.footprints} == set(report.added)
        assert all(
            pad.net is not None for fp in board.footprints for pad in fp.pads if pad.number in ("1", "2")
        )
        doc = drc_sections(inst, config_root(inst), proj / PCB)
        assert_parity_clean(doc, f"{inst.version} fresh")
        assert len(doc["unconnected_items"]) > 0, "a fresh unrouted board has a ratsnest"


def test_place_matches_pos_export_and_items_load(targets, config_root, tmp_path):
    for inst in targets:
        cfg = config_root(inst)
        proj = project_copy(inst, tmp_path / inst.version / "place")
        w, _, _ = fresh_sync(inst, cfg, proj)
        w.place("P1", (60, 60), rotation=90)
        w.place("R4", (80, 60), rotation=0, side="back")
        w.place("U1", (100, 90), rotation=45, locked=True)
        w.place("R1", (120, 60), rotation=-90, side="bottom")
        w.save()
        board = BoardFile.load(proj / PCB)
        a, b = board.footprint("P1").pad("2").position, board.footprint("R4").pad("1").position
        w.add_track([(a.x, a.y), (b.x, a.y), (b.x, b.y)], 0.25, "F.Cu", "Net-(P1-PM)")
        w.add_via((b.x, a.y), 0.8, 0.4, "Net-(P1-PM)")
        w.add_zone("GND", "B.Cu", [(52, 52), (148, 52), (148, 128), (52, 128)])
        w.save()
        doc = drc_sections(inst, cfg, proj / PCB)
        assert doc["schematic_parity"] == []
        assert {v["type"] for v in doc["violations"]} <= {"via_dangling"}, [
            v["description"] for v in doc["violations"]
        ]
        rows = {r.ref: r for r in export_position(inst, cfg, proj / PCB).rows}
        expect = {
            "P1": (60, 60, 90, "top"),
            "R4": (80, 60, 0, "bottom"),
            "U1": (100, 90, 45, "top"),
            "R1": (120, 60, -90, "bottom"),
        }
        for ref, (x, y, rot, side) in expect.items():
            row = rows[ref]
            assert (round(row.x_mm, 3), round(row.y_mm, 3), row.side) == (x, y, side), (
                inst.version,
                ref,
                row,
            )
            assert (row.rotation_deg - rot) % 360 == 0, (inst.version, ref, row.rotation_deg)
        assert BoardFile.load(proj / PCB).footprint("U1").locked


def test_incremental_sync_after_schematic_edit(targets, config_root, tmp_path):
    for inst in targets:
        cfg = config_root(inst)
        proj = project_copy(inst, tmp_path / inst.version / "incr")
        w, _, _ = fresh_sync(inst, cfg, proj)
        w.place("C2", (70, 70), rotation=90, side="back")
        board = BoardFile.load(proj / PCB)
        p1, r4 = board.footprint("P1").pad("2").position, board.footprint("R4").pad("1").position
        w.add_track([(p1.x, p1.y), (r4.x, r4.y)], 0.25, "F.Cu", "Net-(P1-PM)")
        w.save()
        mutate_schematic(proj / SCH)
        net2 = export_netlist_file(inst, cfg, proj / SCH)
        text = net2.read_text(encoding="utf-8")
        assert "SIG_A" in text and "Net-(P1-PM)" not in text
        w2 = writer_for(inst, cfg, proj)
        report = w2.sync(net2)
        w2.save()
        assert report.footprint_changes == [
            ("C2", "Capacitor_THT:C_Disc_D4.7mm_W2.5mm_P5.00mm", "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm")
        ]
        assert report.removed == ["R3"] and report.net_renames == {"Net-(P1-PM)": "/SIG_A"}
        assert {(r, p) for r, p, _, n in report.pad_net_changes if n == "/SIG_A"} >= {
            ("P1", "2"),
            ("R4", "1"),
        }
        doc = drc_sections(inst, cfg, proj / PCB)
        assert_parity_clean(doc, f"{inst.version} incremental")
        after = BoardFile.load(proj / PCB)
        assert after.footprint("R3") is None
        c2 = after.footprint("C2")
        assert (
            c2.fpid.endswith("C_Disc_D5.0mm_W2.5mm_P5.00mm")
            and c2.side == "back"
            and (c2.position.x, c2.position.y) == (70, 70)
        )
        assert {t.net.name for t in after.tracks} == {"/SIG_A"}
        rows = {r.ref: r for r in export_position(inst, cfg, proj / PCB).rows}
        assert (rows["C2"].x_mm, rows["C2"].y_mm, rows["C2"].side) == (70, 70, "bottom") and rows[
            "C2"
        ].rotation_deg % 360 == 90


def test_setup_layers_rules_zones_and_upgrade(targets, config_root, tmp_path):
    for inst in targets:
        cfg = config_root(inst)
        caps = capabilities_for(inst)
        proj = project_copy(inst, tmp_path / inst.version / "setup")
        net = export_netlist_file(inst, cfg, proj / SCH)
        w = writer_for(inst, cfg, proj)
        w.setup(
            layers=4,
            rules={"clearance_mm": 0.2, "track_width_mm": 0.25},
            netclasses={"Power": {"nets": ["GND"], "clearance_mm": 0.2, "track_width_mm": 0.5}},
            outline={"rect": (100, 80), "origin": (50, 50)},
        )
        w.sync(net)
        w.place("C1", (70, 70))
        w.place("R1", (90, 70))
        gnd_b = w.add_zone("GND", "B.Cu", [(52, 52), (148, 52), (148, 128), (52, 128)])
        w.add_zone("GND", ["In1.Cu"], [(52, 52), (148, 52), (148, 128), (52, 128)], priority=1)
        w.add_zone(
            "", "F.Cu", [(60, 60), (65, 60), (65, 65), (60, 65)], keepout={"tracks": True, "vias": True}
        )
        board_path = w.save()
        assert BoardFile.load(board_path).copper_layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        refill = bool(caps.has("cli.drc_refill_flags"))
        doc = drc_sections(inst, cfg, board_path, refill_zones=refill, save_board=refill)
        assert_parity_clean(doc, f"{inst.version} 4-layer + zones")
        if refill:
            after = BoardFile.load(board_path)
            filled = next(z for z in after.zones if z.uuid == gnd_b).filled
            assert filled and all(polys for polys in filled.values()), "refilled zone carries fill polygons"
            assert board_path.read_text(encoding="utf-8").count("(filled_polygon") >= 2
        # the Power netclass is effective: a 3 mm GND clearance makes every GND pad violate
        w = writer_for(inst, cfg, proj)
        w.setup(netclasses={"Power": {"nets": ["GND"], "clearance_mm": 3.0}})
        w.save()
        run_drc(inst, cfg, board_path, severity="all")
        doc = json.loads(board_path.with_suffix(".drc.json").read_text(encoding="utf-8"))
        clearance = [v for v in doc["violations"] if v["type"] == "clearance"]
        assert clearance and all("Power" in v["description"] for v in clearance), inst.version
        # custom rules file: a global 3 mm clearance is picked up from <stem>.kicad_dru
        w = writer_for(inst, cfg, proj)
        w.setup(
            netclasses={"Power": {"nets": ["GND"], "clearance_mm": 0.2}},
            custom_rules=[DruRule("wide", {"clearance": 3.0})],
        )
        w.save()
        run_drc(inst, cfg, board_path, severity="all")
        doc = json.loads(board_path.with_suffix(".drc.json").read_text(encoding="utf-8"))
        assert sum(1 for v in doc["violations"] if v["type"] == "clearance") > len(clearance), inst.version
        (proj / "ecc83-pp.kicad_dru").unlink()
        if caps.has("cli.has_sch_pcb_upgrade"):
            up = upgrade_copy(inst, cfg, board_path, tmp_path / inst.version / "setup_up")
            resaved = BoardFile.load(up)
            assert {fp.reference for fp in resaved.footprints} == {
                fp.reference for fp in BoardFile.load(board_path).footprints
            }
            assert len(resaved.zones) == 3
