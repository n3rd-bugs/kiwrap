# winstream — capture a KiCad window with Windows.Graphics.Capture, stream it to a browser, inject input

Research prototype for `docs/research/display/winstream.md` (read that first: it says what works and
what does not). Windows-only. Not part of the `kiwrap` package; nothing under `src/` imports it.

## What it does

- launches `pcbnew.exe` (or `eeschema.exe`) with a **sandboxed** config root produced by
  `kiwrap.sandbox.ensure()` — the user's `%APPDATA%\kicad` is never written;
- captures every visible top-level window of that process with WGC (`windows-capture`), one
  session per window; popup menus / tooltips / combo drop-downs are cropped from a monitor capture
  instead because a per-window capture of a menu closes the menu;
- encodes JPEG (libjpeg-turbo via `simplejpeg`) on the capture thread and streams frames over a
  WebSocket (52-byte header + JPEG) with drop-to-latest per client;
- serves a viewer page that composites the windows on `<canvas>` elements at their real screen
  offsets, scales to fit, and sends mouse/wheel/keyboard events back;
- injects those events with `SendInput` (default) or `PostMessage` (`--inject postmessage`).

## Surfaces: a whole project session (manager + schematic + PCB + 3D) from one helper

A KiCad project opened through `kicad.exe` is **one process** whose editor frames are unowned sibling
top-level windows (dialogs and the footprint chooser are *owned* by their editor). The server groups the
process's windows into surfaces by owner chain (`windows.group_surfaces`: kinds `pcb`, `sch`, `3d`,
`mgr`, `fp`, `sym`, `gerber`), tracks them with `SetWinEventHook` (11-15 ms from the OS event to the
clients' window list; 250 ms poll as fallback) and serves each surface as its own stream:

```powershell
# attach to a running kicad.exe project session (launched with the sandbox env, see bench/multi/session.py)
C:\scratch\winstream\.venv\Scripts\python.exe -m winstream.server --env env10.json --attach-pid <pid> --title KiCad `
    --quality 70 --fps 30 --port 8765 --gather-dialogs --inject hybrid
# one client per editor frame: http://127.0.0.1:8765/?surface=pcb   ?surface=sch   ?surface=3d   ?surface=mgr
# GET /surfaces -> {kind, root, title, rect, hwnds, focus_target, foreground} per surface
```

`--gather-dialogs` centres an owned dialog/chooser that opens outside its editor frame on the frame (once,
on show), so a per-editor tab never has to grow to a union rect. `--inject hybrid` = posted input with a
*borrowed* cursor: buttons/wheel teleport the real cursor to the target, deliver the message synchronously
(`SendMessageTimeout`) and put the cursor back; keys are posted to the focus window; a posted `Esc` on an
owned dialog becomes `WM_CLOSE`; only right-click (popup menus) flips the foreground. Measured: select /
rotate / delete / undo / zoom / pan / drag / dialogs / chooser while another application keeps the
foreground (`docs/research/display/win-multi.md` §4). The WebSocket refuses permessage-deflate
(`compress=False`: a VS Code webview sent RSV1 frames) and has one writer per client.

Scripts of the project-session spike are in `bench/multi/` (absolute scratch paths inside; adjust):
`session.py start|windows|stop` (manager + Ctrl+E + Ctrl+P, arranged on monitor 2), `probe_windows.py`,
`desk.py` (overlap / foreign window / minimise / dialog / chooser), `standalone.py`, `cpu_sample.py`,
`vnc_multi.mjs` + `vnc_latency.mjs` (TightVNC `-shareapp` + noVNC), `bench_multi.mjs` (three surface
clients at once), `hybrid.mjs` (injector matrix behind a foreground Notepad).

## Run

```powershell
# 0. one-off: venv (kept OUT of the repo, e.g. in the scratchpad) and deps
uv venv --python 3.11 C:\scratch\winstream\.venv
uv pip install --python C:\scratch\winstream\.venv\Scripts\python.exe -r prototypes\display\winstream\requirements.txt

# 1. sandboxed env JSON for the KiCad you want (seeds kicad_common.json + lib tables, pre-warms)
cd C:\Upwork\KiCad
uv run python prototypes\display\winstream\kicad_env.py --kicad-root "C:\Program Files\KiCad\10.0" --storage C:\scratch\winstream\kiwrap-home > C:\scratch\winstream\env10.json

# 2. a scratch copy of a demo project (never open the install's demos in place)
Copy-Item -Recurse "C:\Program Files\KiCad\10.0\share\kicad\demos\ecc83" C:\scratch\winstream\ecc83

# 3. server: launches pcbnew, places the frame at (x,y) with the given PHYSICAL size, serves 127.0.0.1:8765
cd prototypes\display\winstream
C:\scratch\winstream\.venv\Scripts\python.exe -m winstream.server --env C:\scratch\winstream\env10.json `
    --pcb C:\scratch\winstream\ecc83\ecc83-pp.kicad_pcb --size 1920x1080 --pos 3900,100 `
    --quality 70 --fps 30 --inject sendinput --port 8765 [--token secret] [--topmost] [--no-children]

# 4. open http://127.0.0.1:8765/ in a browser (Chromium/Edge; add ?token=secret if set; ?fit=0 for 1:1)
#    GET /stats  /cursor  /clipboard  /shot?hwnd=<n>   POST /quit (also terminates the KiCad it launched)
```

Place the window where nothing else will cover it (`--pos 3900,100` = the second monitor on the
spike machine, physical pixels). `SendInput` clicks go to whatever window is on top at that point.

## Benchmarks (Playwright, headless Chromium)

```powershell
cd C:\scratch\winstream\pw ; npm init -y ; npm i playwright pngjs ; npx playwright install chromium
Copy-Item C:\Upwork\KiCad\prototypes\display\winstream\bench\*.mjs .
node bench_a.mjs C:\scratch\winstream\run1 8765          # mapping, idle/active fps, wheel latency probes
node drive.mjs   C:\scratch\winstream\run2 actions.json  # generic action list -> screenshots + diffs + stats
```

`drive.mjs` action vocabulary is documented at the top of the file; the action files used for the
report are reproduced in `bench/actions/`. Coordinates are frame pixels of the KiCad window
(physical px); with `Home` pressed first on the ecc83 demo at 1920x1080 the reference text of R3 is
at (778,820), the canvas centre is (1000,600).

## Files

| File | Purpose |
|---|---|
| `kicad_env.py` | prints `{version, pcbnew, eeschema, env}` using `kiwrap.sandbox` (run with `uv run` in the repo) |
| `winstream/windows.py` | DPI awareness (pass the context as `c_void_p`!), `windows_of_pid`, `frame_bounds` (DWM), `resize_window`, `bring_to_front`, `focused_hwnd_of_thread`, `WinEventHook` (SetWinEventHook), `group_surfaces` (owner chain -> surface kind) |
| `winstream/capture.py` | `WindowSession` (WGC per HWND), `MonitorCropSession` (menus), `ProcessTracker` (event hook + fallback poll, surfaces, `--gather-dialogs`) |
| `winstream/inject.py` | `SendInputInjector` (per-surface foreground/obscured checks), `PostMessageInjector`, `HybridInjector` (borrowed cursor), `KeyboardEvent.code` -> VK table |
| `winstream/server.py` | aiohttp app, `/ws?surface=<kind>` per-surface streams with per-root offsets, `/surfaces`, frame header `<4sIIIIiiddII`, probes for latency measurement |
| `winstream/static/index.html` | viewer + input capture; `window.__stats()`, `__probe(ev)`, `__ping()` for the benchmarks |
| `bench/bench_a.mjs`, `bench/drive.mjs` | Playwright drivers |

## Events-only input for the patched KiCad (`mr/display-input`, spike b)

Report: `docs/research/display/win-input-patch.md`. A KiCad built from the fork branch
`mr/display-input` and started with `KICAD_INPUT_FROM_EVENTS=1` (or `=cloak`) takes pointer, button
and key state from the messages it receives, so the editor can be driven with `PostMessage` only
while it is covered by another window or cloaked (invisible on the desktop, still captured by WGC).
Stock KiCad cannot (see "Known limitations").

```powershell
# env JSON for the build tree (reuses the sandbox roots of a kicad_env.py JSON of the same major.minor)
python bench\make_build_env.py --base <scratch>\env1099.json --build C:\src\kicaduild\msvc-win64-release `
    --set KICAD_INPUT_FROM_EVENTS=cloak > <scratch>\env-build.json
# server with the message-only injector (window ON-SCREEN: a fully off-screen window gets no WGC updates)
$env:WINSTREAM_ALLOW_CLOAKED = '1'
<venv>\python.exe run_events.py --env <scratch>\env-build.json --pcb <scratch>\ecc83\ecc83-pp.kicad_pcb `
    --size 1920x1080 --pos 5740,1060 --port 8766
# functional matrix over the raw WebSocket (no browser), then the Playwright benches against 8766
<venv>\python.exe bench\ws_drive.py 8766 <outdir> benchctions\events_edit_ops.json
node bench_a.mjs <outdir> 8766 ; node drive.mjs <outdir> benchctions\events_pan_bench.json 8766
```

| File | Purpose |
|---|---|
| `run_events.py` | wrapper: swaps the injector factory for `EventInjector`; `WINSTREAM_ALLOW_CLOAKED=1` lets the tracker keep cloaked windows (`server.py` untouched; fold into `--inject events`) |
| `winstream/inject_events.py` | `EventInjector`: hit-testing inside the process's own windows (z-order, disabled top-levels skipped), `WM_MOUSE*`/`WM_MOUSEWHEEL`/`WM_KEY*` with correct `wParam`/`lParam`, double-click synthesis, `WM_MOUSELEAVE` hand-over, keys to the UI thread's focus window (`GetGUIThreadInfo`), modifiers as posted keys; no `SendInput`/`SetForegroundWindow`/`SetFocus`/`AttachThreadInput` |
| `bench/make_build_env.py` | env JSON for a build tree: DLL dirs + vcpkg bins on PATH, `KICAD_RUN_FROM_BUILD_DIR=1`, extra `--set VAR=VALUE` |
| `bench/ws_drive.py` | raw WebSocket driver: `click/dblclick/drag/wheel/key/chord/type/waitwin/clickwin/shot/shotall/windows/cursor/stats` |
| `bench/actions/events_*.json` | the matrices of the report (edit ops, drag/double-click, properties dialog, context menu + tooltip, chooser, cloaked matrix, pan bench) |
| `bench/dbgview.py` | `OutputDebugString` listener for the `KICAD_INPUT_FROM_EVENTS` trace (`KICAD_ENABLE_WXTRACE=1 WXTRACE=KICAD_INPUT_FROM_EVENTS`) |

## Dependencies and licences

`windows-capture` (MIT; drags in `opencv-python`, Apache-2.0), `simplejpeg` (MIT, libjpeg-turbo),
`aiohttp` (Apache-2.0), `numpy` (BSD-3), `pywin32` (PSF/BSD-like), `psutil` (BSD-3), `Pillow`
(MIT-CMU, measurements only). Node side: `playwright` (Apache-2.0), `pngjs` (MIT).

## Known limitations (measured, see the research doc)

- `SendInput` needs the window unobscured; keys need it foreground (focus stealing, races with the
  user's app). `PostMessage` mouse clicks do not select in KiCad (canvas uses the real cursor
  position); posted `Esc` does not cancel wx dialogs. `hybrid` fixes both (borrowed cursor, `WM_CLOSE`)
  but KiCad's own cursor warps (`Home`) still move the user's pointer, and popup menus need a real
  foreground flip.
- A KiCad project session is one pid: foreground/obscured checks must be per surface (HWND set), not per pid.
- Minimised or fully off-screen windows deliver no frames (DWM stops composing them; `PrintWindow`
  polling works but costs 87 ms per 1080p frame). On-screen-but-covered and cloaked windows do.
- The patched KiCad (`KICAD_INPUT_FROM_EVENTS`) removes the PostMessage limits above, except tooltips
  and the placement of popup menus (OS windows on the desktop).
- Tooltips were never observed under injected hover.
- Menus are only cropped from the monitor that holds the main window.
