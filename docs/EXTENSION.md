# The kiwrap VS Code extension — architecture

Status 2026-09-20: Phase 2, being built. This document is the contract between the pieces
(extension host code under `extension/`, the Rust helper under `helper/`, the `kw_live` tool in
`src/kiwrap/core/tools/`, the packaging in `scripts/build_extension.ps1`) and the reasoning behind
it. The measured facts it relies on are in [DISPLAY.md](DISPLAY.md) (§7, §12) and
[research/display/](research/display/); the decisions in [DECISIONS.md](DECISIONS.md) (D13 Claude
Code inside VS Code, D17 data-first, D18 Windows + VS Code only, D19 private). User-facing
instructions are in [../extension/README.md](../extension/README.md).

Where this document and `extension/package.json` disagree on a setting or command name, the
`package.json` is authoritative and this file is stale.

## 1. What the extension is for

One sentence: it makes VS Code the place where a person and a Claude Code agent work on a KiCad
project together, with KiCad's real editors visible (and, on a patched build, driven) inside VS
Code tabs, and with kiwrap's checks in the Problems panel.

It is deliberately thin. Every fact about a project — items, nets, ERC/DRC, renders, stage gates —
comes from the `kw_*` tools of the kiwrap MCP server (ARCHITECTURE.md §9, §10); the extension is an
MCP client of that server, exactly like the agent is. It never parses `.kicad_*` files itself and
never talks to KiCad's IPC API directly.

What it adds on top of the server:

| surface | what it does | fed by |
|---|---|---|
| Live KiCad tabs | a `.kicad_pcb` / `.kicad_sch` opened "with KiCad (live)" shows the real editor window of a sandboxed KiCad, streamed by the helper; a 3D panel command; per-editor dialogs composited on their frame | `kw_live` + the helper |
| Previews | the same custom editor shows the `kw_pcb_render` / `kw_sch_render` SVG when no live session exists ("headless session, showing the kicad-cli preview") | `kw_*_render` |
| Problems panel | ERC/DRC findings with corrected positions as `Diagnostic`s on the file, refreshed on save and on demand | `kw_sch_check`, `kw_pcb_drc` |
| Status bar / picker | which KiCad install is the target (`kw_env` rows), which live session is bound, the stage the project is in | `kw_env`, `kw_project status`, `kw_live status` |
| Configure Claude Code | writes the workspace `.mcp.json` entry for the kiwrap server, installs the `kiwrap-flow` skill into `.claude/skills/`, points `AGENTS.md` at it | files only |
| Keybinding shield | while a Live KiCad tab has focus, VS Code chords go to KiCad instead of VS Code (§7) | `contributes.keybindings` |

Out of scope (D18): other IDEs, Linux/macOS, Copilot-facing `languageModelTools` facades (the
Phase-0 plan had them; Claude Code is the only certified agent client, D13).

## 2. Processes

```
VS Code window (one per workspace)
 ├─ extension host ........ extension/dist/extension.js (Node, VS Code >= 1.101)
 │    ├─ kiwrap server ..... `uv run --project <kiwrap checkout> kiwrap serve`   MCP over stdio, owned by the extension
 │    │    ├─ kicad-cli ..... short-lived: ERC/DRC/exports/renders, sandboxed KICAD_CONFIG_HOME
 │    │    ├─ api-server .... `kicad-cli api-server` headless IPC session (KiCad 11), per project, NOT started
 │    │    │                  while a live GUI session holds the project (contract 2)
 │    │    └─ KiCad GUI ..... kicad.exe / pcbnew.exe / eeschema.exe, sandboxed, API enabled, launched by kw_live
 │    └─ kiwrap-live.exe ... the helper: WGC capture of the GUI's windows + input injection, owned by the extension,
 │                           attached to the GUI by pid, one helper per live KiCad process
 └─ Claude Code (terminal or its own extension) ... starts ITS OWN `uv run kiwrap serve` from .mcp.json (§6)
```

Ownership and lifetime:

- **kiwrap server (extension's).** `uv run --project <checkout> kiwrap [--storage-home <home>]
  serve`, cwd = the checkout. Started lazily on first use (opening a `.kicad_*` file with the
  custom editor, running a check, the picker), restarted on crash with back-off, killed on
  `deactivate()`. One per VS Code window. Its stdio is the MCP transport; its stderr goes to the
  "kiwrap" output channel. It is a plain `uv run` of the private checkout: kiwrap is not on PyPI
  (D19), so the extension needs the checkout path (`kiwrap.serverProject`, auto-detected when the
  workspace is the checkout itself or `%KIWRAP_REPO%` points at it).
- **KiCad GUI.** Only ever started through `kw_live launch` in that server, which applies kiwrap's
  process rules: `kiwrap.sandbox.ensure(install, enable_api_server=True)` (sandboxed config root,
  user `%APPDATA%\kicad` read-only), a short `TEMP`, no window activation, PID tracked in the
  server's session table and killed on `detach` and on server exit. One GUI per project and one
  session per VS Code window (opening another project closes the current session first); a second
  `launch` on the same project returns the existing session. The GUI's API socket is the `ipc11`
  provider's endpoint for that project while the session lives, so an agent's `kw_pcb_place` lands
  in the window the user is looking at (DISPLAY.md §7.4: same process, two channels).
- **Helper.** Launched by the extension when a live session exists and a Live KiCad tab needs
  frames, as `kiwrap-live.exe --pid <kicad pid> --listen 127.0.0.1:0 [--inject
  none|mirror|hybrid|events] [--cloak-expected] [--fit-to-view]`; it mints its own token (18
  random bytes; `--token <t>` is accepted for tests only - a token on the command line is readable
  by every local process), prints one JSON line `{port, token, version}` on stdout and then
  serves. It refuses a `--pid` whose image is not `kicad.exe` / `pcbnew.exe` / `eeschema.exe`
  unless `--allow-any-process` is passed (the extension's attach command checks the same before
  spawning it). It exits when the KiCad process it watches is gone, when its stdin is closed by
  the launcher (the orderly stop: captures and hooks are released first) and the extension
  terminates it after a grace period on `detach`, on the last tab closing and on `deactivate()`.
  With `--fit-to-view` (passed in the injecting modes when `kiwrap.live.fitWindowToTab` is on) the
  helper sizes the captured window to the tab's viewport on the client's `resize` event, so the
  stream is 1:1 and KiCad's chrome stays legible. The exe ships inside the VSIX under
  `extension/bin/` (built by `scripts/build_extension.ps1`); a VSIX built with `-SkipHelper` still
  installs and does everything but the live view.
- **The agent's server.** Claude Code starts its own `kiwrap serve` from the workspace `.mcp.json`.
  It is a separate process with its own stdio; §6 says how the two coexist.

## 3. Channels

| channel | endpoints | transport | protection |
|---|---|---|---|
| MCP | extension host ↔ its kiwrap server; Claude Code ↔ its kiwrap server | stdio (`@modelcontextprotocol/sdk` stdio client; the server's `--http` mode is not used by the extension) | process boundary only |
| helper control | webview (via the extension host) ↔ helper | `ws://127.0.0.1:<port>/control?token=<t>`, JSON | loopback bind, per-launch token |
| helper stream | webview ↔ helper, one socket per surface | `ws://127.0.0.1:<port>/stream?surface=<id>&token=<t>`, binary frames + JSON input | loopback bind, per-launch token, webview CSP `connect-src ws://127.0.0.1:* ws://localhost:*` (the port changes on a helper restart) |
| KiCad IPC | kiwrap server (`ipc11` provider) ↔ KiCad GUI | named pipe under the sandbox's short temp dir, kicad-python client | KiCad's instance token (see §4), pipe ACL |
| capture | helper → Windows | `Windows.Graphics.Capture` per HWND (surfaces keyed on HWND + owner chain, `SetWinEventHook` for create/destroy/title/rect) | — |
| injection | helper → KiCad | `PostMessage` only (`events`) or the hybrid injector (`mirror`); never `SendInput`, never `SetForegroundWindow` on the user's behalf except the explicit `raise` | — |
| webview ↔ extension host | `postMessage` | VS Code's channel | VS Code's origin isolation |

### 3.1 Helper protocol (contract 1)

- `/control`: `{type:'surfaces'}` → `[{id, kind: pcb|sch|3d|mgr|fp|sym|other, title, hwnd,
  rect_px, dpi, cloaked, dialogs:[…]}]`; the helper pushes the same records as events on
  create/destroy/title/rect change. `{type:'screenshot', surface, max_px}` → base64 PNG
  (`kw_live screenshot` uses this when a helper is attached). `{type:'raise', surface}` brings the
  real window to the front — the only foreground change the product ever makes, and only from a
  user gesture in the tab.
- `/stream?surface=<id>`: binary frames with an 8-byte header `[u32 frame_id][u16 w][u16 h]`
  followed by JPEG or WebP; a JSON text frame `{type:'meta', w, h, dpi_scale, dirty:[…]}` precedes
  every size change. The client sends `{type:'mouse'|'wheel'|'key'|'focus', x, y (surface px),
  buttons, dx, dy, key, code, modifiers, down}`. Idle = 0 frames (WGC only delivers when content
  changes; DISPLAY.md §12.5 measured 0 idle, 27.6 fps in motion).
- Two Electron facts baked into the helper (DISPLAY.md §7.2): no `permessage-deflate` (Electron
  sends RSV1 frames) and a single writer per client, drop-to-latest.
- Surfaces, not pids: a project session is one `kicad.exe` whose editor frames are unowned
  siblings; dialogs and the footprint chooser are owned windows composited onto their frame's
  surface; menus and drop-downs are OS popups captured via the monitor-crop path (§5 hidden-mode
  limits).

### 3.2 `kw_live` (contract 2)

`kw_live(action, …)` with `launch | attach | status | screenshot | detach | raise`.
`launch(project, editor: pcb|sch|project, mode: auto|hidden|mirror, target_kicad?)` returns
`{session_id, pid, install, api_socket, token, editors:[…], mode_used, cloaked, hidden_available}`
(`install` is the install key `<version>@<root>`; `mode_used` is what the supervisor resolved -
`hidden` = `KICAD_INPUT_FROM_EVENTS=cloak` exported and the frame read back as `DWMWA_CLOAKED`,
`mirror` = visible on the desktop, never activated - and the extension decides its injector from
it: `events` for `hidden`, `none` otherwise); `attach(session_id, helper: {port, token, version,
pid?})` registers the helper the extension started so `screenshot` from ANY kiwrap server goes
through it; `status` returns the same plus liveness, the surfaces (`editors`: hwnd / rect_px /
dpi / cloaked) and `remote` / `owner_pid` (which server launched it); `screenshot` returns a
≤ 1024 px PNG through the helper when one is attached, else a `PrintWindow` capture of the frame
(works on a cloaked window), else the `kw_*_render` fallback (`degraded: [live_view_unavailable]`).
`mode` decides the env: `KICAD_INPUT_FROM_EVENTS=cloak` is set only when the install carries the
`display-input` patch (capability `gui.input_from_events`, derived from the manifest's `builds[]`
entry of the built commit) and `mode != mirror` (§5). `hidden` on a stock install is a teaching
error (E_UNSUPPORTED with the fix "use mode='mirror' or target a bench build that carries the
patch"). Implemented in `kiwrap.core.tools.live` over `kiwrap.backends.ipc.gui_sessions`.

### 3.3 Extension ↔ kiwrap (contract 3)

The extension calls `kw_env`, `kw_project`, `kw_sch_check`, `kw_pcb_drc`, `kw_live`,
`kw_sch_render` / `kw_pcb_render` like an agent would: same receipts, same teaching errors, same
stage gates. It does not get private tools. Receipts are rendered, not interpreted: a DRC receipt
becomes diagnostics one-to-one (`severity`, `pos_mm`, `description`, `rule`), and the extension
never re-derives a position from a file.

## 4. Security model

- **Loopback only.** The helper binds `127.0.0.1:0` (the OS picks the port); the kiwrap server's
  HTTP mode is not used; KiCad's API is a named pipe. Nothing listens on a routable interface.
- **Tokens.** The helper mints a fresh random token per launch (18 random bytes, base64url,
  `main.rs random_token`) and hands it to the extension in its stdout hello line; it is never on
  the command line (every local process can read argv; reviewer finding 11). It is required as a
  query parameter on every WebSocket handshake; a bad or missing token closes the socket before
  any frame. The token defends against the browser-origin attack (any web page can open
  `ws://127.0.0.1:<port>`), which is the realistic threat for a loopback service inside an
  Electron app. It does **not** defend against another process of the same Windows user — such a
  process can read the helper's memory, the pipe, and the KiCad window itself — and the design
  does not pretend otherwise. `kw_live attach` only accepts loopback hosts for the helper endpoint
  (`127.0.0.1`, `localhost`, `::1`), so a session record can never make either kiwrap server dial
  out. The `token` in a `kw_live` receipt is **KiCad's instance token**: the KIID the API server
  mints at start and returns in every response header; kiwrap sends it back on every request so a
  request of this session can never land in another KiCad that inherited the pipe path
  (`AS_TOKEN_MISMATCH`). It is not a secret and not an authentication - KiCad accepts requests that
  carry no token at all (`common/api/api_server.cpp`); the pipe's ACL is what limits access to the
  same user.
- **Webview CSP.** `default-src 'none'; img-src ${cspSource} data: blob:; style-src ${cspSource};
  font-src ${cspSource}; script-src 'nonce-<n>' ${cspSource}; connect-src ws://127.0.0.1:*
  ws://localhost:*; worker-src blob:` (`webviewHost.ts buildHtml`) — any loopback port, because
  the helper is restarted with a new port when it exits; the token in the URL is what binds the
  page to the right helper. No remote content, no `localResourceRoots` outside `extension/media`.
- **Attach targets.** `kiwrap.live.attach` (and the helper itself) attach only to `kicad.exe`,
  `pcbnew.exe` and `eeschema.exe`; the mirror (`SendInput`) injector drops events whose
  coordinates fall outside the surface and presses whose target window is not the surface's own
  after the raise attempt, so no input escapes the attached KiCad (reviewer findings 3, 15).
- **Settings scope.** `kiwrap.uvPath`, `kiwrap.serverProject`, `kiwrap.live.helperPath` and
  `kiwrap.live.nodePath` are machine-scoped: a repository's `.vscode/settings.json` cannot point
  the auto-started server or the helper at its own binaries (reviewer finding 14).
- **Process rules carried over from CLAUDE.md.** Sandboxed config roots (the user's `%APPDATA%\kicad`
  is never written; `scripts/ci/user_config_hash.py` proves it), one GUI per project, PID tracking,
  kill on detach/exit; orphan sweep on server start (`kiwrap.backends.ipc.sessions.sweep_orphans`
  and the GUI equivalent).
- **Privacy (D19).** No telemetry, no update checks, no marketplace: the VSIX is installed from a
  file. The helper makes no network connections except its loopback listener.
- **Unsigned binaries.** `kiwrap-live.exe` is not code-signed. SmartScreen may prompt on the first
  launch of an exe extracted from a downloaded VSIX; the README says what to click.

## 5. Live KiCad modes

| mode | KiCad build | env | injector | user experience | limits (measured, DISPLAY.md §12.5–12.6) |
|---|---|---|---|---|---|
| **view** (`auto` on stock) | any registered install | sandbox env only | `none` | live picture of every editor in its tab, dialogs composited, 0 cost idle; a click raises the real window (`raise`); keys are not forwarded | it is a picture; editing happens in the real KiCad window on the desktop |
| **hidden** (`auto` on a patched build) | a fork build carrying `mr/display-input` (`gui.input_from_events` true) | `KICAD_INPUT_FROM_EVENTS=cloak`, launched without activation | `events` (`PostMessage` only), `--cloak-expected` | KiCad's windows are cloaked (invisible on the desktop, still captured); the tab is the UI: select, move, rotate, wheel zoom centred on the tab's pointer, dialogs, chooser, context menus | popup menus and combobox drop-downs pop on the desktop at the pointer (OS windows, DISPLAY.md §12.8 decision 11); tooltips absent; one foreground grab at launch; only pcbnew measured, eeschema/other editors untested; the helper verifies the frame is `DWMWA_CLOAKED` and falls back to `view` (with a warning) when it is not |
| **mirror** (explicit only) | stock | sandbox env only | `mirror` (hybrid: posted moves, borrowed cursor for clicks/wheel, posted keys, `Esc` as `WM_CLOSE` on owned dialogs) | edit from the tab with the KiCad window still on the desktop (a second monitor is the honest setup) | KiCad's own cursor warps (`Home`, `center_on_zoom`) move the real mouse; one foreground flip per right-click; tooltips absent; dialogs/menus visible on the desktop; consent prompt on first use |

`auto` picks `hidden` when the target install's capability record lists `gui.input_from_events`
(the manifest's `builds[]` entry of the built commit lists `display-input`; the supervisor then
reads `DWMWA_CLOAKED` back from the frame and reports `mode_used`, and the extension runs the
helper with `--inject events --cloak-expected`), else `view`. `mirror` is never picked
automatically. The order in which the modes were built is the D0–D2 plan of DISPLAY.md §7.2:
capture-only first, hidden on builds carrying the patch, mirror as the opt-in.

What "a build carrying `mr/display-input`" means today (2026-09-20): the stock 10.0.6 and 10.99
installers do not have it; the registered bench build **`C:\kiwrap-builds\6dd893f7d1-7`**
(branch `kiwrap/6dd893f7d1+display` = `kiwrap/6dd893f7d1` + merge of `mr/display-input`, commit
`918923a346`, private origin only, D19) does, and `kiwrap_patches.json` lists it, so `kw_env`
shows `gui.input_from_events: true` and `kiwrap.patches: [..., display-input]` for it and
`auto` resolves to `hidden` there. The recipe for the next base bump is in `extension/README.md`
§"Hidden mode prerequisites" (merge `mr/display-input` onto a `kiwrap/<sha>+display` branch,
rebuild with the `msvc-win64-release` preset, `scripts/dev/install_kicad_build.ps1 -Register`,
add the `builds[]` entry to the manifest).

## 6. One kiwrap server or two?

Decision (with the ext-core contract): **two**. The extension runs its own `kiwrap serve`; Claude
Code runs its own from `.mcp.json`. They are separate processes that share no memory, and that is
fine because kiwrap's state is on disk and designed for multiple writers:

| shared thing | where | how the two coexist |
|---|---|---|
| storage home | `%KIWRAP_HOME%` or `%LOCALAPPDATA%\kiwrap`: sandbox config roots per install, engines (JRE/Freerouting), `installs.json`, `tmp/` | both read the same registry of installs and the same sandbox roots; `ConfigRoot.lock()` serialises seed writes |
| project state | `<project>/.kiwrap/` (receipts, stage, snapshots, jobs) | written under the per-project mutation lock `<project>/.kiwrap/lock` (`kiwrap.core.state.locks`: `O_EXCL` file + heartbeat + stale-break), which both servers take for every mutating tool |
| KiCad GUI session | one per project; the session record `<temp_dir>/gui-sessions/<pid>.json` (`temp_dir` = `<KIWRAP_HOME>/tmp`, next to the api-server records, `kiwrap.backends.ipc.gui_sessions`): pid, owner (`parent_pid`), session_id, token, API socket, install root / key, editor, mode_used, cloaked, files, the `~*.lck` files the GUI took, the helper endpoint once attached | whichever server launched it owns the process (`GuiSession`); the other server discovers the record (`discover_remote_session`, guarded against recycled pids by the recorded exe + document in the process's command line) and binds a `RemoteGuiSession` to the project (`sessions.bound_session_for`): its `ipc11` provider then talks to the recorded socket, so the agent's `kw_pcb_place` lands in the window the user watches (measured: 895 ms tool call, the tab repainted 446 ms after the call started). `kw_live status/screenshot/attach` answer from either server (`remote: true`, `owner_pid`); `detach` from the non-owner kills the GUI by pid and removes the recorded locks (the owner's liveness poll sees it go); the owner's exit kills the GUI through its job object |
| editor-lock refusal | `<project>/~*.lck` | the mutating tools' prologue (`kiwrap.core.tools._base.mutation`) exempts exactly the lock files the bound GUI session recorded (`release_project()['bound']['locks']`) when that KiCad has the headless IPC provider (`bound.ipc11`, capability `ipc.headless`: the write lands in the GUI), so `kw_pcb_place` / `kw_sch_check` / `kw_pcb_drc` run while the session is live; on a 10.0.x the GUI merely holds the files and the tools stay refused until `kw_live detach`; any other `~*.lck` is still "another KiCad holds the file". `kw_pcb_drc` does not write the refilled zones back while a session is bound (`save_board` defaults to false then, with a warning) |
| headless api-server | per project, per server | not started for a project while a GUI session record for it is alive (contract 2: `pooled_session()` hands out the bound GUI instead); otherwise each server may run its own headless session (they are cheap and the mutation lock keeps writes ordered) |
| helper | owned by the extension only | the agent never starts a helper; the extension registers the helper's port + token with `kw_live attach` right after the hello line (and again after a helper restart), the owner writes it into the record, and `kw_live screenshot` in the agent's server asks that helper; without one it captures the frame with `PrintWindow` (cloaked windows included), and only then falls back to a render |

Why not one server shared over HTTP: it would make the extension a supervisor for the agent's
transport (port, token, lifetime tied to a VS Code window), which is exactly the coupling D13/D18
avoid; stdio per client is what Claude Code expects and what `.mcp.json` already does. The cost of
two servers is a second Python process (~150 MB, idle) and the session-record dance above.

The one thing both servers MUST share is the storage home, because `temp_dir` (where the records
live) derives from `KIWRAP_HOME`: the extension's `kiwrap.storageHome` setting stays empty (no
`--storage-home`) and "Configure Claude Code" writes no `KIWRAP_HOME` into `.mcp.json` unless the
setting names one - then it writes the same value for the agent. Records are swept with the
api-server records (`sweep_orphans` at every launch: a record whose owning kiwrap is gone is
killed by pid and its locks removed; a live owner's record is left alone by the other server).
Verified end to end by `extension/test/e2e` (the extension's server launches, `test/e2e/agent.py`
- a second `kiwrap serve` over stdio - places, the tab repaints) and, without VS Code, by
`tests/test_live_remote_unit.py` (records, discovery, binding, lock exemption, detach).

## 7. Keybinding policy

Measured in `research/display/vscode-shell-keys.md`: with the Live KiCad tab focused, stock VS Code
acts on 54 chords (`Ctrl+W` disposes the tab, `Ctrl+P` opens Quick Open, `Ctrl+B`/`Ctrl+J` toggle
views, `F5` debugs, …) while the page still receives every key. The policy:

- **Shield on by default** (`kiwrap.live.shieldKeys`): a generated `contributes.keybindings` block
  (from the prototype's `tools/gen_keybindings.js` list) binds every such chord to
  `kiwrap.live.forwardKey` with `when: kiwrap.live.shieldKeys && (activeWebviewPanelId ==
  'kiwrap.live' || activeCustomEditorId == 'kiwrap.liveEditor')`. Both context keys are needed: one
  for the 3D panel, one for the custom editors.
- **Reserved for VS Code**, never shielded: `Ctrl+Shift+P` / `F1` (command palette — the way out),
  `Ctrl+0` (focus the side bar, the way to leave the tab by keyboard) and `Ctrl+Alt+K`
  (`kiwrap.live.releaseKeyboard`, toggles the shield for the tab). KiCad binds none of the three.
  When the shield is off, every chord behaves as in stock VS Code and the tab shows a "keys go to
  VS Code" badge.
- **Not shieldable** (custom title bar): `Alt` alone, `Alt+F/E/S/V/G/R/T/H` (menu mnemonics),
  `Alt+Space`, the `Apps` key. On first use of a live tab the extension offers, once, to set
  `window.customMenuBarAltFocus: false` and `window.enableMenuBarMnemonics: false` for the workspace
  (`kiwrap.live.menuBarPromptDone` remembers the answer). KiCad's own `Alt+3` (3D viewer) is fine.
- **Context menu.** `data-vscode-context='{"preventDefaultContextMenuItems": true}'` on the canvas
  removes VS Code's Cut/Copy/Paste overlay so right-click reaches KiCad.
- **Focus.** `retainContextWhenHidden: true` keeps the stream alive across tab switches; on
  `blur` the client sends `{type:'focus', down:false}` so the helper releases any keys it holds
  (no stuck modifiers when the user alt-tabs).

## 8. Settings and commands

Names as read by `extension/src/settings.ts` and `extension/src/live/*` (2026-09-20; the
`contributes.configuration` block in `package.json` is authoritative once it lands). Defaults in
parentheses.

| setting | meaning |
|---|---|
| `kiwrap.serverProject` (auto) | the kiwrap checkout (uv project) to run `uv run --project <path> kiwrap serve` from; empty = a workspace folder whose `pyproject.toml` names `kiwrap`, then `%KIWRAP_REPO%` |
| `kiwrap.uvPath` (`uv`) | the `uv` executable; `scripts/bootstrap.ps1` installs a private one under `<storage>/uv` when PATH has none |
| `kiwrap.storageHome` (see §6) | passed as `--storage-home` to the server and exported to the helper; the contract says "unset = kiwrap's own default" so that the agent's server and the extension's share one home |
| `kiwrap.kicadInstall` (newest) | the KiCad to target, a version spec `kw_env` lists ("Select KiCad" writes it) |
| `kiwrap.checkOnSave` (`true`) | re-run ERC / DRC when a `.kicad_sch` / `.kicad_pcb` is saved |
| `kiwrap.live.mode` (`auto`) | `auto` / `hidden` / `mirror` / `view` (§5) |
| `kiwrap.live.shieldKeys` (`true`) | the keybinding shield (§7); `kiwrap.live.keepCommandPalette` (`true`) leaves `Ctrl+Shift+P` / `F1` to VS Code |
| `kiwrap.live.helperPath` (bundled) | override `kiwrap-live.exe` (developer builds: `helper/target/release` is tried automatically); a configured path that is not a file is reported and the bundled helper used |
| `kiwrap.live.fit` (`fit`), `kiwrap.live.fitWindowToTab` (`true`) | `fit` scales the frame into the tab, `native` shows it 1:1 and scrolls; `fitWindowToTab` sizes the KiCad window to the tab in the injecting modes (hidden / mirror) so the stream is 1:1 and legible |
| `kiwrap.live.hud` (`false`), `kiwrap.live.pointerLock` (`false`) | webview diagnostics overlay (fps, RTT, frame age); pointer lock for infinite drags |
| `kiwrap.live.hiddenCloseDelayMs` | grace period before a hidden-mode session is torn down after its last tab closes |

Commands contributed by `package.json` (2026-09-20): `kiwrap.configureClaude` (Configure Claude
Code; `commands/configurePlan.ts` computes the edits: `.mcp.json`, a managed block in
`CLAUDE.md`/`AGENTS.md`, `.claude/skills/kiwrap-flow/`), `kiwrap.selectInstall` (Select KiCad
install), `kiwrap.pickProject`, `kiwrap.runErc` / `kiwrap.runDrc` / `kiwrap.runChecks`,
`kiwrap.openPreview` (the `kiwrap.preview` custom editor, `priority: option`, on `*.kicad_sch` /
`*.kicad_pcb`), `kiwrap.openLive` / `kiwrap.closeLive`, `kiwrap.restartServer`, `kiwrap.refresh`,
`kiwrap.showOutput`, `kiwrap.showProblems`; the view `kiwrap.status` ("KiCad (kiwrap)": tracked
project, install, stage, session). The live module adds (`src/shared/types.ts`; merged into
`package.json` from `contributes.live.json`): the `kiwrap.live` custom editor and `kiwrap.live.panel`
webview, `kiwrap.live.open` / `openSurface` / `close` / `status` / `raise` / `screenshot`, the
internal `kiwrap.live.forwardKey` and `kiwrap.core.callTool`, and the context keys
`kiwrap.live.shield`, `kiwrap.live.shieldPalette`, `kiwrap.live.active`.

## 9. Repo layout and build

```
extension/               MIT, TypeScript, esbuild bundle -> dist/extension.js; media/ (webview pages), bin/ (helper exe, built, git-ignored),
                         src/test (@vscode/test-electron), README.md (user-facing)
helper/                  MIT, Rust crate `kiwrap-live` (windows-capture MIT, turbojpeg/image, tokio, tungstenite); cargo build --release
scripts/build_extension.ps1   npm ci + esbuild + cargo build --release + copy exe into extension/bin + vsce package -> dist/kiwrap-<ver>.vsix
scripts/bootstrap.ps1         reports Node/Rust (does not install them)
.github/workflows/ci.yml      jobs helper (cargo build/test, windows-latest), extension (npm test, windows-latest), vsix (the script with -SkipHelper)
```

Tests, from cheap to real: `npm run test:unit` (mocha, a fake stdio MCP server), `node
test/live/run.mjs unit` (the live module without VS Code), `npm run test:integration`
(`@vscode/test-electron`: the real `uv run kiwrap serve` and kicad-cli, no GUI), `node
test/live/run.mjs integration` (a dev host with a fake helper: frames, DPI-exact input, clipboard,
panels), `helper/tests/live_smoke.py` (the Rust helper on a real sandboxed pcbnew, stock and
patched) and **`npm run test:e2e`** (`extension/test/e2e`: the whole product in a dev host - the
real server, the real helper, sandboxed stock and patched KiCad GUIs, and `test/e2e/agent.py`,
a second `kiwrap serve` playing the agent). The GUI-launching ones are owner-present runs on
this machine (one KiCad at a time, killed by pid, `%APPDATA%\kicad` hashed before and after),
never CI; the helper's tests are `cargo test`.

## 9.1 Verified end to end (2026-09-20, `npm run test:e2e`, 10/10)

Extension Development Host of VS Code 1.138, `kiwrap-live.exe` 0.1.0, scratch workspace = the
ecc83 demo copy + the Phase 1b blinker board, kiwrap's default storage home shared by both servers.
Evidence: `docs/research/display/img/phase2-*.png` (helper screenshots and PrintWindow captures of
the dev host, quantised to stay < 300 KB); numbers from `<scratch>/out/e2e-report.json`.

| step | what happened | measured |
|---|---|---|
| activate + Configure Claude Code | server ready, 33 tools; `.mcp.json` (`uv run --project <checkout> kiwrap serve`, no `KIWRAP_HOME`), the `CLAUDE.md` block and `.claude/skills/kiwrap-flow/SKILL.md` written | 3 files |
| preview | `ecc83-pp.kicad_pcb` in the `kiwrap.preview` editor: `kw_pcb_render` 3d 784x592 | 3.2 s to the image (`phase2-preview-pcb.png`) |
| Live KiCad, stock 10.99, view-only | `kw_live launch` (pcbnew, `SW_SHOWNOACTIVATE`, mode_used `mirror`), helper attached and registered with `kw_live attach`, the `.kicad_pcb` tab streams the 3840x2088 frame at 0.31 (DPR 1.5); the agent's server finds the session by project (`remote: true`, helper port visible) | session 3.9-4.1 s after the command, first frame in the tab 4.4-4.8 s (`phase2-live-pcb-tab.png`, `phase2-live-pcb-helper.png`) |
| agent edit (second MCP client) | `kw_pcb_get` + `kw_pcb_place C1 +5 mm` through `test/e2e/agent.py` on its own `kiwrap serve`: `provider_used ipc11:10.99.0` (the bound GUI), C1 141.605 -> 146.605 mm read back over the same socket; the tab repainted because KiCad's commit repainted (0 idle frames in the 1.5 s before) | `kw_pcb_place` 870-979 ms (4 runs); tab frame 431-474 ms after the call started (before the tool returned: the commit repaints, the save follows) (`phase2-place-before/after.png`, `phase2-place-after-tab.png`) |
| ERC / DRC while live | `kw_sch_check` 0/0, `kw_pcb_drc` 0 must_fix / 2 review / 80 cosmetic on the edited board -> 82 diagnostics with lines (`[Ln 135, Col 3]` = C1's uuid line; the courtyard overlap the agent's move created is the first entry); the GUI's lock files exempted; the refill not written back under the GUI | (`phase2-problems.png`) |
| close | helper + pcbnew gone, `~*.lck` removed, tree back to idle | < 0.4 s |
| `.kicad_sch` tab | a second session with `editor: sch` (eeschema) streams into the schematic's `kiwrap.live` tab | first frame 3.7-4.1 s (`phase2-live-sch-tab.png`, `phase2-live-sch-helper.png`) |
| hidden mode, build `6dd893f7d1-7` | `mode_used hidden`, frame `DWMWA_CLOAKED`, `--inject events --cloak-expected`; `WindowFromPoint` over the whole frame rect hits only the dev host (`phase2-hidden-desktop.png` shows the desktop with no KiCad), while the tab shows the full UI (`phase2-hidden-tab.png`, `phase2-hidden-helper.png`); posted input from the tab: wheel zoom, `Home`, click, `Ctrl+A` + `R` rotated 15/15 footprints (read back over ipc11 by the agent), `Ctrl+Z` restored them, `A` opened the Footprint Chooser composited on the cloaked surface (`phase2-hidden-dialog.png`), closed through the helper; `GetCursorPos` identical before and after; the desktop foreground unchanged by the input | ready 3-32 s (first start on a fresh sandbox root of that install: 32 s); input-to-frame 91-138 ms per event (wheel 92, Home 92, select-all 137, rotate 138, undo 91) |
| leftovers | `Get-Process kicad,pcbnew,eeschema,kiwrap-live` empty, no `~*.lck`, `%APPDATA%\kicad` hash unchanged | |

Re-verified after the review fixes of 2026-09-20 (`npm run test:e2e`, 12/12, 2 min; the same
scratch workspace, the same builds):

| step | what happened | measured |
|---|---|---|
| agent screenshot (finding 4) | `kw_live screenshot` from the agent's server answers `source: helper` (the `/control` reply is matched by request id past the hello, and kiwrap's `<kind>-<hwnd>` surface names are resolved to the helper's ids by hwnd) | 512x278 PNG, 135 KB |
| session identity (findings 7, 8) | `kiwrap.live.open` with the project directory and with the `.kicad_pro` path both reuse the running pcbnew (same pid); `kiwrap.openLive` on the `.kicad_sch` while the pcb session lives asks before replacing it - declined, the pcb session stays and the schematic tab shows its card | no relaunch |
| keybinding shield (finding 6) | real `Ctrl+N` (SendKeys) with the Explorer focused opens 1 untitled editor while a live tab is the active editor; with the live tab focused it opens 0 | 1 / 0 |
| checkOnSave (finding 1) | `kiwrap.checkOnSave: true`, one "Run DRC" without a live session (the refill rewrites the board): 0 automatic runs in the following 12 s, `lastDrc.at` unchanged, no DRC in flight | 1.5 s DRC, 0 follow-ups |
| hidden mode, fit to tab (finding 5) | `--fit-to-view`: the cloaked frame is sized to the tab's viewport on the webview's `resize` (3840x2088 -> 1902x1071 physical px at DPR 1.5), so the stream is 1:1 and the chrome legible; input-to-frame 45-92 ms | ready 3.2 s |
| agent edit through the bound GUI (finding 2, unit + `tests/test_gui_session_live.py`) | a rotating `kw_pcb_place` (file writer) makes the GUI reload the board (`live_view.reloaded`), the next `ipc11` commit keeps the rotation on disk; the schematic of a pcbnew session is answered by the file engine (finding 9) | `kw_pcb_place` 980 ms via `ipc11`, tab frame 445 ms after the call |

Known, measured, not fixed here: the launched frame takes the foreground once at launch on the
patched build even with `SW_SHOWNOACTIVATE` (DISPLAY.md §12.5); `GuiSession` now hands the
foreground back to the previous window (the ALT-tap route) and reports `foreground_taken` /
`foreground_restored` in `kw_live status`. Popup menus of a cloaked KiCad still appear on the
desktop (§12.8 decision 11); tooltips are absent in every mode.

Licensing: extension and helper MIT; no GPL code copied (the Python prototypes under
`prototypes/display/` were rewritten, not vendored; TightVNC/x11vnc are out of the product).

## 10. Open items

- The eeschema/other-editor matrix for hidden mode (DISPLAY.md §12.7 D5: 0.5 w) has not been run;
  `hidden` on a `.kicad_sch` tab is offered but flagged "untested" in the tab until it has.
- Menu/drop-down capture in hidden mode ships as the monitor-crop path (flash on the desktop);
  in-process cloaking of popups is a 2–3 day spike on the KiCad side (§12.8 decision 11).
- The `display-input` patch lives on the `kiwrap/6dd893f7d1+display` branch (build -7), not on
  the integration branch `kiwrap/6dd893f7d1` itself; every base bump merges it again (25 files on
  hot paths, DISPLAY.md §12.8 decision 5).
- On KiCad 10.0.x the planner's `ipc11` predicate is `ipc.headless`, so a bound GUI session is not
  used by the tools there (reads fall to the file engine, writes through the file writer while the
  GUI holds the file - the GUI does not reload them); the live view on 10.0.x is a picture. The
  predicate should also accept "a GUI session is bound to this project" (`sessions.bound_session_for`).
- The session record is the only cross-process channel; a third server on the same home also
  binds to the GUI (one KiCad per project, any number of readers/writers serialised by the
  project lock). `kw_live launch` from a second server on a project with a live foreign session
  is refused (E_BUSY) with the session id to use or detach.
- `.gitignore` gained `helper/target/`; `extension/bin/` (the helper copied for packaging) is
  ignored by `extension/.gitignore`.
