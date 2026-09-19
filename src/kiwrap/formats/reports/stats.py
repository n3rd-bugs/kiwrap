"""`kicad-cli pcb export stats --format json` parser -> `BoardStats`.

JSON shape, verified live on 10.0.6 and the 10.99 nightly (build 4083) against the ecc83 demo
(scratchpad structured/probe1.py; both outputs are identical apart from `metadata.generator`):

    {"metadata": {"date", "generator", "project", "board_name"},
     "board": {"has_outline": true, "width": "52.0700 mm", "height": "46.3550 mm",
               "area": "2413.705 mm²", "front_component_density": "51.80", ...,
               "min_track_clearance": "0.8510 mm", "board_thickness": "1.6000 mm", ...},
     "pads": {"through_hole": 33, "smd": 0, "connector": 0, "npth": 0, "castellated": 0, "press_fit": 0},
     "vias": {"through": 0, "blind": 0, "buried": 0, "micro": 0},
     "components": {"tht": {"front", "back", "total"}, "smd": {...}, "unspecified": {...}, "total": {...}},
     "drill_holes": [{"count": 10, "shape": "Round", "x_size": "0.8000 mm", "y_size": "0.8000 mm",
                      "plated": true, "source": "Pad", "start_layer": "top_cu", "stop_layer": "bottom_cu"}]}

Quirks this module normalises (docs/ARCHITECTURE.md §2 "unit strings, mojibake"):
- every dimension is a STRING with a unit suffix ("52.0700 mm", "2413.705 mm²", "1.2 in²"), and
  the density fields are unit-less percentage strings ("51.80"); numbers come out as floats in
  millimetres / mm² / percent regardless of `--units`;
- the superscript in "mm²" is written as UTF-8 (C2 B2). Read through a single-byte codec it
  shows up as "mmÂ²"; some builds write "mm^2" / "mm2". All spellings are accepted;
- `x_size`/`y_size` of drill holes and `board_thickness` follow the same rule;
- layer names in `drill_holes` are the board's USER names ("top_cu" on the demo, "F.Cu"
  otherwise); they are passed through untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kiwrap.formats.reports._common import load_json

__all__ = [
    "BoardStats",
    "DrillHole",
    "PadCounts",
    "SideCounts",
    "ViaCounts",
    "parse_quantity",
    "parse_stats_json",
]

# millimetres (or mm² for area units) per one unit; keys are normalised unit spellings
_LENGTH_TO_MM: dict[str, float] = {
    "mm": 1.0,
    "in": 25.4,
    "inch": 25.4,
    "mil": 0.0254,
    "mils": 0.0254,
    "thou": 0.0254,
}
_AREA_TO_MM2: dict[str, float] = {
    "mm2": 1.0,
    "in2": 25.4 * 25.4,
    "inch2": 25.4 * 25.4,
    "mil2": 0.0254 * 0.0254,
}
_QUANTITY_RE = re.compile(r"^\s*(?P<num>[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*(?P<unit>.*?)\s*$")


def _normalise_unit(unit: str) -> str:
    """ "mm²" / "mmÂ²" / "mm^2" / "mm 2" / "MM2" -> "mm2"; "in" -> "in"; "" -> ""."""
    u = unit.strip().replace("Â", "")  # UTF-8 "²" (C2 B2) read as Latin-1/cp1252 leaves a stray "Â"
    u = u.lower().replace("²", "2").replace("^2", "2").replace("³", "3")
    u = re.sub(r"[\s_]+", "", u)
    if u.startswith("sq") and len(u) > 2:  # "sq mm" -> "mm2"
        u = u[2:] + "2"
    return u


def parse_quantity(value: Any) -> tuple[float, str]:
    """Return (magnitude in mm / mm² / raw, normalised unit) for a stats value.

    "52.0700 mm" -> (52.07, "mm"); "2413.705 mm²" -> (2413.705, "mm2"); "1.0 in" -> (25.4, "mm");
    "3.7 in²" -> (2387.092, "mm2"); "51.80" -> (51.8, ""); 12 -> (12.0, ""). Unknown units are kept
    as written (magnitude untouched). Raises ValueError when no number leads the string.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a quantity: {value!r}")
    if isinstance(value, (int, float)):
        return float(value), ""
    m = _QUANTITY_RE.match(str(value))
    if not m:
        raise ValueError(f"not a quantity: {value!r}")
    magnitude = float(m.group("num").replace(",", "."))
    unit = _normalise_unit(m.group("unit"))
    if unit in _LENGTH_TO_MM:
        return magnitude * _LENGTH_TO_MM[unit], "mm"
    if unit in _AREA_TO_MM2:
        return magnitude * _AREA_TO_MM2[unit], "mm2"
    return magnitude, unit


def _mm(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return parse_quantity(value)[0]
    except ValueError:
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class SideCounts:
    front: int = 0
    back: int = 0
    total: int = 0


@dataclass(frozen=True)
class PadCounts:
    through_hole: int = 0
    smd: int = 0
    connector: int = 0
    npth: int = 0
    castellated: int = 0
    press_fit: int = 0

    @property
    def total(self) -> int:
        return self.through_hole + self.smd + self.connector + self.npth + self.castellated + self.press_fit


@dataclass(frozen=True)
class ViaCounts:
    through: int = 0
    blind: int = 0
    buried: int = 0
    micro: int = 0

    @property
    def total(self) -> int:
        return self.through + self.blind + self.buried + self.micro


@dataclass(frozen=True)
class DrillHole:
    """One row of `drill_holes`: `count` holes of this size/shape/plating between two layers."""

    count: int
    shape: str  # "Round" | "Slot" as KiCad spells it
    x_size_mm: float | None
    y_size_mm: float | None
    plated: bool
    source: str  # "Pad" | "Via"
    start_layer: str
    stop_layer: str


@dataclass(frozen=True)
class BoardStats:
    """Normalised `pcb export stats` result. Every dimension is in mm / mm²; densities in percent."""

    source: str
    kicad_version: str
    project: str
    board_name: str
    date: str
    has_outline: bool
    width_mm: float | None
    height_mm: float | None
    area_mm2: float | None
    board_thickness_mm: float | None
    front_copper_area_mm2: float | None
    back_copper_area_mm2: float | None
    front_footprint_area_mm2: float | None
    back_footprint_area_mm2: float | None
    front_component_density_pct: float | None
    back_component_density_pct: float | None
    min_track_clearance_mm: float | None
    min_track_width_mm: float | None
    min_drill_diameter_mm: float | None
    pads: PadCounts
    vias: ViaCounts
    components: dict[str, SideCounts]  # "tht" | "smd" | "unspecified" | "total" (+ any KiCad adds)
    drill_holes: tuple[DrillHole, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def component_count(self) -> int:
        return self.components.get("total", SideCounts()).total

    @property
    def copper_layers_used(self) -> tuple[str, ...]:
        """Layer user-names the drill table spans (start/stop of every row), in first-seen order."""
        seen: list[str] = []
        for h in self.drill_holes:
            for name in (h.start_layer, h.stop_layer):
                if name and name not in seen:
                    seen.append(name)
        return tuple(seen)


def _side_counts(raw: Any) -> SideCounts:
    if not isinstance(raw, dict):
        return SideCounts()
    return SideCounts(front=_int(raw.get("front")), back=_int(raw.get("back")), total=_int(raw.get("total")))


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    """The JSON object under `key`, or an empty dict when it is absent or not an object."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def parse_stats_json(text: str, *, source: str = "", kicad_version: str = "") -> BoardStats:
    """Parse the JSON `pcb export stats --format json` writes (10.0+). Values are normalised as
    described in the module docstring; unknown keys survive in `raw`."""
    data = load_json(text)
    meta = _section(data, "metadata")
    board = _section(data, "board")
    pads_raw = _section(data, "pads")
    vias_raw = _section(data, "vias")
    comps_raw = _section(data, "components")
    holes: list[DrillHole] = []
    for h in data.get("drill_holes", ()) or ():
        if not isinstance(h, dict):
            continue
        holes.append(
            DrillHole(
                count=_int(h.get("count")),
                shape=str(h.get("shape", "")),
                x_size_mm=_mm(h.get("x_size")),
                y_size_mm=_mm(h.get("y_size")),
                plated=bool(h.get("plated", False)),
                source=str(h.get("source", "")),
                start_layer=str(h.get("start_layer", "")),
                stop_layer=str(h.get("stop_layer", "")),
            )
        )
    return BoardStats(
        source=source or str(meta.get("board_name", "")),
        kicad_version=kicad_version or str(meta.get("generator", "")),
        project=str(meta.get("project", "")),
        board_name=str(meta.get("board_name", "")),
        date=str(meta.get("date", "")),
        has_outline=bool(board.get("has_outline", False)),
        width_mm=_mm(board.get("width")),
        height_mm=_mm(board.get("height")),
        area_mm2=_mm(board.get("area")),
        board_thickness_mm=_mm(board.get("board_thickness")),
        front_copper_area_mm2=_mm(board.get("front_copper_area")),
        back_copper_area_mm2=_mm(board.get("back_copper_area")),
        front_footprint_area_mm2=_mm(board.get("front_footprint_area")),
        back_footprint_area_mm2=_mm(board.get("back_footprint_area")),
        front_component_density_pct=_mm(board.get("front_component_density")),
        back_component_density_pct=_mm(board.get("back_component_density")),
        min_track_clearance_mm=_mm(board.get("min_track_clearance")),
        min_track_width_mm=_mm(board.get("min_track_width")),
        min_drill_diameter_mm=_mm(board.get("min_drill_diameter")),
        pads=PadCounts(
            through_hole=_int(pads_raw.get("through_hole")),
            smd=_int(pads_raw.get("smd")),
            connector=_int(pads_raw.get("connector")),
            npth=_int(pads_raw.get("npth")),
            castellated=_int(pads_raw.get("castellated")),
            press_fit=_int(pads_raw.get("press_fit")),
        ),
        vias=ViaCounts(
            through=_int(vias_raw.get("through")),
            blind=_int(vias_raw.get("blind")),
            buried=_int(vias_raw.get("buried")),
            micro=_int(vias_raw.get("micro")),
        ),
        components={str(k): _side_counts(v) for k, v in comps_raw.items()},
        drill_holes=tuple(holes),
        raw=data,
    )
