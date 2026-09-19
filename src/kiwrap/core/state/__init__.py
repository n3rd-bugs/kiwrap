"""Core state: receipts, teaching errors, stages/gates, locks, snapshots, jobs.

These are the shapes every `kw_*` tool returns (docs/ARCHITECTURE.md §11). They are plain
dataclasses converted to JSON for MCP `structuredContent`; `Receipt.image` (PNG bytes) becomes a
separate `ImageContent` block.

Stage machine (server-enforced; `force={"reason": ...}` overrides and is logged in the snapshot):
    created -> sch_written -> erc_clean -> synced -> placed -> routed -> drc_clean -> exported
Gates:  kw_pcb_sync needs erc_clean;  kw_pcb_route_auto needs placed (0 unplaced/off-board);
        kw_fab_export needs drc_clean (must_fix == 0).
Stage results store the content hash of the files they were computed from; any tool that finds a
different hash downgrades the stage and adds `external_edit` to `degraded`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STAGES = ("created", "sch_written", "erc_clean", "synced", "placed", "routed", "drc_clean", "exported")


@dataclass
class ValidatorDelta:
    name: str  # "erc" | "drc" | "intent_diff" | "unrouted" | ...
    before: int | None
    after: int | None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Receipt:
    changed: dict[str, list[str]] = field(default_factory=lambda: {"added": [], "updated": [], "removed": []})
    validators: list[ValidatorDelta] = field(default_factory=list)
    provider_used: str = ""  # e.g. "cli:10.0.0", "swig:9.0.5", "file", "ipc:10.0.6"
    degraded: list[str] = field(default_factory=list)  # e.g. ["external_edit", "erc_unavailable_on_7"]
    snapshot: str | None = None  # git sha of the auto-commit
    stage: str | None = None
    next_suggested: str | None = None
    warnings: list[str] = field(default_factory=list)
    image: bytes | None = field(default=None, repr=False)  # PNG, <= 1024 px, ~<100 KB
    data: dict[str, Any] = field(default_factory=dict)  # tool-specific payload (report, files, ...)


class KiwrapError(Exception):
    """A teaching error: every field is shown to the agent. `code` is stable and documented in kw_help.

    Codes (Phase 0): E_NO_KICAD, E_UNSUPPORTED, E_PROJECT_NOT_FOUND, E_BAD_ARGS, E_KICAD_FAILED,
    E_FILE_LOCKED (KiCad holds a `~*.lck`), E_BUSY (a kiwrap IPC read on the project is in flight),
    E_GATE (stage gate not satisfied), E_EXTERNAL_EDIT, E_TIMEOUT, E_INTERNAL.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        why: str = "",
        fix: str = "",
        candidates: list[str] | None = None,
        example: str | None = None,
        data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code, self.message, self.why, self.fix = code, message, why, fix
        self.candidates, self.example, self.data = candidates or [], example, data or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "why": self.why,
            "fix": self.fix,
            "candidates": self.candidates,
            "example": self.example,
            "data": self.data,
        }
