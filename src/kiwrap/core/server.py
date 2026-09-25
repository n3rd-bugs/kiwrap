"""`build_server()`: the kiwrap MCP server (mcp 2.2 `MCPServer`).

Verified transport behaviour this relies on (docs/VERIFICATION.md claim 11, spikes/mcp-verify,
spikes/verify-mcp22): `from mcp.server import MCPServer`; tools registered with
`@mcp.tool()`; a tool returning `CallToolResult(content=[...], structured_content=...)` reaches
2025-06-18 and 2026-07-28 clients unchanged over stdio and streamable HTTP; `ImageContent`
PNG blocks are delivered as-is. `outputSchema` comes from the `Annotated[CallToolResult,
ReceiptOut]` return annotation of every registered function.

Design (docs/ARCHITECTURE.md §9): stateless; the project path is the handle; deterministic
`tools/list`; `ToolAnnotations` on every tool; errors as `isError` with the structured teaching
error; `_meta['anthropic/maxResultSizeChars']` on report tools.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult
from pydantic import TypeAdapter, ValidationError

from kiwrap import __version__
from kiwrap.core.services import Services
from kiwrap.core.state import KiwrapError, readonly
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.receipts import error_to_result
from kiwrap.core.tools import ALL_TOOLS, TOOL_NAMES
from kiwrap.core.tools._base import ToolContext, ToolSpec, make_callable, to_kiwrap_error
from kiwrap.platform import current

INSTRUCTIONS = (
    "kiwrap drives installed KiCad binaries. Flow: kw_env -> kw_project(create) -> kw_sch_add_symbol (returns "
    "absolute pin endpoints) -> kw_sch_connect (by ref+pin) / kw_sch_add_item (power, PWR_FLAG, no-connect, "
    "labels, sheets) -> kw_sch_stats / kw_sch_get -> kw_sch_check -> kw_sch_annotate -> kw_sch_assign_footprints "
    "-> kw_pcb_setup -> kw_pcb_sync (gated on erc_clean) -> kw_pcb_place with kw_pcb_check / kw_pcb_get feedback "
    "-> kw_pcb_zones / kw_pcb_route_auto (Freerouting; kw_pcb_fix re-routes must_fix DRC) or kw_pcb_route "
    "-> kw_pcb_drc -> kw_fab_export. Every mutation runs its validator (ERC / DRC / placement) "
    "and puts the numbers in the receipt (validators[], stage, next_suggested, snapshot). "
    "Data first: after every change query structured state (kw_sch_get / kw_sch_stats / kw_pcb_get / "
    "kw_pcb_check) and act on it; renders are secondary. Never edit .kicad_* files by hand (stages downgrade "
    "with external_edit); never guess library ids or pin positions. "
    "Positions are millimetres. Errors are isError results with {code, message, why, fix, candidates, example}; "
    "kw_help(topic=<code or tool>) explains each. kw_snapshot(restore) is undo. "
    "A project runs on the installed KiCad that writes its files' own format (kw_project status: "
    "target_effective, epoch_match); a save that would move the files to a newer KiCad format stops with "
    "E_FORMAT_UPGRADE_REQUIRED: tell the user, then kw_project_upgrade with the error's data.upgrade_args "
    '(Claude Code asks them), or have them install the matching KiCad; kw_project(action="upgrade_preview") '
    "previews; never convert files otherwise. persist=false on any call writes nothing into the project "
    "(kw_help topic readonly)."
)

#: appended to INSTRUCTIONS by a read-only server (docs/DECISIONS.md D23)
READONLY_INSTRUCTIONS = (
    " THIS SERVER IS READ-ONLY (kiwrap serve --readonly / KIWRAP_READONLY=1): nothing is written into projects, "
    "reports and exports go to <storage>/results/ or an absolute out_dir you pass, and the mutating tools refuse "
    "with E_READONLY - review with the read tools, kw_sch_check, kw_pcb_drc (then kw_pcb_drc(action='report')), "
    "kw_fab_bom and kw_fab_export(dry_run=true or out_dir=...)."
)


def instructions(ctx: ToolContext | None = None) -> str:
    """The server instructions; a read-only server says so up front (D23)."""
    return INSTRUCTIONS + (READONLY_INSTRUCTIONS if readonly.server_readonly(ctx) else "")


REPORT_TOOLS = {
    "kw_sch_check",
    "kw_pcb_drc",
    "kw_fab_export",
    "kw_env",
    "kw_project",
    "kw_project_upgrade",
    "kw_sch_get",
    "kw_sch_stats",
    "kw_sch_diff",
    "kw_pcb_get",
    "kw_pcb_check",
    # authoring receipts embed their validator's violations / the placement items
    "kw_sch_add_symbol",
    "kw_sch_connect",
    "kw_sch_add_item",
    "kw_sch_edit",
    "kw_sch_annotate",
    "kw_sch_assign_footprints",
    "kw_pcb_setup",
    "kw_pcb_sync",
    "kw_pcb_place",
    "kw_pcb_zones",
    "kw_pcb_route",
    # the routing receipts embed the router log summary, the SES report and the DRC
    "kw_pcb_route_auto",
    "kw_pcb_fix",
}
MAX_RESULT_CHARS = 60_000


#: tools a read-only server does not register (docs/DECISIONS.md D24: a flagged tool whose every call
#: would be refused would still make the client ask the user first); a call by name gets E_READONLY
UNREGISTERED_WHEN_READONLY = frozenset({"kw_project_upgrade"})


@dataclass(frozen=True)
class MovedAction:
    """A retired action or argument and where it went (a teaching E_BAD_ARGS, never an alias: D24,
    and the pre-1.0 policy of D25)."""

    message: str
    why: str
    fix: str
    candidates: tuple[str, ...]
    example: str


_UPGRADE_MOVED = MovedAction(
    message="kw_project(action='upgrade') was retired: converting the files is the tool kw_project_upgrade",
    why=(
        "the retired consent flag was the model attesting the user's consent to itself; kw_project_upgrade is "
        "approved by the user through the client's permission prompt, call by call (D24)"
    ),
    fix=(
        "tell the user what would change; kw_project(action='upgrade_preview', project=...) previews the conversion "
        "and the ERC/DRC counts, kw_project(action='status', project=...) returns upgrade_args; if the user agrees, "
        "kw_project_upgrade with those upgrade_args exactly as given"
    ),
    candidates=("kw_project_upgrade", "kw_project(action='upgrade_preview')", "kw_project(action='status')"),
    example='kw_project(action="upgrade_preview", project="<project>")',
)
_NO_FLAG_ON_UPGRADE = MovedAction(
    message="kw_project_upgrade takes no consent flag and no dry run: the client's permission prompt is the consent",
    why="the user approves each kw_project_upgrade call in the client; a preview is a separate read (D24)",
    fix=(
        "call kw_project_upgrade with exactly project, from_kicad, to_kicad and files from data.upgrade_args; "
        "kw_project(action='upgrade_preview', project=...) previews without changing anything"
    ),
    candidates=("kw_project_upgrade", "kw_project(action='upgrade_preview')"),
    example='kw_project(action="upgrade_preview", project="<project>")',
)
_TARGET_KICAD_NAME = MovedAction(
    message="kw_project has no 'to_kicad': the install argument is target_kicad",
    why=(
        "retarget pins an installed KiCad with target_kicad and never touches the files; converting the files to a "
        "newer format is the separate tool kw_project_upgrade, which the user approves call by call (D24)"
    ),
    fix=(
        "to pin an install: kw_project(action='retarget', project=..., target_kicad=...); create takes "
        "target_kicad too. Only if the user asked to convert the files: kw_project_upgrade with the upgrade_args "
        "kw_project(action='status') returns"
    ),
    candidates=("kw_project(action='retarget')", "kw_project(action='status')"),
    example='kw_project(action="retarget", project="<project>", target_kicad="<version>")',
)
#: `(tool, retired action value or argument name)` -> the teaching E_BAD_ARGS (D24). The SDK drops
#: unknown arguments silently, so a stale consent flag would otherwise run as a plain status call,
#: and a retarget with `to_kicad` instead of `target_kicad` would silently UNPIN the project. A
#: stale upgrade call always carries `action='upgrade'` (caught by the action key), so `to_kicad`
#: on any other action is the argument-name mix-up, not an upgrade (review finding 10).
MOVED_ACTIONS: dict[tuple[str, str], MovedAction] = {
    ("kw_project", "upgrade"): _UPGRADE_MOVED,
    ("kw_project", "confirm"): _UPGRADE_MOVED,
    ("kw_project", "dry_run"): _UPGRADE_MOVED,
    ("kw_project", "to_kicad"): _TARGET_KICAD_NAME,
    ("kw_project_upgrade", "confirm"): _NO_FLAG_ON_UPGRADE,
    ("kw_project_upgrade", "dry_run"): _NO_FLAG_ON_UPGRADE,
}


def moved_action_error(name: str, arguments: dict[str, Any] | None) -> KiwrapError | None:
    """The teaching E_BAD_ARGS for a retired action / argument (`MOVED_ACTIONS`), else None."""
    args = arguments or {}
    action = args.get("action")
    hit = MOVED_ACTIONS.get((name, action)) if isinstance(action, str) else None
    arg: str | None = None
    if hit is None:
        for key in sorted(args):
            if key != "action" and (name, key) in MOVED_ACTIONS:
                hit, arg = MOVED_ACTIONS[(name, key)], key
                break
    if hit is None:
        return None
    what = f"action={action!r}" if arg is None else f"the argument {arg!r}"
    return KiwrapError(
        "E_BAD_ARGS",
        f"{hit.message} ({name} with {what})",
        why=hit.why,
        fix=hit.fix,
        candidates=list(hit.candidates),
        example=hit.example,
        data={"moved": {"tool": name, "action": action if arg is None else None, "argument": arg}},
    )


#: tools published WITHOUT the common `persist` argument (D24: `ToolSpec.common_persist=False`)
_NO_PERSIST_TOOLS = frozenset({"kw_project_upgrade"})
_BOOL = TypeAdapter(bool)


def unpublished_persist_error(name: str, arguments: dict[str, Any] | None) -> KiwrapError | None:
    """A `persist` argument on a tool that does not publish one (`kw_project_upgrade`, D24). The
    SDK drops an unpublished key silently, so the call would convert; every other tool coerces the
    same value (pydantic's lax bool) and writes nothing (D23). Review finding 5: `"false"` and `0`
    converted all the files after a card that showed `persist: "false"`. A value that reads as False
    is E_READONLY (the call's own read-only mode); any other value is E_BAD_ARGS - the tool takes no
    persist."""
    args = arguments or {}
    if name not in _NO_PERSIST_TOOLS or "persist" not in args:
        return None
    value = args["persist"]
    try:
        persist = _BOOL.validate_python(value)
    except ValidationError:
        persist = None
    if persist is False:
        return readonly.readonly_error(name, "converts design files to a newer KiCad format", mode="call")
    return KiwrapError(
        "E_BAD_ARGS",
        f"{name} takes no persist argument (got {value!r}); nothing was converted",
        why="the client's permission prompt for this call is the user's consent; a persist value would only make "
        "the card say something the call does not do (D24)",
        fix=f"call {name} with exactly project, from_kicad, to_kicad and files from data.upgrade_args; "
        "kw_project(action='upgrade_preview', project=...) previews without changing anything",
        candidates=[name, "kw_project(action='upgrade_preview')"],
        example='kw_project(action="upgrade_preview", project="<project>")',
        data={
            "argument": "persist",
            "given": value if isinstance(value, (bool, int, float, str)) else repr(value),
        },
    )


def unregistered_error(name: str) -> KiwrapError:
    """E_READONLY for a catalog tool a read-only server does not register (D24): its candidates are
    the read-only alternatives (`readonly.READ_ALTERNATIVES`: the preview and status)."""
    return readonly.readonly_error(name, "converts design files to a newer KiCad format", mode="server")


class KiwrapServer(MCPServer):
    """`MCPServer` whose schema-level rejections are teaching errors too.

    The SDK validates the arguments against the published inputSchema BEFORE the registered
    function runs and answers an unknown tool name itself; both surface as `ToolError` from
    `call_tool`, which `_handle_call_tool` turns into a bare text `isError` result (a pydantic
    dump such as "1 validation error for kw_projectArguments ... action Field required"). Those
    are exactly the mistakes an LLM makes most, so this override maps them onto
    `{code: E_BAD_ARGS, why, fix, candidates}` like every other failure. `_handle_call_tool`
    dispatches through `self.call_tool`, so the override is reached over the wire.

    docs/DECISIONS.md D24: a retired action or argument (`MOVED_ACTIONS`: `kw_project(action=
    'upgrade')`, a stray `confirm` / `dry_run`) is a teaching E_BAD_ARGS naming `kw_project_upgrade`
    - checked before the SDK, which silently drops unknown arguments - and a call to a catalog tool
    a read-only server did not register (`kw_project_upgrade`) is E_READONLY, not a bare ToolError.
    """

    #: the catalog tools `register_tools` left out because the server is read-only (D24)
    kiwrap_unregistered: frozenset[str] = frozenset()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult | Any:
        if name in self.kiwrap_unregistered:
            # first: on a read-only server the tool does not exist, whatever its arguments (a stale
            # `confirm` there must not teach a call that would only be refused next - D24)
            return error_to_result(unregistered_error(name))
        moved = moved_action_error(name, arguments)
        if moved is not None:
            return error_to_result(moved)
        stray = unpublished_persist_error(name, arguments)
        if stray is not None:
            return error_to_result(stray)
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError):
                raise
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                return error_to_result(to_kiwrap_error(cause))
            if name not in TOOL_NAMES:
                return error_to_result(
                    KiwrapError(
                        "E_BAD_ARGS",
                        f"unknown tool {name!r}",
                        why="the name is not one of the tools this server publishes",
                        fix="call one of the listed tools; kw_help() gives the overview",
                        candidates=[t for t in TOOL_NAMES if t not in self.kiwrap_unregistered],
                    )
                )
            raise


def resolve_storage_home(storage_home: Path | None) -> Path:
    home = storage_home or current().storage_home
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


def build_context(storage_home: Path | None = None, services: Services | None = None) -> ToolContext:
    home = resolve_storage_home(storage_home)
    svc = services or Services.real(home)
    return ToolContext(services=svc, jobs=JobQueue(home / "jobs"), server_version=__version__)


def register_tools(mcp: MCPServer, ctx: ToolContext, specs: tuple[ToolSpec, ...] = ALL_TOOLS) -> None:
    """Register `specs` in order; a read-only server (D23) leaves out `UNREGISTERED_WHEN_READONLY`
    (D24: `kw_project_upgrade`). `spec.meta` is published as the tool's `_meta` (mcp 2.2), which
    carries `anthropic/requiresUserInteraction` for `kw_project_upgrade`."""
    skip = UNREGISTERED_WHEN_READONLY if readonly.server_readonly(ctx) else frozenset()
    skipped = set(getattr(mcp, "kiwrap_unregistered", frozenset()))
    for spec in specs:
        if spec.name in skip:
            skipped.add(spec.name)
            continue
        meta = dict(spec.meta)
        if spec.name in REPORT_TOOLS:
            meta.setdefault("anthropic/maxResultSizeChars", MAX_RESULT_CHARS)
        mcp.tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=spec.annotations,
            meta=meta or None,
        )(make_callable(spec, ctx))
    with contextlib.suppress(AttributeError, TypeError):
        mcp.kiwrap_unregistered = frozenset(skipped)  # type: ignore[attr-defined]


def shutdown_jobs(mcp: MCPServer, *, wait_s: float = 10.0) -> list[str]:
    """Cancel the background jobs still running (a route job's JVM dies with it) and wait a
    bounded time; idempotent, safe from `atexit`."""
    ctx = getattr(mcp, "kiwrap_context", None)
    if ctx is None:
        return []
    try:
        return ctx.jobs.shutdown(cancel_running=True, wait_s=wait_s)
    except Exception:  # noqa: BLE001 - shutdown must never raise into the transport teardown
        return []


def install_shutdown_hooks(mcp: MCPServer) -> None:
    """`atexit`: when the host ends the session by closing stdio (or kills the interpreter
    orderly), the running jobs are cancelled before the pool threads are joined at exit."""
    import atexit

    atexit.register(shutdown_jobs, mcp)


def build_server(
    storage_home: Path | None = None, *, services: Services | None = None, context: ToolContext | None = None
) -> MCPServer:
    """Create the server with every catalog tool (`ALL_TOOLS`) registered in catalog order (setup,
    then per stage: the authoring tools, the data-first reads, the validator, the render; the
    catalog is the single source of the order, so `tools/list` is identical across processes)."""
    ctx = context or build_context(storage_home, services)
    mcp = KiwrapServer(name="kiwrap", version=__version__, instructions=instructions(ctx))
    register_tools(mcp, ctx)
    mcp.kiwrap_context = ctx  # type: ignore[attr-defined]  # handy for tests and the CLI
    return mcp
