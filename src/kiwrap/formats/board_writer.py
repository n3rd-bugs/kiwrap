"""Board authoring on the lossless CST: setup, netlist sync, placement, tracks, vias, zones.

`BoardWriter` edits (or creates) a `.kicad_pcb` through `kiwrap.formats.sexpr`, so every node it
does not touch is re-emitted byte for byte. It knows KiCad **format stamps**, never KiCad
release numbers: the `epoch` mapping (`{"pcb": 20260206, "mod": …, "pro": 3}`, the
`formats.*` keys of the capability record) selects the dialect. Facts encoded, all checked
against the demo boards of the installed KiCads and kicad-cli (docs/VERIFICATION.md claim 5 for
why the netlist import is hand-written; conformance test T8 for the oracle runs):

* nets: `(net N "name")` table + `(net N "name")` on pads / `(net N)` on tracks, vias and zones
  (+ `(net_name "…")` on zones) up to stamp 20250907; from 20260206 on there is **no table** and
  every reference is `(net "name")` (`kiwrap.formats.board` module docstring; KiCad 10.0.6's
  pic_programmer demo and every board the nightly writes). A pad without a net carries no
  `(net …)` list at all;
* layer ids were renumbered with stamp 20241229 (`B.Cu` 31 -> 2, `Edge.Cuts` 44 -> 25, inner
  copper `In1.Cu` 1 -> 4, …; `kiwrap.core.tools.templates` carries the same two tables);
* footprint text angles and pad `(at x y angle)` angles are **absolute** (footprint rotation
  included, `kiwrap.formats.board` docstring), local coordinates are not rotated; a flipped
  footprint stores mirrored local coordinates (y negated), negated angles, swapped `F.*`/`B.*`
  layers and `(justify mirror)` on its texts (`FOOTPRINT::Flip`, pcbnew/footprint.cpp);
* `(locked yes)` is a list from 20241229 on and a bare `locked` flag before;
* a footprint's rule areas (`(zone …)` inside `(footprint …)`) are stored in BOARD coordinates
  on every dialect before `FIRST_FP_AFFINE_TRANSFORM` (KiCad ≤ 10 moves / rotates / flips them
  with the footprint: `FOOTPRINT::SetPosition` / `Rotate` / `Flip` on `m_zones`; a `.kicad_mod`
  holds them in the library frame around (0, 0)) and in the LIBRARY frame from that stamp on
  (the parser of master converts legacy outlines with `TRANSFORM_TRS::InverseApply`). The
  writer bakes a placed footprint's zones into the board frame on the legacy dialect
  (`_bake_zone_frame`) and unbakes them before every placement change (`_unbake_zone_frame`),
  so `kw_pcb_sync` / `kw_pcb_place` leave the ESP32 module's antenna keep-out where KiCad 10
  expects it (Phase 1c finding: the keep-out sat at the library coordinates, off-board);
* the nightly (stamp 20260616+, `FIRST_FP_AFFINE_TRANSFORM`) *writes* a footprint's placement
  as `(transform (translate x y) (rotate r) (scale 1 1))` but still *reads* `(at x y r)`
  (verified: `pcb export pos` and parity DRC on 10.99.0-4083 agree with what this writer
  wrote; `pcb upgrade` re-saves it as `transform`). The writer keeps emitting `(at …)` because
  `kiwrap.formats.board.BoardFile` reads only that form today; `footprint_placement()` /
  `set_footprint_placement()` read and update either form, so boards KiCad re-saved stay editable;
* the footprint `(path "/<sheet uuids>/<symbol uuid>")` is what `pcb drc --schematic-parity`
  uses to pair a footprint with a symbol (`BOARD::FindFootprintByPath`), `(sheetname)` /
  `(sheetfile)` are informational; references live in `(property "Reference" …)` since 8.0;
* a library `.kicad_mod` becomes a board footprint by dropping its `version`/`generator` header,
  qualifying its name to `Nick:Name`, adding `uuid`/`at`/`path`, and regenerating item uuids
  (`FOOTPRINT`'s copy constructor assigns fresh KIIDs);
* design rules and netclasses live in the `.kicad_pro` (`board.design_settings.rules`,
  `net_settings.classes` + `netclass_patterns`), custom rules in `<stem>.kicad_dru`; kicad-cli
  DRC reads both when they sit next to the board.

Library access goes through a `FootprintSource` (the `footprint(fpid)` method of
`kiwrap.formats.libs.LibResolver`, or the private `MinimalFootprintResolver` here, which reads
`fp-lib-table` rows — project-local with `${KIPRJMOD}`, global, nested `Table` rows — and
`<footprints dir>/<Nick>.pretty/<Name>.kicad_mod`).
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid as _uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kiwrap.formats.board import STAMP_FP_AFFINE_TRANSFORM, BoardFile, copper_layer_rows
from kiwrap.formats.dru import DruRule, render_dru
from kiwrap.formats.epochs import read_stamp
from kiwrap.formats.fileio import atomic_write_text
from kiwrap.formats.geometry import BBox, Vec2, rotate
from kiwrap.formats.netlist_sync import (
    CONFLICTING_NET,
    FootprintState,
    FootprintUpdate,
    SyncComponent,
    SyncNetlist,
    SyncReport,
    netlist_from_any,
    pad_net_assignment,
    plan_sync,
)
from kiwrap.formats.reports import Netlist
from kiwrap.formats.sexpr import Atom, Node, build, dumps, load, loads

__all__ = [
    "BoardWriter",
    "BoardWriterError",
    "Dialect",
    "FootprintSource",
    "LoadedFootprint",
    "LoadedPad",
    "MinimalFootprintResolver",
    "SyncReport",
    "dialect_for",
    "flip_footprint",
    "footprint_placement",
    "set_footprint_placement",
]

# Format-stamp thresholds (dates KiCad's writer changed the token set; not release numbers).
STAMP_FOOTPRINT_FIELDS = 20231007  # `(property "Reference" …)` + `(uuid …)` instead of fp_text/tstamp
STAMP_LAYER_IDS_V2 = 20241229  # renumbered layer ids, `(locked yes)` lists, `(tenting …)`
STAMP_NET_NAMES_ONLY = 20260206  # no `(net N "name")` table; `(net "name")` everywhere
#: `net_settings.meta.version` that carries `netclass_patterns` (KiCad NET_SETTINGS schema 3;
#: older file versions are migrated by replacing the patterns with the legacy `nets` lists)
NET_SETTINGS_SCHEMA_PATTERNS = 3
# STAMP_FP_AFFINE_TRANSFORM (20260616, `(transform …)` placements, footprint zones in the library
# frame) lives in `kiwrap.formats.board`; it is re-exported here for the callers that import it.
STAMP_EXCLUDE_FROM_SIM = 20260828  # `exclude_from_sim` footprint attribute (pcb_io_kicad_sexpr.h)
MIN_STAMP = STAMP_FOOTPRINT_FIELDS

GRID_GAP_MM = 2.0  # spacing between freshly placed footprints
GRID_MARGIN_MM = 10.0  # distance between the outline / existing parts and the placement grid
GRID_MIN_ROW_MM = 100.0  # a row of new parts is at least this wide before wrapping

# Layer tables per dialect: (id, name, type, [user name]).
_NON_COPPER_V1: tuple[tuple[int, str, str, str | None], ...] = (
    (32, "B.Adhes", "user", "B.Adhesive"),
    (33, "F.Adhes", "user", "F.Adhesive"),
    (34, "B.Paste", "user", None),
    (35, "F.Paste", "user", None),
    (36, "B.SilkS", "user", "B.Silkscreen"),
    (37, "F.SilkS", "user", "F.Silkscreen"),
    (38, "B.Mask", "user", None),
    (39, "F.Mask", "user", None),
    (40, "Dwgs.User", "user", "User.Drawings"),
    (41, "Cmts.User", "user", "User.Comments"),
    (42, "Eco1.User", "user", "User.Eco1"),
    (43, "Eco2.User", "user", "User.Eco2"),
    (44, "Edge.Cuts", "user", None),
    (45, "Margin", "user", None),
    (46, "B.CrtYd", "user", "B.Courtyard"),
    (47, "F.CrtYd", "user", "F.Courtyard"),
    (48, "B.Fab", "user", None),
    (49, "F.Fab", "user", None),
    (50, "User.1", "user", None),
    (51, "User.2", "user", None),
    (52, "User.3", "user", None),
    (53, "User.4", "user", None),
)
_NON_COPPER_V2: tuple[tuple[int, str, str, str | None], ...] = (
    (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", None),
    (15, "B.Paste", "user", None),
    (5, "F.SilkS", "user", "F.Silkscreen"),
    (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user", None),
    (3, "B.Mask", "user", None),
    (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", None),
    (27, "Margin", "user", None),
    (31, "F.CrtYd", "user", "F.Courtyard"),
    (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user", None),
    (33, "B.Fab", "user", None),
    (39, "User.1", "user", None),
    (41, "User.2", "user", None),
    (43, "User.3", "user", None),
    (45, "User.4", "user", None),
)

# Rule keys accepted by `setup(rules=…)` -> `.kicad_pro` board.design_settings.rules keys.
_RULE_KEYS: dict[str, str] = {
    "clearance_mm": "min_clearance",
    "min_clearance_mm": "min_clearance",
    "min_track_mm": "min_track_width",
    "min_track_width_mm": "min_track_width",
    "min_via_mm": "min_via_diameter",
    "min_via_diameter_mm": "min_via_diameter",
    "min_via_annular_mm": "min_via_annular_width",
    "min_hole_mm": "min_through_hole_diameter",
    "min_through_hole_mm": "min_through_hole_diameter",
    "hole_to_hole_mm": "min_hole_to_hole",
    "min_hole_to_hole_mm": "min_hole_to_hole",
    "hole_clearance_mm": "min_hole_clearance",
    "copper_edge_clearance_mm": "min_copper_edge_clearance",
    "min_copper_edge_clearance_mm": "min_copper_edge_clearance",
    "silk_clearance_mm": "min_silk_clearance",
    "min_text_height_mm": "min_text_height",
    "min_text_thickness_mm": "min_text_thickness",
    "min_connection_mm": "min_connection",
    "solder_mask_to_copper_clearance_mm": "solder_mask_to_copper_clearance",
}
# Netclass keys accepted by `setup(rules=…)` and by every `netclasses[name]` mapping.
_NETCLASS_KEYS: dict[str, str] = {
    "clearance_mm": "clearance",
    "track_width_mm": "track_width",
    "via_size_mm": "via_diameter",
    "via_diameter_mm": "via_diameter",
    "via_drill_mm": "via_drill",
    "microvia_size_mm": "microvia_diameter",
    "microvia_diameter_mm": "microvia_diameter",
    "microvia_drill_mm": "microvia_drill",
    "diff_pair_width_mm": "diff_pair_width",
    "diff_pair_gap_mm": "diff_pair_gap",
    "diff_pair_via_gap_mm": "diff_pair_via_gap",
}
_DEFAULT_NETCLASS: dict[str, Any] = {
    "bus_width": 12,
    "clearance": 0.2,
    "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25,
    "diff_pair_width": 0.2,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "name": "Default",
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "priority": 2147483647,
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "track_width": 0.2,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "wire_width": 6,
}

_FP_ITEM_HEADS = frozenset(
    {
        "fp_line",
        "fp_arc",
        "fp_circle",
        "fp_rect",
        "fp_poly",
        "fp_curve",
        "fp_text",
        "fp_text_box",
        "pad",
        "image",
        "zone",
        "group",
        "dimension",
        "table",
        "barcode",
        "reference_image",
    }
)
_FP_TRAILER_HEADS = frozenset({"embedded_fonts", "embedded_files", "model"})
_FP_HEADER_HEADS = frozenset(
    {
        "version",
        "generator",
        "generator_version",
        "layer",
        "uuid",
        "tstamp",
        "at",
        "descr",
        "tags",
        "property",
    }
)
_FP_LINK_HEADS = frozenset({"path", "sheetname", "sheetfile", "attr"})
_MANDATORY_FIELDS = ("Reference", "Value", "Datasheet", "Description")


class BoardWriterError(ValueError):
    """A request the writer cannot honour (unknown reference/uuid, bad epoch, missing library)."""


# ----------------------------------------------------------------------------------------------
# dialect
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Dialect:
    stamp: int

    @property
    def net_names_only(self) -> bool:
        return self.stamp >= STAMP_NET_NAMES_ONLY

    @property
    def layer_ids_v2(self) -> bool:
        return self.stamp >= STAMP_LAYER_IDS_V2

    @property
    def locked_as_list(self) -> bool:
        return self.stamp >= STAMP_LAYER_IDS_V2

    @property
    def uuid_token(self) -> str:
        return "uuid"

    @property
    def has_exclude_from_sim(self) -> bool:
        return self.stamp >= STAMP_EXCLUDE_FROM_SIM

    @property
    def fp_zones_in_board_frame(self) -> bool:
        """Footprint rule areas are stored in board coordinates (every dialect before the
        affine-transform one, `FIRST_FP_AFFINE_TRANSFORM`; see the module docstring)."""
        return self.stamp < STAMP_FP_AFFINE_TRANSFORM

    def copper_layer_rows(self, names: Sequence[str]) -> list[tuple[int, str, str, str | None]]:
        rows: list[tuple[int, str, str, str | None]] = []
        n = len(names)
        for i, name in enumerate(names):
            if i == 0:
                lid = 0
            elif i == n - 1:
                lid = 2 if self.layer_ids_v2 else 31
            else:
                lid = 2 * i + 2 if self.layer_ids_v2 else i
            rows.append((lid, name, "signal", None))
        return rows

    def non_copper_rows(self) -> tuple[tuple[int, str, str, str | None], ...]:
        return _NON_COPPER_V2 if self.layer_ids_v2 else _NON_COPPER_V1


def dialect_for(epoch: Mapping[str, int]) -> Dialect:
    try:
        stamp = int(epoch["pcb"])
    except (KeyError, TypeError, ValueError):
        raise BoardWriterError(
            "epoch needs an integer 'pcb' stamp (formats.pcb of the capability record)"
        ) from None
    if stamp < MIN_STAMP:
        raise BoardWriterError(
            f"board stamp {stamp} predates the footprint-field dialect ({MIN_STAMP}); the writer targets 8.0+ formats"
        )
    return Dialect(stamp)


# ----------------------------------------------------------------------------------------------
# footprint library access
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedPad:
    number: str
    shape: str
    at_mm: tuple[float, float]
    size_mm: tuple[float, float]
    layers: tuple[str, ...]
    drill: float | None


@dataclass(frozen=True)
class LoadedFootprint:
    """The minimal twin of `kiwrap.formats.libs.LibFootprint`: what the writer consumes."""

    fpid: str
    node: Node
    pads: tuple[LoadedPad, ...] = ()
    courtyard: tuple[tuple[float, float], ...] = ()
    description: str = ""
    path: Path | None = None


class FootprintSource(Protocol):
    """Anything with `footprint(fpid) -> object with a `.node` (the `.kicad_mod` CST root)`."""

    def footprint(self, fpid: str) -> Any: ...


_ENV_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)")


def expand_env(text: str, env: Mapping[str, str]) -> str:
    """`${VAR}` / `$(VAR)` substitution with KiCad's rule that unknown names stay literal."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return _ENV_RE.sub(sub, text)


def read_fp_lib_table(path: Path, env: Mapping[str, str], *, _depth: int = 0) -> dict[str, Path]:
    """nickname -> directory for every `KiCad` row of `path`; `Table` rows are followed (10+)."""
    out: dict[str, Path] = {}
    if _depth > 4 or not path.is_file():
        return out
    try:
        root = loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return out
    for row in root.walk():
        if row.head != "lib":
            continue
        fields = {c.head: (c.atoms[0].value if c.atoms else "") for c in row.nodes}
        name = fields.get("name", "")
        kind = fields.get("type", "KiCad")
        uri = expand_env(fields.get("uri", ""), env)
        if not name and kind.lower() != "table":
            continue
        if kind.lower() == "table":
            nested = Path(uri)
            if not nested.is_absolute():
                nested = path.parent / nested
            for k, v in read_fp_lib_table(nested, env, _depth=_depth + 1).items():
                out.setdefault(k, v)
        elif kind.lower() == "kicad":
            out.setdefault(name, Path(uri))
    return out


class MinimalFootprintResolver:
    """Footprint lookup without `kiwrap.formats.libs` (reported in the build notes).

    Resolution order for `Nick:Name`: the project's `fp-lib-table` (with `${KIPRJMOD}` = the
    project directory), then every table in `tables`, then `<footprints_dir>/<Nick>.pretty`.
    `env` supplies `${KICAD<major>_FOOTPRINT_DIR}`-style variables (pass the sandbox root's
    `env()`); `KIPRJMOD` is always the project dir.
    """

    def __init__(
        self,
        footprints_dir: Path | None = None,
        project_dir: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        tables: Iterable[Path] = (),
    ) -> None:
        self.footprints_dir = Path(footprints_dir) if footprints_dir else None
        self.project_dir = Path(project_dir) if project_dir else None
        base = dict(env if env is not None else os.environ)
        if self.project_dir is not None:
            base["KIPRJMOD"] = self.project_dir.as_posix()
        self.env = base
        self.table_paths = [Path(t) for t in tables]
        self._rows: dict[str, Path] | None = None
        self._cache: dict[str, LoadedFootprint] = {}

    def rows(self) -> dict[str, Path]:
        if self._rows is None:
            rows: dict[str, Path] = {}
            if self.project_dir is not None:
                rows.update(read_fp_lib_table(self.project_dir / "fp-lib-table", self.env))
            for t in self.table_paths:
                for k, v in read_fp_lib_table(t, self.env).items():
                    rows.setdefault(k, v)
            self._rows = rows
        return self._rows

    def tables(self) -> dict[str, list[dict[str, str]]]:
        return {"fp": [{"name": k, "uri": v.as_posix()} for k, v in self.rows().items()], "sym": []}

    def footprint(self, fpid: str) -> LoadedFootprint:
        if fpid in self._cache:
            return self._cache[fpid]
        nick, _, name = fpid.rpartition(":")
        if not name or not nick:
            raise BoardWriterError(f"footprint id {fpid!r} is not of the form Nick:Name")
        candidates: list[Path] = []
        row = self.rows().get(nick)
        if row is not None:
            candidates.append(row / f"{name}.kicad_mod")
        if self.footprints_dir is not None:
            candidates.append(self.footprints_dir / f"{nick}.pretty" / f"{name}.kicad_mod")
        for path in candidates:
            if path.is_file():
                node = load(path)
                fp = load_footprint_node(node, fpid, path=path)
                self._cache[fpid] = fp
                return fp
        looked = ", ".join(p.as_posix() for p in candidates) or "no fp-lib-table row and no footprints dir"
        raise BoardWriterError(f"footprint {fpid!r} not found (looked at: {looked})")


def load_footprint_node(node: Node, fpid: str, *, path: Path | None = None) -> LoadedFootprint:
    if node.head != "footprint" and node.head != "module":
        raise BoardWriterError(
            f"{path or fpid}: not a footprint document (top-level list is ({node.head or '?'} …))"
        )
    pads: list[LoadedPad] = []
    for pad in node.find_all("pad"):
        atoms = pad.atoms
        at = pad.find("at")
        size = pad.find("size")
        drill = pad.find("drill")
        layers_node = pad.find("layers")
        drill_mm: float | None = None
        if drill is not None:
            nums = [a for a in drill.atoms if a.kind == "number"]
            drill_mm = nums[0].as_float() if nums else None
        pads.append(
            LoadedPad(
                number=atoms[0].value if atoms else "",
                shape=atoms[2].value if len(atoms) > 2 else "",
                at_mm=_xy_of(at),
                size_mm=_xy_of(size),
                layers=tuple(a.value for a in (layers_node.atoms if layers_node is not None else [])),
                drill=drill_mm,
            )
        )
    return LoadedFootprint(
        fpid=fpid, node=node, pads=tuple(pads), description=node.get("descr") or "", path=path
    )


def _xy_of(node: Node | None) -> tuple[float, float]:
    if node is None:
        return (0.0, 0.0)
    atoms = node.atoms
    if len(atoms) < 2:
        return (0.0, 0.0)
    try:
        return (atoms[0].as_float(), atoms[1].as_float())
    except ValueError:
        return (0.0, 0.0)


# ----------------------------------------------------------------------------------------------
# small CST helpers
# ----------------------------------------------------------------------------------------------


def new_uuid() -> str:
    return str(_uuid.uuid4())


def _num(v: float) -> float:
    r = round(float(v), 6)
    return 0.0 if r == 0 else r


def _uuid_of(node: Node) -> str:
    return node.get("uuid") or node.get("tstamp") or ""


def _set_uuid(node: Node, value: str) -> None:
    for head in ("uuid", "tstamp"):
        child = node.find(head)
        if child is not None:
            node.set(head, value)
            return
    node.set("uuid", value)


def _regenerate_uuids(node: Node) -> None:
    for n in node.walk():
        for head in ("uuid", "tstamp"):
            child = n.find(head)
            if child is not None and child.atoms:
                child.atoms[0].value = new_uuid()


def _bare_flag(node: Node, name: str) -> bool:
    return any(a.kind == "symbol" and a.value == name for a in node.atoms)


def _property_node(node: Node, name: str) -> Node | None:
    for p in node.find_all("property"):
        atoms = p.atoms
        if atoms and atoms[0].value == name:
            return p
    return None


def _property_value(node: Node, name: str) -> str | None:
    p = _property_node(node, name)
    if p is None:
        return None
    atoms = p.atoms
    return atoms[1].value if len(atoms) > 1 else ""


def _set_property_text(p: Node, value: str) -> None:
    atoms = p.atoms
    if len(atoms) > 1:
        if atoms[1].value != value:
            atoms[1].kind = "string"
            atoms[1].value = value
    else:
        p.insert(2, Atom.string(value))


def _make_property(name: str, value: str, *, hide: bool, layer: str = "F.Fab", angle: float = 0) -> Node:
    node = build("property", name, value, build("at", 0, 0, _num(angle)), build("layer", layer))
    if hide:
        node.append(build("hide", Atom.symbol("yes")))
    node.append(build("uuid", new_uuid()))
    node.append(build("effects", build("font", build("size", 1.27, 1.27), build("thickness", 0.15))))
    return node


def _property_from_fp_text(fp_text: Node, name: str) -> Node:
    """`(fp_text reference "R1" (at …) (layer …) [hide] (effects …) (tstamp …))` -> the 8.0+
    `(property "Reference" "R1" …)` form (KiCad's own 7 -> 8 migration)."""
    text = fp_text.atoms[1].value if len(fp_text.atoms) > 1 else ""
    node = build("property", name, text)
    for child in fp_text.nodes:
        if child.head in ("at", "layer", "effects", "hide", "unlocked"):
            node.append(child.copy())
    if any(a.kind == "symbol" and a.value == "hide" for a in fp_text.atoms) and node.find("hide") is None:
        node.append(build("hide", Atom.symbol("yes")))
    node.append(build("uuid", new_uuid()))
    return node


def _attr_flags(node: Node) -> list[str]:
    attr = node.find("attr")
    return [a.value for a in attr.atoms] if attr is not None else []


def _set_attr_flags(node: Node, flags: Sequence[str], insert_before: Node | None) -> None:
    attr = node.find("attr")
    if not flags:
        if attr is not None:
            node.remove(attr)
        return
    if attr is None:
        attr = build("attr", *(Atom.symbol(f) for f in flags))
        idx = node.index(insert_before) if insert_before is not None else len(node.children)
        node.insert(idx, attr)
        return
    node.set("attr", *(Atom.symbol(f) for f in flags))


def _first_index(node: Node, heads: Iterable[str]) -> int | None:
    wanted = set(heads)
    for i, c in enumerate(node.children):
        if isinstance(c, Node) and c.head in wanted:
            return i
    return None


def _last_index(node: Node, heads: Iterable[str]) -> int | None:
    wanted = set(heads)
    found: int | None = None
    for i, c in enumerate(node.children):
        if isinstance(c, Node) and c.head in wanted:
            found = i
    return found


def _angle_of(at: Node) -> float:
    atoms = at.atoms
    return atoms[2].as_float() if len(atoms) > 2 else 0.0


def _set_at(node: Node, x: float, y: float, angle: float | None) -> None:
    at = node.find("at")
    values: list[Any] = [_num(x), _num(y)]
    if angle:
        values.append(_num(angle))
    if at is None:
        idx = _first_index(node, _FP_ITEM_HEADS | {"descr", "tags", "property"})
        node.insert(idx if idx is not None else len(node.children), build("at", *values))
    else:
        node.set("at", *values)


def footprint_placement(fp: Node) -> tuple[float, float, float]:
    """`(x, y, rotation)` of a footprint from `(at x y [rot])` or, when a board written by a
    KiCad with the affine-transform dialect (stamp 20260616+) carries one, from
    `(transform (translate x y) (rotate r) …)`."""
    transform = fp.find("transform")
    if transform is not None:
        x, y = _xy_of(transform.find("translate"))
        rot_node = transform.find("rotate")
        rot = rot_node.atoms[0].as_float() if rot_node is not None and rot_node.atoms else 0.0
        return x, y, rot
    at = fp.find("at")
    x, y = _xy_of(at)
    return x, y, (_angle_of(at) if at is not None else 0.0)


def set_footprint_placement(fp: Node, x: float, y: float, rotation: float) -> None:
    """Write the placement back into whichever form the footprint uses (see `footprint_placement`)."""
    transform = fp.find("transform")
    if transform is not None:
        transform.set("translate", _num(x), _num(y))
        transform.set("rotate", _num(rotation))
        return
    _set_at(fp, x, y, rotation)


def _normalize180(angle: float) -> float:
    a = math.fmod(angle, 360.0)
    if a > 180:
        a -= 360
    elif a <= -180:
        a += 360
    return _num(a)


def _normalize360(angle: float) -> float:
    a = math.fmod(angle, 360.0)
    if a < 0:
        a += 360
    return _num(a)


def _rotate_items(fp: Node, delta: float) -> None:
    """Add `delta` degrees to the absolute angles of pads and texts (local xy untouched)."""
    if not delta:
        return
    for item in fp.nodes:
        if item.head in ("pad", "property", "fp_text", "fp_text_box"):
            at = item.find("at")
            if at is None:
                continue
            atoms = at.atoms
            new = _normalize360(_angle_of(at) + delta)
            if len(atoms) > 2 and new:
                atoms[2].value = new
            elif len(atoms) > 2:
                at.remove(atoms[2])
            elif new:
                at.append(Atom.number(new))


def _map_zone_points(fp: Node, fn: Callable[[float, float], tuple[float, float]]) -> None:
    """Apply `fn(x, y) -> (x, y)` to every outline / fill vertex of the footprint's zones."""
    for zone in fp.find_all("zone"):
        for poly in [*zone.find_all("polygon"), *zone.find_all("filled_polygon")]:
            pts = poly.find("pts")
            if pts is None:
                continue
            for xy in pts.nodes:
                atoms = xy.atoms
                if xy.head == "xy" and len(atoms) >= 2:
                    x, y = fn(atoms[0].as_float(), atoms[1].as_float())
                    atoms[0].value = _num(x)
                    atoms[1].value = _num(y)


def _bake_zone_frame(fp: Node) -> None:
    """Library frame -> board frame for the footprint's zones (legacy dialect, see the module
    docstring): `p = rotate(q, rotation) + position`, KiCad's `RotatePoint` convention (the same
    transform the board model applies to pads)."""
    x, y, rot = footprint_placement(fp)
    pos = Vec2(x, y)

    def fn(qx: float, qy: float) -> tuple[float, float]:
        v = rotate(Vec2(qx, qy), rot) + pos
        return v.x, v.y

    _map_zone_points(fp, fn)


def _unbake_zone_frame(fp: Node) -> None:
    """Board frame -> library frame (the inverse of `_bake_zone_frame` at the CURRENT placement)."""
    x, y, rot = footprint_placement(fp)
    pos = Vec2(x, y)

    def fn(px: float, py: float) -> tuple[float, float]:
        v = rotate(Vec2(px, py) - pos, -rot)
        return v.x, v.y

    _map_zone_points(fp, fn)


_FLIP_PREFIX = {"F.": "B.", "B.": "F."}


def flip_layer_name(name: str) -> str:
    for src, dst in _FLIP_PREFIX.items():
        if name.startswith(src):
            return dst + name[2:]
    return name


def _mirror_y(node: Node, heads: Iterable[str]) -> None:
    for head in heads:
        for child in node.find_all(head):
            atoms = child.atoms
            if len(atoms) >= 2 and atoms[1].kind == "number":
                atoms[1].value = _num(-atoms[1].as_float())


def _mirror_pts(node: Node) -> None:
    """Mirror the y of every `(pts …)` vertex below `node`: a direct child on `fp_poly` /
    `fp_curve`, nested under `(polygon …)` / `(filled_polygon …)` on a zone."""
    for pts in node.walk():
        if pts.head != "pts":
            continue
        for xy in pts.nodes:
            if xy.head == "xy":
                atoms = xy.atoms
                if len(atoms) >= 2:
                    atoms[1].value = _num(-atoms[1].as_float())
            elif xy.head == "arc":
                _mirror_y(xy, ("start", "mid", "end"))


def _flip_layers(node: Node) -> None:
    layer = node.find("layer")
    if layer is not None and layer.atoms:
        layer.atoms[0].value = flip_layer_name(layer.atoms[0].value)
    layers = node.find("layers")
    if layers is not None:
        for a in layers.atoms:
            a.value = flip_layer_name(a.value)


def _negate_angle(node: Node) -> None:
    at = node.find("at")
    if at is not None and len(at.atoms) > 2:
        at.atoms[2].value = _normalize360(-at.atoms[2].as_float())


def _toggle_mirror(effects_parent: Node) -> None:
    effects = effects_parent.find("effects")
    if effects is None:
        effects = effects_parent.append(build("effects"))
    justify = effects.find("justify")
    if justify is None:
        effects.append(build("justify", Atom.symbol("mirror")))
        return
    flags = [a.value for a in justify.atoms]
    if "mirror" in flags:
        flags.remove("mirror")
    else:
        flags.append("mirror")
    if flags:
        effects.set("justify", *(Atom.symbol(f) for f in flags))
    else:
        effects.remove(justify)


def flip_footprint(fp: Node) -> None:
    """`FOOTPRINT::Flip(position, TOP_BOTTOM)` on the CST: side, mirrored local geometry,
    negated angles, swapped layers, mirrored texts. The board position is kept."""
    _flip_layers(fp)
    x, y, rot = footprint_placement(fp)
    if rot:
        set_footprint_placement(fp, x, y, _normalize180(-rot))
    for item in fp.nodes:
        head = item.head
        if head == "pad":
            _mirror_y(item, ("at",))
            _negate_angle(item)
            _flip_layers(item)
            drill = item.find("drill")
            if drill is not None:
                _mirror_y(drill, ("offset",))
            prims = item.find("primitives")
            if prims is not None:
                for g in prims.nodes:
                    _mirror_y(g, ("start", "mid", "end", "center"))
                    _mirror_pts(g)
                    if g.head == "gr_arc":
                        _swap_children(g, "start", "end")
            chamfer = item.find("chamfer")
            if chamfer is not None:
                swap = {
                    "top_left": "bottom_left",
                    "bottom_left": "top_left",
                    "top_right": "bottom_right",
                    "bottom_right": "top_right",
                }
                for a in chamfer.atoms:
                    a.value = swap.get(a.value, a.value)
        elif head in ("property", "fp_text", "fp_text_box"):
            _mirror_y(item, ("at",))
            _negate_angle(item)
            _flip_layers(item)
            _toggle_mirror(item)
        elif head in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve"):
            _mirror_y(item, ("start", "mid", "end", "center"))
            _mirror_pts(item)
            _flip_layers(item)
            if head == "fp_arc":
                _swap_children(item, "start", "end")
        elif head in ("zone", "image", "dimension", "group"):
            _mirror_pts(item)
            _mirror_y(item, ("at", "start", "end"))
            _flip_layers(item)


def _swap_children(node: Node, a: str, b: str) -> None:
    na, nb = node.find(a), node.find(b)
    if na is None or nb is None:
        return
    va, vb = [x.raw for x in na.atoms], [x.raw for x in nb.atoms]
    for atom, raw in zip(na.atoms, vb, strict=False):
        atom.raw = raw
    for atom, raw in zip(nb.atoms, va, strict=False):
        atom.raw = raw


# ----------------------------------------------------------------------------------------------
# net references
# ----------------------------------------------------------------------------------------------


class _Nets:
    """The board's net vocabulary in either dialect (see module docstring)."""

    def __init__(self, root: Node, dialect: Dialect) -> None:
        self.root = root
        self.dialect = dialect
        self.codes: dict[str, int] = {}
        self.names: dict[int, str] = {}
        for n in root.find_all("net"):
            atoms = n.atoms
            if len(atoms) >= 1 and atoms[0].kind == "number":
                code = atoms[0].as_int()
                name = atoms[1].value if len(atoms) > 1 else ""
                self.codes.setdefault(name, code)
                self.names.setdefault(code, name)
        if not dialect.net_names_only and 0 not in self.names:
            self.codes.setdefault("", 0)
            self.names[0] = ""

    def ensure(self, name: str) -> int:
        """Make `name` known (a table row in the code dialect); returns its code (0 = none)."""
        if self.dialect.net_names_only:
            return 0
        if name in self.codes:
            return self.codes[name]
        code = max(self.names, default=-1) + 1
        self.codes[name] = code
        self.names[code] = name
        self._insert_row(code, name)
        return code

    def _insert_row(self, code: int, name: str) -> None:
        row = build("net", code, name)
        last = _last_index(self.root, ("net",))
        if last is not None:
            self.root.insert(last + 1, row)
            return
        anchor = _last_index(self.root, ("setup", "layers", "title_block", "paper", "general"))
        self.root.insert((anchor + 1) if anchor is not None else len(self.root.children), row)

    def name_of(self, ref: Node | None) -> str:
        """Net name referenced by a `(net …)` child of an item (either dialect)."""
        if ref is None or not ref.atoms:
            return ""
        first = ref.atoms[0]
        if first.kind == "number":
            if len(ref.atoms) > 1:
                return ref.atoms[1].value
            return self.names.get(first.as_int(), "")
        return first.value

    def set_ref(self, item: Node, name: str, *, with_name: bool, before: Iterable[str]) -> None:
        """Write `(net …)` on an item: pads carry code + name, tracks/vias/zones only the code
        (code dialect); everything carries `(net "name")` in the name dialect. An empty name
        removes the reference (a pad without a net)."""
        existing = item.find("net")
        if not name:
            if existing is not None:
                item.remove(existing)
            if item.head == "zone":
                nn = item.find("net_name")
                if nn is not None:
                    item.remove(nn)
            return
        if self.dialect.net_names_only:
            values: list[Any] = [name]
        else:
            code = self.ensure(name)
            values = [code, name] if with_name else [code]
        if existing is not None:
            item.set("net", *values)
        else:
            idx = _first_index(item, before)
            item.insert(idx if idx is not None else len(item.children), build("net", *values))
        if item.head == "zone" and not self.dialect.net_names_only:
            item.set("net_name", name)

    def rebuild_table(self, used: Iterable[str]) -> None:
        """Code dialect: drop table rows nothing references any more (KiCad's
        `RemoveUnusedNets` after an update); codes of surviving nets are kept."""
        if self.dialect.net_names_only:
            return
        keep = set(used) | {""}
        for row in list(self.root.find_all("net")):
            atoms = row.atoms
            if not atoms or atoms[0].kind != "number":
                continue
            name = atoms[1].value if len(atoms) > 1 else ""
            if name not in keep:
                self.root.remove(row)
                code = atoms[0].as_int()
                self.names.pop(code, None)
                self.codes.pop(name, None)


# ----------------------------------------------------------------------------------------------
# the writer
# ----------------------------------------------------------------------------------------------


@dataclass
class _ProjectEdits:
    rules: dict[str, float] = field(default_factory=dict)
    default_class: dict[str, Any] = field(default_factory=dict)
    classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)
    touched: bool = False


class BoardWriter:
    """Author a `.kicad_pcb` (plus its `.kicad_pro` rules/netclasses and `.kicad_dru`).

    `load(board_path)` reads an existing board or starts a fresh one when the file does not
    exist yet (a minimal skeleton in the epoch's dialect: general, paper, full layer table for
    the requested copper count, setup). `save()` writes only what changed.
    """

    def __init__(
        self,
        project_dir: Path,
        *,
        epoch: Mapping[str, int],
        lib_resolver: FootprintSource | None = None,
        generator_version: str | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.epoch = dict(epoch)
        self.dialect = dialect_for(self.epoch)
        self.lib_resolver = lib_resolver
        self.generator_version = generator_version
        self.board_path: Path | None = None
        self.root: Node | None = None
        self._nets: _Nets | None = None
        self._dirty = False
        self._project = _ProjectEdits()
        self._dru_text: str | None = None
        self._copper: list[str] = ["F.Cu", "B.Cu"]

    # -- loading / saving ----------------------------------------------------------------------

    def load(self, board_path: Path | str) -> None:
        path = Path(board_path)
        if not path.is_absolute():
            path = self.project_dir / path
        self.board_path = path
        if path.is_file():
            self.root = load(path)
            if self.root.head != "kicad_pcb":
                raise BoardWriterError(
                    f"{path}: not a KiCad board (top-level list is ({self.root.head or '?'} …))"
                )
            stamp = read_stamp(self.root)
            if stamp < MIN_STAMP:
                raise BoardWriterError(
                    f"{path}: stamp {stamp} predates the supported dialects ({MIN_STAMP}+)"
                )
            self.dialect = Dialect(stamp)
            self._dirty = False
        else:
            self.root = self._skeleton(self._copper)
            self._dirty = True
        self._nets = _Nets(self.root, self.dialect)
        self._copper = self._copper_layers()

    def _require(self) -> Node:
        if self.root is None:
            raise BoardWriterError("call load(board_path) first")
        return self.root

    @property
    def nets(self) -> _Nets:
        self._require()
        assert self._nets is not None
        return self._nets

    @property
    def pro_path(self) -> Path:
        if self.board_path is None:
            raise BoardWriterError("call load(board_path) first")
        return self.board_path.with_suffix(".kicad_pro")

    @property
    def dru_path(self) -> Path:
        if self.board_path is None:
            raise BoardWriterError("call load(board_path) first")
        return self.board_path.with_suffix(".kicad_dru")

    def _skeleton(self, copper: Sequence[str]) -> Node:
        d = self.dialect
        gv = f'\t(generator_version "{self.generator_version}")\n' if self.generator_version else ""
        rows = d.copper_layer_rows(copper) + list(d.non_copper_rows())
        layer_lines = "".join(
            f'\t\t({lid} "{name}" {kind}' + (f' "{user}"' if user else "") + ")\n"
            for lid, name, kind, user in rows
        )
        text = (
            "(kicad_pcb\n"
            f"\t(version {d.stamp})\n"
            '\t(generator "pcbnew")\n'
            f"{gv}"
            "\t(general\n"
            "\t\t(thickness 1.6)\n"
            "\t\t(legacy_teardrops no)\n"
            "\t)\n"
            '\t(paper "A4")\n'
            "\t(layers\n"
            f"{layer_lines}"
            "\t)\n"
            "\t(setup\n"
            "\t\t(pad_to_mask_clearance 0)\n"
            "\t\t(allow_soldermask_bridges_in_footprints no)\n"
            "\t\t(pcbplotparams\n"
            f"\t\t\t(layerselection {'0x00000000_00000000_55555555_5755f5ff' if d.layer_ids_v2 else '0x00010fc_ffffffff'})\n"
            "\t\t\t(plot_on_all_layers_selection "
            f"{'0x00000000_00000000_00000000_00000000' if d.layer_ids_v2 else '0x0000000_00000000'})\n"
            '\t\t\t(outputdirectory "")\n'
            "\t\t)\n"
            "\t)\n" + ("" if d.net_names_only else '\t(net 0 "")\n') + ")\n"
        )
        return loads(text)

    def _copper_layers(self) -> list[str]:
        """The copper layers the way KiCad's parser takes them: the leading block of typed rows
        (`kiwrap.formats.board.copper_layer_rows`), never every row typed `signal`."""
        root = self._require()
        table = root.find("layers")
        out: list[str] = []
        if table is not None:
            out = [row.values()[1] for row in copper_layer_rows(table.nodes) if len(row.values()) >= 2]
        return out or ["F.Cu", "B.Cu"]

    def save(self) -> Path:
        """Write the board (only when touched), the `.kicad_pro` edits and the `.kicad_dru`."""
        root = self._require()
        assert self.board_path is not None
        self.board_path.parent.mkdir(parents=True, exist_ok=True)
        if self._dirty or not self.board_path.is_file():
            atomic_write_text(self.board_path, dumps(root), newline="")
            self._dirty = False
        if self._project.touched:
            self._write_project()
            self._project.touched = False
        if self._dru_text is not None:
            atomic_write_text(self.dru_path, self._dru_text, newline="\n")
            self._dru_text = None
        return self.board_path

    def text(self) -> str:
        return dumps(self._require())

    # -- setup ---------------------------------------------------------------------------------

    def setup(
        self,
        layers: int | Sequence[str] | None = None,
        rules: Mapping[str, float] | None = None,
        netclasses: Mapping[str, Mapping[str, Any]] | None = None,
        outline: Mapping[str, Any] | None = None,
        custom_rules: str | Iterable[DruRule | str] | None = None,
    ) -> dict[str, Any]:
        """Layer table (copper count or names), rules + netclasses (`.kicad_pro`), outline on
        Edge.Cuts (`{"rect": (w, h)}` / `{"w":…, "h":…, "origin": (x, y)}` / `{"polygon": pts}`),
        custom rules (`.kicad_dru`). Returns what was applied."""
        applied: dict[str, Any] = {}
        if layers is not None:
            applied["layers"] = self._set_layers(layers)
        if rules:
            applied["rules"] = self._set_rules(rules)
        if netclasses:
            applied["netclasses"] = self._set_netclasses(netclasses)
        if outline:
            applied["outline"] = self._set_outline(outline)
        if custom_rules is not None:
            self._dru_text = render_dru(custom_rules)
            applied["custom_rules"] = self.dru_path.name
        return applied

    def _set_layers(self, layers: int | Sequence[str]) -> list[str]:
        root = self._require()
        if isinstance(layers, int):
            if layers < 2 or layers % 2:
                raise BoardWriterError("copper layer count must be an even number >= 2")
            names = ["F.Cu", *(f"In{i}.Cu" for i in range(1, layers - 1)), "B.Cu"]
        else:
            names = list(layers)
            if len(names) < 2 or names[0] != "F.Cu" or names[-1] != "B.Cu":
                raise BoardWriterError("copper layers must start with F.Cu and end with B.Cu")
        table = root.find("layers")
        if table is None:
            raise BoardWriterError("board has no (layers …) table")
        copper_rows = copper_layer_rows(table.nodes)
        existing_types = {row.values()[1]: row.values()[2] for row in copper_rows if len(row.values()) >= 3}
        for row in copper_rows:
            table.remove(row)
        first = next((i for i, c in enumerate(table.children) if isinstance(c, Node)), len(table.children))
        for i, (lid, name, kind, _user) in enumerate(self.dialect.copper_layer_rows(names)):
            kind = existing_types.get(name, kind)
            if kind not in ("signal", "power", "mixed", "jumper"):
                kind = "signal"
            row = Node([Atom.number(lid), Atom.string(name), Atom.symbol(kind)])
            row.reformat()
            table.insert(first + i, row)
        self._copper = names
        self._dirty = True
        return names

    def _set_rules(self, rules: Mapping[str, float]) -> dict[str, float]:
        applied: dict[str, float] = {}
        for key, value in rules.items():
            if key in _RULE_KEYS:
                self._project.rules[_RULE_KEYS[key]] = float(value)
                applied[_RULE_KEYS[key]] = float(value)
            if key in _NETCLASS_KEYS:
                self._project.default_class[_NETCLASS_KEYS[key]] = float(value)
                applied[_NETCLASS_KEYS[key]] = float(value)
            if key not in _RULE_KEYS and key not in _NETCLASS_KEYS:
                raise BoardWriterError(
                    f"unknown rule {key!r}; known: {sorted(set(_RULE_KEYS) | set(_NETCLASS_KEYS))}"
                )
        self._project.touched = True
        return applied

    def _set_netclasses(self, netclasses: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        for name, spec in netclasses.items():
            values: dict[str, Any] = {}
            nets: list[str] = []
            patterns: list[str] = []
            for key, value in spec.items():
                if key == "nets":
                    nets = [str(n) for n in value]
                elif key == "patterns":
                    patterns = [str(p) for p in value]
                elif key in _NETCLASS_KEYS:
                    values[_NETCLASS_KEYS[key]] = float(value)
                elif key in _DEFAULT_NETCLASS:
                    values[key] = value
                else:
                    raise BoardWriterError(f"unknown netclass key {key!r} for {name}")
            if name == "Default":
                self._project.default_class.update(values)
            else:
                self._project.classes.setdefault(name, {}).update(values)
            for n in nets:
                self._project.patterns.append((name, _escape_pattern(n)))
            for p in patterns:
                self._project.patterns.append((name, p))
            applied[name] = {**values, "nets": nets, "patterns": patterns}
        self._project.touched = True
        return applied

    def _set_outline(self, outline: Mapping[str, Any]) -> list[tuple[float, float]]:
        root = self._require()
        pts: list[tuple[float, float]]
        if "polygon" in outline or "pts" in outline:
            raw = outline.get("polygon", outline.get("pts"))
            pts = [(float(p[0]), float(p[1])) for p in raw]
            if len(pts) < 3:
                raise BoardWriterError("a polygon outline needs at least 3 points")
        else:
            rect = outline.get("rect")
            if rect is not None:
                w, h = float(rect[0]), float(rect[1])
            else:
                w, h = float(outline["w"]), float(outline["h"])
            ox, oy = outline.get("origin", (0.0, 0.0))
            ox, oy = float(ox), float(oy)
            pts = [(ox, oy), (ox + w, oy), (ox + w, oy + h), (ox, oy + h)]
        if outline.get("replace", True):
            for g in list(root.nodes):
                if g.head.startswith("gr_") and g.get("layer") == "Edge.Cuts":
                    root.remove(g)
        for i, (x1, y1) in enumerate(pts):
            x2, y2 = pts[(i + 1) % len(pts)]
            line = build(
                "gr_line",
                build("start", _num(x1), _num(y1)),
                build("end", _num(x2), _num(y2)),
                build("stroke", build("width", 0.1), build("type", Atom.symbol("default"))),
                build("layer", "Edge.Cuts"),
                build("uuid", new_uuid()),
            )
            self._insert_graphic(line)
        self._dirty = True
        return pts

    def _write_project(self) -> None:
        path = self.pro_path
        data: dict[str, Any]
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        else:
            data = {
                "board": {"design_settings": {"defaults": {}, "rules": {}}},
                "boards": [],
                "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
                "meta": {"filename": path.name, "version": int(self.epoch.get("pro", 1))},
                "pcbnew": {"page_layout_descr_file": ""},
                "sheets": [],
                "text_variables": {},
            }
        ds = data.setdefault("board", {}).setdefault("design_settings", {})
        rules = ds.setdefault("rules", {})
        rules.update(self._project.rules)
        ns = data.setdefault("net_settings", {})
        classes: list[dict[str, Any]] = [c for c in ns.get("classes", []) if isinstance(c, dict)]
        by_name = {c.get("name"): c for c in classes}
        default = by_name.get("Default")
        if default is None:
            default = dict(_DEFAULT_NETCLASS)
            classes.insert(0, default)
            by_name["Default"] = default
        default.update(self._project.default_class)
        for name, values in self._project.classes.items():
            cls = by_name.get(name)
            if cls is None:
                cls = {
                    **_DEFAULT_NETCLASS,
                    **{k: default.get(k, v) for k, v in _DEFAULT_NETCLASS.items()},
                    "name": name,
                    "priority": len(classes) - 1,
                }
                classes.append(cls)
                by_name[name] = cls
            cls.update(values)
        ns["classes"] = classes
        if self._project.patterns:
            patterns = [p for p in ns.get("netclass_patterns", []) or [] if isinstance(p, dict)]
            seen = {(p.get("netclass"), p.get("pattern")) for p in patterns}
            for netclass, pattern in self._project.patterns:
                if (netclass, pattern) not in seen:
                    patterns.append({"netclass": netclass, "pattern": pattern})
                    seen.add((netclass, pattern))
            ns["netclass_patterns"] = patterns
        # KiCad migrates a `net_settings` whose `meta.version` is below its schema on load, and
        # `migrateSchema2to3` (file version < 3) REPLACES `netclass_patterns` with the classes'
        # legacy `nets` lists - a template project (KiCad's own kicad.kicad_pro says version 0)
        # would silently lose every pattern written here, leaving Default only (seen on 10.0.6:
        # one class in the DSN export, the Power class never applied by DRC). Version 3 is the
        # schema that introduced the patterns (common/project/net_settings.cpp); the later
        # migrations (priorities in file order, tuning_profile) are no-ops for what is written.
        meta = ns.get("meta") if isinstance(ns.get("meta"), dict) else {}
        try:
            file_version = int(meta.get("version", 0))
        except (TypeError, ValueError):
            file_version = 0
        ns["meta"] = {**meta, "version": max(file_version, NET_SETTINGS_SCHEMA_PATTERNS)}
        data.setdefault("meta", {}).setdefault("filename", path.name)
        atomic_write_text(path, json.dumps(data, indent=2) + "\n", newline="\n")

    # -- footprints ----------------------------------------------------------------------------

    def footprints(self) -> list[Node]:
        return self._require().find_all("footprint")

    def footprint_node(self, ref: str) -> Node:
        for fp in self.footprints():
            if _property_value(fp, "Reference") == ref:
                return fp
        raise BoardWriterError(f"no footprint with reference {ref!r} on the board")

    def _node_by_uuid(self, uid: str) -> Node | None:
        root = self._require()
        for n in root.nodes:
            if _uuid_of(n) == uid:
                return n
        for n in root.walk():
            if n is not root and _uuid_of(n) == uid:
                return n
        return None

    def footprint_states(self) -> list[FootprintState]:
        """The planner's view of the board's footprints."""
        out: list[FootprintState] = []
        nets = self.nets
        for fp in self.footprints():
            atoms = fp.atoms
            pad_numbers: list[str] = []
            pad_nets: dict[str, str] = {}
            conflicting: set[str] = set()
            for pad in fp.find_all("pad"):
                number = pad.atoms[0].value if pad.atoms else ""
                pad_numbers.append(number)
                net = nets.name_of(pad.find("net"))
                if number in pad_nets and pad_nets[number] != net:
                    conflicting.add(number)  # thermal pads out of step: force a re-assignment
                pad_nets.setdefault(number, net)
            for number in conflicting:
                pad_nets[number] = CONFLICTING_NET
            flags = _attr_flags(fp)
            locked = _bare_flag(fp, "locked") or (fp.get("locked") == "yes")
            props = {}
            for p in fp.find_all("property"):
                pa = p.atoms
                if pa:
                    props[pa[0].value] = pa[1].value if len(pa) > 1 else ""
            out.append(
                FootprintState(
                    uuid=_uuid_of(fp),
                    ref=props.get("Reference", ""),
                    value=props.get("Value", ""),
                    fpid=atoms[0].value if atoms else "",
                    path=fp.get("path") or "",
                    locked=locked,
                    dnp="dnp" in flags,
                    exclude_from_bom="exclude_from_bom" in flags,
                    exclude_from_sim="exclude_from_sim" in flags,
                    exclude_from_pos_files="exclude_from_pos_files" in flags,
                    pad_numbers=tuple(pad_numbers),
                    pad_nets=pad_nets,
                    properties=props,
                    sheet_name=fp.get("sheetname") or "",
                    sheet_file=fp.get("sheetfile") or "",
                )
            )
        return out

    def _resolve(self, fpid: str) -> Node:
        if self.lib_resolver is None:
            raise BoardWriterError(f"no library resolver: cannot load {fpid!r}")
        try:
            lib = self.lib_resolver.footprint(fpid)
        except BoardWriterError:
            raise
        except Exception as exc:  # the libs component raises its own errors
            raise BoardWriterError(f"footprint {fpid!r} could not be resolved: {exc}") from exc
        node = getattr(lib, "node", None)
        if not isinstance(node, Node):
            raise BoardWriterError(f"library result for {fpid!r} carries no CST node")
        return node

    def instantiate(
        self,
        lib_node: Node,
        fpid: str,
        ref: str,
        value: str,
        *,
        at_mm: tuple[float, float] = (0.0, 0.0),
        rotation: float = 0.0,
        uuid: str | None = None,
        path: str = "",
        sheet_name: str = "",
        sheet_file: str = "",
        fields: Mapping[str, str] | None = None,
        dnp: bool = False,
        exclude_from_bom: bool = False,
        exclude_from_sim: bool = False,
        exclude_from_pos_files: bool | None = None,
    ) -> Node:
        """A board footprint node (detached) built from a library footprint document."""
        src = lib_node.copy()
        d = self.dialect
        header: list[Node] = []
        props: dict[str, Node] = {}
        links: dict[str, Node] = {}
        settings: list[Node] = []
        items: list[Node] = []
        trailer: list[Node] = []
        descr_tags: list[Node] = []
        for child in list(src.nodes):
            head = child.head
            if head in ("version", "generator", "generator_version", "layer", "uuid", "tstamp", "at"):
                continue
            if head in ("descr", "tags"):
                descr_tags.append(child)
            elif head == "property":
                pa = child.atoms
                if pa:
                    props[pa[0].value] = child
            elif head in _FP_LINK_HEADS:
                links[head] = child
            elif head == "fp_text" and child.atoms and child.atoms[0].value in ("reference", "value"):
                # legacy (< 8.0) library: `(fp_text reference "REF**" …)` becomes the property
                name = "Reference" if child.atoms[0].value == "reference" else "Value"
                props[name] = _property_from_fp_text(child, name)
            elif head in _FP_ITEM_HEADS:
                items.append(child)
            elif head in _FP_TRAILER_HEADS:
                trailer.append(child)
            else:
                settings.append(child)
        header.append(build("layer", "F.Cu"))
        header.append(build(d.uuid_token, uuid or new_uuid()))
        at_values: list[Any] = [_num(at_mm[0]), _num(at_mm[1])]
        if rotation:
            at_values.append(_normalize180(rotation))
        header.append(build("at", *at_values))
        # mandatory fields, then the library's other properties, then the netlist fields
        ordered_props: list[Node] = []
        texts = {"Reference": ref, "Value": value}
        for name in _MANDATORY_FIELDS:
            node = props.pop(name, None)
            if node is None:
                hide = name not in ("Reference", "Value")
                node = _make_property(
                    name, texts.get(name, ""), hide=hide, layer="F.SilkS" if name == "Reference" else "F.Fab"
                )
            if name in texts:
                _set_property_text(node, texts[name])
            ordered_props.append(node)
        ordered_props.extend(props.values())
        for name, text in (fields or {}).items():
            if name in texts or name == "Footprint":
                continue
            existing = next((p for p in ordered_props if p.atoms and p.atoms[0].value == name), None)
            if existing is not None:
                _set_property_text(existing, text)
            else:
                ordered_props.append(_make_property(name, text, hide=True))
        link_nodes: list[Node] = [build("path", path or "/" + new_uuid())]
        link_nodes.append(build("sheetname", sheet_name))
        link_nodes.append(build("sheetfile", sheet_file))
        flags = self._attr_flags_for(
            [a.value for a in links["attr"].atoms] if "attr" in links else [],
            dnp=dnp,
            exclude_from_bom=exclude_from_bom,
            exclude_from_sim=exclude_from_sim,
            exclude_from_pos_files=exclude_from_pos_files,
        )
        if flags:
            link_nodes.append(build("attr", *(Atom.symbol(f) for f in flags)))
        for name, node in links.items():
            if name not in ("path", "sheetname", "sheetfile", "attr"):
                link_nodes.append(node)
        children: list[Atom | Node] = [Atom.symbol("footprint"), Atom.string(fpid)]
        children.extend(header)
        children.extend(descr_tags)
        children.extend(ordered_props)
        children.extend(link_nodes)
        children.extend(settings)
        children.extend(items)
        children.extend(trailer)
        fp = Node(children)
        _regenerate_uuids(fp)
        for pad in fp.find_all("pad"):
            if pad.find("uuid") is None and pad.find("tstamp") is None:
                pad.append(build(d.uuid_token, new_uuid()))
        _set_uuid(fp, uuid or _uuid_of(fp))
        if rotation:
            _rotate_items(fp, _normalize180(rotation))
        if d.fp_zones_in_board_frame:
            _bake_zone_frame(fp)
        fp.reformat()
        return fp

    def _attr_flags_for(
        self,
        current: Sequence[str],
        *,
        dnp: bool,
        exclude_from_bom: bool,
        exclude_from_sim: bool,
        exclude_from_pos_files: bool | None,
    ) -> list[str]:
        """`(attr …)` flags in KiCad's writer order: body flags kept, netlist-driven flags set."""
        driven = ("dnp", "exclude_from_bom", "exclude_from_sim", "exclude_from_pos_files")
        bodies = ("smd", "through_hole", "virtual", "board_only")
        keep = [f for f in current if f not in driven]
        pos = (
            "exclude_from_pos_files" in current if exclude_from_pos_files is None else exclude_from_pos_files
        )
        out: list[str] = [f for f in bodies if f in keep]
        if pos:
            out.append("exclude_from_pos_files")
        if exclude_from_bom:
            out.append("exclude_from_bom")
        if exclude_from_sim and self.dialect.has_exclude_from_sim:
            out.append("exclude_from_sim")
        out.extend(f for f in keep if f not in out and f not in bodies and f != "allow_soldermask_bridges")
        if dnp:
            out.append("dnp")
        if "allow_soldermask_bridges" in keep:
            out.append("allow_soldermask_bridges")
        return out

    def _insert_footprint(self, fp: Node) -> None:
        root = self._require()
        last = _last_index(root, ("footprint",))
        if last is None:
            last = _last_index(
                root, ("net", "setup", "layers", "property", "title_block", "paper", "general")
            )
        root.insert((last + 1) if last is not None else len(root.children), fp)
        self._dirty = True

    def _insert_graphic(self, node: Node) -> None:
        root = self._require()
        last = _last_index(
            root,
            (
                "gr_line",
                "gr_arc",
                "gr_circle",
                "gr_rect",
                "gr_poly",
                "gr_curve",
                "gr_text",
                "gr_text_box",
                "footprint",
            ),
        )
        if last is None:
            last = _last_index(root, ("net", "setup", "layers", "title_block", "paper", "general"))
        root.insert((last + 1) if last is not None else len(root.children), node)
        self._dirty = True

    def _insert_track(self, node: Node) -> None:
        root = self._require()
        last = _last_index(root, ("segment", "arc", "via"))
        if last is None:
            last = _last_index(
                root,
                (
                    "gr_line",
                    "gr_arc",
                    "gr_circle",
                    "gr_rect",
                    "gr_poly",
                    "gr_curve",
                    "gr_text",
                    "gr_text_box",
                    "footprint",
                    "net",
                    "setup",
                ),
            )
        root.insert((last + 1) if last is not None else len(root.children), node)
        self._dirty = True

    def _insert_zone(self, node: Node) -> None:
        root = self._require()
        last = _last_index(root, ("zone",))
        if last is not None:
            root.insert(last + 1, node)
        else:
            first_trailer = _first_index(root, ("group", "generated", "embedded_fonts", "embedded_files"))
            root.insert(first_trailer if first_trailer is not None else len(root.children), node)
        self._dirty = True

    def _set_pad_nets(
        self, fp: Node, nets_by_pad: Mapping[str, str], comp: SyncComponent | None
    ) -> list[tuple[str, str, str]]:
        """Assign nets to every pad by number; returns (pad, old, new) for changed pads."""
        changes: list[tuple[str, str, str]] = []
        nets = self.nets
        for pad in fp.find_all("pad"):
            number = pad.atoms[0].value if pad.atoms else ""
            if number not in nets_by_pad:
                continue
            new = nets_by_pad[number]
            old = nets.name_of(pad.find("net"))
            if old != new:
                nets.set_ref(pad, new, with_name=True, before=("pinfunction", "pintype", "uuid", "tstamp"))
                changes.append((number, old, new))
            if comp is not None:
                fn = comp.pin_functions.get(number)
                pt = comp.pin_types.get(number)
                if new and fn:
                    _set_scalar_before(pad, "pinfunction", fn, ("pintype", "uuid", "tstamp"))
                if new and pt:
                    _set_scalar_before(pad, "pintype", pt, ("uuid", "tstamp"))
        return changes

    # -- netlist sync --------------------------------------------------------------------------

    def sync(
        self,
        netlist: Netlist | SyncNetlist | str | Path,
        *,
        remove_orphans: bool = True,
        keep_positions: bool = True,
        netlist_text: str | None = None,
        update_fields: bool = True,
        match: str = "path",
    ) -> SyncReport:
        """KiCad's "Update PCB from Schematic" on the CST; see `kiwrap.formats.netlist_sync`
        (`match`: "path" = KiCad's uuid pairing, "reference" = the re-link-by-reference box)."""
        root = self._require()
        nl = netlist_from_any(netlist, text=netlist_text)
        plan = plan_sync(
            self.footprint_states(),
            nl,
            remove_orphans=remove_orphans,
            update_fields=update_fields,
            match=match,
        )
        report = SyncReport(
            unresolved=list(plan.unresolved), warnings=list(plan.warnings), net_renames=dict(plan.net_renames)
        )
        nets = self.nets
        by_uuid = {_uuid_of(fp): fp for fp in self.footprints()}

        # 1. updates (including swaps and renames)
        for upd in plan.update:
            fp = by_uuid[upd.uuid]
            if upd.fpid_change is not None:
                fp = self._swap_footprint(fp, upd, keep_positions=keep_positions)
                by_uuid[upd.uuid] = fp
                report.footprint_changes.append((upd.ref, upd.fpid_change[0], upd.fpid_change[1]))
                # every pad of the new body needs its net
                numbers = [p.atoms[0].value if p.atoms else "" for p in fp.find_all("pad")]
                pad_changes = self._set_pad_nets(
                    fp, pad_net_assignment(numbers, upd.component.pins), upd.component
                )
                report.updated.append(upd.ref)
            else:
                self._apply_update(fp, upd)
                pad_changes = self._set_pad_nets(fp, upd.pad_nets, upd.component) if upd.pad_nets else []
                if upd.changed:
                    report.updated.append(upd.ref)
            if upd.old_ref is not None:
                report.renamed[upd.old_ref] = upd.ref
            for number, old, new in pad_changes:
                report.pad_net_changes.append((upd.ref, number, old, new))
            if pad_changes:
                self._dirty = True

        # 2. removals
        for state in plan.remove:
            node = by_uuid.get(state.uuid)
            if node is not None:
                root.remove(node)
                report.removed.append(state.ref)
                self._dirty = True
        report.kept_locked = [s.ref for s in plan.kept_locked]

        # 3. additions (placed in a grid below everything else)
        new_nodes: list[tuple[SyncComponent, Node]] = []
        for comp in plan.add:
            assert comp.fpid is not None
            try:
                lib = self._resolve(comp.fpid)
            except BoardWriterError as exc:
                report.unresolved.append((comp.ref, comp.fpid, str(exc)))
                continue
            fp = self.instantiate(
                lib,
                comp.fpid,
                comp.ref,
                comp.value,
                path=comp.path,
                sheet_name=comp.sheet_name,
                sheet_file=comp.sheet_file,
                fields=comp.fields,
                dnp=comp.dnp,
                exclude_from_bom=comp.exclude_from_bom,
                exclude_from_sim=comp.exclude_from_sim,
                exclude_from_pos_files=True if comp.exclude_from_pos_files else None,
            )
            self._insert_footprint(fp)
            numbers = [p.atoms[0].value if p.atoms else "" for p in fp.find_all("pad")]
            for number, old, new in self._set_pad_nets(fp, pad_net_assignment(numbers, comp.pins), comp):
                report.pad_net_changes.append((comp.ref, number, old, new))
            present = set(numbers)
            for pin in comp.pins:
                if pin not in present:
                    report.warnings.append(f"{comp.ref}: no pad found for pin {pin} (footprint {comp.fpid})")
            new_nodes.append((comp, fp))
            report.added.append(comp.ref)
        if new_nodes:
            report.placed = self._spread(new_nodes)

        # 4. nets on tracks/vias/zones: renames, then the table
        if plan.net_renames:
            self._rename_item_nets(plan.net_renames)
        nets.rebuild_table(self._used_net_names())
        self._dirty = self._dirty or bool(report.changed)
        return report

    def _apply_update(self, fp: Node, upd: FootprintUpdate) -> None:
        if upd.old_ref is not None:
            p = _property_node(fp, "Reference")
            if p is not None:
                _set_property_text(p, upd.ref)
            self._dirty = True
        if upd.value is not None:
            p = _property_node(fp, "Value")
            if p is None:
                anchor = _first_index(fp, _FP_LINK_HEADS | _FP_ITEM_HEADS)
                fp.insert(
                    anchor if anchor is not None else len(fp.children),
                    _make_property("Value", upd.value, hide=False),
                )
            else:
                _set_property_text(p, upd.value)
            self._dirty = True
        if upd.path is not None:
            _set_link(fp, "path", upd.path)
            self._dirty = True
        if upd.sheet_name is not None:
            _set_link(fp, "sheetname", upd.sheet_name)
            self._dirty = True
        if upd.sheet_file is not None:
            _set_link(fp, "sheetfile", upd.sheet_file)
            self._dirty = True
        if any(
            v is not None
            for v in (upd.dnp, upd.exclude_from_bom, upd.exclude_from_sim, upd.exclude_from_pos_files)
        ):
            comp = upd.component
            flags = self._attr_flags_for(
                _attr_flags(fp),
                dnp=comp.dnp,
                exclude_from_bom=comp.exclude_from_bom,
                exclude_from_sim=comp.exclude_from_sim,
                exclude_from_pos_files=True if comp.exclude_from_pos_files else None,
            )
            first_item = _first_index(fp, _FP_ITEM_HEADS)
            _set_attr_flags(fp, flags, fp.children[first_item] if first_item is not None else None)  # type: ignore[arg-type]
            self._dirty = True
        for name, text in upd.fields.items():
            p = _property_node(fp, name)
            if p is None:
                anchor = _first_index(fp, _FP_LINK_HEADS | _FP_ITEM_HEADS)
                fp.insert(
                    anchor if anchor is not None else len(fp.children), _make_property(name, text, hide=True)
                )
            else:
                _set_property_text(p, text)
            self._dirty = True

    def _swap_footprint(self, old: Node, upd: FootprintUpdate, *, keep_positions: bool) -> Node:
        comp = upd.component
        assert upd.fpid_change is not None
        lib = self._resolve(upd.fpid_change[1])
        x, y, rot = footprint_placement(old)
        layer = old.get("layer") or "F.Cu"
        locked = _bare_flag(old, "locked") or old.get("locked") == "yes"
        fields = {
            **{k: v for k, v in _fp_properties(old).items() if k not in _MANDATORY_FIELDS},
            **comp.fields,
        }
        fp = self.instantiate(
            lib,
            upd.fpid_change[1],
            upd.ref,
            comp.value,
            at_mm=(x, y) if keep_positions else (0.0, 0.0),
            uuid=upd.uuid,
            path=comp.path or (old.get("path") or ""),
            sheet_name=comp.sheet_name,
            sheet_file=comp.sheet_file,
            fields=fields,
            dnp=comp.dnp,
            exclude_from_bom=comp.exclude_from_bom,
            exclude_from_sim=comp.exclude_from_sim,
            exclude_from_pos_files=True if comp.exclude_from_pos_files else None,
        )
        board_frame = self.dialect.fp_zones_in_board_frame
        if board_frame:
            _unbake_zone_frame(fp)
        if keep_positions and layer.startswith("B."):
            flip_footprint(fp)
        if keep_positions and rot:
            set_footprint_placement(fp, x, y, _normalize180(rot))
            _rotate_items(fp, _normalize180(rot))
        if board_frame:
            _bake_zone_frame(fp)
        if locked:
            self._set_locked(fp, True)
        root = self._require()
        root.replace(old, fp)
        fp.reformat()
        self._dirty = True
        return fp

    def _spread(self, new_nodes: list[tuple[SyncComponent, Node]]) -> dict[str, tuple[float, float]]:
        """Place new footprints in rows below the outline (or below everything on the board)."""
        root = self._require()
        model = BoardFile.from_node(root)
        new_uuids = {_uuid_of(fp) for _, fp in new_nodes}
        anchors: list[BBox] = []
        outline = model.outline_bbox()
        if outline is not None:
            anchors.append(outline)
        for fp in model.footprints:
            if fp.uuid not in new_uuids:
                anchors.append(fp.bbox())
        for g in model.graphics:
            b = BBox.of(g.points)
            if b is not None:
                anchors.append(b)
        if anchors:
            x0 = min(b.min_x for b in anchors)
            y0 = max(b.max_y for b in anchors) + GRID_MARGIN_MM
            row_w = max(GRID_MIN_ROW_MM, max(b.max_x for b in anchors) - x0)
        else:
            x0, y0, row_w = 0.0, 0.0, GRID_MIN_ROW_MM
        by_uuid = {fp.uuid: fp for fp in model.footprints}
        placed: dict[str, tuple[float, float]] = {}
        x, y, row_h = x0, y0, 0.0
        for comp, node in new_nodes:
            m = by_uuid.get(_uuid_of(node))
            if m is None:
                w = h = 2.0
                ox = oy = 0.0
            else:
                b = m.bbox()
                w, h = max(b.width, 0.5), max(b.height, 0.5)
                ox, oy = b.min_x - m.position.x, b.min_y - m.position.y  # bbox offset from the anchor
            if x > x0 and x + w > x0 + row_w:
                x = x0
                y += row_h + GRID_GAP_MM
                row_h = 0.0
            px, py = x - ox, y - oy
            board_frame = self.dialect.fp_zones_in_board_frame
            if board_frame:
                _unbake_zone_frame(node)
            set_footprint_placement(node, px, py, footprint_placement(node)[2])
            if board_frame:
                _bake_zone_frame(node)
            placed[comp.ref] = (_num(px), _num(py))
            x += w + GRID_GAP_MM
            row_h = max(row_h, h)
        self._dirty = True
        return placed

    def _used_net_names(self) -> set[str]:
        root = self._require()
        nets = self.nets
        used: set[str] = set()
        for n in root.walk():
            if n.head in ("pad", "segment", "arc", "via", "zone") and n is not root:
                name = nets.name_of(n.find("net"))
                if name:
                    used.add(name)
        return used

    def _rename_item_nets(self, renames: Mapping[str, str]) -> None:
        root = self._require()
        nets = self.nets
        for n in root.nodes:
            if n.head in ("segment", "arc", "via", "zone"):
                name = nets.name_of(n.find("net"))
                if name in renames:
                    nets.set_ref(n, renames[name], with_name=False, before=("uuid", "tstamp"))
                    self._dirty = True

    # -- placement -----------------------------------------------------------------------------

    def place(
        self,
        ref: str,
        at_mm: tuple[float, float],
        rotation: float | None = None,
        side: str | None = None,
        locked: bool | None = None,
    ) -> None:
        fp = self.footprint_node(ref)
        board_frame = self.dialect.fp_zones_in_board_frame
        if board_frame:
            _unbake_zone_frame(fp)
        current_rot = footprint_placement(fp)[2]
        current_side = "back" if (fp.get("layer") or "F.Cu").startswith("B.") else "front"
        if side is not None:
            side = side.lower()
            if side not in ("front", "back", "top", "bottom"):
                raise BoardWriterError("side must be front|back (top|bottom)")
            side = "front" if side in ("front", "top") else "back"
            if side != current_side:
                flip_footprint(fp)
                current_rot = footprint_placement(fp)[2]
        new_rot = _normalize180(rotation) if rotation is not None else current_rot
        set_footprint_placement(fp, at_mm[0], at_mm[1], new_rot)
        delta = new_rot - current_rot
        if delta:
            _rotate_items(fp, delta)
        if board_frame:
            _bake_zone_frame(fp)
        if locked is not None:
            self._set_locked(fp, locked)
        self._dirty = True

    def _set_locked(self, fp: Node, locked: bool) -> None:
        existing_flag = next((a for a in fp.atoms if a.kind == "symbol" and a.value == "locked"), None)
        existing_list = fp.find("locked")
        if not locked:
            if existing_flag is not None:
                fp.remove(existing_flag)
            if existing_list is not None:
                fp.remove(existing_list)
            return
        if existing_flag is not None or existing_list is not None:
            return
        if self.dialect.locked_as_list:
            layer = fp.find("layer")
            idx = fp.index(layer) + 1 if layer is not None else 2
            fp.insert(idx, build("locked", Atom.symbol("yes")))
        else:
            fp.insert(2, Atom.symbol("locked"))

    # -- tracks, vias, zones -------------------------------------------------------------------

    def add_track(
        self, pts: Sequence[tuple[float, float]], width_mm: float, layer: str, net: str
    ) -> list[str]:
        if len(pts) < 2:
            raise BoardWriterError("a track needs at least two points")
        if layer not in self._copper:
            raise BoardWriterError(f"{layer!r} is not a copper layer of this board ({self._copper})")
        uuids: list[str] = []
        for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
            uid = new_uuid()
            seg = build(
                "segment",
                build("start", _num(x1), _num(y1)),
                build("end", _num(x2), _num(y2)),
                build("width", _num(width_mm)),
                build("layer", layer),
            )
            self.nets.set_ref(seg, net, with_name=False, before=())
            seg.append(build(self.dialect.uuid_token, uid))
            self._insert_track(seg)
            uuids.append(uid)
        return uuids

    def add_via(
        self,
        at_mm: tuple[float, float],
        size_mm: float,
        drill_mm: float,
        net: str,
        layers: tuple[str, str] = ("F.Cu", "B.Cu"),
    ) -> str:
        for layer in layers:
            if layer not in self._copper:
                raise BoardWriterError(f"{layer!r} is not a copper layer of this board ({self._copper})")
        uid = new_uuid()
        via = build(
            "via",
            build("at", _num(at_mm[0]), _num(at_mm[1])),
            build("size", _num(size_mm)),
            build("drill", _num(drill_mm)),
            build("layers", layers[0], layers[1]),
        )
        self.nets.set_ref(via, net, with_name=False, before=())
        via.append(build(self.dialect.uuid_token, uid))
        self._insert_track(via)
        return uid

    def add_zone(
        self,
        net: str,
        layers: str | Sequence[str],
        polygon_mm: Sequence[tuple[float, float]],
        priority: int = 0,
        *,
        name: str | None = None,
        clearance_mm: float = 0.5,
        min_thickness_mm: float = 0.25,
        thermal_gap_mm: float = 0.5,
        thermal_bridge_mm: float = 0.5,
        pad_connection: str = "thermal",
        keepout: Mapping[str, bool] | None = None,
    ) -> str:
        """A filled copper zone (or a rule area with `keepout={"tracks": True, …}`); returns its uuid.

        `pad_connection`: thermal (default) | solid | none | thru_hole_only."""
        if len(polygon_mm) < 3:
            raise BoardWriterError("a zone polygon needs at least 3 points")
        layer_list = [layers] if isinstance(layers, str) else list(layers)
        for layer in layer_list:
            if layer not in self._copper:
                raise BoardWriterError(f"{layer!r} is not a copper layer of this board ({self._copper})")
        uid = new_uuid()
        zone = build("zone")
        if not keepout:
            self.nets.set_ref(zone, net, with_name=False, before=())
        elif not self.dialect.net_names_only:
            zone.append(build("net", 0))
            zone.append(build("net_name", ""))
        if len(layer_list) == 1:
            zone.append(build("layer", layer_list[0]))
        else:
            zone.append(build("layers", *layer_list))
        zone.append(build(self.dialect.uuid_token, uid))
        if name:
            zone.append(build("name", name))
        zone.append(build("hatch", Atom.symbol("edge"), 0.5))
        if priority:
            zone.append(build("priority", int(priority)))
        connect = build("connect_pads", build("clearance", _num(clearance_mm)))
        mode = {"thermal": None, "solid": "yes", "none": "no", "thru_hole_only": "thru_hole_only"}.get(
            pad_connection
        )
        if pad_connection not in ("thermal", "solid", "none", "thru_hole_only"):
            raise BoardWriterError("pad_connection must be thermal|solid|none|thru_hole_only")
        if mode is not None:
            connect.insert(1, Atom.symbol(mode))
        zone.append(connect)
        zone.append(build("min_thickness", _num(min_thickness_mm)))
        zone.append(build("filled_areas_thickness", Atom.symbol("no")))
        if keepout:
            ko = build("keepout")
            for key in ("tracks", "vias", "pads", "copperpour", "footprints"):
                ko.append(build(key, Atom.symbol("not_allowed" if keepout.get(key) else "allowed")))
            zone.append(ko)
        fill = build("fill")
        if not keepout:
            fill.append(Atom.symbol("yes"))
        fill.append(build("thermal_gap", _num(thermal_gap_mm)))
        fill.append(build("thermal_bridge_width", _num(thermal_bridge_mm)))
        zone.append(fill)
        pts = build("pts", *(build("xy", _num(x), _num(y)) for x, y in polygon_mm))
        zone.append(build("polygon", pts))
        self._insert_zone(zone)
        return uid

    def delete(self, uuid: str) -> None:
        node = self._node_by_uuid(uuid)
        if node is None or node.parent is None:
            raise BoardWriterError(f"no item with uuid {uuid!r}")
        node.parent.remove(node)
        self._dirty = True

    def delete_node(self, node: Node) -> None:
        """Remove one CST node (an item without a uuid that `delete` cannot address)."""
        if node.parent is None:
            raise BoardWriterError("the node is not attached to the board")
        node.parent.remove(node)
        self._dirty = True

    def mark_dirty(self) -> None:
        """Record an edit made directly on `root` so `save()` writes it."""
        self._dirty = True

    def set_locked(self, uuid: str, locked: bool) -> None:
        """Set the lock flag of a track / arc / via / footprint (KeyError for an unknown uuid).

        KiCad's Specctra session import deletes every UNLOCKED track and via and keeps the
        locked ones (docs/VERIFICATION.md claim 7), so "keep this copper through a re-route"
        means locking it. The flag takes the dialect's form: `(locked yes)` after the layer /
        net on 9+ boards, the bare `locked` symbol on older ones."""
        node = self._node_by_uuid(uuid)
        if node is None:
            raise KeyError(uuid)
        if node.head == "footprint":
            self._set_locked(node, locked)
            self._dirty = True
            return
        existing_flag = next((a for a in node.atoms if a.kind == "symbol" and a.value == "locked"), None)
        existing_list = node.find("locked")
        if not locked:
            if existing_flag is not None:
                node.remove(existing_flag)
            if existing_list is not None:
                node.remove(existing_list)
        elif existing_flag is None and existing_list is None:
            if self.dialect.locked_as_list:
                anchor = node.find("net") or node.find(self.dialect.uuid_token) or node.find("tstamp")
                idx = node.index(anchor) if anchor is not None else len(node.children)
                node.insert(idx, build("locked", Atom.symbol("yes")))
            else:
                node.insert(1, Atom.symbol("locked"))
        self._dirty = True


def _fp_properties(fp: Node) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in fp.find_all("property"):
        pa = p.atoms
        if pa:
            out[pa[0].value] = pa[1].value if len(pa) > 1 else ""
    return out


def _set_link(fp: Node, head: str, value: str) -> None:
    existing = fp.find(head)
    if existing is not None:
        fp.set(head, value)
        return
    order = ("path", "sheetname", "sheetfile")
    idx: int | None = None
    for later in order[order.index(head) + 1 :] if head in order else ():
        idx = _first_index(fp, (later,))
        if idx is not None:
            break
    if idx is None:
        idx = _first_index(fp, ("attr",) + tuple(_FP_ITEM_HEADS))
    fp.insert(idx if idx is not None else len(fp.children), build(head, value))


def _set_scalar_before(node: Node, head: str, value: str, before: Iterable[str]) -> None:
    existing = node.find(head)
    if existing is not None:
        node.set(head, value)
        return
    idx = _first_index(node, before)
    node.insert(idx if idx is not None else len(node.children), build(head, value))


def _escape_pattern(net: str) -> str:
    """A net name as a KiCad netclass pattern: `*`/`?` wildcards are literal for exact names."""
    return net
