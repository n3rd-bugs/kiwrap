"""Grader for the D5 reference board (docs/DECISIONS.md D5 acceptance, docs/ARCHITECTURE.md
section 9 "Evals": graded only by KiCad outputs).

    ERC errors == 0            kw_sch_check (kicad-cli sch erc)
    intent diff empty          KiCad's netlist (kw_sch_get kinds=[netlist]) vs design_plan.json `intent`
    unrouted == 0              kw_pcb_check (KiCad's DRC unconnected items, else the file ratsnest)
    must_fix == 0              kw_pcb_drc (kicad-cli pcb drc, classified)
    fab bundle present         gerbers + drill + pos (+ bom) under <project>/fab
    + counts                   components, nets, footprints, tracks, vias, zones, tool calls

Two entry points:

- `grade(facts)` is pure: `Facts` come from the scripted conversation's receipts
  (tests/test_e2e_reference_board.py) or from `collect_facts()`.
- `collect_facts(project_dir)` asks a kiwrap server in-process (`kiwrap.core.server.build_server`,
  no MCP transport) for the same numbers, so an agent-built project (runner_claude.py) is graded
  with exactly the validators the tools expose. `python -m evals.reference_board.grader <project>`
  prints the grade as JSON and exits 1 when it fails.

The intent diff compares connectivity, not net names, for unlabelled nets: the plan lists them as
unnamed pin groups (`intent.unnamed`), and KiCad's `Net-(R3-Pad1)` style names are matched by
their pin sets. Named nets (power symbols, labels) must carry the plan's name on the board.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.reference_board.plan import PLAN_PATH, load_plan

Node = tuple[str, str]


# ---- intent diff ------------------------------------------------------------------------------------


@dataclass
class IntentDiff:
    """Everything KiCad's netlist got wrong against the plan. Empty lists = the intent is met."""

    missing: list[str] = field(default_factory=list)  # a pin the plan puts on a net is not there
    extra: list[str] = field(default_factory=list)  # a pin on a plan net the plan does not list
    misnamed: list[str] = field(default_factory=list)  # the pin group exists under another name
    unexpected_nets: list[str] = field(default_factory=list)  # board nets with pins the plan never mentions
    connected_nc: list[str] = field(default_factory=list)  # a pin the plan marks no-connect has a net

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.misnamed or self.unexpected_nets or self.connected_nc)

    @property
    def count(self) -> int:
        return (
            len(self.missing)
            + len(self.extra)
            + len(self.misnamed)
            + len(self.unexpected_nets)
            + len(self.connected_nc)
        )

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "count": self.count, **asdict(self)}


def _nodes(items: Iterable[Any]) -> set[Node]:
    out: set[Node] = set()
    for it in items:
        if isinstance(it, Mapping):
            out.add((str(it.get("ref")), str(it.get("pin"))))
        else:
            ref, number = it
            out.add((str(ref), str(number)))
    return out


def _real(nodes: Iterable[Node]) -> set[Node]:
    """Drop power symbols / flags (`#PWR..`, `#FLG..`): the plan lists component pins only."""
    return {n for n in nodes if not n[0].startswith("#")}


def intent_diff(nets: Mapping[str, Iterable[Any]], intent: Mapping[str, Any]) -> IntentDiff:
    """`nets`: board / netlist net name -> nodes (`{"ref", "pin"}` dicts or `(ref, pin)` pairs)."""
    got: dict[str, set[Node]] = {name: _real(_nodes(nodes)) for name, nodes in nets.items()}
    want: dict[str, set[Node]] = {name: _nodes(nodes) for name, nodes in intent.get("nets", {}).items()}
    unnamed: list[set[Node]] = [_nodes(g) for g in intent.get("unnamed", [])]
    nc: set[Node] = _nodes(intent.get("no_connect", []))
    diff = IntentDiff()
    claimed: set[str] = set()
    for name, nodes in want.items():
        have = got.get(name)
        if have is None:
            # the same pins under another name?
            other = next((n for n, g in got.items() if g == nodes and n not in claimed), None)
            if other is not None:
                diff.misnamed.append(f"{name} is named {other!r} on the board")
                claimed.add(other)
            else:
                diff.missing.extend(f"{name}: {r}.{p}" for r, p in sorted(nodes))
            continue
        claimed.add(name)
        diff.missing.extend(f"{name}: {r}.{p}" for r, p in sorted(nodes - have))
        diff.extra.extend(f"{name}: {r}.{p}" for r, p in sorted(have - nodes))
    for group in unnamed:
        match = next((n for n, g in got.items() if g == group and n not in claimed), None)
        if match is not None:
            claimed.add(match)
            continue
        # partial: report the pins that are not together
        anchor = sorted(group)[0]
        holder = next((n for n, g in got.items() if anchor in g and n not in claimed), None)
        if holder is None:
            diff.missing.extend(f"(unnamed) {r}.{p}" for r, p in sorted(group))
            continue
        claimed.add(holder)
        have = got[holder]
        diff.missing.extend(f"{holder}: {r}.{p}" for r, p in sorted(group - have))
        diff.extra.extend(f"{holder}: {r}.{p}" for r, p in sorted(have - group))
    for name, nodes in got.items():
        if name in claimed or not nodes:
            continue
        stray = sorted(nodes)
        if len(stray) == 1:
            # KiCad's netlist lists every open pin as its own `unconnected-(...)` net: fine when
            # the plan marks it no-connect, a dangling pin otherwise
            if stray[0] in nc:
                continue
            diff.unexpected_nets.append(f"{name}: {stray[0][0]}.{stray[0][1]} (dangling)")
            continue
        connected_nc = [n for n in stray if n in nc]
        diff.connected_nc.extend(f"{r}.{p} ({name})" for r, p in connected_nc)
        diff.unexpected_nets.append(f"{name}: " + ", ".join(f"{r}.{p}" for r, p in stray))
    return diff


# ---- the rubric --------------------------------------------------------------------------------------


@dataclass
class Facts:
    """What the grader needs, in tool-receipt terms (millimetres, KiCad's numbers)."""

    erc_errors: int | None
    erc_warnings: int | None
    nets: dict[str, list[Any]]  # netlist name -> nodes
    unrouted: int | None
    must_fix: int | None
    review: int | None = None
    parity_violations: int | None = None
    fab_files: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Grade:
    passed: bool
    failures: list[str]
    erc_errors: int | None
    erc_warnings: int | None
    intent: dict[str, Any]
    unrouted: int | None
    must_fix: int | None
    review: int | None
    parity_violations: int | None
    fab: dict[str, Any]
    counts: dict[str, Any]
    degraded: list[str]
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


FAB_REQUIRED = ("gerbers", "drill", "pos")


def fab_bundle_status(files: Iterable[str]) -> dict[str, Any]:
    """Which of the required fabrication outputs are present, judged by file names."""
    names = [
        str(f).replace("\\", "/").lower() for f in files
    ]  # relative to <project>/fab, so `pos/`, `bom/` count
    gerbers = [
        n
        for n in names
        if n.endswith((".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gm1", ".gko"))
    ]
    drill = [
        n for n in names if n.endswith((".drl", ".xln")) or ("drill" in n and n.endswith((".txt", ".gbr")))
    ]
    pos = [n for n in names if "pos" in n and n.endswith((".csv", ".pos", ".txt"))]
    bom = [n for n in names if "bom" in n and n.endswith((".csv", ".xml"))]
    present = {"gerbers": bool(gerbers), "drill": bool(drill), "pos": bool(pos), "bom": bool(bom)}
    return {
        "present": present,
        "complete": all(present[k] for k in FAB_REQUIRED),
        "file_count": len(names),
        "gerber_count": len(gerbers),
    }


def grade(facts: Facts, plan: Mapping[str, Any] | None = None) -> Grade:
    plan = plan or load_plan()
    failures: list[str] = []
    if facts.erc_errors is None:
        failures.append("ERC did not run")
    elif facts.erc_errors != 0:
        failures.append(f"ERC errors: {facts.erc_errors}")
    diff = intent_diff(facts.nets, plan["intent"])
    if not diff.ok:
        failures.append(f"intent diff: {diff.count} item(s)")
    if facts.unrouted is None:
        failures.append("unrouted count unavailable")
    elif facts.unrouted != 0:
        failures.append(f"unrouted connections: {facts.unrouted}")
    if facts.must_fix is None:
        failures.append("DRC did not run")
    elif facts.must_fix != 0:
        failures.append(f"must_fix DRC violations: {facts.must_fix}")
    if facts.parity_violations not in (None, 0):
        failures.append(f"schematic parity violations: {facts.parity_violations}")
    fab = fab_bundle_status(facts.fab_files)
    if not fab["complete"]:
        missing = [k for k in FAB_REQUIRED if not fab["present"][k]]
        failures.append(f"fab bundle incomplete: missing {', '.join(missing)}")
    expected_parts = len(plan["parts"])
    if facts.counts.get("components") not in (None, expected_parts):
        failures.append(f"component count {facts.counts.get('components')} != {expected_parts}")
    return Grade(
        passed=not failures,
        failures=failures,
        erc_errors=facts.erc_errors,
        erc_warnings=facts.erc_warnings,
        intent=diff.as_dict(),
        unrouted=facts.unrouted,
        must_fix=facts.must_fix,
        review=facts.review,
        parity_violations=facts.parity_violations,
        fab=fab,
        counts=dict(facts.counts),
        degraded=list(facts.degraded),
        notes=list(facts.notes),
    )


# ---- collecting the facts from a project directory ------------------------------------------------------


def fab_files_under(project_dir: Path) -> list[str]:
    fab = project_dir / "fab"
    if not fab.is_dir():
        return []
    return sorted(
        str(p.relative_to(fab)) for p in fab.rglob("*") if p.is_file() and not p.name.startswith(".")
    )


def nets_from_netlist(netlist: Mapping[str, Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for n in netlist.get("nets", []) or []:
        out[str(n.get("name"))] = [
            {"ref": x.get("ref"), "pin": x.get("pin")} for x in n.get("nodes", []) or []
        ]
    return out


def collect_facts(
    project_dir: Path, *, storage_home: Path | None = None, tool_calls: int | None = None
) -> Facts:
    """Grade an existing project with a kiwrap server in-process (no MCP transport)."""
    import anyio

    from kiwrap.core.server import build_server

    server = build_server(storage_home)
    proj = str(project_dir)
    notes: list[str] = []
    degraded: set[str] = set()

    def call(tool: str, **args: Any) -> dict[str, Any] | None:
        r = anyio.run(server.call_tool, tool, args)
        sc = dict(getattr(r, "structured_content", None) or {})
        if getattr(r, "is_error", False):
            notes.append(f"{tool}: {sc.get('code')}: {sc.get('message')}")
            return None
        degraded.update(sc.get("degraded") or [])
        return sc

    erc = call("kw_sch_check", project=proj)
    sch = call("kw_sch_get", project=proj, kinds=["netlist"], detail="normal")
    stats = call("kw_sch_stats", project=proj)
    check = call("kw_pcb_check", project=proj, checks=["overlaps", "off_board", "unrouted"])
    drc = call("kw_pcb_drc", project=proj, schematic_parity=True)
    pcb = call(
        "kw_pcb_get", project=proj, kinds=["footprints", "tracks", "vias", "zones"], detail="summary", limit=1
    )
    nets = nets_from_netlist(((sch or {}).get("data") or {}).get("netlist") or {}) if sch else {}
    counts: dict[str, Any] = {}
    if stats:
        counts["components"] = stats["data"].get("components")
        counts["nets"] = stats["data"].get("nets")
    if pcb:
        counts.update({k: v for k, v in (pcb["data"].get("counts") or {}).items()})
    if tool_calls is not None:
        counts["tool_calls"] = tool_calls
    d = (drc or {}).get("data") or {}
    c = ((check or {}).get("data") or {}).get("summary") or {}
    return Facts(
        erc_errors=(erc or {}).get("data", {}).get("errors") if erc else None,
        erc_warnings=(erc or {}).get("data", {}).get("warnings") if erc else None,
        nets=nets,
        unrouted=c.get("unrouted") if check else None,
        must_fix=d.get("must_fix") if drc else None,
        review=d.get("review") if drc else None,
        parity_violations=d.get("parity_violations") if drc else None,
        fab_files=fab_files_under(Path(project_dir)),
        counts=counts,
        degraded=sorted(degraded),
        notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grade a reference-board project (KiCad is the judge)")
    ap.add_argument("project", type=Path, help="project directory or .kicad_pro")
    ap.add_argument("--plan", type=Path, default=PLAN_PATH)
    ap.add_argument("--storage-home", type=Path, default=None)
    args = ap.parse_args(argv)
    project = args.project if args.project.is_dir() else args.project.parent
    plan = load_plan(args.plan)
    facts = collect_facts(project, storage_home=args.storage_home)
    g = grade(facts, plan)
    print(json.dumps(g.as_dict(), indent=1, default=str))
    return 0 if g.passed else 1


if __name__ == "__main__":
    sys.exit(main())
