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
- Stock KiCad (up to 10.99.0-4083) has NO ratsnest / airwire / unrouted command. The
  cluster walk in `ratsnest()` is the documented approximation for it: per net, group the copper
  items into clusters with `GetConnectedItems` (one request per cluster), then connect the
  clusters with a greedy nearest pad-to-pad spanning tree. Cluster count - 1 is exactly KiCad's
  unrouted-connection count for that net; the airwire endpoints are a nearest-anchor guess.
- `GetRatsnest` (docs/UPSTREAM.md G1; fork branch `mr/g1-ratsnest`, `Since: 11.0`) answers with
  KiCad's own airwires: per net the edges of the ratsnest (RN_NET minimum spanning tree, the
  set DRC reports as unconnected_items) with both end items (KIID), the anchor position the
  editor draws from and the copper layer of that anchor. `get_ratsnest()` is its client and
  `ratsnest()` prefers it: one request instead of one per cluster, exact endpoints. The
  installed kicad-python (f79cebd) predates the proto, so the request / response classes are
  built from a descriptor that mirrors `api/proto/board/board_commands.proto` of the branch
  when `kipy.proto.board.board_commands_pb2` lacks them (`ratsnest_messages()`); a server
  without the handler answers AS_UNHANDLED and `ratsnest()` falls back to the cluster walk.
  Presence probe for the capability key `ipc.has_get_ratsnest`: `probe_get_ratsnest()`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kiwrap.backends.ipc.models import BBox, Point, to_json
from kiwrap.backends.ipc.readers import layer_name
from kiwrap.backends.ipc.units import mm_to_nm, nm_to_mm

__all__ = [
    "Airwire",
    "ConnectedItem",
    "NetConnectivity",
    "Ratsnest",
    "RatsnestEdge",
    "RatsnestNode",
    "bounding_box",
    "connected_items",
    "get_ratsnest",
    "hit_test",
    "items_by_net",
    "net_connectivity",
    "probe_get_ratsnest",
    "ratsnest",
    "ratsnest_messages",
]

CAPABILITY = "ipc.has_get_ratsnest"

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
class RatsnestNode:
    """One end of a KiCad airwire: the item, the anchor point the editor draws from (mm) and
    the copper layer of that anchor (for a through item the layer it shares with the other end,
    else its top-most copper layer)."""

    id: str
    position: Point
    layer: str

    def to_json(self) -> dict[str, Any]:
        return to_json(self)


@dataclass(frozen=True)
class RatsnestEdge:
    net: str
    a: RatsnestNode
    b: RatsnestNode
    length: float  # mm, straight line
    excluded: bool = False  # hidden by a DRC unconnected_items exclusion (not drawn, not counted)

    def to_json(self) -> dict[str, Any]:
        return to_json(self)


@dataclass(frozen=True)
class NetConnectivity:
    net: str
    anchors: tuple[ConnectedItem, ...]  # pads (and free vias) on the net
    clusters: tuple[tuple[str, ...], ...]  # item ids per copper-connected cluster
    airwires: tuple[Airwire, ...]
    unrouted_count: int | None = None  # KiCad's own count (GetRatsnest); None = derive from clusters
    edges: tuple[RatsnestEdge, ...] = ()  # KiCad's airwires with layers (GetRatsnest only), excluded ones too

    @property
    def unrouted(self) -> int:
        """KiCad's unrouted count for the net: the GetRatsnest count of edges that are not excluded
        (what the editor shows) when KiCad answered, else clusters - 1 (0 for a net with one anchor)."""
        if self.unrouted_count is not None:
            return self.unrouted_count
        return max(0, len(self.clusters) - 1)

    def to_json(self) -> dict[str, Any]:
        data = to_json(self)
        data["unrouted"] = self.unrouted
        return data


CLUSTER_METHOD = "GetConnectedItems clusters + nearest-anchor spanning tree"
KICAD_METHOD = "GetRatsnest"


@dataclass(frozen=True)
class Ratsnest:
    nets: tuple[NetConnectivity, ...]
    requests: int = 0  # API round trips spent (for chunk budgeting)
    method: str = field(default=CLUSTER_METHOD)

    @property
    def exact(self) -> bool:
        """True when KiCad computed the airwires (endpoints are the editor's, not guesses)."""
        return self.method == KICAD_METHOD

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


# --- GetRatsnest (KiCad's own airwires; docs/UPSTREAM.md G1) ------------------------------------------

#: The fork's `api/proto/board/board_commands.proto` additions, as a FileDescriptorProto builder.
#: Field numbers and names must stay identical to the branch (`mr/g1-ratsnest`); a regenerated
#: kicad-python makes this fallback unused (`ratsnest_messages` prefers the generated classes).
_RATSNEST_PROTO_FILE = "board/board_commands_ratsnest.proto"


def _ratsnest_file_descriptor() -> Any:
    from google.protobuf import descriptor_pb2

    fd = descriptor_pb2.FileDescriptorProto()
    fd.name = _RATSNEST_PROTO_FILE
    fd.package = "kiapi.board.commands"
    fd.syntax = "proto3"
    fd.dependency.extend(["common/types/base_types.proto", "board/board_types.proto"])
    fdp = descriptor_pb2.FieldDescriptorProto

    def field(msg: Any, name: str, number: int, type_name: str, *, repeated: bool = False) -> None:
        f = msg.field.add()
        f.name, f.number = name, number
        if type_name == "bool":
            f.type = fdp.TYPE_BOOL
        else:
            f.type = fdp.TYPE_ENUM if type_name == ".kiapi.board.types.BoardLayer" else fdp.TYPE_MESSAGE
            f.type_name = type_name
        f.label = fdp.LABEL_REPEATED if repeated else fdp.LABEL_OPTIONAL

    node = fd.message_type.add()
    node.name = "RatsnestNode"
    field(node, "id", 1, ".kiapi.common.types.KIID")
    field(node, "position", 2, ".kiapi.common.types.Vector2")
    field(node, "layer", 3, ".kiapi.board.types.BoardLayer")
    edge = fd.message_type.add()
    edge.name = "RatsnestEdge"
    field(edge, "a", 1, ".kiapi.board.commands.RatsnestNode")
    field(edge, "b", 2, ".kiapi.board.commands.RatsnestNode")
    field(edge, "length", 3, ".kiapi.common.types.Distance")
    field(edge, "excluded", 4, "bool")
    net = fd.message_type.add()
    net.name = "RatsnestNet"
    field(net, "net", 1, ".kiapi.board.types.Net")
    field(net, "edges", 2, ".kiapi.board.commands.RatsnestEdge", repeated=True)
    field(net, "airline_length", 3, ".kiapi.common.types.Distance")
    req = fd.message_type.add()
    req.name = "GetRatsnest"
    field(req, "board", 1, ".kiapi.common.types.DocumentSpecifier")
    field(req, "nets", 2, ".kiapi.board.types.Net", repeated=True)
    resp = fd.message_type.add()
    resp.name = "GetRatsnestResponse"
    field(resp, "nets", 1, ".kiapi.board.commands.RatsnestNet", repeated=True)
    return fd


_RATSNEST_CLASSES: tuple[type, type] | None = None


def ratsnest_messages() -> tuple[type, type]:
    """`(GetRatsnest, GetRatsnestResponse)` message classes: kicad-python's generated ones when
    its protos know the command, else classes built from the descriptor above in kipy's own
    descriptor pool (so `Any.Pack` yields the type url the fork's handler is registered for)."""
    global _RATSNEST_CLASSES
    if _RATSNEST_CLASSES is not None:
        return _RATSNEST_CLASSES
    from kipy.proto.board import board_commands_pb2 as commands

    generated: Any = commands  # the pinned kicad-python (f79cebd) predates the proto (mypy)
    if hasattr(generated, "GetRatsnest"):
        _RATSNEST_CLASSES = (generated.GetRatsnest, generated.GetRatsnestResponse)
        return _RATSNEST_CLASSES
    from google.protobuf import message_factory

    pool = commands.DESCRIPTOR.pool
    try:
        file_desc = pool.FindFileByName(_RATSNEST_PROTO_FILE)
    except KeyError:
        pool.Add(_ratsnest_file_descriptor())
        file_desc = pool.FindFileByName(_RATSNEST_PROTO_FILE)
    classes = message_factory.GetMessageClassesForFiles([file_desc.name], pool)
    _RATSNEST_CLASSES = (
        classes["kiapi.board.commands.GetRatsnest"],
        classes["kiapi.board.commands.GetRatsnestResponse"],
    )
    return _RATSNEST_CLASSES


def _send_get_ratsnest(source: Any, nets: Iterable[str] | None) -> Any:
    """Raw `GetRatsnestResponse` (kipy `ApiError` when the server has no handler; `IpcError`
    when `source` is not a kipy board with a document and a client, e.g. a test double)."""
    from kiwrap.backends.ipc.sessions import IpcError

    board = _board_of(source)
    if getattr(board, "_doc", None) is None or getattr(board, "_kicad", None) is None:
        raise IpcError("GetRatsnest needs a kipy Board bound to a document and a client")
    request_cls, response_cls = ratsnest_messages()
    request = request_cls()
    request.board.CopyFrom(board._doc)
    for name in nets or ():
        request.nets.add().name = name
    return board._kicad.send(request, response_cls)


def _unhandled(exc: Exception) -> bool:
    from kipy.proto.common import ApiStatusCode

    code = getattr(exc, "code", None)
    return code in (ApiStatusCode.AS_UNHANDLED, ApiStatusCode.AS_UNIMPLEMENTED) or "no handler" in str(exc)


def _refine(source: Any, present: bool) -> None:
    """Record the presence probe's outcome in the session's capability record (`ipc.has_get_ratsnest`,
    docs/UPSTREAM.md §3.3) when `source` is a `HeadlessSession`; never lets a cache problem
    surface into the query."""
    caps = getattr(source, "capabilities", None) if hasattr(source, "board") else None
    refine = getattr(caps, "refine", None)
    if callable(refine):
        with contextlib.suppress(Exception):
            refine(CAPABILITY, present)


def probe_get_ratsnest(source: Any) -> bool:
    """Presence probe for `ipc.has_get_ratsnest`: one request against the open board; True when a
    handler answered (AS_OK), False when the server has none (AS_UNHANDLED / AS_UNIMPLEMENTED).
    Any other failure propagates (the probe must not mistake a broken session for absence). The
    outcome is recorded in the session's capability record."""
    from kipy.errors import ApiError

    try:
        _send_get_ratsnest(source, None)
    except ApiError as exc:
        if _unhandled(exc):
            _refine(source, False)
            return False
        raise
    _refine(source, True)
    return True


def get_ratsnest(source: Any, nets: Iterable[str] | None = None) -> Ratsnest:
    """KiCad's ratsnest over `GetRatsnest` (mm). Every net that still has airwires (or `nets`).
    Raises `IpcError` (E_UNSUPPORTED) when the connected server has no handler.

    Two more requests join the endpoint KIIDs to pads (`GetItems` footprints) and item kinds
    (`GetItemsById`) so the result carries the same `ConnectedItem` anchors the cluster walk does;
    `NetConnectivity.clusters` stays empty (KiCad reports edges, not clusters)."""
    from kipy.errors import ApiError

    from kiwrap.backends.ipc.sessions import IpcError

    board = _board_of(source)
    try:
        response = _send_get_ratsnest(board, nets)
    except ApiError as exc:
        if _unhandled(exc):
            raise IpcError(
                "the connected KiCad has no GetRatsnest handler",
                why="GetRatsnest is a kiwrap fork addition (docs/UPSTREAM.md G1) not yet in this build",
                fix="ratsnest() falls back to the GetConnectedItems cluster walk",
            ) from exc
        raise
    owners = _pad_owners(board)
    ids = sorted({node.id.value for net in response.nets for e in net.edges for node in (e.a, e.b)})
    by_id: dict[str, Any] = {}
    if ids:
        from kipy.proto.common.types.base_types_pb2 import KIID

        for item in board.get_items_by_id([KIID(value=i) for i in ids]):
            by_id[item.proto.id.value] = item
    results: list[NetConnectivity] = []
    for net in response.nets:
        name = net.net.name
        anchors: dict[str, ConnectedItem] = {}
        edges: list[RatsnestEdge] = []
        airwires: list[Airwire] = []
        for e in net.edges:
            nodes = []
            for node in (e.a, e.b):
                pos = (nm_to_mm(node.position.x_nm), nm_to_mm(node.position.y_nm))
                nodes.append(RatsnestNode(id=node.id.value, position=pos, layer=layer_name(node.layer)))
                if node.id.value not in anchors:
                    item = by_id.get(node.id.value)
                    if item is not None:
                        ci = _connected_item(item, owners)
                        anchors[node.id.value] = ConnectedItem(
                            ci.id, ci.kind, ci.net or name, pos, ci.ref, ci.number
                        )
                    else:
                        anchors[node.id.value] = ConnectedItem(node.id.value, "item", name, pos)
            length = nm_to_mm(e.length.value_nm)
            edges.append(RatsnestEdge(net=name, a=nodes[0], b=nodes[1], length=length, excluded=e.excluded))
            if e.excluded:
                continue
            airwires.append(
                Airwire(
                    net=name,
                    start=nodes[0].position,
                    end=nodes[1].position,
                    from_item=nodes[0].id,
                    to_item=nodes[1].id,
                    length=length,
                )
            )
        results.append(
            NetConnectivity(
                net=name,
                anchors=tuple(anchors.values()),
                clusters=(),
                airwires=tuple(airwires),
                unrouted_count=len(airwires),
                edges=tuple(edges),
            )
        )
    return Ratsnest(nets=tuple(results), requests=2 + (1 if ids else 0), method=KICAD_METHOD)


def ratsnest(source: Any, nets: Iterable[str] | None = None, *, kicad: bool | None = None) -> Ratsnest:
    """The board's ratsnest over IPC: KiCad's own airwires through `GetRatsnest` when the server
    handles it (`kicad=None`, the default, tries it first; `True` requires it), else the cluster
    approximation over every copper net (or `nets`; `kicad=False` skips the attempt). The
    approximation skips the unnamed net and `unconnected-*` nets (single-pad nets by
    construction)."""
    from kiwrap.backends.ipc.sessions import IpcError

    if kicad is not False:
        try:
            result = get_ratsnest(source, nets)
        except IpcError:
            _refine(source, False)
            if kicad:
                raise
        else:
            _refine(source, True)
            return result
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
