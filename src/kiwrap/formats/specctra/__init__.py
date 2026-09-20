"""Specctra DSN/SES codec — the SWIG-free, stock-KiCad-11 path of the routing loop.

`DsnWriter` turns a `kiwrap.formats.board.BoardFile` into the DSN KiCad's own
`ExportSpecctraDSN` would write (same layers, boundary, keepouts, planes, padstacks, images,
placement, network, classes and wiring conventions; `dsn_writer` docstring), `SesReader`
parses a router session and applies it through `kiwrap.formats.board_writer.BoardWriter` with
KiCad's `ImportSpecctraSES` semantics (`ses_reader` docstring), `rules_from_project` resolves
the netclasses of the `.kicad_pro`, and `parse_dsn` / `compare_dsn` give a semantic model and
a normalised diff of any DSN (what conformance test T7 uses against KiCad's exporter).

Zero dependencies beyond `kiwrap.formats` (import-linter contract).
"""

from __future__ import annotations

from kiwrap.formats.specctra.dsn_reader import DsnDiff, DsnDocument, compare_dsn, load_specctra, parse_dsn
from kiwrap.formats.specctra.dsn_writer import (
    IGNORE_CLASS,
    DsnWriter,
    SpecctraError,
    copper_stack,
    via_padstack_id,
    write_dsn,
)
from kiwrap.formats.specctra.rules import DEFAULT_CLASS, DsnRules, NetclassRule, rules_from_project
from kiwrap.formats.specctra.ses_reader import (
    SesPlace,
    SesReader,
    SesReport,
    SesVia,
    SesWire,
    drill_from_padstack_id,
    session_scale,
)

__all__ = [
    "DEFAULT_CLASS",
    "IGNORE_CLASS",
    "DsnDiff",
    "DsnDocument",
    "DsnRules",
    "DsnWriter",
    "NetclassRule",
    "SesPlace",
    "SesReader",
    "SesReport",
    "SesVia",
    "SesWire",
    "SpecctraError",
    "compare_dsn",
    "copper_stack",
    "drill_from_padstack_id",
    "load_specctra",
    "parse_dsn",
    "rules_from_project",
    "session_scale",
    "via_padstack_id",
    "write_dsn",
]
