"""kw_env: what is installed and what it can do (docs/ARCHITECTURE.md §10 row 1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec
from kiwrap.platform import current

SUMMARY_KEYS = (
    "cli.has_erc",
    "cli.has_drc",
    "cli.has_render",
    "cli.has_jobset",
    "cli.has_sch_pcb_upgrade",
    "cli.drc_refill_flags",
    "cli.erc_json_pos_scale",
    "cli.gerbers_creates_outdir",
    "cli.writes_prl_on_read",
    "formats.sch",
    "formats.pcb",
    "formats.sym",
    "formats.pro",
    "swig.available",
    "ipc.available",
    "ipc.headless",
    "ipc.schematic",
    "cli.has_sch_diff",
    "cli.has_stats_json",
    "cli.has_stackup_json",
    "cli.has_pcb_png",
    "rules.default_min_track_width_mm",
    "cli.has_specctra_export",
    "cli.has_specctra_import",
    "gui.input_from_events",
    "kiwrap.patches",
)

ENGINE_DIRS = {
    "freerouting": "engines/freerouting",
    "jre": "engines/jre",
    "krt": "engines/krt",
    "jlcparts": "engines/jlcparts",
}


class EnvArgs(BaseModel):
    refresh: bool = Field(
        default=False,
        description="Re-discover installs and re-probe capabilities instead of using the cached record",
    )


def _autorouter(ctx: ToolContext) -> dict[str, Any]:
    discover: Any = getattr(ctx.services, "router_engines", None)
    if discover is None:
        try:
            from kiwrap.backends.router import discover_engines
        except ImportError:  # the router backend is optional in a build
            return {"available": False, "reason": "kiwrap.backends.router is not present"}
        discover = discover_engines
    try:
        engines = discover(storage_home=ctx.services.storage_home, fallback=True)
    except TypeError:  # a test double that takes no `fallback`
        engines = discover(storage_home=ctx.services.storage_home)
    except Exception as exc:  # noqa: BLE001 - discovery must never fail kw_env
        return {"available": False, "reason": str(exc)}
    to_dict = getattr(engines, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else {"available": bool(getattr(engines, "available", False))}


def install_dict(install: Any) -> dict[str, Any]:
    return {
        "version": install.version,
        "major_minor": install.major_minor,
        "root": str(install.root),
        "kicad_cli": str(install.kicad_cli),
        "share": str(install.share),
        "source": install.source,
        "python": str(install.python) if getattr(install, "python", None) else None,
    }


def caps_summary(caps: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in SUMMARY_KEYS:
        try:
            out[key] = caps[key]
        except (KeyError, TypeError):
            out[key] = None
    out["probed"] = bool(getattr(caps, "probed", False))
    try:
        out["subcommands"] = len(caps["cli.subcommands"])
    except (KeyError, TypeError):
        out["subcommands"] = None
    return out


def run(ctx: ToolContext, args: EnvArgs) -> Receipt:
    plat = current()
    warnings: list[str] = []
    degraded: list[str] = []
    try:
        installs = ctx.services.installs(refresh=args.refresh)
    except KiwrapError as exc:
        if exc.code != "E_UNSUPPORTED":
            raise
        installs = []
        degraded.append("discovery_unavailable")
        warnings.append(exc.message)

    rows: list[dict[str, Any]] = []
    for inst in installs:
        row = install_dict(inst)
        try:
            row["capabilities"] = caps_summary(ctx.services.caps(inst, refresh=args.refresh))
        except KiwrapError as exc:
            row["capabilities"] = None
            row["capabilities_error"] = exc.to_dict()
            if "capabilities_unavailable" not in degraded:
                degraded.append("capabilities_unavailable")
        rows.append(row)

    storage = ctx.services.storage_home
    engines: dict[str, Any] = {name: (storage / rel).exists() for name, rel in ENGINE_DIRS.items()}
    # the autorouter the routing tools will actually start: the resolved JRE / jar / profile
    # (kiwrap.backends.router.discover_engines, same fallback to the platform's default storage
    # home as kw_pcb_route_auto), never a bare directory flag
    engines["autorouter"] = _autorouter(ctx)
    try:
        data_backends = ctx.services.data_backends()
    except AttributeError:  # a hand-built Services double without the method
        data_backends = {}
    data = {
        "ok": True,
        "kiwrap_version": ctx.server_version,
        "platform": plat.name,
        "installs": rows,
        "default_install": rows[0]["version"] if rows else None,
        "engines": engines,
        # which optional data-first backends this server can reach (kiwrap.core.services.DATA_BACKENDS);
        # the planner falls through a provider whose backend is missing, so an agent reading
        # `provider_used: file` on a KiCad with ipc.headless looks here first
        "data_backends": data_backends,
        "storage": {
            "home": str(storage),
            "sandbox_roots": str(
                storage / "kicad-config"
            ),  # kiwrap.sandbox: kicad-config/<semver>-<root digest>/
            "jobs": str(ctx.jobs.storage_dir) if ctx.jobs.storage_dir else None,
            "temp": str(plat.temp_dir),
            "user_kicad_config_home": str(plat.user_kicad_config_home) + " (read-only, never written)",
        },
    }
    if not rows:
        warnings.append("no KiCad installation found; every KiCad-backed tool will return E_NO_KICAD")
    return Receipt(
        provider_used="discovery",
        degraded=degraded,
        warnings=warnings,
        next_suggested="kw_project",
        data=data,
    )


SPEC = ToolSpec(
    name="kw_env",
    title="Environment",
    description=(
        "List installed KiCad versions, their verified capabilities (ERC/DRC/render/upgrade support, "
        "format epochs, quirks), external engines present and kiwrap's storage paths. Call first; "
        "`refresh=true` re-probes."
    ),
    input_model=EnvArgs,
    handler=run,
    annotations=READ_ONLY,
)
