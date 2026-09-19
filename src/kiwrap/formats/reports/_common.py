"""Shared machinery for the ERC/DRC parsers: JSON item decoding, unit handling, the ×100 fix
for ERC JSON positions and the text-report grammar both checkers share.

Facts encoded here (docs/VERIFICATION.md claim 1):

* `kicad-cli sch erc --format json` on 8.0.1 / 9.0.5 / 10.0.0 reports every `items[].pos`
  exactly 100× too small and also scales the lengths embedded in item `description` strings
  ("Horizontal Wire, length 0.1270 mm" for a 12.70 mm wire). The factor is the same for
  `--units in`. The text report is correct. `pcb drc` JSON is correct. The caller passes the
  factor as `position_scale` (from the capability record); this module never decides it.
* JSON `coordinate_units` is "mm" or "in" (observed) and positions must come out in mm.
* Text reports print items as `    @(x unit, y unit): description`, a rule/severity line
  `    <rule>; <severity>` (7.0 spelled it `Severity: warning`), and DRC wraps sections in
  `** Found N DRC violations **`, `** Found N unconnected pads **`, `** Found N Footprint
  errors **`; ERC groups by `***** Sheet <path>`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from kiwrap.formats.reports import SEVERITIES, Position, Violation, ViolationItem

BOM = chr(0xFEFF)

_UNIT_TO_MM = {
    "mm": 1.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    "mil": 0.0254,
    "mils": 0.0254,
    "thou": 0.0254,
}

# A dimension inside free text. KiCad's MessageTextFromValue always prints a decimal point AND a
# space before the unit ("12.70 mm", "0.0050 in"; every kicad-cli sample under spikes/ agrees), so
# the space is mandatory: footprint / library names such as 'Connector_PinSocket_2.54mm' or
# '..._MountingHolesOffset9.12mm' embed "N.Nmm" with no space and must never be rescaled. The
# lookbehind also refuses a number glued to a word or a dotted version ("P2.77x2.84 mm", "1.2.5 mm").
_DIMENSION = re.compile(r"(?<![\w.])(-?\d+\.\d+)(\s+)(mm|in|mils?|thou)\b")

# Text report grammar.
_VIOLATION_HEAD = re.compile(r"^\[(?P<type>[A-Za-z0-9_]+)\]: ?(?P<desc>.*)$")
_SEVERITY_LINE = re.compile(
    r"^\s{2,}(?P<rule>.*?);\s*(?:Severity:\s*)?(?P<severity>[A-Za-z]+)(?:\s*\((?P<excluded>excluded)\))?\s*$"
)
_ITEM_LINE = re.compile(
    r"^\s{2,}@\(\s*(?P<x>-?\d+(?:\.\d+)?)\s*(?P<xu>[A-Za-z]+),\s*(?P<y>-?\d+(?:\.\d+)?)\s*(?P<yu>[A-Za-z]+)\s*\):\s?(?P<desc>.*)$"
)
_SHEET_LINE = re.compile(r"^\*{3,}\s*Sheet\s+(?P<path>.*?)\s*$")
_DRC_SECTION = re.compile(r"^\*\*\s*Found\s+(?P<n>\d+)\s+(?P<what>.*?)\s*\*\*\s*$")


def unit_factor(unit: str | None) -> float:
    """Millimetres per `unit` ("mm", "in", "mils"); unknown or missing units are taken as mm."""
    if not unit:
        return 1.0
    return _UNIT_TO_MM.get(unit.strip().lower(), 1.0)


def rescale_description(text: str, position_scale: float) -> str:
    """Multiply every `N.NN <unit>` dimension in `text` by `position_scale`, keeping the decimals.

    "Horizontal Wire, length 0.1270 mm" × 100 -> "Horizontal Wire, length 12.7000 mm". Only
    KiCad-formatted dimensions (space before the unit) are touched; "'Connector_PinSocket_2.54mm'"
    is a library name and stays as it is.
    """
    if position_scale == 1.0 or not text:
        return text

    def fix(m: re.Match[str]) -> str:
        number, space, unit = m.group(1), m.group(2), m.group(3)
        decimals = len(number.split(".")[1])
        return f"{float(number) * position_scale:.{decimals}f}{space}{unit}"

    return _DIMENSION.sub(fix, text)


def normalise_severity(text: str | None) -> str:
    """Map a report severity word onto SEVERITIES ("error", "warning", "exclusion", "ignore")."""
    s = (text or "").strip().lower()
    if s in SEVERITIES:
        return s
    if s.startswith("excl"):
        return "exclusion"
    if s in ("ignored", "ignore", "action", "info"):
        return "ignore"
    return s or "error"


def item_from_json(raw: dict[str, Any], *, unit_scale: float, position_scale: float) -> ViolationItem:
    pos_raw = raw.get("pos")
    pos: Position | None = None
    if isinstance(pos_raw, dict) and "x" in pos_raw and "y" in pos_raw:
        factor = unit_scale * position_scale
        pos = Position(float(pos_raw["x"]) * factor, float(pos_raw["y"]) * factor)
    return ViolationItem(
        uuid=str(raw.get("uuid", "")),
        description=rescale_description(str(raw.get("description", "")), position_scale),
        pos=pos,
    )


def violation_from_json(
    raw: dict[str, Any], *, unit_scale: float, position_scale: float, sheet: str | None = None
) -> Violation:
    severity = normalise_severity(raw.get("severity"))
    excluded = bool(raw.get("excluded", False)) or severity == "exclusion"
    items = tuple(
        item_from_json(i, unit_scale=unit_scale, position_scale=position_scale)
        for i in raw.get("items", ())
        if isinstance(i, dict)
    )
    return Violation(
        type=str(raw.get("type", "")),
        severity=severity,
        description=rescale_description(str(raw.get("description", "")), position_scale),
        items=items,
        sheet=sheet,
        excluded=excluded,
    )


def load_json(text: str) -> dict[str, Any]:
    """Parse the report JSON; kicad-cli writes plain UTF-8 (a BOM is tolerated)."""
    data = json.loads(text.removeprefix(BOM))
    if not isinstance(data, dict):
        raise ValueError("report JSON is not an object")
    return data


# ----------------------------------------------------------------------------------------------
# text reports
# ----------------------------------------------------------------------------------------------


class TextSection:
    """One block of a text report: its `name` (DRC section / ERC sheet path) and violations."""

    def __init__(self, name: str | None) -> None:
        self.name = name
        self.violations: list[Violation] = []


def parse_text_blocks(lines: Iterable[str], *, sheet_mode: bool) -> tuple[list[TextSection], dict[str, Any]]:
    """Walk a text report, grouping `[type]: …` blocks into sections.

    `sheet_mode=True` opens a new section at every `***** Sheet <path>` line (ERC); otherwise
    sections open at `** Found N <what> **` lines (DRC). Returns the sections plus a small
    header dict (`title`, `counts`) for `Report.raw`.
    """
    sections: list[TextSection] = [TextSection(None)]
    header: dict[str, Any] = {"format": "text", "counts": {}}
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        sections[-1].violations.append(
            Violation(
                type=current["type"],
                severity=current["severity"],
                description=current["desc"],
                items=tuple(current["items"]),
                sheet=sections[-1].name if sheet_mode else None,
                excluded=current["excluded"],
            )
        )
        current = None

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if sheet_mode and (m := _SHEET_LINE.match(line)):
            flush()
            sections.append(TextSection(m.group("path")))
            continue
        if not sheet_mode and (m := _DRC_SECTION.match(line)):
            flush()
            sections.append(TextSection(m.group("what").lower()))
            header["counts"][m.group("what").lower()] = int(m.group("n"))
            continue
        if m := _VIOLATION_HEAD.match(line):
            flush()
            current = {
                "type": m.group("type"),
                "desc": m.group("desc").strip(),
                "severity": "error",
                "excluded": False,
                "items": [],
            }
            continue
        if current is not None and (m := _ITEM_LINE.match(line)):
            x = float(m.group("x")) * unit_factor(m.group("xu"))
            y = float(m.group("y")) * unit_factor(m.group("yu"))
            current["items"].append(
                ViolationItem(uuid="", description=m.group("desc").strip(), pos=Position(x, y))
            )
            continue
        if current is not None and not current["items"] and (m := _SEVERITY_LINE.match(line)):
            current["severity"] = normalise_severity(m.group("severity"))
            current["excluded"] = bool(m.group("excluded")) or current["severity"] == "exclusion"
            if m.group("rule").strip():
                current["rule"] = m.group("rule").strip()
            continue
        stripped = line.lstrip()
        if (
            stripped.startswith("**")
            or stripped.startswith("ERC report")
            or stripped.startswith("Report includes")
        ):
            flush()
            header.setdefault("header_lines", []).append(line.strip())
            continue
        if current is not None and line.startswith(" "):
            # continuation of a wrapped description
            current["desc"] = (current["desc"] + " " + line.strip()).strip()
    flush()
    return sections, header
