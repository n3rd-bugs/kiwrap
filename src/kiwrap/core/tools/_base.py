"""Shared plumbing for the kw_* tools: the tool registry, argument models -> MCP schemas, the
exception -> teaching-error mapping and the mutation prologue/epilogue.

Every mutating tool follows the same sequence (docs/ARCHITECTURE.md §11):
    project lock -> editor-lock refusal -> external-edit check -> do the work -> re-hash the
    inputs of the stage reached -> git snapshot "[kiwrap] <tool> ..." -> Receipt with
    provider_used, stage, snapshot, next_suggested.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import subprocess
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from kiwrap.core.services import Services
from kiwrap.core.state import KiwrapError, Receipt, ValidatorDelta
from kiwrap.core.state.gates import Force, check_gate
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.locks import project_mutation
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.receipts import ReceiptOut, error_to_result, receipt_to_result
from kiwrap.core.state.snapshots import create as snapshot_create
from kiwrap.core.state.snapshots import head as snapshot_head
from kiwrap.core.state.snapshots import is_dirty as snapshot_is_dirty
from kiwrap.core.state.stages import ExternalEditCheck, check_external_edit, next_suggested

ToolResult = Annotated[CallToolResult, ReceiptOut]

# snake_case attribute names in mcp 2.2 (wire JSON stays camelCase: readOnlyHint, ...)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MUTATING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)


@dataclass
class ToolContext:
    services: Services
    jobs: JobQueue
    server_version: str = ""


Handler = Callable[[ToolContext, Any], "Receipt | CallToolResult"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    annotations: ToolAnnotations
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: a `ReceiptOut` subclass that types `data` for this tool's published outputSchema (None: ReceiptOut)
    output_model: type[BaseModel] | None = None


# ---- exception mapping -----------------------------------------------------------------------


def to_kiwrap_error(exc: BaseException) -> KiwrapError:
    """Map anything a tool can raise onto a stable teaching error code."""
    if isinstance(exc, KiwrapError):
        return exc
    names = {k.__name__ for k in type(exc).__mro__}  # match lower-layer classes by name, subclasses included
    name = type(exc).__name__
    if isinstance(exc, ValidationError):
        return KiwrapError(
            "E_BAD_ARGS",
            "invalid arguments: "
            + "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()),
            why="the arguments do not match the tool's input schema",
            fix="check the inputSchema (tools/list) or kw_help(topic=<tool name>)",
        )
    if "Unsupported" in names:  # kiwrap.capability.Unsupported (and RenderDependencyMissing)
        oldest = getattr(exc, "oldest_kicad_with_it", None)
        fallback = getattr(exc, "fallback", None)
        fix = getattr(exc, "fix", None)
        if not fix:
            # a `fallback` is only actionable through the MCP surface when it names a kw_* tool;
            # "swig ZONE_FILLER" or "open the schematic in eeschema" are not things an agent can call
            if oldest:
                major = oldest.split(".")[0]
                fix = (
                    f"recreate the project with kw_project(action='create', target_kicad='{major}') "
                    f"or newer (the capability needs KiCad >= {oldest})"
                )
            else:
                fix = "this capability is not available on this machine"
            if fallback and "kw_" in fallback:
                fix += f"; or use the fallback: {fallback}"
        return KiwrapError(
            "E_UNSUPPORTED",
            str(exc),
            why="the selected KiCad (or this machine) lacks this capability",
            fix=fix,
            data={
                "capability": getattr(exc, "capability", None),
                "oldest_kicad_with_it": oldest,
                "fallback": fallback,
                "kicad": getattr(getattr(exc, "install", None), "version", None),
            },
        )
    if "JobCancelled" in names:
        # kw_job(action='cancel') reached the running tool: the job ends `cancelled`, the snapshot
        # message says so (never E_INTERNAL / E_TIMEOUT)
        return KiwrapError(
            "E_CANCELLED",
            "cancelled by kw_job(action='cancel')",
            why="the background job was cancelled; the board holds whatever the tool had written before the cancel",
            fix="run the tool again (kw_snapshot restore <ref> brings the pre-call state back if needed)",
        )
    if "NoKiCadError" in names:
        return KiwrapError(
            "E_NO_KICAD",
            str(exc),
            why="no installed KiCad matches the request",
            fix="install KiCad or pass a different target_kicad; kw_env lists what was found",
            candidates=[getattr(i, "version", str(i)) for i in getattr(exc, "found", [])],
        )
    if isinstance(exc, subprocess.TimeoutExpired) or {"TimeoutError", "CliTimeout"} & names:
        return KiwrapError(
            "E_TIMEOUT",
            f"timed out: {exc}",
            why="a KiCad process exceeded its time budget",
            fix="retry with run_async=true where offered, or simplify the input",
            data=_cli_data(exc),
        )
    if "CliLoadError" in names:
        return KiwrapError(
            "E_KICAD_FAILED",
            f"kicad-cli could not load the input: {exc}",
            why="the file is corrupt, written by a newer KiCad than the one used, or contains a token this KiCad rejects",
            fix="check kw_project(action='status') for epochs vs target_kicad; upgrade the target or the file",
            data=_cli_data(exc),
        )
    if {"CliError", "CalledProcessError"} & names:
        return KiwrapError(
            "E_KICAD_FAILED",
            f"kicad-cli failed: {exc}",
            why="kicad-cli returned a non-zero exit status",
            fix="read data.stderr; kw_help(topic='E_KICAD_FAILED') lists the exit codes",
            data=_cli_data(exc),
        )
    if "ApiError" in names:
        # kipy: KiCad's API answered with an error status. AS_UNHANDLED ("no handler available
        # for request of type ...": the document kind this KiCad process does not hold - a
        # standalone pcbnew asked for a schematic, reviewer finding 9) is a capability gap the
        # planner falls through (E_UNSUPPORTED -> the file engine); anything else is the
        # server failing the request (E_KICAD_FAILED, also a planner fall-through for ipc11).
        text = str(exc)
        unhandled = "no handler available" in text.lower() or "not available in headless" in text.lower()
        return KiwrapError(
            "E_UNSUPPORTED" if unhandled else "E_KICAD_FAILED",
            f"KiCad API: {text}",
            why=(
                "this KiCad process holds no document of that kind (its API server has no handler for it)"
                if unhandled
                else "KiCad's API server refused or failed the request"
            ),
            fix=(
                "the file engine answers this read (degraded: ipc11_unavailable); a live kw_live session of "
                "the other editor kind does not serve this document - kw_live detach releases the project"
                if unhandled
                else "retry; kw_live status / kw_env show the KiCad the request went to"
            ),
            data={"api_status": getattr(exc, "code", None), "raw": getattr(exc, "raw_message", None)},
        )
    if "LibError" in names:
        # kiwrap.formats.libs teaching errors (E_LIB_NOT_FOUND / E_LIB_UNSUPPORTED / E_LIB) keep
        # their code, why, fix and candidates
        return KiwrapError(
            str(getattr(exc, "code", "E_LIB") or "E_LIB"),
            str(getattr(exc, "message", None) or exc),
            why=str(getattr(exc, "why", "") or ""),
            fix=str(getattr(exc, "fix", "") or ""),
            candidates=[str(c) for c in (getattr(exc, "candidates", None) or [])],
            data=dict(getattr(exc, "data", None) or {}),
        )
    if "WriterError" in names:
        # the schematic writer's teaching errors: a stable `code` (E_SYMBOL_NOT_FOUND -> the
        # library tools' E_LIB_NOT_FOUND, everything else a caller mistake), `fix`, `candidates`
        code = str(getattr(exc, "code", "") or "")
        candidates = [str(c) for c in (getattr(exc, "candidates", None) or [])]
        fix = str(getattr(exc, "fix", "") or "")
        if code in ("E_SYMBOL_NOT_FOUND", "E_LIB_SYMBOL", "E_LIB_RESOLVER"):
            return KiwrapError(
                "E_LIB_NOT_FOUND",
                str(exc),
                why="the lib_id names no symbol in the library tables of the target KiCad",
                fix=fix or "search with kw_lib_search(kind='symbol', query=<name>) and use the id it returns",
                candidates=candidates,
                data={"writer_code": code},
            )
        return KiwrapError(
            "E_BAD_ARGS",
            str(exc),
            why="the schematic writer refused the request",
            fix=fix or "follow the message; kw_help(topic=<tool name>) lists the accepted values",
            candidates=candidates,
            data={"writer_code": code} if code else {},
        )
    if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError):
        # a backend refusing an argument value (e.g. `severity_flags('bogus')`) is the caller's mistake
        return KiwrapError(
            "E_BAD_ARGS",
            f"invalid argument value: {exc}",
            why="a lower layer rejected one of the argument values",
            fix="check the inputSchema (tools/list) or kw_help(topic=<tool name>) for the accepted values",
        )
    if isinstance(exc, PermissionError):
        return KiwrapError(
            "E_FILE_LOCKED",
            str(exc),
            why="a file is held open by another program (KiCad, an editor or an antivirus scan)",
            fix="close the program holding the file and retry",
        )
    if isinstance(exc, FileNotFoundError):
        return KiwrapError(
            "E_PROJECT_NOT_FOUND", str(exc), why="a file the tool needs is missing", fix="check the path"
        )
    return KiwrapError(
        "E_INTERNAL",
        f"{name}: {exc}",
        why="an unexpected exception inside kiwrap",
        fix="report this with data.traceback",
        data={"traceback": traceback.format_exception(exc)[-12:]},
    )


def _cli_data(exc: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in ("argv", "returncode", "stdout", "stderr"):
        val = getattr(exc, attr, None)
        if val is None:
            res = getattr(exc, "result", None)
            val = getattr(res, attr, None)
        if val is not None:
            out[attr] = val[-3000:] if isinstance(val, str) else val
    return out


# ---- registration ------------------------------------------------------------------------------


def make_callable(spec: ToolSpec, ctx: ToolContext) -> Callable[..., CallToolResult]:
    """Turn a ToolSpec into a plain function whose signature mirrors the input model.

    The mcp SDK derives `inputSchema` from the signature and `outputSchema` from the
    `Annotated[CallToolResult, ReceiptOut]` return annotation; we validate the arguments a
    second time through the pydantic model so the handler receives a typed object.
    """
    params: list[inspect.Parameter] = []
    for fname, finfo in spec.input_model.model_fields.items():
        ann: Any = finfo.annotation if finfo.annotation is not None else Any
        # description AND the numeric constraints (ge/le -> minimum/maximum, ...) so the published
        # inputSchema tells the agent the limits before, not after, a call
        extras = [Field(description=finfo.description)] if finfo.description else []
        extras.extend(finfo.metadata)
        if extras:
            ann = Annotated[ann, *extras]
        default = (
            inspect.Parameter.empty if finfo.is_required() else finfo.get_default(call_default_factory=True)
        )
        params.append(
            inspect.Parameter(fname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann)
        )

    def tool_fn(**kwargs: Any) -> ToolResult:
        try:
            args = spec.input_model.model_validate(kwargs)
            result = spec.handler(ctx, args)
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the agent as isError
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return error_to_result(enrich_unsupported(ctx, to_kiwrap_error(exc)))
        if isinstance(result, CallToolResult):
            return result
        return receipt_to_result(spec.name, result, ok=bool(result.data.get("ok", True)))

    result_type: Any = Annotated[CallToolResult, spec.output_model] if spec.output_model else ToolResult
    tool_fn.__name__ = spec.name
    tool_fn.__qualname__ = spec.name
    tool_fn.__doc__ = spec.description
    tool_fn.__signature__ = inspect.Signature(params, return_annotation=result_type)  # type: ignore[attr-defined]
    tool_fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": result_type}
    return tool_fn


def enrich_unsupported(ctx: ToolContext, err: KiwrapError) -> KiwrapError:
    """E_UNSUPPORTED: `candidates` = the installed KiCads whose capability record HAS the key, so
    the agent can retarget instead of retrying the same install forever."""
    if err.code != "E_UNSUPPORTED" or err.candidates:
        return err
    key = err.data.get("capability")
    if not key:
        return err
    good: list[str] = []
    try:
        installs = ctx.services.installs()
    except Exception:  # noqa: BLE001 - enrichment must never mask the original error
        return err
    for inst in installs:
        try:
            if ctx.services.caps(inst).has(key):
                good.append(inst.version)
        except Exception:  # noqa: BLE001
            continue
    err.candidates = good
    if not good and err.data.get("oldest_kicad_with_it"):
        err.fix = f"install KiCad >= {err.data['oldest_kicad_with_it']} (no installed KiCad has {key})"
    return err


#: The capability a suggested tool needs on the project's target KiCad; a suggestion that cannot
#: work there (`kw_sch_check` on a project whose target lacks cli.has_erc) is withheld.
TOOL_NEEDS: dict[str, str] = {"kw_sch_check": "cli.has_erc", "kw_pcb_drc": "cli.has_drc"}


def suggest_next(ctx: ToolContext, state: ProjectState, preferred: str | None = None) -> str | None:
    """`preferred` (a handler's choice) or the stage's default, unless the project's target KiCad
    lacks the capability that tool needs; None then."""
    tool = preferred or next_suggested(state.stage)
    key = TOOL_NEEDS.get(tool or "")
    if key and state.target_kicad:
        try:
            install = ctx.services.install(state.target_kicad)
            if not ctx.services.caps(install).has(key):
                return None
        except Exception:  # noqa: BLE001 - an unknown target keeps the default suggestion
            return tool
    return tool


# ---- mutation prologue / epilogue ---------------------------------------------------------------


@dataclass
class Mutation:
    project: Project
    state: ProjectState
    tool: str
    external: ExternalEditCheck
    forced_reason: str | None = None
    receipt: Receipt = field(default_factory=Receipt)
    summary: str = ""
    history_extra: dict[str, Any] = field(default_factory=dict)
    pre_snapshot: str | None = None  # sha of the state-before-this-call snapshot, when one was needed
    baseline: str | None = None  # the snapshot holding the files as they were when this call began
    failure_code: str | None = None  # error code when the handler raised (marks the snapshot message)
    #: the live GUI session `kw_live` bound to this project (`release_project()['bound']`: session_id,
    #: pid, socket, locks, files), else None - the tool then writes through that KiCad and must not
    #: rewrite its files on disk behind it
    bound: dict[str, Any] | None = None

    @property
    def degraded(self) -> list[str]:
        return ["external_edit"] if self.external.external_edit else []

    def validator_delta(self, name: str, after: int, detail: dict[str, Any] | None = None) -> ValidatorDelta:
        """Record `name=after` for this call and look up the previous value in the project history."""
        before = None
        for entry in reversed(self.state.history):
            if name in entry.get("validators", {}):
                before = int(entry["validators"][name])
                break
        delta = ValidatorDelta(name=name, before=before, after=after, detail=detail or {})
        self.receipt.validators.append(delta)
        self.history_extra.setdefault("validators", {})[name] = after
        return delta


def release_own_sessions(ctx: ToolContext, project: Project) -> dict[str, Any]:
    """Before the editor-lock check: close kiwrap's own pooled api-servers on this project (a
    headless server holds KiCad's `~<name>.kicad_pro.lck` for its whole lifetime, so a read that
    stayed pooled would otherwise be refused as "KiCad has the file open" - reviewer finding 19),
    and refuse with E_BUSY while a non-pooled read is still in flight in another thread. Returns
    the pool's report (`bound`: the live GUI session kept on the project, with its lock files)."""
    release = getattr(ctx.services, "ipc_release_project", None)
    if not callable(release):
        return {}
    try:
        info = release(project.dir)
    except KiwrapError as exc:
        if exc.code == "E_UNSUPPORTED":  # no IPC backend in this build: nothing to release
            return {}
        raise
    busy = list((info or {}).get("busy") or [])
    if busy:
        raise KiwrapError(
            "E_BUSY",
            f"a kiwrap read is still using {len(busy)} file(s) of this project over IPC",
            why="the headless api-server of that read holds KiCad's project lock until it finishes",
            fix="retry in a moment; the read (kw_pcb_get / kw_sch_get / kw_pcb_check) releases the project when done",
            candidates=[Path(b).name for b in busy],
        )
    return dict(info or {})


@contextmanager
def mutation(
    ctx: ToolContext, project: Project, tool: str, *, force: Any = None, gate: bool = True
) -> Iterator[Mutation]:
    """Lock the project, refuse open editors, detect external edits, check the gate, snapshot the
    state before the call when it differs from the last snapshot (so an external edit or the
    remains of a crashed call stay reachable by `kw_snapshot restore`), then snapshot + save state
    on the way out (also on failure, so a half-applied change is committed as its own snapshot
    and remains undoable; the message then carries `FAILED <code>`)."""
    parsed = Force.parse(force)
    info = release_own_sessions(ctx, project)
    bound = info.get("bound") if isinstance(info.get("bound"), dict) else None
    # the lock files of the GUI session kw_live bound to this project are kiwrap's own when the
    # write lands in that KiCad (its API server, `ipc11`); on a KiCad without the headless IPC
    # provider the GUI merely holds the files and the refusal stands (kw_live detach first)
    exempt = (
        [str(x) for x in (bound or {}).get("locks", []) or []] if (bound or {}).get("ipc11", True) else []
    )
    with project_mutation(project, exempt=exempt):
        state = project.load_state()
        external = check_external_edit(project, state)
        forced = check_gate(tool, state.stage, parsed) if gate else None
        m = Mutation(
            project=project, state=state, tool=tool, external=external, forced_reason=forced, bound=bound
        )
        _snapshot_before(m)
        try:
            yield m
        except BaseException as exc:
            if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                m.failure_code = to_kiwrap_error(exc).code
            raise
        finally:
            _finish(ctx, m)


def _snapshot_before(m: Mutation) -> None:
    """Commit the owned files as they are NOW when they differ from the last snapshot (§11
    universal undo: the epilogue commit alone would fold an external edit into the tool's own
    change and the hand-edited version would be gone)."""
    try:
        if snapshot_is_dirty(m.project.dir):
            m.pre_snapshot = snapshot_create(m.project.dir, f"[kiwrap] pre-{m.tool} (state before this call)")
        m.baseline = m.pre_snapshot or snapshot_head(m.project.dir)
    except KiwrapError as exc:
        m.history_extra.setdefault("warnings", []).append(f"pre-call snapshot failed: {exc.message}")


def _finish(ctx: ToolContext, m: Mutation) -> None:
    r = m.receipt
    r.stage = m.state.stage
    r.next_suggested = suggest_next(ctx, m.state, r.next_suggested)
    for d in m.degraded:
        if d not in r.degraded:
            r.degraded.append(d)
    if m.external.external_edit:
        r.warnings.append(
            "external_edit: "
            + ", ".join(m.external.changed_files)
            + f" changed outside kiwrap; stage downgraded {m.external.stage_before} -> {m.external.stage_after}"
        )
    if m.pre_snapshot:
        r.warnings.append(
            f"pre-call snapshot {m.pre_snapshot[:12]} records the files as they were before this call"
        )
        r.data.setdefault("pre_snapshot", m.pre_snapshot)
    for w in m.history_extra.pop("warnings", []):
        r.warnings.append(w)
    message = f"[kiwrap] {m.tool} {m.summary}".rstrip()
    if m.forced_reason:
        message += f" (forced: {m.forced_reason})"
    if m.failure_code:
        message += f" FAILED {m.failure_code}"
    entry: dict[str, Any] = {
        "tool": m.tool,
        "stage": m.state.stage,
        "at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }
    entry.update(m.history_extra)
    if m.forced_reason:
        entry["force"] = m.forced_reason
    m.state.history = (m.state.history + [entry])[-200:]
    m.project.save_state(m.state)
    try:
        sha = snapshot_create(m.project.dir, message)
    except KiwrapError as exc:
        sha = None
        r.warnings.append(f"snapshot failed: {exc.message}")
        r.degraded.append("snapshot_unavailable")
    r.snapshot = sha
    if sha:
        entry["snapshot"] = sha
        m.project.save_state(m.state)


def read_state(project: Project) -> tuple[ProjectState, ExternalEditCheck]:
    """Read-only tools still detect external edits (and persist the downgrade)."""
    state = project.load_state()
    external = check_external_edit(project, state)
    if external.external_edit:
        project.save_state(state)
    return state, external
