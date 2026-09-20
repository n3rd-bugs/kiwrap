"""Position (CPL) file post-processing: vendor rotation corrections on top of KiCad's own
`pcb export pos --format csv` output.

KiCad's footprint zero orientation and the orientation an assembly house's feeder / tape
database assumes differ per package family (the well-known SOT-23 / SOT-223 / QFN / QFP / SOP
mismatches). The correction is a table of `(footprint-name regex, rotation offset, xy offset)`
rows kept as a data file next to the profile (`jlcpcb_rotations.json`, with its source note);
this module applies it to the rows `kiwrap.formats.reports.position` parsed from KiCad's CSV
and reports every row it changed, so the agent (and the user) can see what the profile did.

Conventions - the same as Bouni/kicad-jlcpcb-tools (MIT, `fabrication.py`, the KiCad plugin
JLCPCB users have validated against real assembly runs for years; it downloads its default
table from matthewlai/JLCKicadTools `cpl_rotations_db.csv`):
- the KiCad rotation is taken from the pos file as written (KiCad's own orientation);
- bottom side rows are first mirrored: `rot = (180 - kicad_rot) mod 360` (KiCad states the angle
  as seen from the top; the vendor wants it as seen from the side the part sits on) - this
  applies to EVERY bottom row, matched or not;
- then the matching rule adds its offset: `rot = (rot + correction) mod 360` on both sides;
- the optional xy offset (mm, footprint frame) is rotated by the (mirrored, uncorrected)
  rotation in KiCad's y-down frame and added; x is negated on the bottom side;
- the rule whose regex match consumes the most characters wins; ties keep table order.

The output rows keep KiCad's pos-file coordinate convention (y up, absolute unless the export
used the drill/place origin), which is what the vendor's template expects from a KiCad export.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.formats.reports.position import PositionFile


@dataclass(frozen=True)
class RotationRule:
    """One row of a correction table."""

    pattern: str  # regex matched with `re.search` against the pos-file package (footprint) name
    rotation: float  # degrees added after the bottom-side mirroring
    offset_mm: tuple[float, float] = (0.0, 0.0)  # footprint-frame xy offset
    note: str = ""

    def match_len(self, package: str) -> int:
        m = re.search(self.pattern, package)
        return -1 if m is None else len(m.group(0))


@dataclass(frozen=True)
class RotationTable:
    rules: tuple[RotationRule, ...]
    source: str = ""

    @classmethod
    def load(cls, path: str | Path) -> RotationTable:
        """Read `{"_source": ..., "rules": [{pattern, rotation, offset_mm?, note?}, ...]}`."""
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RotationTable:
        rules: list[RotationRule] = []
        for r in raw.get("rules", []):
            off = r.get("offset_mm") or (0.0, 0.0)
            rules.append(
                RotationRule(
                    pattern=str(r["pattern"]),
                    rotation=float(r.get("rotation", 0.0)),
                    offset_mm=(float(off[0]), float(off[1])),
                    note=str(r.get("note", "")),
                )
            )
        return cls(rules=tuple(rules), source=str(raw.get("_source", "")))

    def match(self, package: str) -> RotationRule | None:
        """The rule consuming the most of `package`; ties keep table order; None when nothing matches."""
        best: RotationRule | None = None
        best_len = -1
        for rule in self.rules:
            n = rule.match_len(package)
            if n > best_len:
                best, best_len = rule, n
        return best


@dataclass(frozen=True)
class PositionCorrection:
    """A row a table rule changed (bottom-side mirroring alone is not listed here)."""

    ref: str
    package: str
    side: str
    pattern: str
    rotation_kicad: float
    rotation_out: float
    offset_mm: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class CorrectedRow:
    ref: str
    value: str
    package: str
    x_mm: float  # pos-file convention (KiCad's `PosX`)
    y_mm: float  # pos-file convention (KiCad's `PosY`, y up)
    rotation_deg: float  # normalised to [0, 360)
    side: str  # "top" | "bottom"
    rule: str | None = None  # the pattern that matched, if any


@dataclass
class CorrectedPositions:
    rows: list[CorrectedRow] = field(default_factory=list)
    corrections: list[PositionCorrection] = field(default_factory=list)
    bottom_mirrored: list[str] = field(default_factory=list)  # refs whose angle was mirrored (bottom side)
    skipped_refs: list[str] = field(default_factory=list)  # excluded by the caller (DNP / not fitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": len(self.rows),
            "refs": [r.ref for r in self.rows],
            "corrected": [
                {
                    "ref": c.ref,
                    "package": c.package,
                    "side": c.side,
                    "pattern": c.pattern,
                    "rotation_kicad": c.rotation_kicad,
                    "rotation_out": c.rotation_out,
                    "offset_mm": list(c.offset_mm),
                }
                for c in self.corrections
            ],
            "bottom_mirrored": list(self.bottom_mirrored),
            "skipped": list(self.skipped_refs),
        }


def _norm_deg(deg: float) -> float:
    d = math.fmod(deg, 360.0)
    if d < 0:
        d += 360.0
    if abs(d - 360.0) < 1e-9:
        d = 0.0
    return round(d, 4)


def correct_positions(
    pos: PositionFile,
    table: RotationTable | None,
    *,
    skip_refs: Iterable[str] = (),
) -> CorrectedPositions:
    """Apply `table` to every row of a parsed KiCad pos file (see the module docstring for the
    conventions). `skip_refs` (DNP parts when the export could not exclude them itself) are left
    out and listed in `skipped_refs`."""
    skip = set(skip_refs)
    out = CorrectedPositions()
    for row in pos.rows:
        if row.ref in skip:
            out.skipped_refs.append(row.ref)
            continue
        bottom = row.side == "bottom"
        kicad_rot = _norm_deg(row.rotation_deg)
        rot = _norm_deg(180.0 - row.rotation_deg) if bottom else kicad_rot
        if bottom:
            out.bottom_mirrored.append(row.ref)
        rule = table.match(row.package) if table is not None else None
        x, y = row.x_raw, row.y_raw
        if rule is not None:
            if rule.offset_mm != (0.0, 0.0):
                # Bouni/kicad-jlcpcb-tools `reposition`: KiCad's y-down frame, mirrored angle
                a = math.radians(rot)
                ox, oy = rule.offset_mm
                dx = ox * math.cos(a) + oy * math.sin(a)
                dy = -ox * math.sin(a) + oy * math.cos(a)
                if bottom:
                    dx = -dx
                x = round(x + dx, 6)
                y = round(y - dy, 6)  # pos-file y is up: KiCad's y-down offset flips sign
            rot = _norm_deg(rot + rule.rotation)
            if rule.rotation != 0.0 or rule.offset_mm != (0.0, 0.0):
                out.corrections.append(
                    PositionCorrection(
                        ref=row.ref,
                        package=row.package,
                        side=row.side,
                        pattern=rule.pattern,
                        rotation_kicad=kicad_rot,
                        rotation_out=rot,
                        offset_mm=rule.offset_mm,
                    )
                )
        out.rows.append(
            CorrectedRow(
                ref=row.ref,
                value=row.value,
                package=row.package,
                x_mm=x,
                y_mm=y,
                rotation_deg=rot,
                side=row.side,
                rule=rule.pattern if rule is not None else None,
            )
        )
    return out


def cpl_csv(rows: Sequence[CorrectedRow], *, columns: Sequence[str], side_names: dict[str, str]) -> str:
    """Render rows as a vendor CPL CSV. `columns` are the header labels, in order, for
    (ref, value, package, x, y, rotation, side); `side_names` maps top/bottom to the vendor's
    spelling."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(list(columns))
    for r in rows:
        w.writerow(
            [
                r.ref,
                r.value,
                r.package,
                f"{r.x_mm:.4f}",
                f"{r.y_mm:.4f}",
                f"{r.rotation_deg:g}",
                side_names.get(r.side, r.side),
            ]
        )
    return buf.getvalue()
