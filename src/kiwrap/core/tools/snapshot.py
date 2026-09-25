"""kw_snapshot: create / restore / diff / list git snapshots of a project (universal undo).

A restore passes the stamp guard like any other write (review finding 1, D24): putting back a
snapshot whose design files are in a NEWER KiCad format than the files now is refused and rolled
back (E_FORMAT_UPGRADE_REQUIRED); restoring `snapshot_before` of a conversion only lowers the
stamps and passes."""

from __future__ import annotations

import contextlib
from typing import Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state import snapshots as snap
from kiwrap.core.state.project import Project
from kiwrap.core.tools._base import (
    DESTRUCTIVE,
    ToolContext,
    ToolSpec,
    mutation,
    read_state,
    resolve_target,
    suggest_next,
)


class SnapshotArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    action: Literal["create", "restore", "diff", "list"] = Field(description="create | restore | diff | list")
    ref: str | None = Field(default=None, description="restore/diff: snapshot sha (or any git ref)")
    to: str | None = Field(default=None, description="diff: second sha; default = working tree")
    message: str | None = Field(default=None, description="create: note stored in the commit message")
    limit: int = Field(default=30, ge=1, le=500, description="list: how many entries")


def run(ctx: ToolContext, args: SnapshotArgs) -> Receipt:
    project = Project.from_path(args.project)
    if args.action == "list":
        state, _ = read_state(project)
        entries = [
            {"sha": s.sha, "at": s.at, "message": s.message}
            for s in snap.list_snapshots(project.dir, args.limit)
        ]
        return Receipt(
            provider_used="git",
            stage=state.stage,
            next_suggested=suggest_next(ctx, state),
            data={"ok": True, "snapshots": entries},
        )
    if args.action == "diff":
        if not args.ref:
            raise KiwrapError(
                "E_BAD_ARGS", "diff needs `ref`", fix="pass a sha from kw_snapshot(action='list')"
            )
        state, _ = read_state(project)
        return Receipt(
            provider_used="git",
            stage=state.stage,
            data={"ok": True, **snap.diff(project.dir, args.ref, args.to)},
        )
    if args.action == "create":
        with mutation(ctx, project, "kw_snapshot", gate=False) as m:
            m.summary = f"create {args.message or ''}".strip()
            m.receipt = Receipt(provider_used="git", data={"ok": True})
            return m.receipt
    # restore
    if not args.ref:
        raise KiwrapError(
            "E_BAD_ARGS", "restore needs `ref`", fix="pass a sha from kw_snapshot(action='list')"
        )
    try:
        with mutation(ctx, project, "kw_snapshot", gate=False) as m:
            sha = snap.resolve(project.dir, args.ref)
            before = snap.head(project.dir)
            changed = snap.diff(project.dir, sha)
            # the stamp guard compares format GENERATIONS (a re-save inside one KiCad line is not an
            # upgrade): it needs the known release epochs (review finding 1)
            with contextlib.suppress(KiwrapError):
                m.guard_epochs = resolve_target(ctx, project, m.state).known_epochs
            # git restores the working tree incl. .kiwrap/state.json; reload it so the stage travels back too
            snap.restore(project.dir, sha, commit=False)
            m.state = project.load_state()
            m.summary = f"restore {sha[:12]}"
            m.receipt = Receipt(
                changed={"added": [], "updated": [f["path"] for f in changed["files"]], "removed": []},
                provider_used="git",
                data={"ok": True, "restored": sha, "previous_head": before, "files": changed["files"]},
            )
            return m.receipt
    except KiwrapError as err:
        if err.code == "E_FORMAT_UPGRADE_REQUIRED" and isinstance(err.data.get("guard"), dict):
            raise _restore_refusal(err, args.ref) from None
        raise


def _restore_refusal(err: KiwrapError, ref: str) -> KiwrapError:
    """The stamp guard rolled back a restore that raised the files' format generation (review
    finding 1: restoring the post-upgrade snapshot after the user undid a conversion re-applied it
    with no prompt). A restore is an undo, never a conversion: only `kw_project_upgrade` converts,
    with the user's approval (D24). The guard's error keeps its data (`upgrade_args` of the files
    as they are, `guard`) and learns the order: convert first, then restore that snapshot again."""
    guard = err.data.get("guard") or {}
    rose = ", ".join(
        f"{r.get('path')} {r.get('stamp_before')} -> {r.get('stamp_after')}" for r in guard.get("files") or []
    )
    err.message = f"restoring snapshot {ref} would move design files to a newer KiCad format ({rose}); nothing was restored"
    err.why = (
        "a snapshot restore is an undo; moving the files to a newer KiCad format needs the user's consent, which "
        "only kw_project_upgrade asks for (D21, D24)"
    )
    again = (
        f"after the user approved kw_project_upgrade, kw_snapshot(action='restore', ref='{ref}') brings that "
        "snapshot back (the files are then in its format); or restore a snapshot in the files' current format "
        "(kw_snapshot(action='list'))"
    )
    err.fix = f"{err.fix}. {again}" if err.fix else again
    return err


SPEC = ToolSpec(
    name="kw_snapshot",
    title="Snapshot",
    description=(
        "Git snapshots of the project directory: list them, diff two of them, create one explicitly, or restore "
        "one (the universal undo; the restore itself is recorded as a new snapshot). Every mutating tool "
        "snapshots automatically with message '[kiwrap] <tool> ...'."
    ),
    input_model=SnapshotArgs,
    handler=run,
    annotations=DESTRUCTIVE,
)
