"""The Phase 0 tool catalog, in the order `tools/list` returns it (docs/ARCHITECTURE.md §10).

The order is fixed here (setup tools, then the stage order schematic -> board -> fab) so that
`tools/list` is deterministic across processes and clients can cache it.
"""

from __future__ import annotations

from kiwrap.core.tools import checks, env, fab_export, help, job, project, render, snapshot
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

TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in PHASE0_TOOLS)

__all__ = ["PHASE0_TOOLS", "TOOL_NAMES", "ToolSpec"]
