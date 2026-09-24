# MR draft: `pcb import-ses` (commit `d37c712bc1` of `mr/specctra-jobs`; NOT opened, branch pushed to the private fork only, 2026-09-20)

The SECOND MR of `mr/specctra-jobs` (see `specctra-jobs-export-mr.md` for the branch and the
first commit); to be opened once `pcb export specctra` has landed, rebased on `upstream/master`
on the day (`pcbnew_jobs_handler.cpp` and `kicad_cli.cpp` move often).

| Commit | Subject | Files |
|---|---|---|
| `d37c712bc1` | ADDED: Import a Specctra session into a board (pcb import-ses) | 11 files, +745/-1: `common/jobs/job_import_pcb_specctra_ses.{h,cpp}` (JOB "import_ses", `JOB_PARAM<wxString> session_file`, `REGISTER_JOB( pcb_import_ses, "PCB: Import Specctra Session" )`), `kicad/cli/command_pcb_import_ses.{h,cpp}` (`COMMAND( "import-ses" )`, `--ses SES_FILE`, `-o OUT_BOARD`, positional BOARD; `ERR_ARGS` without `--ses`, `ERR_INVALID_INPUT_FILE` for a missing board or session), `kicad/kicad_cli.cpp` (+5, a sibling of `pcb import`), `pcbnew/pcbnew_jobs_handler.{h,cpp}` (+95: `JobImportSpecctraSes`), 2 CMakeLists, `qa/tests/pcbnew/test_specctra_export.cpp` (+167, 1 case), `qa/tests/cli/test_pcb_specctra.py` (+276, 3 cases) |

## Semantics: the editor's, decided and documented

`JobImportSpecctraSes` wraps `DSN::ImportSpecctraSession( brd, sesFile, commit )` in a
`BOARD_COMMIT( toolManager, true, false )` on the headless tool manager the jobs handler already
creates for DRC (`getToolManager( brd )`), and pushes it with `SKIP_UNDO | SKIP_SET_DIRTY`. That
is exactly `PCB_EDIT_FRAME::ImportSpecctraSession` minus the frame (`BOARD_COMMIT( this )`,
`selectionClear`, the dialog), so the outcome is the editor's (`SPECCTRA_DB::FromSESSION`):

- every UNLOCKED track, arc and via is removed (`aCommit.Remove`), locked ones stay (they were
  exported `(type fix)` and a router leaves them out of the session);
- DRC markers are deleted (`DeleteMARKERs`);
- the session's `(placement)`, when present, moves and flips every footprint it names -
  locked footprints included, as the editor does (the importer never looks at
  `FOOTPRINT::IsLocked`; a router that did not move parts writes no placement, Freerouting
  does not);
- routes and vias are added with the session's widths and padstacks (the via drill from the
  `Via[..]_<size>:<drill>_um` padstack name);
- an unresolved reference, layer or padstack skips that item, counted and reported once
  (`Warning: <n> session item(s) were skipped due to unresolved reference, layer, or
  padstack.` on stderr);
- a session without its `session` / `routes` / `library_out` sections throws `IO_ERROR`: the
  commit is reverted, the error is reported, exit `ERR_INVALID_INPUT_FILE` (3), and the board
  file is untouched (nothing is saved on failure).

Then the board is saved: in place (`brd->GetFileName()`) when no output is configured, else to
the `-o` path (directory created). Only the board file is written; the project file is not
touched (the CLI adapter in kiwrap copies `.kicad_pro`/`.kicad_dru` next to an output in another
directory so DRC on the result keeps the board's rules - not something the job should do). The
reporter prints `Replaced <before> track items with <after> from the session` and the saved
path. In a jobset the session path is relative to the project (`wxFileName::MakeAbsolute(
project path )`).

Why `pcb import-ses` and not `pcb import ses`: `pcb import` takes the file to import as its
positional argument, and argparse only looks for a subcommand once a command's positionals are
consumed, so a subcommand of `pcb import` cannot be reached (the same reason `sch import` has
no subcommands). The name mirrors `pcb export specctra`'s object.

## Verification (Windows, RelWithDebInfo `msvc-win64-release`, VS 2022 17.14 / MSVC 14.44)

- Incremental build after the commit: ~1 min.
- `qa_pcbnew.exe --run_test='SpecctraImport*'`: 3/3 (the two pre-existing cases plus
  `SpecctraImportReplacesUnlockedCopper`: on a routed board with one locked track and one locked
  via, a session carrying two wires, a via and a placement for two footprints - after the
  import the unlocked copper is gone, the locked track and via are still there, the session's
  wires and via are present with their widths / size / drill / nets, the two footprints moved
  to the session's position, side and rotation, every other footprint stayed; re-run
  2026-09-20 on `acdc6a3061`, "No errors detected").
- `qa_cli`: `test_pcb_import_ses_round_trip` (export -> a session made of the DSN's own wiring
  -> import to `-o`: the same segments and vias within the 1 um the DSN's six significant
  digits keep, and `pcb drc` on both boards reports the same unconnected count),
  `test_pcb_import_ses_keeps_locked_copper_and_saves_in_place` (a locked track survives, the
  unlocked one is replaced, no `-o` overwrites the board), `test_pcb_import_ses_errors`
  (missing `--ses` -> `ERR_ARGS` (1); missing session file -> 3; a session without
  `routes` -> 3 with `Session file is missing the "routes" section` on stderr and the board
  bytes unchanged); 5/5 for the file, 2.3 s, re-run 2026-09-20.
- `qa_api.exe`: 108/110 on the integration branch (the two known test-side failures).
- `clang-format` 19.1.5 `--dry-run --Werror` clean on the new files and on the changed ranges
  of `pcbnew_jobs_handler.cpp` and the QA file.
- Oracle tests from kiwrap on the installed build (`C:/kiwrap-builds/6dd893f7d1-5`,
  `tests/test_cli_specctra.py`, 15/15 on 2026-09-20):
  - `test_freerouting_accepts_the_dsn_and_import_equals_swig_import`: ecc83 with half of its
    copper removed (seeded) and one track locked, routed by Freerouting 2.4.1 (Java 25, fanout
    off); `pcb import-ses` and KiCad 10.0.6's SWIG `pcbnew.ImportSpecctraSES` produce the same
    tracks and vias (positions within 1e-3 mm, layers, widths, nets), the same footprint
    positions, the locked track survives both, and `kicad-cli pcb drc --refill-zones` on the
    bench build reports the same violations and unconnected items for both results;
  - `test_four_layer_import_equals_swig_import`: the 4-layer coldfire demo with a session made
    of the DSN's own wiring plus a placement moving two footprints to the other side: equal to
    SWIG's import, the copper equals the original board's (2935 segments, 253 vias), the two
    footprints moved, the others did not;
  - `test_import_in_place_replaces_unlocked_copper`: a one-net session in place - locked
    tracks/vias stay, every other unlocked item is gone, the net's routes are back;
  - `test_freerouting_accepts_the_blinker_dsn`: a 2-layer board with a GND zone, 70% of its
    nets stripped and re-routed: 0 DRC violations, 0 unconnected after refill;
  - `test_bad_session_fails_and_leaves_the_board_alone`: `CliLoadError`, board bytes unchanged.
- T4 `--help` tree of the installed build = stock + `pcb import-ses` (`--help`, `--output`,
  `--ses`); T5 IPC handler snapshot EQUAL to the stock nightly's.

## MR description (for GitLab)

Title: `Import a Specctra session into a board from kicad-cli and jobsets (pcb import-ses)`

The routed result of an external autorouter comes back as a Specctra session file. Merging it
into the board was only possible from the board editor (File > Import > Specctra Session) or,
on KiCad 10 and older, through the SWIG helper `ImportSpecctraSES()` of the bundled Python,
which no longer exists.

This adds `JOB_IMPORT_PCB_SPECCTRA_SES`, run by the pcbnew jobs handler, and the kicad-cli
command `pcb import-ses --ses FILE [-o OUT_BOARD] BOARD`. The job wraps
`DSN::ImportSpecctraSession()` in a board editor commit on the headless tool manager, exactly as
the editor does, so the outcome is the editor's: the unlocked tracks, arcs and vias are replaced
by the session's routes, locked ones and every other item stay, the session's placement, when
present, moves the footprints it names (locked ones included, as in the editor), and unresolved
references, layers and padstacks are counted and reported rather than failing the import. A
session without the required sections is rejected and the board is left untouched. The board is
saved in place unless `-o` names another file; the job's session path is relative to the project
in a jobset.

The command is a sibling of `pcb import` rather than a subcommand of it because that command
takes the file to import as its positional argument, and the argument parser only looks for a
subcommand once the positional arguments are consumed.

Tested on Windows against master at 6dd893f7d1: `qa_pcbnew`
(`SpecctraImportReplacesUnlockedCopper`: replacement rule, locked survivors, the session's wires
and via, the placement), `qa_cli` (round trip of the exported wiring through a session gives the
same copper back within the micrometre the DSN keeps, with the same connectivity in DRC; locked
copper and the in-place save; the error paths). Outside the tree, the import equals what KiCad
10.0.6's `pcbnew.ImportSpecctraSES` produces for the same board and Freerouting 2.4.1 session,
on a 2-layer and a 4-layer board, judged by `kicad-cli pcb drc`.

Tool disclosure: the code was written with an LLM assistant (Claude Code) driving the build and the QA runs; the design, the review of every line
and the verification above are mine.
