# Display forwarding research — index

Question (owner, 2026-09-19): can KiCad's display be forwarded into VS Code and made interactive, so
the user gets the identical KiCad UI/UX inside VS Code? Synthesis and recommendation:
[`../../DISPLAY.md`](../../DISPLAY.md). Everything here is private research; prototypes live under
[`../../../prototypes/display/`](../../../prototypes/display/) (a README with run steps per prototype).
All GUI runs used a kiwrap-seeded sandbox root on scratch copies of the ecc83 demo; `%APPDATA%\kicad`
was unchanged after every spike; no processes were left running.

## Reports

| File | Route(s) | Verdict | Key numbers |
|---|---|---|---|
| [00-technology-survey.md](00-technology-survey.md) | VS Code webview facts from the vscode sources; catalogue of every route (WSL VNC, xpra, Windows VNC/WGC/WebRTC, Guacamole, Broadway, PCBJam WASM, KiCanvas); criteria matrix and ranking; local GL check in WSL | no GUI launched; ranked the spikes | Chromium 148 / Electron 42 in VS Code 1.138; `--enable-coi` gate for SharedArrayBuffer; `portMapping` is http(s)-only; Broadway rejected on source evidence |
| [vnc-novnc.md](vnc-novnc.md) | Windows host: TightVNC 2.8.88 `-sharewindow` of sandboxed `pcbnew.exe` → websockify → noVNC 1.7 in Chromium, then in a VS Code webview iframe; UltraVNC evaluated | **partial** — real pixels, interactive, but a screen-rect crop of a window on the user's desktop; no tooltips; input goes to the host foreground | key → first update median 27 ms (OpenGL) / 17 ms (Cairo), p90 230-310 ms; pan 39-45 fps; tvnserver 25 % of a core; clipboard both ways (155 kB) |
| [winstream.md](winstream.md) | Windows host: purpose-built WGC per-window capture + JPEG/WebSocket + SendInput or PostMessage injection; 10.0.6 and 10.99 | **partial** — display half excellent (dialogs, chooser, GL, 0 idle cost); input half needs the window on-screen/unobscured/foreground; PostMessage defeated by `wxGetMousePosition` | wheel 52 ms median, key 54 ms; 24 fps pan at 16 % of a core; 0 px HiDPI mapping error at 150 %; per-window capture dismisses popup menus (monitor-crop workaround) |
| [wsl-linux.md](wsl-linux.md) | WSL2 Ubuntu 24.04, KiCad 10.0.6 PPA: GTK Broadway, xpra 6.5.3 + xpra-html5 21, Xvfb + x11vnc + noVNC, x11vnc on WSLg | **xpra works** (identical UI incl. tooltips, dialogs, chooser, hotkeys, clipboard, reconnect); Broadway not viable; x11vnc on WSLg black canvas | xpra: wheel 18-52 ms, key 68-111 ms, 57 fps pan, pcbnew 410-700 % CPU on llvmpipe; Xvfb+x11vnc 29 fps; Broadway pan 1.5 fps + crashes |
| [vscode-shell.md](vscode-shell.md) | VS Code side only: WebviewPanel + CustomReadonlyEditorProvider in probe / iframe / canvas modes, rescue keybindings, DPI, clipboard, pointer lock, fullscreen, lifecycle | **solved** — 215/215 chords reach the page; 54 stock VS Code conflicts, 85 rescued by keybindings, only Alt-menu settings remain; iframe shape: VS Code sees nothing but clipboard denied | input RTT 0.5 ms idle; stamp-to-paint 18-39 ms; 22 fps synthetic 1920x1200 RGBA; devicePixelRatio 1.5 / 1.8 |
| [vscode-shell-keys.md](vscode-shell-keys.md) | Per-chord table (215 chords × runs A/B/C) | reference | which chords VS Code acts on, which are rescued, which the iframe swallows |

## Screenshots

All PNGs are < 300 KB and show what the *streamed client* saw unless the name says "host".

- `img/vnc-novnc-01…09-*.png` — TightVNC/noVNC: host reference, OpenGL and Cairo framebuffers, popups
  host-vs-framebuffer (tooltip missing), Board Setup dialog, rotate before/after, inside the VS Code
  webview, VS Code intercepting keys, wheel/pan reaching KiCad from VS Code.
- `winstream/01…10-*.png` — WGC: 4K frame, 1080p viewer, injected zoom, select/R/Delete/Ctrl+Z tiles,
  context menu composited, owned dialog, footprint chooser, PostMessage doubled chars, 10.99 nightly,
  modal file-open warning. Raw numbers: `winstream/data/run*.json`.
- `wsl-linux/00…11-*.png` — WSLg reference, Broadway (widget factory, wxGLCanvas error, Cairo UI, menus,
  pan/dialog/chooser nag), xpra (OpenGL GAL, menu/context/tooltip/pan, rotate/properties/chooser,
  clipboard/reconnect/HiDPI), x11vnc on WSLg (black canvas), Xvfb + x11vnc + noVNC.
- `img/vscode-shell-A…E-*.png` — rescue vs stock palette, Alt+F menu, right-click overlay default vs
  suppressed, pointer lock, custom editor, iframe Ctrl+Shift+P, split, zen, canvas stream.

## Prototypes

| Directory | What it is | Platform |
|---|---|---|
| `prototypes/display/vscode-novnc/` | TypeScript extension: WebviewPanel iframe to a noVNC page (`?fit=dpr`), `CustomReadonlyEditorProvider` for `*.kicad_pcb`, start/stop scripts for TightVNC + websockify | Windows |
| `prototypes/display/winstream/` | Python mini-VNC: WGC capture, JPEG/WebSocket server, viewer page, SendInput/PostMessage injectors, Playwright benches | Windows |
| `prototypes/display/xpra-wsl/` | WSL scripts (install, seed sandbox, start xpra / Broadway / Xvfb+VNC, stop) + Playwright drivers with JSON step lists | WSL2 / Linux |
| `prototypes/display/vscode-shell/` | TypeScript extension: panel + custom editor in probe/iframe/canvas modes, generated rescue keybindings, synthetic frame server, Win32 driver, analyzer | VS Code (driver Windows-only) |

## Not spiked (documentary only, see the survey)

KasmVNC / Selkies / n.eko (WebRTC, H.264/AV1), Guacamole / Myrtille / Weston-RDP, Sunshine + virtual
display driver + moonlight-web, custom WGC + NVENC + WebRTC, PCBJam (KiCad compiled to WebAssembly),
TightVNC `-shareapp`, WGC on another Windows virtual desktop, xpra at `--dpi=192`.
