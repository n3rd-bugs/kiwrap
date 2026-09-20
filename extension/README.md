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
| **Live KiCad** (`kiwrap.live` custom editor): the real, sandboxed KiCad editor streamed live into the `.kicad_pcb` / `.kicad_sch` tab; an agent's edit shows up in the tab in well under a second; **Raise KiCad** (HUD button, double-click, command) brings the real window to the front to edit by hand | `src/live/`, `media/live/` |
| commands: Configure Claude Code (`.mcp.json` + CLAUDE.md / AGENTS.md block + skill copy, diff preview), Run ERC, Run DRC, Open Live KiCad, Close Live KiCad, Live KiCad: Raise KiCad, Select KiCad install, Restart server | `src/commands/`, `src/live/index.ts` |

Settings: `kiwrap.uvPath`, `kiwrap.serverProject` (the kiwrap checkout; auto-detected when it is a
workspace folder or `KIWRAP_REPO`), `kiwrap.storageHome`, `kiwrap.kicadInstall`, `kiwrap.checkOnSave`;
Live KiCad: `kiwrap.live.raiseOnDoubleClick` (default on), `kiwrap.live.fit`, `kiwrap.live.hud`,
`kiwrap.live.helperPath`, `kiwrap.live.autoOpen3d`, and one EXPERIMENTAL knob, `kiwrap.live.inputMode`
(see below).

## The live view

`Open Live KiCad` (`kiwrap.openLive`, also the "Open Live KiCad" button of the preview and the
Start button of an empty live tab) hands the project, the editor kind and the selected install
(`kiwrap.kicadInstall`) to the Live KiCad module (`src/live/`, activated from `extension.ts` with
this extension's MCP client): `kw_live launch` starts a sandboxed stock KiCad (10.0.x or 10.99/11)
as a normal window on the desktop, never activated; `kiwrap-live.exe` is spawned
(`bin/kiwrap-live.exe` in the VSIX, `../helper/target/release/` in a dev checkout,
`kiwrap.live.helperPath` overrides) and captures that window with Windows.Graphics.Capture; the
helper is registered with `kw_live attach`; and the file opens in the `kiwrap.live` custom editor
(the `.kicad_pcb` tab shows pcbnew, the `.kicad_sch` tab eeschema; other KiCad windows, the 3D
viewer for one, open as panels).

What you get: **the real editor, live.** The agent's own server (Claude Code, from `.mcp.json`)
finds the live session through its record under the shared storage home and writes into the same
KiCad, so a `kw_pcb_place` from the agent repaints the tab about half a second after the call
(measured 431-474 ms, docs/EXTENSION.md section 9.1). Dialogs KiCad opens are composited on the
tab; ERC / DRC keep filling the Problems panel while the session lives. The tab itself does not
forward your mouse or keyboard to KiCad: **to edit by hand, raise KiCad** - the HUD's "Raise
KiCad" button, a double-click on the picture (`kiwrap.live.raiseOnDoubleClick`) or the command
*Live KiCad: Raise KiCad* bring the real window to the front; when you are done, come back to VS
Code and the tab keeps streaming. VS Code's own keyboard shortcuts behave exactly as in any other
tab. The tree and the status bar mirror the session; *Close Live KiCad* ends it (the KiCad is
killed by pid, its lock files removed).

EXPERIMENTAL, unsupported (docs/DECISIONS.md D20): `kiwrap.live.inputMode` (`none` by default)
can make the tab inject input into KiCad (`mirror` / `hybrid` on a visible KiCad, `events` on a
fork build that cloaks the window). These are research paths with known rough edges (cursor
warps, foreground flips, popup menus on the desktop); they are not offered anywhere in the UI, and
only while one of them is active does the extension shield VS Code's keybindings from the live tab.

## Build / test

```powershell
cd extension
$env:Path = "C:\Program Files\nodejs;$env:Path"
npm install --no-audit --no-fund
npm run typecheck
npm run build                 # esbuild -> dist/extension.js (vscode external, SDK bundled)
npm run test:unit             # mocha: fake stdio MCP server, diagnostics mapping, .mcp.json writer
node test/live/run.mjs unit   # the Live KiCad module: helper process, control client, surfaces, shield, input-mode decision, kw_live unwrapping
npm run test:integration      # @vscode/test-electron: real `uv run kiwrap serve`, ecc83 demo copy, ERC/DRC -> Problems, tree
node test/live/run.mjs integration   # dev host + fake helper: streams, the live view (raise on button / double-click, nothing injected),
                              #   DPI-exact input in the experimental events mode, clipboard, panels, hidden-tab resync
npm run test:e2e              # THE end to end: real server + real helper + real stock KiCad GUIs (10.99 and 10.0.x) in the live view,
                              #   Raise KiCad verified with GetForegroundWindow, + a second MCP client (test/e2e/agent.py);
                              #   evidence -> docs/research/display/img/phase2-*.png
npm run package               # kiwrap.vsix (not published); the repo's scripts/build_extension.ps1 builds dist/kiwrap-<ver>.vsix with the helper
```

`npm run test:e2e` launches sandboxed KiCad GUIs (one at a time, killed by pid) and takes ~2 min;
`KIWRAP_E2E_STOCK2=skip` skips the 10.0.x step, `KIWRAP_E2E_HIDDEN=1` opts into the experimental
cloaked-mode step (needs a fork build carrying `mr/display-input` registered), `KIWRAP_E2E_IMG`
redirects the screenshots.
The suite reads the extension API (`extension.exports`: `session`, `tree`, `diagnostics`, `live`,
`preview`) and the Live view's test hooks (`LiveView.testDispatch` / `testQuery`).

Run by hand: `code --extensionDevelopmentPath=C:\Upwork\KiCad\extension --new-window <kicad project folder>`
with `kiwrap.serverProject` set to the kiwrap checkout (or `KIWRAP_REPO`).

MIT. No GPL code. Runtime dependencies: `@modelcontextprotocol/sdk` (MIT), `zod` (MIT).
