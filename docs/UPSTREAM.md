# UPSTREAM.md — feedback-signal tiering, the gap list, and the fork/patch workflow

Written 2026-09-19 for decisions D16 (KiCad 11 first) and D17 (data-first feedback, expand
kicad-cli/IPC upstream, source build planned) in [DECISIONS.md](DECISIONS.md). This is the
definitive answer to "which structured signal does the agent get from where, and what has to be
built into KiCad itself". It supersedes the tier sketch in D17 and the "gated trigger" wording in
[dev/kicad-source-build.md](dev/kicad-source-build.md).

Evidence used, in order of authority:

1. **T5 handler snapshot** `conformance/snapshots/ipc-handlers/10.99.0.json` — 129 command
   messages of kicad-python f79cebd sent to a headless `kicad-cli api-server` of
   10.99.0-4083-g6dd893f7d1 with the ecc83 board and schematic open; summary pcb: handled 67 /
   bad_request 37 / unhandled 25; schematic: handled 34 / bad_request 26 / unhandled 69 (the
   first snapshot read 52/37/40 and 28/25/76 because the probe's minimal instance left the
   nested `RunJobSettings.document` empty, so the 22 job commands answered `AS_UNHANDLED` — a
   probe artifact, fixed 2026-09-19 in `handlers._minimal_instance`, see G6); stable over
   repeated runs and EQUAL on the bench builds. Every "not available headless" claim below cites a row of that file as
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
| Editing a second unit of a multi-unit symbol over IPC | 3 (bug) | — | `UpdateItems` on two different units of one multi-unit symbol wedges the nightly server (capability `ipc.schematic_update_shared_lib_symbol_hangs`, true on the matrix's own `10.99.0` entry, which `seed_for` now reads) → gap **G13**; workaround: file engine |

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
| Airwires with both endpoints and item ids (what to route next, how far) | **3** (stock) / 1 + 2 on the fork build | `structured.export_ratsnest` (`cli.has_ratsnest_export`), `queries.get_ratsnest` (`ipc.has_get_ratsnest`) | stock nightly: no `GetRatsnest` (T5: not in inventory), no `pcb export ratsnest`; gap **G1**, implemented on the fork (`mr/g1-ratsnest`, `kiwrap-build/6dd893f7d1-4`): pairs == DRC `unconnected_items`, anchors == what the editor draws, exclusions flagged; kiwrap prefers it behind the keys, cluster walk / estimate stay the stock fallback |
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
| Exports over the live IPC session | 2 | `backends.ipc.jobs` (`run_job`, `export_board_stats`, `export_board_svg`, `export_schematic_netlist`) over `RunBoardJobExport*` (15) / `RunSchematicJobExport*` (7) | T5: every board job `handled` on pcb, every schematic job `handled` (netlist `bad_request` without a format) on schematic, `unhandled` on the other document type; `ipc.headless_jobs = true` on 10.99.0-4083 (verified, `tests/test_ipc_patch_g6.py`). The first snapshot's "all 22 unhandled" was a probe artifact (G6, closed). kicad-cli stays `kw_fab_export`'s default |
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

### G1 — Ratsnest / airwires with endpoints (priority 1) — implemented on the fork

- **Why the agent needs it:** placement quality (total airline length, crossings) and "what is
  left to route and where" are the two numbers the placement and routing loops iterate on. On
  stock KiCad the count is exact only through a full DRC run and the endpoints are guesses.
- **Status 2026-09-19:** implemented on the fork branch `mr/g1-ratsnest` (= `base/6dd893f7d1` +
  two commits: job + CLI + schema + `qa_pcbnew`/`qa_cli` tests, then proto + handler + `qa_api`
  e2e case), built as `kiwrap-build/6dd893f7d1-4` (integration branch `kiwrap/6dd893f7d1` =
  base + `mr/g6-headless-jobs` + `mr/g1-ratsnest`, built commit `4a04900fd2`), installed and
  registered as `C:/kiwrap-builds/6dd893f7d1-4`. Issue not filed (draft
  `docs/upstream/drafts/g1-issue.md`), MR not opened (`g1-mr.md`); manifest status `local`.
- **What it is:** `JOB_EXPORT_PCB_RATSNEST` run by `pcbnew_jobs_handler` after the loader's
  `BuildConnectivity()`; `kicad-cli pcb export ratsnest [--units mm|in|mils] [--nets N1,N2]
  [--include-routed] [-o FILE] BOARD` (JSON only — no `--format` with a single option; schema
  `resources/schemas/ratsnest.v1.json`: `{$schema, source, date, kicad_version, coordinate_units,
  unrouted_count, airline_length, nets:[{name, unrouted_count, airline_length, edges:[{a:{uuid,
  description, pos, layer}, b:{...}, length, excluded?}]}]}`); the shared collector
  `pcbnew/ratsnest/ratsnest_report.{h,cpp}` (`CollectRatsnestAirwires` over
  `CONNECTIVITY_DATA::RunOnUnconnectedEdges` = exactly the `DRCE_UNCONNECTED_ITEMS` set, anchors
  = `CN_ANCHOR::Pos()` after `OptimizeRNEdges`); `board_commands.proto` `GetRatsnest` /
  `GetRatsnestResponse` / `RatsnestNet` / `RatsnestEdge` (`excluded`) / `RatsnestNode`, all
  `Since: 11.0`; `API_HANDLER_PCB::handleGetRatsnest` (document check, then busy check, no
  frame). Decisions taken in review: an item pair the project excludes as a DRC
  `unconnected_items` violation is listed with `excluded: true` and left out of the counts and
  lengths, so `unrouted_count` equals the editor's "Unrouted" and the DRC JSON marks the same
  pairs (keyed like DRC: marker position + the two items); the layer of a through item's end
  (THT pad, through via) is the layer it shares with the other end, else its top-most copper
  layer (fixture `qa/data/pcbnew/ratsnest_layers.kicad_pcb`); no `GetSettingsDialogTitle`
  override and no dead enum; `RunBoardJobExportRatsnest` (the job over IPC) is deliberately not
  included — `GetRatsnest` returns the data without a file round trip, the CLI/jobset half runs
  the job, and the extra proto/enum-converter/QA surface is a follow-up once `GetRatsnest` lands.
- **Verified:** export pairs == DRC `unconnected_items` pair for pair (issue8883 7/7; issue17429
  35 listed, 7 excluded, `unrouted_count` 28; ecc83 with 30 % of its copper removed 9/9); end
  points are the pad positions / track ends of the board file; `GetRatsnest` == the export
  (uuids, positions, layers, `excluded`); the file estimate agrees per net; the old cluster walk
  reproduces the count but 1/9 of the pairs. QA on the fork: `qa_pcbnew` `RatsnestReport` 5/5,
  `qa_cli` `test_pcb_export_ratsnest` + `_exclusions`, `qa_api` `ApiE2E/GetRatsnest`; full
  `qa_api` 108/110 (the two known test-side failures, `issue-qa-api-windows-findings.md`).
- **kiwrap side:** `structured.export_ratsnest` + `formats.reports.ratsnest` (parser/model,
  `RatsnestEdge.excluded`, `active_edges`), `backends.ipc.queries.get_ratsnest` /
  `ratsnest(kicad=None)` (GetRatsnest first, cluster walk fallback; message classes built from a
  descriptor while kicad-python predates the proto), planner `connectivity` = ipc11 → cli → file,
  `kw_pcb_check` / `kw_pcb_get` `provider_used` `cli:10.99.0+ratsnest-export` /
  `ipc11:10.99.0+ratsnest-export` with `degraded` empty and `uuid` + `layer` per end point.
  Detection keys `cli.has_ratsnest_export` (help tree) and `ipc.has_get_ratsnest` (derived from
  the same binary, confirmed by the presence probe the first headless session with a board runs,
  §3.3). Tests `tests/test_ratsnest_patch_g1.py` (`kicad_patch("ratsnest-export")`).
- **Upstream plan:** two MRs in sequence, not one (§3.4 size rule; the commits already split):
  the job + CLI + schema + tests first, `GetRatsnest` once it has landed. Precedents: !2707
  (merged 2026-08-02) added `RN_NET::GetTotalAirlineLength()` because Wayne blocked per-call
  `GetEdges()` sorting; `9624684909` + `eff7182892` made `BOARD::CompileRatsnest()` frame-free.
  Pitch: end goal ("external placement grading / routing needs unrouted connections with
  endpoints"), reference the DRC JSON conventions, no per-net `GetEdges()` loops.
- **Open:** the kicad-python wrapper (`Board.get_ratsnest()`), to be built against the fork
  commit (steps in `g1-mr.md`); the T5 inventory cannot see `GetRatsnest` until then.

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
  (T5 rows). No dependency on G6 (closed, not a gap): a `RunBoardJobDrc` handler copies
  `handleRunBoardJobExportStats` (`api_handler_pcb.cpp`) and runs headless the same way — with
  the document check before the busy check, unlike upstream's own job handlers.

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

### G6 — Job exports on the headless server: **closed, not a gap** (2026-09-19)

The 22 `RunBoardJobExport*` / `RunSchematicJobExport*` handlers are registered in the
`API_HANDLER_PCB` / `API_HANDLER_SCH` constructors (`api_handler_pcb.cpp:170-196`,
`api_handler_sch.cpp:159-172`, unconditional) and run through `KIWAY::ProcessJob` on the
context's KIWAY, which the headless contexts carry; upstream's `ApiJobs` e2e suite drives them
through `kicad-cli api-server` (6/6 on the pristine bench). The first T5 snapshot sent an empty
nested `job_settings.document` and `validateDocument` answered `AS_UNHANDLED` by design. Probe
fixed (`handlers._minimal_instance` fills any one-level-nested `DocumentSpecifier`), snapshots
regenerated, `ipc.headless_jobs = true` verified by `tests/test_ipc_patch_g6.py`. Live exports:
`backends.ipc.jobs`. The fork branch `mr/g6-headless-jobs` carries only a QA e2e case
(`ApiJobs/ExportBoardStats`); do not open it standalone (`docs/upstream/drafts/g6-mr.md`).

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
  fix is registering them on the headless contexts (check first whether it is another
  probe artifact like G6). **Effort:** 0.5–1 d.
  **Detection:** `ipc.headless_library_queries` (T5 rows).

### G12 — DRC report capped at 199/499 violations per type (priority 5)

- #25434 closed by the reporter, unfixed; overflow shorts are reported as clearance. Workaround:
  `kw_pcb_drc` flags `truncated: true` when a type hits 199/499 and the skill re-runs after
  fixes. Proposed change: a `--max-violations` CLI/job option (or unlimited for the job path).
  Raise in G2's issue thread. **Effort:** 0.5 d. **Detection:** `cli.drc_unlimited_violations`.

### G13 — `UpdateItems` wedges the headless server on the second unit of a multi-unit symbol (priority 2 as a bug report)

- Verified on 10.99.0-4083, the RelWithDebInfo and the Debug bench (12/12 runs; capability
  `ipc.schematic_update_shared_lib_symbol_hangs`, verified on the matrix's `10.99.0` entry —
  `seed_for` reads that entry since 2026-09-19, so the live record of the nightly says true).
  Condition: `UpdateItems` on two different units of one multi-unit symbol (even unchanged
  items); the same unit twice, two single-unit instances of one library symbol and power symbols
  do not hang. Cause (Debug build + cdb): `PackSymbol` serialises the non-placed units' pins
  from the library symbol, whose pin KIIDs every instance shares; `SCH_SYMBOL::Deserialize`
  adopts them, so after two units two `SCH_PIN`s share a KIID and the connectivity engine throws
  `Duplicate connectivity source identity` (`conn_facts.cpp:495`) inside
  `SCH_COMMIT::Push`; `handleApiRequestString` does not catch it, `Reply()` never runs and the
  nng thread waits forever. Minimal repro `patches/repro/g13_repro.py`; issue draft
  `docs/upstream/drafts/g13-issue.md`; live guard test
  `tests/test_kipy_packaging.py::test_hang_guard_matches_the_live_repro`. Workaround: no update
  helper in `backends.ipc.writes`; edits of symbol instances go through the file engine;
  `close()` still terminates the process. **Action:** file the issue with the repro (no MR from
  us unless assigned); expect a fast fix because it is a hang, not a feature. Effort 0.5 d.

### G14 — kicad-python packaging and launcher (priority 1, Python only, no KiCad build)

- (a) `tools/generate_protos.py`: use `protoc.PROTOC_EXE` and feed `--descriptor_set_out` to
  `protol raw`. Root causes (reproduced 2026-09-19, `patches/kicad-python/0001-*.patch`):
  protoc-wheel-0's console script `os.execv`s the compiler, so on Windows `subprocess.run`
  returns rc=0 before anything is written; and protoletariat's own `protoc` lookup hits the same
  launcher (plus `shlex.split` strips backslashes from `--protoc-path`). The earlier "open
  NamedTemporaryFile blocks protoc" theory was refuted. Until merged kiwrap ships a hatchling
  force-included `.pth` + import-time fallback (documented in `backends/ipc/_pth`, known to put
  top-level `common`/`board`/`schematic` on `sys.path`). Open a kicad-python issue linking
  #82/#140, MR of ~5 lines (draft `docs/upstream/drafts/g14-mr-generate-protos.md`), then drop
  the `.pth`.
  (b) `KiCadServer(..., env=None)` so `KiCad(headless=True)` can carry the sandbox env; kiwrap's
  `_ApiServerProcess` stays as the launcher regardless (tree kill, transcript, readiness by
  `Ping` as in `qa/tests/api/api_e2e_utils.h`).
- **Effort:** 0.5 d each. **Detection:** none needed (kiwrap-side).

### G15 — Display input mode: drive a hidden KiCad editor with posted window messages (Windows; GUI-only, no agent signal) — implemented on the fork

- **Why kiwrap needs it:** the VS Code extension's "Live KiCad" tab ([DISPLAY.md §12](DISPLAY.md))
  captures the real KiCad window with `Windows.Graphics.Capture` and sends the user's input back. On
  stock KiCad the input half needs the window on the desktop and (for `SendInput`) foreground and
  unobscured; a window that is covered by VS Code or hidden cannot be driven with `PostMessage`
  because KiCad and wxMSW consult the hardware instead of the event: `WX_VIEW_CONTROLS` /
  `TOOL_MANAGER` read `wxGetMousePosition()` (`GetCursorPos`), `TOOL_DISPATCHER`'s button state and
  KiCad's `wxGetKeyState()` calls read `GetAsyncKeyState`, modifiers come from `GetKeyState` (never
  updated by posted keys), `wxEVT_CHAR_HOOK` comes from a `WH_KEYBOARD` hook that Windows runs for
  hardware input only (posted `Esc` never reaches `wxDialog`/`DIALOG_SHIM`), `GenerateMouseLeave`
  builds LEAVE events from the real cursor, and `center_on_zoom` warps the physical cursor. This is
  not a tier-3 *data* gap: no agent tool depends on it (D17 keeps the loop data-first); it is the
  only path to "KiCad invisible, VS Code is the UI" on Windows (DISPLAY.md decision 5 → §12.8 row 5).
- **Status 2026-09-20:** implemented on the fork branch `mr/display-input` (= `base/6dd893f7d1` +
  one commit `d70b0af290` "Add an input-from-events mode for message-driven editors (MSW)", 25 files
  +534 −48, `Assisted-by: Claude Code`), built with the `msvc-win64-release` preset and measured on
  pcbnew (`research/display/win-input-patch.md`). Pushed to the private origin only; issue and MR
  **not** filed (D19), drafts in `docs/upstream/drafts/display-input-mr.md`. Not yet on the
  integration branch `kiwrap/6dd893f7d1` and not in the manifest: that is decision 5 in
  DISPLAY.md §12.8 (carry vs not). Manifest status if carried: `local`.
- **What it is:** an environment-variable-gated mode `KICAD_INPUT_FROM_EVENTS=1|cloak`, no change
  while unset, GTK/macOS stubs. New seam in `KIPLATFORM::UI`: `InputFromEvents()`,
  `GetMousePosition()` (last event / last warp), `GetMouseState()` (event buttons + thread-synchronous
  modifiers), `IsKeyDown()` (`GetKeyState`), `WarpPointer()` moving only the reported pointer,
  `InfiniteDragPrepareWindow()` refusing; a process-wide `wxEventFilter` (`INPUT_FROM_EVENTS_FILTER`,
  installed by `PGM_BASE::InitPgm`) that records pointer/buttons, mirrors key transitions with
  `SetKeyboardState`, synthesises `wxEVT_CHAR_HOOK` from `wxEVT_KEY_DOWN` like `wxKeyboardHook`, and
  drops spurious LEAVE events; the state hangs off `wxTheApp`'s client object because kiplatform is a
  static library linked once per module. A mechanical sweep routes the direct `wxGetMousePosition` /
  `wxGetMouseState` / `wxGetKeyState` callers (tool dispatcher, view controls, the three selection
  tools, choosers, view switchers, properties panel, lib tree, AUI art, 3D preview, `SCH_MOVE_TOOL`)
  through the seam. Value `cloak` additionally `DWMWA_CLOAK`s every wx top-level on show (invisible on
  the desktop, still delivered by WGC; only the owning process may cloak).
- **Verified (posted messages only, window covered or cloaked):** click select, `R`, `Delete`,
  `Ctrl+Z`, drag-move, wheel zoom centred on the reported pointer, middle-drag pan, double-click →
  Footprint Properties, `E` → dialog, field click + `Ctrl+A` + typing + OK (73 ms), footprint
  chooser (`A`, filter, two-step `Esc`), context menu at the reported pointer + item click, `Esc` on
  menus/dialogs; `GetCursorPos` and `GetForegroundWindow` unchanged during the runs; wheel → pixel
  69 ms median, 27.6 fps motion at 8.8 % helper / 3.4 % pcbnew, 0 idle. Not fixed by the patch:
  tooltips (comctl32 polls the real cursor), popup menus / drop-downs (OS windows, still shown on the
  desktop), one foreground grab at launch, fully off-screen windows get no WGC updates. **Untested:**
  eeschema and the other editors, a project session (`kicad.exe`), Alt combinations, the auto-pan
  focus path, the 10.0 stable branch. No QA test added yet (the mode is exercised by the
  `prototypes/display/winstream` drivers, `bench/actions/events_*.json`).
- **kiwrap side (planned, DISPLAY.md §10 D6):** the session supervisor launches builds carrying the
  patch with `KICAD_INPUT_FROM_EVENTS=cloak` in the sandbox env and without activation; the display
  helper selects its `events` injector (PostMessage only) when `kiwrap.patches` contains
  `display-input`, and the `hybrid` injector otherwise. No tool branches on it (§3.1).
- **Detection:** there is no CLI subcommand or IPC handler to probe — the mode is GUI-only and
  env-var gated — so the id is taken from the manifest + `kiwrap.patched_build` (the installed
  commit matches a `kiwrap-build/*` tag whose integration branch includes `mr/display-input`), and
  the helper confirms it at run time: a cloaked KiCad window (`DWMWA_CLOAKED` set on the frame after
  launch with `=cloak`) proves the build honours the variable; if the frame is not cloaked the helper
  falls back to the hybrid injector. Proposed key: `gui.input_from_events` (informational, derived
  from the manifest, refined by that runtime check).
- **Upstream plan (on hold, D19):** issue first, pitched as *assistive / automated input on Windows*
  (UI automation, accessibility tooling, test drivers), not as remote display for an IDE; MR = seam +
  filter + the mechanical sweep, `cloak` left out of the first MR (fork-only until asked for). Before
  submitting: rebase on `upstream/master` (the sweep touches `tool_dispatcher.cpp`,
  `wx_view_controls.cpp`, `pcb_selection_tool.cpp`, which move often), guard hardware keys from
  reaching `wxEVT_CHAR_HOOK` twice while the mode is on, `clang-format` the changed ranges, run the
  pcbnew/eeschema QA binaries, and do not batch with the G1/G6 MRs (§3.1 no-batches rule).
- **Effort:** KiCad side done for pcbnew; 0.5 d eeschema/editor matrix, 0.5 d pre-MR hygiene, 0.5–1 w
  optional in-process cloaking of popup menus (untested). Rebase cost on every base bump is the
  ongoing price of carrying it.

Summary table (priority = order of work; "P" = priority):

| P | Gap | Kind | Effort | Acceptance | Detection key |
|---|---|---|---|---|---|
| 1 | G1 ratsnest export (CLI job + `GetRatsnest`) — **on the fork**, two MRs to open | feature | done (3–5 d) | medium-high | `cli.has_ratsnest_export`, `ipc.has_get_ratsnest` |
| 1 | G14 kicad-python protoc fix + `env=` | Python | 1 d | high | — |
| 2 | G3 ERC JSON ×100 (#25582) | bug fix | 1 d | high | `cli.erc_json_pos_scale == 1` |
| 2 | G13 UpdateItems hang | bug report | 0.5 d | — | `ipc.schematic_update_shared_lib_symbol_hangs == false` |
| 2 | G2 DRC/ERC jobs over IPC | feature | 3–5 d | medium | `ipc.has_drc_job`, `ipc.has_erc_job` |
| — | G6 job handlers headless — **closed, not a gap** (probe artifact) | — | 0 | — | `ipc.headless_jobs` (true) |
| 3 | G11 library queries headless | fix | 0.5–1 d | high | `ipc.headless_library_queries` |
| 3 | G5 per-net statistics | feature | 1–2 d | medium | `ipc.has_net_statistics` |
| 3 | G4 DRC marker pos (!2780) | parser only | 0.5 d | — | `cli.drc_json_has_marker_pos` |
| 4 | G7 schematic GetBoundingBox | feature | 1 d | medium-high | `ipc.schematic_bounding_box` |
| 4 | G10 structured ImportNetlist report | feature | 1–2 d | medium | `ipc.import_netlist_structured` |
| 5 | G8 pins as items | convenience | 1 d | medium | `ipc.schematic_pins_are_items` |
| 5 | G12 DRC per-type caps | fix | 0.5 d | medium | `cli.drc_unlimited_violations` |
| 6 | G9 unfiltered GetOpenDocuments | trivial | 0.5 d | low | — |
| — | G15 display input mode (`KICAD_INPUT_FROM_EVENTS`, Windows GUI only) — **on the fork**, MR on hold (D19); not an agent signal | feature (platform) | done (pcbnew); 0.5 d eeschema matrix + 0.5 d pre-MR hygiene | low-medium (assistive-input framing) | manifest + `kiwrap.patched_build`, runtime cloak check (`gui.input_from_events`) |

---

## 3. The fork / patch workflow

### 3.1 Principles (from D17 and the review norms)

- Stock KiCad always works: every tool branches on a capability key (`caps.has(...)`), never on
  "is this our build". A patched build only flips keys to true.
- Upstream first: one issue per change, assigned before coding; one MR per change; human-written
  prose; `Assisted-by: Claude Code` trailer on every fork commit an LLM helped write (KiCad's
  tool-generated-content policy, dev/upstream-workflow.md §1.7 — the trailer travels with the
  commit, the MR description carries the prose disclosure); integration tests on real fixtures;
  KiCad style. Nothing is
  shipped to users *from the fork* — the fork exists so kiwrap's CI and the owner's machine can run
  the patched build while MRs are in review.
- No batches: after Escain's 25 MRs were closed in a day, kiwrap submits at most two MRs at a time
  and lets the first one land before the next.

### 3.2 Branches, tags and the patch series in `n3rd-bugs/kicad-fork` (private; the public fork `n3rd-bugs/kicad-fork` carries none of our refs)

The fork tracks the read-only GitHub mirror; MRs go to GitLab (`upstream` remote =
`https://gitlab.com/kicad/code/kicad.git`, [dev/kicad-source-build.md](dev/kicad-source-build.md)).

| Ref | Purpose | Rule |
|---|---|---|
| `master`, `10.0` | mirror of upstream, never committed to | `git fetch upstream` only |
| `base/<kicad-sha12>` | the exact upstream commit the current nightly / patched build is on (today `base/6dd893f7d1` for 10.99.0-4083) | moves only when the pinned nightly moves (D16 re-probe) |
| `mr/<issue>-<slug>` | one branch per upstream MR, rebased on `master` (or `10.0` for stable-only fixes such as G3) | this is what the GitLab MR points at; force-pushed on rebase; deleted after merge |
| `mr/display-input` (G15) | the one branch without an issue number yet: no issue may be filed under D19, so it is named by slug alone and renamed `mr/<issue>-display-input` when the issue exists | same rules; carried in `kiwrap/<sha>` only if DISPLAY.md §12.8 decision 5 says so |
| `kiwrap/<kicad-sha12>` | integration branch = `base/<sha>` + every `mr/*` branch cherry-picked in priority order + `patches/kiwrap-version.patch` | rebuilt by script, never edited by hand; this is what the bench builds |
| tag `kiwrap-build/<kicad-sha12>-<n>` | an integration branch state that was built, T5-snapshotted and installed | immutable; `n` increments per rebuild; **lightweight tags only** — an annotated tag becomes `git describe`'s output, KiCad's CMake takes it for the version and every reconfigure fails (found 2026-09-19); keep `git fetch upstream --tags` current for the same reason |

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
- **IPC patches** are detected by a cheap live probe on first use of the headless backend:
  `capabilities_for` first DERIVES the key from the CLI half of the same patch (the api-server is
  the probed kicad-cli binary; a build carrying one half only is not supported), and the first
  `HeadlessSession` that has a board open runs every probe in `backends.ipc.presence` whose key
  the record has not probed yet (`queries.probe_get_ratsnest`: one request, `AS_UNHANDLED` →
  false), recording the answer through `Capabilities.refine` (`kiwrap.ipc_probed`, cache
  rewritten — once per install). The T5 snapshot for a bench build is written as
  `conformance/snapshots/ipc-handlers/<version>+<built commit>[.debug].json`
  (`KiCadInstallation.snapshot_name`; the BUILT commit, i.e. the integration branch's sha, never
  the base — a pristine and a patched build of one base must not share a file; T4 uses the same
  name) so it never collides with the stock nightly's file; `compare()` runs stock-vs-patched as
  a drift check (only the manifest's commands may differ; `scripts/dev/t5_bench_snapshot.py`).
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
4. **First patches, in this order:** G1 (ratsnest job + CLI + `GetRatsnest`, the top agent
   signal — done on the fork, `mr/g1-ratsnest`) → G3 (ERC ×100 on `10.0` and `master`, proves a
   stable-branch MR) → G2 → G5 → G7/G8. G6 turned out to be a T5 probe artifact (closed); the
   `mr/g6-headless-jobs` branch only carries the `ApiJobs/ExportBoardStats` e2e case. G14 (Python
   only, patches in `patches/kicad-python/`) and the G13 bug report go out before the bench exists.
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
