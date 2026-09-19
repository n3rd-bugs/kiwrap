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
from kiwrap.core.tools import PHASE0_TOOLS, TOOL_NAMES
from kiwrap.core.tools._base import ToolContext, ToolSpec, make_callable, to_kiwrap_error
from kiwrap.platform import current

INSTRUCTIONS = (
    "kiwrap drives installed KiCad binaries. Flow: kw_env -> kw_project(create) -> kw_sch_check -> "
    "kw_pcb_drc -> kw_fab_export. Never edit .kicad_* files by hand (stages downgrade with external_edit). "
    "Positions are millimetres. Errors are isError results with {code, message, why, fix, candidates, example}; "
    "kw_help(topic=<code or tool>) explains each. kw_snapshot(restore) is undo."
)

REPORT_TOOLS = {"kw_sch_check", "kw_pcb_drc", "kw_fab_export", "kw_env", "kw_project"}
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

    async def call_tool(  # type: ignore[override]
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


def register_tools(mcp: MCPServer, ctx: ToolContext, specs: tuple[ToolSpec, ...] = PHASE0_TOOLS) -> None:
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


def build_server(
    storage_home: Path | None = None, *, services: Services | None = None, context: ToolContext | None = None
) -> MCPServer:
    """Create the server with every Phase 0 tool registered in catalog order."""
    ctx = context or build_context(storage_home, services)
    mcp = KiwrapServer(name="kiwrap", version=__version__, instructions=INSTRUCTIONS)
    register_tools(mcp, ctx)
    mcp.kiwrap_context = ctx  # type: ignore[attr-defined]  # handy for tests and the CLI
    return mcp
