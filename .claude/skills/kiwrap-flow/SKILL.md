---
name: kiwrap-flow
description: End-to-end KiCad flow through the kiwrap MCP server (kw_env -> kw_project create -> kw_sch_stats/kw_sch_get -> kw_sch_check -> kw_pcb_check/kw_pcb_get -> kw_pcb_drc -> kw_fab_export), the data-first feedback protocol, the stage gates, how to read receipts and how to act on teaching errors. Use whenever a task involves a KiCad project, schematic, board, ERC, DRC, gerbers or fabrication outputs.
---

# kiwrap flow (data first)

KiCad is the judge. Every tool returns a **Receipt** (`structuredContent`):
`ok, changed{added,updated,removed}, validators[{name,before,after}], provider_used, degraded[],
snapshot, stage, next_suggested, warnings[], image_info, data{...}`. Never claim success without
the validator numbers from the receipt and a structured query of the result.

## The protocol in one line

**After every mutation, query structured state; never reason from an image when a structured
query exists.** Schematic: `kw_sch_stats` then `kw_sch_get`. Board: `kw_pcb_check` then
`kw_pcb_get`. Renders (`kw_sch_render`, `kw_pcb_render`) are secondary - for the human, not for
your decisions.

## 1. `kw_env(refresh?)`

Installed KiCads (newest first) with capability summaries. **KiCad 11 first**: prefer the newest
(10.99 nightly / 11.0), then 10.0.x. `kw_env(refresh=true)` after installing/removing KiCad.

## 2. `kw_project(action="create", dir, name, target_kicad?, board_size_mm?)`

Writes `<name>.kicad_pro`, an empty root `<name>.kicad_sch` and `<name>.kicad_pcb` stamped in the
target KiCad's format, initialises the snapshot history and stamps `target_kicad` into
`<dir>/.kiwrap/state.json`. Pass `board_size_mm=[w, h]` to get a rectangular outline (without one
DRC reports `invalid_outline`). `action="status"` returns stage, hashes, epochs, external edits and
`next_suggested`.

## 3. Schematic feedback: `kw_sch_stats` -> `kw_sch_get` -> `kw_sch_check`

- `kw_sch_stats(project, provider?, list_limit?)` - cheap numbers: `components`, `power_symbols`,
  `sheets`, `nets`, `single_node_nets`, `dangling_ends`, `unconnected_pins`, `label_only_nets`,
  `missing_footprint`, `per_sheet`; `data.lists` holds the offenders with `sheet` + `x_mm/y_mm`.
  `ok` = no dangling end, no unconnected pin, no label-only net. Fix from the lists, re-run.
- `kw_sch_get(project, kinds?, refs?, nets?, sheets?, bbox_mm?, detail?, cursor?, limit?, provider?)`
  - `kinds` default `["symbols", "hierarchy"]`; items are flat in `data.items` with `kind` =
  symbol | wire | label | junction | no_connect | sheet; `data.hierarchy` and `data.netlist`
  are singletons. A symbol carries `pins[]` with **absolute** `x_mm/y_mm`, `net` and `type` -
  use them to place wires and labels, never guess pin positions.
- `kw_sch_check(project, severity?, netlist?)` - KiCad ERC (positions in mm) + netlist summary.
  `data.ok` = zero errors. Fix errors from `data.violations[*]` (`type`, `description`,
  `pos.x_mm/y_mm`, `items[*].uuid`), re-run until ok. Passing -> stage `erc_clean`.
- `kw_sch_diff(a, b, kinds?, types?, refs?)` - KiCad-computed diff of two `.kicad_sch` files
  (KiCad 11 line); `data.changes[]` with `kind`, `type`, `ref`, `bbox_mm`, `properties` (positions
  also as `before_mm` / `after_mm`). On 10.x it is `E_UNSUPPORTED`: run `kw_sch_get` on both
  projects and compare the items yourself.

## 4. Board feedback: `kw_pcb_check` -> `kw_pcb_get` -> `kw_pcb_drc`

- `kw_pcb_check(project, checks?, refs?, nets?, bbox_mm?, cursor?, limit?, provider?)` - actionable
  items: `courtyard_overlap` (review), `off_board` (pad copper outside the outline = must_fix;
  a courtyard-only overhang = review with `courtyard_bbox_mm` / `outside_by_mm` - edge connectors
  do this by design and KiCad DRC has no rule for it), `unrouted` (info; pad pairs with mm
  endpoints), plus `drc_summary` (the last `kw_pcb_drc`, with `fresh` and `capped`). Each item
  has `refs`, `pos`, `description`, `fix`. `ok` = no copper off-board and no overlap, computed
  over the whole board before `refs` / `nets` / `bbox_mm` (`summary.matched` counts the filtered
  items). Call it after every placement change; it is cheap.
- `kw_pcb_get(project, kinds?, refs?, nets?, layers?, bbox_mm?, detail?, cursor?, limit?, provider?)`
  - `kinds` default `["footprints", "outline"]`; items with `kind` = footprint | pad | track |
  via | zone | net; `data.outline` (`bbox_mm`, `closed`), `data.rules` (min clearance / track
  width / via sizes, copper layers, netclasses), `data.connectivity` (unrouted pairs). Footprints
  carry `x_mm/y_mm`, `rotation_deg`, `bbox_mm`, `courtyard_bbox_mm`, `nets`; `detail="full"` adds
  pads and properties. Filter by `refs` / `nets` / `layers` / `bbox_mm` before paging.
- `kw_pcb_drc(project, schematic_parity?, refill_zones?)` - KiCad DRC classified into
  `must_fix` / `review` / `cosmetic`. `data.ok` = `must_fix == 0`. Use `refill_zones=true`
  (KiCad 10+) when zones are stale. Passing -> stage `drc_clean`.

## 5. `kw_fab_export(project, targets=["gerbers","drill","pos","bom"])`

Gated on `drc_clean`. Files land in `<project>/fab/<target>/`; the receipt lists them. Unsupported
targets on the chosen KiCad come back in `data.unsupported` (the rest still export).
`run_async=true` returns a `job_id` for `kw_job`.

## Reading a receipt

| field | meaning | what to do |
|---|---|---|
| `stage` | highest validated milestone | compare with the gate you need |
| `provider_used` | `ipc11:<v>` KiCad computed it (headless api-server); `file` kiwrap read the files; `cli:<v>` a kicad-cli export | trust ipc11/cli results as KiCad's; treat `file` estimates as hints to verify with the validator |
| `degraded: ["airwire_endpoints_estimated"]` | the unrouted count is KiCad's connectivity (ipc11); the pad pairs are nearest-anchor guesses | trust the count; route by net, not by the exact pair |
| `degraded: ["ratsnest_estimated"]` | unrouted pairs are kiwrap's estimate from the files (no KiCad ratsnest export yet) | confirm with `kw_pcb_drc` (`unconnected_items`) before/after routing |
| `degraded: ["netlist_estimated"]` | nets came from the file walk | `kw_sch_check` exports KiCad's netlist |
| `degraded: ["ipc11_unavailable"]` | the headless server failed; the file engine answered | fine to continue; mention it if results look off |
| `degraded: ["external_edit"]` | a `.kicad_*` file changed outside kiwrap | re-run the validators; stop editing files by hand |
| `degraded: ["render_unavailable"]` | no rasteriser (`uv sync --extra render`) | use the structured tools; renders are secondary anyway |
| `snapshot` | git sha of the auto-commit | `kw_snapshot(action="restore", ref=...)` undoes |
| `next_suggested` | the tool to call next | follow it unless the user says otherwise |
| `data.next_cursor` | more items on the read | call again with `cursor`, or narrow the filters |

`kw_help(topic="providers")` lists every operation's provider chain (ipc11 -> file -> cli) and
what each `degraded` tag means.

## KiCad 11 first - what changes on 10.x

The 10.99 nightly / KiCad 11 exposes `kicad-cli api-server` (headless IPC): `kw_pcb_get` /
`kw_sch_get` answer with `provider_used: ipc11:<version>` and pin nets straight from KiCad.
On 10.0.x the same tools read the files (`provider_used: file`), pin nets come from
`kicad-cli sch export netlist`, `kw_sch_diff` is unsupported, and the ratsnest is always an
estimate. The tool names, arguments and item shapes are identical; only the receipt's
`provider_used` / `degraded` differ - read them instead of asking which version is installed.

## Teaching errors (`isError: true`)

| code | meaning | do this |
|---|---|---|
| `E_NO_KICAD` | no install matches `target_kicad` | `kw_env`, then pick from `candidates` |
| `E_UNSUPPORTED` | capability / component missing on this KiCad or build | use `data.fallback` (names a kw_* tool) or a KiCad from `candidates` |
| `E_PROJECT_NOT_FOUND` | bad `project` path or missing design file | pass the dir or `.kicad_pro`; create with `kw_project` |
| `E_BAD_ARGS` | schema/semantic argument error (unknown `kinds` value, bad `cursor`, `bbox_mm` not 4 numbers, `provider` the operation lacks) | follow `fix` / `example` / `candidates` |
| `E_KICAD_FAILED` | kicad-cli / api-server exited non-zero | read `data.stderr`; check epochs vs target |
| `E_FILE_LOCKED` | KiCad has the file open, or another kiwrap call runs | close KiCad / wait |
| `E_GATE` | stage gate not met | run the validator named in `fix`; `force={"reason"}` only when the user accepts the risk |
| `E_EXTERNAL_EDIT` | a file changed under a running operation | re-run validators |
| `E_TIMEOUT` | KiCad exceeded its time budget | `run_async=true` + `kw_job`; narrow reads with filters |
| `E_INTERNAL` | kiwrap bug or git failure | report `data.traceback`; restore a snapshot |

`kw_help(topic=<code or tool>)` has the long form of every row.

## Rules

- Never edit `.kicad_*` files directly. Never guess library ids or pin positions (`kw_sch_get`
  gives absolute pin endpoints; `kw_pcb_get` gives pad positions). Never route by hand.
- Positions are millimetres. Filter (`refs`, `nets`, `bbox_mm`, `layers`, `sheets`) before you
  page; `limit` defaults to 200 items.
- Renders are <= 1024 px and secondary; `view="2d"` + `layers` isolates layers (no region crop).
- Do not call `kw_fab_export` with `force` to skip DRC unless the user explicitly accepts it.
