---
name: kiwrap-flow
description: End-to-end KiCad flow through the kiwrap MCP server (kw_env -> kw_project create -> kw_lib_search -> kw_sch_add_symbol / kw_sch_connect / kw_sch_add_item -> kw_sch_stats / kw_sch_get -> kw_sch_check -> kw_sch_annotate -> kw_sch_assign_footprints -> kw_pcb_setup -> kw_pcb_sync -> kw_pcb_place with kw_pcb_check / kw_pcb_get -> kw_pcb_zones (B.Cu plane) -> kw_pcb_route_auto (+ kw_job, kw_pcb_fix) -> kw_pcb_zones (F.Cu pour) -> kw_pcb_route touch-ups -> kw_pcb_drc -> kw_fab_bom / kw_fab_export(profile="jlcpcb")), the data-first feedback protocol, the stage gates, how to read receipts and how to act on teaching errors. Use whenever a task involves a KiCad project, schematic, board, ERC, DRC, placement, routing, gerbers or fabrication outputs.
---

# kiwrap flow (data first)

KiCad is the judge. Every tool returns a **Receipt** (`structuredContent`):
`ok, changed{added,updated,removed}, validators[{name,before,after,detail}], provider_used,
degraded[], snapshot, stage, next_suggested, warnings[], data{...}`. Every mutating tool runs
its validator before it returns (ERC for schematic tools, DRC / parity / placement checks for
board tools) and the numbers are in the receipt. Never claim success without them and a
structured query of the result.

## The protocol in one line

**Write with the kw_* tools, read the receipt's validators, query structured state, fix from
refs + mm, repeat.** Schematic: `kw_sch_stats` then `kw_sch_get`. Board: `kw_pcb_check` then
`kw_pcb_get`. Renders (`kw_sch_render`, `kw_pcb_render`) are secondary - for the human, not for
your decisions. Never guess a library id or a pin position; never edit `.kicad_*` files by hand.

`kw_live` is for humans watching; the agent loop stays data-first. If the person asks to see
KiCad while you work, `kw_live(action="launch", project=...)` opens a sandboxed KiCad GUI bound
to the project (a normal window on the desktop, never activated; the extension streams it live
into the file's tab and the person edits by hand in the real window through `raise` / the tab's
"Raise KiCad" - keep the default `mode="view"`; `hidden` is an experimental, parked path).
`kw_pcb_place` moves, `kw_pcb_zones` and
`kw_pcb_route` commit through it; a rotation, `kw_pcb_route_auto`, `kw_pcb_fix` and a saved
refill write the file and the GUI reloads it - `data.live_view.reloaded` says so), `screenshot`
answers "what does it look like", and `detach` closes it when they are done. A `pcbnew` session
serves the board only: schematic reads and writes come from the file engine meanwhile
(`degraded: [ipc11_unavailable]`), never an error. The extension may have started the session
from its own server: `kw_live(action="status", project=...)` finds it (`remote: true`), your
writes go through it, and `launch` on that project answers E_BUSY while it lives. It changes
nothing about how you verify: `kw_pcb_check` / `kw_pcb_get` remain the feedback loop.

## 1. `kw_env(refresh?)` and `kw_project(action="create", dir, name, target_kicad?, board_size_mm?)`

Installed KiCads (newest first) with capability summaries. **KiCad 11 first**: prefer the newest
(10.99 nightly / 11.0), then 10.0.x. `kw_project create` writes `<name>.kicad_pro`, an empty root
`<name>.kicad_sch` and `<name>.kicad_pcb` in the target's format and starts the snapshot history;
pass `board_size_mm=[w, h]` for a rectangular outline (or set it later with `kw_pcb_setup`).
`action="status"` returns stage, hashes, epochs, external edits and `next_suggested`.

## 1b. `kw_intent(action="set", intent={blocks, rails, interfaces, nets, constraints})`

Declare the design before drawing it: `nets` = `{"GND": [["U1", "1"], "C1.2", ...], "VIN": [...]}`
(the pins you mean to connect; local-label nets are matched to `/NAME`, unnamed ones by their
pins), `constraints` = `{size_mm, layers, fab: "jlcpcb", strict_nets: false}`. Stored in
`<project>/.kiwrap/intent.json`; every `kw_sch_check` from then on adds `validators.intent_diff`
and `data.intent_diff` = `missing[]` (declared pin not on its net, `found_in` = where it is),
`wrong_pin[]` (same ref, other pin - a label on the wrong pin, which ERC cannot see), `extra[]`
(counted only with `strict_nets`), `unmatched[]`. Done = 0 ERC errors AND an empty diff.
`kw_intent(action="diff")` runs it alone; `merge=true` (default) updates per key.

## 2. Parts: search before you place

`kw_lib_search(kind="symbol" | "footprint", query, filters?, limit?)` and
`kw_lib_info(id)` (when listed) give real `lib_id`s (`Device:R`, `power:GND`,
`Regulator_Linear:AMS1117-3.3`) and `fpid`s (`Resistor_SMD:R_0603_1608Metric`) with pin / pad
counts and footprint filters. A guessed id costs an `E_BAD_ARGS` / `lib_symbol_issues` round trip.

## 3. Schematic authoring

1. `kw_sch_add_symbol(project, lib_id, at_mm, ref?, value?, rotation?, mirror?, unit?, fields?,
   footprint?, sheet?, erc?)` - returns `data.uuid`, `data.ref` and **`data.pins[]` with absolute
   `x_mm/y_mm`** (the verified 12-orientation transform; you never compute pin geometry). Place
   parts on a 2.54 mm rhythm (`at_mm` snaps to 1.27 mm) and leave room for wires. Use
   `erc=false` while batch-placing; the first symbol alone is `pin_not_connected` until wired.
2. `kw_sch_connect(project, endpoints=[{ref, pin} | {at_mm}], style, net?, route?, sheet?, erc?)`
   - `style="wire"` (default) draws L-routed wires between consecutive endpoints (`route`
   hv | vh | direct; intermediate `{at_mm}` points are corners), split at pins and junction
   sites, snapped to the grid, junctions added; `net` adds a local label on the first endpoint.
   The corner is not obstacle-aware: a route over another pin is refused (E_BAD_ARGS names the
   pin) - use the other route or explicit `{at_mm}` corners; `data.connected_pins` lists pins
   the wire ends on besides the endpoints.
   `style="label"` / `"global_label"` puts a label `net` on every endpoint (the way to join
   distant pins without long wires); `style="power"` puts a power symbol `net` on every endpoint.
3. `kw_sch_add_item(project, item={type, ...}, sheet?, erc?)` - `power` (`net` at `at_mm` or on
   `ref` + `pin`), `pwr_flag` (a PWR_FLAG on every power-input net nothing drives: regulator
   outputs, VBUS - otherwise ERC `power_pin_not_driven`), `no_connect` (every unused pin),
   `label` / `global_label` / `hierarchical_label` (`text`; `kind` = the ERC shape input |
   output | bidirectional | tri_state | passive - give a hierarchical label the kind of its
   sheet pin), `sheet` (`text` name, `file`, `at_mm`, `size_mm`, `pins[{name, side, kind}]` -
   hierarchical sheets get their own file and instance path; the receipt's `data.pins[]` carries
   the sheet pins' absolute `x_mm/y_mm` to wire to), `sheet_pin`, `text`. Address a child sheet
   in every schematic tool with `sheet="<file stem>"` (`"blinker"` for `blinker.kicad_sch`); a
   net through a sheet pin is named after the child's hierarchical label (`/blinker/BLINK`).
4. Feedback: the receipt's `data.erc` (ok / errors / warnings / violations with `type`,
   `pos.x_mm/y_mm`, `items[].uuid`), then `kw_sch_stats` (`dangling_ends`, `unconnected_pins`,
   `label_only_nets`, `missing_footprint` with positions) and `kw_sch_get` (symbols with pins +
   nets, wires, labels, netlist). `kw_sch_check` is the formal gate: `data.ok` = zero ERC errors
   -> stage `erc_clean`. Note: a power symbol placed on a pin (`style="power"`) connects in KiCad
   (the netlist proves it) even if the file estimate lists that pin as unconnected.
5. `kw_sch_edit(project, ops=[...], if_match?, sheet?, erc?)` - `move` (`at_mm`), `rotate`,
   `mirror`, `property` (`name` + `value`), `value`, `footprint`, `dnp`, `delete` by `ref`
   (symbols) or `uuid` (any item; `kw_sch_get` lists uuids). All-or-nothing. Moving a wired symbol
   breaks the wires - reconnect with `kw_sch_connect`. `if_match` = the sheet's sha256 from an
   earlier receipt's `data.sheets`; a mismatch is `E_EXTERNAL_EDIT` (re-read, retry).
6. `kw_sch_annotate(project, scheme="global" | "sheet", start_per_sheet?, reset?)` gives every
   `R?` (and duplicate) a number (`data.mapping`); references you set yourself are kept unless
   `reset=true` (KiCad's default); then `kw_sch_assign_footprints(project, mapping?, rules?, overwrite?,
   suggest?)` - `rules=[{ref_prefix | lib_id | value, fpid}]` for the bulk, `mapping={ref: fpid}`
   for the rest; `data.unresolved[]` carries `suggestions[]` from the library index (the symbol's
   footprint filters) - pick one and call again. `ok` = nothing unresolved -> `kw_pcb_sync`.

## 4. Board authoring

1. `kw_pcb_setup(project, preset?, layers?, rules?, netclasses?, outline?, custom_rules?, drc?)` -
   `preset="jlcpcb-2layer"` / `"jlcpcb-4layer"` writes the vendor's safe rules and layer count in
   one go (explicit keys override; `data.preset` echoes it); or copper
   layer count / names, `rules={clearance_mm, track_width_mm, via_size_mm, via_drill_mm,
   min_track_mm, min_via_mm, min_hole_mm, hole_clearance_mm, copper_edge_clearance_mm, ...}`
   (`kw_help(topic="kw_pcb_setup")` lists them all; an unknown key is `E_BAD_ARGS` with the
   list), `netclasses={"Power": {"nets": ["GND", "+3V3", "/VIN"], "track_width_mm": 0.5}}` (into
   the .kicad_pro; local-label nets are `/<label>`), `outline={"w": 50, "h": 30}` (+ `x`, `y`
   origin) or `{"points_mm": [[x, y], ...]}` on Edge.Cuts, `custom_rules` (.kicad_dru). Do it
   before the sync so the outline exists.
2. `kw_pcb_sync(project, remove_orphans?, keep_positions?, force?, drc?)` - gated on `erc_clean`.
   KiCad's own netlist drives it: footprints instantiated from the fp-lib-table with pad nets,
   values / fields / dnp propagated, existing positions kept, orphans removed, **new parts placed
   off-board in a grid**. Receipt: `data.added / updated / removed`, `data.unresolved[]`
   ({ref, fpid, reason} - fix with `kw_sch_assign_footprints`), `data.checks` (off-board /
   overlaps for the new parts), `data.drc` with `parity`. Stage `synced`.
3. `kw_pcb_place(project, placements=[{ref, at_mm, rotation?, side?, locked?}], relative_to?,
   drc?)` - the receipt carries `courtyard_overlap`, `off_board_copper`, `off_board`,
   `unrouted` as validators (before -> after), `data.items[]` = the overlap / off-board items
   involving the moved refs (with `fix`) and `data.footprints_now` (position / rotation / layer /
   bbox_mm read back). Loop: place -> read the deltas -> `kw_pcb_check` (whole board: `ok`,
   `summary`, `items[]` with refs + mm + fix) -> `kw_pcb_get` (`kinds=["footprints", "pads",
   "outline"]`, `refs`, `bbox_mm`) for geometry -> place again. `placed` = no copper off-board
   and no courtyard overlap (courtyard overhang of an edge connector is `review`, fine).
   Placement heuristics: decoupling caps within 2 mm of their IC pin, connectors on the edge,
   crystals next to the MCU, keep the ratsnest short (`kw_pcb_check` lists `unrouted` pairs with
   `length_mm`).
4. `kw_pcb_zones(project, zones=[{net, layers, rect_mm | polygon_mm, priority, ...}], fill?,
   drc?)` - filled copper (GND pour on B.Cu, power islands) or keepouts (`keepout=true`); the
   fill runs through kicad-cli (KiCad 10+) or RefillZones (KiCad 11); `data.drc` follows.
   **Pour order for the autorouter:** the B.Cu GND plane BEFORE `kw_pcb_route_auto`, the F.Cu
   pour AFTER it (a both-sides pour first makes the router think every top GND pad is
   connected; no pour first turns GND into track connections the router stalls on).
4b. `kw_pcb_route_auto(project, passes?, timeout_s?, nets?, lock_refs?, clean_dangling?,
   from_scratch?, provider?)` - gated on `placed`, DESTRUCTIVE: every unlocked track / via of
   the routed nets goes to the router (`nets=[...]` routes ONLY those nets - the others leave
   the DSN network and their copper is locked as obstacles; `lock_refs` keeps the nets of those
   footprints as they are). A re-run on a partially routed board keeps the existing unlocked
   copper as wiring the router only optimises (same plateau): use `kw_pcb_fix` or
   `from_scratch=true` to make progress. Default `run_async=true`: the receipt is `{job_id}`
   (the gate, unknown nets and missing engines are refused before queueing) - poll
   `kw_job(job_id, action="wait", wait_s=30)` until `data.done`, then read `data.job.result` as
   the receipt (`result.ok == result.data.ok`; kw_help(topic="kw_job") has the shape):
   `data.routed` (`tracks_added`, `vias_added`, `unrouted` = KiCad's unconnected count after the
   mandatory refill, `unrouted_connections` = KiCad's open connections `{net, from: {ref, pad,
   x_mm, y_mm}, to}`),
   `data.router` (`passes`, per-pass `history`, `score`, `stop_reason`), `data.drc`
   (`must_fix` / `review` / `unconnected`), `data.dsn` (nets, classes, planes exported),
   `data.rules` (what the router was told: minimum widths / clearances, fanout off unless BGA,
   copper-to-edge from the board rule), `provider_used` (`codec` = kiwrap's DSN/SES codec on
   stock KiCad; `cli:<v>+specctra-jobs` on the patched build), `next_suggested`. When
   `must_fix > 0` or `unrouted > 0`: `kw_pcb_fix(project, max_iterations?, passes?,
   timeout_s?)` (a job too, gated on `placed`) rips up the nets named in the must_fix
   violations (all of them, not only the 50 listed), cleans stubs, re-routes only those nets and
   repeats until `must_fix == 0`; read `data.must_fix{before, after}`, `data.iterations[]`,
   `data.unfixable[]` (violations naming no net: a pad-to-pad hole clearance, a courtyard - not a
   routing problem); its receipt carries one `drc` delta, initial -> final. `E_UNSUPPORTED` with
   a bootstrap hint means the managed JRE / Freerouting are not installed (`kw_env` lists
   `engines`); `E_TIMEOUT` = raise `timeout_s` or route a subset - its `why` says whether the
   board is unchanged or which stubs / ripped-up copper are gone (`data.restore_ref` is the
   snapshot to restore); a cancelled job ends `cancelled`, never `failed`.
5. `kw_pcb_route(project, ops=[{op: "track", points_mm, net, layer?, width_mm?} | {op: "via",
   at_mm, net, layers?, size_mm?, drill_mm?} | {op: "delete", uuid}], drc?)` - touch-ups, not
   whole-board routing. Get pad positions from `kw_pcb_get(kinds=["pads"], refs=[...])` and route
   pad centre to pad centre with Manhattan corners; the net name must be a board net (local
   labels are `/<label>`; the error lists candidates), layers must be copper. The receipt's
   `drc_touched_nets` validator and `data.drc.violations` (touched nets first, uuid + mm) say what
   the op did: `tracks_crossing` / `shorting_items` = wrong geometry, `unconnected_items` = not
   done yet, `clearance` = too close.
6. `kw_pcb_drc(project, schematic_parity?, refill_zones?)` - KiCad's word: `must_fix` /
   `review` / `cosmetic`, `data.ok` = `must_fix == 0` -> stage `drc_clean`. Copper zones are
   refilled (and saved) before the check by default when the KiCad can (`zones_stale` in
   `degraded` when it cannot; track-vs-zone clearance items are then `review`). Fix from
   `data.violations[]` (`type`, `class`, `pos`, `items[].uuid`) with `kw_pcb_place` /
   `kw_pcb_route` (`delete` the offending uuid, route again) and re-run.

## 5. Fabrication: `kw_fab_bom` and `kw_fab_export(profile="jlcpcb")`

1. `kw_fab_bom(project, profile="jlcpcb")` at any time (not gated): `data.rows[]` = {refs, qty,
   value, footprint, package, lcsc, dnp, mpn, manufacturer, fields, cost: null, stock: null}
   grouped by value + footprint + LCSC (+ DNP apart). `data.missing_lcsc` are fitted parts without
   an `LCSC Part #` symbol field - set it (`kw_sch_add_symbol(fields={"LCSC Part #": "C25804"})`
   / `kw_sch_edit`) before ordering assembly. Source is kicad-cli's `sch export bom` (`data.source`
   = cli), the netlist on a KiCad without it (`degraded: bom_from_netlist`).
2. `kw_fab_export(project, profile="jlcpcb", targets=["gerbers","drill","pos","bom"])` - gated on
   `drc_clean`. Files land in `<project>/fab/<target>/`; `data.files` lists them per target plus
   `bundle` = `<project>/fab/<name>-jlcpcb-gerbers.zip` (gerbers + drill: what the order form
   takes). Read `data.positions.corrected[]` (every CPL row whose rotation / offset the JLCPCB
   table changed: {ref, package, pattern, rotation_kicad, rotation_out}) and tell the user to
   confirm those in JLCPCB's order preview; `data.bom.missing_lcsc` repeats the BOM gap;
   `data.dropped_options` names vendor flags this KiCad lacks (nothing is guessed). Unsupported
   targets come back in `data.unsupported` (the rest still export). `profile="generic"` keeps
   kicad-cli's defaults. `run_async=true` returns a `job_id` for `kw_job`.

## Reading a receipt

| field | meaning | what to do |
|---|---|---|
| `stage` | highest validated milestone (`created -> sch_written -> erc_clean -> synced -> placed -> routed -> drc_clean -> exported`) | compare with the gate you need |
| `validators[]` | `erc` (errors), `drc` (must_fix; detail review / cosmetic / unconnected), `parity`, `drc_touched_nets` (by_net), `courtyard_overlap`, `off_board_copper`, `off_board`, `unrouted` - each `before -> after` | a delta that went the wrong way is your last op |
| `provider_used` | `ipc11:<v>` KiCad did it (headless api-server commit / read); `file` kiwrap's lossless writer / reader; `cli:<v>` a kicad-cli run | trust KiCad's numbers; treat `file` estimates as hints to verify with the validator |
| `degraded: ["ipc11_unavailable"]` | the headless server could not serve; the file engine answered | fine to continue; mention it if results look off |
| `degraded: ["ipc11_guarded"]` | the G13 hang guard kept a multi-unit-symbol write off IPC (docs/UPSTREAM.md G13) | nothing to do; the file engine wrote it |
| `degraded: ["erc_skipped"]` | you passed `erc=false` | run `kw_sch_check` (or the next authoring call with `erc=true`) before relying on the stage |
| `degraded: ["ratsnest_estimated" / "airwire_endpoints_estimated"]` | unrouted pairs are kiwrap's estimate / KiCad's count with guessed ends | confirm with `kw_pcb_drc` (`unconnected_items`) |
| `degraded: ["netlist_estimated"]` | nets came from the file walk | `kw_sch_check` exports KiCad's netlist |
| `degraded: ["external_edit"]` | a `.kicad_*` file changed outside kiwrap | re-run the validators; stop editing files by hand |
| `snapshot` | git sha of the auto-commit | `kw_snapshot(action="restore", ref=...)` undoes |
| `next_suggested` | the tool to call next | follow it unless the user says otherwise |
| `data.sheets` | sha256 per sheet file | the `if_match` for `kw_sch_edit` |
| `data.next_cursor` | more items on the read | call again with `cursor`, or narrow the filters |

`kw_help(topic="providers")` lists every operation's provider chain (ipc11 -> file -> cli) and
what each `degraded` tag means; `kw_help(topic=<tool>)` the long form of every tool.

## KiCad 11 first - what changes on 10.x

On the 10.99 nightly / KiCad 11 the writes go through the headless api-server
(`provider_used: ipc11:<version>`: PlaceSymbolFromLibrary, CreateItems, UpdateItems, ImportNetlist,
RefillZones - one undo step each) and reads answer with KiCad's own connectivity. On 10.0.x the
same tools write through kiwrap's lossless CST writers (`provider_used: file`) and kicad-cli
validates; `kw_sch_diff` is unsupported and the ratsnest is an estimate. Tool names, arguments and
receipts are identical; only `provider_used` / `degraded` differ - read them instead of asking
which version is installed.

## Teaching errors (`isError: true`)

| code | meaning | do this |
|---|---|---|
| `E_NO_KICAD` | no install matches `target_kicad` | `kw_env`, then pick from `candidates` |
| `E_UNSUPPORTED` | capability / component missing on this KiCad or build (e.g. `sheet_pin` needs `pins` on the sheet item) | use `fix` / `data.fallback` or a KiCad from `candidates` |
| `E_PROJECT_NOT_FOUND` | bad `project` path or missing design file | pass the dir or `.kicad_pro`; create with `kw_project` |
| `E_BAD_ARGS` | schema/semantic error: unknown pin (candidates = the pins), unknown ref (candidates = the refs), a net or layer the board lacks, a `?` reference, `force` without a reason | follow `fix` / `example` / `candidates` |
| `E_KICAD_FAILED` | kicad-cli / api-server exited non-zero | read `data.stderr`; check epochs vs target |
| `E_FILE_LOCKED` | KiCad has the file open, or another kiwrap call runs | close KiCad / wait |
| `E_BUSY` | a live `kw_live` session already holds the project (one KiCad per project), or a read is in flight over IPC | use that session or `kw_live detach`; retry after the read |
| `E_GATE` | `kw_pcb_sync` needs `erc_clean`; `kw_fab_export` needs `drc_clean` | run the validator named in `fix`; `force={"reason"}` only when the user accepts the risk |
| `E_EXTERNAL_EDIT` | `if_match` no longer matches the sheet / a file changed under an operation | re-read (`kw_sch_get`, `data.sheets`) and retry; re-run validators |
| `E_TIMEOUT` | KiCad exceeded its time budget | `run_async=true` + `kw_job`; narrow reads with filters |
| `E_INTERNAL` | kiwrap bug or git failure | report `data.traceback`; restore a snapshot |

## Rules

- Never edit `.kicad_*` files directly. Never guess library ids (`kw_lib_search`) or pin / pad
  positions (`kw_sch_add_symbol` / `kw_sch_get` give absolute pin endpoints; `kw_pcb_get` gives
  pad positions). Never route whole boards by hand with `kw_pcb_route`; it is for touch-ups.
- Positions are millimetres. Filter (`refs`, `nets`, `bbox_mm`, `layers`, `sheets`) before you
  page; `limit` defaults to 200 items.
- Read every receipt's `validators` and `stage`; act on `next_suggested`.
- Do not call `kw_pcb_sync` / `kw_fab_export` with `force` to skip a gate unless the user
  explicitly accepts it.
