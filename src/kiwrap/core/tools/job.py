"""kw_job: poll, wait for or cancel a background job started by a tool with `run_async=true`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec


class JobArgs(BaseModel):
    job_id: str | None = Field(
        default=None, description="id returned by the tool that started the job; omit with action='list'"
    )
    action: Literal["status", "wait", "cancel", "list"] = Field(
        default="status", description="status | wait | cancel | list"
    )
    wait_s: float = Field(
        default=30.0, ge=0, le=600, description="wait: seconds to block before returning the current state"
    )


def run(ctx: ToolContext, args: JobArgs) -> Receipt:
    if args.action == "list":
        return Receipt(
            provider_used="jobs", data={"ok": True, "jobs": [j.to_dict() for j in ctx.jobs.list()]}
        )
    if not args.job_id:
        raise KiwrapError(
            "E_BAD_ARGS", f"action={args.action} needs job_id", fix="pass the job_id the async tool returned"
        )
    if args.action == "cancel":
        rec = ctx.jobs.cancel(args.job_id)
    elif args.action == "wait":
        rec = ctx.jobs.wait(args.job_id, args.wait_s)
    else:
        rec = ctx.jobs.get(args.job_id)
    done = rec.state in ("done", "failed", "cancelled")
    return Receipt(
        provider_used="jobs",
        next_suggested=None if done else "kw_job",
        warnings=[]
        if rec.state != "failed"
        else [f"job failed: {(rec.error or {}).get('code')}: {(rec.error or {}).get('message')}"],
        data={"ok": rec.state != "failed", "job": rec.to_dict(), "done": done},
    )


SPEC = ToolSpec(
    name="kw_job",
    title="Job",
    description="Status / wait / cancel for a background job (tools that may run long return a job_id when run_async=true).",
    input_model=JobArgs,
    handler=run,
    annotations=READ_ONLY,
)
