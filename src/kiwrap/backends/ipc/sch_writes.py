"""Schematic authoring twins over the KiCad 11 headless IPC API (`ipc.schematic`).

These are the IPC counterparts of `kiwrap.formats.schematic_writer.SchematicWriter` (the file
writer): same names, same arguments, same result dataclasses, so the planner can route
`kw_sch_*` tools to `ipc11` first and `file` next. Every operation is one commit (one undo step),
takes millimetres and returns KIID strings; pin endpoints are ABSOLUTE sheet coordinates read
back from what KiCad returned (`readers.symbol_transform` over the instance's definition), never
computed from the request.

Verified on the stock 10.99 nightly (10.99.0-4083-g6dd893f7d1, kicad-python f79cebd, 2026-09-19;
scratchpad/phase1b/ipc-writes/probe1..3.py, then tests/test_ipc_writes_live.py):

- `PlaceSymbolFromLibrary` is handled (T5: AS_BAD_REQUEST on an empty lib_id) and works for
  global libraries (`Device:R`, `power:GND`, `power:PWR_FLAG`), project libraries and a chosen
  unit of a multi-unit symbol, but only after `LoadAllLibraries` (`ipc.headless_libraries_need_load`,
  ~1 s once per session). Without a reference KiCad places `R?` (no auto-annotation headless).
  The placed instance carries body style 0 (`(body_style 0)` in the saved file); KiCad reads
  that as the first style but strict readers (the file tier's `pins_for`) see no pins, so
  `sch_add_symbol` normalises it to 1 with its follow-up `UpdateItems` (`_fix_body_style`).
- `UpdateItems` on the placed instance applies value / footprint / mirror / DNP / user fields and
  a new reference; a reference change is written to BOTH the `Reference` property and the
  `instances` block of the saved file and `kicad-cli sch export netlist` follows it (ARCHITECTURE
  §7 writer rule, satisfied by KiCad itself over IPC).
- `CreateItems` handles `SchematicLine` (wires), `LocalLabel`, `GlobalLabel`, `HierarchicalLabel`,
  `Junction` (pruned again at commit time when nothing needs it), `NoConnectMarker` (~45 ms each
  including the commit; `DeleteItems` answers `IDS_NONEXISTENT` per item, which
  `writes.delete_items` turns into KeyError) and a `SchematicSymbolInstance`
  WHEN its pins carry no KIIDs: a clone that keeps the source instance's pin KIIDs wedges the
  server exactly like G13 (duplicate connectivity source identity), and a definition that has
  only pins (no graphics) is accepted but comes back pinless, so the no-`PlaceSymbolFromLibrary`
  fallback clones a same-lib_id instance already in the document or raises `Unsupported`.
- `CreateItems(SheetSymbol)` is accepted and `GetSchematicHierarchy` lists the new sheet, but
  `SaveDocument` then fails ("failed to save schematic") and `SaveCopyOfDocument` writes a file
  without the sheet: hierarchical sheets cannot be authored over IPC on this build, so
  `sch_add_sheet` raises `Unsupported(fallback="file")` without touching the document.
- G13 (`ipc.schematic_update_shared_lib_symbol_hangs`): `UpdateItems` on a second unit of a
  multi-unit library symbol wedges the server WHEN the request echoes the instance's
  `definition` (KiCad deserialises the non-placed units' pins from it and their KIIDs collide).
  Verified 2026-09-19 (probe7.py): with `definition` cleared before `UpdateItems`, units 1, 1, 2
  and 3 of ecc83 U1 update in ~45 ms each, the server keeps answering and the saved file has no
  `lib_name` fork. Echoing the definition has a second cost even on single-unit symbols: KiCad
  compares the round-tripped definition with the library's, finds it different and forks a
  `C_1` lib_symbol (`(lib_name "C_1")`, ERC `lib_symbol_issues`, netlist libsource "") for every
  later placement of that lib_id (probe6.py). Every twin therefore strips `definition` from the
  protos it sends to `UpdateItems` (`_strip_definition`), which is also the G13 workaround.
  `_guard_update` stays as defence in depth for callers that pass `send_definition=True`: on an
  affected build it allows any number of updates of ONE instance per multi-unit lib_id per
  session and raises `Unsupported(fallback="file")` for a second instance.
- The nightly saves `(version 20260830)` schematics, which kicad-cli 10.0.6 refuses to load
  ("Failed to load schematic"), so every IPC result is validated with the nightly's own
  kicad-cli (ERC JSON, netlist) and cross-checked against the file writer's output on 10.0.6 by
  comparing netlists, never by loading the nightly's file on 10.0.6.
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import kiwrap.backends.ipc.writes as _w
from kiwrap.backends.ipc import readers
from kiwrap.backends.ipc.models import Point, SheetInfo
from kiwrap.backends.ipc.units import nm_to_mm
from kiwrap.formats.schematic_writer.model import PinEndpoint, SheetPlacement, SymbolPlacement

__all__ = [
    "WireIds",
    "PinEndpoint",
    "SheetPlacement",
    "SymbolPlacement",
    "WirePlan",
    "plan_wires",
    "probe_place_symbol",
    "sch_add_junction",
    "sch_add_label",
    "sch_add_no_connect",
    "sch_add_power",
    "sch_add_sheet",
    "sch_add_symbol",
    "sch_add_wire",
    "sch_annotate",
    "sch_assign_footprints",
    "sch_delete",
    "sch_edit",
    "sch_pins_of",
    "sch_save",
]

#: capability keys owned by this module (probed live, cached through `Capabilities.refine`)
CAP_PLACE_SYMBOL = "ipc.has_place_symbol_from_library"
CAP_HANG = "ipc.schematic_update_shared_lib_symbol_hangs"
#: pseudo-capabilities named in the `Unsupported` raised for verified-broken paths
CAP_SHEET_CREATE = "ipc.schematic_sheet_create"
CAP_MULTI_UNIT_UPDATE = "ipc.schematic_update_multi_unit"

_TOL = 1e-4  # mm; positions are exact decimals from integer nanometres


# --- result dataclasses: the file writer's own (kiwrap.formats.schematic_writer.model) -----------
# `PinEndpoint(number, name, x_mm, y_mm, electrical_type, unit, hidden, uuid)`,
# `SymbolPlacement(uuid, ref, pins, lib_id, unit, sheet, references)`, `SheetPlacement(...)`: the
# planner hands either provider's result to the same tool code.


# --- proto helpers -----------------------------------------------------------------------------------


def _orientation_enum(rotation: int) -> Any:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    table = {0: O.SSO_0, 90: O.SSO_90, 180: O.SSO_180, 270: O.SSO_270}
    r = int(rotation) % 360
    if r not in table:
        raise ValueError(f"rotation must be a multiple of 90 degrees, got {rotation}")
    return table[r]


def _spin_style(rotation: int) -> Any:
    """Label orientation: 0 -> text runs right, 90 -> up, 180 -> left, 270 -> down (KiCad's
    SPIN_STYLE order for `(at x y angle)` on labels)."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelSpinStyle as S

    table = {0: S.SLSS_RIGHT, 90: S.SLSS_UP, 180: S.SLSS_LEFT, 270: S.SLSS_BOTTOM}
    r = int(rotation) % 360
    if r not in table:
        raise ValueError(f"rotation must be a multiple of 90 degrees, got {rotation}")
    return table[r]


def _label_shape(shape: str) -> Any:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelShape as LS

    table = {
        "input": LS.SLSH_INPUT,
        "output": LS.SLSH_OUTPUT,
        "bidi": LS.SLSH_BIDI,
        "bidirectional": LS.SLSH_BIDI,
        "tristate": LS.SLSH_TRISTATE,
        "tri_state": LS.SLSH_TRISTATE,  # the file writer's / sheet pin spelling (KiCad's token)
        "passive": LS.SLSH_PASSIVE,
    }
    if shape not in table:
        raise ValueError(f"unknown label shape {shape!r}; one of {sorted(table)}")
    return table[shape]


def _lib_id_of(sym_proto: Any) -> str:
    lib = sym_proto.definition.id
    return f"{lib.library_nickname}:{lib.entry_name}" if lib.library_nickname else lib.entry_name


def _pins_from_proto(sym_proto: Any) -> list[PinEndpoint]:
    """Absolute pin endpoints of one instance: the definition's `SchematicPin` children of this
    unit / body style, mapped with `readers.symbol_transform` (verified against wire ends at
    0/90/180/270 and both mirrors, tests/test_ipc_live.py / test_ipc_writes_live.py)."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicPin

    unit = sym_proto.unit.unit
    body = sym_proto.body_style.style if sym_proto.HasField("body_style") else 0
    rot = readers._orientation_degrees(sym_proto.transform.orientation)
    m = readers.symbol_transform(rot, sym_proto.transform.mirror_x, sym_proto.transform.mirror_y)
    ox, oy = sym_proto.position.x_nm, sym_proto.position.y_nm
    out: list[PinEndpoint] = []
    for child in sym_proto.definition.items:
        if not child.item.type_url.endswith("kiapi.schematic.types.SchematicPin"):
            continue
        if child.HasField("unit") and child.unit.unit not in (0, unit):
            continue
        if child.HasField("body_style") and body and child.body_style.style not in (0, body):
            continue
        pin = SchematicPin()
        child.item.Unpack(pin)
        tx, ty = readers.transform_point(m, pin.position.x_nm, pin.position.y_nm)
        etype = readers.pin_type_token(pin.electrical_type)  # KiCad file tokens: power_in, tri_state
        out.append(
            PinEndpoint(
                number=pin.number,
                name=pin.name,
                x_mm=nm_to_mm(ox + tx),
                y_mm=nm_to_mm(oy + ty),
                electrical_type=etype,
                unit=child.unit.unit if child.HasField("unit") else 0,
                hidden=not pin.visible if hasattr(pin, "visible") else False,
                uuid=pin.id.value,
            )
        )
    out.sort(key=lambda p: (len(p.number), p.number))
    return out


def _placement(sym: Any, sheet: str = "") -> SymbolPlacement:
    p = sym.proto
    ref = p.reference_field.text.text
    return SymbolPlacement(
        uuid=p.id.value,
        ref=ref,
        pins=_pins_from_proto(p),
        lib_id=_lib_id_of(p),
        unit=int(p.unit.unit) if p.HasField("unit") else 1,
        sheet=sheet,
        references={readers._kiid_key(p.path): ref} if p.HasField("path") else {},
    )


def _kiid(value: str) -> Any:
    from kipy.proto.common.types.base_types_pb2 import KIID

    return KIID(value=value)


# --- session plumbing --------------------------------------------------------------------------------


def _schematic(source: Any) -> Any:
    return _w.editor_of(source, "schematic")


def _ensure_symbol_libraries(source: Any) -> None:
    """`LoadAllLibraries(symbol)` once per session (`ipc.headless_libraries_need_load`): a headless
    server loads nothing until asked and `PlaceSymbolFromLibrary` answers "not found" before."""
    if not hasattr(source, "load_libraries"):
        return
    if getattr(source, "_kiwrap_symbol_libs_loaded", False):
        return
    with _w.timed("sch.load_libraries"):
        source.load_libraries(("symbol",))
    with contextlib.suppress(AttributeError):
        source._kiwrap_symbol_libs_loaded = True


def _sheet_scope(source: Any, sheet: str) -> tuple[Any, SheetInfo | None]:
    """(kipy Schematic bound to the sheet INSTANCE `sheet`, its SheetInfo). `sheet` is the
    human-readable instance path (`""` or `"/"` = root, `"/sub1/"` = a child instance) or the
    KIID path. Scoping matters for `GetItems` on a sheet file placed several times."""
    sch = _schematic(source)
    hierarchy: SheetInfo | None = None
    try:
        hierarchy = readers._sheet_info(sch.get_hierarchy().root)
    except Exception:  # noqa: BLE001 - no hierarchy answer: unscoped
        return sch, None
    wanted = sheet or "/"
    for info in hierarchy.walk():
        if info.path == wanted or info.kiid_path == wanted or (wanted == "/" and info is hierarchy):
            return readers._sheet_scoped(sch, info), info
    known = [i.path for i in hierarchy.walk()]
    raise ValueError(f"no sheet instance {sheet!r}; known sheet paths: {known}")


def _sheet_items(source: Any, sheet: str) -> tuple[Any, SheetInfo | None, Any]:
    """The scoped schematic plus the sheet's current items as a `SchematicModel` filtered to
    that instance (symbols with pins, wires, junctions, labels, no-connects)."""
    scoped, info = _sheet_scope(source, sheet)
    model = readers.read_schematic(_schematic(source))
    human = info.path if info is not None else ""

    def keep(item: Any) -> bool:
        return human == "" or item.sheet_path == human

    class _View:
        symbols = [s for s in model.symbols if keep(s)]
        wires = [w for w in model.wires if keep(w) and w.kind == "wire"]
        junctions = [j for j in model.junctions if keep(j)]
        labels = [lab for lab in model.labels if keep(lab)]
        no_connects = [n for n in model.no_connects if keep(n)]
        all_symbols = list(model.symbols)
        hierarchy = model.hierarchy

    return scoped, info, _View


# --- G13 guard ---------------------------------------------------------------------------------------

_GUARDS: dict[int, dict[str, set[str]]] = {}


def _hang_affected(source: Any) -> bool:
    caps = _w.capabilities_of(source)
    if caps is None:  # a bare editor: no record, assume the verified nightly behaviour
        return True
    return bool(caps.has(CAP_HANG))


def _guard_update(source: Any, sch: Any, sym_protos: Iterable[Any]) -> None:
    """Refuse (before sending anything) an `UpdateItems` that would touch a second instance of a
    multi-unit library symbol in this session on a build with the G13 hang; records the instances
    this session has updated. Raises `Unsupported(CAP_MULTI_UNIT_UPDATE, fallback="file")`."""
    if not _hang_affected(source):
        return
    guard = _GUARDS.setdefault(id(sch.client), {})
    planned: dict[str, set[str]] = {}
    for p in sym_protos:
        if p.definition.unit_count <= 1:
            continue
        planned.setdefault(_lib_id_of(p), set()).add(p.id.value)
    for lib_id, ids in planned.items():
        seen = guard.setdefault(lib_id, set())
        if len(seen | ids) > 1:
            raise _w.unsupported(source, CAP_MULTI_UNIT_UPDATE, fallback="file")
    for lib_id, ids in planned.items():
        guard[lib_id].update(ids)


def _strip_definition(items: Iterable[Any]) -> None:
    """Drop `definition` from every instance proto before `UpdateItems`: KiCad keeps its own
    library symbol, which avoids both the G13 hang and the `_1` lib_symbol fork (module docstring).
    The response still carries the definition, so pins can be read back afterwards."""
    for item in items:
        item.proto.ClearField("definition")


def _update_symbols(
    source: Any, sch: Any, items: Sequence[Any], message: str, *, send_definition: bool = False
) -> list[Any]:
    """`UpdateItems` on symbol instances in one commit. The definition is stripped unless
    `send_definition` (then the G13 guard decides whether the request may go out)."""
    if send_definition:
        _guard_update(source, sch, [i.proto for i in items])
    else:
        _strip_definition(items)
    with _w.commit(sch, message):
        return list(sch.update_items(list(items)))


# --- presence probe ----------------------------------------------------------------------------------


def probe_place_symbol(source: Any) -> bool:
    """Presence probe for `PlaceSymbolFromLibrary` on the open schematic: one request with an
    empty lib_id; AS_BAD_REQUEST ("lib_id must specify both a library nickname and an entry
    name") means a handler ran, AS_UNHANDLED means none. Records `ipc.has_place_symbol_from_library`
    through `Capabilities.refine` when `source` is a session (once per install)."""
    from kipy.errors import ApiError
    from kipy.proto.schematic.schematic_commands_pb2 import PlaceSymbolFromLibrary

    sch = _schematic(source)
    request = PlaceSymbolFromLibrary()
    request.header.document.CopyFrom(sch.document)
    present: bool
    try:
        from kipy.proto.common.commands.library_commands_pb2 import PlaceFromLibraryResponse

        sch.client.send(request, PlaceFromLibraryResponse)
        present = True
    except ApiError as e:
        present = not _w.is_unhandled(e)
    caps = _w.capabilities_of(source)
    refine = getattr(caps, "refine", None)
    if callable(refine):
        with contextlib.suppress(Exception):
            refine(CAP_PLACE_SYMBOL, present)
    return present


_PROBED_BARE: dict[int, bool] = {}  # id(client) -> PlaceSymbolFromLibrary present (bare editors)


def _can_place_symbol(source: Any) -> bool:
    caps = _w.capabilities_of(source)
    if caps is not None and CAP_PLACE_SYMBOL in (caps.get("kiwrap.ipc_probed", []) or []):
        return bool(caps.has(CAP_PLACE_SYMBOL))
    key = id(_schematic(source).client)
    if key not in _PROBED_BARE:
        _PROBED_BARE[key] = probe_place_symbol(source)
    return _PROBED_BARE[key]


# --- symbols -----------------------------------------------------------------------------------------


def _clone_from_document(sch: Any, lib_id: str) -> Any | None:
    """The fallback definition when `PlaceSymbolFromLibrary` is unhandled: an instance of the same
    lib_id already in the document, copied with its pin KIIDs cleared (keeping them wedges the
    server: verified, probe2.py)."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicPin
    from kipy.schematic_types import SchematicSymbolInstance

    for sym in sch.get_symbols():
        if _lib_id_of(sym.proto) != lib_id:
            continue
        clone = SchematicSymbolInstance()
        clone.proto.CopyFrom(sym.proto)
        clone.proto.ClearField("id")
        clone.proto.ClearField("path")
        for child in clone.proto.definition.items:
            if child.item.type_url.endswith("SchematicPin"):
                pin = SchematicPin()
                child.item.Unpack(pin)
                pin.ClearField("id")
                child.item.Pack(pin)
        return clone
    return None


def _apply_symbol_fields(
    sym: Any,
    *,
    value: str | None,
    footprint: str | None,
    fields: dict[str, str] | None,
    mirror: str | None,
    dnp: bool | None = None,
    reference: str | None = None,
) -> bool:
    """Mutate the instance wrapper in place; True when something changed (an UpdateItems is due)."""
    from kipy.common_types import Text
    from kipy.schematic_types import SchematicField

    p = sym.proto
    changed = False
    if reference is not None and p.reference_field.text.text != reference:
        p.reference_field.text.text = reference
        changed = True
    if value is not None and p.value_field.text.text != value:
        p.value_field.text.text = value
        changed = True
    if footprint is not None and p.footprint_field.text.text != footprint:
        p.footprint_field.text.text = footprint
        changed = True
    if dnp is not None and p.attributes.do_not_populate != dnp:
        p.attributes.do_not_populate = dnp
        changed = True
    if mirror is not None:
        mx, my = mirror == "x", mirror == "y"
        if mirror not in ("x", "y", ""):
            raise ValueError(f"mirror must be 'x', 'y' or None, got {mirror!r}")
        if (p.transform.mirror_x, p.transform.mirror_y) != (mx, my):
            p.transform.mirror_x, p.transform.mirror_y = mx, my
            changed = True
    if fields:
        existing = {f.name: f for f in p.user_fields}
        for name, text in fields.items():
            if name in existing:
                if existing[name].text.text != text:
                    existing[name].text.text = text
                    changed = True
                continue
            f = SchematicField()
            f.name = name
            t = Text()
            t.value = text
            t.position = sym.position  # hidden fields at the symbol origin, not the sheet origin
            f.text = t
            f.visible = False
            sym.user_fields = list(sym.user_fields) + [f]
            changed = True
    return changed


def sch_add_symbol(
    source: Any,
    lib_id: str,
    ref: str | None,
    value: str | None,
    at_mm: Point,
    rotation: int = 0,
    mirror: str | None = None,
    unit: int = 1,
    fields: dict[str, str] | None = None,
    footprint: str | None = None,
    sheet: str = "",
    *,
    snap: bool = True,
    dnp: bool | None = None,
) -> SymbolPlacement:
    """Place `lib_id` (`Nickname:Name`) at `at_mm` (snapped to the 1.27 mm grid unless `snap=False`)
    with `PlaceSymbolFromLibrary`, then apply value / footprint / fields / mirror / dnp with one
    `UpdateItems` commit. Returns the instance's KIID, reference (`R?` when `ref` is None) and its
    absolute pin endpoints (only this unit's pins)."""
    if ":" not in lib_id:
        raise ValueError(f"lib_id must be '<Nickname>:<Name>', got {lib_id!r}")
    if unit < 1:
        raise ValueError("unit must be >= 1")
    pos = _w.snap_point(at_mm) if snap else (float(at_mm[0]), float(at_mm[1]))
    orientation = _orientation_enum(rotation)
    _ensure_symbol_libraries(source)
    scoped, _info = _sheet_scope(source, sheet)
    with _w.timed("sch_add_symbol"):
        if _can_place_symbol(source):
            sym = scoped.place_symbol_from_library(lib_id, _w.vec(pos), orientation, unit, ref)
        else:
            clone = _clone_from_document(scoped, lib_id)
            if clone is None:
                raise _w.unsupported(source, CAP_PLACE_SYMBOL, fallback="file")
            clone.proto.position.CopyFrom(_w.vec(pos).proto)
            clone.proto.transform.orientation = orientation
            clone.proto.transform.mirror_x = False
            clone.proto.transform.mirror_y = False
            clone.proto.unit.unit = unit
            clone.proto.reference_field.text.text = ref or clone.proto.reference_field.text.text
            with _w.commit(scoped, f"kiwrap: add symbol {lib_id}"):
                created = scoped.create_items(clone)
            sym = created[0]
        changed = _apply_symbol_fields(
            sym, value=value, footprint=footprint, fields=fields, mirror=mirror, dnp=dnp
        )
        if _fix_body_style(sym):
            changed = True
        if changed:
            updated = _update_symbols(source, scoped, [sym], f"kiwrap: set fields of {lib_id}")
            if updated:
                sym = updated[0]
    return _placement(sym, sheet)


def _fix_body_style(sym: Any) -> bool:
    """`PlaceSymbolFromLibrary` on the nightly leaves the instance's body style at 0 and the
    saved file then says `(body_style 0)` (verified: probe6 "none" run, every un-updated
    placement). KiCad itself reads 0 as the first body style, but the file tier's
    `SchSymbol.pins_for` (and any strict reader) sees no pins for such a symbol, so the
    placement is normalised to body style 1 with the follow-up `UpdateItems`. True when a fix
    is needed."""
    p = sym.proto
    if p.HasField("body_style") and p.body_style.style >= 1:
        return False
    p.body_style.style = 1
    return True


def sch_add_power(
    source: Any,
    net: str,
    at_mm: Point,
    rotation: int = 0,
    sheet: str = "",
    flag: bool = False,
    *,
    ref: str | None = None,
) -> SymbolPlacement:
    """Place the `power:<net>` symbol at `at_mm` (reference `#PWRnnn`, next free) or, with
    `flag=True`, a `power:PWR_FLAG` there instead (`#FLGnnn`) so a power-input net is driven
    (ARCHITECTURE §7: PWR_FLAG on every power-input net). The same contract as the file writer's
    `SchematicWriter.add_power` and the `pwr_flag` item of kw_sch_add_item: `flag=True` places
    ONLY the flag, never a second power symbol (a `power:<net>` placed at a point that carries a
    label would rename that net). A net without a symbol in the power library gets `power:VCC`
    (or `power:GND` for names containing GND) with its Value set to `net`, which is what names
    the net in KiCad. Returns the placement of the symbol placed."""
    from kipy.errors import ApiError

    _ensure_symbol_libraries(source)
    _scoped, _info, view = _sheet_items(source, sheet)
    if flag:
        flag_ref = ref or _next_reference("#FLG", view.all_symbols)
        return sch_add_symbol(source, "power:PWR_FLAG", flag_ref, None, at_mm, rotation, sheet=sheet)
    reference = ref or _next_reference("#PWR", view.all_symbols)
    try:
        return sch_add_symbol(source, f"power:{net}", reference, None, at_mm, rotation, sheet=sheet)
    except ApiError as e:
        if "not found" not in str(e).lower():
            raise
        generic = "power:GND" if "GND" in net.upper() else "power:VCC"
        return sch_add_symbol(source, generic, reference, net, at_mm, rotation, sheet=sheet)


_REF_RE = re.compile(r"^(?P<prefix>#?[A-Za-z_]+)(?P<number>\d*)(?P<q>\??)$")


def _next_reference(prefix: str, symbols: Iterable[Any]) -> str:
    used = set()
    for s in symbols:
        m = _REF_RE.match(s.ref or "")
        if m and m.group("prefix") == prefix and m.group("number"):
            used.add(int(m.group("number")))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n:02d}" if prefix.startswith("#") else f"{prefix}{n}"


# --- wires -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WirePlan:
    """What `sch_add_wire` will create / split, computed by `plan_wires` (pure, unit-testable):
    `segments` are the new wires (already split at pins, wire ends and junctions lying on them),
    `junctions` the points that need a junction, `split_wires` the existing wire ids to replace
    by two segments each (a new wire end landed on their interior)."""

    segments: list[tuple[Point, Point]]
    junctions: list[Point]
    split_wires: list[tuple[str, Point, Point, Point]]  # (id, start, split point, end)


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) <= _TOL and abs(a[1] - b[1]) <= _TOL


def _on_segment_interior(p: Point, a: Point, b: Point) -> float | None:
    """Parameter t in (0, 1) when `p` lies strictly inside segment a-b, else None."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= _TOL * _TOL:
        return None
    cross = dx * (p[1] - a[1]) - dy * (p[0] - a[0])
    if abs(cross) > _TOL * math.sqrt(length2):
        return None
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2
    if t <= _TOL or t >= 1 - _TOL:
        return None
    return t


def plan_wires(
    points: Sequence[Point],
    pins: Iterable[Point],
    wires: Iterable[tuple[str, Point, Point]],
    junctions: Iterable[Point],
) -> WirePlan:
    """Split the polyline `points` at every pin / existing wire end / junction lying on a segment
    (pins connect only at wire ENDPOINTS - ARCHITECTURE §7) and decide the junctions: a point where
    a new wire end lands on an existing wire's interior (that wire is split there too) or where
    three or more wire ends / pins meet. A bend of the polyline itself needs none."""
    pins = list(pins)
    wires = list(wires)
    junctions = list(junctions)
    wire_ends = [w[1] for w in wires] + [w[2] for w in wires]
    candidates = pins + wire_ends + junctions
    segments: list[tuple[Point, Point]] = []
    for a, b in zip(points, points[1:], strict=False):
        if _same(a, b):
            continue
        cuts: list[tuple[float, Point]] = []
        for c in candidates:
            t = _on_segment_interior(c, a, b)
            if t is not None and not any(_same(c, q) for _, q in cuts):
                cuts.append((t, c))
        cuts.sort(key=lambda x: x[0])
        prev = a
        for _, c in cuts:
            segments.append((prev, c))
            prev = c
        segments.append((prev, b))

    new_ends: list[Point] = [p for seg in segments for p in seg]
    unique: list[Point] = []
    for p in new_ends:
        if not any(_same(p, q) for q in unique):
            unique.append(p)
    need_junction: list[Point] = []
    split: list[tuple[str, Point, Point, Point]] = []
    for p in unique:
        if any(_same(p, j) for j in junctions):
            continue
        interior_hits = [w for w in wires if _on_segment_interior(p, w[1], w[2]) is not None]
        for wid, start, end in interior_hits:
            if not any(s[0] == wid for s in split):
                split.append((wid, start, p, end))
        ends_here = sum(1 for q in new_ends if _same(p, q))
        ends_here += sum(1 for q in wire_ends if _same(p, q))
        ends_here += sum(1 for q in pins if _same(p, q))
        if interior_hits or ends_here >= 3:
            need_junction.append(p)
    return WirePlan(segments, need_junction, split)


class WireIds(list[str]):
    """`sch_add_wire`'s result: the list itself holds the NEW wire segments' KIIDs (what the file
    writer's `add_wire` returns); `junctions` the junction dots added and `split_wires` the two
    halves of every existing wire split under a new end. Kept apart so a receipt never lists a
    junction, or a half KiCad merges away on save, as a wire the agent could delete
    (reviewer finding, 2026-09-20)."""

    def __init__(
        self, wires: Sequence[str] = (), junctions: Sequence[str] = (), split_wires: Sequence[str] = ()
    ):
        super().__init__(wires)
        self.junctions: list[str] = list(junctions)
        self.split_wires: list[str] = list(split_wires)

    @property
    def all_ids(self) -> list[str]:
        return [*self, *self.junctions, *self.split_wires]


def sch_add_wire(source: Any, points_mm: Sequence[Point], sheet: str = "", *, snap: bool = True) -> WireIds:
    """Draw the polyline `points_mm` as wires in one commit: coordinates snapped to 1.27 mm,
    segments split at every pin / wire end / junction on them, existing wires split where a new
    end lands on their interior, junctions added where needed (see `plan_wires`). Returns the
    new wires' KIIDs as a `WireIds` list whose `junctions` / `split_wires` attributes carry the
    other ids the commit created."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType
    from kipy.schematic_types import Junction, SchematicLine

    if len(points_mm) < 2:
        raise ValueError("a wire needs at least two points")
    pts = [_w.snap_point(p) if snap else (float(p[0]), float(p[1])) for p in points_mm]
    with _w.timed("sch_add_wire"):
        scoped, _info, view = _sheet_items(source, sheet)
        pins = [pin.position for s in view.symbols for pin in s.pins]
        wires = [(w.id, w.start, w.end) for w in view.wires]
        plan = plan_wires(pts, pins, wires, [j.position for j in view.junctions])
        if not plan.segments:
            return WireIds()
        items: list[Any] = []
        for a, b in plan.segments:
            line = SchematicLine()
            line.start = _w.vec(a)
            line.end = _w.vec(b)
            line.proto.type = SchematicLineType.SLT_WIRE
            items.append(line)
        for p in plan.junctions:
            j = Junction()
            j.position = _w.vec(p)
            items.append(j)
        for _wid, start, mid, end in plan.split_wires:
            for a, b in ((start, mid), (mid, end)):
                line = SchematicLine()
                line.start = _w.vec(a)
                line.end = _w.vec(b)
                line.proto.type = SchematicLineType.SLT_WIRE
                items.append(line)
        with _w.commit(scoped, "kiwrap: add wire"):
            if plan.split_wires:
                _w.delete_items(scoped, [s[0] for s in plan.split_wires])
            created = scoped.create_items(items)
    ids = [c.proto.id.value for c in created]
    n_wires, n_junctions = len(plan.segments), len(plan.junctions)
    return WireIds(ids[:n_wires], ids[n_wires : n_wires + n_junctions], ids[n_wires + n_junctions :])


# --- labels, no-connects, junctions ------------------------------------------------------------------


def sch_add_label(
    source: Any,
    text: str,
    at_mm: Point,
    kind: str = "local",
    rotation: int = 0,
    sheet: str = "",
    *,
    shape: str = "passive",
    snap: bool = True,
) -> str:
    """One `LocalLabel` / `GlobalLabel` / `HierarchicalLabel` at `at_mm` (snapped); `rotation`
    sets the spin style, `shape` (global / hierarchical only) the ERC direction. A label connects
    anywhere on a wire segment (ARCHITECTURE §7), so no wire split is needed. Returns its KIID."""
    from kipy.common_types import Text
    from kipy.schematic_types import GlobalLabel, HierarchicalLabel, LocalLabel

    classes: dict[str, Any] = {"local": LocalLabel, "global": GlobalLabel, "hierarchical": HierarchicalLabel}
    if kind not in classes:
        raise ValueError(f"kind must be one of {sorted(classes)}, got {kind!r}")
    if not text:
        raise ValueError("label text must not be empty")
    pos = _w.snap_point(at_mm) if snap else (float(at_mm[0]), float(at_mm[1]))
    label = classes[kind]()
    label.position = _w.vec(pos)
    t = Text()
    t.value = text
    label.text = t
    label.spin_style = _spin_style(rotation)
    if kind != "local":
        label.shape = _label_shape(shape)
    with _w.timed("sch_add_label"):
        scoped, _info = _sheet_scope(source, sheet)
        with _w.commit(scoped, f"kiwrap: add {kind} label {text}"):
            created = scoped.create_items(label)
    return created[0].proto.id.value if created else ""


def sch_add_no_connect(source: Any, at_mm: Point, sheet: str = "", *, snap: bool = True) -> str:
    """A `NoConnectMarker` at `at_mm` (a pin endpoint, snapped). Returns its KIID."""
    from kipy.schematic_types import NoConnectMarker

    nc = NoConnectMarker()
    nc.position = _w.vec(_w.snap_point(at_mm) if snap else at_mm)
    with _w.timed("sch_add_no_connect"):
        scoped, _info = _sheet_scope(source, sheet)
        with _w.commit(scoped, "kiwrap: add no-connect"):
            created = scoped.create_items(nc)
    return created[0].proto.id.value if created else ""


def sch_add_junction(source: Any, at_mm: Point, sheet: str = "", *, snap: bool = True) -> str:
    """A `Junction` at `at_mm` (snapped). Returns its KIID. `sch_add_wire` adds the junctions a
    polyline needs by itself; this is for callers that draw wires elsewhere. KiCad prunes a
    junction nothing needs (alone, or on one wire's interior) when the commit is pushed
    (verified: probe11.py), so the returned KIID is dead for such a junction and a later
    `sch_delete` of it raises KeyError."""
    from kipy.schematic_types import Junction

    j = Junction()
    j.position = _w.vec(_w.snap_point(at_mm) if snap else at_mm)
    with _w.timed("sch_add_junction"):
        scoped, _info = _sheet_scope(source, sheet)
        with _w.commit(scoped, "kiwrap: add junction"):
            created = scoped.create_items(j)
    return created[0].proto.id.value if created else ""


# --- sheets ------------------------------------------------------------------------------------------


def sch_add_sheet(
    source: Any,
    name: str,
    file: str,
    at_mm: Point,
    size_mm: tuple[float, float],
    pins: Sequence[tuple[str, str, str]] = (),
    sheet: str = "",
) -> SheetPlacement:
    """Hierarchical sheets cannot be authored over IPC on the verified nightly: `CreateItems`
    accepts the `SheetSymbol`, but `SaveDocument` then fails and `SaveCopyOfDocument` drops it
    (module docstring). Raises `Unsupported(ipc.schematic_sheet_create, fallback="file")` before
    touching the document; the file writer owns this operation."""
    raise _w.unsupported(source, CAP_SHEET_CREATE, fallback="file")


# --- edit / delete -----------------------------------------------------------------------------------

_SYMBOL_KEYS = {
    "at",
    "position",
    "rotation",
    "mirror",
    "value",
    "footprint",
    "reference",
    "ref",
    "dnp",
    "exclude_from_bom",
    "exclude_from_board",
    "fields",
    "unit",
}
_LABEL_KEYS = {"at", "position", "rotation", "text", "shape"}
_POINT_KEYS = {"at", "position"}
_LINE_KEYS = {"start", "end"}


def _point_arg(value: Any, snap: bool) -> Point:
    p = (float(value[0]), float(value[1]))
    return _w.snap_point(p) if snap else p


def sch_edit(source: Any, uuid: str, changes: dict[str, Any], *, snap: bool = True) -> None:
    """Apply `changes` to the item `uuid` with one `UpdateItems` commit. Symbols: `at`/`position`,
    `rotation`, `mirror` ('x' | 'y' | None), `value`, `footprint`, `reference`/`ref`, `dnp`,
    `exclude_from_bom`, `exclude_from_board`, `fields` {name: text}, `unit`. Labels: `at`,
    `rotation`, `text`, `shape`. Wires: `start`, `end`. Junctions / no-connects: `at`. Symbol
    updates strip the definition (G13 workaround, module docstring)."""
    from kipy.errors import ApiError

    sch = _schematic(source)
    try:
        items = sch.get_items_by_id([_kiid(uuid)])
    except ApiError as e:  # "none of the requested IDs were found or valid"
        if "not" in str(e) or "none" in str(e):
            raise KeyError(f"no schematic item with uuid {uuid}") from e
        raise
    if not items:
        raise KeyError(f"no schematic item with uuid {uuid}")
    item = items[0]
    kind = type(item).__name__
    p = item.proto
    with _w.timed("sch_edit"):
        if kind == "SchematicSymbolInstance":
            unknown = set(changes) - _SYMBOL_KEYS
            if unknown:
                raise ValueError(f"unknown symbol changes {sorted(unknown)}; allowed: {sorted(_SYMBOL_KEYS)}")
            for key in _POINT_KEYS & set(changes):
                p.position.CopyFrom(_w.vec(_point_arg(changes[key], snap)).proto)
            if "rotation" in changes:
                p.transform.orientation = _orientation_enum(int(changes["rotation"]))
            if "unit" in changes:
                p.unit.unit = int(changes["unit"])
            if "exclude_from_bom" in changes:
                p.attributes.exclude_from_bill_of_materials = bool(changes["exclude_from_bom"])
            if "exclude_from_board" in changes:
                p.attributes.exclude_from_board = bool(changes["exclude_from_board"])
            _apply_symbol_fields(
                item,
                value=changes.get("value"),
                footprint=changes.get("footprint"),
                fields=changes.get("fields"),
                mirror=(changes["mirror"] or "") if "mirror" in changes else None,
                dnp=changes.get("dnp"),
                reference=changes.get("reference", changes.get("ref")),
            )
            _update_symbols(source, sch, [item], f"kiwrap: edit {p.reference_field.text.text}")
            return
        if kind in ("LocalLabel", "GlobalLabel", "HierarchicalLabel", "DirectiveLabel"):
            unknown = set(changes) - _LABEL_KEYS
            if unknown:
                raise ValueError(f"unknown label changes {sorted(unknown)}; allowed: {sorted(_LABEL_KEYS)}")
            for key in _POINT_KEYS & set(changes):
                p.position.CopyFrom(_w.vec(_point_arg(changes[key], snap)).proto)
            if "rotation" in changes:
                p.spin_style = _spin_style(int(changes["rotation"]))
            if "text" in changes:
                p.text.text = str(changes["text"])
            if "shape" in changes and kind != "LocalLabel":
                p.shape = _label_shape(str(changes["shape"]))
        elif kind == "SchematicLine":
            unknown = set(changes) - _LINE_KEYS
            if unknown:
                raise ValueError(f"unknown wire changes {sorted(unknown)}; allowed: {sorted(_LINE_KEYS)}")
            if "start" in changes:
                p.start.CopyFrom(_w.vec(_point_arg(changes["start"], snap)).proto)
            if "end" in changes:
                p.end.CopyFrom(_w.vec(_point_arg(changes["end"], snap)).proto)
        elif kind in ("Junction", "NoConnectMarker"):
            unknown = set(changes) - _POINT_KEYS
            if unknown:
                raise ValueError(f"unknown {kind} changes {sorted(unknown)}; allowed: {sorted(_POINT_KEYS)}")
            for key in _POINT_KEYS & set(changes):
                p.position.CopyFrom(_w.vec(_point_arg(changes[key], snap)).proto)
        else:
            raise ValueError(f"sch_edit cannot edit a {kind} (uuid {uuid})")
        with _w.commit(sch, f"kiwrap: edit {kind}"):
            sch.update_items(item)


def sch_delete(source: Any, uuid: str) -> None:
    """`DeleteItems` of one item in its own commit; KeyError when KiCad reports the KIID as
    nonexistent (`writes.delete_items` checks the per-item status)."""
    sch = _schematic(source)
    with _w.timed("sch_delete"), _w.commit(sch, f"kiwrap: delete {uuid[:8]}"):
        _w.delete_items(sch, [uuid])


# --- annotation and footprints -----------------------------------------------------------------------


def _split_ref(ref: str) -> tuple[str, int | None]:
    m = _REF_RE.match(ref or "")
    if not m:
        return ref, None
    return m.group("prefix"), (int(m.group("number")) if m.group("number") and not m.group("q") else None)


def plan_annotation(
    symbols: Sequence[Any],
    sheets: Sequence[SheetInfo],
    *,
    scheme: str = "sheet",
    start_per_sheet: bool = False,
    reset: bool = False,
) -> dict[str, tuple[str, str]]:
    """Pure numbering: {uuid: (old ref, new ref)} for the symbols that change. `symbols` are
    `SymbolModel`s (every sheet instance), ordered by sheet, then top-to-bottom, left-to-right.
    Unannotated (`R?`) and duplicated references get numbers; existing ones are kept unless
    `reset`. `scheme="sheet"` with `start_per_sheet` numbers each sheet from page*100 + 1
    (KiCad's "sheet number x 100"); otherwise numbering is global. Units of one multi-unit
    library symbol that are unannotated share one new reference (unit numbers must differ)."""
    order = {info.path: i for i, info in enumerate(sheets)}
    page_of = {info.path: info.page for info in sheets}
    ordered = sorted(symbols, key=lambda s: (order.get(s.sheet_path, 0), s.position[1], s.position[0]))
    used: dict[str, set[int]] = {}
    changes: dict[str, tuple[str, str]] = {}
    todo: list[Any] = []
    seen_refs: dict[tuple[str, str, int], str] = {}  # (sheet, ref, unit) -> uuid
    for s in ordered:
        prefix, number = _split_ref(s.ref)
        if s.ref.startswith("#") and not reset and number is not None:
            used.setdefault(prefix, set()).add(number)
            continue
        key = (s.sheet_path if scheme == "sheet" else "", s.ref, s.unit)
        duplicate = number is not None and key in seen_refs and seen_refs[key] != s.id
        if number is None or reset or duplicate:
            todo.append(s)
            continue
        seen_refs[key] = s.id
        used.setdefault(prefix, set()).add(number)

    def next_number(prefix: str, start: int) -> int:
        n = start
        taken = used.setdefault(prefix, set())
        while n in taken:
            n += 1
        taken.add(n)
        return n

    # Multi-unit grouping: consecutive unannotated units of one lib_id on one sheet share a ref.
    group_ref: dict[tuple[str, str, str], tuple[str, set[int]]] = {}
    for s in todo:
        prefix, _ = _split_ref(s.ref)
        prefix = prefix.rstrip("?") or "U"
        start = 1
        if scheme == "sheet" and start_per_sheet:
            page = page_of.get(s.sheet_path, "1")
            start = (int(page) if str(page).isdigit() else 1) * 100 + 1
        gkey = (s.sheet_path, s.lib_id, prefix)
        new: str | None = None
        if s.unit >= 1 and gkey in group_ref:
            ref, units = group_ref[gkey]
            if s.unit not in units:
                units.add(s.unit)
                new = ref
        if new is None:
            new = f"{prefix}{next_number(prefix, start)}"
            group_ref[gkey] = (new, {s.unit})
        if new != s.ref:
            changes[s.id] = (s.ref, new)
    return changes


def sch_annotate(
    source: Any, scheme: str = "sheet", start_per_sheet: bool = False, *, reset: bool = False
) -> dict[str, str]:
    """Annotate unannotated / duplicated references (`plan_annotation`) and apply the changes with
    one `UpdateItems` commit. Returns {old: new}; when several symbols share the same old
    reference (`R?`, `R?`) the key is `"<old>@<uuid>"` so nothing is lost. On a build with the
    G13 hang, changes touching more than one instance of a multi-unit library symbol raise
    `Unsupported(fallback="file")` before anything is sent (only with `send_definition`; the
    default strips definitions and is safe)."""
    if scheme not in ("sheet", "global"):
        raise ValueError("scheme must be 'sheet' or 'global'")
    sch = _schematic(source)
    with _w.timed("sch_annotate"):
        model = readers.read_schematic(sch, include_pins=False)
        sheets = list(model.hierarchy.walk()) if model.hierarchy else []
        plan = plan_annotation(
            model.symbols, sheets, scheme=scheme, start_per_sheet=start_per_sheet, reset=reset
        )
        if not plan:
            return {}
        items = {i.proto.id.value: i for i in sch.get_symbols() if i.proto.id.value in plan}
        for uuid, (_old, new) in plan.items():
            if uuid in items:
                items[uuid].proto.reference_field.text.text = new
        _update_symbols(source, sch, list(items.values()), "kiwrap: annotate")
    olds = [old for old, _ in plan.values()]
    return {(old if olds.count(old) == 1 else f"{old}@{uuid}"): new for uuid, (old, new) in plan.items()}


def sch_assign_footprints(source: Any, mapping: dict[str, str]) -> list[str]:
    """Set the Footprint field of every symbol instance whose reference is in `mapping`
    (every unit of a multi-unit symbol) with one `UpdateItems` commit; returns the references
    assigned (unknown references are skipped). Definitions are stripped (G13 workaround)."""
    sch = _schematic(source)
    with _w.timed("sch_assign_footprints"):
        targets = []
        for sym in sch.get_symbols():
            ref = sym.proto.reference_field.text.text
            if ref in mapping and sym.proto.footprint_field.text.text != mapping[ref]:
                sym.proto.footprint_field.text.text = mapping[ref]
                targets.append(sym)
        if targets:
            _update_symbols(source, sch, targets, "kiwrap: assign footprints")
    done = {t.proto.reference_field.text.text for t in targets}
    return [ref for ref in mapping if ref in done]


# --- queries and save --------------------------------------------------------------------------------


def sch_pins_of(source: Any, ref: str, sheet: str | None = None) -> list[PinEndpoint]:
    """Absolute pin endpoints of every placed unit of `ref` (optionally on one sheet instance)."""
    sch = _schematic(source)
    out: list[PinEndpoint] = []
    scoped = sch if sheet is None else _sheet_scope(source, sheet)[0]
    for sym in scoped.get_symbols():
        if sym.proto.reference_field.text.text == ref:
            out.extend(_pins_from_proto(sym.proto))
    return out


def sch_save(source: Any, path: str | Path | None = None, *, include_project: bool = False) -> Path:
    """`SaveDocument` in place (`path=None`, returns the session's file) or `SaveCopyOfDocument`
    to `path`. The nightly writes its own format stamp (`ipc.*` builds: 20260830 today), so the
    file is validated with the same install's kicad-cli."""
    sch = _schematic(source)
    with _w.timed("sch_save"):
        if path is None:
            sch.save()
            file = getattr(source, "file", None)
            return Path(file) if file else Path(sch.name)
        return _w.save_copy(sch, path, include_project=include_project)
