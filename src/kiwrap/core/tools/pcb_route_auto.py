"""kw_pcb_route_auto and kw_pcb_fix: Freerouting over the Specctra DSN/SES round trip
(docs/ARCHITECTURE.md §8 routing loop, §10 catalog, §11 receipts; docs/VERIFICATION.md claims 7
and 8; docs/research/04-autorouting.md).

The routing loop, one mutation (`_base.mutation`, gate `placed` for kw_pcb_route_auto):

    snapshot (the mutation prologue) -> lock the tracks / vias to keep -> export the DSN through
    the planner operation `routing.autoroute` (providers: the patched kicad-cli `cli`, kiwrap's
    own `codec`, the bundled-python `swig` driver; the first available one exports AND imports) ->
    Freerouting under the managed JRE (`kiwrap.backends.router`: verified flags, progress,
    cancel, timeout) -> import the SES (KiCad's importer deletes every UNLOCKED track / via and
    re-adds the locked ones - claim 7 - which is why "keep" means "lock") -> restore the lock
    flags kiwrap set -> mandatory zone refill + kicad-cli DRC, classified (`_author.drc_validator`:
    `--refill-zones --save-board` where `cli.drc_refill_flags`, stale fills otherwise reported as
    `zones_stale`) -> receipt with routed / unrouted counts, DRC deltas, provider_used, degraded[]
    and `next_suggested` = kw_pcb_fix when must_fix > 0.

DRC after routing is the judge, never a formality: Freerouting 2.4.1 can emit router-introduced
overlaps (#858, fixed for 2.5.0), the fanout pre-pass 0.15 mm segments (claim 8) - a "0/0"
result is observed per board, never assumed.

A RE-RUN of kw_pcb_route_auto on a partially routed board is NOT a fresh start: the existing
unlocked copper goes to Freerouting as `(type route)` wiring it keeps and only optimises, so the
run converges to the same plateau (reference board: 253 tracks / 1 unrouted in, 252 / 1 out after
18 more passes). Progress on a plateau needs a rip-up: kw_pcb_fix (the nets with violations, then
their neighbourhood, then the board) or `from_scratch=true`, which deletes the unlocked copper of
the nets to route before the export.

The `nets=[...]` subset is enforced by the DSN, not by Freerouting: 2.4.1 headless ignores
`-inc` / `router.ignore_net_classes` (the jar's `setIgnoredByAutorouter` is only called from the
GUI classes; probes on the reference board and the spike DSN routed every net with `-inc
kiwrap_ignore`, `-inc kicad_default,Power` and the `--router.ignore_net_classes=` form alike).
`kiwrap.backends.router.dsn_post.restrict_nets` therefore removes the other nets from the
`(network …)` (their pads stay obstacles as pins of the component images, their wiring stays as
`(type fix)` obstacles) so the router has no airwire to route for them; `-inc` is still passed
for the day it works.

When a run fails after the board was written (the stub clean-up, kw_pcb_fix's rip-up, a cancel
after the SES import) the board-derived stage hashes are re-recorded at `placed` before the error
propagates, so the next call sees kiwrap's own write, not an `external_edit`, and the advertised
retry passes the gate; the error names the copper that is gone and the snapshot that holds it.

kw_pcb_fix (gate `placed`, like kw_pcb_route_auto) iterates: DRC -> the nets of the must_fix
violations (`[NET]` tags in KiCad's descriptions, unconnected_items included; the DRC validator
lists them under `nets_with_must_fix` from EVERY violation, not only the 50 the receipt shows)
-> rip up their unlocked tracks / vias -> clean
zero-length / net-less leftovers -> re-route ONLY those nets (the other nets are locked and moved
into the router's ignore class) -> refill + DRC, until must_fix == 0 or `max_iterations` is spent.
A round that does not reduce must_fix widens the next one's scope the way a human does (a pad
boxed in by its neighbours' copper cannot be reached while that copper is fixed): first the
NEIGHBOURHOOD - the unlocked copper of other nets within a margin (2 mm, then 4, 8, ...) of the
violation's items is ripped up and re-routed together with the violating nets - then, still
stalled, the whole BOARD: every unlocked track / via goes back to the router as `route` wiring
it may rip up and re-route (nothing but the agent's own locks is fixed). The receipt records the
`scope` of every round (`nets`, `neighbourhood`, `board`), the nets ripped up and the margin.
Verified on the ESP32-S3 reference board (tests/test_e2e_reference_board.py): the router's
plateau leaves /SDA open with its header pad boxed in; the restricted round cannot close it, the
neighbourhood round does. Violations that involve no net (a pad-to-pad hole clearance, a
courtyard) cannot be fixed by routing and are listed as `unfixable`.

Contracts this module codes against (built concurrently; every name is resolved lazily so a
missing component is a teaching E_UNSUPPORTED, and a test binds fakes on `Services`):

    kiwrap.formats.specctra.DsnWriter(board: BoardFile, *, rules=None, layers=None, nets=None, name=None,
                                      host_version=...)
        .write() -> str   (a callable returning the text is accepted too)
    kiwrap.formats.specctra.SesReader(text).apply(board_writer, *, keep_locked=True, default_via_drill=...)
        -> SesReport(tracks_added, vias_added, tracks_removed, vias_removed, placement_ignored,
                     nets_routed, warnings, skipped)
    kiwrap.backends.cli.specctra.export_dsn_run(install, config, board, out, caps=None) -> SpecctraRun
    kiwrap.backends.cli.specctra.import_ses_run(install, config, board, ses, out, caps=None) -> SpecctraRun
        (`kicad-cli pcb export specctra` / `pcb import-ses` of the fork patch `specctra-jobs`;
        a plain Path return - the older contract, what the unit tests bind - is accepted too)
    kiwrap.backends.swig.specctra.export_dsn / import_ses (same shapes; only when a driver exists)
    kiwrap.backends.router: discover_engines, Router, rules_from_board (this component)

Locking goes through `BoardWriter.set_locked(uuid, locked)` (KeyError for an unknown uuid); a
writer without it (a test double) is edited through its CST (`_node_by_uuid` + the dialect's
`(locked yes)` / bare `locked` form).

Provider certification (conformance T7, tests/test_cli_specctra.py, tests/test_router_live.py):
the codec's DSN equals KiCad's own export (SWIG on 10.0.6, the patched kicad-cli on 10.99) at
5 um on the ecc83 / blinker / 4-layer boards and its SES apply equals KiCad's importer within
1e-3 mm, so `codec` is ranked ahead of `swig` and the receipts of the two paths agree.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.gates import Force, check_gate
from kiwrap.core.state.jobs import JobCancelled, JobHandle
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import receipt_to_dict
from kiwrap.core.tools._author import (
    board_writer,
    call_with_accepted,
    drc_validator,
    rehash_board_stages,
    violation_net_names,
)
from kiwrap.core.tools._base import (
    DESTRUCTIVE,
    Mutation,
    ToolContext,
    ToolSpec,
    mutation,
    read_state,
    to_kiwrap_error,
)
from kiwrap.core.tools._data import pick, rec
from kiwrap.core.tools.pcb_author import (
    _install,
    _need_board,
    board_model,
    board_nets,
    board_rules,
    check_net,
    settle_after_drc,
)

Engine = Literal["freerouting"]
ProviderArg = Literal["auto", "cli", "codec", "swig"]
FixAction = Literal["rip_up_violations", "reroute", "clean", "refill"]

DEFAULT_PASSES = 100
DEFAULT_TIMEOUT_S = 900.0
MAX_PASSES = 10_000
MAX_TIMEOUT_S = 6 * 3600.0
DEFAULT_FIX_ACTIONS: tuple[str, ...] = ("rip_up_violations", "reroute", "clean", "refill")
MAX_LISTED = 50
SES_SUFFIX = ".ses"
DSN_SUFFIX = ".dsn"

#: contract name -> (module, attribute, hint); a test / the integrator may bind any of them as an
#: attribute of the `Services` instance (`services.dsn_writer = FakeWriter`)
ROUTE_BACKENDS: dict[str, tuple[str, str, str]] = {
    "dsn_writer": ("kiwrap.formats.specctra", "DsnWriter", "the Specctra DSN writer (codec)"),
    "ses_reader": ("kiwrap.formats.specctra", "SesReader", "the Specctra SES reader (codec)"),
    "cli_export_dsn": ("kiwrap.backends.cli.specctra", "export_dsn_run", "kicad-cli pcb export specctra"),
    "cli_import_ses": ("kiwrap.backends.cli.specctra", "import_ses_run", "kicad-cli pcb import-ses"),
    "swig_export_dsn": ("kiwrap.backends.swig.specctra", "export_dsn", "the SWIG ExportSpecctraDSN driver"),
    "swig_import_ses": ("kiwrap.backends.swig.specctra", "import_ses", "the SWIG ImportSpecctraSES driver"),
    "router_engines": ("kiwrap.backends.router", "discover_engines", "the managed engines"),
    "router": ("kiwrap.backends.router", "Router", "the Freerouting router"),
    "route_rules": ("kiwrap.backends.router", "rules_from_board", "the router rules"),
}


def route_backend(ctx: ToolContext, name: str) -> Callable[..., Any]:
    """Like `_author.backend` for this module's contracts (see `ROUTE_BACKENDS`)."""
    bound = getattr(ctx.services, name, None)
    module, attr, hint = ROUTE_BACKENDS[name]
    fn: Any = bound
    if fn is None:
        try:
            fn = importlib.import_module(module)
            for part in attr.split("."):
                fn = getattr(fn, part)
        except (ImportError, AttributeError) as exc:
            raise KiwrapError(
                "E_UNSUPPORTED",
                f"{hint} is not available in this build ({module}.{attr}: {exc})",
                why="the component implementing it is not present in this kiwrap installation",
                fix=f"update kiwrap; this tool needs the {module} component",
                data={"module": module, "function": attr},
            ) from exc

    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except NotImplementedError as exc:
            raise KiwrapError(
                "E_UNSUPPORTED",
                f"{hint} is not implemented yet ({module}.{attr})",
                why="the component implementing it is still a stub in this build",
                fix=f"update kiwrap; this tool needs the {module} component",
                data={"module": module, "function": attr},
            ) from exc

    call.__name__ = attr.replace(".", "_")
    call.__wrapped__ = fn  # type: ignore[attr-defined]
    return call


# ---- arguments ---------------------------------------------------------------------------------------


class PcbRouteAutoArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    nets: list[str] | None = Field(
        default=None,
        description=(
            "route only these nets (board net names): the others are dropped from the DSN's network so the "
            "router has nothing to route for them, their pads and existing copper stay as locked obstacles. "
            "Default: every net"
        ),
    )
    from_scratch: bool = Field(
        default=False,
        description=(
            "delete the unlocked tracks / vias of the nets to route before the export. Default false: the "
            "existing unlocked copper goes to the router as wiring it keeps and only optimises, so a re-run "
            "on a partially routed board converges to the same plateau; kw_pcb_fix rips up the violating "
            "nets instead"
        ),
    )
    passes: int = Field(
        default=DEFAULT_PASSES, ge=1, le=MAX_PASSES, description="Freerouting autorouter passes (-mp)"
    )
    timeout_s: float = Field(
        default=DEFAULT_TIMEOUT_S,
        gt=0,
        le=MAX_TIMEOUT_S,
        description="wall-clock budget for the router; the process tree is killed when exceeded",
    )
    lock_refs: list[str] | None = Field(
        default=None,
        description=(
            "footprint references whose nets' existing tracks and vias are kept as they are (locked "
            "for the router: e.g. a hand-routed escape under U1)"
        ),
    )
    clean_dangling: bool = Field(
        default=True,
        description=(
            "before routing, delete the unlocked stubs of the nets to route (track ends on no pad, item or "
            "zone of their net): Freerouting must connect every leftover fragment and gives up on nets a "
            "clean start routes"
        ),
    )
    engine: Engine = Field(default="freerouting", description="the autorouter (freerouting only)")
    provider: ProviderArg = Field(
        default="auto",
        description=(
            "the DSN/SES codec: auto (patched kicad-cli -> kiwrap's codec -> the SWIG driver) | cli | codec | swig"
        ),
    )
    run_async: bool = Field(
        default=True,
        description=(
            "return a job_id immediately and route in the background (poll with kw_job: progress carries "
            "the pass, unrouted and violation counts). false blocks until the DRC is in"
        ),
    )
    force: dict[str, Any] | str | None = Field(
        default=None, description='override the placed gate: {"reason": "..."} (logged into the snapshot)'
    )


class PcbFixArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    actions: list[FixAction] = Field(
        default_factory=lambda: list(DEFAULT_FIX_ACTIONS),  # type: ignore[arg-type]
        description=(
            "rip_up_violations: delete the unlocked tracks / vias of the nets with must_fix violations; "
            "reroute: route those nets again (Freerouting); clean: drop zero-length and net-less unlocked "
            "segments / vias; refill: refill zones before the DRC (always on where the KiCad can)"
        ),
    )
    max_iterations: int = Field(default=3, ge=1, le=10, description="rip-up / re-route rounds")
    passes: int = Field(default=DEFAULT_PASSES, ge=1, le=MAX_PASSES, description="router passes per round")
    timeout_s: float = Field(
        default=DEFAULT_TIMEOUT_S, gt=0, le=MAX_TIMEOUT_S, description="router budget per round"
    )
    provider: ProviderArg = Field(default="auto", description="the DSN/SES codec: auto | cli | codec | swig")
    run_async: bool = Field(default=True, description="return a job_id immediately (poll with kw_job)")
    force: dict[str, Any] | str | None = Field(
        default=None, description='override the placed gate: {"reason": "..."} (logged into the snapshot)'
    )


# ---- the codec providers -------------------------------------------------------------------------------


@dataclass
class Codec:
    """The DSN/SES round trip a planner provider gives: `export(board_path, out_dsn) -> Path`
    and `import_ses(board_path, ses) -> dict` (the board on disk is rewritten in place; the dict
    carries the SesReport counts the provider knows)."""

    name: str
    export: Callable[[Path, Path], Path]
    import_ses: Callable[[Path, Path], dict[str, Any]]
    patch: str = ""


def _report_dict(report: Any) -> dict[str, Any]:
    r = rec(report)
    out: dict[str, Any] = {}
    for key in (
        "tracks_added",
        "vias_added",
        "tracks_removed",
        "vias_removed",
        "placement_ignored",
        "nets_routed",
        "skipped",
    ):
        val = pick(r, key)
        if isinstance(val, (list, tuple, set)):
            out[key] = sorted(str(v) for v in val) if key == "nets_routed" else len(val)
        elif isinstance(val, (bool, int)):
            out[key] = val
    out["warnings"] = [str(w) for w in (pick(r, "warnings", default=[]) or [])]
    return out


def _specctra_run_dict(result: Any) -> dict[str, Any]:
    """What a `kiwrap.backends.cli.specctra.SpecctraRun` (or a bare Path) says about a run:
    KiCad's stderr warnings, the skipped-item count and the track counts of an import."""
    if result is None or isinstance(result, (str, Path)):
        return {}
    out: dict[str, Any] = {}
    warnings = getattr(result, "warnings", None)
    if isinstance(warnings, (list, tuple)):
        out["warnings"] = [str(w) for w in warnings]
    skipped = getattr(result, "skipped", None)
    if isinstance(skipped, int) and not isinstance(skipped, bool):
        out["skipped"] = skipped
    for key in ("tracks_before", "tracks_after", "duration_s"):
        val = getattr(result, key, None)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
            out[key] = val
    return out


def _run_path(result: Any, default: Path) -> Path:
    """The file a provider wrote: a `SpecctraRun.path`, a Path, or `default`."""
    path = getattr(result, "path", None)
    if isinstance(path, (str, Path)):
        return Path(path)
    if isinstance(result, (str, Path)):
        return Path(result)
    return default


def _default_via_drill(project: Project) -> float | None:
    """The Default netclass `via_drill` of the project's .kicad_pro (mm), else None."""
    try:
        doc = json.loads(project.pro_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for cls in (doc.get("net_settings") or {}).get("classes") or []:
        if isinstance(cls, dict) and cls.get("name") == "Default":
            val = cls.get("via_drill")
            return float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else None
    return None


def codec_provider(ctx: ToolContext, install: Any, caps: Any, config: Any, project: Project) -> Codec:
    """kiwrap's own DSN writer / SES reader on the lossless CST (planner provider `codec`)."""
    writer_cls = route_backend(ctx, "dsn_writer")
    reader_cls = route_backend(ctx, "ses_reader")

    writer_warnings: list[str] = []

    def export(board_path: Path, out: Path) -> Path:
        model = ctx.services.file_read_board(board_path)
        layers = list(board_rules(model, project).get("copper_layers") or []) or None
        # the writer derives its DsnRules from the .kicad_pro next to the board itself; the net
        # restriction is applied by the router's DSN hook (one path for every provider)
        text = call_with_accepted(
            writer_cls,
            model,
            layers=layers,
            name=f"{project.name}{DSN_SUFFIX}",
            host_version=str(getattr(install, "version", "") or "kiwrap"),
        )
        writer_warnings.clear()
        if hasattr(text, "write") and callable(text.write):  # a writer object: DsnWriter(...).write()
            writer = text
            text = writer.write()
            writer_warnings.extend(str(w) for w in (getattr(writer, "warnings", None) or []))
        elif hasattr(text, "text") and not isinstance(text, str):
            text = text.text() if callable(text.text) else text.text
        if not isinstance(text, str) or not text.strip():
            raise KiwrapError(
                "E_INTERNAL",
                "the DSN writer returned no text",
                why="kiwrap.formats.specctra.DsnWriter must return the DSN as a string",
                fix="report this",
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return out

    def import_ses(board_path: Path, ses: Path) -> dict[str, Any]:
        text = ses.read_text(encoding="utf-8", errors="replace")
        reader = reader_cls(text)
        w = board_writer(ctx, install, caps, config, project)
        # the session's via padstacks carry size:drill in their names; a session without that
        # (a hand-written SES) takes the Default netclass drill of the .kicad_pro
        report = call_with_accepted(
            reader.apply, w, keep_locked=True, default_via_drill=_default_via_drill(project)
        )
        w.save()
        out = _report_dict(report)
        if writer_warnings:
            out["warnings"] = [*writer_warnings, *out.get("warnings", [])]
        return out

    return Codec("codec", export, import_ses)


def cli_provider(ctx: ToolContext, install: Any, caps: Any, config: Any, project: Project) -> Codec:
    """The patched kicad-cli (`pcb export specctra` / `pcb import-ses`, fork patch `specctra-jobs`;
    planner provider `cli`). The import saves the board in place (out == board), exactly like the
    editor's Import Specctra Session + Save, so the project's rules stay where kicad-cli DRC
    finds them. KiCad's own stderr warnings (malformed outline, skipped session items) and the
    track counts it reports travel into the receipt's `ses`."""
    export_fn = route_backend(ctx, "cli_export_dsn")
    import_fn = route_backend(ctx, "cli_import_ses")
    patch = ""
    patch_for = getattr(caps, "patch_for", None)
    if callable(patch_for):
        with contextlib.suppress(Exception):
            patch = str(patch_for("cli.has_specctra_export") or "")
    export_info: dict[str, Any] = {}

    def export(board_path: Path, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        result = call_with_accepted(export_fn, install, config, board_path, out, caps=caps)
        export_info.clear()
        export_info.update(_specctra_run_dict(result))
        return _run_path(result, out)

    def import_ses(board_path: Path, ses: Path) -> dict[str, Any]:
        result = call_with_accepted(import_fn, install, config, board_path, ses, board_path, caps=caps)
        info = _specctra_run_dict(result)
        out: dict[str, Any] = {}
        if "tracks_before" in info and "tracks_after" in info:
            out["kicad_tracks_before"] = info["tracks_before"]
            out["kicad_tracks_after"] = info["tracks_after"]
        if "skipped" in info:
            out["skipped"] = info["skipped"]
        warnings = [*export_info.get("warnings", []), *info.get("warnings", [])]
        if warnings:
            out["warnings"] = warnings
        return out

    return Codec("cli", export, import_ses, patch=patch)


def swig_provider(ctx: ToolContext, install: Any, caps: Any, config: Any, project: Project) -> Codec:
    """The bundled-python driver (planner provider `swig`; only where a driver module exists)."""
    export_fn = route_backend(ctx, "swig_export_dsn")
    import_fn = route_backend(ctx, "swig_import_ses")

    def export(board_path: Path, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        return _run_path(call_with_accepted(export_fn, install, config, board_path, out, caps=caps), out)

    def import_ses(board_path: Path, ses: Path) -> dict[str, Any]:
        result = call_with_accepted(import_fn, install, config, board_path, ses, board_path, caps=caps)
        return _report_dict(result) if result is not None and not isinstance(result, (str, Path)) else {}

    return Codec("swig", export, import_ses)


#: planner provider name -> codec factory (literal names: `kiwrap.core.planner` imports this
#: package's `_base` at import time, so its constants are read lazily, never at module level)
PROVIDER_FACTORIES: dict[str, Callable[..., Codec]] = {
    "cli": cli_provider,
    "codec": codec_provider,
    "swig": swig_provider,
}


# ---- board bookkeeping -----------------------------------------------------------------------------------


def _net_name(net: Any) -> str:
    if net is None:
        return ""
    return net if isinstance(net, str) else str(getattr(net, "name", "") or "")


@dataclass
class CopperItem:
    uuid: str
    kind: str  # track | arc | via
    net: str
    locked: bool
    zero_length: bool = False


def copper_items(model: Any) -> list[CopperItem]:
    """Every track / arc / via of the file model with its net and lock flag."""
    out: list[CopperItem] = []
    if model is None:
        return out
    for kind, attr in (("track", "tracks"), ("arc", "arcs"), ("via", "vias")):
        for it in getattr(model, attr, None) or []:
            uid = str(getattr(it, "uuid", "") or "")
            if not uid:
                continue
            zero = False
            if kind == "track":
                length = getattr(it, "length", None)
                with contextlib.suppress(Exception):
                    zero = (length() if callable(length) else float(length or 0.0)) < 1e-6
            out.append(
                CopperItem(
                    uid, kind, _net_name(getattr(it, "net", None)), bool(getattr(it, "locked", False)), zero
                )
            )
    return out


def nets_of_refs(model: Any, refs: Iterable[str]) -> list[str]:
    """The nets on the pads of the given footprints (E_BAD_ARGS for an unknown reference)."""
    wanted = [r for r in refs if r]
    if not wanted or model is None:
        return []
    by_ref = {str(getattr(fp, "reference", "") or ""): fp for fp in getattr(model, "footprints", None) or []}
    missing = [r for r in wanted if r not in by_ref]
    if missing:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"lock_refs: {', '.join(missing)} not on the board",
            fix="use footprint references the board has (kw_pcb_get kinds=['footprints'])",
            candidates=sorted(by_ref)[:50],
        )
    nets: set[str] = set()
    for r in wanted:
        for pad in getattr(by_ref[r], "pads", None) or []:
            name = _net_name(getattr(pad, "net", None))
            if name and not name.startswith("unconnected-("):
                nets.add(name)
    return sorted(nets)


ANCHOR_TOL_MM = 0.005


def dangling_items(model: Any, nets: Iterable[str] | None = None) -> list[CopperItem]:
    """Unlocked tracks / arcs / vias of `nets` (default: every net) with a free end. An end is
    anchored when its copper overlaps (KiCad's connectivity: the track's half width counts) a pad
    of its net on that layer (pad bbox), another surviving item of its net (a point on a track
    of the same layer within the two half widths, a via within its radius) or a copper zone of
    its net (inside the zone outline). Removal is iterated to a fixed point so a chain of stubs
    falls entirely. Verified on the ecc83 demo: 0 items on the pristine board.

    Why: KiCad's SES import leaves unlocked copper to the router, and Freerouting keeps every
    existing wire as an item it must CONNECT - stubs left over from a partial route make the
    autorouter give up on nets it would otherwise route (ecc83 with every other segment stripped:
    6 connections left after the score plateau; the same board fully stripped: 0). Ripping the
    stubs up first is what a human does before re-routing."""
    from kiwrap.formats.geometry.polygon import point_segment_distance

    if model is None:
        return []
    wanted = set(nets) if nets is not None else None
    copper = tuple(getattr(model, "copper_layers", ()) or ())
    planes: dict[str, list[Any]] = {}
    for zone in getattr(model, "zones", None) or []:
        if getattr(zone, "is_rule_area", False):
            continue
        name = _net_name(getattr(zone, "net", None))
        if name:
            planes.setdefault(name, []).extend(getattr(zone, "outline", None) or [])
    pads: dict[str, list[tuple[Any, tuple[str, ...]]]] = {}
    for _fp, pad in model.pads():
        name = _net_name(getattr(pad, "net", None))
        if not name:
            continue
        try:
            pads.setdefault(name, []).append((pad.bbox(), tuple(pad.expand_layers(copper))))
        except Exception:  # noqa: BLE001 - a pad the model cannot box is no anchor
            continue

    def on_pad(net: str, p: Any, layer: str, reach: float) -> bool:
        return any(
            layer in layers and box.expanded(reach + ANCHOR_TOL_MM).contains(p)
            for box, layers in pads.get(net, ())
        )

    def in_plane(net: str, p: Any) -> bool:
        return any(poly.contains(p) for poly in planes.get(net, ()))

    def width(item: Any) -> float:
        return float(getattr(item, "width", 0.0) or 0.0)

    # every item anchors (a locked track or a track of another net's run still connects copper);
    # only unlocked items of the wanted nets are candidates for removal
    all_tracks = [*(getattr(model, "tracks", None) or []), *(getattr(model, "arcs", None) or [])]
    all_vias = list(getattr(model, "vias", None) or [])
    alive_t = {t.uuid: t for t in all_tracks if t.uuid}
    alive_v = {v.uuid: v for v in all_vias if v.uuid}

    def candidate(item: Any) -> bool:
        return not getattr(item, "locked", False) and (wanted is None or _net_name(item.net) in wanted)

    candidates_t = [t.uuid for t in all_tracks if t.uuid and candidate(t)]
    candidates_v = [v.uuid for v in all_vias if v.uuid and candidate(v)]

    def on_track(net: str, p: Any, layer: str | None, reach: float, skip: str) -> bool:
        return any(
            o.uuid != skip
            and _net_name(o.net) == net
            and (layer is None or o.layer == layer)
            and point_segment_distance(p, o.start, o.end) <= reach + width(o) / 2 + ANCHOR_TOL_MM
            for o in alive_t.values()
        )

    def on_via(net: str, p: Any, layer: str, reach: float) -> bool:
        return any(
            _net_name(v.net) == net
            and (not v.layers or layer in v.layers or v.kind == "through")
            and v.position.distance(p) <= reach + float(v.size or 0.0) / 2 + ANCHOR_TOL_MM
            for v in alive_v.values()
        )

    doomed: list[CopperItem] = []
    changed = True
    while changed:
        changed = False
        for uid in list(candidates_t):
            t = alive_t.get(uid)
            if t is None:
                continue
            net, layer, half = _net_name(t.net), t.layer, width(t) / 2
            anchored_ends = sum(
                int(
                    on_pad(net, p, layer, half)
                    or in_plane(net, p)
                    or on_track(net, p, layer, half, t.uuid)
                    or on_via(net, p, layer, half)
                )
                for p in (t.start, t.end)
            )
            if anchored_ends < 2:
                doomed.append(CopperItem(t.uuid, "track", net, False))
                del alive_t[t.uuid]
                changed = True
        for uid in list(candidates_v):
            v = alive_v.get(uid)
            if v is None:
                continue
            net, p, radius = _net_name(v.net), v.position, float(v.size or 0.0) / 2
            # a through via spans every copper layer whatever its (layers F.Cu B.Cu) says
            layers = tuple(copper) if (v.kind == "through" or not v.layers) else tuple(v.layers)
            anchored = in_plane(net, p) or any(on_pad(net, p, layer, radius) for layer in layers)
            anchored = anchored or any(on_track(net, p, layer, radius, v.uuid) for layer in layers)
            if not anchored:
                doomed.append(CopperItem(v.uuid, "via", net, False))
                del alive_v[v.uuid]
                changed = True
    return doomed


def set_locked(writer: Any, uuid: str, locked: bool) -> bool:
    """Lock / unlock one track, arc or via through `BoardWriter.set_locked(uuid, locked)` (KeyError
    for an unknown uuid), else - a writer double without it - through its CST. Returns whether
    the item was found."""
    fn = getattr(writer, "set_locked", None) or getattr(writer, "lock_item", None)
    if callable(fn):
        try:
            fn(uuid, locked)
        except KeyError:
            return False
        return True
    finder = getattr(writer, "_node_by_uuid", None)
    node = finder(uuid) if callable(finder) else None
    if node is None:
        return False
    from kiwrap.formats.sexpr import Atom, build  # the lossless CST the writer edits

    existing_flag = next((a for a in node.atoms if a.kind == "symbol" and a.value == "locked"), None)
    existing_list = node.find("locked")
    if not locked:
        if existing_flag is not None:
            node.remove(existing_flag)
        if existing_list is not None:
            node.remove(existing_list)
    elif existing_flag is None and existing_list is None:
        dialect = getattr(writer, "dialect", None)
        if getattr(dialect, "locked_as_list", True):
            anchor = node.find("net") or node.find("uuid") or node.find("tstamp")
            idx = node.index(anchor) if anchor is not None else len(node)
            node.insert(idx, build("locked", Atom.symbol("yes")))
        else:
            node.insert(1, Atom.symbol("locked"))
    mark = getattr(writer, "mark_dirty", None)
    if callable(mark):
        mark()
    return True


def lock_items(
    ctx: ToolContext,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    uuids: Iterable[str],
    locked: bool,
) -> list[str]:
    """Set the lock flag on the given items and save; returns the uuids actually changed."""
    ids = [u for u in uuids if u]
    if not ids:
        return []
    w = board_writer(ctx, install, caps, config, project)
    changed = [u for u in ids if set_locked(w, u, locked)]
    if changed:
        w.save()
    return changed


def _counts(items: Iterable[CopperItem]) -> dict[str, int]:
    out = {"tracks": 0, "vias": 0, "locked": 0}
    for it in items:
        out["vias" if it.kind == "via" else "tracks"] += 1
        if it.locked:
            out["locked"] += 1
    return out


def _diff_copper(before: list[CopperItem], after: list[CopperItem]) -> dict[str, int]:
    b = {it.uuid: it for it in before}
    a = {it.uuid: it for it in after}
    added = [it for u, it in a.items() if u not in b]
    removed = [it for u, it in b.items() if u not in a]
    return {
        "tracks_added": sum(1 for it in added if it.kind != "via"),
        "vias_added": sum(1 for it in added if it.kind == "via"),
        "tracks_removed": sum(1 for it in removed if it.kind != "via"),
        "vias_removed": sum(1 for it in removed if it.kind == "via"),
    }


FIX_MARGIN_MM = 2.0  # neighbourhood of a stalled violation: 2 mm, doubled per stalled round


def _violation_points(v: dict[str, Any]) -> list[Any]:
    from kiwrap.formats.geometry import Vec2

    pts: list[Any] = []
    for item in v.get("items", []) or []:
        pos = item.get("pos") if isinstance(item, dict) else None
        if isinstance(pos, dict) and "x_mm" in pos and "y_mm" in pos:
            pts.append(Vec2(float(pos["x_mm"]), float(pos["y_mm"])))
    pos = v.get("pos")
    if not pts and isinstance(pos, dict) and "x_mm" in pos and "y_mm" in pos:
        pts.append(Vec2(float(pos["x_mm"]), float(pos["y_mm"])))
    return pts


def neighbour_nets(
    model: Any, violations: Iterable[dict[str, Any]], margin_mm: float, *, exclude: Iterable[str] = ()
) -> list[str]:
    """The nets (other than `exclude`) whose UNLOCKED tracks / arcs / vias lie within `margin_mm`
    of a must_fix violation's items (the box spanned by the item positions, expanded): what a
    human rips up next when re-routing the violating net alone keeps failing."""
    from kiwrap.formats.geometry import BBox

    if model is None:
        return []
    boxes: list[Any] = []
    for v in violations:
        if v.get("class", "must_fix") != "must_fix":
            continue
        box = BBox.of(_violation_points(v))
        if box is not None:
            boxes.append(box.expanded(margin_mm))
    if not boxes:
        return []
    skip = set(exclude)
    found: set[str] = set()
    for t in [*(getattr(model, "tracks", None) or []), *(getattr(model, "arcs", None) or [])]:
        net = _net_name(getattr(t, "net", None))
        if not net or net in skip or getattr(t, "locked", False):
            continue
        seg = BBox.of([t.start, t.end])
        if seg is not None and any(b.intersects(seg) for b in boxes):
            found.add(net)
    for via in getattr(model, "vias", None) or []:
        net = _net_name(getattr(via, "net", None))
        if not net or net in skip or getattr(via, "locked", False):
            continue
        if any(b.contains(via.position) for b in boxes):
            found.add(net)
    return sorted(found)


def violation_nets(violations: Iterable[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """The nets named in must_fix violations (`[NET]` tags KiCad puts into item descriptions,
    e.g. `Track [GND] on F.Cu`, `Pad 3 [VCC] of U1`) and the violations that name none."""
    nets: set[str] = set()
    unfixable: list[dict[str, Any]] = []
    for v in violations:
        if v.get("class", "must_fix") != "must_fix":
            continue
        found = violation_net_names(v)
        if found:
            nets.update(found)
        else:
            unfixable.append(v)
    return sorted(nets), unfixable


_PAD_ITEM = re.compile(r"^Pad (\S+) \[(?P<net>[^\]]*)\] of (?P<ref>\S+)")


def _endpoint(item: dict[str, Any]) -> dict[str, Any]:
    """`{ref, pad, x_mm, y_mm, description}` of a DRC item (`Pad 3 [NET] of U1` parsed; a
    track / via keeps its description and position)."""
    text = str(item.get("description") or "")
    raw_pos = item.get("pos")
    pos: dict[str, Any] = raw_pos if isinstance(raw_pos, dict) else {}
    out: dict[str, Any] = {
        "ref": None,
        "pad": None,
        "x_mm": pos.get("x_mm"),
        "y_mm": pos.get("y_mm"),
        "description": text,
    }
    m = _PAD_ITEM.match(text)
    if m:
        out["ref"], out["pad"] = m.group("ref"), m.group(1)
    return out


def unrouted_from_drc(drc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """KiCad's open connections (`unconnected_items` violations) as `{net, from, to}` with the
    endpoints' ref / pad / position - the shape kw_pcb_check's `unrouted` items have - so the
    receipt names the open nets even when Freerouting did not log its plateau list."""
    if not drc:
        return []
    out: list[dict[str, Any]] = []
    for v in drc.get("violations") or []:
        if v.get("type") != "unconnected_items":
            continue
        items = list(v.get("items") or [])
        nets = sorted(violation_net_names(v))
        a = _endpoint(items[0]) if items else {}
        b = _endpoint(items[1]) if len(items) > 1 else {}
        out.append({"net": nets[0] if len(nets) == 1 else (nets or [""])[0], "from": a, "to": b})
    return out


# ---- the routing core ---------------------------------------------------------------------------------------


@dataclass
class RouteRun:
    """What one DSN -> Freerouting -> SES round trip produced (both tools build their receipts from it)."""

    nets: list[str] | None
    nets_locked: list[str]
    locked_uuids: list[str]
    cleaned: dict[str, Any]
    copper_before: dict[str, int]
    copper_after: dict[str, int]
    diff: dict[str, int]
    router: dict[str, Any]
    ses: dict[str, Any]
    dsn: dict[str, Any]
    rules: dict[str, Any]
    engine: dict[str, Any]
    provider: str
    warnings: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)


def engines_or_unsupported(ctx: ToolContext) -> Any:
    discover = route_backend(ctx, "router_engines")
    # the session's storage home first (kw_env's `engines`), then the platform default where
    # bootstrap installs them (a `--storage-home` override must not hide the engines)
    engines = call_with_accepted(discover, storage_home=ctx.services.storage_home, fallback=True)
    try:
        engines.require()
    except Exception as exc:  # EngineMissing (matched loosely: the backend never imports core)
        if type(exc).__name__ != "EngineMissing":
            raise
        raise KiwrapError(
            "E_UNSUPPORTED",
            str(exc),
            why="the autorouter runs from a private JRE 25 + Freerouting 2.4.1 that bootstrap installs (GPL, never bundled)",
            fix=str(getattr(exc, "fix", "run scripts/bootstrap.ps1"))
            + "; or route by hand with kw_pcb_route",
            data={
                "capability": "engines.freerouting",
                "fallback": "kw_pcb_route",
                "looked_in": str(getattr(exc, "looked_in", "")),
            },
        ) from exc
    return engines


def _cancel_event(handle: JobHandle | None) -> threading.Event | None:
    if handle is None:
        return None
    ev = getattr(handle, "_cancel", None)
    return ev if isinstance(ev, threading.Event) else None


def _progress(handle: JobHandle | None, fraction: float, message: str) -> None:
    if handle is not None:
        handle.check()
        handle.progress(fraction, message)


def route_nets(
    ctx: ToolContext,
    m: Mutation,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    *,
    nets: list[str] | None,
    lock_refs: list[str] | None,
    passes: int,
    timeout_s: float,
    handle: JobHandle | None,
    progress_base: float = 0.0,
    progress_span: float = 0.8,
    prefer: str | None = None,
    operation: str = "routing.autoroute",
    clean_dangling: bool = True,
    from_scratch: bool = False,
    ripped_up: Iterable[str] | None = None,
) -> RouteRun:
    """Lock -> export -> route -> import -> unlock. The board on disk is the routed one afterwards
    (not yet refilled / DRC-checked - the callers do that). `ripped_up` names the nets the caller
    (kw_pcb_fix) already stripped, for the error text of a failed run."""
    # copper removed before the router ran: what a failed run's error must name
    gone = _Removed(
        nets=sorted(set(ripped_up or ())), what="copper" if ripped_up or from_scratch else "stubs"
    )
    try:
        engines = engines_or_unsupported(ctx)
        model = board_model(ctx, project)
        known = board_nets(model)
        if nets is not None:
            nets = [check_net(n, known, "nets") for n in nets]
            if not nets:
                raise KiwrapError(
                    "E_BAD_ARGS", "nets is empty", fix="pass net names, or omit nets to route everything"
                )
        lock_nets = nets_of_refs(model, lock_refs or [])
        return _route_nets_body(
            ctx,
            m,
            install,
            caps,
            config,
            project,
            engines,
            model,
            nets=nets,
            lock_nets=lock_nets,
            passes=passes,
            timeout_s=timeout_s,
            handle=handle,
            progress_base=progress_base,
            progress_span=progress_span,
            prefer=prefer,
            operation=operation,
            clean_dangling=clean_dangling,
            from_scratch=from_scratch,
            gone=gone,
        )
    except BaseException as exc:
        if project.board.name in m.receipt.changed["updated"]:
            # kiwrap's own write (stubs / rip-up gone, or a session imported before a cancel)
            # must not be taken for an external edit by the next call: re-hash at `placed`
            rehash_board_stages(project, m.state, "placed")
        if isinstance(exc, KiwrapError) and exc.code in ("E_TIMEOUT", "E_KICAD_FAILED", "E_CANCELLED"):
            _describe_failure(exc, m, project, operation, gone)
        raise


@dataclass
class _Removed:
    """Copper deleted before the router ran: the nets and whether it was their dangling stubs
    (`clean_dangling`) or whole unlocked copper (`from_scratch`, kw_pcb_fix's rip-up)."""

    nets: list[str]
    what: str  # stubs | copper

    def add(self, names: Iterable[str]) -> None:
        self.nets = sorted({*self.nets, *names})


def _describe_failure(
    exc: KiwrapError, m: Mutation, project: Project, operation: str, gone: _Removed
) -> None:
    """Per-caller wording of a failed run: what is on disk and how to get the copper back."""
    written = project.board.name in m.receipt.changed["updated"]
    restore = m.baseline or "the snapshot before this call (kw_snapshot list)"
    if not written:
        exc.why += "; the board is unchanged"
        return
    names = ", ".join(gone.nets[:12]) + (" ..." if len(gone.nets) > 12 else "")
    if operation == "routing.fix":
        exc.why += f"; the copper ripped up for this round ({names or 'the violating nets'}) is gone"
        exc.fix = f"kw_snapshot(action='restore', ref='{restore}') brings the copper back; then {exc.fix}"
    elif gone.what == "copper":
        exc.why += f"; the unlocked copper of {names} deleted before the run (from_scratch) is gone"
        exc.fix = f"{exc.fix}; kw_snapshot(action='restore', ref='{restore}') brings the deleted copper back"
    else:
        exc.why += f"; the dangling stubs of {names or 'the routed nets'} deleted before the run are gone"
        exc.fix = f"{exc.fix}; kw_snapshot(action='restore', ref='{restore}') brings the stubs back"
    exc.data["copper_removed"] = list(gone.nets)
    exc.data["restore_ref"] = m.baseline


def _route_nets_body(
    ctx: ToolContext,
    m: Mutation,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    engines: Any,
    model: Any,
    *,
    nets: list[str] | None,
    lock_nets: list[str],
    passes: int,
    timeout_s: float,
    handle: JobHandle | None,
    progress_base: float,
    progress_span: float,
    prefer: str | None,
    operation: str,
    clean_dangling: bool,
    from_scratch: bool,
    gone: _Removed,
) -> RouteRun:
    rules_fn = route_backend(ctx, "route_rules")
    rules = (
        call_with_accepted(rules_fn, model, caps, pro_path=project.pro_path) if model is not None else None
    )
    if rules is None:
        from kiwrap.backends.router.rules import RouteRules  # defaults; the file reader is missing

        rules = RouteRules()
        m.receipt.degraded.append("rules_estimated")
        m.receipt.warnings.append("the board file reader is unavailable: router rules are defaults")

    # 0. rip up the stubs of the nets to route (see `dangling_items`), or - `from_scratch` -
    #    every unlocked track / via of those nets, so the router starts over instead of
    #    optimising the plateau it already reached
    cleaned: list[CopperItem] = []
    if from_scratch:
        cleaned = [
            it
            for it in copper_items(model)
            if not it.locked and it.net not in set(lock_nets) and (nets is None or it.net in set(nets))
        ]
    elif clean_dangling:
        cleaned = [it for it in dangling_items(model, nets) if it.net not in set(lock_nets)]
    if cleaned:
        w = board_writer(ctx, install, caps, config, project)
        for it in cleaned:
            with contextlib.suppress(Exception):
                w.delete(it.uuid)
        w.save()
        model = board_model(ctx, project)
        if project.board.name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(project.board.name)
        gone.add(it.net for it in cleaned if it.net)
    # 1. lock what must survive KiCad's SES import (claim 7: unlocked tracks / vias are deleted)
    items = copper_items(model)
    keep_nets: set[str] = set(lock_nets)
    if nets is not None:
        keep_nets.update(it.net for it in items if it.net and it.net not in nets)
    to_lock = [it.uuid for it in items if not it.locked and it.net in keep_nets]
    _progress(handle, progress_base, f"locking {len(to_lock)} kept track(s) / via(s)")
    locked_now = lock_items(ctx, install, caps, config, project, to_lock, True)
    before = _counts(items)
    scratch = project.state_dir / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    dsn_path = scratch / f"{project.name}{DSN_SUFFIX}"
    ses_path = scratch / f"{project.name}{SES_SUFFIX}"
    try:
        # 2. export the DSN through the first available codec provider
        _progress(handle, progress_base + 0.05 * progress_span, "exporting the Specctra DSN")

        def attempt(name: str) -> Callable[[], tuple[Codec, Path]]:
            def run() -> tuple[Codec, Path]:
                codec = PROVIDER_FACTORIES[name](ctx, install, caps, config, project)
                return codec, codec.export(project.board, dsn_path)

            return run

        outcome = planner.run(
            operation,
            caps,
            {name: attempt(name) for name in PROVIDER_FACTORIES},
            install=install,
            prefer=prefer,
        )
        codec, dsn_written = outcome.value
        outcome.patch = codec.patch
        from kiwrap.backends.router.dsn_post import DsnError, dsn_summary, parse_dsn

        try:
            summary = dsn_summary(parse_dsn(dsn_written.read_text(encoding="utf-8", errors="replace")))
        except (DsnError, OSError) as exc:
            raise KiwrapError(
                "E_KICAD_FAILED",
                f"the exported DSN is not parseable: {exc}",
                why="the DSN provider wrote something Freerouting will refuse as well",
                fix="report this with data.dsn; retry with another provider if kw_env lists one",
                data={"dsn": str(dsn_written), "provider": codec.name},
            ) from exc
        if not summary.get("boundary"):
            raise KiwrapError(
                "E_BAD_ARGS",
                "the board has no closed outline on Edge.Cuts: the DSN carries no boundary",
                why="Freerouting needs the board boundary; KiCad's exporter would fall back to a bounding box and warn",
                fix="draw the outline first (kw_pcb_setup outline={w, h} or points_mm)",
            )
        # 3. Freerouting
        router_cls = route_backend(ctx, "router")
        router = call_with_accepted(
            router_cls, engines, temp_dir=ctx.services.storage_home / "tmp" / "freerouting"
        )
        span = 0.75 * progress_span

        def on_progress(p: Any) -> None:
            frac = float(getattr(p, "fraction", 0.0))
            msg = str(getattr(p, "message", "") or "")
            unrouted = getattr(p, "unrouted", None)
            text = f"routing: {msg}" if unrouted is None else f"routing: {msg} ({unrouted} unrouted)"
            if handle is not None:
                handle.progress(progress_base + 0.1 * progress_span + span * frac, text)

        try:
            result = router.route(
                dsn_written,
                ses_path,
                rules=rules,
                nets=nets,
                passes=passes,
                timeout_s=timeout_s,
                progress_cb=on_progress,
                cancel_event=_cancel_event(handle),
            )
        except Exception as exc:  # noqa: BLE001 - classified into the teaching vocabulary
            name = type(exc).__name__
            if name == "RouterCancelled":
                if handle is not None:
                    # the job ends in state `cancelled` (JobQueue), not `failed`; the snapshot
                    # message carries E_CANCELLED through to_kiwrap_error
                    raise JobCancelled(handle.job_id) from exc
                raise KiwrapError(
                    "E_TIMEOUT",
                    f"routing cancelled: {exc}",
                    why="the run was cancelled; no session was imported",
                    fix="run kw_pcb_route_auto again",
                    data={"cancelled": True},
                ) from exc
            if name == "RouterTimeout" or isinstance(exc, TimeoutError):
                raise KiwrapError(
                    "E_TIMEOUT",
                    str(exc),
                    why="the router did not finish within timeout_s; no session was imported",
                    fix="retry with a larger timeout_s or fewer passes, or route a subset (nets=[...])",
                    data={
                        "log": str(getattr(exc, "log", ""))[-4000:],
                        "elapsed_s": getattr(exc, "elapsed_s", None),
                    },
                ) from exc
            if name == "RouterError":
                raise KiwrapError(
                    "E_KICAD_FAILED",
                    str(exc),
                    why="Freerouting exited without a session file; no session was imported",
                    fix="read data.log; kw_help(topic='kw_pcb_route_auto') lists the known causes",
                    data={
                        "engine": "freerouting",
                        "exit_code": getattr(exc, "exit_code", None),
                        "log": str(getattr(exc, "log", ""))[-4000:],
                        "argv": list(getattr(exc, "argv", []) or []),
                    },
                ) from exc
            raise to_kiwrap_error(exc) from exc
        # 4. import the SES with the SAME codec
        _progress(handle, progress_base + 0.88 * progress_span, "importing the session into the board")
        ses_report = codec.import_ses(project.board, ses_path)
        if project.board.name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(project.board.name)
    finally:
        # 5. the lock flags kiwrap set are not the agent's decision: restore them (a failed import
        # left the board as it was, so this is a no-op then)
        if locked_now:
            with contextlib.suppress(KiwrapError):
                lock_items(ctx, install, caps, config, project, locked_now, False)
    after_model = board_model(ctx, project)
    after_items = copper_items(after_model)
    diff = _diff_copper(items, after_items)
    ses = {**diff, **ses_report}
    engine_info = {
        "name": "freerouting",
        "version": getattr(engines, "freerouting_version", "") or None,
        "jre": getattr(engines, "jre_release", "") or None,
    }
    run = RouteRun(
        nets=nets,
        nets_locked=sorted(keep_nets),
        locked_uuids=locked_now,
        cleaned={
            "tracks": sum(1 for it in cleaned if it.kind != "via"),
            "vias": sum(1 for it in cleaned if it.kind == "via"),
            "nets": sorted({it.net for it in cleaned}),
        },
        copper_before=before,
        copper_after=_counts(after_items),
        diff=diff,
        router=result.to_dict() if hasattr(result, "to_dict") else rec(result),
        ses=ses,
        dsn={
            "provider": codec.name,
            "path": str(dsn_written),  # one DSN / SES pair per project under .kiwrap/tmp, overwritten per run
            **{k: v for k, v in summary.items() if k != "nets"},
            "nets": len(summary.get("nets", {})),
        },
        rules=rules.to_dict() if hasattr(rules, "to_dict") else rec(rules),
        engine=engine_info,
        provider=outcome.provider_used(install),
        warnings=[*outcome.warnings, *(getattr(result, "warnings", None) or [])],
        degraded=list(outcome.degraded),
        plan=outcome.plan.to_dict(),
    )
    for note in getattr(rules, "notes", None) or []:
        run.warnings.append(str(note))
    return run


def _apply_run(m: Mutation, run: RouteRun) -> None:
    m.receipt.provider_used = run.provider
    for tag in run.degraded:
        if tag not in m.receipt.degraded:
            m.receipt.degraded.append(tag)
    for w in run.warnings:
        if w not in m.receipt.warnings:
            m.receipt.warnings.append(w)
    m.receipt.data.setdefault("plan", run.plan)


def _drc_after(
    ctx: ToolContext, m: Mutation, install: Any, caps: Any, config: Any, project: Project, nets: Iterable[str]
) -> dict[str, Any] | None:
    """Mandatory refill + classified DRC after a route (`refill_if_zones`: the fill on disk is
    stale after any copper change; kicad-cli refills and saves where it can)."""
    rehash_board_stages(project, m.state, "placed")
    drc = drc_validator(ctx, m, install, caps, config, project, refill_if_zones=True, touched_nets=nets)
    settle_after_drc(project, m.state, drc)
    return drc


def _unrouted_after(m: Mutation, drc: dict[str, Any] | None, run: RouteRun) -> int | None:
    """KiCad's unconnected count when the DRC ran (the judge), else the router's own."""
    n: int
    if drc is not None:
        n = int(drc.get("unconnected") or 0)
    else:
        raw = run.router.get("unrouted")
        if raw is None:
            return None
        n = int(raw)
    m.validator_delta("unrouted", n, {"router_unrouted": run.router.get("unrouted")})
    return n


# ---- kw_pcb_route_auto ----------------------------------------------------------------------------------


def route_auto_sync(ctx: ToolContext, args: PcbRouteAutoArgs, handle: JobHandle | None = None) -> Receipt:
    project = Project.from_path(args.project)
    started = time.perf_counter()
    with mutation(ctx, project, "kw_pcb_route_auto", force=args.force, gate=True) as m:
        _need_board(project)
        install, caps, config = _install(ctx, m)
        run = route_nets(
            ctx,
            m,
            install,
            caps,
            config,
            project,
            nets=args.nets,
            lock_refs=args.lock_refs,
            passes=args.passes,
            timeout_s=args.timeout_s,
            handle=handle,
            progress_base=0.0,
            progress_span=0.85,
            prefer=None if args.provider == "auto" else args.provider,
            clean_dangling=args.clean_dangling,
            from_scratch=args.from_scratch,
        )
        _apply_run(m, run)
        _progress(handle, 0.88, "zone refill + DRC")
        touched = run.nets if run.nets is not None else sorted(run.ses.get("nets_routed") or [])
        drc = _drc_after(ctx, m, install, caps, config, project, touched)
        unrouted = _unrouted_after(m, drc, run)
        must_fix = int(drc["must_fix"]) if drc is not None else None
        ok = (drc["ok"] if drc is not None else bool(run.router.get("ok"))) and not unrouted
        # KiCad's list of open connections is the judge's; the router's own list (empty when it
        # did not log its plateau) is the fallback
        open_connections = unrouted_from_drc(drc) or list(run.router.get("unrouted_connections", []))
        m.receipt.data.update(
            {
                "ok": ok,
                "engine": run.engine,
                "rules": run.rules,
                "dsn": run.dsn,
                "router": run.router,
                "routed": {
                    "nets": run.nets,
                    "nets_locked": run.nets_locked,
                    "kept_locked": len(run.locked_uuids),
                    "cleaned": run.cleaned,
                    "copper_before": run.copper_before,
                    "copper_after": run.copper_after,
                    **run.diff,
                    "unrouted": unrouted,
                    "unrouted_connections": open_connections,
                    "from_scratch": args.from_scratch,
                },
                "ses": run.ses,
                "drc": drc,
                "duration_s": round(time.perf_counter() - started, 2),
                "forced": m.forced_reason,
            }
        )
        if m.forced_reason:
            m.receipt.warnings.append(f"gate overridden: {m.forced_reason}")
        m.summary = (
            f"nets={'all' if run.nets is None else len(run.nets)} passes={run.router.get('passes')} "
            f"unrouted={unrouted} must_fix={must_fix} provider={run.provider}"
        )
        if must_fix or unrouted:
            # a re-run would only optimise the same copper: kw_pcb_fix rips the open nets up
            m.receipt.next_suggested = "kw_pcb_fix"
        else:
            m.receipt.next_suggested = "kw_pcb_drc" if drc is None else None
        _progress(handle, 1.0, "done")
        return m.receipt


def prequeue_checks(
    ctx: ToolContext,
    project: Project,
    *,
    tool: str,
    force: Any,
    nets: list[str] | None = None,
    lock_refs: list[str] | None = None,
) -> None:
    """What an async call validates BEFORE it queues the job, so the gate, an unknown net /
    reference and missing engines arrive as immediate isError results (E_GATE / E_BAD_ARGS /
    E_UNSUPPORTED) instead of one kw_job round trip later behind `ok: true`."""
    _need_board(project)
    state, _external = read_state(project)
    check_gate(tool, state.stage, Force.parse(force))
    engines_or_unsupported(ctx)
    if nets is not None or lock_refs:
        model = board_model(ctx, project)
        if nets is not None:
            known = board_nets(model)
            for n in nets:
                check_net(n, known, "nets")
            if not nets:
                raise KiwrapError(
                    "E_BAD_ARGS", "nets is empty", fix="pass net names, or omit nets to route everything"
                )
        nets_of_refs(model, lock_refs or [])


def route_auto(ctx: ToolContext, args: PcbRouteAutoArgs) -> Receipt:
    if not args.run_async:
        return route_auto_sync(ctx, args)
    project = Project.from_path(args.project)  # fail fast on a bad path before queueing
    prequeue_checks(
        ctx, project, tool="kw_pcb_route_auto", force=args.force, nets=args.nets, lock_refs=args.lock_refs
    )

    def work(handle: JobHandle) -> dict[str, Any]:
        return receipt_to_dict(route_auto_sync(ctx, args, handle))  # ok follows data.ok

    rec_ = ctx.jobs.submit("kw_pcb_route_auto", work)
    return Receipt(
        provider_used="jobs",
        next_suggested="kw_job",
        data={"ok": True, "job_id": rec_.job_id, "state": rec_.state, "engine": args.engine},
    )


PCB_ROUTE_AUTO = ToolSpec(
    name="kw_pcb_route_auto",
    title="Autoroute",
    description=(
        "Autoroute the board with Freerouting (managed JRE) over KiCad's Specctra DSN/SES round trip: lock the "
        "tracks to keep -> export DSN -> route (fanout off unless BGA, copper-to-edge from the board rule, -mt 1) "
        "-> import SES -> zone refill -> kicad-cli DRC classified. `nets` routes a subset (the other nets leave "
        "the DSN network: their pads and copper stay as locked obstacles), `lock_refs` keeps the nets of those "
        "footprints, `passes` / `timeout_s` bound the router. A re-run keeps and only optimises the existing "
        "unlocked copper (same plateau); `from_scratch=true` deletes it first, kw_pcb_fix rips up the violating "
        "nets. Gated on stage placed. Default run_async=true: returns a job_id (kw_job shows pass / unrouted "
        "progress and cancels; the gate, unknown nets and missing engines are refused before queueing). "
        "Receipt: routed counts, unrouted (KiCad's) with unrouted_connections {net, from, to}, drc {must_fix, "
        "review, unconnected, violations}, provider_used (cli / codec / swig), next_suggested kw_pcb_fix when "
        "must_fix > 0 or a connection is open."
    ),
    input_model=PcbRouteAutoArgs,
    handler=route_auto,
    annotations=DESTRUCTIVE,
)


# ---- kw_pcb_fix -----------------------------------------------------------------------------------------------


def _rip_up(
    ctx: ToolContext,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    nets: Iterable[str],
    *,
    clean: bool,
) -> dict[str, Any]:
    """Delete the unlocked tracks / arcs / vias of `nets` (and, with `clean`, zero-length and
    net-less unlocked items anywhere)."""
    model = board_model(ctx, project)
    items = copper_items(model)
    targets = set(nets)
    doomed = [it for it in items if not it.locked and it.net in targets]
    if clean:
        doomed += [
            it for it in items if not it.locked and it not in doomed and (it.zero_length or not it.net)
        ]
    if not doomed:
        return {"tracks_removed": 0, "vias_removed": 0, "cleaned": 0}
    w = board_writer(ctx, install, caps, config, project)
    removed = 0
    for it in doomed:
        with contextlib.suppress(Exception):
            w.delete(it.uuid)
            removed += 1
    w.save()
    return {
        "tracks_removed": sum(1 for it in doomed if it.kind != "via"),
        "vias_removed": sum(1 for it in doomed if it.kind == "via"),
        "cleaned": sum(1 for it in doomed if it.net not in targets),
        "deleted": removed,
    }


def _collapse_validators(m: Mutation, names: Iterable[str]) -> None:
    """kw_pcb_fix records a validator per round (initial DRC, then one per round): keep ONE entry
    per name whose `before` is the first value seen (the initial DRC) and `after` the last, so the
    receipt's `drc` delta reads like `data.must_fix` (before = initial, after = final)."""
    for name in names:
        deltas = [d for d in m.receipt.validators if d.name == name]
        if len(deltas) < 2:
            continue
        first, last = deltas[0], deltas[-1]
        last.before = first.after
        for d in deltas[:-1]:
            m.receipt.validators.remove(d)


def fix_sync(ctx: ToolContext, args: PcbFixArgs, handle: JobHandle | None = None) -> Receipt:
    project = Project.from_path(args.project)
    actions = list(dict.fromkeys(args.actions))
    started = time.perf_counter()
    with mutation(ctx, project, "kw_pcb_fix", force=args.force, gate=True) as m:
        _need_board(project)
        install, caps, config = _install(ctx, m)
        if not caps.has("cli.has_drc"):
            raise KiwrapError(
                "E_UNSUPPORTED",
                "kw_pcb_fix needs kicad-cli DRC (cli.has_drc) to find what to fix",
                why="KiCad is the judge: the violations to rip up come from its DRC",
                fix="retarget the project to a KiCad with cli.has_drc",
                data={"capability": "cli.has_drc", "fallback": "kw_pcb_route"},
            )
        _progress(handle, 0.02, "initial DRC")
        drc = _drc_after(ctx, m, install, caps, config, project, [])
        if drc is None:
            raise KiwrapError("E_KICAD_FAILED", "DRC did not run", fix="see warnings")
        before_must_fix = int(drc["must_fix"])
        iterations: list[dict[str, Any]] = []
        unfixable: list[dict[str, Any]] = []
        provider = ""
        nets_all: set[str] = set()
        stalled = 0  # rounds in a row that did not reduce must_fix -> widen the scope
        for i in range(args.max_iterations):
            if int(drc["must_fix"]) == 0:
                break
            nets, unfixable = violation_nets(drc["violations"])
            # the listed violations are capped at MAX_LISTED; the validator names the nets of
            # EVERY must_fix violation so no net beyond the first 50 escapes the rip-up
            nets = sorted({*nets, *(drc.get("nets_with_must_fix") or [])})
            if not nets:
                break
            scope = "nets"
            margin = 0.0
            widened: list[str] = []
            if stalled >= 2:
                scope = "board"
            elif stalled == 1:
                margin = FIX_MARGIN_MM * (2 ** (stalled - 1))
                widened = neighbour_nets(board_model(ctx, project), drc["violations"], margin, exclude=nets)
                scope = "neighbourhood" if widened else "nets"
            rip_nets = sorted({*nets, *widened})
            route_nets_arg: list[str] | None = None if scope == "board" else rip_nets
            nets_all.update(rip_nets)
            base = 0.05 + 0.9 * i / args.max_iterations
            span = 0.9 / args.max_iterations
            entry: dict[str, Any] = {
                "iteration": i + 1,
                "nets": nets,
                "scope": scope,
                "ripped_up": rip_nets,
                "margin_mm": margin,
                "must_fix_before": int(drc["must_fix"]),
                "violations_truncated": int(drc.get("violations_truncated") or 0),
            }
            _progress(
                handle,
                base,
                f"round {i + 1} ({scope}): {len(nets)} net(s) with must_fix violations"
                + (f", {len(widened)} neighbour(s)" if widened else ""),
            )
            if "rip_up_violations" in actions or "clean" in actions:
                entry["rip_up"] = _rip_up(
                    ctx,
                    install,
                    caps,
                    config,
                    project,
                    rip_nets if "rip_up_violations" in actions else [],
                    clean="clean" in actions,
                )
                if project.board.name not in m.receipt.changed["updated"]:
                    m.receipt.changed["updated"].append(project.board.name)
            if "reroute" in actions:
                run = route_nets(
                    ctx,
                    m,
                    install,
                    caps,
                    config,
                    project,
                    nets=route_nets_arg,
                    lock_refs=None,
                    passes=args.passes,
                    timeout_s=args.timeout_s,
                    handle=handle,
                    progress_base=base + 0.1 * span,
                    progress_span=0.7 * span,
                    prefer=None if args.provider == "auto" else args.provider,
                    operation="routing.fix",
                    clean_dangling="clean" in actions,
                    ripped_up=rip_nets if "rip_up_violations" in actions else None,
                )
                _apply_run(m, run)
                provider = run.provider
                entry["router"] = {
                    k: run.router.get(k) for k in ("passes", "unrouted", "score", "duration_s")
                }
                entry.update({k: run.diff[k] for k in run.diff})
                entry["cleaned"] = run.cleaned
            _progress(handle, base + 0.85 * span, f"round {i + 1}: refill + DRC")
            drc = _drc_after(ctx, m, install, caps, config, project, nets)
            if drc is None:
                break
            entry["must_fix_after"] = int(drc["must_fix"])
            entry["unconnected_after"] = int(drc.get("unconnected") or 0)
            iterations.append(entry)
            if entry["must_fix_after"] >= entry["must_fix_before"]:
                stalled += 1
                m.receipt.warnings.append(
                    f"round {i + 1} ({scope}) did not reduce must_fix "
                    f"({entry['must_fix_before']} -> {entry['must_fix_after']})"
                    + (
                        "; the next round widens the rip-up to the neighbourhood"
                        if stalled == 1
                        else "; the next round re-routes the whole board"
                        if stalled == 2
                        else ""
                    )
                )
            else:
                stalled = 0
        final_must_fix = int(drc["must_fix"]) if drc is not None else None
        if final_must_fix:
            _nets_left, unfixable = violation_nets(drc["violations"]) if drc is not None else ([], unfixable)
        m.receipt.provider_used = provider or ctx.services.provider(install)
        m.validator_delta("unrouted", int(drc.get("unconnected") or 0) if drc is not None else 0)
        _collapse_validators(m, ("drc", "drc_touched_nets", "unrouted"))
        m.receipt.data.update(
            {
                "ok": final_must_fix == 0,
                "actions": actions,
                "iterations": iterations,
                "must_fix": {"before": before_must_fix, "after": final_must_fix},
                "nets_touched": sorted(nets_all),
                "unfixable": unfixable[:MAX_LISTED],
                "drc": drc,
                "duration_s": round(time.perf_counter() - started, 2),
                "forced": m.forced_reason,
            }
        )
        if m.forced_reason:
            m.receipt.warnings.append(f"gate overridden: {m.forced_reason}")
        if unfixable and final_must_fix:
            m.receipt.warnings.append(
                f"{len(unfixable)} must_fix violation(s) name no net (not fixable by re-routing): "
                + ", ".join(sorted({str(v.get("type")) for v in unfixable}))
            )
        m.summary = f"rounds={len(iterations)} must_fix {before_must_fix} -> {final_must_fix}"
        if final_must_fix:
            m.receipt.next_suggested = "kw_pcb_drc" if unfixable and not nets_all else "kw_pcb_fix"
        else:
            m.receipt.next_suggested = None
        _progress(handle, 1.0, "done")
        return m.receipt


def fix(ctx: ToolContext, args: PcbFixArgs) -> Receipt:
    if not args.run_async:
        return fix_sync(ctx, args)
    project = Project.from_path(args.project)
    prequeue_checks(ctx, project, tool="kw_pcb_fix", force=args.force)

    def work(handle: JobHandle) -> dict[str, Any]:
        return receipt_to_dict(fix_sync(ctx, args, handle))  # ok follows data.ok

    rec_ = ctx.jobs.submit("kw_pcb_fix", work)
    return Receipt(
        provider_used="jobs",
        next_suggested="kw_job",
        data={"ok": True, "job_id": rec_.job_id, "state": rec_.state},
    )


PCB_FIX = ToolSpec(
    name="kw_pcb_fix",
    title="Fix DRC by re-routing",
    description=(
        "Repair must_fix DRC violations by routing: DRC -> the nets named in must_fix violations (unconnected "
        "items included) -> rip up their unlocked tracks / vias -> clean zero-length / net-less leftovers -> "
        "re-route only those nets with Freerouting -> zone refill -> DRC, repeated until must_fix == 0 or "
        "max_iterations; a round that does not reduce must_fix widens the next one (scope nets -> neighbourhood: "
        "other nets' copper within 2 mm of the violation ripped up too -> board: everything unlocked re-routable). "
        "Violations naming no net are listed as `unfixable`. Gated on stage placed (force={reason} overrides). "
        "Default run_async=true (kw_job)."
    ),
    input_model=PcbFixArgs,
    handler=fix,
    annotations=DESTRUCTIVE,
)

ROUTE_TOOLS: tuple[ToolSpec, ...] = (PCB_ROUTE_AUTO, PCB_FIX)

__all__ = [
    "PCB_FIX",
    "PCB_ROUTE_AUTO",
    "ROUTE_BACKENDS",
    "ROUTE_TOOLS",
    "Codec",
    "PcbFixArgs",
    "PcbRouteAutoArgs",
    "RouteRun",
    "copper_items",
    "neighbour_nets",
    "route_backend",
    "route_nets",
    "prequeue_checks",
    "set_locked",
    "unrouted_from_drc",
    "violation_nets",
]
