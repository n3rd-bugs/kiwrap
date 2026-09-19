"""`read_board(session)` / `read_schematic(session)`: structured, millimetre models over the IPC API.

Everything is read through kipy's `Board` / `Schematic` wrappers (one `GetItems` per object type,
`GetNets`, `GetBoardDesignRules`, `GetSchematicNetlist`, `GetSchematicHierarchy`, `GetBoundingBox`
per footprint, `GetPadShapeAsPolygon` on request) and converted from the protobuf messages into
`models.*` dataclasses. Units: nm -> mm through `units.nm_to_mm` (exact); angles are degrees.

Filters (`refs`, `bbox`, `nets`) are applied client-side after the fetch: the API's `GetItems` has
no spatial or reference filter (verified: `board_commands.proto` / `editor_commands.proto`), and
`GetItemsByNet` only covers board items. A filtered model records what was applied in `.filters`.

Verified against the 10.99 nightly and the ecc83 demo (tests/test_ipc_live.py):
- ecc83 `C1` at (141.605, 99.695) mm, rotation 90, F.Cu, 2 PTH pads; 15 footprints, 33 pads,
  1 copper zone on B.Cu, 4 Edge.Cuts segments, nets by name (`Net.code` is deprecated).
- Schematic pins are NOT items: `GetItems(KOT_SCH_PIN)` answers "none of the requested types are
  valid for a Schematic object". Pins come from `SchematicSymbolInstance.definition.items`
  (`SchematicSymbolChild` with a `SchematicPin` in library coordinates) and are placed on the sheet
  with the symbol's `SchematicSymbolTransform`, reproducing KiCad's `SCH_SYMBOL::SetOrientation`
  matrix composition (`symbol_transform` below; checked against wire endpoints of the same net).
- `GetSchematicNetlist` lists KIIDs of pins, wires, junctions (and labels) per sheet; the netlist
  model resolves each KIID to its kind and, for pins, to `(ref, number)`.
- `GetBoundingBox` has no schematic handler ("no handler available"): symbol boxes are not
  available over IPC; `HitTest` is.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Sequence
from typing import Any

from kiwrap.backends.ipc.models import (
    BBox,
    BoardModel,
    DesignRulesSummary,
    FootprintModel,
    JunctionModel,
    LabelModel,
    LayerInfo,
    NetItemRef,
    NetModel,
    NoConnectModel,
    PadModel,
    PinModel,
    Point,
    SchematicModel,
    SchematicNetModel,
    ShapeModel,
    SheetInfo,
    SheetSymbolModel,
    SymbolModel,
    TrackModel,
    ViaModel,
    WireModel,
    ZoneModel,
)
from kiwrap.backends.ipc.units import nm_to_mm

__all__ = [
    "BoardFilters",
    "layer_name",
    "read_board",
    "read_schematic",
    "symbol_transform",
    "transform_point",
]


# --- small proto helpers ---------------------------------------------------------------------------


def _pt(v: Any) -> Point:
    return (nm_to_mm(v.x_nm), nm_to_mm(v.y_nm))


def _dist(d: Any) -> float:
    return nm_to_mm(d.value_nm)


def _enum_name(enum: Any, value: int, prefix: str) -> str:
    try:
        name = enum.Name(value)
    except ValueError:
        return "unknown"
    return name[len(prefix) :].lower() if name.startswith(prefix) else name.lower()


#: proto `ElectricalPinType` names (without `EPT_`, lower-cased) whose spelling differs from the
#: KiCad file token the file writer, kw_lib_info and kw_sch_stats report; everything else
#: (input, output, bidirectional, passive, free, unspecified, open_collector, open_emitter,
#: no_connect) is spelled alike
_PIN_TYPE_TOKENS = {"tristate": "tri_state", "power_input": "power_in", "power_output": "power_out"}


def pin_type_token(value: int) -> str:
    """`ElectricalPinType` -> the KiCad file token (`power_in`, `tri_state`, ...), so receipts are
    provider-independent (reviewer finding, 2026-09-20)."""
    from kipy.proto.common.types.base_types_pb2 import ElectricalPinType

    name = _enum_name(ElectricalPinType, value, "EPT_")
    return _PIN_TYPE_TOKENS.get(name, name)


def layer_name(value: int) -> str:
    """`BoardLayer` enum -> KiCad's canonical layer name: `BL_F_Cu` -> `F.Cu`, `BL_In1_Cu` -> `In1.Cu`,
    `BL_Edge_Cuts` -> `Edge.Cuts`, `BL_User_1` -> `User.1`, `BL_Margin` -> `Margin`."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    try:
        raw = BoardLayer.Name(value)
    except ValueError:
        return f"layer_{value}"
    raw = raw.removeprefix("BL_")
    return raw.replace("_", ".", 1) if "_" in raw else raw


def _is_copper(value: int) -> bool:
    return layer_name(value).endswith(".Cu")


def _polyline_points(poly: Any) -> tuple[Point, ...]:
    """`PolyLine` nodes -> points (an arc node contributes its start/mid/end)."""
    pts: list[Point] = []
    for node in poly.nodes:
        if node.HasField("arc"):
            pts += [_pt(node.arc.start), _pt(node.arc.mid), _pt(node.arc.end)]
        else:
            pts.append(_pt(node.point))
    return tuple(pts)


def _polyset_outlines(polyset: Any) -> tuple[tuple[Point, ...], ...]:
    return tuple(_polyline_points(p.outline) for p in polyset.polygons)


def _sheet_path(sp: Any) -> str:
    return sp.path_human_readable or "/".join(k.value for k in sp.path)


# --- board -----------------------------------------------------------------------------------------


class BoardFilters:
    """Client-side chunking filters for `read_board`."""

    def __init__(
        self,
        refs: Iterable[str] | None = None,
        bbox: BBox | tuple[float, float, float, float] | None = None,
        nets: Iterable[str] | None = None,
    ):
        self.refs = frozenset(refs) if refs is not None else None
        self.bbox = BBox(*bbox) if isinstance(bbox, tuple) else bbox
        self.nets = frozenset(nets) if nets is not None else None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.refs is not None:
            out["refs"] = sorted(self.refs)
        if self.bbox is not None:
            out["bbox"] = self.bbox.to_json()
        if self.nets is not None:
            out["nets"] = sorted(self.nets)
        return out

    def keep_net(self, net: str) -> bool:
        return self.nets is None or net in self.nets

    def keep_point(self, p: Point) -> bool:
        return self.bbox is None or self.bbox.contains(p)

    def keep_box(self, b: BBox | None, fallback: Point) -> bool:
        if self.bbox is None:
            return True
        return self.bbox.intersects(b) if b is not None else self.bbox.contains(fallback)


def _enabled_only(names: Iterable[str], enabled: frozenset[str] | None) -> tuple[str, ...]:
    """Layer names restricted to the board's enabled layers. The API reports a padstack's or via's
    full layer SET (`F.Cu` .. `In30.Cu` .. `B.Cu` on a two-layer board, verified on ecc83 via
    GetItems); KiCad's file writer keeps `*.Cu` for the same pad, and the file reader expands it to
    the board's copper layers, so this is what makes both providers of kw_pcb_get agree."""
    out = tuple(names)
    if enabled is None:
        return out
    return tuple(n for n in out if n in enabled)


def _pad_model(pad: Any, polygon: tuple[Point, ...] = (), enabled: frozenset[str] | None = None) -> PadModel:
    from kipy.proto.board.board_types_pb2 import PadStackShape, PadType

    stack = pad.pad_stack
    shape, size, rounding = "unknown", (0.0, 0.0), 0.0
    if stack.copper_layers:
        layer0 = stack.copper_layers[0]
        shape = _enum_name(PadStackShape, layer0.shape, "PSS_")
        size = (nm_to_mm(layer0.size.x_nm), nm_to_mm(layer0.size.y_nm))
        rounding = float(layer0.corner_rounding_ratio)
    drill = None
    if stack.HasField("drill") and (stack.drill.diameter.x_nm or stack.drill.diameter.y_nm):
        drill = (nm_to_mm(stack.drill.diameter.x_nm), nm_to_mm(stack.drill.diameter.y_nm))
    return PadModel(
        id=pad.id.value,
        number=pad.number,
        net=pad.net.name,
        type=_enum_name(PadType, pad.type, "PT_"),
        position=_pt(pad.position),
        shape=shape,
        size=size,
        rotation=float(stack.angle.value_degrees),
        corner_rounding_ratio=rounding,
        drill=drill,
        layers=_enabled_only((layer_name(layer) for layer in stack.layers), enabled),
        polygon=polygon,
        pin_name=pad.symbol_pin.name,
        pin_type=pin_type_token(pad.symbol_pin.type) if pad.symbol_pin.type else "",
    )


def _footprint_pads(fp_proto: Any) -> list[Any]:
    from kipy.proto.board.board_types_pb2 import Pad

    pads = []
    for item in fp_proto.definition.items:
        if item.type_url.endswith("kiapi.board.types.Pad"):
            pad = Pad()
            item.Unpack(pad)
            pads.append(pad)
    return pads


_COURTYARD_LAYERS = frozenset({"F.CrtYd", "B.CrtYd"})


def _footprint_courtyard_box(fp_proto: Any) -> BBox | None:
    """Extent of the footprint's `definition.items` graphic shapes on F.CrtYd / B.CrtYd. The API
    serialises footprint shapes in ABSOLUTE board coordinates (verified 2026-09-19 on the
    nightly: ecc83 R1's four layer-50 segments reproduce the file reader's courtyard box
    [134.771, 106.9, 137.771, 116.62] exactly; scratchpad reviewer-ergonomics/probe_crtyd.py)."""
    from kipy.proto.board.board_types_pb2 import BoardGraphicShape

    boxes: list[BBox] = []
    for item in fp_proto.definition.items:
        if not item.type_url.endswith("kiapi.board.types.BoardGraphicShape"):
            continue
        shape = BoardGraphicShape()
        item.Unpack(shape)
        layer = layer_name(shape.layer)
        if layer not in _COURTYARD_LAYERS:
            continue
        box = _shape_bbox(_shape_model(shape.shape, layer, "", shape.id.value))
        if box is not None:
            boxes.append(box)
    return BBox.union(boxes)


def _footprint_model(
    fp: Any,
    bbox: BBox | None,
    pads: Sequence[PadModel],
    *,
    bbox_with_text: BBox | None = None,
    courtyard_bbox: BBox | None = None,
) -> FootprintModel:
    from kipy.proto.board.board_types_pb2 import FootprintMountingStyle
    from kipy.proto.common.types.base_types_pb2 import LockedState

    p = fp.proto
    lib = p.definition.id
    attrs = p.attributes
    layer = layer_name(p.layer)
    return FootprintModel(
        id=p.id.value,
        ref=p.reference_field.text.text.text,
        value=p.value_field.text.text.text,
        fpid=f"{lib.library_nickname}:{lib.entry_name}" if lib.library_nickname else lib.entry_name,
        position=_pt(p.position),
        rotation=float(p.orientation.value_degrees),
        layer=layer,
        side="back" if layer.startswith("B.") else "front",
        locked=p.locked == LockedState.LS_LOCKED,
        mounting=_enum_name(FootprintMountingStyle, attrs.mounting_style, "FMS_"),
        dnp=attrs.do_not_populate,
        exclude_from_bom=attrs.exclude_from_bill_of_materials,
        exclude_from_pos=attrs.exclude_from_position_files,
        sheet_path=_sheet_path(p.symbol_path),
        bbox=bbox,
        pads=tuple(pads),
        bbox_with_text=bbox_with_text,
        courtyard_bbox=courtyard_bbox,
    )


def _shape_model(shape_proto: Any, layer: str, net: str, item_id: str) -> ShapeModel:
    g = shape_proto
    width = _dist(g.attributes.stroke.width) if g.HasField("attributes") else 0.0
    kind = g.WhichOneof("geometry") or "unknown"
    pts: tuple[Point, ...] = ()
    radius: float | None = None
    if kind == "segment":
        pts = (_pt(g.segment.start), _pt(g.segment.end))
    elif kind == "rectangle":
        pts = (_pt(g.rectangle.top_left), _pt(g.rectangle.bottom_right))
    elif kind == "arc":
        pts = (_pt(g.arc.start), _pt(g.arc.mid), _pt(g.arc.end))
    elif kind == "circle":
        c, r = _pt(g.circle.center), _pt(g.circle.radius_point)
        pts = (c, r)
        radius = ((r[0] - c[0]) ** 2 + (r[1] - c[1]) ** 2) ** 0.5
    elif kind == "polygon":
        outlines = _polyset_outlines(g.polygon)
        pts = outlines[0] if outlines else ()
    elif kind == "bezier":
        pts = (_pt(g.bezier.start), _pt(g.bezier.control1), _pt(g.bezier.control2), _pt(g.bezier.end))
    elif kind in ("ellipse", "ellipse_arc"):
        e = getattr(g, kind)
        pts = (_pt(e.center),) if e.HasField("center") else ()
    return ShapeModel(id=item_id, kind=kind, layer=layer, points=pts, width=width, net=net, radius=radius)


def _shape_bbox(s: ShapeModel) -> BBox | None:
    if not s.points:
        return None
    if s.kind == "circle" and s.radius is not None:
        cx, cy = s.points[0]
        return BBox(cx - s.radius, cy - s.radius, 2 * s.radius, 2 * s.radius)
    xs = [p[0] for p in s.points]
    ys = [p[1] for p in s.points]
    return BBox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _design_rules(board: Any) -> DesignRulesSummary | None:
    from kipy.proto.board import board_commands_pb2 as bc

    cmd = bc.GetBoardDesignRules()
    cmd.board.CopyFrom(board.document)
    try:
        resp = board.client.send(cmd, bc.BoardDesignRulesResponse)
    except Exception:
        return None
    c = resp.rules.constraints
    return DesignRulesSummary(
        min_clearance=_dist(c.min_clearance),
        min_track_width=_dist(c.min_track_width),
        min_connection_width=_dist(c.min_connection_width),
        min_via_size=_dist(c.min_via_size),
        min_via_annular_width=_dist(c.min_via_annular_width),
        min_through_drill=_dist(c.min_through_drill),
        min_microvia_size=_dist(c.min_microvia_size),
        min_microvia_drill=_dist(c.min_microvia_drill),
        copper_edge_clearance=_dist(c.copper_edge_clearance),
        hole_clearance=_dist(c.hole_clearance),
        hole_to_hole_min=_dist(c.hole_to_hole_min),
        silk_clearance=_dist(c.silk_clearance),
    )


def _board_of(source: Any) -> Any:
    """Accept a `HeadlessSession` (uses `.board()`) or a kipy `Board`."""
    return source.board() if hasattr(source, "board") and not hasattr(source, "get_footprints") else source


def read_board(
    source: Any,
    *,
    refs: Iterable[str] | None = None,
    bbox: BBox | tuple[float, float, float, float] | None = None,
    nets: Iterable[str] | None = None,
    include_pads: bool = True,
    pad_polygons: bool = False,
    footprint_bboxes: bool = True,
    include_zone_outlines: bool = True,
) -> BoardModel:
    """Read the open board into a `BoardModel` (mm). `source` is a `HeadlessSession` or a kipy `Board`.

    `refs` keeps only those footprints; `bbox` keeps footprints whose bounding box intersects it and
    tracks/vias/zones/shapes with a point inside it; `nets` keeps footprints having a pad on one of
    the nets and tracks/vias/zones on them. `pad_polygons=True` adds each pad's copper outline from
    `GetPadShapeAsPolygon` (one request per footprint, absolute mm). `footprint_bboxes=True` asks
    `GetBoundingBox` twice (batched over every footprint): `BBM_ITEM_ONLY` gives `bbox` (pads +
    graphics, what placement spacing needs), `BBM_ITEM_AND_CHILD_TEXT` gives `bbox_with_text`
    (the editor's box, reference/value text included - 6.4 mm wide for ecc83 R1 against 3.05 mm
    without text; reviewer finding 2026-09-19). `courtyard_bbox` comes from the definition's
    F.CrtYd / B.CrtYd shapes (`_footprint_courtyard_box`).
    """
    from kipy.proto.board.board_types_pb2 import ViaType, ZoneType
    from kipy.proto.common.types.base_types_pb2 import LockedState

    board = _board_of(source)
    filters = BoardFilters(refs, bbox, nets)
    doc = board.document
    project = doc.project.name if doc.HasField("project") else ""

    enabled = list(board.get_enabled_layers())
    layers = []
    for layer_id in enabled:
        user = ""
        with contextlib.suppress(Exception):
            user = board.get_layer_name(layer_id)
        layers.append(
            LayerInfo(
                id=int(layer_id), name=layer_name(layer_id), user_name=user, copper=_is_copper(layer_id)
            )
        )
    try:
        copper_count = int(board.get_copper_layer_count())
    except Exception:
        copper_count = sum(1 for layer in layers if layer.copper)
    enabled_names: frozenset[str] | None = frozenset(layer.name for layer in layers) or None

    net_wrappers = board.get_nets()
    netclasses: dict[str, str] = {}
    with contextlib.suppress(Exception):
        netclasses = {name: nc.name for name, nc in board.get_netclass_for_nets(net_wrappers).items()}
    net_models = tuple(
        NetModel(name=n.name, netclass=netclasses.get(n.name, "")) for n in net_wrappers if n.name != ""
    )

    footprints: list[FootprintModel] = []
    all_fps = list(board.get_footprints())
    boxes: dict[str, BBox] = {}
    text_boxes: dict[str, BBox] = {}
    if footprint_bboxes and all_fps:
        # One batched GetBoundingBox per mode: item only (placement box) and item + child text.
        for include_text, target in ((False, boxes), (True, text_boxes)):
            with contextlib.suppress(Exception):
                for fp, box in zip(
                    all_fps, board.get_item_bounding_box(all_fps, include_text=include_text), strict=False
                ):
                    if box is not None:
                        target[fp.proto.id.value] = _box_from_kipy(box)
    for fp in all_fps:
        p = fp.proto
        ref = p.reference_field.text.text.text
        if filters.refs is not None and ref not in filters.refs:
            continue
        pad_protos = _footprint_pads(p) if include_pads or filters.nets is not None else []
        if filters.nets is not None and not any(filters.keep_net(pad.net.name) for pad in pad_protos):
            continue
        fp_bbox: BBox | None = boxes.get(p.id.value)
        if not filters.keep_box(fp_bbox, _pt(p.position)):
            continue
        polygons: dict[str, tuple[Point, ...]] = {}
        if include_pads and pad_polygons and pad_protos:
            with contextlib.suppress(Exception):
                polys = board.get_pad_shapes_as_polygons(fp.definition.pads)
                for pad_wrapper, poly in zip(fp.definition.pads, polys, strict=False):
                    polygons[pad_wrapper.id.value] = _polyline_points(poly.proto.outline)
        pads = (
            [_pad_model(pad, polygons.get(pad.id.value, ()), enabled_names) for pad in pad_protos]
            if include_pads
            else []
        )
        courtyard: BBox | None = None
        with contextlib.suppress(Exception):
            courtyard = _footprint_courtyard_box(p)
        footprints.append(
            _footprint_model(
                fp, fp_bbox, pads, bbox_with_text=text_boxes.get(p.id.value), courtyard_bbox=courtyard
            )
        )

    tracks: list[TrackModel] = []
    for tr in board.get_tracks():
        p = tr.proto
        is_arc = p.DESCRIPTOR.name == "Arc"  # `Arc` has `mid`; `Track` does not
        model = TrackModel(
            id=p.id.value,
            kind="arc" if is_arc else "track",
            start=_pt(p.start),
            end=_pt(p.end),
            width=_dist(p.width),
            layer=layer_name(p.layer),
            net=p.net.name,
            mid=_pt(p.mid) if is_arc else None,
            locked=p.locked == LockedState.LS_LOCKED,
        )
        if filters.keep_net(model.net) and (filters.keep_point(model.start) or filters.keep_point(model.end)):
            tracks.append(model)

    vias: list[ViaModel] = []
    for via in board.get_vias():
        p = via.proto
        stack = p.pad_stack
        diameter = 0.0
        if stack.copper_layers:
            diameter = nm_to_mm(stack.copper_layers[0].size.x_nm)  # vias are round: x == y
        via_model = ViaModel(
            id=p.id.value,
            position=_pt(p.position),
            type=_enum_name(ViaType, p.type, "VT_"),
            net=p.net.name,
            diameter=diameter,
            drill=nm_to_mm(stack.drill.diameter.x_nm),
            layers=_enabled_only((layer_name(layer) for layer in stack.layers), enabled_names),
            locked=p.locked == LockedState.LS_LOCKED,
        )
        if filters.keep_net(via_model.net) and filters.keep_point(via_model.position):
            vias.append(via_model)

    zones: list[ZoneModel] = []
    for zone in board.get_zones():
        p = zone.proto
        net = p.copper_settings.net.name if p.HasField("copper_settings") else ""
        outline = _polyset_outlines(p.outline) if include_zone_outlines else ()
        zone_model = ZoneModel(
            id=p.id.value,
            name=p.name,
            type=_enum_name(ZoneType, p.type, "ZT_"),
            net=net,
            layers=tuple(layer_name(layer) for layer in p.layers),
            filled=bool(p.filled),
            priority=int(p.priority),
            outline=outline,
            fill_polygons={layer_name(fpz.layer): len(fpz.shapes.polygons) for fpz in p.filled_polygons},
        )
        if not filters.keep_net(net):
            continue
        if (
            filters.bbox is not None
            and outline
            and not any(filters.keep_point(pt) for o in outline for pt in o)
        ):
            continue
        zones.append(zone_model)

    shapes: list[ShapeModel] = []
    for sh in board.get_shapes():
        p = sh.proto
        shape_model = _shape_model(p.shape, layer_name(p.layer), p.net.name, p.id.value)
        if filters.bbox is not None and not filters.keep_box(
            _shape_bbox(shape_model), shape_model.points[0] if shape_model.points else (0.0, 0.0)
        ):
            continue
        shapes.append(shape_model)

    outline_boxes = [b for b in (_shape_bbox(s) for s in shapes if s.layer == "Edge.Cuts") if b is not None]
    item_boxes = [f.bbox for f in footprints if f.bbox is not None]
    item_boxes += [
        BBox(
            min(t.start[0], t.end[0]),
            min(t.start[1], t.end[1]),
            abs(t.end[0] - t.start[0]),
            abs(t.end[1] - t.start[1]),
        )
        for t in tracks
    ]
    item_boxes += outline_boxes

    return BoardModel(
        path=doc.board_filename,
        name=board.name,
        project=project,
        copper_layer_count=copper_count,
        layers=tuple(layers),
        nets=net_models,
        footprints=tuple(footprints),
        tracks=tuple(tracks),
        vias=tuple(vias),
        zones=tuple(zones),
        shapes=tuple(shapes),
        outline_bbox=BBox.union(outline_boxes),
        items_bbox=BBox.union(item_boxes),
        design_rules=_design_rules(board),
        filters=filters.to_json(),
    )


def _box_from_kipy(box: Any) -> BBox:
    """kipy `Box2` (pos/size Vector2 in nm) -> BBox in mm."""
    return BBox(nm_to_mm(box.pos.x), nm_to_mm(box.pos.y), nm_to_mm(box.size.x), nm_to_mm(box.size.y))


# --- schematic -------------------------------------------------------------------------------------

_ROT_CCW = (0, 1, -1, 0)  # SCH_SYMBOL::SetOrientation(SYM_ROTATE_COUNTERCLOCKWISE) temp matrix
_MIRROR_X = (1, 0, 0, -1)  # SYM_MIRROR_X temp matrix
_MIRROR_Y = (-1, 0, 0, 1)  # SYM_MIRROR_Y temp matrix
#: SYM_ORIENT_0. KiCad's own base is (1, 0, 0, -1) (library Y-up -> sheet Y-down), but the API
#: already serialises `SchematicPin.position` in the sheet's Y-down convention: with (1, 0, 0, -1)
#: the 7 pins of ecc83's unrotated U1 units land off their wires, with (1, 0, 0, 1) all 42 pins of
#: ecc83 coincide with a wire end / junction / no-connect (scratchpad/ipc-backend/probe6.py, and
#: tests/test_ipc_live.py re-checks it on every run).
_IDENTITY = (1, 0, 0, 1)


def _compose(m: tuple[int, int, int, int], t: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """`SCH_SYMBOL::SetOrientation`: new = old * temp (KiCad's TRANSFORM multiplication order)."""
    x1, y1, x2, y2 = m
    tx1, ty1, tx2, ty2 = t
    return (x1 * tx1 + x2 * ty1, y1 * tx1 + y2 * ty1, x1 * tx2 + x2 * ty2, y1 * tx2 + y2 * ty2)


def symbol_transform(rotation: int, mirror_x: bool, mirror_y: bool) -> tuple[int, int, int, int]:
    """2x2 TRANSFORM (x1, y1, x2, y2) for a symbol orientation: rotate by `rotation` (0/90/180/270)
    from `_IDENTITY`, then apply the single mirror, composing the same temp matrices in the same
    order as `SCH_SYMBOL::SetOrientation(SYM_ORIENT_n + SYM_MIRROR_x)`."""
    m = _IDENTITY
    for _ in range((rotation // 90) % 4):
        m = _compose(m, _ROT_CCW)
    if mirror_x:
        m = _compose(m, _MIRROR_X)
    if mirror_y:
        m = _compose(m, _MIRROR_Y)
    return m


def transform_point(m: tuple[int, int, int, int], x: int, y: int) -> tuple[int, int]:
    """`TRANSFORM::TransformCoordinate`: (x1*x + y1*y, x2*x + y2*y)."""
    x1, y1, x2, y2 = m
    return (x1 * x + y1 * y, x2 * x + y2 * y)


def _orientation_degrees(orientation: int) -> int:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    table: dict[int, int] = {O.SSO_0: 0, O.SSO_90: 90, O.SSO_180: 180, O.SSO_270: 270}
    return table.get(orientation, 0)


def _symbol_pins(sym_proto: Any, pin_nets: dict[tuple[str, str], str], sheet_key: str) -> list[PinModel]:
    """Pins of one symbol INSTANCE with absolute positions; `pin_nets` is keyed by
    (sheet KIID path, pin KIID) because the same pin KIID is on a different net in every instance
    of a shared sheet file."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicPin

    unit = sym_proto.unit.unit
    body = sym_proto.body_style.style if sym_proto.HasField("body_style") else 0
    rot = _orientation_degrees(sym_proto.transform.orientation)
    m = symbol_transform(rot, sym_proto.transform.mirror_x, sym_proto.transform.mirror_y)
    ox, oy = sym_proto.position.x_nm, sym_proto.position.y_nm
    pins: list[PinModel] = []
    for child in sym_proto.definition.items:
        if not child.item.type_url.endswith("kiapi.schematic.types.SchematicPin"):
            continue
        # A multi-unit / multi-body-style library symbol carries every unit's pins; keep the ones of
        # this instance's unit (unit 0 = common to all units) and body style.
        if child.HasField("unit") and child.unit.unit not in (0, unit):
            continue
        if child.HasField("body_style") and body and child.body_style.style not in (0, body):
            continue
        pin = SchematicPin()
        child.item.Unpack(pin)
        tx, ty = transform_point(m, pin.position.x_nm, pin.position.y_nm)
        pins.append(
            PinModel(
                id=pin.id.value,
                name=pin.name,
                number=pin.number,
                position=(nm_to_mm(ox + tx), nm_to_mm(oy + ty)),
                electrical_type=pin_type_token(pin.electrical_type),
                net=pin_nets.get((sheet_key, pin.id.value), ""),
            )
        )
    return pins


def _kiid_key(sp: Any) -> str:
    """The KIID form of a `SheetPath` (`<root kiid>/<sheet kiid>/...`): the key `GetSchematicNetlist`
    and `DocumentSpecifier.sheet_path` identify a sheet INSTANCE by. `path_human_readable` is
    filled on symbols / sheet symbols / the hierarchy but EMPTY on the netlist's sheets (verified
    on the nightly with the multichannel_mixer demo), so this is the only key common to all."""
    return "/".join(k.value for k in sp.path)


def _sheet_info(inst: Any, parent_path: str | None = None) -> SheetInfo:
    """`SheetInstance` -> `SheetInfo`; the human-readable path is KiCad's `path_human_readable`
    (`/`, `/CH1/`) or, when a build leaves it empty, `<parent>/<name>/` built the way
    `SCH_SHEET_PATH::PathHumanReadable` and the file reader (`names_path`) do."""
    p = inst.proto if hasattr(inst, "proto") else inst
    human = p.path.path_human_readable
    if not human:
        human = "/" if parent_path is None else f"{parent_path}{p.name}/"
    return SheetInfo(
        path=human,
        name=p.name,
        filename=p.filename,
        page=p.page_number,
        children=tuple(_sheet_info(c, human) for c in p.children),
        kiid_path=_kiid_key(p.path),
    )


def _schematic_of(source: Any) -> Any:
    return (
        source.schematic() if hasattr(source, "schematic") and not hasattr(source, "get_symbols") else source
    )


def _sheet_scoped(sch: Any, info: SheetInfo) -> Any:
    """A kipy `Schematic` bound to the sheet instance `info` (`for_sheet`), so `GetItems` answers
    for that instance only. Needed because line / label / junction / no-connect protos carry no
    sheet path and a sheet file placed N times returns its items N times (same KIIDs) from an
    unscoped `GetItems` (`API_HANDLER_SCH::handleGetItems` walks `Hierarchy()`)."""
    from kipy.common_types import SheetPath
    from kipy.proto.common.types.base_types_pb2 import SheetPath as SheetPathProto

    proto = SheetPathProto()
    for kiid in info.kiid_path.split("/") if info.kiid_path else ():
        proto.path.add().value = kiid
    proto.path_human_readable = info.path
    return sch.for_sheet(SheetPath(proto=proto))


_SHEET_ITEM_TYPES = (
    "KOT_SCH_SYMBOL",
    "KOT_SCH_LINE",
    "KOT_SCH_LABEL",
    "KOT_SCH_GLOBAL_LABEL",
    "KOT_SCH_HIER_LABEL",
    "KOT_SCH_DIRECTIVE_LABEL",
    "KOT_SCH_JUNCTION",
    "KOT_SCH_NO_CONNECT",
    "KOT_SCH_SHEET",
)


def _sheet_items(scoped: Any) -> dict[str, list[Any]]:
    """One `GetItems` for the kinds the reader needs, split by proto message name."""
    from kipy.proto.common.types.enums_pb2 import KiCadObjectType

    types = [getattr(KiCadObjectType, name) for name in _SHEET_ITEM_TYPES if hasattr(KiCadObjectType, name)]
    out: dict[str, list[Any]] = {}
    for item in scoped.get_items(types=types):
        out.setdefault(item.proto.DESCRIPTOR.name, []).append(item)
    return out


def read_schematic(
    source: Any,
    *,
    refs: Iterable[str] | None = None,
    bbox: BBox | tuple[float, float, float, float] | None = None,
    nets: Iterable[str] | None = None,
    include_pins: bool = True,
) -> SchematicModel:
    """Read the open schematic (every sheet INSTANCE) into a `SchematicModel` (mm).

    Sheet instances, not sheet files: the hierarchy from `GetSchematicHierarchy` is walked and
    one scoped `GetItems` is sent per instance, so a sheet file placed several times (multichannel
    demo: `channel` x4; complex_hierarchy: `ampli_ht` x2) yields one symbol / wire / label per
    instance, each with the per-instance reference and net. Nets are keyed by (sheet KIID path,
    item KIID) because `GetSchematicNetlist` lists the SAME item KIID under every instance with a
    different net (`/CH1/ R4.2` -> `Net-(IC2A-IN-)`, `/CH2/ R4.2` -> `Net-(IC5A-IN-)`); keying by
    KIID alone reported 159/290 wrong pin nets on multichannel (reviewer finding, 2026-09-19).
    Every `sheet_path` (symbols, wires, labels, junctions, no-connects, sheet symbols, netlist
    items) is the human-readable form (`/`, `/CH1/`), the one the file reader uses.

    `refs` keeps only those symbols (and the netlist entries touching them); `bbox` keeps items
    with a point inside it (any sheet); `nets` keeps symbols with a pin on one of the nets, wires /
    junctions / labels listed under them, and those netlist entries.
    """
    from kipy.proto.schematic.schematic_types_pb2 import (
        SchematicLabelShape,
        SchematicLineType,
        SchematicSymbolType,
    )

    sch = _schematic_of(source)
    filters = BoardFilters(refs, bbox, nets)
    doc = sch.document
    project = doc.project.name if doc.HasField("project") else ""

    hierarchy: SheetInfo | None = None
    with contextlib.suppress(Exception):
        hierarchy = _sheet_info(sch.get_hierarchy().root)
    instances: list[SheetInfo] = list(hierarchy.walk()) if hierarchy is not None else []
    human_of: dict[str, str] = {info.kiid_path: info.path for info in instances}

    def human_path(sp: Any) -> str:
        key = _kiid_key(sp)
        return sp.path_human_readable or human_of.get(key) or key

    # Netlist first: (sheet instance, KIID) -> net for pins, wires, junctions and labels.
    raw_nets = sch.get_netlist()
    id_net: dict[tuple[str, str], str] = {}
    net_items_raw: dict[str, list[tuple[str, str, str]]] = {}  # name -> [(kid, kiid path, human)]
    for net in raw_nets:
        p = net.proto
        entries: list[tuple[str, str, str]] = []
        for sheet in p.sheets:
            key, human = _kiid_key(sheet.path), human_path(sheet.path)
            for kid in sheet.items:
                id_net[(key, kid.value)] = p.name
                entries.append((kid.value, key, human))
        net_items_raw[p.name] = entries

    symbols: list[SymbolModel] = []
    wires: list[WireModel] = []
    labels: list[LabelModel] = []
    junctions: list[JunctionModel] = []
    no_connects: list[NoConnectModel] = []
    sheets: list[SheetSymbolModel] = []
    pin_owner: dict[tuple[str, str], tuple[str, str]] = {}
    kinds: dict[str, str] = {}  # a KIID's kind does not depend on the instance

    scopes: list[tuple[SheetInfo | None, Any]]
    if instances:
        scopes = [(info, _sheet_scoped(sch, info)) for info in instances]
    else:  # no hierarchy answer: unscoped read, sheet paths from the protos / netlist only
        scopes = [(None, sch)]

    for info, scoped in scopes:
        by_type = _sheet_items(scoped)
        sheet_key = info.kiid_path if info is not None else ""
        sheet_human = info.path if info is not None else ""

        for sym in by_type.get("SchematicSymbolInstance", []):
            p = sym.proto
            key = sheet_key or _kiid_key(p.path)
            human = sheet_human or human_path(p.path)
            ref = p.reference_field.text.text
            pins = _symbol_pins(p, id_net, key) if include_pins or filters.nets is not None else []
            for pin in pins:
                pin_owner[(key, pin.id)] = (ref, pin.number)
            if filters.refs is not None and ref not in filters.refs:
                continue
            if filters.nets is not None and not any(filters.keep_net(pin.net) for pin in pins):
                continue
            pos = _pt(p.position)
            if not (filters.keep_point(pos) or any(filters.keep_point(pin.position) for pin in pins)):
                continue
            lib = p.definition.id
            attrs = p.attributes
            symbols.append(
                SymbolModel(
                    id=p.id.value,
                    ref=ref,
                    value=p.value_field.text.text,
                    lib_id=f"{lib.library_nickname}:{lib.entry_name}"
                    if lib.library_nickname
                    else lib.entry_name,
                    unit=int(p.unit.unit),
                    position=pos,
                    rotation=float(_orientation_degrees(p.transform.orientation)),
                    mirror_x=bool(p.transform.mirror_x),
                    mirror_y=bool(p.transform.mirror_y),
                    footprint=p.footprint_field.text.text,
                    dnp=attrs.do_not_populate,
                    exclude_from_bom=attrs.exclude_from_bill_of_materials,
                    exclude_from_board=attrs.exclude_from_board,
                    sheet_path=human,
                    pins=tuple(pins) if include_pins else (),
                    is_power=p.definition.type
                    in (SchematicSymbolType.SST_GLOBAL_POWER, SchematicSymbolType.SST_LOCAL_POWER),
                )
            )

        for line in by_type.get("SchematicLine", []):
            p = line.proto
            kinds[p.id.value] = (
                "wire"
                if p.type == SchematicLineType.SLT_WIRE
                else "bus"
                if p.type == SchematicLineType.SLT_BUS
                else "graphic"
            )
            model = WireModel(
                id=p.id.value,
                kind=kinds[p.id.value],
                start=_pt(p.start),
                end=_pt(p.end),
                sheet_path=sheet_human,
                net=id_net.get((sheet_key, p.id.value), ""),
            )
            if filters.nets is not None and not filters.keep_net(model.net):
                continue
            if filters.keep_point(model.start) or filters.keep_point(model.end):
                wires.append(model)

        for label_type, kind in (
            ("LocalLabel", "local"),
            ("GlobalLabel", "global"),
            ("HierarchicalLabel", "hierarchical"),
            ("DirectiveLabel", "directive"),
        ):
            for label in by_type.get(label_type, []):
                p = label.proto
                kinds[p.id.value] = "label"
                shape = _enum_name(SchematicLabelShape, p.shape, "SLSH_") if hasattr(p, "shape") else ""
                label_model = LabelModel(
                    id=p.id.value,
                    kind=kind,
                    text=p.text.text,
                    position=_pt(p.position),
                    sheet_path=sheet_human,
                    shape=shape,
                    net=id_net.get((sheet_key, p.id.value), ""),
                )
                if filters.nets is not None and not filters.keep_net(label_model.net):
                    continue
                if filters.keep_point(label_model.position):
                    labels.append(label_model)

        for j in by_type.get("Junction", []):
            p = j.proto
            kinds[p.id.value] = "junction"
            junction_model = JunctionModel(
                id=p.id.value,
                position=_pt(p.position),
                sheet_path=sheet_human,
                net=id_net.get((sheet_key, p.id.value), ""),
            )
            if filters.nets is not None and not filters.keep_net(junction_model.net):
                continue
            if filters.keep_point(junction_model.position):
                junctions.append(junction_model)

        for nc in by_type.get("NoConnectMarker", []):
            p = nc.proto
            kinds[p.id.value] = "no_connect"
            nc_model = NoConnectModel(id=p.id.value, position=_pt(p.position), sheet_path=sheet_human)
            if filters.keep_point(nc_model.position):
                no_connects.append(nc_model)

        for sheet in by_type.get("SheetSymbol", []):
            p = sheet.proto
            kinds[p.id.value] = "sheet"
            parent_human = sheet_human or human_path(p.path)
            sheet_pins = tuple(
                LabelModel(
                    id=sp.id.value,
                    kind="sheet_pin",
                    text=sp.text.text,
                    position=_pt(sp.position),
                    sheet_path=parent_human,
                    shape=_enum_name(SchematicLabelShape, sp.shape, "SLSH_"),
                    net=id_net.get((sheet_key, sp.id.value), ""),
                )
                for sp in p.pins
            )
            for sheet_pin in sheet_pins:
                kinds[sheet_pin.id] = "sheet_pin"
            sheet_model = SheetSymbolModel(
                id=p.id.value,
                name=p.name_field.text.text,
                filename=p.filename_field.text.text,
                position=_pt(p.position),
                size=(nm_to_mm(p.size.x_nm), nm_to_mm(p.size.y_nm)),
                sheet_path=parent_human,
                pins=sheet_pins,
            )
            if filters.keep_point(sheet_model.position):
                sheets.append(sheet_model)

    kept_refs = {s.ref for s in symbols} if (filters.refs is not None or filters.nets is not None) else None
    netlist: list[SchematicNetModel] = []
    for name, entries in net_items_raw.items():
        if not filters.keep_net(name):
            continue
        items = []
        for kid, key, human in entries:
            owner = pin_owner.get((key, kid))
            kind = "pin" if owner else kinds.get(kid, "unknown")
            items.append(
                NetItemRef(
                    id=kid,
                    kind=kind,
                    sheet_path=human,
                    ref=owner[0] if owner else "",
                    pin=owner[1] if owner else "",
                )
            )
        if kept_refs is not None and not any(i.ref in kept_refs for i in items if i.kind == "pin"):
            continue
        netlist.append(SchematicNetModel(name=name, items=tuple(items)))

    return SchematicModel(
        path=hierarchy.filename if hierarchy else "",
        name=sch.name,
        project=project,
        hierarchy=hierarchy,
        symbols=tuple(symbols),
        wires=tuple(wires),
        labels=tuple(labels),
        junctions=tuple(junctions),
        no_connects=tuple(no_connects),
        sheets=tuple(sheets),
        netlist=tuple(netlist),
        filters=filters.to_json(),
    )
