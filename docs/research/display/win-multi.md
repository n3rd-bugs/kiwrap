# Multi-editor capture on Windows: a KiCad *project* session (manager + pcbnew + eeschema + 3D) in VS Code

Hands-on spike, 2026-09-19/20, same machine as the other display spikes (Windows 11 26200, two 3840x2160
monitors at 150 %, RTX 5080), KiCad 10.0.6, sandboxed config (`kiwrap.sandbox.ensure`, `%APPDATA%\kicad`
never targeted by anything launched here). Owner question: *"can VNC + KiCad live on the Windows env?
remember a KiCad project can have multiple editors open at a time"*. Raw numbers:
[`win-multi/data/*.json`](win-multi/data/); screenshots `img/win-multi-*.png`, `img/win-input-*.png`;
code: [`prototypes/display/winstream`](../../../prototypes/display/winstream/README.md) (surfaces, event
hook, hybrid injector, `bench/multi/`) and
[`prototypes/display/vscode-shell`](../../../prototypes/display/vscode-shell/README.md) (`media/live.html`,
`tools/drive_live.ps1`).

**Caveat on every foreground/cursor measurement:** the desktop was shared with another agent's automation
(a source-built `pcbnew.exe` under a python window titled "cover (stands in for VS Code)" on monitor 2, plus a
pytest session using KiCad 8.0) that re-took the foreground and moved the cursor at random moments. Runs
that were visibly disturbed are marked; the VS Code driver retries until the foreground is verified.

## 1. Process model (measured)

Opening a project the way a user does (`kicad.exe ecc83-pp.kicad_pro`, then *Edit Schematic* Ctrl+E and
*Edit PCB* Ctrl+P from the manager, then `A` for the footprint chooser, `E` on a selection, Alt+3 for the 3D
viewer) gives **one process, one GUI thread, four unowned top-level frames plus owned dialogs**:

| Window | HWND (run) | Class | Owner | Thread | Note |
|---|---|---|---|---|---|
| `ecc83-pp — KiCad 10.0` (manager) | 78189602 | `wxWindowNR` | 0 | 92068 | pid 83788 = `kicad.exe` |
| `ecc83-pp — Schematic Editor` | 10161444 | `wxWindowNR` | 0 | 92068 | same pid, same thread (KIFACE DLL `_eeschema.dll`) — opened 1.1 s after Ctrl+E |
| `*ecc83-pp — PCB Editor` | 13444380 | `wxWindowNR` | 0 | 92068 | same pid/thread (`_pcbnew.dll`) — 3.4 s after Ctrl+P |
| `3D Viewer` | 46012570 | `wxWindowNR` | **0** | 92068 | an unowned sibling, not owned by pcbnew; opens over the pcbnew frame; 0.46 s after Alt+3 |
| `Footprint Reference Properties` | 45750426 | `#32770` | pcbnew frame | 92068 | modal: pcbnew frame `enabled=0` while open; 0.27-0.37 s after `E` |
| `Footprint Chooser (15464 items loaded)` | 45815962 | `wxWindowNR` | pcbnew frame | 92068 | DIALOG_SHIM, owned; opens at its **remembered position** (here 1.6 kpx left of pcbnew, over the manager) |
| hidden: 20+ `tooltips_class32`, 9 `ComboLBox`, `wxTimerHiddenWindow`, `wxDisplayHiddenWindow`, `IME`, `NVOpenGLPbuffer` (GL threads 80428/78968), `GDI+ Hook Window` (thread 48224) | | | | | never visible; a stray *visible* tooltip can sit anywhere on the desktop |

Full dump: [`data/windows_base_all.json`](win-multi/data/windows_base_all.json) /
[`data/probe_windows.json`](win-multi/data/probe_windows.json). Standalone case
([`data/standalone.json`](win-multi/data/standalone.json)): `pcbnew.exe` (pid 38188) and `eeschema.exe` (pid
77588) are separate processes with one frame each; no lock-warning dialog appeared although the manager
session had the same files open. Consequences for any capture route: **PID is not enough to tell the
editors apart** (one pid); the unit is the top-level HWND, and ownership (`GW_OWNER`) is what ties a dialog
or the chooser to its editor. Foreground checks by PID (the first winstream prototype) are wrong in a
project session: a key meant for pcbnew was going to eeschema until the check was changed to "is the
foreground HWND one of this surface's windows".

Host reference: [`img/win-multi-01`](img/win-multi-01-host-monitor2-manager-sch-pcb-3d.png) (monitor 2:
manager, schematic editor, PCB editor, 3D viewer).

## 2. Route 1: TightVNC `-controlapp -shareapp <pid>` + noVNC

Stack as in [vnc-novnc.md](vnc-novnc.md) (TightVNC 2.8.88 app mode, websockify, noVNC 1.7 in headless
Chromium via Playwright), but `-shareapp 83788` instead of `-sharewindow`. Data: `data/vnc_*.json`.

| Question | Result |
|---|---|
| Framebuffer | **the whole virtual desktop, 7680x2160** (`usama-home`), not a window rect: every pixel outside the app's windows is black (region stats: 100 % black, 1 colour) |
| All frames visible? | yes: manager, schematic editor, PCB editor, 3D viewer (GL) all present in one framebuffer at their desktop positions ([`img/win-multi-02`](img/win-multi-02-tightvnc-shareapp-framebuffer.png)) |
| Modal dialog / chooser | yes, wherever they open (the chooser at its remembered position over the manager: [`img/win-multi-04`](img/win-multi-04-tightvnc-shareapp-chooser.png)); dialogs of the same pid are part of the app region |
| Overlapping KiCad frames | composited by z-order (3D viewer over pcbnew shows the 3D viewer) |
| A foreign window on top (Notepad over pcbnew) | **black hole**: the covered part of pcbnew is black, the foreign window does not leak (unlike `-sharewindow`) but the KiCad content under it is lost ([`img/win-multi-03`](img/win-multi-03-tightvnc-shareapp-foreign-window-black-and-overlap.png)); a click there goes to the foreign window |
| Minimised frame (eeschema) | its rect goes to (-32000,-32000); its old area is black; nothing of it is streamed |
| Input routing | VNC pointer = the real cursor: wheel over pcbnew zooms pcbnew, wheel over eeschema zooms eeschema, keys go to the **foreground** frame (click first). Measured with per-rect timestamps: wheel -> first update in the frame **median 15-16 ms** (min 3-6, max 55-70), Home -> first rect 40-60 ms (`data/vnc_latency.json`) |
| Frame rate | **8.5-9 framebuffer updates/s** while panning pcbnew or eeschema, 9/s alternating between both (vs 39-45/s with `-sharewindow` at 1600x1000): the server polls the whole desktop |
| CPU | `tvnserver` **94-113 % of one core as soon as a client is connected, even idle** (0.6 % with no client); pcbnew 10-14 %; noVNC/Chromium not attributable |
| Interactive from noVNC | yes for what the foreground allows; the cursor/foreground semantics of vnc-novnc.md are unchanged (clicks activate the real windows) |

Verdict: `-shareapp` answers "all editors + dialogs + chooser + 3D in one stream" with a yes, but as a
16.6-Mpx polled desktop: 9 fps, a full core per client while idle, black holes for any covering window,
no per-editor mapping (the client sees one desktop-sized picture and would have to crop it per tab and
follow window moves itself). It is strictly worse than per-window WGC for the VS Code tab model.

## 3. Route 2: WGC per-window capture, one stream per editor frame ("surfaces")

`prototypes/display/winstream` was extended (all measured, `data/wgc_bench_multi.json`):

- **Surfaces.** Every visible top-level HWND of the pid is grouped by walking `GW_OWNER` to an unowned
  root; the root's title picks the kind (`pcb`, `sch`, `3d`, `mgr`, `fp`, `sym`, `gerber`). Owned dialogs and
  the chooser ride with their owner's surface; unowned popups (menus, tooltips, combo lists) attach to the
  root whose rect contains their centre and are **dropped if they belong to no root** (a stray tooltip window
  parked on monitor 1 otherwise grew the pcb stage to 3.5 kpx and blacked it out). `/surfaces` lists them;
  a client subscribes with `/ws?surface=pcb` and receives only that surface's windows with offsets
  relative to its root frame. `--attach-pid` attaches to a running `kicad.exe`; `--exe kicad` launches the
  manager.
- **`SetWinEventHook`** (create/destroy/show/hide/locationchange/minimise/menupopup for the pid,
  foreground for all pids) replaces the 100 ms `EnumWindows` poll: hook event -> new window list at the
  clients **11-15 ms** typical (26-68 ms during a dialog's create/show storm), fallback poll 250 ms. 822
  hook events / 562 rescans in a 2-minute run. A foreground log shows every focus flip (which surface,
  or "other").
- **`--gather-dialogs`**: an owned window that first shows outside its owner frame (the chooser 1.6 kpx to
  the left) is centred on the frame once (`SetWindowPos`, `SWP_NOACTIVATE`); the pcb stage stays 1920x1080
  instead of growing to 3581 px.
- **Per-surface input**: one injector per root, its raise/foreground checks use the surface's HWND set;
  keys go to the surface's topmost *enabled* window (a modal dialog, not the disabled frame).

Three headless clients (pcb 1920x1080, sch 1800x1200, 3d 1400x900) at once
([`img/win-multi-05`](img/win-multi-05-wgc-per-surface-clients.png)):

| Measure | Value |
|---|---|
| Idle, 3 clients | 0 frames / 3 s on all three; server 1-2 %, kicad 1 % |
| Input routing | wheel in the pcb client -> frames only for pcb (+1-3 title-bar repaints on sch when activation flips); wheel/Home in the sch client -> sch only; foreground follows the surface last clicked (`SendInput` injector) |
| Wheel -> painted, pcb client (sch + 3d live) | median **63-70 ms** (40-83); split ws->inject 1 ms, inject->WGC frame 41-42 ms, capture->paint 20-26 ms |
| Wheel -> painted, sch client | median **58-61 ms** (35-66) |
| Interleaved pcb/sch probes (tab switching) | 37-74 ms, no misses |
| Middle-drag pan pcbnew, others live | **25.7 fps** client = 25.7 fps server, 57.6 Mbit/s, decode 6.6 ms, server 13.7 % (psutil of the process) / 36 % (all python+capture threads), kicad 6-15 %, 3 Chromium clients 42 % |
| Middle-drag pan eeschema, others live | **25.9 fps**, 46.8 Mbit/s, server 19.7 % / 37 %, kicad 8-13 % |
| Both busy (pan pcbnew + wheel-zoom eeschema every 4th move) | pcb **26.9 fps** + sch 6.3 fps (one zoom step per wheel event), 57 Mbit/s, server 43 %, kicad 12 %, 3 clients 48 % |
| Properties dialog (`E`) | listed and painted in the pcb client 371-583 ms after the key (KiCad's own dialog build time; hook->list 11-15 ms of that), never in the sch client; composited at rel (405,209) |
| Footprint chooser (`A`) | gathered onto the pcb frame at rel (428,119), stage unchanged |
| 3D viewer | drag-rotate 24 fps, wheel zoom 12 fps once the window has been clicked once (a drag that starts with the activating click does nothing: 0 fps) |
| 3D viewer overlap | the other agent's windows covered the 3D viewer for part of the run; WGC per-window capture still delivered its frames (occlusion does not matter for capture, only for `SendInput`) |

## 4. Input while VS Code keeps the foreground: the "hybrid" injector

The known blocker (winstream §3): `SendInput` needs the KiCad window on-screen/unobscured/foreground, and
`PostMessage` clicks select nothing because the canvas reads `wxGetMousePosition()`. A third injector was
tried, `--inject hybrid`:

- pointer moves: `WM_MOUSEMOVE` posted with event coordinates (no cursor movement);
- buttons and wheel: **borrow the cursor** — `SetCursorPos(target)`, deliver the `WM_xBUTTON*` /
  `WM_MOUSEWHEEL` **synchronously** (`SendMessageTimeout`, 250 ms, so KiCad reads the cursor while it is
  there), `SetCursorPos(back)`; the target HWND is searched among *this surface's* windows only
  (`ChildWindowFromPointEx`), so a foreign window on top never receives the click;
- keys: posted to the thread's focus window; modifiers real (`GetKeyState`); a posted `Esc` whose focus
  window belongs to an owned top-level (a `#32770` or a DIALOG_SHIM like the chooser) becomes `WM_CLOSE`
  (posted `VK_ESCAPE` is ignored by wxDialog, measured before);
- right button: one real foreground flip (`TrackPopupMenu` in a background window closes immediately —
  measured: no `#32768` window ever appears without the flip).

Measured with **Notepad holding the foreground on monitor 1** and the pcb client driven headless
(`data/hybrid_matrix_run2_console.json`, [`img/win-input-01`](img/win-input-01-hybrid-select-rotate-delete-undo-notepad-foreground.png)):
click-select R3's reference (Properties panel 20-25 % changed), `R` (rotated, verified in the screenshot),
`Delete`, `Ctrl+Z` (run 1: restored; run 2: not — a run disturbed by the other agent's foreground theft, see
data), wheel zoom at the pointer (53-63 % canvas change), middle-drag pan (10-58 %), left-drag move of the
reference then undo, `E` -> modal dialog composited in the tab, `Esc` closes it
([`img/win-input-02`](img/win-input-02-hybrid-dialog-composited.png)), `A` -> chooser, typing filters it,
`Esc` closes it — **Notepad stayed the foreground window through all of it**, `foreground_fixes` 0-1 (the
right-click only, [`img/win-input-04`](img/win-input-04-hybrid-context-menu-needs-foreground.png)),
`SendMessageTimeout` timeouts 0. The user's cursor ends where it started after clicks and drags
(`cursorBefore == cursorAfter`).

What still moves the user's cursor: **KiCad itself** — `Home` (zoom to fit) warps the pointer to the canvas
centre (measured after every `Home` sent from a VS Code tab: cursor lands at the KiCad canvas centre on
monitor 2, e.g. (6736,612) / (4990,1466)); the same for every action with `warp cursor` semantics. A helper
can undo that (restore the cursor when it lands inside a captured surface right after an injected key), or
a fork patch can make the warp a no-op for injected input (the D17 route). Tooltips were again never seen.

## 5. Inside VS Code: two custom-editor tabs + a 3D panel from one helper

`prototypes/display/vscode-shell` got `media/live.html` (the winstream viewer as a webview page, surface
chosen from the file type: `.kicad_pcb` -> `pcb`, `.kicad_sch` -> `sch`; command *Open live KiCad surface*
for `3d`; stats/probe relay through the extension log) and `tools/drive_live.ps1` (launches the dev host,
opens both files side by side, opens the 3D panel, probes, pans, sends **real** `SendInput` into the tabs,
screenshots). Data: `data/vscode_live_lastrun.json`.

| Measure | Value |
|---|---|
| "A project with pcbnew + eeschema open" | two editor tabs, `ecc83-pp.kicad_pcb` and `ecc83-pp.kicad_sch`, each showing its live KiCad frame, connected 2.7 s after the open commands: [`img/win-multi-06`](img/win-multi-06-vscode-pcb-and-sch-tabs.png); with the 3D viewer as a third column: [`img/win-multi-07`](img/win-multi-07-vscode-pcb-sch-3d-panel.png). Scale 0.435 / 0.464 CSS px per physical px at DPR 1.5 in a 2600x1500 window (the tabs downscale; 1:1 needs a wider group) |
| Wheel -> painted in the tab | pcb tab median **59-66 ms** (51-69), sch tab **41-68 ms** (24-72) |
| Pan driven by another client, all three tabs live | pcb tab **24-28 fps** (sch/3d tabs 0), sch tab **26-29 fps**; CPU: helper 26-66 %, `Code.exe` (10 processes of the dev host) 36-52 % (one run 126 % while decoding the 1800x1200 sch stream), kicad 9-17 % |
| Real click + `Home` + wheel into the pcb tab (hybrid injector) | webview `pointerdown` -> helper `pcb down/up` at frame (1002,349) -> `Home` -> 5-6 pcb frames; **the dev host stays the foreground window before, after the click and after the key**; the cursor stays at (559,369) after the click; `sch` untouched. Same for the sch tab (`sch down/up`, 5-8 sch frames, pcb untouched). [`img/win-input-03`](img/win-input-03-vscode-click-in-pcb-tab.png) |
| What broke first | (1) `SendInput` injector: hovering a tab already teleports the cursor to monitor 2 and the click activates KiCad natively; (2) the helper's WebSocket died with 1002 on the first input burst from a VS Code webview — Electron sent permessage-deflate frames (RSV1) that aiohttp had not negotiated; fixed with `compress=False`; (3) interleaved `send_str`/`send_bytes` from two tasks — all writes now go through one sender per client; (4) a 39 s stall of webview->extension messages after a panel was created while the window was unfocused (seen once, not root-caused: the pages kept decoding, the stats burst arrived when focus returned) |
| Keybinding side | unchanged from vscode-shell.md: rescue block active, `window.customMenuBarAltFocus=false`, `enableMenuBarMnemonics=false` in the profile |

## 6. Answer to the owner's question

**Yes, VNC + KiCad "live" on Windows for a multi-editor project, but not the way the owner wants it.**
TightVNC's only multi-window mode (`-shareapp`) streams the entire desktop with everything that is not
KiCad blacked out: 9 fps, a full core per client while idle, black holes wherever VS Code (or anything)
covers a KiCad frame, and no per-editor mapping. The measured way to get *pcbnew in the `.kicad_pcb` tab
and eeschema in the `.kicad_sch` tab* is the WGC helper with per-frame surfaces: 25-29 fps per tab with
three streams live, 40-70 ms wheel-to-pixel, ~40 % of a core in the helper while one editor pans, 0 while
idle, dialogs and the chooser composited into the right tab within ~15 ms of appearing. With the hybrid
injector the mirror no longer fights VS Code for the foreground: clicks, drags, hotkeys, text, dialogs and
the chooser work while VS Code keeps the keyboard; the remaining cursor movements are KiCad's own warps
(fixable in the helper or the fork), context menus need one foreground flip, tooltips are still absent,
and the KiCad windows must still exist on-screen (occlusion is fine for capture; a second monitor or a
reserved area is still the honest setup). See DISPLAY.md §9 decisions 1/3/5: this spike moves decision 3
firmly to "build the helper" and gives decision 5 (fork patch) a much smaller scope (cursor warp + tooltip
hover), because the input wall was mostly a wall of the first injector, not of KiCad.

**Cross-reference (spike b, same day):** the leftovers of the hybrid injector — KiCad's cursor warps on
`Home`/`center_on_zoom`, the foreground flip for context menus, the window having to be on the desktop —
are all removed by the fork patch `mr/display-input` (`KICAD_INPUT_FROM_EVENTS=cloak`) measured in
[win-input-patch.md](win-input-patch.md): a cloaked pcbnew driven with `PostMessage` only, context menu
at the reported pointer, real cursor and foreground untouched. That spike ran standalone `pcbnew` from
the build tree, not a project session, so the surface model of §3 and the patched mode have not yet
been exercised together (DISPLAY.md §12.5, untested list). Folded into DISPLAY.md §12.

## 7. Reproduce

Scratch `…/scratchpad/display2/multi/`; copies in `prototypes/display/winstream/bench/multi/` (absolute
scratch paths inside, adjust): `session.py start` (manager + Ctrl+E + Ctrl+P, arranged on monitor 2),
`probe_windows.py` (dialog/chooser/3D + window dumps), `desk.py` (overlap/foreign/minimise/dialog/chooser),
TightVNC: `tvnserver -run` + `-controlapp -shareapp <pid>` + websockify + `vnc_multi.mjs <step>` /
`vnc_latency.mjs`; WGC: `python -m winstream.server --env env10.json --attach-pid <pid> --title KiCad
--gather-dialogs [--inject hybrid]` + `bench_multi.mjs` / `hybrid.mjs`; VS Code: `tools\drive_live.ps1
[-Phase all|hostinput]`. Cleanup performed: helper, `kicad.exe` session (by pid), standalone
`pcbnew.exe`/`eeschema.exe`, TightVNC + websockify (by pid) + `HKCU\Software\TightVNC`, Notepad, every dev
host (by user-data-dir). `%APPDATA%\kicad` hash: **changed during the spike, by a concurrent pytest run
against KiCad 8.0** (`8.0\kicad_common.json` `working_dir` = `…\pytest-of-usama\pytest-433\test_hierarchy_multi_unit_and_0\hier0`,
written 23:22:39; the `10.0` and `10.99` directories, the only ones a process launched here could touch, are
unchanged since 14-04 and 10:05 respectively).
