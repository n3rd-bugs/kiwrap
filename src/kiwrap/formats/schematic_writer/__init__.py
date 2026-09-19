"""Schematic authoring engine (file tier, every KiCad version; 7 best-effort).

    SchematicWriter(project_dir, epoch={"sch": …, "sym": …}, lib_resolver=…)
        .load(root_path) / .add_symbol / .add_wire / .add_label / .add_power / .add_no_connect
        / .add_sheet / .edit / .delete / .annotate / .assign_footprints / .pins_of / .save

`FallbackLibResolver` reads `<install>/share/kicad/symbols/*.kicad_sym` (nickname = file stem)
plus sym-lib-table rows for callers without `kiwrap.formats.libs`. The verified rules the engine
encodes are listed in `writer.py`; the per-epoch token spellings in
`kiwrap.formats.epoch_dialects`.
"""

from __future__ import annotations

from .libs import FallbackLibResolver, ResolvedSymbol, flatten_symbol, parse_sym_lib_table
from .model import PinEndpoint, SheetPinEndpoint, SheetPlacement, SymbolPlacement, WriterError
from .writer import SchematicWriter, default_resolver

__all__ = [
    "FallbackLibResolver",
    "PinEndpoint",
    "ResolvedSymbol",
    "SchematicWriter",
    "SheetPinEndpoint",
    "SheetPlacement",
    "SymbolPlacement",
    "WriterError",
    "default_resolver",
    "flatten_symbol",
    "parse_sym_lib_table",
]
