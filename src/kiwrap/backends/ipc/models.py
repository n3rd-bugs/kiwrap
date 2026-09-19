"""Millimetre, JSON-serialisable dataclasses for what the headless IPC API reports.

These are the objects `readers.read_board` / `readers.read_schematic` return: plain data (no
protobuf, no kipy wrappers), every length in millimetres (`units.nm_to_mm`, exact), every angle in
degrees, every id the KiCad KIID string. `to_json()` on any model gives nested dicts/lists/scalars
ready for `json.dumps` and for MCP `structuredContent`.

Field provenance (proto message -> model field) is documented per class; the proto definitions are
`api/proto/{board/board_types,schematic/schematic_types,common/types/base_types}.proto` of the
kicad-python submodule pinned in pyproject.toml.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BBox",
    "BoardModel",
    "DesignRulesSummary",
    "FootprintModel",
    "JunctionModel",
    "LabelModel",
    "LayerInfo",
    "NetItemRef",
    "NetModel",
    "NoConnectModel",
    "PadModel",
    "PinModel",
    "Point",
    "SchematicModel",
    "SchematicNetModel",
    "ShapeModel",
    "SheetInfo",
    "SheetSymbolModel",
    "SymbolModel",
    "TrackModel",
    "ViaModel",
    "WireModel",
    "ZoneModel",
    "to_json",
]

Point = tuple[float, float]  # (x_mm, y_mm)


def to_json(obj: Any) -> Any:
    """Recursively convert a model (or list/tuple/dict of models) into JSON-ready data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    return obj


class _Model:
    def to_json(self) -> Any:
        return to_json(self)


@dataclass(frozen=True)
class BBox(_Model):
    """Axis-aligned box in mm (`Box2.position` + `Box2.size`)."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> Point:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def contains(self, p: Point) -> bool:
        return self.x <= p[0] <= self.x2 and self.y <= p[1] <= self.y2

    def intersects(self, other: BBox) -> bool:
        return not (other.x > self.x2 or other.x2 < self.x or other.y > self.y2 or other.y2 < self.y)

    @staticmethod
    def union(boxes: list[BBox]) -> BBox | None:
        if not boxes:
            return None
        x1 = min(b.x for b in boxes)
        y1 = min(b.y for b in boxes)
        x2 = max(b.x2 for b in boxes)
        y2 = max(b.y2 for b in boxes)
        return BBox(x1, y1, x2 - x1, y2 - y1)


# --- board ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerInfo(_Model):
    """`BoardLayer` enum entry -> KiCad's canonical layer name (`BL_F_Cu` -> `F.Cu`) plus the user
    name the board gives it (`GetBoardLayerName`) and whether it is copper."""

    id: int
    name: str
    user_name: str
    copper: bool


@dataclass(frozen=True)
class PadModel(_Model):
    """`Pad`: number, net, type (PT_*), absolute position, the copper-layer shape of its padstack
    (`PadStackLayer`: PSS_* shape + size + rounding), drill, layers, optional polygon outline
    (`GetPadShapeAsPolygon`, absolute mm)."""

    id: str
    number: str
    net: str
    type: str  # "pth" | "smd" | "npth" | "edge_connector" | "unknown"
    position: Point
    shape: str  # "circle" | "rectangle" | "oval" | "roundrect" | "chamferedrect" | "trapezoid" | "custom"
    size: tuple[float, float]
    rotation: float
    corner_rounding_ratio: float
    drill: tuple[float, float] | None
    layers: tuple[str, ...]
    polygon: tuple[Point, ...] = ()
    pin_name: str = ""
    pin_type: str = ""


@dataclass(frozen=True)
class FootprintModel(_Model):
    """`FootprintInstance`: reference/value fields, `definition.id` (FPID), position/orientation,
    layer -> side, attributes, bounding boxes and pads. `bbox` is `GetBoundingBox(include_text=False)`
    (the footprint body: pads + graphics, verified on ecc83 R1 = [134.746, 106.875, 137.796,
    116.645] against the file reader's courtyard box [134.771, 106.9, 137.771, 116.62]); the
    text-inflated editor box is `bbox_with_text`."""

    id: str
    ref: str
    value: str
    fpid: str
    position: Point
    rotation: float
    layer: str
    side: str  # "front" | "back"
    locked: bool
    mounting: str  # "smd" | "through_hole" | "unspecified"
    dnp: bool
    exclude_from_bom: bool
    exclude_from_pos: bool
    sheet_path: str
    bbox: BBox | None
    pads: tuple[PadModel, ...] = ()
    #: `GetBoundingBox(include_text=True)`: what the editor draws, reference/value text included
    bbox_with_text: BBox | None = None
    #: extent of the `definition.shapes` on F.CrtYd / B.CrtYd (absolute mm); None when the
    #: footprint has no courtyard (kw_pcb_check reports that as a review item)
    courtyard_bbox: BBox | None = None


@dataclass(frozen=True)
class TrackModel(_Model):
    """`Track` (kind "track") or `Arc` (kind "arc", with `mid`)."""

    id: str
    kind: str
    start: Point
    end: Point
    width: float
    layer: str
    net: str
    mid: Point | None = None
    locked: bool = False

    @property
    def length(self) -> float:
        """Straight-line length in mm (an arc's chord; the API has no arc-length field)."""
        return ((self.end[0] - self.start[0]) ** 2 + (self.end[1] - self.start[1]) ** 2) ** 0.5


@dataclass(frozen=True)
class ViaModel(_Model):
    """`Via`: position, VT_* type, net, padstack drill + first copper-layer size (diameter), layers."""

    id: str
    position: Point
    type: str
    net: str
    diameter: float
    drill: float
    layers: tuple[str, ...]
    locked: bool = False


@dataclass(frozen=True)
class ZoneModel(_Model):
    """`Zone`: ZT_* type, net (copper zones), layers, `filled`, outline polygons (mm), priority."""

    id: str
    name: str
    type: str
    net: str
    layers: tuple[str, ...]
    filled: bool
    priority: int
    outline: tuple[tuple[Point, ...], ...]
    fill_polygons: dict[str, int] = field(default_factory=dict)  # layer -> filled polygon count


@dataclass(frozen=True)
class ShapeModel(_Model):
    """`BoardGraphicShape`: kind = segment | rectangle | arc | circle | polygon | bezier | ellipse."""

    id: str
    kind: str
    layer: str
    points: tuple[Point, ...]
    width: float
    net: str = ""
    radius: float | None = None


@dataclass(frozen=True)
class NetModel(_Model):
    name: str
    netclass: str = ""


@dataclass(frozen=True)
class DesignRulesSummary(_Model):
    """`BoardDesignRules.constraints` in mm (0.0 when the board leaves a constraint unset)."""

    min_clearance: float
    min_track_width: float
    min_connection_width: float
    min_via_size: float
    min_via_annular_width: float
    min_through_drill: float
    min_microvia_size: float
    min_microvia_drill: float
    copper_edge_clearance: float
    hole_clearance: float
    hole_to_hole_min: float
    silk_clearance: float


@dataclass(frozen=True)
class BoardModel(_Model):
    path: str
    name: str
    project: str
    copper_layer_count: int
    layers: tuple[LayerInfo, ...]
    nets: tuple[NetModel, ...]
    footprints: tuple[FootprintModel, ...]
    tracks: tuple[TrackModel, ...]
    vias: tuple[ViaModel, ...]
    zones: tuple[ZoneModel, ...]
    shapes: tuple[ShapeModel, ...]
    outline_bbox: BBox | None
    items_bbox: BBox | None
    design_rules: DesignRulesSummary | None
    filters: dict[str, Any] = field(default_factory=dict)

    def footprint(self, ref: str) -> FootprintModel | None:
        return next((f for f in self.footprints if f.ref == ref), None)


# --- schematic -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PinModel(_Model):
    """`SchematicPin` of the symbol's `definition.items`, transformed to the sheet (absolute mm) with
    the symbol's `SchematicSymbolTransform` (see readers.symbol_transform); `net` from the netlist."""

    id: str
    name: str
    number: str
    position: Point
    electrical_type: str
    net: str = ""


@dataclass(frozen=True)
class SymbolModel(_Model):
    """`SchematicSymbolInstance`: instance reference/value/footprint fields, `definition.id`,
    unit, position, orientation + mirrors, attributes, pins."""

    id: str
    ref: str
    value: str
    lib_id: str
    unit: int
    position: Point
    rotation: float
    mirror_x: bool
    mirror_y: bool
    footprint: str
    dnp: bool
    exclude_from_bom: bool
    exclude_from_board: bool
    sheet_path: str
    pins: tuple[PinModel, ...] = ()
    is_power: bool = False


@dataclass(frozen=True)
class WireModel(_Model):
    """`SchematicLine`: kind = wire | bus | graphic (SLT_*). `sheet_path` is the human-readable
    sheet instance path (`/`, `/CH1/`); `net` comes from `GetSchematicNetlist`, which lists wire
    KIIDs per net and per sheet instance ("" when the wire is on no net / a graphic line)."""

    id: str
    kind: str
    start: Point
    end: Point
    sheet_path: str
    net: str = ""


@dataclass(frozen=True)
class LabelModel(_Model):
    """`LocalLabel` / `GlobalLabel` / `HierarchicalLabel` / `DirectiveLabel` / `SheetPin`."""

    id: str
    kind: str  # "local" | "global" | "hierarchical" | "directive" | "sheet_pin"
    text: str
    position: Point
    sheet_path: str
    shape: str = ""
    net: str = ""  # from GetSchematicNetlist (per sheet instance), "" when unknown


@dataclass(frozen=True)
class JunctionModel(_Model):
    id: str
    position: Point
    sheet_path: str
    net: str = ""


@dataclass(frozen=True)
class NoConnectModel(_Model):
    id: str
    position: Point
    sheet_path: str


@dataclass(frozen=True)
class SheetSymbolModel(_Model):
    """`SheetSymbol`: the box on a parent sheet that instantiates a sub-sheet."""

    id: str
    name: str
    filename: str
    position: Point
    size: tuple[float, float]
    sheet_path: str
    pins: tuple[LabelModel, ...] = ()


@dataclass(frozen=True)
class SheetInfo(_Model):
    """`SheetInstance` from `GetSchematicHierarchy`. `path` is the human-readable instance path
    (`/`, `/CH1/`, `/CH1/sub/`), `kiid_path` the KIID form (`<root>/<sheet kiid>`) that the
    netlist and `DocumentSpecifier.sheet_path` use; the same sheet FILE placed twice yields two
    `SheetInfo`s with different paths and identical item KIIDs."""

    path: str
    name: str
    filename: str
    page: str
    children: tuple[SheetInfo, ...] = ()
    kiid_path: str = ""

    def walk(self) -> Iterator[SheetInfo]:
        """Depth-first, self first."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class NetItemRef(_Model):
    """One KIID the netlist lists for a net, resolved to what it is: kind = pin | wire | junction |
    label | ... ; `ref`/`pin` filled for pins (from the symbol the pin belongs to)."""

    id: str
    kind: str
    sheet_path: str
    ref: str = ""
    pin: str = ""


@dataclass(frozen=True)
class SchematicNetModel(_Model):
    """`SchematicNet` (`GetSchematicNetlist`): name -> items per sheet."""

    name: str
    items: tuple[NetItemRef, ...]

    @property
    def pins(self) -> tuple[NetItemRef, ...]:
        return tuple(i for i in self.items if i.kind == "pin")


@dataclass(frozen=True)
class SchematicModel(_Model):
    path: str
    name: str
    project: str
    hierarchy: SheetInfo | None
    symbols: tuple[SymbolModel, ...]
    wires: tuple[WireModel, ...]
    labels: tuple[LabelModel, ...]
    junctions: tuple[JunctionModel, ...]
    no_connects: tuple[NoConnectModel, ...]
    sheets: tuple[SheetSymbolModel, ...]
    netlist: tuple[SchematicNetModel, ...]
    filters: dict[str, Any] = field(default_factory=dict)

    def symbol(self, ref: str) -> SymbolModel | None:
        return next((s for s in self.symbols if s.ref == ref), None)
