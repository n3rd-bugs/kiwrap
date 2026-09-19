"""The minimal mutating path over the headless API, to prove writes work end to end.

Steps (all verified on the 10.99 nightly against a scratch copy of the ecc83 demo, see
tests/test_ipc_live.py::test_write_path and the findings in this component's report):
- board:      BeginCommit -> CreateItems(Track) -> EndCommit(CMA_COMMIT) -> SaveCopyOfDocument;
              PlaceFootprintFromLibrary (resolves the FPID through the project's fp-lib-table and
              returns the placed instance with its pads).
- schematic:  PlaceSymbolFromLibrary (+ reference), CreateItems(SchematicLine wires),
              GetSchematicNetlist to see the new wire and pin on one net, SaveCopyOfDocument.

Everything takes millimetres; ids come back as KIID strings. Nothing here touches the original
files: callers pass a scratch copy and `save_copy` writes wherever they say. A `WriteStep` records
each request's outcome so a partially working server still yields a precise report.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.backends.ipc.models import Point, to_json
from kiwrap.backends.ipc.units import mm_to_nm, nm_to_mm

__all__ = [
    "WriteReport",
    "WriteStep",
    "add_track",
    "add_wire",
    "place_footprint",
    "place_symbol",
    "save_copy",
]


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


def _vec(p: Point) -> Any:
    from kipy.geometry import Vector2

    return Vector2.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1]))


def _layer_enum(name: str) -> Any:
    """`F.Cu` -> BoardLayer.BL_F_Cu (the inverse of readers.layer_name). Typed `Any` because the
    generated stub spells the value as `BoardLayer.ValueType`, which is an int at run time."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    return BoardLayer.Value("BL_" + name.replace(".", "_", 1))


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
    commit = board.begin_commit()
    try:
        created = board.create_items(track)
    except Exception:
        with contextlib.suppress(Exception):
            board.drop_commit(commit)
        raise
    board.push_commit(commit, message)
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
    commit = schematic.begin_commit()
    try:
        created = schematic.create_items(line)
    except Exception:
        with contextlib.suppress(Exception):
            schematic.drop_commit(commit)
        raise
    schematic.push_commit(commit, message)
    return created[0].proto.id.value if created else ""


def save_copy(editor: Any, path: str | Path, *, include_project: bool = False) -> Path:
    """`SaveCopyOfDocument` (kipy `save_as`): writes `path` (overwrite allowed) and returns it."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    editor.save_as(str(target), overwrite=True, include_project=include_project)
    return target
