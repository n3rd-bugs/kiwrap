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
        "headless api-servers - live ones and the locks of servers a killed kiwrap process left behind - "
        "before this check, so a lock without any KiCad running is a GUI's; retry once it is closed) or "
        "another kiwrap call holds `<project>/.kiwrap/lock` (wait for it; delete the file only if no kiwrap "
        "process is running). A live `kw_live` session holds the project too: on a KiCad without a headless "
        "IPC provider the mutating tools are refused while it runs - `kw_live(action='detach')` first."
    ),
    "E_BUSY": (
        "A kiwrap read (kw_pcb_get / kw_sch_get / kw_pcb_check over the headless IPC session) is still in "
        "flight on this project in another call; its api-server holds KiCad's project lock until it "
        "finishes. Retry in a moment."
    ),
    "E_GATE": (
        "A stage gate is not satisfied: "
        + "; ".join(f"`{t}` needs `{s}`" for t, s in GATES.items())
        + ". `ok` means zero *errors* (warnings never gate). Reach the stage with the validator "
        "(`kw_sch_check` for erc_clean - every schematic authoring tool also runs ERC and reaches it when clean; "
        "`kw_pcb_drc`) or override with `force={'reason': '...'}` - the reason is written into the snapshot "
        "commit message."
    ),
    "E_EXTERNAL_EDIT": (
        "A `.kicad_*` file changed outside kiwrap since the last validated stage. Tools normally just "
        "downgrade the stage and add `external_edit` to `degraded`; this code is raised only when a tool "
        "cannot proceed on the edited file - `kw_sch_edit(if_match=...)` raises it when the sheet's sha256 no "
        "longer matches the one the ops were planned against (re-read with kw_sch_get and retry with the "
        "current `data.sheets` hash). Otherwise re-run the validators (`kw_sch_check`, `kw_pcb_drc`)."
    ),
    "E_TIMEOUT": (
        "A KiCad process exceeded its time budget. Retry with `run_async=true` where offered and poll "
        "`kw_job`, or reduce the input (fewer targets, smaller render). From kw_pcb_route_auto / kw_pcb_fix it "
        "means the router hit `timeout_s` (the JVM tree was killed, no session was imported; `why` says whether "
        "the board is unchanged or which stubs / ripped-up copper are gone and `data.restore_ref` the snapshot "
        "that holds them): raise timeout_s, lower passes or route a subset with nets=[...]. A run whose session "
        "was already saved when the budget ran out is kept, not discarded."
    ),
    "E_CANCELLED": (
        "kw_job(action='cancel') reached the running tool: the job ends in state `cancelled` (not `failed`), "
        "the snapshot message says `FAILED E_CANCELLED`, and the board holds what the tool had written before "
        "the cancel (kw_pcb_route_auto: at most the stub clean-up; the SES is never imported after a cancel). "
        "Run the tool again, or kw_snapshot(action='restore', ref=...) for the pre-call state."
    ),
    "E_LIB_NOT_FOUND": (
        "A library nickname or item is not in the resolved tables / files: `candidates` lists near names. "
        "Nicknames and names are case-sensitive; ids are `Nick:Name`. Search first with kw_lib_search."
    ),
    "E_LIB_UNSUPPORTED": (
        "The table row is disabled or of a type the file tier cannot read (Legacy, Altium, Database, HTTP, ...). "
        "Only KiCad-format libraries (.kicad_sym / .pretty) are parsed; convert with KiCad's editors."
    ),
    "E_LIB": "A library file does not parse or cannot be read; `data` names the row. Repair it in KiCad's editor.",
    "E_INTERNAL": (
        "An unexpected exception inside kiwrap (git failure, unreadable state file, bug). `data.traceback` "
        "is included; report it. `kw_snapshot(action='restore')` undoes any half-applied change."
    ),
}

AUTHORING_RECEIPT = (
    " Receipt: `changed`, `validators[]` (before -> after), `provider_used` (`ipc11:<v>` = KiCad 11 headless "
    "commit, `file` = kiwrap's lossless CST writer), `degraded` (`ipc11_guarded` = the G13 hang guard kept a "
    "multi-unit write off IPC, `erc_skipped` / `erc_unavailable`), `snapshot` (undo), `stage`, `next_suggested`."
)

TOOL_PAGES: dict[str, str] = {
    "kw_env": "Installed KiCads + capabilities + engines + storage paths. `refresh=true` re-probes.",
    "kw_lib_search": (
        "FTS search over the target KiCad's symbol or footprint libraries (sandbox global tables + the user's "
        "merged rows + the project's tables when `project` is given). `kind` symbol|footprint, `query` free text "
        "split on non-alphanumerics (every token must match; prefix / partial fallbacks are reported as "
        "near-misses), `filters` {pins, pins_min, pins_max, package, keywords, library, power, units, smd, "
        "through_hole, include_hidden}, `limit`. Returns `hits` (id, name, description, keywords, pin_count / "
        "pad_count, footprint_filters, units, power, score) and `near_misses` (same + `miss` = the failing "
        "filter, e.g. '8 pins (wanted 14)'). The index builds on first use (~7 s) and updates incrementally."
    ),
    "kw_lib_info": (
        "Resolve one `Nick:Name`. Symbols: units, properties, footprint filters, `pins` (number, name, "
        "electrical_type, x_mm/y_mm in LIBRARY coordinates, Y-up - kw_sch_add_symbol returns the absolute "
        "sheet positions), `unit` to list one unit, `footprint_matches` (footprints matching ki_fp_filters "
        "with pad_count and pin_count_match). Footprints: pads (number, type, shape, x_mm/y_mm, size_mm, "
        "layers, drill_mm), courtyard rings, attributes, bbox_mm. `include_node` adds the flattened "
        "lib_symbols entry / .kicad_mod text."
    ),
    "kw_sch_add_symbol": (
        "Place a library symbol: `lib_id` (`Nickname:Name` - never guessed; search the libraries first), `at_mm` "
        "[x, y] (snapped to 1.27 mm), `ref?` (None = `R?` until kw_sch_annotate), `value?`, `rotation` 0|90|180|"
        "270, `mirror` x|y, `unit` (multi-unit symbols, 1-based), `fields` {name: value}, `footprint?`, `sheet` "
        "('' = root, else a sheet file name), `erc` (default true: run ERC after the write), `provider` auto|"
        "ipc11|file. Returns `data.uuid`, `data.ref` and `data.pins[]` = {number, name, x_mm, y_mm, type} in "
        "ABSOLUTE sheet coordinates (the verified 12-orientation transform) - pass them as (ref, pin) to "
        "kw_sch_connect. `data.erc` = {ok, errors, warnings, violations[]} and `data.sheets` = {file: sha256} "
        "(the `if_match` values). The lib_symbols entry is embedded as `<Nickname>:<Name>` with `extends` "
        "flattened; the reference goes into the Reference property AND the instances block. Stage: sch_written, "
        "erc_clean when ERC has zero errors; the board falls out of `synced`." + AUTHORING_RECEIPT
    ),
    "kw_sch_connect": (
        "Connect `endpoints[]` (each {ref, pin} - resolved to the pin's absolute end point through the writer - or "
        "{at_mm: [x, y]} for a corner / wire point) with `style`: `wire` (default; an L-shaped `route` hv|vh|direct "
        "between consecutive endpoints, split at pins / junction sites, snapped to 1.27 mm, junctions added; a "
        "`net` adds a local label on the first endpoint), `label` / `global_label` (a label `net` on every "
        "endpoint), `power` (a power symbol `net` on every endpoint - add a PWR_FLAG with kw_sch_add_item on "
        "power-input nets). Returns `data.endpoints` (resolved mm), `data.wires` (the NEW wire segments only), "
        "`data.junctions` / `data.split_wires` (the junction dots added and the halves of existing wires split "
        "under a new end - never listed as wires), `labels` / `power` uuids, `data.connected_pins` (pins the "
        "wires end on besides the endpoints, `endpoint` false = hit by a corner) and `data.erc`. The hv / vh "
        "corner is NOT obstacle-aware: a route that would pass over another pin is refused (E_BAD_ARGS names "
        "the pin) - use the other route, `direct`, or give the corners as `{at_mm}` endpoints. Pins connect "
        "only at wire END points: never hand-compute a wire to a pin - use (ref, pin)." + AUTHORING_RECEIPT
    ),
    "kw_sch_add_item": (
        "One item: `item.type` power (`net`, at `at_mm` or on `ref`+`pin`), pwr_flag (PWR_FLAG on a power-input "
        "net such as a regulator output or a connector's VBUS - ERC `power_pin_not_driven` otherwise), no_connect "
        "(on an unused pin: `ref`+`pin`), label / global_label / hierarchical_label (`text`, `rotation`; `kind` = "
        "the ERC shape input | output | bidirectional | tri_state | passive of a global / hierarchical label - "
        "match the sheet pin's), sheet (`text` = name, `file` = `<name>.kicad_sch` created with its own instance "
        "path /ROOT/SHEET, `at_mm`, `size_mm` [w, h], `pins` [{name, side, kind}]; returns `data.pins[]` with the "
        "sheet pins' absolute x_mm/y_mm; `file` is a bare name inside the project - a path, `..` or a drive is "
        "E_BAD_ARGS; address the child afterwards with `sheet=<file stem>`), sheet_pin "
        "(`sheet_uuid`, `text`, `side`, `kind`) and text. Returns the uuid (+ pins for power symbols / sheets), "
        "`data.erc`." + AUTHORING_RECEIPT
    ),
    "kw_sch_edit": (
        "`ops[]` on symbols by `ref` or any item by `uuid`: move (`at_mm`), rotate (`rotation`), mirror, property "
        "(`name` + `value`), value, footprint, dnp (`dnp`), delete. `if_match` = the sha256 (prefix ok) of the "
        "sheet file from a previous receipt's `data.sheets`: E_EXTERNAL_EDIT when the sheet changed since. All "
        "ops apply or none (the snapshot is the undo). Returns `data.applied[]`, `data.erc`. A symbol with "
        "several units is edited through the file engine on a KiCad with the G13 hang." + AUTHORING_RECEIPT
    ),
    "kw_sch_annotate": (
        "Number every `R?`-style reference (and duplicates): `scheme` global (default: R1, R2, ...) | sheet (per-sheet "
        "hundreds: R101, R201), `start_per_sheet`, `reset` (default false: existing references are kept, as in "
        "KiCad; true renumbers everything). Returns `data.mapping` {old: new}; references are written to the Reference property "
        "AND the instances block (KiCad reads the instances block). Run it before kw_sch_assign_footprints and "
        "kw_pcb_sync (a `?` reference cannot be synced)." + AUTHORING_RECEIPT
    ),
    "kw_sch_assign_footprints": (
        "Set Footprint properties: `mapping` {ref: fpid} and/or `rules[]` ({lib_id | value | ref_prefix} -> "
        "`fpid`, applied to symbols without a footprint unless `overwrite`). `data.assigned` {ref: fpid}; "
        "`data.unresolved[]` = {ref, lib_id, value, suggestions[]} where the suggestions come from the library "
        "index searched with the symbol's footprint filters (`suggest=true`) - pick one and call again with "
        "`mapping`. Every fpid is resolved through the fp-lib-table (global + project rows, env vars, KIPRJMOD) "
        "BEFORE it is written: an unknown one is not assigned and comes back in `data.unresolved[]` with its "
        "reason and `candidates` (same-library names); `allow_unknown=true` writes it anyway (a project "
        "library not on the table yet). `ok` = nothing unresolved; `next_suggested` is kw_pcb_sync."
        + AUTHORING_RECEIPT
    ),
    "kw_pcb_setup": (
        "Board rules and outline (file provider; the .kicad_pro / .kicad_dru have no IPC writer): `layers` "
        "(2 | 4 | ... or copper layer names), `rules` {clearance_mm, track_width_mm, via_size_mm, via_drill_mm, "
        "min_track_mm, min_via_mm, min_via_annular_mm, min_hole_mm, hole_to_hole_mm, hole_clearance_mm, "
        "copper_edge_clearance_mm, silk_clearance_mm, min_connection_mm}, `netclasses` "
        "{name: {nets: [...], clearance_mm, track_width_mm, ...}} (written to the .kicad_pro), `outline` "
        "({w, h} rectangle, optional x/y origin, or {points_mm: [[x, y], ...]}) on "
        "Edge.Cuts, `custom_rules` (.kicad_dru text), `drc` (default true: DRC summary after the write). "
        "`preset` = jlcpcb-2layer | jlcpcb-4layer (kiwrap.core.fab.jlcpcb, JLCPCB's published capabilities as "
        "SAFE values: 2-layer clearance 0.15 / track 0.25 / via 0.6 x 0.3 / min track 0.127 / min hole 0.2 / "
        "copper-to-edge 0.3 mm; 4-layer clearance 0.127 / track 0.2 / via 0.45 x 0.2 / min track 0.09 / min hole "
        "0.15 mm) sets `layers` and `rules`; explicit keys override it and `data.preset` echoes what was used. "
        "Returns `data.rules_now` (what the board reads back), `data.checks` (off-board / overlaps) and "
        "`data.drc`. Do it before kw_pcb_sync so the outline exists when parts are placed."
    ),
    "kw_pcb_sync": (
        "Schematic -> board (gate: `erc_clean`, i.e. the last kw_sch_check / authoring ERC had zero errors; "
        "`force={'reason'}` overrides). The netlist is KiCad's own `sch export netlist`; footprints are "
        "instantiated from the fp-lib-table (env vars, KIPRJMOD, nested rows), pad nets assigned by (ref, pad) "
        "from the netlist nodes, value / footprint / fields / dnp / exclude_from_board propagated, positions of "
        "existing parts kept (`keep_positions`), orphans removed (`remove_orphans`), new parts placed off-board "
        "in a grid below the outline and the parts an earlier sync parked there. Footprints are paired with "
        "symbols by uuid (`match='path'`, KiCad's default: a re-annotated or swapped reference RENAMES the "
        "footprint and keeps its placement and pad nets, reported in `data.renamed` {old: new}; "
        "`match='reference'` is the GUI's re-link-by-reference box). `provider` ipc11 = ImportNetlist when "
        "the headless server handles it, else file. Returns `data.added / updated / removed / renamed`, "
        "`data.footprint_changes`, `data.unresolved[]` = {ref, fpid, "
        "reason} (ok = none), `data.checks` (off-board / overlaps / unrouted for the new parts), `data.drc` with "
        "`parity` (validators `drc` and `parity`). Stage `synced`; `placed` is dropped until kw_pcb_place moves "
        "the new parts inside the outline. Every schematic write after a sync drops `synced` again."
    ),
    "kw_pcb_place": (
        "Move footprints: `placements[]` {ref, at_mm, rotation?, side? top|bottom, locked?}; `relative_to` (a "
        "ref) makes every at_mm an offset from that footprint. The receipt carries the kw_pcb_check numbers "
        "BEFORE -> AFTER as validators (`courtyard_overlap`, `off_board_copper`, `off_board`, `unrouted` - the "
        "file estimate, consistent for a delta), `data.items[]` = the overlap / off-board items involving the "
        "moved refs (with `fix`), `data.footprints_now` (position / rotation / layer / bbox_mm read back) and, "
        "with `drc=true`, a DRC summary (copper zones are refilled first where the KiCad can, else `degraded` "
        "says `zones_stale` and track-vs-zone clearance items are review). `ok` / stage `placed` = no copper "
        "off-board and no overlap. On a KiCad whose IPC rotation write leaves 1 nm noise (capability "
        "ipc.footprint_update_rotation_noise, docs/UPSTREAM.md G16) a placement that changes a rotation is "
        "answered by the file engine (`degraded: ipc11_guarded`). Iterate: kw_pcb_place -> read the deltas -> "
        "kw_pcb_check for the full picture -> kw_pcb_get for geometry."
    ),
    "kw_pcb_zones": (
        "Copper zones / keepouts: `zones[]` {net ('' for none), layers (one or several copper layers), "
        "polygon_mm [[x, y], ...] | rect_mm [x1, y1, x2, y2], priority, name?, clearance_mm?, min_thickness_mm?, "
        "thermal?, keepout?}; `fill` (default true) refills through `kicad-cli pcb drc --refill-zones "
        "--save-board` on a KiCad with cli.drc_refill_flags (RefillZones over ipc11); `drc` (default true). "
        "Returns `data.zones[]` uuids, `data.filled`, `data.drc` with `touched_nets` (must_fix violations "
        "mentioning the zones' nets). A clean DRC (must_fix 0, no unconnected items) marks the board routed."
    ),
    "kw_pcb_route": (
        "Routing primitives (PCBWorld-style touch-ups; never route whole nets by hand): `ops[]` {op: track, "
        "points_mm [[x, y], ...], width_mm? (default the net's netclass / Default), layer? (copper; checked - "
        "KiCad silently drops tracks on other layers), net} | {op: via, at_mm, size_mm?, drill_mm?, net, layers? "
        "[from, to]} | {op: delete, uuid}. `drc` (default true) runs DRC after the write; validator "
        "`drc_touched_nets` = must_fix violations on the touched nets before -> after, `data.drc.violations` "
        "sorted with the touched nets first. must_fix == 0 (unconnected_items included) -> stage routed + "
        "drc_clean; otherwise fix from the violations (uuid + mm) and re-run."
    ),
    "kw_pcb_route_auto": (
        "Autorouting with Freerouting 2.4.1 under the private JRE 25 that scripts/bootstrap.ps1 installs "
        "(kw_env `engines`; E_UNSUPPORTED with the bootstrap hint when absent). Gate: stage `placed`. The loop, "
        "one snapshot: lock the tracks / vias to keep -> export the Specctra DSN (planner `routing.autoroute`: "
        "`cli` = the patched kicad-cli, `codec` = kiwrap's own writer / reader, `swig` = the bundled-python "
        "driver where KiCad still has SWIG; `provider_used` names it) -> Freerouting headless with the flags "
        "derived from the board (`data.rules`: --router.fanout.enabled=false unless a BGA is on the board - the "
        "fanout pre-pass makes 0.15 mm segments that fail min_track_width; --router.copper_to_edge_clearance_um "
        "from the board's min_copper_edge_clearance because KiCad's DSN omits it; -mt 1 for determinism; -mp "
        "`passes`) -> import the SES (KiCad's importer DELETES every unlocked track / via: `nets=[...]` "
        "therefore locks every other net's copper for the run and restores the flags afterwards, AND removes "
        "those nets from the DSN's network so Freerouting has nothing to route for them - 2.4.1 headless "
        "ignores its `-inc` flag; their pads and copper stay obstacles; `lock_refs` keeps the nets of those "
        "footprints) -> mandatory zone refill -> kicad-cli DRC classified. `data.routed` = tracks/vias "
        "added/removed, `unrouted` (KiCad's unconnected count; validator `unrouted`), `unrouted_connections` "
        "(KiCad's open connections as {net, from: {ref, pad, x_mm, y_mm}, to: {...}}; the router's own list "
        "when no DRC ran); `data.router` = passes, score, stop_reason, router_violations (Freerouting's own "
        "DRC - informational), duration_s; `data.drc` as kw_pcb_drc. A RE-RUN on a partially routed board "
        "keeps the existing unlocked copper as wiring the router only optimises, so it converges to the same "
        "plateau: make progress with kw_pcb_fix (rip-up of the open / violating nets) or `from_scratch=true` "
        "(delete the unlocked copper of the nets to route first). Default `run_async=true` returns a job_id "
        "after the cheap checks (gate, unknown nets, engines: those are immediate errors): kw_job progress "
        "carries the pass, unrouted and violation counts; `cancel` kills the router, the job ends `cancelled` "
        "and the board holds at most the stub clean-up. must_fix > 0 or an open connection -> `next_suggested` "
        "kw_pcb_fix. Freerouting 2.4.1 can emit router-introduced overlaps (#858): 0 must_fix is observed per "
        "board by the DRC, never assumed. A failed run after a board write (stubs removed) re-hashes the board "
        "so the retry is not refused as an external edit."
    ),
    "kw_pcb_fix": (
        "Repair loop for must_fix DRC violations that routing can fix (gate: stage `placed`, `force={reason}` "
        "overrides): DRC -> the nets named in the must_fix violations (`[NET]` tags in KiCad's item "
        "descriptions; unconnected_items count; every violation is read, not only the 50 listed) -> `rip_up_violations` "
        "deletes their unlocked tracks / vias -> `clean` drops zero-length and net-less unlocked copper -> "
        "`reroute` routes ONLY those nets (Freerouting; every other net is locked for the run) -> `refill` + DRC "
        "(the refill always happens where the KiCad can) -> repeat until must_fix == 0 or `max_iterations`. "
        "`data.iterations[]` = {nets, scope, ripped_up, margin_mm, must_fix_before/after, rip_up, router}; "
        "`data.unfixable` = must_fix violations that name no net (a pad-to-pad hole clearance, a footprint "
        "problem: fix with kw_pcb_place / kw_pcb_setup); `data.must_fix` before -> after. A round that does not "
        "reduce must_fix widens the next one (`scope`): `nets` (the violating nets alone, everything else "
        "locked) -> `neighbourhood` (the unlocked copper of other nets within 2 mm of the violation's items is "
        "ripped up and re-routed with them: a pad boxed in by its neighbours cannot be reached otherwise) -> "
        "`board` (every unlocked track / via goes back to the router as re-routable wiring). The receipt "
        "carries ONE `drc` validator delta (before = the initial DRC, after = the final one) matching "
        "`data.must_fix`. A failed round (E_TIMEOUT / E_KICAD_FAILED) names the ripped-up nets in `why` and "
        "`data.restore_ref`: kw_snapshot(action='restore', ref=...) brings their copper back. Default "
        "`run_async=true` (kw_job)."
    ),
    "kw_project": (
        "`create(dir, name, target_kicad?, board_size_mm?)` writes a minimal valid project in the target's "
        "format and starts snapshots; `status(project)` returns stage/hashes/epochs/external edits; "
        "`upgrade` is Phase 1."
    ),
    "kw_snapshot": "`list` / `diff(ref, to?)` / `create(message?)` / `restore(ref)` over the project's git history.",
    "kw_job": (
        "`status` / `wait(wait_s)` / `cancel` / `list` for the background jobs that kw_pcb_route_auto, "
        "kw_pcb_fix and kw_fab_export start with `run_async=true` (their receipt: `data.job_id`, `state`). "
        "`wait` blocks up to `wait_s` seconds and returns early as soon as the job ends. Receipt: `data.done` "
        "(true for done / failed / cancelled), `data.job` = {job_id, name, state (queued | running | done | "
        "failed | cancelled), progress (0..1), message (the current step: `routing: pass 12: 3 unrouted, 0 "
        "violations (3 unrouted)`), result, error, created_at / started_at / finished_at}. `data.job.result` is "
        "the started tool's FULL receipt once state == done: read `result.ok` (== `result.data.ok`), "
        "`result.data` (routed / drc / must_fix / files ...), `result.validators`, `result.warnings`, "
        "`result.next_suggested`; it is null while running or on failure. On `failed`, `data.job.error` = "
        "{code, message, why, fix, candidates, data} (the teaching error the tool raised) and the receipt "
        "carries `warnings: ['job failed: <code>: <message>']` with `ok: false`. `cancel` sets the job's cancel "
        "flag: a running router is killed within a poll interval, the job ends `cancelled` (no error) and the "
        "board holds what the tool had written before (kw_pcb_route_auto: at most the stub clean-up, never a "
        "session). The gate, unknown nets and missing engines are refused BEFORE a job is queued, as immediate "
        "errors. `list` returns every job of this server process."
    ),
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
        "yields one item per instance with its own reference and nets. Filters `refs`, `nets`, `sheets` (a "
        "file stem `blinker`, a file name `blinker.kicad_sch` or an instance path `/blinker/`; an unknown sheet "
        "is E_BAD_ARGS with the hierarchy's paths as candidates, never an empty page), "
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
        "kicad-cli DRC with optional `schematic_parity`; `refill_zones` defaults to true when the board has "
        "copper zones and the KiCad can refill headless (cli.drc_refill_flags; the refilled board is saved and "
        "re-hashed, not an external edit) - a check against a stale fill would report tracks added since the "
        "last fill as clearance errors against the pour; without the capability `degraded` says `zones_stale` "
        "and those items are review. Violations are classified must_fix / review / cosmetic (must_fix gates "
        "fabrication). Passing sets stage `drc_clean`; on failure `next_suggested` names the tool that fixes "
        "the first must_fix type (kw_pcb_route / kw_pcb_place / kw_pcb_sync / kw_pcb_zones). KiCad stops "
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
    "kw_live": (
        "FOR HUMANS WATCHING, not for the agent's feedback loop (data-first, D17: keep reading `kw_pcb_get` / "
        "`kw_pcb_check` / `kw_sch_get`). `action=launch` starts a sandboxed KiCad GUI (`editor`: pcb = pcbnew, "
        "sch = eeschema, project = the manager) on the project's target KiCad (or `target_kicad`) with its API "
        "server on its own pipe, never activated, tracked by PID + Job Object, ONE per agent; the session is "
        "BOUND to the project while it lives: the ipc11 provider of the tools for ITS document kind commits "
        "through that GUI (the edit appears in the VS Code Live KiCad view) and no headless api-server is "
        "started for the project; a write that lands on disk instead (a rotating kw_pcb_place - G16 guard -, "
        "kw_pcb_route_auto / kw_pcb_fix, a saved refill) makes the GUI reload the file (RevertDocument; the "
        "receipt says `live_view.reloaded`), and the other document kind (the schematic of a pcbnew session) "
        "is answered by the file engine with `degraded: [ipc11_unavailable]`. "
        "`mode`: hidden = the editor runs cloaked (`KICAD_INPUT_FROM_EVENTS=cloak`, only on a build carrying the "
        "`display-input` patch; the helper drives it), mirror = visible on the desktop, auto = hidden when "
        "available. Returns `{session_id, pid, install, api_socket, token, editors[], mode_used, cloaked}`. "
        "`attach` registers the extension's display helper (`helper={port, token, version}`); `status` reports "
        "liveness + surfaces (hwnd, rect_px, dpi, cloaked); `screenshot` returns a PNG <= 1024 px (helper, else "
        "a PrintWindow capture of the frame, else a render of the SAVED file with `degraded: "
        "[live_view_unavailable]`); `raise` brings the frame to the front (no-op when cloaked); `detach` closes "
        "the documents, kills the GUI, removes its lock files and unbinds the project. On a KiCad without "
        "`ipc.headless` the planner has no ipc11 provider, so while the GUI holds the project the mutating tools "
        "answer E_FILE_LOCKED: detach first. Every action but launch finds the session by `session_id`, by "
        "`project`, or - when exactly one is live - by nothing."
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
        "where the KiCad supports them). Gated on `drc_clean` unless `force`. Returns the file list per target in "
        "`data.files`. `profile='generic'` = kicad-cli defaults. `profile='jlcpcb'` (docs/DECISIONS.md D9): gerbers "
        "of every copper layer + F/B.Paste, F/B.SilkS, F/B.Mask, Edge.Cuts with Protel extensions, X2, soldermask "
        "subtracted from silk, zones checked; drill = Excellon mm decimal, absolute origin, oval holes 'alternate', "
        "PTH/NPTH separate + a Gerber X2 drill map; pos = CSV mm both sides without DNP, post-processed into "
        "`<name>-cpl-jlcpcb.csv` (Designator, Val, Package, Mid X, Mid Y, Rotation, Layer) with the JLCPCB rotation "
        "corrections table applied - `data.positions.corrected[]` lists EVERY changed row ({ref, package, pattern, "
        "rotation_kicad, rotation_out}) and `bottom_mirrored` the bottom-side refs; bom = `<name>-bom-jlcpcb.csv` "
        "(Comment, Designator, Footprint, LCSC Part #, Qty) through kw_fab_bom's builder, `data.bom.missing_lcsc` "
        "names fitted parts without a part number; gerbers + drill are zipped into `<name>-jlcpcb-gerbers.zip` "
        "(`data.files.bundle`). Options a KiCad lacks are dropped and listed in `data.dropped_options`. "
        "`include_dnp` keeps DNP parts in BOM / CPL, `bom_fields` adds columns. Check every rotation in the vendor's "
        "order preview: the table is a community table (`data.profile_notes`)."
    ),
    "kw_fab_bom": (
        "Bill of materials as data: `data.rows[]` = {refs, qty, value, footprint, package, lcsc, dnp, mpn, "
        "manufacturer, fields, cost: null, stock: null} grouped by value + footprint + vendor part (+ DNP: a DNP "
        "part never merges with its fitted twins). `lcsc` is read from the symbol field `LCSC Part #` (aliases: "
        "LCSC, LCSC Part, LCSC#, JLCPCB Part #, ...); set it with kw_sch_add_symbol(fields={'LCSC Part #': 'C1234'}) "
        "or kw_sch_edit. Source: kicad-cli `sch export bom --fields/--group-by` (KiCad 8+; `data.source` = cli), else "
        "the netlist (`degraded: bom_from_netlist`). `profile` generic (DNP rows kept, flagged) | jlcpcb (DNP "
        "dropped, `data.missing_lcsc` + a warning for fitted parts without a part number); `include_dnp` overrides; "
        "`fields` adds symbol fields as columns; `write=true` writes the profile's CSV to <project>/fab/bom/. Not "
        "gated; cost / stock stay null until the parts component fills them."
    ),
    "kw_intent": (
        "The design intent, declared BEFORE drawing (docs/ARCHITECTURE.md §3.3): `action='set'` stores `intent` = "
        "{blocks: [{name, function?, parts?}], rails: [{name, voltage_v?, current_a?, net?}], interfaces: [{type, "
        "connector?, signals?}], nets: {net: [[ref, pin], ...] or ['U1.3', ...]}, constraints: {size_mm?, layers?, "
        "fab?, ...}} in <project>/.kiwrap/intent.json (`merge=true` merges per key / name, false replaces); "
        "`action='get'` reads it; `action='clear'` removes it; `action='diff'` exports KiCad's netlist and compares "
        "the declared nets with it. Net names match as written or with a leading `/` (a local label `VIN` is `/VIN` "
        "in the netlist; hierarchical nets are `/sheet/NAME`); a net whose name matches nothing is matched by the "
        "pins it shares. The diff: `missing[]` = declared (ref, pin) not on the net ({found_in} = the net it IS on, "
        "null = unconnected or no such pin, `reason`), `wrong_pin[]` = the ref is on the net but with another pin "
        "({expected_pin, actual_pin} - a label on the wrong pin, which ERC cannot see), `extra[]` = pins on the "
        "KiCad net the declaration does not list (power symbols never; listed always, counted as a defect only "
        "with `constraints.strict_nets: true` - declarations are usually partial), `unmatched[]` = declared nets "
        "found nowhere. Once an intent with nets exists, every kw_sch_check reports the same diff as "
        "`validators[name=intent_diff]` (after = missing + wrong_pin [+ extra when strict], detail = the lists) and "
        "`data.intent_diff`; it never gates, but `next_suggested` becomes kw_sch_connect while it is non-empty. "
        "The MVP acceptance (docs/DECISIONS.md D5) is 0 ERC errors AND an empty intent diff."
    ),
}


def overview() -> str:
    return (
        "# kiwrap - agent protocol\n\n"
        "1. `kw_env` -> pick a KiCad. 2. `kw_project create` -> minimal project + snapshots. "
        "3. Schematic: `kw_sch_add_symbol` (returns absolute pin endpoints) -> `kw_sch_connect` by (ref, pin) / "
        "`kw_sch_add_item` (power, PWR_FLAG, no_connect, labels, sheets) -> `kw_sch_stats` / `kw_sch_get` -> "
        "`kw_sch_check` (ok = 0 errors; every authoring tool already ran ERC) -> `kw_sch_annotate` -> "
        "`kw_sch_assign_footprints`. 4. Board: `kw_pcb_setup` (`preset='jlcpcb-2layer'`) -> `kw_pcb_sync` (gate "
        "erc_clean) -> `kw_pcb_place` with `kw_pcb_check` / `kw_pcb_get` feedback -> `kw_pcb_zones` / "
        "`kw_pcb_route_auto` (Freerouting; gate placed; `kw_pcb_fix` re-routes the nets with must_fix violations) "
        "or `kw_pcb_route` touch-ups -> `kw_pcb_drc` (must_fix = 0). 5. `kw_fab_bom` (any time) and `kw_fab_export(profile='jlcpcb')` (gated on "
        "drc_clean). Declare the nets you mean to make with `kw_intent` before step 3: `kw_sch_check` then also "
        "reports the intent diff (missing / wrong_pin / extra pins per declared net).\n\n"
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
        "kicad-cli export; for the routing operations `codec` = kiwrap's Specctra DSN writer / SES reader, "
        "`swig:<version>` = the bundled-python driver) and `degraded[]` carries the tags of the provider used plus `<provider>_unavailable` "
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
