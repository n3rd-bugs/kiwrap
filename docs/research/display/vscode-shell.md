# VS Code shell for a remote KiCad display: what a webview can and cannot do

Component: the VS Code side of "KiCad inside VS Code", independent of the display backend (noVNC /
Xpra HTML5 / GTK Broadway / our own frame stream). Everything here was **measured** on
2026-09-19 with a real Extension Development Host (VS Code 1.138.0, Electron 42 / Chromium, Windows 11,
150 % display scaling, isolated `--user-data-dir`) driven by Win32 `SendInput` and window capture.
Prototype: `prototypes/display/vscode-shell/` (run steps in its README). Per-shortcut table:
[`vscode-shell-keys.md`](vscode-shell-keys.md). Screenshots: `img/vscode-shell-*.png`.

Caveat on the measurement: the desktop was shared with other agents' KiCad automation, which grabbed
the foreground every few seconds. The driver re-asserts the foreground before every chord, retries a chord
when nothing arrived, and records the modifier state; runs A/B/C/D below are the clean runs (213-215 of
215 chords observed each). "VS Code acted" is detected as focus leaving the page before the driver's
recovery keystroke, which is a lower bound (e.g. run B `Ctrl+P` opened Quick Open, confirmed by
screenshot, but was not flagged).

## 1. Verdict in one paragraph

A VS Code extension can host a remote KiCad display and the hosting is not the hard part. Both
shapes work: (a) a `WebviewPanel` whose page owns an inline `<canvas>` fed over a WebSocket, and (b) a
webview that iframes a `http://localhost` page (the noVNC / Xpra / Broadway shape). A
`CustomReadonlyEditorProvider` for `*.kicad_pcb` / `*.kicad_sch` gives "open the board = see KiCad"
with the view as a normal editor tab, splittable next to text editors. Keyboard: **every** KiCad
shortcut tested (215 chords: letters, Delete/Backspace/Esc/Tab/arrows/Home/End, digits, numpad,
`Ctrl+Z/Y/X/C/V/S/A/F`, `Ctrl+Shift+Z`, `Ctrl+P`, `Ctrl+Shift+P`, `Ctrl+W`, `Ctrl+Tab`, F1-F12,
`Alt+letters`, `Space`, `Ctrl/Shift/Alt+wheel`, middle button) reaches the page as a DOM event in
all three shapes. What differs is whether VS Code *also* acts on it: with stock VS Code 54 of the
chords trigger a workbench command (and `Ctrl+W` / `Ctrl+F4` close the KiCad view); with one
`contributes.keybindings` block guarded by `activeWebviewPanelId` / `activeCustomEditorId` that drops
to the four things that are not keybindings at all (`Alt`, `Alt+<menu mnemonic>`, `Alt+Space`, the
context-menu key), which are settings, not code. With the cross-origin iframe shape VS Code sees
nothing at all (0 commands fired, nothing rescued, nothing needed). Pointer lock works; element
fullscreen does not; the clipboard works from the webview origin but not from a `localhost` iframe.
DPI is a plain `devicePixelRatio` (1.5 here, 1.8 after one `zoomIn`). The measured floor for an
in-process canvas path is ~0.5 ms input round trip and ~18 ms server-stamp-to-paint per frame, so the
backend, not VS Code, will dominate latency.

## 2. What was built (prototype `prototypes/display/vscode-shell/`)

- `WebviewPanel` "KiCad view" in three modes: `probe` (event logger), `view` (`<iframe>` to a URL,
  default a page served by the extension host over `http://localhost:<port>` declared through
  `webview.options.portMapping`), `canvas` (inline `<canvas>` + WebSocket client to a synthetic
  30 fps RGBA frame server running in the extension host, with an input-echo round-trip probe).
- `CustomReadonlyEditorProvider` (`viewType kiwrapShell.boardEditor`, `priority: default`) for
  `*.kicad_pcb` / `*.kicad_sch`: opens the same page as an editor tab, posts `{type:'open', file}` to
  it (the backend contract), `supportsMultipleEditorsPerDocument: false`.
- 178 rescue keybindings (`tools/gen_keybindings.js`), all `when:
  kiwrapShell.rescueKeys && (activeWebviewPanelId == 'kiwrapShell.view' || activeCustomEditorId ==
  'kiwrapShell.boardEditor')`, bound to a command that logs and `postMessage`s the chord to the page.
- A control channel (`cmd.jsonl` next to the log) so the external driver can run workbench commands
  (`toggleZenMode`, `zoomIn`, `focusFirstEditorGroup`, `reveal`, open files, clipboard read/write).
- `tools/drive_probe.ps1`: Win32 driver (SendInput keyboard + mouse, `GetForegroundWindow` guards,
  `CopyFromScreen` capture, kills the dev host by PID and by `--user-data-dir` at the end).
- `tools/analyze_probe.py`: JSONL -> markdown table.

## 3. Findings, by the questions asked

### (a) Fidelity of the *shell*

- Webviews are ordinary Chromium iframes: any HTML5 display client renders. Nothing in VS Code
  interferes with WebGL/canvas (the canvas mode pushed 1920x1200 RGBA at 22 fps with 0.4 ms
  `putImageData`). What the *user sees* of KiCad is entirely the backend's problem (real GAL canvas,
  menus, dialogs, tooltips): the shell neither adds nor removes fidelity, except:
- **Right-click**: VS Code overlays its own Cut/Copy/Paste menu on any webview right-click, even when
  the page calls `preventDefault()` (`img/vscode-shell-A-rightclick-default-menu.png`). Fix, verified:
  put `data-vscode-context='{"preventDefaultContextMenuItems": true}'` on the container element; the
  `contextmenu` event still reaches the page and no VS Code menu appears
  (`img/vscode-shell-E-rightclick-suppressed.png`). Inside a cross-origin iframe VS Code never sees the
  right-click, so noVNC-style clients are unaffected.
- The view is an editor tab: title, split (`img/vscode-shell-C-split.png`), zen mode
  (`img/vscode-shell-C-zen-mode.png`, page grew to 1582x1370 CSS px) and workbench fullscreen
  (2212x1348 CSS px on the 3840x2160 monitor) all work; the page gets `resize` events each time.

### (b) Input

Run A (page focused, rescue on) / B (rescue off) / C (iframe focused): 214 / 213 / 214 of 215 chords
observed as DOM `keydown` (+`keyup`), always with correct `key`, `code`, modifiers, `location`,
`repeat`. Details that matter for a KiCad forwarder:

- VS Code's webview pre-script calls `preventDefault()` (our listener still runs, browser default is
  gone) on `Ctrl+Z/Y`, `Ctrl+Shift+Z`, `Ctrl+X/C/V`, `Ctrl+S`, `Ctrl+F`, `Ctrl+P`, `Ctrl+Shift+P`,
  `Ctrl+Shift+F/X/C/V/S/Y`, `Ctrl+K Ctrl+S`, `Ctrl+Alt+F/P`. Harmless for forwarding.
- Stock VS Code (run B) acts on 54 chords while the KiCad view is focused. Observed side effects:
  `Ctrl+W` and `Ctrl+F4` **dispose the KiCad panel**, `Ctrl+P`/`Ctrl+E` Quick Open
  (`img/vscode-shell-B-ctrl-shift-p-palette.png` shows the palette on `Ctrl+Shift+P`), `Ctrl+B` /
  `Ctrl+J` / `Ctrl+Shift+E/F/G/D/X/M/H/U/O` toggle side bar / panel / views, `Ctrl+Tab`, `Ctrl+PgUp/PgDn`,
  `Ctrl+1/2/3/0`, `Ctrl+=`/`Ctrl+-`/`Ctrl+numpad +/-` (window zoom), `Ctrl+,`, `Ctrl+K Z` (zen), `F1`,
  `F5`/`Ctrl+F5` (debug), `F6`, `F11` (fullscreen), `Ctrl+Shift+N` (new window), `Ctrl+N`, `Ctrl+O`.
  `Ctrl+Z/Y/X/C/V/S/A` and the letters do **not** trigger anything (no editor focused).
- With the rescue keybindings (run A) all 85 of those chords that are keybindings fire our command
  instead (`img/vscode-shell-A-ctrl-shift-p-rescued.png`: `Ctrl+P`, `Ctrl+Tab`, `Ctrl+PgUp` logged as
  `forwardedKey`, no palette). The page had already received the raw event, so the command only has to
  swallow; forwarding through `postMessage` is only needed for backends that cannot see the raw event
  (a nested iframe), and there it is not needed either because VS Code never resolves those chords.
- Not rescuable by keybindings (custom title bar behaviour, still `acted` in run A): `Alt` alone
  (focuses the menu bar), `Alt+F/E/S/V/G/R/T/H` (menu mnemonics, `img/vscode-shell-A-alt-f-menu.png`),
  `Alt+Space` (system menu), `Apps` key (context menu). Settings: `window.customMenuBarAltFocus: false`,
  `window.enableMenuBarMnemonics: false` (or `window.menuBarVisibility: "compact"`/`"toggle"`). KiCad
  uses `Alt+3` (3D viewer) which is fine; only letters that are menu titles collide.
- `keyboard.dispatch`: default `code` here; irrelevant for the page (which gets the DOM event) but the
  rescue bindings resolve through it, so a `keyCode` layout mismatch on non-US keyboards would only
  affect which chords are swallowed, never what the page sees.
- Inside a cross-origin iframe (run C, `img/vscode-shell-C-iframe-ctrl-shift-p.png`) VS Code sees
  nothing: 0 keybindings resolved, no palette, no menus on `Alt+F`, no `Ctrl+W` close. The price: VS
  Code shortcuts are dead while the frame has focus, and `panel.reveal()` /
  `workbench.action.focusActiveEditorGroup` focus the webview **body**, not the frame, so the host page
  must push focus into the frame on `focus`/`focusin` (added to `media/view.html`, then 215/215).
- Mouse: `pointerdown/up/move` with `buttons` bitmask for left/middle/right drags, `auxclick`,
  `dblclick`, `wheel` (`deltaY` +/-100, `deltaMode` 0) with `ctrlKey`/`shiftKey`/`altKey`; middle-click
  autoscroll is suppressed by `preventDefault` on `mousedown`; no VS Code interference except the
  context menu above and `Alt+wheel` (Alt toggles the menu bar). `pointermove` batches arrived at
  ~10 Hz throttled by the probe, not by VS Code.
- Pointer lock: `requestPointerLock({unadjustedMovement:true})` succeeds in the webview and in the
  iframe (`allow="pointer-lock"`), `movementX/Y` delivered, `pointerlockchange` fires.
- Focus and splits: keys go to the text editor while it is focused (nothing reaches the page);
  `workbench.action.focusFirstEditorGroup`, `focusActiveEditorGroup`, `panel.reveal(col, false)` and a
  click all return focus to the page. `retainContextWhenHidden: true` kept the page alive across
  hide/reveal (`vscode.getState()` counter continued, no reload); with `false` the page is re-created
  on reveal (run B) so a stream client must reconnect and re-request a full frame.

### (c) Latency and frame rate (canvas mode, run D, loopback)

Synthetic raw-RGBA server in the extension host (JS pixel loop, 30 fps target):

| canvas size (device px) | fps | Mbit/s | input RTT median (page -> ext host -> page) | server-stamp -> painted, median | `putImageData` |
|---|---|---|---|---|---|
| 1056x852 | 22.0 | 640 | 0.5 ms (idle) to 7.8 ms (while generating) | 18-19 ms | 0.1 ms |
| 1920x1200 | 22.3 | 1645 | 14-18 ms | 36-39 ms | 0.4-0.7 ms |

The fps ceiling and most of the stamp-to-paint time are the single-threaded JS generator (2.3 Mpx
per frame); the transport and the webview add well under 5 ms. A real backend would send compressed
tiles/H.264 (WebCodecs is available in the webview) and this floor is what it competes against.

### (d) HiDPI

`window.devicePixelRatio` = OS scale x VS Code zoom: 1.5 at 150 % (AppliedDPI 144), 1.8 after
`workbench.action.zoomIn` (zoomLevel 1), back to 1.5 on `zoomReset`; `matchMedia('(resolution:
1.5dppx)')` follows. `screen.width/height` report CSS px (2560x1440 for the 3840x2160 panel). A
stream client should request `clientWidth * dpr` pixels and re-request on `resize` (it fires on
zoom, split, zen, fullscreen).

### (e) Multi-window

Not a shell property: the webview is one rectangle; KiCad's modal dialogs and the footprint chooser
must be composited into the stream by the backend (VNC/Xpra do it, Broadway does it per top-level
window, a custom capture must). The shell side can open additional `WebviewPanel`s per top-level
window if the backend reports them (not prototyped).

### (f) Clipboard

- Webview origin (`vscode-webview://`): `navigator.clipboard.writeText/readText` succeed
  (`permissions.query` says `granted`) when the document is focused; unfocused calls throw
  `NotAllowedError: Document is not focused`. `Ctrl+V` triggers VS Code's webview paste, delivering a
  DOM `paste` event with the OS clipboard text (`paste-me-A` round-tripped from
  `vscode.env.clipboard.writeText`). `vscode.env.clipboard` on the extension host reads/writes freely.
- `http://localhost` iframe: `writeText`/`readText` are `Write permission denied` even inside a user
  gesture and with `allow="clipboard-read; clipboard-write"`; `Ctrl+V` produces no `paste` event.
  Workaround: the host page relays clipboard through `postMessage` (host can, frame cannot), or the
  backend protocol carries clipboard (VNC/Xpra do).

### (g) CPU / GPU cost of the shell

Not separately profiled; the only shell-side work is `putImageData`/WebGL upload and the WebSocket
receive, which measured at 0.1-0.7 ms per frame. Dev-host idle cost is that of any VS Code window.

### (h) Platform coverage

The shell is platform-neutral (Windows measured; the same code runs on Linux/macOS VS Code and in
Remote/WSL windows where `portMapping` becomes necessary). Windows-only detail: the custom title bar
mnemonics above. Not measured: Linux, WSL remote extension host.

### (i) Licensing

Prototype MIT. Dependencies: `ws` (MIT), `esbuild` (MIT), `typescript` (Apache-2.0), `@types/*`
(MIT). VS Code extension API: MIT / VS Code EULA for the Marketplace build; no GPL code touched.

### (j) Effort to productize

Shell only: small. The prototype already has the panel, custom editor, keybinding rescue, the
context-menu opt-out, focus hand-off, DPI handling and a client for a binary frame protocol. Remaining
shell work (1-2 weeks): settings/commands to disable the Alt-menu behaviours with consent, reconnect /
full-frame resync on `retainContextWhenHidden: false`, `webview/context` menu contributions if any,
WebCodecs decoder for a compressed stream, a status bar item, tests via `@vscode/test-electron`
driving the same probe page (the SendInput driver is Windows-only). Everything hard is in the
backend: capturing KiCad's wx/GAL windows including dialogs, encoding, and input injection with
identical semantics.

## 4. What the shell expects from a backend (contract sketch)

Either a URL or a WebSocket, chosen per setting:

- **URL mode** (`kiwrapShell.url`): any page that renders the display and takes DOM input. The shell
  iframes it with `allow="clipboard-read; clipboard-write; pointer-lock"`, pushes focus into it, and
  relays `{type:'open', file}` via `postMessage`. Must be `http://localhost:<port>` (or
  `127.0.0.1`) so `portMapping` can forward it from a remote extension host; CSP `frame-src` is set
  accordingly. Keyboard capture is total; VS Code chords are dead while focused (acceptable for a
  "KiCad mode", document it).
- **Stream mode** (`kiwrapShell.wsUrl`), binary little-endian, as implemented in `src/extension.ts`
  and `media/canvas.html`:
  - server -> client frame: `u8 type=1 | u32 seq | f64 t_server_ms | u16 w | u16 h | u8 fmt | pixels`;
    `fmt 0` = RGBA8 (prototype); a product adds `fmt 1` = JPEG/WebP tile list and `fmt 2` = H.264/AV1
    access unit for WebCodecs, plus a `type=2` dirty-rect update and `type=3` cursor shape.
  - client -> server: `{type:'resize', w, h, dpr}` on `resize`; `{type:'input', id, t_client, ev:{kind:
    keydown|keyup|pointerdown|pointerup|pointermove|wheel, key, code, x, y, button, buttons, dy, ctrl,
    shift, alt}}` with device-pixel coordinates; `{type:'open', file}`; `{type:'clipboard', text}` both
    ways.
  - server -> client `{type:'echo', id, t_client, t_server}` for the RTT HUD.
  The client coalesces frames to one `requestAnimationFrame`, so the server may send at any rate.

## 5. Concrete recommendations for the kiwrap extension

1. Use the custom editor as the primary surface (`priority: "option"` in the product so text
   editing stays default, with an "Open with KiCad" entry); keep a `WebviewPanel` command for the
   project-level KiCad window.
2. Ship the keybinding rescue block generated from a list, guarded by the two context keys, plus a
   one-time prompt offering to set `window.customMenuBarAltFocus=false` and
   `window.enableMenuBarMnemonics=false` for the workspace.
3. `data-vscode-context='{"preventDefaultContextMenuItems": true}'` on the display element;
   `retainContextWhenHidden: true` (a stream reconnect is worse than the memory).
4. Prefer the stream shape over the iframe shape when the backend is ours: same input fidelity,
   clipboard works, VS Code chords can be selectively kept (e.g. `Ctrl+Shift+P` could stay VS Code's
   while `Ctrl+W` goes to KiCad), and no cross-origin focus dance. Use the iframe shape to get going
   with noVNC/Xpra unchanged.
