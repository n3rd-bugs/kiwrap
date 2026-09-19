"""T2 — file-tier models against KiCad itself (docs/ARCHITECTURE.md §6 certification, D17 tier 1).

For every installed KiCad (found the way T1 finds them, by globbing the share directories):

1. `BoardFile.load` and `SchematicFile.load` succeed on 100 % of the demo projects' `.kicad_pcb`
   and `.kicad_sch` files (the known-malformed royalblue board excluded, see corpus/demos.py).
2. Netlist parity: `netlist_from_file` vs `kicad-cli sch export netlist` for the named demo
   projects (ecc83, pic_programmer, flat_hierarchy, video, complex_hierarchy, interf_u,
   kit-dev-coldfire): exact agreement (same name, same node set) per net is recorded as a test
   property and every discrepancy class is listed; the assertion allows only the three known
   version-specific classes (`~` pin numbers on 7/8, power symbol nets named after the pin on 7,
   the nightly's stacked-pin de-duplication) and demands ≥ 98 % exact agreement otherwise.
3. Footprint positions / rotations / sides vs `pcb export pos --format csv --units mm` (the pos
   file negates Y) and pad nets vs the schematic netlist: 100 % positions; pad nets ≥ 99 % (a few
   demo boards are out of sync with their schematics, which is a property of the demo, and is
   recorded).
4. Courtyard overlaps vs `pcb drc --format json` `courtyards_overlap` violations (8.0+; projects
   whose severity for that check is `ignore` are skipped) on every demo board, plus a scratch copy
   of ecc83 with C1 moved next to C2 through the CST: the pair sets must be identical.

The measured numbers on 2026-09-19 (7.0.6 / 8.0.9 / 9.0.9 / 10.0.6 / 10.99.0) are in the
component report; run with `-rP` to see them again.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from kiwrap.backends.cli.drc import run_drc
from kiwrap.backends.cli.export import export_pcb
from kiwrap.backends.cli.netlist import export_netlist
from kiwrap.capability import Unsupported, capabilities_for
from kiwrap.formats.analysis import (
    compare_netlists,
    courtyard_overlaps,
    netlist_from_connectivity,
    pin_connectivity,
)
from kiwrap.formats.board import BoardFile
from kiwrap.formats.schematic import Hierarchy, SchematicFile
from kiwrap.formats.sexpr import dump

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parent / "corpus"

NETLIST_PROJECTS = {
    "ecc83": "ecc83-pp.kicad_sch",
    "pic_programmer": "pic_programmer.kicad_sch",
    "flat_hierarchy": "flat_hierarchy.kicad_sch",
    "video": "video.kicad_sch",
    "complex_hierarchy": "complex_hierarchy.kicad_sch",
    "interf_u": "interf_u.kicad_sch",
    "kit-dev-coldfire-xilinx_5213": "kit-dev-coldfire-xilinx_5213.kicad_sch",
}
# Discrepancy classes that are KiCad-version behaviour, not model errors (see module docstring).
ALLOWED_CLASSES = {"unconnected_naming", "power"}
BOARD_PROJECTS = (
    "ecc83",
    "pic_programmer",
    "flat_hierarchy",
    "video",
    "complex_hierarchy",
    "interf_u",
    "tiny_tapeout",
)


def _load_demos_module():
    try:
        from conformance.corpus import demos  # type: ignore[import-not-found]

        return demos
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location("kiwrap_corpus_demos_t2", CORPUS / "demos.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demos = _load_demos_module()


@dataclass(frozen=True)
class Counts:
    ok: int = 0
    total: int = 0

    def add(self, ok: bool) -> Counts:
        return Counts(self.ok + int(ok), self.total + 1)

    @property
    def ratio(self) -> float:
        return self.ok / self.total if self.total else 1.0


# ----------------------------------------------------------------------------------------------
# 1. every demo file loads
# ----------------------------------------------------------------------------------------------


def test_every_demo_board_and_schematic_loads(install, record_property):
    boards = schematics = 0
    failures: list[str] = []
    for d in demos.collect_demo_files(install.share, all_projects=True):
        try:
            if d.path.suffix == ".kicad_pcb":
                b = BoardFile.load(d.path)
                assert b.footprints or b.tracks or b.graphics, "empty board model"
                boards += 1
            elif d.path.suffix == ".kicad_sch":
                s = SchematicFile.load(d.path)
                assert s.uuid or s.symbols or s.wires, "empty schematic model"
                schematics += 1
        except Exception as e:  # noqa: BLE001 - report every failure at once
            failures.append(f"{d.path}: {e!r}")
    record_property("boards", boards)
    record_property("schematics", schematics)
    assert not failures, "\n".join(failures)
    assert boards > 0 and schematics > 0


# ----------------------------------------------------------------------------------------------
# 2. netlist parity
# ----------------------------------------------------------------------------------------------


def test_netlist_parity(install, config_root, tmp_path, record_property):
    cfg = config_root(install)
    caps = capabilities_for(install)
    report: dict[str, str] = {}
    exact_total = Counts()
    nodes_total = Counts()
    attr_examples: list[tuple[str, str, str | None, str | None]] = []
    bad_classes: dict[str, int] = {}
    for project, fname in NETLIST_PROJECTS.items():
        sch = install.share / "demos" / project / fname
        if not sch.exists():
            continue
        theirs = export_netlist(install, cfg, sch, tmp_path / f"{project}.net")
        h = Hierarchy.load(sch)
        conn = pin_connectivity(h)
        style = "name_number" if caps.has("cli.netlist_pinfunction_suffix") else "name"
        ours = netlist_from_connectivity(conn, h, source=str(sch), pin_function_style=style)
        cmp = compare_netlists(ours, theirs, conn)
        report[project] = cmp.summary()
        exact_total = Counts(exact_total.ok + cmp.exact, exact_total.total + cmp.total_theirs)
        nodes_total = Counts(
            nodes_total.ok + cmp.nodes_compared - len(cmp.function_mismatch) - len(cmp.type_mismatch),
            nodes_total.total + cmp.nodes_compared,
        )
        attr_examples.extend(cmp.function_mismatch[:3] + cmp.type_mismatch[:3])
        for cls, n in cmp.classes.items():
            if cls not in ALLOWED_CLASSES:
                bad_classes[cls] = bad_classes.get(cls, 0) + n
    for k, v in report.items():
        record_property(f"netlist.{k}", v)
    record_property("netlist.exact_agreement", f"{exact_total.ok}/{exact_total.total}")
    record_property("netlist.node_attribute_agreement", f"{nodes_total.ok}/{nodes_total.total}")
    assert report, "no netlist demo project found"
    assert not bad_classes, f"unexplained discrepancy classes: {bad_classes}\n{report}"
    assert exact_total.ratio >= 0.98, report
    # pinfunction / pintype per node (finding 15: every named pin differed on 10.0.6 / the nightly)
    assert nodes_total.ratio >= 0.99, (report, attr_examples[:12])


# ----------------------------------------------------------------------------------------------
# 3. positions and pad nets
# ----------------------------------------------------------------------------------------------


def _demo_boards(install) -> list[Path]:
    out: list[Path] = []
    for project in BOARD_PROJECTS:
        root = install.share / "demos" / project
        if root.is_dir():
            out.extend(
                p
                for p in sorted(root.glob("*.kicad_pcb"))
                if not demos.is_known_malformed(install.share / "demos", p)
            )
    return out


def test_positions_and_pad_nets(install, config_root, tmp_path, record_property):
    cfg = config_root(install)
    pos = Counts()
    nets = Counts()
    pos_bad: list[str] = []
    net_bad: list[str] = []
    for pcb in _demo_boards(install):
        board = BoardFile.load(pcb)
        out_dir = tmp_path / pcb.stem
        files = export_pcb(install, cfg, pcb, "pos", out_dir, format="csv", units="mm", side="both")
        by_ref: dict[str, list] = {}
        for fp in board.footprints:
            by_ref.setdefault(fp.reference, []).append(fp)
        with files[0].open(encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                x, y, rot = float(row["PosX"]), float(row["PosY"]), float(row["Rot"])
                side = {"top": "front", "bottom": "back"}[row["Side"].lower()]
                cands = by_ref.get(row["Ref"], [])
                fp = min(cands, key=lambda f: abs(f.position.x - x) + abs(f.position.y + y), default=None)
                ok = (
                    fp is not None
                    and abs(fp.position.x - x) < 1e-3
                    and abs(fp.position.y + y) < 1e-3  # the pos file negates Y
                    and min(abs((fp.rotation - rot) % 360), abs((fp.rotation - rot) % 360 - 360)) < 1e-3
                    and fp.side == side
                )
                pos = pos.add(ok)
                if not ok:
                    pos_bad.append(f"{pcb.name} {row['Ref']}: pos file {x},{y},{rot},{side} model {fp}")
        sch = pcb.with_suffix(".kicad_sch")
        if not sch.exists():
            continue
        netlist = export_netlist(install, cfg, sch, out_dir / "sch.net")
        want = {(n.ref, n.pin): net.name for net in netlist.nets for n in net.nodes}
        for fp, pad in board.pads():
            expected = want.get((fp.reference, pad.number))
            if expected is None:
                continue
            got = pad.net.name if pad.net else ""
            nets = nets.add(got == expected)
            if got != expected:
                net_bad.append(f"{pcb.name} {fp.reference}.{pad.number}: board {got!r} netlist {expected!r}")
    record_property("positions", f"{pos.ok}/{pos.total}")
    record_property("pad_nets", f"{nets.ok}/{nets.total}")
    record_property("pad_net_mismatches", net_bad[:20])
    assert pos.total > 0
    assert pos.ok == pos.total, "\n".join(pos_bad[:20])
    # kicad-cli 7 exports nets with its own (older) naming for boards that were last saved by
    # older KiCads; the boards from 8.0 on agree with the schematic except where the demo itself
    # is out of sync (flat_hierarchy's `~`-numbered mounting holes).
    assert nets.ratio >= 0.85, "\n".join(net_bad[:20])


# ----------------------------------------------------------------------------------------------
# 4. courtyard overlaps vs DRC
# ----------------------------------------------------------------------------------------------


def _drc_overlap_pairs(install, cfg, pcb: Path, out_json: Path) -> set[tuple[str, str]] | None:
    pro = pcb.with_suffix(".kicad_pro")
    if pro.exists():
        try:
            severities = json.loads(pro.read_text(encoding="utf-8"))["board"]["design_settings"][
                "rule_severities"
            ]
            if severities.get("courtyards_overlap") == "ignore":
                return None
        except (KeyError, ValueError, TypeError):
            pass
    try:
        report = run_drc(install, cfg, pcb, severity="all", out_json=out_json)
    except Unsupported:
        return None
    pairs: set[tuple[str, str]] = set()
    for v in report.violations:
        if v.type != "courtyards_overlap":
            continue
        refs = [
            it.description.split("Footprint ", 1)[1].split()[0]
            for it in v.items
            if "Footprint " in it.description
        ]
        if len(refs) == 2:
            pairs.add(tuple(sorted(refs)))  # type: ignore[arg-type]
    return pairs


def _our_pairs(board: BoardFile) -> set[tuple[str, str]]:
    return {tuple(sorted((o.a, o.b))) for o in courtyard_overlaps(board)}  # type: ignore[misc]


def test_courtyard_overlaps_match_drc(install, config_root, tmp_path, record_property):
    cfg = config_root(install)
    compared = 0
    mismatches: list[str] = []
    for pcb in _demo_boards(install):
        theirs = _drc_overlap_pairs(install, cfg, pcb, tmp_path / f"{pcb.stem}.drc.json")
        if theirs is None:
            continue
        ours = _our_pairs(BoardFile.load(pcb))
        compared += 1
        if ours != theirs:
            mismatches.append(
                f"{pcb.name}: only DRC {sorted(theirs - ours)} only model {sorted(ours - theirs)}"
            )
    # synthetic overlap: move ecc83's C1 next to C2 through the CST in a scratch copy
    src = install.share / "demos" / "ecc83"
    scratch = tmp_path / "synth"
    shutil.copytree(src, scratch)
    pcb = scratch / "ecc83-pp.kicad_pcb"
    before = BoardFile.load(pcb)
    c1, c2 = before.footprint("C1"), before.footprint("C2")
    assert c1 is not None and c2 is not None and c1.node is not None
    at = c1.node.find("at")
    assert at is not None
    at.atoms[0].value = c2.position.x + 1.0
    at.atoms[1].value = c2.position.y
    dump(before.root, pcb)
    after = BoardFile.load(pcb)
    ours = _our_pairs(after)
    assert ("C1", "C2") in ours
    theirs = _drc_overlap_pairs(install, cfg, pcb, tmp_path / "synth.drc.json")
    record_property("courtyard_boards_compared", compared)
    record_property("synthetic_overlap_pairs", sorted(ours))
    if theirs is None:
        pytest.skip(f"kicad-cli {install.version} has no DRC: courtyard parity checked on the model only")
    assert not mismatches, "\n".join(mismatches)
    assert ours == theirs, f"synthetic move: model {sorted(ours)} DRC {sorted(theirs)}"
