"""Board authoring: kw_pcb_setup, kw_pcb_sync, kw_pcb_place, kw_pcb_zones, kw_pcb_route
(docs/ARCHITECTURE.md §8 placement/routing strategy, §10 catalog, §11 receipts and gates).

Every tool is a mutation (`_base.mutation`) writing through a planner operation (`board.setup`
file only; `board.sync` / `board.place` / `board.zones` / `board.route_primitives` ipc11 -> file,
see `_author`). Validators land in the receipt after every write and drive the stage machine:

    kw_pcb_setup   -> DRC summary; placement checks decide whether `placed` survives
    kw_pcb_sync    -> gate erc_clean (last kw_sch_check ok); DRC with schematic parity;
                      stage `synced`; new footprints land off-board, so `placed` is dropped
    kw_pcb_place   -> courtyard overlaps / off-board / unrouted before -> after (the kw_pcb_check
                      code); no copper off-board and no overlap -> `placed`
    kw_pcb_zones   -> zone fill (kicad-cli drc --refill-zones --save-board on a KiCad with
                      cli.drc_refill_flags, RefillZones over IPC) + DRC summary
    kw_pcb_route   -> track polyline / via / delete primitives + DRC with the delta of the
                      touched nets (`drc_touched_nets`); must_fix == 0 -> `routed` + `drc_clean`

The board-derived stage hashes are re-recorded after every write so the next call does not
mistake kiwrap's own write for an external edit; everything above the stage a write invalidates
is dropped (§11: a validator result is only ever valid for the file it was computed from).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.stages import downgrade_below, record_stage, stage_at_least
from kiwrap.core.tools._author import (
    apply_outcome,
    board_writer,
    call_with_accepted,
    drc_validator,
    g16_guard,
    hierarchy,
    ipc_batch,
    note_delta,
    placement_ok,
    placement_summary,
    rehash_board_stages,
    run_write,
    settle_placed,
)
from kiwrap.core.tools._base import DESTRUCTIVE, MUTATING, Mutation, ToolContext, ToolSpec, mutation
from kiwrap.core.tools._data import pick, rec, ref_key
from kiwrap.core.tools.pcb_get import norm_footprint, norm_rules, project_netclasses

ProviderArg = Literal["auto", "ipc11", "file"]
Side = Literal["top", "bottom"]

DEFAULT_TRACK_WIDTH_MM = 0.25
DEFAULT_VIA_SIZE_MM = 0.8
DEFAULT_VIA_DRILL_MM = 0.4
OFF_BOARD_NOTE = "new footprints are placed off-board in a grid: kw_pcb_place moves them inside the outline"


def _install(ctx: ToolContext, m: Mutation) -> tuple[Any, Any, Any]:
    install = ctx.services.install(m.state.target_kicad)
    return install, ctx.services.caps(install), ctx.services.config_root(install)


def _need_board(project: Project) -> None:
    if not project.board.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"board {project.board.name} is missing",
            fix="kw_project(action='create') writes it",
        )


def _pt(value: Any, name: str) -> tuple[float, float]:
    if value is None or len(value) != 2:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{name} must be [x, y] in millimetres",
            fix=f"pass {name}=[x_mm, y_mm]",
            example=f"{name}=[25.4, 30]",
        )
    return float(value[0]), float(value[1])


def _saved(m: Mutation, project: Project, path: Any) -> None:
    name = getattr(path, "name", None) or project.board.name
    if name not in m.receipt.changed["updated"]:
        m.receipt.changed["updated"].append(name)


def board_model(ctx: ToolContext, project: Project) -> Any:
    """The file reader's board model, or None when the reader is not wired."""
    try:
        return ctx.services.file_read_board(project.board)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED":
            return None
        raise


def board_rules(model: Any, project: Project | None = None) -> dict[str, Any]:
    rules = (norm_rules(rec(model)) if model is not None else None) or {}
    if project is not None and rules and not rules.get("netclasses"):
        rules["netclasses"] = project_netclasses(project.pro_path)
    return rules


def _net_pattern_matches(pattern: str, net: str) -> bool:
    """KiCad's netclass pattern semantics: a plain name matches exactly, `*` / `?` are wildcards
    (`WILDCARD_NETCLASS_ASSIGNMENT`, case-sensitive)."""
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(net, pattern)
    return pattern == net


def netclass_warnings(netclasses: Mapping[str, Any], nets: list[str]) -> list[str]:
    """One warning per netclass net / pattern that matches none of `nets` (the schematic's
    netlist, else the board's), with the closest real names: `VIN` is a mistake when the label is
    local (`/VIN`) or lives in a child sheet (`/blinker/VIN`) - KiCad accepts the pattern silently
    and the netclass never applies (reviewer finding, 2026-09-20)."""
    out: list[str] = []
    if not nets:
        return out
    for name, spec in netclasses.items():
        patterns = (
            pick(rec(spec), "nets", "patterns", default=[]) or [] if not isinstance(spec, list) else spec
        )
        for pattern in patterns:
            pat = str(pattern)
            if any(_net_pattern_matches(pat, n) for n in nets):
                continue
            bare = pat.lstrip("/").split("/")[-1]
            near = [
                n for n in nets if n == bare or n.endswith("/" + bare) or (bare and bare.lower() in n.lower())
            ]
            hint = f"; did you mean {', '.join(near[:5])}?" if near else ""
            kind = " (local-label nets are named /<label>, child-sheet nets /<sheet>/<label>)" if near else ""
            out.append(f"netclass {name}: {pat!r} matches no net{kind}{hint}")
    return out


def board_nets(model: Any) -> list[str]:
    """The named nets of the board (file model `nets`), sorted; empty when unknown."""
    if model is None:
        return []
    names: set[str] = set()
    for n in pick(rec(model), "nets", default=[]) or []:
        name = n if isinstance(n, str) else pick(rec(n), "name", "net_name")
        if name:
            names.add(str(name))
    return sorted(names)


def check_net(net: str, nets: list[str], where: str) -> str:
    """A track / via / zone net must be a net the board has: a local label's net is `/SIG` on the
    board, not `SIG`, and a misspelt net would silently create a new one (a KiCad net_conflict /
    unconnected_items later). E_BAD_ARGS names the candidates that end with the given text."""
    if not net or not nets or net in nets:
        return net
    close = [n for n in nets if n.endswith("/" + net) or n.lower() == net.lower() or net in n]
    raise KiwrapError(
        "E_BAD_ARGS",
        f"{where}: net {net!r} is not on the board",
        why="a local label's net is named '/<label>' on the board; an unknown net would create a new, unconnected one",
        fix="use one of the board's net names (kw_pcb_get kinds=['nets'])",
        candidates=close or nets[:50],
    )


def footprints_by_ref(model: Any) -> dict[str, dict[str, Any]]:
    if model is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for fp in pick(rec(model), "footprints", default=[]) or []:
        d = norm_footprint(fp)
        if d.get("ref"):
            out[str(d["ref"])] = d
    return out


def _copper_layer(layer: str, copper: list[str]) -> str:
    if not layer.endswith(".Cu"):
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{layer!r} is not a copper layer",
            why="a track or via on a non-copper layer is silently dropped by KiCad (docs/UPSTREAM.md 1.3)",
            fix="use one of the board's copper layers",
            candidates=copper or ["F.Cu", "B.Cu"],
        )
    if copper and layer not in copper:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{layer!r} is not a copper layer of this board",
            fix="use one of the board's copper layers (kw_pcb_get kinds=['rules'])",
            candidates=copper,
        )
    return layer


def settle_after_drc(project: Project, state: Any, drc: dict[str, Any] | None) -> None:
    """After a routing / zone write: a clean DRC (must_fix == 0, unconnected_items included) is
    KiCad saying the board is routed -> `routed` + `drc_clean`; anything else drops `routed`."""
    if drc is not None and drc["ok"]:
        if stage_at_least(state.stage, "placed"):
            record_stage(project, state, "routed")
        record_stage(project, state, "drc_clean", advance=stage_at_least(state.stage, "routed"))
    else:
        downgrade_below(state, "routed")


# ---- kw_pcb_setup -------------------------------------------------------------------------------------------


class PcbSetupArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    preset: str | None = Field(
        default=None,
        description=(
            "a fabrication rules preset (jlcpcb-2layer | jlcpcb-4layer, kiwrap.core.fab): sets `layers` and the "
            "vendor's safe `rules`; explicit `layers` / `rules` keys override the preset's"
        ),
    )
    layers: int | list[str] | None = Field(
        default=None, description="copper layer count (2, 4, ...) or explicit copper layer names"
    )
    rules: dict[str, float] | None = Field(
        default=None,
        description=(
            "design rules in mm: clearance_mm, track_width_mm, via_size_mm, via_drill_mm (the Default netclass), "
            "min_track_mm, min_via_mm, min_via_annular_mm, min_hole_mm, hole_to_hole_mm, hole_clearance_mm, "
            "copper_edge_clearance_mm, silk_clearance_mm, min_connection_mm (board minimums)"
        ),
    )
    netclasses: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description="{name: {nets: [...], clearance_mm, track_width_mm, via_size_mm, via_drill_mm, ...}} written to the .kicad_pro",
    )
    outline: dict[str, Any] | None = Field(
        default=None,
        description="{w, h} rectangle (+ x, y origin) or {points_mm: [[x, y], ...]} polygon on Edge.Cuts",
    )
    custom_rules: str | None = Field(
        default=None, description="KiCad custom DRC rules text -> <name>.kicad_dru"
    )
    drc: bool = Field(default=True, description="run DRC after the write (summary only; it never gates here)")


def outline_spec(outline: dict[str, Any] | None) -> dict[str, Any] | None:
    """The writer's outline dict for the tool's: `{w, h[, x, y]}` -> `{w, h, origin}`,
    `{points_mm}` -> `{polygon}` (the writer's own keys `rect`, `origin`, `polygon` pass through)."""
    if outline is None:
        return None
    out = dict(outline)
    if "points_mm" in out:
        pts = out.pop("points_mm")
        if not isinstance(pts, list) or len(pts) < 3:
            raise KiwrapError(
                "E_BAD_ARGS",
                "outline.points_mm needs at least 3 [x, y] points",
                fix="pass points_mm=[[0, 0], [40, 0], [40, 30], [0, 30]] or {w, h}",
            )
        out["polygon"] = [_pt(p, "outline.points_mm[]") for p in pts]
    if "x" in out or "y" in out:
        out["origin"] = (float(out.pop("x", 0.0)), float(out.pop("y", 0.0)))
    if "polygon" not in out and "pts" not in out and "rect" not in out and not ("w" in out and "h" in out):
        raise KiwrapError(
            "E_BAD_ARGS",
            "outline needs {w, h} (+ x, y) or {points_mm: [[x, y], ...]}",
            example='outline={"w": 40, "h": 30}',
        )
    return out


def known_nets(ctx: ToolContext, m: Mutation, install: Any, config: Any, project: Project) -> list[str]:
    """The net names a netclass pattern can target: the schematic's netlist (KiCad's export; the
    board's nets before a sync are empty), else the board's nets, else nothing (no warning)."""
    names: set[str] = set()
    if project.root_sch.exists():
        try:
            nl, _path = load_netlist(ctx, m, install, config, project)
            names.update(str(n.name) for n in nl.nets if getattr(n, "name", ""))
        except KiwrapError:
            names.clear()
    if not names:
        names.update(board_nets(board_model(ctx, project)))
    return sorted(names)


def apply_preset(args: PcbSetupArgs) -> dict[str, Any] | None:
    """Fold a fab rules preset into `args` (explicit layers / rule keys win); returns the preset."""
    if not args.preset:
        return None
    from kiwrap.core.fab import preset_rules

    try:
        preset = preset_rules(args.preset)
    except ValueError as exc:
        from kiwrap.core.fab import PRESET_NAMES

        raise KiwrapError(
            "E_BAD_ARGS",
            str(exc),
            fix="pick a known preset",
            candidates=list(PRESET_NAMES),
            example='kw_pcb_setup(project=..., preset="jlcpcb-2layer", outline={"w": 50, "h": 30})',
        ) from exc
    if args.layers is None:
        args.layers = preset.layers
    args.rules = {**preset.rules, **(args.rules or {})}
    if preset.netclasses:
        args.netclasses = {**preset.netclasses, **(args.netclasses or {})}
    return preset.to_dict()


def pcb_setup(ctx: ToolContext, args: PcbSetupArgs) -> Receipt:
    project = Project.from_path(args.project)
    outline = outline_spec(args.outline)
    preset = apply_preset(args)
    if not any(
        v is not None for v in (args.layers, args.rules, args.netclasses, args.outline, args.custom_rules)
    ):
        raise KiwrapError(
            "E_BAD_ARGS",
            "nothing to set up: pass layers, rules, netclasses, outline and/or custom_rules",
            example='kw_pcb_setup(project=..., layers=2, rules={"clearance_mm": 0.2, "track_width_mm": 0.25}, outline={"w": 50, "h": 30})',
        )
    with mutation(ctx, project, "kw_pcb_setup", gate=False) as m:
        _need_board(project)
        install, caps, config = _install(ctx, m)

        def via_file() -> Any:
            w = board_writer(ctx, install, caps, config, project)
            applied = call_with_accepted(
                w.setup,
                layers=args.layers,
                rules=args.rules,
                netclasses=args.netclasses,
                outline=outline,
                custom_rules=args.custom_rules,
            )
            _saved(m, project, w.save())
            return applied

        outcome = run_write("board.setup", caps, install, ipc=None, file=via_file, prefer=None)
        apply_outcome(m, outcome, install, ctx)
        if args.netclasses is not None and project.pro_path.name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(project.pro_path.name)
        if args.custom_rules is not None:
            dru = project.dir / f"{project.name}.kicad_dru"
            m.receipt.changed["added" if dru.exists() else "updated"].append(dru.name)
        applied = outcome.value
        m.receipt.data.update(
            {
                "ok": True,
                "applied": rec(applied)
                if applied is not None and not isinstance(applied, dict)
                else (applied or {}),
                "layers": args.layers,
                "rules": args.rules or {},
                "netclasses": sorted((args.netclasses or {}).keys()),
                "outline": args.outline,
                "custom_rules": bool(args.custom_rules),
                "preset": preset,
            }
        )
        rehash_board_stages(project, m.state, "synced")
        summary = placement_summary(ctx, install, caps, config, project, unrouted=False)
        m.receipt.data["checks"] = summary
        m.receipt.degraded.extend(d for d in summary["degraded"] if d not in m.receipt.degraded)
        settle_placed(project, m.state, placement_ok(summary))
        m.receipt.data["drc"] = drc_validator(ctx, m, install, caps, config, project) if args.drc else None
        m.receipt.data["rules_now"] = board_rules(board_model(ctx, project), project)
        if args.netclasses:
            known = known_nets(ctx, m, install, config, project)
            m.receipt.warnings.extend(netclass_warnings(args.netclasses, known))
        m.summary = (
            f"layers={args.layers} rules={sorted((args.rules or {}).keys())} outline={'yes' if args.outline else 'no'}"
            + (f" preset={args.preset}" if preset else "")
        )
        m.receipt.next_suggested = (
            "kw_pcb_sync" if not stage_at_least(m.state.stage, "synced") else "kw_pcb_place"
        )
        return m.receipt


PCB_SETUP = ToolSpec(
    name="kw_pcb_setup",
    title="Board setup",
    description=(
        "Board rules and outline: copper layer count or names, design rules (clearance_mm, track_width_mm, "
        "via_size_mm, via_drill_mm, min_*) or a vendor preset (jlcpcb-2layer | jlcpcb-4layer), netclasses with "
        "their nets (written to the .kicad_pro), a rectangle or polygon outline on Edge.Cuts and custom DRC rules "
        "(.kicad_dru). DRC runs after the write as a summary."
    ),
    input_model=PcbSetupArgs,
    handler=pcb_setup,
    annotations=MUTATING,
)


# ---- kw_pcb_sync ----------------------------------------------------------------------------------------------


class PcbSyncArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    remove_orphans: bool = Field(
        default=True, description="delete footprints whose symbol is no longer in the schematic"
    )
    keep_positions: bool = Field(
        default=True, description="keep position / side / rotation of footprints already on the board"
    )
    match: Literal["path", "reference"] = Field(
        default="path",
        description=(
            "how footprints are paired with symbols: 'path' (KiCad's default, by symbol uuid: a "
            "re-annotated or swapped reference RENAMES the footprint and keeps its nets) or "
            "'reference' (KiCad's 're-link by reference designators' box: the footprint carrying "
            "the reference takes the symbol's value and pad nets)"
        ),
    )
    force: dict[str, str] | str | None = Field(
        default=None, description='override the erc_clean gate: {"reason": "..."} (logged into the snapshot)'
    )
    drc: bool = Field(default=True, description="run DRC with schematic parity after the sync")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 (ImportNetlist) | file")


def sync_report_dict(report: Any) -> dict[str, Any]:
    r = rec(report)

    def refs(name: str) -> list[str]:
        vals = pick(r, name, default=[]) or []
        return sorted(
            (str(v if isinstance(v, str) else pick(rec(v), "ref", default=v)) for v in vals), key=ref_key
        )

    unresolved: list[dict[str, Any]] = []
    for u in pick(r, "unresolved", default=[]) or []:
        if isinstance(u, (list, tuple)) and len(u) >= 3:
            unresolved.append({"ref": str(u[0]), "fpid": str(u[1]), "reason": str(u[2])})
        else:
            ur = rec(u)
            unresolved.append(
                {
                    "ref": pick(ur, "ref"),
                    "fpid": pick(ur, "fpid", "footprint"),
                    "reason": pick(ur, "reason", default=""),
                }
            )
    fp_changes = pick(r, "footprint_changes", default=[]) or []
    pad_changes = pick(r, "pad_net_changes", default=[]) or []
    renamed = pick(r, "renamed", default={}) or {}
    return {
        "added": refs("added"),
        "updated": refs("updated"),
        "removed": refs("removed"),
        "renamed": {str(k): str(v) for k, v in dict(renamed).items()} if isinstance(renamed, dict) else {},
        "footprint_changes": [
            rec(c) if not isinstance(c, (dict, list, tuple, str)) else c for c in fp_changes
        ][:200],
        "pad_net_changes": len(pad_changes) if not isinstance(pad_changes, int) else pad_changes,
        "unresolved": unresolved,
    }


def load_netlist(
    ctx: ToolContext, m: Mutation, install: Any, config: Any, project: Project
) -> tuple[Any, Path | None]:
    """KiCad's own netlist (`sch export netlist`, the oracle) written under .kiwrap/tmp; the file
    walk (`netlist_from_file`, tagged netlist_estimated) when the export is not available."""
    tmp = project.state_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / f"{project.name}.sync.net"
    try:
        return ctx.services.export_netlist(install, config, project.root_sch, out=out), out
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        m.receipt.warnings.append(
            f"kicad-cli netlist export unavailable ({err.message}); using the file netlist"
        )
    hier = hierarchy(ctx, project)
    if hier is None:
        raise KiwrapError(
            "E_UNSUPPORTED",
            "no netlist source: neither kicad-cli sch export netlist nor the file reader is available",
            fix="select a KiCad with cli.has_netlist_export (kw_env) or update kiwrap",
            data={"capability": "cli.has_netlist_export"},
        )
    m.receipt.degraded.append("netlist_estimated")
    return ctx.services.netlist_from_file(hier), None


def pcb_sync(ctx: ToolContext, args: PcbSyncArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_pcb_sync", force=args.force) as m:
        _need_board(project)
        if not project.root_sch.exists():
            raise KiwrapError("E_PROJECT_NOT_FOUND", f"root schematic {project.root_sch.name} is missing")
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        netlist, netlist_path = load_netlist(ctx, m, install, config, project)

        def via_file() -> Any:
            w = board_writer(ctx, install, caps, config, project)
            # the parsed Netlist carries no symbol uuids / sheet tstamps; the kicad-cli `.net`
            # text (when it exists) lets the writer give each footprint its `(path …)`, which is
            # what KiCad pairs footprints and symbols by (BOARD::FindFootprintByPath)
            extra: dict[str, Any] = {}
            if netlist_path is not None and netlist_path.exists():
                extra["netlist_text"] = netlist_path.read_text(encoding="utf-8", errors="replace")
            report = call_with_accepted(
                w.sync,
                netlist,
                remove_orphans=args.remove_orphans,
                keep_positions=args.keep_positions,
                match=args.match,
                **extra,
            )
            _saved(m, project, w.save())
            return report

        def via_ipc() -> Any:
            if netlist_path is None:
                raise KiwrapError(
                    "E_UNSUPPORTED",
                    "ImportNetlist needs the kicad-cli netlist file; only the file netlist is available",
                    data={"capability": "cli.has_netlist_export"},
                )
            with ipc_batch(ctx, install, config, project, project.board, save="ipc_pcb_save") as call:
                return call(
                    "ipc_pcb_sync",
                    netlist_path=netlist_path,
                    netlist=netlist,
                    remove_orphans=args.remove_orphans,
                    keep_positions=args.keep_positions,
                    match=args.match,
                )

        outcome = run_write("board.sync", caps, install, ipc=via_ipc, file=via_file, prefer=prefer)
        report = sync_report_dict(outcome.value)
        m.receipt.changed["added"].extend(report["added"])
        m.receipt.changed["updated"].extend(
            r for r in report["updated"] if r not in m.receipt.changed["updated"]
        )
        m.receipt.changed["removed"].extend(report["removed"])
        apply_outcome(m, outcome, install, ctx)
        ok = not report["unresolved"]
        m.receipt.data.update(
            {
                "ok": ok,
                **report,
                "netlist": {"components": len(list(netlist.components)), "nets": len(list(netlist.nets))},
            }
        )
        # stage: synced (advance only when the gate really held, i.e. not forced from below erc_clean)
        record_stage(project, m.state, "synced", advance=stage_at_least(m.state.stage, "erc_clean"))
        downgrade_below(m.state, "placed")
        record_stage(project, m.state, "synced", advance=False)
        checks = placement_summary(ctx, install, caps, config, project, refs=report["added"], unrouted=False)
        m.receipt.data["checks"] = checks
        m.receipt.degraded.extend(d for d in checks["degraded"] if d not in m.receipt.degraded)
        settle_placed(project, m.state, placement_ok(checks))
        if report["added"]:
            m.receipt.warnings.append(OFF_BOARD_NOTE)
        m.receipt.data["drc"] = (
            drc_validator(ctx, m, install, caps, config, project, parity=True) if args.drc else None
        )
        if not ok:
            m.receipt.warnings.append(
                f"{len(report['unresolved'])} footprint(s) could not be resolved: "
                + "; ".join(f"{u['ref']} {u['fpid']}: {u['reason']}" for u in report["unresolved"][:10])
            )
        m.summary = f"added={len(report['added'])} updated={len(report['updated'])} removed={len(report['removed'])} unresolved={len(report['unresolved'])}"
        if report["renamed"]:
            m.summary += f" renamed={len(report['renamed'])}"
        m.receipt.next_suggested = "kw_sch_assign_footprints" if not ok else "kw_pcb_place"
        return m.receipt


PCB_SYNC = ToolSpec(
    name="kw_pcb_sync",
    title="Sync schematic to board",
    description=(
        "Update the board from the schematic netlist (KiCad's own export): footprints instantiated from the "
        "libraries with pad nets, values/fields/dnp propagated, positions of existing parts kept, orphans removed, "
        "new parts placed off-board in a grid. Gated on erc_clean (the last kw_sch_check ok). After the sync: "
        "DRC with schematic parity and the placement checks. Unresolved footprints come back with reasons."
    ),
    input_model=PcbSyncArgs,
    handler=pcb_sync,
    annotations=DESTRUCTIVE,
)


# ---- kw_pcb_place ----------------------------------------------------------------------------------------------


class Placement(BaseModel):
    ref: str
    at_mm: list[float] = Field(description="[x, y] mm (absolute, or relative to `relative_to`)")
    rotation: float | None = Field(default=None, description="degrees; None keeps the current rotation")
    side: Side | None = Field(default=None, description="top | bottom; None keeps the current side")
    locked: bool | None = None


class PcbPlaceArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    placements: list[Placement] = Field(min_length=1)
    relative_to: str | None = Field(
        default=None, description="a reference: every at_mm is an offset from that footprint's position"
    )
    drc: bool = Field(
        default=False, description="also run DRC after placing (the placement checks always run)"
    )
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def pcb_place(ctx: ToolContext, args: PcbPlaceArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_pcb_place", gate=False) as m:
        _need_board(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        model = board_model(ctx, project)
        fps = footprints_by_ref(model)
        origin = (0.0, 0.0)
        if args.relative_to:
            base = fps.get(args.relative_to)
            if base is None:
                raise KiwrapError(
                    "E_BAD_ARGS",
                    f"relative_to {args.relative_to!r} is not on the board",
                    fix="use a reference kw_pcb_get lists",
                    candidates=sorted(fps, key=ref_key)[:50],
                )
            origin = (float(base["x_mm"] or 0.0), float(base["y_mm"] or 0.0))
        refs = [p.ref for p in args.placements]
        missing = [r for r in refs if model is not None and r not in fps]
        if missing:
            raise KiwrapError(
                "E_BAD_ARGS",
                f"not on the board: {', '.join(missing)}",
                why="footprints reach the board through kw_pcb_sync",
                fix="run kw_pcb_sync, or use references kw_pcb_get lists",
                candidates=sorted(fps, key=ref_key)[:50],
            )
        before = placement_summary(ctx, install, caps, config, project, refs=refs)
        moves: list[dict[str, Any]] = []
        for p in args.placements:
            x, y = _pt(p.at_mm, f"placements[{p.ref}].at_mm")
            moves.append(
                {
                    "ref": p.ref,
                    "at_mm": (round(origin[0] + x, 4), round(origin[1] + y, 4)),
                    "rotation": p.rotation,
                    # the writers speak front / back (the file writer also takes top / bottom)
                    "side": {"top": "front", "bottom": "back"}.get(p.side or "", p.side),
                    "locked": p.locked,
                }
            )

        def via_file() -> Any:
            w = board_writer(ctx, install, caps, config, project)
            for mv in moves:
                call_with_accepted(
                    w.place,
                    mv["ref"],
                    mv["at_mm"],
                    rotation=mv["rotation"],
                    side=mv["side"],
                    locked=mv["locked"],
                )
            _saved(m, project, w.save())
            return moves

        def via_ipc() -> Any:
            with ipc_batch(ctx, install, config, project, project.board, save="ipc_pcb_save") as call:
                for mv in moves:
                    call(
                        "ipc_pcb_place",
                        ref=mv["ref"],
                        at_mm=mv["at_mm"],
                        rotation=mv["rotation"],
                        side=mv["side"],
                        locked=mv["locked"],
                    )
            return moves

        def current_rotation(ref: str) -> float | None:
            rot = (fps.get(ref) or {}).get("rotation_deg")
            return float(rot) if isinstance(rot, (int, float)) else None

        def changes_rotation(mv: dict[str, Any]) -> bool:
            if mv["rotation"] is None:
                return False
            now = current_rotation(mv["ref"])
            return now is None or abs(float(mv["rotation"]) - now) > 1e-6

        rotates = any(changes_rotation(mv) for mv in moves)
        outcome = run_write(
            "board.place",
            caps,
            install,
            ipc=via_ipc,
            file=via_file,
            prefer=prefer,
            skip_ipc_reason=g16_guard(caps, rotates=rotates),
        )
        m.receipt.changed["updated"].extend(refs)
        apply_outcome(m, outcome, install, ctx)
        rehash_board_stages(project, m.state, "synced")
        after = placement_summary(ctx, install, caps, config, project, refs=refs)
        for name in ("courtyard_overlap", "off_board_copper", "off_board", "unrouted"):
            note_delta(name, before, after, m)
        m.receipt.degraded.extend(d for d in after["degraded"] if d not in m.receipt.degraded)
        ok = placement_ok(after)
        settle_placed(project, m.state, ok)
        m.receipt.data.update(
            {
                "ok": ok is not False,
                "placed": [
                    {
                        "ref": mv["ref"],
                        "at_mm": list(mv["at_mm"]),
                        "rotation": mv["rotation"],
                        "side": mv["side"],
                    }
                    for mv in moves
                ],
                "before": {
                    k: before[k] for k in ("courtyard_overlap", "off_board", "off_board_copper", "unrouted")
                },
                "after": {
                    k: after[k] for k in ("courtyard_overlap", "off_board", "off_board_copper", "unrouted")
                },
                "items": after["items"],
                "providers": after["providers"],
                "footprints_now": [
                    {k: d.get(k) for k in ("ref", "x_mm", "y_mm", "rotation_deg", "layer", "bbox_mm")}
                    for r, d in footprints_by_ref(board_model(ctx, project)).items()
                    if r in set(refs)
                ],
            }
        )
        m.receipt.data["drc"] = (
            drc_validator(ctx, m, install, caps, config, project, refill_if_zones=True) if args.drc else None
        )
        m.summary = f"{len(moves)} footprint(s); overlaps={after['courtyard_overlap']} off_board={after['off_board']} unrouted={after['unrouted']}"
        m.receipt.next_suggested = "kw_pcb_check" if ok is not True else "kw_pcb_zones"
        return m.receipt


PCB_PLACE = ToolSpec(
    name="kw_pcb_place",
    title="Place footprints",
    description=(
        "Move footprints: placements[{ref, at_mm, rotation?, side?, locked?}] (at_mm relative to `relative_to` when "
        "given). The receipt carries the kw_pcb_check numbers before -> after (courtyard overlaps, off-board, "
        "unrouted estimate) and the offending items for the moved refs; no copper off-board and no overlap -> "
        "stage placed. drc=true adds a DRC summary."
    ),
    input_model=PcbPlaceArgs,
    handler=pcb_place,
    annotations=MUTATING,
)


# ---- kw_pcb_zones ------------------------------------------------------------------------------------------------


class ZoneSpec(BaseModel):
    net: str = Field(description="net name (GND) or '' for a keepout / unconnected zone")
    layers: list[str] | str = Field(description="copper layer(s): 'F.Cu', ['F.Cu', 'B.Cu']")
    polygon_mm: list[list[float]] | None = Field(
        default=None, description="[[x, y], ...] outline (3+ points)"
    )
    rect_mm: list[float] | None = Field(default=None, description="[x1, y1, x2, y2] instead of polygon_mm")
    priority: int = Field(default=0, ge=0)
    name: str | None = None
    clearance_mm: float | None = None
    min_thickness_mm: float | None = None
    thermal: bool | None = Field(default=None, description="thermal reliefs on pads (default KiCad's)")
    keepout: bool = Field(default=False, description="a rule area (keepout) instead of a copper fill")


class PcbZonesArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    zones: list[ZoneSpec] = Field(min_length=1)
    fill: bool = Field(
        default=True, description="fill the zones after adding them (needs cli.drc_refill_flags or ipc11)"
    )
    drc: bool = Field(default=True, description="run DRC after the write")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def zone_polygon(z: ZoneSpec) -> list[tuple[float, float]]:
    if z.polygon_mm:
        pts = [_pt(p, "zones[].polygon_mm[]") for p in z.polygon_mm]
        if len(pts) < 3:
            raise KiwrapError(
                "E_BAD_ARGS", "a zone polygon needs at least 3 points", fix="pass 3+ [x, y] points or rect_mm"
            )
        return pts
    if z.rect_mm and len(z.rect_mm) == 4:
        x1, y1, x2, y2 = (float(v) for v in z.rect_mm)
        return [
            (min(x1, x2), min(y1, y2)),
            (max(x1, x2), min(y1, y2)),
            (max(x1, x2), max(y1, y2)),
            (min(x1, x2), max(y1, y2)),
        ]
    raise KiwrapError(
        "E_BAD_ARGS",
        "a zone needs polygon_mm (3+ points) or rect_mm [x1, y1, x2, y2]",
        example='zones=[{"net": "GND", "layers": ["F.Cu", "B.Cu"], "rect_mm": [0, 0, 50, 30]}]',
    )


def pcb_zones(ctx: ToolContext, args: PcbZonesArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_pcb_zones", gate=False) as m:
        _need_board(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        model = board_model(ctx, project)
        copper = list(board_rules(model).get("copper_layers") or [])
        nets = board_nets(model)
        specs: list[dict[str, Any]] = []
        for i, z in enumerate(args.zones):
            if z.net:
                check_net(z.net, nets, f"zones[{i}]")
            layers = [z.layers] if isinstance(z.layers, str) else list(z.layers)
            for layer in layers:
                _copper_layer(layer, copper)
            extra: dict[str, Any] = {}
            if z.name:
                extra["name"] = z.name
            if z.clearance_mm is not None:
                extra["clearance_mm"] = z.clearance_mm
            if z.min_thickness_mm is not None:
                extra["min_thickness_mm"] = z.min_thickness_mm
            if z.thermal is not None:
                extra["pad_connection"] = "thermal" if z.thermal else "solid"
            if z.keepout:
                extra["keepout"] = {"tracks": True, "vias": True, "copperpour": True}
            specs.append(
                {
                    "net": z.net,
                    "layers": layers,
                    "polygon_mm": zone_polygon(z),
                    "priority": z.priority,
                    **extra,
                }
            )

        def via_file() -> Any:
            w = board_writer(ctx, install, caps, config, project)
            ids = [str(call_with_accepted(w.add_zone, **spec)) for spec in specs]
            _saved(m, project, w.save())
            return {"uuids": ids, "filled": False}

        def via_ipc() -> Any:
            with ipc_batch(ctx, install, config, project, project.board, save="ipc_pcb_save") as call:
                ids = [str(call("ipc_pcb_add_zone", refill=False, **spec)) for spec in specs]
                filled = False
                if args.fill:
                    call("ipc_pcb_refill")
                    filled = True
            return {"uuids": ids, "filled": filled}

        outcome = run_write("board.zones", caps, install, ipc=via_ipc, file=via_file, prefer=prefer)
        result = outcome.value
        m.receipt.changed["added"].extend(result["uuids"])
        apply_outcome(m, outcome, install, ctx)
        rehash_board_stages(project, m.state, "placed")
        drc = None
        if args.drc or (args.fill and not result["filled"]):
            drc = drc_validator(
                ctx,
                m,
                install,
                caps,
                config,
                project,
                refill=args.fill and not result["filled"],
                touched_nets=[s["net"] for s in specs],
            )
            if drc is not None and drc["refilled"]:
                result["filled"] = True
                # the refill wrote the board: re-hash so the fill is not taken for an external edit
                rehash_board_stages(project, m.state, "placed")
        settle_after_drc(project, m.state, drc if args.drc else None)
        m.receipt.data.update(
            {
                "ok": drc["ok"] if drc is not None else True,
                "zones": [
                    {
                        "uuid": u,
                        "net": s["net"],
                        "layers": s["layers"],
                        "priority": s["priority"],
                        "points": len(s["polygon_mm"]),
                    }
                    for u, s in zip(result["uuids"], specs, strict=False)
                ],
                "filled": result["filled"],
                "drc": drc,
            }
        )
        m.summary = f"{len(specs)} zone(s) nets={sorted({s['net'] for s in specs})} filled={result['filled']}"
        m.receipt.next_suggested = "kw_pcb_drc" if drc is None or not drc["ok"] else "kw_pcb_check"
        return m.receipt


PCB_ZONES = ToolSpec(
    name="kw_pcb_zones",
    title="Add copper zones",
    description=(
        "Add filled copper zones / keepouts: zones[{net, layers, polygon_mm | rect_mm, priority, name?, "
        "clearance_mm?, min_thickness_mm?, thermal?, keepout?}]; fill=true refills (kicad-cli --refill-zones on "
        "KiCad 10+, RefillZones over IPC). DRC runs after the write with the delta of the zones' nets."
    ),
    input_model=PcbZonesArgs,
    handler=pcb_zones,
    annotations=MUTATING,
)


# ---- kw_pcb_route ------------------------------------------------------------------------------------------------


class RouteOp(BaseModel):
    op: Literal["track", "via", "delete"]
    points_mm: list[list[float]] | None = Field(
        default=None, description="track: polyline [[x, y], ...] (2+ points)"
    )
    width_mm: float | None = Field(
        default=None, description="track: width (default the net's netclass / Default track width)"
    )
    layer: str | None = Field(default=None, description="track: copper layer (default F.Cu)")
    net: str | None = Field(default=None, description="track / via: net name")
    at_mm: list[float] | None = Field(default=None, description="via: [x, y]")
    size_mm: float | None = Field(default=None, description="via: diameter (default netclass / 0.8)")
    drill_mm: float | None = Field(default=None, description="via: drill (default netclass / 0.4)")
    layers: list[str] | None = Field(default=None, description="via: [from, to] (default [F.Cu, B.Cu])")
    uuid: str | None = Field(default=None, description="delete: the track / via / zone uuid")


class PcbRouteArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    ops: list[RouteOp] = Field(min_length=1)
    drc: bool = Field(default=True, description="run DRC after the write (delta of the touched nets)")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def _netclass_value(rules: dict[str, Any], net: str | None, key: str, default: float) -> float:
    classes = rules.get("netclasses") or {}
    for nc in classes.values():
        nets = nc.get("nets") or []
        if net and net in nets and isinstance(nc.get(key), (int, float)):
            return float(nc[key])
    dflt = classes.get("Default") or {}
    if isinstance(dflt.get(key), (int, float)):
        return float(dflt[key])
    return default


def route_plan(
    ops: list[RouteOp], rules: dict[str, Any], nets: list[str] | None = None
) -> list[dict[str, Any]]:
    copper = list(rules.get("copper_layers") or [])
    plan: list[dict[str, Any]] = []
    for i, op in enumerate(ops):
        if op.net and nets:
            check_net(op.net, nets, f"ops[{i}]")
        if op.op == "track":
            pts = [_pt(p, f"ops[{i}].points_mm[]") for p in (op.points_mm or [])]
            if len(pts) < 2:
                raise KiwrapError(
                    "E_BAD_ARGS",
                    f"ops[{i}]: a track needs 2+ points",
                    fix="pass points_mm=[[x1, y1], [x2, y2], ...]",
                )
            width = (
                float(op.width_mm)
                if op.width_mm
                else _netclass_value(rules, op.net, "track_width_mm", DEFAULT_TRACK_WIDTH_MM)
            )
            plan.append(
                {
                    "op": "track",
                    "points_mm": pts,
                    "width_mm": width,
                    "layer": _copper_layer(op.layer or "F.Cu", copper),
                    "net": op.net or "",
                }
            )
        elif op.op == "via":
            at = _pt(op.at_mm, f"ops[{i}].at_mm")
            layers = list(op.layers or ["F.Cu", "B.Cu"])
            if len(layers) != 2:
                raise KiwrapError(
                    "E_BAD_ARGS",
                    f"ops[{i}]: via layers must be [from, to]",
                    fix="pass layers=['F.Cu', 'B.Cu']",
                )
            for layer in layers:
                _copper_layer(layer, copper)
            plan.append(
                {
                    "op": "via",
                    "at_mm": at,
                    "net": op.net or "",
                    "size_mm": float(op.size_mm)
                    if op.size_mm
                    else _netclass_value(rules, op.net, "via_size_mm", DEFAULT_VIA_SIZE_MM),
                    "drill_mm": float(op.drill_mm)
                    if op.drill_mm
                    else _netclass_value(rules, op.net, "via_drill_mm", DEFAULT_VIA_DRILL_MM),
                    "layers": layers,
                }
            )
        else:
            if not op.uuid:
                raise KiwrapError(
                    "E_BAD_ARGS", f"ops[{i}]: delete needs `uuid`", fix="pass the uuid from kw_pcb_get"
                )
            plan.append({"op": "delete", "uuid": op.uuid, "net": op.net or ""})
    return plan


def pcb_route(ctx: ToolContext, args: PcbRouteArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_pcb_route", gate=False) as m:
        _need_board(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        model = board_model(ctx, project)
        plan = route_plan(args.ops, board_rules(model), board_nets(model))
        touched = sorted({p["net"] for p in plan if p["net"]})

        def run_plan(track: Any, via: Any, delete: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for p in plan:
                if p["op"] == "track":
                    ids = [
                        str(u)
                        for u in (
                            track(pts=p["points_mm"], width_mm=p["width_mm"], layer=p["layer"], net=p["net"])
                            or []
                        )
                    ]
                    out.append(
                        {
                            "op": "track",
                            "uuids": ids,
                            "net": p["net"],
                            "layer": p["layer"],
                            "width_mm": p["width_mm"],
                            "segments": len(p["points_mm"]) - 1,
                        }
                    )
                elif p["op"] == "via":
                    uid = via(
                        at_mm=p["at_mm"],
                        size_mm=p["size_mm"],
                        drill_mm=p["drill_mm"],
                        net=p["net"],
                        layers=p["layers"],
                    )
                    out.append(
                        {
                            "op": "via",
                            "uuids": [str(uid)],
                            "net": p["net"],
                            "at_mm": list(p["at_mm"]),
                            "layers": p["layers"],
                        }
                    )
                else:
                    delete(p["uuid"])
                    out.append({"op": "delete", "uuids": [p["uuid"]], "net": p["net"] or None})
            return out

        def via_file() -> Any:
            w = board_writer(ctx, install, caps, config, project)
            out = run_plan(
                lambda **kw: call_with_accepted(w.add_track, **kw),
                lambda **kw: call_with_accepted(w.add_via, **kw),
                w.delete,
            )
            _saved(m, project, w.save())
            return out

        def via_ipc() -> Any:
            with ipc_batch(ctx, install, config, project, project.board, save="ipc_pcb_save") as call:
                return run_plan(
                    lambda **kw: call("ipc_pcb_add_track", **kw),
                    lambda **kw: call("ipc_pcb_add_via", **kw),
                    lambda uuid: call("ipc_pcb_delete", uuid=uuid),
                )

        outcome = run_write(
            "board.route_primitives", caps, install, ipc=via_ipc, file=via_file, prefer=prefer
        )
        applied = outcome.value
        for a in applied:
            key = "removed" if a["op"] == "delete" else "added"
            m.receipt.changed[key].extend(a["uuids"])
        apply_outcome(m, outcome, install, ctx)
        rehash_board_stages(project, m.state, "placed")
        drc = (
            drc_validator(ctx, m, install, caps, config, project, touched_nets=touched, refill_if_zones=True)
            if args.drc
            else None
        )
        settle_after_drc(project, m.state, drc)
        m.receipt.data.update(
            {
                "ok": drc["ok"] if drc is not None else True,
                "applied": applied,
                "touched_nets": touched,
                "drc": drc,
            }
        )
        m.summary = f"{len(applied)} op(s) nets={touched}"
        m.receipt.next_suggested = "kw_pcb_drc" if drc is None or not drc["ok"] else "kw_pcb_check"
        return m.receipt


PCB_ROUTE = ToolSpec(
    name="kw_pcb_route",
    title="Route primitives",
    description=(
        "Manual routing primitives: ops[{op: track, points_mm, width_mm?, layer?, net} | {op: via, at_mm, "
        "size_mm?, drill_mm?, net, layers?} | {op: delete, uuid}]. Widths / via sizes default to the net's "
        "netclass; layers are checked to be copper. DRC runs after the write with `drc_touched_nets` (must_fix "
        "violations on the touched nets, before -> after); must_fix == 0 -> stage routed + drc_clean."
    ),
    input_model=PcbRouteArgs,
    handler=pcb_route,
    annotations=DESTRUCTIVE,
)


PCB_AUTHOR_TOOLS: tuple[ToolSpec, ...] = (PCB_SETUP, PCB_SYNC, PCB_PLACE, PCB_ZONES, PCB_ROUTE)

__all__ = [
    "PCB_AUTHOR_TOOLS",
    "PCB_PLACE",
    "PCB_ROUTE",
    "PCB_SETUP",
    "PCB_SYNC",
    "PCB_ZONES",
]
