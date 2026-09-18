---
name: kicad-flow
description: End-to-end KiCad electronics workflow for AI agents - create or edit schematics, run ERC, generate the PCB, place and route, run DRC, and export fabrication files using the kicad MCP tools. Use when the user asks to design, check, lay out, route, or export a PCB or schematic.
license: MIT
compatibility: Requires the kicad MCP server (tools prefixed kicad_*) and KiCad 7-10 installed.
metadata:
  author: kicad-agent
  version: "0.1"
---
# KiCad flow

1. Call `kicad_project_info` first; it reports the KiCad version and which backend (kicad-cli / IPC / file) is active.
2. Schematic: edit, then ALWAYS run `kicad_erc` and fix errors before `kicad_update_pcb_from_sch`.
3. Layout: `kicad_place`, `kicad_route`, then `kicad_drc`; call `kicad_render_board` to LOOK at the result before declaring done.
4. Fab: `kicad_export_gerbers`, `kicad_export_drill`, `kicad_export_bom`, `kicad_export_pos`.

See [references/tool-cheatsheet.md](references/tool-cheatsheet.md) for parameters.
