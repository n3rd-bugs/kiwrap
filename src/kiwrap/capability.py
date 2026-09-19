"""Layer 2 — capability record keyed on FULL semver.

A capability record answers "can this install do X, and with which quirk?" so that tools never
branch on a version number. It is seeded from `kiwrap/data/kicad_version_matrix.json` (the
verified matrix from docs/ARCHITECTURE.md §2), then refined by cheap runtime probes (`--help`
trees, presence of python.exe, api schema file), and cached under the storage home per
`KiCadInstallation.key()`.

Key naming: dotted, lower-case, stable. Values are JSON scalars or lists. Unknown key -> `default`.
Documented keys (all of these must exist for every supported version, see the JSON file):
    cli.subcommands                 list[str]   e.g. ["pcb export gerbers", "sch erc", ...]
    cli.flags.<subcommand>          list[str]   long flags parsed from `--help`
    cli.has_erc / cli.has_drc       bool        (>= 8.0)
    cli.has_render                  bool        (>= 9.0)
    cli.has_jobset                  bool        (>= 9.0)
    cli.has_sch_pcb_upgrade         bool        (>= 10.0)
    cli.drc_refill_flags            bool        `pcb drc --refill-zones --save-board` (>= 10.0)
    cli.erc_json_pos_scale          number      100 on 8.0.1-10.0.x (ERC JSON positions are x100 too small)
    cli.gerbers_creates_outdir      bool        (>= 9.0)
    cli.writes_prl_on_read          bool        (>= 10.0)
    cli.help_exit_code              int         exit status of `--help` (non-zero on 7/8)
    cli.sch_stamp_gate_needs_generator_version  bool  (8-10)
    swig.available                  bool        (7-10; removed in 11)
    swig.shim                       str         "v7" | "v8plus" | ""
    swig.ses_import_headless        bool        (>= 8.0)
    swig.write_drc_report_ok        bool        (7-9 with shim; false on 10)
    swig.needs_newboard_for_fill    bool        ZONE_FILLER on BOARD() crashes (7-9)
    ipc.available                   bool        (>= 9.0)
    ipc.headless                    bool        (>= 11)
    ipc.schematic                   bool        (>= 11)
    ipc.schematic_write_crashes     bool        9.0.0-10.0.6 (#24966)
    ipc.commit_needs_document       bool        (>= 10.0.7)
    ipc.single_fixed_pipe           bool        Windows 9/10
    formats.sch / formats.pcb / formats.sym / formats.pro  int  epoch stamps written by this version
    libs.ships_wrl                  bool        (<= 9)
    rules.default_min_track_width_mm number     0.2 on 10, 0 on 9
    setup.wizard_needs_three_tables bool        (10.0)
    setup.prompt_flags              list[str]   do_not_show_again keys that must be true
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiwrap.installation import KiCadInstallation


class Capabilities(Mapping[str, Any]):
    """Read-only view over the merged (seed + probe) record for one install."""

    def __init__(self, install: KiCadInstallation, values: dict[str, Any], *, probed: bool):
        self._install, self._values, self.probed = install, dict(values), probed

    @property
    def install(self) -> KiCadInstallation:
        return self._install

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def has(self, key: str) -> bool:
        """True iff key exists and is truthy."""
        return bool(self._values.get(key))

    def flags(self, subcommand: str) -> list[str]:
        return list(self._values.get(f"cli.flags.{subcommand}", []))

    def to_json(self) -> dict[str, Any]:
        return {"install": self._install.key(), "version": self._install.version, "probed": self.probed, "values": self._values}


def capabilities_for(install: KiCadInstallation, *, refresh: bool = False) -> Capabilities:
    """Seed from the matrix (nearest known version <= this one, same major), probe, cache, return."""
    raise NotImplementedError  # implemented by the installation/capability component


class Unsupported(RuntimeError):
    """Raised by backends when a capability is missing. Tools turn it into E_UNSUPPORTED."""

    def __init__(self, capability: str, install: KiCadInstallation, *, oldest_kicad_with_it: str | None, fallback: str | None):
        self.capability, self.install = capability, install
        self.oldest_kicad_with_it, self.fallback = oldest_kicad_with_it, fallback
        msg = f"{capability} is not available on KiCad {install.version}"
        if oldest_kicad_with_it:
            msg += f" (first available in {oldest_kicad_with_it})"
        if fallback:
            msg += f"; fallback: {fallback}"
        super().__init__(msg)
