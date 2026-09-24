# MR draft: `mr/g1-ratsnest` (NOT opened; branch pushed to the GitHub fork only, 2026-09-19)

Branch `mr/g1-ratsnest` on `n3rd-bugs/kicad-fork` (private; the public fork `n3rd-bugs/kicad-source-mirror` carries none of our refs) = `base/6dd893f7d1` (nightly
10.99.0-4083) + two commits (reworked after the internal review of 2026-09-19: `excluded` flag,
shared-layer rule for through items, `--format` and the dead dialog title dropped):

| Commit | Subject | Files |
|---|---|---|
| `8f5b175459` | ADDED: Export the board ratsnest as JSON (pcb export ratsnest) | 18 files: `common/jobs/job_export_pcb_ratsnest.{h,cpp}`, `pcbnew/ratsnest/ratsnest_report.{h,cpp}`, `pcbnew/pcbnew_jobs_handler.{h,cpp}` (+78), `kicad/cli/command_pcb_export_ratsnest.{h,cpp}`, `kicad/kicad_cli.cpp` (+3), `include/rc_json_schema.h` (+70), `resources/schemas/ratsnest.v1.json`, 3 CMakeLists, `qa/tests/pcbnew/test_ratsnest_report.cpp` (5 cases), `qa/data/pcbnew/ratsnest_layers.kicad_pcb` (hand-made fixture: THT pad -> B.Cu track, THT -> THT, SMD -> B.Cu track), `qa/tests/cli/test_pcb.py` (+108, 2 cases) |
| `f474f77212` | ADDED: GetRatsnest board API command | 4 files: `api/proto/board/board_commands.proto` (+62), `pcbnew/api/api_handler_pcb.{h,cpp}` (+66), `qa/tests/api/test_api_e2e.cpp` (+100) |

The commits split cleanly: the API commit only reuses `CollectRatsnestAirwires()` from the first.

## Recommendation

Open the issue first (`g1-issue.md`), wait for assignment, then open **two MRs in sequence**
(docs/UPSTREAM.md §3.4: ~300 lines per MR, one logical change each; !2780's four files have
waited a month, a 22-file MR spanning jobs, CLI, schema, proto and handler needs both Seth and
Jon and is the shape that stalls): first `8f5b175459` (job + CLI + schema + qa_pcbnew/qa_cli),
then `f474f77212` (proto + handler + qa_api) once the first has landed, with the kicad-python
wrapper linked from the second. Do not batch either with G2/G6. Rebase on `upstream/master` on
the day; the branch is on the 09-17 nightly commit and touches files that move often
(`pcbnew_jobs_handler.cpp`, `api_handler_pcb.cpp`, `board_commands.proto`).

Deliberately not in the branch: `RunBoardJobExportRatsnest` (the job over the jobs API).
`GetRatsnest` returns the data without a file round trip, the CLI/jobset half runs the job, and
the job-over-IPC variant costs a proto message, `ToProtoEnum`/`FromProtoEnum` specialisations for
the job's units enum, an enum test and an `ApiJobs` case for no new data; propose it as a
follow-up once `GetRatsnest` has landed (kipy `Board.export_ratsnest()` would come with it).

## Verification (Windows, RelWithDebInfo `msvc-win64-release`, VS 2022 17.14 / MSVC 14.44)

- Incremental builds: 55 s (job + CLI), 2 min 27 s (proto regeneration + handler + QA binaries).
- `qa_pcbnew.exe --run_test=RatsnestReport`: 5/5 cases, 274 assertions, 74 ms
  (`AirwiresMatchConnectivity`: edge count == `GetUnconnectedCount(false)` == 7 on
  `issue8883.kicad_pcb`, every end item resolves by uuid on the same net, copper layers,
  lengths; `ReportTotalsAndUnits`; `ExcludedAirwires`: issue17429 lists 35 edges, 7 flagged
  excluded, `unrouted_count` 28, the JSON carries `excluded` only when true;
  `ThroughItemLayers` on the hand-made `ratsnest_layers` fixture: THT pad -> B.Cu track reads
  B.Cu at both ends, THT -> THT F.Cu, SMD(F.Cu) -> B.Cu track F.Cu / B.Cu;
  `NetFilterAndRoutedNets`).
- `qa_cli` `test_pcb_export_ratsnest` (pairs == `pcb drc --format json` `unconnected_items` on
  issue8883, 7 pairs, `--nets`, `--include-routed`, no `excluded` key) and
  `test_pcb_export_ratsnest_exclusions` (issue17429: the flagged pairs == DRC's excluded ones,
  counts and per-net lengths skip them); 2.1 s.
- `qa_api.exe` full run: 108/110 on the integration branch (the G6 test commit adds one case);
  the two failures are the known test-side ones (`ApiE2E/CreateDocument` path separator,
  `ApiWizards/QueryWizards` Windows mock gap, `issue-qa-api-windows-findings.md`). Case
  `ApiE2E/GetRatsnest`: 7 edges over 2 nets on issue8883, lengths match positions, net filter,
  then (after an explicit `CloseDocument`) issue17429: 35 listed, 7 `excluded`, airline lengths
  skip them.
- `clang-format` (LLVM 19.1.5, `_clang-format`) `--dry-run` clean on every new file and on the
  changed ranges of the existing ones (the `NLOHMANN_JSON_SERIALIZE_ENUM` block is laid out like
  `job_export_pcb_stats.cpp`'s).
- End to end from kiwrap on the installed build (`C:/kiwrap-builds/6dd893f7d1-4`,
  `tests/test_ratsnest_patch_g1.py`): ecc83 demo 0 airwires; ecc83 with 30 % of its copper
  removed (seeded) 9 airwires over 6 nets, pairs equal to DRC's, end points equal to the pad
  positions / track ends of the board file, `GetRatsnest` equal to the export; an exclusion
  written into the `.kicad_pro` flags exactly that pair in the export, in `GetRatsnest` and in
  DRC; THT pad ends read the track's layer; export 0.17 s vs DRC 0.99 s.
- T5 handler snapshot of the build equals the stock nightly's (the inventory comes from
  kicad-python's protos, which do not know `GetRatsnest` yet; nothing else changed).

## MR description (for GitLab)

Title: `Export the board ratsnest as JSON and over the API (pcb export ratsnest, GetRatsnest)`

Tools that place or route a board from outside KiCad need the unrouted connections with their
end points: which items an airwire joins, where on them it is drawn from, and how long it is.
Today the only way to get that out of KiCad is a full DRC run, whose `unconnected_items` carries
the item pairs but not the airwire end points, or to rebuild the connectivity outside KiCad and
guess.

This adds `JOB_EXPORT_PCB_RATSNEST`, run by the pcbnew jobs handler, and
`kicad-cli pcb export ratsnest [--units U] [--nets N1,N2] [--include-routed] [-o FILE] BOARD`.
The report lists, per net, the airwire count, the total airline length and every airwire with
both end items (uuid, description, position, copper layer) and its length, plus the totals over
the reported nets. The edges are the ones `CONNECTIVITY_DATA::RunOnUnconnectedEdges()` yields,
i.e. exactly the `DRCE_UNCONNECTED_ITEMS` set DRC reports; the end points are the ratsnest
anchors after `OptimizeRNEdges()`, which is what the editor draws. An item pair the project
excludes as an unconnected_items violation is listed flagged `excluded` and left out of the
counts and lengths, so the totals match the unrouted count the editor shows. The layer of an end
on a through hole pad or via is the layer it shares with the other end, so a router reading the
report does not add a via the pad makes unnecessary. The JSON follows the DRC report
conventions (`rc_json_schema.h` structs, `coordinate_units`, uuids) and is documented in
`resources/schemas/ratsnest.v1.json`. The job is registered for jobsets without a settings dialog;
its parameters (units, nets, include_routed) can be edited in the jobset file.

(Second MR.) `GetRatsnest` in `board_commands.proto` (`Since: 11.0`): given the board and an
optional net filter it returns, per net that still has airwires, the ratsnest edges with both end
items (KIID, anchor position, copper layer), the edge length and whether the project excludes
the pair, plus the net's total airline length over the edges that are not excluded. The handler
walks the same collector, so a client sees the same edges DRC reports; the connectivity is
current after every commit, so nothing is recomputed in the handler. Document check before busy
check.

Tested on Windows against master at 6dd893f7d1: `qa_pcbnew` (new suite `RatsnestReport`,
5 cases incl. a project with excluded pairs and a hand-made layers fixture), `qa_cli`
(`test_pcb_export_ratsnest`: pairs equal to `pcb drc` unconnected_items on issue8883;
`test_pcb_export_ratsnest_exclusions` on issue17429), `qa_api` (`ApiE2E/GetRatsnest` through a
real `kicad-cli api-server`). On the ecc83 demo with 30 % of its copper removed the export agrees
with DRC pair for pair and every end point is a pad position or a track end of the board.

Related kicad-python MR: (to be opened; `Board.get_ratsnest(nets=None)`, see below).

Tool disclosure: the code was written with an LLM assistant (Claude Code) driving the build and the QA runs; the design, the review of every line
and the verification above are mine.

## kicad-python: `Board.get_ratsnest()` and how to build it against this branch

kiwrap's client (`kiwrap.backends.ipc.queries`) already talks to the handler: while the pinned
kicad-python (f79cebd) predates the proto, it builds the `GetRatsnest` / `GetRatsnestResponse`
classes from a `FileDescriptorProto` that mirrors the branch (`ratsnest_messages()`), and it
prefers the generated classes as soon as `kipy.proto.board.board_commands_pb2` has them. The
wrapper for kicad-python itself:

```python
# kipy/board.py, class Board
def get_ratsnest(self, nets: Sequence[str] | None = None) -> Sequence[RatsnestNet]:
    """Retrieves the unrouted connections (airwires) of the board, per net.

    .. versionadded:: 0.9.0 (KiCad 11.0.0)
    """
    command = board_commands_pb2.GetRatsnest()
    command.board.CopyFrom(self._doc)
    for name in nets or ():
        command.nets.add().name = name
    return self._kicad.send(command, board_commands_pb2.GetRatsnestResponse).nets
```

Building kicad-python against the fork commit (not done in this pass; needs network to
gitlab.com and ~10 min):

1. `git clone https://gitlab.com/kicad/code/kicad-python.git` (or the `n3rd-bugs/kicad-python`
   fork), branch `get-ratsnest`.
2. Point the `kicad` submodule at the fork commit: `cd kicad && git remote add fork
   https://github.com/n3rd-bugs/kicad-fork.git && git fetch fork mr/g1-ratsnest && git
   checkout f474f77212` (only `api/proto/**` and `cmake/KiCadVersion.cmake` are read; a sparse
   checkout as in `scripts/ci/build_kicad_python_wheel.py` `sparse_submodule()` works, the
   script only needs `fork` as the submodule remote and the sha above).
3. `uv build --wheel` (protoc-wheel-0 + protoletariat + mypy-protobuf), then the wheel repair
   from `scripts/ci/build_kicad_python_wheel.py` (bare `common/board/schematic` imports, the
   `kicad_api_version.py` stamp, the `py3-none-any` retag).
4. Install the wheel into kiwrap's venv (`uv pip install --force-reinstall <wheel>`);
   `python -c "from kipy.proto.board import board_commands_pb2 as c; print(c.GetRatsnest)"` must
   succeed and `tests/test_ratsnest_patch_g1.py::test_message_classes_are_built_when_kipy_lacks_the_proto`
   then exercises the generated classes instead of the descriptor fallback.
5. Re-run `uv run pytest conformance/tests/t5_ipc_handlers.py` with the bench registered: the
   inventory grows by one command and the `+6dd893f7d1` snapshot gains the row
   `GetRatsnest pcb -> AS_OK` (`compare()` tolerates it: the manifest lists the command).
