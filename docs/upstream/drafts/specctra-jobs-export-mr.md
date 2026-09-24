# MR draft: `pcb export specctra` (commit `9fe37dff4d` of `mr/specctra-jobs`; NOT opened, branch pushed to the private fork only, 2026-09-20)

Branch `mr/specctra-jobs` on `n3rd-bugs/kicad-fork` (private; never pushed to `public` or
GitLab) = `base/6dd893f7d1` (nightly 10.99.0-4083) + two commits, one per command
(docs/UPSTREAM.md §3.4: one logical change per MR, the G1 ratsnest branch is the template).
This draft is the FIRST MR; `specctra-jobs-import-mr.md` is the second, opened once this one has
landed (it only shares the `kicad_cli.cpp` registration block and the QA files).

| Commit | Subject | Files |
|---|---|---|
| `9fe37dff4d` | ADDED: Export a board as Specctra DSN (pcb export specctra) | 11 files, +502: `common/jobs/job_export_pcb_specctra.{h,cpp}` (JOB "specctra", `SetDefaultOutputPath` -> `<board>.dsn`, `REGISTER_JOB( pcb_export_specctra, "PCB: Export Specctra DSN" )`), `kicad/cli/command_pcb_export_specctra.{h,cpp}` (a `PCB_EXPORT_BASE_COMMAND( "specctra" )`, `-o` only), `kicad/kicad_cli.cpp` (+3), `pcbnew/pcbnew_jobs_handler.{h,cpp}` (+55: `JobExportSpecctra`, registered without a settings dialog), 2 CMakeLists, `qa/tests/pcbnew/test_specctra_export.cpp` (+94, 1 case in the existing `SpecctraExport` suite), `qa/tests/cli/test_pcb_specctra.py` (+168, 2 cases) |

## What the job does and does not do

- `JobExportSpecctra` loads the board (`getBoard`, the jobs handler's cached loader), resolves
  the output like every other export (`resolveJobOutputPath`, default `<board>.dsn` next to the
  working output path, `PATHS::EnsurePathExists`) and calls the editor's own
  `DSN::ExportBoardToSpecctraFile( brd, outPath )`. The file is byte for byte what File > Export
  > Specctra DSN writes: `(resolution um 10)`, six significant digits, unlocked tracks and vias
  `(type protect)` (upstream b76f3d52f5), locked ones `(type fix)`, zones as `(wire (polygon
  ...) (window ...))` (upstream 31220febea), keepouts, the fractured outline as `boundary`.
- A board whose outline is not a closed polygon is not an error: `ExportBoardToSpecctraFile`
  writes the bounding box and `wxLogWarning`s "Board outline is malformed. Run DRC for a full
  analysis." - in kicad-cli that is a `Warning:` line on stderr (the console app has no dialog),
  exit code 0, the DSN written. `IO_ERROR` (unwritable output, a footprint without a
  reference) is reported through the job reporter and exits `ERR_UNKNOWN`.
- No new options. The exporter has none in the editor either; `--output` is the base command's.
- Nothing changes for the editor: the exporter function is untouched, the menu entry keeps
  calling it.

## Verification (Windows, RelWithDebInfo `msvc-win64-release`, VS 2022 17.14 / MSVC 14.44)

- Incremental build after the commit: ~1 min (job + CLI + handler + QA binaries).
- `qa_pcbnew.exe --run_test='SpecctraExport*'`: 3/3 (the two pre-existing cases plus
  `SpecctraExportCarriesBoardContents`: on a routed board the DSN has every copper layer in
  order, every footprint as a `place` with its side and rotation, every net with its pins, and
  one two-point `wire` per track segment; re-run 2026-09-20 on the integration commit
  `acdc6a3061`, "No errors detected").
- `qa_cli` `test_pcb_export_specctra` (same contents check through the command; the default
  output name `<board>.dsn`; exit 0) and `test_pcb_export_specctra_missing_board` (exit
  `ERR_INVALID_INPUT_FILE`, no file written); 5/5 for the file (with the import cases of the
  second commit), 2.3 s, re-run 2026-09-20.
- `qa_api.exe` full run on the integration branch: 108/110, the two known test-side failures
  (`ApiE2E/CreateDocument` path separator, `ApiWizards/QueryWizards` Windows mock gap,
  `issue-qa-api-windows-findings.md`); nothing in this commit touches the API.
- `clang-format` (LLVM 19.1.5, `_clang-format`) `--dry-run --Werror` clean on every new file and
  on the changed ranges of `pcbnew_jobs_handler.cpp` and the QA file.
- Oracle test from kiwrap on the installed build (`C:/kiwrap-builds/6dd893f7d1-5`,
  `tests/test_cli_specctra.py::test_dsn_export_equals_swig_export[ecc83|blinker|coldfire]`):
  the DSN of the command is semantically equal to what KiCad 10.0.6's SWIG
  `pcbnew.ExportSpecctraDSN` writes for the same board file (layers, boundary, rules, via
  padstacks, keepouts, placement, images with pins/outlines/keepouts, padstacks, nets and pins,
  classes, every track segment and via), on the ecc83 demo (2 layers), a small 2-layer board
  with a GND zone, and the 4-layer coldfire demo. Two master-side differences are normalised
  and documented there: protect vs route wire type, and zones as wiring polygons vs planes.
- Freerouting 2.4.1 (Java 25) accepts the command's DSN and routes it: ecc83 with half of its
  copper removed, the blinker with 70% of its nets stripped -> session -> import -> `pcb drc
  --refill-zones` 0 violations, 0 unconnected.
- T4 `--help` tree of the installed build = the stock nightly's + `pcb export specctra`
  (`--help`, `--output`) (+ the ratsnest command of the other branch); T5 IPC handler snapshot
  EQUAL to the stock nightly's.
- Observed while testing, not part of this MR: KiCad's own DSN reader (`SPECCTRA_DB::LoadPCB`,
  used by nothing in the tree) rejects KiCad's own export at `(PN ...)` because the keyword
  table only knows `pn`; inert, could be a separate one-line fix.
- Found by the same oracle test after this draft was written (2026-09-20, Phase 1c review):
  `makeIMAGE()` exported a footprint's rule areas from the zone's LIBRARY-frame outline as if it
  were board geometry (un-rotated about the footprint position, position subtracted), so a placed
  part's keepout landed tens of millimetres away and the router routed through the real one. That
  is a bug in the exporter this job calls, not in the job: fixed on its own branch
  `mr/specctra-fp-zone-frame` (`specctra-fp-zone-frame-mr.md`, one commit, its own MR), built into
  the bench `C:/kiwrap-builds/6dd893f7d1-6` together with the two commands of this draft.

## MR description (for GitLab)

Title: `Export a board as Specctra DSN from kicad-cli and jobsets (pcb export specctra)`

External autorouters such as Freerouting take a Specctra DSN design file as their input. Until
now the only ways to write one were the board editor's File > Export > Specctra DSN menu entry
and, on KiCad 10 and older, the SWIG helper `ExportSpecctraDSN()` of the bundled Python, which
no longer exists. A tool that routes boards from outside KiCad, or a jobset that hands a board
to a router, had no headless way to get the DSN.

This adds `JOB_EXPORT_PCB_SPECCTRA`, run by the pcbnew jobs handler around the existing
`DSN::ExportBoardToSpecctraFile()`, and the kicad-cli command
`pcb export specctra [-o FILE] BOARD`. The output defaults to `<board>.dsn` like the other
exports; a malformed board outline is reported on stderr through `wxLogWarning` and the export
still writes the bounding box boundary, as the editor does. The job is registered for jobsets
without a settings dialog (the exporter has no options in the editor either).

Tested on Windows against master at 6dd893f7d1: `qa_pcbnew` (`SpecctraExportCarriesBoardContents`
in the existing suite: every copper layer, footprint, net with pads and track of a routed board is
in the file), `qa_cli` (`test_pcb_export_specctra`, `test_pcb_export_specctra_missing_board`).
Outside the tree, the file equals what KiCad 10.0.6's `pcbnew.ExportSpecctraDSN` writes for the
same boards apart from the two intentional master changes (protect wires, zones as wiring
polygons), and Freerouting 2.4.1 routes it.

A follow-up MR adds the matching import (`pcb import-ses`).

Tool disclosure: the code was written with an LLM assistant (Claude Code) driving the build and the QA runs; the design, the review of every line
and the verification above are mine.
