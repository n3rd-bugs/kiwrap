"""`kicad-cli pcb export ratsnest` parser -> `RatsnestReport` (schema ratsnest.v1).

The export is a kiwrap fork addition (docs/UPSTREAM.md G1, branch `mr/g1-ratsnest`; capability
`cli.has_ratsnest_export`). JSON shape, verified on the fork builds of base 6dd893f7d1 against
`qa/data/pcbnew/issue8883.kicad_pcb` (7 airwires over 2 nets), `issue17429` (35 airwires, 7 of
them excluded) and the ecc83 demo:

    {"$schema": "https://schemas.kicad.org/ratsnest.v1.json", "source": "board.kicad_pcb",
     "date": "2026-09-19T19:27:49", "kicad_version": "10.99.0", "coordinate_units": "mm",
     "unrouted_count": 7, "airline_length": 47.31,
     "nets": [{"name": "+3V3", "unrouted_count": 2, "airline_length": 14.11,
               "edges": [{"a": {"uuid": "...", "description": "Pad 24 [+3V3] of IC2 on F.Cu",
                                "pos": {"x": 118.35, "y": -80.9375}, "layer": "F.Cu"},
                          "b": {...}, "length": 7.055339}]}]}

What the numbers are (pcbnew/ratsnest/ratsnest_report.cpp): the edges are the RN_NET minimum
spanning tree between the copper-connected clusters of a net, i.e. exactly the pairs
`kicad-cli pcb drc` lists as `unconnected_items` (verified pair-for-pair on issue8883), and the
end points are the ratsnest anchors the editor draws from (`CN_ANCHOR::Pos()` after
`OptimizeRNEdges`), not the item centres DRC reports. `layer` is the canonical name of the copper
layer of the anchor; for a through item (THT pad, through via) it is the layer shared with the
other end, else its top-most copper layer. An edge whose item pair the project excludes as a DRC
`unconnected_items` violation is listed with `"excluded": true` (absent otherwise) and is NOT
counted in `unrouted_count` / `airline_length` (the editor's "Unrouted" count and the airwires it
draws leave those out too). `description` is KiCad's localised item text ("Pad 2 [GND] of C27 on
F.Cu", "Track [+3V3] on F.Cu, length 1.9976 mm", "Via [GND] on F.Cu - B.Cu"): human-readable,
never parsed here; join `uuid` to a board model for the pad reference / number. Everything is
converted to millimetres from `coordinate_units` (mm / in / mils, the same enum as the DRC report).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kiwrap.formats.reports import Position
from kiwrap.formats.reports._common import load_json, unit_factor

__all__ = ["RatsnestEdge", "RatsnestNet", "RatsnestNode", "RatsnestReport", "parse_ratsnest_json"]


@dataclass(frozen=True)
class RatsnestNode:
    """One end of an airwire: the connected item and the anchor point on it (mm)."""

    uuid: str
    description: str
    pos: Position
    layer: str  # canonical copper layer name ("F.Cu"), "" when the export omitted it


@dataclass(frozen=True)
class RatsnestEdge:
    net: str
    a: RatsnestNode
    b: RatsnestNode
    length_mm: float
    excluded: bool = False  # hidden by a DRC unconnected_items exclusion: not drawn, not counted


@dataclass(frozen=True)
class RatsnestNet:
    name: str
    unrouted_count: int  # edges that are not excluded
    airline_length_mm: float  # over the edges that are not excluded
    edges: tuple[RatsnestEdge, ...]  # excluded ones included, flagged

    @property
    def active_edges(self) -> tuple[RatsnestEdge, ...]:
        """The edges the editor draws (not excluded)."""
        return tuple(e for e in self.edges if not e.excluded)


@dataclass(frozen=True)
class RatsnestReport:
    source: str
    kicad_version: str
    coordinate_units: str  # as written by KiCad ("mm" / "in" / "mils"); values below are mm
    unrouted_count: int
    airline_length_mm: float
    nets: tuple[RatsnestNet, ...]
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def edges(self) -> tuple[RatsnestEdge, ...]:
        """Every edge, excluded ones included (`RatsnestEdge.excluded`)."""
        return tuple(e for n in self.nets for e in n.edges)

    @property
    def active_edges(self) -> tuple[RatsnestEdge, ...]:
        return tuple(e for n in self.nets for e in n.active_edges)

    def net(self, name: str) -> RatsnestNet | None:
        return next((n for n in self.nets if n.name == name), None)

    @property
    def unrouted_by_net(self) -> dict[str, int]:
        """Only nets with airwires (a `--include-routed` export also lists zero-count nets)."""
        return {n.name: n.unrouted_count for n in self.nets if n.unrouted_count}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _node(raw: Any, scale: float) -> RatsnestNode:
    r = raw if isinstance(raw, dict) else {}
    raw_pos = r.get("pos")
    pos: dict[str, Any] = raw_pos if isinstance(raw_pos, dict) else {}
    return RatsnestNode(
        uuid=str(r.get("uuid", "")),
        description=str(r.get("description", "")),
        pos=Position(_num(pos.get("x")) * scale, _num(pos.get("y")) * scale),
        layer=str(r.get("layer", "") or ""),
    )


def parse_ratsnest_json(text: str, *, source: str = "", kicad_version: str = "") -> RatsnestReport:
    """Parse the export. `source` / `kicad_version` fall back to the JSON's own fields; a net's
    `unrouted_count` falls back to its edge count and the totals to the sums (older or hand-made
    files), so the model is consistent whatever the writer put in."""
    data = load_json(text)
    units = str(data.get("coordinate_units", "mm") or "mm")
    scale = unit_factor(units)
    nets: list[RatsnestNet] = []
    for raw_net in data.get("nets", ()) or ():
        if not isinstance(raw_net, dict):
            continue
        name = str(raw_net.get("name", ""))
        edges: list[RatsnestEdge] = []
        for raw_edge in raw_net.get("edges", ()) or ():
            if not isinstance(raw_edge, dict):
                continue
            a, b = _node(raw_edge.get("a"), scale), _node(raw_edge.get("b"), scale)
            length = raw_edge.get("length")
            if length is None:
                length_mm = ((a.pos.x_mm - b.pos.x_mm) ** 2 + (a.pos.y_mm - b.pos.y_mm) ** 2) ** 0.5
            else:
                length_mm = _num(length) * scale
            excluded = raw_edge.get("excluded") is True
            edges.append(RatsnestEdge(net=name, a=a, b=b, length_mm=length_mm, excluded=excluded))
        count = raw_net.get("unrouted_count")
        airline = raw_net.get("airline_length")
        active = [e for e in edges if not e.excluded]
        nets.append(
            RatsnestNet(
                name=name,
                unrouted_count=int(count) if isinstance(count, int) else len(active),
                airline_length_mm=(
                    _num(airline) * scale if airline is not None else sum(e.length_mm for e in active)
                ),
                edges=tuple(edges),
            )
        )
    total = data.get("unrouted_count")
    total_len = data.get("airline_length")
    return RatsnestReport(
        source=source or str(data.get("source", "")),
        kicad_version=kicad_version or str(data.get("kicad_version", "")),
        coordinate_units=units,
        unrouted_count=int(total) if isinstance(total, int) else sum(n.unrouted_count for n in nets),
        airline_length_mm=(
            _num(total_len) * scale if total_len is not None else sum(n.airline_length_mm for n in nets)
        ),
        nets=tuple(nets),
        raw=data,
    )
