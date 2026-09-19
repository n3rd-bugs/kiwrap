# vscode-novnc – live KiCad window inside a VS Code webview (spike prototype)

Status: spike, Windows only, **not** wired into `src/kiwrap`. Findings and measurements:
`docs/research/display/vnc-novnc.md`.

What it does: a VNC server (TightVNC 2.8.88 in application mode, no service) shares **one** sandboxed
`pcbnew.exe` window on `127.0.0.1:5900`; websockify bridges it to `ws://127.0.0.1:6080` and serves a noVNC
checkout plus `page/kicad.html`; the extension opens that page in a `WebviewPanel` (iframe) and registers a
read-only custom editor for `*.kicad_pcb` that shows the same panel.

```
pcbnew.exe (sandboxed)  <-SendInput/BitBlt-  tvnserver.exe -run  <-RFB 5900-  websockify  <-ws 6080-  noVNC in VS Code webview
```

## Run

Prerequisites (portable, nothing installed system-wide; UAC never asked):

```powershell
$S = "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike"; New-Item -ItemType Directory -Force $S | Out-Null
# TightVNC 2.8.88 GPL, unpacked with an administrative install (no service, no registration)
Invoke-WebRequest https://www.tightvnc.com/download/2.8.88/tightvnc-2.8.88-gpl-setup-64bit.msi -OutFile $S\tightvnc.msi
Start-Process msiexec -ArgumentList "/a `"$S\tightvnc.msi`" /qn TARGETDIR=`"$S\tvn`"" -Wait      # -> $S\tvn\PFiles\TightVNC\tvnserver.exe
git clone --depth 1 https://github.com/novnc/noVNC.git $S\novnc                                 # MPL-2.0, tested at 1.7.0
uv venv $S\pyvenv; uv pip install --python $S\pyvenv\Scripts\python.exe websockify              # LGPL-3.0, tested at 0.13.0
```

Start the stack (sandboxed config root is seeded through `scripts/seed_config_root.py`, so the user's
`%APPDATA%\kicad` is never touched):

```powershell
cd C:\Upwork\KiCad
powershell -ExecutionPolicy Bypass -File prototypes\display\vscode-novnc\scripts\start-stack.ps1            # OpenGL canvas
powershell -ExecutionPolicy Bypass -File prototypes\display\vscode-novnc\scripts\start-stack.ps1 -Canvas cairo
```

Check in a browser first: `http://127.0.0.1:6080/kicad.html?fit=dpr` (`fit=dpr` = 1:1 physical pixels on a
HiDPI display, `fit=window` = scale to the page, no `fit` = raw framebuffer size).

Build and run the extension:

```powershell
cd prototypes\display\vscode-novnc
npm install
npm run compile
$env:KIWRAP_VNC_AUTO_OPEN = "1"     # opens the panel on activation (spike convenience)
code --extensionDevelopmentPath="$PWD" --user-data-dir="$env:TEMP\kiwrap-vnc-vsc" --extensions-dir="$env:TEMP\kiwrap-vnc-ext" --new-window "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike\proj"
```

In the dev host: command **kiwrap: Open KiCad (noVNC) panel**, or right-click `vncspike.kicad_pcb` -> *Open
With...* -> *KiCad PCB Editor (noVNC)*. Settings: `kiwrapVnc.url`, `kiwrapVnc.port`.

Stop everything (kills only the PIDs the start script recorded, removes `HKCU\Software\TightVNC`):

```powershell
powershell -ExecutionPolicy Bypass -File prototypes\display\vscode-novnc\scripts\stop-stack.ps1
```

## Files

* `src/extension.ts` – command + `WebviewPanel` (iframe, CSP `frame-src` = the noVNC origin, `portMapping`),
  `CustomReadonlyEditorProvider` for `*.kicad_pcb`.
* `page/kicad.html` – minimal noVNC page: creates `RFB`, exposes `window.rfb` and a framebuffer-update counter
  (`window.__stats`, used by the Playwright measurements), `fit=dpr|window`.
* `scripts/start-stack.ps1`, `scripts/stop-stack.ps1` – the exact server flags that worked
  (`tvnserver.exe -run`, `-controlapp -sharewindow "vncspike — PCB Editor"`, registry `LoopbackOnly=1`,
  `UseVncAuthentication=0`, `GrabTransparentWindows=1`, `PollingInterval=30`).

## Known limits (measured, see the research note)

* Single-window share is a screen-rect crop: other windows covering KiCad show through and receive input;
  dialogs/menus outside the rect are invisible; tooltips are never captured.
* VNC input goes to the host's foreground window; a KiCad dialog opened without focus swallows keys until clicked.
* Inside VS Code, `F1`, `Ctrl+P`, `Ctrl+Shift+P` and `Alt` are taken by VS Code even when the KiCad iframe is focused.
* No DPI negotiation (KiCad renders at the host's scale); no client-side resize (`SetDesktopSize` unsupported).
* ~25 % of a core in `tvnserver` while panning (screen polling); no GPU path without a driver install.
* Licensing: TightVNC GPL-2.0 (separate process), noVNC MPL-2.0, websockify LGPL-3.0.
