"""`sch erc` adapter: runs kicad-cli ERC with `--format json` and normalises the result.

Verified facts encoded here (docs/VERIFICATION.md):
- claim 1: on kicad-cli 8.0.1 / 9.0.5 / 10.0.0 (and still on 10.0.6 and master) ERC JSON item
  positions are exactly 100x too small (the JSON path uses pcbIUScale for schematic IUs), for
  `--units mm` and `--units in` alike, and the item `description` strings carry the same error
  ("length 0.2032 mm" for a 20.32 mm wire). DRC JSON is correct. The factor is the capability
  `cli.erc_json_pos_scale`; it is handed to `kiwrap.formats.reports.erc.parse_erc_json`, which
  multiplies positions AND the "N.NNNN mm" dimensions inside descriptions (never the "2.54mm" of a
  library name) so `Report` consumers only ever see millimetres.
- matrix row "`sch erc --format json`": absent on 7.0.6 -> `Unsupported(oldest='8.0.0')`.
- `--exit-code-violations` -> exit 5 means "violations found", not failure (`--severity-all`
  makes warnings count too); the exit code is mapped into the `Report` and never raised.
- ERC type names differ across versions (8: `label_dangling`; 9: `global_label_dangling`;
  10: `label_dangling` + `unconnected_wire_endpoint`), so nothing here matches on them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiwrap.backends.cli.runner import CliResult, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.formats.reports import Report
from kiwrap.formats.reports.erc import parse_erc_json
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

#: kicad-cli's first release with `sch erc` / `pcb drc` (matrix §2; 7.0.6 has neither).
OLDEST_KICAD_WITH_ERC = "8.0.0"
_SEVERITY_FLAGS: Mapping[str, str] = {
    "all": "--severity-all",
    "error": "--severity-error",
    "warning": "--severity-warning",
    "exclusion": "--severity-exclusions",
    "exclusions": "--severity-exclusions",
}


def severity_flags(severity: str) -> list[str]:
    """Map "all" | "error" | "warning" | "exclusions" | "error,warning" to kicad-cli flags."""
    flags: list[str] = []
    for part in severity.replace("+", ",").split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in _SEVERITY_FLAGS:
            raise ValueError(f"unknown severity {part!r}; expected one of {sorted(_SEVERITY_FLAGS)}")
        flag = _SEVERITY_FLAGS[key]
        if flag not in flags:
            flags.append(flag)
    return flags or ["--severity-all"]


def report_from_erc_json(doc: dict[str, Any], *, source: str, position_scale: float) -> Report:
    """Normalise an erc.v1 document (already parsed) into a `Report` with positions in mm.

    `position_scale` is `cli.erc_json_pos_scale` (100 on the kicad-cli releases that have ERC so
    far). The quirk is undone by `kiwrap.formats.reports.erc.parse_erc_json`, which rescales the
    document once (so `Report.raw` is also correct) before parsing it; there is deliberately no
    second rescaler in the backend.
    """
    return parse_erc_json(json.dumps(doc), position_scale=position_scale, source=source)


def run_erc(
    install: KiCadInstallation,
    config: ConfigRoot,
    schematic: Path,
    *,
    severity: str = "all",
    units: str = "mm",
    caps: Capabilities | None = None,
    out_json: Path | None = None,
    timeout_s: float = 300,
    define_vars: Mapping[str, str] | None = None,
) -> Report:
    """Run `kicad-cli sch erc --format json --exit-code-violations` and return a `Report`.

    Positions in the returned report are millimetres regardless of `units` (the JSON is produced in
    `units`, then converted). Exit 5 (violations) is normal and shows up as `report.errors /
    report.warnings`; a schematic that cannot be loaded raises `CliLoadError`.
    """
    caps = resolve_caps(install, caps)
    if not caps.has("cli.has_erc"):
        raise Unsupported(
            "cli.has_erc",
            install,
            oldest_kicad_with_it=OLDEST_KICAD_WITH_ERC,
            fallback="none (KiCad 7 has no headless ERC; open the schematic in eeschema)",
        )
    schematic = Path(schematic)
    out = Path(out_json) if out_json else schematic.with_name(schematic.stem + ".erc.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["sch", "erc", "--format", "json", "--units", units, *severity_flags(severity)]
    args += ["--exit-code-violations", "-o", str(out)]
    for k, v in (define_vars or {}).items():
        args += ["-D", f"{k}={v}"]
    args.append(str(schematic))
    res: CliResult = run_kicad_cli(install, args, config=config, caps=caps, timeout_s=timeout_s)
    text = out.read_text(encoding="utf-8")
    doc = json.loads(text)
    scale = float(caps.get("cli.erc_json_pos_scale", 1) or 1)
    report = report_from_erc_json(doc, source=str(schematic), position_scale=scale)
    report.raw["_cli"] = {
        "returncode": res.returncode,
        "exit_code_violations": res.violations,
        "path": str(out),
    }
    return report
