# Display route: VNC single-window share of KiCad + noVNC (browser, then VS Code webview)

Hands-on spike on the Windows host, 2026-09-19. Question: can KiCad's real display be forwarded into VS Code
and stay interactive, so the user gets KiCad's own UI/UX inside VS Code? This route streams the pixels of one
sandboxed `pcbnew.exe` window over RFB (VNC) to a noVNC canvas, first in Chromium (driven by Playwright), then
inside a VS Code `WebviewPanel` (prototype extension `prototypes/display/vscode-novnc/`).

Everything below was measured, not guessed; the numbers come from `scratchpad/display/vnc-novnc/results/*.json`
and the screenshots in `img/`. Scripts are listed at the end so the spike can be repeated.

## TL;DR

| | Result |
|---|---|
| Works? | **Yes, partially.** The real KiCad UI (menus, context menus, modal dialogs, GAL canvas with OpenGL *and* Cairo) is rendered and interactive through noVNC in Chromium and inside a VS Code webview. |
| Fidelity | Pixel-exact copy of the real window (it *is* the real window). Tooltips are the one thing that never reached the framebuffer. A modal dialog is captured only if it lies inside the shared window rect. |
| Input | Mouse move/click/right-click/wheel/middle-drag and keyboard incl. Ctrl/Shift modifiers, letters (R), Delete, Esc, Ctrl+Z all reach KiCad through noVNC. Inside VS Code: wheel + middle-drag confirmed; **F1, Ctrl+P, Ctrl+Shift+P and Alt are consumed by VS Code** even with the KiCad iframe focused. |
| Latency | Key press -> first framebuffer update: median 18-27 ms, p90 ~230-300 ms (polling server). Pan: 35-45 framebuffer updates/s at 1600x1000. |
| Cost | tvnserver ~25 % of one core while panning (screen polling, no mirror driver), pcbnew ~10 %, Chromium ~11 %. Idle: <3 %. GPU: nothing attributable. |
| HiDPI | Framebuffer = physical pixels of a 150 %-scaled window; the noVNC page needs `fit=dpr` (CSS size = fb / devicePixelRatio) to be crisp at 1:1; otherwise Chromium upscales by DPR (blurry) or downsamples. |
| Multi-window | Single-window share is a **screen-rect crop**, not a per-HWND capture: dialogs/menus inside the rect show, anything else covering the rect shows too (other windows leak in), anything outside is cut off. `-shareapp <pid>` exists but was not measured. |
| Clipboard | Both directions work (KiCad Ctrl+C -> 155 kB s-expression in the browser clipboard; browser -> host clipboard via RFB ClientCutText). |
| Platforms | Windows host only for this stack (TightVNC `-sharewindow`). Linux/WSLg would use a different server (x11vnc `-id`, or a per-app Xvfb/Wayland-compositor which is the cleaner design). |
| Licensing | TightVNC 2.8.88 server **GPL-2.0** (separate process, launched by us, not linked; commercial licence available), noVNC 1.7.0 **MPL-2.0**, websockify (python) **LGPL-3.0**, UltraVNC 1.8.3 GPL-3.0 (evaluated, not usable: single-window sharing was removed upstream). KiCad GPL-3.0 untouched. |
| Effort to productize | Medium-high: 2-3 weeks for a Windows-only "live KiCad tab" (bundled VNC server, per-session port/auth, window tracking, focus/keybinding shim, reconnect, HiDPI); it stays a *remote-desktop-in-a-tab*, not an integration. |

**Recommendation:** treat this as a fallback "escape hatch" (open the live KiCad window in a tab), not as the
plan for "identical UI/UX inside VS Code". The identical-UX goal is met by definition (it is KiCad's own
pixels) but at the price of a remote-desktop feel: focus/keybinding fights with VS Code, a shared-screen
crop that leaks other windows, polling CPU, no per-monitor DPI negotiation, and a GPL server process to ship.

## 1. Setup that worked (exact flags)

* KiCad 10.0.6 `pcbnew.exe` on a scratch copy of the ecc83 demo, sandboxed with
  `uv run python scripts/seed_config_root.py seed --kicad-root "C:/Program Files/KiCad/10.0" --storage <scratch>/storage`
  and `KICAD_CONFIG_HOME/KICAD_DOCUMENTS_HOME/KICAD_CACHE_HOME` + short `TEMP` pointing at it (no wizard appeared).
  The project copy was renamed `vncspike.kicad_pcb` so the window title is unique (`*vncspike — PCB Editor`).
  User config hash before/after: `a90607a5...` unchanged.
* GAL backend switched via the sandbox's `10.0/pcbnew.json`: `{"graphics":{"canvas_type":1}}` = OpenGL,
  `2` = Cairo (`EDA_DRAW_PANEL_GAL::GAL_TYPE`). Both rendered (mean luminance of the canvas region 79, 0 % black
  pixels in both cases; a black canvas would read ~0/100 %).
* **VNC server: TightVNC 2.8.88 (64-bit), no service, no install.** The MSI was unpacked with an administrative
  install (no elevation): `msiexec /a tightvnc-2.8.88-gpl-setup-64bit.msi /qn TARGETDIR=<dir>` ->
  `<dir>/PFiles/TightVNC/tvnserver.exe`. Settings live in `HKCU\Software\TightVNC\Server` (application mode):
  `UseVncAuthentication=0 LoopbackOnly=1 AllowLoopback=1 RfbPort=5900 AcceptHttpConnections=0
  GrabTransparentWindows=1 UseMirrorDriver=0 PollingInterval=30 EnableFileTransfers=0`. Start with
  `tvnserver.exe -run`; share one window with `tvnserver.exe -controlapp -sharewindow "<title substring>"`
  (also available: `-shareapp <pid>`, `-sharerect`, `-shareprimary`, `-sharefull`, `-sharedisplay`; these are
  CLI-only, see the TightVNC 2.7 server command-line PDF). The registry key was deleted afterwards.
* **UltraVNC 1.8.3.0** (portable zip, SHA256 `11163a0b...`) was downloaded and inspected first: the current
  source's `vncDesktop::SetSW()` only cycles monitors; the old "share only the window named" feature is gone from
  `winvnc.exe` (no such strings, `uvnc_settings` still writes an unused `SingleWindow` ini key). It does embed a
  noVNC-based HTML client in `winvnc.exe` (interesting for a zero-dependency viewer) but cannot share one window,
  so it was not used.
* **noVNC 1.7.0** (`git clone --depth 1`) + **websockify** (pip, scratch venv):
  `websockify --web <noVNC dir> 127.0.0.1:6080 127.0.0.1:5900`. A 40-line page `kicad.html` (in the scratch clone)
  creates `RFB` directly, exposes it as `window.rfb`, counts framebuffer updates by wrapping
  `rfb._display.flip`, and supports `?fit=dpr` (1:1 physical pixels on HiDPI) and `?fit=window`.
* **Playwright 1.63 / Chromium** in the scratchpad drove `kicad.html` (viewport 1700x1080, DPR 1, no scaling so
  framebuffer px == page px); screenshots are of the canvas element; diffs are computed with pngjs.

A hard lesson from the shared desktop: `-sharewindow "PCB Editor"` first matched *another* agent's WSLg window
titled `... PCB Editor (Ubuntu)` on the second monitor, so the first run streamed (and injected input into) the
wrong 3840x2160 area. The driver now refuses to inject input unless the framebuffer is exactly the 1600x1000
window. Other agents also killed every `pcbnew` on the machine several times during the spike (blanket
`Stop-Process pcbnew`), and stole the foreground window, which is why some keyboard runs are marked inconclusive.

## 2. Fidelity (a)

* `img/vnc-novnc-01-host-pcbnew.png` – the real window on the host (reference).
* `img/vnc-novnc-02-novnc-framebuffer-opengl.png` – the same window as seen by noVNC in Chromium: full
  toolbars, Properties/Appearance panels, OpenGL GAL canvas rendered (not black). Framebuffer 1600x1000 = the
  window rect incl. the 8 px DWM border (a sliver of what is behind the window is visible at the edges).
* `img/vnc-novnc-05-novnc-framebuffer-cairo.png` – Cairo backend, identical result.
* `img/vnc-novnc-03-popups-host-vs-framebuffer.png` – host crop (left) vs framebuffer (right): the right-click
  **context menu** and the **File menu** popup are captured (present at 300 ms, 1.5 s, 4 s). The **"Zoom In"
  tooltip** is on the host but never in the framebuffer (BitBlt polling misses the layered tooltip window even
  with `GrabTransparentWindows=1`).
* `img/vnc-novnc-04-board-setup-dialog-host-vs-framebuffer.png` – the modal **Board Setup** dialog is captured
  because it opens centred inside the window rect. A dialog that opens elsewhere (KiCad remembers dialog
  positions) is simply not in the stream: in a later run the dialog had been opened while the window sat at
  another position and the VS Code webview showed the main window without it while `pcbnew` was modal-blocked.
* Single-window share = screen-rect crop: when another agent's Extension Development Host overlapped the rect,
  its content appeared in the stream and received the injected clicks/keys. `-shareapp <pid>` (all windows of
  a process) is the better mode to test next; a per-HWND capture (PrintWindow / Windows.Graphics.Capture) is what
  a product would need, and no VNC server does that.

## 3. Input (b) – noVNC in Chromium, measured with pixel diffs of the canvas region

From `results/opengl.json` (OpenGL) and `results/cairo.json` (Cairo):

| action | reaches KiCad | evidence |
|---|---|---|
| hover + left click on C1 | yes | Properties panel switches to the item |
| `R` (rotate) | yes | reference text orientation 90° -> 180° (`img/vnc-novnc-06-rotate-before-after.png`); canvas diff 0.17 % (small text) |
| `Ctrl+Z` / `Ctrl+Z` after Delete | yes | orientation back to 90°; deleted item restored |
| wheel up at cursor | yes | Zoom 3.45 -> 5.18, 70 % of canvas pixels changed |
| middle-button drag | yes (pan) | 98 % of canvas pixels changed |
| `Delete` on selection | yes | 13 % canvas change (Cairo run 0.9 %) |
| `Esc` | yes (clears selection / closes menus) | context menu and File menu closed |
| `Home` (zoom fit) | yes | |
| Board Setup toolbar click | yes, dialog opens in the stream | |
| typing into Position X + Enter | typed text visible (3.6 % diff of the panel); the move itself did not apply in the Cairo run (focus stolen by another agent between Enter and capture – inconclusive) | |
| `Esc` to close Board Setup | works only after a click *inside* the dialog; the freshly opened dialog did not have keyboard focus | foreground was another process at that moment (shared desktop) – retest on a quiet desktop |
| `Ctrl+A`, `Ctrl+C` | yes (see clipboard) | |

TightVNC injects input with `SendInput`, so anything the real keyboard can do works, *provided the shared window
is the foreground window* – VNC input goes to whatever has focus, not to the shared window.

## 4. Latency and frame rate (c)

Key press (`R` on a selected item) -> first framebuffer update seen by noVNC, 10 repetitions:

* OpenGL: `[300, 83, 23, 14, 23, 309, 7, 27, 24, 178]` ms, **median 27 ms**
* Cairo:  `[12, 17, 12, 244, 13, 35, 12, -, 825, 66]` ms, **median 17 ms**

The bimodal shape is the server's screen **polling** (`PollingInterval=30` ms, plus its change-detection cycle);
the mirror driver / DXGI capture options were not used. Single actions: click 3-25 ms, wheel 2-3 ms, Delete 7-20 ms,
menu open 13-20 ms, dialog open 20-40 ms (first update; the full dialog paints over several updates).

Continuous middle-drag pan (40 synthetic moves over ~1.45 s): **39 fps** (OpenGL) / **45 fps** (Cairo) framebuffer
updates; a heavier 7 s pan+zoom loop: **42 fps** (300 updates, 1600x1000, Tight encoding, loopback).

## 5. HiDPI (d)

Host display: 3840x2160 at 150 % (system DPI 144). `pcbnew` is per-monitor DPI aware, so the shared framebuffer
is 1600x1000 *physical* pixels of a 150 %-scaled UI. In Chromium at DPR 1 it is shown 1:1 (large UI). In the
VS Code webview (DPR 1.5) a plain noVNC canvas would be upscaled 1.5x (blurry); `kicad.html?fit=dpr` sizes the
canvas to `fb / devicePixelRatio` CSS px and lets noVNC scale, giving a crisp 1:1 image
(`img/vnc-novnc-07-vscode-webview-kicad.png`). There is no DPI negotiation: KiCad renders at the *host's* scale,
so a remote client on a 100 % display gets a 150 % UI and vice versa. Resizing the window from the client side
(`SetDesktopSize`) is not supported by TightVNC.

## 6. Multi-window (e)

See §2: menus and dialogs inside the rect are streamed; anything outside is lost; anything on top leaks in.
The footprint chooser was not opened (it is a separate top-level window whose position KiCad remembers, so it
would show only if it happens to land inside the rect). The Footprint Editor opened by a stray keystroke was a
second top-level window of the same process that the single-window share ignored entirely.

## 7. Clipboard (f)

* Host -> browser: setting the Windows clipboard produced `ServerCutText`; noVNC 1.7 writes it straight into
  `navigator.clipboard` (the `clipboard` event only fires as a fallback, which is why the first test read null).
  `Ctrl+A`, `Ctrl+C` in KiCad delivered the 155 054-character `(kicad_pcb ...)` s-expression into the browser clipboard.
* Browser -> host: `rfb.clipboardPasteFrom("...")` set the Windows clipboard (read back with `Get-Clipboard`).
  Only text; no images or files.

## 8. CPU / GPU (g)

`cpu.ps1` (process CPU-time deltas, one core = 100 %, plus GPU engine counters):

| state | pcbnew | tvnserver | Chromium (all) | websockify |
|---|---|---|---|---|
| idle, client connected, OpenGL | 3.7 % | 2.0 % | 12.9 % | ~0 (python child not attributed) |
| continuous pan + zoom through noVNC, 9 s | 9.5 % | **25.5 %** | 10.9 % | – |

No GPU engine usage attributable to the stack (the only busy 3D engine was an unrelated game process of the
owner). The server cost is pure CPU polling + Tight encoding of a 1600x1000 window; a full 4K desktop would be
~5x. UltraVNC's DXGI/DDEngine capture or TightVNC's mirror driver (driver install, admin) would cut this.

## 9. VS Code webview (j) – `prototypes/display/vscode-novnc/`

* Extension: `kiwrapVnc.open` command -> `WebviewPanel` whose HTML is a single `<iframe src="http://127.0.0.1:6080/kicad.html?nostatus=1&fit=dpr" allow="clipboard-read; clipboard-write">`
  with `portMapping 6080->6080`; plus a `CustomReadonlyEditorProvider` for `*.kicad_pcb` (priority `option`) that
  shows the same page when a board file is opened with it. Launched with an isolated profile:
  `code --extensionDevelopmentPath=<dir> --user-data-dir=<scratch> --extensions-dir=<scratch> --new-window <ws>`
  and `KIWRAP_VNC_AUTO_OPEN=1`.
* Result: the live KiCad window renders inside the editor area at 1:1 physical pixels
  (`img/vnc-novnc-07-vscode-webview-kicad.png`). The http iframe is allowed (127.0.0.1 is a "potentially
  trustworthy" origin, no mixed-content block); `portMapping` is a no-op locally and would only rewrite
  `localhost` *http* fetches in remote sessions – the RFB WebSocket does not go through it, so a remote/WSL
  setup needs a forwarded port instead.
* Keyboard, driven with host `SendInput` into the dev host window (`vscode_keys.ps1`, KiCad window captured on
  the host after each step, `img/vnc-novnc-08-vscode-keys-intercepted.png`):
  * `F1`, `Ctrl+Shift+P` -> VS Code Command Palette opened (88 % of the top area changed), KiCad unchanged.
  * `Ctrl+P` -> VS Code Quick Open, KiCad unchanged.
  * `Alt` -> VS Code menu bar mnemonic mode (File highlighted).
  * wheel over the canvas -> KiCad zoomed (71 % canvas change); middle-drag -> KiCad panned (99 %)
    (`img/vnc-novnc-09-vscode-wheel-pan-reach-kicad.png`).
  * `R`, `Delete`, `Ctrl+Z`, `Ctrl+Shift+Z`, `Esc`, `Ctrl+F`, `Home`: **inconclusive** in this environment – the first
    matrix ran with a modal Board Setup dialog already open (left by an earlier run), the second matrix lost the
    foreground to other agents' windows and the owner's own VS Code between steps, so I stopped injecting host
    input. Mechanically these keys are delivered to the cross-origin iframe first and VS Code has no global binding
    for bare letters/Delete/Esc, so they are expected to reach KiCad; `Ctrl+Z`/`Ctrl+F` are bound in VS Code only
    when a text editor has focus. Needs a re-run on a quiet desktop (10 minutes with the scripts below).
* Why VS Code wins for F1/Ctrl+P/Ctrl+Shift+P/Alt: VS Code's workbench receives keydown for webviews (the
  `did-keydown` relay) and additionally Electron's window-level menu accelerators (`Alt`, `F1`) fire before the
  iframe. Chords that VS Code owns globally cannot be given to KiCad without a keybinding shim (e.g. a webview
  `when` context that unbinds them while the KiCad panel is focused – possible for `Ctrl+P` style bindings via
  `keybindings` contributions with `when: activeWebviewPanelId == kiwrapVnc.panel`, not for the Electron menu accelerators).
* Focus: the VNC server injects into the *foreground* window on the host; when the user clicks into the VS Code
  webview, the host focus is VS Code, not KiCad – TightVNC clicks activate the KiCad window, but any other host
  window (or a KiCad dialog opened without focus) silently swallows keys. A product would have to keep
  `pcbnew` foreground (or hide it on a virtual desktop / second session) – this is the fundamental weakness of
  screen-sharing the user's own session.
* Multiple VS Code windows / remote: each viewer is another VNC client of the same window (TightVNC allows
  shared connections), so two panels show the same live window – fine for viewing, one cursor for input.

## 10. Platform coverage (h)

* Windows host: works (this spike). Requires a VNC server we ship or the user installs; TightVNC in app mode
  needs no admin or service.
* WSL2 / Linux: `-sharewindow` is TightVNC-Windows-only. On Linux the equivalents are `x11vnc -id <xid>` (X11
  only; WSLg's Weston runs KiCad as an X11/XWayland client, so it may work under WSLg's Xwayland) or, cleaner,
  running KiCad in its own headless Xvfb/Xvnc/wayvnc session per kiwrap session – that is also the only design
  that removes the focus/foreground problem on Windows (no equivalent exists on Windows without a second
  session or a virtual display driver).
* macOS: no single-window VNC share; would need the per-session virtual display design too.

## 11. Licensing (i)

| component | version | licence | how used |
|---|---|---|---|
| TightVNC server (`tvnserver.exe`) | 2.8.88 | GPL-2.0 (GlavSoft also sells commercial licences) | separate process, launched with argv; not linked. Shipping it in the extension = distributing a GPL binary (fine with source offer), or make it a user install. |
| UltraVNC | 1.8.3.0 | GPL-3.0 | evaluated only |
| noVNC | 1.7.0 | MPL-2.0 | JS served to the webview; file-level copyleft, compatible with MIT kiwrap |
| websockify (python) | 0.13 | LGPL-3.0 | separate process; could be replaced by a 50-line Node ws->tcp bridge inside the extension (MIT) |
| Playwright, pngjs | 1.63 | Apache-2.0 / MIT | test tooling only |
| KiCad | 10.0.6 | GPL-3.0 | unmodified |

## 12. Effort to productize (j)

Windows-only "Live KiCad tab" on top of this route, honest estimate **2-3 weeks**:
bundle/locate a VNC server and start it per kiwrap session with a random loopback port and a password
(TightVNC app mode reads HKCU, so per-session config means registry writes or a fork), track the `pcbnew`
HWND and re-issue `-sharewindow`/`-shareapp` on title changes and on new top-level windows (dialogs, footprint
chooser, footprint editor), a Node WebSocket bridge instead of websockify, reconnect/resize handling in the
page, `fit=dpr` + `SetDesktopSize`-less resizing, a keybinding shim for the chords VS Code owns, a "focus
guard" that re-foregrounds KiCad when the panel is focused, and a Linux path that is a different server.
What it will never give: tooltips, dialogs that open outside the rect, DPI negotiation, isolation from the
user's own desktop (the KiCad window must exist on the user's screen, so it is a picture-in-picture of a window
the user could just Alt-Tab to).

## 13. Reproduce

Scratch dir `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/vnc-novnc/`:

* `launch_pcbnew.ps1 [-Canvas cairo]` – sandboxed pcbnew on `proj/vncspike.kicad_pcb`
* `session.ps1 [-Canvas cairo] [-Mode window|app]` – launch + place topmost bottom-right + `-sharewindow` + verify
* `capture.ps1 -Out x.png (-ProcId N | -Title substr | -Screen) [-PrintWindow]` – Win32 CopyFromScreen/PrintWindow
* `pw/drive.mjs <tag>` – Playwright: fidelity/input/latency matrix -> `results/<tag>.json`, `shots/<tag>_*.png`
* `pw/popup.mjs ctx|menu|dialog|tooltip` – popup captured vs host at 0.3/1.5/4 s
* `pw/load.mjs` + `cpu.ps1` – fps under load and CPU/GPU sampling
* `pw/clip6.mjs` – clipboard both directions; `focus.ps1` – force foreground onto the shared window
* `vscode_launch.ps1`, `vsin.ps1`, `vscode_keys.ps1` – dev host launch, host-level input, key matrix
* `novnc/kicad.html` – the minimal noVNC page (copy into the prototype's own noVNC checkout when productizing)

Cleanup performed: my `pcbnew` (by PID), `tvnserver` (both processes), `websockify` + python children, the
Playwright Chromium, the dev-host VS Code (isolated `--user-data-dir`), `HKCU\Software\TightVNC` removed;
`%APPDATA%\kicad` hash unchanged.
