# kiwrap — an AI-agent-facing wrapper over KiCad

**Status: Phase 0 (foundations) implemented — 2026-09-19.** An MCP server with ten `kw_*` tools runs
ERC, DRC, renders and fabrication exports against *installed* KiCad 7/8/9/10 from a sandboxed
configuration, with a lossless s-expression engine underneath. Schematic/board authoring, netlist
sync, placement and routing are Phase 1+ (see the roadmap in the architecture document).

kiwrap lets an AI agent (Claude Code first; Copilot agent mode, Cursor, Windsurf, Codex best-effort)
do end-to-end electronics development on KiCad — spec → schematic → ERC → board → placement →
routing → DRC → fabrication files — as a wrapper over installed KiCad binaries so KiCad can be
upgraded underneath (7 → 8 → 9 → 10 → 11).

## Quick start (Windows or Linux)

```powershell
git clone git@github.com:n3rd-bugs/kiwrap.git; cd kiwrap
.\scripts\bootstrap.ps1          # uv, deps, private JRE 25 + Freerouting into %LOCALAPPDATA%\kiwrap (Linux: scripts/bootstrap.sh)
uv run kiwrap env                # discovered KiCad installs + capability records
uv run pytest -q                 # ~5 min with four KiCads installed; ~1 min without KiCad
uv run kiwrap serve              # MCP server over stdio (Claude Code picks it up from .mcp.json)
```

Phase 0 tools: `kw_env`, `kw_project` (create/status), `kw_snapshot`, `kw_job`, `kw_help`,
`kw_sch_check` (ERC + netlist), `kw_sch_render`, `kw_pcb_drc`, `kw_pcb_render` (3D/2D PNG),
`kw_fab_export` (gerbers/drill/pos/pdf/step/bom). The agent-facing protocol is in
[AGENTS.md](AGENTS.md) and the skill in [.claude/skills/kiwrap-flow/SKILL.md](.claude/skills/kiwrap-flow/SKILL.md).

## Read in this order

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the decision document: verified capability
   matrix, components, integration strategy per KiCad version, the in-KiCad-plugin and
   source-build decisions, tool catalog, roadmap, risks, §15 decisions, §16 what verification
   changed, §17 known gaps.
2. [docs/DECISIONS.md](docs/DECISIONS.md) — the 15 decisions taken on 2026-09-19 with alternatives and
   consequences (Windows + Linux from Phase 0, Claude Code first-class, manual evals, gated source build).
3. [docs/VERIFICATION.md](docs/VERIFICATION.md) — the 12 load-bearing claims, each attacked by an
   empirical and a documentary verifier, with corrections.
4. [CLAUDE.md](CLAUDE.md) — working conventions for anyone (human or agent) changing the code.
5. [docs/research/](docs/research/) — nine research digests and the three competing proposals with
   judge reports; [spikes/](spikes/) — the experiment scripts that produced the evidence.
6. [docs/dev/](docs/dev/) — KiCad source-build recipe (gated), crash symbolication without a build;
   [docs/LINUX.md](docs/LINUX.md) — what the Linux CI job proves.

## Layout

```
src/kiwrap/            single MIT distribution (src layout)
  platform/            Windows/Linux seam — the only place that inspects sys.platform
  installation.py      KiCadInstallation + discover()/find()   (registry, Program Files, PATH; Linux: apt/Flatpak/Snap/AppImage)
  capability.py        capability record per FULL semver, seeded from data/kicad_version_matrix.json + --help probes
  sandbox.py           sandboxed KICAD_CONFIG_HOME roots; the user's own KiCad config is read-only (tests assert its hash)
  formats/             zero-dependency: lossless s-expression CST, format epochs, ERC/DRC/netlist parsers
  backends/cli/        kicad-cli adapters with the verified quirks (ERC JSON ×100, exit codes, .kicad_prl, out dirs, …)
  core/                MCP server, kw_* tools, receipts/teaching errors, stage machine + gates, locks, snapshots, jobs
conformance/           T1 (CST round-trip on 1,400+ KiCad demo files), T4 (kicad-cli help drift), T6 (quirks) + corpus
tests/                 unit + live tests (auto-skip without KiCad)
scripts/               bootstrap.ps1 / bootstrap.sh, engines.json (pinned JRE/Freerouting), seed_config_root.py, ci/
```

## Shape of the design (one paragraph)

One Python MCP server (`kiwrap-core`, 36 `kw_*` tools when complete) is the only agent contract; a thin
TypeScript VS Code extension registers it, previews `.kicad_*` files and feeds ERC/DRC into the
Problems panel; a thin PCM-packaged KiCad plugin (`KiWrap Connect`) hands a running KiCad's API
token/PID to the server. Schematics are written on disk by an in-house lossless s-expression
engine with one serializer per KiCad format epoch (there is no schematic API before KiCad 11, and
eeschema 10.0.x crashes on IPC writes). Boards are built by a file emitter plus a SWIG driver run
under each install's own bundled `python.exe` (the only headless path for footprint loading,
DSN/SES and zone fill on 7–10), with a SWIG-free twin certified before KiCad 11 removes SWIG.
ERC/DRC/exports/renders come exclusively from `kicad-cli` — KiCad is the judge. Routing is
Freerouting 2.4.1 over DSN/SES (managed JRE 25) with KiCadRoutingTools as the no-Java engine and
placement seeder. The IPC API is an optional live-session backend on 9/10 (PCB only) and becomes
the primary headless backend on 11. Every mutating tool returns a receipt with KiCad-computed
validator deltas; stage gates (ERC → sync → place → route → DRC → fab) are server-enforced.

## Local environment this was verified on

Windows 11; KiCad 7.0.6, 8.0.1, 9.0.5, 10.0.0 side by side under `C:\Program Files\KiCad`;
Python 3.11 + uv; Node 24 LTS; Java 17 on PATH (Freerouting 2.4.1 needs 25 → managed JRE);
VS 2022 17.9; `cdb` from the Windows 10 SDK. Linux is designed and CI-tested (see docs/LINUX.md),
not yet verified on a developer machine.
