# G6 "job handlers absent headless": no issue to file (NOT posted; 2026-09-19)

docs/UPSTREAM.md G6 planned an ask-first issue ("are the `RunBoardJobExport*` /
`RunSchematicJobExport*` handlers missing from the headless server by omission or by decision?")
before patching `HEADLESS_PCB_CONTEXT` / `HEADLESS_SCH_CONTEXT`. **There is nothing to ask:** the
handlers are registered and work on the headless `kicad-cli api-server` of the stock nightly.
The 22 `-> AS_UNHANDLED` rows in the first T5 snapshot were produced by kiwrap's probe, not by
KiCad. This file records the evidence so the gap card can be rewritten and so nobody opens the
issue.

## What the source says (6dd893f7d1, read 2026-09-19)

- `pcbnew/api/api_handler_pcb.cpp:170-196`: the 15 `registerHandler<RunBoardJobExport*, ...>`
  calls sit in the `API_HANDLER_PCB( std::shared_ptr<PCB_CONTEXT>, PCB_EDIT_FRAME* = nullptr )`
  constructor with every other handler, unconditionally. `pcbnew/pcbnew.cpp:1101` and `:1179`
  (`IFACE::handleOpenPcb` / `handleCreatePcb`, the headless api-server paths) construct exactly
  that handler with a `HEADLESS_PCB_CONTEXT` and a null frame. There is no separate headless
  registration list to add to.
- `eeschema/api/api_handler_sch.cpp:159-172`: same for the 7 schematic jobs;
  `eeschema/eeschema.cpp:999` / `:1063` construct `API_HANDLER_SCH( m_openContext )` headless.
- The job handlers need a KIWAY, not a frame: `ExecuteBoardJob( PCB_CONTEXT*, JOB& )`
  (`api_handler_pcb.cpp:2190`) runs `aContext->GetKiway()->ProcessJob( KIWAY::FACE_PCB, &aJob,
  &reporter )`; `HEADLESS_PCB_CONTEXT` takes the KIWAY in its constructor
  (`headless_pcb_context.h`, `pcbnew.cpp:1073` passes `m_kiway`). `ExecuteSchematicJob( KIWAY*,
  JOB& )` likewise. `checkForBusy()` returns "not busy" when `m_frame` is null.
- Commits 46da153141 ("ADDED: IPC API support for jobs", 2026-03-12) and fee17a9580 ("Switch
  schematic job handlers to using context", 2026-03-21) are the history: the schematic handlers
  were moved from the frame to the context precisely so that they run headless; the board ones
  were written against `PCB_CONTEXT` from the start.
- Upstream already tests this: `qa/tests/api/test_api_jobs.cpp` (suite `ApiJobs`, 6 cases:
  `ExportBoardSvg`, `ExportSchematicPng`, `ExportBoardDrill`, `ExportBoardPng`,
  `ExportSchematicNetlist`, `ExportSchematicBom`) runs every one through `API_SERVER_E2E_FIXTURE`,
  i.e. a real `kicad-cli api-server` child. On the pristine RelWithDebInfo bench build:
  `qa_api.exe --run_test=ApiJobs` -> 6/6 passed, 81 assertions, 1.6 s.

## Why T5 said "unhandled"

`API_HANDLER_PCB::validateDocumentInternal` (`api_handler_pcb.cpp:353`) answers
`AS_UNHANDLED` when `aDocument.type() != DOCTYPE_PCB`, on purpose: "no message needed for
AS_UNHANDLED; this is an internal flag for the API server" so that another editor's handler can
claim the request. The job requests carry their document inside `RunJobSettings job_settings`
(`api/proto/common/types/jobs.proto`), and kiwrap's T5 "minimal instance" only filled top-level
`DocumentSpecifier` fields and `ItemHeader.document`. An empty nested document has type
`DOCTYPE_UNKNOWN`, every handler passes, and the server reports "no handler available for request
of type kiapi.board.jobs.RunBoardJobExportSvg" - the exact text in the 22 rows.

Verified live (stock nightly 10.99.0-4083 and bench 6dd893f7d1-2, kicad-python f79cebd,
scratchpad g6/probe_jobs.py):

| Request | Answer |
|---|---|
| `RunBoardJobExportStats` with empty `job_settings` | `AS_UNHANDLED` "no handler available for request of type kiapi.board.jobs.RunBoardJobExportStats" |
| same with `job_settings.document` = the open board | `AS_OK`, `JS_SUCCESS`, `ecc83-pp_statistics.rpt` written, 0.02 s |
| `Board.export_stats(json)` (kipy) | `JS_SUCCESS`, JSON with `board/components/pads/vias/drill_holes`, 0.03 s |
| `Board.export_svg()` with kipy's default (no layers) | `AS_OK`, `JS_ERROR` "At least one layer must be specified" (handler ran, job refused) |
| `Schematic.export_netlist(SNF_KICAD_XML)` | `JS_SUCCESS`, 0.14 s |
| `RunSchematicJobExportNetlist` with document, format unset | `AS_BAD_REQUEST` "RunSchematicJobExportNetlist requires a valid format" |

After `kiwrap.backends.ipc.handlers._minimal_instance` also fills `RunJobSettings.document`,
the regenerated `conformance/snapshots/ipc-handlers/10.99.0.json` differs from the old one in
exactly the 22 job rows: all 15 board jobs `unhandled -> handled` on the board, 6 schematic jobs
`unhandled -> handled` and the netlist `unhandled -> bad_request` on the schematic (each job is
still `unhandled` on the other document type, which is correct). Summary pcb: handled 52 -> 67,
unhandled 40 -> 25; schematic: handled 28 -> 34, bad_request 25 -> 26, unhandled 76 -> 69. The
bench snapshot `10.99.0+6dd893f7d1.json` is EQUAL to it; the Debug build's `.debug.json` still
differs in the single known `SetBoardDesignRules` row (no job enum converter asserts on an
UNKNOWN value, unlike `PageSize`).

## Consequences for kiwrap (done in this component)

- `ipc.headless_jobs` stays `true` for 10.99.0 / 11.0.0 (matrix), now backed by presence: the
  T5 rows above, `backends.ipc.jobs.probe_headless_jobs()` (one cheap job per document type) and
  `tests/test_ipc_patch_g6.py` (runs on stock and bench installs; no `kicad_patch` marker
  because there is no patch).
- `backends.ipc.jobs` is the live-session export provider (`export_board_stats` returns the same
  `BoardStats` as `structured.export_stats`; `export_board_svg`; `export_schematic_netlist`
  yields the same nets/nodes as `kicad-cli sch export netlist` on ecc83). kicad-cli remains the
  default for `kw_fab_export` (core owns the wiring).
- No `mr/g6-headless-jobs` handler change. The branch of that name carries only the optional
  QA e2e case for `RunBoardJobExportStats` (see `g6-mr.md`).

## Text to put in UPSTREAM.md instead of the G6 card (for its owner)

> ### G6 - Job exports on the headless server: **closed, not a gap** (2026-09-19)
>
> The 22 `RunBoardJobExport*` / `RunSchematicJobExport*` handlers are registered in the
> `API_HANDLER_PCB` / `API_HANDLER_SCH` constructors and run through `KIWAY::ProcessJob` on the
> context's KIWAY, which the headless contexts carry; upstream's `ApiJobs` e2e suite drives them
> through `kicad-cli api-server`. The first T5 snapshot sent an empty nested
> `job_settings.document` and `validateDocument` answered `AS_UNHANDLED` by design. Probe fixed
> (`handlers._minimal_instance`), snapshots regenerated, `ipc.headless_jobs = true` verified by
> `tests/test_ipc_patch_g6.py`. Live exports: `backends.ipc.jobs`. G2 (DRC/ERC jobs) does not
> depend on anything here: a `RunBoardJobDrc` handler copies `handleRunBoardJobExportStats` and
> runs headless the same way.

Also §1.5 "Exports over the live IPC session": tier 2 (not "GUI only"), provider
`backends.ipc.jobs`; §4.4 first-patch order: G6 drops out, G1 is first; the summary table row
for G6 goes.
