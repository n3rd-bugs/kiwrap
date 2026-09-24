"""kw_sch_check and kw_pcb_drc: KiCad is the judge.

Both run kicad-cli through `kiwrap.backends.cli` (which already applies the verified quirks:
ERC JSON positions x`cli.erc_json_pos_scale`, docs/VERIFICATION.md claim 1) and turn the
normalised `Report` into JSON with millimetre positions. `ok` = zero errors; warnings are listed
and never gate (docs/ARCHITECTURE.md §11).

DRC classification (§11): must_fix = clearance, shorting_items, tracks_crossing,
unconnected_items, hole_clearance, net_conflict, track_width; review = courtyards_overlap,
solder_mask_bridge, lib_footprint_issues; cosmetic = silk_*. The tables live in the capability
record per version (`rules.drc_must_fix` / `rules.drc_review` / `rules.drc_cosmetic_prefix`,
seeded from kiwrap/data/kicad_version_matrix.json); the constants below are the §11 defaults used
when a record lacks them. Any other
error-severity violation is must_fix (an error KiCad refuses to plot is not something to ship),
any other warning is review.

Which KiCad runs the check: `_base.effective_target` (docs/DECISIONS.md D21). A check is a read:
on a project whose format no installed KiCad writes, the nearest newer KiCad checks it in memory.
`kw_pcb_drc` does NOT save by default (D22, requests/kiwrap-feature-requests.review.md item 1
(a): the default refill-and-save re-stamped a 10.0 board as 10.99): zones are refilled in
kicad-cli's memory for the check (verified by the review: the board stays byte-identical, the
DRC result is the same). `save_board=true` writes the refill back, and only when that KiCad
writes the board's own format (else E_FORMAT_UPGRADE_REQUIRED / the D21 consent prompt).

The DRC result store (requests/kiwrap-feature-requests.review.md item 3, theme T2; D23): every
`kw_pcb_drc` run keeps its COMPLETE result - every violation incl. excluded ones, with class,
footprint refs and nets joined from the board file, KiCad's raw JSON - under the storage home
(`<storage>/results/<project>-<sha1>/drc/<board sha256>.json`), never in the project (the raw JSON
used to land as `<board>.drc.json` beside the board). `kw_pcb_drc(action='report', types, classes,
refs, nets, bbox_mm, severity, cursor, limit)` re-reads it without running KiCad or taking the
project lock; a board whose bytes changed since the run is E_STALE (so is a project never checked).

Also encoded here:
- item 19: `all_track_errors` -> `--all-track-errors` (default on where kicad-cli has the flag;
  without it each track reports its first violation only and the review saw a hidden short).
- item 23: `ignored_checks` - KiCad's own list on 10.0+ (project ignores PLUS KiCad's defaults, e.g.
  missing_courtyard / footprint_type_mismatch; §5 claim 6), attributed to `project` or
  `kicad_default` from the `.kicad_pro` `rule_severities`; on 8/9 only the project's ignores are
  known. An ignored check kiwrap treats as must_fix is a warning.
- item 20 (small defects): `class` is the first key of every violation; `violations_truncated`
  counts listed (non-excluded) violations only; `excluded` counts KiCad's exclusions.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt, readonly
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.stages import (
    STAGE_INPUTS,
    STAGES,
    downgrade_below,
    record_stage,
    sha256_file,
    stage_index,
)
from kiwrap.core.tools._base import (
    DESTRUCTIVE,
    MUTATING,
    ToolContext,
    ToolSpec,
    effective_target,
    mutation,
    saves_in_format,
    scratch_folder,
    to_kiwrap_error,
)
from kiwrap.core.tools._data import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Filters,
    Lane,
    boxes_intersect,
    count_by,
    page_lanes,
    project_results_dir,
)
from kiwrap.core.tools.intent import attach_intent_diff
from kiwrap.formats.reports.drc import (
    ignored_checks_from_json,
    ignored_rule_keys,
    included_severities_from_json,
    item_net,
    item_ref,
)

DRC_MUST_FIX = (
    "clearance",
    "shorting_items",
    "tracks_crossing",
    "unconnected_items",
    "hole_clearance",
    "net_conflict",
    "track_width",
)
DRC_REVIEW = ("courtyards_overlap", "solder_mask_bridge", "lib_footprint_issues")
DRC_COSMETIC_PREFIX = "silk_"

#: kw_sch_check writes state + a snapshot but never touches a design file
VALIDATOR_ANNOTATIONS = MUTATING
#: kw_pcb_drc rewrites the board when asked to (`save_board=true`: the refilled zones) - review
#: theme T8: a tool that CAN rewrite a design file carries destructive_hint
DRC_ANNOTATIONS = DESTRUCTIVE
MAX_VIOLATIONS = 500


def install_for(
    ctx: ToolContext,
    project: Project,
    state: ProjectState,
    *,
    for_write: bool = False,
    kinds: tuple[str, ...] | None = None,
) -> Any:
    """The install a check runs on (`effective_target`; a write only for `save_board=true`)."""
    return effective_target(ctx, project, state, for_write=for_write, kinds=kinds).install


def board_has_copper_zones(ctx: ToolContext, project: Project) -> bool:
    """True when the board has a copper zone (a net, not a rule area) - then a DRC is only
    meaningful against a fresh fill (reviewer finding, 2026-09-20). Unknown counts as False."""
    try:
        model = ctx.services.file_read_board(project.board)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED":
            return False
        raise
    for zone in getattr(model, "zones", None) or []:
        if getattr(zone, "is_rule_area", False) or getattr(zone, "keepout", False):
            continue
        net = getattr(zone, "net", None)
        name = net if isinstance(net, str) else getattr(net, "name", "")
        if name:
            return True
    return False


def involves_zone(violation: dict[str, Any]) -> bool:
    return any(str(i.get("description") or "").startswith("Zone") for i in violation.get("items", []))


def rehash_board_stages_upto(project: Project, state: ProjectState, keep: str) -> None:
    """Re-record the input hashes of every board-derived stage up to `keep` after kiwrap itself
    (here: kicad-cli's `--refill-zones --save-board`) rewrote the board, so `check_external_edit`
    keeps the stage. Same rule as `kiwrap.core.tools._author.rehash_board_stages`, which cannot
    be imported here (that module imports this one)."""
    for stage in STAGES[: stage_index(keep) + 1]:
        if stage in state.hashes and "pcb" in STAGE_INPUTS[stage]:
            record_stage(project, state, stage, advance=False)


def pos_dict(pos: Any) -> dict[str, float] | None:
    if pos is None:
        return None
    return {"x_mm": round(float(pos.x_mm), 4), "y_mm": round(float(pos.y_mm), 4)}


def violation_dict(v: Any) -> dict[str, Any]:
    return {
        "type": v.type,
        "severity": v.severity,
        "description": v.description,
        "sheet": getattr(v, "sheet", None),
        "excluded": bool(getattr(v, "excluded", False)),
        "pos": pos_dict(v.pos),
        "items": [{"uuid": i.uuid, "description": i.description, "pos": pos_dict(i.pos)} for i in v.items],
    }


def drc_type_limit(vtype: str, caps: Any) -> int:
    """KiCad's per-type reporting limit for a DRC violation type (capability keys
    `cli.drc_error_limit` / `cli.drc_extended_error_limit` / `cli.drc_extended_error_types`;
    DRC_ENGINE ERROR_LIMIT 199 / EXTENDED_ERROR_LIMIT 499 when the record has none)."""
    extended = _table(caps, "cli.drc_extended_error_types", ("clearance", "unconnected_items"))
    key = "cli.drc_extended_error_limit" if vtype in extended else "cli.drc_error_limit"
    default = 499 if vtype in extended else 199
    try:
        val = caps[key]
    except (KeyError, TypeError):
        return default
    return int(val) if isinstance(val, (int, float)) and val > 0 else default


def drc_capped_types(report: Any, caps: Any) -> list[str]:
    """Violation types whose count reached KiCad's reporting limit (a lower bound, not a total).
    Prefers what the backend recorded (`report.raw['_cli']['capped']`, computed by
    `kiwrap.backends.cli.drc.capped_types`); recomputed here for reports without it."""
    raw = getattr(report, "raw", None)
    if isinstance(raw, dict) and isinstance(raw.get("_cli"), dict) and "capped" in raw["_cli"]:
        return sorted(str(t) for t in raw["_cli"]["capped"])
    counts: dict[str, int] = {}
    for v in (
        *getattr(report, "violations", ()),
        *getattr(report, "unconnected", ()),
        *getattr(report, "schematic_parity", ()),
    ):
        counts[v.type] = counts.get(v.type, 0) + 1
    return sorted(t for t, n in counts.items() if n >= drc_type_limit(t, caps))


def _table(caps: Any, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    try:
        val = caps[key]
    except (KeyError, TypeError):
        return default
    return tuple(val) if isinstance(val, (list, tuple)) else default


def _text(caps: Any, key: str, default: str) -> str:
    try:
        val = caps[key]
    except (KeyError, TypeError):
        return default
    return val if isinstance(val, str) and val else default


def classify_drc(v: Any, *, must_fix: tuple[str, ...], review: tuple[str, ...], cosmetic_prefix: str) -> str:
    if v.type.startswith(cosmetic_prefix):
        return "cosmetic"
    if v.type in review:
        return "review"
    if v.severity == "error":
        return "must_fix"
    return "review" if v.type in must_fix or v.severity == "warning" else "cosmetic"


# ---- what to run after a failing validator ---------------------------------------------------------

#: ERC violation type -> the schematic tool that fixes it (first match in this order wins)
ERC_FIX_TOOLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("power_pin_not_driven",), "kw_sch_add_item"),  # a pwr_flag item on the undriven net
    (
        (
            "pin_not_connected",
            "unconnected_wire_endpoint",
            "wire_dangling",
            "label_dangling",
            "isolated_pin_label",
            "pin_not_driven",
        ),
        "kw_sch_connect",
    ),
    (("no_connect_connected", "no_connect_dangling"), "kw_sch_edit"),
    (
        (
            "unannotated",
            "duplicate_reference",
            "extra_units",
            "different_unit_footprint",
            "different_unit_net",
        ),
        "kw_sch_annotate",
    ),
    (
        ("lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues", "missing_footprint"),
        "kw_sch_assign_footprints",
    ),
)
#: DRC violation type -> the board tool that fixes it
DRC_FIX_TOOLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "schematic_parity",
            "footprint",
            "net_conflict",
            "missing_footprint",
            "extra_footprint",
            "footprint_type_mismatch",
        ),
        "kw_pcb_sync",
    ),
    (
        (
            "courtyards_overlap",
            "copper_edge_clearance",
            "footprints_overlap",
            "silk_overlap",
            "silk_over_copper",
        ),
        "kw_pcb_place",
    ),
    (
        (
            "unconnected_items",
            "clearance",
            "shorting_items",
            "tracks_crossing",
            "track_width",
            "via_dangling",
            "track_dangling",
            "hole_clearance",
            "via_hole_larger_than_pad",
            "drill_out_of_range",
            "via_diameter",
            "annular_width",
        ),
        "kw_pcb_route",
    ),
    (("starved_thermal", "isolated_copper", "zone_has_empty_net", "copper_sliver"), "kw_pcb_zones"),
)


def fix_tool_for(types: list[str], table: tuple[tuple[tuple[str, ...], str], ...], fallback: str) -> str:
    """The tool that fixes the first listed violation type (a failing validator must never suggest
    itself: an agent following `next_suggested` would loop on the check - reviewer finding,
    2026-09-20)."""
    seen = {str(t) for t in types}
    for names, tool in table:
        if seen & set(names):
            return tool
    return fallback


# ---- kw_sch_check ---------------------------------------------------------------------------------


#: The kicad-cli `--severity-*` filters (`backends.cli.erc.severity_flags`); a Literal so the enum
#: lands in the published inputSchema and a typo is E_BAD_ARGS instead of a backend ValueError.
Severity = Literal["all", "error", "warning", "exclusions"]


class SchCheckArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    severity: Severity = Field(
        default="all",
        description="ERC severity filter passed to kicad-cli (all = errors + warnings; exclusions = excluded markers)",
    )
    netlist: bool = Field(default=True, description="also export and summarise the netlist")


def netlist_summary(nl: Any) -> dict[str, Any]:
    comps = list(nl.components)
    nets = list(nl.nets)
    single = [n.name for n in nets if len(n.nodes) < 2]
    return {
        "source": nl.source,
        "kicad_version": nl.kicad_version,
        "components": len(comps),
        "nets": len(nets),
        "single_node_nets": single[:50],
        "missing_footprint": [c.ref for c in comps if not c.footprint][:100],
        "component_list": [
            {"ref": c.ref, "value": c.value, "footprint": c.footprint, "lib_id": c.lib_id}
            for c in comps[:200]
        ],
    }


def sch_check(ctx: ToolContext, args: SchCheckArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_sch_check", gate=False) as m:
        if not project.root_sch.exists():
            raise KiwrapError(
                "E_PROJECT_NOT_FOUND",
                f"root schematic {project.root_sch.name} is missing",
                fix="kw_project create writes it; if you renamed files, keep the .kicad_pro stem",
            )
        install = install_for(ctx, project, m.state)
        config = ctx.services.config_root(install)
        # the ERC JSON and the netlist go to a throw-away folder under the storage home: the
        # backends' default is beside the root sheet (`<root>.erc.json`, `<root>.net` - review §3
        # small defects / item 2: never leave reports in a project)
        with scratch_folder(ctx, "erc") as folder:
            report = ctx.services.run_erc(
                install,
                config,
                project.root_sch,
                severity=args.severity,
                units="mm",
                out_json=folder / f"{project.root_sch.stem}.erc.json",
            )
        violations = [violation_dict(v) for v in report.violations]
        errors, warnings = report.errors, report.warnings
        ok = report.ok
        data: dict[str, Any] = {
            "ok": ok,
            "kicad_version": report.kicad_version,
            "errors": errors,
            "warnings": warnings,
            "violations": violations[:MAX_VIOLATIONS],
            "violations_truncated": max(0, len(violations) - MAX_VIOLATIONS),
            "netlist": None,
            "intent_diff": None,
        }
        receipt_warnings: list[str] = []
        if args.netlist:
            try:
                with scratch_folder(ctx, "netlist") as folder:
                    nl = ctx.services.export_netlist(
                        install, config, project.root_sch, folder / f"{project.root_sch.stem}.net"
                    )
                data["netlist"] = netlist_summary(nl)
            except Exception as exc:  # noqa: BLE001 - ERC is the primary result; a missing netlist only degrades
                err = to_kiwrap_error(exc)
                if err.code not in ("E_UNSUPPORTED", "E_KICAD_FAILED"):
                    raise err from exc
                receipt_warnings.append(f"netlist export skipped: {err.code}: {err.message}")
                m.receipt.degraded.append("netlist_unavailable")
            else:
                # the intent diff (kiwrap.core.tools.intent): validators.intent_diff + data.intent_diff
                # when the project declared nets with kw_intent; nothing otherwise
                data["intent_diff"] = attach_intent_diff(project, m, nl)
        m.validator_delta("erc", errors, {"warnings": warnings})
        if ok:
            record_stage(project, m.state, "erc_clean")
        else:
            downgrade_below(m.state, "erc_clean")
        m.summary = f"{'ok' if ok else 'FAIL'} errors={errors} warnings={warnings}"
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.warnings.extend(receipt_warnings)
        m.receipt.data = data
        if not ok:
            error_types = [v["type"] for v in violations if v["severity"] == "error"]
            m.receipt.next_suggested = fix_tool_for(error_types, ERC_FIX_TOOLS, "kw_sch_connect")
        elif (data.get("intent_diff") or {}).get("count"):
            # ERC clean but the wiring differs from the declared nets: fix the wires before syncing
            m.receipt.next_suggested = "kw_sch_connect"
        return m.receipt


SCH_CHECK = ToolSpec(
    name="kw_sch_check",
    title="Schematic check",
    description=(
        "Run KiCad ERC on the root schematic (positions in mm) and export the netlist. ok means zero errors; "
        "warnings are listed but never gate. Passing moves the project to stage erc_clean."
    ),
    input_model=SchCheckArgs,
    handler=sch_check,
    annotations=VALIDATOR_ANNOTATIONS,
)


# ---- kw_pcb_drc -----------------------------------------------------------------------------------

DrcAction = Literal["run", "report"]
DrcClass = Literal["must_fix", "review", "cosmetic"]


class PcbDrcArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    action: DrcAction = Field(
        default="run",
        description=(
            "run = run KiCad DRC (and store the complete result under the kiwrap storage home); report = re-read "
            "the stored result of the last run WITHOUT running KiCad, filtered and paged (E_STALE when the board "
            "changed since, or when it was never checked)"
        ),
    )
    schematic_parity: bool = Field(
        default=False, description="run: also check footprint/net parity against the schematic"
    )
    refill_zones: bool | None = Field(
        default=None,
        description=(
            "run: refill copper zones before checking (needs the cli.drc_refill_flags capability; else "
            "E_UNSUPPORTED). Default (null): refill when the board has copper zones and this KiCad can - a DRC "
            "against a stale fill reports tracks added since the last fill as clearance errors against the pour"
        ),
    )
    save_board: bool = Field(
        default=False,
        description=(
            "run, with refill_zones: write the refilled zones back to the board. Default false (D22): the refill "
            "is in memory for the check and the board file is untouched; true saves only when this KiCad writes "
            "the board's own file format"
        ),
    )
    all_track_errors: bool | None = Field(
        default=None,
        description=(
            "run: report EVERY violation of each track (kicad-cli --all-track-errors). Default (null): on where "
            "this kicad-cli has the flag; without it KiCad stops at a track's first violation and can hide a short"
        ),
    )
    severity: Severity = Field(
        default="all",
        description=(
            "run: the kicad-cli severity filter (all = errors + warnings; exclusions = excluded markers). report: "
            "the same words filter the stored violations (all = every non-excluded one)"
        ),
    )
    types: list[str] | None = Field(
        default=None, description="report: only these violation types (clearance, unconnected_items, ...)"
    )
    classes: list[DrcClass] | None = Field(default=None, description="report: only these classes")
    refs: list[str] | None = Field(
        default=None, description="report: only violations involving these footprint references"
    )
    nets: list[str] | None = Field(default=None, description="report: only violations involving these nets")
    bbox_mm: list[float] | None = Field(
        default=None,
        description="report: [x1, y1, x2, y2] in mm; only violations whose items' positions touch the box",
    )
    cursor: str | None = Field(default=None, description="report: the previous data.next_cursor")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="report: violations per page")


#: the arguments that only mean something to one action (a warning names the ignored ones)
RUN_ONLY_ARGS: dict[str, Any] = {
    "schematic_parity": False,
    "refill_zones": None,
    "save_board": False,
    "all_track_errors": None,
}
REPORT_ONLY_ARGS: dict[str, Any] = {
    "types": None,
    "classes": None,
    "refs": None,
    "nets": None,
    "bbox_mm": None,
    "cursor": None,
}


def _ignored_args(args: PcbDrcArgs, defaults: dict[str, Any], action: str) -> str | None:
    used = [name for name, default in defaults.items() if getattr(args, name) != default]
    if not used:
        return None
    return f"{', '.join(used)} {'is' if len(used) == 1 else 'are'} ignored by action='{action}'"


def pcb_drc(ctx: ToolContext, args: PcbDrcArgs) -> Receipt:
    if args.action == "report":
        return drc_report(ctx, args)
    return drc_run(ctx, args)


# ---- the DRC result store (review item 3) ------------------------------------------------------------

DRC_RESULT_SCHEMA = "kiwrap.drc_result.v1"
#: stored results kept per project (the newest win; one per board content)
DRC_KEEP_RESULTS = 12
DRC_LATEST = "latest.json"


def drc_store_dir(ctx: ToolContext, project: Project) -> Path:
    """`<storage>/results/<project>-<sha1(dir)>/drc/` - outside the project (D23, review T2)."""
    return project_results_dir(ctx.services.storage_home, project.dir, project.name) / "drc"


def rules_sha256(project: Project) -> str:
    """sha256 over the `.kicad_pro` and `.kicad_dru` bytes: a stored result whose rules changed is
    served with a warning (the board decides freshness; kiwrap itself rewrites the `.kicad_pro`)."""
    import hashlib

    h = hashlib.sha256()
    for path in (project.pro_path, project.dir / f"{project.name}.kicad_dru"):
        with contextlib.suppress(OSError):
            h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes((json.dumps(doc, indent=1, default=str) + "\n").encode("utf-8"))
    tmp.replace(path)


def save_drc_result(ctx: ToolContext, project: Project, doc: dict[str, Any]) -> Path:
    """Store `doc` as `<board sha256>.json`, point `latest.json` at it and prune old results."""
    folder = drc_store_dir(ctx, project)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc['board_sha256']}.json"
    _write_json(path, doc)
    _write_json(
        folder / DRC_LATEST,
        {"board_sha256": doc["board_sha256"], "file": path.name, "run_at": doc.get("run_at")},
    )
    results = sorted(
        (p for p in folder.glob("*.json") if p.name != DRC_LATEST and not p.name.endswith(".drc.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in results[DRC_KEEP_RESULTS:]:
        for extra in (old, old.with_suffix(".drc.json")):
            with contextlib.suppress(OSError):
                extra.unlink()
    return path


def load_drc_result(ctx: ToolContext, project: Project, board_sha: str) -> dict[str, Any] | None:
    path = drc_store_dir(ctx, project) / f"{board_sha}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == DRC_RESULT_SCHEMA else None


def latest_drc_result(ctx: ToolContext, project: Project) -> dict[str, Any] | None:
    try:
        pointer = json.loads((drc_store_dir(ctx, project) / DRC_LATEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = pointer.get("board_sha256") if isinstance(pointer, dict) else None
    return load_drc_result(ctx, project, str(sha)) if sha else None


# ---- joins: violation items -> footprint refs / nets ------------------------------------------------


def board_item_index(model: Any) -> dict[str, tuple[str | None, str | None]]:
    """`{uuid: (footprint ref, net)}` over footprints, pads, tracks, vias and zones of a board model
    (`kw_pcb_get`'s normalisation), so each DRC item joins to what it belongs to."""
    from kiwrap.core.tools.pcb_get import board_items  # lazy: pcb_get is the heavier module

    index: dict[str, tuple[str | None, str | None]] = {}
    lists = board_items(model)
    for fp in lists["footprints"]:
        if fp.get("uuid"):
            index[str(fp["uuid"])] = (fp.get("ref"), None)
    for kind in ("pads", "tracks", "vias", "zones"):
        for it in lists[kind]:
            if it.get("uuid"):
                index[str(it["uuid"])] = (it.get("ref"), it.get("net"))
    return index


def enrich_violation(d: dict[str, Any], index: dict[str, tuple[str | None, str | None]]) -> None:
    """Add `refs`, `nets` and `bbox_mm` (the extent of the item positions) to a violation dict, and
    `ref` / `net` to its items: uuid join first, KiCad's (English) item description second."""
    refs: list[str] = []
    nets: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    for item in d.get("items", []):
        ref, net = index.get(str(item.get("uuid") or ""), (None, None))
        text = str(item.get("description") or "")
        ref = ref or item_ref(text)
        net = net or item_net(text)
        item["ref"], item["net"] = ref, net
        if ref and ref not in refs:
            refs.append(ref)
        if net and net not in nets:
            nets.append(net)
        pos = item.get("pos")
        if isinstance(pos, dict) and pos.get("x_mm") is not None and pos.get("y_mm") is not None:
            xs.append(float(pos["x_mm"]))
            ys.append(float(pos["y_mm"]))
    d["refs"], d["nets"] = refs, nets
    d["bbox_mm"] = [min(xs), min(ys), max(xs), max(ys)] if xs else None


# ---- ignored checks (review item 23) ------------------------------------------------------------------


def project_ignored_keys(project: Project) -> tuple[str, ...]:
    try:
        return ignored_rule_keys(json.loads(project.pro_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ()


def ignored_checks_of(report: Any, project: Project) -> tuple[list[dict[str, Any]], str]:
    """`(ignored_checks, source)`: KiCad's own list (10.0+, `source='kicad'`), each entry attributed
    to `project` (the `.kicad_pro` says ignore) or `kicad_default`; on 8/9 only the project's
    ignores (`source='project_only'`: KiCad's defaults are not reported by that kicad-cli)."""
    raw = getattr(report, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    own = project_ignored_keys(project)
    listed = ignored_checks_from_json(raw)
    if listed is None:
        return [{"key": k, "description": "", "source": "project"} for k in own], "project_only"
    out: list[dict[str, Any]] = []
    for c in listed:
        out.append(
            {
                "key": c.key,
                "description": c.description,
                "source": "project" if c.key in own else "kicad_default",
            }
        )
    keys = {c["key"] for c in out}
    out.extend({"key": k, "description": "", "source": "project"} for k in own if k not in keys)
    return out, "kicad"


# ---- action='run' ---------------------------------------------------------------------------------


def _supports_all_track_errors(caps: Any) -> bool:
    """`kiwrap.backends.cli.drc.supports_all_track_errors` without importing the backend here:
    `pcb drc` exists and its flag list (when the record has one) names `--all-track-errors`."""
    if not caps.has("cli.has_drc"):
        return False
    try:
        listed = caps.flags("pcb drc")
    except (AttributeError, KeyError, TypeError):
        return True
    return not listed or "--all-track-errors" in listed


def _read_board_model(ctx: ToolContext, project: Project) -> Any:
    """The file model of the board, or None when this build cannot read it (joins degrade)."""
    try:
        return ctx.services.file_read_board(project.board)
    except Exception:  # noqa: BLE001 - the model only enriches the result
        return None


def drc_run(ctx: ToolContext, args: PcbDrcArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_pcb_drc", gate=False) as m:
        if not project.board.exists():
            raise KiwrapError(
                "E_PROJECT_NOT_FOUND",
                f"board {project.board.name} is missing",
                fix="kw_project create writes it",
            )
        note = _ignored_args(args, REPORT_ONLY_ARGS, "run")
        if note:
            m.receipt.warnings.append(note + " (filters and paging belong to action='report')")
        # a save is the only way this tool writes a design file: then (and only then) the target
        # must write the board's own format (D21; the consent prompt / E_FORMAT_UPGRADE_REQUIRED)
        wants_save = bool(args.save_board) and args.refill_zones is not False
        install = install_for(ctx, project, m.state, for_write=wants_save, kinds=("pcb",))
        caps = ctx.services.caps(install)
        config = ctx.services.config_root(install)
        refill = args.refill_zones
        stale_zones = False
        if refill is None:
            zones = board_has_copper_zones(ctx, project)
            refill = zones and caps.has("cli.drc_refill_flags")
            if zones and not refill:
                stale_zones = True
                m.receipt.degraded.append("zones_stale")
                m.receipt.warnings.append(
                    "the board has copper zones but this KiCad cannot refill them headless (cli.drc_refill_flags): "
                    "the check runs against the fill on disk; track-vs-zone clearance items are listed as review"
                )
        save = wants_save and bool(refill)
        if save and not saves_in_format(install, caps, project, "pcb", m):
            # belt and braces behind effective_target: never let --save-board raise the stamp
            save = False
            m.receipt.degraded.append("refill_not_saved_epoch")
            m.receipt.warnings.append(
                f"the refilled zones were not saved: KiCad {install.version} would write the board in a newer file "
                "format (D21); the check used the refill in memory"
            )
        if bool(refill) and not save:
            m.receipt.warnings.append(
                "zones were refilled in memory for this check; the board on disk keeps its previous fill "
                "(save_board=true writes the refill back)"
            )
        # item 19: every violation of every track, where this kicad-cli can
        track_flag = _supports_all_track_errors(caps)
        all_tracks = track_flag if args.all_track_errors is None else bool(args.all_track_errors)
        if all_tracks and not track_flag:
            all_tracks = False
            m.receipt.degraded.append("all_track_errors_unsupported")
            m.receipt.warnings.append(
                f"all_track_errors ignored: kicad-cli {install.version} has no --all-track-errors; each track "
                "reports its first violation only"
            )
        store = drc_store_dir(ctx, project)
        store.mkdir(parents=True, exist_ok=True)
        raw_out = store / f"run-{uuid.uuid4().hex[:12]}.drc.json"
        board_sha_before = sha256_file(project.board)
        try:
            report = ctx.services.run_drc(
                install,
                config,
                project.board,
                schematic_parity=args.schematic_parity,
                refill_zones=bool(refill),
                save_board=save,
                severity=args.severity,
                all_track_errors=all_tracks,
                out_json=raw_out,  # the storage home, never `<board>.drc.json` in the project (D23)
            )
        except BaseException:
            raw_out.unlink(missing_ok=True)
            raise
        board_sha = sha256_file(project.board)
        if board_sha != board_sha_before:
            # kicad-cli rewrote the board (refill_zones + save_board): re-hash the board-derived
            # stages up to the current one so the next call does not take kiwrap's own save for
            # an external edit and drop the project back to erc_clean (kw_pcb_author does the
            # same after its refills; reviewer finding, 2026-09-20).
            rehash_board_stages_upto(project, m.state, m.state.stage)
            m.receipt.changed["updated"].append(project.board.name)
        must_fix_t = _table(caps, "rules.drc_must_fix", DRC_MUST_FIX)
        review_t = _table(caps, "rules.drc_review", DRC_REVIEW)
        cosmetic_prefix = _text(caps, "rules.drc_cosmetic_prefix", DRC_COSMETIC_PREFIX)
        all_v = [*report.violations, *report.unconnected, *report.schematic_parity]
        index: dict[str, tuple[str | None, str | None]] = {}
        if all_v:
            model = _read_board_model(ctx, project)
            if model is not None:
                try:
                    index = board_item_index(model)
                except Exception:  # noqa: BLE001 - descriptions still give refs / nets
                    index = {}
        buckets: dict[str, list[dict[str, Any]]] = {"must_fix": [], "review": [], "cosmetic": []}
        stored: list[dict[str, Any]] = []
        excluded = 0
        for v in all_v:
            cls = classify_drc(v, must_fix=must_fix_t, review=review_t, cosmetic_prefix=cosmetic_prefix)
            d: dict[str, Any] = {"class": cls, **violation_dict(v)}  # class first (review item 20)
            if stale_zones and cls == "must_fix" and v.type == "clearance" and involves_zone(d):
                d["class"] = "review"
                d["note"] = "against a stale zone fill; re-check with refill_zones=true on a KiCad 10+"
            enrich_violation(d, index)
            stored.append(d)
            if getattr(v, "excluded", False):
                excluded += 1
                continue
            listed = {k: val for k, val in d.items() if k not in ("nets", "bbox_mm")}
            listed["items"] = [
                {k: val for k, val in i.items() if k not in ("ref", "net")} for i in d.get("items", [])
            ]
            buckets[d["class"]].append(listed)
        must_fix = len(buckets["must_fix"])
        ok = must_fix == 0
        capped = drc_capped_types(report, caps)
        ignored, ignored_source = ignored_checks_of(report, project)
        included = included_severities_from_json(getattr(report, "raw", None) or {})
        listed_all = [*buckets["must_fix"], *buckets["review"], *buckets["cosmetic"]]
        options = {
            "schematic_parity": args.schematic_parity,
            "refill_zones": bool(refill),
            "save_board": save,
        }
        data: dict[str, Any] = {
            "ok": ok,
            "kicad_version": report.kicad_version,
            "errors": report.errors,
            "warnings": report.warnings,
            "must_fix": must_fix,
            "review": len(buckets["review"]),
            "cosmetic": len(buckets["cosmetic"]),
            "unconnected": len([v for v in report.unconnected if not getattr(v, "excluded", False)]),
            "parity_violations": len(report.schematic_parity),
            "excluded": excluded,
            "capped": capped,
            "violations": listed_all[:MAX_VIOLATIONS],
            # listed (non-excluded) violations beyond MAX_VIOLATIONS (review §3: excluded ones were counted)
            "violations_truncated": max(0, len(listed_all) - MAX_VIOLATIONS),
            "ignored_checks": ignored,
            "ignored_checks_source": ignored_source,
            "included_severities": list(included) if included is not None else None,
            "all_track_errors": all_tracks,
            "options": options,
        }
        ignored_must_fix = sorted({c["key"] for c in ignored} & set(must_fix_t))
        if ignored_must_fix:
            m.receipt.warnings.append(
                f"DRC skipped checks kiwrap treats as must_fix because they are set to ignore: "
                f"{', '.join(ignored_must_fix)} - their violations are NOT in this result (rule_severities in the "
                ".kicad_pro)"
            )
        if ignored_source == "project_only":
            m.receipt.warnings.append(
                f"kicad-cli {report.kicad_version or install.version} does not report which checks it ignored: "
                "ignored_checks lists the project's own rule_severities only, not KiCad's built-in defaults"
            )
        m.validator_delta(
            "drc",
            must_fix,
            {"review": data["review"], "cosmetic": data["cosmetic"], "unconnected": data["unconnected"]},
        )
        m.history_extra["board_sha256"] = board_sha  # kw_pcb_check: is this DRC fresh?
        if capped:
            m.history_extra["drc_capped"] = list(capped)
            m.receipt.degraded.append("drc_counts_capped")
            limits = ", ".join(f"{t} after {drc_type_limit(t, caps)}" for t in capped)
            m.receipt.warnings.append(
                f"KiCad stops reporting a violation type once its limit is reached ({limits}): the counts for "
                f"{', '.join(capped)} are LOWER BOUNDS. Fix the listed items and re-run kw_pcb_drc"
                + (
                    "; for the complete unrouted count use kw_pcb_check (its connectivity providers are uncapped)"
                    if "unconnected_items" in capped
                    else ""
                )
            )
        if ok:
            record_stage(project, m.state, "drc_clean")
        else:
            downgrade_below(m.state, "drc_clean")
        m.summary = f"{'ok' if ok else 'FAIL'} must_fix={must_fix} review={data['review']} cosmetic={data['cosmetic']}"
        m.receipt.provider_used = ctx.services.provider(install)
        data["result"] = _store_run(
            ctx,
            project,
            m.receipt,
            raw_out,
            board_sha,
            data,
            stored,
            {**options, "all_track_errors": all_tracks, "severity": args.severity},
            ignored,
            ignored_source,
            included,
        )
        m.receipt.data = data
        if not ok:
            must_types = [v["type"] for v in buckets["must_fix"]]
            if any(not getattr(x, "excluded", False) for x in report.schematic_parity):
                must_types = ["schematic_parity", *must_types]
            m.receipt.next_suggested = fix_tool_for(must_types, DRC_FIX_TOOLS, "kw_pcb_route")
        return m.receipt


SUMMARY_KEYS = (
    "ok",
    "errors",
    "warnings",
    "must_fix",
    "review",
    "cosmetic",
    "unconnected",
    "parity_violations",
    "excluded",
    "capped",
)


def _store_run(
    ctx: ToolContext,
    project: Project,
    receipt: Receipt,
    raw_out: Path,
    board_sha: str,
    data: dict[str, Any],
    stored: list[dict[str, Any]],
    options: dict[str, Any],
    ignored: list[dict[str, Any]],
    ignored_source: str,
    included: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Keep the complete result for `action='report'` (never fails the run: a store error is a warning)."""
    raw_path: Path | None = None
    if raw_out.is_file():
        raw_path = raw_out.with_name(f"{board_sha}.drc.json")
        raw_out.replace(raw_path)
    doc = {
        "schema": DRC_RESULT_SCHEMA,
        "project": str(project.dir),
        "board": project.board.name,
        "board_sha256": board_sha,
        "rules_sha256": rules_sha256(project),
        "run_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "kicad_version": data.get("kicad_version"),
        "provider_used": receipt.provider_used,
        "options": options,
        "summary": {k: data.get(k) for k in SUMMARY_KEYS},
        "ignored_checks": ignored,
        "ignored_checks_source": ignored_source,
        "included_severities": list(included) if included is not None else None,
        "violations": stored,
        "raw_json": str(raw_path) if raw_path else None,
    }
    try:
        path = save_drc_result(ctx, project, doc)
    except OSError as exc:
        receipt.warnings.append(f"the DRC result was not stored for action='report': {exc}")
        return {"stored": False, "board_sha256": board_sha}
    return {
        "stored": True,
        "board_sha256": board_sha,
        "path": str(path),
        "raw_json": doc["raw_json"],
        "violations": len(stored),
        "reread_with": "kw_pcb_drc(action='report', ...) - filters types/classes/refs/nets/bbox_mm/severity",
    }


# ---- action='report' ------------------------------------------------------------------------------


def _severity_matches(v: dict[str, Any], severity: str) -> bool:
    if severity == "exclusions":
        return bool(v.get("excluded"))
    if v.get("excluded"):
        return False
    return severity == "all" or v.get("severity") == severity


def drc_report(ctx: ToolContext, args: PcbDrcArgs) -> Receipt:
    """Review item 3: the last DRC result, filtered, without KiCad (no lock, no snapshot, no write)."""
    project = Project.from_path(args.project)
    if not project.board.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND", f"board {project.board.name} is missing", fix="kw_project create writes it"
        )
    filters = Filters.from_args(args)  # validates bbox_mm
    board_sha = sha256_file(project.board)
    doc = load_drc_result(ctx, project, board_sha)
    if doc is None:
        latest = latest_drc_result(ctx, project)
        run_fix = f"run kw_pcb_drc(project='{args.project}') (action='run') first, then action='report'"
        if latest is None:
            raise KiwrapError(
                "E_STALE",
                f"no stored DRC result for {project.board.name}",
                why="action='report' only re-reads the result a kw_pcb_drc run stored; this board was never checked "
                "by this kiwrap storage home",
                fix=run_fix,
                data={"reason": "no_result", "board_sha256": board_sha},
            )
        raise KiwrapError(
            "E_STALE",
            f"the stored DRC result is for an older {project.board.name} (checked {latest.get('run_at')})",
            why="the board's bytes changed since that run, so its violations may no longer exist or may be "
            "incomplete",
            fix=run_fix + " (it re-checks the current board)",
            data={
                "reason": "board_changed",
                "stored_board_sha256": latest.get("board_sha256"),
                "current_board_sha256": board_sha,
                "stored_at": latest.get("run_at"),
                "stored_kicad_version": latest.get("kicad_version"),
            },
        )
    warnings: list[str] = []
    degraded: list[str] = []
    note = _ignored_args(args, RUN_ONLY_ARGS, "report")
    if note:
        warnings.append(
            note
            + " (they shape a run; this result was stored with options "
            + json.dumps(doc["options"])
            + ")"
        )
    if doc.get("rules_sha256") and doc["rules_sha256"] != rules_sha256(project):
        degraded.append("drc_rules_changed")
        warnings.append(
            "the .kicad_pro / .kicad_dru changed since this DRC ran: severities or rules may differ now; re-run "
            "kw_pcb_drc to be sure"
        )
    types = set(args.types or ())
    classes = set(args.classes or ())
    refs = set(filters.refs)
    nets = set(filters.nets)
    matching: list[dict[str, Any]] = []
    for v in doc.get("violations", []):
        if not _severity_matches(v, args.severity):
            continue
        if types and v.get("type") not in types:
            continue
        if classes and v.get("class") not in classes:
            continue
        if refs and not refs & set(v.get("refs") or ()):
            continue
        if nets and not nets & set(v.get("nets") or ()):
            continue
        if filters.bbox_mm and not (v.get("bbox_mm") and boxes_intersect(v["bbox_mm"], filters.bbox_mm)):
            continue
        matching.append(v)
    order = {"must_fix": 0, "review": 1, "cosmetic": 2}
    matching.sort(key=lambda v: order.get(str(v.get("class")), 3))  # stable: KiCad's order within a class
    mp = page_lanes([Lane("v", matching)], args.cursor, args.limit, version=board_sha[:8])
    hint = mp.warning(args.limit, {"v": "violations"})
    if hint:
        warnings.append(hint)
    summary = dict(doc.get("summary") or {})
    data: dict[str, Any] = {
        "ok": bool(summary.get("ok", True)),
        "action": "report",
        "fresh": True,
        "run_at": doc.get("run_at"),
        "kicad_version": doc.get("kicad_version"),
        "board_sha256": board_sha,
        "options": doc.get("options"),
        "summary": summary,
        "filters": {
            **filters.to_dict(),
            "types": sorted(types),
            "classes": sorted(classes),
            "severity": args.severity,
        },
        "counts": {"class": count_by(matching, "class"), "type": count_by(matching, "type")},
        "total": len(matching),
        "returned": len(mp.pages["v"]),
        "next_cursor": mp.next_cursor,
        "violations": mp.pages["v"],
        "ignored_checks": doc.get("ignored_checks"),
        "ignored_checks_source": doc.get("ignored_checks_source"),
        "included_severities": doc.get("included_severities"),
        "raw_json": doc.get("raw_json"),
    }
    stage = None
    try:
        # a read-only session's stage lives in memory (D23), never in .kiwrap/state.json
        stage = (
            readonly.load_state(project) if readonly.current() is not None else project.load_state()
        ).stage
    except KiwrapError:
        stage = None
    return Receipt(
        provider_used=f"stored:{doc.get('provider_used') or 'drc'}",
        degraded=degraded,
        warnings=warnings,
        stage=stage,
        data=data,
    )


PCB_DRC = ToolSpec(
    name="kw_pcb_drc",
    title="Board DRC",
    description=(
        "action=run (default): run KiCad DRC on the board (optionally schematic parity; zones are refilled in "
        "memory when the board has copper zones - the board file is not written unless save_board=true; every "
        "track error reported where kicad-cli can). Violations are classified must_fix / review / cosmetic with mm "
        "positions and footprint refs; ok means must_fix == 0 and moves the project to drc_clean; ignored_checks "
        "says which checks KiCad skipped. The complete result is stored outside the project. action=report: "
        "re-read that stored result without running KiCad, filtered by types / classes / refs / nets / bbox_mm / "
        "severity and paged (E_STALE after a board change)."
    ),
    input_model=PcbDrcArgs,
    handler=pcb_drc,
    annotations=DRC_ANNOTATIONS,
)
