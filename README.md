# kiwrap — an AI-agent-facing wrapper over KiCad

**Status: Phase 1c implemented — 2026-09-20.** KiCad 11 (nightly) is the primary target
(decision D16): a headless IPC backend on `kicad-cli api-server` gives structured, data-first
feedback (D17) — board/schematic items, nets, connectivity, ratsnest — and authors schematics
and boards (symbols, wires, labels, power, hierarchical sheets, annotation, footprint
assignment, board setup, netlist sync, placement, zones, routing primitives) with file-tier
and kicad-cli fallbacks on KiCad 10.0.x, 9 and 8. Phase 1c adds **autorouting**
(`kw_pcb_route_auto` / `kw_pcb_fix`: Freerouting 2.4.1 under a managed JRE 25 over a Specctra
DSN/SES round trip — kiwrap's own codec certified against KiCad's exporter/importer, the
patched `kicad-cli pcb export specctra` / `pcb import-ses` on the fork build, mandatory refill +
kicad-cli DRC as the judge), the **JLCPCB fabrication profile** (`kw_fab_export(profile=
"jlcpcb")`, `kw_fab_bom`, `kw_pcb_setup(preset=…)`), **intent** (`kw_intent`, diffed by
`kw_sch_check`) and the **D5 reference board** eval. An agent conversation over MCP stdio
builds a two-sheet LDO + 555 blinker from nothing to gerbers — hand-routed and autorouted — with
ERC 0 / parity 0 / DRC 0 on the 10.99 nightly (ipc11) and 10.0.6 (file)
(tests/test_e2e_authoring.py), and the ESP32-S3 reference board (29 parts, 3 sheets, 2 layers)
from `kw_intent` to a JLCPCB bundle with the grader green — ERC 0, intent diff empty, unrouted
0, must_fix 0 — on both (tests/test_e2e_reference_board.py; numbers below). A source-build
bench of the fork carries the CLI/IPC additions listed in [docs/UPSTREAM.md](docs/UPSTREAM.md).

**Phase 2 (integrated and verified end to end, 2026-09-20): the VS Code extension.** `extension/`
(TypeScript, MIT, a private `.vsix` built by `scripts/build_extension.ps1` into `dist/`) is an MCP
client of its own `kiwrap serve`; it shows **Live KiCad** — the real, sandboxed KiCad editors
streamed into `.kicad_pcb` / `.kicad_sch` tabs by the Rust helper `helper/kiwrap-live.exe`
(Windows.Graphics.Capture, loopback + token) — in three modes: capture-only view on any KiCad,
**hidden** (KiCad cloaked, invisible on the desktop, the tab is the UI: posted input, dialogs
composited) on a build carrying the fork's `mr/display-input` patch (the registered bench build
`6dd893f7d1-7`), and an opt-in mirror on stock; plus ERC/DRC in the Problems panel, a KiCad picker
and a one-command Claude Code setup. The server side is the `kw_live` tool (a sandboxed GUI session
whose API socket the `ipc11` provider binds to) and the two-server model: the agent's own `kiwrap
serve` finds the extension's session through its record and writes into the same window — measured
in `extension/test/e2e` (10/10): an agent's `kw_pcb_place` over a second MCP client repainted the
live tab 431-474 ms after the call started (`kw_pcb_place` 870-979 ms via ipc11), hidden-mode input
91-138 ms per event, 15/15 footprints rotated by posted keys and read back over IPC. Details and the
numbers: [docs/EXTENSION.md](docs/EXTENSION.md) (§9.1), user guide
[extension/README.md](extension/README.md), the measured display investigation
[docs/DISPLAY.md](docs/DISPLAY.md), screenshots `docs/research/display/img/phase2-*.png`.

kiwrap lets an AI agent (Claude Code inside VS Code; Windows only — decision D18)
do end-to-end electronics development on KiCad — spec → schematic → ERC → board → placement →
routing → DRC → fabrication files — as a wrapper over installed KiCad binaries so KiCad can be
upgraded underneath (7 → 8 → 9 → 10 → 11).

## Quick start (Windows)

```powershell
git clone git@github.com:n3rd-bugs/kiwrap.git; cd kiwrap
.\scripts\bootstrap.ps1          # uv, deps, private JRE 25 + Freerouting into %LOCALAPPDATA%\kiwrap (Linux: scripts/bootstrap.sh)
uv run kiwrap env                # discovered KiCad installs + capability records
uv run pytest -q                 # ~5 min with four KiCads installed; ~1 min without KiCad
uv run kiwrap serve              # MCP server over stdio (Claude Code picks it up from .mcp.json)
.\scripts\build_extension.ps1     # VS Code extension + helper -> dist\kiwrap-<ver>.vsix (needs Node 24 + Rust stable-msvc)
code --install-extension .\dist\kiwrap-0.1.0.vsix   # then "kiwrap: Select KiCad", "kiwrap: Configure Claude Code"
```

Tools (33): `kw_env`, `kw_project` (create/status), `kw_snapshot`, `kw_job`, `kw_help`,
`kw_live` (launch / attach / status / screenshot / detach / raise a sandboxed GUI KiCad the
extension displays and the `ipc11` provider writes into),
`kw_intent` (declare nets / rails / interfaces; diffed against KiCad's netlist by `kw_sch_check`),
`kw_lib_search` / `kw_lib_info` (FTS index over the sym-/fp-lib-tables), the schematic authoring
tools `kw_sch_add_symbol` (absolute pin endpoints back), `kw_sch_connect`, `kw_sch_add_item`
(power, PWR_FLAG, labels, sheets), `kw_sch_edit`, `kw_sch_annotate`, `kw_sch_assign_footprints`,
the reads `kw_sch_get` (symbols with pin nets, wires, labels, hierarchy, netlist), `kw_sch_stats`,
`kw_sch_check` (ERC + netlist), `kw_sch_diff` (KiCad 11), the board authoring tools
`kw_pcb_setup`, `kw_pcb_sync` (KiCad's netlist, parity-checked), `kw_pcb_place`, `kw_pcb_zones`,
`kw_pcb_route` (track / via primitives with per-net DRC deltas), `kw_pcb_route_auto`
(Freerouting over DSN/SES, a background job with pass / unrouted progress), `kw_pcb_fix` (rip up
the nets of must_fix violations and re-route them until DRC is clean), the reads `kw_pcb_get`
(footprints, pads, tracks, vias, zones, nets, connectivity), `kw_pcb_check` (courtyard overlaps,
off-board, unrouted / ratsnest), `kw_pcb_drc`, `kw_fab_bom`, `kw_fab_export` (generic or the
JLCPCB profile: Protel gerbers, PTH/NPTH drills, corrected CPL, BOM, zip), and the secondary
renders `kw_sch_render` / `kw_pcb_render`. Every tool names its provider (`ipc11`, `file`,
`cli`, `codec`, `cli:<v>+<patch>`) and any degradation. The agent-facing protocol is in
[AGENTS.md](AGENTS.md) and the skill in
[.claude/skills/kiwrap-flow/SKILL.md](.claude/skills/kiwrap-flow/SKILL.md).

## Phase 1c end-to-end numbers (2026-09-20)

Recorded by `tests/test_e2e_reference_board.py` and `tests/test_e2e_authoring.py` (each run
writes `<project>/.kiwrap/e2e-out.json` and `e2e-log.json` with `provider_used` per call):

| flow | KiCad | writes | DSN/SES | router | result |
|---|---|---|---|---|---|
| ESP32-S3 reference board, 29 parts / 52 nets | 10.99.0-4083 | ipc11 (+ file for sheets, no-connects, footprints, setup) | codec | Freerouting 2.4.1, run-dependent: 4 passes, 21.7 s, 221 tracks + 34 vias, 70 -> 0 unrouted (full-suite run); or 18 passes, 12.3 s, 231 + 38, 70 -> 1 (score plateau) and `kw_pcb_fix` 2 rounds (the net alone stalls, the 2 mm neighbourhood closes it) -> 0 | ERC 0, intent diff 0 (strict), parity 0, DRC 0 must_fix / 0 unconnected after refill, grade passed, 18 JLCPCB files, 202-206 tool calls, 4.5-5.4 min |
| ESP32-S3 reference board | 10.0.6 | file | codec | 4 passes, 39.8 s, 217 tracks + 28 vias, 70 -> 0 (full-suite run); or 18 passes, 12.3 s, 204 + 38, 70 -> 1 and `kw_pcb_fix` 1 round -> 0 | ERC 0, intent diff 0, parity 0, DRC 0/0, grade passed, 18 files, 202 tool calls, 2.1 min |
| blinker, 11 parts / 8 nets, autorouted | 10.99.0-4083 | ipc11 | codec | 2 passes, 3.2 s, 37-38 tracks + 5-6 vias, 21 -> 0 unrouted, no fix needed | DRC 0 must_fix / 0 unconnected (1 review: the nightly's own `lib_footprint_mismatch` on import), gerbers + drill + pos, 2.8 min |
| blinker, autorouted | 10.0.6 | file | codec | 2 passes, 3.2 s, 36-37 tracks + 5-6 vias, 21 -> 0 unrouted, no fix needed | DRC 0/0, gerbers + drill + pos, 56 s |
| ecc83 demo stripped to 0 copper (2 and 4 layers), `tests/test_route_auto_providers_live.py` | bench 10.99.0+dfd50b8639 | file | `cli:10.99.0+specctra-jobs` (patched kicad-cli); the codec's DSN == the CLI's | 0 unrouted | DRC 0/0 on both, `kw_pcb_fix` closes two stripped nets through the same chain |

Unrouted is always KiCad's `unconnected_items` count after `pcb drc --refill-zones --save-board`,
never Freerouting's own score. Pour order matters for the router: the B.Cu GND plane before
routing, the F.Cu pour after (docs/ARCHITECTURE.md §8).

## Read in this order

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the decision document: verified capability
   matrix, components, integration strategy per KiCad version, the in-KiCad-plugin and
   source-build decisions, tool catalog, roadmap, risks, §15 decisions, §16 what verification
   changed, §17 known gaps.
2. [docs/DECISIONS.md](docs/DECISIONS.md) — the 15 decisions taken on 2026-09-19 with alternatives and
   consequences, plus D16 (KiCad 11 first) and D17 (data-first feedback, upstream CLI/IPC expansion, source build).
3. [docs/VERIFICATION.md](docs/VERIFICATION.md) — the 12 load-bearing claims, each attacked by an
   empirical and a documentary verifier, with corrections.
4. [CLAUDE.md](CLAUDE.md) — working conventions for anyone (human or agent) changing the code.
5. [docs/research/](docs/research/) — nine research digests and the three competing proposals with
   judge reports; [spikes/](spikes/) — the experiment scripts that produced the evidence.
6. [docs/dev/](docs/dev/) — the KiCad source-build bench (executed), crash symbolication, upstream workflow;
   [docs/LINUX.md](docs/LINUX.md) — what the Linux CI job proves; [docs/UPSTREAM.md](docs/UPSTREAM.md) — gap cards
   and the fork/patch workflow for the KiCad CLI/IPC additions.
7. [docs/EXTENSION.md](docs/EXTENSION.md) — the VS Code extension: processes, channels, security, the
   two-server model, live modes, keybinding policy; [docs/DISPLAY.md](docs/DISPLAY.md) — the measured
   display investigation behind it; [extension/README.md](extension/README.md) — the user guide.

## Layout

```
src/kiwrap/            single MIT distribution (src layout)
  platform/            Windows/Linux seam — the only place that inspects sys.platform
  installation.py      KiCadInstallation + discover()/find()   (registry, Program Files, PATH; Linux: apt/Flatpak/Snap/AppImage)
  capability.py        capability record per FULL semver, seeded from data/kicad_version_matrix.json + --help probes
  sandbox.py           sandboxed KICAD_CONFIG_HOME roots; the user's own KiCad config is read-only (tests assert its hash)
  formats/             zero-dependency: lossless CST, board/schematic models, geometry + analyses (overlaps, connectivity, ratsnest), parsers
  backends/cli/        kicad-cli adapters with the verified quirks (ERC JSON ×100, exit codes, .kicad_prl, out dirs, …) + structured exports
  backends/ipc/        KiCad 11 headless IPC sessions (kicad-python), mm-unit readers, connectivity queries, writes, T5 probe
  core/                MCP server, kw_* tools, receipts/teaching errors, stage machine + gates, locks, snapshots, jobs
conformance/           T1 CST round-trip, T2 file models vs KiCad, T4 kicad-cli help drift, T5 IPC handler snapshot, T6 quirks, T7 IPC writes + Specctra codec vs KiCad's DSN/SES, T8 writers vs KiCad + corpus
tests/                 unit + live tests (auto-skip without KiCad)
extension/             the VS Code extension (TypeScript, MIT): MCP client of `kiwrap serve`, Live KiCad tabs, Problems panel, Configure Claude Code
helper/                Rust workspace (MIT): kiwrap-live.exe, the Windows.Graphics.Capture + input helper the extension bundles
scripts/               bootstrap.ps1 / bootstrap.sh, build_extension.ps1 (-> dist/*.vsix), engines.json (pinned JRE/Freerouting), seed_config_root.py, ci/, dev/
```

## Shape of the design (one paragraph)

One Python MCP server (`kiwrap-core`, `kw_*` tools) is the only agent contract; a thin
TypeScript VS Code extension is a client of it (its own instance; the agent runs another, both
sharing the storage home and the per-project lock), shows the sandboxed KiCad GUI that `kw_live`
launched inside VS Code tabs through a Rust capture helper, and feeds ERC/DRC into the Problems
panel; the `KiWrap Connect` KiCad plugin of the Phase-0 plan is not needed while `kw_live` owns
the GUI it displays. On KiCad 11 the primary backend is the headless IPC API (`kicad-cli
api-server`, driven through kicad-python): structured reads of every board and schematic item,
connectivity, netlist and hierarchy, and writes as undoable commits. Underneath sits an in-house
lossless s-expression engine that models boards and schematics on every version 7–11 (pin
endpoints, courtyards, zone fills, connectivity, a ratsnest estimate verified against KiCad's own
DRC) and is the only schematic path on ≤ 10. ERC/DRC/exports come exclusively from `kicad-cli` —
KiCad is the judge. Routing will be Freerouting 2.4.1 over DSN/SES (managed JRE 25). Every tool
answers in millimetres with its provider (`ipc11`, `file`, `cli`) and any degradation named; stage
gates (ERC → sync → place → route → DRC → fab) are server-enforced. Where KiCad exposes no
structured surface (ratsnest export, DRC/ERC over the API, headless jobs) the fork at
`n3rd-bugs/kicad-source-mirror` carries the additions until they are merged upstream.

## Local environment this was verified on

Windows 11; KiCad 7.0.6, 8.0.9, 9.0.9, 10.0.6 and the 10.99 nightly (10.99.0-4083, KiCad 11
preview) side by side under `C:\Program Files\KiCad`, plus the registered source builds under
`C:\kiwrap-builds`; Python 3.11 + uv; Node 24 LTS and Rust 1.95 stable-msvc (the extension build);
VS Code 1.138; Java 17 on PATH (Freerouting 2.4.1 needs 25 → managed JRE); VS 2022 17.14 (MSVC
14.44) + vcpkg for the KiCad source bench; `cdb` from the Windows 10 SDK. Linux code paths exist behind the platform seam
but are out of scope (D18).
