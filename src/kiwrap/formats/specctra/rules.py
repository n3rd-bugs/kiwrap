"""Design rules a DSN carries, resolved the way KiCad's exporter reads them.

KiCad's `SPECCTRA_DB::FromBOARD` takes every rule from the project's `NET_SETTINGS`
(`.kicad_pro` `net_settings`): the Default netclass gives the board-level `(rule (width) (clearance)
(clearance … (type smd_smd)))`, every netclass in use becomes a `(class …)` with its own width,
clearance and `use_via`, and a via padstack per distinct (diameter, drill). Board-level
`design_settings.rules` supply the hole clearance (NPTH pads become round keepouts of
drill + 2 × `min_hole_clearance`), the polygonisation error (`max_error`) and the copper-to-edge
clearance the router must be told about separately (KiCad's DSN omits it; VERIFICATION claim 8).

Netclass membership follows `NET_SETTINGS::GetEffectiveNetClass` (common/project/net_settings.cpp,
tag 10.0.6): explicit `netclass_assignments` (net name -> classes) first, then every
`netclass_patterns` entry whose pattern matches the whole net name either as an anchored regular
expression or as an anchored `*`/`?` wildcard (`EDA_COMBINED_MATCHER` in `CTX_NETCLASS` mode);
the empty net name is always Default. When several classes match, KiCad 9+/10 builds an
EFFECTIVE netclass (`NET_SETTINGS::makeEffectiveNetclass`): the constituents are sorted by
`priority` (lowest value first, ties by name) and every parameter comes from the highest-priority
class that defines it; when one of clearance / track_width / via_diameter / via_drill /
microvia_diameter / microvia_drill / diff_pair_width / diff_pair_gap / wire_width / bus_width is
still missing (`addMissingDefaults`, common/project/net_settings.cpp tag 10.0.6) the Default class
is appended as the last constituent. The class NAME the exporter writes is `NETCLASS::GetName()`:
the constituents joined with `,` - a class that defines every parameter keeps its bare name, a
diff-pair class that sets only its diff-pair values exports as `85Ohm-diff_DP,Default`, a net in
two classes as `A,B` (verified on the 10.0.6 jetson-agx-thor-baseboard demo: the six diff-pair
classes are `<name>,Default` in KiCad's DSN). A class named in `netclass_assignments` but absent
from `classes` is an implicit netclass (no parameters, priority INT_MAX - 1). A missing Default
falls back to `NETCLASS`'s built-in defaults (common/netclass.cpp: clearance 0.2, track 0.2, via
0.6 / 0.3 mm). `NetclassRule.name` is that effective name and `constituents` the parts, so the
`-inc` class names `kiwrap.backends.router.rules.netclass_nets` derives match the DSN.

KiCad migrates an old `net_settings` on load (`NESTED_SETTINGS`: when `meta.version` is present
and below the schema, 5 on 10.0.6), and the migrations change what the DSN sees, so they are
replayed here: `migrateSchema2to3` (file version < 3) *replaces* `netclass_patterns` with the
classes' legacy `nets` lists — a project written with `meta.version 0` and no `nets` keys loses
every pattern, which is why such a board exports with Default only; `migrateSchema3to4`
(< 4) assigns priorities in file order and turns string `netclass_assignments` into lists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kiwrap.formats.board import BoardFile

__all__ = [
    "DEFAULT_CLASS",
    "DsnRules",
    "NetclassRule",
    "effective_class",
    "netclass_pattern_matches",
    "rules_from_project",
]

DEFAULT_CLASS = "Default"
# common/netclass.cpp DEFAULT_* (millimetres).
_KICAD_DEFAULT_CLEARANCE = 0.2
_KICAD_DEFAULT_TRACK_WIDTH = 0.2
_KICAD_DEFAULT_VIA_DIAMETER = 0.6
_KICAD_DEFAULT_VIA_DRILL = 0.3
# pcbnew/board_design_settings.h DEFAULT_HOLECLEARANCE / DEFAULT_COPPEREDGECLEARANCE, ARC_HIGH_DEF.
_KICAD_DEFAULT_HOLE_CLEARANCE = 0.25
_KICAD_DEFAULT_COPPER_EDGE_CLEARANCE = 0.5
_KICAD_DEFAULT_MAX_ERROR = 0.005
_KICAD_DEFAULT_MIN_TRACK_WIDTH = 0.2  # KiCad 10 BOARD() default; claim 8
_INT_MAX = 2**31 - 1


#: the .kicad_pro keys whose absence makes `addMissingDefaults` append the Default class
_MERGED_PARAMS = (
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
    "microvia_diameter",
    "microvia_drill",
    "diff_pair_width",
    "diff_pair_gap",
    "wire_width",
    "bus_width",
)
_IMPLICIT_PRIORITY = _INT_MAX - 1


@dataclass(frozen=True)
class NetclassRule:
    """One (effective) netclass as the DSN needs it (millimetres). `defined` lists the merged
    parameters the class sets itself; `constituents` the classes an effective class is made of
    (`("A", "Default")` for a class that borrows from Default) - empty for a plain class."""

    name: str
    track_width: float
    clearance: float
    via_diameter: float
    via_drill: float
    priority: int = _INT_MAX
    defined: frozenset[str] = frozenset(_MERGED_PARAMS)
    constituents: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_CLASS

    @property
    def base_name(self) -> str:
        """The first constituent: the class name without the `,Default` (or `,B`) suffix."""
        return self.constituents[0] if self.constituents else self.name


@dataclass
class DsnRules:
    """Rules for one board: the Default class, the other classes, and each net's class name.

    `net_class` maps a net name to the netclass that governs it (nets absent from the map are
    Default). `min_track_width`, `min_via_diameter`, `hole_clearance`, `copper_edge_clearance` and
    `max_error` come from `design_settings.rules` and drive the round keepouts, the
    polygonisation and the router flags.
    """

    default: NetclassRule
    classes: tuple[NetclassRule, ...] = ()
    net_class: dict[str, str] = field(default_factory=dict)
    hole_clearance: float = _KICAD_DEFAULT_HOLE_CLEARANCE
    copper_edge_clearance: float = _KICAD_DEFAULT_COPPER_EDGE_CLEARANCE
    max_error: float = _KICAD_DEFAULT_MAX_ERROR
    min_track_width: float = _KICAD_DEFAULT_MIN_TRACK_WIDTH
    min_via_diameter: float | None = None
    min_clearance: float | None = None

    @classmethod
    def kicad_defaults(cls) -> DsnRules:
        return cls(
            NetclassRule(
                DEFAULT_CLASS,
                _KICAD_DEFAULT_TRACK_WIDTH,
                _KICAD_DEFAULT_CLEARANCE,
                _KICAD_DEFAULT_VIA_DIAMETER,
                _KICAD_DEFAULT_VIA_DRILL,
            )
        )

    def class_of(self, net_name: str) -> NetclassRule:
        name = self.net_class.get(net_name, DEFAULT_CLASS)
        for c in self.classes:
            if c.name == name:
                return c
        return self.default

    def by_name(self, name: str) -> NetclassRule | None:
        if name == DEFAULT_CLASS:
            return self.default
        for c in self.classes:
            if c.name == name:
                return c
        return None


# ----------------------------------------------------------------------------------------------
# netclass pattern matching (EDA_COMBINED_MATCHER, CTX_NETCLASS)
# ----------------------------------------------------------------------------------------------

_WILDCARD_ESCAPE = ".*+?^${}()|[]/\\"


def _wildcard_regex(pattern: str) -> str:
    """`EDA_PATTERN_MATCH_WILDCARD_ANCHORED::SetPattern`: `?` -> `.`, `*` -> `.*`, the rest literal."""
    out = ["^"]
    for ch in pattern:
        if ch == "?":
            out.append(".")
        elif ch == "*":
            out.append(".*")
        elif ch in _WILDCARD_ESCAPE:
            out.append("\\" + ch)
        else:
            out.append(ch)
    out.append("$")
    return "".join(out)


def netclass_pattern_matches(pattern: str, net_name: str) -> bool:
    """True when the whole net name matches `pattern` as an anchored regex or anchored wildcard."""
    if pattern == net_name:
        return True
    try:
        if re.fullmatch(pattern, net_name):
            return True
    except re.error:
        pass
    return re.match(_wildcard_regex(pattern), net_name) is not None


# ----------------------------------------------------------------------------------------------
# reading the project file
# ----------------------------------------------------------------------------------------------


def _num(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _net_settings_schema(net_settings: Mapping[str, Any]) -> int:
    """`net_settings.meta.version` as KiCad's NESTED_SETTINGS reads it: -1 (no migration) when
    absent, otherwise the number, which triggers the migrations below when it is older than the
    running KiCad's schema (5 on 10.0.6)."""
    meta = net_settings.get("meta")
    if not isinstance(meta, Mapping):
        return -1
    try:
        return int(meta.get("version", -1))
    except (TypeError, ValueError):
        return -1


def _class_from_json(entry: Mapping[str, Any], default: NetclassRule) -> NetclassRule:
    name = str(entry.get("name", ""))
    defined = frozenset(k for k in _MERGED_PARAMS if entry.get(k) is not None)
    return NetclassRule(
        name=name,
        track_width=_num(entry.get("track_width"), default.track_width),
        clearance=_num(entry.get("clearance"), default.clearance),
        via_diameter=_num(entry.get("via_diameter"), default.via_diameter),
        via_drill=_num(entry.get("via_drill"), default.via_drill),
        priority=int(_num(entry.get("priority"), _INT_MAX)),
        defined=defined,
    )


def _implicit_class(name: str, default: NetclassRule) -> NetclassRule:
    """A netclass a label assigns but the project never configured (`getOrAddImplicitNetcless`)."""
    return NetclassRule(
        name=name,
        track_width=default.track_width,
        clearance=default.clearance,
        via_diameter=default.via_diameter,
        via_drill=default.via_drill,
        priority=_IMPLICIT_PRIORITY,
        defined=frozenset(),
    )


def effective_class(matched: Sequence[NetclassRule], default: NetclassRule) -> NetclassRule:
    """`NET_SETTINGS::makeEffectiveNetclass` + `NETCLASS::GetName` for the classes a net matched
    (module docstring): sorted by (priority, name), parameters filled from the lowest priority
    upwards so the highest-priority definition wins, Default appended when a merged parameter is
    still undefined; the name joins the constituents with `,`."""
    if not matched:
        return default
    ordered = sorted(matched, key=lambda c: (c.priority, c.name))
    values: dict[str, float] = {}
    defined: set[str] = set()
    for cls in reversed(ordered):
        for key in ("track_width", "clearance", "via_diameter", "via_drill"):
            if key in cls.defined:
                values[key] = getattr(cls, key)
        defined.update(cls.defined)
    names = [c.name for c in ordered]
    if defined < set(_MERGED_PARAMS):
        names.append(default.name)
    if len(names) == 1:
        return ordered[0]
    return NetclassRule(
        name=",".join(names),
        track_width=values.get("track_width", default.track_width),
        clearance=values.get("clearance", default.clearance),
        via_diameter=values.get("via_diameter", default.via_diameter),
        via_drill=values.get("via_drill", default.via_drill),
        priority=ordered[0].priority,
        defined=frozenset(_MERGED_PARAMS),
        constituents=tuple(names),
    )


def rules_from_project(
    board: BoardFile | None = None,
    project: Mapping[str, Any] | str | Path | None = None,
    *,
    net_names: Iterable[str] | None = None,
) -> DsnRules:
    """Build `DsnRules` from a `.kicad_pro` (`project`: its parsed JSON or path) next to `board`.

    With no project file the KiCad built-in defaults apply. The net -> class map is resolved for
    `net_names` (default: every net of `board`).
    """
    doc: Mapping[str, Any] = {}
    if isinstance(project, Mapping):
        doc = project
    else:
        path: Path | None = Path(project) if project is not None else None
        if path is None and board is not None and board.path is not None:
            candidate = board.path.with_suffix(".kicad_pro")
            path = candidate if candidate.is_file() else None
        if path is not None and path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
    net_settings = doc.get("net_settings", {}) if isinstance(doc, Mapping) else {}
    if not isinstance(net_settings, Mapping):
        net_settings = {}
    schema = _net_settings_schema(net_settings)
    fallback = DsnRules.kicad_defaults().default
    default = fallback
    others: list[NetclassRule] = []
    classes_json = net_settings.get("classes", [])
    if not isinstance(classes_json, list):
        classes_json = []
    for entry in classes_json:
        if isinstance(entry, Mapping) and entry.get("name") == DEFAULT_CLASS:
            default = _class_from_json(entry, fallback)
    order = 0
    for entry in classes_json:
        if isinstance(entry, Mapping) and entry.get("name") not in (DEFAULT_CLASS, None, ""):
            rule = _class_from_json(entry, default)
            if 0 <= schema < 4:
                # migrateSchema3to4: priorities are assigned in file order
                rule = replace(rule, priority=order)
                order += 1
            others.append(rule)
    by_name = {c.name: c for c in others}

    assignments: dict[str, list[str]] = {}
    raw_assign = net_settings.get("netclass_assignments", {})
    if isinstance(raw_assign, Mapping):
        for net, names in raw_assign.items():
            if isinstance(names, list):
                assignments[str(net)] = [str(n) for n in names]
            elif isinstance(names, str) and names:
                assignments[str(net)] = [names]
    patterns: list[tuple[str, str]] = []
    if 0 <= schema < 3:
        # migrateSchema2to3 REPLACES `netclass_patterns` with the classes' legacy `nets` lists
        # (an empty list when a class has none), whatever the file already carried.
        for entry in classes_json:
            if isinstance(entry, Mapping) and isinstance(entry.get("nets"), list):
                patterns.extend((str(n), str(entry.get("name", ""))) for n in entry["nets"])
    else:
        raw_patterns = net_settings.get("netclass_patterns", [])
        if isinstance(raw_patterns, list):
            for entry in raw_patterns:
                if isinstance(entry, Mapping) and "pattern" in entry and "netclass" in entry:
                    patterns.append((str(entry["pattern"]), str(entry["netclass"])))

    resolved: list[str]
    if net_names is not None:
        resolved = list(net_names)
    elif board is not None:
        resolved = [n.name for n in board.nets]
    else:
        resolved = []
    net_class: dict[str, str] = {}
    effective: dict[str, NetclassRule] = {}
    implicit: dict[str, NetclassRule] = {}
    for net in resolved:
        if not net:
            continue
        matched: list[NetclassRule] = []
        for cname in assignments.get(net, []):
            hit = by_name.get(cname)
            if hit is None and cname and cname != DEFAULT_CLASS:
                hit = implicit.setdefault(cname, _implicit_class(cname, default))
            if hit is not None and hit not in matched:
                matched.append(hit)
        for pattern, cname in patterns:
            hit = by_name.get(cname)
            if hit is not None and hit not in matched and netclass_pattern_matches(pattern, net):
                matched.append(hit)
        if matched:
            eff = effective_class(matched, default)
            effective.setdefault(eff.name, eff)
            net_class[net] = eff.name
    others = [c for c in [*others, *implicit.values()] if c.name not in effective] + list(effective.values())

    design = doc.get("board", {}).get("design_settings", {}) if isinstance(doc, Mapping) else {}
    rules_json = design.get("rules", {}) if isinstance(design, Mapping) else {}
    if not isinstance(rules_json, Mapping):
        rules_json = {}
    min_via = rules_json.get("min_via_diameter")
    min_clr = rules_json.get("min_clearance")
    return DsnRules(
        default=default,
        classes=tuple(others),
        net_class=net_class,
        hole_clearance=_num(rules_json.get("min_hole_clearance"), _KICAD_DEFAULT_HOLE_CLEARANCE),
        copper_edge_clearance=_num(
            rules_json.get("min_copper_edge_clearance"), _KICAD_DEFAULT_COPPER_EDGE_CLEARANCE
        ),
        max_error=_num(rules_json.get("max_error"), _KICAD_DEFAULT_MAX_ERROR),
        min_track_width=_num(rules_json.get("min_track_width"), _KICAD_DEFAULT_MIN_TRACK_WIDTH),
        min_via_diameter=_num(min_via, 0.0) if min_via is not None else None,
        min_clearance=_num(min_clr, 0.0) if min_clr is not None else None,
    )
