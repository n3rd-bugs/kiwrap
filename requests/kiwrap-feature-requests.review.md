# Review: kiwrap feature requests from the PR Repeater V2.0 review

Reviewed 2026-09-23 against `main` @ 1f69ed4. This synthesises six cluster reports. Each cluster
reproduced its items through the real MCP server (`uv run kiwrap --storage-home <scratch> serve` plus
an stdio client) on scratch copies of the KiCad demos (kit-dev-coldfire, video, ecc83, pic_programmer,
One-Air-Max, RoyalBlue54L), or proved them from code. They used stock kicad-cli 10.0.6 and 10.99.0
where that was cheap. No test touched `%APPDATA%\kicad`, no test launched a GUI, and no api-server was
left running. The reviewed board itself was not available, so board-specific numbers (0.044 mm, U8,
F1) are the doc's own, and the review reproduced the mechanisms behind them on the demos.

## 1. Verdict

**Urgent: item 1 is real, and it is worse than the doc says.** kiwrap chooses the KiCad from
`state.target_kicad`, and only `kw_project create` sets that field (`core/tools/project.py:151`).
Every project kiwrap did not create therefore runs on `installation.find(None)`, which returns
`installs[0]`, the newest install (`installation.py:333-335`). On this machine that is the 10.99
nightly. Every save path then writes 10.99-format files into a KiCad 10.0 project. The review
reproduced four such paths:
- `kw_pcb_drc` with its defaults: board `20260206` became `20260901`, and kicad-cli 10.0.6 then
  exits 3 with "created with a more recent version".
- Every ipc11 write (`kw_pcb_place(provider='ipc11')`, a no-op move): the board was up-converted.
- `kw_sch_edit(provider='ipc11')`: both sheets were up-converted, including a child sheet the call
  never touched.
- `kw_sch_edit(provider='file')`: the root sheet was re-stamped and the child was not, leaving a
  mixed-epoch project that 10.0.6 cannot load.

No receipt mentions any of this. Recovery works (`kw_snapshot restore` returned the file
byte-identical), but only if the user knows to use it. The code also breaks ARCHITECTURE §6 ("a
project pins `target_kicad`… never emit an uncertified epoch"). A second irreversible write was
found beside it: kicad-cli 10.0.x rewrites an **existing** `.kicad_prl` even on runs that should
not write, and kiwrap neither snapshots nor restores that file (`backends/cli/runner.py:250-253`).

**Where the doc is right:** the P0 framing of item 1, the missing data in `kw_pcb_get` (zones,
graphics, stackup, pad and footprint detail are dropped even where both readers already hold the
data), the DRC position problem (item 18), and the lack of an analysis layer (items 25-39). Its KiCad
claim about stroke inflation (item 9) is correct.

**Where the doc is wrong or overstated:**
- Item 20 already exists: every violation carries a `class` field.
- The item 6 cursor bug does not reproduce, and current code cannot produce it. There is a real
  cursor bug next to it that silently skips unrouted airwires.
- Item 7 blames the wrong trigger: the file is deleted by the *next render*, not by server exit.
- "Every call made a snapshot" (item 2) is true only for tools wrapped in `mutation()`.
- `missing_courtyard` and `footprint_type_mismatch` are **KiCad 10 defaults** set to ignore, not a
  designer's choice. `footprint_type_mismatch` never fires on footprints typed "unspecified", so
  ignoring it hid none of the "30 wrongly typed parts".
- Item 31's keepout miss is KiCad behaviour: rule areas do not govern graphic shapes.
- Item 35 cites IPC-2152, which has no closed form. The usable formula is IPC-2221.
- Item 37 `field_parity` is already reported by `kw_pcb_drc(schematic_parity=true)`, as text.
- Item 38: layer lists already work with `view='2d'`. The real resolution loss is a full-sheet PDF
  page, and it happens before the 1024 px cap.

Section 5 has the evidence for each.

**Priorities after review:**
- P0: item 1 only.
- Down from P0: items 2-4. Once item 1 is fixed, nothing left in them is irreversible except the
  `.kicad_prl` rewrite, which is fixed unconditionally with item 1.
- Up from P2 to P1: items 25, 26, 30, 31, 32, 37 (paste_coverage) and 38. They carry the review's
  real value and are affordable with zero-dependency geometry plus "KiCad as the instrument" (§6).

Counts: 6 bug-confirmed, 16 gap-confirmed, 16 partially-exists, 1 claim-incorrect, 0 already-exists
as a whole item (many *parts* exist; see §4), 0 bug-not-reproduced as a whole item (item 6's
sub-claim did not reproduce).

## 2. Triage table

Effort: S < 0.5 d, M 0.5-2 d, L 2-5 d, XL > 5 d.

| # | Title | Verdict | Doc → reviewed | Effort | One-line design | Depends |
|---|---|---|---|---|---|---|
| 1 | DRC refill-save up-converts the board | bug-confirmed | P0 → **P0** | L | `effective_target()` (pin, else install whose `formats.*` == file stamp); `save_board` default false; ipc11 write precondition epoch==caps; stamp guard in `mutation()` epilogue → `E_EPOCH_UPGRADE`; `kw_project retarget/upgrade` | none |
| 2 | Read-only mode | gap-confirmed | P0 → P1 | L | `serve --readonly` / `KIWRAP_READONLY=1` + `persist=false` on validators; new `detached()` context; outputs under `<storage>/results/`; `E_READONLY` | 1 |
| 3 | `kw_pcb_drc_report` re-read | partially-exists | P0 → P2 | M | No new tool: `kw_pcb_check(checks=['drc'], types, classes, refs, bbox_mm, cursor)` over a stored full result keyed by board sha | 1, 2 |
| 4 | `kw_fab_export` for reviewers | partially-exists | P0 → P2 | M | `out_dir` exists; add `layers`, `dry_run` (plan + `positions_preview.excluded[{ref,reason}]` + `bom_diff`), `positions.excluded` on real runs | 2 |
| 5 | One-shot CLI `kiwrap call/tools` | gap-confirmed | P1 → P2 | M | `core/cli_call.py`: in-process `build_server` + `mcp.call_tool`; `@file`/stdin args; `--all-pages`, `--out`; force `run_async=false`; refuse `kw_live` launch | 6 |
| 6 | `kw_pcb_get` paging | partially-exists (doc's bug not reproduced; adjacent real bug) | P1 → P1 | M | `out_path`/`format='ndjson'`; composite cursor `i<n>.u<n>@<sha8>`; one budget for items+airwires; compact JSON text body | 2 |
| 7 | Render PNG disappears | bug-confirmed (wrong trigger in doc) | P1 → P2 | S | Per-call dir keyed by name+sha8(path), retention sweep, `out_path`, `png_sha256` | none |
| 8 | Zones: fills, settings, teardrop, fp zones | partially-exists | P1 → P1 (island contents P2) | L | `zone_type`, `parent_ref`, `fill_area_mm2`; `settings{}`/`keepout{}` at full; `zone_geometry='fill'` with `formats.geometry.unfracture()`; `include_footprint_zones` | 6 |
| 9 | Graphics kind | gap-confirmed | P1 → P1 | M | `kinds=['graphics']`, `{shape, layer, net, fill, stroke_width_mm, inflate_mm, owner_ref}`; `graphic_scope='copper'` default | none |
| 10 | Outline null on ipc11 | bug-confirmed | P1 → P1 | M | Build rings for both providers with `chain_outline`+`nest_rings`; `rings[{role, points_mm}]`, `primitive_count` | 9 |
| 11 | Stackup kind | partially-exists | P1 → P1 | S (M with plane nets) | Singleton `data.stackup` from `formats.reports.stackup`; planner `stackup` op gains ipc11+file; fix ipc11 copper-layer order | 8 (plane) |
| 12 | Pad detail | partially-exists (+oval-drill bug) | P1 → P1 core / P2 margins | M | Normal detail: `rotation_deg`, `drill_shape`, `drill_size_mm`. Full detail: `pin_function`, `shape_params`, resolved `mask`/`paste`/`zone_connection`. Opt-in `pad_geometry` | none |
| 13 | Footprint detail, `properties {}` | bug-confirmed | P1 → P1 (props/flags) / P2 / P3 | M | Unpack IPC Field items; flags `exclude_from_pos`, `board_only`, `net_tie_pad_groups`, jumpers; `courtyard_mm`; parity via item 37 | 17, 37 |
| 14 | Nets: pad_count/netclass null | bug-confirmed | P1 → P1 (fields) / P2 (pad list) | M | Group pads by net → `pad_count`; file-side netclass resolver; `netclass` into normal KEEP; `code` documented null on 10-format | 13 |
| 15 | Rules: mask/paste, severities | gap-confirmed | P1 → P1 | M | `rules.mask`, `rules.severities` (+`severities_source`), `rules.custom_rules` via `formats.dru`; DRCET→JSON-key table as data | 23 |
| 16 | Vias near a point | partially-exists | P1 → P2 | M | `near`/`near_items`, `k`, `metric='edge'|'center'`; new zero-dep `formats/geometry/index.py` | none |
| 17 | Schematic `pin_context` | partially-exists | P1 → P2 | M | `kw_sch_get` kind `pin_context`, `pins=['U8.13']`, `depth`, `through='passives'`; BFS over existing netlist | 13, 14 |
| 18 | DRC location | bug-confirmed | P1 → P1 | L | Tier A: description parser → `rule`, `required_mm`, `actual_mm`, `layer`, rename `pos`→`anchor_pos`. Tier B: `formats/analysis/locate.py` closest points. Tier C: upstream draft | 9, 8 |
| 19 | `all_track_errors` | partially-exists | P1 → P1 | S | Expose `all_track_errors` on `kw_pcb_drc` (default true there, false in inner validators) | none |
| 20 | Bucket per violation | claim-incorrect | P1 → P3 | S | Already `class`; move it first, document it, fix `violations_truncated`, list `excluded[]` | 3 |
| 21 | By-design hints | gap-confirmed | P1 → P2 | M | `hints[{code:'net_tie_candidate'|'duplicate_pad_group', message, fix}]`, never reclassify; fix `_PAD_ITEM` regex | 18, 13 |
| 22 | Padstack "no paste" cross-check | gap-confirmed | P1 → P2 | L | Consume item 37 paste model; `hint{code:'paste_cross_check', coverage_ratio}` | 8, 12, 37 |
| 23 | `ignored_checks` in DRC | gap-confirmed | P1 → P1 | S | Surface `report.raw.ignored_checks` + `included_severities`; warn on must-fix types; optional `audit_ignored` on a temp copy | none |
| 24 | Fab-capability audit | partially-exists | P1 → P2 | L | `FabLimits` per (vendor, layers, oz); `kw_pcb_drc(fab_audit='jlcpcb-4layer')` on a temp copy with generated `.kicad_dru`; separate `data.fab_audit` | 19, 23, 18, 1, 2 |
| 25 | return_path | gap-confirmed | P2 → **P1** | L | `kw_pcb_analyze(checks=['return_path'])`: split segment at adjacent-plane fill-boundary crossings after unfracture | 8, 11, 26 |
| 26 | plane_slots | gap-confirmed | P2 → **P1** | M | Unfractured plane holes with ≥2 drills or aspect > 2, plus crossing traces | 25, 8 |
| 27 | Stitching family | gap-confirmed | P2 → P2 | L | Vector edge-fence gaps + zero-dep raster kit (`gnd_reach`, `islands`, `pour_necks`) | 8, 25, 32 |
| 28 | copper_path / net_topology | partially-exists | P2 → P2 | L | Per-net Dijkstra over tracks/vias/pads (+ fills via raster) | 9, 8, 27 |
| 29 | loop_area | gap-confirmed | P2 → P2 | L | Ideal shoelace (S, early) + real via item 28 legs, labelled estimate | 28, 25 |
| 30 | pad_return / exposed_pad | partially-exists | P2 → **P1** | M | Nearest same-net via, ring-sampled spokes, EP via-in-pad, opposite-side blockers | 8, 9, 12 |
| 31 | measure / region / keepout_audit | partially-exists | P2 → **P1** | M | `backends/cli/drc_probe.py`: temp-copy DRC with generated `kw_probe_*` rules (`physical_clearance`, `disallow graphic`) | 9, 8 |
| 32 | probe_via / via_spots | gap-confirmed | P2 → **P1** | M | Insert candidate vias in the probe copy, judge by KiCad DRC | 31 |
| 33 | coupling | gap-confirmed | P2 → P2 | M | Grid-indexed segment pairs: parallel length and min gap; NEXT labelled estimate | 11 |
| 34 | diffpair / rf_line | partially-exists | P2 → P2 (rf_line P1 on antenna boards) | L | Geometry first; `formats/analysis/transline.py` written from papers (MIT) | 11, 33, 27, 30 |
| 35 | current_capacity | gap-confirmed | P2 → P3 | M | IPC-2221 along a copper_path bottleneck, mΩ | 28, 27, 11 |
| 36 | interfaces / esd_entry | gap-confirmed | P2 → P2 | M | Template composed from items 28 + 30 (+25) | 28, 30, 25 |
| 37 | Assembly checks | partially-exists | P2 → P1 (paste_coverage) / P2 / P3 (field_parity) | M | Paste/mask from KiCad Gerber (X2) or IPC-2581 on a copy; structured `field_parity` from parity DRC text | 4, 12, 8, 31 |
| 38 | Render crop / px_per_mm / mirror | partially-exists (+page-size bug) | P2 → **P1** | S | `bbox_mm` (default outline+1 mm), `px_per_mm`, `mirror`, auto-2D; pdfium crop at `--scale 1` | 7 |
| 39 | Render overlays | gap-confirmed | P2 → P2 | M | Annotations on a spare user layer in a scratch copy; `kw_live` `highlight`/`markers` via IPC HighlightNets/InjectDrcError | 38, 27 |

## 3. Confirmed bugs: evidence and fix

### B1 (item 1): design files are up-converted to the newest install's format (P0)
- **Evidence.** kit-dev-coldfire re-saved by 10.0.6 (`version 20260206`, sha 63c9e98f); called
  `kw_project status` then `kw_pcb_drc` with its defaults.
  - Status reported `target_kicad=null`, `epochs.pcb=20260206`, and no warning.
  - DRC ran on `cli:10.99.0` with `{refill_zones:true, save_board:true}`, which leaves the board at
    `(version 20260901)(generator_version "10.99")`.
  - `kicad-cli` 10.0.6 then fails, rc 3.
  - Restore by pre-call snapshot gives back the byte-identical original.
- **All paths.** Every one takes the newest install when `target_kicad` is null:
  - (a) Default refill+save in `kw_pcb_drc` (`core/tools/checks.py:396-419` → `backends/cli/drc.py:124-127`).
    Reproduced.
  - (b) `drc_validator`'s `save_refill = do_refill and m.bound is None` (`core/tools/_author.py:672`),
    reached from `kw_pcb_place(drc=true)`, `kw_pcb_route`, `kw_pcb_zones fill`,
    `kw_pcb_route_auto` and `kw_pcb_fix`.
  - (c) Every ipc11 write ends in SaveDocument (`_author.py:227-253`, `backends/ipc/pcb_writes.py:602`).
    The planner's ipc11 predicate never checks the file epoch (`core/planner.py:77-110`).
    Reproduced on a board and on a two-sheet schematic.
  - (d) The file schematic writer stamps `epoch_for(caps)` and re-stamps touched sheets upward
    (`_author.py:190-194`, `formats/schematic_writer/writer.py:1528-1532`). Reproduced: a
    mixed-epoch project. The board writer keeps the file's dialect (`board_writer.py:1042`),
    verified.
  - (e) SES import through the patched CLI on a bench build (`pcb_route_auto.py:421-456`).
  - (f) `kw_live launch` picks the newest GUI (`core/tools/live.py:186-189`), and the extension's
    `kiwrap.kicadInstall` defaults to newest (EXTENSION.md:307). Proven from code only.
- **Fix.** Three layers, all keyed on capabilities.
  1. **`effective_target(ctx, project, state)`** in `core/tools/_base.py` replaces the ~15
     `ctx.services.install(state.target_kicad)` sites (`checks.py:54` `install_for`,
     `fab_bom.py:139`, `fab_export.py:320`, `intent.py:445`, `live.py:188`, `pcb_author.py:64`,
     `pcb_check.py:279`, `pcb_get.py:888`, `render.py:96/214`, `sch_author.py:93`,
     `sch_get.py:609`, `sch_stats.py:124`). It resolves in this order:
     - the pinned target;
     - else the newest point release whose `formats.pcb`/`formats.sch` **equal** the file stamps;
     - else the nearest newer epoch, marked read-only for writes;
     - else `E_EPOCH`.
     The result is persisted with a `target_source` on the first mutation. `kw_project status`
     shows `target_kicad_effective`, `target_source` and `epoch_match`. Add
     `kw_project(action='retarget')` and implement `action='upgrade'` (on a copy, semantic diff,
     then replace) as the only sanctioned up-conversion.
  2. **Preventive defaults.**
     - `kw_pcb_drc.save_board` defaults to false. kicad-cli refills in memory without
       `--save-board`, verified to leave the board byte-identical with the same DRC result.
     - `save_refill` in `drc_validator` and explicit saves require epoch equality, else
       `E_EPOCH_UPGRADE` or degraded `refill_not_saved_epoch`.
     - An ipc11 write precondition checks that the document epoch equals caps `formats.<kind>`.
       If not, the write falls back to file with degraded `ipc11_epoch_mismatch`.
     - Writers stamp `min(caps epoch, file epoch)`.
     - `kw_live launch` defaults to the effective target, and the extension default becomes
       `project`.
  3. **Guard.** The `mutation()` prologue records each design file's `(version N)`, reading about
     4 KB per file. `_finish` compares the stamps. If one rose without `allow_format_upgrade=true`,
     it restores from `m.baseline` and raises `E_EPOCH_UPGRADE {file, stamp_before, stamp_after,
     kicad, fix}`, and gets a `kw_help` page. Skip the restore and only warn when a live session
     is bound. Every save receipt carries `data.epochs {before, after}`.
- **Tests.** Unit tests with a fake Services per path. A `kicad_version('10.0')` conformance test
  runs every mutating `kw_*` over a 10.0 demo copy with 10.99 installed and asserts the stamps are
  unchanged and that 10.0.6 still loads the files.
- **Interim for users today:**
  - Pass `save_board=false` to `kw_pcb_drc`.
  - Pass `provider='file'` for board writes.
  - Avoid schematic writes on non-kiwrap projects.
  - The only way to pin a target on an existing project is to hand-edit `target_kicad` in
    `.kiwrap/state.json`. It works (DRC ran on 10.0.6 and kept `20260206`), but it is unsupported.

### B2: an existing `.kicad_prl` is rewritten and cannot be restored (P0, fixed with item 1)
kicad-cli 10.0.6 rewrote a user `.kicad_prl` during `kw_pcb_drc(refill_zones=false)`: the sha
changed and KiCad's defaults were merged in. The runner deletes only a `.prl` that the run created
(`backends/cli/runner.py:250-253`), and `.kicad_prl` is excluded from snapshots (ARCHITECTURE §6).
**Fix:** `run_kicad_cli` snapshots the bytes of every pre-existing sibling `.kicad_prl` and restores
them afterwards, in all modes. Check the editor lock (`~*.lck`) before restoring.

### B3 (item 6, adjacent): the shared cursor skips unrouted airwires (P1)
`pcb_get.py:961-977` pages `connectivity.unrouted` with the **same** cursor and its own budget, then
sets `next_cursor = items_cursor or airwire_cursor`. `sch_get.py:703-713` does the same with
`netlist.nets`. A simulation with `chunk_by_budget` stopped items at 92 and airwires at 43, so
airwires 43-91 are never returned. Routing can look complete when it is not. **Fix:** an opaque
composite cursor `i<off>.u<off>` that still accepts a bare int, one shared budget, and
`next_cursor=null` only when both lists are exhausted. Add `@<sha8>` so a board edit between pages
raises `E_BAD_ARGS`.

### B4 (item 7): the render directory is wiped on every render (P2)
`render_dir()` runs `shutil.rmtree` on `<storage>/tmp/render/<project.name>/<kind>` at the start of
every render (`core/tools/render.py:34-35`). The directory is keyed by name, and the storage home
is shared by the extension, agents and the CLI.

Reproduced:
- a bottom render deleted the top PNG;
- a second copy with the same name deleted the first copy's PNG;
- the PNG survived server exit.

Concurrent agents can race kicad-cli or pdfium mid-write. `backends/cli/structured.py:151-156` has
the same pattern.

**Fix:**
- per-call directories under `<name>-<sha8(path)>/<kind>/<ts>-<rand>/`;
- `sweep_renders()` keeps the newest 20 per project and kind, deletes anything older than 24 h,
  and skips directories younger than 60 s;
- optional `out_path`;
- receipt gains `png_sha256` and `kept_until`.

### B5 (item 10): outline `closed`/`segments` are null on ipc11 (P1)
The IPC `BoardModel` has only `outline_bbox` (`backends/ipc/readers.py:537,562`), so `norm_outline`
takes the bbox-only branch (`pcb_get.py:294-296`). The file provider is also misleading:
`segments` = 198 **polygonised vertices** against 58 real primitives (`pcb_get.py:312`). The ipc11
reader also misses footprint Edge.Cuts. **Fix:** build `rings[]` for both providers with
`formats.geometry.chain_outline` + `nest_rings`, feeding IPC shapes plus footprint
`definition.items`. Add a file-vs-ipc11 parity test on ecc83, One-Air-Max and RoyalBlue.

### B6 (item 13): footprint `properties` are `{}` on ipc11 (P1)
`FootprintModel` has no properties field (`backends/ipc/models.py:141-168`), and `_footprint_model`
ignores the Field items in `definition.items` (`readers.py:257-291`). One-Air-Max holds 207 of
them, 193 of which are 'LCSC Part'. The file provider returns them.

`SymbolModel` has the same gap for schematic fields (`models.py:296-315`), so `kw_sch_get`
returns `fields {}` on ipc11.

**Fix:**
- Unpack Field items and synthesise the `Reference`/`Value`/... keys the way the file reader does.
- Map symbol `USER_FIELDS`/`CUSTOM_PROPERTIES`.
- Expose the fab flags both readers already hold.

### B7 (item 14): `pad_count` is always null and `netclass` is hidden or null (P1)
Neither `Net(code, name)` (`formats/board.py:284-288`) nor `NetModel` (`models.py:234-236`) carries
pads, so `norm_net` (`pcb_get.py:279-287`) emits null for fields it advertises at normal detail.

On ipc11, `netclass` is correct but is pruned out of `KEEP['net']['normal']` (`pcb_get.py:117-120`).
On the file provider nothing resolves it.

**Fix:**
- Group pads by net in `board_items` (`pcb_get.py:468-496`).
- Add a file-side netclass resolver: assignments, then patterns, then Default, with
  `netclass_estimated` for regex patterns.
- Move `netclass` and `pad_count` into normal detail.
- `code` is null on 10-format boards by design (see §5).

### B8 (item 18): the DRC `pos` is the first item's anchor, not the violation location (P1)
`Violation.pos = items[0].pos` (`formats/reports/__init__.py:42-44`), emitted at `checks.py:96-105`.
kicad-cli's JSON writes `item->GetPosition()` for each item. That is ZONE corner 0, a polygon's
first vertex, or a track's start (KiCad `common/rc_item.cpp:163-215`, `zone.cpp:565-571`,
`eda_shape.cpp:526-534`).

Reproduced on ecc83 with injected copper: the polygon's first vertex was reported, and an added
zone was reported at its first corner, which lies off the board. That is the doc's "off-board
starved thermal". The same anchors inflate `kw_pcb_fix`'s rip-up box (`pcb_route_auto.py:764-797`)
and feed the VS Code diagnostics.

**Fix:**
- Tier A (S): a zero-dep description parser gives `rule`, `required_mm`, `actual_mm` and `layer`,
  plus `anchor_pos`, with `pos` kept as a deprecated alias.
- Tier B (M-L): `formats/analysis/locate.py` computes `closest_points_mm` and `location_mm` from
  file geometry, cross-checked against KiCad's actual value at a 0.005 mm tolerance.
- Tier C: a draft upstream patch; see §6.

### Smaller confirmed defects found while reproducing
| Where | Defect | Fix |
|---|---|---|
| `_data.py` `drill_mm()` (item 12) | `(drill oval 0.6 1.7)` comes back as `drill_mm 0.6` on both providers | `drill_shape`, `drill_size_mm [x,y]` |
| `pcb_get.py:335` (item 11) | ipc11 `rules.copper_layers` is in enum order (F, B, In1, In2) | sort by `layer_sort_key` |
| `backends/cli/render.py:121-125` (item 38) | `--page-size-mode 2` does not exist on 10.0/10.99 `pcb export pdf`, so the whole sheet is rasterised: about 2.4-3.4 px/mm on the demos | crop to board bbox via pdfium |
| `sch_get.py:535`, `backends/cli/netlist.py:41` (item 2) | `kw_sch_get(kinds=['netlist'])` is READ_ONLY but leaves `<root>.net` (319 KB) in the project | explicit `out` under storage |
| `sch_get.py:613-615` (item 17) | `kw_sch_get(kinds=['netlist'], provider='cli')` gives E_BAD_ARGS although the op has a cli provider | route the netlist op separately from `schematic.read` |
| `checks.py:443-444,468` (item 20) | `violations_truncated` counts excluded markers; exclusions and their comments are dropped | count non-excluded; add `excluded[]` |
| `pcb_route_auto.py:817` (item 21) | `_PAD_ITEM` misses KiCad 10's `PTH pad 2 [GND] of R4` / `NPTH pad of X` | `(PTH \|NPTH \|SMD )?[Pp]ad` |
| `checks.py:49` (item 1) | comment says validators "never touch design files"; `kw_pcb_drc` rewrites the board | fix with save default; honest annotations |
| `core/state/receipts.py:125-129` + `_data.py:482` (item 6) | the 40k budget is measured at indent 0; the rendered body is about 25% larger (48.6-49.7k) | measure what is sent, or compact JSON (-30%) |

## 4. What already exists: the exact call today

| Need (item) | Call |
|---|---|
| DRC without writing the board (1, 3) | `kw_pcb_drc(project, refill_zones=true, save_board=false)`: refills in memory; board byte-identical; still writes `.kiwrap` state + snapshot |
| Last DRC summary without re-running (3) | `kw_pcb_check(project, checks=['drc_summary'])`: must_fix count, timestamp, `fresh` vs board sha |
| Raw full DRC report (3) | `<board>.drc.json` beside the board (left by every run) |
| Bucket per violation (20) | `kw_pcb_drc(...).data.violations[i].class` ∈ must_fix / review / cosmetic (sorted in that order) |
| Fab outputs outside the project (4) | `kw_fab_export(project, targets=['pos','gerbers'], profile='jlcpcb', out_dir='<abs path>')`. Still a mutation (state + snapshot + stage `exported`) and gated on `drc_clean` unless `force={'reason':...}` |
| Render any layer list (38) | `kw_pcb_render(project, view='2d', layers=['In1.Cu','F.Paste','F.Mask','Edge.Cuts'])` (`layers` is silently ignored with the default `view='3d'`) |
| Footprint properties / MPN / LCSC (13, 14) | `kw_pcb_get(project, kinds=['footprints'], detail='full', provider='file')` |
| Pad list with values on a net (14) | `kw_pcb_get(project, kinds=['footprints'], nets=['Net-(U4-Vc)'], detail='full', provider='file')` |
| Netclass per net (14) | `kw_pcb_get(project, kinds=['nets'], detail='full', provider='ipc11')` |
| Pin neighbours (17) | `kw_sch_get(kinds=['symbols'], refs=['U8'])` → pin net; then `kw_sch_get(kinds=['symbols'], nets=['<net>'])`; repeat per hop |
| Vias in a radius (16) | `kw_pcb_get(kinds=['vias'], nets=['GND'], bbox_mm=[x-r, y-r, x+r, y+r])`, sort client-side |
| Outline closed flag (10) | `kw_pcb_get(kinds=['outline'], provider='file')` (`segments` is a vertex count) |
| Teardrop zones (8) | name `$teardrop_padvia$` in `kw_pcb_get(kinds=['zones'])` |
| PCB vs schematic field diff (37) | `kw_pcb_drc(project, schematic_parity=true)` → `footprint_symbol_field_mismatch` / `footprint_symbol_mismatch` violations (text only) |
| Isolate renders per agent (7) | give each agent its own `kiwrap --storage-home <dir> serve` |
| Report all track errors (19) | not reachable from MCP; the backend has it (`backends/cli/drc.py:85,125-126`) |

## 5. Claims that are incorrect or overstated

1. **Item 20: "the bucket can only be inferred from counts."** `checks.py:445-452` sets
   `d['class']` on every violation. Verified over MCP. It is easy to miss because the key is last,
   undocumented, and falls past the 60k text cut in text-only clients.
2. **Item 2: "every call made a pre-call snapshot".** The project tree was diffed before and after
   calls:
   - `kw_project status`, `kw_pcb_get` and `kw_pcb_check` wrote nothing.
   - `kw_sch_stats`, `kw_pcb_render` and `kw_fab_bom` wrote only an empty `.kiwrap/tmp`.
   - Only `mutation()` tools (`kw_pcb_drc`, `kw_sch_check`, `kw_fab_export`, `kw_project`) write
     state and snapshots. The pre-call snapshot happens only when the files differ from the last
     snapshot (`_base.py:452-460`).
3. **Item 6: "text said cursor=180 while `data.next_cursor` was null".** The two values come from
   the same `Page` object (`pcb_get.py:912-928`). `shortened=True` implies a non-null cursor
   (`_data.py:514-529`). The code has been unchanged since 7e2dcbf, which is what the reviewers
   ran. The likely cause is on the client side, such as a truncated text or a hand transcription.
   The char-budget observation itself is correct: 181/179 tracks, 183/182 vias.
4. **Item 7: "gone once the server exited".** The PNG survives exit. The next render, from any
   process that shares the storage home and uses the same project name, deletes it (B4).
5. **Item 4: "30 unspecified-type SMD parts" dropped from the CPL.** The jlcpcb profile uses
   `smd_only=False, exclude_dnp=True` (`core/fab/jlcpcb.py:151-159`), so unspecified parts are
   **not** dropped by kiwrap's CPL. They are dropped only with `--smd-only`.
6. **Items 15 and 23: "`missing_courtyard` and `footprint_type_mismatch` were ignored, hiding … 30
   wrongly typed parts".**
   - Both are KiCad 10.0 default ignores (`board_design_settings.cpp:173-212` in the 10.0.0 tag).
     The ecc83 `.kicad_pro` lists only one ignore, yet KiCad reports three.
   - `footprint_type_mismatch` never fires for a footprint whose attribute is neither SMD nor THT.
     This was verified on 10.99: with the attr removed, no violation; with `attr smd` on a THT part,
     a violation.
   - Unspecified parts were therefore not hidden by the ignore. They need a kiwrap check (item 37
     `assembly_outputs`).
   - The `missing_courtyard` claim stands.
   - Note that `.kicad_pro` `rule_severities` is incomplete by design: 63 keys in the file against
     77 over IPC on pic_programmer.
7. **Item 12: "`size_mm` is footprint-local".** It is the pad's own unrotated size, and pad rotation
   in both readers is absolute (RoyalBlue U1: footprint -90°, pad 270°, identical on both).
8. **Item 8: "filled polygons with holes (not fractured)".** KiCad serves fills fractured on
   **both** paths: all 298 RoyalBlue IPC `PolygonWithHoles.holes` lists were empty. kiwrap must
   unfracture. A prototype recovers holes with zero area error on 4 demos.
   - "`plotted:false`" for footprint F.Paste zones is unverified KiCad semantics. Derive it from a
     real paste plot (item 37).
9. **Item 11: "plane net per layer".** This is not a KiCad stackup property. Derive it from zones
   and the layer type.
10. **Item 14: "`code` was null".** That is correct for KiCad 10-format boards, which no longer
    store net codes; the kipy `Net.code` is deprecated. Document it rather than fix it.
11. **Item 18.** KiCad *does* compute the marker position, layer and closest-point pair
    (`drc_test_provider.cpp:101-121`, since 10.0), but `RC_JSON::VIOLATION` does not serialise
    them. The request asks for more than kicad-cli exports, not more than KiCad knows.
12. **Item 19: "which one varied between runs".** This is expected. Without `--all-track-errors`
    each track stops at its first violation (`drc_test_provider_copper_clearance.cpp:289,304,668`),
    and the order depends on the R-tree and the worker threads. Reproduced: the default run showed
    12 violations; with the flag there were 18, including a **short** the default hid.
13. **Item 24: `profile='jlcpcb-4L'`.** The preset is `jlcpcb-4layer` (`core/fab/profiles.py:245`),
    and it holds kw_pcb_setup safe defaults, not fab minimums. The doc's fab numbers are right
    (0.044 mm < 0.09 mm JLC multilayer minimum).
14. **Item 31: "VSYS polygon covers a keepout without any DRC report".** This is KiCad behaviour.
    Rule-area keepouts do not govern graphic shapes, verified on 10.0.6. The custom rule
    `(constraint disallow graphic)` with `intersectsArea` does catch it.
15. **Item 35: "ΔT per IPC-2152".** IPC-2152 is chart-based. KiCad's calculator and the usable
    closed form are IPC-2221 (`I = k·ΔT^0.44·A^0.725`).
16. **Item 37 `field_parity`.** KiCad's `--schematic-parity` already reports
    `footprint_symbol_field_mismatch`, as in "Field 'Datasheet' differs (PCB: …, Schematic: …)".
    The F1 LCSC mismatch should have appeared in `kw_pcb_drc(schematic_parity=true)`. What is
    missing is structure and filtering.
17. **Item 38: "the 1024 px cap makes 0.2 mm features unreadable".** The larger loss is the
    full-sheet PDF page (B-table above). Layer lists already work in 2D.
18. **Correct claims worth recording:**
    - Item 1's failure message is exact.
    - Item 9's stroke/2 inflation is exact: KiCad's own 0.348 mm = 0.475 raw - 0.127.
    - Item 21's net-tie suggestion matches KiCad's convention: stock antenna footprints ship
      `net_tie_pad_groups`, and with it the same-footprint shorts disappear.

## 6. Cross-cutting themes that change the architecture

**T1: Target resolution and epoch safety become a server-wide policy.** ARCHITECTURE §6 already
states the rule ("pin `target_kicad` … never emit an uncertified epoch"). The code has no resolver
for existing projects, and `kw_project upgrade` raises `E_UNSUPPORTED` (`project.py:231`).

D16 ("KiCad 11 first") should be read as *what kiwrap develops against*, not *what it writes into
a user's 10.0 project*. §6 also contradicts `kw_project create`: the doc says "default: the oldest
KiCad the user wants", while create defaults to the newest.

The owner should restate the policy, and the code should enforce it in three places:
- one `effective_target()`;
- an epoch precondition in the planner for every write provider;
- the stamp guard in the `mutation()` epilogue, which covers write paths not yet written, such as
  new ipc twins, bench-build CLI patches and GUI saves.

The stamp guard is the only layer that catches the unknown.

**T2: A third execution context, `detached()`, alongside reads and `mutation()`.**
- State lives in memory: no snapshot, no stage change, no history.
- The lock lives under the storage home. It must still honour an existing `<project>/.kiwrap/lock`,
  because the extension and the agent share it (EXTENSION.md §6).
- Every kicad-cli output goes to `<storage>/results/<sha1(project dir)>/`.
- It backs `serve --readonly` (item 2), `persist=false` on validators, the DRC result store
  (item 3), `dry_run` (item 4) and all of `kw_pcb_analyze`.
- It pairs with one `resolve_out_path(arg, kind, project)` helper in `_base.py`, shared by items 4,
  5, 6 and 7. Allow-rules: absolute path, extension whitelist, never `*.kicad_*`, never KiCad config
  dirs, parent must exist, and `'auto'` = `<storage>/exports/…`.

The helper also delivers the `resource_link for large artifacts` that §11 promises but nothing
implements (grep finds none).

**T3: A temp-copy KiCad runner ("KiCad as the instrument").**
`backends/cli/drc_probe.py` next to `run_drc` does the following:
- copies the board, `.kicad_pro` and `.kicad_dru` to a storage scratch directory;
- appends generated `kw_probe_*` rules, never replacing the project's rules;
- sets unrelated severities to ignore;
- refills and saves **on the copy**;
- maps the violations back.

Verified on 10.0.6:
- `physical_clearance(min 50mm)` with `memberOfFootprint` selectors returns the exact edge-to-edge
  `actual` for any item pair, same-net pairs included;
- `disallow graphic` catches copper graphics in keepouts.

One runner serves items 18 (fresh fills for locating), 23 (`audit_ignored`), 24 (fab audit),
31, 32, 27 (legal fill-in) and 37 (paste/mask plots). It also sidesteps item 1 for analysis: a
newer KiCad may bump the copy's epoch, but the tolerant reader reads it and the project is never
touched. DRC expression functions vary by version, so gate them with capability keys (e.g.
`cli.drc_expr.intersectsArea`).

**T4: One provider-parity pass.** The ipc11 reader already fetches most of items 8-15 and then
discards it:
- zone type and settings;
- shape net;
- `exclude_from_pos`;
- `pin_name`;
- pad polygons;
- Field items;
- net ties;
- oval drill `(x, y)`;
- severities and `solder_mask_paste`.

Footprint-owned items (zones, copper graphics, Edge.Cuts) exist only in
`FootprintInstance.definition.items` on ipc11, and the file parser ignores footprint zones. One pass
over `backends/ipc/readers.py`, `models.py`, `formats/board.py` and the `pcb_get.py` `norm_*`
functions closes most of it:
- a shared `definition.items` unpacker;
- the footprint-frame rule `STAMP_FP_AFFINE_TRANSFORM` (board frame before 20260616);
- an audit of the `KEEP` tables;
- a file-vs-ipc11 parity test on ecc83, One-Air-Max and RoyalBlue54L.

A single "effective board setup" normaliser should combine the file setup, `.kicad_pro`, IPC
`BoardDesignRules` (with severities) and `.kicad_dru`. It then feeds items 15, 23 and 24 from one
source.

**T5: A zero-dependency geometry engine, and `kw_pcb_analyze`.** No shapely, numpy or pyclipper is
needed:
- Vector kit in `kiwrap.formats.geometry`:
  - `unfracture()`;
  - segment-segment distance with closest points;
  - capsule, disc and polygon distances;
  - segment clipping against polygons;
  - Sutherland-Hodgman;
  - min-area rectangle;
  - a grid index `index.py` that generalises `ratsnest._FillIndex`.
- Raster kit `formats/geometry/raster.py`: bytearray masks, scanline fill, BFS and chamfer distance
  transform, and components.
- Measured on coldfire In1.Cu (157×91 mm): raster plus multi-source BFS takes 1.7 s at 0.1 mm and
  5.9 s at 0.05 mm, so enforce a cell budget, window by bbox, and use `run_async`.

The `kiwrap.formats` import rule holds. Add **one** read-only tool, `kw_pcb_analyze(project,
checks[], refs?, nets?, layers?, bbox_mm?, params{<check>:{…}}, provider: auto|kicad|file|ipc11,
cursor, limit, run_async)`. Its items use the `kw_pcb_check` shape plus an `overlay` object that
`kw_pcb_render` draws (item 39). `degraded` tags include `fills_stale`, `stackup_defaulted`,
`raster_resolution` and `estimate`. Every fill-based check must report fill freshness, and a refill
happens only on a copy.

**T6: Catalog budget.** 33 tools today (`core/tools/__init__.py`), budget 36 (ARCHITECTURE §10).
The review adds only `kw_pcb_analyze` (34th). The rest fold into existing tools:
- `kw_pcb_drc_report` becomes `kw_pcb_check(checks=['drc'])`;
- `measure` and `probe_via` become checks inside `kw_pcb_analyze`;
- `pin_context` becomes a `kw_sch_get` kind;
- `graphics` and `stackup` become `kw_pcb_get` kinds;
- live highlight and markers become `kw_live` actions.

That leaves two slots for the parts, library and report tools still planned in §10.

**T7: DRC text parsing, locale, and an upstream draft.** kicad-cli's JSON has no marker, layer,
rule or numeric fields, so descriptions are kiwrap's only source (items 18, 21, 37 and `kw_pcb_fix`).

- Put one parser in `kiwrap.formats.reports`, backed by a per-version description corpus (8.0.9,
  9.0.9, 10.0.6, 10.99).
- The sandbox `kicad_common.json` (`sandbox.py:626-662`) does not pin the UI language, so a
  non-English Windows would break the parsing. Pin English there. That is a contract change to
  `sandbox.py`, and it still never touches the user config.
- Upstream (D17 tier 3, drafts only per D19): about a 40-line `DRC_ITEM::GetJsonViolation` override
  that adds `marker{pos, layer, path}` would make item 18 exact. Gate it with the capability key
  `cli.drc_json_marker`.

**T8: Honest annotations and process-bound state.**
- `kw_pcb_drc` must carry `destructive_hint` while `save_board` can be true.
- `kw_sch_get` stays READ_ONLY only after the `.net` leak is fixed.
- Jobs, `kw_live` sessions and headless api-servers die with the process, so `kiwrap call` must
  force `run_async=false` and refuse `kw_live launch/attach`.
- A per-process board-model cache keyed on (path, mtime, size) or the IPC revision would cut the
  ipc11 re-read per page (3.1 s against 0.87 s for the file provider on the video demo).

**T9: Licensing.** KiCad's `pcb_calculator` transline code is GPL (derived from Transcalc). The MIT
core (D8) must implement item 34's impedance models from the original papers and must never port
the C++.

## 7. Phased plan

| Phase | Items | Effort | Rationale |
|---|---|---|---|
| **0. Epoch safety** | 1 (+ B2 `.kicad_prl`, annotation fixes) | L (3-5 d) | Stops silent format loss on users' files; everything else runs on top of `effective_target()` and the stamp guard. Ship the `save_board=false` default and the ipc11 epoch precondition first (hours), then the guard and `retarget`/`upgrade` |
| **1. Read-only and outputs** | 2, 3, 4, 7, 38 | XL (7-10 d) | `detached()` + `resolve_out_path` + results store unlock third-party-project use. Item 38 is S, fixes the page-size resolution bug and makes every later check visually verifiable |
| **2. Data parity and DRC truthfulness** | 6, 10, 11, 12, 13, 14, 15, 19, 20, 23 | XL (8-12 d) | Mostly S/M exposures of data both readers already hold. Includes the cursor skip (B3), `ignored_checks`, `all_track_errors`, and one parity test that locks it in. Removes the reasons reviewers re-parsed the `.kicad_pcb` |
| **3. Model depth and DRC location** | 5, 8, 9, 16, 17, 18, 21 | XL (10-14 d) | Zones with unfracture, graphics, the description parser (Tier A then B) and hints; the CLI lands here once paging is sane. Prerequisites for every fill-based analysis |
| **4. `kw_pcb_analyze` foundation** | 24, 25, 26, 30, 31, 32, 37 | XL (15-20 d) | Build `drc_probe` (T3) and the vector kit (T5) once. Order: 31 → 32 → 25+26 → 37 (paste_coverage + structured field_parity) → 30 → 24 |
| **5. Analysis breadth and overlays** | 22, 27, 28, 29, 33, 34, 35, 36, 39 | XL (20+ d) | Raster kit, copper graph and templates. 39 turns every overlay into a reviewable image. 35 and 29's real area are estimates and come last |

## 8. Decisions for the owner

1. **Default KiCad for projects kiwrap did not create.** Should it be the install matching the file
   epoch (recommended) or the newest (the current behaviour, arguably D16)? Restate D16 as "develop
   against the newest; never write a newer epoch into a user's file without `kw_project upgrade`",
   and reconcile ARCHITECTURE §6 ("oldest KiCad the user wants") with `kw_project create`
   (newest).
2. **`kw_pcb_drc` saving refilled zones.** Recommended: `save_board=false` by default, saving only
   on explicit request and with epoch equality. The cost is stale on-disk fills: fab exports must
   refill (the JLC profile's `check_zones=True` already does) or warn.
3. **Shape of read-only mode.** A server flag plus per-call `persist=false` on validators is
   recommended over a `readonly` argument on all 33 schemas. Decide whether a read-only server takes
   the project `.kiwrap/lock` (recommended when it exists) or refuses.
4. **Catalog.** Accept `kw_pcb_analyze` as the 34th tool, and fold `kw_pcb_drc_report`, `measure`
   and `probe_via` into existing tools (T6).
5. **Breaking receipt changes.** These are:
   - `pos` → `anchor_pos`, with a one-release alias;
   - the composite, versioned cursor string, which breaks callers doing int arithmetic (check
     `extension/src`);
   - a compact-JSON text body, which is 30% smaller but less readable in logs.
6. **Upstream work under D19.** Should the kicad-cli DRC JSON `marker{pos, layer, path}` patch be
   drafted in `n3rd-bugs/kicad-fork` now (small, high value), and is an impedance/transline export
   worth drafting at all (low priority)?
7. **Pin the sandbox UI language to English.** This changes the `sandbox.py` contract and is needed
   before more DRC and ERC description parsing.
8. **Dependencies.** Stay pure-Python zero-dep for geometry and raster (measured as sufficient), or
   allow an optional numpy accelerator later behind the same API.
9. **Fab limits.** Which vendors and layer counts to curate, where each limit's source URL and read
   date are recorded, and whether `kw_fab_export` should gate on the fab audit (`require_fab_audit`)
   or only warn.
10. **The extension's `kiwrap.kicadInstall` default.** Change it from `newest` to `project` (follows
    from decision 1; affects `kw_live launch`).
