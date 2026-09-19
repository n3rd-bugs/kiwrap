# Forwarding KiCad's display into VS Code — synthesis and recommendation

Status: research synthesis, 2026-09-19; **follow-up folded in 2026-09-20 (§12)** for the owner's second
question *"can VNC + KiCad live on the Windows env? remember a KiCad project can have multiple editors
open at a time"*: the multi-editor process model, TightVNC `-shareapp` vs WGC surfaces in VS Code tabs,
a hybrid injector, and a KiCad patch on the fork that makes a hidden (cloaked) KiCad drivable with
posted messages. §1, §2, §7.2, §8, §9 and §10 carry the updates; §12 has the measurements and the new
decisions. Private. Owner question: *"can we forward the KiCad display to
VS Code and make it interactive, so the user has the identical UI/UX of KiCad from within VS Code?"*

This document consolidates five hands-on spikes and one technology survey (all in
[`research/display/`](research/display/README.md), with screenshots and raw numbers). Every claim
below is tied to a spike report; anything not measured is marked **untested**. Decisions this touches:
D8 (licensing), D12/D18 (platforms), D13 (VS Code is the client), D16 (KiCad 11 first), D17 (data-first
feedback, fork for patches). ARCHITECTURE §9 "Live-KiCad panel" is the plan this refines.

---

## 1. Short answer

**Yes, the real KiCad can be shown live and driven from a VS Code tab — it was done four different ways
this week — but "identical UI/UX inside VS Code" is only fully true when KiCad runs somewhere VS Code is
not (a Linux virtual display), and on the Windows host it is a *mirror of a window that must also exist on
the user's desktop*.** The physics:

- A VS Code extension has no native surface. Every route ends in a sandboxed Chromium `<iframe>` that
  receives pixels over a socket and sends DOM events back (survey §1.1). There is no way to re-parent
  an HWND/X11 window into VS Code.
- The VS Code side is solved and cheap: a `CustomReadonlyEditorProvider` for `*.kicad_pcb` /
  `*.kicad_sch` (or a `WebviewPanel`) hosts any HTML5 display client; all 215 tested chords reach the
  page; VS Code's own 54 conflicting chords can be silenced with one generated `contributes.keybindings`
  block; measured shell floor is ~0.5 ms input round trip and <5 ms per frame
  ([vscode-shell](research/display/vscode-shell.md)).
- **Windows host, stock KiCad (D18's target):** the display half is excellent — `Windows.Graphics.Capture`
  per window gives the real wxMSW UI with the OpenGL GAL canvas, owned dialogs and the footprint chooser,
  24-30 fps, 40-55 ms input-to-pixel, ~5 % of one core, 0 cost idle
  ([winstream](research/display/winstream.md)). The input half is the structural problem: reliable
  input needs `SendInput`, which needs the KiCad window on-screen, unobscured and (for keys)
  foreground; the hidden-window alternative (`PostMessage`) is defeated by KiCad's own canvas reading the
  real OS cursor (`wxGetMousePosition`) and wxDialog ignoring posted `Esc`. A VNC server
  (TightVNC `-sharewindow`) has the same limits plus screen-rect leakage, no tooltips and 25 % of a core
  polling ([vnc-novnc](research/display/vnc-novnc.md)).
- **Windows host, follow-up 2026-09-20 (§12):** a project session is one `kicad.exe` process with the
  manager, schematic editor, PCB editor and 3D viewer as unowned sibling frames and dialogs owned by
  their editor; the WGC helper streams each frame as its own *surface* into the `.kicad_pcb` /
  `.kicad_sch` tabs plus a 3D panel (25-29 fps per tab with three live, 40-70 ms wheel-to-pixel), while
  TightVNC `-shareapp` is a 9 fps whole-desktop poll with black holes under VS Code. The input wall
  moved: a hybrid injector (posted moves, borrowed cursor for clicks) drives stock KiCad while VS Code
  keeps the foreground and the cursor, and a 25-file KiCad patch on the fork (`mr/display-input`,
  `KICAD_INPUT_FROM_EVENTS=cloak`) makes a KiCad that is *not drawn on the desktop at all* fully
  drivable with `PostMessage` (select, drag, hotkeys, dialogs, chooser, context menu; 69 ms wheel-to-pixel,
  0 idle). Still unfixed everywhere: tooltips, OS popup menus shown on the desktop, one foreground grab
  at KiCad launch; eeschema in the patched mode is untested.
- **Linux KiCad on a private virtual display (WSL2 here; a Linux host/container in general):** xpra
  seamless + xpra-html5 delivers exactly what the owner asked for — the identical UI with menus, tooltips,
  modal dialogs, the footprint chooser with its GL preview, all hotkeys, two-way clipboard, 57 fps pan,
  18-52 ms wheel latency, session survives reconnect ([wsl-linux](research/display/wsl-linux.md)). Its
  costs: it is a *second, Linux* KiCad (1.5 GB WSL install, own config/libraries), llvmpipe burns 4-7
  cores while panning under WSL2 (no GPU offload there), and D18 has put Linux out of scope.
- **Not viable:** GTK Broadway (KiCad and wx hard-pin X11, no GL, ~1.5 fps pan, broadwayd crashed
  twice in 10 min), x11vnc of a WSLg window (black GL canvas), UltraVNC (single-window share removed
  upstream), TightVNC `-shareapp` (measured in the follow-up: 9 fps, a core per idle client, black
  holes). **Untested:** KiCad-in-WebAssembly (PCBJam), KasmVNC/Selkies, Guacamole, a Windows virtual
  display driver (no longer needed with the patch, §12.7).

**Recommendation (§7, updated in §12.6):** build the cheap, robust Windows parts now — a capture-only
**Live KiCad** custom editor per `.kicad_pcb`/`.kicad_sch` plus a 3D panel, fed by one WGC helper with
per-editor surfaces, click-to-raise the real window, and a `kw_live screenshot` action for the agent
loop — on the vscode-shell design; make the injector a mode of the launched session: the events
injector against a cloaked, patched KiCad ("KiCad invisible, VS Code is the UI") when the supervisor
started a fork build carrying `mr/display-input`, and the hybrid mirror as an explicit opt-in on stock
installs (VS Code keeps the foreground; KiCad's own cursor warps and right-click flips remain); keep
xpra as the documented Linux/remote provider behind the same shell contract, informational under D18.
The patch stays in the private fork with an upstream draft on hold (D19).

---

## 2. What is physically possible

Two families exist, and only two:

| Family | How the pixels get into the tab | Real KiCad? | What limits it |
|---|---|---|---|
| **A. Remote display of the real KiCad** | a capture/compositor process (VNC server, xpra, WGC helper) encodes KiCad's window(s) and a client in the webview decodes them; input travels back over the same socket and is injected on the KiCad side | yes, pixel-identical (it *is* KiCad) | where KiCad's window physically lives: on the user's desktop (Windows: focus/foreground/occlusion fights) or on a private virtual display (Linux X11: clean; Windows: needs a second session or a virtual monitor driver) |
| **B. KiCad's code running inside the tab** | KiCad compiled to WebAssembly with a wx DOM port (PCBJam) or a re-implementation (KiCanvas) | PCBJam: real code, HTML widgets, WebGL GAL, not pixel-identical; KiCanvas: viewer only | PCBJam is a GPL-3 fork of one KiCad revision, needs `--enable-coi` + JSPI, no self-hosting yet; **untested**. KiCanvas is not KiCad's UI (already planned as previews, D17) |

Within family A the decisive variable is **input injection**, not capture:

- Linux X11: `XTEST` injects into a private X server where only KiCad lives. Nothing competes for
  focus. This is why xpra "just works".
- Windows: `SendInput` is global and goes to the foreground window at the real cursor position; there
  is no per-window injection API. `PostMessage` is per-window but KiCad reads the OS cursor and key
  state directly (measured: clicks select nothing, context menu opens at the real cursor, posted
  `Esc` ignored — winstream §2b). A Windows virtual display driver would make KiCad "on-screen but
  invisible" for capture, but input would still be `SendInput` with the same foreground contest
  (**untested**). *Follow-up (§12):* the `PostMessage` failures were root-caused to six places where
  KiCad/wxMSW consult the hardware (`GetCursorPos`, `GetAsyncKeyState`, `GetKeyState`, the `WH_KEYBOARD`
  hook behind `wxEVT_CHAR_HOOK`, `GenerateMouseLeave`, `center_on_zoom`'s `SetCursorPos`); a hybrid
  injector that borrows the real cursor for the instant of a click works around most of them on stock
  KiCad, and the fork patch `KICAD_INPUT_FROM_EVENTS` removes them, after which `DWMWA_CLOAK` gives the
  "on-screen but invisible" placement without any driver (WGC keeps capturing a cloaked window; a
  fully off-screen one gets no updates).

GTK Broadway is not a third family: it is family A with GDK as the compositor, and it is blocked by
both KiCad (`setenv("GDK_BACKEND","x11")`) and wxGTK's GL module (`gdk_set_allowed_backends("x11")`);
even with an `LD_PRELOAD` shim there is no GL at all (wsl-linux §3, survey §2.6).

---

## 3. Evidence base

| Spike | What was built and driven | Report | Prototype |
|---|---|---|---|
| Technology survey | VS Code source reading (webview sandbox, keybinding relay, portMapping, COI), route catalogue, prior art (google/vscode-vnc, PCBJam), local GL check in WSL | [00-technology-survey.md](research/display/00-technology-survey.md) | — |
| vnc-novnc | Windows: sandboxed `pcbnew.exe` 10.0.6 shared by TightVNC 2.8.88 `-sharewindow` → websockify → noVNC 1.7 in Playwright/Chromium, then inside a VS Code webview (`portMapping` iframe) | [vnc-novnc.md](research/display/vnc-novnc.md) | [prototypes/display/vscode-novnc](../prototypes/display/vscode-novnc/README.md) |
| winstream | Windows: purpose-built mini-VNC — WGC per-window capture + JPEG over WebSocket + SendInput/PostMessage injection; 10.0.6 and 10.99 nightly; Playwright benchmarks | [winstream.md](research/display/winstream.md) | [prototypes/display/winstream](../prototypes/display/winstream/README.md) |
| wsl-linux | WSL2 Ubuntu 24.04, KiCad 10.0.6 PPA: GTK Broadway, xpra 6.5.3 + xpra-html5, Xvfb + x11vnc + noVNC, x11vnc on WSLg; CDP-screencast latency/fps | [wsl-linux.md](research/display/wsl-linux.md) | [prototypes/display/xpra-wsl](../prototypes/display/xpra-wsl/README.md) |
| vscode-shell | VS Code 1.138 Extension Development Host: WebviewPanel + custom editor in probe/iframe/canvas modes, 178 rescue keybindings, 215-chord matrices A-E, DPI/clipboard/pointer-lock/fullscreen | [vscode-shell.md](research/display/vscode-shell.md), [vscode-shell-keys.md](research/display/vscode-shell-keys.md) | [prototypes/display/vscode-shell](../prototypes/display/vscode-shell/README.md) |
| win-multi (follow-up, 2026-09-20) | Windows: a *project* session (`kicad.exe` + schematic + PCB + 3D viewer + dialogs/chooser = one pid); TightVNC `-shareapp` vs WGC per-frame surfaces (event hook) into two VS Code custom-editor tabs + a 3D panel; "hybrid" injector (posted input, borrowed cursor) that leaves VS Code the foreground | [win-multi.md](research/display/win-multi.md) | winstream `bench/multi/`, vscode-shell `media/live.html` + `tools/drive_live.ps1` |
| win-input-patch (follow-up, 2026-09-20) | Windows: KiCad built from the fork branch `mr/display-input` (`KICAD_INPUT_FROM_EVENTS=1\|cloak`, 25 files, commit `d70b0af290`, private origin only); pcbnew covered by an opaque window, cloaked with `DWMWA_CLOAK`, or fully off-screen; driven with `PostMessage` only through a new events injector; WGC capture; Playwright + raw-WebSocket drivers | [win-input-patch.md](research/display/win-input-patch.md), upstream draft [display-input-mr.md](upstream/drafts/display-input-mr.md) | winstream `run_events.py`, `winstream/inject_events.py`, `bench/actions/events_*.json`; `C:/src/kicad` on `mr/display-input` |

Every GUI ran with a kiwrap-seeded sandbox root (`KICAD_CONFIG_HOME` etc.) on scratch copies of the
ecc83 demo; `%APPDATA%\kicad` hash `a90607a5…6d9d` was unchanged after the 2026-09-19 spikes; during
the 2026-09-20 follow-up it changed to `9299d260…23ee` because a *concurrent* pytest session ran KiCad
8.0 (`8.0\kicad_common.json` etc. written 23:22; the `10.0`/`10.99` directories, the only ones the
follow-up's processes could touch, are unchanged); no processes were left running (all stopped by PID). One caveat applies to all Windows measurements: the desktop was shared by three spikes
injecting input and stealing the foreground at the same time, so anomalies attributed to that were
re-run and are marked in the reports; the vnc-novnc VS Code key matrix is inconclusive for that reason.

---

## 4. Criteria matrix (measured)

Legend: **V** measured in a spike; **D** documentary (survey only); **—** not applicable; *untested* as
written. Numbers are from the linked reports. "Real UI" means the actual wxMSW/wxGTK KiCad pixels.

| Criterion | Windows: WGC helper (winstream) | Windows: TightVNC `-sharewindow` + noVNC | Linux/WSL: xpra seamless + html5 | Linux/WSL: Xvfb + x11vnc + noVNC | GTK Broadway | x11vnc on WSLg `:0` | KiCad WASM (PCBJam) |
|---|---|---|---|---|---|---|---|
| Status | V, partial | V, partial | **V, works** | V, works (inferior) | V, not viable | V, not viable | D, **untested** |
| **Fidelity: real UI** | yes (physical px, 4K verified) | yes (1600x1000 window rect crop, 8 px DWM border included) | yes | yes | yes but Cairo only, two nag dialogs per GL canvas | GTK widgets only | real code, HTML widgets, WebGL GAL (not pixel-identical) |
| GL canvas rendered | yes (OpenGL; 10.0.6 and 10.99) | yes (OpenGL and Cairo, 0 % black) | yes (llvmpipe, no fallback dialog) | yes (llvmpipe) | **no GL at all** | **black** | WebGL GAL (D) |
| Menus / context menus | yes via monitor-crop (per-window capture *dismisses* a menu) | yes (captured at 0.3/1.5/4 s) | yes | yes (whole framebuffer) | yes | no (`-id` grabs one window) | DOM popups (D) |
| Tooltips | **never observed** under injected hover | **never captured** (layered window missed by polling) | yes | untested | yes | no | D |
| Modal dialogs | yes, per-HWND composited at real offset (56-440 ms to appear) | only if inside the shared rect; off-rect dialog invisible while pcbnew is modal-blocked | yes, separate seamless windows with modality | inside the fixed framebuffer | yes (second in-page window) | no | DOM overlays (D) |
| Footprint chooser | yes, composited incl. its GL preview | not opened (would depend on remembered position) | yes, 15 460 items, GL preview renders, typing filters | untested | crashed broadwayd before showing | no | D |
| **Input: mouse** | move/click/drag/middle-drag pan/wheel/right-click all work **when window on-screen, unobscured, foreground**; clicks otherwise land in the covering window | all work; input goes to the host foreground window | all work | all work | click/wheel/box-select ok; **pan 1.5 fps** | 1 fps | native DOM (D) |
| Input: keys R, Delete, Ctrl+Z, Esc, letters, text | all verified (R rotate, Delete, Ctrl+Z, Esc, A, E, Home, text entry, Ctrl+C) with SendInput; PostMessage: hotkeys/text yes, **click select fails, Esc on dialog fails** | R (90°→180° verified), Ctrl+Z, Delete, Esc, Home, Ctrl+A/C verified in Chromium; Esc on a freshly opened dialog needed a click first (focus) | R, Ctrl+Z, E, A, Esc, Home, Ctrl+A/C/V, typing verified | Home verified, others delivered by noVNC | R, Ctrl+Z, E, Esc, A ok | untested | D |
| Input inside VS Code | not run inside VS Code (browser client only); shell contract identical | wheel zoom (71 %) and middle-drag pan (99 %) verified; F1/Ctrl+P/Ctrl+Shift+P/Alt taken by VS Code in that run; letters/Delete/Ctrl+Z/Esc inconclusive (desktop contention) — see §5 | not run inside VS Code (iframe shape measured generically in vscode-shell run C) | same | — | — | — |
| **Latency** (input → pixels) | wheel median **52 ms** (34-66), key R **54 ms**; split 1-2 ms ws, 20-29 ms KiCad+DWM+WGC, ~24 ms encode/ws/decode/paint | key → first framebuffer update median **27 ms** (OpenGL) / 17 ms (Cairo), **p90 230-310 ms** (30 ms screen polling); click 3-25 ms, wheel 2-3 ms | wheel **18-52 ms** (29/52/34/18/36/33), key R **68-111 ms** | wheel 6-111 ms, Home 58 ms | wheel 53-65 ms, key 75-91 ms | wheel 92-853 ms | none (in-process, D) |
| **Frame rate** (middle-drag pan) | **24 fps** sustained (30 fps cap, nothing dropped); continuous zoom 15.6 fps = one frame per KiCad zoom step; idle **0 fps / 0 bytes** | **39-45 fps**, 42 fps under 7 s pan+zoom | **57 fps** (452-463 frames / 8 s ×3) | 29 fps | ~1.5 fps | 1 fps | D |
| **HiDPI** (host 150 %) | physical px, PMv2-aware helper, **0 px mapping error** at 4 probe points; crisp at 1:1 | framebuffer = physical px of the 150 % UI; `?fit=dpr` gives crisp 1:1 in a DPR-1.5 webview; no DPI negotiation, no `SetDesktopSize` | html5 client draws CSS px: at DPR 2 the 96-dpi render is upscaled (blurry); needs `--dpi=192` + `GDK_SCALE=2` (**untested**) | fixed framebuffer | moot | 3828x2127 window, tiny UI | native DPR (D) |
| **Clipboard** | host clipboard shared automatically (Ctrl+C → 877 B `(kicad_pcb …)`); remote would need a relay | both directions (155 kB s-expr KiCad → browser; browser → host via ClientCutText), text only | **both directions verified**, text | noVNC panel only, untested | KiCad-internal only, no bridge | untested | browser clipboard (D) |
| **CPU / GPU** (pan) | server **16 %** of one core (JPEG q60 1080p), pcbnew 6 %; idle 2-5 % / 0.5-2 %; GPU system-wide 24→34 % | tvnserver **25 %**, pcbnew 10 %, Chromium 11 %; idle <3 %; no attributable GPU | **pcbnew 410-700 %** (llvmpipe), xpra 36-56 %, Xvfb 7-11 %; VirtualGL offload impossible under WSL2 | x11vnc 8 %, Xvfb 6 % (+ pcbnew llvmpipe) | n/a | n/a | WASM CPU + WebGL (D) |
| **Multi-window UX** | every top-level HWND its own stream, composited to a union rect; minimised/fully off-screen → no frames | screen-rect crop: overlapping windows leak in and receive input; Footprint Editor (second top-level) ignored | best: floating, movable, modal-aware windows inside the panel; reconnect restores state | single framebuffer | second in-page windows | — | DOM (D) |
| **Platforms** | Windows 10 1903+/11 only; nothing runs in WSL | Windows only (`-sharewindow` is TightVNC-Windows) | Linux host/container/WSL2; Linux KiCad only | same | Linux only | WSL2 only | anywhere incl. vscode.dev, needs `--enable-coi` (D) |
| **Licensing** | windows-capture MIT, simplejpeg MIT (libjpeg-turbo), aiohttp Apache-2.0; a product helper in C#/Rust would be MIT; WGC per-window needs no consent picker | TightVNC server **GPL-2.0** (separate process; commercial licence sold), noVNC MPL-2.0, websockify LGPL-3.0 (replaceable by a 50-line MIT ws bridge) | xpra **GPL-2.0-or-later** (separate spawned process), xpra-html5 MPL-2.0, Xvfb/Mesa MIT | x11vnc GPL-2.0, noVNC MPL-2.0, websockify LGPL-3.0 | GTK LGPL-2.1 | — | **GPL-3.0 bundle** + LGPL-2 wx files; separate download, own webview (D8 pattern) |
| **Effort to productize** | capture-only panel + `kw_live screenshot`: ~1.5-2 weeks; interactive mirror helper (C#/.NET 8 or Rust): **3-4 weeks**, and still "mirrored", not "inside" | 2-3 weeks Windows-only "live tab"; cannot fix tooltips, off-rect dialogs, DPI negotiation, desktop dependence | 1-2 weeks Linux/remote + 1 week if the extension bootstraps WSL (apt, PPA, xpra.org, ~1.5 GB) | similar plumbing, strictly less capable | not worth it (patched wx + patched KiCad + Cairo-only, unstable daemon) | n/a | high: Docker build 1-2 h/16 GB, COI, FS bridge, library provisioning, tracking a fork |
| Screenshots | [winstream/01-10](research/display/winstream/) | [img/vnc-novnc-01-09](research/display/img/) | [wsl-linux/06-09](research/display/wsl-linux/) | [wsl-linux/11](research/display/wsl-linux/11-x11vnc-xvfb-novnc.png) | [wsl-linux/01-05](research/display/wsl-linux/) | [wsl-linux/10](research/display/wsl-linux/10-x11vnc-on-wslg-window-black-canvas.png) | none |

Routes measured in the 2026-09-20 follow-up and tabulated in §12 rather than here: TightVNC
`-shareapp <pid>` (§12.3, partial), WGC per-editor surfaces standalone and inside VS Code tabs (§12.3,
works), the hybrid injector on stock KiCad (§12.4, partial), and the patched KiCad driven with posted
messages while covered, cloaked or off-screen (§12.5, works / works / partial).

Routes catalogued in the survey but **not spiked**: KasmVNC / Selkies / n.eko (WebRTC upgrade of the
VNC class, H.264/AV1 with NVENC), Guacamole / Myrtille / Weston-RDP (session model blocked on Windows
Pro, no advantage on Linux), Sunshine + virtual display driver + moonlight-web (KiCad on an invisible
monitor that is still part of the desktop), custom WGC + NVENC + WebRTC (superset of winstream's
display half). See survey §2.4-2.5, §2.3.

### 4.1 VS Code shell criteria (backend-independent, vscode-shell runs A-E)

| Criterion | Inline `<canvas>` + WebSocket (our backend) | `<iframe http://localhost>` (noVNC / xpra-html5 unchanged) |
|---|---|---|
| Chords reaching the page | 214-215 / 215 as DOM keydown/keyup with correct key/code/modifiers | 214 / 215 |
| VS Code also acting (stock) | **54 chords**: Ctrl+W / Ctrl+F4 **dispose the panel**, Ctrl+P / Ctrl+E / Ctrl+Shift+P / F1 (palette, quick open), Ctrl+B/J/G, Ctrl+Shift+E/F/G/D/X/M/H/I/U/O/Y/A/S, Ctrl+Tab / Ctrl+Shift+Tab, Ctrl+0/2/3, Ctrl+, Ctrl+\ Ctrl+`, Ctrl+K chords, Ctrl+N/O/Q, F5/F6/Ctrl+F5, Alt+F/E/V/T/R/S/H (menu mnemonics), Alt+1/2/3, Alt+Left, Alt+Space, Alt, Apps, Ctrl+Shift+N | **0 keybindings resolved**, no palette, no Alt menus, no Ctrl+W close: total capture while the frame has focus; VS Code chords are dead |
| With the rescue keybindings (`when: activeWebviewPanelId == … \|\| activeCustomEditorId == …`) | 85 chords swallowed; only Alt, Alt+menu-mnemonic, Alt+Space, Apps remain (settings `window.customMenuBarAltFocus=false`, `window.enableMenuBarMnemonics=false`) | not needed |
| preventDefault by VS Code's pre-script (listener still fires) | Ctrl+Z/Y, Ctrl+Shift+Z, Ctrl+X/C/V, Ctrl+S, Ctrl+F, Ctrl+P, Ctrl+Shift+P/F/X/C/V/S/Y, Ctrl+K Ctrl+S, Ctrl+Alt+F/P | n/a |
| Mouse | left/middle/right drags (buttons bitmask), dblclick, auxclick, wheel ±100 with Ctrl/Shift/Alt, pointer lock with `unadjustedMovement` | same, inside the frame |
| Right-click | VS Code overlays Cut/Copy/Paste unless `data-vscode-context='{"preventDefaultContextMenuItems": true}'` (verified) | VS Code never sees it |
| Clipboard | `navigator.clipboard.writeText/readText` granted when focused; Ctrl+V delivers a DOM paste event | **denied** even in a gesture with the `allow` attribute; no paste event; must relay via postMessage or the display protocol (VNC/xpra carry it) |
| Focus | `panel.reveal()`, `focusActiveEditorGroup` focus the page | they focus the webview **body**, not the frame; host page must push focus into the frame (fixed in `media/view.html`) |
| DPI | `devicePixelRatio` 1.5 at 150 %, 1.8 after one zoomIn; `resize` fires on zoom/split/zen (1582x1370 CSS px)/fullscreen (2212x1348) | same, visible inside the frame |
| Lifecycle | `retainContextWhenHidden: true` keeps the page alive across hide/reveal; `false` re-creates it (stream must reconnect) | same |
| Fullscreen | element `requestFullscreen` disallowed by permissions policy; workbench zen/fullscreen work | same |
| Shell floor | input RTT median 0.5 ms idle / 8-18 ms under load; stamp-to-paint 18-19 ms (1056x852) / 36-39 ms (1920x1200) at 22 fps synthetic RGBA; `putImageData` 0.1-0.7 ms | no measurable addition |
| Screenshots | [vscode-shell-A/B/D/E](research/display/img/) | [vscode-shell-C-*](research/display/img/) |

---

## 5. Exact shortcut findings and the one conflict between spikes

- **KiCad's own hotkeys are never a problem for VS Code.** Letters (R, M, X, V, E, D, F, B, G, A, …),
  Shift+letter, Delete, Backspace, Esc, Tab, Space, Enter, Insert, Home/End, PgUp/PgDn, arrows,
  digits, Ctrl+Z/Y/X/C/V/S/A/F, Ctrl+Shift+Z, F2-F4/F7-F12 all reach the page in every shape and
  trigger nothing in VS Code (vscode-shell-keys.md, runs A/B/C).
- **What VS Code takes by default while a webview page is focused** is the 54-chord list in §4.1; the
  ones that hurt a KiCad user are Ctrl+W (closes the KiCad view), Ctrl+P / Ctrl+E / Ctrl+Shift+P / F1,
  Ctrl+B / Ctrl+J, Ctrl+Tab, Ctrl+= / Ctrl+- (window zoom), F5, F11, Alt+letters. All but the Alt cases
  are ordinary keybindings and were verified silenced by the generated rescue block; the Alt cases are
  two user settings.
- **What the cross-origin iframe shape does:** VS Code resolves nothing at all (0/215), which is both a
  feature (nothing to shield) and a cost (Ctrl+Shift+P is dead while KiCad has focus).
- **The conflict:** vnc-novnc §9 reports F1, Ctrl+P, Ctrl+Shift+P and Alt being consumed by VS Code
  *with the KiCad iframe focused*, while vscode-shell run C reports 0 VS Code actions with an iframe
  focused. The most likely explanation is the focus finding from vscode-shell: `panel.reveal()` focuses
  the webview body, not the frame, and the vscode-novnc prototype has no focus push, so those chords hit
  the host page (where VS Code does act). Treat this as **to be confirmed** by the 10-minute re-run of
  `vscode_keys2.ps1` on a quiet desktop that vnc-novnc asks for; the shell-level behaviour is settled.
- **Injection-side caveats on Windows** (independent of VS Code): a KiCad dialog opened without focus
  swallows keys until clicked (vnc-novnc §3); the ALT-tap foreground trick puts wx's menu bar into
  keyboard mode (winstream §3.2); every injected mouse move moves the user's real cursor with the
  `SendInput` injector (the hybrid injector only borrows it for the instant of a click, and the events
  injector on the patched build never touches it, §12.4/§12.5).

---

## 6. Screenshots (all small PNGs in the repo)

Windows, WGC helper: [01 4K frame](research/display/winstream/01-wgc-4k-maximised-frame.png) ·
[02 first 1080p frame](research/display/winstream/02-viewer-first-frame-1080p.png) ·
[03 after injected zoom](research/display/winstream/03-viewer-after-injected-zoom.png) ·
[04 select / R / Delete / Ctrl+Z](research/display/winstream/04-select-rotate-delete-undo.png) ·
[05 context menu composited](research/display/winstream/05-context-menu-composited.png) ·
[06 owned dialog](research/display/winstream/06-owned-dialog-composited.png) ·
[07 footprint chooser](research/display/winstream/07-footprint-chooser-composited.png) ·
[08 PostMessage doubled chars](research/display/winstream/08-postmessage-typed-doubled.png) ·
[09 10.99 nightly](research/display/winstream/09-nightly-1099.png) ·
[10 modal warning](research/display/winstream/10-modal-file-open-warning.png).

Windows, TightVNC + noVNC: [01 host reference](research/display/img/vnc-novnc-01-host-pcbnew.png) ·
[02 OpenGL framebuffer](research/display/img/vnc-novnc-02-novnc-framebuffer-opengl.png) ·
[03 popups host vs framebuffer (tooltip missing)](research/display/img/vnc-novnc-03-popups-host-vs-framebuffer.png) ·
[04 Board Setup dialog](research/display/img/vnc-novnc-04-board-setup-dialog-host-vs-framebuffer.png) ·
[05 Cairo](research/display/img/vnc-novnc-05-novnc-framebuffer-cairo.png) ·
[06 rotate before/after](research/display/img/vnc-novnc-06-rotate-before-after.png) ·
[07 inside VS Code webview](research/display/img/vnc-novnc-07-vscode-webview-kicad.png) ·
[08 VS Code intercepting keys](research/display/img/vnc-novnc-08-vscode-keys-intercepted.png) ·
[09 wheel/pan reach KiCad from VS Code](research/display/img/vnc-novnc-09-vscode-wheel-pan-reach-kicad.png).

Linux/WSL: [00 WSLg pcbnew on the Windows desktop](research/display/wsl-linux/00-wslg-pcbnew-on-windows-desktop.png) ·
Broadway [01](research/display/wsl-linux/01-broadway-gtk-widget-factory-in-chromium.png) [02 wxGLCanvas error](research/display/wsl-linux/02-broadway-pcbnew-wxglcanvas-error.png) [03 Cairo full UI](research/display/wsl-linux/03-broadway-pcbnew-cairo-full-ui.png) [04](research/display/wsl-linux/04-broadway-menu-context-tooltip-zoom.png) [05](research/display/wsl-linux/05-broadway-pan-rotate-dialog-chooser-nag.png) ·
xpra [06 OpenGL GAL](research/display/wsl-linux/06-xpra-html5-pcbnew-opengl-gal.png) [07 menu/context/tooltip/pan](research/display/wsl-linux/07-xpra-menu-context-tooltip-pan.png) [08 rotate/properties/chooser](research/display/wsl-linux/08-xpra-rotate-properties-dialog-footprint-chooser.png) [09 clipboard/reconnect/HiDPI](research/display/wsl-linux/09-xpra-clipboard-reconnect-hidpi.png) ·
[10 x11vnc on WSLg black canvas](research/display/wsl-linux/10-x11vnc-on-wslg-window-black-canvas.png) ·
[11 Xvfb + x11vnc + noVNC](research/display/wsl-linux/11-x11vnc-xvfb-novnc.png).

VS Code shell: [A Ctrl+Shift+P rescued](research/display/img/vscode-shell-A-ctrl-shift-p-rescued.png) ·
[B palette opens (stock)](research/display/img/vscode-shell-B-ctrl-shift-p-palette.png) ·
[A Alt+F menu](research/display/img/vscode-shell-A-alt-f-menu.png) ·
[A default right-click overlay](research/display/img/vscode-shell-A-rightclick-default-menu.png) ·
[E overlay suppressed](research/display/img/vscode-shell-E-rightclick-suppressed.png) ·
[A pointer lock](research/display/img/vscode-shell-A-pointerlock.png) ·
[A custom editor on a .kicad_pcb](research/display/img/vscode-shell-A-custom-editor-pcb.png) ·
[C iframe Ctrl+Shift+P (nothing)](research/display/img/vscode-shell-C-iframe-ctrl-shift-p.png) ·
[C split](research/display/img/vscode-shell-C-split.png) · [C zen mode](research/display/img/vscode-shell-C-zen-mode.png) ·
[C custom editor split](research/display/img/vscode-shell-C-custom-editor-split.png) ·
[D canvas stream](research/display/img/vscode-shell-D-canvas-stream.png).

Follow-up 2026-09-20, multi-editor (win-multi): [01 host: manager, sch, pcb, 3D](research/display/img/win-multi-01-host-monitor2-manager-sch-pcb-3d.png) ·
[02 `-shareapp` framebuffer](research/display/img/win-multi-02-tightvnc-shareapp-framebuffer.png) ·
[03 foreign window = black hole](research/display/img/win-multi-03-tightvnc-shareapp-foreign-window-black-and-overlap.png) ·
[04 `-shareapp` chooser](research/display/img/win-multi-04-tightvnc-shareapp-chooser.png) ·
[05 WGC per-surface clients](research/display/img/win-multi-05-wgc-per-surface-clients.png) ·
[06 VS Code pcb + sch tabs](research/display/img/win-multi-06-vscode-pcb-and-sch-tabs.png) ·
[07 + 3D panel](research/display/img/win-multi-07-vscode-pcb-sch-3d-panel.png) ·
hybrid injector [01 select/R/Delete/undo with Notepad foreground](research/display/img/win-input-01-hybrid-select-rotate-delete-undo-notepad-foreground.png) ·
[02 dialog composited](research/display/img/win-input-02-hybrid-dialog-composited.png) ·
[03 real click into the pcb tab](research/display/img/win-input-03-vscode-click-in-pcb-tab.png) ·
[04 context menu needs the foreground](research/display/img/win-input-04-hybrid-context-menu-needs-foreground.png).

Follow-up 2026-09-20, patched KiCad (win-input-patch): [edit matrix, covered window, posted input](research/display/img/win-input-patch-edit-matrix.png) ·
[properties dialog typed + chooser filtered](research/display/img/win-input-patch-dialog-and-chooser.png) ·
[cloaked: desktop pixels vs WGC frame](research/display/img/win-input-patch-cloaked-desktop-vs-wgc.png) ·
[cloaked dialog + browser client HUD](research/display/img/win-input-patch-cloaked-dialog-and-client.png).

---

## 7. Recommended architecture for the kiwrap VS Code extension

### 7.1 Shape: one shell, pluggable display providers

```
VS Code extension (MIT, TypeScript)
  CustomReadonlyEditorProvider  *.kicad_pcb / *.kicad_sch   priority "option"  ("Open with KiCad (live)")
  WebviewPanel "Live KiCad"     project-level, retainContextWhenHidden: true
  contributes.keybindings       generated rescue block, when: activeWebviewPanelId == kiwrap.live
                                || activeCustomEditorId == kiwrap.liveEditor
  one page: inline <canvas> (WebGL/putImageData) + WebSocket client; data-vscode-context
            preventDefaultContextMenuItems; requests clientWidth*devicePixelRatio; resync on resize
  ──────────────── display-session contract (§4 of vscode-shell.md) ────────────────
  provider "win-wgc"   (Windows, primary)   helper process: WGC per-window capture grouped into
                                            *surfaces* (root frame + GW_OWNER chain; /ws?surface=pcb),
                                            JPEG/H.264, SetWinEventHook window tracking, dialog
                                            gathering, loopback ws + per-session token, PMv2 DPI;
                                            injector mode per session (§12.6): none (view) |
                                            hybrid (stock KiCad, opt-in mirror) | events (fork build
                                            with mr/display-input, KiCad cloaked)
  provider "xpra"      (Linux/WSL/remote)   xpra seamless per user + xpra-html5 in the same page
                                            via the iframe shape (or its protocol on the canvas)
  provider "vnc"       (fallback/escape)    any RFB server (TightVNC -shareapp / x11vnc) + noVNC
```

The KiCad process under every provider is launched by kiwrap's existing rules: `kiwrap.sandbox`
(`ConfigRoot.env()`, `enable_api_server=True` for live sessions), one GUI at a time, short `TEMP`,
tracked and killed by PID (WER restarts are different processes), scratch-free user config.

### 7.2 Windows (D18's target): what to build and in which order (updated 2026-09-20, see §12.6)

1. **Capture-only Live KiCad view** (the part every spike agrees is cheap and robust): the `win-wgc`
   helper streams the sandboxed KiCad windows — each editor frame of a project session (`kicad.exe`
   is one pid; the frames are unowned siblings, §12.2) as its own *surface* with its owned dialogs and
   the footprint chooser composited at their offsets, the chooser gathered onto its frame; menus via
   the monitor-crop path — into the `.kicad_pcb` / `.kicad_sch` custom editors and a 3D panel (the
   shape measured in §12.3). 0 bytes when idle. **No input injection**: a click on the view raises the
   real KiCad window (`SetForegroundWindow` from a user gesture is allowed), keys are not forwarded.
   This is ARCHITECTURE §9's "Live-KiCad panel" with a live picture instead of a launch button, and it
   never fights the user for focus.
2. **`kw_live screenshot`**: one WGC frame of the main window (<10 ms, GL canvas included) returned as
   an image ≤1024 px / <100 KB per the §9 image rule — "what the user sees", better than a kicad-cli
   render for that question, and still secondary to structured data (D17).
3. **Interactive input as a mode of the launched session**, replacing the earlier SendInput mirror:
   - **events + cloak** when the supervisor launched a fork build carrying `mr/display-input`
     (`kiwrap.patches` contains `display-input`): KiCad started with `KICAD_INPUT_FROM_EVENTS=cloak`
     and without activation, every wx top-level cloaked (invisible on the desktop, still captured),
     the helper posting window messages only (§12.5). This *is* "KiCad inside VS Code" on Windows,
     minus tooltips and minus popup menus/drop-downs, which are OS windows that still appear on the
     desktop until decision 11 (§12.8) is taken; only pcbnew is measured, eeschema untested.
   - **hybrid** on stock KiCad as an explicit opt-in ("KiCad mirrored in VS Code"): posted moves,
     borrowed cursor for clicks/wheel, posted keys, `Esc` as `WM_CLOSE` on owned dialogs, one
     foreground flip per right-click (§12.4). VS Code keeps the foreground and the cursor; the user
     must still be told that the KiCad window exists on the desktop, that `Home`/zoom-fit and
     `center_on_zoom` warp their cursor (the helper should restore it), and that a context menu flips
     focus once. A second monitor or reserved area remains the honest setup for this mode.
   - `SendInput` is not a product mode any more (the foreground/cursor contest measured in winstream
     and vnc-novnc was that injector's, not KiCad's).

The helper must not be Python (the prototype's venv + 60 MB OpenCV is not shippable): C#/.NET 8 with
the WinRT `Windows.Graphics.Capture` projection, D3D11 staging and libjpeg-turbo (or Media Foundation
H.264 for 4K), or Rust with the `windows` crate. Loopback only, random token per launch in the URL
and the ws handshake, CSP restricted to that origin. Dirty-rect streaming (`WGC` exposes dirty
regions) is the main optimisation for editing workloads where only the canvas changes. Two measured
requirements for the WebSocket to a VS Code webview: no permessage-deflate (Electron sends RSV1
frames; aiohttp closed with 1002) and a single writer per client (§12.3). The tracker must keep
cloaked windows (`DWMWA_CLOAKED` is set on them) and must key on HWND + owner chain, never on pid
alone (§12.2).

### 7.3 Linux, WSL and remote (informational under D18, kept behind the same contract)

xpra seamless + xpra-html5 is the provider: it is the only measured route that meets the "identical
UI/UX" bar completely (menus, tooltips, modal dialogs, chooser with GL preview, hotkeys, clipboard,
57 fps, reconnect). Provisioning is the cost (Linux KiCad + xpra.org packages, ~1.5 GB in WSL), plus
server-side DPI (`--dpi=192` + `GDK_SCALE=2`, untested) and GPU offload on real Linux hosts
(VirtualGL/EGL; impossible under WSL2, where llvmpipe costs 4-7 cores while panning). The xpra-html5
client goes in via the iframe shape first (clipboard is then carried by xpra's own protocol, which
sidesteps the iframe clipboard denial); `portMapping` / `asExternalUri` handle remote extension hosts
(the RFB/xpra WebSocket itself is not port-mapped, survey §1.1). A WSL-hosted KiCad must never be the
default for a Windows user: it is a second KiCad with its own config and libraries.

### 7.4 How the live view integrates with the MCP tools

- **Same process, two channels.** The GUI that the display provider captures is the same sandboxed
  `pcbnew.exe` that `kw_live` binds over IPC (`enable_api_server=True`, named pipe on Windows).
  An agent call such as `kw_pcb_place` with the live provider bound becomes one undoable IPC commit
  in that pcbnew; KiCad repaints; WGC delivers a frame *because the content changed* (idle = no
  frames), so the edit appears in the VS Code view within the measured ~20-30 ms KiCad→WGC path plus
  ~24 ms encode/decode — without polling and without the extension knowing what changed. The custom
  editor's title/status can show the `kw_live` session state (attached / detached / crashed) from the
  supervisor, and a `FileSystemWatcher` refreshes nothing — KiCad owns the file while a session is live
  (ARCHITECTURE §9: "warn when a GUI holds a file").
- **KiCad 11: `kicad-cli api-server` vs GUI-with-API.** The headless `api-server` (D16, primary
  backend for schematic and board) has **no window**, so there is nothing to display; its feedback is
  structured data (D17) and `kw_*_render` previews. A live *view* therefore always means the GUI with
  its API enabled: on 9/10 that is PCB-only, standalone `pcbnew.exe` (D2, #24966 until 10.0.7); on 11
  ARCHITECTURE §4 expects both editors — not re-verified in these spikes. The session supervisor
  should expose which one is bound so the panel can say "headless session — no live view; showing the
  kicad-cli preview" instead of an empty canvas. Switching a headless session to a GUI session is a
  save + relaunch under the same sandbox root, never two processes on one file.
- **Agent loop stays data-first.** The view is for the human (and for `kw_live screenshot` when a
  human asks "what does it look like"); the agent's own verification remains `kw_*_check` / `kw_*_get`.

### 7.5 Shell details to adopt verbatim from the prototypes

`prototypes/display/vscode-shell`: generated keybinding block (`tools/gen_keybindings.js`), focus
hand-off, DPI/resize handling, the binary frame protocol in `src/extension.ts` + `media/canvas.html`
(`type=1` frame header `seq | t_server_ms | w | h | fmt | pixels`; add `fmt 1` JPEG tiles, `fmt 2`
H.264/AV1 for WebCodecs, `type=2` dirty rects, `type=3` cursor), the input JSON (`keydown/keyup/
pointerdown/pointerup/pointermove/wheel` with `code`, device-pixel coordinates, modifier flags) and the
echo probe for a latency HUD. `prototypes/display/winstream`: window enumeration per PID, DWM extended
frame bounds, PMv2 awareness (`DPI_AWARENESS_CONTEXT` passed as `c_void_p(-4)`), menu-crop tracker,
scancode mapping, drop-to-latest per client. Playwright drivers in both prototypes become the
regression tests for the display path (`@vscode/test-electron` for the shell).

---

## 8. Risks and honest limits

1. **"Identical UI/UX inside VS Code" on Windows is not reachable with stock KiCad** — still true, but
   the wall is lower than the first spikes suggested. VNC input targets the host foreground window and
   the shared rect leaks other windows (vnc-novnc); per-window capture is clean but `SendInput` needs
   the window visible/unobscured/foreground and plain `PostMessage` is defeated by KiCad reading the
   hardware (winstream). *Updated:* the hybrid injector (§12.4) keeps VS Code the foreground owner and
   restores the user's cursor around each click, so on stock KiCad the mirror is no longer a
   focus-flapping feature; what remains is KiCad's own cursor warps, one foreground flip per context
   menu, missing tooltips and the window's presence on the desktop. With the fork patch (§12.5) the
   window is cloaked and none of the contest remains.
2. **The KiCad-side patch exists** (`mr/display-input`, 25 files +534 −48 on `base/6dd893f7d1`,
   private fork only) and was measured to remove the wall for pcbnew (§12.5). The risks moved: it
   touches hot files (`tool_dispatcher.cpp`, `wx_view_controls.cpp`, `pcb_selection_tool.cpp`) that
   every base bump rebases; it only helps builds that carry it (stock users keep mirror semantics;
   patched builds are *development* installs under UPSTREAM §3.3 until the MR lands, and D19 holds the
   MR); the state lives in `wxTheApp`'s client object because kiplatform is a static lib per module;
   `wxGetMousePosition()` calls inside the wx *library* (e.g. `PopupMenu` with a default position)
   were not swept; eeschema, the symbol/footprint editors, Alt combinations and the auto-pan focus
   path are **untested**; hardware keys while the mode is on reach `wxEVT_CHAR_HOOK` twice (guard
   needed before any MR).
3. **Tooltips** never appeared under injected hover in any Windows route; root cause (win-input-patch
   §5): the comctl32 tooltip control polls the real cursor, so no injected or posted input shows one.
   A polling VNC server also never captured the layered tooltip window. Only xpra showed them. Related
   and new: **popup menus and combobox drop-downs are OS windows**, not wx top-levels, so a cloaked
   KiCad still shows its context menu on the user's desktop (decision 11, §12.8).
4. **Latency class.** Windows helper 40-55 ms input-to-pixel at 24-30 fps; TightVNC median 17-27 ms
   but p90 230-310 ms from polling; xpra 18-52 ms at 57 fps. All are usable for placement/routing but
   none is native (native KiCad is one frame, ~16 ms). Continuous zoom is KiCad-bound (one frame per
   zoom step, 15.6 fps).
5. **HiDPI.** Windows routes deliver the host's physical pixels of a 150 % UI; a remote client at
   another scale gets the wrong size and there is no negotiation. xpra needs server DPI set per client.
6. **Desktop contention corrupted measurements** three times (foreground theft, an externally held
   Shift, one run that typed into another agent's pcbnew and opened "Save Board File As" there). The
   VS Code key matrix for the VNC path is inconclusive; future GUI spikes must be serialised.
7. **Licensing.** The capture path can be MIT end-to-end (WGC helper, noVNC/xpra-html5 are MPL-2.0).
   TightVNC and xpra are GPL-2.0 *separate processes* — acceptable under D8's Freerouting pattern
   (downloaded at bootstrap, never linked) but shipping a GPL binary in the VSIX means a source offer.
   PCBJam would be a GPL-3 bundle in its own webview.
8. **Platform coverage is asymmetric.** The only complete route (xpra) is Linux-only; the Windows
   route (D18's target) is the one with the input wall. macOS has no measured route at all.
9. **KiCad process ownership.** A live view multiplies the ways a wrong KiCad gets targeted (the
   user's own KiCad, a sibling agent's, a WER-restarted one): the provider must key on the PID the
   supervisor launched, never on titles (`-sharewindow "PCB Editor"` matched another agent's WSLg
   window during the spike) — and, within that PID, on the top-level HWND and its owner chain, because
   a project session is one PID for every editor (§12.2).
10. **Untested items that could change the picture:** WGC of a window on another Windows virtual
    desktop; KasmVNC/Selkies H.264 for the codec cost; xpra at `--dpi=192`; PCBJam loading in a webview
    with/without `--enable-coi`; VirtualGL on a real Linux host; the 10.99 GUI's IPC in a live session;
    the 10.99 project-session window model; the patched mode on eeschema and in a project session;
    in-process cloaking of popup menus. Measured and closed since 2026-09-19: TightVNC `-shareapp`
    (§12.3); the IddCx virtual display driver idea is superseded by `DWMWA_CLOAK` under the patch
    (it would still have the `SendInput` contest on stock builds).
11. **Hidden mode limits that stay after the patch** (§12.5): KiCad takes the foreground once at
    launch (start without activation); a dialog opened in the background can behave differently from
    one opened in the foreground (Footprint Properties' fields grid consumes `Esc` when its editor has
    focus: click Cancel/OK); a fully off-screen window is not composed by DWM, so capture needs a
    covered or cloaked on-screen placement.

---

## 9. Decisions the owner must take

Rows 1, 3, 5 and 6 were superseded on 2026-09-20 by §12.8 (which also adds rows 11-14); they are kept
here struck through so the history of the options stays readable.

| # | Decision | Options (recommendation in bold) | What it changes |
|---|---|---|---|
| ~~1~~ | ~~What does "Live KiCad in VS Code" mean on Windows?~~ → §12.8 row 1 | ~~**(a) capture-only live view + click-to-raise + `kw_live screenshot`**; (b) interactive mirror (window on a second monitor/area, SendInput, focus flapping); (c) both, mirror opt-in~~ | ~~(a) ~1.5-2 weeks, no UX caveats; (b) adds 3-4 weeks and a settings/consent surface; (c) is the §7 plan~~ |
| 2 | Accept remote-display latency and frame rate? | **yes for a view/mirror** (40-70 ms, 24-30 fps Windows incl. the multi-editor and patched runs; 18-52 ms, 57 fps xpra); no → only kicad-cli previews and the real KiCad window | if no, §7.2 items 1-2 still stand (a picture, not a control surface) |
| ~~3~~ | ~~Ship a GPL VNC server or build the MIT WGC helper?~~ → settled, §12.8 row 3 | ~~**build the helper (C#/.NET 8 or Rust)**; TightVNC only as an unsupported escape hatch behind a setting~~ | ~~helper = 2-3 weeks of native code and a second language in the repo; TightVNC = registry-based config, polling CPU, tooltips/off-rect dialogs lost~~ |
| 4 | Fund the Linux/WSL xpra provider now? | **no under D18 — keep the prototype and docs**; yes → 1-2 weeks + 1 week WSL bootstrap, plus llvmpipe/GPU caveats | if yes, the display-session contract gets its second provider and remote SSH/containers come for free |
| ~~5~~ | ~~Patch KiCad in the fork for hidden-window input (event-coordinate mode + dialog Esc)?~~ → done as a spike, §12.8 row 5 | ~~**defer; open a design note in UPSTREAM.md**; yes → 1-2 weeks spike on the source build (D1/D17)~~ | ~~only path to "KiCad invisible, VS Code is the UI" on Windows; stock users keep mirror semantics~~ |
| ~~6~~ | ~~Spend 2-3 days on the untested Windows "invisible monitor" idea (IddCx virtual display driver, WGC on another virtual desktop)?~~ → drop, §12.8 row 6 | ~~**one time-boxed spike after decision 1**~~ | ~~could turn the mirror into a hidden-KiCad view without a KiCad patch; needs an admin-installed driver and still has the SendInput foreground contest~~ |
| 7 | Spend 2-3 days on the PCBJam (KiCad-in-WASM) feasibility check? | **defer until PCBJam ships self-hosting**; the survey's questions (loads in a webview with/without `--enable-coi`, WebGL GAL, bundle size) are still open | the only route that would run on vscode.dev; breaks the installed-binaries premise (D3/D16) |
| 8 | Keyboard policy while the KiCad view is focused | **"KiCad mode": rescue block silences the 54 chords, one consented workspace change for the two Alt settings; Ctrl+Shift+P optionally kept for VS Code** | all-or-nothing (iframe shape) vs selective (canvas shape); selective needs our own client |
| 9 | Surface defaults | **custom editor `priority: "option"` + "Open with KiCad (live)" + project-level panel; text editor stays default** | `priority: "default"` would hijack `.kicad_pcb` opens |
| 10 | Where the helper's screenshots go for the agent | **`kw_live screenshot` returns ≤1024 px / <100 KB images; never automatic in the agent loop (D17)** | keeps the data-first rule; humans get the picture |

---

## 10. Phased plan (effort assumes one engineer, the prototypes as starting points; updated 2026-09-20, §12.7)

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **D0 Close the open measurements** | 0.25 | Quiet-desktop re-run of the VNC key matrix (`vscode_keys2.ps1`) to settle the §5 conflict; WGC of a window on another virtual desktop; the 10.99 project-session window model; write results into the spike reports. Done since 2026-09-19: `-shareapp` (§12.3), tooltip root cause (comctl32 polls the real cursor) | every "inconclusive/untested" item in §4 that is cheap has a verdict |
| **D1 Capture-only Live KiCad (Windows)** | 2-2.5 | C#/.NET 8 (or Rust) helper: WGC per-window grouped into surfaces by owner chain (§12.2), owned dialogs composited, chooser gathering, menu-crop, libjpeg-turbo, dirty rects, `SetWinEventHook` tracking, loopback ws + token (no permessage-deflate, one writer per client), PMv2, cloaked windows kept; shell from `vscode-shell` (custom editor `option` per `.kicad_pcb`/`.kicad_sch` mapped to its surface, 3D `WebviewPanel`, rescue keybindings, context-menu opt-out, DPI/resize, reconnect); launched by the extension under the kiwrap sandbox with kill-by-PID; click-to-raise; `kw_live screenshot` | opening a project's board and schematic "with KiCad (live)" shows both sandboxed editors in their tabs including a modal dialog and the chooser in the right tab; idle 0 bytes; 1080p pan ≥ 24 fps per tab with three streams live at ≤ 45 % of one core in the helper (measured 36-43 % in Python); `%APPDATA%\kicad` hash unchanged in CI on the self-hosted runner |
| **D2 MCP integration** | 1-1.5 | `kw_live` session state in the view's title/status; agent `kw_pcb_place` / `push` visible in the view without polling; headless-session fallback message + kicad-cli preview; supervisor-owned PID + HWND/owner chain is the only capture target; @vscode/test-electron + Playwright regression of the display path | an agent edit through `kw_live` appears in the view within 100 ms of the commit; a WER-restarted or foreign pcbnew is never captured |
| **D6 Hidden UI on patched builds (new; needs decision 5 = carry)** | 0.5 | Port `EventInjector` (hit-test in z-order, disabled-window rule, double-click synthesis, `WM_MOUSELEAVE` hand-over, keys to the thread focus window, Alt) into the helper as the `events` mode; launcher sets `KICAD_INPUT_FROM_EVENTS=cloak` in the sandbox env and starts KiCad without activation; mode selected from `kiwrap.patches`; menu handling per decision 11 (monitor crop first) | the win-input-patch §5 matrix (select, R, Delete, Ctrl+Z, drag, wheel, pan, double-click, dialog typed + OK, chooser, context menu) passes from inside the VS Code tab with KiCad cloaked, `GetCursorPos` and `GetForegroundWindow` unchanged throughout |
| **D3 Interactive mirror on stock KiCad (opt-in, hybrid injector)** | 1-1.5 | Hybrid injector in the helper (posted moves, borrowed cursor with `SendMessageTimeout`, posted keys, `Esc` → `WM_CLOSE` on owned dialogs, one foreground flip for right-click), cursor restore after KiCad's own warps, key release on blur, scancode mapping, modifier guarding; consent UI and the two Alt settings; H.264 via Media Foundation for 4K if JPEG cost is too high | the win-multi §4 hybrid matrix passes from inside the VS Code view on a quiet desktop with VS Code the foreground before and after; documented as "mirrored", with the cursor-warp / right-click / on-desktop caveats in the UI |
| **D4 Linux/WSL/remote provider (deferred under D18)** | 1-2 (+1 WSL bootstrap) | `xpra` provider: per-user session (unix socket or `--bind-tcp=127.0.0.1` + token, `--html=on`), sandbox env forwarded, DPI (`--dpi=192`, `GDK_SCALE=2`), keyboard layout, dispose lifecycle; iframe shape first; VirtualGL/EGL on real hosts | the wsl-linux battery (menus, tooltips, chooser, hotkeys, clipboard, reconnect) passes from a VS Code Remote-WSL window |
| **D5 KiCad-side follow-ups on `mr/display-input`** | 0.5-2 | eeschema / symbol / footprint editor / project-session matrix in the patched mode (0.5); hardware-key `wxEVT_CHAR_HOOK` guard, rebase on `upstream/master`, `clang-format`, QA binaries before any MR (0.5, MR itself on hold under D19); optional in-process cloaking of popup menus via a `WH_CBT` / `EVENT_OBJECT_SHOW` hook (0.5-1, untested) | a written verdict per item in `research/display/`; the branch rebases cleanly on the next base bump |
| **D7 Research options (each time-boxed, only on a decision)** | 0.5-2 each | PCBJam feasibility (decision 7); KasmVNC/Selkies codec check | a written verdict per item in `research/display/` |

Totals: the recommended path A + C (D0 + D1 + D2 + D6) is **4-5 weeks**; the stock-KiCad hybrid
mirror (D3) adds **1-1.5 weeks** (was 3-4 with SendInput); the KiCad-side follow-ups 0.5-2 weeks; the
Linux provider **2-3 weeks** if ever funded.

---

## 11. What this changes in the existing plan

- ARCHITECTURE §9 "Live-KiCad panel (launch sandboxed `pcbnew.exe`/`kicad.exe`, detect crashes,
  relaunch, warn when a GUI holds a file)" becomes a *display session* with a live capture view and
  provider abstraction (§7.1); the launch/crash/lock semantics are unchanged.
- D17 unchanged: the view and `kw_live screenshot` are human-facing; the agent loop stays structured.
- D18 unchanged: Windows-native is the recommendation; xpra stays informational with a working
  prototype and a measured verdict, so re-opening Linux later costs nothing in research.
- D8 unchanged: MIT helper preferred; any GPL server (TightVNC, xpra) is a separate process
  downloaded at bootstrap, never linked; PCBJam (if ever) is a GPL bundle in its own webview.
- Roadmap: Phase 3 "Live-KiCad panel" absorbs D1/D2 (3-4 weeks) plus D6 (0.5 week) when the fork
  build is the bench install; D3/D4/D5/D7 are optional rows.
- UPSTREAM.md: new gap card **G15** (display input mode, `mr/display-input`) in the fork's patch
  series and manifest (`display-input`, detection through the manifest/build tag, not a CLI/IPC
  probe: the mode is GUI-only and env-var gated); the MR draft is
  `docs/upstream/drafts/display-input-mr.md`, held under D19.
- `kiwrap.sandbox` / the session supervisor: a project session (`kicad.exe`) is one pid for every
  editor; the supervisor must expose the HWND/owner-chain map, launch without activation, and set
  `KICAD_INPUT_FROM_EVENTS=cloak` only for builds that carry the patch.
- `docs/LINUX.md`: the wsl-linux spike verified the sandbox seed recipe and "GUI under Xvfb" on
  10.0.6/Ubuntu 24.04 (KiCad honours `KICAD_*_HOME` exactly as on Windows; wizard-free start) — a
  one-line update for that file's owner. Trap recorded there: any KiCad binary run without the env
  creates `~/.config/kicad`; `pkill -f pcbnew` also matches an xpra server's command line (`pkill -x`).

---

## 12. Follow-up (2026-09-20): "can VNC + KiCad live on the Windows env, with several editors open?"

Two spikes answered the owner's follow-up on the same machine and KiCad builds as §3, again under a
kiwrap-seeded sandbox root and on scratch copies of the ecc83 demo: **(a)** the multi-editor capture
question ([win-multi.md](research/display/win-multi.md): TightVNC `-shareapp`, WGC per-editor
*surfaces*, two live custom-editor tabs + a 3D panel in VS Code, and a "hybrid" injector) and **(b)** the
hidden-window input question ([win-input-patch.md](research/display/win-input-patch.md): a KiCad patch
on the fork, `KICAD_INPUT_FROM_EVENTS`, driven with `PostMessage` only while covered or cloaked). Every
number below is from those two reports; the caveat of §3 applies twice over: both spikes ran on the
same desktop at the same time, each stealing the other's foreground and cursor, and a third session's
KiCad 8.0 pytest wrote to `%APPDATA%\kicad\8.0` (the `10.0`/`10.99` config dirs, the only ones a process
launched here could touch, are unchanged).

### 12.1 Short answer

**Yes, but "VNC" is the wrong tool and "live" splits into two products.** A KiCad *project* is one
process with several top-level frames (§12.2), so any per-PID or per-window sharing has to model that.
TightVNC's only multi-window mode, `-shareapp`, does stream every editor, dialog and the chooser, but as
a 7680x2160 polled desktop at 9 fps with a full core burnt per idle client and black holes wherever
anything covers KiCad. The WGC helper with per-frame surfaces does the same job the way the tab model
needs it: pcbnew in the `.kicad_pcb` tab, eeschema in the `.kicad_sch` tab, the 3D viewer in a panel,
25-29 fps per tab with three streams live, 40-70 ms wheel-to-pixel, 0 while idle (§12.3). On the input
side the "wall" of §8.1 has moved twice: the hybrid injector lets VS Code keep the foreground and the
user's cursor while clicks, drags, hotkeys, dialogs and the chooser still reach stock KiCad (§12.4), and
a 25-file patch carried in the fork (`mr/display-input`) removes the remaining dependency on the
physical cursor, key state and `WH_KEYBOARD` hook so that a **cloaked** KiCad, not drawn on the desktop
at all, can be driven purely with posted messages (§12.5). What no route fixed: tooltips (comctl32
polls the real cursor), popup menus / combobox drop-downs (OS windows that Windows still shows on the
desktop), and one foreground grab by KiCad at launch.

### 12.2 Process model of a project session (measured, KiCad 10.0.6; 10.99 **untested** for this)

| Fact | Measurement (win-multi §1) |
|---|---|
| One process, one GUI thread | `kicad.exe` pid 83788, thread 92068 hosts the manager, the schematic editor (`_eeschema.dll`, frame 1.1 s after Ctrl+E), the PCB editor (`_pcbnew.dll`, 3.4 s after Ctrl+P) and the 3D viewer (0.46 s after Alt+3) |
| Frames are **unowned** siblings | all four are class `wxWindowNR` with `GW_OWNER` 0; the 3D viewer is *not* owned by pcbnew, it opens over it |
| Dialogs are **owned** | `Footprint Reference Properties` (`#32770`) and the `Footprint Chooser` (`wxWindowNR`, DIALOG_SHIM) are owned by the pcbnew frame and disable it while open (0.27-0.37 s after `E`); the chooser opens at its **remembered position** (1.6 kpx away from pcbnew, over the manager) |
| Hidden clutter | 20+ `tooltips_class32`, 9 `ComboLBox`, timer/display hidden windows, IME, `NVOpenGLPbuffer` (GL threads), a GDI+ hook window; a stray *visible* tooltip can sit anywhere on the desktop |
| Standalone editors | `pcbnew.exe` / `eeschema.exe` started directly are separate pids with one frame each (no lock warning appeared although the manager session had the same files open) |

Consequences that every route has to respect: **PID cannot tell the editors apart**; the unit is the
top-level HWND plus its `GW_OWNER` chain; a foreground check by PID sends a pcb key to eeschema (this
bug was measured in the first winstream injector and fixed); the chooser and other remembered-position
windows must be gathered onto their owner or the tab grows to a 3.5 kpx stage.

### 12.3 Per-route multi-window behaviour (measured)

| Criterion | TightVNC 2.8.88 `-controlapp -shareapp <pid>` + websockify + noVNC 1.7 | WGC per-window *surfaces* (winstream extended), 3 headless clients | WGC surfaces inside VS Code (`vscode-shell` live mode) |
|---|---|---|---|
| Status | V, partial: not worth productizing | **V, works** | **V, works** |
| What is captured | the whole 7680x2160 virtual desktop with every non-KiCad pixel black; all four frames + owned dialogs + chooser + GL 3D at their desktop positions; overlapping KiCad frames composite by z-order | each editor frame its own stream at physical px (pcb 1920x1080, sch 1800x1200, 3d 1400x900, mgr 1000x700); owned dialogs composited at their real offset **in the owner's tab only**; chooser gathered onto its frame (rel (428,119)); menus via monitor crop | `.kicad_pcb` tab = pcb surface, `.kicad_sch` tab = sch surface, 3D viewer as a `WebviewPanel` column, one helper; connected 2.7 s after the open commands; tabs downscale the physical frame (0.435 / 0.464 at DPR 1.5 in a 2600x1500 window) |
| A foreign window (VS Code, Notepad) on top of a frame | **black hole**: the covered KiCad content is lost, the foreign window does not leak, a click there goes to the foreign window | capture unaffected (occlusion only matters for `SendInput`) | same |
| Minimised frame | vanishes (rect at -32000) | no frames (as before) | same |
| Per-editor mapping | none: the client would crop the desktop per tab and follow window moves itself | by surface id (`/ws?surface=pcb`), owner-chain grouping, `SetWinEventHook` (hook event to window list 11-15 ms, 26-68 ms in dialog storms) instead of polling | file type to surface |
| Input routing | VNC pointer = the real cursor; wheel over pcbnew zooms pcbnew, over eeschema zooms eeschema; keys go to the host **foreground** frame (click first) | wheel/keys in the pcb client change pcb only (1-3 title-bar repaints on sch when activation flips); per-surface foreground check; 3D viewer needs one activating click before a drag rotates | real `SendInput` clicks + Home + wheel into each tab reach the right editor (helper log `pcb down/up` at frame (1002,349), then 5-6 pcb frames; sch likewise) with the hybrid injector, **dev host stays foreground, cursor stays in VS Code** |
| Latency (wheel to pixel) | first framebuffer rect median 15-16 ms (3-70), Home 40-60 ms, but see frame rate | pcb median 63-70 ms (40-83), sch 58-61 ms (35-66); split ws to inject 1 ms, inject to WGC frame 41-42 ms, capture to paint 20-26 ms; interleaved tab switching 37-74 ms | pcb tab 59-66 ms (51-69), sch tab 41-68 ms (24-72) |
| Frame rate while panning | **8.5-9 updates/s** (whole-desktop poll; 39-45 with `-sharewindow`) | pcbnew **25.7 fps** with sch+3d live, eeschema 25.9 fps; both busy 26.9 + 6.3 fps at 57 Mbit/s; 3D drag-rotate 24 fps; idle 0 frames | pcb tab **24-28 fps**, sch tab 26-29 fps, three tabs live; idle 0 |
| Dialog / chooser appearance | wherever it opens on the desktop | properties dialog painted in the pcb tab 371-583 ms after `E` (KiCad's own build time; 11-15 ms of it is hook to list), never in the sch tab | same helper |
| CPU | `tvnserver` **94-113 % of a core with one client connected, even idle** (0.6 % without); pcbnew 10-14 % | helper 14-20 % (process) / 36-43 % (with capture threads), kicad 6-15 %, 3 Chromium clients 42-48 % | `Code.exe` (10 dev-host processes) 36-52 %, one run 126 % while decoding the 1800x1200 stream; helper 26-66 %; kicad 9-17 % |
| Bugs found on the way | none | stray tooltip windows outside every root grew a stage to 3.5 kpx (now dropped) | Electron sends permessage-deflate RSV1 frames and aiohttp closed the socket with 1002 (fixed with `compress=False`); interleaved `send_str`/`send_bytes` (one writer per client now); one unexplained 39 s stall of webview-to-extension messages after a panel was created while unfocused (seen once) |
| Screenshots | [02 framebuffer](research/display/img/win-multi-02-tightvnc-shareapp-framebuffer.png) · [03 foreign window = black hole](research/display/img/win-multi-03-tightvnc-shareapp-foreign-window-black-and-overlap.png) · [04 chooser](research/display/img/win-multi-04-tightvnc-shareapp-chooser.png) | [01 host reference](research/display/img/win-multi-01-host-monitor2-manager-sch-pcb-3d.png) · [05 three clients](research/display/img/win-multi-05-wgc-per-surface-clients.png) | [06 pcb + sch tabs](research/display/img/win-multi-06-vscode-pcb-and-sch-tabs.png) · [07 + 3D panel](research/display/img/win-multi-07-vscode-pcb-sch-3d-panel.png) · [input-03 click into the pcb tab](research/display/img/win-input-03-vscode-click-in-pcb-tab.png) |

Verdict on the VNC half of the question: `-shareapp` is measured out (9 fps, a core per idle client,
black holes under VS Code, no per-editor mapping, GPL server); decision 3 of §9 is settled in favour of
the helper.

### 12.4 Input with stock KiCad while VS Code keeps the foreground: the hybrid injector (measured, partial)

`--inject hybrid` in winstream: pointer moves are posted `WM_MOUSEMOVE`s; buttons and wheel **borrow
the cursor** (`SetCursorPos` to the target, deliver `WM_xBUTTON*`/`WM_MOUSEWHEEL` synchronously with
`SendMessageTimeout` so KiCad reads the cursor while it is there, restore); keys are posted to the
thread's focus window; a posted `Esc` whose focus window belongs to an owned top-level becomes
`WM_CLOSE`; the target HWND is searched among *this surface's* windows only, so a covering window never
receives the click; right-click makes one real foreground flip (`TrackPopupMenu` closes at once in a
background window: measured, no `#32768` ever appeared without the flip).

Measured with **Notepad holding the foreground** (win-multi §4, hybrid matrix run 2): click-select R3's
reference (Properties panel 20-25 % changed), `R` rotate, `Delete`, `Ctrl+Z` (restored in run 1; not in
run 2, which the other spike's foreground theft disturbed), wheel zoom at the pointer (53-63 % canvas
change), middle-drag pan, left-drag move + undo, `E` opens the modal dialog composited in the tab, `Esc`
closes it, `A` opens the chooser, typing filters it, `Esc` closes it; `foreground_fixes` 0-1 (the
right-click only), `SendMessageTimeout` timeouts 0, `cursorBefore == cursorAfter` after clicks and
drags. Inside VS Code the same injector routed real clicks/keys from the pcb and sch tabs to the right
editor with the dev host remaining foreground (§12.3, last column).

Left over with stock KiCad: **KiCad's own cursor warps** (`Home`/zoom-fit and every `center_on_zoom`
action land the user's pointer on the KiCad canvas centre, measured after each `Home` from a VS Code tab);
one foreground flip per context menu; no tooltips; the KiCad windows must exist on-screen (occlusion is
fine for capture and for the hybrid clicks, but dialogs, menus and the right-click flip surface on the
desktop, so a second monitor or a reserved area remains the honest setup). Cost: ~200 lines in the helper.

### 12.5 Hidden-window input with a patched KiCad (`mr/display-input`, measured, works)

Root causes of the stock failure, read from wx 3.3.2 and KiCad sources and each matching the earlier
measurements (win-input-patch §1): `WX_VIEW_CONTROLS` / `TOOL_MANAGER` read `wxGetMousePosition()`
(`GetCursorPos`); `TOOL_DISPATCHER`'s button state and KiCad's `wxGetKeyState()` calls read
`GetAsyncKeyState`; wx fills modifiers from `GetKeyState`, which posted messages never update;
`wxEVT_CHAR_HOOK` comes from a `WH_KEYBOARD` hook that Windows runs for hardware input only (so a posted
`Esc` never reaches `wxDialog`/`DIALOG_SHIM`); wxMSW's `GenerateMouseLeave` builds LEAVE events from the
real cursor; `center_on_zoom` (default on) warps the physical cursor on every wheel step.

The patch (fork branch `mr/display-input` = `base/6dd893f7d1` + commit `d70b0af290`, 25 files
+534 -48, `Assisted-by: Claude Code` trailer, pushed to the **private** origin only, nothing posted,
D19): an env-var-gated mode `KICAD_INPUT_FROM_EVENTS=1|cloak`, invisible while unset, GTK/macOS stubs.
`KIPLATFORM::UI::GetMousePosition/GetMouseState/IsKeyDown` return event-derived state, `WarpPointer`
moves only the reported pointer, a process-wide `wxEventFilter` installed by `PGM_BASE::InitPgm` records
pointer/buttons, mirrors key transitions with `SetKeyboardState`, synthesises `wxEVT_CHAR_HOOK` from
`wxEVT_KEY_DOWN` like `wxKeyboardHook`, and drops spurious LEAVE events; the state hangs off `wxTheApp`
because kiplatform is a static library with one copy per module (a file-static was written in kicommon
and read stale in `_pcbnew.dll`: two hours lost). `cloak` additionally `DWMWA_CLOAK`s every wx top-level
on show; only the owning process may cloak (`DwmSetWindowAttribute` from the helper: `E_ACCESSDENIED`).
The matching injector (`winstream/inject_events.py`) is `PostMessage` only, no `SendInput`,
`SetForegroundWindow`, `SetFocus` or `AttachThreadInput`, with hit-testing inside the process's own
windows in z-order, correct `wParam`/`lParam`, double-click synthesis, `WM_MOUSELEAVE` hand-over and keys
to the UI thread's focus window (`GetGUIThreadInfo`).

| Criterion | Patched pcbnew on-screen but **covered** by an opaque topmost window (stand-in for VS Code) | Patched pcbnew **cloaked** (`=cloak`), nothing covering it | Patched pcbnew fully **off-screen** (or a 2 px strip on-screen) |
|---|---|---|---|
| Status | **V, works** | **V, works**: the placement to build on | V, partial: not a product path |
| Capture | WGC per window at full rate (27.6 fps during motion, 0 idle); dialogs open centred on the frame and can stick out of the covered area | desktop shows **0 pixels** at the window rect (screen BitBlt blank, `WindowFromPoint` returns what is behind); WGC frame is the full UI at full rate (41/41 frames vs uncloaked; 47 over the edit matrix); owned dialogs cloaked too | WGC delivers the initial frame and **no updates** (DWM stops composing); `PrintWindow(PW_RENDERFULLCONTENT)` polling works at 87 ms/frame (~11 fps, 8 MB readback) |
| Input (all posted) | click select (track / pad / text / footprint), `R`, `Delete`, `Ctrl+Z`, left-drag move + undo, wheel zoom **centred on the reported pointer with the physical cursor untouched**, middle-drag pan, double-click opens Footprint Properties, `E` opens the dialog, click into X, `Ctrl+A`, type `140`, OK (73 ms), `A` opens the chooser, filter `led`, two-step `Esc`, right-click opens the context menu at the reported pointer, item click dispatches, `Esc` closes menus/dialogs | same matrix (Home, select, R, Ctrl+Z, drag-move, wheel, pan, E/double-click dialog, Cancel click); `Esc` on Footprint Properties is consumed by its fields-grid editor when that holds focus (click Cancel/OK instead) | input applies (verified via `PrintWindow`: Home, pointer, selection) |
| Real cursor / foreground | `GetCursorPos` constant across four page moves; `GetForegroundWindow` stayed on the cover or other apps; two flips coincided with a posted `Esc` while the sibling spike was destroying foreground windows, not reproducible with a steady foreground | same; pcbnew still takes the foreground **once at launch** (cloaked or not) | same |
| Latency / cost (browser client) | wheel to pixel median **69 ms** (33-84): 1.4 ms ws to post, 37 ms post to WGC frame, 25 ms capture to paint; 27.6 fps motion at 8.8 % server / 3.4 % pcbnew; ~40 fps pan; 19.5 fps continuous zoom; Footprint Properties appears 0.6-2.4 s (first construction with the 3D preview) | same pipeline | n/a |
| What escapes | popup menus, combobox drop-downs and tooltips are OS windows, not wx top-levels: a context menu of a cloaked KiCad pops up **on the user's desktop** at the reported pointer; tooltips never appear (comctl32 polls the real cursor) | same | none |
| Screenshots | [edit matrix](research/display/img/win-input-patch-edit-matrix.png) · [dialog typed + chooser filtered](research/display/img/win-input-patch-dialog-and-chooser.png) | [desktop vs WGC of the same moment](research/display/img/win-input-patch-cloaked-desktop-vs-wgc.png) · [cloaked dialog + client HUD](research/display/img/win-input-patch-cloaked-dialog-and-client.png) | none |

**Untested** on the patch: eeschema, the symbol/footprint editors and a project session (`kicad.exe`
with several frames) in this mode; Alt combinations (`WM_SYSKEYDOWN` implemented, not exercised); the
auto-pan `HasFocus()` path; another Windows virtual desktop as a placement; cloaking menus/drop-downs
from inside KiCad (`WH_CBT` / `EVENT_OBJECT_SHOW` hook); the guard against hardware keys reaching
`wxEVT_CHAR_HOOK` twice while the mode is on; the 10.0 stable branch (the patch is on the 10.99 base).

### 12.6 Updated recommendation for the Windows product

Three shapes are now measured; they share the shell, the helper's capture and the surface model, and
differ only in the injector and where the KiCad windows live:

| Shape | KiCad build | Where KiCad's windows are | Injector | What the user gets | Measured limits | Extra effort on top of D1 |
|---|---|---|---|---|---|---|
| **A. Capture-only view** (+ click-to-raise, `kw_live screenshot`) | stock 10.0.x / 10.99 | on the desktop, wherever | none | live picture of every editor in its tab, dialogs/chooser composited, 0 cost idle | it is a picture; editing happens in the real window | none (this *is* D1; the surface model is now part of it) |
| **B. Mirror with the hybrid injector** (opt-in) | stock | on the desktop (a second monitor / reserved area is still the honest setup; occlusion is tolerated) | hybrid (§12.4) | edit from the tab; VS Code keeps the foreground and the cursor for clicks, drags, hotkeys, dialogs, chooser | KiCad's own cursor warps (`Home`, `center_on_zoom`) move the user's mouse; one foreground flip per right-click; tooltips absent; dialogs/menus visible on the desktop | ~200 lines of injector + cursor restore after KiCad warps + consent UI: estimate **1-1.5 weeks** (replaces the 3-4 week SendInput/AttachThreadInput mirror of §7.2 item 3) |
| **C. Hidden UI on a patched build** ("KiCad invisible, VS Code is the UI") | fork build carrying `mr/display-input`, launched with `KICAD_INPUT_FROM_EVENTS=cloak` and without activation | cloaked on-screen (not fully off-screen) | events (§12.5), `PostMessage` only | real wxMSW UI in the tab, no desktop window, no cursor or foreground contest, wheel centred on the tab's pointer | menus/drop-downs still pop on the desktop (needs the menu-capture decision below); tooltips absent; one foreground grab at launch; only pcbnew measured; only builds with the patch (development installs under UPSTREAM §3.3 until the MR lands, which D19 defers) | helper **1-2 days** (fold `EventInjector` into the native helper) + launcher change (`cloak` env, no-activate start) + menu decision; KiCad side done for pcbnew, eeschema matrix still to run |

Recommendation: build **A** as planned (D1, now including surfaces so a project session lands in
per-file tabs plus a 3D panel), make the injector a *mode of the launched session*: **C when the
supervisor started a build whose `kiwrap.patches` contains `display-input`, B as the opt-in on stock
installs**, never `SendInput` (the first injector's foreground contest was the actual wall, and both
newer injectors avoid it), and keep `-shareapp`/TightVNC out of the product. The helper stays
C#/.NET 8 or Rust as in §7.2; the Python prototypes remain the regression drivers. Carry
`mr/display-input` in the fork's integration branch (D17) and hold the upstream issue/MR draft
(`docs/upstream/drafts/display-input-mr.md`, pitched as assistive/automation input with `cloak` kept
fork-only) until D19 lifts; UPSTREAM.md card **G15** tracks it. The stock-user story does not change:
without the patch the product is A plus, on consent, B.

### 12.7 Updated effort (one engineer, prototypes as the starting point; estimates, not measurements)

| Item | Was (§10) | Now | Why |
|---|---|---|---|
| D0 close open measurements | 0.5 w | 0.25 w | `-shareapp` measured; tooltip absence root-caused (comctl32 polls the real cursor); left: quiet-desktop VNC key matrix re-run (§5), WGC on another virtual desktop, 10.99 project-session model |
| D1 capture-only Live KiCad | 1.5-2 w | **2-2.5 w** | + surface/owner-chain model, `SetWinEventHook`, dialog gathering, per-surface streams, file-type-to-surface mapping and the 3D panel (all done in Python; the two WebSocket fixes for Electron carry over) |
| D2 MCP integration | 1-1.5 w | 1-1.5 w | unchanged; the project session is one pid, so the supervisor keys captures on HWND + owner chain, not pid alone |
| D3 interactive mirror (stock, opt-in) | 3-4 w | **1-1.5 w** | hybrid injector instead of SendInput + AttachThreadInput + monitor pinning; still needs cursor restore after KiCad warps, the right-click flip, key release on blur, consent UI |
| D5 fork patch spike (decision 5) | 1-2 w research | **done** (spike b) | KiCad side on `mr/display-input`; remaining KiCad work: eeschema/editor matrix (0.5 w), hardware-key guard + rebase hygiene before any MR (0.5 w), optional in-process menu cloaking (untested, 0.5-1 w) |
| D6 hidden UI on patched builds (new) | none | **0.5 w** | helper `EventInjector` port 1-2 d, launcher (`cloak`, no-activate), tracker keeps cloaked windows, menu-capture decision |
| Decision 6 virtual display driver | 0.5 w spike | drop | cloaking gives an invisible KiCad without a driver, but only with the patch; for stock builds the driver route stays untested and would still have the foreground contest |

Recommended path A + C for patched builds (D0 + D1 + D2 + D6): **4-5 weeks**; B on stock adds
**1-1.5 weeks**; the Linux provider is unchanged (2-3 weeks, deferred under D18).

### 12.8 Decisions the owner must take now (supersedes the same-numbered rows of §9)

| # | Decision | Options (recommendation in bold) | What it changes |
|---|---|---|---|
| 1 | What is "Live KiCad in VS Code" on Windows? | (a) capture-only view; (b) + hybrid mirror on stock (opt-in); (c) + hidden UI on patched builds; **(d) a + c, with b as the stock opt-in** (§12.6) | (a) 2-2.5 w; (b) +1-1.5 w; (c) +0.5 w and the fork build on the critical path; (d) is the §12.6 plan |
| 3 | GPL VNC server vs MIT WGC helper | **helper**, settled by measurement (`-shareapp`: 9 fps, a core per idle client, black holes) | TightVNC leaves the product entirely, including the escape hatch |
| 5 | Patch KiCad in the fork for hidden-window input? | was "defer"; **done as `mr/display-input`; carry it in `kiwrap/<sha>`, keep `cloak` fork-only, hold the MR under D19**, or (b) do not carry it (25 files on hot paths: `tool_dispatcher.cpp`, `wx_view_controls.cpp`, `pcb_selection_tool.cpp`; every base bump rebases it) | carrying it makes C available on every bench/dev build; not carrying it leaves A + B only |
| 6 | Virtual display driver spike? | **drop** (cloak does the job with the patch; stock builds would still have the SendInput contest) | saves the time-boxed spike |
| 11 (new) | Popup menus and drop-downs in hidden mode C | (a) helper captures the monitor region while a menu is open (winstream's monitor crop; the menu flashes on the desktop); (b) cloak them from inside KiCad with a `WH_CBT`/`EVENT_OBJECT_SHOW` hook (**untested**, menus are fragile under per-window capture); **(c) ship (a) first, spike (b) for 2-3 days** | (a) is a visible flash per menu; (b) keeps KiCad fully invisible |
| 12 (new) | Tab model for a project session | **custom editor per `.kicad_pcb`/`.kicad_sch` + a 3D `WebviewPanel` + the manager left uncaptured** (measured shape) vs one project panel with in-page windows (untested) | the per-file shape is what the `.kicad_*` custom editors already give; the 3D viewer and manager need commands |
| 13 (new) | Accept the hidden mode's remaining limits | tooltips absent (all Windows routes), one foreground grab at KiCad launch, dialogs may stick out of a covered area (moot when cloaked), eeschema/other editors **untested** | if any is a blocker, C waits for the KiCad-side follow-ups in §12.7 |
| 14 (new) | Effort on the eeschema/editor matrix for the patch | **0.5 w before C ships** (same code paths, not run) | de-risks the `.kicad_sch` tab in hidden mode |
