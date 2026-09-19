# Display route: Linux KiCad in WSL2, streamed to a browser (Broadway, xpra HTML5, x11vnc + noVNC)

Hands-on spike, 2026-09-19. Host: Windows 11 Pro 26200, Core Ultra 9 285K (24 threads), 127 GB, NVIDIA + Intel
iGPU, two 3840x2160 monitors. Guest: WSL2 Ubuntu 24.04.3 (kernel 6.18.33.2-microsoft, WSLg 1.0.73.2, Mesa 25.2.8),
KiCad **10.0.6~ubuntu24.04.1** from `ppa:kicad/kicad-10.0-releases`, wxGTK 3.2.4 (Ubuntu build, no EGL), GTK
3.24.41, **xpra 6.5.3-r0 + xpra-html5 21** (xpra.org repo; Ubuntu's own xpra is 3.1.5 without html5),
x11vnc 0.9.16, noVNC 1.3.0, VirtualGL 3.1.5. Client: Chromium 153 driven by Playwright 1.63 on the Windows side,
hitting WSL's localhost forwarding. Prototype + scripts: [`prototypes/display/xpra-wsl/`](../../../prototypes/display/xpra-wsl/README.md).
Screenshots (all taken by Playwright from the browser page, i.e. what the streamed client sees): `wsl-linux/*.png`.

**Question asked:** can a Linux KiCad (WSL now; a Linux host or container later) be forwarded into VS Code with the
identical KiCad UI/UX, and which of Broadway / xpra / VNC is the right transport?

**Short answer:** **xpra HTML5 is the Linux transport.** It shows the real pcbnew UI with the OpenGL GAL canvas
(software GL via Xvfb + llvmpipe), menus, context menus, tooltips, modal dialogs and the footprint chooser (with its
own GL preview) as seamless windows in the browser, with working mouse/keyboard/modifiers/hotkeys, two-way clipboard,
session persistence across reconnects, ~57 fps while panning, 18-52 ms wheel-to-pixels latency and 68-111 ms
key-to-pixels latency. Costs: pcbnew burns 4-7 cores on llvmpipe while panning (GPU offload via VirtualGL is not
possible under WSL2; it is on a real Linux host), HiDPI needs server-side DPI, and it is a 1.5 GB Linux install
that does not help a Windows-KiCad user. **GTK Broadway is not viable** for KiCad: KiCad and wxWidgets both
hard-pin GDK to X11, it has no GL at all (Cairo fallback with nag dialogs), middle-drag pan runs at ~1.5 fps and
broadwayd crashed twice in ten minutes. **x11vnc on WSLg's own display is not viable** (GL canvas comes out
black, menus are not captured); x11vnc on a private Xvfb works but is strictly less capable than xpra.

---

## 1. Environment facts [VERIFIED LOCALLY]

| Item | Result |
|---|---|
| WSLg | present: `/mnt/wslg`, `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`, `XDG_RUNTIME_DIR=/run/user/1000`; X screen 7680x2160 (two 4K monitors, 96 dpi reported); a pcbnew window on `:0` is 3828x2127 pixels (image `00-…`) |
| GPU | `/dev/dxg` present, `/usr/lib/wsl/lib/libd3d12.so` present, `nvidia-smi` works. Default `glxinfo -B` on `:0` = **llvmpipe** (Mesa's loader does not pick d3d12 by itself; `dmesg` shows `dxgkio_query_adapter_info: Ioctl failed`). With `GALLIUM_DRIVER=d3d12`: **"D3D12 (Intel(R) Graphics)", Accelerated: yes** (adapter 0 = the iGPU; `MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA` should select the RTX, not tried). `MESA_LOADER_DRIVER_OVERRIDE=d3d12` does nothing; EGL on Wayland stays llvmpipe. |
| glxgears | llvmpipe on Xvfb: 2000-2950 fps; d3d12 on WSLg `:0`: 220-280 fps (composited through RDP) — the 24-core CPU is faster than the WSLg GPU path for this trivial load |
| KiCad PPA | `kicad 10.0.6~ubuntu24.04.1`; binaries `/usr/bin/pcbnew` (launcher) + `/usr/bin/_pcbnew.kiface`; libs `libkicommon.so.10.0.6`, `libkigal`, `libkiapi`; SWIG `pcbnew.py` for system Python 3.12 (as documented in `docs/LINUX.md`) |
| Sandbox on Linux | `KICAD_CONFIG_HOME` / `KICAD_DOCUMENTS_HOME` / `KICAD_CACHE_HOME` are honoured exactly as on Windows: kicad-cli and pcbnew wrote `10.0/{kicad.json,pcbnew.json,colors/,3d/,toolbars/}` under the sandbox root, never under `~/.config`. pcbnew started **wizard-free** with the kiwrap seed (kicad_common.json flags + the three template lib tables). **Trap:** one `kicad-cli version` run *without* the env vars created `~/.config/kicad/10.0/{kicad_common.json,kicad.json}` — it had not existed before and was deleted; every later command sourced the env file. `~/.config/kicad` is absent for both `usama` and `root` at the end of the spike. |
| WSL process lifetime | children of `wsl -d Ubuntu -- bash -c "…"` die when the call returns; servers must be started with `setsid -f` or `--daemon=yes` |
| Ports | WSL localhost forwarding worked for 8085 (broadwayd), 14600 (xpra), 6081 (websockify). **14500 is already bound by the xpra package's `xpra.socket` systemd unit** (auth-protected proxy) — bind another port |

## 2. What was measured and how

`prototypes/display/xpra-wsl/pw/drive.js` (Playwright, headed Chromium 1600x950, DPR 1) executes JSON step lists
and measures with the CDP screencast: **latency** = synthetic wheel/key event → first screencast frame that
changed (includes Chromium's own capture cost, a few ms); **fps** = screencast frames during a continuous
middle-button drag (8 ms between mouse moves). CPU: `top -p <pid>` in WSL during the 8 s pan. Screenshots are the
browser page. Same battery for every route: View menu, right-click context menu, toolbar tooltip, wheel zoom,
middle-drag pan, left-drag box select, click-select + `R` (rotate) + `Ctrl+Z`, `E` (properties dialog), `A`
(footprint chooser) + typing in its filter, `Esc`, clipboard both directions, reconnect, DPR 2.

## 3. GTK Broadway (`broadwayd :5` + `GDK_BACKEND=broadway`) — **not viable** (partial at best)

Pipeline sanity: a plain GTK3 app (`gtk3-widget-factory`) rendered in Chromium through broadwayd on
`http://localhost:8085` at once (image `01-…`). Then the KiCad-specific problems:

1. **pcbnew ignores `GDK_BACKEND=broadway`** and connects to `/tmp/.X11-unix/X0` (strace). Two independent causes,
   proven with an `LD_PRELOAD` shim that logs and neuters them (`wsl/noallow.c`):
   - `/usr/bin/pcbnew` calls **`setenv("GDK_BACKEND", "x11")`** at startup (the string is a wide `wxString`
     literal, which is why `strings` without `-e L` does not find it). `KICAD_WAYLAND=1` (the only related env var
     in the binaries) did not change the behaviour on 10.0.6.
   - `libwx_gtk3u_gl-3.2.so.0` calls **`gdk_set_allowed_backends("x11")`** when the GL module loads (this Ubuntu
     wx build has no EGL: "This program wasn't compiled with EGL support required under Wayland").
   The same two pins are what stop KiCad from running natively on Wayland, so this is not Broadway-specific.
2. With both neutered pcbnew connects to Broadway and shows **"Fatal Error: wxGLCanvas is only supported on
   Wayland and X11 currently"** (image `02-…`; a `wxLogFatalError`-style dialog that is in fact not fatal), then
   **"Could not use OpenGL, falling back to software rendering"**, then the complete pcbnew UI in the **Cairo GAL**
   (image `03-…`). Pre-setting `graphics.canvas_type = 2` (Cairo) in `pcbnew.json` does **not** avoid the
   wxGLCanvas creation attempt; every later wxGLCanvas (footprint chooser preview) nags again (`05-…`). The 3D
   viewer is impossible.
3. Interaction (images `04-…`, `05-…`): View menu, context menu, "Zoom In" tooltip, wheel zoom (Zoom 3.79→4.55),
   `R` rotates the selected item, `Ctrl+Z` undoes, `E` opens *Copper Zone Properties* as a second in-page window,
   left-drag box select streams at **53 fps**, but **middle-drag pan updates at ~1.5 fps** (3 frames in 2 s;
   full-canvas Cairo redraw + Broadway diffing) — unusable for panning. Wheel→pixels **53-65 ms**, key `R`→pixels
   **75-91 ms**.
4. Clipboard: KiCad-internal copy works ("Selection copied"); Broadway has no bridge to the browser clipboard.
5. **broadwayd 3.24.41 crashed twice in ~10 minutes** (`can't write to client` then
   `broadwayd.c:61:client_free: assertion failed: (client->disconnect_idle == 0)`), once when the browser closed
   and once mid-session while the chooser was opening; **pcbnew dies with its display**. Broadway is also
   single-client by design and has no GL. Footprint chooser never became visible before the second crash.
6. HiDPI: not measured (moot).

Effort to productize: would need a patched wxWidgets (drop the backend pin, EGL build) and a patched KiCad launcher,
plus a Cairo-only canvas; still no 3D viewer, single client, and an unstable daemon. Not worth it.

## 4. xpra 6.5.3 seamless + HTML5 client — **works** (the Linux route)

Command (see `wsl/start_xpra.sh`): `xpra start :100 --html=on --bind-tcp=0.0.0.0:14600 --dpi=96
--env=KICAD_CONFIG_HOME=… --start="pcbnew <scratch ecc83-pp.kicad_pcb>" --clipboard=yes --notifications=no
--pulseaudio=no --speaker=no --microphone=no --webcam=no --printing=no --file-transfer=no --systemd-run=no
--mdns=no --exit-with-children=no`. xpra spawns `Xvfb-for-Xpra-100 +extension GLX … -screen 0 7680x2160x24`; GL
inside is llvmpipe ("Error querying drm devices: -2" in the log is harmless). Client URL
`http://localhost:14600/?keyboard_layout=us&sound=false&clipboard=true&floating_menu=false`.

| Criterion | Result [VERIFIED LOCALLY] |
|---|---|
| (a) Fidelity | Real pcbnew, **OpenGL GAL rendered** (no fallback dialog), seamless window with the HTML5 client's own title bar (image `06-…`). View menu, context menu (full footprint menu), toolbar tooltip: yes (`07-…`). Footprint Properties dialog and the Footprint Chooser ("15460 items loaded") open as **separate seamless windows** with their own decorations; the chooser's **wxGLCanvas preview renders** the 0805 pads (`08-…`). Typing `0805` filters the chooser. |
| (b) Input | Mouse move/click/right-click/wheel/left-drag box select/middle-drag pan: yes. Keys: `R` rotate, `Ctrl+Z` undo, `E`, `A`, `Esc`, `Home`, `Ctrl+A`/`Ctrl+C`/`Ctrl+V`, text typing: yes (`keyboard_layout=us`). |
| (c) Latency / fps | Wheel→first changed frame: **29, 52, 34, 18, 36, 33 ms**. Key `R`→pixels: **68, 82, 111, 97 ms**. Middle-drag pan: **57 fps** (171 frames / 3 s; 452-463 frames / 8 s in three runs) — the canvas moves smoothly (`07-…`, bottom right). Page load 200 ms, first paint of the app ~1 s after connect. Encoding `auto` (server offers h264, vp9, vp8, webp, png, jpeg, avif, rgb, scroll). |
| (d) HiDPI | The HTML5 client draws in CSS pixels: at DPR 2 the canvas backing store stays 1600x950 and the 96-dpi render is upscaled (blurry, `09-…` bottom). Server log: `DPI set to 21 x 44 (wanted 96 x 96)` (Xvfb quirk, xpra suggests `--dpi`), client reported "Chromium 153 (423x251 mm - DPI: 96x96)". A crisp 2x session needs `--dpi=192` + `GDK_SCALE=2` for KiCad (not measured). |
| (e) Multi-window | Modal dialog and the footprint chooser are separate windows, draggable, with modality preserved (`08-…`). Menus/tooltips are override-redirect windows and just work. Desktop mode (`xpra start-desktop`) was not needed. |
| (f) Clipboard | **Both directions verified**: `navigator.clipboard.writeText('hello-from-browser-clipboard')` then `Ctrl+V` in the chooser filter pasted it (`09-…` top left); a unique string typed inside KiCad, `Ctrl+A`, `Ctrl+C`, then `navigator.clipboard.readText()` returned it. Needs the page to have clipboard permission (Playwright granted it; in a VS Code webview the iframe needs `allow="clipboard-read; clipboard-write"`). |
| (g) CPU/GPU | During the 8 s pan: **pcbnew 410-480 % of a core (peaks 700 %)** — llvmpipe rasterising a 1600x950 GL canvas; **xpra server 36-56 %** of one core; **Xvfb 7-11 %**; whole 24-thread box 15 % busy. Idle: pcbnew 0-1 %. RSS: pcbnew 2.3 GB, xpra 179 MB, Xvfb 160 MB. No GPU use. **VirtualGL 3.1.5 cannot offload under WSL2**: `vglrun -d :0` (WSLg Xwayland, d3d12) fails with `glx: failed to create drisw screen` / `GLXBadValue X_GLXCreateNewContext`; the EGL backend needs a DRM render node that WSL does not expose. On a real Linux host `vglrun -d /dev/dri/renderD128` is the standard fix. |
| Reconnect | Closing the browser and opening a new context restores the session with its state (zoom, rotated U1) (`09-…` top right). The X session outlives clients; `--exit-with-children=no` keeps it alive if pcbnew exits. |
| Stability | No crash in ~20 minutes of driving. (One xpra death was self-inflicted: `pkill -f "pcbnew.*ecc83"` also matched the xpra server's command line, which contains `--start=pcbnew …/ecc83…`. Use `pkill -x`.) |
| (h) Platforms | Linux host, container, WSL2 (this spike). **Not a Windows-KiCad solution**: the Windows build cannot run under xpra; a Windows user would need the 1.5 GB WSL install and gets a second KiCad (Linux) with its own config/libraries. macOS: xpra client only. |
| (i) Licensing | xpra GPL-2.0-or-later (separate process, spawned; no linking), xpra-html5 MPL-2.0 (embeddable), Xvfb MIT, Mesa MIT, GTK LGPL, KiCad GPL-3.0 (unmodified). Playwright Apache-2.0 (measurement only). |
| (j) Effort | The pieces exist; the extension work is lifecycle + embedding: start `xpra` per user (`--bind=<unix socket>` + `--html=on`, or `--bind-tcp=127.0.0.1:<port>` with `--tcp-auth`/token), open the HTML5 client in a webview iframe (the sibling `prototypes/display/vscode-novnc` already takes a configurable `url`; VS Code `portMapping` covers remote/WSL), forward the sandboxed env, stop on dispose. ~1-2 weeks for a solid Linux/remote experience incl. DPI and keyboard-layout handling; +1 week if WSL bootstrap from Windows (apt installs, PPA, xpra.org repo) must be orchestrated by the extension. |

Rendering cost note: llvmpipe on this CPU keeps 57 fps, but on a 4-8 core laptop the same pan would drop to
~10-20 fps; the fix on Linux hosts is VirtualGL/EGL to a real GPU, or the Cairo GAL (cheaper, uglier).

## 5. x11vnc + noVNC (Linux VNC variant) — comparison only

- **`x11vnc -id <window>` on WSLg's `:0`:** x11vnc 0.9.16 refuses to start while `WAYLAND_DISPLAY` is set
  ("Wayland display server detected"); with `env -u WAYLAND_DISPLAY` it serves the window, but the **GL canvas is
  black** (XGetImage of an Xwayland window does not contain the DRI/d3d12-rendered GL content; only the GTK widgets
  are captured), **menus are not captured** (`-id` grabs one window; menus are separate override-redirect windows),
  the window is 3828x2127 so the UI is tiny in a 1600x950 client, pan streamed at 1 fps, wheel latency 92-853 ms
  (image `10-…`). Not viable.
- **Plain `Xvfb :101 1600x950` + pcbnew + x11vnc + noVNC** (`wsl/start_vnc_xvfb.sh`): GL canvas visible (llvmpipe),
  menus captured (whole framebuffer), pan **29 fps**, wheel→pixels **6-111 ms**, `Home`→pixels 58 ms,
  x11vnc 8 % + Xvfb 6 % CPU (image `11-…`). Works, but it is one framebuffer: no seamless windows, dialogs cannot
  leave the fixed canvas, no clipboard integration beyond noVNC's own text box, no session-side encoding
  choice. xpra is strictly better on Linux; this variant only matters if the xpra dependency is unwanted.

## 6. Reading for the VS Code plan (ARCHITECTURE §9, D12/D13/D17)

- **Linux/remote/container users:** xpra HTML5 in a webview gives the identical KiCad UI/UX and is the one Linux
  transport worth building on. D17 keeps renders secondary; this is the human-review window, not the agent loop.
- **Windows host users:** this route does not apply (Linux KiCad only). The Windows-side capture routes in
  `winstream.md` / `00-technology-survey.md` remain the Windows answer; a WSL-hosted KiCad is a second, separately
  configured KiCad and should not be the default for a Windows user.
- **Wayland:** the two X11 pins found in §3 (KiCad's `setenv` and wx's `gdk_set_allowed_backends`) are also why
  KiCad under a Wayland compositor is X11-only; any "run KiCad natively in a browser" idea (Broadway, wasm) is blocked
  at the toolkit level, not at KiCad's.
- **kiwrap sandbox on Linux:** the seed recipe transfers 1:1 (`docs/LINUX.md` §3 rows "KiCad config home semantics"
  and "GUI under Xvfb" can be marked verified for 10.0.6/Ubuntu 24.04: wizard-free start, `KICAD_*_HOME` honoured,
  Xvfb + GLX + llvmpipe renders the OpenGL GAL).

## 7. Clean-up state at the end of the spike

All WSL processes started here were stopped (`xpra stop :100`, `pkill -x pcbnew broadwayd x11vnc …`; `pgrep`
empty). `~/.config/kicad` absent for `usama` and `root`. `%APPDATA%\kicad` hash unchanged before/after:
`a90607a50b40498441be60c88dca677b6e142599f1957f6b81a1cd78856f6d9d`. Installed packages stay in WSL (KiCad PPA,
xpra.org repo, VirtualGL .deb) — ~1.6 GB; remove with `apt-get purge kicad xpra xpra-html5 virtualgl` if unwanted.
