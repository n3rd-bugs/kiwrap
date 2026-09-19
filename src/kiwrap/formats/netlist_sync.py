"""Netlist -> board synchronisation semantics (KiCad's "Update PCB from Schematic", headless).

No KiCad surface performs a netlist import headlessly on 7-10 (docs/VERIFICATION.md claim 5:
kicad-cli has no import command, SWIG exposes no `BOARD_NETLIST_UPDATER`, the released IPC API
cannot resolve a LIB_ID; KiCad 11 adds `ImportNetlist` over IPC). This module re-implements the
updater's decisions (`pcbnew/netlist_reader/board_netlist_updater.cpp`) as pure data: it reads
the kicadsexpr netlist `kicad-cli sch export netlist` writes, compares it with the board's
footprints (`FootprintState`, extracted by `kiwrap.formats.board_writer`) and produces a
`SyncPlan` the board writer executes on the CST. The oracle is KiCad itself: after the plan is
applied, `kicad-cli pcb drc --schematic-parity` must report zero parity violations
(`tests/test_netlist_sync.py`, `conformance/tests/t8_board_writer.py`).

Semantics encoded (all mirrored from the updater; the footprint `(path …)` = sheet path +
symbol uuid is what the updater pairs footprints and symbols by, `pcb drc --schematic-parity`
pairs them **by reference** (`BOARD::FindFootprintByReference`,
drc_test_provider_schematic_parity.cpp), so both are always written):

* A component is matched to a footprint **by path** first (`match="path"`, the updater's
  default `NMM_UUID` mode — the GUI's "Re-link footprints to schematic symbols based on their
  reference designators" box is off by default, and KiCad 11's `ImportNetlist` twin runs the same
  mode): a path match whose reference differs is a *renamed* symbol (re-annotation, a swap of two
  references) and is re-referenced with its placement and pad nets kept — matching by reference
  there would rewrite value / pad nets onto whichever footprint happens to carry the old
  reference and put routed copper on the wrong nets. A reference is used only when one side
  carries no path (a board not made by kiwrap, a netlist without tstamps); a component whose path
  matches nothing while a footprint with its reference carries another path is a re-created
  symbol: KiCad removes the old footprint and adds a new one, and so does this planner (with a
  warning, since the placement is lost). `match="reference"` is the GUI checkbox: reference
  first, path as the fallback for footprints whose reference left the netlist.
* A different footprint id in the netlist is a **footprint swap**: the writer re-instantiates
  the library footprint and keeps position, side, rotation, lock state, uuid and path.
* Pad nets are assigned **by pad number string**: every pad whose number equals a pin number
  gets that pin's net (thermal pads repeat a number and all of them get it); pads whose number
  is not a pin of the component lose their net (KiCad "disconnects" them); a pin with no pad is
  reported (`SyncReport.warnings`, KiCad's "No pad found for pin").
* Value, `dnp`, `exclude_from_bom`, `exclude_from_sim` (a footprint attribute from board stamp
  20260828 on; the parity check of the nightly compares it), the Sheetname/Sheetfile properties
  and the netlist fields (Datasheet, Description, user fields — the nightly's parity check
  compares every one of them) are propagated; `exclude_from_pos_files` is set when the netlist
  marks it and otherwise left to the library footprint (older netlists do not carry it);
  Reference/Value/Footprint fields are never written as extra properties.
* A footprint not in the netlist is an **orphan**: removed when `remove_orphans` is set, unless
  it is locked (the updater skips locked footprints with a warning) — `SyncPlan.kept_locked`.
* Components flagged `exclude_from_board` are absent from KiCad's netlist altogether (the
  parser docstring in `kiwrap.formats.reports.netlist`); a netlist written by another tool that
  marks them explicitly is honoured the same way: such components are never added and an
  existing footprint for one counts as an orphan.
* **Net renames**: when every pad that carried an old net name now carries one and the same new
  name, and the old name no longer exists in the netlist, tracks/vias/zones on the old net are
  moved to the new name (KiCad itself only re-nets zones this way and leaves tracks for DRC to
  flag; moving tracks is the agent-friendly superset and never hides a real change because the
  pad set is identical). Merges and splits are reported, not guessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from kiwrap.formats.reports import Netlist
from kiwrap.formats.sexpr import Atom, Node, loads

__all__ = [
    "CONFLICTING_NET",
    "MATCH_MODES",
    "FootprintState",
    "FootprintUpdate",
    "SyncComponent",
    "SyncNetlist",
    "SyncPlan",
    "SyncReport",
    "netlist_from_any",
    "plan_sync",
    "read_sync_netlist",
]

# Netlist fields that are footprint attributes, not extra properties (BOARD_NETLIST_UPDATER
# skips them when it copies fields).
RESERVED_FIELDS = frozenset({"Reference", "Value", "Footprint"})
# Properties KiCad emits per component that are library metadata, not footprint fields.
_META_PROPERTIES = frozenset({"Sheetname", "Sheetfile", "ki_keywords", "ki_fp_filters", "ki_description"})
#: `FootprintState.pad_nets` value for a pad number whose pads carry different nets (thermal
#: pads out of step); the planner then re-assigns every pad of that number.
CONFLICTING_NET = "<conflicting pad nets>"
_FLAG_PROPERTIES = frozenset(
    {"dnp", "exclude_from_board", "exclude_from_bom", "exclude_from_sim", "exclude_from_pos_files"}
)


# ----------------------------------------------------------------------------------------------
# the netlist side
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncComponent:
    """One `(comp …)` of the netlist with everything the updater consumes."""

    ref: str
    value: str
    fpid: str | None
    path: str  # KIID path `/<sheet uuids…>/<symbol uuid>` ("" when the netlist carries no tstamps)
    uuids: tuple[str, ...] = ()  # one per unit; `path` uses the first (as KiCad does)
    sheet_path: str = "/"  # `(sheetpath (names …))`
    sheet_name: str = ""
    sheet_file: str = ""
    lib_id: str | None = None
    fields: dict[str, str] = field(default_factory=dict)  # Datasheet, Description, user fields
    properties: dict[str, str] = field(default_factory=dict)  # ki_keywords, ki_fp_filters, …
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_board: bool = False
    exclude_from_sim: bool = False
    exclude_from_pos_files: bool = False
    pins: dict[str, str] = field(default_factory=dict)  # pin number -> net name
    pin_functions: dict[str, str] = field(default_factory=dict)
    pin_types: dict[str, str] = field(default_factory=dict)

    @property
    def uuid(self) -> str:
        return self.uuids[0] if self.uuids else ""


@dataclass(frozen=True)
class SyncNetlist:
    components: tuple[SyncComponent, ...]
    nets: dict[str, tuple[tuple[str, str], ...]]  # net name -> ((ref, pin), …)
    source: str = ""

    def component(self, ref: str) -> SyncComponent | None:
        return next((c for c in self.components if c.ref == ref), None)

    def by_ref(self) -> dict[str, SyncComponent]:
        return {c.ref: c for c in self.components}

    def by_path(self) -> dict[str, SyncComponent]:
        return {c.path: c for c in self.components if c.path}


def _scalar(node: Node | None) -> str | None:
    if node is None:
        return None
    atoms = node.atoms
    return atoms[0].value if atoms else None


def _named_value(node: Node) -> tuple[str, str | None]:
    name = _scalar(node.find("name")) or ""
    value_node = node.find("value")
    if value_node is not None:
        return name, _scalar(value_node) or ""
    trailing = [c for c in node.args if isinstance(c, Atom)]
    return name, trailing[0].value if trailing else None


def _kiid_path(sheet_tstamps: str, uuid: str) -> str:
    """`/` + "uuid" -> `/uuid`; `/abc/` + "uuid" -> `/abc/uuid` (KIID_PATH string form)."""
    if not uuid:
        return ""
    base = sheet_tstamps.strip()
    if not base.startswith("/"):
        base = "/" + base
    if not base.endswith("/"):
        base += "/"
    return base + uuid


def _component(comp: Node) -> SyncComponent:
    fields: dict[str, str] = {}
    properties: dict[str, str] = {}
    dnp = exclude_board = exclude_bom = exclude_sim = exclude_pos = False
    fields_node = comp.find("fields")
    if fields_node is not None:
        for f in fields_node.find_all("field"):
            name, value = _named_value(f)
            if name and name not in RESERVED_FIELDS:
                fields[name] = value or ""
    for prop in comp.find_all("property"):
        name, value = _named_value(prop)
        if name in _FLAG_PROPERTIES and _flag_value(value):
            dnp = dnp or name == "dnp"
            exclude_board = exclude_board or name == "exclude_from_board"
            exclude_bom = exclude_bom or name == "exclude_from_bom"
            exclude_sim = exclude_sim or name == "exclude_from_sim"
            exclude_pos = exclude_pos or name == "exclude_from_pos_files"
        elif name in _FLAG_PROPERTIES:
            continue
        elif name:
            properties[name] = value or ""
    lib_id: str | None = None
    libsource = comp.find("libsource")
    if libsource is not None:
        lib = _scalar(libsource.find("lib")) or ""
        part = _scalar(libsource.find("part")) or ""
        lib_id = f"{lib}:{part}" if lib else part or None
    sheetpath = comp.find("sheetpath")
    names = _scalar(sheetpath.find("names")) if sheetpath is not None else None
    tstamps_path = _scalar(sheetpath.find("tstamps")) if sheetpath is not None else None
    tstamps = comp.find("tstamps")
    uuids = tuple(a.value for a in tstamps.atoms) if tstamps is not None else ()
    footprint = _scalar(comp.find("footprint")) or None
    return SyncComponent(
        ref=_scalar(comp.find("ref")) or "",
        value=_scalar(comp.find("value")) or "",
        fpid=footprint,
        path=_kiid_path(tstamps_path or "/", uuids[0] if uuids else ""),
        uuids=uuids,
        sheet_path=names or "/",
        sheet_name=properties.get("Sheetname", ""),
        sheet_file=properties.get("Sheetfile", ""),
        lib_id=lib_id,
        fields=fields,
        properties={k: v for k, v in properties.items() if k not in ("Sheetname", "Sheetfile")},
        dnp=dnp,
        exclude_from_bom=exclude_bom,
        exclude_from_board=exclude_board,
        exclude_from_sim=exclude_sim,
        exclude_from_pos_files=exclude_pos,
    )


def _flag_value(value: str | None) -> bool:
    """A value-less `(property (name "dnp"))` is true; KiCad's reader also accepts a value
    (`0`/`false` -> false, `1`/`true` -> true, anything else -> non-empty)."""
    if value is None:
        return True
    v = value.strip().lower()
    if v in ("0", "false"):
        return False
    if v in ("1", "true"):
        return True
    return bool(v)


def read_sync_netlist(text: str, *, source: str = "") -> SyncNetlist:
    """Parse a kicadsexpr netlist (the `(export …)` document) into a `SyncNetlist`."""
    root = loads(text)
    if root.head != "export":
        raise ValueError(f"not a KiCad netlist: top-level list is ({root.head or '?'} …)")
    design = root.find("design")
    if not source and design is not None:
        source = _scalar(design.find("source")) or ""
    comps_node = root.find("components")
    comps = [_component(c) for c in comps_node.find_all("comp")] if comps_node is not None else []
    nets: dict[str, tuple[tuple[str, str], ...]] = {}
    pins: dict[str, dict[str, str]] = {c.ref: {} for c in comps}
    functions: dict[str, dict[str, str]] = {c.ref: {} for c in comps}
    types: dict[str, dict[str, str]] = {c.ref: {} for c in comps}
    nets_node = root.find("nets")
    for net in nets_node.find_all("net") if nets_node is not None else []:
        name = _scalar(net.find("name")) or ""
        nodes: list[tuple[str, str]] = []
        for n in net.find_all("node"):
            ref = _scalar(n.find("ref")) or ""
            pin = _scalar(n.find("pin")) or ""
            nodes.append((ref, pin))
            if ref in pins:
                pins[ref][pin] = name
                fn = _scalar(n.find("pinfunction"))
                pt = _scalar(n.find("pintype"))
                if fn:
                    functions[ref][pin] = fn
                if pt:
                    types[ref][pin] = pt
        nets[name] = tuple(nodes)
    components = tuple(
        replace(c, pins=pins[c.ref], pin_functions=functions[c.ref], pin_types=types[c.ref]) for c in comps
    )
    return SyncNetlist(components=components, nets=nets, source=source)


def netlist_from_any(netlist: Netlist | SyncNetlist | str | Path, *, text: str | None = None) -> SyncNetlist:
    """Accept what callers have: a `SyncNetlist`, netlist text, a `.net` path, or the
    `kiwrap.formats.reports.Netlist` model (which carries no symbol uuids, so `text` — the raw
    export — should be passed alongside it; without it paths are empty and footprints are
    matched by reference only, see `plan_sync`)."""
    if isinstance(netlist, SyncNetlist):
        return netlist
    if text is not None:
        return read_sync_netlist(text)
    if isinstance(netlist, Path):
        return read_sync_netlist(netlist.read_text(encoding="utf-8", errors="replace"), source=str(netlist))
    if isinstance(netlist, str):
        stripped = netlist.lstrip()
        if stripped.startswith("("):
            return read_sync_netlist(netlist)
        return read_sync_netlist(Path(netlist).read_text(encoding="utf-8", errors="replace"), source=netlist)
    return _from_report_netlist(netlist)


def _from_report_netlist(netlist: Netlist) -> SyncNetlist:
    pins: dict[str, dict[str, str]] = {c.ref: {} for c in netlist.components}
    nets: dict[str, tuple[tuple[str, str], ...]] = {}
    for net in netlist.nets:
        nets[net.name] = tuple((n.ref, n.pin) for n in net.nodes)
        for n in net.nodes:
            if n.ref in pins:
                pins[n.ref][n.pin] = net.name
    comps = tuple(
        SyncComponent(
            ref=c.ref,
            value=c.value,
            fpid=c.footprint,
            path="",
            sheet_path=c.sheet_path or "/",
            sheet_name=c.properties.get("Sheetname", ""),
            sheet_file=c.properties.get("Sheetfile", ""),
            lib_id=c.lib_id,
            fields={k: v for k, v in c.properties.items() if k not in RESERVED_FIELDS | _META_PROPERTIES},
            properties={
                k: v for k, v in c.properties.items() if k in _META_PROPERTIES - {"Sheetname", "Sheetfile"}
            },
            dnp=c.dnp,
            exclude_from_board=c.exclude_from_board,
            pins=pins[c.ref],
        )
        for c in netlist.components
    )
    return SyncNetlist(components=comps, nets=nets, source=netlist.source)


# ----------------------------------------------------------------------------------------------
# the board side and the plan
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FootprintState:
    """What the planner needs to know about a footprint already on the board."""

    uuid: str
    ref: str
    value: str
    fpid: str
    path: str
    locked: bool = False
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_sim: bool = False
    exclude_from_pos_files: bool = False
    pad_numbers: tuple[str, ...] = ()  # every pad's number, repeats kept
    pad_nets: dict[str, str] = field(default_factory=dict)  # pad number -> net name ("" = none)
    properties: dict[str, str] = field(default_factory=dict)
    sheet_name: str = ""
    sheet_file: str = ""


@dataclass
class FootprintUpdate:
    """Changes to apply to one existing footprint (`None` = leave as is)."""

    uuid: str
    ref: str  # the reference after the update
    component: SyncComponent
    matched_by: str  # "reference" | "path"
    old_ref: str | None = None
    value: str | None = None
    fpid_change: tuple[str, str] | None = None
    path: str | None = None
    dnp: bool | None = None
    exclude_from_bom: bool | None = None
    exclude_from_sim: bool | None = None
    exclude_from_pos_files: bool | None = None  # only ever True (never cleared from a netlist)
    pad_nets: dict[str, str] = field(default_factory=dict)  # pad number -> new net ("" = disconnect)
    fields: dict[str, str] = field(default_factory=dict)
    sheet_name: str | None = None
    sheet_file: str | None = None

    @property
    def changed(self) -> bool:
        flags = (self.dnp, self.exclude_from_bom, self.exclude_from_sim, self.exclude_from_pos_files)
        return any(
            v is not None for v in (self.old_ref, self.value, self.fpid_change, self.path, *flags)
        ) or bool(self.pad_nets or self.fields or self.sheet_name is not None or self.sheet_file is not None)


@dataclass
class SyncPlan:
    add: list[SyncComponent] = field(default_factory=list)
    update: list[FootprintUpdate] = field(default_factory=list)
    remove: list[FootprintState] = field(default_factory=list)
    kept_locked: list[FootprintState] = field(default_factory=list)
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, fpid, reason)
    net_renames: dict[str, str] = field(default_factory=dict)  # old board net -> new netlist net
    nets: tuple[str, ...] = ()  # every net name of the netlist, in netlist order
    warnings: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    """The contract result of `BoardWriter.sync`."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    footprint_changes: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, old, new)
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, fpid, reason)
    pad_net_changes: list[tuple[str, str, str, str]] = field(default_factory=list)  # (ref, pad, old, new)
    renamed: dict[str, str] = field(default_factory=dict)  # old ref -> new ref
    net_renames: dict[str, str] = field(default_factory=dict)
    kept_locked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    placed: dict[str, tuple[float, float]] = field(default_factory=dict)  # new footprints -> (x, y) mm

    @property
    def changed(self) -> bool:
        return bool(
            self.added
            or self.updated
            or self.removed
            or self.footprint_changes
            or self.pad_net_changes
            or self.renamed
            or self.net_renames
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "added": list(self.added),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "footprint_changes": [list(t) for t in self.footprint_changes],
            "unresolved": [list(t) for t in self.unresolved],
            "pad_net_changes": [list(t) for t in self.pad_net_changes],
            "renamed": dict(self.renamed),
            "net_renames": dict(self.net_renames),
            "kept_locked": list(self.kept_locked),
            "warnings": list(self.warnings),
            "placed": {k: list(v) for k, v in self.placed.items()},
        }


def pad_net_assignment(pad_numbers: Iterable[str], pins: Mapping[str, str]) -> dict[str, str]:
    """Net per pad number: the pin's net, "" for pads that are not a pin (or unnumbered)."""
    out: dict[str, str] = {}
    for number in pad_numbers:
        out[number] = pins.get(number, "") if number else ""
    return out


MATCH_MODES = ("path", "reference")


def plan_sync(
    existing: Iterable[FootprintState],
    netlist: SyncNetlist,
    *,
    remove_orphans: bool = True,
    update_fields: bool = True,
    match: str = "path",
) -> SyncPlan:
    """Decide adds/updates/removes; pure and deterministic (input order preserved).

    `match` is the updater's pairing mode (module docstring): `"path"` (KiCad's default uuid
    mode) or `"reference"` (the GUI's re-link-by-reference checkbox)."""
    if match not in MATCH_MODES:
        raise ValueError(f"match must be one of {MATCH_MODES}, not {match!r}")
    plan = SyncPlan(nets=tuple(netlist.nets))
    boards = list(existing)
    by_ref: dict[str, FootprintState] = {}
    for state in boards:
        by_ref.setdefault(state.ref, state)
    by_path: dict[str, FootprintState] = {}
    for state in boards:
        if state.path:
            by_path.setdefault(state.path, state)
    claimed: set[str] = set()  # footprint uuids matched to a component

    def free(state: FootprintState | None) -> FootprintState | None:
        return None if state is None or state.uuid in claimed else state

    for comp in netlist.components:
        if comp.exclude_from_board:
            continue
        fp: FootprintState | None
        if match == "reference":
            fp, matched_by = free(by_ref.get(comp.ref)), "reference"
            if fp is None:
                fp, matched_by = free(by_path.get(comp.path) if comp.path else None), "path"
        else:
            fp, matched_by = free(by_path.get(comp.path) if comp.path else None), "path"
            if fp is None:
                same_ref = free(by_ref.get(comp.ref))
                if same_ref is not None and (not comp.path or not same_ref.path):
                    fp, matched_by = same_ref, "reference"
                elif same_ref is not None:
                    plan.warnings.append(
                        f"{comp.ref}: the symbol's uuid changed (footprint path {same_ref.path}, "
                        f"netlist {comp.path}); KiCad re-adds the footprint, its placement is not kept"
                    )
        if fp is None:
            if not comp.fpid:
                plan.unresolved.append((comp.ref, "", "no footprint assigned in the schematic"))
                continue
            plan.add.append(comp)
            continue
        claimed.add(fp.uuid)
        upd = FootprintUpdate(uuid=fp.uuid, ref=comp.ref, component=comp, matched_by=matched_by)
        if fp.ref != comp.ref:
            upd.old_ref = fp.ref
        if fp.value != comp.value:
            upd.value = comp.value
        if comp.fpid and comp.fpid != fp.fpid:
            upd.fpid_change = (fp.fpid, comp.fpid)
        elif not comp.fpid:
            plan.warnings.append(f"{comp.ref}: schematic assigns no footprint; keeping {fp.fpid}")
        if comp.path and comp.path != fp.path:
            upd.path = comp.path
        if comp.dnp != fp.dnp:
            upd.dnp = comp.dnp
        if comp.exclude_from_bom != fp.exclude_from_bom:
            upd.exclude_from_bom = comp.exclude_from_bom
        if comp.exclude_from_sim != fp.exclude_from_sim:
            upd.exclude_from_sim = comp.exclude_from_sim
        if comp.exclude_from_pos_files and not fp.exclude_from_pos_files:
            upd.exclude_from_pos_files = True
        if comp.sheet_name != fp.sheet_name:
            upd.sheet_name = comp.sheet_name
        if comp.sheet_file != fp.sheet_file:
            upd.sheet_file = comp.sheet_file
        if update_fields:
            for name, value in comp.fields.items():
                if fp.properties.get(name) != value:
                    upd.fields[name] = value
        # pad nets by pad number; a swap re-instantiates, so every pad of the new body is assigned
        pad_numbers = fp.pad_numbers if upd.fpid_change is None else ()
        if upd.fpid_change is None:
            wanted = pad_net_assignment(pad_numbers, comp.pins)
            for number, net in wanted.items():
                if fp.pad_nets.get(number, "") != net:
                    upd.pad_nets[number] = net
            present = set(pad_numbers)
            for pin in comp.pins:
                if pin not in present:
                    plan.warnings.append(f"{comp.ref}: no pad found for pin {pin} (footprint {fp.fpid})")
        plan.update.append(upd)

    for state in boards:
        if state.uuid in claimed:
            continue
        if not remove_orphans:
            plan.warnings.append(f"{state.ref}: not in the netlist (kept, remove_orphans=False)")
            continue
        if state.locked:
            plan.kept_locked.append(state)
            plan.warnings.append(f"{state.ref}: not in the netlist but locked; left in place")
            continue
        plan.remove.append(state)

    plan.net_renames = _net_renames(boards, plan, netlist)
    return plan


def _net_renames(boards: list[FootprintState], plan: SyncPlan, netlist: SyncNetlist) -> dict[str, str]:
    """old -> new for board nets whose entire (surviving) pad set moved to one new name."""
    removed = {fp.uuid for fp in plan.remove}
    updates = {u.uuid: u for u in plan.update}
    moves: dict[str, set[str]] = {}
    for fp in boards:
        if fp.uuid in removed:
            continue
        upd = updates.get(fp.uuid)
        if upd is None:
            continue
        comp = upd.component
        for number, old in fp.pad_nets.items():
            if not old or old == CONFLICTING_NET or old in netlist.nets:
                continue
            new = comp.pins.get(number, "") if upd.fpid_change is None else comp.pins.get(number, "")
            moves.setdefault(old, set()).add(new)
    return {old: next(iter(news)) for old, news in moves.items() if len(news) == 1 and next(iter(news))}
