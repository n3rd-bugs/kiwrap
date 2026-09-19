# UPSTREAM.md — feedback-signal tiering, the gap list, and the fork/patch workflow

Written 2026-09-19 for decisions D16 (KiCad 11 first) and D17 (data-first feedback, expand
kicad-cli/IPC upstream, source build planned) in [DECISIONS.md](DECISIONS.md). This is the
definitive answer to "which structured signal does the agent get from where, and what has to be
built into KiCad itself". It supersedes the tier sketch in D17 and the "gated trigger" wording in
[dev/kicad-source-build.md](dev/kicad-source-build.md).

Evidence used, in order of authority:

1. **T5 handler snapshot** `conformance/snapshots/ipc-handlers/10.99.0.json` — 129 command
   messages of kicad-python f79cebd sent to a headless `kicad-cli api-server` of
   10.99.0-4083-g6dd893f7d1 with the ecc83 board and schematic open; summary pcb: handled 52 /
   bad_request 37 / unhandled 40; schematic: handled 28 / bad_request 25 / unhandled 76; stable
   over three runs. Every "not available headless" claim below cites a row of that file as
   `T5: <Command> <doc> -> <status>`. A command that is *absent from the 129-message inventory*
   does not exist in the nightly's protos at all (the inventory is derived from the descriptor
   pool by rule, `kiwrap.backends.ipc.handlers.command_messages`); those are cited as
   `T5: not in inventory`.
2. The Phase 1 build reports of the IPC backend (`kiwrap.backends.ipc`), the file-tier models
   (`kiwrap.formats.board/schematic/analysis`), the structured kicad-cli exports
   (`kiwrap.backends.cli.structured`) and the data-first tools (`kiwrap.core.tools.*_get/_check`),
   all verified live on 10.99.0-4083 and 10.0.6 (and 9.0.9 / 8.0.9 / 7.0.6 where the subcommand
   exists).
3. [research/10-kicad-upstream-mrs.md](research/10-kicad-upstream-mrs.md) and
   [dev/upstream-workflow.md](dev/upstream-workflow.md) (GitLab REST + discussions, fork git log),
   re-checked live on 2026-09-19 through the GitLab API: open MRs matching "ratsnest" = none;
   open MRs labelled `api` = none; !2780 (DRC JSON marker pos) still `opened`, `unchecked`, no
   milestone, last update 2026-09-07; Escain's KICAD_MCP series !2712–!2736 all `closed`
   2026-08-03; !2808 (`FocusOnItems`, interactive-only) is the only open API MR.

Vocabulary: **tier 1** = files (lossless CST + semantic models) plus kicad-cli structured
outputs; works on every installed KiCad that has the subcommand, no process kept alive.
**Tier 2** = KiCad 11 headless IPC (`ipc.headless`), a long-lived `api-server` per project;
also the only live-editing path. **Tier 3** = not obtainable from stock KiCad by any means the
agent can rely on: needs a KiCad change carried in the fork until merged upstream.

---

## 1. The definitive tiering, by design stage

Provider names are kiwrap modules; "verified" cites what actually ran. "est." marks a
computed estimate that has no KiCad oracle. Items whose tier-1/2 provider is not written yet are
flagged **kiwrap TODO** so they are not confused with KiCad gaps.

### 1.1 Schematic capture (symbols, wires, labels, hierarchy, ERC)

| Signal the agent needs | Tier | Provider | Status / verification |
|---|---|---|---|
| Symbols with reference, value, footprint, position, rotation, mirror, unit, DNP, per-instance refs in repeated sheets | 1 | `formats.schematic.SchematicFile` / `Hierarchy` | loads 405 demo schematics of 7.0.6–10.99.0; `kw_sch_get` file provider |
| Same, live, after IPC edits | 2 | `backends.ipc.readers.read_schematic` (`GetItems`) | ecc83 26 symbols / U1 three units; `kw_sch_get` provider `ipc11:10.99.0` |
| Absolute pin end points (needed to wire to a pin, to detect "pin resting on a wire interior") | 1 | `SchematicFile` claim-3 transform | all 42 ecc83 pins coincide with wire ends/junctions |
| Absolute pin end points, live | 2 (with client math) | `readers.symbol_transform` over `SchematicSymbolInstance.definition.items` | verified at 0/90/180/270 with placed ECC83. Pins are **not** `GetItems` items: T5 is silent because `KOT_SCH_PIN` is a type, not a command; the live test recorded "none of the requested types are valid for a Schematic object". Gap **G8** removes the client math |
| Pin connectivity, derived net names, dangling wire ends, unconnected pins, label-only nets, bus membership | 1 | `formats.analysis.pin_connectivity` / `netlist_from_file` | agrees with `kicad-cli sch export netlist` on 12779/12787 nets over every demo of 5 installs |
| KiCad's own netlist (the oracle) | 1 | `backends.cli` `sch export netlist` + `formats.reports.netlist` | all versions |
| KiCad's own netlist, live, per sheet, with pin/wire/junction KIIDs | 2 | `GetSchematicNetlist` / `GetSchematicHierarchy` | T5: both `schematic -> AS_OK`; ecc83 13 nets |
| Symbol / label / sheet bounding boxes (overlap and clutter checks) | 1 **kiwrap TODO** | `LibSymbol` graphics are not yet in the file model; pin-set bbox only | T5: `GetBoundingBox schematic -> AS_UNHANDLED` ("no handler available"), so IPC cannot supply it either → gap **G7** |
| Hit test on a schematic item | 2 | `queries.hit_test` (`HitTest`) | T5: `HitTest schematic -> AS_BAD_REQUEST` with a fake id (i.e. a handler ran); live test passes with a real id |
| ERC violations with sheet, type, severity, item uuids and mm positions | 1 | `backends.cli` `sch erc --format json` + `formats.reports.erc` (`cli.erc_json_pos_scale` = 100 on 8–10 and the nightly) | verified by T6 `test_t6_erc_json_position_scale` on 8.0.9 / 9.0.9 / 10.0.6 / 10.99.0. Over IPC: **absent** — no `RunSchematicJobErc`/`RunERC` message exists (T5: not in inventory) → gap **G2** |
| ERC positions correct without a ×100 fix | 3 (bug) | — | issue #25582 open, unassigned → gap **G3** |
| Semantic diff of two schematics (what did the last edit change) | 1 | `structured.sch_diff` (`sch diff --format json`, `cli.has_sch_diff`) | nightly only; positions in 1/10000 mm; regenerated UUIDs diff as removed+added |
| Symbol placement from a library with real pins | 2 | `writes.place_symbol` (`PlaceSymbolFromLibrary` after `LoadAllLibraries`) | T5: `LoadAllLibraries schematic -> AS_OK`; `PlaceSymbolFromLibrary schematic -> AS_BAD_REQUEST` with an empty lib_id (handler present). ≤ 10: file writer only |
| Library search ("never guess library ids") | 1 **kiwrap TODO** / 2 | file: read `sym-lib-table` rows + `.kicad_sym` names through the CST; IPC: `GetLibraryItems` / `SearchLibraries` | T5: `GetLibraryItems`, `GetLibraryTable`, `SearchLibraries`, `GetItemsFromLibrary` `-> AS_UNHANDLED` on **both** documents on 10.99.0-4083 even though master carries the handlers (12cdf280 09-15) → gap **G11** (re-probe on the next nightly first) |
| Text variables (`${VAR}`) resolved | 2 partial | `ExpandTextVariables` | T5: `ExpandTextVariables -> AS_OK` both; `GetTextVariables`/`SetTextVariables -> AS_UNHANDLED` both (read the `.kicad_pro` JSON instead — tier 1) |
| Editing a second instance of a shared library symbol over IPC | 3 (bug) | — | `UpdateItems` wedges the nightly server (capability `ipc.schematic_update_shared_lib_symbol_hangs`) → gap **G13**; workaround: file engine |

### 1.2 Placement

| Signal | Tier | Provider | Status / verification |
|---|---|---|---|
| Footprint position / rotation / side / locked / DNP / exclude-from-pos, every footprint | 1 | `formats.board.BoardFile` | matches `pcb export pos` 100 % on 84 demo boards × 5 installs; sees the footprints `pos` omits |
| Same, live | 2 | `readers.read_board` (`GetItems`) | ecc83 C1 (141.605, 99.695) rot 90 |
| Footprint bounding box incl. text (clutter, spacing) | 2 | `queries.bounding_box` (`GetBoundingBox`, batched) | T5: `GetBoundingBox pcb -> AS_OK` |
| Footprint bounding box from courtyard/pads/graphics (no text) | 1 | `Footprint.bbox()` | the two legitimately differ; `kw_pcb_get` reports which |
| Courtyard overlaps (the placement error that matters) | 1 | `analysis.courtyard_overlaps` | pair sets identical to `pcb drc courtyards_overlap` on every demo board where the rule is not `ignore` (8.0.9–10.99.0) |
| Off-board / outside outline / inside cutouts | 1 | `analysis.off_board` | model-only signal; deliberately reports overhanging connectors |
| Pad shapes as polygons | 1 / 2 | `Pad.polygons()` / `GetPadShapeAsPolygon` | T5: `GetPadShapeAsPolygon pcb -> AS_OK` |
| Rule areas / keepouts | 1 / 2 | `Zone.is_rule_area` / zones in `read_board` | file verified; IPC zones read on ecc83 |
| Design rules, netclasses, custom rules (`.kicad_dru`) | 1 / 2 | `.kicad_pro` + `BoardFile.design_rules` / `GetBoardDesignRules`, `GetCustomDesignRules`, `GetNetClasses`, `GetNetClassForNets` | T5: all four `pcb -> AS_OK` |
| Rule severities (must the model's courtyard hit count as an error?) | 1 | `.kicad_pro` `rule_severities` JSON | StickHub sets `courtyards_overlap: ignore`; tools must read it before scoring |
| Estimated airwire length / crossings after a move (placement quality) | 1 est. | `analysis.ratsnest_estimate` (MST between connected groups) | **no oracle exists**; gap **G1** provides one |
| Board stats: area, density per side, component counts | 1 | `structured.export_stats` (`pcb export stats --format json`, `cli.has_stats_json`) | 10.0.6, 10.99.0 |
| Footprint placement from a library with real pads | 2 | `writes.place_footprint` (`PlaceFootprintFromLibrary` after `LoadAllLibraries`) | 2 pads for CP_Radial; saved file has 16 footprints |
| Netlist → board sync (footprints appear with nets) | 2 | `ImportNetlist` (headless since 10f8e995) fed by `sch export netlist` | T5: `ImportNetlist pcb -> AS_BAD_REQUEST` "netlist file could not be opened" (handler present; probe passed no path). ≤ 10: hand-written updater (ARCHITECTURE §17). Report is free text → gap **G10** |

### 1.3 Routing

| Signal | Tier | Provider | Status / verification |
|---|---|---|---|
| Tracks, arcs, vias, zones with nets, layers, widths | 1 / 2 | `BoardFile` / `read_board` | file: vme-wren 25k tracks; IPC: ecc83 59 tracks |
| Copper connectivity: which items are connected, items per net | 2 | `queries.connected_items`, `items_by_net` (`GetConnectedItems`, `GetItemsByNet`, `GetItemsByNetClass`) | T5: `GetItemsByNet`/`GetItemsByNetClass pcb -> AS_OK`; `GetConnectedItems -> AS_BAD_REQUEST` with an empty id list (handler present). Also on GUI 10.0.1+ |
| Unrouted count per net | 1 (exact, slow) / 2 (approx.) / 1 est. | `pcb drc --format json` `unconnected_items` / `queries.ratsnest` (clusters − 1 per net, one `GetConnectedItems` per cluster) / `ratsnest_estimate` | DRC is KiCad's answer but costs a full DRC run (seconds) and needs the file on disk; the IPC count equals KiCad's but the **airwire endpoints are nearest-anchor guesses** |
| Airwires with both endpoints and item ids (what to route next, how far) | **3** | — | no `GetRatsnest` (T5: not in inventory), no `pcb export ratsnest` (`cli.subcommands` of 10.99.0 has none), nothing open upstream → gap **G1** |
| Routed length per net, via count | 1 | `structured.export_ipc2581` (`Ipc2581Net.track_length_mm`, 8.0+) or sum over `BoardFile.tracks` | IPC-2581 sums `Line` features only (arcs skipped); no KiCad oracle for the sum → gap **G5** (also airline length) |
| Clearance / width / short / crossing violations with mm positions | 1 | `pcb drc --format json` | correct mm on 8–10.99 (`drc.v1`) |
| Zone refill after routing | 1 / 2 | `pcb drc --refill-zones --save-board` (`cli.drc_refill_flags`, 10+) / `RefillZones` | T5: `RefillZones pcb -> AS_OK` |
| Track creation (write path) | 2 | `writes.add_track` (`BeginCommit`/`CreateItems`/`EndCommit`) | 59 → 60 tracks, saved |
| Track on a non-copper layer silently dropped | 3 (bug, minor) | — | Escain !2722 closed; kiwrap validates the layer client-side before `CreateItems` |
| DSN / SES for Freerouting | 1 | own codec (Phase 4 twin) / SWIG ≤ 10 | unchanged by D16 |

### 1.4 DRC / ERC review

| Signal | Tier | Provider | Status / verification |
|---|---|---|---|
| DRC violations (type, severity, items with uuid + mm pos), unconnected items, schematic parity | 1 | `kw_pcb_drc` (`pcb drc --format json --severity-all --exit-code-violations`) | 8.0.9 / 9.0.9 / 10.0.6 / 10.99.0; 7 has no DRC |
| Marker position + layer for item-less violations (`copper_sliver`, some zone/edge checks) | 3 (in progress upstream) | parser reads `items[].pos` only today | !2780 adds optional `violations[].pos` / `.layer` → gap **G4**: parser + `cli.drc_json_has_marker_pos` key, **no MR to write** |
| DRC over IPC (no file save, no process spawn, markers as protobuf) | **3** | — | no `RunBoardJobDrc`/`RunDRC` message (T5: not in inventory); only `InjectDrcError` (T5: `pcb -> AS_OK`, a test hook). Marker/exclusion proto types exist since eeac1a1c17 → gap **G2** |
| ERC over IPC | **3** | — | no `RunSchematicJobErc` (T5: not in inventory) → gap **G2** |
| More than 199/499 violations of one type | 3 (bug) | re-run after fixing the first batch | #25434 closed by reporter, unfixed → gap **G12** |
| Which DRC types gate `kw_fab_export` | 1 | capability `rules.drc_must_fix` / `rules.drc_review` | per version |
| ERC/DRC exclusions | 1 | `.kicad_pro` / board `(exclusion ...)` via CST | not yet exposed by a tool (kiwrap TODO, no KiCad gap) |

### 1.5 Fabrication review

| Signal | Tier | Provider | Status / verification |
|---|---|---|---|
| Gerbers, drill, position, STEP, PDF, BOM | 1 | `kw_fab_export` (`pcb export …`, `sch export bom`) | all versions with the subcommand |
| Every pad and via with net and position (KiCad-computed) | 1 | `structured.export_ipc2581` (8.0+) / `export_ipcd356` (9.0+) | 2.3 MB export parses in 0.04 s; D-356 mangles net names on ≤ 10 |
| Component placement with side/mirror/IPC rotation (JLCPCB CPL sanity) | 1 | `export_ipc2581`, `export_position` | y flipped back to board frame |
| Stackup (thickness, materials, εr, finish) | 1 | `structured.export_stackup` (`pcb export stackup --format json`, `cli.has_stackup_json`, nightly; board-file fallback on ≤ 10) | !2691; nightly rejects `report` |
| 2-D layer PNGs for human review | 1 (secondary) | `structured.export_png` (`pcb export png`, `cli.has_pcb_png`) | nightly only |
| Gerber round-trip checks | 1 | `gerber info`, `gerber diff`, `gerber convert png` (nightly `cli.subcommands`) | not wrapped yet (kiwrap TODO) |
| Exports over the live IPC session | 2 **on GUI only** | `RunBoardJobExport*` (15), `RunSchematicJobExport*` (7) | T5: **all 22 `-> AS_UNHANDLED` on both documents** headless (`ipc.headless_jobs = false` on 10.99.0-4083) → gap **G6**; kicad-cli stays the export path on 11 |
| Board file written by the nightly round-trips through the CST | 1 **unverified** | `formats.sexpr` | the nightly ships no demo in its own newest format; run a nightly-written board through `BoardFile` once (T9 canary) |

### 1.6 Signals that are *not* gaps (decided)

- **Full structured JSON dump** (`pcb/sch export json`): do not propose. Jon Evans on #19870:
  the file format is not an API; the boundary is `GetItems`. kiwrap's CST plus `kw_pcb_get` /
  `kw_sch_get` already give the agent the same data.
- **Rendered images** stay secondary (D17); `pcb render`, `pcb export png`, `sch export svg`
  are tier 1 and complete.
- **GUI-only commands** (`RunAction`, `InteractiveMoveItems`, selection, visibility, appearance
  — T5: all `AS_UNIMPLEMENTED "not available in headless mode"`): by design; never propose
  headless variants (#25503, !2543 reviews).
- **Footprint courtyard polygons over IPC**: `FootprintInstance.definition` items carry the
  `F.CrtYd` shapes; the reader just does not extract them yet (kiwrap TODO), and the file tier
  already has them.

---

## 2. Gap cards (tier 3), priority order

Effort figures are calendar days for one engineer with a working source build, from
[dev/upstream-workflow.md §3](dev/upstream-workflow.md); the build itself is §4 below.
"Detection" names the capability key that flips true on a build that has the change (§3.3).

### G1 — Ratsnest / airwires with endpoints (priority 1)

- **Why the agent needs it:** placement quality (total airline length, crossings) and "what is
  left to route and where" are the two numbers the placement and routing loops iterate on. Today
  the count is exact only through a full DRC run and the endpoints are guesses.
- **Workaround today:** `formats.analysis.ratsnest_estimate` (MST between connected groups; no
  oracle) tagged `ratsnest_estimated` in `kw_pcb_check`; `backends.ipc.queries.ratsnest`
  (cluster count − 1 = KiCad's unrouted count; nearest-anchor endpoints) on the nightly; exact
  count from `pcb drc` `unconnected_items` (items and positions of the two ends *are* in that
  report, so a DRC run is the slow exact fallback for endpoints too).
- **Upstream status:** nothing (no issue, no MR; live check 2026-09-19). Precedents: !2707
  (merged 2026-08-02) added `RN_NET::GetTotalAirlineLength()` because Wayne blocked per-call
  `GetEdges()` sorting; `9624684909` + `eff7182892` made `BOARD::CompileRatsnest()` frame-free.
- **Proposed change (one job class → CLI + jobset + IPC):**
  - `common/jobs/job_export_pcb_ratsnest.{h,cpp}` (`JOB_EXPORT_PCB_RATSNEST`: output path,
    format json, optional net filter, `--include-routed=false`), run by
    `pcbnew/pcbnew_jobs_handler.cpp` after `CompileRatsnest()`.
  - CLI `kicad-cli pcb export ratsnest --format json [-o FILE] [--nets N1,N2] BOARD`
    (`kicad/cli/command_pcb_export_ratsnest.{cpp,h}`), JSON via `include/rc_json_schema.h`
    style structs, schema `resources/schemas/ratsnest.v1.json`:
    `{source, coordinate_units, nets:[{name, unrouted_count, airline_length, edges:[{a:{id,pos,layer}, b:{id,pos,layer}, length}]}]}`.
  - IPC: `api/proto/board/board_jobs.proto` `RunBoardJobExportRatsnest` (jobs convention, the
    part Jon calls stable) **and** `api/proto/board/board_commands.proto`
    `GetRatsnest { DocumentSpecifier board = 1; repeated string nets = 2; }` →
    `GetRatsnestResponse { repeated RatsnestNet nets = 1; }` with `RatsnestEdge { KIID a; KIID b;
    Vector2 a_pos; Vector2 b_pos; BoardLayer a_layer; BoardLayer b_layer; Distance length; }`,
    all `// Since: 11.0`; handler `API_HANDLER_PCB::handleGetRatsnest` (document check → busy
    check), QA case in `qa/tests/api/test_api_e2e.cpp` on `api_kitchen_sink.kicad_pcb`.
  - kipy: `Board.get_ratsnest(nets=None) -> list[RatsnestNet]`; kiwrap: `queries.ratsnest`
    becomes the client of `GetRatsnest` when `ipc.has_get_ratsnest`, `structured.export_ratsnest`
    when `cli.has_ratsnest_export`, estimate otherwise.
- **Effort:** 2–3 d (job + CLI + QA) + 1–2 d (IPC + kipy). **Detection:** `cli.has_ratsnest_export`
  (`--help` tree lists `pcb export ratsnest`), `ipc.has_get_ratsnest` (T5 row `GetRatsnest pcb ->
  AS_OK`). Pitch: end goal ("external placement grading / routing needs unrouted connections with
  endpoints"), reference the DRC JSON conventions, avoid per-net `GetEdges()` loops.

### G2 — DRC and ERC over IPC as jobs, with structured markers (priority 2)

- **Why:** the live-session loop (place → check → fix) currently has to save the document, spawn
  `kicad-cli pcb drc` (seconds, a second process, `.kicad_prl` side effects) and re-read; the agent
  cannot check a board it has not saved. ERC likewise.
- **Workaround:** `SaveDocument` (T5: `pcb`/`schematic -> AS_OK`) then `kw_pcb_drc` /
  `kw_sch_check` through kicad-cli; the planner already falls back to it.
- **Upstream status:** absent on master (only `InjectDrcError`); kicad-python#14 (2025-02): Jon —
  "not currently really possible in KiCad code"; since then markers and exclusions became proto
  types (`board_rules.proto`, `schematic_rules.proto`, eeac1a1c17), constraints retrievable
  (0a9a212eea), and the headless job path runs `JOB_PCB_DRC` / `JOB_SCH_ERC` without a frame.
  Escain's !2724 (headless *Update PCB from schematic*) shows the pattern the maintainers did not
  review; nobody has proposed DRC/ERC.
- **Proposed change:** follow the jobs convention exactly — `board_jobs.proto`
  `RunBoardJobDrc { JobSettings job_settings; DrcJobOptions options (severity_all, refill_zones,
  schematic_parity, units); }` → `RunJobResponse` (report path) plus
  `RunBoardJobDrcResponse { repeated DrcMarker markers; counts }` reusing the existing marker
  message; `schematic_jobs.proto` `RunSchematicJobErc` likewise with `ErcMarker`. Handlers copy
  a `RunBoardJobExport*` handler; QA e2e case with a deliberately violating fixture. kipy
  `Board.run_drc()` / `Schematic.run_erc()`. Raise the 199/499 caps question (G12) in the same
  issue thread. Frame it as "the DRC/ERC job over the jobs API like the exports", never "RunDRC".
- **Effort:** 3–5 d. **Acceptance:** medium. **Detection:** `ipc.has_drc_job`, `ipc.has_erc_job`
  (T5 rows). Depends on G6 for headless job dispatch (§ G6) — do G6 first or in the same MR.

### G3 — ERC JSON positions ×100 (priority 2, first KiCad MR)

- **Why:** every ERC consumer must rescale positions *and* the dimensions inside description
  strings; the capability key exists but the fix is a one-line scale error in KiCad.
- **Workaround:** `cli.erc_json_pos_scale = 100` applied once in `formats.reports.erc`
  (`rescale_erc_document`), verified 8.0.9–10.99.0.
- **Upstream status:** issue #25582 (2026-09-17, `status::new`, unassigned). Escain's !2727 was
  rejected for adding `LOCALE_IO`; Seth's own c02e4c8844 fixed the same class of bug for HitTest
  by using the schematic IU scale.
- **Proposed change:** in the ERC JSON writer use the schematic IU scale (1/10000 mm) instead of
  the board scale, on `10.0` and `master` as separate MRs (10.0 diverged); QA test on the issue's
  project; `Fixes https://gitlab.com/kicad/code/kicad/-/issues/25582`.
- **Effort:** 1 d. **Detection:** the matrix probe already planned — `cli.erc_json_pos_scale`
  becomes 1 on fixed builds; keep the key forever (10.0.x users).

### G4 — DRC marker position / layer in the JSON report (priority 3, no MR to write)

- **Why:** item-less violations (`copper_sliver`, some zone checks) have no position today, so the
  agent cannot navigate to them.
- **Workaround:** none for item-less types; report them with `pos: null`.
- **Upstream status:** MR !2780 (nordic-style) open since 2026-08-23, rebased 09-07, `unchecked`,
  no milestone, no maintainer review as of 2026-09-19. Do not duplicate.
- **kiwrap change:** `formats.reports._common.violation_from_json` reads optional
  `violations[].pos` / `.layer`; capability key `cli.drc_json_has_marker_pos` (false everywhere
  until the MR ships; probe: presence of the field in a DRC run on the `copper_sliver` fixture).
  If the MR stalls past KiCad 11 GA, carry it in the fork (it is 4 files) and offer to take it over.
- **Effort:** 0.5 d (parser + key).

### G5 — Per-net statistics: routed length, via count, airline length (priority 3)

- **Why:** length matching, "is this net done", and routing-quality scoring.
- **Workaround:** routed length from IPC-2581 `Line` features (arcs skipped) or a sum over
  `BoardFile.tracks`/`ArcTrack`; via count from the file; airline length only from the estimate.
- **Upstream status:** kicad-python#96 (2026-03-11, wraps `BOARD::GetTrackLength()`) open,
  unanswered; `RN_NET::GetTotalAirlineLength()` exists since !2707; Net Inspector lengths are
  GUI/CSV only.
- **Proposed change:** `board_commands.proto` `GetNetStatistics { DocumentSpecifier board;
  repeated string nets; }` → `NetStatistics { name, Distance routed_length, Distance
  package_length, int32 via_count, Distance airline_length, int32 unrouted_count }` `// Since:
  11.0`, handler over `BOARD::GetTrackLength()` + `RN_NET::GetTotalAirlineLength()`; kipy
  `Board.get_net_statistics()`. Comment on #96 first. A `pcb export netstats --format json` job is
  a bonus once G1's job exists.
- **Effort:** 1–2 d. **Detection:** `ipc.has_net_statistics`.

### G6 — Job exports (and future jobs) dispatched on the headless server (priority 3)

- **Why:** on the nightly, every `RunBoardJobExport*` / `RunSchematicJobExport*` answers "no
  handler available" headless (T5: 22 commands, both documents `-> AS_UNHANDLED`), so a live
  session cannot export without a second process, and G2 would inherit the same hole.
- **Workaround:** kicad-cli for every export (`kw_fab_export`, `structured.*`) — complete, so the
  cost is latency and the double process only.
- **Upstream status:** the job handlers exist for GUI frames (46da153141, fee17a9580 "Switch
  schematic job handlers to using context"); they are not registered on the headless
  `HEADLESS_BOARD_CONTEXT` / `HEADLESS_SCH_CONTEXT`. Probably an omission rather than a decision —
  ask in an issue before patching.
- **Proposed change:** register the job handlers in the headless contexts
  (`pcbnew/api/headless_board_context.cpp`, `eeschema/api/headless_sch_context.cpp`) and add a
  `RunBoardJobExportStats` e2e case. **Effort:** 1–2 d. **Detection:** `ipc.headless_jobs` (key
  exists, false on 10.99.0-4083).

### G7 — Schematic `GetBoundingBox` (priority 4)

- **Why:** overlap/clutter checks and "where is free space" on a sheet.
- **Workaround:** pin-set bbox from the file model; **kiwrap TODO** extend `LibSymbol` with
  graphic primitives so the file tier computes body extents (this also serves ≤ 10).
- **Upstream status:** T5: `GetBoundingBox schematic -> AS_UNHANDLED`; Escain's !2713 implemented
  it (with `RevertDocument`, `ExpandTextVariables`, `SaveCopyOfDocument` — all three of which Jon
  has since landed himself, T5: `-> AS_OK`), closed unreviewed 2026-08-03.
- **Proposed change:** `API_HANDLER_SCH::handleGetBoundingBox` mirroring the PCB handler's
  `mode` semantics (item only / item + child text), registered in `api_handler_sch.cpp`; e2e case
  on `api_kitchen_sink.kicad_sch`. Re-derive from !2713, do not resubmit it. **Effort:** 1 d.
  **Detection:** `ipc.schematic_bounding_box` (T5 row).

### G8 — Pins as `GetItems` items (priority 5)

- **Why:** wiring to a pin needs its absolute position; today the client re-implements the
  symbol transform.
- **Workaround:** `readers.symbol_transform` over `definition.items` — verified exact at four
  rotations, so this is convenience, not correctness. Note the verified quirk: pin positions in
  `definition.items` are already Y-down (base transform `(1,0,0,1)`), unlike the file format.
- **Upstream status:** Escain !2721 (`SCH_PIN_T` whitelisted for queries, `header.container`
  honoured to scope to one symbol) closed unreviewed. **Proposed change:** the same idea,
  re-derived; keep deletion of pins refused. **Effort:** 0.5–1 d. **Detection:**
  `ipc.schematic_pins_are_items` (live probe: `GetItems(types=[KOT_SCH_PIN])` not `AS_BAD_REQUEST`).

### G9 — Unfiltered `GetOpenDocuments` (priority 6)

- T5: `GetOpenDocuments pcb/schematic -> AS_UNHANDLED` when `type` is unset; filtered calls work.
  Workaround: always pass the type (kiwrap does). Escain !2715 proposed a server-side fan-out.
  Trivial, low value; bundle into another MR only if a maintainer asks.

### G10 — Netlist sync with a structured report (priority 4)

- **Why:** after `ImportNetlist` the agent must know which footprints were added/replaced/removed
  and which pads changed net; today that is a free-text report.
- **Workaround:** diff `read_board` before/after (footprint refs, pad nets) — exact, but blind to
  warnings. ≤ 10: the hand-written updater (ARCHITECTURE §17).
- **Upstream status:** !2627 review: Jon asked for "an overall machine-readable status" and got
  counts; Escain !2724 (`UpdatePCBFromSchematic` with the dialog's options and a per-entry audit
  log, `dry_run`) closed unreviewed but is the right shape for the report.
- **Proposed change:** extend `ImportNetlistResponse` with `repeated NetlistChange changes { kind
  (added|replaced|removed|pad_net|warning|error), reference, footprint_id, pad, old_net, new_net,
  message }` `// Since: 11.0` and a `dry_run` request flag. **Effort:** 1–2 d. **Detection:**
  `ipc.import_netlist_structured` (response has `changes`).

### G11 — Library queries headless (priority 3, re-probe first)

- **Why:** `AGENTS.md` rule "never guess library ids (search first)".
- **Workaround:** tier 1 **kiwrap TODO** — read `sym-lib-table` / `fp-lib-table` rows (global
  template + user's read-only tables + project) and list `.kicad_sym` / `.pretty` entries through
  the CST; this is needed for ≤ 10 anyway.
- **Upstream status:** T5: `GetLibraryItems`, `GetLibraryTable`, `SearchLibraries`,
  `GetItemsFromLibrary` `-> AS_UNHANDLED` on both documents; `LoadAllLibraries -> AS_OK`,
  `GetLibraryStatuses -> AS_BAD_REQUEST "unknown library table scope"` (handler present). Master
  has the handlers (12cdf280, a97e2521, 09-15) but the installed nightly (HEAD 6dd893f7d1, 09-17)
  answers "no handler", so either they are GUI-frame-only or the build predates registration.
  **Action:** re-run T5 on the next nightly bump before writing anything; if still unhandled, the
  fix is registering them on the headless contexts (same shape as G6). **Effort:** 0.5–1 d.
  **Detection:** `ipc.headless_library_queries` (T5 rows).

### G12 — DRC report capped at 199/499 violations per type (priority 5)

- #25434 closed by the reporter, unfixed; overflow shorts are reported as clearance. Workaround:
  `kw_pcb_drc` flags `truncated: true` when a type hits 199/499 and the skill re-runs after
  fixes. Proposed change: a `--max-violations` CLI/job option (or unlimited for the job path).
  Raise in G2's issue thread. **Effort:** 0.5 d. **Detection:** `cli.drc_unlimited_violations`.

### G13 — `UpdateItems` wedges the headless server on a second instance of a shared library symbol (priority 2 as a bug report)

- Verified on 10.99.0-4083 (capability `ipc.schematic_update_shared_lib_symbol_hangs`); repro
  scripts `scratchpad/ipc-backend/probe10..14.py`. Workaround: no update helper in
  `backends.ipc.writes`; edits of symbol instances go through the file engine; `close()` still
  terminates the process. **Action:** file the issue with the minimal repro (no MR from us
  unless assigned); likely touches the new connectivity engine (!2807) — expect the maintainers
  to fix it fast because it is a hang, not a feature. Effort 0.5 d to report.

### G14 — kicad-python packaging and launcher (priority 1, Python only, no KiCad build)

- (a) `tools/generate_protos.py`: use `protoc.PROTOC_EXE` and `protol … protoc --protoc-path`,
  root-caused (protoc-wheel-0 `os.execv` on Windows + protoletariat's open temp file); until
  merged kiwrap ships a hatchling force-included `.pth` + import-time fallback (documented in
  `backends/ipc/_pth`, known to put top-level `common`/`board`/`schematic` on `sys.path`). Open a
  kicad-python issue linking #82/#140, MR of ~5 lines, then drop the `.pth`.
  (b) `KiCadServer(..., env=None)` so `KiCad(headless=True)` can carry the sandbox env; kiwrap's
  `_ApiServerProcess` stays as the launcher regardless (tree kill, transcript, readiness by
  `Ping` as in `qa/tests/api/api_e2e_utils.h`).
- **Effort:** 0.5 d each. **Detection:** none needed (kiwrap-side).

Summary table (priority = order of work; "P" = priority):

| P | Gap | Kind | Effort | Acceptance | Detection key |
|---|---|---|---|---|---|
| 1 | G1 ratsnest export (CLI job + `GetRatsnest`) | feature | 3–5 d | medium-high | `cli.has_ratsnest_export`, `ipc.has_get_ratsnest` |
| 1 | G14 kicad-python protoc fix + `env=` | Python | 1 d | high | — |
| 2 | G3 ERC JSON ×100 (#25582) | bug fix | 1 d | high | `cli.erc_json_pos_scale == 1` |
| 2 | G13 UpdateItems hang | bug report | 0.5 d | — | `ipc.schematic_update_shared_lib_symbol_hangs == false` |
| 2 | G2 DRC/ERC jobs over IPC | feature | 3–5 d | medium | `ipc.has_drc_job`, `ipc.has_erc_job` |
| 3 | G6 job handlers on headless contexts | fix/feature | 1–2 d | medium-high | `ipc.headless_jobs` |
| 3 | G11 library queries headless | fix | 0.5–1 d | high | `ipc.headless_library_queries` |
| 3 | G5 per-net statistics | feature | 1–2 d | medium | `ipc.has_net_statistics` |
| 3 | G4 DRC marker pos (!2780) | parser only | 0.5 d | — | `cli.drc_json_has_marker_pos` |
| 4 | G7 schematic GetBoundingBox | feature | 1 d | medium-high | `ipc.schematic_bounding_box` |
| 4 | G10 structured ImportNetlist report | feature | 1–2 d | medium | `ipc.import_netlist_structured` |
| 5 | G8 pins as items | convenience | 1 d | medium | `ipc.schematic_pins_are_items` |
| 5 | G12 DRC per-type caps | fix | 0.5 d | medium | `cli.drc_unlimited_violations` |
| 6 | G9 unfiltered GetOpenDocuments | trivial | 0.5 d | low | — |

---

## 3. The fork / patch workflow

### 3.1 Principles (from D17 and the review norms)

- Stock KiCad always works: every tool branches on a capability key (`caps.has(...)`), never on
  "is this our build". A patched build only flips keys to true.
- Upstream first: one issue per change, assigned before coding; one MR per change; human-written
  prose; `Assisted-by:` trailers; integration tests on real fixtures; KiCad style. Nothing is
  shipped to users *from the fork* — the fork exists so kiwrap's CI and the owner's machine can run
  the patched build while MRs are in review.
- No batches: after Escain's 25 MRs were closed in a day, kiwrap submits at most two MRs at a time
  and lets the first one land before the next.

### 3.2 Branches, tags and the patch series in `n3rd-bugs/kicad-source-mirror`

The fork tracks the read-only GitHub mirror; MRs go to GitLab (`upstream` remote =
`https://gitlab.com/kicad/code/kicad.git`, [dev/kicad-source-build.md](dev/kicad-source-build.md)).

| Ref | Purpose | Rule |
|---|---|---|
| `master`, `10.0` | mirror of upstream, never committed to | `git fetch upstream` only |
| `base/<kicad-sha12>` | the exact upstream commit the current nightly / patched build is on (today `base/6dd893f7d1` for 10.99.0-4083) | moves only when the pinned nightly moves (D16 re-probe) |
| `mr/<issue>-<slug>` | one branch per upstream MR, rebased on `master` (or `10.0` for stable-only fixes such as G3) | this is what the GitLab MR points at; force-pushed on rebase; deleted after merge |
| `kiwrap/<kicad-sha12>` | integration branch = `base/<sha>` + every `mr/*` branch cherry-picked in priority order + `patches/kiwrap-version.patch` | rebuilt by script, never edited by hand; this is what the bench builds |
| tag `kiwrap-build/<kicad-sha12>-<n>` | an integration branch state that was built, T5-snapshotted and installed | immutable; `n` increments per rebuild |

The kiwrap repo carries the manifest `src/kiwrap/data/kiwrap_patches.json` (proposed; the
`data/` package is allowed to name versions and commits):

```json
{"base": "6dd893f7d1", "patches": [
  {"id": "ratsnest-export", "gap": "G1", "branch": "mr/25xxx-pcb-export-ratsnest",
   "issue": "https://gitlab.com/kicad/code/kicad/-/issues/25xxx", "mr": null,
   "status": "in-review", "keys": ["cli.has_ratsnest_export", "ipc.has_get_ratsnest"]}
]}
```

`status` ∈ `local | issue-open | in-review | merged | upstream-fixed-differently | abandoned`.
When `status` becomes `merged`, the entry stays until the first nightly that contains the change
is pinned, then it is deleted together with the `mr/*` branch.

### 3.3 How a patched build is detected — the `kiwrap.patches` capability

The capability record is keyed on full semver and re-probed on first use (ARCHITECTURE §6); a
patched build has the *same* semver as the nightly it is based on, so detection must be by
presence, never by version string:

- **CLI patches** are detected by the existing `--help` tree probe: a new subcommand appears in
  `cli.subcommands` and `flatten_help_tree` derives `cli.has_ratsnest_export` from it, exactly as
  `cli.has_sch_diff` / `cli.has_pcb_png` are derived today.
- **IPC patches** are detected by a cheap live probe on first use of the headless backend: send
  the new command's minimal instance (the T5 mechanism, `backends.ipc.handlers.probe` restricted
  to the commands the manifest names) and set `ipc.has_get_ratsnest` etc. from the outcome
  (`AS_UNHANDLED` → false). The T5 snapshot for a patched build is written as
  `conformance/snapshots/ipc-handlers/<version>+<kicad-sha12>.json` so it never collides with the
  stock nightly's file; `compare()` runs stock-vs-patched as a drift check (only the manifest's
  commands may differ).
- **Bug-fix patches** (G3, G13) are detected by the quirk probes that already define the key
  (`cli.erc_json_pos_scale` from an ERC run on the T6 fixture;
  `ipc.schematic_update_shared_lib_symbol_hangs` from the live repro under a short timeout).
- `kiwrap.patches` (`list[str]`, new key) = the ids from the manifest whose *every* detection key
  probed true; `kiwrap.patched_build` (`bool`) = `KiCadInstallation.commit` matches a
  `kiwrap-build/*` tag in the manifest. Both are informational: `kw_env` shows them, receipts put
  `provider_used: "ipc11:10.99.0+ratsnest-export"` when a patched key was used, and T9 refuses to
  certify an install whose `kiwrap.patches` is non-empty as *supported* (patched builds are
  *development* installs). Tools never branch on `kiwrap.patches`.
- Cache safety: the capability cache key already includes the install root; the manifest's
  `base` sha is added to `CACHE_SCHEMA` input so a rebuilt install at the same root re-probes.
- Optional, to be confirmed on the first build: KiCad's CMake `KICAD_VERSION_EXTRA` appends a
  suffix to the version string; set it to `kiwrap<n>` so `kicad-cli version --format plain`
  shows the build id in logs. Do not parse it for capability decisions.

### 3.4 Lifecycle of one change (G1 as the example)

1. Issue on GitLab (end goal, version block, "may I work on this"), wait for assignment.
2. `git checkout -b mr/<issue>-pcb-export-ratsnest upstream/master`; job → CLI → proto → handler →
   QA → kipy branch on `n3rd-bugs/kicad-python`; `ctest -R qa_api` green; ≤ 300 lines.
3. Rebuild `kiwrap/<sha>` with the new `mr/*` cherry-picked; build; install to
   `C:\kiwrap-builds\<sha>-<n>`; `uv run kiwrap env` shows the install with
   `cli.has_ratsnest_export = true`; T5 snapshot `+<sha>.json` written; the kiwrap-side consumer
   (`structured.export_ratsnest`, `queries.ratsnest`) lands behind the key with a live test marked
   `kicad_patch("ratsnest-export")` (auto-skip on stock installs).
4. MR to GitLab with the kicad-python MR linked; manifest `status: in-review`.
5. On merge: manifest `merged`; on the next nightly bump that carries it, delete the entry and the
   branch; the stock T5 snapshot now shows the command as handled and the estimate becomes the
   fallback for ≤ 10.

---

## 4. What the source-build bench must enable first

The build is now "planned, Phase 1" (D17). [dev/kicad-source-build.md](dev/kicad-source-build.md)
has the recipe (unexecuted); the order below is what makes the first day pay for itself.

1. **Toolchain parity before anything else:** VS 2022 17.14 (MSVC 14.44, the compiler of the
   official binaries — vcpkg binary-cache hits depend on it), vcpkg at the `kicad-win-builder`
   pin at `C:\vcpkg`, the `kicad-gitlab` NuGet cache, antivirus exclusions, short paths
   (`C:\src\kicad`). Budget one working day, 40–80 GB.
2. **Build exactly the nightly's commit first** (`base/6dd893f7d1`), preset `msvc-win64-release`
   (RelWithDebInfo), `KICAD_IPC_API=ON`, `KICAD_BUILD_QA_TESTS=ON`, scripting OFF (the nightly has
   no SWIG), so that (a) PDB file@line numbers match the installed nightly, (b) `ctest -R qa_api`
   gives the upstream baseline, and (c) **T5 run against the built `kicad-cli` must equal
   `10.99.0.json`** — that equality is the bench's acceptance test. Only then branch.
3. **Install + discovery:** `cmake --install` into `C:\kiwrap-builds\<sha>-<n>` and register the
   path as an explicit install (`kiwrap.installation` explicit-setting path), so every kiwrap
   test that takes `installs` sees the patched build as one more KiCad; the sandbox env, seed and
   `%APPDATA%\kicad` hash assertion apply unchanged.
4. **First patches, in this order:** G6 (register job handlers headless — smallest C++ change,
   validates the handler/QA loop and unblocks G2) → G1 (ratsnest job + CLI + `GetRatsnest`, the
   top agent signal) → G3 (ERC ×100 on `10.0` and `master`, proves a stable-branch MR) → G2 →
   G5 → G7/G8. G14 (Python only) and the G13 bug report go out before the bench exists.
5. **Bench outputs that must exist before the first MR:** the `kiwrap/<sha>` integration branch
   script, the manifest, `conformance/snapshots/ipc-handlers/<version>+<sha>.json`, the
   `kicad_patch(<id>)` pytest marker, and a CI job (self-hosted runner, D14) that rebuilds the
   integration branch on manifest change and runs T5 + the `kicad_patch` tests.
6. **Do not use the bench for:** driving 7–10 (installed binaries), regenerating kipy protos (the
   nightly's `api/proto` + kicad-python main suffice), or PNS-as-batch-engine (out of scope).

---

## 5. What the upstream MRs teach about the approach (asked 2026-09-19)

The features kiwrap builds on were almost all **direct pushes by Jon Evans**, so the MRs that do
exist are the best guide to what gets accepted:

- **`ImportNetlist` !2627 (merged in a day):** 4 files, counts added on request, headless support
  as a follow-up commit by the same author. Copy its size and its response shape.
- **`sch diff` !2620, stackup !2691, RevertDocument !2779, variants !2648:** feature MRs from
  outside are merged when they close an assigned issue, reuse existing job/CLI plumbing, and
  contain no AI-sounding prose; the reviewer (Jon for `api/`, Seth otherwise) may rebase the branch
  himself if "Allow commits from members who can merge" is ticked.
- **Escain's KICAD_MCP series (!2712–!2736, closed unreviewed within a day):** the *designs* map
  one-to-one onto T5's gaps (G7 `GetBoundingBox`, G8 pins, G9 `GetOpenDocuments`, G10
  `UpdatePCBFromSchematic` with `dry_run` + audit log, !2722 non-copper tracks, !2729 connectivity
  crash after commits) and are worth reading for the C++ touch points — but they were rejected on
  process (25 MRs at once, LLM-written descriptions, no assigned issues), and Jon then implemented
  the parts he wanted himself (`RevertDocument`, `ExpandTextVariables`, `SaveCopyOfDocument`,
  library placement). kiwrap re-derives, never resubmits, and ships one at a time.
- **Rejected designs (!2264, !2332, !2543, #25503):** anything that opens frames, switches the
  displayed sheet, or exposes KIWAY/processes is refused; `Interactive*` naming, `DocumentSpecifier`
  first, no `auto`, no frame dependencies in handlers, document check before busy check (#24966
  fallout, !2808 review).
- **!2780 (DRC marker pos):** the smallest possible JSON-schema change (4 files, one QA case) has
  been waiting four weeks without review — expect the same latency for G1/G2 and plan the fork
  branch to carry them in the meantime.

Open items to watch on every nightly bump: !2780 (G4), !2808 (interactive only, ignore),
kicad-python !39/!55/!56, issue #25582 (G3), the post-!2807 connectivity engine (ERC/netlist drift
— re-run `refresh_matrix.py` and T2/T5).
