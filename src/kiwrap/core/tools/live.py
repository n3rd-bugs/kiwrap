"""kw_live: a sandboxed KiCad GUI on a project, for humans watching (docs/DISPLAY.md §7.4, §12.6).

The agent loop stays data-first (docs/DECISIONS.md D17): `kw_live` exists so the human next to
the agent can see the board or schematic change in the VS Code "Live KiCad" view while the
agent keeps reading `kw_pcb_get` / `kw_pcb_check` and writing through `kw_pcb_place` & co. The
GUI started here (`kiwrap.backends.ipc.gui_sessions.GuiSession`: sandboxed root with the API
server on, its own short TEMP so its pipe is unique, no activation, tracked by PID and Job
Object, one per agent) is BOUND to the project for as long as it lives: the ipc11 provider of
every tool then talks to its API server instead of starting a headless `kicad-cli api-server`
(`kiwrap.backends.ipc.sessions.bound_session_for`), so a commit an agent makes appears in the live
view without the extension being told anything - WGC delivers a frame because the content
changed (§7.4).

Actions
- `launch(project, editor=pcb|sch|project, mode=view, target_kicad?)`: start the GUI on the
  project's target install (or `target_kicad`): a normal window on the desktop, started without
  activation (`SW_SHOWNOACTIVATE`, the foreground handed back if KiCad took it), streamed into
  the VS Code tab by the helper; the person interacts with KiCad natively (`raise`). Returns
  `{session_id, pid, install, api_socket, token, editors[], mode_used, cloaked}` (the contract's
  launch shape; `mode_used` is `view`). `mode="hidden"` is an experimental, parked path (docs/
  DECISIONS.md D20): only on a build carrying the `display-input` patch the editor runs cloaked
  (`KICAD_INPUT_FROM_EVENTS=cloak`), refused with E_UNSUPPORTED elsewhere; `auto` / `mirror` are
  older spellings of `view`.
- `attach(session_id?, helper={port, token, version})`: the extension registers the display
  helper it started for this session; screenshots then come through the helper's `/control`.
- `status(session_id?)`: liveness, editors (surfaces with hwnd / rect / dpi / cloaked), socket.
- `screenshot(session_id?, surface?, max_px)`: PNG <= 1024 px: the helper when attached, else a
  `PrintWindow` capture of the frame (what the user sees, GL canvas included), else
  `kw_pcb_render` / `kw_sch_render` of the SAVED file (`degraded: [live_view_unavailable]`).
- `raise(session_id?, surface?)`: bring the frame to the front (the way a person takes over:
  the tab's "Raise KiCad" button calls it; a no-op for a cloaked one).
- `detach(session_id?)`: close documents through the API where handled, terminate, remove the
  lock files the GUI took, unbind the project (headless servers serve it again).

`project` may be omitted for every action but `launch` when exactly one session is live.
"""

from __future__ import annotations

import importlib
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import png_size
from kiwrap.core.tools._base import DESTRUCTIVE, ToolContext, ToolSpec, read_state, to_kiwrap_error

Action = Literal["launch", "attach", "status", "screenshot", "detach", "raise"]
Editor = Literal["pcb", "sch", "project"]
Mode = Literal["view", "hidden", "auto", "mirror"]
MAX_PX = 1024
TARGET_BYTES = 100_000

#: contract callables of the GUI supervisor: a binding on the Services instance (tests / the
#: integrator) wins, else the module attribute (same scheme as `_author.AUTHOR_BACKENDS`)
LIVE_BACKENDS: dict[str, tuple[str, str, str]] = {
    "gui_launch": ("kiwrap.backends.ipc.gui_sessions", "launch", "the GUI session supervisor"),
    "gui_find": ("kiwrap.backends.ipc.gui_sessions", "find_gui_session", "the GUI session supervisor"),
    "gui_sessions": ("kiwrap.backends.ipc.gui_sessions", "live_gui_sessions", "the GUI session supervisor"),
    "gui_input_from_events": (
        "kiwrap.backends.ipc.gui_sessions",
        "input_from_events",
        "the GUI capability probe",
    ),
}


def backend(ctx: ToolContext, name: str) -> Any:
    bound = getattr(ctx.services, name, None)
    if bound is not None:
        return bound
    module, attr, hint = LIVE_BACKENDS[name]
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise KiwrapError(
            "E_UNSUPPORTED",
            f"{hint} is not available in this build ({module}.{attr}: {exc})",
            why="the component implementing it is not present in this kiwrap installation",
            fix=f"update kiwrap; kw_live needs the {module} component",
            data={"module": module, "function": attr},
        ) from exc


class HelperArg(BaseModel):
    port: int = Field(ge=1, le=65535, description="the helper's loopback port (its stdout JSON line)")
    token: str = Field(min_length=1, description="the helper's session token")
    version: str = Field(default="", description="the helper's version string")
    host: str = Field(default="127.0.0.1", description="loopback only: 127.0.0.1 (default), localhost or ::1")
    pid: int | None = Field(default=None, description="the helper process id, if the extension tracks it")

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        # the helper contract is loopback-only; anything else would make both kiwrap servers
        # open outbound WebSockets to a caller-chosen address (reviewer finding 13)
        from kiwrap.backends.ipc.gui_sessions import is_loopback_host

        if not is_loopback_host(value):
            raise ValueError(f"helper host {value!r} is not a loopback address (127.0.0.1, localhost, ::1)")
        return value.strip()


class LiveArgs(BaseModel):
    action: Action = Field(description="launch | attach | status | screenshot | detach | raise")
    project: str | None = Field(
        default=None,
        description="project directory or .kicad_pro path (required for launch; identifies the session otherwise)",
    )
    session_id: str | None = Field(default=None, description="a session id returned by launch")
    editor: Editor = Field(
        default="pcb", description="launch: pcb (pcbnew), sch (eeschema) or project (kicad manager)"
    )
    mode: Mode = Field(
        default="view",
        description=(
            "launch: view (default) = a visible KiCad window on the desktop, never activated, streamed live "
            "into the VS Code tab; the person interacts with it natively. hidden = EXPERIMENTAL, parked "
            "(D20): cloaked editor driven by the helper, only on a build carrying the display-input patch. "
            "auto / mirror are older spellings of view."
        ),
    )
    target_kicad: str | None = Field(
        default=None, description="launch: install to use (default: the project's target KiCad)"
    )
    helper: HelperArg | None = Field(
        default=None, description="attach: the helper endpoint the extension started"
    )
    surface: str | None = Field(
        default=None, description="screenshot / raise: surface kind (pcb | sch | 3d | mgr | fp | sym) or id"
    )
    max_px: int = Field(default=MAX_PX, ge=128, le=MAX_PX, description="screenshot: longest edge in pixels")
    ready_timeout_s: float = Field(
        default=180.0, ge=5, le=900, description="launch: how long to wait for the API"
    )


# ---- helpers ------------------------------------------------------------------------------------------


def _project(args: LiveArgs) -> Project | None:
    return Project.from_path(args.project) if args.project else None


def _find(ctx: ToolContext, args: LiveArgs) -> Any:
    project = _project(args)
    find = backend(ctx, "gui_find")
    session = find(args.session_id, project.dir if project else None)
    if session is None:
        live = [s.status() for s in backend(ctx, "gui_sessions")()]
        if not args.session_id and not project and len(live) > 1:
            raise KiwrapError(
                "E_BAD_ARGS",
                f"{len(live)} GUI sessions are live; name one",
                fix="pass session_id or project",
                candidates=[s["session_id"] for s in live],
            )
        raise KiwrapError(
            "E_BAD_ARGS",
            "no live GUI session"
            + (f" {args.session_id!r}" if args.session_id else "")
            + (f" on {project.dir}" if project else ""),
            why="the GUI was never launched, was detached, or exited (a crash, or the user closed it)",
            fix="kw_live(action='launch', project=...) starts one; status of a dead session is not kept",
            candidates=[s["session_id"] for s in live],
        )
    return session


def _receipt(
    session: Any, *, extra: dict[str, Any] | None = None, warnings: list[str] | None = None
) -> Receipt:
    status = session.status()
    data = {"ok": True, **status}
    if extra:
        data.update(extra)
    return Receipt(
        provider_used=f"gui:{status.get('kicad', '')}",
        warnings=[*(warnings or []), *status.get("warnings", [])],
        data=data,
    )


def _install_for(ctx: ToolContext, project: Project, args: LiveArgs) -> tuple[Any, Any, Any]:
    state, _ = read_state(project)
    spec = args.target_kicad or state.target_kicad
    install = ctx.services.install(spec)
    caps = ctx.services.caps(install)
    # the GUI needs the API flag in its seed; the tools' kicad-cli root is the same root, which
    # keeps the sandbox's per-install state (window layout, fp-info-cache) in one place
    config = ctx.services.sandbox_ensure(
        install, storage_home=ctx.services.storage_home, enable_api_server=True
    )
    return install, caps, config


# ---- actions ------------------------------------------------------------------------------------------


def _launch(ctx: ToolContext, args: LiveArgs) -> Receipt:
    if not args.project:
        raise KiwrapError(
            "E_BAD_ARGS",
            "launch needs a project",
            fix="pass project=<dir or .kicad_pro>",
            example='kw_live(action="launch", project="C:/work/blinky", editor="pcb")',
        )
    project = Project.from_path(args.project)
    install, caps, config = _install_for(ctx, project, args)
    warnings: list[str] = []
    if args.editor == "sch" and caps.has("ipc.schematic_write_crashes"):
        warnings.append(
            "this KiCad crashes on schematic writes over IPC (#24966): the live schematic is a view; "
            "schematic edits keep going through the file engine after detach"
        )
    if args.editor == "sch" and not caps.has("ipc.schematic"):
        warnings.append("this KiCad has no schematic IPC: the live schematic is a view only")
    if not caps.has("ipc.headless"):
        warnings.append(
            "this KiCad has no headless IPC provider, so the planner's ipc11 provider is not eligible: "
            "reads come from the file engine while the GUI holds the project, mutating tools are refused "
            "(E_FILE_LOCKED) until kw_live detach"
        )
    launch = backend(ctx, "gui_launch")
    session = launch(
        install,
        project.dir,
        editor=args.editor,
        mode=args.mode,
        config_root=config,
        capabilities=caps,
        storage_home=ctx.services.storage_home,
        ready_timeout_s=args.ready_timeout_s,
    )
    status = session.status()
    next_hint = (
        "the extension starts the helper and registers it with kw_live(action='attach'); the agent keeps "
        "using kw_pcb_get / kw_pcb_check / kw_pcb_place - commits appear live"
    )
    return Receipt(
        provider_used=f"gui:{install.version}",
        warnings=[*warnings, *status.get("warnings", [])],
        data={
            "ok": True,
            **status,
            "install": install.key(),
            "hidden_available": bool(backend(ctx, "gui_input_from_events")(install, caps)),
            "next": next_hint,
        },
    )


def _attach(ctx: ToolContext, args: LiveArgs) -> Receipt:
    if args.helper is None:
        raise KiwrapError(
            "E_BAD_ARGS",
            "attach needs the helper endpoint",
            fix="pass helper={port, token, version} exactly as the helper printed on stdout",
            example='kw_live(action="attach", session_id="g1a2b3c4", helper={"port": 51234, "token": "...", "version": "0.1.0"})',
        )
    session = _find(ctx, args)
    endpoint = session.attach_helper(args.helper.model_dump())
    return _receipt(session, extra={"attached": True, "helper": endpoint.to_json()})


def _status(ctx: ToolContext, args: LiveArgs) -> Receipt:
    session = _find(ctx, args)
    return _receipt(session)


def _detach(ctx: ToolContext, args: LiveArgs) -> Receipt:
    session = _find(ctx, args)
    status = session.status()
    dead = session.close()
    # `close()` of a foreign session says whether the GUI is gone (a recycled pid counts as
    # gone); an owned session's `running` polls its own process. Never `running` of a remote:
    # it turned False the moment close() marked the object, kill or no kill (finding 24).
    alive = (not dead) if isinstance(dead, bool) else bool(getattr(session, "running", False))
    return Receipt(
        provider_used=f"gui:{status.get('kicad', '')}",
        warnings=["the GUI process survived the kill; it stays recorded for the orphan sweep"]
        if alive
        else [],
        data={
            "ok": not alive,
            "session_id": status["session_id"],
            "pid": status["pid"],
            "project": status["project"],
            "closed": not alive,
            "locks_removed": status.get("locks", []),
        },
    )


def _raise(ctx: ToolContext, args: LiveArgs) -> Receipt:
    session = _find(ctx, args)
    result = session.raise_frame(args.surface)
    return _receipt(session, extra=result)


def _screenshot(ctx: ToolContext, args: LiveArgs) -> Receipt:
    session = _find(ctx, args)
    warnings: list[str] = []
    try:
        png, summary = session.screenshot(surface=args.surface, max_px=args.max_px)
    except Exception as exc:  # noqa: BLE001 - every failure of the live path falls back to a render
        err = to_kiwrap_error(exc)
        warnings.append(f"live view unavailable ({err.code}: {err.message}); rendering the saved file")
        return _render_fallback(ctx, args, session, warnings)
    size = png_size(png)
    if size and max(size) > MAX_PX:
        raise KiwrapError(
            "E_INTERNAL",
            f"screenshot is {size[0]}x{size[1]} px, over the {MAX_PX} px cap",
            why="the capture path ignored max_px",
            fix="report this",
        )
    if len(png) > TARGET_BYTES:
        warnings.append(
            f"screenshot is {len(png)} bytes (> {TARGET_BYTES} target); pass a smaller max_px for a lighter image"
        )
    data = {
        "mode": "live",
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "bytes": len(png),
        **summary,
    }
    receipt = _receipt(session, extra=data, warnings=warnings)
    receipt.image = png
    return receipt


def _render_fallback(ctx: ToolContext, args: LiveArgs, session: Any, warnings: list[str]) -> Receipt:
    """`kw_pcb_render` / `kw_sch_render` of the saved file (the document on disk, not the live
    one: an unsaved edit in the GUI is not in this picture)."""
    from kiwrap.core.tools import render

    status = session.status()
    project = Project.from_path(status["project"])
    editor = status.get("editor", "pcb")
    if editor == "sch":
        receipt = render.sch_render(ctx, render.SchRenderArgs(project=str(project.dir), max_px=args.max_px))
    else:
        receipt = render.pcb_render(
            ctx, render.PcbRenderArgs(project=str(project.dir), view="2d", max_px=args.max_px)
        )
    receipt.degraded = [*receipt.degraded, "live_view_unavailable"]
    receipt.warnings = [*warnings, *receipt.warnings]
    receipt.data = {**receipt.data, "source": "render", "session_id": status["session_id"], "mode": "render"}
    return receipt


ACTIONS = {
    "launch": _launch,
    "attach": _attach,
    "status": _status,
    "screenshot": _screenshot,
    "detach": _detach,
    "raise": _raise,
}


#: the supervisor's teaching-error codes (`IpcError.code`) -> the catalogue's stable codes
BACKEND_CODES: dict[str, str] = {
    "E_GUI_BUSY": "E_BUSY",
    "E_GUI_UNSUPPORTED": "E_UNSUPPORTED",
    "E_IPC_START": "E_KICAD_FAILED",
    "E_GUI_SESSION": "E_KICAD_FAILED",
    "E_IPC_SESSION": "E_KICAD_FAILED",
    "E_IPC": "E_KICAD_FAILED",
}


def run(ctx: ToolContext, args: LiveArgs) -> Receipt:
    try:
        return ACTIONS[args.action](ctx, args)
    except KiwrapError:
        raise
    except Exception as exc:  # noqa: BLE001 - the supervisor's IpcError shape (code / why / fix) is preserved
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not hasattr(exc, "why") or not hasattr(exc, "fix"):
            raise
        raise KiwrapError(
            BACKEND_CODES.get(code, "E_KICAD_FAILED"),
            str(exc),
            why=str(getattr(exc, "why", "") or ""),
            fix=str(getattr(exc, "fix", "") or ""),
            data={"backend_code": code, "details": str(getattr(exc, "details", "") or "")[-3000:]},
        ) from exc


SPEC = ToolSpec(
    name="kw_live",
    title="Live KiCad session",
    description=(
        "For humans watching: launch a sandboxed KiCad GUI (pcbnew / eeschema / manager) on the project, "
        "bound to kiwrap's IPC so the agent's commits appear live; attach the display helper; status; "
        "screenshot (<= 1024 px PNG); raise; detach. The agent's own feedback stays data-first "
        "(kw_pcb_get / kw_pcb_check); one GUI at a time; never activates the window."
    ),
    input_model=LiveArgs,
    handler=run,
    annotations=DESTRUCTIVE,
)
