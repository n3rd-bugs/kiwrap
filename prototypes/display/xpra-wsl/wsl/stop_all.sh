#!/bin/bash
# Stop everything the spike scripts can have started. Uses `pkill -x` on purpose: `pkill -f pcbnew` would also
# match the xpra server's own command line (it contains --start=pcbnew ...), which is how the spike once killed
# its own xpra server.
xpra stop :100 > /dev/null 2>&1
sleep 2
pkill -9 -x pcbnew; pkill -9 -x eeschema; pkill -9 -x kicad
pkill -9 -x broadwayd; pkill -9 -x x11vnc; pkill -9 -f websockify
pkill -9 -f "Xvfb :101"; pkill -9 -f Xvfb-for-Xpra; pkill -9 -f gtk3-widget-factory; pkill -9 -x ibus-daemon
sleep 1
pgrep -a -f "pcbnew|broadwayd|xpra|x11vnc|websockify|Xvfb" || echo "nothing left"
if [ -d "$HOME/.config/kicad" ]; then echo "WARNING: $HOME/.config/kicad exists"; else echo "$HOME/.config/kicad absent (good)"; fi
