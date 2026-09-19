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
| `winstream/windows.py` | DPI awareness (pass the context as `c_void_p`!), `windows_of_pid`, `frame_bounds` (DWM), `resize_window`, `bring_to_front`, `focused_hwnd_of_thread` |
| `winstream/capture.py` | `WindowSession` (WGC per HWND), `MonitorCropSession` (menus), `ProcessTracker` (poll + sessions) |
| `winstream/inject.py` | `SendInputInjector`, `PostMessageInjector`, `KeyboardEvent.code` -> VK table |
| `winstream/server.py` | aiohttp app, frame header `<4sIIIIiiddII`, probes for latency measurement |
| `winstream/static/index.html` | viewer + input capture; `window.__stats()`, `__probe(ev)`, `__ping()` for the benchmarks |
| `bench/bench_a.mjs`, `bench/drive.mjs` | Playwright drivers |

## Dependencies and licences

`windows-capture` (MIT; drags in `opencv-python`, Apache-2.0), `simplejpeg` (MIT, libjpeg-turbo),
`aiohttp` (Apache-2.0), `numpy` (BSD-3), `pywin32` (PSF/BSD-like), `psutil` (BSD-3), `Pillow`
(MIT-CMU, measurements only). Node side: `playwright` (Apache-2.0), `pngjs` (MIT).

## Known limitations (measured, see the research doc)

- `SendInput` needs the window unobscured; keys need it foreground (focus stealing, races with the
  user's app). `PostMessage` mouse clicks do not select in KiCad (canvas uses the real cursor
  position); posted `Esc` does not cancel wx dialogs.
- Minimised or fully off-screen windows deliver no frames.
- Tooltips were never observed under injected hover.
- Menus are only cropped from the monitor that holds the main window.
