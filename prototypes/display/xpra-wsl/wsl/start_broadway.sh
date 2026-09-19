#!/bin/bash
# EXPERIMENTAL - negative result, kept as evidence. GTK Broadway route: pcbnew drawn by broadwayd into a browser
# page (http://localhost:8085). It needs an LD_PRELOAD shim (noallow.c) because
#   (1) /usr/bin/pcbnew calls setenv("GDK_BACKEND", "x11") at startup, and
#   (2) wxWidgets' GL module (libwx_gtk3u_gl) calls gdk_set_allowed_backends("x11") when it loads.
# With the shim pcbnew connects to Broadway, every wxGLCanvas creation fails ("wxGLCanvas is only supported on
# Wayland and X11") and KiCad falls back to the Cairo GAL after two dialogs (press Enter twice). broadwayd
# 3.24.41 crashed twice within ~10 minutes (client_free assertion) taking pcbnew with it.
# See docs/research/display/wsl-linux.md section 3.
set -euo pipefail
source "$HOME/spike/env.sh"
HERE=$(cd "$(dirname "$0")" && pwd)
gcc -shared -fPIC -o /tmp/noallow.so "$HERE/noallow.c" -ldl
pkill -9 -x broadwayd || true
setsid -f broadwayd --address 0.0.0.0 --port 8085 :5 > /tmp/broadwayd.log 2>&1
sleep 1
KICAD_WAYLAND=1 LD_PRELOAD=/tmp/noallow.so GDK_BACKEND=broadway BROADWAY_DISPLAY=:5 \
  setsid -f pcbnew "$PCB" > /tmp/broadway-pcbnew.log 2>&1
echo "open http://localhost:8085/ and press Enter twice to dismiss the two OpenGL dialogs"
