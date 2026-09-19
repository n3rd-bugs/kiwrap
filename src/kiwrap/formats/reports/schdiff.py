"""`kicad-cli sch diff --format json` parser -> `SchDiff` (KiCad 11 / 10.99 nightly).

Wire format (KiCad MR !2620 "Add native diff/merge for KiCad files", master
common/diff_merge/kicad_diff_types.cpp `DOCUMENT_DIFF::ToJson`; verified live on nightly build
4083 with the ecc83 demo, scratchpad structured/probe2.py):

    {"path": "<comparison file>", "docType": "kicad_sch",
     "changes": [{"id": "/<sheet uuid>/<item uuid>", "typeName": "SCH_SYMBOL", "kind": "modified",
                  "refdes": "R1",                       # symbols only
                  "bbox": [x, y, w, h],                 # schematic internal units
                  "properties": [{"name": "Value",
                                  "before": {"type": "string", "v": "1.5K"},
                                  "after":  {"type": "string", "v": "2.2K"}}],
                  "children": [ ...same shape... ]}]}

`kind` is one of KINDS; `before`/`after` values carry a `type` tag from TYPE_TAGS (`"none"` when
the side does not exist, i.e. for added/removed items) and `v`: bool / int / int64 / double /
string / kiid string / `[x, y]` (vec2) / `[x, y, w, h]` (box2) / `[r, g, b, a]` (color) /
layer int / enum int with a `label` ("Solid") / polyset (list of polygons of `[x, y]`).

Units: KiCad's schematic internal unit is 1/10000 mm (`schIUScale`, base_units.h), so a
`Position X` of 1549400 is 154.94 mm and a removed wire's `End X` 1981200 is 198.12 mm — the
`--format text` output of the same run prints exactly those millimetre values. Property names
that carry a distance or coordinate (`… X`, `… Y`, `Length`, `Line Width`, `Text Size`, ...) are
converted to millimetres into `PropertyDelta.before_mm` / `after_mm`; everything else is kept
verbatim. Exit code: 0 identical, 5 (`ERR_RC_VIOLATIONS`) when changes exist, 3 when a file does
not load — the CLI layer passes the exit status in as `exit_code`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kiwrap.formats.reports._common import load_json

__all__ = [
    "KINDS",
    "SCH_IU_PER_MM",
    "TYPE_TAGS",
    "BBox",
    "DiffValue",
    "ItemChange",
    "PropertyDelta",
    "SchDiff",
    "is_distance_property",
    "parse_sch_diff_json",
]

SCH_IU_PER_MM = 10_000.0
KINDS: tuple[str, ...] = ("added", "removed", "modified", "collision", "duplicate_uuid")
TYPE_TAGS: tuple[str, ...] = (
    "none",
    "bool",
    "int",
    "int64",
    "double",
    "string",
    "kiid",
    "vec2",
    "box2",
    "color",
    "layer",
    "enum",
    "polyset",
)

# Property names whose numeric value is a length/coordinate in internal units (the text formatter
# prints them in mm through the property manager's DISTANCE/COORD display hints; the JSON carries
# no hint, so the names are matched instead — every name below was seen in a live diff or is a
# KiCad PROPERTY name registered with PT_SIZE/PT_COORD in eeschema).
_DISTANCE_RE = re.compile(
    r"(?:^|\s)(?:X|Y|Length|Width|Height|Thickness|Radius|Diameter|Size|Offset|Pitch|Spacing)$"
    r"|^(?:Line Width|Text Size|Text Thickness|Pin Length|Start X|Start Y|End X|End Y|Position X|Position Y|"
    r"Center X|Center Y|Width|Height)$",
    re.IGNORECASE,
)


def is_distance_property(name: str) -> bool:
    return bool(_DISTANCE_RE.search(name.strip()))


@dataclass(frozen=True)
class BBox:
    """Axis-aligned box in millimetres (schematic coordinates: y grows downwards)."""

    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float

    @property
    def center(self) -> tuple[float, float]:
        return (round(self.x_mm + self.w_mm / 2, 6), round(self.y_mm + self.h_mm / 2, 6))


@dataclass(frozen=True)
class DiffValue:
    """One side of a property delta. `value` is the JSON `v` as written (internal units for
    coordinates); `label` is the enum display label when `type == "enum"`."""

    type: str
    value: Any = None
    label: str | None = None

    @property
    def exists(self) -> bool:
        return self.type != "none"

    def as_mm(self, iu_per_mm: float = SCH_IU_PER_MM) -> float | None:
        if self.type in ("int", "int64", "double") and isinstance(self.value, (int, float)):
            return round(float(self.value) / iu_per_mm, 6)
        return None

    def display(self) -> str:
        if self.type == "none":
            return "<none>"
        if self.type == "enum" and self.label:
            return self.label
        if self.type == "bool":
            return "true" if self.value else "false"
        return str(self.value)


@dataclass(frozen=True)
class PropertyDelta:
    name: str
    before: DiffValue
    after: DiffValue
    before_mm: float | None = None  # set when `is_distance_property(name)`
    after_mm: float | None = None

    @property
    def is_distance(self) -> bool:
        return is_distance_property(self.name)

    def describe(self) -> str:
        if self.is_distance and (self.before_mm is not None or self.after_mm is not None):
            b = "<none>" if self.before_mm is None else f"{self.before_mm:g} mm"
            a = "<none>" if self.after_mm is None else f"{self.after_mm:g} mm"
            return f"{self.name}: {b} -> {a}"
        return f"{self.name}: {self.before.display()} -> {self.after.display()}"


@dataclass(frozen=True)
class ItemChange:
    id: str  # KIID_PATH: "/<sheet uuid>/<item uuid>"
    type_name: str  # "SCH_SYMBOL", "SCH_LINE", "SCH_LABEL", ...
    kind: str  # one of KINDS
    refdes: str | None
    properties: tuple[PropertyDelta, ...]
    bbox: BBox | None
    children: tuple[ItemChange, ...] = ()

    @property
    def uuid(self) -> str:
        return self.id.rsplit("/", 1)[-1]

    @property
    def sheet_path(self) -> str:
        """The KIID path of the containing sheet ("/<root uuid>" for the top sheet)."""
        head, _, _ = self.id.rpartition("/")
        return head or "/"

    def property(self, name: str) -> PropertyDelta | None:
        return next((p for p in self.properties if p.name == name), None)

    def describe(self) -> str:
        who = f"{self.type_name} {self.refdes}" if self.refdes else f"{self.type_name} {self.uuid}"
        return f"{self.kind} {who}"


@dataclass(frozen=True)
class SchDiff:
    reference: str  # file A as given to the CLI (the JSON only names the comparison file)
    comparison: str
    doc_type: str
    changes: tuple[ItemChange, ...]
    exit_code: int | None = None  # 0 identical, 5 differences (from the CLI run when known)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def identical(self) -> bool:
        return not self.changes

    def by_kind(self, kind: str) -> tuple[ItemChange, ...]:
        return tuple(c for c in self.changes if c.kind == kind)

    @property
    def added(self) -> tuple[ItemChange, ...]:
        return self.by_kind("added")

    @property
    def removed(self) -> tuple[ItemChange, ...]:
        return self.by_kind("removed")

    @property
    def modified(self) -> tuple[ItemChange, ...]:
        return self.by_kind("modified")

    @property
    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in KINDS}
        for c in self.changes:
            out[c.kind] = out.get(c.kind, 0) + 1
        return out

    def summary(self) -> str:
        """One line per change, the way `--format text` prints it."""
        lines = [f"{len(self.changes)} change(s)"]
        for c in self.changes:
            lines.append("  " + c.describe())
            lines.extend("    " + p.describe() for p in c.properties)
        return "\n".join(lines)


def _value(raw: Any) -> DiffValue:
    if not isinstance(raw, dict):
        return DiffValue(type="none")
    t = str(raw.get("type", "none"))
    return DiffValue(type=t, value=raw.get("v"), label=raw.get("label"))


def _delta(raw: dict[str, Any], iu_per_mm: float) -> PropertyDelta:
    name = str(raw.get("name", ""))
    before, after = _value(raw.get("before")), _value(raw.get("after"))
    b_mm = a_mm = None
    if is_distance_property(name):
        b_mm, a_mm = before.as_mm(iu_per_mm), after.as_mm(iu_per_mm)
    return PropertyDelta(name=name, before=before, after=after, before_mm=b_mm, after_mm=a_mm)


def _bbox(raw: Any, iu_per_mm: float) -> BBox | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x, y, w, h = (float(v) / iu_per_mm for v in raw)
    except (TypeError, ValueError):
        return None
    return BBox(round(x, 6), round(y, 6), round(w, 6), round(h, 6))


def _change(raw: dict[str, Any], iu_per_mm: float) -> ItemChange:
    return ItemChange(
        id=str(raw.get("id", "")),
        type_name=str(raw.get("typeName", "")),
        kind=str(raw.get("kind", "unknown")),
        refdes=str(raw["refdes"]) if raw.get("refdes") is not None else None,
        properties=tuple(
            _delta(p, iu_per_mm) for p in raw.get("properties", ()) or () if isinstance(p, dict)
        ),
        bbox=_bbox(raw.get("bbox"), iu_per_mm),
        children=tuple(_change(c, iu_per_mm) for c in raw.get("children", ()) or () if isinstance(c, dict)),
    )


def parse_sch_diff_json(
    text: str,
    *,
    reference: str = "",
    comparison: str = "",
    exit_code: int | None = None,
    iu_per_mm: float = SCH_IU_PER_MM,
) -> SchDiff:
    """Parse the JSON document; `reference` is file A (not recorded in the JSON), `exit_code` the
    CLI status if known. `iu_per_mm` exists so the same parser can read a `pcb diff` document
    (1e6 IU per mm) — the shape is identical."""
    data = load_json(text)
    changes = tuple(_change(c, iu_per_mm) for c in data.get("changes", ()) or () if isinstance(c, dict))
    return SchDiff(
        reference=reference,
        comparison=comparison or str(data.get("path", "")),
        doc_type=str(data.get("docType", "")),
        changes=changes,
        exit_code=exit_code,
        raw=data,
    )
