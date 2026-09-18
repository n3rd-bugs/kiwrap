# Spikes — experiment scripts from the research phase

These are the scripts the research and verification agents actually ran on 2026-09-18 against the
installed KiCad 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0 (plus Freerouting 2.4.1, kicad-python 0.8.0, mcp 2.2).
They are preserved verbatim (paths inside them still point at the original scratchpad; large
binaries, venvs, JREs, PDB caches and cloned repos were not copied). They are the seed corpus for
the conformance suite (T1–T9 in `docs/ARCHITECTURE.md` §6) and the fastest way to reproduce any
finding in `docs/research/` or `docs/VERIFICATION.md`.

| Folder | What it proves |
|---|---|
| `ipc-api/`, `verify-gui-api/`, `verify-ipc-config-home/`, `verify-sch-ipc/`, `verify-24966/` | IPC API live tests, config-home seeding, the eeschema handler crash (#24966), handler inventories 10 vs master |
| `swig-pcbnew/`, `verify-headless-board/`, `verify-headless-build/` | Headless board build / zone fill / save under each version's bundled python; the driver checklist |
| `schematic-authoring/`, `erc_verify/`, `verify-sch-20260306/`, `pin-orient-verify/`, `verify-pin-orient/`, `erc-json-scale/`, `verify-erc-json-units/` | Hand-written `.kicad_sch` generator (`gen.py` + `sexp.py`) that passes kicad-cli 10 ERC; the 12-orientation pin transform; the ERC JSON ×100 bug |
| `autorouting/`, `freerouting-verify/`, `verify-freerouting/`, `specctra-verify/`, `verify-specctra/` | DSN export → Freerouting 2.4.1 → SES import round trips, flag experiments, KiCadRoutingTools |
| `versioning-strategy/`, `fmt-compat/`, `verify-format-compat/`, `verify-cli-api/`, `verify-cli-api-claim/` | Format stamps per version, cross-version load matrix, kicad-cli flag drift 7→10 |
| `mcp-verify/`, `verify-mcp22/`, `vscode-agent-surface/` | mcp 2.2 server returning PNG ImageContent over stdio and streamable HTTP |
| `verify-symbols/`, `verify-symbols-pdb/` | cdb + symbols.kicad.org symbolication of KiCad crash dumps (no source build) |
| `kicad-plugin-and-build/`, `prior-art/`, `e2e-agent-workflow/` | Plugin mechanisms, source-build research, prior-art survey notes, tool-catalog derivation |
