# MR draft: `mr/g6-headless-jobs` (NOT opened; branch pushed to the GitHub fork only, 2026-09-19)

Branch `mr/g6-headless-jobs` on `n3rd-bugs/kicad-fork` (private; the public fork `n3rd-bugs/kicad-fork` carries none of our refs) = `base/6dd893f7d1` + one
commit, `517c0ccfea` "Add an API e2e case for the board statistics export job". It touches
`qa/tests/api/test_api_jobs.cpp` only (+50 lines, one `#include <json_common.h>` and one
`BOOST_FIXTURE_TEST_CASE( ExportBoardStats, API_SERVER_E2E_FIXTURE )`). There is **no handler
change**: the job handlers already run headless (see `g6-issue.md`), so the "smallest possible
change" for G6 is zero lines of KiCad code.

## Recommendation: do not open this as a standalone MR

- KiCad's policy wants an assigned issue behind an MR and the team closed a batch of small
  unreviewed MRs in August; a test-only MR for a handler that already works is the kind of
  drive-by that costs goodwill before G1/G2 are reviewed.
- Fold the case into the G2 MR instead (`RunBoardJobDrc` will copy the `ExportBoardStats`
  shape: open a fixture, run the job into a temp file, parse the JSON) or into the G1 MR's QA if
  a statistics-style export is part of it. Keep the branch so the case is ready.
- If a maintainer asks for more `ApiJobs` coverage, or a rebase of G1/G2 lands on a nightly
  where the stats JSON path breaks, this becomes an MR on its own; the text below is ready.

## Verification (bench, RelWithDebInfo `msvc-win64-release` at 6dd893f7d1)

- `cmake --build ... --target qa_api`: 28 s incremental (one .obj + link).
- `qa_api.exe --run_test=ApiJobs`: before 6/6 passed (81 assertions, 1.6 s); after 7/7 passed
  (94 assertions, 1.6 s); the new case takes 76 ms.
- Full `qa_api.exe`: before 106/108, after 107/109 - the same two known test-side failures
  (`ApiE2E/CreateDocument` separator, `ApiWizards/QueryWizards` Windows mock gap, both in
  `issue-qa-api-windows-findings.md`).
- `clang-format` (`_clang-format`, LLVM 19.1.5) leaves the new case unchanged.
- The release tree was rebuilt at the pristine base afterwards; the checkout is detached at
  `6dd893f7d1` again, the installed bench prefixes were not touched (qa_api is not installed).

## MR description (for GitLab, if it is ever opened)

Title: `Add an API e2e case for the board statistics export job`

The `ApiJobs` suite exercises the plot, drill, netlist and BOM jobs through a headless
`kicad-cli api-server`, but not `RunBoardJobExportStats`. That is the one board job whose output
is meant to be read by the caller rather than sent to a fab house, and it is what an external
tool driving placement over the API uses as its board summary, so the JSON path deserves the same
end-to-end coverage as the others.

The new case opens `qa/data/cli/basic_test/basic_test.kicad_pcb`, runs the job with
`SOF_JSON` / `U_MM` into a temporary file and checks that the job succeeds, reports the requested
output path, and that the JSON describes the opened board (`metadata.board_name`,
`board.has_outline`, `components.total.total == 9`, `pads` and `vias` present). The content of
the report itself is already covered by `qa/tests/pcbnew/test_board_statistics.cpp`.

Tested on Windows (MSVC 14.44, RelWithDebInfo): `qa_api --run_test=ApiJobs` 7/7; full `qa_api`
unchanged apart from the added case.

Tool disclosure: the case was drafted with Claude Code from the neighbouring `ExportBoardDrill`
/ `ExportSchematicNetlist` cases and reviewed and run by hand; the commit carries an
`Assisted-by:` trailer per the tool-generated-content policy.

## Commit (as pushed)

    Add an API e2e case for the board statistics export job

    The ApiJobs suite runs the plot, drill, netlist and BOM jobs through a
    headless kicad-cli api-server but never RunBoardJobExportStats, the one
    board job whose output is meant to be parsed by the caller rather than
    sent to a fab. External tools that drive placement over the API use the
    JSON statistics as their board summary, so the JSON path deserves the
    same end-to-end coverage: the job has to run headless, honour the
    requested output path and format, and describe the board that was
    opened (basic_test: outline present, nine footprints).

    Assisted-by: Claude Code

Diffstat: `qa/tests/api/test_api_jobs.cpp | 50 ++++++++++++`, 1 file changed, 50 insertions(+).

## Integration branch

`kiwrap/6dd893f7d1` was left at `base/6dd893f7d1`: the commit changes no shipped binary, so
merging it would not change the installed prefixes, their `kiwrap-build/*` tags or the T5
snapshots. Merge it when the integration branch is next rebuilt for a real patch (G1).
