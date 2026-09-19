---
name: kiwrap-flow
description: End-to-end KiCad flow through the kiwrap MCP server (kw_env -> kw_project create -> kw_sch_check -> kw_pcb_drc -> kw_fab_export), the stage gates, how to read receipts and how to act on teaching errors. Use whenever a task involves a KiCad project, schematic, board, ERC, DRC, gerbers or fabrication outputs.
---

# kiwrap flow (Phase 0)

KiCad is the judge. Every step below returns a **Receipt** (`structuredContent`):
`ok, changed{added,updated,removed}, validators[{name,before,after}], provider_used, degraded[],
snapshot, stage, next_suggested, warnings[], image_info, data{...}`. Never claim success without
the validator numbers from the receipt and, for boards, a render you looked at.

## 1. `kw_env`

Lists installed KiCads (newest first) with capability summaries. Pick the newest unless the user
pins a version. `kw_env(refresh=true)` after installing/removing KiCad.

## 2. `kw_project(action="create", dir, name, target_kicad?, board_size_mm?)`

Writes `<name>.kicad_pro`, an empty root `<name>.kicad_sch` and `<name>.kicad_pcb` stamped in the
target KiCad's format, initialises the snapshot history and stamps `target_kicad` into
`<dir>/.kiwrap/state.json`. Pass `board_size_mm=[w, h]` to get a rectangular outline (without one
DRC reports `invalid_outline`). `action="status"` returns stage, hashes, epochs, external edits and
`next_suggested`.

## 3. `kw_sch_check(project)`

kicad-cli ERC (positions in mm) + netlist summary. `data.ok` = zero errors. Fix errors from
`data.violations[*]` (each has `type`, `description`, `pos.x_mm/y_mm`, `items[*].uuid`), re-run
until ok. Passing -> stage `erc_clean`.

## 4. `kw_pcb_drc(project, schematic_parity?, refill_zones?)`

kicad-cli DRC classified into `must_fix` / `review` / `cosmetic`. `data.ok` = `must_fix == 0`.
Use `refill_zones=true` (KiCad 10+) when zones are stale. Passing -> stage `drc_clean`.
Then `kw_pcb_render(project)` (3d) or `view="2d"` and look at the image.

## 5. `kw_fab_export(project, targets=["gerbers","drill","pos","bom"])`

Gated on `drc_clean`. Files land in `<project>/fab/<target>/`; the receipt lists them. Unsupported
targets on the chosen KiCad come back in `data.unsupported` (the rest still export).
`run_async=true` returns a `job_id` for `kw_job`.

## Reading a receipt

| field | meaning | what to do |
|---|---|---|
| `stage` | highest validated milestone | compare with the gate you need |
| `degraded: ["external_edit"]` | a `.kicad_*` file changed outside kiwrap | re-run the validators; stop editing files by hand |
| `degraded: ["render_unavailable"]` | no rasteriser (`uv sync --extra render`) | use the numeric summary / the PDF path |
| `snapshot` | git sha of the auto-commit | `kw_snapshot(action="restore", ref=...)` undoes |
| `next_suggested` | the tool to call next | follow it unless the user says otherwise |

## Teaching errors (`isError: true`)

| code | meaning | do this |
|---|---|---|
| `E_NO_KICAD` | no install matches `target_kicad` | `kw_env`, then pick from `candidates` |
| `E_UNSUPPORTED` | capability missing on this KiCad / build | use `data.fallback` or a newer KiCad |
| `E_PROJECT_NOT_FOUND` | bad `project` path | pass the dir or `.kicad_pro`; create with `kw_project` |
| `E_BAD_ARGS` | schema/semantic argument error | follow `fix` / `example` |
| `E_KICAD_FAILED` | kicad-cli exited non-zero | read `data.stderr`; check epochs vs target |
| `E_FILE_LOCKED` | KiCad has the file open, or another kiwrap call runs | close KiCad / wait |
| `E_GATE` | stage gate not met | run the validator named in `fix`; `force={"reason"}` only when the user accepts the risk |
| `E_EXTERNAL_EDIT` | a file changed under a running operation | re-run validators |
| `E_TIMEOUT` | KiCad exceeded its time budget | `run_async=true` + `kw_job` |
| `E_INTERNAL` | kiwrap bug or git failure | report `data.traceback`; restore a snapshot |

`kw_help(topic=<code or tool>)` has the long form of every row.

## Rules

- Never edit `.kicad_*` files directly. Never guess library ids. Never route by hand.
- Positions are millimetres. Renders are <= 1024 px; ask for `view="2d"` + `layers` to isolate layers (there is no region crop in Phase 0).
- Do not call `kw_fab_export` with `force` to skip DRC unless the user explicitly accepts it.
