"""ERC report parsers: `kicad-cli sch erc --format json` (schema erc.v1) and `--format report`.

JSON shape (8.0.1 / 9.0.5 / 10.0.0): `{"$schema", "coordinate_units", "date", "kicad_version",
"sheets": [{"path", "uuid_path", "violations": [{"type", "severity", "description",
"items": [{"uuid", "description", "pos": {"x", "y"}}], "excluded"?}]}], "ignored_checks"?,
"included_severities"?}`.

The JSON positions are 100× too small on every version that has the command; pass
`position_scale=100` (capability `cli.erc_json_pos_scale`) and both `pos` and the dimensions in
the description strings are corrected (docs/VERIFICATION.md claim 1). The correction is applied
ONCE, to the document (`rescale_erc_document`), so `Report.raw` is corrected as well and no
other layer needs a second rescaler. The text report is already in real units, so
`parse_erc_text` takes no scale.
"""

from __future__ import annotations

import copy
from typing import Any

from kiwrap.formats.reports import Report, Violation
from kiwrap.formats.reports._common import (
    load_json,
    parse_text_blocks,
    rescale_description,
    unit_factor,
    violation_from_json,
)

__all__ = ["parse_erc_json", "parse_erc_text", "rescale_erc_document"]


def rescale_erc_document(data: dict[str, Any], position_scale: float) -> dict[str, Any]:
    """Deep copy of an erc.v1 document with every item `pos` and every violation / item
    description multiplied by `position_scale` (claim 1: positions AND the "length 0.2032 mm"
    inside descriptions are 100× too small). Positions are rounded to 6 decimals so the float
    noise of `1.0033 * 100` does not leak into `Report.raw`; descriptions keep the number of
    decimals kicad-cli printed. Identity (still a copy) for `position_scale == 1`."""
    out = copy.deepcopy(data)
    if position_scale == 1.0:
        return out
    for sheet in out.get("sheets", ()):
        if not isinstance(sheet, dict):
            continue
        for violation in sheet.get("violations", ()):
            if not isinstance(violation, dict):
                continue
            if isinstance(violation.get("description"), str):
                violation["description"] = rescale_description(violation["description"], position_scale)
            for item in violation.get("items", ()):
                if not isinstance(item, dict):
                    continue
                pos = item.get("pos")
                if isinstance(pos, dict):
                    for axis in ("x", "y"):
                        if isinstance(pos.get(axis), int | float) and not isinstance(pos[axis], bool):
                            pos[axis] = round(pos[axis] * position_scale, 6)
                if isinstance(item.get("description"), str):
                    item["description"] = rescale_description(item["description"], position_scale)
    return out


def parse_erc_json(
    text: str, *, position_scale: float = 1.0, source: str = "", kicad_version: str = ""
) -> Report:
    """Parse ERC JSON. `source`/`kicad_version` fall back to what the JSON itself says.

    With `position_scale != 1` the document is rescaled first (`rescale_erc_document`), so the
    violations and `Report.raw` agree.
    """
    data = rescale_erc_document(load_json(text), position_scale)
    units = unit_factor(data.get("coordinate_units"))
    violations: list[Violation] = []
    for sheet in data.get("sheets", ()):
        if not isinstance(sheet, dict):
            continue
        path = str(sheet.get("path", "")) or None
        for raw in sheet.get("violations", ()):
            if isinstance(raw, dict):
                violations.append(violation_from_json(raw, unit_scale=units, position_scale=1.0, sheet=path))
    return Report(
        kind="erc",
        source=source or str(data.get("source", "")),
        kicad_version=kicad_version or str(data.get("kicad_version", "")),
        violations=tuple(violations),
        raw=data,
    )


def parse_erc_text(text: str, *, source: str = "", kicad_version: str = "") -> Report:
    """Parse the `--format report` text (also what the GUI writes; the only ERC output on 7.0).

    Positions are read from `@(x mm, y mm)` and converted to mm; each `***** Sheet <path>`
    heading becomes `Violation.sheet`.
    """
    sections, header = parse_text_blocks(text.splitlines(), sheet_mode=True)
    violations = tuple(v for s in sections for v in s.violations)
    return Report(kind="erc", source=source, kicad_version=kicad_version, violations=violations, raw=header)
