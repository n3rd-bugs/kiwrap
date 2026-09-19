# xpra-wsl — KiCad (Linux build) streamed into a browser / VS Code webview via xpra HTML5

Prototype for the Linux display route (WSL2 here; the same scripts apply to a Linux host or a container).
Findings and measurements: [`docs/research/display/wsl-linux.md`](../../../docs/research/display/wsl-linux.md).

What it does: runs the distro KiCad 10.0.6 inside an xpra seamless session (Xvfb + llvmpipe GL) and serves the
xpra HTML5 client on `http://localhost:14600`. The full pcbnew UI (OpenGL GAL canvas, menus, context menus,
tooltips, modal dialogs, footprint chooser with its GL preview) appears in the browser, with two-way clipboard,
seamless per-window rendering and session persistence across reconnects. Also included, as evidence only:
the GTK Broadway variant (negative result) and an Xvfb + x11vnc + noVNC comparison.

Nothing here touches `~/.config/kicad` (WSL) or `%APPDATA%\kicad` (Windows): every KiCad process gets a sandboxed
`KICAD_CONFIG_HOME` seeded with the kiwrap recipe (`wsl/seed_config.sh`).

## Run steps (Windows host, WSL2 Ubuntu 24.04 with WSLg)

```powershell
# 1. one-time install inside WSL (as root; ~1.5 GB: KiCad PPA, xpra.org repo, x11vnc/noVNC, Xvfb, gcc)
wsl -d Ubuntu -u root -- bash /mnt/c/Upwork/KiCad/prototypes/display/xpra-wsl/wsl/install.sh

# 2. seed the sandboxed config root + scratch ecc83 demo (as the same user that will run xpra)
wsl -d Ubuntu -u root -- bash /mnt/c/Upwork/KiCad/prototypes/display/xpra-wsl/wsl/seed_config.sh

# 3. start xpra :100 + pcbnew; HTML5 client on http://localhost:14600 (WSL localhost forwarding works)
wsl -d Ubuntu -u root -- bash /mnt/c/Upwork/KiCad/prototypes/display/xpra-wsl/wsl/start_xpra.sh

# 4. open in a browser (or set the sibling prototype's `url` setting, prototypes/display/vscode-novnc, to this URL)
start "http://localhost:14600/?keyboard_layout=us&sound=false&clipboard=true&floating_menu=false"

# 5. stop everything
wsl -d Ubuntu -u root -- bash /mnt/c/Upwork/KiCad/prototypes/display/xpra-wsl/wsl/stop_all.sh
```

Notes:
- `wsl -d Ubuntu -u root -- bash -c "..."` sessions kill their background children when they return; the scripts
  use `setsid -f` / xpra `--daemon=yes` so the servers survive.
- Port 14500 is taken by the xpra package's systemd socket (`xpra.socket`, a proxy that needs auth); the
  prototype binds 14600. Port 8085 = broadwayd, 6081 = noVNC.
- Running as root is fine for a spike (xpra warns). For a product, run xpra as the user.
- HiDPI: the HTML5 client draws CSS pixels, so a DPR-2 browser just upscales the 96-dpi render. Pass
  `DPI=192` to `start_xpra.sh` and `--env=GDK_SCALE=2` if you want a crisp 2x session (not measured).
- To use the GPU on a real Linux host: `vglrun -d /dev/dri/renderD128 pcbnew` (VirtualGL EGL backend) inside
  the xpra session. This does not work under WSL2 (no DRM render node; VirtualGL's GLX interposer to WSLg's
  `:0` fails with a `drisw` error), see the research note.

## Measuring with Playwright (Windows side, Chromium)

```powershell
cd C:\Upwork\KiCad\prototypes\display\xpra-wsl\pw
npm install; npx playwright install chromium
node drive.js "http://localhost:14600/?keyboard_layout=us&sound=false&clipboard=true&floating_menu=false" out\xpra steps-xpra-interact.json
node drive.js "http://localhost:14600/?..." out\xpra steps-xpra-pan.json      # 8 s middle-drag pan: fps + CPU
node clip.js  "http://localhost:14600/?..." out\xpra                            # clipboard both ways, reconnect, DPR 2
```

`drive.js` executes a JSON step list (click / drag / wheel / key / type / shot) and measures:
`latency` = time from a synthetic wheel event to the first CDP screencast frame that changed;
`keylatency` = same for a key press; `fpsdrag` = screencast frames per second during a continuous drag.
`sheet.py` (Pillow) builds contact sheets from the PNGs.

## Variants

- `wsl/start_broadway.sh` — GTK Broadway (negative result): needs `noallow.c` as an `LD_PRELOAD` shim to defeat
  KiCad's `setenv(GDK_BACKEND=x11)` and wx's `gdk_set_allowed_backends("x11")`; no GL at all (Cairo GAL after two
  nag dialogs, and every wxGLCanvas creation nags again); broadwayd crashed twice in ten minutes.
- `wsl/start_vnc_xvfb.sh` — Xvfb :101 + x11vnc + noVNC on `http://localhost:6081/vnc.html` (works, single
  framebuffer, no seamless windows, no clipboard integration beyond noVNC's own).

## Licensing of the pieces

xpra: GPL-2.0-or-later (server + Python client); xpra-html5: MPL-2.0; GTK/broadwayd: LGPL-2.1+; Xvfb/x11vnc:
MIT/X11 (x11vnc GPL-2.0); noVNC: MPL-2.0; websockify: LGPL-3.0; VirtualGL: wxWindows Library Licence / LGPL-2.1;
Mesa: MIT; KiCad: GPL-3.0-or-later (unmodified, run as a separate process). Playwright (measurement only): Apache-2.0.
