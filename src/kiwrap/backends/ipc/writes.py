"""The mutating path over the headless API: shared helpers plus the minimal end-to-end proof.

Two layers live here:

1. The minimal write path that proved the API works (verified on the 10.99 nightly against a
   scratch copy of the ecc83 demo, tests/test_ipc_live.py::test_write_path):
   - board:      BeginCommit -> CreateItems(Track) -> EndCommit(CMA_COMMIT) -> SaveCopyOfDocument;
                 PlaceFootprintFromLibrary (resolves the FPID through the project's fp-lib-table and
                 returns the placed instance with its pads).
   - schematic:  PlaceSymbolFromLibrary (+ reference), CreateItems(SchematicLine wires),
                 GetSchematicNetlist to see the new wire and pin on one net, SaveCopyOfDocument.
   These keep their names (`add_track`, `add_wire`, `place_footprint`, `place_symbol`, `save_copy`).

2. The helpers every authoring twin shares (`sch_writes`, `pcb_writes`): the `commit()` context
   manager (one undo step per operation, `EndCommit(CMA_DROP)` on failure), `editor_of()` (a
   `HeadlessSession` or a bare kipy editor), `unsupported()` (the teaching error for "fall back
   to the file writer"), the 1.27 mm grid helpers and the per-operation latency recorder
   (`LATENCY`, `timed()`), which the live tests print so every nightly bump gets numbers.

The authoring twins themselves are resolved lazily by this module (PEP 562 `__getattr__`) so
callers can import `kiwrap.backends.ipc.writes.sch_add_symbol` etc. (the contract names); the
implementations live in `sch_writes.py` (schematic) and `pcb_writes.py` (board).

Everything takes millimetres; ids come back as KIID strings. Nothing here touches the original
files unless a caller asks for `sch_save` / `pcb_save` without a path: callers normally pass a
scratch copy and `save_copy` writes wherever they say. A `WriteStep` records each request's
outcome so a partially working server still yields a precise report.
"""

from __future__ import annotations

import contextlib
import statistics
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from kiwrap.backends.ipc.models import Point, to_json
from kiwrap.backends.ipc.units import mm_to_nm, nm_to_mm
from kiwrap.capability import Unsupported

__all__ = [
    "GRID_MM",
    "LATENCY",
    "LatencyRecorder",
    "WriteReport",
    "WriteStep",
    "add_track",
    "add_wire",
    "commit",
    "delete_items",
    "editor_of",
    "is_unhandled",
    "on_grid",
    "place_footprint",
    "place_symbol",
    "save_copy",
    "snap_mm",
    "snap_point",
    "timed",
    "unsupported",
    "vec",
]

#: The schematic connection grid (ARCHITECTURE §7: pins connect only at wire endpoints and every
#: coordinate must be a multiple of 1.27 mm, else `endpoint_off_grid`).
GRID_MM = Decimal("1.27")


@dataclass(frozen=True)
class WriteStep:
    name: str
    ok: bool
    detail: str = ""
    ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return to_json(self)


@dataclass
class WriteReport:
    steps: list[WriteStep] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", ids: tuple[str, ...] = ()) -> WriteStep:
        step = WriteStep(name, ok, detail, ids)
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "steps": [s.to_json() for s in self.steps]}


# --- latency -----------------------------------------------------------------------------------------


class LatencyRecorder:
    """Per-operation wall-clock samples (milliseconds), thread-safe. `summary()` gives count /
    median / max per operation; the live tests print it and the report quotes it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: dict[str, list[float]] = {}

    def record(self, op: str, ms: float) -> None:
        with self._lock:
            self._samples.setdefault(op, []).append(ms)

    def samples(self, op: str) -> list[float]:
        with self._lock:
            return list(self._samples.get(op, []))

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def summary(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                op: {
                    "n": float(len(v)),
                    "median_ms": round(statistics.median(v), 1),
                    "max_ms": round(max(v), 1),
                }
                for op, v in sorted(self._samples.items())
                if v
            }


LATENCY = LatencyRecorder()


@contextlib.contextmanager
def timed(op: str) -> Iterator[None]:
    """Record the wall time of one operation under `op` (also on failure, so hangs show up)."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        LATENCY.record(op, (time.perf_counter() - t0) * 1000.0)


# --- geometry helpers --------------------------------------------------------------------------------


def snap_mm(value: float) -> float:
    """Nearest multiple of 1.27 mm (decimal arithmetic, so 96.52 stays 96.52 and never 96.519999)."""
    q = (Decimal(repr(float(value))) / GRID_MM).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return float(q * GRID_MM)


def snap_point(p: Point) -> Point:
    return (snap_mm(p[0]), snap_mm(p[1]))


def on_grid(value: float, tol: float = 1e-6) -> bool:
    return abs(snap_mm(value) - float(value)) <= tol


def vec(p: Point) -> Any:
    """kipy `Vector2` (nm) from a millimetre point."""
    from kipy.geometry import Vector2

    return Vector2.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1]))


def _vec(p: Point) -> Any:  # kept for the original functions below
    return vec(p)


def _layer_enum(name: str) -> Any:
    """`F.Cu` -> BoardLayer.BL_F_Cu (the inverse of readers.layer_name). Typed `Any` because the
    generated stub spells the value as `BoardLayer.ValueType`, which is an int at run time."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    return BoardLayer.Value("BL_" + name.replace(".", "_", 1))


# --- session / editor plumbing -----------------------------------------------------------------------


def editor_of(source: Any, kind: str) -> Any:
    """The kipy editor (`Board` / `Schematic`) behind `source`, which is a `HeadlessSession` (the
    normal case: the session also carries the capability record for the guards) or already a
    kipy editor object (accepted for symmetry with `readers`)."""
    if kind == "schematic":
        if hasattr(source, "schematic") and not hasattr(source, "get_symbols"):
            return source.schematic()
        return source
    if hasattr(source, "board") and not hasattr(source, "get_footprints"):
        return source.board()
    return source


def install_of(source: Any) -> Any:
    return getattr(source, "install", None)


def capabilities_of(source: Any) -> Any:
    """The session's `Capabilities`, or None for a bare editor (guards then assume the worst)."""
    if hasattr(source, "capabilities") and not hasattr(source, "get_symbols"):
        with contextlib.suppress(Exception):
            return source.capabilities
    return None


def unsupported(source: Any, capability: str, *, fallback: str = "file") -> Unsupported:
    """`Unsupported(capability, install, fallback=...)` for a twin that cannot run over IPC here.
    The planner turns it into E_UNSUPPORTED and tries the next provider (`file`)."""
    install = install_of(source)
    if install is None:  # a bare kipy editor: synthesise the minimum the error needs

        class _Install:  # noqa: D401 - stand-in with the attribute the message uses
            version = "unknown"

        install = _Install()
    return Unsupported(capability, install, oldest_kicad_with_it=None, fallback=fallback)


def is_unhandled(exc: BaseException) -> bool:
    """True when KiCad answered AS_UNHANDLED / AS_UNIMPLEMENTED ("no handler available", "not
    available in headless mode"): the presence-probe negative, never a broken session."""
    try:
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode
    except ImportError:  # pragma: no cover - kipy missing: nothing is handled
        return False
    return isinstance(exc, ApiError) and exc.code in (
        ApiStatusCode.AS_UNHANDLED,
        ApiStatusCode.AS_UNIMPLEMENTED,
    )


@contextlib.contextmanager
def commit(editor: Any, message: str) -> Iterator[Any]:
    """One undoable commit around a group of mutations: `BeginCommit` ... `EndCommit(CMA_COMMIT)`;
    any exception drops the commit (`CMA_DROP`, best effort) and propagates. `ipc.commit_needs_document`
    is satisfied by kipy (the editor's document is in every header)."""
    handle = editor.begin_commit()
    try:
        yield handle
    except BaseException:
        with contextlib.suppress(Exception):
            editor.drop_commit(handle)
        raise
    editor.push_commit(handle, message)


def delete_items(editor: Any, uuids: Sequence[str]) -> list[str]:
    """`DeleteItems` with the per-item result checked: kipy's `remove_items_by_id` ignores the
    `deleted_items[].status`, and the server answers AS_OK with `IDS_NONEXISTENT` for an unknown
    KIID (verified on the nightly: deleting a deleted item "succeeds"). Returns the KIIDs actually
    deleted; raises KeyError naming the ones KiCad did not find and ValueError for immutable ones.
    Call inside `commit()`."""
    from kipy.proto.common.commands import editor_commands_pb2 as ec
    from kipy.proto.common.types.base_types_pb2 import KIID

    command = ec.DeleteItems()
    command.header.document.CopyFrom(editor.document)
    command.item_ids.extend(KIID(value=u) for u in uuids)
    response = editor.client.send(command, ec.DeleteItemsResponse)
    deleted: list[str] = []
    missing: list[str] = []
    immutable: list[str] = []
    for result in response.deleted_items:
        if result.status == ec.ItemDeletionStatus.IDS_OK:
            deleted.append(result.id.value)
        elif result.status == ec.ItemDeletionStatus.IDS_IMMUTABLE:
            immutable.append(result.id.value)
        else:
            missing.append(result.id.value)
    for u in uuids:  # ids the server did not even echo
        if u not in deleted and u not in missing and u not in immutable:
            missing.append(u)
    if immutable:
        raise ValueError(f"KiCad refuses to delete immutable item(s) {immutable}")
    if missing:
        raise KeyError(
            f"no item with uuid {missing[0]}" if len(missing) == 1 else f"no items with uuids {missing}"
        )
    return deleted


# --- the original minimal write path -----------------------------------------------------------------


def add_track(
    board: Any,
    start: Point,
    end: Point,
    width_mm: float,
    layer: str,
    net: str,
    *,
    message: str = "kiwrap: add track",
) -> str:
    """Create one track in its own commit (one undo step). Returns the new KIID."""
    from kipy.board_types import Net, Track

    track = Track()
    track.start = _vec(start)
    track.end = _vec(end)
    track.width = mm_to_nm(width_mm)
    track.layer = _layer_enum(layer)
    if net:
        track.net = Net(name=net)
    with commit(board, message):
        created = board.create_items(track)
    return created[0].proto.id.value if created else ""


def place_footprint(
    board: Any, lib_id: str, position: Point, rotation: float = 0.0, layer: str = "F.Cu"
) -> dict[str, Any]:
    """`PlaceFootprintFromLibrary`; returns {id, ref, fpid, position, pads} of the placed instance."""
    from kipy.geometry import Angle

    fp = board.place_footprint_from_library(
        lib_id, _vec(position), Angle.from_degrees(rotation), _layer_enum(layer)
    )
    p = fp.proto
    return {
        "id": p.id.value,
        "ref": p.reference_field.text.text.text,
        "fpid": f"{p.definition.id.library_nickname}:{p.definition.id.entry_name}",
        "position": (nm_to_mm(p.position.x_nm), nm_to_mm(p.position.y_nm)),
        "rotation": float(p.orientation.value_degrees),
        "pads": len(fp.definition.pads),
    }


def place_symbol(
    schematic: Any,
    lib_id: str,
    position: Point,
    rotation: int = 0,
    reference: str | None = None,
    unit: int | None = None,
) -> dict[str, Any]:
    """`PlaceSymbolFromLibrary`; returns {id, ref, lib_id, position, rotation, pins} of the instance."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    orientation = {0: O.SSO_0, 90: O.SSO_90, 180: O.SSO_180, 270: O.SSO_270}[rotation % 360]
    sym = schematic.place_symbol_from_library(lib_id, _vec(position), orientation, unit, reference)
    p = sym.proto
    pins = sum(1 for c in p.definition.items if c.item.type_url.endswith("SchematicPin"))
    return {
        "id": p.id.value,
        "ref": p.reference_field.text.text,
        "lib_id": f"{p.definition.id.library_nickname}:{p.definition.id.entry_name}",
        "position": (nm_to_mm(p.position.x_nm), nm_to_mm(p.position.y_nm)),
        "rotation": rotation % 360,
        "pins": pins,
    }


def add_wire(schematic: Any, start: Point, end: Point, *, message: str = "kiwrap: add wire") -> str:
    """Create one wire (`SchematicLine`, SLT_WIRE) in its own commit. Returns the new KIID."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType
    from kipy.schematic_types import SchematicLine

    line = SchematicLine()
    line.start = _vec(start)
    line.end = _vec(end)
    line.proto.type = SchematicLineType.SLT_WIRE
    with commit(schematic, message):
        created = schematic.create_items(line)
    return created[0].proto.id.value if created else ""


def save_copy(editor: Any, path: str | Path, *, include_project: bool = False) -> Path:
    """`SaveCopyOfDocument` (kipy `save_as`): writes `path` (overwrite allowed) and returns it."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    editor.save_as(str(target), overwrite=True, include_project=include_project)
    return target


# --- the authoring twins (contract names), implemented in sch_writes / pcb_writes -------------------
# Resolved lazily (PEP 562): both modules use the helpers above, so importing them at the top would
# be circular, and a bottom-of-file import breaks whenever `sch_writes` is imported first. The
# names stay importable as `kiwrap.backends.ipc.writes.<twin>` (the contract's stable path).

_SCH_TWINS = frozenset(
    {
        "WireIds",
        "PinEndpoint",
        "SheetPlacement",
        "SymbolPlacement",
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
    }
)
_PCB_TWINS = frozenset(
    {
        "SyncReport",
        "pcb_add_track",
        "pcb_add_via",
        "pcb_add_zone",
        "pcb_delete",
        "pcb_place",
        "pcb_refill",
        "pcb_save",
        "pcb_sync",
        "probe_import_netlist",
    }
)
__all__ += sorted(_SCH_TWINS | _PCB_TWINS)


def __getattr__(name: str) -> Any:
    if name in _SCH_TWINS:
        from kiwrap.backends.ipc import sch_writes

        return getattr(sch_writes, name)
    if name in _PCB_TWINS:
        from kiwrap.backends.ipc import pcb_writes

        return getattr(pcb_writes, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
