# kiwrap helpers (Windows, native)

Cargo workspace for kiwrap's native helpers. Windows only (decision D18); every crate is MIT like
the rest of the kiwrap distribution and links no GPL code (`docs/DISPLAY.md` §8.7).

- **`kiwrap-live`** — the Live KiCad helper: productization of the `winstream` prototype
  (`prototypes/display/winstream`). It attaches to a running, sandboxed KiCad GUI **by PID**,
  captures every editor frame as its own *surface* with `Windows.Graphics.Capture`, streams JPEG
  frames over a loopback WebSocket, and injects a client's input back in one of three modes.
  See [`kiwrap-live/README.md`](kiwrap-live/README.md).

```powershell
cargo build --release          # -> target/release/kiwrap-live.exe  (~2.8 MB, ~35 ms to ready)
cargo test  --release          # 26 unit tests (surface grouping, protocol, codec, bounds, image check)
cargo clippy --release         # clean
```

Toolchain: Rust 1.95 stable-msvc. `turbojpeg` builds libjpeg-turbo through CMake + NASM at first
build (both on PATH on the bench box); everything else is pure Rust.

## Integration test

`kiwrap-live/tests` is not a `cargo test` target (it drives a real KiCad GUI); it is a Python
harness run through the kiwrap uv workspace, so it can reuse `kiwrap.sandbox`, `kiwrap.installation`
and `kiwrap.formats`:

```powershell
uv run --project C:\Upwork\KiCad python helper\tests\live_smoke.py --out <dir> `
    --kicad-root "C:\Program Files\KiCad\10.0" --inject mirror        # stock KiCad, mirror
uv run --project C:\Upwork\KiCad python helper\tests\live_smoke.py --out <dir> `
    --kicad-root "C:\kiwrap-builds\6dd893f7d1-7" --inject events --cloak   # patched build, cloaked
```

It launches a sandboxed `pcbnew.exe` on a scratch copy of the ecc83 demo, starts the helper on that
PID, and asserts: the stdout hello line and token auth; the `pcb` surface appears with the right
record (kind, rect, dpi, cloaked flag); `meta` + JPEG stream frames with a consistent header; a
valid non-black screenshot PNG ≤ 1024 px; 0 frames while idle; per-mode input (double-click →
Footprint Properties composited, `Home` repaint, wheel-zoom latency, middle-drag pan, `WM_CLOSE`
dialog handling; in `events` mode the real cursor never moves); and the lifetime guard (the helper
dies with its parent, and reports `process_exit` + exits when KiCad is killed). Every KiCad it
launches is sandboxed, opened on a scratch copy, and killed by PID; `%APPDATA%\kicad` is never
written. `helper/tests/wsclient.py` is a dependency-free RFC 6455 client (handles frame
fragmentation; offers no permessage-deflate, like a VS Code webview).
