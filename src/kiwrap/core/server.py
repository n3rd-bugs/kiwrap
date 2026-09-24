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

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult
from pydantic import ValidationError

from kiwrap import __version__
from kiwrap.core.services import Services
from kiwrap.core.state import KiwrapError
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
    "-> kw_pcb_zones / kw_pcb_route_auto (Freerouting; kw_pcb_fix repairs must_fix DRC by re-routing) or kw_pcb_route -> kw_pcb_drc -> kw_fab_export. Every mutation runs its validator (ERC / DRC / "
    "placement checks) and puts the numbers in the receipt (validators[], stage, next_suggested, snapshot). "
    "Data first: after every change query structured state (kw_sch_get / kw_sch_stats / kw_pcb_get / "
    "kw_pcb_check) and act on it; renders (kw_sch_render / kw_pcb_render) are secondary. Never edit .kicad_* "
    "files by hand (stages downgrade with external_edit); never guess library ids or pin positions. "
    "Positions are millimetres. Errors are isError results with {code, message, why, fix, candidates, example}; "
    "kw_help(topic=<code or tool>) explains each. kw_snapshot(restore) is undo. "
    "A project runs on the installed KiCad that writes its files' own format (kw_project status: "
    "target_effective, epoch_match); a save that would move the files to a newer KiCad format stops with "
    "E_FORMAT_UPGRADE_REQUIRED (or an elicitation prompt): ask the user, then kw_project(action='upgrade', "
    "confirm=true), or have them install the matching KiCad - never work around it. persist=false on any call "
    "writes nothing into the project (kw_help topic readonly)."
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
    from kiwrap.core.state import readonly

    return INSTRUCTIONS + (READONLY_INSTRUCTIONS if readonly.server_readonly(ctx) else "")


REPORT_TOOLS = {
    "kw_sch_check",
    "kw_pcb_drc",
    "kw_fab_export",
    "kw_env",
    "kw_project",
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


class KiwrapServer(MCPServer):
    """`MCPServer` whose schema-level rejections are teaching errors too.

    The SDK validates the arguments against the published inputSchema BEFORE the registered
    function runs and answers an unknown tool name itself; both surface as `ToolError` from
    `call_tool`, which `_handle_call_tool` turns into a bare text `isError` result (a pydantic
    dump such as "1 validation error for kw_projectArguments ... action Field required"). Those
    are exactly the mistakes an LLM makes most, so this override maps them onto
    `{code: E_BAD_ARGS, why, fix, candidates}` like every other failure. `_handle_call_tool`
    dispatches through `self.call_tool`, so the override is reached over the wire.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult | Any:
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
                        candidates=list(TOOL_NAMES),
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
    for spec in specs:
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
