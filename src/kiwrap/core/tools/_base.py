"""Shared plumbing for the kw_* tools: the tool registry, argument models -> MCP schemas, the
exception -> teaching-error mapping and the mutation prologue/epilogue.

Every mutating tool follows the same sequence (docs/ARCHITECTURE.md §11):
    project lock -> editor-lock refusal -> external-edit check -> do the work -> re-hash the
    inputs of the stage reached -> git snapshot "[kiwrap] <tool> ..." -> Receipt with
    provider_used, stage, snapshot, next_suggested.

Which KiCad a project runs on (docs/DECISIONS.md D21, requests/kiwrap-feature-requests.review.md item
1 / B1 and theme T1): `effective_target()` is the one resolver every tool calls instead of
`services.install(state.target_kicad)`. It answers the pinned target, else the newest installed
point release that writes the files' own format, else the nearest newer one (reads only). A write
that would save the design files in a newer format fails with `E_FORMAT_UPGRADE_REQUIRED`, whose
`data.upgrade_args` are the exact arguments of `kw_project_upgrade` - the only conversion, which
the client's permission flow approves (docs/DECISIONS.md D24: published with
`_meta["anthropic/requiresUserInteraction"]`). MCP elicitation stays a frozen optional shortcut:
asked only when the client declares it and the server does not run under the VS Code panel
(`CLAUDE_CODE_ENTRYPOINT=claude-vscode` declines every elicitation, ANALYSIS-TOOLING.md §6), through
the SDK's resolver API on both protocol eras; an accept runs the same conversion core. Files newer
than every install are `E_FORMAT_NEWER`. The mutation epilogue re-reads the stamps and undoes any
call that raised one without consent (the stamp guard: the only layer that catches a write path
nobody anticipated).
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import datetime as _dt
import inspect
import os
import re
import shutil
import subprocess
import traceback
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver.context import Context as McpContext
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from kiwrap.core.services import Services
from kiwrap.core.state import KiwrapError, Receipt, ValidatorDelta, readonly
from kiwrap.core.state.gates import Force, check_gate
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.locks import project_mutation
from kiwrap.core.state.project import FileStamp, Project, ProjectState
from kiwrap.core.state.receipts import ReceiptOut, error_to_result, receipt_to_result
from kiwrap.core.state.snapshots import create as snapshot_create
from kiwrap.core.state.snapshots import head as snapshot_head
from kiwrap.core.state.snapshots import is_dirty as snapshot_is_dirty
from kiwrap.core.state.snapshots import owned_files
from kiwrap.core.state.snapshots import restore as snapshot_restore
from kiwrap.core.state.stages import ExternalEditCheck, check_external_edit, next_suggested
from kiwrap.installation import version_tuple

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
    #: publish the common `persist` argument (D23). False for `kw_project_upgrade` (D24): a call the
    #: client asks the user about must never be one that can only fail (persist=false would be
    #: refused with E_READONLY after the prompt)
    common_persist: bool = True


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


#: the MCP request context (`mcp.server.mcpserver.Context`) of the tool call running in this
#: thread, bound by `make_callable` so a consent point deep inside a handler can elicit from the
#: client (D21); None outside an MCP call (direct handler calls, background jobs, the CLI)
_REQUEST: contextvars.ContextVar[Any] = contextvars.ContextVar("kiwrap_mcp_request", default=None)
#: the keyword the SDK injects the request context through (excluded from the inputSchema)
MCP_CONTEXT_ARG = "kiwrap_mcp_context"


class InputRequired(BaseException):
    """A consent point needs the client's answer under the 2026-07-28 protocol, where a server
    cannot send `elicitation/create` mid-call: `make_callable` returns `result` (an
    `InputRequiredResult`) and the client retries the call with the answer in `input_responses`.
    A BaseException so no `except Exception` on the way up swallows it; raised before the tool
    writes anything (the consent point is the target resolution at the start of a mutation)."""

    def __init__(self, result: Any):
        super().__init__("input required")
        self.result = result


def bound_request() -> Any:
    """The MCP request context of the current tool call, or None."""
    return _REQUEST.get()


def make_callable(spec: ToolSpec, ctx: ToolContext) -> Callable[..., CallToolResult]:
    """Turn a ToolSpec into a plain function whose signature mirrors the input model.

    The mcp SDK derives `inputSchema` from the signature and `outputSchema` from the
    `Annotated[CallToolResult, ReceiptOut]` return annotation; we validate the arguments a
    second time through the pydantic model so the handler receives a typed object. One extra
    keyword (`MCP_CONTEXT_ARG`, annotated with the SDK's `Context`, hence injected by the SDK and
    left out of the inputSchema) carries the request context into `_REQUEST` for the D21
    consent prompt.

    Every tool also publishes the common argument `persist` (docs/DECISIONS.md D23, review item 2):
    `persist=false` - or a read-only server (`kiwrap serve --readonly`, `KIWRAP_READONLY=1`) - runs
    the handler inside a `readonly.DetachedCall` (nothing written into the project; a tool that
    would write it is refused with E_READONLY before the handler runs). A spec with
    `common_persist=False` (`kw_project_upgrade`, D24) does not publish it; a read-only server
    still refuses it.
    """
    params: list[inspect.Parameter] = []
    own_persist = readonly.PERSIST_ARG in spec.input_model.model_fields
    common_persist = spec.common_persist and not own_persist
    read_only_hint = bool(getattr(spec.annotations, "read_only_hint", False))
    readonly.register_tool(spec.name, read_only_hint=read_only_hint)
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
    if common_persist:
        params.append(
            inspect.Parameter(
                readonly.PERSIST_ARG,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[bool | None, Field(description=readonly.PERSIST_DESCRIPTION)],
            )
        )
    params.append(
        inspect.Parameter(
            MCP_CONTEXT_ARG, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=McpContext | None
        )
    )

    def tool_fn(**kwargs: Any) -> ToolResult:
        token = _REQUEST.set(kwargs.pop(MCP_CONTEXT_ARG, None))
        persist = kwargs.get(readonly.PERSIST_ARG) if own_persist else kwargs.pop(readonly.PERSIST_ARG, None)
        if not own_persist and not common_persist:
            persist = None  # never published: only the server-wide switch applies
        call: readonly.DetachedCall | None = None
        try:
            ro_persist = persist if isinstance(persist, bool) else None
            args = spec.input_model.model_validate(kwargs)
            # D23: a non-persisting call is refused (E_READONLY) or runs detached from the project
            spec_project = getattr(args, "project", None)
            call = readonly.begin(
                ctx,
                spec.name,
                args,
                ro_persist,
                read_only_hint=read_only_hint,
                project_spec=spec_project if isinstance(spec_project, str) else None,
            )
            if call is None:
                result = spec.handler(ctx, args)
            else:
                args = readonly.prepare_args(call, args)
                with call:
                    result = spec.handler(call.ctx, args)
                result = call.annotate(result)
        except InputRequired as need:  # D21 consent under the 2026-07-28 protocol: ask, then retry
            return need.result
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the agent as isError
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            err = enrich_unsupported(ctx, to_kiwrap_error(exc))
            if call is not None and err.code != "E_READONLY":
                err.data.setdefault("readonly", call.info())
            return error_to_result(err)
        finally:
            _REQUEST.reset(token)
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


def suggest_next(
    ctx: ToolContext, state: ProjectState, preferred: str | None = None, *, target: Any = None
) -> str | None:
    """`preferred` (a handler's choice) or the stage's default, unless the project's target KiCad
    lacks the capability that tool needs; None then. The target is `target` (a `TargetChoice`),
    else the one the running mutation resolved, else the pinned `state.target_kicad`."""
    tool = preferred or next_suggested(state.stage)
    key = TOOL_NEEDS.get(tool or "")
    if not key:
        return tool
    install = getattr(target, "install", None)
    if install is None:
        running = _CURRENT.get()
        install = getattr(getattr(running, "target", None), "install", None)
    try:
        if install is None and state.target_kicad:
            install = ctx.services.install(state.target_kicad)
        if install is not None and not ctx.services.caps(install).has(key):
            return None
    except Exception:  # noqa: BLE001 - an unknown target keeps the default suggestion
        return tool
    return tool


# ---- which KiCad writes this project (D21) --------------------------------------------------------

#: the design-file kinds whose `(version N)` stamp decides which KiCad may write a project
DESIGN_KINDS: tuple[str, ...] = ("sch", "pcb")
#: `TargetChoice.source` values (the contract's vocabulary)
SOURCE_PINNED = "pinned"
SOURCE_CREATED = "created"
SOURCE_FILE_EPOCH = "file_epoch"
SOURCE_NEWER_READONLY = "file_epoch_newer_readonly"


@dataclass
class TargetChoice:
    """`effective_target()`'s answer: the install a call runs on and why.

    `source`: `pinned` (kw_project retarget / upgrade, or a hand-pinned state), `created`
    (kw_project create stamped the files in that install's format), `file_epoch` (not pinned:
    the newest install writing the files' own format), `file_epoch_newer_readonly` (no install
    writes that format; the nearest newer one reads it, writes need an upgrade).
    `project_epoch` is `{kind: newest stamp}` of the design files, `install_epoch` the install's
    `formats.<kind>`; `writable_kinds` the kinds the install can save without moving any file to a
    newer KiCad line (every file's generation equals the install's epoch), `writable` all of them.
    `readable` is False when some file is newer than the install (kicad-cli cannot load it).
    """

    install: Any
    source: str
    project_epoch: dict[str, int]
    install_epoch: dict[str, int]
    writable: bool
    notes: list[str] = field(default_factory=list)
    writable_kinds: frozenset[str] = frozenset()
    readable: bool = True
    files: list[FileStamp] = field(default_factory=list)
    #: every installed KiCad considered: {version, root, formats, reads, matches}
    candidates: list[dict[str, Any]] = field(default_factory=list)
    #: the KiCad line the files belong to ("10.0"; None when no known release writes that format)
    project_kicad: str | None = None
    #: the newest released install command for that line (`backends.cli.upgrade.install_hint`)
    install_hint: str | None = None
    #: {kind: the generations (known release epochs) the files of that kind belong to}
    file_generations: dict[str, list[int]] = field(default_factory=dict)
    #: {kind: every known release epoch} (the version matrix plus the installs)
    known_epochs: dict[str, list[int]] = field(default_factory=dict)
    #: the upgrade report when this call upgraded the project with the user's consent
    upgraded: dict[str, Any] | None = None
    #: the version matrix as data (`format_releases`), newest first
    releases: list[dict[str, Any]] = field(default_factory=list)

    def release_for(self, kinds: Iterable[str] | None = None) -> tuple[str | None, str | None]:
        """(KiCad line, install hint) of the newest released KiCad that writes the files of
        `kinds` in their own format and reads every other design file - what to install instead
        of upgrading. For a board-only write on a project whose schematics are older (the review's
        kit-dev-coldfire: 9.0 sheets, board re-saved by 10.0.6) that is KiCad 10.0.

        Never a line newer than the chosen install (the upgrade target), and (None, None) when the
        files of a wanted kind already mix format generations - no KiCad writes such a set, so
        there is nothing to install instead (fixer finding 6: the matrix's unreleased 11.0.0
        shares 10.99's epochs, and a mixed 10.99/10.0 project was called 'KiCad 11.0 format' with
        the advice to install KiCad 11.0)."""
        wanted = set(kinds) if kinds is not None else set(self.project_epoch)
        if any(len(self.file_generations.get(k, [])) > 1 for k in wanted):
            return None, None
        newest = {k: max(v) for k, v in self.file_generations.items() if v}
        wanted &= set(newest)
        if not wanted or wanted == set(self.project_epoch):
            return self.project_kicad, self.install_hint
        limit = self.version if self.readable else None
        fallback: dict[str, Any] | None = None
        for rel in self.releases:  # newest first
            if _newer_than(str(rel.get("version") or ""), limit):
                continue
            formats = rel.get("formats") or {}
            writes = all(formats.get(k) == newest[k] for k in wanted)
            reads = all(
                isinstance(formats.get(k), int) and formats[k] >= e for k, e in self.project_epoch.items()
            )
            if writes and reads:
                if rel.get("released") and rel.get("install_hint"):
                    return str(rel.get("line")), str(rel["install_hint"])
                fallback = rel  # the oldest unreleased match: a nightly before the next major's seed
        return (str(fallback.get("line")), None) if fallback else (self.project_kicad, self.install_hint)

    @property
    def mixed(self) -> bool:
        """The files of some kind belong to different KiCad format generations (a sheet re-saved
        by a newer KiCad next to older ones): no single KiCad writes this project as it is."""
        return any(len(v) > 1 for v in self.file_generations.values())

    @property
    def version(self) -> str:
        return str(getattr(self.install, "version", "") or "")

    def writable_for(self, kinds: Iterable[str] | None = None) -> bool:
        wanted = set(kinds) if kinds is not None else set(self.project_epoch)
        return self.readable and wanted <= set(self.writable_kinds)

    def upgrade_files(self) -> list[FileStamp]:
        """The files an upgrade to this install would move to a newer KiCad line."""
        out = []
        for f in self.files:
            epoch = self.install_epoch.get(f.kind)
            if f.stamp is None or epoch is None or f.stamp >= epoch:
                continue
            if _generation_of(f.stamp, self.known_epochs.get(f.kind, [])) != epoch:
                out.append(f)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "kicad": self.version,
            "source": self.source,
            "project_epoch": dict(self.project_epoch),
            "install_epoch": dict(self.install_epoch),
            "writable": self.writable,
            "writable_kinds": sorted(self.writable_kinds),
            "readable": self.readable,
            "project_kicad": self.project_kicad,
            "notes": list(self.notes),
        }

    def summary(self) -> dict[str, Any]:
        """The compact form every mutation receipt carries as `data.target`."""
        out: dict[str, Any] = {"kicad": self.version, "source": self.source, "epoch_match": self.writable}
        if self.upgraded:
            out["upgraded"] = True
        return out


def _generation_of(stamp: int, known: list[int]) -> int | None:
    """The epoch of the oldest known KiCad release that reads `stamp` (the smallest known epoch
    >= stamp), None when the stamp is newer than every known one. Two stamps with the same
    generation are the same file format as far as any known KiCad is concerned: KiCad 10.0.6
    re-saving the 10.0 demo's `(version 20260101)` schematic writes 20260306, which every 10.0.x
    reads (verified 2026-09-24, `sch upgrade` on a copy of pic_programmer)."""
    for epoch in known:
        if epoch >= stamp:
            return epoch
    return None


def _install_epochs(ctx: ToolContext, install: Any) -> dict[str, int]:
    """`{kind: formats.<kind>}` of an install for the design kinds (missing keys are left out);
    `services.install_formats` when the services have it (no probe), else the record."""
    fn = getattr(ctx.services, "install_formats", None)
    try:
        if callable(fn):
            values: Any = fn(install)
        else:
            caps = ctx.services.caps(install)
            values = {k: caps[f"formats.{k}"] for k in DESIGN_KINDS if f"formats.{k}" in caps}
    except (KiwrapError, TypeError):
        return {}
    out: dict[str, int] = {}
    for kind in DESIGN_KINDS:
        val = values.get(kind) if isinstance(values, dict) else None
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[kind] = int(val)
    return out


def format_releases(ctx: ToolContext) -> list[dict[str, Any]]:
    """The version matrix as `[{version, line, formats, nightly, released, install_hint}]`
    (`services.format_releases`; [] when the build has no matrix - unit tests with fakes)."""
    fn = getattr(ctx.services, "format_releases", None)
    if not callable(fn):
        return []
    try:
        return list(fn() or [])
    except KiwrapError:
        return []


def _newer_than(version: str, limit: str | None) -> bool:
    """`version` is a newer KiCad than `limit` (False without a limit or a parsable version)."""
    if not limit or not version:
        return False
    a, b = version_tuple(version), version_tuple(limit)
    return bool(a and b) and a > b


def _line_for(
    generations: dict[str, int | None], releases: list[dict[str, Any]], *, not_newer_than: str | None = None
) -> tuple[str | None, str | None]:
    """(KiCad line, install hint) of the matrix release writing exactly `generations`: the newest
    released point release with an install hint first, else the OLDEST unreleased one (the 10.99
    nightly, not the unreleased 11.0.0 the matrix seeds with the same epochs - fixer finding 6);
    a release newer than `not_newer_than` (the chosen install) is never named."""
    if not generations or any(g is None for g in generations.values()):
        return None, None
    fallback: dict[str, Any] | None = None
    for rel in releases:  # newest first
        if _newer_than(str(rel.get("version") or ""), not_newer_than):
            continue
        formats = rel.get("formats") or {}
        if all(formats.get(k) == g for k, g in generations.items()):
            if rel.get("released") and rel.get("install_hint"):
                return str(rel.get("line")), str(rel["install_hint"])
            fallback = rel
    if fallback is not None:
        return str(fallback.get("line")), None
    return None, None


def _kicad_line(version: str) -> str:
    """'10.99.0' -> '10.99' (the line a KiCad version belongs to)."""
    parts = [p for p in str(version).split(".") if p]
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


def _file_line(choice: TargetChoice, f: FileStamp) -> str | None:
    """The KiCad line whose format one design file is in (its generation named by the matrix,
    never newer than the chosen install; else an installed KiCad writing it), None if unknown."""
    if f.stamp is None:
        return None
    gen = _generation_of(int(f.stamp), choice.known_epochs.get(f.kind, []))
    if gen is None:
        return None
    line, _hint = _line_for({f.kind: gen}, choice.releases, not_newer_than=choice.version or None)
    if line:
        return line
    for cand in choice.candidates:
        if (cand.get("formats") or {}).get(f.kind) == gen:
            return _kicad_line(str(cand.get("version") or ""))
    return None


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


#: how many files a conversion message names before it says "and N more"
_FILES_NAMED = 6


def _conversion_text(choice: TargetChoice) -> tuple[str, str, int]:
    """(the files an upgrade to the chosen install would convert, each with the KiCad line of its
    format; the KiCad lines that could open them before; how many files) - the wording of the D21
    prompt when the project has no single named KiCad line (fixer finding 6)."""
    files = choice.upgrade_files() or [f for f in choice.files if f.stamp is not None]
    named: list[str] = []
    lines: list[str] = []
    for f in files:
        line = _file_line(choice, f)
        named.append(f"{f.path} ({f'KiCad {line} format' if line else f'format {f.stamp}'})")
        if line and line not in lines:
            lines.append(line)
    shown = named[:_FILES_NAMED]
    if len(named) > _FILES_NAMED:
        shown.append(f"{len(named) - _FILES_NAMED} more files")
    olds = _join([f"KiCad {x}" for x in lines]) if lines else "the KiCad that wrote them"
    return _join(shown), olds, len(files)


def resolve_target(
    ctx: ToolContext, project: Project, state: ProjectState, *, prefer: str | None = None
) -> TargetChoice:
    """Which installed KiCad this project runs on, without any policy (never raises for the
    files' format; E_NO_KICAD when nothing is installed or the pin names a missing install).

    Order (review item 1 fix layer 1): `prefer` (a per-call choice, kw_live's `target_kicad`) or
    the pinned `state.target_kicad`; else the newest install whose format generation equals every
    design file's (`file_epoch`); else the install with the lowest epochs that still reads every
    file (`file_epoch_newer_readonly`); else the newest install, `readable=False`. A project
    without any stamped design file runs on the newest install.
    """
    installs = list(ctx.services.installs())
    if not installs:
        ctx.services.install(None)  # raises E_NO_KICAD with its teaching fix
    releases = format_releases(ctx)
    files = project.format_stamps()
    stamped = [f for f in files if f.stamp is not None]
    project_epoch: dict[str, int] = {}
    for f in stamped:
        project_epoch[f.kind] = max(project_epoch.get(f.kind, 0), int(f.stamp or 0))
    epochs_by_key = {i.key(): _install_epochs(ctx, i) for i in installs}
    known: dict[str, list[int]] = {}
    for kind in DESIGN_KINDS:
        vals = {
            int(r["formats"][kind]) for r in releases if isinstance((r.get("formats") or {}).get(kind), int)
        }
        vals |= {e[kind] for e in epochs_by_key.values() if kind in e}
        known[kind] = sorted(vals)
    gens: dict[str, set[int | None]] = {}
    for f in stamped:
        gens.setdefault(f.kind, set()).add(_generation_of(int(f.stamp or 0), known[f.kind]))

    def assess(epochs: dict[str, int]) -> tuple[bool, frozenset[str]]:
        reads = all(k not in epochs or epochs[k] >= e for k, e in project_epoch.items())
        wk = frozenset(
            k for k in DESIGN_KINDS if k not in project_epoch or k not in epochs or gens.get(k) == {epochs[k]}
        )
        return reads, wk

    candidates: list[dict[str, Any]] = []
    for inst in installs:
        e = epochs_by_key[inst.key()]
        reads, wk = assess(e)
        candidates.append(
            {
                "version": inst.version,
                "root": str(getattr(inst, "root", "")),
                "formats": dict(e),
                "reads": reads,
                "matches": reads and set(project_epoch) <= wk,
            }
        )

    notes: list[str] = []
    spec = prefer or state.target_kicad
    pinned = None
    if spec:
        try:
            pinned = ctx.services.install(spec)
        except KiwrapError as err:
            if prefer or err.code != "E_NO_KICAD":
                raise
            # the pinned KiCad was uninstalled (a point-release update): the files' format decides
            notes.append(
                f"the pinned KiCad {spec} is not installed; the install writing the files' format is used "
                "(kw_project(action='retarget') pins another)"
            )
    if pinned is not None:
        install = pinned
        source = SOURCE_PINNED if prefer else (state.target_source or SOURCE_PINNED)
    elif not stamped:
        install = installs[0]
        source = SOURCE_FILE_EPOCH
        if files:
            notes.append("no design file carries a (version N) stamp: the newest installed KiCad is used")
    else:
        matching = [i for i, c in zip(installs, candidates, strict=True) if c["matches"]]
        readers = [(n, i) for n, (i, c) in enumerate(zip(installs, candidates, strict=True)) if c["reads"]]
        if matching:
            install, source = matching[0], SOURCE_FILE_EPOCH
        elif readers:
            kinds = sorted(project_epoch)
            _n, install = min(
                readers,
                key=lambda item: (tuple(epochs_by_key[item[1].key()].get(k, 0) for k in kinds), item[0]),
            )
            source = SOURCE_NEWER_READONLY
        else:
            install, source = installs[0], SOURCE_FILE_EPOCH
    epochs = epochs_by_key.get(install.key()) or _install_epochs(ctx, install)
    readable, writable_kinds = assess(epochs)
    newest_gen = {k: max((g for g in v if g is not None), default=None) for k, v in gens.items()}
    mixed = {k: sorted(g for g in v if g is not None) for k, v in gens.items() if len(v) > 1}
    line: str | None = None
    hint: str | None = None
    if not mixed:
        # a mixed project belongs to no single KiCad line (fixer finding 6); a release newer than
        # the install that reads the files is never named (the matrix's unreleased 11.0.0 shares
        # the 10.99 nightly's epochs)
        line, hint = _line_for(newest_gen, releases, not_newer_than=install.version if readable else None)
        if line is None and all(v is not None for v in newest_gen.values()) and newest_gen:
            for inst in installs:  # no matrix (unit tests): name the line by an install writing it
                e = epochs_by_key[inst.key()]
                if all(e.get(k) == g for k, g in newest_gen.items()):
                    line = getattr(inst, "major_minor", None) or ".".join(inst.version.split(".")[:2])
                    break
    for kind, values in mixed.items():
        names = [f"{f.path} ({f.stamp})" for f in stamped if f.kind == kind]
        notes.append(
            f"the {kind} files mix KiCad format generations {values}: "
            + ", ".join(names)
            + " - an older KiCad cannot load the project; kw_project_upgrade (arguments: kw_project status "
            "upgrade_args) makes them uniform (or kw_snapshot restore the state before the mix)"
        )
    choice = TargetChoice(
        install=install,
        source=source,
        project_epoch=project_epoch,
        install_epoch=dict(epochs),
        writable=readable and set(project_epoch) <= writable_kinds,
        notes=notes,
        writable_kinds=writable_kinds,
        readable=readable,
        files=files,
        candidates=candidates,
        project_kicad=line,
        install_hint=hint,
        file_generations={k: sorted(g for g in v if g is not None) for k, v in gens.items()},
        known_epochs={k: list(v) for k, v in known.items()},
        releases=releases,
    )
    fmt = ", ".join(f"{k} {v}" for k, v in sorted(project_epoch.items()))
    what = f"KiCad {line} format ({fmt})" if line else f"file format {fmt}"
    if not readable:
        notes.append(
            f"the files ({what}) are newer than KiCad {install.version} "
            f"({', '.join(f'{k} {v}' for k, v in sorted(epochs.items()))}): it cannot load them"
        )
    elif stamped and not set(project_epoch) <= writable_kinds:
        if line:
            notes.append(
                f"the files are in {what}; KiCad {install.version} writes a newer format, so it runs reads only - "
                "a write needs the user's consent to upgrade the project (D21, D24) or the matching KiCad"
                + (f" ({hint})" if hint else "")
            )
        else:
            convert, _olds, _n = _conversion_text(choice)
            notes.append(
                f"KiCad {install.version} would save {convert} in the KiCad {_kicad_line(install.version)} format, "
                "so it runs reads only - a write needs the user's consent to upgrade the project first (D21, D24)"
            )
    return choice


def _uniform_label(choice: TargetChoice, kinds: Iterable[str] | None = None) -> tuple[str | None, str | None]:
    """('KiCad <line>', install hint) when the files the write concerns belong to one named KiCad
    line; (None, None) for a project whose files mix format generations or whose format no known
    release names - the prompt then names the files that would be converted instead (fixer finding
    6: 'from file format pcb 20260206, sch 20260830 format', and 'is in KiCad 11.0 format ...
    install KiCad 11.0' for a mixed 10.99/10.0 project). `release_for` never names a line newer
    than the upgrade target."""
    if choice.mixed:
        return None, None
    line, hint = choice.release_for(kinds)
    if not line:
        return None, None
    return f"KiCad {line}", hint


def preview_call(project: Project) -> str:
    """The read-only preview of an upgrade (D24: `kw_project(action='upgrade_preview')` converts a
    copy under the storage home and reports the ERC/DRC counts; it writes nothing in the project)."""
    return f'kw_project(action="upgrade_preview", project="{project.pro_path.as_posix()}")'


#: `E_FORMAT_UPGRADE_REQUIRED.example` (D24): a `kw_project_upgrade` call built from placeholders, so
#: an agent that treats `example` as the corrective call still has to take the real arguments from
#: `data.upgrade_args` (and the client asks the user before it runs)
UPGRADE_EXAMPLE = (
    "kw_project_upgrade(project=<data.upgrade_args.project>, from_kicad=<data.upgrade_args.from_kicad>, "
    "to_kicad=<data.upgrade_args.to_kicad>, files=<data.upgrade_args.files>)"
)
#: the answers `ask_upgrade_consent` reports when the elicitation did not end in an accept
NO_ANSWER = ("decline", "cancel", "error")


def format_upgrade_error(
    choice: TargetChoice,
    project: Project,
    *,
    answer: str | None = None,
    guard: dict[str, Any] | None = None,
    kinds: Iterable[str] | None = None,
) -> KiwrapError:
    """E_FORMAT_UPGRADE_REQUIRED (contract (b), D21 as amended by D24): saving would move the files
    to a newer KiCad format and no installed KiCad writes theirs. `answer`: the elicitation's
    outcome when one was sent (decline / cancel / error are all reported as "the prompt was
    declined or could not be shown": the VS Code panel declines every elicitation and a hook can
    answer one, so a decline is no answer, D24); `guard`: the stamp guard's findings when the write
    already happened and was rolled back; `kinds`: the design kinds the refused write saves (they
    pick only the KiCad line / install hint named as the alternative, `release_for`).

    `data.upgrade_args` is `project.upgrade_args(choice, project)`, the single source (D24) that
    `kw_project status` and the `kw_project_upgrade` recompute also use - independent of `kinds`,
    so a board-only refusal on a mixed-generation project (older sheets, newer board) carries
    arguments the tool accepts. `example` is a placeholder `kw_project_upgrade(...)` call; no text
    mentions the retired consent flag.

    When `choice` converts nothing (its install writes the files' own format: the stamp guard
    caught a write by ANOTHER install), `data.upgrade_args` is None and `example` is a retarget:
    arguments with an empty `files` would only make the client ask the user for a call the tool
    refuses as "nothing to convert" (review finding 8, D24: never a prompt for a call that can only
    fail)."""
    from kiwrap.core.tools.project import upgrade_args  # the kw_project module imports this one

    args: dict[str, Any] | None = upgrade_args(choice, project)
    if args is not None and not args["files"]:
        args = None
    uniform, hint = _uniform_label(choice, kinds)
    y = choice.version
    y_line = _kicad_line(y) if y else ""
    matching = [c["version"] for c in choice.candidates if c.get("matches")]
    preview = preview_call(project)
    if uniform:
        message = (
            f"{project.name} is in {uniform} format; saving it with KiCad {y} would upgrade the files to a newer "
            f"format that {uniform} cannot open"
        )
        tell = (
            f"tell the user that {project.name} would be upgraded to the KiCad {y_line} format ({uniform} can no "
            "longer open it afterwards) and name the alternatives below"
        )
    else:
        convert, olds, n = _conversion_text(choice)
        them = "it" if n == 1 else "them"
        mixed = " (its files already mix KiCad file formats)" if choice.mixed else ""
        message = (
            f"saving {project.name}{mixed} with KiCad {y} would convert {convert} to the KiCad {y_line} format, "
            f"which {olds} cannot open"
        )
        tell = (
            f"tell the user that {convert} would be converted to the KiCad {y_line} format ({olds} can no longer "
            f"open {them} afterwards) and name the alternatives below"
        )
    if args is not None:
        fix = (
            f"{tell}; {preview} previews the conversion and the ERC/DRC counts before and after without changing "
            "anything. If the user agrees: kw_project_upgrade with data.upgrade_args passed exactly as given (the "
            "client asks the user to approve that call; a snapshot is taken first), then retry this call"
        )
    else:
        fix = (
            f"{tell}; no upgrade is pending for the KiCad {project.name} runs on (KiCad {y}), so data.upgrade_args "
            "is null. To convert the files to a newer installed KiCad's format: tell the user, pin that KiCad with "
            "kw_project(action='retarget', target_kicad=...) (no file changes), then kw_project_upgrade with the "
            "upgrade_args kw_project(action='status') returns (the client asks the user)"
        )
    if matching:
        fix += (
            f"; alternative - keep the format: kw_project(action='retarget', target_kicad='{matching[0]}') "
            f"(KiCad {matching[0]} writes the files' own format)"
        )
    elif uniform:
        fix += f"; alternative - install {uniform}" + (f" ({hint})" if hint else "") + " and retry"
    elif choice.mixed:
        fix += (
            "; alternative - restore a snapshot from before the formats were mixed (kw_snapshot(action='list'), "
            "then kw_snapshot(action='restore', ref=...))"
        )
    else:
        fix += "; alternative - install the KiCad that wrote the files and retry"
    fix += "; never convert the files any other way"
    if answer in NO_ANSWER:
        why = (
            "the upgrade prompt was declined or could not be shown; kiwrap never saves a project in a newer KiCad "
            "format without the user's consent (D21, D24)"
        )
    elif guard:
        why = "a write raised the files' format stamp without the user's consent; the call was rolled back (D21)"
    else:
        why = (
            "kiwrap never saves a project in a newer KiCad format without the user's consent (D21); the only "
            "conversion is kw_project_upgrade, whose call the user approves (D24)"
        )
    files = choice.upgrade_files() or [f for f in choice.files if f.stamp is not None]
    data: dict[str, Any] = {
        "project": project.pro_path.as_posix(),
        "upgrade_args": args,
        "files": [f.to_dict() for f in files],
        "project_epoch": dict(choice.project_epoch),
        "project_kicad": uniform[len("KiCad ") :] if uniform else None,
        "mixed": choice.mixed,
        "candidates": [{"version": c["version"], "formats": c["formats"]} for c in choice.candidates],
        "upgrade_to": y or None,
        "install_hint": hint if uniform else None,
        "target_source": choice.source,
        "install_epoch": dict(choice.install_epoch),
        "preview_call": preview,
    }
    if answer:
        data["answer"] = answer
    if guard:
        data["guard"] = guard
    return KiwrapError(
        "E_FORMAT_UPGRADE_REQUIRED",
        message,
        why=why,
        fix=fix,
        candidates=matching,
        example=UPGRADE_EXAMPLE
        if args is not None
        else retarget_example(project, matching[0] if matching else None),
        data=data,
    )


def retarget_example(project: Project, version: str | None = None) -> str:
    """`kw_project(action="retarget", ...)`: the example of a refusal whose remedy is a pin first."""
    return (
        f'kw_project(action="retarget", project="{project.pro_path.as_posix()}", '
        f'target_kicad="{version or "<version>"}")'
    )


def _upgrade_refusal(
    ctx: ToolContext,
    project: Project,
    state: ProjectState,
    choice: TargetChoice,
    prefer: str | None,
    *,
    answer: str | None,
    kinds: Iterable[str] | None,
) -> KiwrapError:
    """E_FORMAT_UPGRADE_REQUIRED for a write `choice` cannot do in the files' format.

    Without `prefer` it is `format_upgrade_error(choice)`. With `prefer` (kw_live's per-call
    `target_kicad`) the refusal's `data.upgrade_args` must still be the ones `kw_project_upgrade`
    accepts, and that tool recomputes them WITHOUT any per-call choice: from the pinned or auto
    target (D24's single source; review findings 2 and 3 - the old refusal carried the `prefer`
    arguments, the tool refused them as 'nothing to convert' after the user approved the card, and
    the extension looped on it). So:
    - the project's own target converts to the same format: its `upgrade_args` are attached;
    - else `data.upgrade_args` is None and `data.retarget_first` says what to pin first
      (`kw_project(action='retarget', target_kicad=<the requested install>)`, no file changes) and
      which `upgrade_args` `kw_project status` returns after that pin - the fix says the order."""
    err = format_upgrade_error(choice, project, answer=answer, kinds=kinds)
    if not prefer:
        return err
    from kiwrap.core.tools.project import upgrade_args  # the kw_project module imports this one

    wanted = upgrade_args(choice, project)
    own: TargetChoice | None = None
    with contextlib.suppress(KiwrapError):
        own = resolve_target(ctx, project, state)
    own_args = upgrade_args(own, project) if own is not None else None
    if (
        own_args is not None
        and own_args["files"]
        and all(own_args[k] == wanted[k] for k in ("from_kicad", "to_format", "files"))
    ):
        err.data["upgrade_args"] = own_args
        return err
    pro = project.pro_path.as_posix()
    spec = wanted["to_kicad"]
    runs_on = own.version if own is not None else None
    err.data["upgrade_args"] = None
    err.data["runs_on"] = runs_on
    err.data["retarget_first"] = {
        "project": pro,
        "target_kicad": spec,
        "target_kicad_before": state.target_kicad,
        "upgrade_args": wanted if wanted["files"] else None,
    }
    keeps = (
        f"without target_kicad the call runs on KiCad {runs_on}, which writes the files' own format"
        if own is not None and own.writable
        else f"the project itself runs on KiCad {runs_on}"
        if runs_on
        else "the project has no usable target"
    )
    err.fix = (
        f"KiCad {choice.version} (the call's target_kicad) is not the KiCad {project.name} runs on: {keeps}. "
        f"To move the project to KiCad {_kicad_line(choice.version)} format instead: tell the user, then "
        f"kw_project(action='retarget', project='{pro}', target_kicad='{spec}') (no file changes), then "
        "kw_project_upgrade with the upgrade_args kw_project(action='status') returns (the client asks the user; "
        "they equal data.retarget_first.upgrade_args), then retry this call; never convert the files any other way"
    )
    err.candidates = [runs_on] if own is not None and own.writable and runs_on else []
    err.example = retarget_example(project, spec)
    return err


def format_newer_error(choice: TargetChoice, project: Project) -> KiwrapError:
    """E_FORMAT_NEWER: the design files are newer than the KiCad that would open them."""
    readers = [c["version"] for c in choice.candidates if c.get("reads")]
    fmt = ", ".join(f"{k} {v}" for k, v in sorted(choice.project_epoch.items()))
    if readers:
        fix = (
            f"KiCad {readers[0]} reads these files: kw_project(action='retarget', target_kicad='{readers[0]}'), "
            "or kw_project(action='retarget', target_kicad='auto') to follow the files' format"
        )
    else:
        fix = "install a KiCad that reads this format" + (
            f" ({choice.install_hint})" if choice.install_hint else " (the newest release or nightly)"
        )
    fix += "; reads that do not need KiCad still work: kw_pcb_get / kw_sch_get with provider='file'"
    return KiwrapError(
        "E_FORMAT_NEWER",
        f"{project.name}'s files ({fmt}) are newer than KiCad {choice.version} "
        f"({', '.join(f'{k} {v}' for k, v in sorted(choice.install_epoch.items()))})",
        why="a KiCad refuses files written by a newer version (kicad-cli exits 3: 'created with a more recent version')",
        fix=fix,
        candidates=readers,
        data={
            "project": project.pro_path.as_posix(),
            "files": [f.to_dict() for f in choice.files if f.stamp is not None],
            "project_epoch": dict(choice.project_epoch),
            "needed_kicad": choice.project_kicad,
            "candidates": [{"version": c["version"], "formats": c["formats"]} for c in choice.candidates],
            "install_hint": choice.install_hint,
            "kicad": choice.version,
            "target_source": choice.source,
        },
    )


def consent_message(project: Project, choice: TargetChoice, kinds: Iterable[str] | None = None) -> str:
    """'Upgrade <project> from KiCad <x> format to <y>? KiCad <x> will no longer open it.
    Alternatively install KiCad <x>.' (the wording of the SAFETY contract) when the files belong
    to one named KiCad line. A project whose files mix generations (or carry a format no known
    release names) is asked about the files that would actually be converted instead, and is never
    told to install a KiCad newer than the upgrade target (fixer finding 6)."""
    uniform, hint = _uniform_label(choice, kinds)
    y = choice.version
    if uniform:
        alt = f" Alternatively install {uniform}" + (f" ({hint})." if hint else ".")
        return f"Upgrade {project.name} from {uniform} format to KiCad {y}? {uniform} will no longer open it.{alt}"
    convert, olds, n = _conversion_text(choice)
    them = "it" if n == 1 else "them"
    text = (
        f"Upgrade {project.name} to the KiCad {_kicad_line(y)} format (KiCad {y})? {convert} would be converted; "
        f"{olds} will no longer open {them}."
    )
    if choice.mixed:
        return text + (
            " The project's files already mix KiCad formats, so no single KiCad writes them all; the alternative is "
            "restoring a snapshot from before they were mixed."
        )
    return text + " Alternatively install the KiCad that wrote them."


# ---- the consent record (D24) --------------------------------------------------------------------------

#: the `_meta` key Claude Code checks before its permission-mode and allow-rule branches: a tool
#: published with it asks the user on every call (docs/DECISIONS.md D24; code.claude.com/docs/en/mcp
#: "Require approval for a specific tool")
REQUIRES_USER_INTERACTION = "anthropic/requiresUserInteraction"
#: the oldest Claude Code that honours `REQUIRES_USER_INTERACTION` (a Claude Code version, not a
#: KiCad one; code.claude.com/docs/en/mcp "Require approval for a specific tool": v2.1.199)
CLAUDE_CODE_MIN_CONSENT: tuple[int, int, int] = (2, 1, 199)
#: `clientInfo.name` of Claude Code (a literal anchor of the claude.exe 2.1.280/2.1.281 bundles, D24)
CLAUDE_CODE_CLIENT = "claude-code"
#: `clientInfo.name` of the kiwrap VS Code extension's MCP client: its modal click is the consent (D24)
KIWRAP_VSCODE_CLIENT = "kiwrap-vscode"
#: the environment variable Claude Code sets for the servers it starts, and the VS Code panel's value
#: (the panel answers every elicitation with decline: ANALYSIS-TOOLING.md §6, D24)
CLAUDE_CODE_ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
PANEL_ENTRYPOINT = "claude-vscode"
_SEMVER3 = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _semver3(version: Any) -> tuple[int, int, int] | None:
    m = _SEMVER3.search(str(version or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _client_identity(request: Any) -> tuple[bool, str | None, str | None]:
    """(an MCP request is bound, clientInfo.name, clientInfo.version): the session's initialize
    parameters (2025-era handshake), else the request's `_meta` client info (2026-07-28 stateless
    requests carry it per request), else unknown."""
    if request is None:
        return False, None, None
    try:
        rc = request.request_context
    except Exception:  # noqa: BLE001 - no request context: a direct in-process call
        return False, None, None
    if rc is None:
        return False, None, None
    info: Any = None
    with contextlib.suppress(Exception):
        info = _field(rc.session.client_params, "client_info")
    if info is None:
        from mcp_types import CLIENT_INFO_META_KEY

        meta = getattr(rc, "meta", None)
        if isinstance(meta, dict):
            info = meta.get(CLIENT_INFO_META_KEY)
    name, version = _field(info, "name"), _field(info, "version")
    return True, (str(name) if name else None), (str(version) if version else None)


def consent_source(request: Any) -> dict[str, Any]:
    """The consent record of a `kw_project_upgrade` call (D24): `{gate, via, client {name, version},
    entrypoint}`.

    - `claude-code`: gate `anthropic/requiresUserInteraction`, via `client_permission_flow` - the
      client's permission flow approved the call. That is NOT a claim that a human clicked: an
      Agent SDK host's `canUseTool` receives flagged calls and may approve them programmatically
      (code.claude.com/docs/en/mcp, the "Require approval for a specific tool" section).
    - `kiwrap-vscode`: gate and via `kiwrap_vscode_modal` (the extension's modal click).
    - no MCP request bound (a direct handler call, the CLI, tests): gate `none`, via `in_process`.
    - any other client: gate `none`, via `unknown_client` (kiwrap cannot tell whether it asked; the
      conversion runs with a warning).
    The elicitation shortcut records gate and via `elicitation` (`effective_target`)."""
    bound, name, version = _client_identity(request)
    entrypoint = os.environ.get(CLAUDE_CODE_ENTRYPOINT_ENV) or None
    if not bound:
        gate, via = "none", "in_process"
    elif name == CLAUDE_CODE_CLIENT:
        gate, via = REQUIRES_USER_INTERACTION, "client_permission_flow"
    elif name == KIWRAP_VSCODE_CLIENT:
        gate, via = "kiwrap_vscode_modal", "kiwrap_vscode_modal"
    else:
        gate, via = "none", "unknown_client"
    return {"gate": gate, "via": via, "client": {"name": name, "version": version}, "entrypoint": entrypoint}


def refuse_unsupported_client(consent: dict[str, Any]) -> None:
    """E_UNSUPPORTED for a Claude Code older than `CLAUDE_CODE_MIN_CONSENT` (or whose version cannot
    be read): it does not honour `anthropic/requiresUserInteraction`, so the call may have run without
    asking the user (D24)."""
    client = consent.get("client") or {}
    if client.get("name") != CLAUDE_CODE_CLIENT:
        return
    have = _semver3(client.get("version"))
    if have is not None and have >= CLAUDE_CODE_MIN_CONSENT:
        return
    need = ".".join(str(p) for p in CLAUDE_CODE_MIN_CONSENT)
    raise KiwrapError(
        "E_UNSUPPORTED",
        f"this Claude Code ({client.get('version') or 'unknown version'}) does not ask the user before "
        "kw_project_upgrade runs; nothing was converted",
        why=f"Claude Code honours {REQUIRES_USER_INTERACTION} (a permission prompt on every call) from {need} on; "
        "without it the call is not the user's consent (D24)",
        fix=f"tell the user: update Claude Code to {need} or newer and retry, or install the KiCad that writes "
        "the files' own format (kw_project(action='status') names it)",
        candidates=[f"Claude Code >= {need}"],
        data={"client": dict(client), "client_capability": REQUIRES_USER_INTERACTION, "min_version": need},
    )


# ---- elicitation: the frozen optional shortcut (D24) ----------------------------------------------------


class UpgradeConsent(BaseModel):
    """The elicitation form of the D21 prompt (one boolean; the action carries accept/decline)."""

    upgrade: bool = Field(
        default=True,
        title="Upgrade the project files",
        description="Convert the schematic and board to the newer KiCad format (a snapshot is taken first)",
    )


def _client_elicits(request: Any) -> bool:
    """The client declared form elicitation (an empty `elicitation: {}` means form, 2025-06-18)."""
    try:
        caps = request.client_capabilities
    except Exception:  # noqa: BLE001 - no request context: no client to ask
        return False
    el = getattr(caps, "elicitation", None) if caps is not None else None
    if el is None:
        return False
    return getattr(el, "form", None) is not None or getattr(el, "url", None) is None


def elicitation_allowed(request: Any) -> bool:
    """D24: elicitation is sent only when the client declared it AND the server does not run under
    the VS Code panel (`CLAUDE_CODE_ENTRYPOINT=claude-vscode`), which declines every elicitation
    while still declaring the capability (ANALYSIS-TOOLING.md §6)."""
    if request is None or os.environ.get(CLAUDE_CODE_ENTRYPOINT_ENV) == PANEL_ENTRYPOINT:
        return False
    return _client_elicits(request)


def _answer_of(action: str, content: Any) -> str:
    if action != "accept":
        return action
    if isinstance(content, BaseModel):
        content = content.model_dump()
    if isinstance(content, dict) and content.get("upgrade") is False:
        return "decline"
    return "accept"


async def _elicit_consent(request: Any, message: str) -> Any:
    """Ask through the SDK's resolver API (`Resolve` + `Elicit`), which follows the negotiated
    protocol: a mid-call `elicitation/create` up to 2025-11-25, an `InputRequiredResult` round trip
    (question digest and answer carried in `request_state`) from 2026-07-28, where `ctx.elicit()`
    has no back-channel (ANALYSIS-TOOLING.md §6). Returns `{"consent": ElicitationResult}` or the
    `InputRequiredResult` to hand back to the client."""
    from mcp.server.mcpserver.resolve import Elicit, Resolve, build_resolver_plans, resolve_arguments

    def kiwrap_format_upgrade() -> Any:  # the wire key is this function's module:qualname (stable)
        return Elicit(message, UpgradeConsent)

    params = {"consent": (Resolve(kiwrap_format_upgrade), True)}
    plans = build_resolver_plans(params, set())
    return await resolve_arguments(params, plans, {}, request)


def ask_upgrade_consent(message: str) -> str | None:
    """Ask the MCP client's user (the frozen elicitation shortcut, D24). Returns 'accept', 'decline',
    'cancel' or 'error' (the prompt could not be shown), or None when no elicitation may be sent
    (`elicitation_allowed`). Under the 2026-07-28 protocol the question travels as an
    `InputRequiredResult` (`InputRequired` is raised) and the answer arrives on the retry."""
    request = _REQUEST.get()
    if not elicitation_allowed(request):
        return None
    try:
        import anyio.from_thread

        resolved = anyio.from_thread.run(_elicit_consent, request, message)
    except Exception:  # noqa: BLE001 - not in a worker thread / no back-channel / client error
        return "error"
    from mcp.types import InputRequiredResult

    if isinstance(resolved, InputRequiredResult):
        raise InputRequired(resolved)
    outcome = resolved.get("consent") if isinstance(resolved, dict) else None
    if outcome is None:
        return "error"
    return _answer_of(str(getattr(outcome, "action", "cancel")), getattr(outcome, "data", None))


def _upgrade_on_accept(
    ctx: ToolContext,
    project: Project,
    state: ProjectState,
    choice: TargetChoice,
    running: Mutation | None,
    prefer: str | None,
) -> dict[str, Any]:
    """The elicitation said yes: run the ONE conversion core (`project.run_upgrade`) with this
    choice's `upgrade_args` and the consent gate/via `elicitation` - exactly `upgrade_files()`, ERC/DRC
    before and after, a snapshot first (D24; it used to convert every design file unchecked).
    Inside a running mutation it converts under that call's lock; else in its own."""
    from kiwrap.core.tools.project import UpgradeArgs, run_upgrade, upgrade_args

    args = upgrade_args(choice, project)
    consent = {**consent_source(_REQUEST.get()), "gate": "elicitation", "via": "elicitation"}
    receipt = run_upgrade(
        ctx,
        UpgradeArgs(**{k: args[k] for k in ("project", "from_kicad", "to_kicad", "files")}),
        consent,
        running=running,
        prefer=prefer,
    )
    pinned = running.state if running is not None else project.load_state()
    state.target_kicad, state.target_source = pinned.target_kicad, pinned.target_source
    return dict(receipt.data)


def effective_target(
    ctx: ToolContext,
    project: Project,
    state: ProjectState,
    *,
    for_write: bool,
    kinds: Iterable[str] | None = None,
    allow_newer: bool = False,
    prefer: str | None = None,
) -> TargetChoice:
    """The install a tool call runs on, with the D21 policy applied (review item 1).

    - Files newer than the chosen install: `E_FORMAT_NEWER`, except for a read that can be served
      without KiCad loading the files (`allow_newer=True`: kw_pcb_get / kw_sch_get / ... fall
      through to the file provider).
    - `for_write` and the install would move a file of `kinds` (default: every design kind) to a
      newer KiCad line: `E_FORMAT_UPGRADE_REQUIRED` with `data.upgrade_args` (D24), unless the
      frozen elicitation shortcut may ask and the user accepts - then the project is converted by
      `kw_project_upgrade`'s core (inside this call's lock and snapshot history) and pinned. With
      `prefer`, the refusal carries only arguments the tool accepts (`_upgrade_refusal`).
    Inside a `mutation()` the choice is recorded on it (`Mutation.target`: the planner's ipc11
    precondition and the receipt's `data.target` read it)."""
    if for_write:
        readonly.refuse_writes("saving design files")  # D23: a non-persisting call never writes one
    choice = resolve_target(ctx, project, state, prefer=prefer)
    running = current_mutation(project)
    if not choice.readable and (for_write or not allow_newer):
        raise format_newer_error(choice, project)
    if for_write and not choice.writable_for(kinds):
        answer = ask_upgrade_consent(consent_message(project, choice, kinds))
        if answer != "accept":
            raise _upgrade_refusal(ctx, project, state, choice, prefer, answer=answer, kinds=kinds)
        report = _upgrade_on_accept(ctx, project, state, choice, running, prefer)
        choice = resolve_target(ctx, project, state)
        choice.upgraded = report
        choice.notes.append(
            f"the user agreed (elicitation): the project was upgraded to KiCad {choice.version} format "
            f"(pre-upgrade snapshot {str(report.get('snapshot_before') or '')[:12]})"
        )
    if running is not None:
        running.target = choice
    return choice


def _same_install(a: Any, b: Any) -> bool:
    try:
        return bool(a.key() == b.key())
    except AttributeError:
        return a is b


def saves_in_format(install: Any, caps: Any, project: Project, kind: str, m: Mutation | None = None) -> bool:
    """True when `install` saving a `kind` file of `project` keeps every file of that kind in
    its KiCad line: the precondition of a refill save (`drc_validator`, `kw_pcb_drc`) and of an
    ipc11 write (the planner; review item 1 (b)/(c)). Uses the running mutation's resolved target
    when it is this install; else every stamped file of the kind must equal `formats.<kind>`.
    An install whose record has no `formats.<kind>` cannot be judged: True (the stamp guard
    still watches the result)."""
    running = m if m is not None else current_mutation(project)
    target = getattr(running, "target", None)
    if (
        isinstance(target, TargetChoice)
        and target.install is not None
        and _same_install(target.install, install)
    ):
        return target.readable and kind in target.writable_kinds
    try:
        epoch = int(caps[f"formats.{kind}"])
    except (KeyError, TypeError, ValueError):
        return True
    return all(f.stamp == epoch for f in project.format_stamps() if f.kind == kind and f.stamp is not None)


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
    #: `{relpath: (version N)}` of the design files when the call began (the stamp guard's baseline)
    stamps_before: dict[str, int | None] = field(default_factory=dict)
    #: the call may raise format stamps (kw_project_upgrade, or after the user consented: D24)
    allow_format_upgrade: bool = False
    #: what `effective_target()` resolved inside this call (None until a tool asks)
    target: Any = None
    #: the state as loaded, for the stamp guard's rollback
    initial_state: ProjectState | None = None
    #: the report of an upgrade the user consented to during this call (`data.upgrade`)
    upgrade_report: dict[str, Any] | None = None
    #: False inside a non-persisting call (D23): no snapshot, the state stays in memory
    persist: bool = True
    #: the owned files (`snapshots.owned_files`) when the call began: the stamp guard's rollback
    #: removes what the call added beside them (fixer finding 5), None when not listed
    files_before: frozenset[str] | None = None
    #: `{kind: known release epochs}` for the stamp guard when the tool resolved no `target`
    #: (`kw_snapshot restore`: a re-save inside one KiCad line is not an upgrade; review finding 1)
    guard_epochs: dict[str, list[int]] | None = None

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


#: the mutation running in this thread (its project lock is held): `effective_target()` records
#: its choice on it and upgrades inside it, the planner reads its stamps (ipc11 precondition)
_CURRENT: contextvars.ContextVar[Mutation | None] = contextvars.ContextVar("kiwrap_mutation", default=None)


def current_mutation(project: Project | None = None) -> Mutation | None:
    """The mutation running in this thread (on `project`, when given), else None."""
    m = _CURRENT.get()
    if m is None or project is None:
        return m
    try:
        same = m.project.dir.resolve() == project.dir.resolve()
    except OSError:
        same = m.project.dir == project.dir
    return m if same else None


def stamp_map(project: Project) -> dict[str, int | None]:
    return {f.path: f.stamp for f in project.format_stamps()}


def _owned_listing(project: Project) -> frozenset[str] | None:
    """The project's owned files now (relative posix paths), None when they cannot be listed."""
    try:
        return frozenset(owned_files(project.dir))
    except OSError:
        return None


def _remove_added_files(m: Mutation) -> list[str]:
    """After the stamp guard put the files back from the baseline: delete the owned files the call
    CREATED (absent from the prologue's listing; `.kiwrap/` state excluded). A snapshot restore
    only rewinds tracked files, so a template `.kicad_pro` of a rolled-back create or a
    `.kiwrap-upgrade.tmp` stayed behind and went into the next snapshot (fixer finding 5)."""
    if m.files_before is None:
        return []
    removed: list[str] = []
    for rel in sorted(_owned_listing(m.project) or ()):
        if rel in m.files_before or rel.startswith(".kiwrap/"):
            continue
        try:
            (m.project.dir / rel).unlink()
        except OSError:
            continue
        removed.append(rel)
    return removed


@contextmanager
def scratch_folder(ctx: ToolContext, kind: str) -> Iterator[Path]:
    """A throw-away folder `<storage>/tmp/<kind>/<id>/` for a kicad-cli intermediate that must never
    land in the project (an ERC / DRC report JSON, a netlist), removed on exit. The backends'
    default output is BESIDE the input (`<root>.erc.json`, `<board>.drc.json`, `<root>.net`):
    requests/kiwrap-feature-requests.review.md item 2 and §3 small defects found those left in a
    reviewed project."""
    folder = Path(ctx.services.storage_home) / "tmp" / kind / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _guard_writer(m: Mutation, rose: list[dict[str, Any]]) -> str | None:
    """The installed KiCad that wrote the raised stamps (its `formats.<kind>` equals every raised
    file's new stamp), newest first; None when no single install explains them. The stamp guard's
    error names it as `upgrade_to` - the effective target may well write the files' own format
    (the write ran on another install), so naming the target would be wrong (EXTENSION build
    report, review item 1)."""
    target = m.target if isinstance(m.target, TargetChoice) else None
    if target is None or not rose:
        return None
    kinds = {".kicad_sch": "sch", ".kicad_pcb": "pcb"}
    for cand in target.candidates:  # newest first (discovery order)
        formats = cand.get("formats") or {}
        if all(formats.get(kinds.get(Path(r["path"]).suffix, "")) == r["stamp_after"] for r in rose):
            return str(cand.get("version") or "") or None
    return None


@contextmanager
def mutation(
    ctx: ToolContext, project: Project, tool: str, *, force: Any = None, gate: bool = True
) -> Iterator[Mutation]:
    """Lock the project, refuse open editors, detect external edits, check the gate, snapshot the
    state before the call when it differs from the last snapshot (so an external edit or the
    remains of a crashed call stay reachable by `kw_snapshot restore`), record the design files'
    format stamps, then (stamp guard) undo the call if it raised a stamp without consent, and
    snapshot + save state on the way out (also on failure, so a half-applied change is committed
    as its own snapshot and remains undoable; the message then carries `FAILED <code>`).

    A consent question under the 2026-07-28 protocol (`InputRequired`) leaves no trace: nothing
    was written yet, the call is repeated with the answer.

    Inside a non-persisting call (D23, `readonly.current()`) the same tool body runs detached:
    `_detached_mutation`."""
    call = readonly.current()
    if call is not None:
        with _detached_mutation(ctx, project, tool, call, force=force, gate=gate) as dm:
            yield dm
        return
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
        m.initial_state = copy.deepcopy(state)
        m.stamps_before = stamp_map(project)
        m.files_before = _owned_listing(project)
        _snapshot_before(m)
        token = _CURRENT.set(m)
        finish = True
        guard_error: KiwrapError | None = None
        try:
            yield m
        except InputRequired:
            finish = False
            raise
        except BaseException as exc:
            if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                err = to_kiwrap_error(exc)
                m.failure_code = err.code
                found = _format_guard(m)
                if found is not None:
                    # the rollback is reported whatever failed - a PermissionError mid-write too
                    # (fixer finding 5: only a KiwrapError used to carry `format_guard`)
                    err.data.setdefault("format_guard", found)
                    if err is not exc:
                        raise err from exc
            raise
        else:
            found = _format_guard(m)
            if found is not None and found.get("restored"):
                guard_error = format_upgrade_error(
                    m.target if isinstance(m.target, TargetChoice) else _guard_choice(ctx, m),
                    project,
                    guard=found,
                )
                writer = _guard_writer(m, list(found.get("files") or []))
                if writer:
                    found["written_by"] = writer
                    guard_error.data["upgrade_to"] = writer
                    guard_error.message = (
                        f"{project.name}: a write by KiCad {writer} raised the files' format "
                        + ", ".join(
                            f"{r['path']} {r['stamp_before']} -> {r['stamp_after']}" for r in found["files"]
                        )
                        + "; the call was rolled back"
                    )
                    target = m.target if isinstance(m.target, TargetChoice) else None
                    if target is not None and target.writable and target.version != writer:
                        # the project's own KiCad writes its format; another install did the write
                        # D24: the conversion is kw_project_upgrade with the upgrade_args that
                        # `project.upgrade_args` gives once the writer is the pinned target
                        guard_error.fix = (
                            f"the project runs on KiCad {target.version}, which writes its format: pin it with "
                            f"kw_project(action='retarget', target_kicad='{target.version}') and retry; converting "
                            f"to KiCad {writer} format needs the user's consent: tell the user, "
                            f"kw_project(action='retarget', target_kicad='{writer}'), then kw_project_upgrade with the "
                            "upgrade_args kw_project(action='status') returns (the client asks the user)"
                        )
                        guard_error.candidates = [target.version]
                        # data.upgrade_args is None here (the target converts nothing): the
                        # corrective call is the pin, never kw_project_upgrade (review finding 8)
                        guard_error.example = retarget_example(project, target.version)
                m.failure_code = guard_error.code
        finally:
            _CURRENT.reset(token)
            if finish:
                _finish(ctx, m)
        if guard_error is not None:
            raise guard_error


@contextmanager
def _detached_mutation(
    ctx: ToolContext, project: Project, tool: str, call: readonly.DetachedCall, *, force: Any, gate: bool
) -> Iterator[Mutation]:
    """`mutation()` for a non-persisting call (D23, review item 2 / theme T2 `detached()`): the
    tool must be one a read-only call may run (else E_READONLY - normally refused before the
    handler); the lock is the project's `.kiwrap/lock` when `.kiwrap/` exists, else one under the
    storage home; no editor-lock refusal (nothing is written); the state comes from and goes back
    to the in-memory store; no snapshot before or after; the stamp guard is replaced by the call's
    project guard, which puts back any design file the call changed."""
    readonly.refuse_writes_unless_detachable(tool)
    parsed = Force.parse(force)
    release_own_sessions(ctx, project)
    with readonly.detached_lock(ctx.services.storage_home, project):
        state = readonly.load_state(project)
        external = check_external_edit(project, state)
        forced = check_gate(tool, state.stage, parsed) if gate else None
        m = Mutation(
            project=project, state=state, tool=tool, external=external, forced_reason=forced, persist=False
        )
        m.initial_state = copy.deepcopy(state)
        m.stamps_before = stamp_map(project)
        token = _CURRENT.set(m)
        finish = True
        try:
            yield m
        except InputRequired:
            finish = False
            raise
        except BaseException as exc:
            if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                m.failure_code = to_kiwrap_error(exc).code
            raise
        finally:
            _CURRENT.reset(token)
            if finish:
                _finish(ctx, m)


def _guard_choice(ctx: ToolContext, m: Mutation) -> TargetChoice:
    """A `TargetChoice` for the guard's error when the tool never resolved one."""
    try:
        return resolve_target(ctx, m.project, m.state)
    except KiwrapError:
        return TargetChoice(
            install=None, source=SOURCE_FILE_EPOCH, project_epoch={}, install_epoch={}, writable=False
        )


def _format_guard(m: Mutation) -> dict[str, Any] | None:
    """The stamp guard (review item 1 fix layer 3): compare every design file's `(version N)`
    with the prologue's. A stamp that rose (or a new file stamped above everything its kind had)
    without `allow_format_upgrade` is an up-conversion nobody consented to: the files go back to
    the call's baseline snapshot and the state to what it was (unless a live GUI session holds the
    project - then the disk is the GUI's and only a warning is given). Returns the findings, or
    None when nothing rose.

    No tool is exempt: `kw_snapshot restore` of a snapshot holding newer-format files (the state
    after a conversion the user undid, a pre-call snapshot of an external save) is an up-conversion
    too and is rolled back like any other write - only `kw_project_upgrade` converts (D24; review
    finding 1). Restoring `snapshot_before` only lowers the stamps and passes."""
    if m.allow_format_upgrade:
        return None
    after = stamp_map(m.project)
    known = m.target.known_epochs if isinstance(m.target, TargetChoice) else (m.guard_epochs or {})

    def generation(rel: str, stamp: int) -> int:
        # a re-save inside one KiCad line (20260101 -> 20260306 by 10.0.6) is not an upgrade;
        # without a resolved target the raw stamps are compared
        epochs = known.get({".kicad_sch": "sch", ".kicad_pcb": "pcb"}.get(Path(rel).suffix, ""), [])
        gen = _generation_of(stamp, epochs) if epochs else None
        return gen if gen is not None else stamp

    top: dict[str, int] = {}
    for rel, stamp in m.stamps_before.items():
        kind = Path(rel).suffix
        if stamp is not None:
            top[kind] = max(top.get(kind, 0), stamp)
    rose: list[dict[str, Any]] = []
    for rel, stamp in after.items():
        if stamp is None:
            continue
        before = m.stamps_before.get(rel)
        limit = before if before is not None else top.get(Path(rel).suffix)
        if limit is not None and generation(rel, stamp) > generation(rel, limit):
            rose.append({"path": rel, "stamp_before": before, "stamp_after": stamp})
    if not rose:
        return None
    found: dict[str, Any] = {"files": rose, "restored": False}
    if m.bound is not None:
        m.receipt.warnings.append(
            "format_upgraded: "
            + ", ".join(f"{r['path']} {r['stamp_before']} -> {r['stamp_after']}" for r in rose)
            + " - the live KiCad session saved the files in its newer format; kw_snapshot restore "
            + f"{(m.baseline or '')[:12]} brings the previous format back"
        )
        if "format_upgraded" not in m.receipt.degraded:
            m.receipt.degraded.append("format_upgraded")
        return found
    if m.baseline:
        try:
            snapshot_restore(m.project.dir, m.baseline, tool=m.tool, commit=False)
        except KiwrapError as exc:
            found["restore_error"] = exc.message
        else:
            found["restored"] = True
            found["snapshot"] = m.baseline
            removed = _remove_added_files(m)
            if removed:
                found["removed"] = removed
            if m.initial_state is not None:
                m.state = copy.deepcopy(m.initial_state)
            m.receipt.changed = {"added": [], "updated": [], "removed": []}
    if not found["restored"]:
        m.receipt.warnings.append(
            "format_upgraded: "
            + ", ".join(f"{r['path']} {r['stamp_before']} -> {r['stamp_after']}" for r in rose)
            + " - could not be rolled back automatically; restore a snapshot from before this call"
        )
    return found


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
    r.next_suggested = suggest_next(ctx, m.state, r.next_suggested, target=m.target)
    if isinstance(m.target, TargetChoice):
        r.data.setdefault("target", m.target.summary())
        if m.target.source == SOURCE_NEWER_READONLY:
            r.warnings.extend(n for n in m.target.notes if n not in r.warnings)
    if m.upgrade_report is not None:
        r.data.setdefault("upgrade", m.upgrade_report)
    try:
        after = stamp_map(m.project)
    except OSError:
        after = m.stamps_before
    if after != m.stamps_before:
        # every call that changed a design file's stamp (or added one) says so (review item 1)
        r.data.setdefault("epochs", {"before": dict(m.stamps_before), "after": after})
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
    if not m.persist:
        # D23: the stage, hashes and validator history stay in this process's memory; no
        # .kiwrap/state.json, no snapshot
        readonly.store_state(m.project, m.state)
        r.snapshot = None
        return
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
    """Read-only tools still detect external edits (and persist the downgrade - in memory only
    inside a non-persisting call, D23)."""
    if readonly.current() is not None:
        state = readonly.load_state(project)
        external = check_external_edit(project, state)
        if external.external_edit:
            readonly.store_state(project, state)
        return state, external
    state = project.load_state()
    external = check_external_edit(project, state)
    if external.external_edit:
        project.save_state(state)
    return state, external
