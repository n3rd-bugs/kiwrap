# kiwrap — an AI-agent-facing wrapper over KiCad

**Status: design decisions taken (2026-09-19), Phase 0 starting. No product code yet.** This repository currently holds the
researched, adversarially verified architecture for a tool that lets an AI agent (Claude Code,
Copilot agent mode, Cursor, Windsurf, Codex) do end-to-end electronics development on KiCad —
spec → schematic → ERC → board → placement → routing → DRC → fabrication files — as a wrapper
over *installed* KiCad binaries so KiCad can be upgraded underneath (7 → 8 → 9 → 10 → 11).

## Read in this order

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the decision document: verified capability
   matrix, components, integration strategy per KiCad version, the in-KiCad-plugin and
   source-build decisions, tool catalog, roadmap, risks, **§15 decisions needed**, §16 what
   verification changed, §17 known gaps.
1. [docs/DECISIONS.md](docs/DECISIONS.md) — the 15 decisions taken on 2026-09-19 with alternatives and
   consequences (Windows + Linux from Phase 0, Claude Code first-class, manual evals, gated source build).
2. [docs/VERIFICATION.md](docs/VERIFICATION.md) — the 12 load-bearing claims, each attacked by an
   empirical and a documentary verifier, with corrections.
3. [docs/research/](docs/research/) — nine research digests (IPC API, SWIG, schematic file
   authoring, autorouting, prior art, VS Code/agent surfaces, versioning, end-to-end agent
   workflow, KiCad plugins & source build) and the three competing proposals with judge reports.
4. [spikes/](spikes/) — the experiment scripts that produced the evidence; seed corpus for the
   conformance suite.

## Shape of the design (one paragraph)

One Python MCP server (`kiwrap-core`, 36 `kw_*` tools) is the only agent contract; a thin
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
Python 3.11 + uv; Java 17 on PATH (Freerouting 2.4.1 needs 25 → managed JRE); no Node yet;
VS 2022 17.9; `cdb` from the Windows 10 SDK.
