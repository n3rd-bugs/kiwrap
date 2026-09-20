# kiwrap-live — the Live KiCad helper

Productization of `prototypes/display/winstream` (see `docs/DISPLAY.md` §7.2 / §12.6 and the two
spike reports `docs/research/display/{winstream.md, win-multi.md, win-input-patch.md}`). Windows
only, MIT.

The helper is launched **by the VS Code extension or the `kw_live` MCP tool**, never by itself: they
launch a sandboxed KiCad GUI (`kiwrap.sandbox`, one at a time, short `TEMP`, killed by PID) and pass
the helper that KiCad's PID. The helper attaches, captures, streams and injects; it never owns the
KiCad process.

```
kiwrap-live.exe --pid <kicad pid> --listen 127.0.0.1:0 \
                [--inject none|mirror|events|hybrid] [--cloak-expected] [--fit-to-view] \
                [--fps 30] [--quality 70] [--gather-dialogs true] [--menus true] \
                [--esc-close auto|always|never] [--tile 32] [--parent-pid <pid>] \
                [--allow-any-process] [--token <t>]
```

The token is minted by the helper (18 random bytes, base64url) and reported only in the hello line;
`--token` exists for tests (a token on the command line is readable by every local process). `--pid`
must be a KiCad editor (`kicad.exe`, `pcbnew.exe`, `eeschema.exe`; exit code 4 otherwise) unless
`--allow-any-process` is given. `--fit-to-view` sizes a surface's frame to the client's viewport on
its `resize` input event (physical px, ignored under 200 x 150), so the stream is 1:1 in the tab.

On startup it prints exactly one JSON line on stdout and then logs to stderr:

```json
{"port": 51234, "token": "…", "version": "0.1.0", "pid": <kicad>, "helper_pid": <self>}
```

## Protocol (loopback WebSocket, `?token=` on every handshake)

### `/control` — JSON text both ways
Requests carry an optional `id`, echoed on the reply:

| request | reply |
|---|---|
| `{type:"surfaces"}` | `{type:"surfaces", surfaces:[SurfaceInfo]}` |
| `{type:"screenshot", surface, max_px, format?:"png"\|"jpeg", quality?}` | `{type:"screenshot", surface, ok, w, h, png\|jpeg:<base64>, rect_px, cloaked}` |
| `{type:"raise", surface}` | `{type:"raise", surface, ok}` (refused when cloaked) |
| `{type:"stats"}` | counters + injector stats |
| `{type:"keyframe", surface}` | forces a full frame |
| `{type:"hittest", surface, x, y}` | the child window under a surface point (diagnostics/drivers) |
| `{type:"place", surface, x?, y?, w?, h?}` | move/resize the editor frame (kw_live keeps a cloaked window on-screen) |
| `{type:"close_dialog", surface}` | WM_CLOSE the top owned dialog (a DIALOG_SHIM whose grid eats Esc) |

`SurfaceInfo` = `{id, kind: pcb\|sch\|3d\|mgr\|fp\|sym\|other, title, hwnd, rect_px, frame_px, dpi,
cloaked, cloak_flags, enabled, foreground, dialogs:[{hwnd,title,class,rect_px,offset,enabled,popup}]}`.

The server pushes `{type:"event", event:"create"|"destroy"|"title"|"rect"|"dialogs"|"cloak"|"foreground"|"process_exit", surface?, id?}`
on every change (driven by `SetWinEventHook`, no polling).

### `/stream?surface=<id>` — binary frames + JSON input
Server → client: a binary frame is an 8-byte little-endian header `[u32 frame_id][u16 w][u16 h]`
followed by JPEG. A JSON text `{type:"meta", w, h, dpi_scale, dirty:[[x,y,w,h]…], frame_id, surface,
rect_px}` precedes every size change; a `{type:"probe",…}` accompanies a latency-probed frame. Idle =
0 frames.

Client → server: `{type:"mouse"|"wheel"|"key"|"focus", x, y (surface px), buttons, button, dx, dy,
key, code, modifiers, down, probe?}`. Coordinates are surface pixels; the server maps them to screen
pixels, and a pointer event outside `[0, w) x [0, h)` is dropped and counted (`dropped_outside` in
the injector stats) rather than mapped onto the desktop. `modifiers` accepts `{ctrl,shift,alt,meta}`,
a name list, or a bitmask. `{type:"resize", w, h, dpi_scale}` sizes the frame with `--fit-to-view`;
`{type:"keyframe"}` asks for a full frame.

No permessage-deflate is negotiated (a VS Code webview sends RSV1 frames aiohttp rejected; measured);
one writer per client.

## Surfaces
A KiCad *project* session is one `kicad.exe` process whose editor frames are **unowned sibling**
top-level windows; dialogs and the footprint chooser are **owned** by their editor; menus, combo
drop-downs and tooltips are transient popups (`surfaces.rs`, unit-tested). The tracker groups every
window by its `GW_OWNER` chain into a surface per editor frame (`pcb`/`sch`/`3d`/`mgr`/`fp`/`sym`),
composites owned dialogs at their real offset, gathers a dialog that opens off-screen onto its frame
(`--gather-dialogs`), crops popup menus from a monitor capture (`--menus`; a per-window capture
dismisses a menu), and keeps **cloaked** windows (still composed by DWM). Tooltips are ignored; a
popup outside every root belongs to no surface.

## Injectors (one per launched session, `docs/DISPLAY.md` §12.6)
- **`none`** — capture-only view; a click on the page raises the real window via the `raise` control
  request (a user-gesture `SetForegroundWindow`), keys are not forwarded.
- **`events`** — `PostMessage` only, for a KiCad built with `mr/display-input`
  (`KICAD_INPUT_FROM_EVENTS=1|cloak`): hit-tests inside the process's own windows in z-order,
  double-click synthesis, `WM_MOUSELEAVE` hand-over, keys to the UI thread's focus window,
  Esc→`WM_CLOSE` on owned dialogs. Never touches the user's cursor, focus or keyboard; drives a
  **cloaked** KiCad (invisible on the desktop). This is "KiCad inside VS Code".
- **`mirror`** — real OS input via `SendInput` for stock KiCad on the desktop: raise-if-obscured
  before a click, raise-if-not-foreground before a key, release held keys on blur. Contained: a
  press is only sent when the window under the point belongs to the surface after the raise, a key
  only when the foreground root does (`dropped_obscured` / `dropped_foreground` in the stats), so a
  refused `SetForegroundWindow` never turns a click into input for whatever else is there.
- **`hybrid`** — posted input with a *borrowed* cursor for stock KiCad (`SendMessageTimeout` around
  a `SetCursorPos`), so VS Code keeps the foreground and the user's cursor; one foreground flip per
  right-click; real modifiers (stock wx reads `GetKeyState`).

## Lifetime
Two guards on the parent (extension host / `kw_live`): a kill-on-close Job Object whose only handle
is duplicated into the parent (the OS terminates the helper when the parent dies, even if wedged),
and a thread that waits on the parent handle and exits cleanly first. The helper also exits when the
target KiCad process disappears (pushes `process_exit`) and, when its stdin is a pipe, when the
launcher closes it (the extension's orderly stop: captures and hooks are released before exit).

## Measured (bench: Windows 11, 4K @ 150 %, 1920×1080 surface)
Startup ~35 ms to ready; first frame ~10-40 ms; screenshot (≤1024 px PNG) ~20-30 ms; JPEG encode
~4-5 ms/frame; wheel-zoom probe→painted 27-49 ms median (`events`, cloaked) / 24 ms (`hybrid`);
middle-drag pan 19-25 fps (39-55 frames/2.5 s); idle 0 frames / 0 bytes; the real cursor never moves
in `events` mode. Release binary ~2.8 MB. Full numbers per run in the harness's `report.json`.
