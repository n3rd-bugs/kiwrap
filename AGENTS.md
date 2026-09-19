# kiwrap - always-on rules for agents

kiwrap is an MCP server (`kiwrap serve`, registered in `.mcp.json` as `kiwrap`) that drives the
KiCad binaries installed on this machine. KiCad is the judge: validation comes from kicad-cli
ERC/DRC and structured exports, never from the model's own geometry.

## Data first (docs/DECISIONS.md D17)

The feedback loop is **structured data, not images**. After every mutation, query the state and
act on refs and millimetre coordinates:

| you changed | query next | then |
|---|---|---|
| a schematic | `kw_sch_stats` (counts, dangling ends, unconnected pins, label-only nets) and `kw_sch_get` (symbols with absolute pin endpoints + nets, wires, labels, netlist) | `kw_sch_check` (ERC) when stats are clean |
| a board | `kw_pcb_check` (courtyard overlaps, copper off the board = must_fix, courtyard overhang = review, unrouted pairs, last DRC) and `kw_pcb_get` (footprints/pads/tracks/vias/zones/nets/outline/rules) | `kw_pcb_drc` when `ok` (review items such as an edge connector's courtyard overhang do not block it) |
| two schematic versions | `kw_sch_diff` (KiCad-computed, KiCad 11 line) or `kw_sch_get` on both | - |

**Never reason from an image when a structured query exists.** `kw_sch_render` / `kw_pcb_render`
are secondary (human review, VS Code previews); a render never replaces `kw_*_get` / `kw_*_check`.

Filters and paging, per tool (the table below is exact): `kw_pcb_get` takes `refs` / `nets` /
`layers` / `bbox_mm`, `detail` (summary | normal | full) and `cursor` / `limit`; `kw_sch_get`
takes `refs` / `nets` / `sheets` / `bbox_mm`, `detail` and `cursor` / `limit`; `kw_pcb_check`
takes `refs` / `nets` (unrouted items only) / `bbox_mm` and `cursor` / `limit` (no `detail`, and
its `ok` / `summary` describe the whole board regardless of filters); `kw_sch_diff` takes
`kinds` / `types` / `refs` and `cursor` / `limit`; `kw_sch_stats` takes no filter (`list_limit`
caps its offender lists). `next_cursor` is null on the last page; a page closes early when it
would exceed ~40 KB (`page_chars`), so always follow `next_cursor` instead of assuming
`limit` items came back. Filter first; never page through a whole board when you need one net.

## KiCad 11 first (docs/DECISIONS.md D16)

The primary target is the newest KiCad (10.99 nightly today, 11.0 at GA); 10.0.x is co-primary.
The planner (`kw_help(topic="providers")`) tries, per operation, `ipc11` (headless
`kicad-cli api-server`, KiCad computes the answer) -> `file` (kiwrap's lossless file engine) ->
`cli` (kicad-cli exports). Read the receipt, not the version number:

- `provider_used`: `ipc11:<version>` = KiCad computed it; `file` = kiwrap computed it from the
  files; `cli:<version>` = a kicad-cli export.
- `degraded[]`: `airwire_endpoints_estimated` (the unrouted count is KiCad's connectivity, the
  pad pairs are nearest-anchor guesses - ipc11 on KiCad 11), `ratsnest_estimated` (unrouted pairs
  are kiwrap's file estimate - KiCad exposes no ratsnest export yet), `netlist_estimated` (netlist
  from the file walk, not KiCad),
  `ipc11_unavailable` / `cli_unavailable` (an earlier provider failed at call time),
  `external_edit` (a `.kicad_*` file was hand-edited), `render_unavailable`, `*_unavailable`.

What degrades on 10.x: boards and schematics are read from the files (`provider_used: file`),
pin nets come from `kicad-cli sch export netlist`, `kw_sch_diff` is `E_UNSUPPORTED`
(fallback: `kw_sch_get` on both files and compare), and the ratsnest is always an estimate.

## The stage protocol

```
kw_env -> kw_project(create) -> [edit] -> kw_sch_stats/kw_sch_get -> kw_sch_check
        -> [edit] -> kw_pcb_check/kw_pcb_get -> kw_pcb_drc -> kw_fab_export
stages: created -> sch_written -> erc_clean -> synced -> placed -> routed -> drc_clean -> exported
```

- `kw_sch_check` ok = zero ERC **errors** (warnings never gate) -> stage `erc_clean`.
- `kw_pcb_drc` ok = zero **must_fix** violations (clearance, shorting_items, tracks_crossing,
  unconnected_items, hole_clearance, net_conflict, track_width, any other error) -> `drc_clean`.
  `review` (courtyards_overlap, solder_mask_bridge, lib_footprint_issues) and `cosmetic` (silk_*)
  never gate.
- Gates (server-enforced): `kw_fab_export` needs `drc_clean`. The planned Phase 1 mutating tools
  (`kw_pcb_sync` needs `erc_clean`, `kw_pcb_route_auto` needs `placed`) are NOT in `tools/list`
  yet - do not call them; place and route in KiCad for now. Override a gate only with
  `force={"reason": "..."}`; the reason is written into the snapshot commit message.
- `kw_pcb_drc` counts can be LOWER BOUNDS: KiCad stops reporting a violation type after 199
  items (499 for clearance / unconnected_items); `data.capped` names the saturated types and
  `degraded` carries `drc_counts_capped` - fix the listed items and re-run.
- After every mutating step read the Receipt (`changed`, `validators`, `stage`, `next_suggested`,
  `degraded`), then query structured state before claiming success.

## Tools (in `tools/list` order) and their arguments

| tool | args | returns |
|---|---|---|
| `kw_env` | `refresh?` | installs (newest first), capabilities, engines, storage paths |
| `kw_project` | `action: create\|status`, `dir`, `name`, `target_kicad?`, `board_size_mm?`, `project?` | paths, epochs, stage, next |
| `kw_snapshot` | `project`, `action: list\|diff\|create\|restore`, `ref?`, `to?`, `message?` | sha / files / diff |
| `kw_job` | `job_id`, `action: status\|wait\|cancel\|list`, `wait_s?` | state, result |
| `kw_help` | `topic: overview\|stages\|errors\|providers\|kw_*\|E_*` | markdown |
| `kw_sch_get` | `project`, `kinds?` [symbols, wires, labels, junctions, no_connects, sheets, hierarchy, netlist], `refs?`, `nets?`, `sheets?`, `bbox_mm?`, `detail?`, `cursor?`, `limit?`, `provider?` | `data.items[]` (flat, `kind` per item), `data.hierarchy`, `data.netlist` |
| `kw_sch_check` | `project`, `severity?`, `netlist?` | ERC ok/errors/warnings/violations (mm), netlist summary |
| `kw_sch_stats` | `project`, `provider?`, `list_limit?` | components, power_symbols, sheets, nets, single_node_nets, dangling_ends, unconnected_pins, label_only_nets, missing_footprint, per_sheet, `lists` with positions |
| `kw_sch_diff` | `a`, `b` (.kicad_sch paths), `target_kicad?`, `kinds?`, `types?`, `refs?`, `cursor?`, `limit?` | `identical`, `counts`, `by_type`, `changes[]` {kind, type, ref, bbox_mm, properties (+ `_mm`), changed_properties} |
| `kw_sch_render` | `project`, `sheet?`, `page?`, `max_px?` | PNG + summary (secondary) |
| `kw_pcb_get` | `project`, `kinds?` [footprints, pads, tracks, vias, zones, nets, outline, rules, connectivity], `refs?`, `nets?`, `layers?`, `bbox_mm?`, `detail?`, `cursor?`, `limit?`, `provider?` | `data.items[]`, `data.outline`, `data.rules`, `data.connectivity` |
| `kw_pcb_check` | `project`, `checks?` [overlaps, off_board, unrouted, drc_summary], `refs?`, `nets?` (unrouted only), `bbox_mm?`, `cursor?`, `limit?`, `provider?` | `ok` (no copper off-board, no overlap; whole board, before filters), `summary` (+ `matched`), `drc_summary` (must_fix, fresh, capped), `items[]` {check, severity, refs, net, pos, bbox_mm, courtyard_bbox_mm, outside_by_mm, description, fix} |
| `kw_pcb_drc` | `project`, `schematic_parity?`, `refill_zones?`, `save_board?`, `severity?` | must_fix / review / cosmetic violations (mm) |
| `kw_pcb_render` | `project`, `view?`, `side?`, `layers?`, `max_px?` | PNG + summary (secondary) |
| `kw_fab_export` | `project`, `targets?`, `profile?`, `force?`, `run_async?` | files, unsupported targets |

`kw_help(topic=<tool>)` has the long form of every row, including the item shapes.

## Hard rules

- **Never edit `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` / `.kicad_sym` / `.kicad_mod` by hand.**
  kiwrap re-hashes the inputs of every stage; a hand edit downgrades the stage and adds
  `external_edit` to `degraded`. Use the kw_* tools.
- **Positions and sizes are millimetres** everywhere (ERC JSON positions from kicad-cli and the
  schematic diff's internal units are already converted).
- The user's real KiCad configuration (`%APPDATA%\kicad`, `~/.config/kicad`) is never written;
  every KiCad process (kicad-cli, the headless api-server) runs against a kiwrap-owned sandbox and
  every api-server a tool starts is stopped before the tool returns.
- Never launch a KiCad GUI from tests or tools.
- Errors come back as `isError` results with `{code, message, why, fix, candidates, example}`;
  `kw_help(topic="E_...")` explains every code. Do not retry blindly - apply `fix`.
- `kw_snapshot(action="restore", ref=<sha>)` is the universal undo; every mutating tool commits
  `[kiwrap] <tool> ...` automatically.

## Teaching errors

| code | meaning | do this |
|---|---|---|
| `E_NO_KICAD` | no install matches `target_kicad` | `kw_env`, then pick from `candidates` |
| `E_UNSUPPORTED` | capability / component missing on this KiCad or build | use `data.fallback` (e.g. `kw_sch_get` on both + compare) or a KiCad from `candidates` |
| `E_PROJECT_NOT_FOUND` | bad `project` path or a missing design file | pass the dir or `.kicad_pro`; create with `kw_project` |
| `E_BAD_ARGS` | schema/semantic argument error (unknown kind, bad `cursor`, 3-number `bbox_mm`, `provider` the operation lacks) | follow `fix` / `example` / `candidates` |
| `E_KICAD_FAILED` | kicad-cli or the api-server exited non-zero | read `data.stderr`; check epochs vs target |
| `E_FILE_LOCKED` | KiCad has the file open, or another kiwrap call runs | close KiCad / wait |
| `E_GATE` | stage gate not met | run the validator named in `fix`; `force={"reason"}` only when the user accepts the risk |
| `E_EXTERNAL_EDIT` | a file changed under a running operation | re-run validators |
| `E_TIMEOUT` | KiCad exceeded its time budget | `run_async=true` + `kw_job`, or filter/page the read |
| `E_INTERNAL` | kiwrap bug or git failure | report `data.traceback`; restore a snapshot |

## Where things are

- Tool catalog + design: `docs/ARCHITECTURE.md` (§6 planner, §9 agent surface, §10 catalog, §11 receipts/gates).
- Decisions: `docs/DECISIONS.md` (D16 KiCad-11-first, D17 data-first).
- Verified KiCad facts: `docs/VERIFICATION.md`; the scripts behind them: `spikes/`.
- The flow skill: `.claude/skills/kiwrap-flow/SKILL.md`.
