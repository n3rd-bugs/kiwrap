# Display route: per-window capture + browser streaming + input injection ("winstream", Windows)

Hands-on spike, 2026-09-19, machine: Windows 11 Pro 26200, Core Ultra 9 285K, RTX 5080 + Intel iGPU,
two 3840x2160 monitors at **150 % scaling** (system DPI 144), KiCad 10.0.6 and 10.99 nightly.
Prototype: `prototypes/display/winstream/` (Python, run steps in its README). Raw numbers:
`docs/research/display/winstream/data/*.json`; screenshots: `docs/research/display/winstream/*.png`
(all taken by Playwright from the browser page, i.e. they show what the *streamed* client sees, not
the desktop).

**Question asked:** can we own a "mini-VNC" that captures only the KiCad window(s), streams them
into a VS Code webview and injects the webview's input back, giving the identical KiCad UI/UX inside
VS Code?

**Short answer:** the *display* half works and is cheap (WGC per-window capture gives the real,
GL-rendered KiCad UI, dialogs included, at 24-30 fps, 40-55 ms end-to-end, ~5 % of one core).
The *input* half is the problem: reliable input needs `SendInput`, which only works when the KiCad
window is on-screen, unobscured and (for keys) the foreground window — i.e. the opposite of "hidden
behind VS Code". `PostMessage` (which would allow a hidden window) breaks on KiCad's own design:
its canvas reads the *real* OS cursor position (`wxGetMousePosition`) for clicks, context menus and
zoom centre, so posted mouse clicks land at the wrong place. The route is viable only as
"KiCad visible on a second monitor / virtual desktop area, mirrored and driven from VS Code", not as
"KiCad invisible, VS Code is the UI".

---

## 1. What was built

```
prototypes/display/winstream/
  kicad_env.py            sandboxed env JSON via kiwrap.sandbox.ensure() (no wizard, %APPDATA% untouched)
  winstream/windows.py    DPI awareness (PMv2), window enumeration per PID, DWM frame bounds, launch, focus
  winstream/capture.py    WGC per-window sessions (windows-capture), tracker for dialogs/menus, JPEG encode
  winstream/inject.py     SendInput injector (mouse abs. coords on the virtual desktop, VK/scancode keys)
                          + PostMessage injector (WM_* to the HWND under the point / the thread's focus)
  winstream/server.py     aiohttp: /ws (binary frames + JSON input), /stats, /shot, /cursor, /clipboard
  winstream/static/index.html  viewer: one <canvas> per window, composited by screen rect, input capture
  bench/bench_a.mjs, bench/drive.mjs   Playwright drivers (headless Chromium) that produced every number below
```

Pipeline: `Windows.Graphics.Capture` (per HWND) -> BGRA frame on the capture thread -> libjpeg-turbo
(`simplejpeg`, GIL released) -> 52-byte header (hwnd, seq, size, offset relative to the main frame,
capture timestamp, probe id) + JPEG over a WebSocket -> `createImageBitmap` -> canvas. The server
keeps one "latest frame per window" slot per client (drop-to-latest, no queueing). Input goes the
other way as JSON (`move/down/up/wheel/key`, frame-pixel coordinates, `KeyboardEvent.code`).

## 2. Findings per criterion

### (a) Fidelity — real KiCad UI, GL canvas, dialogs, menus, tooltips

| Item | Result | Evidence |
|---|---|---|
| Main window incl. GAL (OpenGL) canvas | **Yes.** WGC reads the DWM-composed surface, the GL canvas is present (centre-region std 52, 1908 colours; no black) | `01-wgc-4k-maximised-frame.png` (3840x2088 physical px), `02-…` |
| Frame size / origin | == `DWMWA_EXTENDED_FRAME_BOUNDS` (visible frame, no invisible resize borders), physical px | measured: bounds (60,60,1980,1140) -> 1920x1080 frame |
| Owned modal dialogs (`#32770`, wx dialogs) | **Yes**, as separate HWNDs: per-window WGC session each, composited at their screen offset | `10-modal-file-open-warning.png`, `06-owned-dialog-composited.png` |
| Footprint chooser (large modal, GL preview inside) | **Yes**, composited; extends above the main frame (viewer grows to the union rect) | `07-footprint-chooser-composited.png` |
| Context menu / menu bar menus (`#32768`) | **Yes, but** a per-window WGC capture of a menu window **dismisses the menu** 50-100 ms after it opens (reproduced 3/3, never with child capture off). Fixed by cropping menus from a *monitor* capture instead | `05-context-menu-composited.png` |
| Tooltips | **Not observed** in any run (SendInput hover 1.8 s over a toolbar button, also 3 s manual). Unclear whether wxAuiToolBar tooltips need a genuinely active window; not resolved | run2/run5 `windows_tooltip` |
| Cursor | not captured (`cursor_capture=False`); the browser draws its own. KiCad's crosshair cursor (when enabled) *is* part of the frame | — |
| Idle behaviour | WGC delivers frames **only when content changes**: idle = 0 frames, 0 bandwidth | run1 `idle` |

### (b) Input — what reached KiCad (SendInput injector, window on monitor 2, unobscured)

| Action | Result | Evidence |
|---|---|---|
| Mouse move | works (real cursor moves; exact mapping, see (d)) | run1 `mapping` |
| Left click select | works (Properties panel filled, item highlighted) | `04-…` first two tiles, run2 `diff_select_props_panel` 21 % |
| `R` rotate | works (selected text turned 90°) | `04-…` tile 3 |
| `Delete` | works | `04-…` tile 4 (label gone) |
| `Ctrl+Z` | works (label back, still rotated) | `04-…` tile 5 |
| Middle-drag pan | works (74.5 % of canvas pixels changed) | run2 `diff_pan` |
| Left-drag move footprint | works (R4 moved; undone) | run2 `b6_dragged_R4` |
| Wheel zoom | works, 40-55 ms end-to-end | run1 `wheel_latency` |
| Right-click context menu | works once menus are captured via monitor crop | `05-…` |
| Menu bar click | opens (window count 2 within 73 ms) | run5 `wait_file_menu` |
| `A` -> footprint chooser | opens in 220-440 ms | run5/run6 |
| `E` on selection -> properties dialog | opens in 56-270 ms | run3/run5 |
| Text entry in a dialog, `Esc` closes dialog | **worked in run3** (properties dialog appeared, Esc closed it); **failed in run5/run6** because another agent's automation on the same desktop re-stole the foreground every ~100 ms (see 2.9); keys go to whichever window is foreground | run3 vs run6 `windows_after_chooser_esc` |
| `Home` (zoom fit), `Esc` (deselect) | work | every run |
| Clipboard `Ctrl+C` | works: Windows clipboard receives KiCad's `(kicad_pcb …)` s-expression text (877 B) | run3 `clipboard_after_ctrl_c` |

**PostMessage injector** (no focus stealing, works while another app is foreground, works partially
off-screen):

| Action | Result |
|---|---|
| Wheel zoom | works, 46 ms e2e |
| Middle-drag pan | works (75 % changed) |
| Left click select | **fails** (nothing selected): KiCad's `WX_VIEW_CONTROLS` reads `wxGetMousePosition()`, i.e. the real OS cursor, not the message's coordinates |
| Right-click menu | menu opens **at the real cursor position** (on the other monitor, rect [839,599,…]) for the same reason |
| Hotkeys `A`, `Home`, `Ctrl+F` | work without focus (posted to the thread's focus window found via `AttachThreadInput`+`GetFocus`) |
| Text entry | reaches the chooser's filter box; **do not** post `WM_CHAR` yourself (wx's loop translates the posted `WM_KEYDOWN`, giving doubled chars, `08-…`) — fixed in the prototype |
| `Esc` to cancel a dialog | **fails** (posted VK_ESCAPE ignored by wxDialog; `WM_CLOSE` does close it) |
| Modifiers (`Ctrl`, `Shift`) | must be genuinely down (`GetKeyState`) -> still needs SendInput for the modifier, which then also reaches the real foreground app |

### (c) Latency and frame rate (1920x1080 physical, JPEG q60-70, headless Chromium client)

| Measure | Value |
|---|---|
| Wheel zoom, page `send` -> pixel painted (12 probes) | median **52 ms**, min 34, max 66 (one 625 ms outlier on the very first event after launch) |
| split: ws -> SendInput | 1-2 ms |
| split: SendInput -> WGC frame arrival (KiCad render + DWM + WGC) | median 20-29 ms |
| split: capture -> JPEG -> ws -> decode -> paint | median 24 ms (encode 4-6 ms, decode 7-10 ms, rest is ws + `createImageBitmap` scheduling) |
| Key `R` -> painted | 54 ms (37 ms with PostMessage) |
| Sustained middle-drag pan, 7.3 s | **24 fps** at the client (captured 175 = sent 175 = painted 175; nothing dropped), 30 fps cap set on WGC |
| Continuous wheel zoom (one event / 60 ms) | 15.6 fps = one frame per zoom step (KiCad-bound, not pipeline-bound) |
| Idle | 0 fps, 0 bytes |
| Loopback ws RTT | 1 ms |

### (d) HiDPI / scaling

The machine runs 150 %. The server opts into per-monitor-v2 DPI awareness (note: the
`DPI_AWARENESS_CONTEXT` handle must be passed as a pointer, `ctypes.c_void_p(-4)`; passing `-4` as
int fails silently and every rect comes back in logical px — cost 20 minutes). Everything is then
physical pixels: DWM frame bounds, WGC frame size, `SendInput` absolute coordinates over the virtual
screen (`SM_*VIRTUALSCREEN`). The browser page scales the composite to fit (`transform: scale`) and
divides pointer coordinates by the scale. **Measured mapping error: 0 px at all four probe points
(200,200), (960,540), (1900,1060), (17,900)** — `GetCursorPos` after an injected move equals the
requested frame pixel exactly (run1 `mapping`). KiCad renders at native 144 dpi, so the stream is
crisp when the webview shows it 1:1 and merely downscaled otherwise.

### (e) Multi-window

Per-window capture means every top-level HWND of the process is its own stream: main frame, modal
dialogs, chooser, "Save Changes?" box. The tracker polls `EnumWindows` every 100 ms (visible,
non-cloaked, non-iconic windows of the PID), starts/stops a WGC session per window and sends the
z-ordered rect list; the client composites canvases by screen offset relative to the main frame and
grows the stage to the union rect (the chooser opens 24 px above the frame). Dialog appearance
latency is the poll interval (measured 56-270 ms to first frame). Menus, tooltips and combo
drop-downs are cropped out of one monitor capture that is started lazily when such a window appears
and stopped when it is gone (only for the monitor holding the main window).

### (f) Clipboard

`Ctrl+C` in KiCad writes the Windows clipboard (s-expression text). It is the *host* clipboard, so a
VS Code webview on the same machine already shares it; a remote client would need an explicit relay
(`/clipboard` endpoint exists for reading). Note: another agent's clipboard test overwrote it
mid-run (`paste-me-A`), which is a reminder that the clipboard is global.

### (g) CPU / GPU cost (1080p)

| Phase | Server process (one core = 100 %) | pcbnew | GPU (system-wide, `nvidia-smi`) |
|---|---|---|---|
| Idle (no changes) | 2-5 % | 0.5-2 % | 24-25 % (other apps) |
| Sustained pan 24 fps, q60 | **16 %** (encode 4.7 ms/frame) | 6.4 % | 34 % |
| Continuous zoom 15 fps, q70 | 6 % | 1.2 % | 34 % |

Encoder cost scales with pixels: at 3840x2088 libjpeg-turbo needs 16-19 ms/frame (q50-90) for
0.7-1.5 MB per frame -> 170-370 Mbit/s at 30 fps; at 1080p 3.8 ms and 130-236 KB (32-58 Mbit/s).
Pillow WebP is 2-3x smaller but 27 ms (1080p) / 130-650 ms (4K): not usable without a native
encoder. Fast PNG: 25 ms / 90 ms. Bandwidth is irrelevant on loopback but the decode cost in the
webview is not: 7-10 ms per 1080p JPEG on the page's main thread.

### (h) Platform coverage

Windows 10 1903+ only (WGC per-window needs 1903, `IsBorderRequired`/cursor flags 2004+). Nothing
here runs in WSL (WGC, SendInput, DWM are host-side; WSLg windows are RDP-composited, so a WSL KiCad
would need the Linux route — Xvfb/xpra/VNC — covered by the sibling reports). Linux equivalents
would be a different code base (X11 `XComposite`/`xdamage` + `XTest`, or Wayland portals + libei).
macOS: `SCStream` + `CGEvent`, different again.

### (i) Licensing

| Component | Licence | Note |
|---|---|---|
| `windows-capture` 2.0.1 (Rust, PyO3) | MIT | pulls `opencv-python` (Apache-2.0, 60 MB) only for `save_as_image`; a productised helper would call WGC directly |
| `simplejpeg` 1.9.0 | MIT (bundles libjpeg-turbo: IJG/BSD-3/zlib) | |
| `aiohttp` 3.x | Apache-2.0 | |
| `numpy` | BSD-3 | |
| `pywin32` 312 | PSF-like (BSD-ish) | |
| `psutil` | BSD-3 | measurement only |
| `Pillow` | MIT-CMU | measurement only |
| Playwright + Chromium | Apache-2.0 / BSD | test harness only |
| Windows APIs (WGC, DWM, SendInput) | OS | WGC per-window capture of another process needs no consent prompt (`GraphicsCaptureItem.CreateForWindow` via the interop, no picker) |

### (j) Effort to productise inside the VS Code extension

See section 4. Rough order: display side 2-3 weeks to a robust helper (Rust/C#), input side is
where the design decision lies — see the honest limits in section 3.

## 3. Hard limits found (these decide the recommendation)

1. **`SendInput` requires the window on-screen and unobscured at the point.** The very first stage-B
   run injected clicks into *another agent's VS Code Extension Development Host window* that was
   lying on top of the KiCad window on monitor 1 (`WindowFromPoint` returned
   `Chrome_RenderWidgetHostHWND` of that process). The prototype now checks `WindowFromPoint` before
   every click/wheel and raises KiCad first; in production the window on top would be *VS Code
   itself*, so every click would flash KiCad in front of VS Code unless KiCad lives where VS Code is
   not (second monitor, or the extension shrinks VS Code and parks KiCad next to it — at which point
   the user might as well look at KiCad). `HWND_TOPMOST` on KiCad (`--topmost`) avoids the
   re-raise but then KiCad permanently covers that region of the desktop.
2. **Keyboard via `SendInput` needs foreground.** `SetForegroundWindow` from a background process
   is refused; the `AttachThreadInput` trick works, the ALT-tap trick works but puts wx's menu bar
   into keyboard mode and swallowed the next click (this cost the context-menu test two runs).
   Another process that actively re-asserts foreground (here: a sibling agent's automation, every
   ~100 ms) wins the race and the keystrokes go to *it*. In production the competitor is VS Code
   whenever the user touches it; the extension's webview cannot hold a real focus that KiCad also
   has.
3. **`PostMessage` cannot replace it for KiCad.** Hotkeys and text entry can be posted, but the
   canvas takes the cursor position from `wxGetMousePosition()` (WX_VIEW_CONTROLS), so clicks select
   nothing and context menus open at the real cursor; posted `VK_ESCAPE` is ignored by wxDialog;
   modifier state comes from `GetKeyState`. Fixing this means patching KiCad (an "input coordinates
   from the event" mode) — possible in the fork (D17) but it is a real, ongoing patch.
4. **Per-window WGC capture of a popup menu dismisses the menu.** Handled by monitor-crop; the cost
   is one full-monitor capture stream while a menu is open, and it only covers menus on the main
   window's monitor.
5. **Minimised or fully off-screen windows produce no frames** (partially off-screen is fine). The
   "hidden KiCad" would have to be on-screen somewhere: a second monitor, a Windows virtual desktop
   (WGC of windows on another virtual desktop is untested here), or a region VS Code is told to
   avoid.
6. **Tooltips never appeared** under injected hover. Not root-caused.
7. **Focus/activation is shared state of the desktop.** Every foreground fix is visible to the user
   (title bars flip, VS Code loses focus), and menus close when KiCad loses activation — observed on
   the nightly run: the context menu opened (window count 2 in 60 ms) and was gone 500 ms later
   because the sibling automation re-activated its own window.

## 4. Productisation path (if the "mirror KiCad on a second screen area" scope is accepted)

- **Helper process:** not Python. A small **C#/.NET 8 (WinRT projection of
  `Windows.Graphics.Capture`, `Direct3D11` staging, `SendInput`) or Rust (`windows` crate, same
  code the `windows-capture` crate already has)** helper spawned by the extension, talking to the
  webview over a loopback WebSocket with a per-session token (both `/ws` and `/stats` already refuse
  without it). Python's cost was negligible here, but shipping a venv + 60 MB of OpenCV for a helper
  is not acceptable and the encode/copy path wants to live next to the D3D texture anyway.
- **Encoding:** keep JPEG (libjpeg-turbo) for a first version at 1080p-class windows; WebRTC
  (H.264 via Media Foundation / NVENC) if 4K at 30 fps is required (JPEG at 4K = 16-19 ms encode +
  ~13 ms decode per frame and 200-370 Mbit/s, which is fine on loopback but burns a full core).
  Dirty-region streaming (WGC exposes dirty rects; `dirty_region=True`) would cut both by an order
  of magnitude for typical editing, where only the canvas changes.
- **Transport:** WebSocket + binary frames is enough on loopback (RTT 1 ms); the webview decodes
  with `createImageBitmap` (7-10 ms at 1080p, off the main thread when done in a Worker with
  `OffscreenCanvas`). WebRTC only buys hardware video decode.
- **Window tracking:** EnumWindows poll -> replace with `SetWinEventHook(EVENT_OBJECT_SHOW/HIDE,
  EVENT_SYSTEM_MENUPOPUPSTART/END, EVENT_OBJECT_LOCATIONCHANGE)` for the process, no polling and
  no 100 ms dialog delay.
- **Input:** `SendInput` only; keep the "raise if obscured" check and the AttachThreadInput
  foreground path; never the ALT trick. Pin KiCad to a screen area the extension reserves (second
  monitor, or a Windows virtual desktop if capture there works — to be verified). Map
  `KeyboardEvent.code` -> scancodes (done), send modifiers as real key events, and release all keys
  on webview blur (done). Clipboard is shared automatically on the same machine.
- **Security:** bind 127.0.0.1, random token per launch in the URL and the ws handshake, no CORS,
  webview `Content-Security-Policy` allowing only that origin; the helper is started by the
  extension with the sandboxed `KICAD_CONFIG_HOME` (`kiwrap.sandbox`) so the user's config is never
  touched.
- **Multi-monitor / DPI:** all coordinates physical, per-monitor-v2 awareness in the helper;
  choose the monitor for the menu-crop session per menu rect, not per main window.
- **Minimised / hidden:** refuse to minimise (intercept `WM_SYSCOMMAND` `SC_MINIMIZE` via
  `SetWindowsHookEx` is intrusive; simpler: restore on next input), or accept that the stream freezes.
- **Risks:** (1) KiCad updates change window classes/titles (we key on PID and z-order, not
  titles); (2) the user's own mouse fights the injected cursor (every injected move moves the real
  cursor); (3) focus flapping with VS Code; (4) any other topmost window (notifications, screen
  recorders) over the KiCad area receives the clicks; (5) WGC yellow border/consent: none for
  `CreateForWindow` via interop, but Windows 11 24H2+ may draw the capture border unless
  `IsBorderRequired=false` (set; needs the `GraphicsCaptureAccess` capability check in packaged
  apps).

## 5. Recommendation for kiwrap

Do **not** pursue "identical KiCad UI/UX inside VS Code" through screen mirroring as a product
feature. It is technically a mirror of a KiCad window that must stay visible somewhere else, with
input that steals focus and races with VS Code, and the parts that would make it feel native
(posted input to a hidden window) are blocked by KiCad's own cursor handling. What *is* cheap and
robust from this spike:

- **"Live KiCad panel" = launch the sandboxed GUI (ARCHITECTURE §9 already plans this) + a read-only
  live thumbnail/mirror** in a webview: capture-only, no input, 0 cost when idle, dialogs
  included. Useful for the agent loop ("look at what KiCad shows") and for a human glancing at the
  board while chatting; clicking the panel just raises the real KiCad window (`SetForegroundWindow`
  from a user gesture is allowed).
- **Screenshots for the agent** (`kw_live` `screenshot` action): one WGC frame of the main window
  is < 10 ms and includes the GL canvas — better than `kicad-cli` renders for "what does the user
  see".
- If interactive mirroring is still wanted for a *second-monitor* workflow, the helper design in
  section 4 is a 3-4 week job in C#/Rust, and it should be sold as "KiCad mirrored in VS Code",
  not as "KiCad inside VS Code".

## 6. Process notes

- All GUI launches used the kiwrap sandbox roots under the scratchpad
  (`KICAD_CONFIG_HOME`/`_DOCUMENTS_HOME`/`_CACHE_HOME`); `%APPDATA%\kicad` hash before/after:
  `a90607a5…6d9d` unchanged. Scratch copies of `demos/ecc83` were used; stale `~*.lck` files from
  a terminated first run produced the "File Open Warning" dialog (`10-…`), which doubled as the
  first dialog-capture test.
- The desktop was shared with two other spikes (a VNC one and a VS Code extension one) that also
  inject input and steal focus; every anomaly attributed to that is marked as such above and was
  re-run at least once.
- Every KiCad process started by this spike was terminated (`POST /quit` terminates the child;
  verified with `Get-Process pcbnew`). The one pcbnew left on the machine (`*vncspike`) belongs to
  the VNC spike.
