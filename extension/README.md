# KiCad (kiwrap) - VS Code extension

The VS Code front end of kiwrap (docs/ARCHITECTURE.md section 9; D18: VS Code is the one user surface,
D13: Claude Code inside VS Code is the agent client). The extension is an MCP client of
`uv run kiwrap serve` and calls the `kw_*` tools like an agent would.

| feature | where |
|---|---|
| server process + MCP stdio client, typed wrappers (`kw_env`, `kw_project`, `kw_sch_check`, `kw_pcb_drc`, `kw_pcb_check`, `kw_sch_render`, `kw_pcb_render`, `kw_live`), reconnect / restart, stderr in the "KiCad (kiwrap)" output channel | `src/mcpClient.ts`, `src/kiwrapServer.ts` |
| "KiCad (kiwrap)" view: server, installs (default + selected marked), engines, project (stage, target KiCad, last ERC / DRC numbers), live session | `src/status/tree.ts`, `src/status/statusBar.ts` |
| Problems panel: `kw_sch_check` / `kw_pcb_drc` violations as diagnostics on the `.kicad_sch` / `.kicad_pcb` line of the item's uuid (line 1 fallback), code = KiCad type, related information for the second item, refresh on save / on demand | `src/diagnostics/` |
| `kiwrap.preview` read-only custom editor (priority `option`) for `*.kicad_sch` / `*.kicad_pcb`: `kw_*_render` PNG / SVG with zoom + pan, re-render on file change, "Open Live KiCad" | `src/editors/` |
| commands: Configure Claude Code (`.mcp.json` + CLAUDE.md / AGENTS.md block + skill copy, diff preview), Run ERC, Run DRC, Open Live KiCad, Select KiCad install, Restart server | `src/commands/` |

Settings: `kiwrap.uvPath`, `kiwrap.serverProject` (the kiwrap checkout; auto-detected when it is a
workspace folder or `KIWRAP_REPO`), `kiwrap.storageHome`, `kiwrap.kicadInstall`, `kiwrap.checkOnSave`.

`Open Live KiCad` (`kiwrap.openLive`) hands the project, the editor kind and the selected install
(`kiwrap.kicadInstall`) to the Live KiCad module (`src/live/`, activated from `extension.ts` with
this extension's MCP client): `kw_live launch` starts a sandboxed KiCad, `kiwrap-live.exe` is
spawned (`bin/kiwrap-live.exe` in the VSIX, `../helper/target/release/` in a dev checkout,
`kiwrap.live.helperPath` overrides), the helper is registered with `kw_live attach`, and the file
opens in the `kiwrap.live` custom editor (the `.kicad_pcb` tab shows pcbnew, the `.kicad_sch` tab
eeschema; other KiCad windows open as panels). Mode (`kiwrap.live.mode`, default `auto`): view-only
on a stock KiCad (a click raises the real window), **hidden** on a build carrying
`mr/display-input` (the KiCad is cloaked, the tab is the UI), `mirror` as the opt-in on stock.
The agent's own server (Claude Code, from `.mcp.json`) finds the live session through its record
under the shared storage home and writes into the same KiCad, so `kw_pcb_place` from the agent
repaints the tab (docs/EXTENSION.md section 6). The tree and the status bar mirror the session.

## Build / test

```powershell
cd extension
$env:Path = "C:\Program Files\nodejs;$env:Path"
npm install --no-audit --no-fund
npm run typecheck
npm run build                 # esbuild -> dist/extension.js (vscode external, SDK bundled)
npm run test:unit             # mocha: fake stdio MCP server, diagnostics mapping, .mcp.json writer
node test/live/run.mjs unit   # the Live KiCad module: helper process, control client, surfaces, shield, mode, kw_live unwrapping
npm run test:integration      # @vscode/test-electron: real `uv run kiwrap serve`, ecc83 demo copy, ERC/DRC -> Problems, tree
node test/live/run.mjs integration   # dev host + fake helper: streams, DPI-exact input, clipboard, panels, hidden-tab resync
npm run test:e2e              # THE end to end: real server + real helper + real KiCad GUIs (stock view-only, display-input build hidden)
                              #   + a second MCP client (test/e2e/agent.py); evidence -> docs/research/display/img/phase2-*.png
npm run package               # kiwrap.vsix (not published); the repo's scripts/build_extension.ps1 builds dist/kiwrap-<ver>.vsix with the helper
```

`npm run test:e2e` launches sandboxed KiCad GUIs (one at a time, killed by pid) and takes ~2 min;
`KIWRAP_E2E_HIDDEN=skip` skips the hidden-mode test, `KIWRAP_E2E_IMG` redirects the screenshots.
The suite reads the extension API (`extension.exports`: `session`, `tree`, `diagnostics`, `live`,
`preview`) and the Live view's test hooks (`LiveView.testDispatch` / `testQuery`).

Run by hand: `code --extensionDevelopmentPath=C:\Upwork\KiCad\extension --new-window <kicad project folder>`
with `kiwrap.serverProject` set to the kiwrap checkout (or `KIWRAP_REPO`).

MIT. No GPL code. Runtime dependencies: `@modelcontextprotocol/sdk` (MIT), `zod` (MIT).
