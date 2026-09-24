"""kw_pcb_check: placement/routing sanity as ACTIONABLE items with refs and mm coordinates -
courtyard overlaps, footprints outside the outline, unrouted connections (ratsnest) and the last
kw_pcb_drc summary from the project history. The tool never gates a stage (kw_pcb_drc does); it
is the cheap, structured "look" after every placement or routing edit (docs/DECISIONS.md D17).

Providers (kiwrap.core.planner): `overlaps` and off-board are `file` only (courtyard polygons /
bounding boxes from the lossless CST); `connectivity` is ipc11 -> cli -> file. ipc11 answers
with KiCad's own airwires when the server handles `GetRatsnest` (kiwrap patch ratsnest-export,
docs/UPSTREAM.md G1; nothing degraded, `provider_used` = `ipc11:<v>+ratsnest-export`), else
from KiCad's connectivity clusters (`kiwrap.backends.ipc.queries.ratsnest`: the unrouted count
is KiCad's, the pad pairs are nearest-anchor guesses -> `airwire_endpoints_estimated`); `cli` is
`kicad-cli pcb export ratsnest` (same patch, `cli.has_ratsnest_export`; exact end
items with uuid, anchor position and copper layer, pair-for-pair equal to DRC's
unconnected_items; `degraded` empty, `provider_used` = `cli:<v>+ratsnest-export`), attempted
before the ipc11 cluster walk whenever the export is available; the file provider is kiwrap's
estimate (`ratsnest_estimated`; measured 2026-09-19 against `kicad-cli pcb drc` unconnected_items
on demo boards with 30 % of their tracks deleted: 15/17 boards agree per net on the nightly and
14/17 on 10.0.6, the rest being boards whose DRC count hits KiCad's 499-item cap - the DRC
oracle only validates counts below `cli.drc_extended_error_limit`; see
tests/test_ratsnest_drc_live.py). Stock KiCad has no ratsnest export (D17 tier 3); once the
patch lands upstream the stock nightly gets the exact providers by presence probe.

Items (`data.items`, chunked): `{check, severity, refs[], net, pos{x_mm,y_mm}, bbox_mm,
description, fix}` with check in courtyard_overlap | off_board | unrouted; an unrouted item's
`a` / `b` end points carry `uuid` and `layer` when KiCad computed them. off_board is graded by
WHAT is outside: pad copper outside the outline is `must_fix` (KiCad DRC `copper_edge_clearance`
fails), a courtyard-only overhang is `review` (KiCad has no such rule; the ecc83 demo's P4 and
the jetson baseboard's ten edge connectors overhang by design and pass DRC) and carries
`courtyard_bbox_mm` + `outside_by_mm`. `data.ok` = no must_fix off-board footprint AND no
courtyard overlap (unrouted connections are expected before routing and reported as counts).
`data.summary` / `ok` are computed over EVERY item before `refs` / `nets` / `bbox_mm` are
applied (`nets` only applies to unrouted items); `summary.matched` and `total` count what the
filters kept. `nets` filters unrouted connections only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.state.stages import sha256_file
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec, effective_target, read_state
from kiwrap.core.tools._data import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Filters,
    chunk_by_budget,
    count_by,
    external_tags,
    matches,
    mm,
    mm_box,
    mm_pair,
    pick,
    rec,
    ref_key,
)
from kiwrap.core.tools.pcb_get import ProviderArg, _FileCache, read_connectivity

Check = Literal["overlaps", "off_board", "unrouted", "drc_summary"]
ALL_CHECKS: tuple[str, ...] = ("overlaps", "off_board", "unrouted", "drc_summary")


def norm_overlap(o: Any) -> dict[str, Any]:
    r = rec(o)
    raw_refs = pick(r, "refs")
    if raw_refs is None:
        raw_refs = [pick(r, "a", "ref_a"), pick(r, "b", "ref_b")]
    refs: list[str] = []
    for x in raw_refs:
        if x is not None:
            refs.append(x if isinstance(x, str) else str(pick(rec(x), "ref", default=x)))
    refs.sort(key=ref_key)
    box = mm_box(pick(r, "bbox_mm", "overlap_bbox_mm", "bbox"))
    pos = None
    if box:
        pos = {"x_mm": round((box[0] + box[2]) / 2, 4), "y_mm": round((box[1] + box[3]) / 2, 4)}
    area = mm(pick(r, "area_mm2", "overlap_area_mm2", "area"))
    return {
        "check": "courtyard_overlap",
        "severity": "review",
        "refs": refs,
        "net": None,
        "layer": pick(r, "layer", "side"),
        "pos": pos,
        "bbox_mm": box,
        "area_mm2": area,
        "description": f"courtyards of {' and '.join(refs)} overlap" + (f" by {area} mm2" if area else ""),
        "fix": f"move one of {', '.join(refs)} so the courtyards no longer intersect",
    }


def norm_off_board(o: Any) -> dict[str, Any]:
    r = rec(o)
    ref = str(pick(r, "ref", "reference", default=""))
    box = mm_box(pick(r, "bbox_mm", "bbox"))
    pos = mm_pair(pick(r, "position", "at"))
    x, y = (mm(pick(r, "x_mm", "x")), mm(pick(r, "y_mm", "y"))) if pos is None else (pos[0], pos[1])
    if (x is None or y is None) and box:
        x, y = round((box[0] + box[2]) / 2, 4), round((box[1] + box[3]) / 2, 4)
    by = mm(pick(r, "outside_by_mm", "outside_by", "distance_mm", "overhang_mm"))
    what = pick(r, "kind", default="")  # the file analysis: courtyard | pad (+ pad number in detail)
    detail = pick(r, "detail", default="")
    part = f" ({what}{' ' + str(detail) if detail else ''})" if what else ""
    reason = pick(r, "reason", default="outside the board outline" if not by else f"outside by {by} mm")
    copper = what == "pad"
    return {
        "check": "off_board",
        "severity": "must_fix" if copper else "review",
        "what": what or None,
        "pads": [str(detail)] if copper and detail else [],
        "refs": [ref] if ref else [],
        "net": None,
        "layer": pick(r, "layer"),
        "pos": {"x_mm": x, "y_mm": y} if x is not None and y is not None else None,
        "bbox_mm": box,
        "courtyard_bbox_mm": box if what == "courtyard" else None,
        "outside_by_mm": by,
        "description": f"{ref}{part} is {reason}",
        "fix": (
            f"move {ref} inboard by at least {by or '?'} mm with kw_pcb_place: its copper is outside the "
            "outline and DRC copper_edge_clearance will fail (or enlarge the Edge.Cuts outline with "
            "kw_pcb_setup(outline=...))"
            if copper
            else f"review: only the courtyard of {ref} overhangs the outline (by {by or '?'} mm); KiCad DRC has "
            "no rule for it - edge connectors and switches do this by design, otherwise move it inboard"
        ),
    }


def merge_off_board(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One off_board item per footprint: the pad items (copper outside) and the courtyard item
    of one reference fold into the stricter one, keeping the courtyard box and the pad numbers."""
    by_ref: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for it in items:
        ref = it["refs"][0] if it["refs"] else ""
        cur = by_ref.get(ref)
        if cur is None:
            by_ref[ref] = dict(it)
            order.append(ref)
            continue
        keep, other = (
            (it, cur) if it["severity"] == "must_fix" and cur["severity"] != "must_fix" else (cur, it)
        )
        merged = dict(keep)
        merged["pads"] = sorted(set(keep.get("pads", [])) | set(other.get("pads", [])), key=ref_key)
        merged["courtyard_bbox_mm"] = keep.get("courtyard_bbox_mm") or other.get("courtyard_bbox_mm")
        merged["outside_by_mm"] = max(keep.get("outside_by_mm") or 0.0, other.get("outside_by_mm") or 0.0)
        if merged["severity"] == "must_fix" and merged["pads"]:
            merged["description"] = (
                f"{ref} pad{'s' if len(merged['pads']) > 1 else ''} {', '.join(merged['pads'])} outside the "
                f"board outline by {merged['outside_by_mm']} mm"
                + (" (courtyard overhangs too)" if merged["courtyard_bbox_mm"] else "")
            )
        by_ref[ref] = merged
    return [by_ref[r] for r in order]


def _end_label(e: dict[str, Any]) -> str:
    """`R1.2` for a pad, `via`/`track`/`zone` (+ the layer when known) for other items."""
    if e.get("pad"):
        return f"{e.get('ref') or '?'}.{e['pad']}"
    label = str(e.get("ref") or "item")
    return f"{label} on {e['layer']}" if e.get("layer") else label


def norm_unrouted_item(u: dict[str, Any]) -> dict[str, Any]:
    a, b = u["a"], u["b"]
    refs = sorted({r for r in (a["ref"], b["ref"]) if r}, key=ref_key)
    pos = None
    if None not in (a["x_mm"], a["y_mm"]):
        pos = {"x_mm": a["x_mm"], "y_mm": a["y_mm"]}
    return {
        "check": "unrouted",
        "severity": "info",
        "refs": refs,
        "net": u["net"],
        "layer": a.get("layer") if a.get("layer") == b.get("layer") else None,
        "pos": pos,
        "bbox_mm": None,
        "a": a,
        "b": b,
        "length_mm": u["length_mm"],
        "description": f"net {u['net']}: {_end_label(a)} -> {_end_label(b)} ({u['length_mm']} mm)",
        "fix": (
            f"route it with kw_pcb_route: a track op from a ({_end_label(a)}) to b ({_end_label(b)}) on net "
            f"{u['net']!r} (points a/b carry the pad coordinates in mm), or accept it until routing"
        ),
    }


def last_drc(project: Project, state: ProjectState) -> dict[str, Any] | None:
    """The last kw_pcb_drc result in the project history (validators.drc + detail), with `fresh`
    = whether the board file still hashes like the last drc_clean stage (None when unknown)."""
    for entry in reversed(state.history):
        if entry.get("tool") != "kw_pcb_drc" or "drc" not in entry.get("validators", {}):
            continue
        fresh: bool | None = None
        stored = state.hashes.get("drc_clean", {})
        rel = project.relpath(project.board)
        if rel in stored and project.board.exists():
            fresh = sha256_file(project.board) == stored[rel]
        elif entry.get("stage") == "drc_clean":
            fresh = False  # the stage hash was invalidated (external edit) since that DRC passed
        if fresh is None and entry.get("board_sha256") and project.board.exists():
            fresh = sha256_file(project.board) == entry["board_sha256"]  # recorded by kw_pcb_drc
        return {
            "must_fix": int(entry["validators"]["drc"]),
            "at": entry.get("at"),
            "snapshot": entry.get("snapshot"),
            "fresh": fresh,
            "capped": list(entry.get("drc_capped", [])),
        }
    return None


class PcbCheckArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    checks: list[Check] = Field(
        default=["overlaps", "off_board", "unrouted", "drc_summary"],
        description="overlaps (courtyards), off_board (outside the outline), unrouted (ratsnest), drc_summary (last kw_pcb_drc)",
    )
    refs: list[str] | None = Field(default=None, description="only items involving these refs")
    nets: list[str] | None = Field(
        default=None, description="only unrouted connections on these nets (overlaps / off_board have no net)"
    )
    bbox_mm: list[float] | None = Field(
        default=None,
        description="[x1, y1, x2, y2] in mm: items whose box / position / airwire extent intersects it",
    )
    cursor: str | None = Field(default=None, description="continue a chunked listing")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="items per page")
    provider: ProviderArg = Field(
        default="auto",
        description="connectivity provider: auto | ipc11 | cli (kicad-cli pcb export ratsnest, patched KiCad) | file",
    )


class PcbCheckData(BaseModel):
    ok: bool
    board: str
    checks: list[str]
    filters: dict[str, Any]
    summary: dict[str, Any] = Field(
        description=(
            "whole-board counts (before filters): courtyard_overlap, off_board, off_board_copper (must_fix), "
            "unrouted, nets_with_unrouted; matched = items kept by the filters"
        )
    )
    drc_summary: dict[str, Any] | None = None
    total: int
    returned: int
    next_cursor: str | None = None
    items: list[dict[str, Any]]
    providers: dict[str, str]
    skipped: list[dict[str, str]] = Field(
        default_factory=list, description="checks that could not run and why"
    )


class PcbCheckOut(ReceiptOut):
    data: PcbCheckData  # type: ignore[assignment]


def pcb_check(ctx: ToolContext, args: PcbCheckArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, external = read_state(project)
    if not project.board.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND", f"board {project.board.name} is missing", fix="kw_project create writes it"
        )
    install = effective_target(ctx, project, state, for_write=False, allow_newer=True).install
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    filters = Filters.from_args(args)
    prefer = None if args.provider == "auto" else args.provider
    files = _FileCache(ctx, project.board)
    board: Path = project.board

    items: list[dict[str, Any]] = []
    degraded: list[str] = external_tags(external)
    warnings: list[str] = []
    providers: dict[str, str] = {}
    skipped: list[dict[str, str]] = []

    def file_check(name: str, fn: Any, norm: Any) -> None:
        try:
            out = planner.run(name, caps, {planner.FILE: lambda: fn(files.get())}, install=install)
        except KiwrapError as err:
            if err.code != "E_UNSUPPORTED":
                raise
            skipped.append({"check": name, "reason": f"{err.code}: {err.message}"})
            degraded.append(f"{name}_unavailable")
            return
        providers[name] = out.provider_used(install)
        items.extend(norm(o) for o in (out.value or []))

    if "overlaps" in args.checks:
        file_check("overlaps", ctx.services.courtyard_overlaps, norm_overlap)
    if "off_board" in args.checks:
        try:
            raw = ctx.services.off_board(files.get())
            providers["off_board"] = planner.provider_label(planner.FILE, install)
            items.extend(merge_off_board([norm_off_board(o) for o in (raw or [])]))
        except KiwrapError as err:
            if err.code != "E_UNSUPPORTED":
                raise
            skipped.append({"check": "off_board", "reason": f"{err.code}: {err.message}"})
            degraded.append("off_board_unavailable")
    if "unrouted" in args.checks:
        try:
            conn = read_connectivity(
                ctx, install, caps, config, board, ipc_model=None, files=files, prefer=prefer
            )
        except KiwrapError as err:
            if err.code != "E_UNSUPPORTED":
                raise
            skipped.append({"check": "unrouted", "reason": f"{err.code}: {err.message}"})
            degraded.append("connectivity_unavailable")
        else:
            providers["unrouted"] = conn.provider_used(install)
            items.extend(norm_unrouted_item(u) for u in conn.value)
            degraded.extend(d for d in conn.degraded if d not in degraded)
            warnings.extend(conn.warnings)

    order = {"off_board": 0, "courtyard_overlap": 1, "unrouted": 2}
    items.sort(key=lambda i: (order[i["check"]], [ref_key(r) for r in i["refs"]], i.get("net") or ""))
    # summary / ok describe the WHOLE board; the filters only choose which items are listed
    counts = count_by(items, "check")
    must_fix_off_board = sum(1 for i in items if i["check"] == "off_board" and i["severity"] == "must_fix")
    summary = {
        "courtyard_overlap": counts.get("courtyard_overlap", 0),
        "off_board": counts.get("off_board", 0),
        "off_board_copper": must_fix_off_board,
        "unrouted": counts.get("unrouted", 0),
        "nets_with_unrouted": sorted({i["net"] for i in items if i["check"] == "unrouted" and i["net"]}),
    }
    ok = must_fix_off_board == 0 and summary["courtyard_overlap"] == 0
    items = [it for it in items if matches(it, filters)]
    summary["matched"] = len(items)
    paged = chunk_by_budget(items, args.cursor, args.limit)
    page, nxt = paged.items, paged.next_cursor
    if paged.warning(args.limit):
        warnings.append(paged.warning(args.limit) or "")
    drc = last_drc(project, state) if "drc_summary" in args.checks else None
    if "drc_summary" in args.checks and drc is None:
        warnings.append("no kw_pcb_drc result in the project history yet")
    data: dict[str, Any] = {
        "ok": ok,
        "board": board.name,
        "checks": list(args.checks),
        "filters": filters.to_dict(),
        "summary": summary,
        "drc_summary": drc,
        "total": len(items),
        "returned": len(page),
        "next_cursor": nxt,
        "items": page,
        "providers": providers,
        "skipped": skipped,
    }
    used = sorted(set(providers.values()))
    if not used and "drc_summary" in args.checks:
        used = ["history"]  # only the project history was consulted, no provider ran
    next_suggested: str | None = None
    if ok:
        drc_clean = drc is not None and drc["fresh"] is True and drc["must_fix"] == 0 and not drc["capped"]
        next_suggested = "kw_fab_export" if drc_clean else "kw_pcb_drc"
    return Receipt(
        provider_used=used[0] if len(used) == 1 else ",".join(used),
        degraded=degraded,
        warnings=warnings,
        stage=state.stage,
        next_suggested=next_suggested,
        data=data,
    )


SPEC = ToolSpec(
    name="kw_pcb_check",
    title="Board check",
    description=(
        "Placement/routing feedback as actionable items (refs + mm): courtyard overlaps, footprints off the "
        "board outline (copper outside = must_fix, courtyard-only overhang = review), unrouted connections "
        "(ratsnest: KiCad's own airwires with end items and layers on a KiCad that exports them, else "
        "estimated and tagged in degraded) and the last kw_pcb_drc summary. ok = no copper off-board "
        "and no overlaps, computed before filters. Cheap; call after every placement change, before kw_pcb_drc."
    ),
    input_model=PcbCheckArgs,
    output_model=PcbCheckOut,
    handler=pcb_check,
    annotations=READ_ONLY,
)
