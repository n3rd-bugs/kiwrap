"""`kicad-cli pcb export ipc2581` parser -> `Ipc2581Board`, a compact structured view of the
board KiCad itself computed: components with placement, pads with nets, vias, tracks, drill
holes, the net list (derived from the per-net feature sets) and the board outline.

Verified against kicad-cli 8.0.9, 9.0.9, 10.0.6 and the 10.99 nightly (build 4083) on the ecc83
and pic_programmer demos (scratchpad structured/probe1.py, probe3.py). What the XML looks like
(namespace http://webstds.ipc.org/2581, revision "C"; pcbnew/pcb_io/ipc2581/pcb_io_ipc2581.cpp):

    IPC-2581/Content/LayerRef, DictionaryStandard/EntryStandard@id (pad shapes), DictionaryLineDesc
    IPC-2581/Bom/BomItem@{OEMDesignNumberRef,quantity,pinCount}/RefDes@{name,packageRef,populate,
        layerRef} + Characteristics/Textual@{textualCharacteristicName="Value",
        textualCharacteristicValue}
    IPC-2581/Ecad/CadHeader@units="MILLIMETER"; CadData/Layer@{name,layerFunction,side,polarity};
        CadData/Stackup/StackupGroup/StackupLayer@{layerOrGroupRef,thickness,sequence}
    IPC-2581/Ecad/CadData/Step@name/
        Profile/Polygon/PolyBegin+PolyStepSegment@{x,y}           board outline (may be several)
        PadStackDef@name/PadstackHoleDef@{diameter,platingStatus}, PadstackPadDef@layerRef
        Package@name/Outline/Polygon, Pin@number/Location@{x,y}   footprint-relative geometry
        Component@{refDes,packageRef,part,layerRef,mountType}/Xform@{rotation,mirror}, Location
        LayerFeature@layerRef/Set@{net,padUsage="VIA"?,geometry}/
            Pad@padstackDefRef/Xform, Location, StandardPrimitiveRef|UserPrimitiveRef, PinRef@{componentRef,pin}
            Features/UserSpecial/Line@{startX,startY,endX,endY}/LineDesc@lineWidth      tracks
            Features/(UserSpecial/Contour/)Polygon                                       zone fills, shapes
            Hole@{name,diameter,platingStatus,x,y}                                        drill layer

Conventions and quirks encoded here:
- every coordinate is in millimetres with the Y axis NEGATED relative to the board file
  (`addXY` writes `-m_scale * y`): a footprint at `(at 141.605 99.695)` is
  `<Location x="141.6050" y="-99.6950"/>`. This model flips it back so `x_mm`/`y_mm` are KiCad
  board coordinates (y grows downwards, the same frame as `.kicad_pcb` and DRC JSON);
- 8.0 prefixes every identifier ("CMP:C1", "PIN:2", "LAYER:top_cu", "NET:GND", "KI:<package>",
  "REF:<part>", "PKG:", "COLOR:", "BOARD:ecc83-pp", "DRILL:top_cu_bottom_cu", "BOM:"); 9.0+ writes
  them bare. The prefixes are stripped (the full set was grepped from the 8.0.9 output);
- layer names are the board's USER names when the user renamed a layer ("top_cu", "top_layer")
  and the canonical ones otherwise; the `layers` table maps them to `layerFunction`/`side`;
- a flipped footprint carries `Xform mirror="true"` and `rotation = (-angle - 180) mod 360`
  (pcb_io_ipc2581.cpp `fp_angle.Invert() - ANGLE_180`); `Ipc2581Component.rotation_deg` is
  KiCad's orientation recovered from that, `ipc_rotation_deg` the value as written;
- a through-hole pad is repeated on every copper (and mask) layer it touches; pads are keyed by
  (refdes, pin) with the set of copper layers they appear on. Vias are the `Pad` elements
  without a `PinRef` inside a `Set padUsage="VIA"`;
- `LogicalNet`/`PhyNetGroup` sections are NOT written by default (nightly `--sections` can add
  them); the net list here is derived from `Set@net` of pads/vias/tracks, so nets without any
  copper item never appear;
- the output can be large (2 MB for pic_programmer); parsing uses `xml.etree` iteratively per
  `Step` child so memory stays bounded by the largest element.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any

__all__ = [
    "NS",
    "Ipc2581Board",
    "Ipc2581Component",
    "Ipc2581Hole",
    "Ipc2581Layer",
    "Ipc2581Net",
    "Ipc2581Package",
    "Ipc2581Pad",
    "Ipc2581PadStack",
    "Ipc2581Track",
    "Ipc2581Via",
    "PackagePin",
    "Point",
    "parse_ipc2581",
]

NS = "http://webstds.ipc.org/2581"
_PREFIX_RE = re.compile(r"^(?:CMP|PIN|LAYER|NET|REF|KI|PKG|COLOR|BOARD|DRILL|BOM|STEP|PADSTACK):")


def _strip(value: str | None) -> str:
    """Remove the 8.0 identifier prefixes ("CMP:C1" -> "C1"); 9.0+ values pass through."""
    return _PREFIX_RE.sub("", value or "")


def _tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _q(name: str) -> str:
    return f"{{{NS}}}{name}"


def _f(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Point:
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class Ipc2581Layer:
    name: str
    function: str  # CONDUCTOR | SOLDERMASK | SILKSCREEN | BOARD_OUTLINE | DRILL | ...
    side: str  # TOP | BOTTOM | INTERNAL | ALL | NONE
    polarity: str = "POSITIVE"

    @property
    def is_copper(self) -> bool:
        return self.function == "CONDUCTOR"


@dataclass(frozen=True)
class PackagePin:
    number: str
    x_mm: float  # footprint-relative, y flipped back to KiCad's frame
    y_mm: float
    type: str = ""  # THRU | SURFACE
    primitive: str = ""


@dataclass(frozen=True)
class Ipc2581Package:
    name: str
    outline: tuple[Point, ...]  # footprint-relative, KiCad frame
    pins: tuple[PackagePin, ...]
    type: str = ""
    pin_one: str = ""


@dataclass(frozen=True)
class Ipc2581PadStack:
    name: str
    hole_diameter_mm: float | None
    plated: bool | None
    layers: tuple[str, ...]


@dataclass(frozen=True)
class Ipc2581Component:
    ref: str
    package: str  # Package@name (footprint name with a numeric suffix)
    part: str
    layer: str  # copper layer user-name it sits on
    side: str  # "top" | "bottom"
    x_mm: float
    y_mm: float
    rotation_deg: float  # KiCad footprint orientation (recovered for flipped parts)
    ipc_rotation_deg: float  # Xform@rotation as written (0 when absent)
    mirror: bool
    mount_type: str  # THMT | SMT | OTHER
    value: str | None = None  # from the BOM section when present
    populate: bool = True


@dataclass(frozen=True)
class Ipc2581Pad:
    ref: str
    pin: str
    net: str | None
    x_mm: float
    y_mm: float
    rotation_deg: float
    layers: tuple[str, ...]  # copper layers this pad appears on
    padstack: str
    primitive: str  # StandardPrimitiveRef / UserPrimitiveRef id (shape dictionary key)

    @property
    def through_hole(self) -> bool:
        return len(self.layers) > 1


@dataclass(frozen=True)
class Ipc2581Via:
    net: str | None
    x_mm: float
    y_mm: float
    layers: tuple[str, ...]
    padstack: str


@dataclass(frozen=True)
class Ipc2581Track:
    net: str | None
    layer: str
    start: Point
    end: Point
    width_mm: float

    @property
    def length_mm(self) -> float:
        return round(math.hypot(self.end.x_mm - self.start.x_mm, self.end.y_mm - self.start.y_mm), 6)


@dataclass(frozen=True)
class Ipc2581Hole:
    name: str
    net: str | None
    x_mm: float
    y_mm: float
    diameter_mm: float
    plating: str  # PLATED | NONPLATED | VIA
    padstack: str


@dataclass(frozen=True)
class Ipc2581Net:
    name: str
    pins: tuple[tuple[str, str], ...]  # (refdes, pin) sorted
    via_count: int = 0
    track_count: int = 0
    track_length_mm: float = 0.0
    layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ipc2581Board:
    source: str
    name: str
    kicad_version: str
    units: str  # "mm" (KiCad writes MILLIMETER; INCH is converted)
    layers: tuple[Ipc2581Layer, ...]
    outline: tuple[tuple[Point, ...], ...]  # one polygon per Profile polygon, KiCad frame
    components: tuple[Ipc2581Component, ...]
    pads: tuple[Ipc2581Pad, ...]
    vias: tuple[Ipc2581Via, ...]
    tracks: tuple[Ipc2581Track, ...]
    holes: tuple[Ipc2581Hole, ...]
    nets: tuple[Ipc2581Net, ...]
    packages: dict[str, Ipc2581Package] = field(default_factory=dict, repr=False)
    padstacks: dict[str, Ipc2581PadStack] = field(default_factory=dict, repr=False)
    stackup_thickness_mm: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def copper_layers(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self.layers if layer.is_copper)

    def component(self, ref: str) -> Ipc2581Component | None:
        return next((c for c in self.components if c.ref == ref), None)

    def net(self, name: str) -> Ipc2581Net | None:
        return next((n for n in self.nets if n.name == name), None)

    def pads_of(self, ref: str) -> tuple[Ipc2581Pad, ...]:
        return tuple(p for p in self.pads if p.ref == ref)

    @property
    def bbox(self) -> tuple[Point, Point] | None:
        """(min, max) corner of the outline polygons, KiCad frame; None without an outline."""
        pts = [p for poly in self.outline for p in poly]
        if not pts:
            return None
        return (
            Point(min(p.x_mm for p in pts), min(p.y_mm for p in pts)),
            Point(max(p.x_mm for p in pts), max(p.y_mm for p in pts)),
        )


# --- parsing helpers ------------------------------------------------------------------------------


def _polygon_points(poly: ET.Element, scale: float, y_sign: float) -> tuple[Point, ...]:
    pts: list[Point] = []
    for child in poly:
        t = _tag(child)
        if t in ("PolyBegin", "PolyStepSegment", "PolyStepCurve"):
            pts.append(Point(*_xy(child.get("x"), child.get("y"), scale, y_sign)))
    return tuple(pts)


def _xy(x: str | None, y: str | None, scale: float, y_sign: float) -> tuple[float, float]:
    """Scaled, y-flipped, rounded; `+ 0.0` turns the -0.0 of a flipped zero into 0.0."""
    return round(_f(x) * scale, 6) + 0.0, round(_f(y) * scale * y_sign, 6) + 0.0


def _location(elem: ET.Element, scale: float, y_sign: float) -> tuple[float, float]:
    loc = elem.find(_q("Location"))
    if loc is None:
        return 0.0, 0.0
    return _xy(loc.get("x"), loc.get("y"), scale, y_sign)


def _xform(elem: ET.Element) -> tuple[float, bool]:
    xf = elem.find(_q("Xform"))
    if xf is None:
        return 0.0, False
    return _f(xf.get("rotation")), (xf.get("mirror") or "").lower() == "true"


def _primitive(elem: ET.Element) -> str:
    for name in ("StandardPrimitiveRef", "UserPrimitiveRef"):
        ref = elem.find(_q(name))
        if ref is not None:
            return _strip(ref.get("id"))
    return ""


def _kicad_rotation(ipc_rotation: float, mirror: bool) -> float:
    """Invert pcb_io_ipc2581.cpp: flipped parts write `(-angle - 180) mod 360`."""
    angle = (-ipc_rotation - 180.0) if mirror else ipc_rotation
    angle = angle % 360.0
    return round(angle, 4)


def _iter_lines(features: ET.Element) -> Iterator[ET.Element]:
    for child in features.iter():
        if _tag(child) in ("Line", "Arc"):
            yield child


def _line_width(line: ET.Element, line_descs: dict[str, float]) -> float:
    desc = line.find(_q("LineDesc"))
    if desc is not None:
        return _f(desc.get("lineWidth"))
    ref = line.find(_q("LineDescRef"))
    if ref is not None:
        return line_descs.get(_strip(ref.get("id")), 0.0)
    return 0.0


def _read(source: str | IO[str] | IO[bytes] | ET.Element) -> ET.Element:
    if isinstance(source, ET.Element):
        return source
    if isinstance(source, str) and source.lstrip().startswith("<"):
        return ET.fromstring(source)
    return ET.parse(source).getroot()  # a path or a file object


def parse_ipc2581(source: str | IO[str] | IO[bytes] | ET.Element, *, source_name: str = "") -> Ipc2581Board:
    """Parse an IPC-2581 XML document (text, path, file object or parsed root) into the compact
    model. Coordinates come out in KiCad's board frame (mm, y down) — see the module docstring."""
    root = _read(source)
    ecad = root.find(_q("Ecad"))
    header = ecad.find(_q("CadHeader")) if ecad is not None else None
    units = (header.get("units") if header is not None else "MILLIMETER") or "MILLIMETER"
    scale = {"MILLIMETER": 1.0, "INCH": 25.4, "MICRON": 0.001}.get(units.upper(), 1.0)
    y_sign = -1.0

    version = ""
    pkg = root.find(f".//{_q('SoftwarePackage')}")
    if pkg is not None:
        version = pkg.get("revision") or ""

    line_descs: dict[str, float] = {}
    for entry in root.iter(_q("EntryLineDesc")):
        desc = entry.find(_q("LineDesc"))
        if desc is not None:
            line_descs[_strip(entry.get("id"))] = _f(desc.get("lineWidth")) * scale

    values: dict[str, str] = {}
    populate: dict[str, bool] = {}
    for item in root.iter(_q("BomItem")):
        value: str | None = None
        for textual in item.iter(_q("Textual")):
            if textual.get("textualCharacteristicName") == "Value":
                value = textual.get("textualCharacteristicValue") or ""
        for refdes in item.findall(_q("RefDes")):
            ref = _strip(refdes.get("name"))
            if value is not None:
                values[ref] = value
            populate[ref] = (refdes.get("populate") or "true").lower() != "false"

    layers: list[Ipc2581Layer] = []
    stackup_thickness: float | None = None
    cad_data = ecad.find(_q("CadData")) if ecad is not None else None
    if cad_data is not None:
        for layer_el in cad_data.findall(_q("Layer")):
            layers.append(
                Ipc2581Layer(
                    name=_strip(layer_el.get("name")),
                    function=layer_el.get("layerFunction") or "",
                    side=layer_el.get("side") or "",
                    polarity=layer_el.get("polarity") or "POSITIVE",
                )
            )
        stackup = cad_data.find(_q("Stackup"))
        if stackup is not None and stackup.get("overallThickness"):
            stackup_thickness = round(_f(stackup.get("overallThickness")) * scale, 6)
    layer_side = {layer.name: layer.side for layer in layers}
    copper = {layer.name for layer in layers if layer.is_copper}

    step = cad_data.find(_q("Step")) if cad_data is not None else None
    name = _strip(step.get("name")) if step is not None else ""
    outline: list[tuple[Point, ...]] = []
    packages: dict[str, Ipc2581Package] = {}
    padstacks: dict[str, Ipc2581PadStack] = {}
    components: list[Ipc2581Component] = []
    pads: dict[tuple[str, str], dict[str, Any]] = {}
    vias: list[Ipc2581Via] = []
    tracks: list[Ipc2581Track] = []
    holes: list[Ipc2581Hole] = []
    counts: dict[str, int] = {}

    for child in list(step) if step is not None else []:
        t = _tag(child)
        counts[t] = counts.get(t, 0) + 1
        if t == "Profile":
            for poly in child.iter(_q("Polygon")):
                pts = _polygon_points(poly, scale, y_sign)
                if pts:
                    outline.append(pts)
        elif t == "PadStackDef":
            hole = child.find(_q("PadstackHoleDef"))
            padstacks[_strip(child.get("name"))] = Ipc2581PadStack(
                name=_strip(child.get("name")),
                hole_diameter_mm=round(_f(hole.get("diameter")) * scale, 6) if hole is not None else None,
                plated=(hole.get("platingStatus") == "PLATED") if hole is not None else None,
                layers=tuple(_strip(p.get("layerRef")) for p in child.findall(_q("PadstackPadDef"))),
            )
        elif t == "Package":
            pins: list[PackagePin] = []
            for pin in child.findall(_q("Pin")):
                x, y = _location(pin, scale, y_sign)
                pins.append(
                    PackagePin(
                        number=_strip(pin.get("number")),
                        x_mm=x,
                        y_mm=y,
                        type=pin.get("type") or "",
                        primitive=_primitive(pin),
                    )
                )
            outline_pts: tuple[Point, ...] = ()
            out_el = child.find(_q("Outline"))
            if out_el is not None:
                outline_poly = out_el.find(_q("Polygon"))
                if outline_poly is not None:
                    outline_pts = _polygon_points(outline_poly, scale, y_sign)
            packages[_strip(child.get("name"))] = Ipc2581Package(
                name=_strip(child.get("name")),
                outline=outline_pts,
                pins=tuple(pins),
                type=child.get("type") or "",
                pin_one=child.get("pinOne") or "",
            )
        elif t == "Component":
            ref = _strip(child.get("refDes"))
            layer = _strip(child.get("layerRef"))
            rot, mirror = _xform(child)
            x, y = _location(child, scale, y_sign)
            side = "bottom" if (layer_side.get(layer) == "BOTTOM" or mirror) else "top"
            components.append(
                Ipc2581Component(
                    ref=ref,
                    package=_strip(child.get("packageRef")),
                    part=_strip(child.get("part")),
                    layer=layer,
                    side=side,
                    x_mm=x,
                    y_mm=y,
                    rotation_deg=_kicad_rotation(rot, mirror),
                    ipc_rotation_deg=rot,
                    mirror=mirror,
                    mount_type=child.get("mountType") or "",
                    value=values.get(ref),
                    populate=populate.get(ref, True),
                )
            )
        elif t == "LayerFeature":
            layer = _strip(child.get("layerRef"))
            is_copper = layer in copper or not copper
            for fset in child.findall(_q("Set")):
                net = _strip(fset.get("net")) or None
                for pad in fset.findall(_q("Pad")):
                    pinref = pad.find(_q("PinRef"))
                    x, y = _location(pad, scale, y_sign)
                    if pinref is None:
                        if is_copper:
                            _add_via(vias, net, x, y, layer, _strip(pad.get("padstackDefRef")))
                        continue
                    if not is_copper:
                        continue
                    key = (_strip(pinref.get("componentRef")), _strip(pinref.get("pin")))
                    rec = pads.get(key)
                    if rec is None:
                        rot, _m = _xform(pad)
                        pads[key] = {
                            "net": net,
                            "x": x,
                            "y": y,
                            "rot": rot,
                            "layers": [layer],
                            "padstack": _strip(pad.get("padstackDefRef")),
                            "primitive": _primitive(pad),
                        }
                    elif layer not in rec["layers"]:
                        rec["layers"].append(layer)
                        rec["net"] = rec["net"] or net
                for hole in fset.findall(_q("Hole")):
                    hx, hy = _xy(hole.get("x"), hole.get("y"), scale, y_sign)
                    holes.append(
                        Ipc2581Hole(
                            name=hole.get("name") or "",
                            net=net,
                            x_mm=hx,
                            y_mm=hy,
                            diameter_mm=round(_f(hole.get("diameter")) * scale, 6),
                            plating=hole.get("platingStatus") or "",
                            padstack=_strip(fset.get("geometry")),
                        )
                    )
                if is_copper and net:
                    for features in fset.findall(_q("Features")):
                        for line in _iter_lines(features):
                            if _tag(line) != "Line":
                                continue  # arcs: endpoints only would misreport length; skipped
                            tracks.append(
                                Ipc2581Track(
                                    net=net,
                                    layer=layer,
                                    start=Point(*_xy(line.get("startX"), line.get("startY"), scale, y_sign)),
                                    end=Point(*_xy(line.get("endX"), line.get("endY"), scale, y_sign)),
                                    width_mm=round(_line_width(line, line_descs) * scale, 6),
                                )
                            )

    pad_records = tuple(
        Ipc2581Pad(
            ref=ref,
            pin=pin,
            net=rec["net"],
            x_mm=rec["x"],
            y_mm=rec["y"],
            rotation_deg=rec["rot"],
            layers=tuple(rec["layers"]),
            padstack=rec["padstack"],
            primitive=rec["primitive"],
        )
        for (ref, pin), rec in pads.items()
    )
    nets = _build_nets(pad_records, vias, tracks)
    return Ipc2581Board(
        source=source_name,
        name=name,
        kicad_version=version,
        units="mm",
        layers=tuple(layers),
        outline=tuple(outline),
        components=tuple(components),
        pads=pad_records,
        vias=tuple(vias),
        tracks=tuple(tracks),
        holes=tuple(holes),
        nets=nets,
        packages=packages,
        padstacks=padstacks,
        stackup_thickness_mm=stackup_thickness,
        raw={"step_children": counts, "units": units},
    )


def _add_via(vias: list[Ipc2581Via], net: str | None, x: float, y: float, layer: str, padstack: str) -> None:
    """Merge the per-layer copies of a via (same position + padstack) into one record."""
    for i, v in enumerate(vias):
        if v.padstack == padstack and abs(v.x_mm - x) < 1e-6 and abs(v.y_mm - y) < 1e-6:
            if layer not in v.layers:
                vias[i] = Ipc2581Via(
                    net=v.net or net, x_mm=v.x_mm, y_mm=v.y_mm, layers=(*v.layers, layer), padstack=padstack
                )
            return
    vias.append(Ipc2581Via(net=net, x_mm=x, y_mm=y, layers=(layer,), padstack=padstack))


def _build_nets(
    pads: Iterable[Ipc2581Pad], vias: Iterable[Ipc2581Via], tracks: Iterable[Ipc2581Track]
) -> tuple[Ipc2581Net, ...]:
    pins: dict[str, set[tuple[str, str]]] = {}
    via_count: dict[str, int] = {}
    track_count: dict[str, int] = {}
    length: dict[str, float] = {}
    layers: dict[str, set[str]] = {}
    for p in pads:
        if p.net:
            pins.setdefault(p.net, set()).add((p.ref, p.pin))
            layers.setdefault(p.net, set()).update(p.layers)
    for v in vias:
        if v.net:
            via_count[v.net] = via_count.get(v.net, 0) + 1
            layers.setdefault(v.net, set()).update(v.layers)
    for t in tracks:
        if t.net:
            track_count[t.net] = track_count.get(t.net, 0) + 1
            length[t.net] = length.get(t.net, 0.0) + t.length_mm
            layers.setdefault(t.net, set()).add(t.layer)
    names = sorted(set(pins) | set(via_count) | set(track_count))
    return tuple(
        Ipc2581Net(
            name=n,
            pins=tuple(sorted(pins.get(n, ()))),
            via_count=via_count.get(n, 0),
            track_count=track_count.get(n, 0),
            track_length_mm=round(length.get(n, 0.0), 6),
            layers=tuple(sorted(layers.get(n, ()))),
        )
        for n in names
    )
