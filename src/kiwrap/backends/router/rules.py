"""`RouteRules`: the Freerouting settings derived from the board's own rules.

Why each field exists (docs/VERIFICATION.md claim 8, docs/research/04-autorouting.md):

* `fanout` - Freerouting 2.4.1 runs a fanout pre-pass by default; it produced the 0.15 mm
  segments that fail KiCad 10's 0.2 mm default `min_track_width` (27/26 `track_width` errors on
  two 30-net boards). `--router.fanout.enabled=false` alone gave 0 violations; disabling
  `automatic_neckdown` was neither necessary nor sufficient. Fanout stays ON only when the board
  has a BGA (the pre-pass exists for BGA escapes).
* `copper_to_edge_mm` - KiCad's DSN export omits the copper-to-edge clearance (Freerouting #558)
  and Freerouting substitutes 500 um; the board's `min_copper_edge_clearance` is passed as
  `--router.copper_to_edge_clearance_um` so KiCad's edge-clearance DRC agrees afterwards.
* `min_track_width_mm` / `min_clearance_mm` / `min_via_*` - the .kicad_pro `design_settings.rules`
  minimums (`min_track_width`, `min_clearance`, `min_via_diameter`, `min_through_hole_diameter`),
  with `rules.default_min_track_width_mm` of the capability record (0.2 on 10, 0 on 9) standing
  in when the project has none. They are reported in the receipt and used by the DSN
  post-processor's sanity notes; the DSN rules themselves come from KiCad's export.
* `plane_nets` - nets with a filled copper zone: KiCad exports zones as `(plane …)`, so the
  router treats their pins as connected through the plane. `ignore_net_classes` lists the
  netclasses whose EVERY net is a plane net (`-inc`): a class that also holds signal nets is
  never ignored.
* `passes` / `timeout_s` defaults, `via_costs` untouched (Freerouting's default).

`rules_from_board()` reads a `kiwrap.formats.board.BoardFile` (its `design_rules.project_rules`
is the .kicad_pro rules object; `footprints[].fpid`/`pads`; `zones[]`) but only through
attribute access with defaults, so any object shaped like it (IPC reader records, fakes) works.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_COPPER_TO_EDGE_MM = 0.5  # Freerouting's own default (DefaultSettings.java) when the board has no rule
DEFAULT_MIN_CLEARANCE_MM = 0.2
DEFAULT_MIN_VIA_DIAMETER_MM = 0.4
DEFAULT_MIN_VIA_DRILL_MM = 0.3
BGA_MIN_GRID_PADS = 36  # an SMD-only footprint with at least this many pads in a >= 4x4 grid counts as a BGA
_BGA_RE = re.compile(r"\bBGA\b|_BGA|BGA-|BGA_|LGA-\d{3,}|CSP", re.IGNORECASE)


@dataclass
class RouteRules:
    """Everything the router needs to know about the board (millimetres)."""

    min_track_width_mm: float = 0.2
    min_clearance_mm: float = DEFAULT_MIN_CLEARANCE_MM
    min_via_diameter_mm: float = DEFAULT_MIN_VIA_DIAMETER_MM
    min_via_drill_mm: float = DEFAULT_MIN_VIA_DRILL_MM
    copper_to_edge_mm: float = DEFAULT_COPPER_TO_EDGE_MM
    copper_to_edge_from_board: bool = False  # False: Freerouting's default stood in (no board rule)
    fanout: bool = False
    has_bga: bool = False
    bga_refs: list[str] = field(default_factory=list)
    plane_nets: list[str] = field(default_factory=list)
    ignore_net_classes: list[str] = field(default_factory=list)
    copper_layers: int = 2
    automatic_neckdown: bool = False  # harmless either way (claim 8); off keeps widths at the rule
    notes: list[str] = field(default_factory=list)

    @property
    def copper_to_edge_um(self) -> int:
        return int(round(self.copper_to_edge_mm * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_track_width_mm": self.min_track_width_mm,
            "min_clearance_mm": self.min_clearance_mm,
            "min_via_diameter_mm": self.min_via_diameter_mm,
            "min_via_drill_mm": self.min_via_drill_mm,
            "copper_to_edge_mm": self.copper_to_edge_mm,
            "copper_to_edge_from_board": self.copper_to_edge_from_board,
            "fanout": self.fanout,
            "has_bga": self.has_bga,
            "bga_refs": list(self.bga_refs),
            "plane_nets": list(self.plane_nets),
            "ignore_net_classes": list(self.ignore_net_classes),
            "copper_layers": self.copper_layers,
            "automatic_neckdown": self.automatic_neckdown,
            "notes": list(self.notes),
        }

    def cli_settings(self) -> list[str]:
        """The `--section.key=value` settings this rule set turns into (the flag names verified
        against the 2.4.1 jar's settings classes: router.fanout.enabled,
        router.copper_to_edge_clearance_um, router.automatic_neckdown)."""
        return [
            f"--router.fanout.enabled={'true' if self.fanout else 'false'}",
            f"--router.copper_to_edge_clearance_um={self.copper_to_edge_um}",
            f"--router.automatic_neckdown={'true' if self.automatic_neckdown else 'false'}",
        ]


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _project_rules(board: Any) -> dict[str, Any]:
    """The .kicad_pro `design_settings.rules` object the board reader attached (or a
    `project_rules` / `rules` mapping on a record), else {}."""
    dr = getattr(board, "design_rules", None)
    if isinstance(dr, Mapping):
        pr = dr.get("project_rules") or dr
        return dict(pr) if isinstance(pr, Mapping) else {}
    pr = getattr(dr, "project_rules", None)
    if isinstance(pr, Mapping):
        return dict(pr)
    pr = getattr(board, "project_rules", None)
    return dict(pr) if isinstance(pr, Mapping) else {}


def _cap(caps: Any, key: str, default: float) -> float:
    try:
        val = caps[key] if caps is not None else None
    except (KeyError, TypeError):
        val = None
    num = _num(val)
    return default if num is None else num


def _net_name(net: Any) -> str:
    if net is None:
        return ""
    if isinstance(net, str):
        return net
    return str(getattr(net, "name", "") or "")


def _is_grid(pads: Iterable[Any]) -> bool:
    """>= 4 distinct x and >= 4 distinct y positions among SMD pads (a pad array, not a QFP ring)."""
    xs: set[float] = set()
    ys: set[float] = set()
    for pad in pads:
        pos = getattr(pad, "position", None)
        x = getattr(pos, "x", None)
        y = getattr(pos, "y", None)
        if x is None and isinstance(pos, (tuple, list)) and len(pos) == 2:
            x, y = pos
        if x is None or y is None:
            continue
        xs.add(round(float(x), 2))
        ys.add(round(float(y), 2))
    return len(xs) >= 4 and len(ys) >= 4


def bga_references(board: Any) -> list[str]:
    """Footprints that look like a BGA: the fpid / description says so, or an SMD-only footprint
    with >= `BGA_MIN_GRID_PADS` pads laid out as a grid (>= 4 rows and columns)."""
    out: list[str] = []
    for fp in getattr(board, "footprints", None) or []:
        ref = str(getattr(fp, "reference", None) or getattr(fp, "ref", "") or "")
        text = " ".join(
            str(getattr(fp, attr, "") or "") for attr in ("fpid", "footprint", "description", "value")
        )
        pads = list(getattr(fp, "pads", None) or [])
        smd = [p for p in pads if str(getattr(p, "type", "")) == "smd"]
        if _BGA_RE.search(text) or (
            pads and len(smd) == len(pads) and len(smd) >= BGA_MIN_GRID_PADS and _is_grid(smd)
        ):
            out.append(ref or text[:40])
    return out


def plane_nets(board: Any) -> list[str]:
    """Nets that have a copper zone (a filled pour, never a rule area), sorted."""
    names: set[str] = set()
    for zone in getattr(board, "zones", None) or []:
        if getattr(zone, "is_rule_area", False) or getattr(zone, "keepout", False):
            continue
        name = _net_name(getattr(zone, "net", None))
        if name:
            names.add(name)
    return sorted(names)


def netclass_nets(pro_path: Path | None, nets: Iterable[str]) -> dict[str, list[str]]:
    """`{netclass: [nets]}` resolved the way KiCad's exporter resolves them
    (`kiwrap.formats.specctra.rules.rules_from_project`: explicit `netclass_assignments`, then
    the `netclass_patterns` as anchored regular expressions / wildcards, lowest priority value
    first, with the `net_settings.meta.version` migrations replayed - a project below schema 3
    loses its patterns exactly like KiCad drops them); nets in no explicit class are `Default`.
    This keeps the `-inc` classes in step with the classes the DSN carries."""
    from kiwrap.formats.specctra.rules import rules_from_project

    all_nets = [n for n in nets if n]
    if not all_nets:
        return {}
    path = Path(pro_path) if pro_path is not None else None
    if path is None or not path.is_file():
        return {"Default": list(all_nets)}
    try:
        rules = rules_from_project(project=path, net_names=all_nets)
    except (OSError, ValueError):
        return {"Default": list(all_nets)}
    out: dict[str, list[str]] = {}
    for net in all_nets:
        out.setdefault(rules.class_of(net).name, []).append(net)
    return out


def rules_from_board(board: Any, caps: Any = None, *, pro_path: Path | None = None) -> RouteRules:
    """Derive the router settings from a board model (see the module docstring). `pro_path`
    defaults to the `.kicad_pro` next to `board.path`; it supplies the netclass patterns."""
    pr = _project_rules(board)
    default_min_track = _cap(caps, "rules.default_min_track_width_mm", 0.2)
    rules = RouteRules()
    rules.min_track_width_mm = _num(pr.get("min_track_width")) or default_min_track
    rules.min_clearance_mm = _num(pr.get("min_clearance")) or DEFAULT_MIN_CLEARANCE_MM
    rules.min_via_diameter_mm = _num(pr.get("min_via_diameter")) or DEFAULT_MIN_VIA_DIAMETER_MM
    rules.min_via_drill_mm = (
        _num(pr.get("min_through_hole_diameter")) or _num(pr.get("min_via_drill")) or DEFAULT_MIN_VIA_DRILL_MM
    )
    edge = _num(pr.get("min_copper_edge_clearance"))
    if edge is None:
        edge = _num(pr.get("copper_edge_clearance"))
    if edge is not None:
        rules.copper_to_edge_mm = edge
        rules.copper_to_edge_from_board = True
    else:
        rules.notes.append(
            "the board has no copper-to-edge rule; Freerouting's 0.5 mm default is used "
            "(KiCad's DSN export omits it either way)"
        )
    layers = getattr(board, "copper_layers", None)
    if callable(layers):
        layers = layers()
    if isinstance(layers, (list, tuple)):
        rules.copper_layers = len(layers) or 2
    elif isinstance(layers, int):
        rules.copper_layers = layers
    rules.bga_refs = bga_references(board)
    rules.has_bga = bool(rules.bga_refs)
    rules.fanout = rules.has_bga
    if rules.has_bga:
        rules.notes.append(
            "BGA footprint(s) present (" + ", ".join(rules.bga_refs[:5]) + "): the fanout pre-pass stays on; "
            "its escape segments may be narrower than min_track_width - check the DRC"
        )
    rules.plane_nets = plane_nets(board)
    if pro_path is None:
        path = getattr(board, "path", None)
        if path is not None:
            pro_path = Path(path).with_suffix(".kicad_pro")
    net_names = [_net_name(n) for n in (getattr(board, "nets", None) or [])]
    if rules.plane_nets and net_names:
        classes = netclass_nets(pro_path, net_names)
        planes = set(rules.plane_nets)
        rules.ignore_net_classes = sorted(
            cls for cls, members in classes.items() if members and all(n in planes for n in members)
        )
        if rules.ignore_net_classes:
            rules.notes.append(
                "netclass(es) made only of plane nets are skipped by the router (-inc): "
                + ", ".join(rules.ignore_net_classes)
            )
    return rules


__all__ = [
    "BGA_MIN_GRID_PADS",
    "DEFAULT_COPPER_TO_EDGE_MM",
    "RouteRules",
    "bga_references",
    "netclass_nets",
    "plane_nets",
    "rules_from_board",
]
