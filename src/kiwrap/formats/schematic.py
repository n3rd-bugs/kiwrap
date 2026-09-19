"""Semantic, read-only view of a `.kicad_sch` (and its sheet hierarchy) over the lossless CST.

`SchematicFile.load(path)` gives symbols with *absolute* pin end points, the resolved library
symbols (`lib_symbols`, `extends` flattened, units and body styles kept), wires, buses and bus
entries (read-only), labels of every kind, junctions, no-connects, sheets with their pins and
the per-path instance data (`reference`/`unit` per sheet path). `Hierarchy.load(root)` walks the
sheet files and yields one `SheetInstance` per sheet *path*, so a sheet file placed twice appears
twice with different references — the shape `kicad-cli sch export netlist` reports. The CST stays
reachable (`.root`, `.node`) so untouched tokens survive edits.

Pin end points implement docs/VERIFICATION.md claim 3 (verified 12/12 on 8.0.1, 9.0.5 and 10.0.0):
for a symbol at `(at X Y rot)` and a library pin at `(at lx ly angle)` (library frame, Y-up) the
sheet position is rot0 `(X+lx, Y-ly)`, rot90 `(X-ly, Y-lx)`, rot180 `(X-lx, Y+ly)`, rot270
`(X+ly, Y+lx)`; `(mirror x)` negates the Y offset, `(mirror y)` the X offset, after the rotation.
KiCad's parser inverts the library Y (`parseXY(true)`) and applies `TRANSFORM(0,1,-1,0)` for 90°
etc.; the four formulas are that composition.

Dialects handled (verified on every demo schematic of 7.0.6 – 10.99, conformance test T2):

* `(generator eeschema)` bare (7) vs quoted (8+); `(tstamp)`/`(uuid)` both accepted;
* pins hidden as a bare `hide` (7/8) or `(hide yes)` (9+); properties/effects likewise;
* `(power)` (≤ 8), `(power global)` / `(power local)` (9+ local power symbols) on lib symbols;
* `(instances (project … (path "/uuid" (reference "R1") (unit 1))))` on 7+ symbols and
  `(path "/uuid" (page "2"))` on sheets; symbols without instances fall back to the `Reference`
  property and `(unit N)`;
* sheet name/file as `Sheetname`/`Sheetfile` properties (7+) and the 6.0 `Sheet name`/
  `Sheet file` spelling;
* placed symbols pointing at a renamed embedded symbol through `(lib_name …)`;
* lib symbols in `lib_symbols` keyed `"Lib:Name"` (with `(extends "Parent")` resolved inside the
  same library nickname) and unit blocks named `<Name>_<unit>_<bodystyle>`.

Buses are parsed (`buses`, `bus_entries`, bus labels are recognised by their `[a..b]` / `{…}` text)
but bus members are NOT resolved into nets — that is the Phase 2 item of DECISIONS.md D10; the
connectivity analysis reports them as such.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.formats.geometry import Vec2
from kiwrap.formats.sexpr import Node, load, loads

__all__ = [
    "Bus",
    "BusEntry",
    "Hierarchy",
    "Junction",
    "Label",
    "LibPin",
    "LibSymbol",
    "NoConnect",
    "SchSymbol",
    "SchematicFile",
    "Sheet",
    "SheetInstance",
    "SheetPin",
    "SymbolInstance",
    "SymbolPin",
    "Wire",
    "is_bus_label",
    "pin_sheet_position",
    "unit_letter",
]

_TRUE = {"yes", "true", "1"}
_UNIT_RE = re.compile(r"^(?P<base>.*)_(?P<unit>\d+)_(?P<style>\d+)$")
_BUS_VECTOR_RE = re.compile(r".*\[\s*\d+\s*\.\.\s*\d+\s*\]\s*$")
_BUS_GROUP_RE = re.compile(r"^[^{}]*\{.*\}\s*$")
_LABEL_HEADS = ("label", "global_label", "hierarchical_label", "netclass_flag", "directive_label")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in ("no", "false", "0"):
        return False
    return default


def _flag(node: Node, name: str) -> bool:
    for a in node.atoms:
        if a.kind == "symbol" and a.value == name:
            return True
    return _bool(node.get(name))


def _uuid(node: Node) -> str:
    return node.get("uuid") or node.get("tstamp") or ""


def _at(node: Node) -> tuple[Vec2, float]:
    at = node.find("at")
    if at is None:
        return Vec2(0.0, 0.0), 0.0
    atoms = at.atoms
    x = atoms[0].as_float() if len(atoms) > 0 else 0.0
    y = atoms[1].as_float() if len(atoms) > 1 else 0.0
    rot = atoms[2].as_float() if len(atoms) > 2 and atoms[2].kind == "number" else 0.0
    return Vec2(x, y), rot


def _pts(node: Node) -> list[Vec2]:
    pts = node.find("pts")
    if pts is None:
        return []
    out: list[Vec2] = []
    for xy in pts.find_all("xy"):
        a = xy.atoms
        if len(a) >= 2:
            out.append(Vec2(a[0].as_float(), a[1].as_float()))
    return out


def _properties(node: Node) -> dict[str, str]:
    out: dict[str, str] = {}
    for prop in node.find_all("property"):
        a = prop.atoms
        if a:
            out[a[0].value] = a[1].value if len(a) > 1 else ""
    return out


def is_bus_label(text: str) -> bool:
    """`NAME[0..7]` vector buses and `{A B C}` / `NAME{A B}` group buses (SCH_CONNECTION rules)."""
    return bool(_BUS_VECTOR_RE.match(text) or _BUS_GROUP_RE.match(text))


def unit_letter(unit: int) -> str:
    """KiCad's default sub-reference: 1 -> A … 26 -> Z, 27 -> AA (LIB_SYMBOL::LetterSubReference)."""
    if unit <= 0:
        return ""
    out = ""
    u = unit
    while u > 0:
        u, rem = divmod(u - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def pin_sheet_position(symbol_pos: Vec2, rotation: float, mirror: str | None, lib_pos: Vec2) -> Vec2:
    """Claim 3 (module docstring): sheet position of a library pin at `lib_pos` (Y-up)."""
    lx, ly = lib_pos.x, lib_pos.y
    r = int(round(rotation)) % 360
    if r == 0:
        dx, dy = lx, -ly
    elif r == 90:
        dx, dy = -ly, -lx
    elif r == 180:
        dx, dy = -lx, ly
    elif r == 270:
        dx, dy = ly, lx
    else:  # non-orthogonal rotations are not legal for symbols; fall back to the general form
        a = math.radians(r)
        dx = lx * math.cos(a) + ly * math.sin(a)
        dy = -(-lx * math.sin(a) + ly * math.cos(a))
    if mirror == "x":
        dy = -dy
    elif mirror == "y":
        dx = -dx
    # KiCad keeps nanometre integers; rounding to 1e-6 mm removes binary float noise (50.8 - 3.81)
    return Vec2(symbol_pos.x + dx, symbol_pos.y + dy).rounded(6)


# ----------------------------------------------------------------------------------------------
# library symbols
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LibPin:
    number: str
    name: str
    type: str  # electrical type: passive, input, power_in, …
    shape: str  # graphic style: line, inverted, clock, …
    position: Vec2  # library frame (Y-up): the connection point (claim 3)
    length: float
    angle: float
    hidden: bool
    unit: int  # 0 = common to all units
    body_style: int  # 0 = common to both De Morgan styles
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def shown_name(self) -> str:
        """`~` means "no name" in KiCad's pin name field."""
        return "" if self.name == "~" else self.name

    @property
    def shown_number(self) -> str:
        """`~` as a pin number is shown as empty by KiCad 9+ (`SCH_PIN::GetShownNumber`); 7 keeps
        the tilde and 8 keeps it in net names only — the newest behaviour is followed."""
        return "" if self.number == "~" else self.number


@dataclass(frozen=True)
class LibSymbol:
    """A `lib_symbols` entry with `extends` flattened: `pins` are the resolved pins of every unit."""

    name: str  # as written in lib_symbols, normally "Lib:Name"
    extends: str | None
    power: str | None  # None | "global" | "local"
    properties: dict[str, str]
    pins: tuple[LibPin, ...]
    unit_count: int
    in_bom: bool = True
    on_board: bool = True
    exclude_from_sim: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def is_power(self) -> bool:
        return self.power is not None

    @property
    def reference_prefix(self) -> str:
        return self.properties.get("Reference", "")

    def pins_for(self, unit: int, body_style: int = 1) -> list[LibPin]:
        """Pins visible for `unit` / `body_style` (unit 0 and body style 0 blocks are shared)."""
        return [p for p in self.pins if p.unit in (0, unit) and p.body_style in (0, body_style)]


def _parse_lib_pin(node: Node, unit: int, body_style: int) -> LibPin:
    atoms = node.atoms
    ptype = atoms[0].value if atoms else "passive"
    shape = atoms[1].value if len(atoms) > 1 else "line"
    pos, angle = _at(node)
    name_node, number_node = node.find("name"), node.find("number")
    name = name_node.atoms[0].value if name_node is not None and name_node.atoms else ""
    number = number_node.atoms[0].value if number_node is not None and number_node.atoms else ""
    return LibPin(
        number=number,
        name=name,
        type=ptype,
        shape=shape,
        position=pos,
        length=float(node.get("length") or 0.0),
        angle=angle,
        hidden=_flag(node, "hide"),
        unit=unit,
        body_style=body_style,
        node=node,
    )


def _own_pins(node: Node) -> list[LibPin]:
    """Pins declared directly in a symbol node plus those of its `_U_S` unit blocks."""
    pins = [_parse_lib_pin(p, 0, 0) for p in node.find_all("pin")]
    for sub in node.find_all("symbol"):
        sa = sub.atoms
        subname = sa[0].value if sa else ""
        m = _UNIT_RE.match(subname)
        unit, style = (int(m.group("unit")), int(m.group("style"))) if m else (0, 0)
        pins.extend(_parse_lib_pin(p, unit, style) for p in sub.find_all("pin"))
    return pins


def _unit_count(node: Node, pins: list[LibPin]) -> int:
    units = {p.unit for p in pins if p.unit > 0}
    for sub in node.find_all("symbol"):
        sa = sub.atoms
        m = _UNIT_RE.match(sa[0].value if sa else "")
        if m and int(m.group("unit")) > 0:
            units.add(int(m.group("unit")))
    return max(units) if units else 1


def _power_kind(node: Node) -> str | None:
    power = node.find("power")
    if power is None:
        return None
    atoms = power.atoms
    if atoms and atoms[0].value == "local":
        return "local"
    return "global"


def parse_lib_symbols(lib_symbols: Node | None) -> dict[str, LibSymbol]:
    """Parse `(lib_symbols …)`, resolving `extends` chains (child properties override, parent
    supplies the drawing units and pins when the child declares none — a derived symbol cannot
    carry its own graphics in KiCad ≤ 10)."""
    raw: dict[str, Node] = {}
    if lib_symbols is not None:
        for sym in lib_symbols.find_all("symbol"):
            a = sym.atoms
            if a:
                raw[a[0].value] = sym
    resolved: dict[str, LibSymbol] = {}

    def resolve(name: str, chain: tuple[str, ...] = ()) -> LibSymbol | None:
        if name in resolved:
            return resolved[name]
        node = raw.get(name)
        if node is None or name in chain:
            return None
        parent_name = node.get("extends")
        parent: LibSymbol | None = None
        if parent_name:
            lib = name.split(":", 1)[0] if ":" in name else ""
            candidates = [f"{lib}:{parent_name}", parent_name] if lib else [parent_name]
            for cand in candidates:
                parent = resolve(cand, (*chain, name))
                if parent is not None:
                    break
        pins = _own_pins(node)
        props = _properties(node)
        power = _power_kind(node)
        if parent is not None:
            if not pins:
                pins = list(parent.pins)
            merged = dict(parent.properties)
            merged.update(props)
            props = merged
            power = power or parent.power
        sym = LibSymbol(
            name=name,
            extends=parent_name,
            power=power,
            properties=props,
            pins=tuple(pins),
            unit_count=max(_unit_count(node, pins), parent.unit_count if parent else 1),
            in_bom=_bool(node.get("in_bom"), True),
            on_board=_bool(node.get("on_board"), True),
            exclude_from_sim=_bool(node.get("exclude_from_sim"), False),
            node=node,
        )
        resolved[name] = sym
        return sym

    for name in raw:
        resolve(name)
    return resolved


# ----------------------------------------------------------------------------------------------
# sheet items
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolInstance:
    project: str
    path: str  # "/<root uuid>[/<sheet uuid>…]"
    reference: str
    unit: int


@dataclass(frozen=True)
class SymbolPin:
    """A pin of a placed symbol with its absolute sheet position."""

    number: str
    name: str
    type: str
    position: Vec2
    hidden: bool
    lib_pin: LibPin
    uuid: str = ""
    alternate: str | None = None

    @property
    def shown_name(self) -> str:
        if self.alternate:
            return self.alternate
        return "" if self.name == "~" else self.name

    @property
    def shown_number(self) -> str:
        return self.lib_pin.shown_number


@dataclass(frozen=True)
class SchSymbol:
    lib_id: str
    lib_name: str | None
    lib_symbol: LibSymbol | None
    position: Vec2
    rotation: float
    mirror: str | None  # None | "x" | "y"
    unit: int
    body_style: int
    reference: str  # the Reference property (per-path values in `instances`)
    value: str
    footprint: str
    properties: dict[str, str]
    in_bom: bool
    on_board: bool
    dnp: bool
    exclude_from_sim: bool
    uuid: str
    pins: tuple[SymbolPin, ...]
    instances: tuple[SymbolInstance, ...]
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def is_power(self) -> bool:
        """A power symbol: the library says so, or (legacy files) a `#PWR…` reference with a
        power_in pin."""
        if self.lib_symbol is not None and self.lib_symbol.power is not None:
            return True
        if (
            self.lib_symbol is None
            or self.lib_symbol.node is None
            or self.lib_symbol.node.find("power") is None
        ):
            return self.reference.startswith("#PWR") and any(p.type == "power_in" for p in self.pins)
        return False

    @property
    def power_kind(self) -> str | None:
        if self.lib_symbol is not None and self.lib_symbol.power is not None:
            return self.lib_symbol.power
        return "global" if self.is_power else None

    def instance_for(self, path: str) -> SymbolInstance | None:
        return next((i for i in self.instances if i.path == path), None)

    def reference_for(self, path: str) -> str:
        inst = self.instance_for(path)
        return inst.reference if inst is not None else self.reference

    def unit_for(self, path: str) -> int:
        inst = self.instance_for(path)
        return inst.unit if inst is not None else self.unit

    def pins_for(self, path: str) -> list[SymbolPin]:
        """Pins of the unit placed on `path` (unit-0 pins included)."""
        unit = self.unit_for(path)
        return [
            p
            for p in self.pins
            if p.lib_pin.unit in (0, unit) and p.lib_pin.body_style in (0, self.body_style)
        ]

    @property
    def unit_count(self) -> int:
        return self.lib_symbol.unit_count if self.lib_symbol is not None else 1


@dataclass(frozen=True)
class Wire:
    start: Vec2
    end: Vec2
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def points(self) -> tuple[Vec2, Vec2]:
        return (self.start, self.end)


@dataclass(frozen=True)
class Bus:
    start: Vec2
    end: Vec2
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class BusEntry:
    position: Vec2
    size: Vec2
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def end(self) -> Vec2:
        return self.position + self.size


@dataclass(frozen=True)
class Label:
    kind: str  # label | global_label | hierarchical_label | netclass_flag | directive_label
    text: str
    position: Vec2
    rotation: float
    shape: str | None  # input | output | bidirectional | tri_state | passive (global/hier)
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def is_bus(self) -> bool:
        return is_bus_label(self.text)

    @property
    def is_net_label(self) -> bool:
        return self.kind in ("label", "global_label", "hierarchical_label")


@dataclass(frozen=True)
class Junction:
    position: Vec2
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class NoConnect:
    position: Vec2
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SheetPin:
    name: str
    shape: str
    position: Vec2
    rotation: float
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Sheet:
    name: str
    file: str
    position: Vec2
    size: Vec2
    uuid: str
    pins: tuple[SheetPin, ...]
    pages: dict[str, str]  # parent path -> page number
    properties: dict[str, str]
    node: Node | None = field(default=None, repr=False, compare=False)


def _parse_symbol(node: Node, libs: dict[str, LibSymbol]) -> SchSymbol:
    lib_id = node.get("lib_id") or ""
    lib_name = node.get("lib_name")
    lib_symbol = None
    for key in (lib_name, lib_id):
        if key and key in libs:
            lib_symbol = libs[key]
            break
    pos, rot = _at(node)
    mirror_node = node.find("mirror")
    mirror = mirror_node.atoms[0].value if mirror_node is not None and mirror_node.atoms else None
    unit = int(node.get("unit") or 1)
    body_style = int(node.get("body_style") or node.get("convert") or 1)
    props = _properties(node)
    pin_uuids: dict[str, str] = {}
    pin_alternates: dict[str, str] = {}
    for pin in node.find_all("pin"):
        a = pin.atoms
        if a:
            pin_uuids[a[0].value] = _uuid(pin)
            alt = pin.get("alternate")
            if alt:
                pin_alternates[a[0].value] = alt
    pins: list[SymbolPin] = []
    if lib_symbol is not None:
        for lp in lib_symbol.pins:
            pins.append(
                SymbolPin(
                    number=lp.number,
                    name=lp.name,
                    type=lp.type,
                    position=pin_sheet_position(pos, rot, mirror, lp.position),
                    hidden=lp.hidden,
                    lib_pin=lp,
                    uuid=pin_uuids.get(lp.number, ""),
                    alternate=pin_alternates.get(lp.number),
                )
            )
    instances: list[SymbolInstance] = []
    inst_node = node.find("instances")
    if inst_node is not None:
        for project in inst_node.find_all("project"):
            pa = project.atoms
            pname = pa[0].value if pa else ""
            for path in project.find_all("path"):
                ppa = path.atoms
                instances.append(
                    SymbolInstance(
                        project=pname,
                        path=ppa[0].value if ppa else "",
                        reference=path.get("reference") or props.get("Reference", ""),
                        unit=int(path.get("unit") or unit),
                    )
                )
    return SchSymbol(
        lib_id=lib_id,
        lib_name=lib_name,
        lib_symbol=lib_symbol,
        position=pos,
        rotation=rot,
        mirror=mirror,
        unit=unit,
        body_style=body_style,
        reference=props.get("Reference", ""),
        value=props.get("Value", ""),
        footprint=props.get("Footprint", ""),
        properties=props,
        in_bom=_bool(node.get("in_bom"), True),
        on_board=_bool(node.get("on_board"), True),
        dnp=_bool(node.get("dnp"), False),
        exclude_from_sim=_bool(node.get("exclude_from_sim"), False),
        uuid=_uuid(node),
        pins=tuple(pins),
        instances=tuple(instances),
        node=node,
    )


def _parse_sheet(node: Node) -> Sheet:
    pos, _ = _at(node)
    size_node = node.find("size")
    sa = size_node.atoms if size_node is not None else []
    size = Vec2(sa[0].as_float(), sa[1].as_float()) if len(sa) >= 2 else Vec2(0.0, 0.0)
    props = _properties(node)
    pins: list[SheetPin] = []
    for pin in node.find_all("pin"):
        pa = pin.atoms
        ppos, prot = _at(pin)
        pins.append(
            SheetPin(
                name=pa[0].value if pa else "",
                shape=pa[1].value if len(pa) > 1 else "passive",
                position=ppos,
                rotation=prot,
                uuid=_uuid(pin),
                node=pin,
            )
        )
    pages: dict[str, str] = {}
    inst = node.find("instances")
    if inst is not None:
        for project in inst.find_all("project"):
            for path in project.find_all("path"):
                ppa = path.atoms
                if ppa:
                    pages[ppa[0].value] = path.get("page") or ""
    return Sheet(
        name=props.get("Sheetname", props.get("Sheet name", "")),
        file=props.get("Sheetfile", props.get("Sheet file", "")),
        position=pos,
        size=size,
        uuid=_uuid(node),
        pins=tuple(pins),
        pages=pages,
        properties=props,
        node=node,
    )


@dataclass
class SchematicFile:
    """One `.kicad_sch` file; see the module docstring."""

    path: Path | None
    root: Node
    stamp: int | None
    generator: str
    generator_version: str | None
    uuid: str
    lib_symbols: dict[str, LibSymbol]
    symbols: tuple[SchSymbol, ...]
    wires: tuple[Wire, ...]
    buses: tuple[Bus, ...]
    bus_entries: tuple[BusEntry, ...]
    labels: tuple[Label, ...]
    junctions: tuple[Junction, ...]
    no_connects: tuple[NoConnect, ...]
    sheets: tuple[Sheet, ...]
    paper: str | None = None
    title_block: dict[str, str] = field(default_factory=dict)
    sheet_instances: dict[str, str] = field(default_factory=dict)  # path -> page (root files)
    bus_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)  # (bus_alias "N" (members …))

    @classmethod
    def load(cls, path: str | Path) -> SchematicFile:
        p = Path(path)
        return cls.from_node(load(p), path=p)

    @classmethod
    def loads(cls, text: str, path: str | Path | None = None) -> SchematicFile:
        return cls.from_node(loads(text), path=Path(path) if path else None)

    @classmethod
    def from_node(cls, root: Node, path: Path | None = None) -> SchematicFile:
        if root.head != "kicad_sch":
            raise ValueError(f"not a KiCad schematic: top-level list is ({root.head or '?'} …)")
        stamp_text = root.get("version")
        libs = parse_lib_symbols(root.find("lib_symbols"))
        symbols: list[SchSymbol] = []
        wires: list[Wire] = []
        buses: list[Bus] = []
        entries: list[BusEntry] = []
        labels: list[Label] = []
        junctions: list[Junction] = []
        ncs: list[NoConnect] = []
        sheets: list[Sheet] = []
        for child in root.nodes:
            head = child.head
            if head == "symbol":
                symbols.append(_parse_symbol(child, libs))
            elif head == "wire":
                pts = _pts(child)
                for p0, p1 in zip(pts, pts[1:], strict=False):
                    wires.append(Wire(p0, p1, _uuid(child), child))
            elif head == "bus":
                pts = _pts(child)
                for p0, p1 in zip(pts, pts[1:], strict=False):
                    buses.append(Bus(p0, p1, _uuid(child), child))
            elif head == "bus_entry":
                pos, _ = _at(child)
                size_node = child.find("size")
                sa = size_node.atoms if size_node is not None else []
                size = Vec2(sa[0].as_float(), sa[1].as_float()) if len(sa) >= 2 else Vec2(0.0, 0.0)
                entries.append(BusEntry(pos, size, _uuid(child), child))
            elif head in _LABEL_HEADS:
                a = child.atoms
                pos, rot = _at(child)
                labels.append(
                    Label(head, a[0].value if a else "", pos, rot, child.get("shape"), _uuid(child), child)
                )
            elif head == "junction":
                pos, _ = _at(child)
                junctions.append(Junction(pos, _uuid(child), child))
            elif head == "no_connect":
                pos, _ = _at(child)
                ncs.append(NoConnect(pos, _uuid(child), child))
            elif head == "sheet":
                sheets.append(_parse_sheet(child))
        title: dict[str, str] = {}
        tb = root.find("title_block")
        if tb is not None:
            for c in tb.nodes:
                if c.head and c.atoms:
                    title[c.head] = " ".join(c.values())
        aliases: dict[str, tuple[str, ...]] = {}
        for ba in root.find_all("bus_alias"):
            a = ba.atoms
            members = ba.find("members")
            if a and members is not None:
                aliases[a[0].value] = tuple(members.values())
        pages: dict[str, str] = {}
        si = root.find("sheet_instances")
        if si is not None:
            for p in si.find_all("path"):
                pa = p.atoms
                if pa:
                    pages[pa[0].value] = p.get("page") or ""
        return cls(
            path=path,
            root=root,
            stamp=int(stamp_text) if stamp_text and stamp_text.isdigit() else None,
            generator=root.get("generator") or "",
            generator_version=root.get("generator_version"),
            uuid=root.get("uuid") or "",
            lib_symbols=libs,
            symbols=tuple(symbols),
            wires=tuple(wires),
            buses=tuple(buses),
            bus_entries=tuple(entries),
            labels=tuple(labels),
            junctions=tuple(junctions),
            no_connects=tuple(ncs),
            sheets=tuple(sheets),
            paper=root.get("paper"),
            title_block=title,
            sheet_instances=pages,
            bus_aliases=aliases,
        )

    # queries ----------------------------------------------------------------------------------
    def symbol(self, reference: str, path: str | None = None) -> SchSymbol | None:
        for s in self.symbols:
            if (s.reference_for(path) if path else s.reference) == reference:
                return s
        return None

    def power_symbols(self) -> list[SchSymbol]:
        return [s for s in self.symbols if s.is_power]

    def labels_of(self, kind: str) -> list[Label]:
        return [label for label in self.labels if label.kind == kind]


# ----------------------------------------------------------------------------------------------
# hierarchy
# ----------------------------------------------------------------------------------------------


@dataclass
class SheetInstance:
    """One sheet *path*: the schematic file as instantiated at `path`.

    `path` is the instance-style path `/<root uuid>[/<sheet uuid>…]` used by symbol `instances`;
    `names_path` the human-readable `/name/…/` form (`/` for the root) that kicad-cli prints in
    `sheetpath (names …)`; `tstamps_path` the `/uuid/…/` form of `sheetpath (tstamps …)`.
    """

    path: str
    names_path: str
    tstamps_path: str
    file: SchematicFile
    sheet: Sheet | None  # the (sheet …) element in the parent, None for the root
    parent: SheetInstance | None
    depth: int
    page: str = ""

    @property
    def name(self) -> str:
        return self.sheet.name if self.sheet is not None else ""

    def references(self) -> dict[str, SchSymbol]:
        return {s.reference_for(self.path): s for s in self.file.symbols}


@dataclass
class Hierarchy:
    """All sheet instances reachable from a root schematic, depth-first in sheet order."""

    root: SchematicFile
    instances: list[SheetInstance]
    files: dict[Path, SchematicFile]
    missing: list[tuple[str, str]]  # (parent path, sheet file) that could not be loaded

    @classmethod
    def load(cls, root_path: str | Path, *, max_depth: int = 32) -> Hierarchy:
        root_path = Path(root_path)
        files: dict[Path, SchematicFile] = {}
        missing: list[tuple[str, str]] = []

        def get(p: Path) -> SchematicFile | None:
            key = p.resolve()
            if key in files:
                return files[key]
            if not p.is_file():
                return None
            sch = SchematicFile.load(p)
            files[key] = sch
            return sch

        root = get(root_path)
        if root is None:
            raise FileNotFoundError(root_path)
        instances: list[SheetInstance] = []
        root_inst = SheetInstance(
            path="/" + root.uuid if root.uuid else "/",
            names_path="/",
            tstamps_path="/",
            file=root,
            sheet=None,
            parent=None,
            depth=0,
            page=root.sheet_instances.get("/", "1"),
        )
        instances.append(root_inst)

        def walk(inst: SheetInstance) -> None:
            if inst.depth >= max_depth:
                return
            base = inst.file.path.parent if inst.file.path else root_path.parent
            for sheet in inst.file.sheets:
                child_file = get(base / sheet.file) if sheet.file else None
                if child_file is None:
                    missing.append((inst.path, sheet.file))
                    continue
                child = SheetInstance(
                    path=f"{inst.path.rstrip('/')}/{sheet.uuid}",
                    names_path=f"{inst.names_path}{sheet.name}/",
                    tstamps_path=f"{inst.tstamps_path}{sheet.uuid}/",
                    file=child_file,
                    sheet=sheet,
                    parent=inst,
                    depth=inst.depth + 1,
                    page=sheet.pages.get(inst.path, ""),
                )
                instances.append(child)
                if any(a.file is child_file for a in _ancestors(inst)):
                    continue  # recursive sheet inclusion: do not descend forever
                walk(child)

        walk(root_inst)
        return cls(root=root, instances=instances, files=files, missing=missing)

    @classmethod
    def single(cls, sch: SchematicFile) -> Hierarchy:
        """A one-sheet hierarchy for a schematic treated as its own root (sheets not followed)."""
        inst = SheetInstance(
            path="/" + sch.uuid if sch.uuid else "/",
            names_path="/",
            tstamps_path="/",
            file=sch,
            sheet=None,
            parent=None,
            depth=0,
            page=sch.sheet_instances.get("/", "1"),
        )
        files = {sch.path.resolve(): sch} if sch.path else {}
        return cls(root=sch, instances=[inst], files=files, missing=[])

    def by_path(self) -> dict[str, SheetInstance]:
        return {i.path: i for i in self.instances}

    def children(self, inst: SheetInstance) -> Iterator[SheetInstance]:
        return (i for i in self.instances if i.parent is inst)

    def symbols(self) -> Iterator[tuple[SheetInstance, SchSymbol]]:
        for inst in self.instances:
            for s in inst.file.symbols:
                yield inst, s

    def bus_aliases(self) -> dict[str, tuple[str, ...]]:
        """Every `bus_alias` of every sheet file (KiCad resolves aliases project-wide)."""
        out: dict[str, tuple[str, ...]] = {}
        for sch in self.files.values() or [self.root]:
            out.update(sch.bus_aliases)
        out.update(self.root.bus_aliases)
        return out


def _ancestors(inst: SheetInstance) -> Iterator[SheetInstance]:
    p: SheetInstance | None = inst
    while p is not None:
        yield p
        p = p.parent
