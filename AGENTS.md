# kiwrap - always-on rules for agents

kiwrap is an MCP server (`kiwrap serve`, registered in `.mcp.json` as `kiwrap`) that drives the
KiCad binaries installed on this machine. KiCad is the judge: validation comes from kicad-cli
ERC/DRC and structured exports, never from the model's own geometry. You author schematics and
boards through the `kw_*` tools only; every mutating tool runs its validator and returns the
numbers in a Receipt.

## The flow (docs/DECISIONS.md D16 KiCad-11-first, D17 data-first)

```
kw_env -> kw_project(create) -> kw_intent(set: blocks, rails, interfaces, nets {net: [[ref, pin]...]}, constraints)
  -> [search the libraries: kw_lib_search when listed, else the ids kw_help / the user give; never guess a lib_id]
  -> kw_sch_add_symbol (returns the symbol's pins with ABSOLUTE mm endpoints)
  -> kw_sch_connect by (ref, pin) [wire | label | global_label | power] / kw_sch_add_item (power, pwr_flag, no_connect, labels, sheets)
  -> kw_sch_stats / kw_sch_get -> kw_sch_check (ERC ok = 0 errors + validators.intent_diff = 0; every authoring tool already ran ERC)
  -> kw_sch_annotate -> kw_sch_assign_footprints (unresolved parts come back with index suggestions)
  -> kw_pcb_setup (preset="jlcpcb-2layer" | layers, rules, netclasses, outline, custom rules)
  -> kw_pcb_sync (gate: erc_clean)  -> kw_pcb_place with kw_pcb_check / kw_pcb_get feedback
  -> kw_pcb_zones (the B.Cu GND plane) -> kw_pcb_route_auto (gate: placed; a job -> kw_job)
     -> kw_pcb_fix while the receipt says must_fix > 0 -> kw_pcb_zones (the F.Cu pour)
     [kw_pcb_route for touch-ups: DRC delta of the touched nets] -> kw_pcb_drc (must_fix = 0)
  -> kw_fab_bom (any time: rows + missing LCSC numbers) -> kw_fab_export(profile="jlcpcb")
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

**`kw_live` is for humans watching; the agent loop stays data-first.** When the person wants to
see the board or schematic change as you work, `kw_live(action="launch", project=...)` starts a
sandboxed KiCad GUI on the project (never activated, one per agent, killed on `detach` or when
the server exits) and the VS Code extension shows it in the Live KiCad view. While it lives, the
session is bound to the project: `kw_pcb_place` (moves, flips, locks), `kw_pcb_zones` and
`kw_pcb_route` commit through that GUI's API and appear live. Writes that land on disk instead -
a `kw_pcb_place` that changes a rotation (the G16 guard routes it to the file writer on every
10.99 build), `kw_pcb_route_auto` and `kw_pcb_fix` (they import the router's session into the
file), a `kw_pcb_drc(refill_zones=true, save_board=true)` - make the GUI reload the file
(`RevertDocument`, receipt `data.live_view.reloaded` + a warning), so the view catches up and the
editor's next save cannot undo them; a person's unsaved edit in that GUI is lost by that reload,
which is why they watch and you write. While a session is bound the DRC validators inside the
authoring tools never write the refill back (`refill_saved: false`). A session serves ONE
document kind: with a `pcbnew` session bound the schematic tools (`kw_sch_get`, `kw_sch_stats`,
`kw_sch_*` writes) are answered by the file engine (`degraded: [ipc11_unavailable]`, a warning
names the session), and the reverse for an `eeschema` session; nothing errors. Keep verifying
with `kw_pcb_check` / `kw_pcb_get`; `kw_live(action="screenshot")` answers "what does it look
like", not "is it right". The session may have been started by the VS Code extension (its own
kiwrap server): `kw_live(action="status", project=...)` finds it all the same (`remote: true`),
your writes go through it, and `launch` on that project is refused with E_BUSY while it lives -
use it, or `detach` it. `detach` when the person is done: on a KiCad without a headless IPC
provider the GUI holds the files and the mutating tools are refused until then.

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
- **Route with `kw_pcb_route_auto`, never by hand net by net.** Order: `kw_pcb_zones` the GND
  plane on B.Cu first (the router works against it), then `kw_pcb_route_auto(project, passes?,
  timeout_s?, nets?, lock_refs?, from_scratch?)` - gated on `placed`, DESTRUCTIVE (every
  unlocked track / via of the routed nets goes to the router; lock what you hand-routed with
  `lock_refs` or `nets`). `nets=[...]` really routes only those nets: the others leave the DSN's
  network (their pads and copper stay as locked obstacles), so "power first, signals by hand"
  works. A RE-RUN on a partially routed board is NOT a fresh start: the existing unlocked copper
  is handed to the router as wiring it keeps and only optimises, so it converges to the same
  plateau - make progress with `kw_pcb_fix` (rip-up of the open / violating nets) or
  `from_scratch=true` (delete the unlocked copper of the nets to route first). It returns a
  `job_id` by default (the gate, unknown nets and missing engines are refused immediately, before
  a job is queued): poll `kw_job(job_id, action="wait", wait_s=30)` until `data.done`; the job's
  `result` is the receipt and `result.ok == result.data.ok` (kw_help(topic="kw_job") has the
  shape): `data.routed` (tracks / vias added, `unrouted` = KiCad's unconnected count after the
  refill, `unrouted_connections` = KiCad's open connections `{net, from: {ref, pad, x_mm, y_mm},
  to}`), `data.router` (passes, per-pass history, score, stop reason), `data.drc` (must_fix /
  review / unconnected), `data.dsn` (the exported nets / classes / planes), `provider_used`
  (`codec` = kiwrap's own DSN/SES codec on stock KiCad, `cli:<v>+specctra-jobs` on a patched
  build), `degraded`, `next_suggested`. When `must_fix > 0` or `unrouted > 0` call
  `kw_pcb_fix(project, max_iterations?)` (also a job, gated on `placed`): it rips up the nets
  named in the must_fix violations (every violation, not only the 50 listed), re-routes only
  those and repeats until `must_fix == 0`; its receipt has ONE `drc` delta (initial -> final);
  `data.unfixable` lists violations that name no net (not a routing problem). A failed run
  (`E_TIMEOUT`, `E_KICAD_FAILED`) says in `why` what is on disk - unchanged, stubs removed, or
  the ripped-up nets - and `data.restore_ref` names the snapshot that brings the copper back;
  the retry is never refused as an external edit. `kw_job(action="cancel")` ends the job
  `cancelled` (not `failed`). Then `kw_pcb_zones` the F.Cu pour and `kw_pcb_drc`. The
  autorouter needs the engines `scripts/bootstrap.ps1` installs (`E_UNSUPPORTED` with the hint
  otherwise; `kw_env` lists them).
- `kw_pcb_route` is for touch-ups (track polyline / via / delete): widths default to the net's
  netclass, layers must be copper, nets must exist on the board (candidates are suggested). The
  receipt's `drc_touched_nets` validator tells you what the op did to those nets. `kw_pcb_zones`
  adds filled zones / keepouts (refilled on KiCad 10+ through kicad-cli, RefillZones over IPC).
  `kw_pcb_drc` remains the gate; `must_fix == 0` (unconnected items included) makes the board
  `routed` + `drc_clean`. When the board has copper zones, `kw_pcb_place` / `kw_pcb_route` /
  `kw_pcb_route_auto` / `kw_pcb_fix` / `kw_pcb_drc` refill them before the DRC (a stale fill
  reports new tracks as clearance errors against the pour); on a KiCad without headless refill
  the receipt says `zones_stale` and those items are `review`.

## Intent, fabrication profile, BOM (Phase 1c)

- **Declare before you draw.** `kw_intent(action="set", intent={blocks, rails, interfaces, nets:
  {net: [["U1", "3"], "C1.1", ...]}, constraints: {size_mm, layers, fab: "jlcpcb", strict_nets?}})`
  stores `<project>/.kiwrap/intent.json`. From then on `kw_sch_check` reports
  `validators[name=intent_diff]` and `data.intent_diff`: `missing[]` (declared pin not on the net;
  `found_in` says where it is), `wrong_pin[]` (the ref is on the net with another pin - a label on
  the wrong pin, invisible to ERC), `extra[]` (undeclared pins on the net; counted only with
  `constraints.strict_nets`), `unmatched[]`. Net names match as written or with `/` (local labels
  are `/VIN`; hierarchical `/sheet/NAME`); an unnamed net is matched by its pins. The MVP
  acceptance is 0 ERC errors AND an empty intent diff. `kw_intent(action="diff")` runs it alone.
- **Rules preset.** `kw_pcb_setup(preset="jlcpcb-2layer" | "jlcpcb-4layer", outline=...)` writes
  JLCPCB-safe rules (2-layer: clearance 0.15, track 0.25, via 0.6/0.3, min track 0.127, min hole
  0.2, copper-to-edge 0.3 mm; 4-layer: 0.127 / 0.2 / 0.45x0.2 / 0.09 / 0.15 / 0.3); explicit
  `layers` / `rules` keys override. `data.preset` echoes what was applied.
- **LCSC numbers are symbol fields.** Give every fitted part `fields={"LCSC Part #": "C25804"}`
  in `kw_sch_add_symbol` (or `kw_sch_edit`); `kw_fab_bom(profile="jlcpcb")` lists the parts
  without one in `data.missing_lcsc` - JLCPCB cannot assemble those. DNP parts are grouped apart
  and left out of the vendor BOM / CPL. `cost` / `stock` are null until the parts tools exist.
- **`kw_fab_export(profile="jlcpcb", targets=["gerbers","drill","pos","bom"])`** = the vendor
  bundle: Protel-named X2 gerbers of every copper + paste/silk/mask/Edge.Cuts, Excellon PTH/NPTH +
  map, `<name>-cpl-jlcpcb.csv` with the rotation corrections applied (`data.positions.corrected[]`
  names every changed row - check them in JLCPCB's order preview, the table is a community
  table), `<name>-bom-jlcpcb.csv`, and `<project>/fab/<name>-jlcpcb-gerbers.zip` to upload.
  Options this KiCad lacks are dropped and listed in `data.dropped_options`, never guessed.

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
- `kw_pcb_sync` needs `erc_clean`; `kw_fab_export` needs `drc_clean`; `kw_pcb_route_auto`
  and `kw_pcb_fix` need `placed`. Override only with `force={"reason": "..."}`; the reason is
  written into the snapshot commit message.
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
| `kw_intent` | `project`, `action: set\|get\|diff\|clear`, `intent?{blocks, rails, interfaces, nets, constraints}`, `merge?` | intent, counts; `diff`: missing / wrong_pin / extra / unmatched |
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
| `kw_pcb_setup` | `project`, `preset?`, `layers?`, `rules?`, `netclasses?`, `outline?`, `custom_rules?`, `drc?` | `rules_now`, `checks`, `drc`, `preset` |
| `kw_pcb_sync` | `project`, `remove_orphans?`, `keep_positions?`, `match?`, `force?`, `drc?`, `provider?` | `added` / `updated` / `removed` / `renamed`, `unresolved[]`, `checks`, `drc` (+ parity) |
| `kw_pcb_place` | `project`, `placements[{ref, at_mm, rotation?, side?, locked?}]`, `relative_to?`, `drc?`, `provider?` | `before` -> `after`, `items[]`, `footprints_now` |
| `kw_pcb_zones` | `project`, `zones[{net, layers, polygon_mm \| rect_mm, priority, ...}]`, `fill?`, `drc?`, `provider?` | `zones[]`, `filled`, `drc` |
| `kw_pcb_route` | `project`, `ops[{op: track \| via \| delete, ...}]`, `drc?`, `provider?` | `applied[]`, `touched_nets`, `drc` |
| `kw_pcb_route_auto` | `project`, `nets?`, `passes?`, `timeout_s?`, `lock_refs?`, `clean_dangling?`, `from_scratch?`, `engine?`, `provider?` auto\|cli\|codec\|swig, `run_async?`, `force?` | `job_id` (default), else `routed{tracks_added, vias_added, unrouted, unrouted_connections}`, `router{passes, history, score}`, `dsn`, `rules`, `drc`, `provider_used` |
| `kw_pcb_fix` | `project`, `actions?`, `max_iterations?`, `passes?`, `timeout_s?`, `provider?`, `run_async?`, `force?` | `job_id` (default), else `must_fix{before, after}`, `iterations[]`, `nets_touched`, `unfixable[]`, `drc` |
| `kw_pcb_get` | `project`, `kinds?`, `refs?`, `nets?`, `layers?`, `bbox_mm?`, `detail?`, `cursor?`, `limit?`, `provider?` | `data.items[]`, `data.outline`, `data.rules`, `data.connectivity` |
| `kw_pcb_check` | `project`, `checks?`, `refs?`, `nets?`, `bbox_mm?`, `cursor?`, `limit?`, `provider?` | `ok`, `summary`, `drc_summary`, `items[]` with `fix` |
| `kw_pcb_drc` | `project`, `schematic_parity?`, `refill_zones?`, `save_board?`, `severity?` | must_fix / review / cosmetic violations (mm) |
| `kw_pcb_render` | `project`, `view?`, `side?`, `layers?`, `max_px?` | PNG + summary (secondary) |
| `kw_fab_bom` | `project`, `profile?`, `include_dnp?`, `fields?`, `write?` | `rows[]` (refs, qty, value, footprint, lcsc, dnp, mpn, cost/stock null), `missing_lcsc`, `dnp_refs` |
| `kw_fab_export` | `project`, `targets?`, `profile?` generic\|jlcpcb, `include_dnp?`, `bom_fields?`, `out_dir?`, `clean?`, `force?`, `run_async?` | files per target (+ `bundle` zip), `positions.corrected[]`, `bom`, `dropped_options`, unsupported targets |
| `kw_live` | `action` launch\|attach\|status\|screenshot\|detach\|raise, `project?`, `session_id?`, `editor?` pcb\|sch\|project, `mode?` auto\|hidden\|mirror, `target_kicad?`, `helper?` {port, token, version}, `surface?`, `max_px?` | `session_id`, `pid`, `install`, `api_socket`, `token`, `editors[]`, `mode_used`, `cloaked`; screenshot: PNG + summary (for humans; secondary) |

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
| `E_FILE_LOCKED` | KiCad has the file open, or another kiwrap call runs (kiwrap's own headless servers and dead `kw_live` sessions, theirs or another server's, are released first) | close KiCad / wait |
| `E_BUSY` | one KiCad per project: a live `kw_live` session (yours or the extension's) already holds it, or a read is still in flight over IPC | use the session (`kw_live status` finds it) or `kw_live detach`; retry after the read |
| `E_GATE` | stage gate not met (`kw_pcb_sync` needs `erc_clean`, `kw_fab_export` needs `drc_clean`) | run the validator named in `fix`; `force={"reason"}` only when the user accepts the risk |
| `E_EXTERNAL_EDIT` | `if_match` no longer matches the sheet, or a file changed under a running operation | re-read (`kw_sch_get`, `data.sheets`) and retry; re-run validators |
| `E_CANCELLED` | `kw_job(action="cancel")` reached a running job (state `cancelled`, no error payload) | run the tool again; `kw_snapshot restore` for the pre-call state if needed |
| `E_TIMEOUT` | KiCad exceeded its time budget | `run_async=true` + `kw_job`, or filter/page the read |
| `E_INTERNAL` | kiwrap bug or git failure | report `data.traceback`; restore a snapshot |

## Where things are

- Tool catalog + design: `docs/ARCHITECTURE.md` (§6 planner, §7 schematic writer rules, §8
  placement/routing, §9 agent surface, §10 catalog, §11 receipts/gates).
- Decisions: `docs/DECISIONS.md` (D16 KiCad-11-first, D17 data-first, D18 Windows + VS Code).
- Verified KiCad facts: `docs/VERIFICATION.md`; the scripts behind them: `spikes/`.
- The flow skill: `.claude/skills/kiwrap-flow/SKILL.md`.
