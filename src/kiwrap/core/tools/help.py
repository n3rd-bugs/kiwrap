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
        "returns the 2d layer plot itself); `kw_project upgrade` needs `cli.has_sch_pcb_upgrade` (KiCad 10+)."
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
        "6 jobset failed; 7/8 report a newer file as 127 / -1. A file newer than the KiCad is normally caught "
        "before KiCad runs (E_FORMAT_NEWER); `kw_project(action='status')` shows `epochs`, `target_effective` "
        "and `epoch_match`."
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
    "E_FORMAT_UPGRADE_REQUIRED": (
        "A write would save the project's schematic / board in a NEWER KiCad file format than the files have, "
        "and the KiCad that wrote them could then no longer open the project (docs/DECISIONS.md D21). kiwrap "
        "never does that without the user's consent: a client with MCP elicitation was asked (`data.answer` = "
        "`decline` / `cancel` when they said no); otherwise nothing was written. `data`: `project`, "
        "`files[{path, kind, stamp}]`, `project_epoch`, `project_kicad` (the KiCad line of the files, e.g. 10.0), "
        "`candidates[{version, formats}]` (every installed KiCad), `upgrade_to` (the KiCad whose format the files "
        "would get), `install_hint` (e.g. `winget install KiCad.KiCad --version 10.0.6`, when the release is known) "
        "and - when the stamp guard rolled back a write that had already happened - `guard{files, restored, "
        "snapshot, written_by}`, `mixed` (the files already mix KiCad formats: the message then names each file "
        "that would be converted instead of one KiCad line). **Ask the user; never work around it** (no hand "
        "edits, no other provider). The error's `example` is the non-committing "
        "`kw_project(action='upgrade', project=..., dry_run=true)`: it converts a copy and reports the ERC/DRC "
        "counts before and after, which you can show the user. Only if they agree: "
        "`kw_project(action='upgrade', project=..., confirm=true)` (converted on a copy, ERC/DRC before / "
        "after, a snapshot first, then the install is pinned) and retry the call; no -> install the KiCad in "
        "`install_hint` (kiwrap then picks it by itself), or, when the error's `candidates` name an installed "
        "KiCad that writes the files' format, `kw_project(action='retarget', target_kicad=<it>)`. Reads never "
        "raise it. `kw_help(topic='formats')` has the policy."
    ),
    "E_FORMAT_NEWER": (
        "The design files are newer than the KiCad the call would run on (KiCad refuses them: kicad-cli exits 3 "
        "'created with a more recent version'). `data.needed_kicad` names the KiCad line of the files, "
        "`candidates` the installed KiCads that read them, `data.install_hint` how to install one. Fix: "
        "`kw_project(action='retarget', target_kicad=<a candidate>)` (or `'auto'` to follow the files' format), "
        "or install a newer KiCad. Reads served by the file provider (`kw_pcb_get`, `kw_sch_get`, `kw_pcb_check`, "
        "`kw_sch_stats` with provider auto / file) still answer."
    ),
    "E_STALE": (
        "A stored result or a paging cursor belongs to an older version of the file. `data.reason`: `no_result` "
        "(`kw_pcb_drc(action='report')` before any stored run - run `kw_pcb_drc` first), `board_changed` (the "
        "board changed since the stored DRC - run `kw_pcb_drc` again, then report), `cursor_version` (the file "
        "changed between two pages of `kw_pcb_get` / `kw_sch_get` / a DRC report - start again without `cursor`). "
        "Pass cursors back verbatim; a malformed one is E_BAD_ARGS."
    ),
    "E_READONLY": (
        "The call would write the project, and it runs read-only (D23): the server was started with "
        "`kiwrap serve --readonly` / `KIWRAP_READONLY=1` (`data.mode='server'`), or the call passed "
        "`persist=false` (`data.mode='call'`). `data.reason` says what would have been written. In a read-only "
        "server tell the user the change needs a writable kiwrap - never work around it by editing files; with "
        "`persist=false` drop the flag to make the change (it is snapshotted and undoable). What still works "
        "read-only is in `candidates`; `kw_help(topic='readonly')` has the whole list."
    ),
}

#: kw_help(topic='readonly'): D23 read-only mode (requests/kiwrap-feature-requests.review.md item 2)
READONLY_PAGE = (
    "# Read-only mode (D23)\n\n"
    "A read-only call writes nothing into the project: no snapshot, no `.kiwrap/` directory or state, no saved "
    "zone refill, no report or export inside the project directory, and the `.kicad_prl` KiCad rewrites is put "
    "back. Use it to review a project you do not own.\n\n"
    "**Turn it on:** `kiwrap serve --readonly` (or `KIWRAP_READONLY=1`) makes every call of that server read-only; "
    "on a normal server pass `persist=false` to any single call (every tool has the argument). A read-only server "
    "ignores `persist=true`.\n\n"
    "**What runs:** every read tool (`kw_env`, `kw_sch_get`, `kw_sch_stats`, `kw_sch_diff`, `kw_pcb_get`, "
    "`kw_pcb_check`, `kw_sch_render`, `kw_pcb_render`, `kw_lib_search`, `kw_lib_info`, `kw_help`, `kw_job`), the "
    "validators `kw_sch_check` and `kw_pcb_drc` (zones are refilled in memory; `save_board=true` is refused), "
    "`kw_fab_bom` and `kw_fab_export`, `kw_project(action='status')` and `kw_project(action='upgrade', "
    "dry_run=true)`, `kw_snapshot(action='list'|'diff')`, `kw_intent(action='get'|'diff')` and "
    "`kw_live(action='status'|'screenshot'|'raise'|'detach')`.\n\n"
    "**Annotations:** MCP tool annotations are per tool, so `kw_project` is advertised `destructiveHint: true` "
    "(its `upgrade` replaces design files) although `status` and `upgrade(dry_run=true)` only read; likewise "
    "`kw_pcb_drc` (for `save_board=true`) and `kw_fab_export`. A client that confirms destructive tools asks for "
    "those read-only actions too; the list above is what a read-only server really runs.\n\n"
    "**What is refused (E_READONLY):** every authoring tool (`kw_sch_*` edits, `kw_pcb_setup`, `kw_pcb_sync`, "
    "`kw_pcb_place`, `kw_pcb_zones`, `kw_pcb_route`, `kw_pcb_route_auto`, `kw_pcb_fix`), `kw_project` "
    "create / retarget / upgrade, `kw_snapshot` create / restore, `kw_intent` set / clear, `kw_live` launch / "
    "attach, `kw_pcb_drc(save_board=true)`.\n\n"
    "**Where outputs go:** reports and exports go to a per-call directory under kiwrap's storage home, "
    "`<storage>/results/<project>-<hash>/<time>-<id>/` (the newest 20 per project are kept, at most 24 h), or to "
    "the absolute `out_dir` you pass. The receipt's `data.readonly` says `mode` (server | call), `results_dir`, "
    "and anything the call's project guard had to undo: `relocated` (a file written into the project, moved to the "
    "results directory - receipt paths point at the new place), `restored` (a changed file put back), `left` (a "
    "change the guard did not undo because a KiCad editor or another kiwrap process was writing the project).\n\n"
    "**Stages still work:** the stage, hashes and validator history of a read-only session live in the server's "
    "memory - a clean `kw_pcb_drc` opens the `kw_fab_export` gate for that session - and are dropped when "
    "`.kiwrap/state.json` changes on disk. The project lock is `.kiwrap/lock` when the project already has "
    "`.kiwrap/`, else a lock file under the storage home. `snapshot` is null in every read-only receipt, and a "
    "read-only server withholds a `next_suggested` it could not run (`data.readonly.next_suggested_withheld`)."
)

#: kw_help(topic='formats'): which KiCad a project runs on (D21, review item 1)
FORMATS_PAGE = (
    "# File formats and the KiCad a project runs on (D21)\n\n"
    "Every `.kicad_sch` / `.kicad_pcb` carries a `(version N)` stamp; a KiCad refuses files stamped newer than "
    "it writes, and every KiCad saves in its own (newest) format. So kiwrap picks, per call "
    "(`kw_project(action='status')` shows it as `target_effective` + `target_source`):\n\n"
    "1. the pinned KiCad (`target_source` `created` by `kw_project create`, `pinned` by `retarget` / `upgrade`);\n"
    "2. else the newest installed point release that writes the files' own format (`file_epoch`) - a 10.0 "
    "project runs on the newest 10.0.x even when 10.99 / 11 is installed;\n"
    "3. else the nearest newer KiCad that reads them, for READS only (`file_epoch_newer_readonly`, `epoch_match: "
    "false`, a warning names the missing KiCad and its `install_hint`).\n\n"
    "**A write that would save the files in a newer format asks the user first** (MCP elicitation where the "
    "client supports it; the VS Code extension shows a modal): 'Upgrade <project> from KiCad <x> format to "
    "KiCad <y>? KiCad <x> will no longer open it. Alternatively install KiCad <x>.' Without a yes the call "
    "fails with `E_FORMAT_UPGRADE_REQUIRED` and nothing is written. The only sanctioned conversion is "
    "`kw_project(action='upgrade', project, to_kicad?, confirm=true)`: every sheet (the whole hierarchy, sub-sheets "
    "in subdirectories such as `sch/` included) and the board are upgraded "
    "with `kicad-cli sch/pcb upgrade` on a copy, ERC/DRC counted before and after, a snapshot taken, then the "
    "files are replaced and the KiCad pinned (`dry_run=true` reports without replacing anything).\n\n"
    "**Pins:** `kw_project(action='retarget', target_kicad='10.0')` pins an install without touching a file; "
    "`target_kicad='auto'` unpins (follow the files). **Guards:** `kw_pcb_drc` never saves by default (D22); "
    "a refill or an ipc11 write that would re-stamp the files falls back (`refill_not_saved_epoch`, "
    "`ipc11_epoch_mismatch`); the file writers keep each file's stamp; and every mutation compares the stamps "
    "before and after - a rise nobody consented to is rolled back from the call's snapshot "
    "(`E_FORMAT_UPGRADE_REQUIRED` with `data.guard`; files the rolled-back call created are deleted, "
    "`data.guard.removed`). A project whose sheets already mix KiCad formats (one re-saved by a newer KiCad) "
    "belongs to no single KiCad line: the prompt names each file that would be converted, and the alternative "
    "is a snapshot from before the mix, never an install. Files newer than every installed KiCad are "
    "`E_FORMAT_NEWER`. Every mutation receipt carries `data.target {kicad, source, epoch_match}` and, when a "
    "stamp changed, `data.epochs {before, after}`."
)

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
        "format, pins it (`target_source: created`) and starts snapshots; a `dir` that already holds a "
        "`.kicad_sch` / `.kicad_pcb` is refused (E_BAD_ARGS) - an existing design is adopted with `status` on its "
        "`.kicad_pro`, never re-created. `status(project)`: stage, hashes, "
        "epochs, external edits, and which KiCad the project runs on - `target_effective`, `target_source` "
        "(pinned | created | file_epoch | file_epoch_newer_readonly), `epoch_match` (that KiCad writes the files' "
        "own format; `epoch_match_kinds` per kind), `project_kicad`, `install_hint`, `candidates`, `warnings`. "
        "`retarget(project, target_kicad)`: pin an installed KiCad without touching any file (`'auto'` unpins). "
        "`upgrade(project, to_kicad?, confirm, dry_run?)`: the ONLY sanctioned up-conversion (D21) - pass "
        "`confirm=true` only after the user agreed (without it the user is asked, else E_FORMAT_UPGRADE_REQUIRED); "
        "every sheet and the board are converted with kicad-cli on a copy, `data.upgrade.checks{before, after}` "
        "holds the ERC / DRC counts, `snapshot_before` the pre-upgrade snapshot, then the files are replaced and the "
        "KiCad pinned; `dry_run=true` converts the copy and reports only. Every sheet of the hierarchy is part of "
        "it, sub-sheets in subdirectories (`sch/*.kicad_sch`) included; a replacement that fails puts the files "
        "already replaced back (`data.upgrade_rollback`). `status` and `upgrade(dry_run=true)` are read-only even "
        "though the tool is annotated destructive. `kw_help(topic='formats')` has the policy."
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
    "kw_help": (
        "This page. Topics: `overview`, `stages`, `errors`, `providers`, `formats` (which KiCad a project runs on, "
        "upgrade consent), `readonly` (read-only mode), a tool name or an error code."
    ),
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
        "`bbox_mm`; `detail` summary | normal | full. Paging: ONE opaque cursor pages `data.items` AND "
        "`data.netlist.nets` together (`i<n>.n<n>@<sha8>`; pass `data.next_cursor` back verbatim - it is null "
        "only when every list is exhausted; `limit` applies per list; a page closes early at ~40 KB, `page_chars`; "
        "a cursor from before a schematic edit is E_STALE). `out_path` (an absolute .json / .ndjson / .jsonl / "
        ".txt file or directory, or 'auto' = under the storage home) with `format` json | ndjson writes the "
        "COMPLETE result in one call (`data.out {path, bytes, sha256, counts}`). `provider_used` is "
        "`ipc11:<version>` (KiCad computed, needs ipc.headless + ipc.schematic) or `file`; `provider='cli'` forces "
        "KiCad's netlist (items then come from the file provider; the netlist export goes to a throw-away folder, "
        "never into the project); `degraded` lists `netlist_estimated` when the netlist came from the file walk "
        "instead of KiCad, `<provider>_unavailable` when a forced provider could not serve."
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
        "`data.rules` / `data.connectivity`. Filters `refs`, `nets`, `layers`, `bbox_mm`; `detail`. Paging: ONE "
        "opaque cursor pages `data.items` AND `connectivity.unrouted` together (`i<n>.u<n>@<sha8>`; pass "
        "`data.next_cursor` back verbatim - it is null only when both lists are exhausted, so a routing check "
        "never misses airwires; `limit` per list; a page closes early at ~40 KB, `page_chars`; a cursor from "
        "before a board edit is E_STALE). `out_path` (absolute .json / .ndjson / .jsonl / .txt file or directory, "
        "or 'auto') with `format` json | ndjson writes the COMPLETE result in one call instead of pages "
        "(`data.out {path, bytes, sha256, lines, counts}`). `data.plan` carries "
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
        "reporting at its limit; the violations themselves: kw_pcb_drc(action='report')). Each item: {check, severity, refs[], net, pos{x_mm,y_mm}, bbox_mm, "
        "description, fix}. `ok` = no must_fix off_board and no overlap, computed over the WHOLE board before "
        "the filters; `summary.matched` / `total` count what `refs` / `nets` (unrouted only) / `bbox_mm` kept. "
        "Paging as in kw_pcb_get. `next_suggested` is kw_pcb_drc, or kw_fab_export when the last DRC is fresh "
        "and clean. Cheap: call after every placement change; kw_pcb_drc remains the gate."
    ),
    "kw_pcb_drc": (
        "`action='run'` (default): kicad-cli DRC with optional `schematic_parity`. `refill_zones` defaults to "
        "true when the board has copper zones and the KiCad can refill headless (cli.drc_refill_flags) - a check "
        "against a stale fill would report tracks added since the last fill as clearance errors against the "
        "pour; without the capability `degraded` says `zones_stale` and those items are review. The refill is "
        "IN MEMORY: `save_board` defaults to false (D22) and the board file stays byte-identical; "
        "`save_board=true` writes the refill back only when this KiCad writes the board's own format (D21). "
        "`all_track_errors` (default on where kicad-cli has `--all-track-errors`) reports every violation of "
        "each track, not just the first. Every violation carries `class` (must_fix / review / cosmetic; "
        "must_fix gates fabrication) FIRST, then type, severity, description, pos, items[] (uuid, ref, net) and "
        "`refs`; `excluded` counts KiCad's excluded markers, `violations_truncated` the listed ones beyond the "
        "cap. `ignored_checks[{key, description, source: project | kicad_default}]` names the checks KiCad did "
        "not run (on 8 / 9 only the project's own ignores: `ignored_checks_source='project_only'`). The COMPLETE "
        "result is stored under the storage home (`data.result {stored, board_sha256, path, raw_json}`; nothing "
        "is written into the project). `action='report'` re-reads that stored result WITHOUT running KiCad - "
        "filters `types`, `classes`, `refs`, `nets`, `bbox_mm`, `severity`, paged by `cursor` / `limit`, with "
        "`counts{class, type}` - and is E_STALE when the board changed since (or was never checked). Passing sets "
        "stage `drc_clean`; on failure `next_suggested` names the tool that fixes the first must_fix type "
        "(kw_pcb_route / kw_pcb_place / kw_pcb_sync / kw_pcb_zones). KiCad stops reporting a violation type "
        "after 199 items (499 for clearance / unconnected_items): `data.capped` lists the saturated types, "
        "`degraded` carries `drc_counts_capped` and the counts are lower bounds - fix and re-run; kw_pcb_check's "
        "unrouted count is not capped. Read-only (`persist=false`): the run works, `save_board=true` is refused."
    ),
    "kw_sch_render": (
        "SECONDARY - prefer kw_sch_get / kw_sch_stats for feedback; renders are for human review. "
        "Schematic sheet as PNG (<= `max_px`, default 1024) via kicad-cli `sch export pdf` + pypdfium2. "
        "Args: `sheet` (file name; default the root schematic), `page` (1-based; hierarchical designs export one "
        "page per sheet - an out-of-range page is E_BAD_ARGS with the valid range), `max_px`, `out_path` (an "
        "absolute .png the PNG is written to and kept). Without `out_path` every render gets its own directory "
        "under the storage home (the newest 20 per project are kept for 24 h; the receipt's `kept_until`); "
        "`png_sha256` identifies the image. Without the `render` extra the receipt carries the PDF path and "
        "`degraded: ['render_unavailable']`."
    ),
    "kw_live": (
        "FOR HUMANS WATCHING, not for the agent's feedback loop (data-first, D17: keep reading `kw_pcb_get` / "
        "`kw_pcb_check` / `kw_sch_get`). `action=launch` starts a sandboxed KiCad GUI (`editor`: pcb = pcbnew, "
        "sch = eeschema, project = the manager) on the KiCad the project runs on (`target_effective`, D21; or "
        "`target_kicad`) with its API "
        "server on its own pipe, never activated, tracked by PID + Job Object, ONE per agent; the session is "
        "BOUND to the project while it lives: the ipc11 provider of the tools for ITS document kind commits "
        "through that GUI (the edit appears in the VS Code Live KiCad view) and no headless api-server is "
        "started for the project; a write that lands on disk instead (a rotating kw_pcb_place - G16 guard -, "
        "kw_pcb_route_auto / kw_pcb_fix, a saved refill) makes the GUI reload the file (RevertDocument; the "
        "receipt says `live_view.reloaded`), and the other document kind (the schematic of a pcbnew session) "
        "is answered by the file engine with `degraded: [ipc11_unavailable]`. "
        "`mode`: view (default) = a visible KiCad window on the desktop, never activated, streamed live into the "
        "VS Code tab; the person interacts with it natively (`raise`). hidden = experimental, parked (D20): the "
        "editor runs cloaked, only on a build carrying the `display-input` patch. Returns `{session_id, pid, "
        "install, api_socket, token, editors[], mode_used, cloaked}` (`mode_used` is `view`). "
        "A GUI that would save the files in a newer format than they have asks the user first (the D21 consent; "
        "E_FORMAT_UPGRADE_REQUIRED without it). "
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
        "Board as PNG. `view='3d'` uses kicad-cli `pcb render` where the install has `cli.has_render` (sizes "
        "1024x768 down to 480x360, or the request's own 4:3 size below that, until the PNG is under ~100 KB); "
        "`view='2d'` is the layer plot (`pcb export pdf --scale 1` of `layers` + pypdfium2 cropped to the region, "
        "never the whole sheet). Default view: 2d when `layers`, `bbox_mm`, `px_per_mm` or `mirror_bottom` is "
        "given, else 3d. Args: `side` (top|bottom), `layers` (any of the board's: In1.Cu, F.Paste, F.Mask, ...; "
        "default copper + silk + Edge.Cuts of `side`), `bbox_mm` [x0, y0, x1, y1] (default the Edge.Cuts outline + "
        "1 mm), `px_per_mm` (<= 60), `mirror_bottom` (with side='bottom': the view from below), `max_px` (<= 1024; "
        "`bbox_mm` + `px_per_mm` together may reach 4096 px), `out_path` (an absolute .png, kept). Receipt: "
        "`region{bbox_mm, px_per_mm, width, height, capped}`, `blank` (the region holds no board content), "
        "`png_sha256`, `kept_until` (per-call directory under the storage home, newest 20 kept for 24 h), "
        "`inline=false` + the path when the image is over 2048 px or 1.5 MB. With an explicit `view='3d'` the 2d "
        "arguments are ignored with a warning. Without a rasteriser the receipt carries the SVG files and "
        "`degraded`."
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
        "order preview: the table is a community table (`data.profile_notes`). For reviewers: `out_dir` is any "
        "absolute directory (outside the project allowed; its parent must exist); `layers` limits gerbers (and "
        "pdf / svg / dxf) to e.g. ['F.Paste', 'F.Mask'] (KiCad, standard or the board's own layer names; unknown "
        "-> E_BAD_ARGS with the board's layers); `dry_run=true` returns the file plan (`data.plan`, `data.gate`) "
        "without running KiCad or writing anything, and for `pos` a `positions_preview` = included refs, "
        "`excluded[{ref, reason}]` (exclude_from_pos / attr_unspecified / tht / dnp / side), `flags` and "
        "`bom_diff` against the schematic (`missing_from_cpl` / `extra_in_cpl`) - real pos runs report "
        "`positions.excluded` / `flags` / `bom_diff` too. Zones: gerbers / pdf / svg / dxf / odb refill in memory "
        "(`--check-zones`) where the KiCad can, else `degraded: zones_stale` (`data.zones` per target); the board "
        "is never saved. Read-only (`persist=false`): outputs go to a results directory under the storage home "
        "or `out_dir`."
    ),
    "kw_fab_bom": (
        "Bill of materials as data: `data.rows[]` = {refs, qty, value, footprint, package, lcsc, dnp, mpn, "
        "manufacturer, fields, cost: null, stock: null} grouped by value + footprint + vendor part (+ DNP: a DNP "
        "part never merges with its fitted twins). `lcsc` is read from the symbol field `LCSC Part #` (aliases: "
        "LCSC, LCSC Part, LCSC#, JLCPCB Part #, ...); set it with kw_sch_add_symbol(fields={'LCSC Part #': 'C1234'}) "
        "or kw_sch_edit. Source: kicad-cli `sch export bom --fields/--group-by` (KiCad 8+; `data.source` = cli), else "
        "the netlist (`degraded: bom_from_netlist`). `profile` generic (DNP rows kept, flagged) | jlcpcb (DNP "
        "dropped, `data.missing_lcsc` + a warning for fitted parts without a part number); `include_dnp` overrides; "
        "`fields` adds symbol fields as columns; `write=true` writes the profile's CSV to <project>/fab/bom/ (or the "
        "absolute `out_dir`; read-only: a results directory under the storage home). Not gated; cost / stock stay "
        "null until the parts component fills them."
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
        "Formats (D21): a project runs on the installed KiCad that writes its files' own format; a save that "
        "would upgrade the files asks the user first (`E_FORMAT_UPGRADE_REQUIRED` otherwise: ask, then "
        "`kw_project(action='upgrade', confirm=true)`) - topic `formats`. Read-only (D23): `persist=false` on any "
        "call, or `kiwrap serve --readonly`, writes nothing into the project - topic `readonly`.\n\n"
        "Rules: never edit `.kicad_*` files by hand (kiwrap re-hashes and downgrades the stage with "
        "`external_edit`); positions are millimetres; every mutating tool returns a Receipt "
        "(changed/validators/provider_used/degraded/snapshot/stage/next_suggested); errors are "
        "`isError` with `{code, message, why, fix, candidates, example}`; `kw_snapshot restore` is undo.\n\n"
        "Stages: " + " -> ".join(STAGES) + "\n\n"
        "Gates: " + "; ".join(f"{t} needs {s}" for t, s in GATES.items()) + "\n\n"
        "Topics: " + ", ".join([*GENERAL_TOPICS, *TOOL_PAGES, *ERROR_PAGES])
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


GENERAL_TOPICS = ("overview", "stages", "errors", "providers", "formats", "readonly")


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
    if key == "readonly":
        return READONLY_PAGE, []
    if key == "formats":
        return FORMATS_PAGE, []
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
        description=(
            "overview | stages | errors | providers | formats | readonly | a tool name (kw_...) | an error code (E_...)"
        ),
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
        "Markdown help: the stage protocol (overview), stages, providers (which backend answers what), file formats "
        "and upgrade consent (formats), read-only mode (readonly), every error code (E_...) and every tool (kw_...)."
    ),
    input_model=HelpArgs,
    handler=run,
    annotations=READ_ONLY,
)
