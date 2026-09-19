"""DRC report parsers: `kicad-cli pcb drc --format json` (schema drc.v1) and `--format report`.

JSON shape (8.0.1 / 9.0.5 / 10.0.0): `{"$schema", "coordinate_units", "date", "kicad_version",
"source", "violations": [...], "unconnected_items": [...], "schematic_parity": [...],
"ignored_checks"?, "included_severities"?}` with the same violation objects as ERC.

DRC JSON positions are correct in millimetres on every version (docs/VERIFICATION.md claim 1);
`position_scale` exists only so callers can drive both checkers through one code path and
defaults to 1.0.

Text report sections: `** Found N DRC violations **`, `** Found N unconnected pads **`,
`** Found N Footprint errors **` (schematic parity), `** End of Report **`.
"""

from __future__ import annotations

from typing import Any

from kiwrap.formats.reports import Report, Violation
from kiwrap.formats.reports._common import (
    load_json,
    parse_text_blocks,
    unit_factor,
    violation_from_json,
)

__all__ = ["parse_drc_json", "parse_drc_text"]


def _bucket(data: dict[str, Any], key: str, *, units: float, position_scale: float) -> tuple[Violation, ...]:
    return tuple(
        violation_from_json(raw, unit_scale=units, position_scale=position_scale)
        for raw in data.get(key, ())
        if isinstance(raw, dict)
    )


def parse_drc_json(
    text: str, *, position_scale: float = 1.0, source: str = "", kicad_version: str = ""
) -> Report:
    """Parse DRC JSON. `source`/`kicad_version` fall back to the JSON's own fields."""
    data = load_json(text)
    units = unit_factor(data.get("coordinate_units"))
    return Report(
        kind="drc",
        source=source or str(data.get("source", "")),
        kicad_version=kicad_version or str(data.get("kicad_version", "")),
        violations=_bucket(data, "violations", units=units, position_scale=position_scale),
        unconnected=_bucket(data, "unconnected_items", units=units, position_scale=position_scale),
        schematic_parity=_bucket(data, "schematic_parity", units=units, position_scale=position_scale),
        raw=data,
    )


def parse_drc_text(text: str, *, source: str = "", kicad_version: str = "") -> Report:
    """Parse the `--format report` text (7.0's `pcbnew` GUI/SWIG `WriteDRCReport` use the same)."""
    sections, header = parse_text_blocks(text.splitlines(), sheet_mode=False)
    violations: list[Violation] = []
    unconnected: list[Violation] = []
    parity: list[Violation] = []
    for section in sections:
        name = section.name or ""
        if "unconnected" in name:
            unconnected.extend(section.violations)
        elif "footprint" in name or "parity" in name:
            parity.extend(section.violations)
        else:
            violations.extend(section.violations)
    return Report(
        kind="drc",
        source=source,
        kicad_version=kicad_version,
        violations=tuple(violations),
        unconnected=tuple(unconnected),
        schematic_parity=tuple(parity),
        raw=header,
    )
