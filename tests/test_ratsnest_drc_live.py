"""The ratsnest estimate against KiCad's own connectivity on PARTIALLY ROUTED boards.

`ratsnest_estimate` exists for boards under construction, yet its earlier verification only used
routed demo boards where both sides report 0 (reviewer finding, 2026-09-19). Here a fixed fraction
of the segments / arcs / vias of a demo board is deleted through the CST (seeded, so the mutation
is reproducible) and the per-net airwire counts are compared with `kicad-cli pcb drc`
`unconnected_items` (one violation per missing connection) on the newest installed KiCad and on
10.0 (co-primary). Boards whose DRC count would hit KiCad's 499-item cap (`cli.drc_extended_error_limit`)
are excluded: the oracle only validates counts below it.

Measured 2026-09-19 (30 % removed): 10.99.0-4083 seed 1 - 15/17 demo boards match, the two others
are capped at 499; 10.0.6 seed 2 - 14/17, two capped, RoyalBlue54L-NFC-Antenna 24 vs 23 (an
antenna coil of sub-micron arcs, the estimate tests arcs by their end discs only).
"""

from __future__ import annotations

import random
import shutil
from collections import Counter

import pytest

from kiwrap.backends.cli.drc import capped_types, run_drc
from kiwrap.capability import capabilities_for
from kiwrap.formats.analysis import ratsnest_estimate
from kiwrap.formats.board import BoardFile
from kiwrap.formats.sexpr import dump

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

BOARDS = (
    ("ecc83", "ecc83-pp.kicad_pcb"),
    ("complex_hierarchy", "complex_hierarchy.kicad_pcb"),
    ("stickhub", "StickHub.kicad_pcb"),
    ("royalblue54L_feather", "RoyalBlue54L-Feather.kicad_pcb"),
)
FRACTION = 0.3
SEED = 2


def _targets(installs):
    """The newest install and the 10.0 one (when different), both with `cli.has_drc`."""
    picked = []
    for inst in installs:
        if capabilities_for(inst).has("cli.has_drc") and (
            inst is installs[0] or (inst.major_minor == "10.0" and inst not in picked)
        ):
            picked.append(inst)
    return picked


def _drc_net_of(violation) -> str:
    for it in violation.items:
        d = it.description or ""
        if "[" in d and "]" in d:
            return d[d.index("[") + 1 : d.index("]")]
    return ""


@pytest.mark.parametrize("demo,name", BOARDS, ids=[b[0] for b in BOARDS])
def test_mutated_board_airwires_match_drc(installs, config_root, tmp_path, demo, name):
    targets = _targets(installs)
    if not targets:
        pytest.skip("no install with kicad-cli DRC")
    checked = 0
    for inst in targets:
        src = inst.dir("demos") / demo
        if not (src / name).is_file():
            continue
        dst = tmp_path / inst.version / demo
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.lck", "*-backups"))
        copy = dst / name
        board = BoardFile.load(copy)
        rng = random.Random(SEED)
        victims = [
            n for n in board.root.nodes if n.head in ("segment", "via", "arc") and rng.random() < FRACTION
        ]
        for n in victims:
            board.root.remove(n)
        dump(board.root, copy)
        report = run_drc(inst, config_root(inst), copy, severity="all")
        assert "unconnected_items" not in capped_types(report), "board too large for the DRC oracle"
        est = ratsnest_estimate(BoardFile.load(copy))
        drc_nets = Counter(_drc_net_of(v) for v in report.unconnected)
        # DRC descriptions print net names unescaped ("A/B"), the board file stores "A{slash}B"
        est_nets = Counter({net.replace("{slash}", "/"): n for net, n in est.unrouted_by_net.items() if n})
        diff = {
            net: (drc_nets.get(net, 0), est_nets.get(net, 0))
            for net in set(drc_nets) | set(est_nets)
            if drc_nets.get(net, 0) != est_nets.get(net, 0)
        }
        assert diff == {}, f"{inst.version} {demo}: per-net (drc, estimate) differences"
        assert est.unrouted == len(report.unconnected) > 0
        checked += 1
    if not checked:
        pytest.skip(f"{demo} demo not shipped")
