# Research digests

Nine parallel research tracks, each combining web research with local experiments against the installed KiCad versions (2026-09-18). `[VERIFIED LOCALLY]` marks findings reproduced on this machine; everything else is from sources cited inline.

- [ipc-api: KiCad IPC API (protobuf over nng) and kicad-python/kipy as they exist in KiCad 9.0.x and 10.0.0, with live tests against the installed 10.0.0 and 9.0.5 binaries](01-ipc-api.md) — 16 findings
- [swig-pcbnew: headless KiCad 10 SWIG "import pcbnew" on Windows (with 7/8/9 drift), Freerouting round-trip, DRC, DSN/SES, netlist, DLL pitfalls, deprecation status](02-swig-pcbnew.md) — 15 findings
- [schematic-authoring: programmatic creation/editing of KiCad .kicad_sch (7/8/9/10) without an eeschema API](03-schematic-authoring.md) — 20 findings
- [autorouting](04-autorouting.md) — 20 findings
- [prior-art: AI-agent / programmatic electronics design on KiCad (2024-2026) and VS Code + KiCad integrations; what to reuse vs. build, and which mistakes to avoid (esp. tying to one KiCad version and the in-KiCad-plugin channel)](05-prior-art.md) — 29 findings
- [vscode-agent-surface: exposing one KiCad tool backend to Claude Code, Copilot agent mode, Cursor, Windsurf and Codex from a VS Code extension (MCP, VS Code LM Tools API, extension UI surfaces, Agent Skills)](06-vscode-agent-surface.md) — 25 findings
- [versioning-strategy: what a version-agnostic wrapper over KiCad 7/8/9/10(+11) must handle (file-format drift, kicad-cli argument drift, library/env-var/config resolution, Python API coupling) plus an adapter/IR design recommendation](07-versioning-strategy.md) — 18 findings
- [e2e-agent-workflow: natural-language spec -> schematic -> ERC -> PCB -> placement -> routing -> DRC -> fabrication, as a version-agnostic KiCad wrapper driven by AI agents (tool catalog + skills)](08-e2e-agent-workflow.md) — 17 findings
- [kicad-plugin-and-build: (A) KiCad 9/10 plugin mechanisms that could host a VS Code bridge (SWIG action plugins, IPC API plugins, eeschema hooks, in-process vs external capabilities) and (B) building KiCad from source on Windows vs debugging the installed release with public symbols](09-kicad-plugin-and-build.md) — 22 findings
- [KiCad upstream MRs behind the KiCad 11 API features](10-kicad-upstream-mrs.md) — how handlers land, what is open, what to re-derive

Proposals and judging: [proposals/](proposals/)
