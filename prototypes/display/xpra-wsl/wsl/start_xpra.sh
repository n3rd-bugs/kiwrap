#!/bin/bash
# Start an xpra seamless session on :100 with the HTML5 client on http://localhost:14600 and pcbnew inside it.
# Verified 2026-09-19 with xpra 6.5.3 / xpra-html5 21 / KiCad 10.0.6 on WSL2 Ubuntu 24.04 (WSLg present).
# GL inside the session is Xvfb GLX + Mesa llvmpipe (software): the OpenGL GAL canvas renders correctly, at CPU
# cost (pcbnew 400-700 % of a core while panning on a 24-core machine). Requires `bash seed_config.sh` first.
#   PORT=14600 DPI=96 bash start_xpra.sh
set -euo pipefail
source "$HOME/spike/env.sh"
PORT=${PORT:-14600}   # 14500 is owned by the xpra package's systemd socket (proxy) - leave it alone
DPI=${DPI:-96}
xpra start :100 --html=on --bind-tcp=0.0.0.0:$PORT --daemon=yes --dpi=$DPI \
  --env=KICAD_CONFIG_HOME=$KICAD_CONFIG_HOME \
  --env=KICAD_DOCUMENTS_HOME=$KICAD_DOCUMENTS_HOME \
  --env=KICAD_CACHE_HOME=$KICAD_CACHE_HOME \
  --start="pcbnew $PCB" \
  --notifications=no --pulseaudio=no --speaker=no --microphone=no --webcam=no --printing=no --file-transfer=no \
  --clipboard=yes --systemd-run=no --mdns=no --exit-with-children=no
sleep 8
pgrep -x pcbnew > /dev/null && echo "pcbnew running" || echo "pcbnew NOT running - check the log"
echo "open: http://localhost:$PORT/?keyboard_layout=us&sound=false&clipboard=true&floating_menu=false"
echo "log:  /run/xpra/100/server.log (root) or ~/.xpra/:100.log"
