"""`sch export netlist --format kicadsexpr` adapter.

Verified facts: `sch export netlist` exists on 7.0.6-10.0.0 with `--format kicadsexpr` as the
default and `-o` an output FILE (spikes/versioning-strategy/clihelp/*__sch_export_netlist.txt,
cli_flag_drift.txt: only `--variant` was added in 10.0). The exported netlist is the oracle the
T1 round-trip and the intent diff compare against (docs/ARCHITECTURE.md §6, §11).

Parsing: `kiwrap.formats.reports.netlist` owns the parser (verified against every spike sample
from 7.0.6 to 10.0.0); `export_netlist_file` always gives access to the raw text.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from kiwrap.backends.cli.runner import resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities
from kiwrap.formats.reports import Netlist
from kiwrap.formats.reports.netlist import parse_netlist
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

NETLIST_FORMAT = "kicadsexpr"


def export_netlist_file(
    install: KiCadInstallation,
    config: ConfigRoot,
    schematic: Path,
    out: Path | None = None,
    *,
    fmt: str = NETLIST_FORMAT,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> Path:
    """Run `kicad-cli sch export netlist --format <fmt> -o <out> <schematic>` and return `out`."""
    caps = resolve_caps(install, caps)
    schematic = Path(schematic)
    suffix = ".net" if fmt == NETLIST_FORMAT else f".{fmt}.net"
    out = Path(out) if out else schematic.with_name(schematic.stem + suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["sch", "export", "netlist", "--format", fmt, "-o", str(out), str(schematic)]
    run_kicad_cli(install, args, config=config, caps=caps, timeout_s=timeout_s, accept=(0,))
    if not out.is_file():
        raise FileNotFoundError(f"kicad-cli reported success but wrote no netlist at {out}")
    return out


def export_netlist(
    install: KiCadInstallation,
    config: ConfigRoot,
    schematic: Path,
    out: Path | None = None,
    *,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> Netlist:
    """Export the kicadsexpr netlist of `schematic` and parse it into a `Netlist`."""
    path = export_netlist_file(install, config, schematic, out, caps=caps, timeout_s=timeout_s)
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_netlist_text(text, source=str(schematic), kicad_version=install.version)


def parse_netlist_text(text: str, *, source: str, kicad_version: str) -> Netlist:
    """Parse a kicadsexpr netlist with `kiwrap.formats.reports.netlist.parse_netlist`."""
    return parse_netlist(text, source=source, kicad_version=kicad_version)


def netlist_summary(netlist: Netlist) -> Mapping[str, int]:
    """Small counts for receipts: components, nets, nodes."""
    return {
        "components": len(netlist.components),
        "nets": len(netlist.nets),
        "nodes": sum(len(n.nodes) for n in netlist.nets),
    }
