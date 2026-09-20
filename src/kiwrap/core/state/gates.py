"""The three server-enforced stage gates and the `force={"reason": ...}` override.

docs/ARCHITECTURE.md §11: `kw_pcb_sync` requires the last `kw_sch_check` to be ok (stage
`erc_clean`); `kw_pcb_route_auto` requires zero unplaced/off-board footprints (`placed`);
`kw_fab_export` requires `must_fix_count == 0` (`drc_clean`). `ok` = zero errors; warnings never
gate. A force override is accepted only with a non-empty reason and is logged into the snapshot
commit message (`[kiwrap] <tool> ... (forced: <reason>)`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiwrap.core.state import KiwrapError
from kiwrap.core.state.stages import stage_at_least

GATES: dict[str, str] = {
    "kw_pcb_sync": "erc_clean",
    "kw_pcb_route_auto": "placed",
    "kw_pcb_fix": "placed",
    "kw_fab_export": "drc_clean",
}

GATE_FIX: dict[str, str] = {
    "kw_pcb_sync": "run kw_sch_check and fix every ERC error (warnings do not gate) until ok=true",
    "kw_pcb_route_auto": (
        "place every footprint inside the board outline with kw_pcb_place until kw_pcb_check reports no "
        "off_board / courtyard_overlap items (that settles the project at `placed`)"
    ),
    "kw_pcb_fix": (
        "reach `placed` first (kw_pcb_place until kw_pcb_check is clean): re-routing against an unplaced "
        "board fixes nothing"
    ),
    "kw_fab_export": "run kw_pcb_drc and fix every must_fix violation until must_fix == 0",
}


@dataclass(frozen=True)
class Force:
    reason: str

    @classmethod
    def parse(cls, raw: Any) -> Force | None:
        """Accept `None`, `{"reason": "..."}`, or a plain non-empty string; anything else is E_BAD_ARGS."""
        if raw is None or raw is False:
            return None
        if isinstance(raw, Force):
            return raw
        if isinstance(raw, str):
            reason = raw.strip()
        elif isinstance(raw, dict):
            reason = str(raw.get("reason", "")).strip()
        else:
            reason = ""
        if not reason:
            raise KiwrapError(
                "E_BAD_ARGS",
                "force needs a reason",
                why="gate overrides are logged into the snapshot so a reviewer can see why the gate was skipped",
                fix='pass force={"reason": "<why this is safe>"}',
                example='kw_fab_export(project=..., targets=["gerbers"], force={"reason": "prototype, DRC courtyard overlaps accepted"})',
            )
        return cls(reason=reason)


def check_gate(tool: str, stage: str, force: Force | None = None) -> str | None:
    """Raise E_GATE unless `stage` satisfies the gate for `tool` (or a force is given).

    Returns the forced reason when the gate was overridden (to be logged), else None.
    """
    needed = GATES.get(tool)
    if needed is None or stage_at_least(stage, needed):
        return None
    if force is not None:
        return force.reason
    raise KiwrapError(
        "E_GATE",
        f"{tool} needs stage '{needed}' but the project is at '{stage}'",
        why="stage gates keep the agent from building on unvalidated inputs (KiCad is the judge)",
        fix=GATE_FIX.get(tool, f"reach stage '{needed}' first"),
        example=f'{tool}(project=..., force={{"reason": "explain why skipping the gate is safe"}})',
        data={"required_stage": needed, "stage": stage},
    )
