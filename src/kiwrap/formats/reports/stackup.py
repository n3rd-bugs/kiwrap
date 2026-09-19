"""Board stackup parsers -> `Stackup`, from three sources that carry the same information:

1. `kicad-cli pcb export stackup --format json` (10.99 nightly build 4083; KiCad MR !2691 added
   the command with csv/report, json arrived on master afterwards). The JSON is the protobuf-JSON
   rendering of the IPC API's `kiapi.board.BoardStackup` message (api/proto/board/board.proto):
   `{"finish": {"typeName"}, "impedance": {"isControlled"?}, "edge": {"connector": {"type":
   "BECT_NONE"}, "plating": {"hasEdgePlating"?}}, "layers": [{"thickness": {"valueNm": "35000"},
   "layer": "BL_F_Cu", "enabled": true, "type": "BSLT_COPPER", "materialName": "copper",
   "userName": "F.Cu", "color": {"r","g","b","a"}, "dielectric": {"layer": [{"epsilonR",
   "lossTangent", "materialName", "thickness": {"valueNm"}, "specFrequency", "dielectricModel"}],
   "type": "BSDT_CORE"}, "soldermask": {"epsilonR", "materialName", "thickness"}, "silkscreen":
   {"materialName"}}]}`. proto3 JSON omits default-valued fields, so a missing `isControlled` is
   false, a missing colour channel is 0.0, and a missing `thickness` is 0 nm. `valueNm` is a
   STRING (int64). Verified on the ecc83 demo (scratchpad structured/out/10.99.0/stackup.json).
2. `--format csv` from the same command: a quoted CSV `"Layer","Type","Sublayer","Thickness",
   "Locked","Material","Color","EpsilonR","LossTg"` followed by a blank line and a
   `"Property","Value"` table (`"Finish","None"`). Thickness cells carry a unit ("0.035 mm").
3. The board file itself: `(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035))
   (layer "dielectric 1" (type "core"|"prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4.3)
   (loss_tangent 0.02) addsublayer (thickness ...) ...) ... (copper_finish "ENIG")
   (dielectric_constraints no) (edge_connector yes|bevelled) (castellated_pads yes)
   (edge_plating yes)))`, parsed with the lossless CST. KiCad only writes the section once the
   user opened Board Setup > Physical Stackup; boards without it get a `Stackup` derived from the
   `(layers ...)` table with `has_stackup_section=False`. This is the fallback on every KiCad
   version that has no stackup export (7.0 - 10.0).

Model conventions: layer `name` is KiCad's canonical id ("F.Cu", "In1.Cu", "B.Mask", "" for a
dielectric slot), `user_name` the display name ("top_cu" on the ecc83 demo), `kind` one of
KINDS, thicknesses in millimetres, colours as written ("Green", "#00ff00" or "#rrggbbaa" from the
JSON floats).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from kiwrap.formats.reports._common import load_json
from kiwrap.formats.reports.stats import parse_quantity
from kiwrap.formats.sexpr import Atom, Node, loads

__all__ = [
    "KINDS",
    "Stackup",
    "StackupLayer",
    "parse_stackup_board",
    "parse_stackup_csv",
    "parse_stackup_json",
]

KINDS: tuple[str, ...] = ("copper", "dielectric", "soldermask", "solderpaste", "silkscreen", "unknown")

_PROTO_KIND = {
    "BSLT_COPPER": "copper",
    "BSLT_DIELECTRIC": "dielectric",
    "BSLT_SILKSCREEN": "silkscreen",
    "BSLT_SOLDERMASK": "soldermask",
    "BSLT_SOLDERPASTE": "solderpaste",
}
_PROTO_DIELECTRIC = {"BSDT_CORE": "core", "BSDT_PREPREG": "prepreg", "BSDT_NONE": None, "BSDT_UNKNOWN": None}
_PROTO_EDGE = {"BECT_NONE": None, "BECT_PLAIN": "plain", "BECT_BEVELED": "bevelled", "BECT_UNKNOWN": None}
_BOARD_TYPE_KIND = {
    "copper": "copper",
    "core": "dielectric",
    "prepreg": "dielectric",
    "top silk screen": "silkscreen",
    "bottom silk screen": "silkscreen",
    "top solder paste": "solderpaste",
    "bottom solder paste": "solderpaste",
    "top solder mask": "soldermask",
    "bottom solder mask": "soldermask",
}
_CSV_TYPE_DIELECTRIC = {"core", "prepreg"}


@dataclass(frozen=True)
class StackupLayer:
    """One physical layer. Dielectric slots may hold several `sublayers` (prepreg stacks); their
    own `thickness_mm` is the slot total and `material`/`epsilon_r`/`loss_tangent` those of the
    first sublayer."""

    name: str  # canonical KiCad layer id ("F.Cu", "In2.Cu", "B.Mask"); "" for dielectric slots
    user_name: str  # display name; "dielectric 1" for slots
    kind: str  # one of KINDS
    thickness_mm: float | None = None
    material: str | None = None
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    color: str | None = None
    dielectric_type: str | None = None  # "core" | "prepreg" for dielectric slots
    enabled: bool = True
    thickness_locked: bool = False
    sublayers: tuple[StackupLayer, ...] = ()

    @property
    def is_copper(self) -> bool:
        return self.kind == "copper"


@dataclass(frozen=True)
class Stackup:
    source: str  # "json" | "csv" | "board"
    layers: tuple[StackupLayer, ...]
    finish: str | None = None  # "None", "ENIG", "HAL SnPb", ... as KiCad spells it
    impedance_controlled: bool = False
    edge_connector: str | None = None  # None | "plain" | "bevelled"
    castellated_pads: bool = False
    edge_plating: bool = False
    has_stackup_section: bool = True  # board source only: False when derived from `(layers ...)`
    raw: Any = field(default=None, repr=False)

    @property
    def copper_layers(self) -> tuple[StackupLayer, ...]:
        return tuple(layer for layer in self.layers if layer.is_copper and layer.enabled)

    @property
    def copper_count(self) -> int:
        return len(self.copper_layers)

    @property
    def total_thickness_mm(self) -> float | None:
        """Sum of the enabled layers' thicknesses; None when no layer has a thickness at all."""
        values = [layer.thickness_mm for layer in self.layers if layer.enabled and layer.thickness_mm]
        return round(sum(values), 6) if values else None

    def layer(self, name: str) -> StackupLayer | None:
        return next((layer for layer in self.layers if name in (layer.name, layer.user_name)), None)


# --- canonical layer names -----------------------------------------------------------------------

_BL_RE = re.compile(r"^BL_(?P<body>.+)$")


def proto_layer_name(token: str) -> str:
    """`BL_F_Cu` -> "F.Cu", `BL_In12_Cu` -> "In12.Cu", `BL_F_SilkS` -> "F.SilkS", `BL_UNDEFINED`
    / `BL_UNKNOWN` -> "" (the proto enum names are the KiCad layer ids with "." -> "_")."""
    m = _BL_RE.match(token or "")
    if not m or m.group("body") in ("UNDEFINED", "UNKNOWN"):
        return ""
    body = m.group("body")
    return body.replace("_", ".", 1) if "_" in body else body


def _float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nm_to_mm(thickness: Any) -> float | None:
    """proto `Distance {valueNm: "35000"}` -> 0.035; missing/empty -> None."""
    if not isinstance(thickness, dict):
        return None
    v = _float(thickness.get("valueNm"))
    return None if v is None else round(v / 1e6, 9)


def _color_hex(color: Any) -> str | None:
    """proto `Color {r,g,b,a}` (floats 0-1, defaults omitted) -> "#rrggbb[aa]". A colour with
    no r/g/b key at all (`{"a": 1}`) is what KiCad writes for NAMED colours ("White", "Green" on
    the ecc83 demo: the CSV carries the name, the JSON only an opaque alpha) -> None, not black."""
    if not isinstance(color, dict) or not any(k in color for k in ("r", "g", "b")):
        return None
    r, g, b = (_float(color.get(k)) or 0.0 for k in ("r", "g", "b"))
    a = _float(color.get("a"))
    hexes = f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
    if a is not None and a < 1.0:
        hexes += f"{round(a * 255):02x}"
    return hexes


# --- JSON -----------------------------------------------------------------------------------------


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    """The JSON object under `key`, or an empty dict when it is absent or not an object."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def parse_stackup_json(text: str) -> Stackup:
    """Parse `pcb export stackup --format json` (the proto-JSON `BoardStackup`)."""
    data = load_json(text)
    layers: list[StackupLayer] = []
    for raw in data.get("layers", ()) or ():
        if not isinstance(raw, dict):
            continue
        kind = _PROTO_KIND.get(str(raw.get("type", "")), "unknown")
        name = proto_layer_name(str(raw.get("layer", "")))
        user_name = str(raw.get("userName") or name)
        thickness = _nm_to_mm(raw.get("thickness"))
        material = raw.get("materialName") or None
        eps = loss = None
        dielectric_type = None
        subs: list[StackupLayer] = []
        locked = False
        if isinstance(raw.get("dielectric"), dict):
            d = raw["dielectric"]
            dielectric_type = _PROTO_DIELECTRIC.get(str(d.get("type", "")))
            for i, sub in enumerate(d.get("layer", ()) or ()):
                if not isinstance(sub, dict):
                    continue
                subs.append(
                    StackupLayer(
                        name="",
                        user_name=f"{user_name or 'dielectric'} [{i + 1}]",
                        kind="dielectric",
                        thickness_mm=_nm_to_mm(sub.get("thickness")),
                        material=sub.get("materialName") or None,
                        epsilon_r=_float(sub.get("epsilonR")),
                        loss_tangent=_float(sub.get("lossTangent")),
                        dielectric_type=dielectric_type,
                        thickness_locked=bool(sub.get("thicknessLocked", False)),
                    )
                )
            if subs:
                material = material or subs[0].material
                eps, loss = subs[0].epsilon_r, subs[0].loss_tangent
                locked = subs[0].thickness_locked
                if thickness is None or len(subs) > 1:
                    # the slot's own `thickness` is the FIRST sublayer's, not the total (nightly
                    # 4083, jetson demo: 70000 nm for a 2 x 0.07 mm prepreg slot); sum them
                    total = [s.thickness_mm for s in subs if s.thickness_mm]
                    thickness = round(sum(total), 9) if total else thickness
            if not user_name:
                user_name = f"dielectric {sum(1 for x in layers if x.kind == 'dielectric') + 1}"
        elif isinstance(raw.get("soldermask"), dict):
            sm = raw["soldermask"]
            material = material or sm.get("materialName") or None
            eps, loss = _float(sm.get("epsilonR")), _float(sm.get("lossTangent"))
            thickness = thickness if thickness is not None else _nm_to_mm(sm.get("thickness"))
        elif isinstance(raw.get("silkscreen"), dict):
            material = material or raw["silkscreen"].get("materialName") or None
        layers.append(
            StackupLayer(
                name=name,
                user_name=user_name,
                kind=kind,
                thickness_mm=thickness,
                material=material,
                epsilon_r=eps,
                loss_tangent=loss,
                color=_color_hex(raw.get("color")),
                dielectric_type=dielectric_type,
                enabled=bool(raw.get("enabled", True)),
                thickness_locked=locked,
                sublayers=tuple(subs) if len(subs) > 1 else (),
            )
        )
    finish_raw = _section(data, "finish").get("typeName")
    finish = finish_raw if isinstance(data.get("finish"), dict) else None
    edge = _section(data, "edge")
    connector = _section(edge, "connector")
    plating = _section(edge, "plating")
    impedance = _section(data, "impedance")
    return Stackup(
        source="json",
        layers=tuple(layers),
        finish=str(finish) if finish is not None else None,
        impedance_controlled=bool(impedance.get("isControlled", False)),
        edge_connector=_PROTO_EDGE.get(str(connector.get("type", "BECT_NONE"))),
        castellated_pads=False,  # `Castellation` is reserved/unused in the proto (never set by KiCad)
        edge_plating=bool(plating.get("hasEdgePlating", False)),
        raw=data,
    )


# --- CSV ------------------------------------------------------------------------------------------


def _csv_kind(type_cell: str) -> str:
    t = type_cell.strip().lower()
    if t in _CSV_TYPE_DIELECTRIC:
        return "dielectric"
    return _BOARD_TYPE_KIND.get(t, "unknown")


def parse_stackup_csv(text: str) -> Stackup:
    """Parse `pcb export stackup --format csv` (layer table + blank line + property table)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    layers: list[StackupLayer] = []
    props: dict[str, str] = {}
    section = "layers"
    header: list[str] = []
    for row in rows:
        if not row or all(not c.strip() for c in row):
            section = "props"
            header = []
            continue
        if not header:
            header = [c.strip().lower() for c in row]
            continue
        cells = dict(zip(header, row, strict=False))
        if section == "props" or header[:2] == ["property", "value"]:
            props[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
            continue
        type_cell = cells.get("type", "")
        kind = _csv_kind(type_cell)
        thickness = None
        if cells.get("thickness", "").strip():
            try:
                thickness = parse_quantity(cells["thickness"])[0]
            except ValueError:
                thickness = None
        user_name = cells.get("layer", "").strip()
        sub = cells.get("sublayer", "").strip()
        layer = StackupLayer(
            name="" if kind == "dielectric" else user_name,
            user_name=user_name,
            kind=kind,
            thickness_mm=thickness,
            material=cells.get("material", "").strip() or None,
            epsilon_r=_float(cells.get("epsilonr", "").strip()),
            loss_tangent=_float(cells.get("losstg", "").strip()),
            color=cells.get("color", "").strip() or None,
            dielectric_type=type_cell.strip().lower() if kind == "dielectric" else None,
            thickness_locked=cells.get("locked", "").strip().lower() in ("1", "true", "yes", "locked"),
        )
        if sub and layers and layers[-1].kind == "dielectric" and layers[-1].user_name == user_name:
            parent = layers[-1]
            subs = (*parent.sublayers, layer) if parent.sublayers else (parent, layer)
            total = [s.thickness_mm for s in subs if s.thickness_mm]
            layers[-1] = StackupLayer(
                name=parent.name,
                user_name=parent.user_name,
                kind=parent.kind,
                thickness_mm=round(sum(total), 9) if total else None,
                material=parent.material,
                epsilon_r=parent.epsilon_r,
                loss_tangent=parent.loss_tangent,
                color=parent.color,
                dielectric_type=parent.dielectric_type,
                thickness_locked=parent.thickness_locked,
                sublayers=subs,
            )
            continue
        layers.append(layer)
    finish = props.get("Finish")
    return Stackup(
        source="csv",
        layers=tuple(layers),
        finish=finish,
        impedance_controlled=props.get("Impedance controlled", "").strip().lower() in ("yes", "true", "1"),
        edge_connector=_csv_edge(props.get("Edge connector")),
        castellated_pads=props.get("Castellated pads", "").strip().lower() in ("yes", "true", "1"),
        edge_plating=props.get("Edge plating", "").strip().lower() in ("yes", "true", "1"),
        raw={"layers_rows": rows, "properties": props},
    )


def _csv_edge(value: str | None) -> str | None:
    v = (value or "").strip().lower()
    if not v or v in ("no", "none", "false"):
        return None
    return "bevelled" if "bevel" in v else "plain"


# --- board file -----------------------------------------------------------------------------------

_CANONICAL_LAYER_RE = re.compile(r"^(F|B|In\d+)\.(Cu|Mask|Paste|SilkS|Adhes)$")


def _node_str(node: Node | None) -> str | None:
    if node is None:
        return None
    atoms = node.atoms
    return atoms[0].as_str() if atoms else None


def _node_float(node: Node | None) -> float | None:
    if node is None:
        return None
    atoms = node.atoms
    return atoms[0].as_float() if atoms else None


def _split_sublayers(layer: Node) -> list[list[Node]]:
    """`(layer "dielectric 3" (type ...) (thickness ..) ... addsublayer (thickness ..) ...)` ->
    one property list per physical sublayer (the bare `addsublayer` atom starts the next one)."""
    groups: list[list[Node]] = [[]]
    for child in layer.args:
        if isinstance(child, Atom) and child.kind == "symbol" and child.value == "addsublayer":
            groups.append([])
        elif isinstance(child, Node):
            groups[-1].append(child)
    return groups


def _props(nodes: list[Node]) -> dict[str, Node]:
    return {n.head: n for n in nodes}


def parse_stackup_board(text: str) -> Stackup:
    """Read the stackup out of a `.kicad_pcb` (`(setup (stackup ...))`, 7.0-11 syntax). Boards
    without the section get copper layers from `(layers ...)` and `has_stackup_section=False`."""
    root = loads(text)
    setup = root.find("setup")
    stackup = setup.find("stackup") if setup is not None else None
    if stackup is None:
        layers_node = root.find("layers")
        copper: list[StackupLayer] = []
        for entry in layers_node.nodes if layers_node is not None else []:
            vals = [a.value for a in entry.children if isinstance(a, Atom) and a.kind != "comment"]
            # `(0 "F.Cu" signal "top_cu")`: id, canonical name, type, optional user name (no head)
            if (
                len(vals) >= 3
                and vals[1].endswith(".Cu")
                and vals[2] in ("signal", "power", "mixed", "jumper")
            ):
                copper.append(
                    StackupLayer(name=vals[1], user_name=vals[3] if len(vals) > 3 else vals[1], kind="copper")
                )
        return Stackup(source="board", layers=tuple(copper), has_stackup_section=False, raw=None)

    layers: list[StackupLayer] = []
    finish: str | None = None
    impedance = castellated = plating = False
    edge_connector: str | None = None
    for child in stackup.nodes:
        head = child.head
        if head == "layer":
            atoms = child.atoms
            user_name = atoms[0].as_str() if atoms else ""
            groups = _split_sublayers(child)
            first = _props(groups[0])
            type_cell = (_node_str(first.get("type")) or "").strip()
            kind = _BOARD_TYPE_KIND.get(type_cell.lower(), "unknown")
            if kind == "unknown" and _CANONICAL_LAYER_RE.match(user_name):
                kind = "copper" if user_name.endswith(".Cu") else "unknown"
            subs: list[StackupLayer] = []
            for i, group in enumerate(groups):
                p = _props(group)
                subs.append(
                    StackupLayer(
                        name="",
                        user_name=f"{user_name} [{i + 1}]",
                        kind="dielectric",
                        thickness_mm=_node_float(p.get("thickness")),
                        material=_node_str(p.get("material")),
                        epsilon_r=_node_float(p.get("epsilon_r")),
                        loss_tangent=_node_float(p.get("loss_tangent")),
                        dielectric_type=type_cell.lower() if kind == "dielectric" else None,
                        thickness_locked=_is_locked(p.get("thickness")),
                    )
                )
            total = [s.thickness_mm for s in subs if s.thickness_mm]
            layers.append(
                StackupLayer(
                    name=user_name if kind != "dielectric" else "",
                    user_name=user_name,
                    kind=kind,
                    thickness_mm=(round(sum(total), 9) if total else None)
                    if len(subs) > 1
                    else _node_float(first.get("thickness")),
                    material=_node_str(first.get("material")),
                    epsilon_r=_node_float(first.get("epsilon_r")),
                    loss_tangent=_node_float(first.get("loss_tangent")),
                    color=_node_str(first.get("color")),
                    dielectric_type=type_cell.lower() if kind == "dielectric" else None,
                    thickness_locked=_is_locked(first.get("thickness")),
                    sublayers=tuple(subs) if len(subs) > 1 else (),
                )
            )
        elif head == "copper_finish":
            finish = _node_str(child)
        elif head == "dielectric_constraints":
            impedance = (_node_str(child) or "") == "yes"
        elif head == "edge_connector":
            v = _node_str(child) or ""
            edge_connector = "bevelled" if v.startswith("bevel") else ("plain" if v == "yes" else None)
        elif head == "castellated_pads":
            castellated = (_node_str(child) or "") == "yes"
        elif head == "edge_plating":
            plating = (_node_str(child) or "") == "yes"
    return Stackup(
        source="board",
        layers=tuple(layers),
        finish=finish,
        impedance_controlled=impedance,
        edge_connector=edge_connector,
        castellated_pads=castellated,
        edge_plating=plating,
        has_stackup_section=True,
        raw=None,
    )


def _is_locked(thickness: Node | None) -> bool:
    """`(thickness 0.2 locked)`."""
    if thickness is None:
        return False
    return any(a.kind == "symbol" and a.value == "locked" for a in thickness.atoms)
