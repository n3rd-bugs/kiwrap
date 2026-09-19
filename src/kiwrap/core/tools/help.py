"""kw_help: markdown pages for the stage protocol, every tool and every error code.

Every `KiwrapError.code` has a page here (docs/ARCHITECTURE.md §11: "every code has a kw_help
page"); `test_core_help.py` asserts that against the code list in `kiwrap.core.state`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import STAGES, Receipt
from kiwrap.core.state.gates import GATES
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec

ERROR_PAGES: dict[str, str] = {
    "E_NO_KICAD": (
        "No installed KiCad matches the request. `kw_env` lists what was discovered (registry, "
        "`C:/Program Files/KiCad/*`, PATH, distro packages, Flatpak). Fix: install KiCad (the newest "
        "release is recommended) or pass `target_kicad` as a major (`'N'`), major.minor (`'N.M'`), a full "
        "semver, or the root/kicad-cli path of an install `kw_env` lists (a path never selects anything "
        "discovery did not find)."
    ),
    "E_UNSUPPORTED": (
        "The selected KiCad (or this kiwrap build) lacks the capability. The error's `data` names "
        "`oldest_kicad_with_it`; `candidates` lists the installed KiCads that have it, and `fix` is the "
        "`kw_project(action='create', target_kicad=...)` call that retargets. Examples: ERC/DRC via "
        "kicad-cli need `cli.has_erc`/`cli.has_drc`; 3D renders need `cli.has_render` (the tool then "
        "returns the 2d layer plot itself); `kw_project upgrade` is a Phase 1 feature."
    ),
    "E_PROJECT_NOT_FOUND": (
        "`project` must be an existing directory holding exactly one `.kicad_pro`, or the `.kicad_pro` path "
        "itself. Create one with `kw_project(action='create', dir=..., name=...)`."
    ),
    "E_BAD_ARGS": (
        "The arguments do not satisfy the tool's input schema or its semantic checks (missing `ref`, "
        "invalid name, `force` without a reason, ...). The message names the field; `example` shows a "
        "valid call."
    ),
    "E_KICAD_FAILED": (
        "kicad-cli exited with an error. `data.stderr`/`data.returncode` carry the details. Exit codes "
        "(verified): 0 ok, 1 bad arguments, 2 unknown, 3 invalid input file (also: file written by a newer "
        "KiCad), 4 output conflict, 5 ERC/DRC violations with --exit-code-violations (handled, not an error), "
        "6 jobset failed; 7/8 report a newer file as 127 / -1. Check `kw_project(action='status')`: the "
        "`epochs` must not exceed what `target_kicad` writes."
    ),
    "E_FILE_LOCKED": (
        "Either KiCad holds a `~<file>.lck` next to a design file (close it in KiCad; kiwrap closes its own "
        "headless api-servers before this check, so a lock without any KiCad or kicad-cli api-server running "
        "is stale and may be deleted) or another kiwrap call holds `<project>/.kiwrap/lock` (wait for it; "
        "delete the file only if no kiwrap process is running)."
    ),
    "E_BUSY": (
        "A kiwrap read (kw_pcb_get / kw_sch_get / kw_pcb_check over the headless IPC session) is still in "
        "flight on this project in another call; its api-server holds KiCad's project lock until it "
        "finishes. Retry in a moment."
    ),
    "E_GATE": (
        "A stage gate is not satisfied: "
        + "; ".join(f"`{t}` needs `{s}`" for t, s in GATES.items())
        + " (kw_pcb_sync / kw_pcb_route_auto are planned Phase 1 tools, not in tools/list yet)"
        ". `ok` means zero *errors* (warnings never gate). Reach the stage with the validator "
        "(`kw_sch_check`, `kw_pcb_drc`) or override with `force={'reason': '...'}` - the reason is written "
        "into the snapshot commit message."
    ),
    "E_EXTERNAL_EDIT": (
        "A `.kicad_*` file changed outside kiwrap since the last validated stage. Tools normally just "
        "downgrade the stage and add `external_edit` to `degraded`; this code is raised only when a tool "
        "cannot proceed on the edited file. Re-run the validators (`kw_sch_check`, `kw_pcb_drc`)."
    ),
    "E_TIMEOUT": (
        "A KiCad process exceeded its time budget. Retry with `run_async=true` where offered and poll "
        "`kw_job`, or reduce the input (fewer targets, smaller render)."
    ),
    "E_INTERNAL": (
        "An unexpected exception inside kiwrap (git failure, unreadable state file, bug). `data.traceback` "
        "is included; report it. `kw_snapshot(action='restore')` undoes any half-applied change."
    ),
}

TOOL_PAGES: dict[str, str] = {
    "kw_env": "Installed KiCads + capabilities + engines + storage paths. `refresh=true` re-probes.",
    "kw_project": (
        "`create(dir, name, target_kicad?, board_size_mm?)` writes a minimal valid project in the target's "
        "format and starts snapshots; `status(project)` returns stage/hashes/epochs/external edits; "
        "`upgrade` is Phase 1."
    ),
    "kw_snapshot": "`list` / `diff(ref, to?)` / `create(message?)` / `restore(ref)` over the project's git history.",
    "kw_job": "`status` / `wait(wait_s)` / `cancel` / `list` for background jobs.",
    "kw_help": "This page. Topics: `overview`, `stages`, `errors`, a tool name or an error code.",
    "kw_sch_get": (
        "Structured schematic state in mm - the primary feedback after every schematic change. "
        "`kinds` (default symbols + hierarchy): symbols (ref, value, lib_id, unit, sheet, x_mm/y_mm, rotation, "
        "pins with ABSOLUTE x_mm/y_mm + type (KiCad tokens: passive, power_in, power_out, tri_state, ...) + net + "
        "connected = has a net that is not an `unconnected-` placeholder), wires (start_mm, end_mm, net), labels "
        "(label_kind, text, net), junctions (net), no_connects, sheets -> `data.items` (flat, `kind` per item, "
        "deterministic order); hierarchy and netlist -> `data.hierarchy` / `data.netlist`. Nets on wires / "
        "labels / junctions come from GetSchematicNetlist (ipc11) or kiwrap's pin connectivity (file). `sheet` "
        "is the human-readable instance path (`/`, `/CH1/`) on both providers; a sheet file placed N times "
        "yields one item per instance with its own reference and nets. Filters `refs`, `nets`, `sheets`, "
        "`bbox_mm`; `detail` summary | normal | full; `cursor`/`limit` page `data.items` AND `data.netlist.nets` "
        "(`next_cursor` null on the last page; a page closes early when it would exceed ~40 KB, "
        "`page_chars` says how big it was). `provider_used` is `ipc11:<version>` (KiCad computed, needs "
        "ipc.headless + ipc.schematic) or `file`; `degraded` lists `netlist_estimated` when the netlist came "
        "from the file walk instead of KiCad, `<provider>_unavailable` when a forced provider could not serve."
    ),
    "kw_sch_check": (
        "kicad-cli ERC (positions corrected to mm) + netlist export. Returns ok (zero errors), errors, "
        "warnings, violations with uuid/pos/sheet, and a netlist summary. Passing sets stage `erc_clean`."
    ),
    "kw_sch_stats": (
        "Schematic health as numbers: components (non-power symbols), power_symbols, sheets, nets, "
        "single_node_nets, dangling_ends, unconnected_pins, label_only_nets, missing_footprint, per_sheet counts, "
        "and `data.lists` with the offenders (sheet + x_mm/y_mm, up to `list_limit`). `ok` = no dangling end, "
        "no unconnected pin, no label-only net. Cheap: call after every schematic edit, then kw_sch_check."
    ),
    "kw_sch_diff": (
        "KiCad-computed diff of two `.kicad_sch` files (`a` = before, `b` = after) via kicad-cli `sch diff "
        "--format json` (needs the `sch diff` subcommand - the KiCad 11 line; elsewhere E_UNSUPPORTED with "
        "fallback `kw_sch_get` on both + compare). `data.changes[]`: kind added|removed|modified, type "
        "(SCH_SYMBOL, SCH_LINE, ...), ref, bbox_mm, properties [{name, before, after, before_mm?, after_mm?}], "
        "changed_properties. `identical` is true when there are no changes. Filters `kinds`, `types`, `refs`; "
        "paging with `cursor`/`limit`. Files whose UUIDs were regenerated diff as removed + added pairs."
    ),
    "kw_pcb_get": (
        "Structured board state in mm - the primary feedback after every board change. `kinds` (default "
        "footprints + outline): footprints (ref, value, lib_id, layer, x_mm/y_mm, rotation_deg, bbox_mm = the "
        "body box - pads + graphics incl. courtyard, NO reference/value text - on both providers, "
        "bbox_with_text_mm (ipc11: the editor's box), courtyard_bbox_mm, pad_count, nets; `detail=full` adds "
        "pads + properties), pads (pad_type thru_hole|smd|np_thru_hole|connect, shape circle|rect|oval|"
        "trapezoid|roundrect|custom, layers in stack order - the same words on both providers), tracks "
        "(start_mm, end_mm, width_mm, layer, net), vias, zones, nets (named nets only; `code` from the file "
        "provider, `netclass` from ipc11) -> `data.items`; outline (bbox_mm, closed), rules (min clearance/"
        "track width, netclasses) and connectivity (unrouted pad pairs with mm endpoints; paged by the same "
        "`cursor`/`limit`, `truncated` + `unrouted_total` say when more remain) -> `data.outline` / "
        "`data.rules` / `data.connectivity`. Filters `refs`, `nets`, `layers`, `bbox_mm`; `detail`; "
        "`cursor`/`limit` (a page closes early when it would exceed ~40 KB; `page_chars`). `data.plan` carries "
        "`elapsed_s` of the provider that answered - on big boards ipc11 starts an api-server (kept alive for "
        "later calls), `provider='file'` answers in well under a second. `provider_used` `ipc11:<version>` "
        "(needs ipc.headless) or `file`; "
        "`degraded: ['airwire_endpoints_estimated']` when ipc11 answered from KiCad's connectivity clusters "
        "(the unrouted COUNT is KiCad's, the pad pairs are nearest-anchor guesses), "
        "`['ratsnest_estimated']` when the unrouted list is kiwrap's file estimate (KiCad exposes no "
        "ratsnest export yet)."
    ),
    "kw_pcb_check": (
        "Actionable placement/routing items with refs + mm: `checks` overlaps (courtyard_overlap, review), "
        "off_board (pad copper outside the outline = must_fix; a courtyard-only overhang = review with "
        "courtyard_bbox_mm + outside_by_mm - KiCad DRC has no rule for it, edge connectors do this by design), "
        "unrouted (ratsnest pairs, info) and drc_summary (the last kw_pcb_drc must_fix count from the project "
        "history with `fresh` = the board is unchanged since, and `capped` = violation types KiCad stopped "
        "reporting at its limit). Each item: {check, severity, refs[], net, pos{x_mm,y_mm}, bbox_mm, "
        "description, fix}. `ok` = no must_fix off_board and no overlap, computed over the WHOLE board before "
        "the filters; `summary.matched` / `total` count what `refs` / `nets` (unrouted only) / `bbox_mm` kept. "
        "Paging as in kw_pcb_get. `next_suggested` is kw_pcb_drc, or kw_fab_export when the last DRC is fresh "
        "and clean. Cheap: call after every placement change; kw_pcb_drc remains the gate."
    ),
    "kw_pcb_drc": (
        "kicad-cli DRC with optional `schematic_parity` and `refill_zones`. Violations are classified "
        "must_fix / review / cosmetic (must_fix gates fabrication). Passing sets stage `drc_clean`. KiCad stops "
        "reporting a violation type after 199 items (499 for clearance / unconnected_items): `data.capped` lists "
        "the saturated types, `degraded` carries `drc_counts_capped` and the counts are lower bounds - fix and "
        "re-run; kw_pcb_check's unrouted count is not capped."
    ),
    "kw_sch_render": (
        "SECONDARY - prefer kw_sch_get / kw_sch_stats for feedback; renders are for human review. "
        "Schematic sheet as PNG (<= `max_px`, default 1024) via kicad-cli `sch export pdf` + pypdfium2. "
        "Args: `sheet` (file name; default the root schematic), `page` (1-based; hierarchical designs export one "
        "page per sheet - an out-of-range page is E_BAD_ARGS with the valid range), `max_px`. Without the "
        "`render` extra the receipt carries the PDF path and `degraded: ['render_unavailable']`."
    ),
    "kw_pcb_render": (
        "SECONDARY - prefer kw_pcb_get / kw_pcb_check for feedback; renders are for human review. "
        "Board as PNG (<= `max_px`, default 1024). `view='3d'` (default) uses kicad-cli `pcb render` where the "
        "install has `cli.has_render` (sizes 1024x768 down to 480x360, or the request's own 4:3 size below "
        "that, until the PNG is under ~100 KB); otherwise, or with `view='2d'`, the 2d layer plot "
        "(`pcb export pdf` of `layers` + pypdfium2; default layers copper + silk + Edge.Cuts of `side`). "
        "Args: `side` (top|bottom), `view`, `layers` (2d only; isolates layers - there is no region crop in "
        "Phase 0), `max_px`. Without a rasteriser the receipt carries the SVG files and `degraded`."
    ),
    "kw_fab_export": (
        "Fabrication outputs: `targets` from gerbers, drill, pos, pdf, step, bom (plus svg/dxf/stats/ipc2581/odb "
        "where the KiCad supports them). Gated on `drc_clean` unless `force`. Returns the file list."
    ),
}


def overview() -> str:
    return (
        "# kiwrap - agent protocol\n\n"
        "1. `kw_env` -> pick a KiCad. 2. `kw_project create` -> minimal project + snapshots. "
        "3. `kw_sch_check` after every schematic change (ok = 0 errors). 4. `kw_pcb_drc` after every board "
        "change (must_fix = 0). 5. `kw_fab_export` (gated on drc_clean).\n\n"
        "Data first: after every mutation query structured state - `kw_sch_get` / `kw_sch_stats` for "
        "schematics, `kw_pcb_get` / `kw_pcb_check` for boards - and act on refs + mm coordinates; never reason "
        "from an image when a structured query exists (renders are secondary). Topic `providers` lists which "
        "backend answers each operation (ipc11 -> file -> cli) and what `degraded` tags mean.\n\n"
        "Rules: never edit `.kicad_*` files by hand (kiwrap re-hashes and downgrades the stage with "
        "`external_edit`); positions are millimetres; every mutating tool returns a Receipt "
        "(changed/validators/provider_used/degraded/snapshot/stage/next_suggested); errors are "
        "`isError` with `{code, message, why, fix, candidates, example}`; `kw_snapshot restore` is undo.\n\n"
        "Stages: " + " -> ".join(STAGES) + "\n\n"
        "Gates: " + "; ".join(f"{t} needs {s}" for t, s in GATES.items()) + "\n\n"
        "Topics: " + ", ".join(["overview", "stages", "errors", "providers", *TOOL_PAGES, *ERROR_PAGES])
    )


def providers_page() -> str:
    lines = [
        "# Providers (planner)\n",
        "Each operation tries its providers in order; the receipt's `provider_used` names the one that answered "
        "(`ipc11:<version>` = headless KiCad IPC, `file` = kiwrap's lossless file engine, `cli:<version>` = a "
        "kicad-cli export) and `degraded[]` carries the tags of the provider used plus `<provider>_unavailable` "
        "when an earlier one failed at call time. `*_estimated` means kiwrap computed what KiCad would normally "
        "compute (KiCad 11 lifts most of these; on 10.x boards and schematics are read from the files).\n",
    ]
    for name, op in planner.describe().items():
        chain = " -> ".join(
            p["provider"]
            + (" [needs " + ", ".join(p["needs"]) + "]" if p["needs"] else "")
            + (" {" + ", ".join(p["degraded"]) + "}" if p["degraded"] else "")
            for p in op["providers"]
        )
        lines.append(f"- `{name}`: {chain}" + (f"; fallback: {op['fallback']}" if op["fallback"] else ""))
    return "\n".join(lines)


GENERAL_TOPICS = ("overview", "stages", "errors", "providers")


def normalise_topic(topic: str) -> str | None:
    """The canonical page key for `topic` (whitespace and case are forgiven: ' E_GATE ', 'e_gate',
    'KW_ENV'), or None when no page matches."""
    key = topic.strip()
    if key in ("", "help"):
        return "overview"
    by_lower = {t.lower(): t for t in [*GENERAL_TOPICS, *ERROR_PAGES, *TOOL_PAGES]}
    return by_lower.get(key.lower())


def page(topic: str) -> tuple[str, list[str]]:
    key = normalise_topic(topic)
    if key == "overview":
        return overview(), []
    if key == "stages":
        return "# Stages\n\n" + " -> ".join(STAGES) + "\n\nGates:\n" + "\n".join(
            f"- {t}: {s}" for t, s in GATES.items()
        ), []
    if key == "providers":
        return providers_page(), []
    if key == "errors":
        return "# Error codes\n\n" + "\n".join(f"## {c}\n{t}\n" for c, t in ERROR_PAGES.items()), []
    if key in ERROR_PAGES:
        return f"# {key}\n\n{ERROR_PAGES[key]}", []
    if key in TOOL_PAGES:
        return f"# {key}\n\n{TOOL_PAGES[key]}", []
    lowered = topic.strip().lower()
    cands = [t for t in [*TOOL_PAGES, *ERROR_PAGES, *GENERAL_TOPICS] if lowered and lowered in t.lower()]
    return overview(), cands


class HelpArgs(BaseModel):
    topic: str = Field(
        default="overview",
        description="overview | stages | errors | providers | a tool name (kw_...) | an error code (E_...)",
    )


def run(ctx: ToolContext, args: HelpArgs) -> Receipt:
    text, cands = page(args.topic)
    key = normalise_topic(args.topic)  # the same normalisation page() uses, so the warning never lies
    warnings = [] if key is not None else [f"unknown topic {args.topic!r}; showing overview"]
    return Receipt(
        provider_used="docs",
        data={"ok": True, "topic": key or args.topic, "markdown": text, "candidates": cands},
        warnings=warnings,
    )


SPEC = ToolSpec(
    name="kw_help",
    title="Help",
    description=(
        "Markdown help: the stage protocol (overview), stages, providers (which backend answers what), every "
        "error code (E_...) and every tool (kw_...)."
    ),
    input_model=HelpArgs,
    handler=run,
    annotations=READ_ONLY,
)
