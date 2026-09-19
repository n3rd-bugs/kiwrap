"""Connectivity, ratsnest approximation, bounding boxes and hit tests over the headless IPC API.

What the API offers (10.99.0-4083, `board_commands.proto` / `editor_commands.proto`; every claim
below was exercised on the ecc83 demo in scratchpad/ipc-backend/probe2.py and is re-run by
tests/test_ipc_live.py):
- `GetNets` (names; `Net.code` is deprecated), `GetItemsByNet` (board items on nets, by type),
  `GetItemsByNetClass`, `GetNetClassForNets`.
- `GetConnectedItems(items, types)`: the items copper-connected to the sources (pads, tracks,
  vias, zones), i.e. one connectivity cluster. The source item is part of the answer.
- `GetBoundingBox(items, mode)`: boards only. The schematic handler answers "no handler available".
- `HitTest(id, position, tolerance)`: boards and schematics.
- There is NO ratsnest / airwire / unrouted command (no `GetRatsnest`, nothing in `RunAction` that
  returns data, no `RunDRC`; D17 tier 3 lists the upstream candidate). `ratsnest()` below is the
  documented approximation: per net, group the copper items into clusters with
  `GetConnectedItems` (one request per cluster), then connect the clusters with a greedy nearest
  pad-to-pad spanning tree. Cluster count - 1 is exactly KiCad's unrouted-connection count for
  that net; the airwire endpoints are a nearest-anchor guess, not KiCad's own topology.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kiwrap.backends.ipc.models import BBox, Point, to_json
from kiwrap.backends.ipc.units import mm_to_nm, nm_to_mm

__all__ = [
    "Airwire",
    "ConnectedItem",
    "NetConnectivity",
    "Ratsnest",
    "bounding_box",
    "connected_items",
    "hit_test",
    "items_by_net",
    "net_connectivity",
    "ratsnest",
]

_BOARD_KINDS = {
    "Pad": "pad",
    "Track": "track",
    "ArcTrack": "arc",
    "Via": "via",
    "Zone": "zone",
    "FootprintInstance": "footprint",
    "BoardSegment": "shape",
    "BoardArc": "shape",
    "BoardCircle": "shape",
    "BoardRectangle": "shape",
    "BoardPolygon": "shape",
    "BoardText": "text",
}


@dataclass(frozen=True)
class ConnectedItem:
    """One board item as the connectivity commands report it (mm)."""

    id: str
    kind: str
    net: str
    position: Point
    ref: str = ""  # owning footprint reference for pads
    number: str = ""  # pad number

    def to_json(self) -> dict[str, Any]:
        return to_json(self)


@dataclass(frozen=True)
class Airwire:
    net: str
    start: Point
    end: Point
    from_item: str
    to_item: str
    length: float

    def to_json(self) -> dict[str, Any]:
        return to_json(self)


@dataclass(frozen=True)
class NetConnectivity:
    net: str
    anchors: tuple[ConnectedItem, ...]  # pads (and free vias) on the net
    clusters: tuple[tuple[str, ...], ...]  # item ids per copper-connected cluster
    airwires: tuple[Airwire, ...]

    @property
    def unrouted(self) -> int:
        """KiCad's unrouted count for the net: clusters - 1 (0 for a net with one anchor)."""
        return max(0, len(self.clusters) - 1)

    def to_json(self) -> dict[str, Any]:
        data = to_json(self)
        data["unrouted"] = self.unrouted
        return data


@dataclass(frozen=True)
class Ratsnest:
    nets: tuple[NetConnectivity, ...]
    requests: int = 0  # API round trips spent (for chunk budgeting)
    method: str = field(default="GetConnectedItems clusters + nearest-anchor spanning tree")

    @property
    def unrouted(self) -> int:
        return sum(n.unrouted for n in self.nets)

    @property
    def airwires(self) -> tuple[Airwire, ...]:
        return tuple(a for n in self.nets for a in n.airwires)

    def to_json(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "requests": self.requests,
            "unrouted": self.unrouted,
            "nets": [n.to_json() for n in self.nets],
        }


# --- helpers ---------------------------------------------------------------------------------------


def _board_of(source: Any) -> Any:
    return source.board() if hasattr(source, "board") and not hasattr(source, "get_footprints") else source


def _schematic_of(source: Any) -> Any:
    return (
        source.schematic() if hasattr(source, "schematic") and not hasattr(source, "get_symbols") else source
    )


def _position_of(item: Any) -> Point:
    """Anchor point of an item: `position` (pads, vias, footprints), else `start` (tracks, arcs),
    else the first outline point (zones)."""
    p = item.proto
    names = {f.name for f in p.DESCRIPTOR.fields}
    if "position" in names:
        return (nm_to_mm(p.position.x_nm), nm_to_mm(p.position.y_nm))
    if "start" in names:
        return (nm_to_mm(p.start.x_nm), nm_to_mm(p.start.y_nm))
    if "outline" in names:
        for poly in p.outline.polygons:
            for node in poly.outline.nodes:
                if node.HasField("point"):
                    return (nm_to_mm(node.point.x_nm), nm_to_mm(node.point.y_nm))
    return (0.0, 0.0)


def _net_of(item: Any) -> str:
    p = item.proto
    names = {f.name for f in p.DESCRIPTOR.fields}
    if "net" in names:
        return p.net.name
    if "copper_settings" in names and p.HasField("copper_settings"):
        return p.copper_settings.net.name
    return ""


def _connected_item(item: Any, pad_owner: dict[str, tuple[str, str]] | None = None) -> ConnectedItem:
    kind = _BOARD_KINDS.get(type(item).__name__, type(item).__name__.lower())
    ref, number = ("", "")
    if kind == "pad":
        number = item.proto.number
        if pad_owner:
            ref = pad_owner.get(item.proto.id.value, ("", ""))[0]
    return ConnectedItem(
        id=item.proto.id.value,
        kind=kind,
        net=_net_of(item),
        position=_position_of(item),
        ref=ref,
        number=number,
    )


def _pad_owners(board: Any) -> dict[str, tuple[str, str]]:
    """pad KIID -> (footprint ref, pad number), from one GetItems(footprints)."""
    owners: dict[str, tuple[str, str]] = {}
    for fp in board.get_footprints():
        ref = fp.proto.reference_field.text.text.text
        for pad in fp.definition.pads:
            owners[pad.proto.id.value] = (ref, pad.proto.number)
    return owners


def _distance(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# --- public API ------------------------------------------------------------------------------------


def connected_items(source: Any, item_or_id: Any, types: Sequence[int] | None = None) -> list[ConnectedItem]:
    """Everything copper-connected to `item_or_id` (a kipy BoardItem, a KIID, or a KIID string)."""
    from kipy.proto.common.types.base_types_pb2 import KIID

    board = _board_of(source)
    if isinstance(item_or_id, str):
        kiid = KIID()
        kiid.value = item_or_id
        item_or_id = kiid
    owners = _pad_owners(board)
    return [_connected_item(i, owners) for i in board.get_connected_items(item_or_id, types)]


def items_by_net(
    source: Any, nets: str | Iterable[str], types: Sequence[int] | None = None
) -> list[ConnectedItem]:
    """Board items on the named net(s) (`GetItemsByNet`)."""
    board = _board_of(source)
    wanted = {nets} if isinstance(nets, str) else set(nets)
    net_wrappers = [n for n in board.get_nets() if n.name in wanted]
    if not net_wrappers:
        return []
    owners = _pad_owners(board)
    return [_connected_item(i, owners) for i in board.get_items_by_net(net_wrappers, types)]


def net_connectivity(
    source: Any, net: str, *, owners: dict[str, tuple[str, str]] | None = None
) -> NetConnectivity:
    """Clusters + airwires for one net (see the module docstring for the approximation)."""
    from kipy.proto.common.types.enums_pb2 import KiCadObjectType as KOT

    board = _board_of(source)
    owners = owners if owners is not None else _pad_owners(board)
    net_wrappers = [n for n in board.get_nets() if n.name == net]
    if not net_wrappers:
        return NetConnectivity(net=net, anchors=(), clusters=(), airwires=())
    copper_types = [KOT.KOT_PCB_PAD, KOT.KOT_PCB_TRACE, KOT.KOT_PCB_ARC, KOT.KOT_PCB_VIA, KOT.KOT_PCB_ZONE]
    items = list(board.get_items_by_net(net_wrappers, copper_types))
    by_id = {i.proto.id.value: i for i in items}
    anchors = [_connected_item(i, owners) for i in items if type(i).__name__ in ("Pad", "Via")]
    seen: set[str] = set()
    clusters: list[tuple[str, ...]] = []
    for item in items:
        iid = item.proto.id.value
        if iid in seen:
            continue
        cluster = {iid}
        for c in board.get_connected_items(item, copper_types):
            cluster.add(c.proto.id.value)
        seen |= cluster
        clusters.append(tuple(sorted(cluster)))
    airwires = _spanning_airwires(net, clusters, anchors, by_id, owners)
    return NetConnectivity(
        net=net, anchors=tuple(anchors), clusters=tuple(clusters), airwires=tuple(airwires)
    )


def _spanning_airwires(
    net: str,
    clusters: list[tuple[str, ...]],
    anchors: list[ConnectedItem],
    by_id: dict[str, Any],
    owners: dict[str, tuple[str, str]],
) -> list[Airwire]:
    """Greedy nearest-anchor spanning tree between clusters (Prim over cluster anchors)."""
    if len(clusters) < 2:
        return []
    anchor_by_id = {a.id: a for a in anchors}
    cluster_anchors: list[list[ConnectedItem]] = []
    for cluster in clusters:
        chosen = [anchor_by_id[i] for i in cluster if i in anchor_by_id]
        if not chosen:  # a track-only cluster: use its items' start points as anchors
            chosen = [_connected_item(by_id[i], owners) for i in cluster if i in by_id]
        cluster_anchors.append(chosen)
    connected = {0}
    airwires: list[Airwire] = []
    while len(connected) < len(clusters):
        best: tuple[float, int, ConnectedItem, ConnectedItem] | None = None
        for ci in connected:
            for cj in range(len(clusters)):
                if cj in connected:
                    continue
                for a in cluster_anchors[ci]:
                    for b in cluster_anchors[cj]:
                        d = _distance(a.position, b.position)
                        if best is None or d < best[0]:
                            best = (d, cj, a, b)
        if best is None:  # pragma: no cover - every cluster has at least one item
            break
        d, cj, a, b = best
        connected.add(cj)
        airwires.append(
            Airwire(net=net, start=a.position, end=b.position, from_item=a.id, to_item=b.id, length=d)
        )
    return airwires


def ratsnest(source: Any, nets: Iterable[str] | None = None) -> Ratsnest:
    """Ratsnest approximation over every copper net (or `nets`). Skips the unnamed net and
    `unconnected-*` nets (single-pad nets by construction)."""
    board = _board_of(source)
    owners = _pad_owners(board)
    names = [n.name for n in board.get_nets() if n.name and not n.name.startswith("unconnected-")]
    if nets is not None:
        wanted = set(nets)
        names = [n for n in names if n in wanted]
    results = [net_connectivity(board, name, owners=owners) for name in names]
    requests = 2 + sum(1 + len(r.clusters) for r in results)  # GetNets + footprints + per-net items/clusters
    return Ratsnest(nets=tuple(results), requests=requests)


def bounding_box(source: Any, items: Any, *, include_text: bool = False) -> BBox | list[BBox | None] | None:
    """`GetBoundingBox` in mm for one board item or a sequence (boards only; schematics have no
    handler on the nightly)."""
    board = _board_of(source)
    result = board.get_item_bounding_box(items, include_text=include_text)
    if result is None:
        return None
    if isinstance(result, list):
        return [
            BBox(nm_to_mm(b.pos.x), nm_to_mm(b.pos.y), nm_to_mm(b.size.x), nm_to_mm(b.size.y)) if b else None
            for b in result
        ]
    return BBox(
        nm_to_mm(result.pos.x), nm_to_mm(result.pos.y), nm_to_mm(result.size.x), nm_to_mm(result.size.y)
    )


def hit_test(
    source: Any, item: Any, point_mm: Point, tolerance_mm: float = 0.0, *, schematic: bool = False
) -> bool:
    """`HitTest` on a board or schematic item at a millimetre point."""
    from kipy.geometry import Vector2

    editor = _schematic_of(source) if schematic else _board_of(source)
    return bool(
        editor.hit_test(
            item, Vector2.from_xy(mm_to_nm(point_mm[0]), mm_to_nm(point_mm[1])), mm_to_nm(tolerance_mm)
        )
    )
