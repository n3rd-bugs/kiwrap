"""Dataclasses and duck-typed protocols of `kiwrap.formats.libs`.

`kiwrap.formats` imports nothing from the rest of kiwrap (pyproject import-linter contract), so
the resolver takes the install and the sandbox root as *protocols* (`InstallLike`,
`ConfigRootLike`): `kiwrap.installation.KiCadInstallation` and `kiwrap.sandbox.ConfigRoot`
satisfy them structurally, and tests pass small stand-ins.

Positions are millimetres. Library pin positions are the `(at x y angle)` connection point in
KiCad's library frame, Y-up (docs/VERIFICATION.md claim 3: the schematic writer maps them to
sheet coordinates with the verified 12-orientation transform). Pad positions are the
footprint-local `(at x y)` (Y-down, like a board).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kiwrap.formats.sexpr import Node

__all__ = [
    "ConfigRootLike",
    "InstallLike",
    "LibError",
    "LibFootprint",
    "LibHit",
    "LibPad",
    "LibPin",
    "LibSymbol",
    "LibTableRow",
    "LibraryTables",
    "NotFound",
    "UnsupportedLibrary",
    "numbered_pad_count",
    "split_lib_id",
]


@runtime_checkable
class InstallLike(Protocol):
    """What the resolver needs from `kiwrap.installation.KiCadInstallation`."""

    root: Path
    share: Path
    version: str

    @property
    def major(self) -> int: ...


@runtime_checkable
class ConfigRootLike(Protocol):
    """What the resolver needs from `kiwrap.sandbox.ConfigRoot`: the directory KiCad reads the
    global tables from and the process environment KiCad would run with."""

    @property
    def versioned(self) -> Path: ...

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]: ...


class LibError(Exception):
    """A library problem with a stable `code`, a `why` and a `fix` (mirrors the teaching-error
    shape of `kiwrap.core.state.KiwrapError`, which the tool layer maps onto)."""

    code = "E_LIB"

    def __init__(
        self,
        message: str,
        *,
        why: str = "",
        fix: str = "",
        candidates: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.why = why
        self.fix = fix
        self.candidates = candidates or []
        self.data = data or {}


class NotFound(LibError):
    """The nickname or the item does not exist in the resolved tables/libraries."""

    code = "E_LIB_NOT_FOUND"


class UnsupportedLibrary(LibError):
    """A table row of a plugin type the file tier cannot read (Legacy, Altium, Database, HTTP, ...)."""

    code = "E_LIB_UNSUPPORTED"


def split_lib_id(lib_id: str) -> tuple[str, str]:
    """`"Device:R"` -> `("Device", "R")`; a bare name -> `("", name)`. KiCad splits on the FIRST
    colon (`LIB_ID::Parse`); item names may contain further colons."""
    if ":" in lib_id:
        nick, name = lib_id.split(":", 1)
        return nick.strip(), name
    return "", lib_id


@dataclass(frozen=True)
class LibTableRow:
    """One `(lib ...)` row of a sym-/fp-lib-table after nested `Table` rows were inlined.

    `path` is the URI with `${VAR}` / `$(VAR)` / `~` expanded (None when a variable stayed
    unresolved), `exists` whether that path is on disk. `scope` says which table the row came
    from (a project row shadows a global row of the same nickname, as in KiCad).
    """

    name: str
    type: str
    uri: str
    options: str = ""
    descr: str = ""
    hidden: bool = False
    disabled: bool = False
    scope: str = "global"  # global | project
    source: Path | None = None  # the table file that declared the row
    path: Path | None = None
    exists: bool = False
    error: str | None = None  # why the row cannot be used (unsupported type, unresolved var, ...)

    @property
    def is_kicad(self) -> bool:
        return self.type.lower() == "kicad"

    @property
    def usable(self) -> bool:
        return self.is_kicad and not self.disabled and self.path is not None and self.exists

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "uri": self.uri,
            "path": str(self.path) if self.path is not None else None,
            "exists": self.exists,
            "hidden": self.hidden,
            "disabled": self.disabled,
            "scope": self.scope,
            "descr": self.descr,
            "options": self.options,
            "source": str(self.source) if self.source is not None else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class LibraryTables:
    sym: tuple[LibTableRow, ...]
    fp: tuple[LibTableRow, ...]
    env: Mapping[str, str]  # the variables the URIs were expanded with
    sources: tuple[Path, ...]  # every table file read (global, project, nested), for digests

    def row(self, kind: str, nickname: str) -> LibTableRow | None:
        rows = self.sym if kind == "sym" else self.fp
        for r in rows:
            if r.name == nickname:
                return r
        return None

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {"sym": [r.as_dict() for r in self.sym], "fp": [r.as_dict() for r in self.fp]}


@dataclass(frozen=True)
class LibPin:
    """A library pin. `x_mm`/`y_mm` = the connection point in library coordinates (Y-up);
    `orientation` = the `(at ... angle)` in degrees: 0 the pin runs to +X from its endpoint
    (KiCad "right", endpoint on the west side of the body), 90 up, 180 left, 270 down."""

    number: str
    name: str
    x_mm: float
    y_mm: float
    orientation: int
    electrical_type: str  # passive, input, output, power_in, power_out, bidirectional, ...
    unit: int  # 0 = shared by every unit
    body_style: int  # 0 = shared by both De Morgan body styles
    length_mm: float = 2.54
    shape: str = "line"
    hidden: bool = False

    @property
    def direction(self) -> str:
        """R / U / L / D for 0 / 90 / 180 / 270 (KiCad's legacy pin orientation letters)."""
        return {0: "R", 90: "U", 180: "L", 270: "D"}.get(self.orientation % 360, "?")

    @property
    def shown_name(self) -> str:
        return "" if self.name == "~" else self.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.shown_name,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "orientation": self.orientation,
            "direction": self.direction,
            "electrical_type": self.electrical_type,
            "unit": self.unit,
            "body_style": self.body_style,
            "length_mm": self.length_mm,
            "shape": self.shape,
            "hidden": self.hidden,
        }


@dataclass(frozen=True)
class LibSymbol:
    """A resolved library symbol with `extends` flattened the way `LIB_SYMBOL::Flatten()` does
    (docs/VERIFICATION.md claim 2; `kiwrap.formats.libs.symbols.flatten`).

    `node` is a detached CST copy of the flattened symbol whose top-level name is already
    `<nickname>:<name>` (sub-unit blocks `<name>_U_S` stay unprefixed) - exactly the entry the
    schematic writer embeds in `lib_symbols` (ARCHITECTURE section 7 writer rules). It keeps the
    library file's whitespace (a symbol at nesting depth 1), so after `lib_symbols.append(node)`
    the writer calls `node.reformat()` to re-indent it at depth 2. `raw_node` is the verbatim
    library entry (unflattened, unprefixed).
    """

    lib_id: str
    name: str
    nickname: str
    node: Node = field(repr=False, compare=False)
    units: int = 1
    pins: tuple[LibPin, ...] = ()
    properties: dict[str, str] = field(default_factory=dict)
    footprint_filters: tuple[str, ...] = ()
    keywords: str = ""
    description: str = ""
    extends: str | None = None
    power: str | None = None  # None | global | local
    path: Path | None = None
    unit_names: dict[int, str] = field(default_factory=dict)
    body_styles: int = 1
    pin_names_hidden: bool = False
    pin_numbers_hidden: bool = False
    pin_name_offset_mm: float | None = None
    exclude_from_sim: bool = False
    in_bom: bool = True
    on_board: bool = True
    raw_node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def is_power(self) -> bool:
        return self.power is not None

    @property
    def reference_prefix(self) -> str:
        return self.properties.get("Reference", "")

    @property
    def footprint(self) -> str:
        return self.properties.get("Footprint", "")

    @property
    def datasheet(self) -> str:
        return self.properties.get("Datasheet", "")

    @property
    def pin_count(self) -> int:
        """`LIB_SYMBOL::GetPinCount()`: pins of every unit in body style 1 (the chooser's column)."""
        return len(self.pins_for(unit=0, body_style=1))

    def pins_for(self, unit: int = 1, body_style: int = 1) -> list[LibPin]:
        """Pins of `unit` (0 = every unit) in `body_style`; shared (0) blocks always included."""
        return [
            p for p in self.pins if (unit == 0 or p.unit in (0, unit)) and p.body_style in (0, body_style)
        ]

    def as_dict(self, *, unit: int = 0) -> dict[str, Any]:
        return {
            "lib_id": self.lib_id,
            "nickname": self.nickname,
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "footprint_filters": list(self.footprint_filters),
            "units": self.units,
            "unit_names": {str(k): v for k, v in self.unit_names.items()},
            "body_styles": self.body_styles,
            "power": self.power,
            "extends": self.extends,
            "reference_prefix": self.reference_prefix,
            "footprint": self.footprint,
            "datasheet": self.datasheet,
            "properties": dict(self.properties),
            "pin_count": self.pin_count,
            "pins": [p.as_dict() for p in self.pins_for(unit=unit)],
            "path": str(self.path) if self.path is not None else None,
        }


@dataclass(frozen=True)
class LibPad:
    """A footprint pad in footprint-local millimetres (Y-down)."""

    number: str
    shape: str  # circle | rect | oval | trapezoid | roundrect | chamfered_rect | custom
    at_mm: tuple[float, float]
    size_mm: tuple[float, float]
    layers: tuple[str, ...]
    drill: float | None = None  # drill diameter (or slot width) in mm; None for SMD
    drill_slot_mm: tuple[float, float] | None = None  # (width, height) for oval slots
    type: str = "smd"  # thru_hole | smd | np_thru_hole | connect
    rotation: float = 0.0
    pin_function: str | None = None
    pin_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "type": self.type,
            "shape": self.shape,
            "x_mm": self.at_mm[0],
            "y_mm": self.at_mm[1],
            "rotation": self.rotation,
            "size_mm": list(self.size_mm),
            "layers": list(self.layers),
            "drill_mm": self.drill,
            "drill_slot_mm": list(self.drill_slot_mm) if self.drill_slot_mm else None,
            "pin_function": self.pin_function,
            "pin_type": self.pin_type,
        }


@dataclass(frozen=True)
class LibFootprint:
    """A `.kicad_mod` footprint. `courtyard` = closed rings on the front (or back, for a
    back-side footprint) courtyard layer as (x, y) mm tuples; `courtyard_open` = chains that did
    not close (KiCad's "malformed courtyard")."""

    fpid: str
    name: str
    nickname: str
    node: Node = field(repr=False, compare=False)
    pads: tuple[LibPad, ...] = ()
    courtyard: tuple[tuple[tuple[float, float], ...], ...] = ()
    description: str = ""
    tags: str = ""
    attributes: tuple[str, ...] = ()
    properties: dict[str, str] = field(default_factory=dict)
    layer: str = "F.Cu"
    path: Path | None = None
    courtyard_open: tuple[tuple[tuple[float, float], ...], ...] = ()
    bbox_mm: tuple[float, float, float, float] | None = None  # pads + courtyard extent
    models: tuple[str, ...] = ()

    @property
    def pad_count(self) -> int:
        """KiCad 11's `FOOTPRINT::GetNumberedPadCount()`: distinct electrical pad numbers on
        copper, NPTH excluded, ganged `A1_B12` numbers counting twice."""
        return numbered_pad_count(self.pads)

    @property
    def is_smd(self) -> bool:
        return "smd" in self.attributes

    @property
    def is_through_hole(self) -> bool:
        return "through_hole" in self.attributes

    def as_dict(self) -> dict[str, Any]:
        return {
            "fpid": self.fpid,
            "nickname": self.nickname,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "attributes": list(self.attributes),
            "layer": self.layer,
            "pad_count": self.pad_count,
            "pads_total": len(self.pads),
            "pads": [p.as_dict() for p in self.pads],
            "courtyard": [[list(p) for p in ring] for ring in self.courtyard],
            "courtyard_open": [[list(p) for p in ring] for ring in self.courtyard_open],
            "bbox_mm": list(self.bbox_mm) if self.bbox_mm else None,
            "models": list(self.models),
            "properties": dict(self.properties),
            "path": str(self.path) if self.path is not None else None,
        }


def _electrical_pad_number(num: str) -> bool:
    """`FOOTPRINT::GetNumberedPadCount`'s rule: up to two letters then only digits."""
    if not num:
        return False
    i = 0
    while i < len(num) and num[i].isalpha():
        i += 1
    if i > 2 or i == len(num):
        return False
    return num[i:].isdigit()


def numbered_pad_count(pads: Iterable[LibPad]) -> int:
    """See `LibFootprint.pad_count` (pcbnew/footprint.cpp `GetNumberedPadCount`, KiCad 11)."""
    counted: set[str] = set()
    for pad in pads:
        if pad.type == "np_thru_hole":
            continue
        if not any(layer.endswith(".Cu") for layer in pad.layers):
            continue
        num = pad.number
        if _electrical_pad_number(num):
            counted.add(num)
        elif "_" in num:
            first, second = num.split("_", 1)
            if _electrical_pad_number(first) and _electrical_pad_number(second):
                counted.add(first)
                counted.add(second)
    return len(counted)


@dataclass(frozen=True)
class LibHit:
    """One search result. `score` is higher = better (the FTS rank plus name boosts)."""

    id: str
    name: str
    description: str
    keywords: str
    pin_count: int | None
    footprint_filters: tuple[str, ...]
    score: float
    kind: str = "symbol"  # symbol | footprint
    nickname: str = ""
    pad_count: int | None = None
    units: int = 1
    power: bool = False
    hidden: bool = False
    reference_prefix: str = ""
    footprint: str = ""
    attributes: tuple[str, ...] = ()
    miss: str | None = None  # near-miss reason (None for a full match)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "nickname": self.nickname,
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "score": round(self.score, 3),
            "hidden": self.hidden,
        }
        if self.kind == "symbol":
            d.update(
                {
                    "pin_count": self.pin_count,
                    "units": self.units,
                    "power": self.power,
                    "reference_prefix": self.reference_prefix,
                    "footprint": self.footprint,
                    "footprint_filters": list(self.footprint_filters),
                }
            )
        else:
            d.update({"pad_count": self.pad_count, "attributes": list(self.attributes)})
        if self.miss:
            d["miss"] = self.miss
        return d
