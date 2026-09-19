# kiwrap — an AI-agent-facing wrapper over KiCad

**Status: Phase 1a implemented — 2026-09-19.** KiCad 11 (nightly) is the primary target
(decision D16): a headless IPC backend on `kicad-cli api-server` gives structured, data-first
feedback (D17) — board/schematic items, nets, connectivity, ratsnest — with file-tier and
kicad-cli fallbacks on KiCad 10.0.x, 9 and 8. 823 tests pass across five installed KiCads. A
source-build bench of the fork exists for the CLI/IPC additions listed in
[docs/UPSTREAM.md](docs/UPSTREAM.md). Schematic/board authoring, netlist sync, placement and
routing tools are next.

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
```

Tools (15): `kw_env`, `kw_project` (create/status), `kw_snapshot`, `kw_job`, `kw_help`,
`kw_sch_get` (symbols with pin nets, wires, labels, hierarchy, netlist), `kw_sch_stats`,
`kw_sch_check` (ERC + netlist), `kw_sch_diff` (KiCad 11), `kw_pcb_get` (footprints, pads, tracks,
vias, zones, nets, connectivity), `kw_pcb_check` (courtyard overlaps, off-board, unrouted /
ratsnest), `kw_pcb_drc`, `kw_fab_export`, and the secondary renders `kw_sch_render` /
`kw_pcb_render`. Every read names its provider (`ipc11`, `file`, `cli`) and any degradation. The
agent-facing protocol is in [AGENTS.md](AGENTS.md) and the skill in
[.claude/skills/kiwrap-flow/SKILL.md](.claude/skills/kiwrap-flow/SKILL.md).

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
conformance/           T1 CST round-trip, T2 file models vs KiCad, T4 kicad-cli help drift, T5 IPC handler snapshot, T6 quirks + corpus
tests/                 unit + live tests (auto-skip without KiCad)
scripts/               bootstrap.ps1 / bootstrap.sh, engines.json (pinned JRE/Freerouting), seed_config_root.py, ci/
```

## Shape of the design (one paragraph)

One Python MCP server (`kiwrap-core`, `kw_*` tools) is the only agent contract; a thin
TypeScript VS Code extension will register it, preview `.kicad_*` files and feed ERC/DRC into the
Problems panel; a thin PCM-packaged KiCad plugin (`KiWrap Connect`) hands a running KiCad's API
token/PID to the server. On KiCad 11 the primary backend is the headless IPC API (`kicad-cli
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
preview) side by side under `C:\Program Files\KiCad`; Python 3.11 + uv; Node 24 LTS; Java 17 on
PATH (Freerouting 2.4.1 needs 25 → managed JRE); VS 2022 17.14 (MSVC 14.44) + vcpkg for the
KiCad source bench; `cdb` from the Windows 10 SDK. Linux code paths exist behind the platform seam
but are out of scope (D18).
