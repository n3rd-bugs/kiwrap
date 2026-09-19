"""Schematic connectivity from the file alone: nets derived from wires, junctions, labels, power
symbols, sheet pins and no-connects, with dangling wire ends, unconnected pins and label-only nets.

This reproduces the rules of KiCad's `CONNECTION_GRAPH` (eeschema/connection_graph.cpp,
eeschema/sch_pin.cpp, eeschema/sch_connection.cpp at tag 10.0, read 2026-09-19; identical in
substance on master) closely enough to be diffed against `kicad-cli sch export netlist`
(conformance test T2 reports the agreement per KiCad version). What is encoded:

* Items connect where their *connection points* coincide exactly (`updateItemConnectivity`):
  wire end points, label anchors, symbol pin end points (claim 3 transform), no-connect markers,
  sheet pins. A junction also connects wires that pass *through* it (`GetBusesAndWires(point,
  true)` special case) and a label anywhere along a wire connects to it (verified empirically
  on kicad-cli 10.0.6 with the kit-dev-coldfire demo: label `JTAG_EN` sits mid-wire on a single
  wire and names the net `/JTAG_EN`; deleting it through the CST renames the net to
  `Net-(U102-JTAG_EN)`). A pin end resting on a wire's interior is NOT a connection
  (docs/VERIFICATION.md claim 2, negative control B) and is reported in `warnings`.
* Driver priority (`CONNECTION_SUBGRAPH::GetDriverPriority`): GLOBAL label 7 > global power pin 6
  > local power pin 5 > LOCAL label 4 > HIER label 3 > SHEET pin 2 > PIN 1 > `#`-reference pins
  0. A global power pin is a `power_in` pin of a global power symbol, or (legacy) a hidden
  `power_in` pin on any symbol (`SCH_PIN::IsGlobalPower`). Strong drivers are priority ≥ 3.
* Candidate ordering among equal priority (`ResolveDrivers`): power symbols before ordinary pins,
  output sheet pins before inputs, names without "-Pad" before generated ones, then alphabetical.
* Net names (`SCH_PIN::GetDefaultNetName`, `SCH_CONNECTION::recacheName`): power pins name the net
  after the symbol value (power symbols) or the pin name (legacy hidden pins); labels after their
  text with `/` escaped as `{slash}`; plain pins `Net-(<ref>[<unit>]-<pinname>[-Pad<n>])` or
  `Net-(<ref>-Pad<n>)`, `unconnected-(…-Pad<n>)` for a pin that is the only driver of a weakly
  driven net or carries a no-connect. Local labels, hierarchical labels, sheet pins and local
  power pins are prefixed with the sheet path (`/`, `/sheet name/…`); global labels, global power
  pins and ordinary pins are not.
* Same-sheet merging (`processSubGraphs`): strongly driven subgraphs on one sheet whose strong
  drivers share a name become one net. Across sheets a parent's sheet pin links to the child's
  hierarchical labels of the same name (`propagateToNeighbors`) and the whole chain takes the best
  driver: a global first, else a strong driver over a weak one, higher priority, shorter sheet path,
  lower name. Weakly driven subgraphs whose full name collides get a `_1`, `_2`, … suffix.
* Global power pins and global labels join by name across the whole hierarchy.

* Buses: a subgraph containing bus wires is a bus; its members come from the driving bus label
  (`bus_members`: vector `A[0..7]`, group `{A B}` / `P{A B}`); a net subgraph that reaches a bus
  through a bus entry (or a net-named sheet pin sitting on a bus) is a *bus neighbour* and, when
  its local name is one of the members, takes the member's full name — or gives the member its
  own name when it is driven by a global label / power pin (`propagate_bus_neighbors`).

Known gaps (reported, not silently wrong): bus aliases are not expanded; text variables (`${…}`) in label text are not
substituted; "duplicate pin numbers are jumpers" and stacked-pin de-duplication are not applied;
the order in which colliding weak names get their suffix follows file order, KiCad's follows its
spatial index, so `_1`/`_2` may be swapped between two otherwise identical nets.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kiwrap.formats.geometry import Vec2, point_segment_distance
from kiwrap.formats.schematic import (
    Hierarchy,
    Label,
    SchematicFile,
    SchSymbol,
    Sheet,
    SheetInstance,
    SheetPin,
    SymbolPin,
    Wire,
    unit_letter,
)

__all__ = [
    "BusMember",
    "Connectivity",
    "DerivedNet",
    "PinRef",
    "SheetPinRef",
    "WireEnd",
    "bus_members",
    "default_net_name",
    "escape_net_name",
    "pin_connectivity",
    "split_bus_label",
]

# CONNECTION_SUBGRAPH::PRIORITY
_P_NONE, _P_PIN, _P_SHEET_PIN, _P_HIER, _P_LOCAL, _P_LOCAL_POWER, _P_GLOBAL_POWER, _P_GLOBAL = range(8)
_STRONG = _P_HIER
_KEY_SCALE = 10000  # 0.1 µm buckets: file coordinates carry at most 4 decimals of a millimetre


def escape_net_name(text: str) -> str:
    """`EscapeString(…, CTX_NETNAME)`: `/` -> `{slash}`, CR/LF dropped."""
    return text.replace("/", "{slash}").replace("\n", "").replace("\r", "")


def _key(p: Vec2) -> tuple[int, int]:
    return (int(round(p.x * _KEY_SCALE)), int(round(p.y * _KEY_SCALE)))


def _on_interior(p: Vec2, w: Wire, eps: float = 1e-4) -> bool:
    if p.close_to(w.start, eps) or p.close_to(w.end, eps):
        return False
    return point_segment_distance(p, w.start, w.end) <= eps


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, i: int) -> None:
        self.parent.setdefault(i, i)

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:
            self.parent[i], i = root, self.parent[i]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ----------------------------------------------------------------------------------------------
# result models
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PinRef:
    """A pin of a placed symbol on one sheet path."""

    path: str
    names_path: str
    reference: str
    unit: int
    symbol: SchSymbol = field(compare=False, repr=False)
    pin: SymbolPin = field(compare=False, repr=False)

    @property
    def number(self) -> str:
        return self.pin.shown_number

    @property
    def name(self) -> str:
        return self.pin.shown_name

    @property
    def type(self) -> str:
        return self.pin.type

    @property
    def position(self) -> Vec2:
        return self.pin.position

    @property
    def is_global_power(self) -> bool:
        """`SCH_PIN::IsGlobalPower`: power_in on a global power symbol, or hidden power_in on a
        non-power symbol (legacy invisible power pins)."""
        if self.pin.type != "power_in":
            return False
        kind = self.symbol.power_kind
        if kind == "global":
            return True
        if kind == "local":
            return False
        return self.pin.hidden

    @property
    def is_local_power(self) -> bool:
        return self.pin.type == "power_in" and self.symbol.power_kind == "local"

    @property
    def in_netlist(self) -> bool:
        return self.symbol.on_board and not self.reference.startswith("#")


@dataclass(frozen=True)
class SheetPinRef:
    path: str  # the parent sheet path (where the pin lives)
    names_path: str
    sheet: Sheet
    pin: SheetPin
    child_path: str


@dataclass(frozen=True)
class WireEnd:
    path: str
    position: Vec2


@dataclass
class DerivedNet:
    """A net as derived from the file. `name` follows kicad-cli's naming (module docstring)."""

    name: str
    pins: list[PinRef]
    labels: list[tuple[str, Label]]  # (sheet path, label)
    sheet_pins: list[SheetPinRef]
    no_connect: bool
    is_bus: bool
    via_bus_entry: bool
    driver_kind: (
        str  # global_label | global_power | local_power | local_label | hier_label | sheet_pin | pin | none
    )
    strong: bool
    subgraphs: int = 1

    @property
    def netlist_pins(self) -> list[PinRef]:
        """Pins kicad-cli lists as nodes: not `#`-references, not excluded from board, one per
        (reference, number)."""
        seen: set[tuple[str, str]] = set()
        out: list[PinRef] = []
        for p in sorted(self.pins, key=lambda p: (p.reference, p.number)):
            if not p.in_netlist:
                continue
            k = (p.reference, p.number)
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    @property
    def label_only(self) -> bool:
        return not self.pins and bool(self.labels)


@dataclass
class Connectivity:
    nets: list[DerivedNet]
    dangling_wire_ends: list[WireEnd]
    unconnected_pins: list[PinRef]  # pins on a net with no other pin (NC-marked ones included)
    no_connect_pins: list[PinRef]  # pins on a subgraph with a no-connect MARKER (not NC-type pins)
    label_only_nets: list[DerivedNet]
    warnings: list[str]
    bus_count: int
    bus_entry_count: int
    #: (sheet names path, item uuid) -> derived net name for wires, buses, junctions and net labels
    #: (what `GetSchematicNetlist` lists per sheet instance over IPC; kw_sch_get uses it to put a
    #: `net` on wires / labels / junctions of the file provider)
    item_nets: dict[tuple[str, str], str] = field(default_factory=dict)

    def net_by_name(self) -> dict[str, DerivedNet]:
        return {n.name: n for n in self.nets}

    def net_of_pin(self, reference: str, number: str) -> DerivedNet | None:
        for n in self.nets:
            for p in n.pins:
                if p.reference == reference and p.number == number:
                    return n
        return None


# ----------------------------------------------------------------------------------------------
# bus labels
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BusMember:
    """One member of a bus label: `name` is what a net label must read to belong to it on the same
    sheet (`AN3`, `PWR.SCL`); `key` matches members across differently named buses in the
    hierarchy (`matchBusMember`): the vector index for vector buses, the unprefixed member name
    for group buses."""

    name: str
    key: int | str


def split_bus_label(
    text: str, aliases: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, list[BusMember]] | None:
    """`("vector"|"group", members)` for a bus label, None for a net label.

    Follows `NET_SETTINGS::ParseBusVector` / `ParseBusGroup` (common/project/net_settings.cpp,
    tag 10.0): `AN[0..7]` -> `AN0`…`AN7` (ascending, optional `+ - P N` suffix after `]`);
    `{A B C}` / `{A, B, C}` -> `A B C`; `PWR{A B}` -> `PWR.A PWR.B`; formatting braces after `~ _ ^`
    (`~{RST}`, `I2C_{SYS}{SCL SDA}`) belong to the name, not the member list; vectors inside a
    group expand in turn; a group member naming a bus alias (`(bus_alias "USB" (members "D_P"
    "D_N"))`, collected project-wide) expands to the alias members.
    """
    text = text.strip()
    m = _VECTOR_RE.match(text)
    if m and not _format_brace_before(text, text.index("[")):
        prefix, a, b, suffix = (
            m.group("prefix"),
            int(m.group("a")),
            int(m.group("b")),
            m.group("suffix") or "",
        )
        if a == b:
            return None
        if a > b:
            a, b = b, a
        return "vector", [BusMember(f"{prefix}{i}{suffix}", k) for k, i in enumerate(range(a, b + 1))]
    open_brace = _group_brace(text)
    if open_brace is None or not text.endswith("}"):
        return None
    prefix = text[:open_brace].strip()
    body = text[open_brace + 1 : -1]
    members: list[BusMember] = []
    for tok in body.replace(",", " ").split():  # space or comma separates members
        toks = list(aliases[tok]) if aliases and tok in aliases else [tok]
        for t in toks:
            sub = split_bus_label(t, aliases)
            names = [mm.name for mm in sub[1]] if sub else [t]
            for n in names:
                members.append(BusMember((prefix + "." + n) if prefix else n, n))
    return "group", members


def bus_members(text: str, aliases: Mapping[str, Sequence[str]] | None = None) -> list[str] | None:
    """Member names of a bus label (see `split_bus_label`), None for a net label."""
    parsed = split_bus_label(text, aliases)
    return [m.name for m in parsed[1]] if parsed else None


def _format_brace_before(text: str, i: int) -> bool:
    """True when position `i` sits inside a `~{…}` / `_{…}` / `^{…}` formatting group."""
    depth = 0
    for k, ch in enumerate(text[:i]):
        if ch == "{" and k > 0 and text[k - 1] in "~_^":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
    return depth > 0


def _group_brace(text: str) -> int | None:
    """Index of the `{` that opens a bus group member list (not a formatting brace), or None."""
    depth = 0
    for k, ch in enumerate(text):
        if ch == "{":
            if k > 0 and text[k - 1] in "~_^":
                depth += 1
            elif depth == 0:
                return k
        elif ch == "}" and depth > 0:
            depth -= 1
    return None


_VECTOR_RE = re.compile(r"^(?P<prefix>[^\[\]]*)\[(?P<a>\d+)\.\.(?P<b>\d+)\](?P<suffix>[+\-PN]*)$")


# ----------------------------------------------------------------------------------------------
# per-sheet items
# ----------------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Item:
    id: int
    kind: str  # wire | bus | junction | label | pin | nc | sheet_pin | bus_entry
    points: tuple[Vec2, ...]
    payload: Any


_NET_CLASS = {"wire", "pin", "nc", "bus_entry"}
_BUS_CLASS = {"bus"}


@dataclass(eq=False)
class _Subgraph:
    inst: SheetInstance
    items: list[_Item]
    pins: list[PinRef] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    sheet_pins: list[SheetPinRef] = field(default_factory=list)
    no_connect: bool = False
    is_bus: bool = False  # contains bus wires (or is driven by a bus label)
    bus_neighbors: list[tuple[_Subgraph, BusMember]] = field(default_factory=list)  # (bus, member)
    touches_bus: bool = False  # reaches a bus through a bus entry or a sheet pin on a bus
    # resolved
    priority: int = _P_NONE
    driver: Any = None
    driver_kind: str = "none"
    local_name: str = ""  # Name(true): without the sheet prefix
    full_name: str = ""  # with the prefix when applicable
    own_name: str = ""  # full_name as resolved from this subgraph's own driver (never overwritten)
    prefixed: bool = False
    strong: bool = False
    driver_count: int = 0
    members: list[BusMember] = field(default_factory=list)  # own driver's bus members
    merged_into: _Subgraph | None = None
    hier_children: list[_Subgraph] = field(default_factory=list)
    hier_parents: list[_Subgraph] = field(default_factory=list)
    chain: list[_Subgraph] = field(default_factory=list)
    head: _Subgraph | None = None  # the chain member whose driver names the chain
    aliases: Mapping[str, Sequence[str]] = field(default_factory=dict)
    index: int = -1

    def root(self) -> _Subgraph:
        s = self
        while s.merged_into is not None:
            s = s.merged_into
        return s

    @property
    def via_bus_entry(self) -> bool:
        return self.touches_bus or bool(self.bus_neighbors)


def default_net_name(pin: PinRef, force_no_connect: bool = False) -> str:
    """`SCH_PIN::GetDefaultNetName` (sch_pin.cpp 1484-1592 at tag 10.0)."""
    sym = pin.symbol
    if pin.is_global_power or pin.is_local_power:
        if sym.power_kind is not None and (sym.lib_symbol is None or sym.lib_symbol.power is not None):
            return escape_net_name(sym.value)
        return escape_net_name(pin.pin.name or "??")
    prefix = "unconnected-(" if (force_no_connect or pin.pin.type == "no_connect") else "Net-("
    unconnected = prefix.startswith("unconnected")
    ref = pin.reference
    shown_name, shown_number = pin.pin.shown_name, pin.pin.shown_number
    if ref.endswith("?"):
        # KiCad appends a hash of the symbol UUID here; reproduce the shape, not the hash
        return f"{prefix}{ref}-{sym.uuid[:8]}-Pad{shown_number})"
    has_multiple = any(
        p.shown_name == shown_name
        and p.shown_number != shown_number
        and unconnected == (p.type == "no_connect")
        for p in sym.pins_for(pin.path)
    )
    if shown_name and shown_name != shown_number:
        ref_unit = ref + (unit_letter(pin.unit) if sym.unit_count > 1 else "")
        name = f"{prefix}{ref_unit}-{escape_net_name(shown_name)}"
        if unconnected or has_multiple:
            name += f"-Pad{escape_net_name(shown_number)}"
        return name + ")"
    return f"{prefix}{ref}-Pad{escape_net_name(shown_number)})"


def _names_prefix(inst: SheetInstance) -> str:
    """`SCH_SHEET_PATH::PathHumanReadable(true, false, true)`: `/` for the root, `/name/…/` below,
    with `/` inside sheet names escaped."""
    if inst.parent is None:
        return "/"
    parts: list[str] = []
    p: SheetInstance | None = inst
    while p is not None and p.parent is not None:
        parts.append(escape_net_name(p.name))
        p = p.parent
    return "/" + "/".join(reversed(parts)) + "/"


def _line_through(p: Vec2, line: Any) -> bool:
    return _on_interior(p, line)


def _build_sheet(
    inst: SheetInstance,
    child_paths: dict[str, str],
    warnings: list[str],
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[_Subgraph], list[WireEnd], int]:
    """Union-find over one sheet instance's items; returns its subgraphs and dangling wire ends."""
    sch = inst.file
    items: list[_Item] = []
    uf = _UnionFind()

    def add(kind: str, points: Sequence[Vec2], payload: Any) -> _Item:
        it = _Item(len(items), kind, tuple(points), payload)
        items.append(it)
        uf.add(it.id)
        return it

    for wire in sch.wires:
        add("wire", (wire.start, wire.end), wire)
    for b in sch.buses:
        add("bus", (b.start, b.end), b)
    for j in sch.junctions:
        add("junction", (j.position,), j)
    for label in sch.labels:
        if label.is_net_label:
            add("label", (label.position,), label)
    for nc in sch.no_connects:
        add("nc", (nc.position,), nc)
    for sym in sch.symbols:
        ref = sym.reference_for(inst.path)
        unit = sym.unit_for(inst.path)
        for pin in sym.pins_for(inst.path):
            add("pin", (pin.position,), PinRef(inst.path, inst.names_path, ref, unit, sym, pin))
    for sheet in sch.sheets:
        child_path = child_paths.get(sheet.uuid, f"{inst.path.rstrip('/')}/{sheet.uuid}")
        for sheet_pin in sheet.pins:
            add(
                "sheet_pin",
                (sheet_pin.position,),
                SheetPinRef(inst.path, inst.names_path, sheet, sheet_pin, child_path),
            )
    for be in sch.bus_entries:
        add("bus_entry", (be.position, be.end), be)

    point_map: dict[tuple[int, int], list[_Item]] = {}
    for it in items:
        for p in it.points:
            point_map.setdefault(_key(p), []).append(it)
    wires = [it for it in items if it.kind == "wire"]
    buses = [it for it in items if it.kind == "bus"]

    def bus_at(p: Vec2) -> list[_Item]:
        """`SCH_SCREEN::GetBus(point)`: bus lines that end at or pass through `p`."""
        here = [it for it in point_map.get(_key(p), []) if it.kind == "bus"]
        here += [b for b in buses if b not in here and _line_through(p, b.payload)]
        return here

    # (bus_entry item, bus item) links recorded while joining points
    entry_bus_links: list[tuple[_Item, _Item]] = []
    # net-typed sheet pins sitting on a bus become bus neighbours instead of bus members
    pin_bus_links: list[tuple[_Item, _Item]] = []

    for key, group in point_map.items():
        p = Vec2(key[0] / _KEY_SCALE, key[1] / _KEY_SCALE)
        bus_here = bus_at(p)
        net_side: list[_Item] = []
        bus_side: list[_Item] = []
        for it in group:
            if it.kind == "bus_entry":
                if bus_here:
                    entry_bus_links.extend((it, b) for b in bus_here)
                else:
                    net_side.append(it)
            elif it.kind == "sheet_pin" and bus_here and bus_members(it.payload.pin.name, aliases) is None:
                pin_bus_links.extend((it, b) for b in bus_here)
            elif it.kind in _NET_CLASS:
                net_side.append(it)
            elif it.kind in _BUS_CLASS:
                bus_side.append(it)
            else:  # junction, label, sheet_pin join whichever class is present
                net_side.append(it)
                bus_side.append(it)
        for side in (net_side, bus_side):
            for other in side[1:]:
                uf.union(side[0].id, other.id)

    for it in items:
        if it.kind == "junction":
            p = it.points[0]
            for w in (*wires, *buses):
                if _line_through(p, w.payload):
                    uf.union(it.id, w.id)
        elif it.kind == "label":
            p = it.points[0]
            for w in (*wires, *buses):  # a label anywhere along a wire/bus connects to it (module doc)
                if _line_through(p, w.payload):
                    uf.union(it.id, w.id)
        elif it.kind == "pin":
            p = it.points[0]
            if len(point_map.get(_key(p), [])) == 1 and any(_line_through(p, w.payload) for w in wires):
                pr: PinRef = it.payload
                warnings.append(
                    f"{inst.names_path}: pin {pr.reference}.{pr.number} at ({p.x}, {p.y}) rests on the interior "
                    "of a wire — KiCad does not connect it (wires connect at their end points only)"
                )

    dangling: list[WireEnd] = []
    for w in wires:
        for p in w.points:
            if len(point_map.get(_key(p), [])) == 1 and not any(
                _line_through(p, o.payload) for o in wires if o is not w
            ):
                dangling.append(WireEnd(inst.path, p))

    groups: dict[int, list[_Item]] = {}
    for it in items:
        groups.setdefault(uf.find(it.id), []).append(it)
    by_root: dict[int, _Subgraph] = {}
    subgraphs: list[_Subgraph] = []
    for root, members in groups.items():
        sg = _Subgraph(inst, members, aliases=aliases or {})
        for it in members:
            if it.kind == "pin":
                sg.pins.append(it.payload)
            elif it.kind == "label":
                sg.labels.append(it.payload)
            elif it.kind == "sheet_pin":
                sg.sheet_pins.append(it.payload)
            elif it.kind == "nc":
                sg.no_connect = True  # the MARKER only (CONNECTION_SUBGRAPH::m_no_connect); a
                # no_connect-TYPE pin names its own net `unconnected-(...)` inside
                # default_net_name and is NOT a marker (netlist `+no_connect`, finding 15)
            elif it.kind == "bus":
                sg.is_bus = True
        by_root[root] = sg
        subgraphs.append(sg)
    for entry, _bus in entry_bus_links:
        by_root[uf.find(entry.id)].touches_bus = True
    for spin, _bus in pin_bus_links:
        by_root[uf.find(spin.id)].touches_bus = True
    return subgraphs, dangling, len(sch.bus_entries)


# ----------------------------------------------------------------------------------------------
# driver resolution
# ----------------------------------------------------------------------------------------------


def _driver_candidates(sg: _Subgraph) -> list[tuple[int, str, Any, str, bool]]:
    """`(priority, kind, item, local_name, prefixed)` for every driver-capable item. On a bus
    subgraph only bus-typed labels / sheet pins drive (a net label on a bus is an ERC error)."""
    out: list[tuple[int, str, Any, str, bool]] = []
    for label in sg.labels:
        if sg.is_bus and bus_members(label.text, sg.aliases) is None:
            continue
        name = escape_net_name(label.text)
        if label.kind == "global_label":
            out.append((_P_GLOBAL, "global_label", label, name, False))
        elif label.kind == "hierarchical_label":
            out.append((_P_HIER, "hier_label", label, name, True))
        else:
            out.append((_P_LOCAL, "local_label", label, name, True))
    for sp in sg.sheet_pins:
        if sg.is_bus and bus_members(sp.pin.name, sg.aliases) is None:
            continue
        out.append((_P_SHEET_PIN, "sheet_pin", sp, escape_net_name(sp.pin.name), True))
    if sg.is_bus:
        return out
    for pin in sg.pins:
        if pin.is_global_power:
            out.append((_P_GLOBAL_POWER, "global_power", pin, default_net_name(pin), False))
        elif pin.is_local_power:
            out.append((_P_LOCAL_POWER, "local_power", pin, default_net_name(pin), True))
        elif pin.reference.endswith("?"):
            continue  # unannotated symbols do not drive (SCH_CONNECTION::IsDriver)
        elif pin.reference.startswith("#") or not pin.symbol.on_board:
            out.append((_P_NONE, "pin", pin, default_net_name(pin, sg.no_connect), False))
        else:
            out.append((_P_PIN, "pin", pin, default_net_name(pin, sg.no_connect), False))
    return out


def _candidate_sort_key(c: tuple[int, str, Any, str, bool]) -> tuple[int, int, int, str]:
    _, kind, item, name, _ = c
    power_rank = {"global_power": 0, "local_power": 1}.get(kind, 2)
    sheet_rank = 0 if kind == "sheet_pin" and item.pin.shape == "output" else 1
    low_quality = 1 if "-Pad" in name else 0
    return (power_rank, sheet_rank, low_quality, name)


def _resolve(sg: _Subgraph) -> None:
    cands = _driver_candidates(sg)
    sg.driver_count = len(cands)
    sg.priority, sg.driver, sg.driver_kind, sg.strong = _P_NONE, None, "none", False
    sg.local_name = sg.full_name = sg.own_name = ""
    sg.members = []
    if not cands:
        return
    highest = max(c[0] for c in cands)
    best = sorted((c for c in cands if c[0] == highest), key=_candidate_sort_key)[0]
    priority, kind, item, name, prefixed = best
    sg.priority = priority
    sg.driver = item
    sg.driver_kind = kind
    sg.strong = priority >= _STRONG
    if sg.strong:
        sg.driver_count = sum(1 for c in cands if c[0] >= _STRONG)
    if kind in ("global_label", "local_label", "hier_label", "sheet_pin"):
        label_text = item.text if kind != "sheet_pin" else item.pin.name
        parsed = split_bus_label(label_text, sg.aliases)
        if parsed is not None:
            sg.is_bus = True
            sg.members = [BusMember(escape_net_name(m.name), m.key) for m in parsed[1]]
    # weakly driven single-pin nets get the unconnected- prefix
    if not sg.strong and sg.driver_count == 1 and kind == "pin":
        name = default_net_name(item, True)
    sg.local_name = name
    sg.prefixed = prefixed
    sg.full_name = (_names_prefix(sg.inst) + name) if prefixed else name
    sg.own_name = sg.full_name


def _strong_names(sg: _Subgraph) -> set[str]:
    return {c[3] for c in _driver_candidates(sg) if c[0] >= _STRONG}


# ----------------------------------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------------------------------


def pin_connectivity(source: SchematicFile | Hierarchy) -> Connectivity:
    """Derive nets from a schematic file (treated as a single root sheet) or a whole hierarchy."""
    hierarchy = source if isinstance(source, Hierarchy) else Hierarchy.single(source)
    warnings: list[str] = []
    all_subgraphs: list[_Subgraph] = []
    dangling: list[WireEnd] = []
    bus_entries = 0
    by_inst: dict[str, list[_Subgraph]] = {}
    aliases = hierarchy.bus_aliases()
    for inst in hierarchy.instances:
        child_paths = {c.sheet.uuid: c.path for c in hierarchy.children(inst) if c.sheet is not None}
        sgs, dang, be = _build_sheet(inst, child_paths, warnings, aliases)
        all_subgraphs.extend(sgs)
        dangling.extend(dang)
        bus_entries += be
        by_inst[inst.path] = sgs
    for sg in all_subgraphs:
        _resolve(sg)

    # same-sheet merge of strongly driven subgraphs sharing a strong driver name (same type)
    for sgs in by_inst.values():
        names: dict[tuple[str, bool], _Subgraph] = {}
        for s in sgs:
            if not s.strong:
                continue
            for n in _strong_names(s):
                k = (n, s.is_bus)
                other = names.get(k)
                if other is None:
                    names[k] = s
                elif other.root() is not s.root():
                    _merge(other.root(), s.root())
    roots = [s for s in all_subgraphs if s.merged_into is None]
    for r in roots:
        _resolve(r)

    # hierarchy links: parent sheet pin <-> child hierarchical labels of the same name
    for sg in roots:
        for sp in sg.sheet_pins:
            wanted = escape_net_name(sp.pin.name)
            for child in by_inst.get(sp.child_path, []):
                child = child.root()
                linked = child.strong and any(
                    lbl.kind == "hierarchical_label" and escape_net_name(lbl.text) == wanted
                    for lbl in child.labels
                )
                if linked and child is not sg and child not in sg.hier_children:
                    sg.hier_children.append(child)
                    child.hier_parents.append(sg)

    # global drivers sharing a name (transitively) are one net; the representative is the
    # highest-priority driver, ties broken by the lower name (buildConnectionGraph, issue 23719)
    guf = _UnionFind()
    by_global_name: dict[str, list[_Subgraph]] = {}
    for idx, r in enumerate(roots):
        r.index = idx
        if r.priority < _P_GLOBAL_POWER:
            continue
        guf.add(idx)
        for c in _driver_candidates(r):
            if c[0] >= _P_GLOBAL_POWER:
                by_global_name.setdefault(c[3], []).append(r)
    for sgs in by_global_name.values():
        for other in sgs[1:]:
            guf.union(sgs[0].index, other.index)
    classes: dict[int, list[_Subgraph]] = {}
    for r in roots:
        if r.priority >= _P_GLOBAL_POWER:
            classes.setdefault(guf.find(r.index), []).append(r)
    for members in classes.values():
        rep = min(members, key=lambda x: (-x.priority, x.full_name))
        for r in members:
            if r is not rep:
                _adopt_name(r, rep)

    # weak-name collisions get suffixes (processSubGraphs)
    counts: dict[str, int] = {}
    for r in roots:
        counts[r.full_name] = counts.get(r.full_name, 0) + 1
    taken = set(counts)
    for r in roots:
        if not r.strong and counts.get(r.full_name, 0) > 1:
            base = r.full_name
            suffix = 1
            new = f"{base}_{suffix}"
            while new in taken:
                suffix += 1
                new = f"{base}_{suffix}"
            counts[base] -= 1
            taken.add(new)
            r.full_name = r.own_name = new
            r.local_name = f"{r.local_name}_{suffix}"

    # hierarchical chains take the best driver
    for chain in _chains(roots):
        best = _best_in_chain(chain)
        for s in chain:
            s.chain = chain
            s.head = best
            _adopt_name(s, best)

    # Bus members. A bus member is a "slot" (bus subgraph, member key). Slots are the same net when
    # (a) their buses are linked through the hierarchy (matched by vector index or group member
    # name, `matchBusMember`), or (b) two buses on one sheet name the same member (`IO[8..15]`
    # inside `IO[0..31]`: KiCad's per-label member subgraphs share the name, so renaming one renames
    # the other). A slot class is named by the best-driving bus among its buses (same rule as
    # hierarchical chains), prefix + member name. On each sheet, net subgraphs whose driver name is
    # a member of a bus on that sheet are that bus's neighbours (membership is by NAME — bus
    # entries are graphics; a net-named sheet pin sitting on a bus counts too) and take the slot
    # name, unless they carry a global driver, then the slot takes theirs.
    bus_roots = [r for r in roots if r.is_bus and r.members]
    slots = _UnionFind()
    slot_id: dict[tuple[int, int | str], int] = {}
    slot_bus: list[tuple[_Subgraph, BusMember]] = []

    def slot(b: _Subgraph, m: BusMember) -> int:
        key = (id(b), m.key)
        if key not in slot_id:
            slot_id[key] = len(slot_bus)
            slot_bus.append((b, m))
            slots.add(slot_id[key])
        return slot_id[key]

    for b in bus_roots:
        for m in b.members:
            slot(b, m)
        for other in (*b.hier_children, *b.hier_parents):
            other = other.root()
            if not (other.is_bus and other.members):
                continue
            for m in b.members:
                om = next((x for x in other.members if x.key == m.key), None)
                if om is not None:
                    slots.union(slot(b, m), slot(other, om))
    for sgs in by_inst.values():
        by_name: dict[str, int] = {}
        for b in {s.root() for s in sgs}:
            if not (b.is_bus and b.members):
                continue
            for m in b.members:
                first = by_name.setdefault(m.name, slot(b, m))
                slots.union(first, slot(b, m))
    class_members: dict[int, list[int]] = {}
    for sid in range(len(slot_bus)):
        class_members.setdefault(slots.find(sid), []).append(sid)
    class_name: dict[int, str] = {}
    for cid, sids in class_members.items():
        best = _best_in_chain(slot_bus[sid][0] for sid in sids)
        member = next(slot_bus[sid][1] for sid in sids if slot_bus[sid][0] is best)
        class_name[cid] = (_names_prefix(best.inst) if best.prefixed else "") + member.name

    for sgs in by_inst.values():
        local_buses = list({s.root() for s in sgs if s.root().is_bus and s.root().members})
        if not local_buses:
            continue
        member_index: dict[str, list[tuple[_Subgraph, BusMember]]] = {}
        for b in local_buses:
            for m in b.members:
                member_index.setdefault(m.name, []).append((b, m))
        for s in sgs:
            s = s.root()
            if s.is_bus or s.merged_into is not None:
                continue
            candidate_names = _strong_names(s) if s.strong else {s.local_name}
            for candidate in candidate_names:
                for b, m in member_index.get(candidate, []):
                    if (b, m) not in s.bus_neighbors:
                        s.bus_neighbors.append((b, m))
    for net_sg in roots:  # global drivers rename the slot
        if net_sg.priority >= _P_GLOBAL_POWER:
            for b, m in net_sg.bus_neighbors:
                class_name[slots.find(slot(b, m))] = net_sg.full_name
    for net_sg in roots:
        if net_sg.priority >= _P_GLOBAL_POWER or not net_sg.bus_neighbors:
            continue
        b, m = sorted(net_sg.bus_neighbors, key=lambda bm: bm[1].name)[0]
        new_name = class_name[slots.find(slot(b, m))]
        for s in net_sg.chain or [net_sg]:
            s.full_name = new_name
            s.driver_kind = "bus_member"

    # final nets keyed by full name (net codes are shared by name in KiCad)
    nets: dict[str, DerivedNet] = {}
    item_nets: dict[tuple[str, str], str] = {}
    for r in roots:
        for it in r.items:
            if it.kind in ("wire", "bus", "junction", "label"):
                uuid = getattr(it.payload, "uuid", "")
                if uuid:
                    item_nets[(r.inst.names_path, uuid)] = r.full_name
        net = nets.get(r.full_name)
        if net is None:
            nets[r.full_name] = DerivedNet(
                name=r.full_name,
                pins=list(r.pins),
                labels=[(r.inst.path, lbl) for lbl in r.labels],
                sheet_pins=list(r.sheet_pins),
                no_connect=r.no_connect,
                is_bus=r.is_bus,
                via_bus_entry=r.via_bus_entry,
                driver_kind=r.driver_kind,
                strong=r.strong,
            )
        else:
            net.pins.extend(r.pins)
            net.labels.extend((r.inst.path, lbl) for lbl in r.labels)
            net.sheet_pins.extend(r.sheet_pins)
            net.no_connect = net.no_connect or r.no_connect
            net.via_bus_entry = net.via_bus_entry or r.via_bus_entry
            net.is_bus = net.is_bus or r.is_bus
            net.subgraphs += 1
    ordered = sorted(nets.values(), key=lambda n: n.name)
    unconnected = [p for n in ordered for p in n.pins if len(n.netlist_pins) <= 1 and p.in_netlist]
    nc_pins = [p for n in ordered if n.no_connect for p in n.pins]
    return Connectivity(
        nets=ordered,
        dangling_wire_ends=dangling,
        unconnected_pins=unconnected,
        no_connect_pins=nc_pins,
        label_only_nets=[n for n in ordered if n.label_only],
        warnings=warnings,
        bus_count=sum(len(i.file.buses) for i in hierarchy.instances),
        bus_entry_count=bus_entries,
        item_nets=item_nets,
    )


def _merge(keep: _Subgraph, other: _Subgraph) -> None:
    if keep is other:
        return
    other.merged_into = keep
    keep.items.extend(other.items)
    keep.pins.extend(other.pins)
    keep.labels.extend(other.labels)
    keep.sheet_pins.extend(other.sheet_pins)
    keep.no_connect = keep.no_connect or other.no_connect
    keep.is_bus = keep.is_bus or other.is_bus
    keep.bus_neighbors.extend(b for b in other.bus_neighbors if b not in keep.bus_neighbors)
    keep.touches_bus = keep.touches_bus or other.touches_bus


def _chains(roots: Sequence[_Subgraph]) -> list[list[_Subgraph]]:
    seen: set[int] = set()
    chains: list[list[_Subgraph]] = []
    for r in roots:
        if id(r) in seen or (not r.hier_children and not r.hier_parents):
            continue
        chain: list[_Subgraph] = []
        stack = [r]
        while stack:
            s = stack.pop()
            if id(s) in seen:
                continue
            seen.add(id(s))
            chain.append(s)
            stack.extend(x.root() for x in s.hier_children)
            stack.extend(x.root() for x in s.hier_parents)
        chains.append(chain)
    return chains


def _adopt_name(s: _Subgraph, best: _Subgraph) -> None:
    """Take `best`'s net name; the subgraph keeps its own driver priority/strength (KiCad's later
    decisions look at each subgraph's own driver)."""
    s.full_name = best.full_name
    s.driver_kind = best.driver_kind


def _best_in_chain(chain: Iterable[_Subgraph]) -> _Subgraph:
    """`propagateToNeighbors`: global wins; else strong over weak, higher priority, shorter path,
    lower own name. The chosen subgraph's sheet gives the prefix of bus member names."""
    best: _Subgraph | None = None
    for s in chain:
        if best is None:
            best = s
            continue
        if s.priority >= _P_GLOBAL_POWER and best.priority < _P_GLOBAL_POWER:
            best = s
            continue
        if best.priority >= _P_GLOBAL_POWER:
            if s.priority >= _P_GLOBAL_POWER and s.own_name < best.own_name:
                best = s
            continue
        if (not best.strong and s.strong) or (s.priority > best.priority and s.strong):
            best = s
            continue
        if s.priority == best.priority and s.strong and s.inst.depth < best.inst.depth:
            best = s
            continue
        if (
            s.strong == best.strong
            and s.inst.depth <= best.inst.depth
            and s.priority == best.priority
            and s.own_name < best.own_name
        ):
            best = s
    assert best is not None
    return best
