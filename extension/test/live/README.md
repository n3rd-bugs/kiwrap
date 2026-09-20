# Live KiCad tests (`extension/test/live`)

From `extension/` (Node 24 on PATH, `npm install` done by ext-core's package.json):

```
node test/live/run.mjs unit          # gen_contributes --check, tsc, node --test (protocol.js), mocha (host parts)
node test/live/run.mjs integration   # real VS Code dev host + fake helper (~30 s, a window appears, isolated profile)
node test/live/run.mjs all
```

| path | what |
|---|---|
| `unit/protocol.test.mjs` | `media/live/protocol.js` under `node --test`: 8-byte LE frame header + JPEG/WebP sniffing, `meta` parsing (array and object rects), fit/native layout, DPI-exact `clientToSurface` (zoom, clamping, inverse), mouse/wheel/key/focus/keyframe/resize/clipboard serialisation, HUD stats, stream URL, backoff |
| `unit/live.test.ts` | mocha: `HelperProcess` (hello parsing, argv golden, path resolution, a real spawn of the fake helper with crash + restart + dispose, no-hello timeout), `ControlClient` (id / FIFO matching, `ok:false` and `error` replies, kiwrap-live `hello` / `event` pushes and the older shapes, `process_exit`), surfaces model + wire normalisation, keybinding shield generation vs `contributes.live.json`, mode decision, `kw_live` result unwrapping (MCP result / Receipt / plain) |
| `fake_helper/` | dependency-free stand-in for `kiwrap-live.exe`: same argv and stdout hello, raw RFC 6455 server (`ws.mjs`), `/control` (hello with surfaces, `surfaces` / `screenshot` (PNG, `png.mjs`) / `raise` / `keyframe` / `ping`, `event:create` push of a 3D surface after `KIWRAP_FAKE_3D_DELAY_MS`), `/stream` (meta + synthetic baseline JPEG frames from `jpeg.mjs`: gradient, a bar moving with the frame id, a crosshair at the last pointer, a block per key held; frames only while active, idle = 0). Every input event lands in `KIWRAP_FAKE_HELPER_LOG` (JSONL). Env knobs: `KIWRAP_FAKE_FPS`, `KIWRAP_FAKE_ACTIVE_MS`, `KIWRAP_FAKE_QUALITY`, `KIWRAP_FAKE_SURFACES`, `KIWRAP_FAKE_ECHO_CLIPBOARD`, `KIWRAP_FAKE_CRASH_AFTER_MS`, `KIWRAP_FAKE_WATCH_PID` |
| `harness/` | test extension: `activateLive` alone with a fake `kw_live` client (returns an ext-core-shaped Receipt with the extension host's own pid and a patched-install capability) built by `build.mjs` into a scratch folder together with `contributes.live.json` and `media/live`; `KIWRAP_HARNESS_ATTACH_PID` / `_INJECT` / `_OPEN_FILE` auto-attach for the manual run |
| `integration/suite.ts` | runs inside the dev host (`--extensionTestsPath`): commands registered; `kiwrap.live.open` -> fake launch -> helper spawned with `--inject events --cloak-expected` -> surfaces (rects normalised) -> hidden mode; the `kiwrap.live` custom editor binds `pcb-1` and paints frames (960x600, aspect kept, frame ids parsed); synthetic DOM pointer/wheel/key events reach the helper log with DPI-exact surface coordinates (25 % / 50 % of the frame), button/buttons bitmask, middle button, Windows wheel sign, `{ctrl,shift,alt,meta}` modifiers, key up, resize hint; clipboard both ways; a 3D surface pushed later opens as a `kiwrap.live.panel` and streams; a hidden tab closes its stream after the grace period, reconnects + resyncs on reveal, and a quick switch sends `keyframe` on the open socket; `kiwrap.live.screenshot` returns a PNG (<= max_px); desktop screenshot of the dev host; close kills the helper and calls `kw_live detach`; view-only mode forwards nothing and raises on click; final close leaves no helper |
| `integration/runTest.ts` | outside runner: builds the harness, writes a scratch workspace (`ecc83-pp.kicad_pro/pcb/sch`), an isolated `--user-data-dir` with `kiwrap.live.helperPath` = fake helper, launches the installed `Code.exe` through `@vscode/test-electron` (`KIWRAP_VSCODE_EXE` overrides; downloads stable when none is installed), answers screenshot requests with `capture_window.ps1` (matches the window by `--user-data-dir`, so several dev hosts may run), prints `results.json`. `KIWRAP_LIVE_SCRATCH` sets the scratch dir |
| `manual/run_real_helper.ps1` | the real helper: copies the ecc83 demo, launches ONE sandboxed pcbnew/eeschema through `kiwrap.sandbox` (`prototypes/display/winstream/kicad_env.py`), starts the dev host with the harness auto-attaching `kiwrap-live.exe` to that pid (`-Inject none\|hybrid\|mirror\|events`, `-Cloak` for a build with `mr/display-input`), screenshots, then kills the dev host (by profile path), the helper and the KiCad by pid. `-AttachPid` reuses a KiCad you run yourself; `-KeepOpen` leaves everything up for hands-on testing |

The keybinding shield itself was measured with real `SendInput` in the prototype
(`docs/research/display/vscode-shell-keys.md`); here it is covered by generation tests (chords, `when` clauses,
palette guard) and by the manual run.

Compiled output goes to `test/live/.out/` (git-ignored); the scratch of the runs defaults to `%TEMP%\kiwrap-live-it`
/ `%TEMP%\kiwrap-live-manual`. Nothing writes to the user's VS Code profile or `%APPDATA%\kicad`.
