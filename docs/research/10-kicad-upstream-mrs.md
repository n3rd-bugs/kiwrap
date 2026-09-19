# kicad-upstream-mrs: the merge requests, commits and issues behind the KiCad 11 features kiwrap builds on (headless api-server, schematic API, document lifecycle, ImportNetlist / PlaceFromLibrary, IPC jobs, netlist/hierarchy queries, #24966, kicad-python headless, sch diff) plus what is open upstream around ratsnest/DRC/ERC/net-stats/JSON dumps

Researched 2026-09-19 against gitlab.com/kicad/code/kicad (REST API v4, unauthenticated; MR
discussions via the web `discussions.json` endpoint because `/notes` needs auth), the blobless
clone of `n3rd-bugs/kicad-source-mirror` at the nightly's commit (`git log` on `api/`,
`common/api/`, `pcbnew/api/`, `eeschema/api/`, `kicad/cli/`), gitlab.com/kicad/code/kicad-python,
and the installed 10.99.0-4083-g6dd893f7d1 nightly. `[VERIFIED LOCALLY]` marks things reproduced
on this machine. Raw API dumps are in the scratchpad (`upstream-research/`).

**Headline:** almost everything kiwrap depends on was pushed to `master` directly by Jon Evans
(craftyjon) without a merge request; the MRs that exist are from outside contributors and are the
best evidence of the review norms. Nothing upstream implements ratsnest export, RunDRC/RunERC over
IPC, net statistics or a structured JSON dump; one open MR (!2780) adds marker positions to the
DRC JSON report and one open issue (#25582) reports the ERC JSON 100x scale bug kiwrap already
normalises.

## Findings

### (a) `kicad-cli api-server` headless mode

- **[high]** Landed as a direct push, no MR: `caf1bcc455` "ADDED: Headless API server mode for
  kicad-cli", Jon Evans, 2026-03-14. Touches `api/proto/common/commands/{editor,project}_commands.proto`,
  `common/api/{api_handler_common,api_handler_editor,api_server}.cpp`, `common/kiway.cpp`,
  `include/api/*`, `kicad/cli/command_api_server.{cpp,h}` (new), `kicad/kicad_cli.cpp`,
  `pcbnew/api/{board_context,headless_board_context}.{cpp,h}` (new: the "context" abstraction that
  lets handlers run without a frame), `pcbnew/pcbnew.cpp`, and `qa/tests/pcbnew/test_headless_board_context.cpp`.
  The next commit `a4b2c69852` (same day) adds `qa/tests/api/test_api_e2e.cpp`: a Boost test that
  spawns the built `kicad-cli api-server --socket <tmp>` and drives it over nng.
  - Sources: https://gitlab.com/kicad/code/kicad/-/commit/caf1bcc4559bcbede1e4791f024c886a5825153e,
    https://gitlab.com/kicad/code/kicad/-/commit/a4b2c698528602c9af558e48ec3b6de820c75b41
- **[high]** Follow-ups (all Jon Evans, direct pushes): `9fe748f39a` 2026-03-21 "Support opening
  schematic documents from headless API" (adds `eeschema/api/{sch_context,headless_sch_context}`);
  `ca6f5ccf38` 2026-08-05 "Allow opening multiple project documents in headless mode" (adds
  `CloseAllDocuments`); `4e04012724`/`ad602a438d`/`826d27a4a7` 2026-08-29 (closing projects,
  refuse `CloseDocument` of another project, library items); Mike Williams `85d2a9f7d8`/`df5fdec4eb`
  2026-09-01 (close child documents before unloading a project); `6c96d5a7df` 2026-09-08
  "support creating new documents in headless" (`CreateDocument`, `Since: 11.0`); `0db01920fd`
  2026-09-09 "Support headless revert and create new document" (+ `SaveDocumentToString` for
  schematics). Seth Hillbrand hardened headless commits in `bcf953882c`, `7b9c9ccd9e`,
  `f47543c257`, `cdf88eb7a2` (2026-09-04..09, "Validate headless schematic roots and CLI
  diagnostics").
- **[high, VERIFIED LOCALLY]** Not backported: the `10.0` branch has no `kicad/cli/command_api_server.*`
  (GitLab tree listing of `kicad/cli` on `10.0`). Nightly `kicad-cli api-server --help`:
  `[--socket SOCKET_PATH] PROJECT_OR_FILE` ("Optional path to a .kicad_pro, .kicad_pcb, or
  .kicad_sch file to pre-load").
- **[high]** The QA harness (`qa/tests/api/api_e2e_utils.h`, 721 lines) is the model for kiwrap's
  own session manager: one shared `API_SERVER_MANAGER` per test binary ("Starting the api-server
  takes several seconds", "rapid connect/disconnect cycles on IPC sockets" leave "stale pipe state
  in the server's rep socket"), socket `/tmp/kicad-api-e2e-<pid>.sock` (POSIX) or a temp path +
  `.sock` (Windows), readiness = connect + `Ping` until not `AS_NOT_READY`, `QA_KICAD_CLI_PATH`
  injected by CMake. Test cases on master: ServerStartsAndResponds, OpenSingleBoard,
  DeleteMandatoryFieldIsRefused, SwitchBoards, Get/SetBoardDesignRules (+ validation failure,
  severity overrides), Get/SetCustomDesignRules, OpenProjectWithBoardAndSchematic, CreateDocument,
  CreateDocumentRejectsUnsavedModifications.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/master/qa/tests/api/api_e2e_utils.h,
    https://gitlab.com/kicad/code/kicad/-/raw/master/qa/tests/api/test_api_e2e.cpp
- **[medium]** MR !2577 (thesamprice, 2026-04-27, closed by the author) asked "the ipc tests do not
  run in the pipeline. Are those run as part of a release process?" - no maintainer answer; the
  e2e test is built as a normal `qa_api` target but depends on a built `kicad-cli`, so treat "did
  CI run it" as unknown when reviewing upstream MRs.

### (b) Schematic API handlers (`eeschema/api/api_handler_sch.cpp`, `schematic_commands.proto`)

- **[high]** Built in April 2026 by Jon Evans as direct pushes: `4ccede3a24` 2026-04-05 "Define
  schematic graphic types; implement QA framework" (adds `qa/data/eeschema/api_kitchen_sink.kicad_sch`,
  `qa/tests/eeschema/test_sch_api_{enums,proto}.cpp`, `eeschema/api/api_sch_enums.cpp`);
  `edc6236b07`/`7526d2901e` 04-06 (textbox, text, directive labels; groups, sheet symbols);
  `3dca4e0e38` 04-07 "implement schematic item creation"; `9533fdac22` 04-07 "support symbols and
  pins"; `76ac9e07a6` 04-13 instances; `48f4d67cfe` 04-16 `GetSchematicHierarchy`; `17bb9026aa`
  04-19 page settings; `b90628aa0d` 04-23 `GetSchematicNetlist`; Seth `c02e4c8844` 04-24 "Scale
  schematic API HitTest positions by the schematic IU scale" (schematic IU != board IU: the same
  class of bug as the ERC JSON x100 issue).
- **[high]** Outside contributions that were merged (all milestone 11.0, all reviewed by Jon):
  !2635 Benjamin Chung "Implement IPC API selection support for eeschema" (2026-06-10 -> 08-01);
  !2634 "Add user field support for eeschema IPC API" (06-14); !2648 "API: Implement variant
  support for schematics" (06-14 -> 09-01; Jon took over the branch to rebase - "I can take over
  your branch and rebase it myself"); !2649 "Fix schematic unwiring after IPC API updates to
  symbols"; !2779 victorstoica114 "expose RevertDocument through IPC API" (08-22 -> 08-29, issue
  #24803); Alex Shvartzkop `5c1c5a0c03`/`6998ac64bc` 06-12/13 (fix crash using schematic commits
  in headless; schematic SaveDocument/SaveCopyOfDocument).
- **[high]** August/September 2026 expansion (direct pushes): `144ca549c2` 08-26 "Fix package name
  for schematic commands" (proto package rename - clients generated before this date are
  incompatible), `a3aafd8499` rule areas, `59e7182ce2` tables, `6a91fed912` expand symbol support,
  `eeac1a1c17` 08-16 "Fully move DRC and ERC markers to protobuf" (adds `board_rules.proto`,
  `schematic_rules.proto`, `DRC_EXCLUSION`/`ERC_EXCLUSION`), `25b63376f0`/`2f9c0ec7d1`/`82729df8a4`
  08-15/16 (cross-probing and net highlighting moved from DDE to the API: `CrossProbeAnnounce`,
  `SyncSelection`, `HighlightNets` - Jon on #25542: "`SyncSelection` is an internal API, it should
  not be used by plugins/third party tools ... `// Internal KiCad API; may be removed without
  notice.`"), `7907978134` 09-05 variant refactor,
  `be90a7e200` 09-06 `GetDocumentModified`, `4e39455fa1` 09-10 GetItems with empty types,
  `118c18a436` 09-12 `path_human_readable` (issue #24439), `dc35a39731` 09-13
  `PlaceSymbolFromLibrary`, `c7e77dbafb` 09-16 PNG plotting.
- **[high]** The nightly's `master` HEAD `6dd893f7d1` is the merge of Seth's MR !2807 "Schematic
  Editor Connectivity Update" (created 09-16, merged 09-18, 186 files, no review discussion): a
  new threaded, sheet- and variant-aware connectivity engine ("Enable new connectivity engine by
  default" `15ad07306e`); `f75eb24798` "Read published connectivity from the editor" and
  `9f93bd95ba` "Run ERC on published connectivity" touch `eeschema/api/`. Expect ERC/netlist
  behaviour on nightlies after 09-18 to differ from 10.0.x; re-probe with `refresh_matrix.py` on
  every nightly bump (D16).
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2807
- **[high, VERIFIED LOCALLY]** Registered schematic handlers on master (36): GetOpenDocuments,
  SaveDocument, SaveCopyOfDocument, RevertDocument, SaveDocumentToString, SaveSelectionToString,
  GetItems, GetItemsById, GetSelection, ClearSelection, AddToSelection, RemoveFromSelection,
  RunSchematicJobExport{Svg,Dxf,Pdf,Ps,Png,Netlist,BOM}, GetSchematicHierarchy, Get/SetPageSettings,
  GetSchematicNetlist, CrossProbeAnnounce, SyncSelection, HighlightNets, GetVariants, AddVariant,
  DeleteVariant, RenameVariant, CopyVariant, SetVariantDescription, Set/GetCurrentVariant,
  ExpandTextVariables, PlaceSymbolFromLibrary (+ the inherited editor handlers: CreateItems,
  UpdateItems, DeleteItems, HitTest, GetBoundingBox, Begin/EndCommit). The `10.0` branch still
  registers exactly one (`GetOpenDocuments`) and its `schematic_commands.proto` is empty.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/api/api_handler_sch.cpp,
    https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/api/api_handler_sch.cpp

### (c) OpenDocument / CreateDocument / CloseDocument

- **[high]** `OpenDocument` has existed as a message since the original API commit `f613cd1cb4`
  (2023-01-29) but the common handler returned `AS_UNIMPLEMENTED`; it became real only in
  `caf1bcc455` (headless) and is still headless-only ("Not currently supported for GUI mode" in the
  kipy docstrings). `CloseDocument` and `CloseAllDocuments` came with `ca6f5ccf38` (08-05,
  "currently only supported in CLI api-server mode"); `CreateDocument` with `6c96d5a7df` (09-08,
  `Since: 11.0`, "Will return an error if the current document is unsaved: save or revert
  first"). The proto comment: "A document cannot be opened unless no project is currently loaded
  or it is part of a currently loaded project. To switch projects, use CloseDocument or
  CloseAllDocuments".
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/master/api/proto/common/commands/project_commands.proto
- **[high]** Two community attempts show what the maintainers do NOT want: !2543 Finerestaurant
  "Add OpenEditor/CloseEditor IPC API commands" (open since 2026-04-09; Jon: "I'm not sure whether
  or not we want to allow plugins to open/close the frames like this", "Where does the idea that
  there is a 'headless-first direction' come from? I would not say that is accurate", "This API
  could only possibly be used in GUI project mode"), and !2577 (OpenDocument to switch the active
  sheet; closed). Issue #25503 (buganini, 2026-09-10, "KiCad Instance Lifecycle Support in the IPC
  API"): Jon: "we don't want to make API guarantees around there being separate binaries",
  "it's probably best to talk about what end goals you have rather than assuming the kiway system
  and separate processes will keep existing". kiwrap must not propose anything that exposes
  KIWAY/frames/processes.
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2543,
    https://gitlab.com/kicad/code/kicad/-/work_items/25503

### (d) ImportNetlist, PlaceFootprintFromLibrary, PlaceSymbolFromLibrary

- **[high]** `ImportNetlist`: MR !2627 by dsa-t (Alex Shvartzkop, core team), created 2026-06-05,
  approved by Jon and merged 06-06, label `api`, commit `6fd26c3126`; 4 files
  (`api/proto/board/board_commands.proto` +42, `pcbnew/api/api_handler_pcb.cpp` +73,
  `api_handler_pcb.h` +3, `netlist_reader/board_netlist_updater.h` +6) and the paired kicad-python
  MR !48 "Add Board.import_netlist" (merged the same day). Review in full: Jon asked for "an
  overall machine-readable status even if you don't want to structure the whole report" -> Alex
  added `error_count`/`warning_count`/`new_footprint_count`; Jon: "can the netlist importer work
  without GUI?" -> Alex: `BOARD_NETLIST_UPDATER` needs a frame; Jon: "how much work would it take
  to remove that dependency / are you up for working on that?" -> separate commit `10f8e995`
  2026-06-10 "API: support netlist import in headless mode" (22 files; adds
  `pcb_netlist_utils.{cpp,h}`, `board_exchange_footprint.cpp`, `PCB_CONTEXT::MakeNetlistUpdater`).
  The report is still a free-text string.
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2627,
    https://gitlab.com/kicad/code/kicad-python/-/merge_requests/48,
    https://gitlab.com/kicad/code/kicad/-/commit/10f8e995025e938b0e0cb5d69a89bb49fc23c962
- **[high]** `PlaceFootprintFromLibrary` / `PlaceSymbolFromLibrary`: protos in `4437bf3879`
  2026-09-07 "API: Add protos for library management" (new `library_commands.proto`,
  `library_types.proto`: GetLibraryTable, Add/Update/DeleteLibraryTableEntry, GetLibraryStatuses,
  ReloadLibrary, LoadAllLibraries, GetLibraryItems, GetItemsFromLibrary, SearchLibraries - all
  `Since: 11.0`), implementation in `dc35a39731` 2026-09-13 "API: Implement placing
  symbols/footprints from libraries" (trailer `Assisted-by: GLM` - the core team uses the same
  disclosure rule they impose), `12cdf280b8` GetLibraryItems, `a97e25211a` GetItemsFromLibrary,
  `a8598ca89c` LoadAllLibraries (09-15). kipy: `e7a15587` "Add place_symbol / place_footprint from
  library", `b90bd8a4` get_library_symbol/get_library_footprint (09-17).
- **[high]** Rejected community designs for the same feature: !2264 liangtie "API Support for
  Design Block and Symbol Placement" (draft since 2025-06, closed by Seth 2026-08-27 "doesn't align
  with future plans"); !2332 Liuweijian123 "interactive symbol placement" (2025-10, closed) with
  Jon's review points that are still the rules: `DocumentSpecifier` must be the first field of
  every document command; interactive commands are named `Interactive*`; no per-command source
  files; no `wxLogWarning`; no `auto` parameters; do not generate Python in the CMake build; "Any
  dependency introduced [on EDA_BASE_FRAME] will eventually need to be removed before it will be
  possible to use this API in headless mode"; "the document validation should actually be
  implemented before giving the user any schematic API commands". Escain's !2714 "IPC-placed
  symbols are empty phantoms" and !2717 "IPC-placed footprints are empty shells" (2026-08-02) were
  closed by Seth the next day without comment; Jon then implemented the feature himself six weeks
  later.
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2332,
    https://gitlab.com/kicad/code/kicad/-/merge_requests/2264

### (e) Jobs over IPC (`RunBoardJobExport*`, `RunSchematicJobExport*`)

- **[high]** `46da153141` 2026-03-12 "ADDED: IPC API support for jobs" (direct push): new
  `api/proto/board/board_jobs.proto`, `api/proto/common/types/jobs.proto`,
  `api/proto/schematic/schematic_jobs.proto`, handlers in both editors, enum tests. Follow-ups:
  `ab00e101b5` 03-21 drill, `fee17a9580` 03-21 "Switch schematic job handlers to using context",
  `325a3e33b5` 08-02 missing Gerber fields, `c3570f4925` 08-02 Get/SetBoardPlotSettings,
  `19174c2b8b` (Seth, 08-31) drill maps/charts, `c7e77dbafb` 09-16 PNG. Board handlers on master:
  Export3D, Render, Svg, Dxf, Pdf, Ps, Png, Gerbers, Drill, Position, Gencad, Ipc2581, IpcD356,
  ODB, Stats; schematic: Svg, Dxf, Pdf, Ps, Png, Netlist, BOM. There is NO `RunBoardJobDrc` /
  `RunSchematicJobErc` even though `JOB_PCB_DRC`/`JOB_SCH_ERC` exist for kicad-cli.
- **[high]** kipy wrappers: `board_jobs.py` (Jon, March), `schematic_jobs.py` + `47085f83`
  (Béla Szalontai, MR !46 "Expand schematic IPC wrapper coverage", 2026-05-20 -> 09-12, plot,
  netlist, BOM and hit-test wrappers), `f79cebd1` 09-18 "export_bom: make fields required; add
  reasonable defaults for format" (the pinned commit). Jon on issue kicad-python#109: "If you would
  like to expose export jobs to the schematic, I'm OK with that, but please note that the non-jobs
  part of the API is not going to be considered stable until next year when we release KiCad 11",
  "If you want [a stable and documented API surface], please wait until ~March 2027".
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/work_items/109,
    https://gitlab.com/kicad/code/kicad-python/-/merge_requests/46

### (f) GetSchematicNetlist / GetSchematicHierarchy

- **[high]** `48f4d67cfe` 2026-04-16 "API: Add GetSchematicHierarchy" and `b90628aa0d` 2026-04-23
  "API: Add GetSchematicNetlist" - both direct pushes touching exactly
  `api/proto/schematic/schematic_commands.proto`, `schematic_types.proto`,
  `eeschema/api/api_handler_sch.{cpp,h}` (no QA test added in the commit; the proto round-trip
  tests live in `test_sch_api_proto.cpp`). `df8f79916a` 04-19 "Fix up subsheet handling",
  `118c18a436` 09-12 populate `path_human_readable` (issue #24439, fix-committed 11.0), `2f85f55f76`
  09-07 sheet-specific page info. kicad-python #103 ("full schematic access instead of current
  sheet only") closed 09-12: "See `iter_sheets` and `get_items` now takes an optional path argument".
  Both commands are `11.0`-only (not on `10.0`).
  - Sources: https://gitlab.com/kicad/code/kicad/-/commit/b90628aa0d5e9a3d8ff1e6121270651663ab298c,
    https://gitlab.com/kicad/code/kicad/-/commit/48f4d67cfe5637d8e219cfd45ab96e70fb678866

### (g) Issue #24966 and its fixes (`6545d4a2`, `f3105844`, milestone 10.0.7)

- **[high]** Issue #24966 (grandfatherpikhto, 2026-07-17): `Board.update_items()` from kipy 0.7.1
  crashes KiCad 10.0.4 when the schematic editor is open and no interactive edit happened yet.
  First bot-closed for a missing version block, reopened; dsa-t posted a WinDbg stack
  (`SRV*https://symbols.kicad.org/kicad-stable`); the reporter's corrected ProcDump stack pins it
  to `API_HANDLER_EDITOR::checkForBusy()` (`api_handler_editor.cpp:207`) with a null frame reached
  from `handleUpdateItems` - the schematic handler was answering a board request. Labels `api`,
  `crash`, `priority::high`, `status::fix-committed`, milestone 10.0.7. #25322 (08-22, first
  `begin_commit()` with schematic open crashes) closed as duplicate.
  - Sources: https://gitlab.com/kicad/code/kicad/-/work_items/24966
- **[high]** Fixes on the `10.0` branch, Jon Evans 2026-09-06: `6545d4a28d` "Fix shadowed m_frame
  in API_HANDLER_SCH" (API_HANDLER_SCH now passes the frame to the base ctor; drops its own
  `m_frame`; `SCH_EDIT_FRAME::doCloseWindow` deregisters the handler first "as an additional safety
  net") and `f3105844c6` "API: add document specifier for BeginCommit/EndCommit" (`BeginCommit.document`
  = field 1, `EndCommit.document` = field 4, both `// Since: 10.0.7`; new static
  `rejectIfMultipleEditors()` returns `AS_BAD_REQUEST` "BeginCommit without a document specified is
  not allowed when multiple editors are open"; `KICAD_API_SERVER::Handlers()` added). Also on 10.0:
  `01640bb4` "Align Begin/EndCommit with V11 API". Master equivalents: `bdcb150903` 09-06 and Seth's
  `63ace6104b`/`dbcbae087c` 08-21 "api: check document ownership before the editor busy check".
  kicad-python `a8c63528` 09-06 "begin_commit and end_commit must include a document".
  **Consequence for kiwrap:** kipy 0.8.0 sends no `document` in BeginCommit; on 10.0.7+ with
  both editors open it will get `AS_BAD_REQUEST`; kipy main sends it (and needs the 10.0.7+ server
  to honour it - on 10.0.6 the field is unknown and ignored).
  - Sources: https://gitlab.com/kicad/code/kicad/-/commit/6545d4a28d4978edb96d038328500d8fde58b3dd,
    https://gitlab.com/kicad/code/kicad/-/commit/f3105844c6dbe68bc3a0154f11c83e6de816c4ec
- **[high]** The review-order rule this produced is now enforced in MRs: in !2808 (09-18) Jon:
  "We want the order to be document check -> busy check (the headless check can happen first
  because GUI or not is independent of how many document handlers are open) ... if you have two
  editors open, if one is busy and the API requests targets the other one, you can get a false
  error. By putting the document check first, you bypass the rest of the handler and defer to
  another handler". (`ImportNetlist` on master still does busy -> document; new code must not
  copy it.)

### (h) kicad-python: headless `KiCadServer` and main-branch changes since 0.8.0

- **[high]** `kipy/server.py` was added in `bf458267` 2026-03-21 "Support headless kicad-cli
  server mode" (Jon) and only reformatted since. `KiCadServer(kicad_cli_path, file_path=None,
  socket_path=None)`: socket `<tempdir>/kicad/kicad-python/api-<pid>-<8hex>.sock`, `Popen([cli,
  "api-server", "--socket", path, file])` with piped stdout/stderr drained by daemon threads,
  readiness = a stdout line starting with `"KiCad API server listening at "` (then the socket path
  is taken from that line), `wait_for_ready(timeout_s=15)`, `stop()` = `terminate()` on Windows /
  SIGTERM elsewhere, `wait(5)`, then `kill()`. `find_kicad_cli()` on Windows globs
  `%ProgramFiles%/KiCad/*/bin/kicad-cli.exe` and picks the highest version directory.
  **There is no `env=` parameter**: the server inherits `os.environ`, so kiwrap cannot pass the
  sandbox env (`KICAD_CONFIG_HOME` etc.) through `KiCad(headless=True, ...)` without either setting
  process-global env or subclassing/replacing `KiCadServer`. (Small upstream candidate.)
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/raw/main/kipy/server.py,
    https://gitlab.com/kicad/code/kicad-python/-/commits/main/kipy/server.py
- **[high]** Tags: 0.8.0 = 2026-08-30 (`473a03cb`), 0.7.1/0.7.0 = 04-17, 0.6.0 = 03-15. `main` is
  0.9.0.dev0; its `kicad` submodule at the pinned `f79cebd1` points to KiCad `c7e77dbafb`
  (2026-09-16 "API: Support PNG plotting"), which is an ancestor of the nightly HEAD `6dd893f7d1`
  [VERIFIED LOCALLY with `git merge-base --is-ancestor`], so the pinned protos match the installed
  nightly. Since 0.8.0 (86 commits, 08-30..09-18, almost all Jon): schematic docs and ergonomics
  (`iter_sheets`, `schematic.for_sheet`, `get_items(path=)`), `CreateDocument` (`a303d584`), library
  load/`get_library_items`/`place_symbol`/`place_footprint`, design variants, embedded files,
  custom properties, tables/reference points/grid items/constraints, `Project.set_net_classes`,
  `KiCad.get_paths`, `is_document_modified`, plot settings, drill chart/map, PNG plotting, mypy +
  ruff in CI (!52), `protobuf` pinned to the exact point release (issue #108, `b7c3cb72`),
  `protoc-wheel-0==29` added to build requirements (issue #140, `cc6ed91a` 09-16), "Handle unknown
  git versions more gracefully" (`96c004cb`). Outside contributions merged in this window:
  set-soft (Salvador Tropea) !51 export_drill_gerber/excellon, !53 Pad.fab_property, !54 courtyard
  exemption; Béla Szalontai !46. Open: !55 `move()` for all items, !56 `__repr__`, !39
  open_editor/close_editor (paired with KiCad !2543).
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/commits/main,
    https://gitlab.com/kicad/code/kicad-python/-/merge_requests
- **[high, VERIFIED LOCALLY]** Root cause of the "bare `common.types` imports" packaging issue
  (reproduced with `uv pip install -v "kicad-python @ git+...@f79cebd1"` in a fresh venv, log in
  scratchpad `upstream-research/kpy_build.log`): `build.py` -> `tools/generate_protos.py` runs
  `protoc` then `protol` (protoletariat 3.3.10) to rewrite the generated absolute imports into
  relative ones. On Windows the `protoc.exe` installed by `protoc-wheel-0` is a Python console
  script whose `exec_protoc()` calls `os.execv(PROTOC_EXE, argv)`; on Windows `execv` spawns the
  real compiler and returns immediately, so `subprocess.run([...], check=True)` sees exit 0 before
  protoc has written anything. protoletariat's `Protoc.generate_file_descriptor_set_bytes()` reads
  its descriptor-set temp file straight after that call, gets 0 bytes, finds 0 files and rewrites
  nothing - silently. (With captured stdio the pipes keep the parent alive until the grandchild
  exits, which is why a capturing repro "works".) The PyPI 0.8.0 wheel is built on Linux with a
  real `protoc` and has `from ...common.types import enums_pb2`. Verified fix: run the real binary
  - `from protoc import PROTOC_EXE` (exported by protoc-wheel-0) and pass it as the protoc path
  and as `protol ... protoc --protoc-path <PROTOC_EXE>`; with that the Windows output is
  `from ...common.types import enums_pb2` (scratchpad `upstream-research/protogen2/`). A second
  consequence: uv/pip check the `kicad` submodule out shallow (`.gitmodules: shallow = true`), so
  `git describe --long` fails and `kipy/kicad_api_version.py` is `"unknown"` -> `check_version()`
  is skipped for git installs. A `.pth` that prepends `<site-packages>/kipy/proto` is NOT an
  acceptable long-term fix: it puts top-level packages named `common`, `board`, `schematic` on
  `sys.path` for every program in the environment. Proper fixes ranked: (1) upstream MR to
  `tools/generate_protos.py` (five lines; needs an issue first), (2) until merged, pin kiwrap to
  the fork `n3rd-bugs/kicad-python` carrying that patch, or generate kiwrap's own `_pb2` modules
  from the nightly's `api/proto` (D16 already plans a client generated from the nightly's protos).
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/raw/main/tools/generate_protos.py,
    https://gitlab.com/kicad/code/kicad-python/-/raw/main/build.py,
    https://gitlab.com/kicad/code/kicad-python/-/work_items/140,
    https://gitlab.com/kicad/code/kicad-python/-/work_items/82
- **[high]** kicad-python contribution norms from the tracker: issue kicad-python#109 (gwtteam /
  KiPilot): "You sound like an LLM. Have you read [tool-generated-content policy]?"; "Please remove
  the tests file. It introduces a testing paradigm (trivial mocking and tiny unit tests) that we
  don't currently use for this project"; "You should not modify the README.md just to talk about a
  specific feature"; "I'm not currently looking for help in the form of large LLM-generated
  contributions right now. If you or anyone else wants to help expand coverage in small easily-
  reviewable chunks that is better". Labels used: `accepting-mrs`, `requires-kicad-change`,
  `priority::*`. `tests/` holds only `test_geometry.py` and `test_symbol.py`.

### (i) `sch diff`

- **[high, VERIFIED LOCALLY]** MR !2620 Seth Hillbrand "Add native diff/merge for KiCad files",
  created 2026-05-30, merged 06-13, 226 files, no review comments; commit `4a6f6f52b2`. Adds
  `common/diff_merge/*` (kicad_differ, kicad_merge_engine, identity_reconciler, diff renderers),
  `common/jobs/job_{sch,pcb,fp,sym}_diff`, CLI `sch diff`, `pcb diff`, `fp diff`, `sym diff`,
  `gerber diff|info|convert png`, `mergetool`, `git-mergedriver`, plus GUI dialogs
  (`dialog_kicad_diff`, `dialog_kicad_merge_3way`, `dialog_git_mr_review`). Nightly help:
  `sch diff [--format json|text|png|svg] [-o PATH] [--exit-code-only] FILE1 FILE2`, exit code 0 =
  identical, 5 = differences found. Only follow-up commits: Damjan `2d427f7325` 06-16 "Compare
  Library with File" (symbol editor), Jeff `ec9670729e` formatting. Not on `10.0`.
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2620,
    https://gitlab.com/kicad/code/kicad/-/commit/4a6f6f52b21008146ec850b8649776a7f5a952c4

### (j) What is open or absent upstream: ratsnest, DRC/ERC over IPC, net stats, JSON dumps, connectivity

- **[high]** **Ratsnest / airwire export: nothing.** No issue or MR mentions exporting ratsnest
  edges (searched MRs and issues for "ratsnest", "airwire", "unrouted", "GetRatsnest"). Closest
  precedent: MR !2707 Francois Belanger "pcbnew: add Unrouted Length column to the Net Inspector"
  (issue #25077, created 07-29, merged by Wayne Stambaugh 08-02, milestone 11.0) - it sums
  `RN_NET::GetEdges()`; Wayne blocked it until the per-call `std::stable_sort` in `GetEdges()` was
  avoided, so the MR added `RN_NET::GetTotalAirlineLength()`. Also `9624684909` (Alex, 06-09)
  "Refactor PCB_BASE_FRAME::Compile_Ratsnest into BOARD::CompileRatsnest" and `eff7182892` (Jeff,
  09-06) "Call board-level CompileRatsnest()" - the ratsnest can now be compiled from a `BOARD`
  without a frame, which is exactly what a headless `pcb export ratsnest` job or an IPC
  `GetRatsnest` handler needs. On merge Wayne offered to credit the contributor in the forum's
  "Post-V10 New Features and Development News" thread - features get announced there.
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2707,
    https://gitlab.com/kicad/code/kicad/-/work_items/25077
- **[high]** **RunDRC / RunERC over IPC: absent, and the maintainers have said why.** Master
  registers only `InjectDrcError` (board) and no ERC command; `RunBoardJobExport*` covers exports
  only. kicad-python#14 "Provide a method for running DRC for an item" (mairas, 2025-02; labels
  `requires-kicad-change`): Jon: "it is not currently really possible to do this in KiCad code, so
  that would have to be fixed before it could be added to the API". Since then: `eeac1a1c17`
  (08-16) made DRC/ERC markers and exclusions protobuf types (`board_rules.proto`,
  `schematic_rules.proto`), `4952e90b44` (08-05) added missing DRC codes, `0a9a212eea` (08-27)
  "Allow retrieval of constraints", and the e2e tests cover `Get/SetBoardDesignRules` and
  `Get/SetCustomDesignRules`. The headless `kicad-cli pcb drc`/`sch erc` path (JOB_PCB_DRC,
  JOB_SCH_ERC) already runs the engines without a frame, so a `RunBoardJobDrc` following the jobs
  convention is the plausible shape; nobody has proposed it. Escain's !2724 "'Update PCB from
  Schematic' has no headless path" (08-02) was closed unreviewed.
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/work_items/14,
    https://gitlab.com/kicad/code/kicad/-/commit/eeac1a1c17b4c7003f2bbe2a4373fd1aed7524c0
- **[high]** **DRC JSON marker position: in progress upstream - do not duplicate.** Issue #25285
  nordic-style (Konnect MCP author), 2026-08-19: the JSON report has only `items[].pos`, not the
  marker position/layer (e.g. `copper_sliver` has no item). MR !2780 "DRC JSON export: Add marker
  position and layer" (08-23; 4 files: `include/rc_json_schema.h` adds
  `std::optional<COORDINATE> pos` + `wxString layer` to `VIOLATION`, `pcbnew/drc/drc_report.cpp`
  fills them from the `PCB_MARKER` parent for violations, unconnected_items and schematic_parity,
  `resources/schemas/drc.v1.json`, a new `qa/tests/pcbnew/drc/test_drc_copper_sliver.cpp` case).
  The issue was assigned to the author; MR rebased 09-07 and "now mergeable"; no maintainer review
  yet. kiwrap's DRC parser must accept `violations[].pos` / `.layer` as optional.
  - Sources: https://gitlab.com/kicad/code/kicad/-/merge_requests/2780,
    https://gitlab.com/kicad/code/kicad/-/work_items/25285
- **[high]** **ERC JSON x100 bug: reported upstream 2026-09-17, unassigned.** Issue #25582
  paulius.zaleckas "kicad-cli sch erc: JSON report coordinates are 100x too small (text report is
  correct)" - `127 -> 1.27`, all units; `status::new`, `priority::undecided`, no milestone. This
  is the bug behind kiwrap's `cli.erc_json_pos_scale = 100`. Escain's earlier fix !2727 (08-02,
  "ERC JSON positions off by 100x, locale-dependent decimals") was closed by Seth with
  "We're trying to remove LOCALE_IO not add more" (label `status::awaiting-changes`). A minimal fix
  that only uses the schematic IU scale in the ERC JSON writer (cf. Seth's own `c02e4c8844` for
  HitTest) has not been submitted. Related: #23948 "broken schema links in ERC and DRC Json output
  files" (confirmed, low), #25434 "kicad-cli pcb drc truncates the report at 499/199 violations
  per type and reports the excess shorts as clearance violations" (`DRC_ENGINE::RunTests()`
  `ERROR_LIMIT`/`EXTENDED_ERROR_LIMIT` apply to every caller; closed by the reporter, NOT fixed),
  #25413 "crash kicad-cli pcb drc when there is no window server" (macOS, fix-committed 10.0.7),
  #25447 "kicad-cli sch erc crashes in _cvpcb.dll on full hierarchical project ERC" (10.0.7).
  - Sources: https://gitlab.com/kicad/code/kicad/-/work_items/25582,
    https://gitlab.com/kicad/code/kicad/-/merge_requests/2727,
    https://gitlab.com/kicad/code/kicad/-/work_items/25434
- **[high]** **Net length / statistics: requested, unanswered.** kicad-python#96 "Add an IPC
  function to get a full length of a net" (2026-03-11, wraps `BOARD::GetTrackLength()` which
  returns count/length/package length/delay) - open, no maintainer reply. Board-level
  `RunBoardJobExportStats` exists (counts only). The Net Inspector's per-net lengths are GUI/CSV
  only. `2204` (JamesJCode, 2025-03) "Unify length calculation between router, board / frame, and
  DRC" is the shared implementation an API command would call.
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/work_items/96
- **[high]** **Structured JSON dump of a board/schematic: not wanted as a file-format API.**
  Issue #19870 (`ParseAndCreateItemsFromString` stub) 2026-09-13, Jon to the KiPilot author:
  "The problem is that the file format is not really an API. The readout is a debug tool but I
  am concerned that if people start writing workflows (especially AI ones) around writing
  s-expressions, that is not actually the right API boundary. The point of the API is to abstract
  away from things like the s-expression file format ... Teaching AI to write s-expressions is a
  cheap hack and not the right way IMO" and "I am hesitant to actually implement this API".
  Expect a `pcb export json` proposal to be redirected to `GetItems` over IPC. (kiwrap's CST stays
  an internal bridging layer; do not pitch it upstream.)
  - Sources: https://gitlab.com/kicad/code/kicad/-/work_items/19870
- **[high]** **Connectivity queries: done for boards (10.0.1).** `5051a0398c` 2026-03-24 "API:
  Add connected items helpers" (`GetConnectedItems`, `GetItemsByNet`, `GetItemsByNetClass`;
  issue #22990, backported as `da800cc8` on 10.0, milestone 10.0.1). Schematic side:
  `GetSchematicNetlist` (11.0) and the new connectivity engine (!2807). Events/notifications:
  kicad-python#38 - Jon: "not planned ... no mechanism for server-to-client notifications ...
  large lift".
- **[medium]** Other open API items worth knowing: #25542/!2808 `FocusOnItems` (Benjamin Chung,
  approved 09-18, interactive-only); #25384 "implement UpdateBoardStackup API" and #22737
  "unfractured polygon" are labelled `starter` (the tool-generated-content policy forbids AI
  tools on `starter` issues); #23994/#20880 one named pipe per machine on Windows (still open;
  the headless `--socket` sidesteps it); #21580 `get_footprints()` fails from a jobset; !2617
  "skip PYTHONHOME/PYTHONPATH for plugin venv under the Linux AppImage" (open). MCP servers by
  other people are already colliding with kiwrap's space and shaping maintainer patience:
  KICAD_MCP (Escain, 25 MRs closed unreviewed on 08-03), KiPilot (gwtteam), Konnect (nordic-style,
  mixelpixx), kikakuka (buganini).

### Review norms visible across the MR discussions (what gets an MR merged or closed)

- **[high]** Process: an MR must close an issue that a team member assigned to you first;
  otherwise the bot closes it (!2802: "KiCad policy requires each merge request to close an
  issue. A KiCad team member must assign that issue to you before you start work"). Feature MRs
  without an issue are closed even when they are good (!2699 pybind11 SCH_IO_MGR: "We already
  have an API system and have moved away from directly integrating Python ... please open issues
  for the features you want ... and comment on them before starting work"). Check "Allow commits
  from members who can merge" or the maintainer cannot rebase for you (!2592 was merged by hand
  because the box was unchecked). Rebase, never merge master into your branch.
- **[high]** Tool-generated content: `Assisted-by: <tool>` trailer on substantial AI-assisted
  code (the core team uses it: `dc35a39731` "Assisted-by: GLM", `eeac1a1c17` "Refactoring
  Assisted-by: Kimi"); MR/issue/commit text and code comments must be human-written (!2779: "This
  comment reads like AI-generated text which is not permitted", "remove *all* AI-generated
  comments"; !2770 Seth: "It is much more efficient for me to just tell the AI agents what to do
  than pointing out all the places where your AI agent is doing the wrong thing ... Each review
  takes a core developer at least 30-60 minutes ... Each MR needs to be fully baked and fully
  tested"). Issue labels `incl-AI-content` and `AI Slop` exist and are applied.
- **[high]** Tests: no trivial mocked unit tests (!2583: "I do not think simple mocked tests like
  that are valuable. If you want to develop a test for this, please do it as an integration test
  (not using mocks) and put it in its own commit"; !2779: "Tests should be covering tricky
  behavior, regressions, etc, we generally don't go for TDD-style coverage of basic
  functionality"); use the issue's reproduction project as the QA fixture and the existing
  harness (!2770); a test "that passes with and without the code" is rejected.
- **[high]** Code: KiCad coding style is enforced line by line (spaces inside parens, no `auto`
  unless the type is on the RHS, braces symmetric with the else branch, no descriptive comments
  that restate code); dialogs must be edited in wxFormBuilder `.fbp`; reuse existing CLI flags
  (`--variant`); performance regressions block (!2707); `LOCALE_IO` is being removed; no new
  frame dependencies in API handlers; handler check order document -> busy (-> headless first).
- **[high]** Scope: "small easily-reviewable chunks" beat one big MR; overly broad work items
  are closed ("This work item should be closed because it is way too broad"); design proposals
  should state end goals, not prescribe KIWAY/process-level mechanisms.
- **[high]** Milestones/backports: API feature work is milestoned `11.0`; board data-model
  additions and small board commands ARE backported to the `10.0` branch by Jon (the 10.0 `api/proto`
  log shows 08-28..09-06 backports: tables, teardrops, embedded files, variants, FlipItems,
  GetBoardLayerByName; annotated `Since: 10.0.6` / `Since: 10.0.7`), while the headless server,
  the schematic API, jobs, library management and document lifecycle stay master-only
  (`Since: 11.0`). The 10.0 kicad-cli DRC fix !2545 shows a 10.0-targeted MR is accepted when the
  master code has diverged ("if the problem exists there it should be a different commit not a
  cherry-pick", milestone 10.0.4).

## Capabilities (upstream facts kiwrap can rely on)

- Headless `kicad-cli api-server --socket <path> [file]` with `OpenDocument`/`CloseDocument`/
  `CloseAllDocuments`/`CreateDocument`, schematic + board in one process, `SaveDocumentToString`
  for both, revert in headless; e2e QA precedent for a shared long-lived server.
- Full schematic read/write API on the nightly (36 handlers) including netlist, hierarchy,
  variants, jobs, library placement; all `Since: 11.0`.
- `ImportNetlist` works headless (since `10f8e995`) and returns counts + free-text report.
- Board connectivity queries (`GetConnectedItems`, `GetItemsByNet`) on 10.0.1+.
- `sch diff --format json` with exit code 5 on differences.
- kicad-python main tracks the nightly protos within days; the maintainer merges small, human-
  written, non-mocked contributions quickly (set-soft's three MRs merged in 1-3 days).

## Limitations (what upstream does not give us)

- No DRC/ERC execution or marker retrieval over IPC (only `InjectDrcError`); no
  ratsnest/airwire export anywhere; no net length/statistics command; no structured full dump; no
  server-to-client events; no `env` injection in `KiCadServer`.
- Non-jobs schematic API explicitly unstable and undocumented until KiCad 11 GA (~March 2027);
  proto package names changed as late as 2026-08-26.
- DRC JSON lacks marker positions until !2780 merges; ERC JSON positions are x100 too small
  (#25582 open); DRC reports are capped at 199/499 violations per type (#25434, unfixed).
- Git installs of kicad-python on Windows produce unusable `_pb2` imports (protoc-wheel-0 execv
  race) and report `KICAD_API_VERSION="unknown"`.
- Maintainer bandwidth for API review is one person (Jon) and is being consumed by AI-generated
  MCP-server MRs; expect slow or blunt reviews and plan contributions accordingly.
