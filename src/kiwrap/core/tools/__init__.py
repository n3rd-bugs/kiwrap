"""The tool catalog, in the order `tools/list` returns it (docs/ARCHITECTURE.md §10).

The order is fixed here (setup tools, then the stage order schematic -> board -> fab, with the
data-first read tools of a stage before its validators and renders) so that `tools/list` is
deterministic across processes and clients can cache it. `PHASE0_TOOLS` is the original ten;
`DATA_TOOLS` are the data-first tools (docs/DECISIONS.md D17); `ALL_TOOLS` is what the server
registers and `TOOL_NAMES` its names.
"""

from __future__ import annotations

from kiwrap.core.tools import (
    checks,
    env,
    fab_export,
    help,
    job,
    pcb_check,
    pcb_get,
    project,
    render,
    sch_diff,
    sch_get,
    sch_stats,
    snapshot,
)
from kiwrap.core.tools._base import ToolSpec

PHASE0_TOOLS: tuple[ToolSpec, ...] = (
    env.SPEC,
    project.SPEC,
    snapshot.SPEC,
    job.SPEC,
    help.SPEC,
    checks.SCH_CHECK,
    render.SCH_RENDER,
    checks.PCB_DRC,
    render.PCB_RENDER,
    fab_export.SPEC,
)

DATA_TOOLS: tuple[ToolSpec, ...] = (sch_get.SPEC, sch_stats.SPEC, sch_diff.SPEC, pcb_get.SPEC, pcb_check.SPEC)

ALL_TOOLS: tuple[ToolSpec, ...] = (
    env.SPEC,
    project.SPEC,
    snapshot.SPEC,
    job.SPEC,
    help.SPEC,
    sch_get.SPEC,
    checks.SCH_CHECK,
    sch_stats.SPEC,
    sch_diff.SPEC,
    render.SCH_RENDER,
    pcb_get.SPEC,
    pcb_check.SPEC,
    checks.PCB_DRC,
    render.PCB_RENDER,
    fab_export.SPEC,
)
TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in ALL_TOOLS)

__all__ = ["ALL_TOOLS", "DATA_TOOLS", "PHASE0_TOOLS", "TOOL_NAMES", "ToolSpec"]
