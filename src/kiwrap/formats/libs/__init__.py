"""Library resolution and search for the file tier (zero third-party dependencies; sqlite3 from
the stdlib for the search index only).

    LibResolver(install, config_root, project_dir)   tables -> .kicad_sym / .pretty -> models
        .symbol("Device:R") -> LibSymbol (extends flattened, node ready for lib_symbols, pins mm Y-up)
        .footprint("Resistor_SMD:R_0603_1608Metric") -> LibFootprint (pads, courtyard, attributes)
        .tables() -> {"sym": [rows], "fp": [rows]}   (sandbox global tables + project tables, env
                                                    vars, ${KIPRJMOD}, nested Table rows, hidden/disabled)
    LibIndex(install, config_root, storage_home)     sqlite FTS5 over names/descriptions/keywords/
        .build() / .ensure() / .search(kind, query, filters, limit) -> list[LibHit]   filters/pin counts
        .query(...) -> SearchResult (hits + near-misses with the failing filter named)

Sub-modules: `tables` (lib-table parsing + variable expansion), `symbols` (`.kicad_sym`,
`flatten`), `footprints` (`.kicad_mod`), `resolver`, `index`, `models`.
"""

from __future__ import annotations

from kiwrap.formats.libs.footprints import load_footprint, parse_footprint
from kiwrap.formats.libs.index import (
    BuildReport,
    LibIndex,
    SearchResult,
    filter_regex,
    footprint_filter_matches,
)
from kiwrap.formats.libs.models import (
    LibError,
    LibFootprint,
    LibHit,
    LibPad,
    LibPin,
    LibraryTables,
    LibSymbol,
    LibTableRow,
    NotFound,
    UnsupportedLibrary,
    numbered_pad_count,
    split_lib_id,
)
from kiwrap.formats.libs.resolver import LibResolver
from kiwrap.formats.libs.symbols import SymbolLibrary, flatten, parse_symbol
from kiwrap.formats.libs.tables import expand_uri, library_env, load_tables, parse_table

__all__ = [
    "BuildReport",
    "LibError",
    "LibFootprint",
    "LibHit",
    "LibIndex",
    "LibPad",
    "LibPin",
    "LibResolver",
    "LibSymbol",
    "LibTableRow",
    "LibraryTables",
    "NotFound",
    "SearchResult",
    "SymbolLibrary",
    "UnsupportedLibrary",
    "expand_uri",
    "filter_regex",
    "flatten",
    "footprint_filter_matches",
    "library_env",
    "load_footprint",
    "load_tables",
    "numbered_pad_count",
    "parse_footprint",
    "parse_symbol",
    "parse_table",
    "split_lib_id",
]
