"""DRC report parsers: `kicad-cli pcb drc --format json` (schema drc.v1) and `--format report`.

JSON shape (8.0.1 / 9.0.5 / 10.0.0): `{"$schema", "coordinate_units", "date", "kicad_version",
"source", "violations": [...], "unconnected_items": [...], "schematic_parity": [...],
"ignored_checks"?, "included_severities"?}` with the same violation objects as ERC.

DRC JSON positions are correct in millimetres on every version (docs/VERIFICATION.md claim 1);
`position_scale` exists only so callers can drive both checkers through one code path and
defaults to 1.0.

Text report sections: `** Found N DRC violations **`, `** Found N unconnected pads **`,
`** Found N Footprint errors **` (schematic parity), `** Ignored checks **` (10.0+: one
`    - <description>` line per check), `** End of Report **`.

Ignored checks (requests/kiwrap-feature-requests.review.md item 23, §5 claim 6):
- kicad-cli 10.0+ JSON carries `ignored_checks: [{key, description}]` and `included_severities`
  (docs/research/07-versioning-strategy.md, verified on 10.0.0 and 10.0.6). The list is KiCad's
  EFFECTIVE set: the project's `rule_severities` 'ignore' entries PLUS KiCad's own default ignores.
  Verified 2026-09-24 with a bare `{}` `.kicad_pro` on 10.0.6: missing_courtyard,
  track_not_centered_on_via, tuning_profile_track_geometries, footprint_filters_mismatch,
  footprint_type_mismatch (the review: "ecc83 lists one ignore, yet KiCad reports three").
- 8.0 / 9.0 JSON has neither key and their text report has no ignored section (verified on 8.0.9
  / 9.0.9), so only the project's own `rule_severities` can be reported there
  (`ignored_rule_keys`); KiCad's built-in defaults stay unknown on those versions.
- `.kicad_pro` `rule_severities` is incomplete by design (63 keys in pic_programmer's file against
  77 over IPC): a key missing from the file takes KiCad's default.

Item descriptions (`item_ref` / `item_net`): kicad-cli prints English UI texts such as
`PTH pad 2 [GND] of R4`, `NPTH pad of X`, `Footprint U3`, `Track [GND] on F.Cu, length 1.2 mm`,
`Zone [GND] on bottom_layer` (review B8 / item 21). They are a fallback for joining a violation to
footprint references and nets when the item uuid cannot be joined to the board file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kiwrap.formats.reports import Report, Violation
from kiwrap.formats.reports._common import (
    load_json,
    parse_text_blocks,
    unit_factor,
    violation_from_json,
)

__all__ = [
    "IgnoredCheck",
    "ignored_checks_from_json",
    "ignored_rule_keys",
    "included_severities_from_json",
    "item_net",
    "item_ref",
    "parse_drc_json",
    "parse_drc_text",
]


@dataclass(frozen=True)
class IgnoredCheck:
    """One DRC check KiCad skipped. `key` is the settings key (`missing_courtyard`); the text
    report only prints the description, so `key` is "" there."""

    key: str
    description: str


def ignored_checks_from_json(data: Mapping[str, Any]) -> tuple[IgnoredCheck, ...] | None:
    """`ignored_checks` of a drc.v1 document (10.0+), or None when the document has no such key
    (8.0 / 9.0: KiCad did not say which checks it skipped)."""
    raw = data.get("ignored_checks")
    if not isinstance(raw, list):
        return None
    out: list[IgnoredCheck] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            out.append(IgnoredCheck(str(entry.get("key") or ""), str(entry.get("description") or "")))
        elif isinstance(entry, str):
            out.append(IgnoredCheck(entry, ""))
    return tuple(out)


def included_severities_from_json(data: Mapping[str, Any]) -> tuple[str, ...] | None:
    """`included_severities` of a drc.v1 document (10.0+: `["error", "warning", "exclusion"]`)."""
    raw = data.get("included_severities")
    if not isinstance(raw, list):
        return None
    return tuple(str(s) for s in raw)


def ignored_rule_keys(project: Mapping[str, Any]) -> tuple[str, ...]:
    """The DRC check keys a parsed `.kicad_pro` sets to 'ignore'
    (`board.design_settings.rule_severities`), sorted. Only what the FILE says: keys it omits take
    KiCad's defaults (module docstring)."""
    board = project.get("board") if isinstance(project, Mapping) else None
    settings = board.get("design_settings") if isinstance(board, Mapping) else None
    severities = settings.get("rule_severities") if isinstance(settings, Mapping) else None
    if not isinstance(severities, Mapping):
        return ()
    return tuple(sorted(str(k) for k, v in severities.items() if str(v).strip().lower() == "ignore"))


_NET_IN_TEXT = re.compile(r"\[([^\]]*)\]")
_REF_AFTER_OF = re.compile(r"\bof\s+(?P<ref>[^\s,;\[\]]+)")
_FOOTPRINT_HEAD = re.compile(r"^Footprint\s+(?P<ref>[^\s,;\[\]]+)")


def item_ref(description: str) -> str | None:
    """The footprint reference a KiCad item description names (`PTH pad 2 [GND] of R4` -> R4,
    `Footprint U3` -> U3, `Line of U8 on F.SilkS` -> U8), else None. English texts only."""
    text = (description or "").strip()
    m = _FOOTPRINT_HEAD.match(text)
    if m:
        return m.group("ref")
    refs = [m.group("ref") for m in _REF_AFTER_OF.finditer(text)]
    return refs[-1] if refs else None


def item_net(description: str) -> str | None:
    """The net a KiCad item description names in brackets (`Track [GND] on F.Cu` -> GND); None
    for `<no net>` or when there is none."""
    m = _NET_IN_TEXT.search(description or "")
    if not m:
        return None
    net = m.group(1).strip()
    return None if not net or net.startswith("<") else net


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
    lines = text.splitlines()
    sections, header = parse_text_blocks(lines, sheet_mode=False)
    ignored = _text_ignored_checks(lines)
    if ignored is not None:
        header["ignored_checks"] = [{"key": c.key, "description": c.description} for c in ignored]
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


def _text_ignored_checks(lines: list[str]) -> tuple[IgnoredCheck, ...] | None:
    """The `** Ignored checks **` section of a 10.0+ text report (descriptions only), else None."""
    out: list[IgnoredCheck] | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("**"):
            if out is not None:
                break
            if stripped.strip("* ").lower() == "ignored checks":
                out = []
            continue
        if out is not None and stripped.startswith("- "):
            out.append(IgnoredCheck("", stripped[2:].strip()))
    return None if out is None else tuple(out)
