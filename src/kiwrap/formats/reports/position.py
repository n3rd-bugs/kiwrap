"""`kicad-cli pcb export pos` parser -> `PositionFile` (footprint placement rows).

Formats KiCad writes (pcbnew/exporters/place_file_exporter.cpp; verified on 7.0.6 - 10.99 with
the ecc83 / pic_programmer demos, scratchpad structured/out/*/pos*.csv):

    csv:    Ref,Val,Package,PosX,PosY,Rot,Side
            "C1","10uF","CP_Radial_D10.0mm_P5.00mm",141.605000,-99.695000,90.000000,top
    ascii:  ### Footprint positions - created on ... ###
            ### Printed by KiCad version ...
            ## Unit = mm, Angle = deg.
            ## Side : top
            # Ref     Val       Package                     PosX       PosY       Rot  Side
            C1        10uF      CP_Radial_D10.0mm_P5.00mm   141.6050   -99.6950   90.0000  top
            ## End
    gerber: an X2 gerber (not parsed here)

Conventions and quirks:
- `PosY` is NEGATED relative to the board file (y up, "Cartesian" as fab houses expect): a
  footprint at `(at 141.605 99.695 90)` is written `141.605000,-99.695000`. `--use-drill-file-origin`
  makes both axes relative to the aux origin. This model returns KiCad board coordinates
  (`x_mm`/`y_mm`, y down) given the same `aux_origin_mm` the export used (0,0 unless the caller
  passed the flag), and keeps the file values in `x_raw`/`y_raw`;
- `--units in` writes inches; the caller passes `units` because the CSV has no unit header (the
  ascii header line `## Unit = mm` is honoured when present);
- `--bottom-negate-x` mirrors X of bottom-side rows; the caller passes `bottom_negate_x` so it
  can be undone;
- values with spaces are quoted in csv; in ascii they are quoted with double quotes as well.
"""

from __future__ import annotations

import csv
import io
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PositionFile", "PositionRow", "parse_pos_ascii", "parse_pos_csv", "parse_pos"]

_UNIT_MM = {"mm": 1.0, "in": 25.4, "inch": 25.4}
_ASCII_UNIT_RE = re.compile(r"^##\s*Unit\s*=\s*(?P<unit>\w+)", re.IGNORECASE)


@dataclass(frozen=True)
class PositionRow:
    ref: str
    value: str
    package: str
    x_mm: float  # KiCad board frame (y down), absolute
    y_mm: float
    rotation_deg: float
    side: str  # "top" | "bottom"
    x_raw: float  # as written (file units converted to mm, y up, origin-relative)
    y_raw: float


@dataclass(frozen=True)
class PositionFile:
    source: str
    format: str  # "csv" | "ascii"
    rows: tuple[PositionRow, ...]
    units: str = "mm"
    aux_origin_mm: tuple[float, float] = (0.0, 0.0)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def row(self, ref: str) -> PositionRow | None:
        return next((r for r in self.rows if r.ref == ref), None)

    def side(self, side: str) -> tuple[PositionRow, ...]:
        return tuple(r for r in self.rows if r.side == side)

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(r.ref for r in self.rows)


def _row(
    cells: list[str], *, factor: float, origin: tuple[float, float], bottom_negate_x: bool
) -> PositionRow | None:
    if len(cells) < 7:
        return None
    ref, val, pkg = cells[0].strip(), cells[1].strip(), cells[2].strip()
    try:
        x_raw = float(cells[3]) * factor
        y_raw = float(cells[4]) * factor
        rot = float(cells[5])
    except ValueError:
        return None
    side = cells[6].strip().lower()
    x_file = -x_raw if (bottom_negate_x and side == "bottom") else x_raw
    return PositionRow(
        ref=ref,
        value=val,
        package=pkg,
        x_mm=round(x_file + origin[0], 6),
        y_mm=round(origin[1] - y_raw, 6),
        rotation_deg=rot,
        side=side,
        x_raw=round(x_raw, 6),
        y_raw=round(y_raw, 6),
    )


def parse_pos_csv(
    text: str,
    *,
    source: str = "",
    units: str = "mm",
    aux_origin_mm: tuple[float, float] = (0.0, 0.0),
    bottom_negate_x: bool = False,
) -> PositionFile:
    """Parse `--format csv` output. `units` is what the export was asked for ("mm" | "in")."""
    factor = _UNIT_MM.get(units.lower(), 1.0)
    rows: list[PositionRow] = []
    header: list[str] = []
    for cells in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if not cells or not any(c.strip() for c in cells):
            continue
        if not header:
            header = [c.strip() for c in cells]
            continue
        row = _row(cells, factor=factor, origin=aux_origin_mm, bottom_negate_x=bottom_negate_x)
        if row is not None:
            rows.append(row)
    return PositionFile(
        source=source,
        format="csv",
        rows=tuple(rows),
        units=units,
        aux_origin_mm=aux_origin_mm,
        raw={"header": header},
    )


def parse_pos_ascii(
    text: str,
    *,
    source: str = "",
    units: str | None = None,
    aux_origin_mm: tuple[float, float] = (0.0, 0.0),
    bottom_negate_x: bool = False,
) -> PositionFile:
    """Parse the default `.pos` text format. The `## Unit = mm` header wins over `units`."""
    unit = (units or "mm").lower()
    rows: list[PositionRow] = []
    header: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            header.append(line)
            m = _ASCII_UNIT_RE.match(line)
            if m:
                unit = m.group("unit").lower()
            continue
        try:
            cells = shlex.split(line, posix=True)
        except ValueError:
            cells = line.split()
        row = _row(
            cells, factor=_UNIT_MM.get(unit, 1.0), origin=aux_origin_mm, bottom_negate_x=bottom_negate_x
        )
        if row is not None:
            rows.append(row)
    return PositionFile(
        source=source,
        format="ascii",
        rows=tuple(rows),
        units=unit,
        aux_origin_mm=aux_origin_mm,
        raw={"header": header},
    )


def parse_pos(text: str, **kw: Any) -> PositionFile:
    """Dispatch on the first non-empty line: `#` -> ascii, otherwise csv."""
    first = next((ln for ln in text.lstrip("﻿").splitlines() if ln.strip()), "")
    if first.startswith("#"):
        return parse_pos_ascii(text, **kw)
    return parse_pos_csv(text, **kw)
