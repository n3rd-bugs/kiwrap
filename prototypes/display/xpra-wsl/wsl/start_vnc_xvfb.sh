#!/bin/bash
# Comparison variant: plain Xvfb :101 (1600x950, GLX via llvmpipe) + pcbnew + x11vnc + noVNC on
# http://localhost:6081/vnc.html . x11vnc 0.9.16 must NOT see WAYLAND_DISPLAY (it exits with "Wayland display
# server detected"), hence `env -u`. Do not point x11vnc at WSLg's :0 - the GL canvas comes out black there
# (docs/research/display/wsl-linux.md section 5).
set -euo pipefail
source "$HOME/spike/env.sh"
setsid -f Xvfb :101 -screen 0 1600x950x24 +extension GLX +extension RANDR -nolisten tcp > /tmp/xvfb101.log 2>&1
sleep 2
DISPLAY=:101 setsid -f pcbnew "$PCB" > /tmp/vnc-pcbnew.log 2>&1
sleep 10
WID=$(DISPLAY=:101 xdotool search --name "PCB Editor" | head -1)
DISPLAY=:101 xdotool windowsize "$WID" 1600 950
DISPLAY=:101 xdotool windowmove "$WID" 0 0
env -u WAYLAND_DISPLAY setsid -f x11vnc -display :101 -rfbport 5901 -nopw -forever -shared -o /tmp/x11vnc.log > /dev/null 2>&1
sleep 2
setsid -f websockify --web=/usr/share/novnc 6081 localhost:5901 > /tmp/websockify.log 2>&1
echo "open http://localhost:6081/vnc.html?autoconnect=true&resize=off"
