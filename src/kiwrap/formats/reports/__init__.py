"""Normalised ERC/DRC report and netlist models — the shapes every tool returns.

Parsers live in `erc.py`, `drc.py`, `netlist.py`. They take the raw kicad-cli output plus the
quirk parameters the caller read from the capability record (e.g. `position_scale`); they never
import anything outside `kiwrap.formats`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITIES = ("error", "warning", "exclusion", "ignore")


@dataclass(frozen=True)
class Position:
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class ViolationItem:
    uuid: str
    description: str
    pos: Position | None


@dataclass(frozen=True)
class Violation:
    type: str  # kicad's type id, e.g. "clearance", "pin_not_connected"
    severity: str  # one of SEVERITIES
    description: str  # human text, positions already rescaled to mm where the source was wrong
    items: tuple[ViolationItem, ...]
    sheet: str | None = None  # ERC: sheet path; DRC: None
    excluded: bool = False

    @property
    def pos(self) -> Position | None:
        return self.items[0].pos if self.items and self.items[0].pos else None


@dataclass(frozen=True)
class Report:
    kind: str  # "erc" | "drc"
    source: str  # file the check ran on
    kicad_version: str
    violations: tuple[Violation, ...]
    unconnected: tuple[Violation, ...] = ()  # DRC only
    schematic_parity: tuple[Violation, ...] = ()  # DRC only (--schematic-parity)
    raw: dict = field(default_factory=dict, repr=False)  # the parsed JSON for anything not modelled

    def count(self, severity: str, *, include_unconnected: bool = True) -> int:
        n = sum(1 for v in self.violations if v.severity == severity and not v.excluded)
        if include_unconnected:
            n += sum(1 for v in self.unconnected if v.severity == severity and not v.excluded)
        return n

    @property
    def errors(self) -> int:
        return self.count("error")

    @property
    def warnings(self) -> int:
        return self.count("warning")

    @property
    def ok(self) -> bool:
        """The gate definition: zero errors. Warnings are reported, never gated."""
        return self.errors == 0


@dataclass(frozen=True)
class NetNode:
    ref: str
    pin: str
    pin_function: str | None = None
    pin_type: str | None = None


@dataclass(frozen=True)
class Net:
    code: int
    name: str
    nodes: tuple[NetNode, ...]


@dataclass(frozen=True)
class Component:
    ref: str
    value: str
    footprint: str | None
    lib_id: str | None
    sheet_path: str | None
    properties: dict[str, str] = field(default_factory=dict)
    dnp: bool = False
    exclude_from_board: bool = False


@dataclass(frozen=True)
class Netlist:
    source: str
    kicad_version: str
    components: tuple[Component, ...]
    nets: tuple[Net, ...]

    def net_by_name(self) -> dict[str, Net]:
        return {n.name: n for n in self.nets}

    def component(self, ref: str) -> Component | None:
        return next((c for c in self.components if c.ref == ref), None)
