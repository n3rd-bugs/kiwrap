"""`pcb drc` adapter: runs kicad-cli DRC with `--format json` and normalises the result.

Verified facts encoded here (docs/VERIFICATION.md / docs/ARCHITECTURE.md §2):
- claim 1: DRC JSON positions are correct millimetres on 8.0.1 / 9.0.5 / 10.0.0 (both the text and
  JSON paths use pcbIUScale), so no rescale is applied — only the `coordinate_units` conversion.
- matrix row "`pcb drc --format json`": absent on 7.0.6 -> `Unsupported(oldest='8.0.0')` with the
  SWIG `WriteDRCReport` shim as fallback.
- `--refill-zones --save-board` exist only where `cli.drc_refill_flags` (10.0+; claim 8 notes
  9.0.5 lacks them) -> otherwise `Unsupported` with fallback "swig ZONE_FILLER". kicad-cli does
  not refill zones by itself, so a board with stale fills reports phantom clearance errors.
- `--exit-code-violations` -> exit 5 when violations exist (with `--severity-all` warnings count
  too); mapped into the `Report`, never raised. Load failures exit 3 on 9/10 and -1/0xFFFFFFFF/127
  on 7/8 (claim 9) -> `CliLoadError` from the runner.
- The DRC engine CAPS every violation type: `DRC_ENGINE::RunTests` sets `m_errorLimits[type]` to
  ERROR_LIMIT (199) or, for `clearance` / `unconnected_items`, EXTENDED_ERROR_LIMIT (499), and
  `ReportViolation` drops further items of that type (pcbnew/drc/drc_engine.cpp; verified
  2026-09-19 on 10.0.6 and the nightly: 203 missing courtyards -> 199 in the JSON, a board with
  634 airwires -> exactly 499 unconnected_items). `capped_types()` names the saturated types so
  kw_pcb_drc can say the counts are lower bounds (matrix keys `cli.drc_error_limit`,
  `cli.drc_extended_error_limit`, `cli.drc_extended_error_types`).
- `--all-track-errors` (review item 19, §5 claim 12): without it each track stops at its FIRST
  violation (`drc_test_provider_copper_clearance.cpp:289,304,668`) and which one is reported
  depends on the R-tree and worker threads; the review reproduced 12 violations by default against
  18 with the flag, including a short the default hid. Listed in `cli.flags.pcb drc` from 8.0
  (matrix); `supports_all_track_errors()` gates it and `run_drc` refuses it where it is absent.
- Where the JSON goes: `out_json` (kw_pcb_drc passes a path under the storage home, review item 3
  / D23 - never the project); the default beside the board stays for direct callers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiwrap.backends.cli.erc import OLDEST_KICAD_WITH_ERC, severity_flags
from kiwrap.backends.cli.runner import CliResult, has_flag, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.formats.reports import Report
from kiwrap.formats.reports.drc import parse_drc_json
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

OLDEST_KICAD_WITH_DRC = OLDEST_KICAD_WITH_ERC  # both arrived in 8.0 (matrix §2)
OLDEST_KICAD_WITH_DRC_REFILL = "10.0.0"
OLDEST_KICAD_WITH_ALL_TRACK_ERRORS = "8.0.0"


def supports_all_track_errors(caps: Capabilities) -> bool:
    """True when this kicad-cli's `pcb drc` takes `--all-track-errors` (module docstring)."""
    return bool(caps.has("cli.has_drc")) and has_flag(caps, "pcb drc", "--all-track-errors")


def report_from_drc_json(doc: dict[str, Any], *, source: str) -> Report:
    """Normalise a drc.v1 document into a `Report` (violations, unconnected_items, schematic_parity)
    via `kiwrap.formats.reports.drc.parse_drc_json`; no position scale because DRC JSON is already
    millimetres (claim 1)."""
    return parse_drc_json(json.dumps(doc), position_scale=1.0, source=source)


#: DRC_ENGINE defaults, used when a capability record carries no limit keys
DEFAULT_ERROR_LIMIT = 199
DEFAULT_EXTENDED_ERROR_LIMIT = 499
DEFAULT_EXTENDED_ERROR_TYPES = ("clearance", "unconnected_items")


def capped_types(report: Report, caps: Capabilities | None = None) -> list[str]:
    """Violation types whose count reached KiCad's per-type reporting limit (module docstring), so
    their count in `report` is a lower bound. Excluded markers count: the engine decrements the
    limit before the exclusion is looked at."""
    limit = int(_cap_value(caps, "cli.drc_error_limit", DEFAULT_ERROR_LIMIT))
    extended = int(_cap_value(caps, "cli.drc_extended_error_limit", DEFAULT_EXTENDED_ERROR_LIMIT))
    extended_types = set(_cap_value(caps, "cli.drc_extended_error_types", DEFAULT_EXTENDED_ERROR_TYPES))
    counts: dict[str, int] = {}
    for v in (*report.violations, *report.unconnected, *report.schematic_parity):
        counts[v.type] = counts.get(v.type, 0) + 1
    return sorted(t for t, n in counts.items() if n >= (extended if t in extended_types else limit) > 0)


def _cap_value(caps: Capabilities | None, key: str, default: Any) -> Any:
    if caps is None:
        return default
    value = caps.get(key) if hasattr(caps, "get") else None
    return default if value is None else value


def run_drc(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    schematic_parity: bool = False,
    refill_zones: bool = False,
    save_board: bool = False,
    severity: str = "all",
    units: str = "mm",
    all_track_errors: bool = False,
    caps: Capabilities | None = None,
    out_json: Path | None = None,
    timeout_s: float = 600,
    define_vars: Mapping[str, str] | None = None,
) -> Report:
    """Run `kicad-cli pcb drc --format json --exit-code-violations` and return a `Report`.

    `refill_zones` / `save_board` need `cli.drc_refill_flags`; `save_board` mutates `board` in
    place (KiCad refills and re-saves), so callers snapshot first. `schematic_parity` needs the
    project's schematic next to the board (kicad-cli resolves it from the `.kicad_pro`).
    `all_track_errors` needs `supports_all_track_errors(caps)` (else `Unsupported`).
    """
    caps = resolve_caps(install, caps)
    if not caps.has("cli.has_drc"):
        raise Unsupported(
            "cli.has_drc",
            install,
            oldest_kicad_with_it=OLDEST_KICAD_WITH_DRC,
            fallback="swig WriteDRCReport (with the wx assert shim)",
        )
    if save_board and not refill_zones:
        raise ValueError(
            "save_board=True requires refill_zones=True (kicad-cli: --save-board must be used with --refill-zones)"
        )
    if refill_zones and not (
        caps.has("cli.drc_refill_flags") and has_flag(caps, "pcb drc", "--refill-zones")
    ):
        raise Unsupported(
            "cli.drc_refill_flags",
            install,
            oldest_kicad_with_it=OLDEST_KICAD_WITH_DRC_REFILL,
            fallback="swig ZONE_FILLER",
        )
    if all_track_errors and not supports_all_track_errors(caps):
        raise Unsupported(
            "cli.flags.pcb drc --all-track-errors",
            install,
            oldest_kicad_with_it=OLDEST_KICAD_WITH_ALL_TRACK_ERRORS,
            fallback="run without all_track_errors (each track reports its first violation only)",
        )
    board = Path(board)
    out = Path(out_json) if out_json else board.with_name(board.stem + ".drc.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["pcb", "drc", "--format", "json", "--units", units, *severity_flags(severity)]
    args.append("--exit-code-violations")
    if schematic_parity:
        args.append("--schematic-parity")
    if all_track_errors:
        args.append("--all-track-errors")
    if refill_zones:
        args.append("--refill-zones")
    if save_board:
        args.append("--save-board")
    args += ["-o", str(out)]
    for k, v in (define_vars or {}).items():
        args += ["-D", f"{k}={v}"]
    args.append(str(board))
    res: CliResult = run_kicad_cli(install, args, config=config, caps=caps, timeout_s=timeout_s)
    doc = json.loads(out.read_text(encoding="utf-8"))
    report = report_from_drc_json(doc, source=str(board))
    report.raw["_cli"] = {
        "returncode": res.returncode,
        "exit_code_violations": res.violations,
        "path": str(out),
        "board_saved": bool(save_board),
        "all_track_errors": bool(all_track_errors),
        "capped": capped_types(report, caps),
    }
    return report
