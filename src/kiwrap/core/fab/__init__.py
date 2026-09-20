"""Fabrication profiles, BOM and position-file post-processing (docs/DECISIONS.md D9: JLCPCB is
the default profile; docs/ARCHITECTURE.md §10 `kw_fab_export` / `kw_fab_bom`).

A `FabProfile` owns everything a vendor expects from the fabrication bundle: the kicad-cli
options per target (gerbers / drill / pos / bom), the file names, the position-file rotation
corrections and the BOM columns. `kw_fab_export(profile=...)` and `kw_fab_bom(profile=...)` ask
the profile; `kw_pcb_setup(preset=...)` takes the capability rules a profile publishes.

Public surface:
    get_profile(name) -> FabProfile          "generic" | "jlcpcb"
    PROFILE_NAMES                            the registry keys
    preset_rules(name) -> RulesPreset        "jlcpcb-2layer" | "jlcpcb-4layer"
    PRESET_NAMES
    build_bom(...) / BomRow / bom_csv        kiwrap.core.fab.bom
    correct_positions(...) / RotationTable   kiwrap.core.fab.position
"""

from __future__ import annotations

from kiwrap.core.fab.bom import BomRow, BomTable, bom_csv, bom_from_cli_csv, bom_from_netlist
from kiwrap.core.fab.position import PositionCorrection, RotationRule, RotationTable, correct_positions
from kiwrap.core.fab.profiles import (
    PRESET_NAMES,
    PROFILE_NAMES,
    FabProfile,
    RulesPreset,
    get_profile,
    preset_rules,
)

__all__ = [
    "PRESET_NAMES",
    "PROFILE_NAMES",
    "BomRow",
    "BomTable",
    "FabProfile",
    "PositionCorrection",
    "RotationRule",
    "RotationTable",
    "RulesPreset",
    "bom_csv",
    "bom_from_cli_csv",
    "bom_from_netlist",
    "correct_positions",
    "get_profile",
    "preset_rules",
]
