# kiwrap - always-on rules for agents

kiwrap is an MCP server (`kiwrap serve`, registered in `.mcp.json` as `kiwrap`) that drives the
KiCad binaries installed on this machine. KiCad is the judge: validation comes from kicad-cli
ERC/DRC and structured exports, never from the model's own geometry. You author schematics and
boards through the `kw_*` tools only; every mutating tool runs its validator and returns the
numbers in a Receipt.

## The flow (docs/DECISIONS.md D16 KiCad-11-first, D17 data-first)

```
kw_env -> kw_project(create)
  -> [search the libraries: kw_lib_search when listed, else the ids kw_help / the user give; never guess a lib_id]
  -> kw_sch_add_symbol (returns the symbol's pins with ABSOLUTE mm endpoints)
  -> kw_sch_connect by (ref, pin) [wire | label | global_label | power] / kw_sch_add_item (power, pwr_flag, no_connect, labels, sheets)
  -> kw_sch_stats / kw_sch_get -> kw_sch_check (ERC ok = 0 errors; every authoring tool already ran ERC)
  -> kw_sch_annotate -> kw_sch_assign_footprints (unresolved parts come back with index suggestions)
  -> kw_pcb_setup (layers, rules, netclasses, outline, custom rules)
  -> kw_pcb_sync (gate: erc_clean)  -> kw_pcb_place with kw_pcb_check / kw_pcb_get feedback
  -> kw_pcb_zones / kw_pcb_route (DRC delta of the touched nets) -> kw_pcb_drc (must_fix = 0)
  -> kw_fab_export
stages: created -> sch_written -> erc_clean -> synced -> placed -> routed -> drc_clean -> exported
```

Every mutation: lock -> external-edit check -> write (planner: `ipc11` on KiCad 11, else the
lossless `file` writer) -> validator (ERC for schematic tools; DRC / schematic parity / placement
checks for board tools) -> stage transition -> snapshot. Read the Receipt before the next call:
`changed`, `validators[{name, before, after}]`, `provider_used`, `degraded`, `stage`,
`next_suggested`, `snapshot`, `data.erc` / `data.drc` / `data.checks`.

## Data first

The feedback loop is **structured data, not images**. After every mutation, query the state and
act on refs and millimetre coordinates:

| you changed | the receipt already has | query next | then |
|---|---|---|---|
| a schematic (add_symbol / connect / add_item / edit / annotate / assign) | `data.erc` (ok, errors, warnings, violations with mm + uuid), `data.sheets` (sha256 per sheet = `if_match`) | `kw_sch_stats` (dangling ends, unconnected pins, label-only nets) and `kw_sch_get` (symbols with absolute pin endpoints + nets, wires, labels, netlist) | `kw_sch_check` when stats are clean; `kw_sch_annotate` -> `kw_sch_assign_footprints` |
| the board (setup / sync / place / zones / route) | `data.drc` (must_fix / review / cosmetic, `touched_nets`), `data.checks` or before -> after validators (courtyard overlaps, off-board, unrouted), `data.footprints_now` | `kw_pcb_check` (actionable items with refs + mm + fix) and `kw_pcb_get` (footprints / pads / tracks / vias / zones / nets / outline / rules) | `kw_pcb_drc` when `ok` (review items never block it) |
| two schematic versions | - | `kw_sch_diff` (KiCad-computed, KiCad 11 line) or `kw_sch_get` on both | - |

**Never reason from an image when a structured query exists.** `kw_sch_render` / `kw_pcb_render`
are secondary (human review, VS Code previews); a render never replaces `kw_*_get` / `kw_*_check`.

Filters and paging: `kw_pcb_get` takes `refs` / `nets` / `layers` / `bbox_mm`, `detail`
(summary | normal | full) and `cursor` / `limit`; `kw_sch_get` takes `refs` / `nets` / `sheets` /
`bbox_mm`, `detail` and `cursor` / `limit`; `kw_pcb_check` takes `refs` / `nets` (unrouted items
only) / `bbox_mm` and `cursor` / `limit`; `kw_sch_diff` takes `kinds` / `types` / `refs`.
`next_cursor` is null on the last page; a page closes early when it would exceed ~40 KB. Filter
first; never page through a whole board when you need one net.

## Authoring rules (docs/ARCHITECTURE.md §7, §8)

- **Never guess library ids or pin positions.** `lib_id` is `Nickname:Name` from the library
  tables (`Device:R`, `power:GND`, `Connector:USB_C_Receptacle_USB2.0_16P`). `kw_sch_add_symbol`
  returns `data.pins[]` with absolute `x_mm/y_mm`; `kw_sch_connect` takes `(ref, pin)` and
  resolves the endpoint itself. Pins connect only at wire END points; the writer splits wires at
  pins, snaps to the 1.27 mm grid and adds junctions. The `hv` / `vh` corner of `kw_sch_connect`
  is NOT obstacle-aware: a route that would pass over another pin is refused (E_BAD_ARGS names
  the pin; `data.connected_pins` lists pins a wire ends on besides the endpoints) - use the
  other route or give the corners as `{at_mm}` endpoints.
- Batch placement with `erc=false` (`kw_sch_add_symbol`, `kw_sch_connect`, `kw_sch_add_item`)
  and let the last call (or `kw_sch_check`) run ERC; a placed-but-unwired symbol is an ERC
  `pin_not_connected` error until it is wired, so do not expect `erc_clean` mid-way.
- Power: `kw_sch_connect(style="power", net="GND", endpoints=[{ref, pin}])` puts the power
  symbol on the pin; add a `pwr_flag` item (`kw_sch_add_item`) on every power-input net that
  nothing drives (regulator outputs, a connector's VBUS) or ERC reports `power_pin_not_driven`.
  Unused pins get a `no_connect` item. A local label's net is named `/<label>` on the board.
- `kw_sch_annotate` (default `scheme="global"`: R1, R2, ...; references you set are kept unless
  `reset=true`) before `kw_sch_assign_footprints` and `kw_pcb_sync`; `?` references cannot be
  synced. `kw_sch_assign_footprints` resolves every fpid through the fp-lib-table first: an
  unknown one is not written and comes back in `unresolved` with candidates
  (`allow_unknown=true` for a project library not on the table yet).
- Hierarchy: `kw_sch_add_item(type="sheet", text, file, at_mm, size_mm, pins=[{name, side,
  kind}])` creates the child file (`file` is a bare name inside the project, never a path);
  address it afterwards with `sheet="<file stem>"` in every schematic tool (`kw_sch_get`'s
  `sheets` filter also takes the file name or the instance path `/blinker/`); put a
  `hierarchical_label` of the same `text` and `kind` inside the child. A net through a sheet
  pin is named after the child's label (`/blinker/BLINK`) on the board.
- `kw_sch_edit` ops: move / rotate / mirror / property / value / footprint / dnp / delete by `ref`
  (symbols) or `uuid` (anything). Moving a wired symbol breaks its wires (ERC tells you). Pass
  `if_match` (the sheet sha256 from `data.sheets`) when your ops were planned from an earlier
  read; a mismatch is `E_EXTERNAL_EDIT`.
- Board: `kw_pcb_setup` first (outline + rules; a netclass net that matches nothing is warned
  about - local-label nets are `/NAME`), `kw_pcb_sync` needs `erc_clean` (force only when the
  user accepts it) and pairs footprints with symbols by uuid (`match="path"`: a re-annotated or
  swapped reference RENAMES the footprint and keeps its nets; `renamed` in the receipt). New
  footprints land off-board in a grid: `kw_pcb_place` them inside the
  outline (`placements[{ref, at_mm, rotation, side}]`; `relative_to` for offsets); the receipt
  carries overlaps / off-board / unrouted before -> after and `data.items` for the moved refs.
  `placed` = no copper off-board and no courtyard overlap.
- `kw_pcb_route` is for touch-ups (track polyline / via / delete): widths default to the net's
  netclass, layers must be copper, nets must exist on the board (candidates are suggested). The
  receipt's `drc_touched_nets` validator tells you what the op did to those nets. `kw_pcb_zones`
  adds filled zones / keepouts (refilled on KiCad 10+ through kicad-cli, RefillZones over IPC).
  `kw_pcb_drc` remains the gate; `must_fix == 0` (unconnected items included) makes the board
  `routed` + `drc_clean`. When the board has copper zones, `kw_pcb_place` / `kw_pcb_route` /
  `kw_pcb_drc` refill them before the DRC (a stale fill reports new tracks as clearance errors
  against the pour); on a KiCad without headless refill the receipt says `zones_stale` and
  those items are `review`.

## KiCad 11 first

The primary target is the newest KiCad (10.99 nightly today, 11.0 at GA); 10.0.x is co-primary.
The planner (`kw_help(topic="providers")`) tries, per operation, `ipc11` (headless
`kicad-cli api-server`, KiCad computes / commits the change) -> `file` (kiwrap's lossless file
engine) -> `cli` (kicad-cli exports). Read the receipt, not the version number:

- `provider_used`: `ipc11:<version>` = KiCad did it; `file` = kiwrap's CST writer / reader;
  `cli:<version>` = a kicad-cli run.
- `degraded[]`: `ipc11_unavailable` (the headless server could not serve; the file engine
  answered), `ipc11_guarded` (the G13 hang guard kept a multi-unit write off IPC), `erc_skipped`
  / `erc_unavailable` / `drc_unavailable` / `refill_unavailable`, `ratsnest_estimated` /
  `airwire_endpoints_estimated` / `netlist_estimated` (kiwrap computed what KiCad normally
  computes), `external_edit` (a `.kicad_*` file was hand-edited), `render_unavailable`.

## Stage gates and receipts

- `kw_sch_check` ok = zero ERC **errors** (warnings never gate) -> `erc_clean`; every schematic
  authoring tool runs the same ERC and reaches `erc_clean` when clean. A schematic write drops
  `synced` (the board must be re-synced).
- `kw_pcb_sync` needs `erc_clean`; `kw_fab_export` needs `drc_clean`; `kw_pcb_route_auto` (planned)
  will need `placed`. Override only with `force={"reason": "..."}`; the reason is written into
  the snapshot commit message.
- `kw_pcb_drc` ok = zero **must_fix** (clearance, shorting_items, tracks_crossing,
  unconnected_items, hole_clearance, net_conflict, track_width, any other error) -> `drc_clean`.
  Counts can be LOWER BOUNDS (`data.capped`, `degraded: drc_counts_capped`).
- Every mutating tool commits `[kiwrap] <tool> ...` to the private snapshot store;
  `kw_snapshot(action="restore", ref=<sha>)` is the universal undo.

## Tools (in `tools/list` order) and their arguments

| tool | args | returns |
|---|---|---|
| `kw_env` | `refresh?` | installs (newest first), capabilities, engines, storage paths |
| `kw_project` | `action: create\|status`, `dir`, `name`, `target_kicad?`, `board_size_mm?`, `project?` | paths, epochs, stage, next |
| `kw_snapshot` | `project`, `action: list\|diff\|create\|restore`, `ref?`, `to?`, `message?` | sha / files / diff |
| `kw_job` | `job_id`, `action: status\|wait\|cancel\|list`, `wait_s?` | state, result |
| `kw_help` | `topic: overview\|stages\|errors\|providers\|kw_*\|E_*` | markdown |
| `kw_sch_add_symbol` | `project`, `lib_id`, `at_mm`, `ref?`, `value?`, `rotation?`, `mirror?`, `unit?`, `fields?`, `footprint?`, `sheet?`, `erc?`, `provider?` | `uuid`, `ref`, `pins[]` (absolute mm), `erc`, `sheets` |
| `kw_sch_connect` | `project`, `endpoints[{ref, pin} \| {at_mm}]`, `style?`, `net?`, `route?`, `sheet?`, `erc?`, `provider?` | resolved `endpoints`, `wires` / `junctions` / `split_wires` / `labels` / `power` uuids, `connected_pins`, `erc` |
| `kw_sch_add_item` | `project`, `item{type, at_mm \| ref+pin, net, text, rotation, file, size_mm, pins, ...}`, `sheet?`, `erc?` | `uuid` (+ pins), `erc` |
| `kw_sch_edit` | `project`, `ops[{op, ref \| uuid, at_mm, rotation, mirror, name, value, dnp}]`, `if_match?`, `sheet?`, `erc?` | `applied[]`, `erc` |
| `kw_sch_annotate` | `project`, `scheme?`, `start_per_sheet?`, `reset?`, `erc?` | `mapping` {old: new} |
| `kw_sch_assign_footprints` | `project`, `mapping?`, `rules?`, `overwrite?`, `suggest?`, `allow_unknown?`, `erc?` | `assigned`, `rejected`, `unresolved[]` with suggestions |
| `kw_sch_get` | `project`, `kinds?`, `refs?`, `nets?`, `sheets?`, `bbox_mm?`, `detail?`, `cursor?`, `limit?`, `provider?` | `data.items[]`, `data.hierarchy`, `data.netlist` |
| `kw_sch_check` | `project`, `severity?`, `netlist?` | ERC ok/errors/warnings/violations (mm), netlist summary |
| `kw_sch_stats` | `project`, `provider?`, `list_limit?` | counts + offender lists with positions |
| `kw_sch_diff` | `a`, `b`, `target_kicad?`, `kinds?`, `types?`, `refs?`, `cursor?`, `limit?` | `identical`, `changes[]` |
| `kw_sch_render` | `project`, `sheet?`, `page?`, `max_px?` | PNG + summary (secondary) |
| `kw_pcb_setup` | `project`, `layers?`, `rules?`, `netclasses?`, `outline?`, `custom_rules?`, `drc?` | `rules_now`, `checks`, `drc` |
| `kw_pcb_sync` | `project`, `remove_orphans?`, `keep_positions?`, `match?`, `force?`, `drc?`, `provider?` | `added` / `updated` / `removed` / `renamed`, `unresolved[]`, `checks`, `drc` (+ parity) |
| `kw_pcb_place` | `project`, `placements[{ref, at_mm, rotation?, side?, locked?}]`, `relative_to?`, `drc?`, `provider?` | `before` -> `after`, `items[]`, `footprints_now` |
| `kw_pcb_zones` | `project`, `zones[{net, layers, polygon_mm \| rect_mm, priority, ...}]`, `fill?`, `drc?`, `provider?` | `zones[]`, `filled`, `drc` |
| `kw_pcb_route` | `project`, `ops[{op: track \| via \| delete, ...}]`, `drc?`, `provider?` | `applied[]`, `touched_nets`, `drc` |
| `kw_pcb_get` | `project`, `kinds?`, `refs?`, `nets?`, `layers?`, `bbox_mm?`, `detail?`, `cursor?`, `limit?`, `provider?` | `data.items[]`, `data.outline`, `data.rules`, `data.connectivity` |
| `kw_pcb_check` | `project`, `checks?`, `refs?`, `nets?`, `bbox_mm?`, `cursor?`, `limit?`, `provider?` | `ok`, `summary`, `drc_summary`, `items[]` with `fix` |
| `kw_pcb_drc` | `project`, `schematic_parity?`, `refill_zones?`, `save_board?`, `severity?` | must_fix / review / cosmetic violations (mm) |
| `kw_pcb_render` | `project`, `view?`, `side?`, `layers?`, `max_px?` | PNG + summary (secondary) |
| `kw_fab_export` | `project`, `targets?`, `profile?`, `force?`, `run_async?` | files, unsupported targets |

`kw_help(topic=<tool>)` has the long form of every row, including the item shapes. Library
tools (`kw_lib_search` / `kw_lib_info`) appear in `tools/list` when the library index component
is present; use them before every `lib_id` / `fpid`.

## Hard rules

- **Never edit `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` / `.kicad_sym` / `.kicad_mod` by hand.**
  kiwrap re-hashes the inputs of every stage; a hand edit downgrades the stage and adds
  `external_edit` to `degraded`. Use the kw_* tools (`kw_sch_edit` covers what a hand edit would).
- **Positions and sizes are millimetres** everywhere.
- The user's real KiCad configuration (`%APPDATA%\kicad`) is never written; every KiCad process
  (kicad-cli, the headless api-server) runs against a kiwrap-owned sandbox and every api-server a
  tool starts is stopped before the tool returns.
- Never launch a KiCad GUI from tests or tools.
- Errors come back as `isError` results with `{code, message, why, fix, candidates, example}`;
  `kw_help(topic="E_...")` explains every code. Do not retry blindly - apply `fix`.
- Never claim success without the validator numbers from the receipt and a structured query of
  the result (`kw_sch_check` / `kw_pcb_drc` are KiCad's word).

## Teaching errors

| code | meaning | do this |
|---|---|---|
| `E_NO_KICAD` | no install matches `target_kicad` | `kw_env`, then pick from `candidates` |
| `E_UNSUPPORTED` | capability / component missing on this KiCad or build | use `data.fallback` (names a kw_* tool) or a KiCad from `candidates` |
| `E_PROJECT_NOT_FOUND` | bad `project` path or a missing design file | pass the dir or `.kicad_pro`; create with `kw_project` |
| `E_BAD_ARGS` | schema/semantic argument error (unknown pin, unknown ref, a net or layer the board lacks, 3-number `bbox_mm`) | follow `fix` / `example` / `candidates` |
| `E_KICAD_FAILED` | kicad-cli or the api-server exited non-zero | read `data.stderr`; check epochs vs target |
| `E_FILE_LOCKED` | KiCad has the file open, or another kiwrap call runs (kiwrap's own headless servers, live or dead, are released first) | close KiCad / wait |
| `E_GATE` | stage gate not met (`kw_pcb_sync` needs `erc_clean`, `kw_fab_export` needs `drc_clean`) | run the validator named in `fix`; `force={"reason"}` only when the user accepts the risk |
| `E_EXTERNAL_EDIT` | `if_match` no longer matches the sheet, or a file changed under a running operation | re-read (`kw_sch_get`, `data.sheets`) and retry; re-run validators |
| `E_TIMEOUT` | KiCad exceeded its time budget | `run_async=true` + `kw_job`, or filter/page the read |
| `E_INTERNAL` | kiwrap bug or git failure | report `data.traceback`; restore a snapshot |

## Where things are

- Tool catalog + design: `docs/ARCHITECTURE.md` (§6 planner, §7 schematic writer rules, §8
  placement/routing, §9 agent surface, §10 catalog, §11 receipts/gates).
- Decisions: `docs/DECISIONS.md` (D16 KiCad-11-first, D17 data-first, D18 Windows + VS Code).
- Verified KiCad facts: `docs/VERIFICATION.md`; the scripts behind them: `spikes/`.
- The flow skill: `.claude/skills/kiwrap-flow/SKILL.md`.
