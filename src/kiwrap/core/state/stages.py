"""Stage machine: `created -> sch_written -> erc_clean -> synced -> placed -> routed -> drc_clean -> exported`.

Encodes docs/ARCHITECTURE.md §11:
- every stage result stores the sha256 of the files it was computed from;
- any tool that finds a different hash downgrades the stage and adds `external_edit` to `degraded`;
- the stage is the highest validated milestone, so a tool may advance several steps at once
  (Phase 0 has no `kw_sch_*`/`kw_pcb_sync` tools: `kw_sch_check` takes `created` straight to
  `erc_clean`).

Which files feed a stage (`STAGE_INPUTS`): schematic-derived stages hash the `.kicad_sch` files,
board-derived stages hash the `.kicad_pcb`. `created` and `sch_written` carry no validator
result, so they never invalidate.

Nothing here writes to disk: the functions change a `ProjectState` in memory and the caller saves
it (`_base._finish` / `read_state`). Inside a non-persisting call (docs/DECISIONS.md D23) that
state lives in `kiwrap.core.state.readonly`'s per-process store instead of `.kiwrap/state.json`, so
a read-only session still advances and downgrades stages (a clean `kw_pcb_drc` opens the
`kw_fab_export` gate) without writing the project.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.core.state import STAGES
from kiwrap.core.state.project import Project, ProjectState

STAGE_INPUTS: dict[str, tuple[str, ...]] = {
    "created": (),
    "sch_written": (),
    "erc_clean": ("sch",),
    "synced": ("sch", "pcb"),
    "placed": ("pcb",),
    "routed": ("pcb",),
    "drc_clean": ("pcb",),
    "exported": ("pcb",),
}

#: The tool to call next per stage. Every value is a tool this server publishes (or None: the
#: Phase 0 flow ends at `exported`; a `kw_report` tool does not exist yet, so nothing is suggested).
NEXT_SUGGESTED: dict[str, str | None] = {
    "created": "kw_sch_check",
    "sch_written": "kw_sch_check",
    "erc_clean": "kw_pcb_sync",
    "synced": "kw_pcb_place",
    "placed": "kw_pcb_route",
    "routed": "kw_pcb_drc",
    "drc_clean": "kw_fab_export",
    "exported": None,
}


def stage_index(stage: str) -> int:
    return STAGES.index(stage)


def stage_at_least(stage: str, floor: str) -> bool:
    return stage_index(stage) >= stage_index(floor)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_files(project: Project, paths: list[Path]) -> dict[str, str]:
    """`{relative posix path: sha256}` for every existing file in `paths`."""
    return {project.relpath(p): sha256_file(p) for p in paths if p.exists()}


def files_for(project: Project, kinds: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    if "sch" in kinds:
        out += project.schematic_files()
    if "pcb" in kinds:
        out += project.board_files()
    return out


@dataclass
class ExternalEditCheck:
    """Result of comparing the stored hashes with the files on disk."""

    stage_before: str
    stage_after: str
    changed_files: list[str] = field(default_factory=list)
    invalidated_stages: list[str] = field(default_factory=list)

    @property
    def external_edit(self) -> bool:
        return bool(self.changed_files)

    @property
    def downgraded(self) -> bool:
        return self.stage_after != self.stage_before


def settle(state: ProjectState, below: str | None = None) -> None:
    """Move the stage to the highest stage that is still valid.

    A stage is valid when it has no inputs (`created`, `sch_written`) or its recorded hashes are
    present; `below` caps the result strictly under that stage (the machine is linear, so a stage
    whose result was invalidated takes every later stage down with it).
    """
    ceiling = stage_index(below) if below else len(STAGES)
    ceiling = min(ceiling, stage_index(state.stage) + 1)
    valid = [s for s in STAGES[:ceiling] if not STAGE_INPUTS[s] or s in state.hashes]
    state.stage = valid[-1] if valid else "created"


def check_external_edit(project: Project, state: ProjectState) -> ExternalEditCheck:
    """Re-hash every file a recorded stage depends on; drop the stages whose inputs changed.

    Mutates `state` (stage + hashes) and returns what happened. A file that disappeared counts
    as changed. Because the machine is linear, the stage after the check is the highest valid
    stage strictly below the earliest invalidated one (a hand-edited schematic invalidates
    `erc_clean` and therefore everything after it, even if the board's own DRC hash still matches).
    """
    before = state.stage
    changed: set[str] = set()
    invalidated: list[str] = []
    for stage, recorded in list(state.hashes.items()):
        current = {
            rel: (sha256_file(project.dir / rel) if (project.dir / rel).exists() else None)
            for rel in recorded
        }
        diff = sorted(rel for rel, sha in recorded.items() if current.get(rel) != sha)
        if diff:
            changed.update(diff)
            invalidated.append(stage)
            del state.hashes[stage]
    invalidated = [s for s in STAGES if s in invalidated]
    if invalidated:
        for later in STAGES[stage_index(invalidated[0]) + 1 :]:
            state.hashes.pop(later, None)
        settle(state, below=invalidated[0])
    return ExternalEditCheck(
        stage_before=before,
        stage_after=state.stage,
        changed_files=sorted(changed),
        invalidated_stages=invalidated,
    )


def record_stage(project: Project, state: ProjectState, stage: str, *, advance: bool = True) -> None:
    """Store the input hashes for `stage` and move the project there if it is a step forward.

    `advance=False` records the hashes without moving the stage (a passing validator that does
    not change the stage, e.g. re-running `kw_pcb_drc` at `exported`).
    """
    state.hashes[stage] = hash_files(project, files_for(project, STAGE_INPUTS[stage]))
    if advance and stage_index(stage) > stage_index(state.stage):
        state.stage = stage


def downgrade_below(state: ProjectState, stage: str) -> None:
    """A validator for `stage` failed: forget its result (and every later one) and settle below it."""
    for later in STAGES[stage_index(stage) :]:
        state.hashes.pop(later, None)
    settle(state, below=stage)


def next_suggested(stage: str) -> str | None:
    return NEXT_SUGGESTED.get(stage, "kw_help")
