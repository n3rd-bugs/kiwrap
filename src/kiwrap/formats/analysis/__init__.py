"""File-tier analyses over `kiwrap.formats.board` / `kiwrap.formats.schematic` models — pure
Python, no KiCad process (docs/DECISIONS.md D17 tier 1).

    courtyard_overlaps(board)          -> [CourtyardOverlap(a, b, side, bbox)]
    off_board(board)                   -> [OffBoardItem(reference, kind, detail, position)]
    pin_connectivity(schematic|hier)   -> Connectivity(nets, dangling_wire_ends, unconnected_pins, …)
    netlist_from_file(schematic|hier)  -> kiwrap.formats.reports.Netlist (diffable with kicad-cli's)
    ratsnest_estimate(board, netlist)  -> Ratsnest(airwires, unrouted_by_net) — an ESTIMATE

Each module's docstring names the KiCad source it reproduces and where it knowingly differs.
"""

from __future__ import annotations

from kiwrap.formats.analysis.connectivity import (
    Connectivity,
    DerivedNet,
    PinRef,
    WireEnd,
    default_net_name,
    pin_connectivity,
)
from kiwrap.formats.analysis.courtyard import (
    CourtyardOverlap,
    OffBoardItem,
    courtyard_overlaps,
    missing_courtyards,
    off_board,
)
from kiwrap.formats.analysis.netlist import (
    NetlistComparison,
    compare_netlists,
    netlist_from_connectivity,
    netlist_from_file,
)
from kiwrap.formats.analysis.ratsnest import Airwire, Ratsnest, ratsnest_estimate

__all__ = [
    "Airwire",
    "Connectivity",
    "CourtyardOverlap",
    "DerivedNet",
    "NetlistComparison",
    "OffBoardItem",
    "PinRef",
    "Ratsnest",
    "WireEnd",
    "compare_netlists",
    "courtyard_overlaps",
    "default_net_name",
    "missing_courtyards",
    "netlist_from_connectivity",
    "netlist_from_file",
    "off_board",
    "pin_connectivity",
    "ratsnest_estimate",
]
