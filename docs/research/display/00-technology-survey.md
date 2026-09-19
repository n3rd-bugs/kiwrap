# display-survey: forwarding KiCad's live UI into VS Code and making it interactive — technology survey

Date: 2026-09-19. Scope: every route to show a native application's *live* UI inside a VS Code
webview and drive it (mouse, keyboard, clipboard), applied to KiCad 10.0.x / 10.99 (wxWidgets
3.2: wxMSW on Windows, wxGTK3 on Linux; OpenGL "GAL" canvas with a Cairo fallback). Web research +
VS Code source reading + a few headless probes in WSL (no GUI was launched for this component).
`[VERIFIED LOCALLY]` marks things reproduced on this machine; everything else is cited inline.
Companion documents (hands-on spikes) land next to this file as `01-…`, `02-…`.

The question the owner asked is "can the user get the IDENTICAL UI/UX of KiCad from within VS
Code?". Short answer up front:

- **Pixel-identical, fully interactive KiCad inside a VS Code tab is achievable**, but only by
  *remote-displaying a real KiCad process* into a webview (VNC/Xpra/WebRTC class). The webview is
  an HTML iframe; VS Code has no API to host a native window (§1.1). Remote display costs one
  extra copy of every frame and ~1–3 frames of latency on loopback, and it only becomes
  *clean* (no second copy of the window on the user's desktop, complete popup/menu capture,
  correct modifier state) when KiCad runs on a **virtual display** — which on this machine means
  the Linux KiCad 10.0.6 inside **WSL2**, where OpenGL 4.6 is GPU-accelerated on the RTX 5080
  through Mesa's d3d12 driver, even under Xvfb `[VERIFIED LOCALLY]` (§6).
- **Windows-native KiCad can only be *mirrored***: TightVNC `-sharewindow`/`-shareapp`,
  UltraVNC "single window" and Windows.Graphics.Capture all capture a window that must exist on
  the real desktop. Per-window capture misses popup menus/tooltips (separate HWNDs), input has to
  be injected into a window that is not foreground, and the user sees KiCad twice. It is a demo,
  not a product (§2.3).
- The only route that runs KiCad *inside* the webview process — no remote display at all — is
  **KiCad compiled to WebAssembly** (PCBJam, GPL-3.0, active, all six KiCad apps build; §2.7).
  Its widgets are DOM projections of wx controls, so it is "the real KiCad code with HTML
  widgets", not pixel-identical; it needs SharedArrayBuffer (VS Code must be started with
  `--enable-coi`) and JSPI (Chromium ≥137; local VS Code 1.138 ships Chromium 148 `[VERIFIED
  LOCALLY]`). It is a KiCad *fork*, so it breaks kiwrap's "installed KiCad binaries" premise.
- GTK Broadway is dead on arrival for KiCad: wxGLCanvas aborts with "wxGLCanvas is only supported
  on Wayland and X11" (§2.6).

Ranking and the recommended spikes are in §5.

---

## 1. VS Code webview facts that decide everything

All line numbers are from `microsoft/vscode` `main` on 2026-09-19 (`src/vscode-dts/vscode.d.ts`,
`src/vs/workbench/contrib/webview/browser/pre/index.html` = the webview preload/host page,
`src/vs/workbench/contrib/webview/browser/webviewElement.ts`).

### 1.1 What a webview is, and what it is not

- **[high]** Extensions have no DOM access and no native surface: "Extensions have no access to
  the DOM of VS Code UI"; custom UI is only possible through the Webview API (HTML/CSS/JS in an
  iframe). There is no API to host an HWND/X11 window, no Electron `BrowserView` exposure, and
  the extension host is a separate process. Consequence: *every* route ends in an `<iframe>` that
  must obtain pixels from KiCad through the network stack (WebSocket/WebRTC/HTTP) or run KiCad in
  WASM.
  - Sources: https://code.visualstudio.com/api/extension-capabilities/overview ("Restrictions"),
    https://code.visualstudio.com/api/extension-guides/webview
- **[high]** The webview content is a *nested* iframe: VS Code's host page (`pre/index.html`)
  creates `<iframe sandbox="allow-same-origin allow-pointer-lock allow-scripts allow-downloads
  [allow-forms]" allow="cross-origin-isolated; autoplay; local-network-access; clipboard-read;
  clipboard-write;">` (`pre/index.html` lines 1024–1038). Not in the sandbox list: `allow-popups`,
  `allow-modals`, `allow-top-navigation`. Not in the `allow` list: `fullscreen`, `gamepad`,
  `display-capture`. Consequences:
  - Pointer Lock: **available** (`allow-pointer-lock`).
  - Clipboard API (`navigator.clipboard.readText/writeText`): **available when scripts are
    enabled** (not on Firefox-based web builds). Additionally VS Code intercepts Ctrl+C/V/X in
    Electron and re-dispatches copy/paste itself (`handleInnerKeydown`, lines 590–622).
  - Fullscreen API: **not available** (no `allowfullscreen`/`fullscreen` permission);
    `window.open`/`alert()`: blocked.
  - `local-network-access` is granted, so `http://localhost`/`ws://localhost` connections from
    the webview are permitted by Chromium's local-network-access policy.
- **[high]** WebGL/WebGL2 work in webviews (KiCanvas-based extensions ship WebGL renderers; the
  prior-art digest verified KiCad Studio Kit locally). WebRTC and WebSocket are plain Chromium
  features and are not sandboxed away (google/vscode-vnc uses a `ws://localhost` socket from the
  webview; n.eko/Selkies clients are ordinary WebRTC pages). WebCodecs (used by KasmVNC's
  H.264/AV1 path) is available in Chromium 148.
  - Sources: docs/research/05-prior-art.md (Studio Kit), https://github.com/google/vscode-vnc
- **[high]** CSP: none is applied by default, the docs *recommend* `default-src 'none'; …
  ${webview.cspSource}`. To iframe a local server the page must allow `frame-src http://localhost:*`
  — the built-in Simple Browser does exactly this (`frame-src *;` in
  `extensions/simple-browser/src/simpleBrowserView.ts` line 132, with its own inner
  `<iframe sandbox="allow-scripts allow-forms allow-same-origin allow-downloads">`, line 169).
  Loading `http://localhost` from the `vscode-webview://` origin is not blocked as mixed content
  (localhost is "potentially trustworthy").
- **[high]** `WebviewOptions.portMapping` (`vscode.d.ts` 9885–9947): maps a static
  `webviewPort` to an `extensionHostPort`, **"port mappings only work for `http` or `https`
  urls. Websocket urls (e.g. `ws://localhost:3000`) cannot be mapped to another port."** For
  remote scenarios (SSH/WSL/Codespaces) the pattern is `vscode.env.asExternalUri(http://localhost:P)`
  and then swapping the scheme to `ws:` — google/vscode-vnc does this (`src/session.ts` 161–164)
  and falls back to a **postMessage-tunnelled socket** when the webview cannot reach localhost
  at all (vscode.dev). `asWebviewUri` is only for local *files* (extension/workspace) and cannot
  serve a live socket.
- **[high]** Lifecycle: a webview's iframe is destroyed when the tab is hidden unless
  `retainContextWhenHidden: true` ("high memory overhead"), and **"You cannot send messages to a
  hidden webview, even with `retainContextWhenHidden`"** (`vscode.d.ts` 10064–10080). A remote
  KiCad session therefore must survive webview teardown on the extension-host side (keep the VNC
  /Xpra/WebRTC session alive, reconnect on reveal) — exactly what Xpra's "persistent" design is
  for, and what noVNC reconnect handles.
- **[high]** Custom editors: `vscode.window.registerCustomEditorProvider(viewType, provider,
  {webviewOptions, supportsMultipleEditorsPerDocument})` with `CustomReadonlyEditorProvider.
  openCustomDocument(uri, openContext, token)` + `resolveCustomEditor(document, webviewPanel,
  token)` (`vscode.d.ts` 10519–10570, 11791–11807) and `contributes.customEditors` with
  `selector: [{filenamePattern: "*.kicad_pcb"}]`, `priority: "option"|"default"`. So
  double-clicking a `.kicad_pcb` can open the live KiCad panel; for a *live* editor use
  `CustomReadonlyEditorProvider` (KiCad owns the file, VS Code must not think it holds a dirty
  model) plus a `FileSystemWatcher` to refresh. Context keys for keybindings: `activeCustomEditorId`
  (`customEditor.ts` 19), `activeWebviewPanelId` (`webviewEditor.ts` 33), `webviewFindWidgetEnabled/
  Visible/Focused` (`webview.ts` 25–35).
- **[high]** Cross-origin isolation (SharedArrayBuffer, WASM threads): the webview iframe only
  gets `vscode-coi=3` (COOP+COEP) **if the workbench itself is cross-origin isolated**
  (`COI.addSearchParam` in `src/vs/base/common/network.ts` 434–445: "Will be a noop when
  `crossOriginIsolated` isn't enabled the current context"; `webviewElement.ts` 468). The desktop
  workbench is COI only when launched with the CLI flag **`--enable-coi`** (`argv.ts` 220). A
  child iframe cannot become COI on its own. This is the gate for the WASM route (§2.7).
- **[high, VERIFIED LOCALLY]** Installed VS Code 1.138.0 = Electron 42.10.0 / Chromium
  148.0.7778.280 (strings in `Code.exe`). JSPI (WebAssembly JS-Promise Integration) shipped in
  Chromium 137, WebGPU/WebGL2/WebCodecs/WebRTC are all present.

### 1.2 Keyboard: what VS Code sees before/after the webview

- **[high]** Mechanism (`pre/index.html` 590–640, 911–912, 1052–1053; `webviewElement.ts`
  719–743): the host page attaches bubble-phase `keydown`/`keyup` listeners on the inner iframe's
  `contentWindow`. Every **trusted** key event is copied (`key, keyCode, code, shift/alt/ctrl/meta,
  repeat`) and posted as `did-keydown` to the workbench, which re-dispatches a synthetic
  `KeyboardEvent` on the webview element ("workaround for electron/electron#14258 — these events
  do not bubble to the parent window anymore") *only while the webview element is the active
  element*. The keybinding service then resolves it like any keypress. So a key is delivered to
  **both** the page and VS Code; VS Code does not "steal" it first, it acts *in addition*.
- **[high]** Keys the preload actively `preventDefault()`s in the page (so the browser default is
  suppressed, the page's own handlers still run): Ctrl/Cmd+Z, +Y (undo/redo), +P (print), +F
  (find), +S (save); on Electron also Ctrl+C/V/X and Shift+Insert (VS Code re-issues copy/paste
  to the webview itself); on web builds Ctrl+W/N, F1, F5 (`isCloseTab/isNewWindow/isHelp/isRefresh`).
- **[high]** Consequences observed in issues: Ctrl+P/Ctrl+Shift+P/F1 open Quick Open/the
  palette while a webview is focused (#320435, #82592), Ctrl+W closes the webview tab (#188669
  reports it firing twice), a webview can trigger any workbench shortcut (#319593, security
  discussion), quick-pick shortcuts vs webviews (#116897), key presses leaking into webviews
  when a workbench shortcut fires (#85879 — "out-of-scope"), and #139163 asks for a way for
  webviews to override shortcuts. **Nested iframes** (a noVNC/Xpra page iframed inside the
  webview) are *not* observed by the preload at all — keys stay in the inner document (good for
  the app), but on macOS Cmd+A/C/V then stop working because Electron's menu accelerators are
  the only copy/paste path (#129178, open, backlog).
  - Sources: https://github.com/microsoft/vscode/issues/85879, /82592, /116897, /139163,
    /188669, /129178, /319593, /320435
- **[high]** Workarounds that follow from the mechanism (to be verified in the spike):
  1. In the webview page, call `event.stopPropagation()` on `keydown`/`keyup` at the `document`
     bubble phase *after* the client (noVNC/Xpra) handled the key: the preload's `contentWindow`
     listener is bubble-phase, so it never runs and VS Code never hears the key. Nothing else in
     the desktop build intercepts keys before the keybinding service (VS Code has a custom title/
     menu bar on Windows, so there are no native accelerators). `forwardUntrustedKeypressEvents`
     is irrelevant (it only concerns synthetic events).
  2. Contribute no-op keybindings with `when: activeWebviewPanelId == 'kiwrap.kicad'` (or
     `activeCustomEditorId == …`) for the shortcuts that must reach KiCad (Ctrl+P, Ctrl+Shift+P,
     F1, Ctrl+W, Ctrl+Tab, Ctrl+B, Ctrl+E, Ctrl+G, Ctrl+K chords, Ctrl+`, Escape is safe).
     Extension keybindings take precedence over defaults; the page still receives every key.
  3. Do **not** enable `enableFindWidget`: it adds Ctrl+F/F3/Escape bindings scoped to the
     webview (`webviewFindWidgetEnabled`) that fight the app's Ctrl+F (KiCad "Find").
  4. `workbench.action.toggleKeybindingsLog` (Developer: Toggle Keyboard Shortcuts
     Troubleshooting) shows exactly which binding consumed a key — the tool for the spike.
  5. Simple Browser ships a "focus lock indicator" (`simpleBrowser.focusLockIndicator.enabled`)
     purely to warn users that VS Code shortcuts stop working while the iframe has focus — an
     acknowledged UX cost of route (1).

### 1.3 Pointer, wheel, DPI

- Mouse move/click/drag/wheel arrive as ordinary DOM events; noVNC maps them to RFB
  PointerEvent, Xpra-html5 to its own packets, WebRTC clients to data-channel messages. Middle-
  drag pan works (button 2), horizontal wheel works, `wheel` deltas are normalised by each client.
  Pointer lock is allowed but not needed for KiCad.
- `window.devicePixelRatio` inside a webview follows VS Code's zoom level × OS scaling. Remote
  framebuffers are produced at the *server's* DPI: for a crisp 1:1 render the virtual display's
  DPI/scale (`GDK_SCALE`, `Xft.dpi`, Xvfb `-dpi`, `KICAD`'s own "canvas scale") must be set to
  match, and the client must not rescale (noVNC `scaleViewport=false` + remote resize
  `resizeSession=true` lets the server framebuffer follow the panel size). Windows mirroring
  captures physical pixels of a per-monitor-DPI-aware KiCad window.

---

## 2. Route catalogue

Each route is described as it would be used for KiCad: which process runs where, how pixels get
to the webview, how input gets back, and the ten criteria the task asks for (fidelity, input,
latency, HiDPI, multi-window, clipboard, CPU/GPU, platforms, licence, effort).

### 2.1 VNC over a virtual X display (WSL2 / Linux): x11vnc or TigerVNC/TurboVNC + websockify + noVNC

**Shape.** In WSL2 (Ubuntu 24.04, KiCad 10.0.6 from the PPA is already installed
`[VERIFIED LOCALLY]`), start a virtual X server sized to the panel — `Xvfb :7 -screen 0 WxHx24`
+ `x11vnc -display :7`, or TigerVNC/TurboVNC `Xvnc :7` (Xvnc *is* the VNC server, no x11vnc
needed, supports client-driven resize via RFB DesktopSize/ExtendedDesktopSize) — a minimal window
manager (openbox/xfwm4/matchbox) so KiCad's modal dialogs stack and focus correctly, then
`pcbnew board.kicad_pcb` with the kiwrap sandbox env. `websockify` (or TigerVNC/KasmVNC/x11vnc
built-in WebSocket support: x11vnc has `-http`/`-httpport`; TurboVNC ≥3 bundles a noVNC "Web
Viewer") bridges RFB to `ws://localhost:P`; the extension embeds noVNC's `RFB` class in the
webview (google/vscode-vnc shows the whole pattern in ~600 lines, BSD-3, archived 2025-04).

- **Fidelity:** the real KiCad (wxGTK3) UI, every menu/dialog/tooltip/context menu, because the
  VNC server sees the *whole* virtual screen (popups are X windows on that screen). GAL OpenGL
  canvas renders through Mesa; with `GALLIUM_DRIVER=d3d12 MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA`
  it is GPU-accelerated on the RTX 5080 *even under Xvfb* `[VERIFIED LOCALLY]` (§6); default is
  llvmpipe (CPU, still OpenGL 4.5 — KiCad's GAL works on llvmpipe, slower). Antialiasing/
  subpixel text depends on the X DPI settings. A window manager's title bars appear inside the
  panel (can be avoided by running KiCad maximised/undecorated: `openbox` with a `<decor>no</decor>`
  rule, or no WM plus `wmctrl`).
- **Input:** complete. x11vnc/Xvnc inject via XTEST, so modifiers, key repeat, Ctrl+Z, Delete,
  Esc, R, Shift+drag, middle-drag pan, wheel zoom all behave exactly as native; noVNC sends
  `event.code`-based keysyms (QEMU extended key events) so layouts survive. Only the VS Code
  shortcut collisions of §1.2 remain.
- **Latency / frame rate:** loopback RFB with Tight/ZRLE encoding: typically 1–2 frames of
  capture + encode; x11vnc polls the framebuffer (`-defer`/`-wait`, ~30 fps realistic, spikes on
  large canvas redraws); Xvnc (TigerVNC/TurboVNC) pushes damage-driven updates and TurboVNC's
  libjpeg-turbo pipeline is designed for 3D apps (60 fps at 1080p locally). No video codec, so
  large zoom/pan redraws cost bandwidth (irrelevant on loopback) and CPU for JPEG/zlib.
  noVNC decodes in JS on the main thread (Tight JPEG via `createImageBitmap`); a 2560×1440
  panel at 30 fps is fine on this CPU.
- **HiDPI:** server-side DPI; set `Xvfb -dpi 192`/`GDK_SCALE=2` to match VS Code's zoom; noVNC
  `scaleViewport` off for 1:1, `resizeSession` on for server-follows-panel (RFB SetDesktopSize —
  supported by Xvnc; for Xvfb use `xrandr` with a preconfigured mode list or restart).
- **Multi-window:** every KiCad window (footprint chooser, DRC dialog, 3D viewer, preferences)
  appears inside the virtual screen; a WM is required for modality/focus. Windows cannot leave
  the panel (no seamless mode) — that is the UX cost relative to Xpra (§2.2).
- **Clipboard:** RFB ClientCutText/extended clipboard = text only; x11vnc/Xvnc sync the X
  CLIPBOARD selection, noVNC exposes `clipboardPasteFrom()`/`clipboard` event; bridge to
  `vscode.env.clipboard` in the extension host. KiCad's own copy/paste of board items works
  inside the session; pasting KiCad clipboard text (s-expression) into a VS Code editor works via
  the text bridge; images/files do not.
- **CPU/GPU:** KiCad + Mesa d3d12 (GPU) or llvmpipe (CPU, 2–4 cores busy while panning);
  VNC encode ~1 core at 30 fps; noVNC decode <1 core. Memory: WSL VM (already running here).
- **Platforms:** Windows host via WSL2 (requires WSL + Linux KiCad, an extra install kiwrap must
  provision), Linux native (trivial), macOS via a Linux VM only. Remote (SSH) for free.
- **Licensing:** noVNC MPL-2.0 (SPDX shows NOASSERTION in the API because of the mixed
  licence file; core is MPL-2.0), websockify LGPL-3.0, x11vnc GPL-2.0, TigerVNC/TurboVNC GPL-2.0,
  Xvfb MIT, KiCad GPL-3.0 — all separate processes; noVNC would be bundled into the extension
  (MPL is file-level copyleft, fine for an MIT extension).
- **Effort to productize:** low–medium. Extension: WebSocket bridge (or `websockify`), noVNC
  webview, session supervisor (start Xvnc/KiCad in WSL with the sandbox env, health checks,
  reconnect), keybinding shield (§1.2), DPI/resize plumbing, clipboard bridge, "Open in native
  KiCad" escape hatch. Provisioning WSL + KiCad + Xvnc is the biggest product cost.
- **Verdict:** the most robust "identical UI" route. **Spike it first.**

### 2.2 Xpra (server in WSL2 / Linux, HTML5 client in the webview)

**Shape.** `xpra seamless :100 --start="pcbnew …" --bind-tcp=127.0.0.1:14500 --html=on` in WSL;
the webview iframes `http://localhost:14500/?…` (or loads the xpra-html5 client files locally and
connects over WebSocket). Each X11 top-level window becomes its own canvas/div in the client
("seamless"); dialogs, menus and tooltips are forwarded as separate windows with the correct
transient/override-redirect hints. Xpra also has `desktop` (one virtual screen) and `shadow`
modes.

- **Fidelity:** real KiCad; popups are forwarded as separate override-redirect windows and drawn
  by the HTML5 client. Xpra's HTML5 client draws its own window decorations for top-levels
  (movable within the page, can be maximised to fill the panel). GL: Xpra's own virtual
  display (Xvfb/Xdummy) is where KiCad renders; OpenGL docs recommend VirtualGL (`vglrun`)
  for GPU, otherwise llvmpipe — in WSL, Mesa d3d12 gives GPU GL directly under Xvfb (§6), so
  VirtualGL is unnecessary. Xpra "video regions" auto-switch the canvas area to H.264/VP8/WebP
  when it changes quickly; fine detail (schematic text) is sent lossless after the region
  settles. Seamless mode "is not available on MS Windows and MacOS" servers.
- **Input:** XTEST-based on the server; the HTML5 client has keyboard-layout handling (`?
  keyboard_layout=`), clipboard, wheel, and pointer events. Same VS Code-shortcut caveats.
- **Latency:** adaptive (bandwidth/latency-aware batching); on loopback comparable to VNC, with
  a stronger encoder pipeline (rgb/webp/jpeg/png/h264 via WebCodecs or JS decoders, decoding in a
  worker). Xpra's own docs warn shadow mode "can use more CPU on both computers"; seamless is the
  performant mode.
- **HiDPI:** server DPI is settable (`--dpi`); the HTML5 client has `scaling` options and
  reports `devicePixelRatio`.
- **Multi-window:** the best of all routes — each KiCad window is an independent, movable,
  minimisable HTML window; modality hints are honoured.
- **Clipboard:** text both ways (and images with limits) in the HTML5 client.
- **CPU/GPU:** Xpra server (Python + Cython codecs) is heavier than Xvnc: ~1–2 cores during
  animation; memory ~200 MB.
- **Platforms:** Linux/WSL server; Windows server only as *shadow* (mirror of the real desktop —
  no window-only mode documented, `docs/Usage/Shadow.md`), so Windows-native KiCad gains nothing.
- **Licensing:** xpra GPL-2.0 (server, separate process), xpra-html5 MPL-2.0 (client, can be
  bundled).
- **Effort:** medium. More moving parts than VNC (Python server with many optional codecs; WSL
  packaging), but the seamless-window UX inside the panel is unmatched. Version skew between
  server and html5 client matters.
- **Verdict:** **second spike**, specifically to compare popup/dialog UX and text sharpness
  against noVNC.

### 2.3 Windows-native KiCad, mirrored: TightVNC `-sharewindow`/`-shareapp`, UltraVNC single window, Windows.Graphics.Capture + WebRTC

**Shape.** KiCad (`pcbnew.exe` 10.0.6 or 10.99) runs on the user's real Windows desktop; a
capture agent mirrors its window into the webview and injects input back.

- TightVNC 2.7 `tvnserver.exe -run` (application mode, no service) then `tvnserver.exe
  -controlapp -sharewindow CAPTION | -shareapp PID` (`-sharerect`, `-sharedisplay` also exist;
  "no analogues in graphical user interface"). Documented semantics: `-sharewindow` shares "the
  first window with the caption that matches… Other windows placed above shared one also become
  visible… If the window… has been minimized, a viewer will receive a screen with zero
  dimensions"; `-shareapp` shares "all windows of the application with the specified PID. The
  size of the shared screen area is equal to the size of virtual desktop… all regions that are not
  relevant to the application… will be darkened." I.e. both are *screen-region* crops of the live
  desktop: the KiCad window must be on-screen (not minimised, not on another virtual desktop).
  Client: noVNC via websockify, exactly as §2.1. GPL-2.0.
  - Source: TightVNC 2.7 for Windows: Server Command-Line Options (PDF), extracted text in the
    survey scratchpad.
- UltraVNC "Single Window" / "Share only the Server Window Named" — same model, GPL-2.0.
- Windows.Graphics.Capture (WGC, Win10 1903+): `GraphicsCaptureItem` from an HWND; captures
  the DWM-composed window content even when occluded (not when minimised); yellow border can be
  disabled (`IsBorderRequired`, build ≥20348) and cursor toggled. **Per-window capture only
  includes that HWND's own surface** — popup menus, tooltips, combo dropdowns and wx's
  `wxPopupWindow`s are separate top-level HWNDs and are *not* in the frame; a VNC-style screen
  region capture does include them (that is why TightVNC says "other windows placed above… also
  become visible"). Building blocks: `windows-capture` (Rust + Python bindings, MIT, active
  2026-09-18), aiortc (BSD-3) or webrtc-rs (Apache-2.0) to encode H.264 (NVENC via ffmpeg/
  GStreamer) and stream to the webview over WebRTC; input via a data channel + `SendInput`
  (requires the target to be the foreground window — which pulls KiCad above VS Code) or
  `PostMessage(WM_MOUSE*/WM_KEY*)` to the child HWND (works for wx's message-based handling, but
  wx reads modifier state with `GetKeyState`, so Shift/Ctrl-drag need real modifier injection).
  - Sources: https://learn.microsoft.com/windows/uwp/audio-video-camera/screen-capture,
    https://github.com/NiiightmareXD/windows-capture
- Sunshine (GPL-3.0) captures *displays* (DXGI duplication / NVFBC), not windows; it would need a
  virtual display (e.g. the "Virtual Display Driver" IddSampleDriver-based projects) and a
  browser Moonlight client (third-party `moonlight-web`, GPL-3.0, 2026-09) — the streaming
  quality is excellent (NVENC, <1 frame) but KiCad would live on an invisible extra monitor,
  which Windows still treats as part of the desktop (mouse can wander onto it).

- **Fidelity:** pixel-identical wxMSW KiCad, but per-window capture drops popups (WGC) and
  region capture shows whatever overlaps (TightVNC); the window is *also* on the real desktop.
- **Input:** partial — foreground/z-order fights, modifier injection is global (affects the
  user's real keyboard state), focus flips between VS Code and KiCad.
- **Latency:** TightVNC hook-driven capture on loopback ~30 fps; WGC+NVENC+WebRTC 60 fps, ~1–2
  frames.
- **HiDPI:** physical pixels of a per-monitor-aware window (good).
- **Multi-window:** `-shareapp` (all windows of the PID, darkened desktop around them) is the
  only variant that keeps dialogs; WGC needs one capture item per HWND plus compositing.
- **Clipboard:** native (same session) — trivially perfect.
- **CPU/GPU:** low (native KiCad on the NVIDIA GPU) + encoder.
- **Platforms:** Windows only.
- **Licensing:** TightVNC/UltraVNC GPL-2.0 separate process; windows-capture MIT; aiortc BSD-3.
- **Effort:** TightVNC demo: low (an afternoon). WGC/WebRTC product: high (capture, NVENC,
  WebRTC signalling, input injection edge cases, popup compositing) and still UX-broken.
- **Verdict:** worth **one short spike with TightVNC `-shareapp`** to measure and to *show the
  owner why it is not it*; not a product path.

### 2.4 WebRTC desktop streamers (Linux/containers): Selkies, n.eko, KasmVNC, Kasm Workspaces

- **Selkies** (MPL-2.0, active 2026-09-19): X11 desktop → GStreamer (NVENC/VAAPI/x264) → WebRTC
  to a browser; "at least 60+ FPS at Full HD with an NVIDIA GPU"; designed around Xvfb and
  containers; ships as a Python package plus a JS/HTML client. In WSL: `/usr/lib/wsl/lib/
  libnvidia-encode.so` is present `[VERIFIED LOCALLY]`, so NVENC inside WSL is plausible (needs the
  CUDA/NVENC-capable GStreamer plugin; untested). Input via a WebRTC data channel + XTEST (uinput).
  Fidelity/multi-window identical to §2.1 (whole virtual screen). Latency: best of the Linux
  class (video codec, hardware). Cost: more setup than VNC (GStreamer, TURN/STUN not needed on
  loopback, but a signalling server is).
- **n.eko** (Apache-2.0, 22k stars): a Docker image runs Xorg-dummy + the app + GStreamer +
  WebRTC (pion) with a Vue client; "run applications" beyond browsers is supported via custom
  images (`m1k1o/neko-apps`). Requires Docker (on Windows → Docker Desktop on WSL2 → extra VM
  layer); GPU images exist (nvidia). Strong client (clipboard, keyboard layouts, multi-user).
  Latency similar to Selkies. Effort: medium; the KiCad container would have to see the
  workspace files (bind mounts) and the sandbox config.
- **KasmVNC** (GPL-2.0, active): a TigerVNC-derived Xvnc with a built-in web client (no
  websockify), WebSocket transport, "WebCodecs video streaming with H.264, H.265 and AV1",
  "DRI3 GPU acceleration with open source drivers" (AMD/Intel/ARM — not the NVIDIA proprietary
  path, but Mesa d3d12 in WSL is an open driver). It is the highest-quality *VNC-family* server
  and drops the JS-decode bottleneck. Kasm Workspaces' "app streaming" = a container per app in
  kiosk layout — the same virtual-screen model, so nothing new for fidelity.
- **Sunshine/Moonlight:** see §2.3; on Linux Sunshine captures KMS/X11 displays; there is no
  official browser client, only GPL third-party `moonlight-web` bridges.
- **Verdict:** KasmVNC and Selkies are the *upgrade path* of §2.1 when frame rate or decode CPU
  become the bottleneck; same architecture, better codec. Try KasmVNC in the §2.1 spike if noVNC
  JPEG decoding shows in the profile.

### 2.5 RDP in HTML5 (Guacamole, Myrtille, FreeRDP-WebConnect) and Weston's RDP backend

- **Apache Guacamole** (Apache-2.0, active): `guacd` (Linux only — runs in WSL/Docker) speaks RDP
  (FreeRDP), VNC and SSH; HTML5 client `guacamole-common-js`. Supports **RDP RemoteApp** (`remote-app`
  connection parameter) which would show a single Windows app… but the app has to run in an RDP
  *session*: on Windows 11 Pro a loopback RDP logon takes over the console (one interactive
  session), RemoteApp publishing needs the `TSAppAllowList` registry hack, and the RDP host cannot
  be this same desktop. Guacamole-over-VNC is just §2.1 with a heavier middle layer.
- **Myrtille** (Apache-2.0, last push 2024-03) and **FreeRDP-WebConnect** (Apache-2.0, archived
  2022) are Windows-hosted RDP→HTML5 gateways with the same session limitation.
- **Weston RDP backend:** Weston can act as an RDP server (`--backend=rdp`); WSLg *is* a Weston
  with a Microsoft RAIL/VAIL-extended RDP backend that mstsc.exe consumes to draw Linux windows
  on the Windows desktop. A second Weston instance in WSL (`weston --backend=rdp --rdp-tls-…`)
  could host KiCad (Xwayland) and be reached by Guacamole's RDP client — a legitimate but
  heavyweight variant of the virtual-display idea with no advantage over Xvnc/Xpra; RAIL
  (seamless) is not supported by Guacamole's client.
- **Verdict:** not worth a spike; note as fallback for environments that already run Guacamole.

### 2.6 GTK Broadway

- **[high]** GDK's Broadway backend renders GTK3/4 apps into a browser over WebSocket. It has
  **no OpenGL**: `gdk_window_create_gl_context` fails with `GDK_GL_ERROR_NOT_AVAILABLE` when the
  backend implements no `create_gl_context` (`gdk/gdkwindow.c` 2859–2862; the Broadway
  `gdkwindow-broadway.c` contains no `gl_context` implementation `[VERIFIED by grep of GTK
  3.24 sources]`). wxWidgets' GTK `wxGLCanvas` refuses anything but X11/Wayland: "wxGLCanvas is
  only supported on Wayland and X11 currently… setting environment variable GDK_BACKEND=x11"
  (`src/gtk/glcanvas.cpp` `IsAvailable()`), so KiCad's GAL cannot even fall back to Cairo through
  the normal path — the `wxSafeShowMessage` fires before KiCad's OpenGL test. Broadway is also
  deprecated together with X11 in GTK 4.x for removal in GTK 5 (GNOME announcement, Feb 2025),
  and never left "experimental".
- **Verdict:** **no** (documentary). Only a KiCad fork that adds a Cairo-only, GL-free canvas
  path could use it, and Broadway's input/clipboard/popup handling is weak anyway.

### 2.7 KiCad compiled to WebAssembly — PCBJam (`PCBJam/pcbjam`, GPL-3.0)

**What exists.** "The real KiCad, compiled to WebAssembly and running in your browser. Not a
viewer, not a rewrite: the schematic editor, the PCB editor, DRC, the 3D viewer and Gerber
export" (pcbjam.com; hosted SaaS, free tier + $10/mo Pro; "[ ] self-hosting" still on the
roadmap). Source: `PCBJam/pcbjam` (created 2025-11-28, pushed 2026-09-18, 44 stars) with
submodules `PCBJam/kicad-source-mirror` (branch `wasm-port`) and `PCBJam/wxWidgets`
(`wasm-port`, derived from `ahilss/wxWidgets-wasm`, LGPL-2 files). Docker build "~1–2 hours
full", 16 GB RAM; outputs `pcbnew.js/.wasm` (+ eeschema, symbol_editor, gerbview, pl_editor,
calculator — "All six KiCad apps build and run"). Browser support: Chrome/Edge/Firefox; Safari
waits for JSPI.

**How the UI is rendered.** The wx "DOM port": "widgets are real HTML elements (wxButton →
`<button>`, wxTextCtrl → `<input>`, menus → popup divs) instead of pixels on a canvas"; "C++ owns
state; the DOM is a projection"; layout is wx sizers with absolute positioning; owner-drawn
widgets (wxGrid, listctrl, tree, AUI, STC) paint into per-window canvases; "Menus/toolbars
serialize C++→JS as JSON; popups are DOM divs"; modal dialogs run on a JSPI/Asyncify coroutine
runtime; the GAL renders through a **WebGL GAL** (`kicad/common/gal/webgl/`) with a visual-
regression suite against native OpenGL. Threads: Emscripten pthreads (SharedArrayBuffer) are
compiled in — KiCad's thread pool spawns 16 Workers — although "effectively nothing runs
multithreaded" today.

- **Fidelity:** the real KiCad *logic, layouts, shortcuts and dialogs*, drawn with HTML widgets:
  not pixel-identical to wxMSW/wxGTK, fonts/menus/tooltips look "web". Canvas rendering is the
  KiCad GAL algorithms on WebGL (their gal-regression suite exists precisely because it is not
  bit-identical). 3D viewer works (OCC/raytracer ported).
- **Input:** native DOM events → wx; keyboard "goes untouched to focused DOM editables (Escape
  excepted)"; no remote-display latency at all.
- **Latency:** zero transport latency; WASM single-core execution speed (JSPI overhead on every
  modal/yield); large boards may be slower than native.
- **HiDPI:** native browser DPR (best possible).
- **Multi-window:** dialogs are DOM overlays inside the page (cannot leave the panel).
- **Clipboard:** browser clipboard (available in webviews, §1.1).
- **CPU/GPU:** WebGL on the host GPU; CPU-bound WASM otherwise.
- **Platforms:** anywhere VS Code runs — including vscode.dev — the only route with that
  property.
- **Licensing:** GPL-3.0 for the whole WASM bundle (KiCad) + LGPL-2 wasm-port files; kiwrap
  (MIT) would treat it like Freerouting: downloaded at bootstrap, loaded in its own webview,
  never linked (D8).
- **Blocking constraints for VS Code:** (a) SharedArrayBuffer → the workbench must run with
  `--enable-coi` (§1.1) or the bundle must be rebuilt without pthreads (their own docs say the
  pool is serial today, so a `-sPTHREAD_POOL_SIZE=0`/no-`-pthread` build is conceivable but is a
  fork-of-a-fork); (b) the artifacts must be served **same-origin** with the page under COEP
  (`web/README.md`), which in a webview means `asWebviewUri` resources (same `vscode-webview://`
  origin) — plausible but unverified; (c) files: the WASM FS is not the workspace — every open/
  save must be synchronised through postMessage (`workspace.fs`), and KiCad's global symbol/
  footprint libraries (hundreds of MB) must be mounted into the WASM FS (PCBJam ships "a curated
  slice"); (d) **version skew**: it is a fork of a specific KiCad revision, not the installed
  10.0.6/10.99 — kiwrap's file-format/version isolation (D3/D16) and the IPC backend do not
  apply; (e) maturity: 44-star single-vendor project, no self-hosting release yet.
- **Effort:** low to *try* (iframe `https://app.pcbjam.com` in a webview — but that is their
  hosted service, files leave the machine, not acceptable for kiwrap); high to *productize*
  (build pipeline, COI, FS bridge, library provisioning, tracking the fork).
- **Verdict:** the most exciting prior art and the only "no remote display" route. **Spike
  scoped to feasibility questions only**: does a self-built or hosted bundle load in a webview
  with/without `--enable-coi`, does WebGL GAL render, how big is the bundle. Do not build a
  product on it until PCBJam ships self-hosting and the fork tracks upstream.

### 2.8 For contrast: re-implementations (KiCanvas, ecad-viewer) and kicad-cli renders

Not "identical UI" by definition (viewer-only, alpha, KiCad 9/10 gaps — prior-art digest). They
remain the right *preview* for kiwrap's data-first design (D17) and are unaffected by this survey.
KiCad itself has no web/remote UI plans; wxWidgets has no web backend (wxQt + Qt-for-WASM is
theoretical, nobody has done it; the only wx-in-browser work is the ahilss/PCBJam WASM port).

---

## 3. Prior art of exactly this

- **google/vscode-vnc** (BSD-3, archived 2025-04-30, extracted from ChromiumIDE): noVNC `RFB`
  in a `WebviewPanel` (`retainContextWhenHidden: true`), a `ws` WebSocket→TCP proxy in the
  extension host (`src/proxy.ts`), `asExternalUri` for remote hosts, and a *postMessage socket*
  fallback for vscode.dev. **This is the template for §2.1.**
- **Remote VNC** (OlehPlotnikov.remote-vnc) and **vnc-extension** (king2021) on the Marketplace:
  same pattern, third-party.
- **Containerlab VS Code extension** (Apache-2.0, active): "Wireshark capture is now by default
  via noVNC" — an app-in-webview via noVNC in a shipping extension.
- **jupyter-remote-desktop-proxy** (BSD-3, active 2026-09): TigerVNC/TurboVNC + websockify +
  noVNC in a JupyterLab tab — the canonical "desktop app inside an IDE tab" deployment; its
  operational lessons (Xvnc resize, DPI, clipboard, WM choice) transfer 1:1.
- **JetBrains Projector** (Apache-2.0/MIT, archived 2024-01): rendered Swing IDEs in a browser by
  intercepting Java2D drawing commands — a *toolkit-level* remote UI, which is what Broadway is
  for GTK and what does not exist for wxWidgets. JetBrains deprecated it in favour of Gateway
  (thin native client + remote backend), citing latency/fidelity limits of the approach.
  **Fleet** is a native client, not a browser UI.
- **VS Code Remote-X11** (joelspadin, 41 stars): only sets `DISPLAY` for SSH X-forwarding; no
  embedding. Searches for "xpra vscode"/"x11 vscode webview" find no maintained extension that
  embeds Xpra; the Xpra HTML5 client in a webview would be new work (small).
- **PlatformIO / other native-tool integrations:** launch external windows; none embed.
- **KiCad-specific:** KiCanvas (MIT, alpha), Huaqiu ecad-viewer fork, KiCad Studio Kit (custom
  editors with KiCanvas + "Open in KiCad"), PCBJam (§2.7). No upstream KiCad discussion of a
  remote/web UI beyond KiCanvas being listed as an external tool.

---

## 4. Criteria matrix

Scores: 3 = as good as native, 2 = acceptable with known limits, 1 = poor, 0 = blocked.
"V" = verified locally, "D" = documentary, "?" = to measure in a spike.

| Route | Real KiCad UI | Popups/dialogs captured | GL canvas | Input completeness | Loopback latency | HiDPI | Multi-window UX | Clipboard | CPU/GPU cost | Win host | Linux | macOS | Licence fit (MIT ext.) | Productize effort | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2.1 WSL Xvnc/x11vnc + noVNC | 3 | 3 (whole virtual screen) | 3 (GPU via d3d12; llvmpipe fallback) | 3 (XTEST) | 2 (30–60 fps, JS decode) | 2 (server DPI must match) | 2 (inside panel, WM needed) | 2 (text) | 2 | via WSL | 3 | VM only | 3 (separate processes; noVNC MPL) | low–med | V (GL), D |
| 2.2 WSL Xpra + xpra-html5 | 3 | 3 (forwarded windows) | 3 | 3 | 2 (adaptive, video regions) | 2 | 3 (seamless windows in panel) | 2–3 | 2 | via WSL | 3 | VM only | 3 | med | D |
| 2.4 KasmVNC / Selkies (WSL) | 3 | 3 | 3 | 3 | 3 (H.264/AV1, NVENC) | 2 | 2 | 2 | 2 (encoder) | via WSL | 3 | VM only | 3 (GPL/MPL separate) | med | D |
| 2.4 n.eko (Docker) | 3 | 3 | 3 (GPU images) | 3 | 3 | 2 | 2 | 3 | 1 (Docker VM + VM) | via Docker Desktop | 3 | Docker | 3 (Apache) | med–high | D |
| 2.3 TightVNC `-shareapp` (Windows native) | 3 | 2 (region-based; darkened desktop) | 3 (native) | 1 (window must be on-screen/foreground) | 2 | 3 | 1 (window also on real desktop) | 3 | 3 | 3 | 0 | 0 | 3 (GPL separate) | low (demo) | D |
| 2.3 WGC + WebRTC (custom) | 3 | 1 (per-HWND; menus missing) | 3 | 1 (SendInput/PostMessage fights) | 3 | 3 | 1 | 3 | 3 | 3 | 0 | 0 | 3 (MIT/BSD) | high | D |
| 2.3 Sunshine + virtual display + moonlight-web | 3 | 3 (whole virtual monitor) | 3 | 2 (extra monitor is part of the desktop) | 3 | 3 | 2 | 3 | 3 | 3 (driver install) | 2 | 0 | 2 (GPL client must be separate) | high | D |
| 2.5 Guacamole RDP/VNC | 3 | 3 (VNC) / RemoteApp | 3 | 3 | 2 | 2 | 2 | 2 | 1 | needs second RDP session (blocked on Pro) | 3 | 0 | 3 (Apache) | med–high | D |
| 2.6 GTK Broadway | 3 (widgets) | 2 | **0** (wxGLCanvas aborts) | 2 | 2 | 2 | 2 | 1 | 2 | 0 | 0 (blocked) | 0 | 3 (LGPL) | n/a | D (source) |
| 2.7 KiCad WASM (PCBJam) | 2 (real code, HTML widgets) | 2 (DOM overlays) | 2 (WebGL GAL) | 3 | 3 (none) | 3 | 2 | 3 | 2 | 3 (with `--enable-coi`) | 3 | 3 | 2 (GPL bundle, separate download) | high | D |
| 2.8 KiCanvas re-implementation | 0 (viewer) | 0 | n/a | 1 | 3 | 3 | n/a | n/a | 3 | 3 | 3 | 3 | 3 (MIT) | n/a | prior digest |

---

## 5. Ranking and which routes get hands-on spikes

1. **WSL2 virtual X display + VNC (TigerVNC/TurboVNC `Xvnc` or `Xvfb`+`x11vnc`) + noVNC in a
   custom-editor webview — SPIKE (primary).** Cleanest path to "identical UI": whole-screen
   capture keeps every popup, XTEST keeps every key/modifier, GPU GL is verified in WSL, all
   components are mature, separately-licensed processes, and google/vscode-vnc is a working
   template for the extension side. Measure: fps/latency with a timestamp overlay (`xclock`-style
   or KiCad status bar), menu/context-menu/tooltip capture, footprint chooser, Ctrl+Z/Delete/
   Esc/R/Shift-drag, wheel zoom + middle-drag pan, clipboard text bridge, DPI at 100/150/200 %,
   the VS Code shortcut shield (§1.2 options 1 and 2, checked with the keybindings log), CPU per
   process. Also try KasmVNC as a drop-in server for the codec upgrade.
2. **WSL2 Xpra seamless + xpra-html5 — SPIKE (secondary).** Same fidelity, better multi-window
   UX (floating dialogs, movable windows inside the panel), built-in HTML5 client with clipboard/
   DPI/layout handling. Compare latency, sharpness of schematic text, CPU against (1).
3. **Windows-native mirror with TightVNC `-run` + `-shareapp PID` + noVNC — SHORT SPIKE (to
   quantify, not to ship).** Zero extra runtime (no WSL), native GPU, but the window lives on the
   desktop and input/focus is compromised; document the exact failure modes with screenshots so
   the decision is evidence-based.
4. **KiCad-in-WebAssembly (PCBJam) — FEASIBILITY SPIKE, time-boxed.** Questions: does a bundle
   load in a webview (self-built from `PCBJam/pcbjam`, or the hosted app iframed *for the test
   only*), with and without `--enable-coi`; WebGL GAL rendering; bundle size and load time; what
   the DOM-projected UI looks like next to native KiCad. Product decision deferred until
   self-hosting exists and the fork's upstream distance is known.
5. **WebRTC streamers (Selkies, n.eko)** — not spiked now; they are the upgrade path of (1)
   when codec/decode cost matters. **Guacamole/Myrtille/Weston-RDP** — not spiked (session
   model blocked on Windows Pro; no advantage on Linux). **WGC + WebRTC custom** — not spiked
   (high effort, same UX defects as 3). **GTK Broadway** — rejected on source evidence.

Product implications for kiwrap (ARCHITECTURE §9 "Live-KiCad panel"): the panel becomes a
*display session* abstraction with two providers — `wsl-vnc` (routes 1/2) and `native-mirror`
(route 3, demo/opt-in) — sharing one webview client; the extension keeps the KiCad process under
the existing sandbox rules (`ConfigRoot.env()`, one GUI at a time, kill by PID) and the IPC/`kw_*`
tools keep talking to the same process (KiCad 11 IPC over a Unix socket in WSL, or the named pipe
on Windows). Provisioning WSL + Linux KiCad is the cost that must be made explicit in the
roadmap; Linux hosts get the same design for free.

---

## 6. Local verification log (no GUI launched)

- WSL2 Ubuntu 24.04.3, kernel 6.18.33.2-microsoft-standard-WSL2, WSLg 1.0.73.2
  (`DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`, `/dev/dxg` present, `nvidia-smi -L` → RTX 5080),
  24 vCPU / 62 GB. Installed: `kicad 10.0.6~ubuntu24.04.1` (+ demos/footprints/symbols/
  templates), `libwxgtk-gl3.2-1t64`, `xvfb 21.1.12`, `x11vnc 0.9.16`, `novnc 1.3.0`,
  `mesa-utils`, `libgl1-mesa-dri 25.2.8`, `broadwayd`; `/usr/lib/wsl/lib` has `libd3d12.so`,
  `libdxcore.so`, `libnvidia-encode.so`, `libcuda.so`.
- `glxinfo -B` default → `llvmpipe (LLVM 20.1.2, 256 bits)`, "Accelerated: no", OpenGL 4.5.
- `GALLIUM_DRIVER=d3d12 glxinfo -B` → `D3D12 (Intel(R) Graphics)`, "Accelerated: yes"
  (3/3 runs); `MESA_LOADER_DRIVER_OVERRIDE=d3d12` alone did **not** switch drivers (3/3 runs
  llvmpipe).
- `GALLIUM_DRIVER=d3d12 MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA glxinfo -B` → `D3D12 (NVIDIA
  GeForce RTX 5080)`, "Accelerated: yes", OpenGL 4.6 core/compat, GLSL 4.60, ES 3.1.
- The same env under `xvfb-run -a glxinfo -B` (no WSLg, pure virtual X) → `D3D12 (NVIDIA GeForce
  RTX 5080)`, "Accelerated: yes". This is the fact that makes routes 2.1/2.2/2.4 GPU-backed.
- x11vnc `-help` (0.9.16): `-id windowid` "New windows like popup menus, transient toplevels,
  etc, may not be seen or may be clipped… x11vnc may crash if the window is initially partially
  obscured, changes size, is iconified"; `-appshare` exists ("every new toplevel window… induces
  a new viewer window via a reverse connection") — confirms whole-display capture is the sane
  mode.
- VS Code 1.138.0 (`code --version`), `Code.exe` strings: `Chrome/148.0.7778.280 Electron/42.10.0`.
- Scratch evidence files: `<scratchpad>/display/survey/` (`vscode.d.ts`, `pre-index.html`,
  `webviewElement.ts`, `gdkwindow-broadway.c`, `tightvnc-cli.txt`).

---

## 7. Sources

VS Code: https://code.visualstudio.com/api/extension-guides/webview ·
https://code.visualstudio.com/api/extension-guides/custom-editors ·
https://code.visualstudio.com/api/extension-capabilities/overview ·
https://github.com/microsoft/vscode/blob/main/src/vscode-dts/vscode.d.ts ·
https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/webview/browser/pre/index.html ·
https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/webview/browser/webviewElement.ts ·
https://github.com/microsoft/vscode/blob/main/src/vs/base/common/network.ts ·
https://github.com/microsoft/vscode/blob/main/src/vs/platform/environment/node/argv.ts ·
https://github.com/microsoft/vscode/tree/main/extensions/simple-browser ·
issues #85879, #82592, #116897, #139163, #188669, #129178, #319593, #320435 ·
https://github.com/electron/electron/issues/14258.
Remote display: https://github.com/novnc/noVNC · https://github.com/novnc/websockify ·
https://www.tightvnc.com/doc/win/TightVNC_2.7_for_Windows_Server_Command-Line_Options.pdf ·
https://github.com/LibVNC/x11vnc · https://github.com/TigerVNC/tigervnc ·
https://github.com/TurboVNC/turbovnc · https://github.com/VirtualGL/virtualgl ·
https://github.com/Xpra-org/xpra (docs/Usage/Seamless.md, Shadow.md, OpenGL.md) ·
https://github.com/Xpra-org/xpra-html5 · https://github.com/kasmtech/KasmVNC ·
https://github.com/selkies-project/selkies · https://github.com/m1k1o/neko ·
https://github.com/LizardByte/Sunshine · https://github.com/moonlight-stream/moonlight-qt ·
https://github.com/linckosz/moonlight-web · https://github.com/apache/guacamole-server ·
https://github.com/myrtille-rdp/myrtille · https://github.com/FreeRDP/FreeRDP-WebConnect ·
https://learn.microsoft.com/windows/uwp/audio-video-camera/screen-capture ·
https://github.com/NiiightmareXD/windows-capture · https://github.com/aiortc/aiortc ·
https://github.com/webrtc-rs/webrtc.
Toolkits: https://gitlab.gnome.org/GNOME/gtk (gtk-3-24 `gdk/gdkwindow.c`, `gdk/broadway/`) ·
https://github.com/wxWidgets/wxWidgets/blob/master/src/gtk/glcanvas.cpp ·
GTK 5 deprecation news (Feb 2025).
Prior art: https://github.com/google/vscode-vnc · https://marketplace.visualstudio.com/items?itemName=OlehPlotnikov.remote-vnc ·
https://github.com/srl-labs/vscode-containerlab · https://github.com/jupyterhub/jupyter-remote-desktop-proxy ·
https://github.com/JetBrains/projector-server · https://github.com/joelspadin/vscode-remote-x11 ·
https://github.com/theacodes/kicanvas · https://github.com/PCBJam/pcbjam · https://www.pcbjam.com/ ·
https://github.com/ahilss/wxWidgets-wasm.
