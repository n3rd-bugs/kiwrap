# Issue draft: ratsnest / airwire export for external placement and routing tools (G1)

NOT filed (2026-09-19). Written for gitlab.com/kicad/code/kicad per docs/dev/upstream-workflow.md
§1.1: end goal first, no mechanism, version block, "may I work on this", one paragraph of
approach. The implementation exists on the fork branch `mr/g1-ratsnest`
(github.com/n3rd-bugs/kicad-fork, base `6dd893f7d1` = nightly 10.99.0-4083) so the
issue can point at working code once a maintainer has answered; the MR text is in `g1-mr.md`.

Live check before filing (2026-09-19, GitLab REST): no open or closed issue or MR mentions
"ratsnest", "airwire" or "GetRatsnest"; closest precedents are !2707 (Net Inspector unrouted
length, merged 2026-08-02) and the frame-free `BOARD::CompileRatsnest()` (9624684909,
eff7182892). Re-run the search on the day of filing.

---

## Title

Export the board ratsnest (unrouted connections with end points) from kicad-cli and the API

## Description

**Goal.** Tools that place or route a board from outside the editor need to know which
connections are still unrouted, where each airwire starts and ends, and how long it is. That is
what a placement optimiser scores (total airline length, crossings) and what an external router
consumes ("route this pad to that track end on F.Cu"). The editor has this data at all times
(the ratsnest it draws), but nothing outside the editor can get it.

**What exists today, and why it is not enough.**

- `kicad-cli pcb drc --format json` lists `unconnected_items`: the right item pairs, but only
  after a full DRC run (seconds on a real board, a second process, `.kicad_prl` side effects) and
  with the items' own positions (pad centre, track start), not the points the airwire is drawn
  between; and the report is capped at 499 entries of one type.
- Over the API, `GetConnectedItems` lets a client rebuild the connectivity clusters of a net
  (one request per cluster), which yields the correct unrouted *count* but leaves the client to
  guess which anchors the airwire joins. Measured on the ecc83 demo with 30 % of its copper
  removed: the guess reproduces 1 of 9 of KiCad's airwire pairs.
- Reimplementing the connectivity outside KiCad drifts from the editor's answer as soon as
  zones, thermal reliefs or the new connectivity engine are involved.

**Proposal.** A `ratsnest` export job, run like the other board exports:

- `kicad-cli pcb export ratsnest [--units mm|in|mils] [--nets N1,N2] [--include-routed]
  [-o FILE] BOARD` writing JSON: per net, the airwire count, the total airline length and each
  airwire with both end items (uuid, description, position, copper layer) and its length, plus
  the totals. An item pair the project excludes as an `unconnected_items` violation is listed
  flagged `excluded` and left out of the counts, so the totals match the editor's "Unrouted"
  count and the DRC report's exclusions. The JSON follows the DRC report conventions
  (`coordinate_units`, uuids, `kicad_version`, `$schema`), documented in
  `resources/schemas/ratsnest.v1.json`.
- In a second step, the same data over the API as `GetRatsnest { DocumentSpecifier board;
  repeated Net nets; }` → `GetRatsnestResponse { repeated RatsnestNet nets; }` (`RatsnestEdge {
  RatsnestNode a, b; Distance length; bool excluded; }`, `RatsnestNode { KIID id; Vector2
  position; BoardLayer layer; }`), `Since: 11.0`, so a live session gets it without saving.

Both read the edges `CONNECTIVITY_DATA::RunOnUnconnectedEdges()` yields — the same set the DRC
connectivity test reports — with the anchor positions the editor draws (`CN_ANCHOR::Pos()`
after `OptimizeRNEdges()`), so the export agrees with DRC pair for pair and with the canvas
point for point; no per-net `GetEdges()` loops (one walk over the nets, as DRC does).

**May I work on this?** The job, the CLI command, the API command and the tests are written
and pass on Windows against master at 6dd893f7d1 (qa_pcbnew, qa_cli, qa_api). I would open two
MRs in sequence: the job + CLI command + schema + tests first (~500 lines of C++ plus tests and
the schema document), then `GetRatsnest` (~120 lines of proto/handler plus an e2e case) once the
first has landed, with the kicad-python wrapper alongside the second.

## Version block

```
Application: KiCad CLI x64 on x64
Version: 10.99.0-4083-g6dd893f7d1, release build
Libraries: wxWidgets 3.3.2, FreeType 2.14.1, HarfBuzz 12.2.0, FontConfig 2.17.1, libcurl/8.16.0
Platform: Windows 11 (build 26200), 64-bit edition, 64 bit, Little endian, wxMSW
Build Info: Date: Sep 17 2026 ..., wxWidgets: 3.3.2 (wchar_t,wx containers), Boost: 1.89.0,
OCC: 7.9.2, Curl: 8.16.0, ngspice: 45, Compiler: Visual C++ 1944 with C++ ABI 1944
Build settings: KICAD_IPC_API=ON
```
(copy the exact block from `kicad-cli version --format about` of the nightly on the day of
filing.)

---

## Replacement text for the G1 card in docs/UPSTREAM.md (applied 2026-09-19; kept for the record)

- **Status 2026-09-19:** implemented on the fork (`mr/g1-ratsnest` = `base/6dd893f7d1` + 2
  commits, 21 files, +1227/-1: job + CLI + schema + qa_pcbnew/qa_cli tests; proto + handler +
  qa_api e2e case), built as `kiwrap-build/6dd893f7d1-4` (integration branch `kiwrap/6dd893f7d1`
  = base + `mr/g6-headless-jobs` + `mr/g1-ratsnest`), installed and registered as
  `C:/kiwrap-builds/6dd893f7d1-4`. Issue not yet filed (draft `docs/upstream/drafts/g1-issue.md`),
  MR not opened (`g1-mr.md`); manifest status `local`.
- **Verified:** export pairs == DRC `unconnected_items` pair for pair (issue8883: 7/7; ecc83 with
  30 % of its copper removed: 9/9); end points are the pad positions / track ends of the board
  file; `GetRatsnest` == the export (uuids, positions, layers); the file estimate agrees per net
  on the mutated ecc83; the cluster walk reproduces the count but 1/9 of the pairs. Timings on
  the mutated ecc83: export 0.17 s, DRC 0.99 s, GetRatsnest 0.06 s (3 requests), cluster walk
  0.59 s (29 requests), estimate 0.05 s.
- **kiwrap side:** `structured.export_ratsnest` + `formats.reports.ratsnest` (parser/model),
  `backends.ipc.queries.get_ratsnest` / `ratsnest(kicad=None)` (GetRatsnest first, cluster walk
  fallback; message classes built from a descriptor while kicad-python predates the proto),
  planner `connectivity` = ipc11 → cli → file, `kw_pcb_check` / `kw_pcb_get` `provider_used`
  `cli:10.99.0+ratsnest-export` / `ipc11:10.99.0+ratsnest-export` with `degraded` empty and
  `uuid` + `layer` per end point. Detection keys `cli.has_ratsnest_export` (help tree) and
  `ipc.has_get_ratsnest` (derived from the same binary, refined by the presence probe on first
  IPC use). Tests `tests/test_ratsnest_patch_g1.py` (`kicad_patch("ratsnest-export")`).
- **Open:** the kicad-python wrapper (`Board.get_ratsnest()`), to be built against the fork
  commit (steps in `g1-mr.md`); the T5 inventory cannot see `GetRatsnest` until then.
