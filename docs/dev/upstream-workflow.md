# Contributing kiwrap's KiCad changes upstream

How to add a command to KiCad's IPC API or kicad-cli end to end, the rules the KiCad team
actually enforces (from their dev-docs and from the 2026 MR discussions), and the ranked list of
contributions kiwrap should make first. Evidence for every claim is in
[research/10-kicad-upstream-mrs.md](../research/10-kicad-upstream-mrs.md); the source build the
recipe needs is in [kicad-source-build.md](kicad-source-build.md) (D17 makes it "planned,
Phase 1"). Patches live in the fork `n3rd-bugs/kicad-source-mirror` (and, for the Python side, a
fork of `kicad-python`) until they are merged; kiwrap always keeps working on stock KiCad and
branches on capability keys, never on "our patch is present".

## 1. The end-to-end recipe for one new IPC command

Model commits to copy from: `ImportNetlist` (MR !2627 + `6fd26c3126`, 4 files, ~120 lines) for a
board command; `GetSchematicNetlist` (`b90628aa0d`) for a schematic query; the jobs family
(`46da153141`) for anything that should run a KiCad job headless. Every step below is what those
commits did.

### 1.1 Before writing code: the issue

1. Search `gitlab.com/kicad/code/kicad` issues and MRs (the `api` label, plus free text). If a
   related issue exists, comment there; do not open a duplicate.
2. Open one issue per command. Describe the user-visible goal and the data you need to get out,
   not the mechanism ("unrouted connections with their endpoints so an external tool can route
   or grade placement", not "expose RN_NET"). Include the full KiCad version block or the bot
   closes it. Say "may I work on this?" and outline the approach in one paragraph.
3. Wait for a team member (in practice Jon Evans for anything under `api/`) to assign it. An MR
   without an assigned issue is auto-closed by the bot.

### 1.2 Proto (`api/proto/...`)

- Board commands go in `api/proto/board/board_commands.proto`, schematic commands in
  `api/proto/schematic/schematic_commands.proto`, editor-generic ones in
  `api/proto/common/commands/editor_commands.proto`, jobs in `board_jobs.proto` /
  `schematic_jobs.proto`. New shared types go in `common/types/base_types.proto`.
- First field of any document command is `kiapi.common.types.DocumentSpecifier` (board/schematic
  or `ItemHeader header` for item-producing commands). Distances are `Distance` (int64 nm),
  angles `Angle`, ids `KIID`; never raw doubles in millimetres.
- Wrap scalars in messages (a bare `bool` response is not extensible); enums start with
  `<PREFIX>_UNKNOWN = 0`; never renumber or rename; deprecate by commenting out.
- Annotate the message (and every added field) with `// Since: 11.0` (or `10.0.x` if you are
  targeting a stable backport). Document the request/response contract in the proto comment
  ("Returns XResponse", blocking or not, headless-only?) - the proto comments are the public docs.
- If the command exposes a new object type, the C++ class also needs `SERIALIZABLE::Serialize/
  Deserialize` and a round-trip case in `qa/tests/api/test_api_proto.cpp` (or
  `qa/tests/eeschema/test_sch_api_proto.cpp`), plus `ToProtoEnum`/`FromProtoEnum` specialisations in
  `common/api/api_enums.cpp` with a case in `qa/tests/api/test_api_enums.cpp`.
- `api/CMakeLists.txt` lists every `.proto`; add new files there.

### 1.3 Handler (`pcbnew/api/api_handler_pcb.cpp` or `eeschema/api/api_handler_sch.cpp`)

- Declare `HANDLER_RESULT<Resp> handleX( const HANDLER_CONTEXT<Req>& aCtx );` in the `.h`; register
  in the constructor with `registerHandler<Req, Resp>( &API_HANDLER_PCB::handleX );`. Keep it in
  the existing handler file - no per-command source files.
- Check order inside the handler: **document validation first** (`validateDocument(
  aCtx.Request.board() )` - returns `AS_UNHANDLED` so another editor's handler can answer),
  **then** `checkForBusy()`, and `checkForHeadless( "X" )` only for GUI-only commands (may go
  first). This order is a review requirement since #24966 (!2808 review comments).
- Work through the context objects (`PCB_CONTEXT`/`SCH_CONTEXT`, `pcbContext()`, `frame()` only
  behind a headless check), never through `EDA_BASE_FRAME` directly: "Any dependency introduced
  will eventually need to be removed before it will be possible to use this API in headless
  mode". Mutations go through `createCommit()` / the commit API so they are undoable in GUI mode
  and safe in headless mode (see Seth's headless commit fixes).
- Errors: `ApiResponseStatus e; e.set_status( ApiStatusCode::AS_BAD_REQUEST ); e.set_error_message(
  fmt::format( "lowercase, unpunctuated message: {}", detail ) ); return tl::unexpected( e );`.
- For a "run a job" command, copy a `RunBoardJobExport*` handler: build the `JOB_*` object from
  the request, run it through the job runner on the context's board, return `RunJobResponse`
  (`kiapi.common.types`), and reuse the existing job settings message. This is the pattern that
  works headless without new frame dependencies and the part of the API Jon calls stable.

### 1.4 QA (`qa/tests/api/`)

- Proto round-trip and enum tests are mandatory for new types (dev-docs).
- For handlers: an integration test against the real object, no mocks. Board handler tests live
  in `qa/tests/api/test_api_handler_pcb.cpp` (introduced with Seth's `RefillZones` fix
  `f61fe5e850`); end-to-end through a real `kicad-cli api-server` in `qa/tests/api/test_api_e2e.cpp`
  (fixture `API_SERVER_E2E_FIXTURE`, one shared server per binary). Extend
  `qa/data/pcbnew/api_kitchen_sink.kicad_pcb` / `qa/data/eeschema/api_kitchen_sink.kicad_sch` when a
  new object type needs non-default values. Use an issue's reproduction project as fixture when
  fixing a bug.
- Run `ctest -R qa_api` (and `qa_pcbnew`/`qa_eeschema` for proto tests) locally and say so in the
  MR; note that the e2e test needs a built `kicad-cli`.

### 1.5 kicad-python (`kipy/`)

- Add the wrapper in the same week as the KiCad MR and link both ("Related kicad-python MR:
  ..." in the KiCad MR description, as !2627 did). Pattern (`Board.import_netlist`): build the
  `*_pb2` request, `command.board.CopyFrom(self._doc)`, `self._kicad.send(command,
  ResponsePb2)`, wrap the response in a small result class; docstring names the KiCad version
  ("requires KiCad 11"). Jobs: fill `command.job_settings.document` / `output_path`, return
  `JobResult`.
- Update the `kicad` submodule pointer to the KiCad commit that has the proto, run
  `poetry run python build.py` / `nox` (ruff + mypy are CI gates since !52), add a docstring, do
  not touch README.md, do not add mocked unit tests (only `tests/test_geometry.py` and
  `tests/test_symbol.py` exist and that is deliberate).
- Anything not yet in a KiCad release is "unstable and undocumented" until 11.0 GA; expect the
  wrapper to be merged only after the KiCad side is on master.

### 1.6 kicad-cli command (for the CLI half of a feature)

- A CLI command is a thin argparse wrapper (`kicad/cli/command_<x>.{cpp,h}`, registered in
  `kicad/kicad_cli.cpp`) over a `JOB_*` class in `common/jobs/` executed by the editor's job
  handler (`pcbnew/pcbnew_jobs_handler.cpp` / `eeschema/eeschema_jobs_handler.cpp`); the same job
  is what the jobset GUI and (via 1.3) the IPC `RunBoardJob*` command run. So one job class gives
  CLI + jobset + IPC; write the job first. Reuse shared argument helpers (`--variant`, units,
  drawing sheet); JSON output uses the `nlohmann::json` `to_json` structs under `include/rc_json_schema.h`
  and a schema under `resources/schemas/` (see !2780 for the smallest example).

### 1.7 The MR itself

- Fork settings: CI/CD visible to everyone, pipeline timeout > 3 h, "Allow commits from members
  who can merge to the target branch" checked (or the maintainer cannot rebase and will merge by
  hand or wait).
- Branch off `master` (or `10.0` for a stable-only fix); one logical change per MR; rebase, never
  merge master in. Title = what changed, description = why + how tested + `Fixes
  https://gitlab.com/kicad/code/kicad/-/issues/<n>` (or `Closes #<n>`).
- Commit message: subject <= 72 chars, body wrapped at 75, `ADDED:`/`CHANGED:`/`REMOVED:` prefix
  for user-visible features (Jon's own `ADDED: Headless API server mode for kicad-cli`), a
  `Fixes https://gitlab.com/kicad/code/kicad/-/issues/NNNN` line per issue, and
  `Assisted-by: <tool>` when an LLM produced a substantial part of the code. No DCO/CLA:
  KiCad has no sign-off or contributor agreement; contributions are GPL-3.0-or-later (KiCad) or
  MIT (kicad-python) by submission.
- Write the MR description, commit message and every code comment yourself. Reviewers reject
  AI-sounding prose outright and deprioritise the author afterwards. Include the tool disclosure
  (tools, prompts summary, how tested) in the MR description when tools were used.
- Expect Jon (API) or Seth (everything else) to review within days for small MRs and to leave
  large ones for months; answer every thread, push fixups, rebase when asked. Merged feature MRs
  get announced by Wayne in the forum's "Post-V10 New Features and Development News" thread.

## 2. Contribution rules digest (with sources)

| Rule | Where it is written / shown |
| --- | --- |
| Issue first, assigned to you, before any MR; bot auto-closes otherwise | dev-docs code-contribution policy; bot comment on !2802 |
| Human in the loop; `Assisted-by:` trailer; no AI-written prose/comments; no AI on `starter` issues | dev-docs tool-generated-content policy; !2779, !2770, kicad-python#109 |
| Subject <= 72, body <= 75, `Fixes <issue URL>`, `ADDED/CHANGED/REMOVED` | dev-docs commit policy |
| Proto: CamelCase messages, `_UNKNOWN=0`, wrap scalars, nm int64, `// Since: X.Y.Z`, never renumber/remove in a bugfix release | dev-docs "IPC API for KiCad developers"; `f3105844` |
| Handler: `HANDLER_RESULT`, `AS_UNHANDLED` to pass, lowercase error text, document -> busy order, no frame deps | dev-docs; !2332, !2808 reviews |
| Tests: proto round-trip + enums required; integration not mocks; fixture = issue project | dev-docs; !2583, !2770 |
| Style: KiCad coding-style policy, wxFormBuilder for dialogs, no `auto` without RHS type, no restating comments | !2477, !2779, !2770 |
| Small reviewable MRs; broad work items get closed | kicad-python#109 |
| Milestone `11.0` for API features; board data-model additions get backported to `10.0` with `Since: 10.0.x`; headless/schematic/jobs/library do not | 10.0 branch `api/proto` log (backports 2026-08-28..09-06); `Since:` histogram on master |
| A `10.0`-only fix is a separate MR against `10.0`, not a cherry-pick, when master diverged | !2545 |
| Non-jobs schematic API unstable until 11.0 GA (~March 2027) | kicad-python#109 (Jon, 2026-08-28) |
| Do not propose file-format (s-expression) APIs or KIWAY/process-level lifecycle APIs | #19870, #25503 |

## 3. Ranked candidate contributions for kiwrap

Effort = calendar days for one engineer who already has the source build working (add the build
cost from [kicad-source-build.md](kicad-source-build.md) once). "Acceptance" is my read of the
maintainers' stated positions. Nothing in this list is being worked on upstream unless noted.

| # | Contribution | Where | Effort | Acceptance | Notes / already in progress |
| --- | --- | --- | --- | --- | --- |
| 1 | `tools/generate_protos.py`: use `protoc.PROTOC_EXE` and `protol ... --protoc-path` so git installs work on Windows | kicad-python | 0.5 d | high (bug, 5 lines, verified fix) | Open an issue linking #82/#140 with the execv analysis; until merged pin kiwrap to the fork. Builds credibility cheaply. |
| 2 | `KiCadServer(..., env=None)` / `KiCad(headless=True, env=...)` so callers can sandbox `KICAD_CONFIG_HOME` | kicad-python | 0.5 d | high (small, obvious) | kiwrap needs it for every headless session; until then use our own launcher subclass. |
| 3 | ERC JSON positions x100 (#25582) - use the schematic IU scale in the ERC JSON writer, no `LOCALE_IO` | kicad, `10.0` + master | 1 d | high if minimal | Issue exists (2026-09-17, unassigned). Ask to take it. Keep `cli.erc_json_pos_scale` keyed per version forever; add a matrix probe so the key flips to 1 on fixed builds. Escain's !2727 was rejected for adding `LOCALE_IO`. |
| 4 | Ratsnest export: `JOB_EXPORT_PCB_RATSNEST` + `kicad-cli pcb export ratsnest --format json` (per edge: net, layer(s), both endpoint positions + item KIIDs, length) via `BOARD::CompileRatsnest()` + `RN_NET::GetEdges()` | kicad (master) | 2-3 d + QA | medium-high (no prior art; !2707 precedent, frame-free ratsnest exists since `9624684909`) | Do not call `GetEdges()` per net in a loop without checking its sort cost (Wayne blocked !2707 for that). Pitch the end goal (external routing/placement grading), reference `--format json` conventions of DRC. |
| 5 | IPC `GetRatsnest` (board_commands.proto + handler + `Board.get_ratsnest()`) reusing #4's job/data | kicad + kicad-python | 1-2 d after #4 | medium-high | Same issue or a follow-up issue; `Since: 11.0`. |
| 6 | `RunBoardJobDrc` / `RunSchematicJobErc` over IPC returning the report path + structured markers (types already in `board_rules.proto` / `schematic_rules.proto` since `eeac1a1c17`) | kicad + kicad-python | 3-5 d | medium (Jon 2025-02: "not currently really possible in KiCad code"; the headless job path has since made it possible, but he has not said he wants it) | Frame it as "the DRC/ERC job over the jobs API, like the exports", not as "RunDRC". Also raise #25434's per-type caps in the CLI/job path in the same issue thread. |
| 7 | Per-net statistics: IPC `GetNetLengths` (routed length, via count, unrouted airline length via `RN_NET::GetTotalAirlineLength()`) | kicad + kicad-python | 1-2 d | medium (kicad-python#96 open, unanswered) | Comment on #96 first; a CLI `pcb export netstats` is a bonus once the job exists. |
| 8 | DRC JSON marker position/layer | kicad | 0 | - | **Already upstream: !2780 (nordic-style) rebased 2026-09-07, awaiting review.** Do not duplicate; make kiwrap's parser accept optional `pos`/`layer` on violations and add a capability probe. |
| 9 | Structured full JSON dump (`pcb/sch export json`) | kicad | 3-5 d | low (Jon: the file format is not an API; the IPC API is the boundary) | Do not pursue. Use `GetItems` over IPC + kiwrap's CST instead. |
| 10 | `ImportNetlist` structured report (typed change list instead of free text) | kicad + kicad-python | 1-2 d | medium (Jon asked for "machine-readable status" in !2627 and got counts) | Nice-to-have once kiwrap consumes the report. |

Suggested order: 1 and 2 immediately (Python only, no KiCad build), 3 as the first KiCad MR
(one-line fix, issue exists), then 4 -> 5 -> 7 -> 6. Keep every MR under ~300 lines and ship the
kicad-python wrapper with it.

## 4. Things to avoid (learned from closed MRs)

- Batches of MRs "from building our MCP server" (Escain: 25 MRs closed unreviewed in a day;
  gwtteam: umbrella work item closed as "way too broad").
- Adding commands that need the GUI (`OpenEditor`, sheet switching via `OpenDocument`) or that
  assume separate pcbnew/eeschema processes.
- Rewriting existing CLI flags, adding `LOCALE_IO`, editing generated `*_base.cpp` dialogs by
  hand, generating Python in CMake, trivial mocked tests, README edits for one feature.
- Submitting proto changes without `Since:` annotations or handlers that check busy before the
  document.
- Pitching the s-expression CST as an API: it is kiwrap's internal bridge for KiCad <= 10, not
  something upstream will bless.
