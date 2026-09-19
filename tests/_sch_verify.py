"""KiCad is the judge: run ERC, export the netlist and (10+) a `sch upgrade` re-save on a
writer-produced project and compare against the design's intent (tests/_sch_designs.py)."""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.backends.cli.erc import run_erc
from kiwrap.backends.cli.netlist import export_netlist
from kiwrap.backends.cli.upgrade import upgrade_copy
from kiwrap.capability import Capabilities
from kiwrap.formats.reports import Netlist
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot
from tests._sch_designs import Design

__all__ = ["Verdict", "epoch_for", "netlist_partition", "upgrade_diff", "verify_design"]

#: lines a kicad-cli `sch upgrade` re-save is known to change regardless of the writer: the
#: project name comes from the .kicad_pro (absent in the upgrade scratch dir) and 10+ kicad-cli
#: drops the root `(embedded_fonts no)` the GUI / IPC saves keep.
_KNOWN_RESAVE_NOISE = ("(project ", "(embedded_fonts no)")


@dataclass
class Verdict:
    erc_errors: int | None  # None when the install has no `sch erc`
    erc_warnings: int | None
    erc_types: list[str] = field(default_factory=list)
    netlist_ok: bool = False
    netlist_missing: list[frozenset[tuple[str, str]]] = field(default_factory=list)
    netlist_extra: list[frozenset[tuple[str, str]]] = field(default_factory=list)
    named_ok: dict[str, bool] = field(default_factory=dict)
    components: set[str] = field(default_factory=set)
    upgrade_diff_lines: int | None = None  # None when the install cannot `sch upgrade`
    upgrade_diff: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.erc_errors in (None, 0)) and self.netlist_ok and all(self.named_ok.values())


def epoch_for(caps: Capabilities) -> dict[str, int]:
    """The writer epoch of an install: its `formats.sch` / `formats.sym` / `formats.pro` stamps."""
    epoch = {"sch": int(caps["formats.sch"]), "sym": int(caps["formats.sym"])}
    if caps.get("formats.pro") is not None:
        epoch["pro"] = int(caps["formats.pro"])
    return epoch


def netlist_partition(netlist: Netlist) -> list[frozenset[tuple[str, str]]]:
    """Nets as sets of (ref, pin), power symbols / flags excluded, single-node nets kept."""
    out = []
    for net in netlist.nets:
        nodes = frozenset((n.ref, n.pin) for n in net.nodes if not n.ref.startswith("#"))
        if nodes:
            out.append(nodes)
    return out


def upgrade_diff(
    install: KiCadInstallation, config: ConfigRoot, caps: Capabilities, sch: Path, scratch: Path
) -> list[str]:
    """Unified diff (0 context) between the writer's file and kicad-cli's `sch upgrade` of a
    copy, known re-save noise removed. The .kicad_pro travels with the copy so instance
    `(project …)` names survive."""
    scratch.mkdir(parents=True, exist_ok=True)
    for extra in sch.parent.glob("*.kicad_pro"):
        shutil.copy2(extra, scratch / extra.name)
    for other in sch.parent.glob("*.kicad_sch"):
        if other != sch:
            shutil.copy2(other, scratch / other.name)
    up = upgrade_copy(install, config, sch, scratch, caps=caps)
    a = sch.read_bytes().replace(b"\r\n", b"\n").decode("utf-8").splitlines()
    b = up.read_bytes().replace(b"\r\n", b"\n").decode("utf-8").splitlines()
    diff = [
        line
        for line in difflib.unified_diff(a, b, lineterm="", n=0)
        if (line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    ]
    return [line for line in diff if not any(noise in line for noise in _KNOWN_RESAVE_NOISE)]


def verify_design(
    install: KiCadInstallation,
    config: ConfigRoot,
    caps: Capabilities,
    design: Design,
    scratch: Path,
    *,
    upgrade: bool = True,
) -> Verdict:
    sch = design.root
    v = Verdict(erc_errors=None, erc_warnings=None)
    if caps.has("cli.has_erc"):
        report = run_erc(install, config, sch, caps=caps, out_json=scratch / "erc.json")
        v.erc_errors, v.erc_warnings = report.errors, report.warnings
        v.erc_types = sorted({f"{x.severity}:{x.type}@{x.pos}" for x in report.violations})
    netlist = export_netlist(install, config, sch, scratch / "out.net", caps=caps)
    got = netlist_partition(netlist)
    want = list(design.nets)
    v.netlist_missing = [n for n in want if n not in got]
    v.netlist_extra = [n for n in got if n not in want]
    v.netlist_ok = not v.netlist_missing and not v.netlist_extra
    v.components = {c.ref for c in netlist.components}
    by_name = {
        n.name: frozenset((x.ref, x.pin) for x in n.nodes if not x.ref.startswith("#")) for n in netlist.nets
    }
    for name, nodes in design.named.items():
        v.named_ok[name] = by_name.get(name) == nodes
    if upgrade and caps.has("cli.has_sch_pcb_upgrade"):
        v.upgrade_diff = upgrade_diff(install, config, caps, sch, scratch / "upgrade")
        v.upgrade_diff_lines = len(v.upgrade_diff)
    return v
