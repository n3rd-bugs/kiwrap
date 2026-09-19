# Display forwarding research — index

Question (owner, 2026-09-19): can KiCad's display be forwarded into VS Code and made interactive, so
the user gets the identical KiCad UI/UX inside VS Code? Follow-up (owner, 2026-09-20): can VNC + KiCad
live on the Windows env when a project has several editors open? Synthesis and recommendation:
[`../../DISPLAY.md`](../../DISPLAY.md) (§12 for the follow-up). The KiCad-side patch of the follow-up
is on the private fork branch `mr/display-input`; its issue/MR text is drafted, not posted (D19), in
[`../../upstream/drafts/display-input-mr.md`](../../upstream/drafts/display-input-mr.md) and tracked as
gap card G15 in [`../../UPSTREAM.md`](../../UPSTREAM.md). Everything here is private research; prototypes live under
[`../../../prototypes/display/`](../../../prototypes/display/) (a README with run steps per prototype).
All GUI runs used a kiwrap-seeded sandbox root on scratch copies of the ecc83 demo; `%APPDATA%\kicad`
was unchanged after every spike (the win-multi spike saw it change under a *concurrent* pytest run against
KiCad 8.0, see its §7); no processes were left running.

## Reports

| File | Route(s) | Verdict | Key numbers |
|---|---|---|---|
| [00-technology-survey.md](00-technology-survey.md) | VS Code webview facts from the vscode sources; catalogue of every route (WSL VNC, xpra, Windows VNC/WGC/WebRTC, Guacamole, Broadway, PCBJam WASM, KiCanvas); criteria matrix and ranking; local GL check in WSL | no GUI launched; ranked the spikes | Chromium 148 / Electron 42 in VS Code 1.138; `--enable-coi` gate for SharedArrayBuffer; `portMapping` is http(s)-only; Broadway rejected on source evidence |
| [vnc-novnc.md](vnc-novnc.md) | Windows host: TightVNC 2.8.88 `-sharewindow` of sandboxed `pcbnew.exe` → websockify → noVNC 1.7 in Chromium, then in a VS Code webview iframe; UltraVNC evaluated | **partial** — real pixels, interactive, but a screen-rect crop of a window on the user's desktop; no tooltips; input goes to the host foreground | key → first update median 27 ms (OpenGL) / 17 ms (Cairo), p90 230-310 ms; pan 39-45 fps; tvnserver 25 % of a core; clipboard both ways (155 kB) |
| [winstream.md](winstream.md) | Windows host: purpose-built WGC per-window capture + JPEG/WebSocket + SendInput or PostMessage injection; 10.0.6 and 10.99 | **partial** — display half excellent (dialogs, chooser, GL, 0 idle cost); input half needs the window on-screen/unobscured/foreground; PostMessage defeated by `wxGetMousePosition` | wheel 52 ms median, key 54 ms; 24 fps pan at 16 % of a core; 0 px HiDPI mapping error at 150 %; per-window capture dismisses popup menus (monitor-crop workaround) |
| [wsl-linux.md](wsl-linux.md) | WSL2 Ubuntu 24.04, KiCad 10.0.6 PPA: GTK Broadway, xpra 6.5.3 + xpra-html5 21, Xvfb + x11vnc + noVNC, x11vnc on WSLg | **xpra works** (identical UI incl. tooltips, dialogs, chooser, hotkeys, clipboard, reconnect); Broadway not viable; x11vnc on WSLg black canvas | xpra: wheel 18-52 ms, key 68-111 ms, 57 fps pan, pcbnew 410-700 % CPU on llvmpipe; Xvfb+x11vnc 29 fps; Broadway pan 1.5 fps + crashes |
| [vscode-shell.md](vscode-shell.md) | VS Code side only: WebviewPanel + CustomReadonlyEditorProvider in probe / iframe / canvas modes, rescue keybindings, DPI, clipboard, pointer lock, fullscreen, lifecycle | **solved** — 215/215 chords reach the page; 54 stock VS Code conflicts, 85 rescued by keybindings, only Alt-menu settings remain; iframe shape: VS Code sees nothing but clipboard denied | input RTT 0.5 ms idle; stamp-to-paint 18-39 ms; 22 fps synthetic 1920x1200 RGBA; devicePixelRatio 1.5 / 1.8 |
| [vscode-shell-keys.md](vscode-shell-keys.md) | Per-chord table (215 chords × runs A/B/C) | reference | which chords VS Code acts on, which are rescued, which the iframe swallows |
| [win-multi.md](win-multi.md) | Windows host, a *project* session (`kicad.exe` + schematic editor + PCB editor + 3D viewer + dialogs/chooser, one pid): TightVNC `-shareapp` + noVNC vs WGC per-frame *surfaces* (event hook, per-editor streams) mapped to two VS Code custom-editor tabs + a 3D panel; a "hybrid" injector (posted input with a borrowed cursor) | **WGC surfaces work** (pcb tab + sch tab + 3D from one helper, VS Code keeps the foreground); `-shareapp` is a 9-fps whole-desktop poll with black holes | process model: 1 pid / 1 thread / unowned frames, dialogs owned; WGC 25-29 fps per tab with 3 streams live, 40-70 ms wheel->pixel, hook->window list 11-15 ms; `-shareapp` 9 fps, 100 % core idle; hybrid injector: select/R/Delete/undo/zoom/pan/drag/dialog/chooser with Notepad foreground, right-click needs one flip, KiCad's own `Home` warps the cursor |
| [win-input-patch.md](win-input-patch.md) | Windows host, KiCad built from the fork branch `mr/display-input` (`KICAD_INPUT_FROM_EVENTS`): the editor covered by another window or **cloaked** (`DWMWA_CLOAK`), driven with `PostMessage` only (no SendInput / foreground / focus / AttachThreadInput), WGC capture | **works** — select, R, Delete, Ctrl+Z, drag-move, wheel zoom, middle pan, double-click, properties dialog (type + OK), chooser (A, filter, Esc), context menu at the reported pointer + item click; real cursor and foreground untouched; tooltips and popup-menu placement remain OS-limited; fully off-screen windows get no WGC updates | 25 files +534 −48; wheel→pixel 69 ms median (37 ms KiCad+WGC), 27.6 fps motion at 8.8 % server CPU, ≈40 fps pan, dialog OK 73 ms, cloaked window: 0 desktop pixels, full WGC rate |

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
- `img/win-multi-01…07-*.png` — project session on the host, TightVNC `-shareapp` framebuffer (all
  frames, foreign window = black hole, chooser), WGC per-surface clients, VS Code with the `.kicad_pcb`
  and `.kicad_sch` tabs live side by side (+ 3D panel). `img/win-input-01…04-*.png` — hybrid injector
  matrix with Notepad in the foreground, dialog composited, a real click into the pcb tab, the
  context-menu foreground flip. Raw numbers: `win-multi/data/*.json`.
- `img/win-input-patch-*.png` — the `KICAD_INPUT_FROM_EVENTS` spike: edit matrix (10 tiles, covered
  window, posted input), properties dialog field typed + chooser filtered, cloaked window: desktop
  pixels vs the WGC frame of the same moment, cloaked dialog + the browser client's HUD. Step lists:
  `prototypes/display/winstream/bench/actions/events_*.json`; numbers are quoted in the report.

## Prototypes

| Directory | What it is | Platform |
|---|---|---|
| `prototypes/display/vscode-novnc/` | TypeScript extension: WebviewPanel iframe to a noVNC page (`?fit=dpr`), `CustomReadonlyEditorProvider` for `*.kicad_pcb`, start/stop scripts for TightVNC + websockify | Windows |
| `prototypes/display/winstream/` | Python mini-VNC: WGC capture per *surface* (editor frame + owned dialogs), SetWinEventHook tracking, JPEG/WebSocket server (`/ws?surface=pcb`), viewer page, SendInput / PostMessage / hybrid injectors, plus the message-only `EventInjector` for the patched KiCad (`run_events.py`, `winstream/inject_events.py`, `bench/make_build_env.py`, `bench/ws_drive.py`, `bench/actions/events_*.json`), Playwright benches (`bench/multi/` = project-session scripts) | Windows |
| `prototypes/display/xpra-wsl/` | WSL scripts (install, seed sandbox, start xpra / Broadway / Xvfb+VNC, stop) + Playwright drivers with JSON step lists | WSL2 / Linux |
| `prototypes/display/vscode-shell/` | TypeScript extension: panel + custom editor in probe/iframe/canvas/**live** modes (live = one winstream surface per tab, `kiwrapShell.liveWs`), generated rescue keybindings, synthetic frame server, Win32 drivers (`drive_probe.ps1`, `drive_live.ps1`), analyzer | VS Code (drivers Windows-only) |

## Not spiked (documentary only, see the survey)

KasmVNC / Selkies / n.eko (WebRTC, H.264/AV1), Guacamole / Myrtille / Weston-RDP, Sunshine + virtual
display driver + moonlight-web, custom WGC + NVENC + WebRTC, PCBJam (KiCad compiled to WebAssembly),
WGC on another Windows virtual desktop, xpra at `--dpi=192`, an IddCx virtual display driver (superseded
by `DWMWA_CLOAK` under the patch, win-input-patch.md §4). (TightVNC `-shareapp` is now measured in
win-multi.md.)
