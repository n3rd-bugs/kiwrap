# Forwarding KiCad's display into VS Code — synthesis and recommendation

Status: research synthesis, 2026-09-19. Private. Owner question: *"can we forward the KiCad display to
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
- **Linux KiCad on a private virtual display (WSL2 here; a Linux host/container in general):** xpra
  seamless + xpra-html5 delivers exactly what the owner asked for — the identical UI with menus, tooltips,
  modal dialogs, the footprint chooser with its GL preview, all hotkeys, two-way clipboard, 57 fps pan,
  18-52 ms wheel latency, session survives reconnect ([wsl-linux](research/display/wsl-linux.md)). Its
  costs: it is a *second, Linux* KiCad (1.5 GB WSL install, own config/libraries), llvmpipe burns 4-7
  cores while panning under WSL2 (no GPU offload there), and D18 has put Linux out of scope.
- **Not viable:** GTK Broadway (KiCad and wx hard-pin X11, no GL, ~1.5 fps pan, broadwayd crashed
  twice in 10 min), x11vnc of a WSLg window (black GL canvas), UltraVNC (single-window share removed
  upstream). **Untested:** KiCad-in-WebAssembly (PCBJam), KasmVNC/Selkies, Guacamole, a Windows virtual
  display driver, TightVNC `-shareapp`.

**Recommendation (§7):** build the cheap, robust Windows parts now — a capture-only **Live KiCad**
panel/custom editor fed by a WGC helper, click-to-raise the real window, and a `kw_live screenshot`
action for the agent loop — on the vscode-shell design; make the interactive mirror an explicit opt-in
("KiCad mirrored in VS Code", second-monitor workflow) only if the owner accepts its semantics; keep
xpra as the documented Linux/remote provider behind the same shell contract, informational under D18.
A true "hidden KiCad, VS Code is the UI" on Windows requires either a KiCad patch carried in the fork
(D17) or an untested virtual-display route; decide that separately (§8).

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
  (**untested**).

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

Every GUI ran with a kiwrap-seeded sandbox root (`KICAD_CONFIG_HOME` etc.) on scratch copies of the
ecc83 demo; `%APPDATA%\kicad` hash `a90607a5…6d9d` was unchanged after every spike; no processes were
left running. One caveat applies to all Windows measurements: the desktop was shared by three spikes
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
  keyboard mode (winstream §3.2); every injected mouse move moves the user's real cursor.

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
  provider "win-wgc"   (Windows, primary)   helper process: WGC per-window capture, JPEG/H.264,
                                            SendInput (opt-in), SetWinEventHook window tracking,
                                            loopback ws + per-session token, PMv2 DPI
  provider "xpra"      (Linux/WSL/remote)   xpra seamless per user + xpra-html5 in the same page
                                            via the iframe shape (or its protocol on the canvas)
  provider "vnc"       (fallback/escape)    any RFB server (TightVNC -shareapp / x11vnc) + noVNC
```

The KiCad process under every provider is launched by kiwrap's existing rules: `kiwrap.sandbox`
(`ConfigRoot.env()`, `enable_api_server=True` for live sessions), one GUI at a time, short `TEMP`,
tracked and killed by PID (WER restarts are different processes), scratch-free user config.

### 7.2 Windows (D18's target): what to build and in which order

1. **Capture-only Live KiCad view** (the part every spike agrees is cheap and robust): the `win-wgc`
   helper streams the sandboxed `pcbnew.exe`/`eeschema.exe` windows — main frame plus owned dialogs
   and the footprint chooser composited at their offsets; menus via the monitor-crop path — into the
   custom editor/panel. 0 bytes when idle. **No input injection**: a click on the view raises the real
   KiCad window (`SetForegroundWindow` from a user gesture is allowed), keys are not forwarded. This is
   ARCHITECTURE §9's "Live-KiCad panel" with a live picture instead of a launch button, and it never
   fights the user for focus.
2. **`kw_live screenshot`**: one WGC frame of the main window (<10 ms, GL canvas included) returned as
   an image ≤1024 px / <100 KB per the §9 image rule — "what the user sees", better than a kicad-cli
   render for that question, and still secondary to structured data (D17).
3. **Interactive mirror as an explicit opt-in** ("KiCad mirrored in VS Code"): same helper with
   `SendInput` enabled, the raise-if-obscured check, the AttachThreadInput foreground path (never the
   ALT trick), key release on webview blur, and KiCad pinned to a screen area VS Code does not cover
   (second monitor; a Windows virtual desktop is **untested** for WGC). The user must be told that the
   KiCad window exists on their desktop, that every injected move moves their cursor, and that focus
   flips. Do not sell it as "KiCad inside VS Code".

The helper must not be Python (the prototype's venv + 60 MB OpenCV is not shippable): C#/.NET 8 with
the WinRT `Windows.Graphics.Capture` projection, D3D11 staging and libjpeg-turbo (or Media Foundation
H.264 for 4K), or Rust with the `windows` crate. Loopback only, random token per launch in the URL
and the ws handshake, CSP restricted to that origin. Dirty-rect streaming (`WGC` exposes dirty
regions) is the main optimisation for editing workloads where only the canvas changes.

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

1. **"Identical UI/UX inside VS Code" on Windows is not reachable with stock KiCad.** Two independent
   spikes hit the same wall from opposite sides: VNC input targets the host foreground window and
   the shared rect leaks other windows (vnc-novnc); per-window capture is clean but `SendInput` needs
   the window visible/unobscured/foreground and `PostMessage` is defeated by `wxGetMousePosition()`
   and by wxDialog ignoring posted `Esc` (winstream). In production the window covering KiCad *is* VS
   Code. The interactive mirror is therefore a second-monitor feature with focus flapping, cursor
   hijack and any topmost window (notifications, screen recorders) stealing clicks.
2. **A KiCad-side patch would remove the wall** — an "input coordinates from the event" mode in
   `WX_VIEW_CONTROLS` plus dialog handling of posted `Esc` — and D17 allows carrying patches in the
   fork; but it is an ongoing patch on the fork's critical path, it only helps builds that carry it
   (stock 10.0.x users keep the mirror semantics), and it is **untested** that nothing else in wx reads
   global cursor/key state.
3. **Tooltips** never appeared under injected hover in either Windows route (not root-caused); a
   polling VNC server also never captured the layered tooltip window. Only xpra showed them.
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
   window during the spike).
10. **Untested items that could change the picture:** TightVNC `-shareapp <pid>` (keeps dialogs,
    darkens the rest); WGC of a window on another Windows virtual desktop; an IddCx virtual display
    driver (admin install) as an invisible monitor for KiCad; KasmVNC/Selkies H.264 for the codec cost;
    xpra at `--dpi=192`; PCBJam loading in a webview with/without `--enable-coi`; VirtualGL on a real
    Linux host; the 10.99 GUI's IPC in a live session.

---

## 9. Decisions the owner must take

| # | Decision | Options (recommendation in bold) | What it changes |
|---|---|---|---|
| 1 | What does "Live KiCad in VS Code" mean on Windows? | **(a) capture-only live view + click-to-raise + `kw_live screenshot`**; (b) interactive mirror (window on a second monitor/area, SendInput, focus flapping); (c) both, mirror opt-in | (a) ~1.5-2 weeks, no UX caveats; (b) adds 3-4 weeks and a settings/consent surface; (c) is the §7 plan |
| 2 | Accept remote-display latency and frame rate? | **yes for a view/mirror** (40-55 ms, 24-30 fps Windows; 18-52 ms, 57 fps xpra); no → only kicad-cli previews and the real KiCad window | if no, §7.2 items 1-2 still stand (a picture, not a control surface) |
| 3 | Ship a GPL VNC server or build the MIT WGC helper? | **build the helper (C#/.NET 8 or Rust)**; TightVNC only as an unsupported escape hatch behind a setting | helper = 2-3 weeks of native code and a second language in the repo; TightVNC = registry-based config, polling CPU, tooltips/off-rect dialogs lost |
| 4 | Fund the Linux/WSL xpra provider now? | **no under D18 — keep the prototype and docs**; yes → 1-2 weeks + 1 week WSL bootstrap, plus llvmpipe/GPU caveats | if yes, the display-session contract gets its second provider and remote SSH/containers come for free |
| 5 | Patch KiCad in the fork for hidden-window input (event-coordinate mode + dialog Esc)? | **defer; open a design note in UPSTREAM.md**; yes → 1-2 weeks spike on the source build (D1/D17) | only path to "KiCad invisible, VS Code is the UI" on Windows; stock users keep mirror semantics |
| 6 | Spend 2-3 days on the untested Windows "invisible monitor" idea (IddCx virtual display driver, WGC on another virtual desktop)? | **one time-boxed spike after decision 1** | could turn the mirror into a hidden-KiCad view without a KiCad patch; needs an admin-installed driver and still has the SendInput foreground contest |
| 7 | Spend 2-3 days on the PCBJam (KiCad-in-WASM) feasibility check? | **defer until PCBJam ships self-hosting**; the survey's questions (loads in a webview with/without `--enable-coi`, WebGL GAL, bundle size) are still open | the only route that would run on vscode.dev; breaks the installed-binaries premise (D3/D16) |
| 8 | Keyboard policy while the KiCad view is focused | **"KiCad mode": rescue block silences the 54 chords, one consented workspace change for the two Alt settings; Ctrl+Shift+P optionally kept for VS Code** | all-or-nothing (iframe shape) vs selective (canvas shape); selective needs our own client |
| 9 | Surface defaults | **custom editor `priority: "option"` + "Open with KiCad (live)" + project-level panel; text editor stays default** | `priority: "default"` would hijack `.kicad_pcb` opens |
| 10 | Where the helper's screenshots go for the agent | **`kw_live screenshot` returns ≤1024 px / <100 KB images; never automatic in the agent loop (D17)** | keeps the data-first rule; humans get the picture |

---

## 10. Phased plan (effort assumes one engineer, the prototypes as starting points)

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **D0 Close the open measurements** | 0.5 | Quiet-desktop re-run of the VNC key matrix (`vscode_keys2.ps1`) to settle the §5 conflict; tooltip root cause with WGC (real hover vs injected); `TightVNC -shareapp <pid>` dialog/darkening check; WGC of a window on another virtual desktop; write results into the spike reports | every "inconclusive/untested" item in §4 that is cheap has a verdict |
| **D1 Capture-only Live KiCad (Windows)** | 1.5-2 | C#/.NET 8 (or Rust) helper: WGC per-window + owned dialogs + menu-crop, libjpeg-turbo, dirty rects, `SetWinEventHook` tracking, loopback ws + token, PMv2; shell from `vscode-shell` (custom editor `option`, panel, rescue keybindings, context-menu opt-out, DPI/resize, reconnect); launched by the extension under the kiwrap sandbox with kill-by-PID; click-to-raise; `kw_live screenshot` | opening a board "with KiCad (live)" shows the sandboxed pcbnew including a modal dialog and the chooser; idle 0 bytes; 1080p pan ≥ 24 fps at ≤ 20 % of one core; `%APPDATA%\kicad` hash unchanged in CI on the self-hosted runner |
| **D2 MCP integration** | 1-1.5 | `kw_live` session state in the view's title/status; agent `kw_pcb_place` / `push` visible in the view without polling; headless-session fallback message + kicad-cli preview; supervisor-owned PID is the only capture target; @vscode/test-electron + Playwright regression of the display path | an agent edit through `kw_live` appears in the view within 100 ms of the commit; a WER-restarted or foreign pcbnew is never captured |
| **D3 Interactive mirror (opt-in, only if decision 1 ≠ a)** | 3-4 | SendInput path with raise-if-obscured, AttachThreadInput foreground, key release on blur, scancode mapping, modifier guarding; monitor pinning (second monitor / reserved area); consent UI and the two Alt settings; H.264 via Media Foundation for 4K if JPEG cost is too high | the winstream stage B-D action matrix passes from inside the VS Code view on a quiet desktop; documented as "mirrored", with the focus/cursor caveats in the UI |
| **D4 Linux/WSL/remote provider (deferred under D18)** | 1-2 (+1 WSL bootstrap) | `xpra` provider: per-user session (unix socket or `--bind-tcp=127.0.0.1` + token, `--html=on`), sandbox env forwarded, DPI (`--dpi=192`, `GDK_SCALE=2`), keyboard layout, dispose lifecycle; iframe shape first; VirtualGL/EGL on real hosts | the wsl-linux battery (menus, tooltips, chooser, hotkeys, clipboard, reconnect) passes from a VS Code Remote-WSL window |
| **D5 Research options (each time-boxed, only on a decision)** | 0.5-2 each | KiCad fork patch for event-coordinate input + dialog Esc (decision 5); IddCx virtual display spike (decision 6); PCBJam feasibility (decision 7); KasmVNC/Selkies codec check | a written verdict per item in `research/display/` |

Totals: the recommended path (D0 + D1 + D2) is **3-4 weeks**; the full interactive mirror adds
**3-4 weeks**; the Linux provider **2-3 weeks** if ever funded.

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
- Roadmap: Phase 3 "Live-KiCad panel" absorbs D1/D2 (3-4 weeks); D3/D4/D5 are new optional rows.
- `docs/LINUX.md`: the wsl-linux spike verified the sandbox seed recipe and "GUI under Xvfb" on
  10.0.6/Ubuntu 24.04 (KiCad honours `KICAD_*_HOME` exactly as on Windows; wizard-free start) — a
  one-line update for that file's owner. Trap recorded there: any KiCad binary run without the env
  creates `~/.config/kicad`; `pkill -f pcbnew` also matches an xpra server's command line (`pkill -x`).
