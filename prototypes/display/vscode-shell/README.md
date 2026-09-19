# kiwrap display shell (prototype)

Prototype VS Code extension that hosts a remote KiCad display and measures what a webview can do.
Findings: `docs/research/display/vscode-shell.md` and `docs/research/display/vscode-shell-keys.md`.
Not published, not packaged; `private: true`.

## What is in here

| path | purpose |
|---|---|
| `src/extension.ts` | activation, `WebviewPanel` "KiCad view" (modes `probe` / `view` / `canvas`), `CustomReadonlyEditorProvider` for `*.kicad_pcb` / `*.kicad_sch`, demo HTTP server (iframe target through `portMapping`), synthetic WebSocket frame server, `cmd.jsonl` control channel for the driver, JSONL logging |
| `media/probe.js` | shared input probe: logs every `keydown/keyup/keypress/pointer*/click/auxclick/contextmenu/wheel/focus/blur/paste/resize` event plus DPI, clipboard, pointer-lock and fullscreen checks; relays from a nested iframe to the extension host |
| `media/probe.html` | probe page shown in the panel (mode `probe`) |
| `media/view.html` | iframe host page (mode `view` and the custom editor by default); pushes focus into the frame |
| `media/demo.html` | page served over `http://localhost:<port>` and iframed by `view.html` (stands in for noVNC / Xpra / Broadway) |
| `media/canvas.html` | `<canvas>` + WebSocket client with fps / bandwidth / RTT / stamp-to-paint HUD (mode `canvas`) |
| `tools/gen_keybindings.js` | regenerates the 178 rescue keybindings in `package.json` |
| `tools/drive_probe.ps1` | Windows driver: launches the Extension Development Host in an isolated profile, sends real input with `SendInput`, runs the DPI / clipboard / pointer-lock / zen / fullscreen / split / retain / custom-editor checks, captures screenshots, closes the host |
| `tools/analyze_probe.py` | turns `probe.jsonl` into a markdown table |

## Build

Node 24 LTS (`C:\Program Files\nodejs` on this machine, not on PATH in every shell):

```powershell
cd C:\Upwork\KiCad\prototypes\display\vscode-shell
$env:Path = "C:\Program Files\nodejs;$env:Path"
npm install --no-audit --no-fund      # ws, esbuild, typescript, @types/vscode, @types/ws, @types/node
npm run typecheck
npm run build                          # -> dist/extension.js (esbuild bundle; vscode + ws natives external)
```

`node_modules/` and `dist/` are git-ignored.

## Run by hand

```powershell
code --extensionDevelopmentPath=C:\Upwork\KiCad\prototypes\display\vscode-shell --new-window <some folder>
```

Then `Ctrl+Shift+P` -> `kiwrap: Open input probe` / `Open KiCad view (iframe URL)` / `Open KiCad view
(canvas + WebSocket)`, or open any `*.kicad_pcb` / `*.kicad_sch` (the custom editor is `priority:
default` in the prototype; "Reopen Editor With..." switches back to text).

Settings (`kiwrapShell.*`): `url` (iframe target; empty = built-in demo page), `wsUrl` (frame stream;
empty = built-in synthetic stream), `retainContextWhenHidden`, `rescueKeys` (turns the contributed
keybindings on/off through a context key), `logFile` (JSONL sink; `cmd.jsonl` next to it is the
control channel), `autoOpen` (`probe` / `view` / `canvas` at startup, used by the driver).

## Run the automated investigation (Windows, owner present)

Windows pop up on the desktop; the driver needs the foreground and fights for it. From a PowerShell
(not `-File`, so the `[bool]` parameters bind):

```powershell
cd C:\Upwork\KiCad\prototypes\display\vscode-shell
.\tools\drive_probe.ps1 -Run A -Rescue $true  -Retain $true  -Mode probe    # webview page, rescue keybindings on
.\tools\drive_probe.ps1 -Run B -Rescue $false -Retain $false -Mode probe    # stock VS Code behaviour
.\tools\drive_probe.ps1 -Run C -Rescue $true  -Retain $true  -Mode iframe   # cross-origin localhost iframe focused
.\tools\drive_probe.ps1 -Run D -Rescue $true  -Retain $true  -Mode canvas   # stream stats (fps / RTT)
.\tools\drive_probe.ps1 -Run E -Only mouse -Mode probe                      # mouse block only (context menu check)
python tools\analyze_probe.py <scratch>\runA\probe.jsonl --md <scratch>\runA\summary.md
```

Each run takes 4-6 minutes, writes `run<X>/probe.jsonl`, `run<X>/cmd.jsonl`, `run<X>/shots/*.png`
and a fresh `udd-*` profile under the scratch dir (`-Scratch` overrides; default is this session's
scratchpad `display/vscode-shell`), copies the ecc83 demo `.kicad_pcb` / `.kicad_sch` into a scratch
workspace, and kills the dev host by PID and by profile path at the end (`-KeepOpen` to inspect). No
KiCad process is started. `win+shift+s` is listed but never sent. CapsLock is restored at the end.

## What the shell expects from a backend

See section 4 of `docs/research/display/vscode-shell.md`: either a `http://localhost:<port>` page to
iframe (noVNC / Xpra HTML5 / Broadway), or a WebSocket that sends binary frames
(`u8 type=1 | u32 seq | f64 t_server_ms | u16 w | u16 h | u8 fmt | pixels`, `fmt 0` = RGBA8) and
receives JSON `{type:'resize'|'input'|'open'|'clipboard', ...}` with device-pixel coordinates. The
synthetic server in `src/extension.ts` (`startWs`) and the client in `media/canvas.html` are the
reference implementation of that sketch.

## Findings that changed the code while measuring

- Right-click: VS Code overlays Cut/Copy/Paste on webviews; `data-vscode-context='{"preventDefaultContextMenuItems": true}'` on the container removes it (verified, run E).
- Iframe focus: `panel.reveal()` / `focusActiveEditorGroup` focus the webview body, not a nested frame; `view.html` now pushes focus into the frame on `focus` / `focusin` / stray `keydown` (run C went from keys landing on the host page to 215/215 in the frame).
- Keybinding rescue needs both `activeWebviewPanelId == '<viewType>'` and `activeCustomEditorId == '<viewType>'` in the `when` clause, one for the panel and one for the custom editor.
