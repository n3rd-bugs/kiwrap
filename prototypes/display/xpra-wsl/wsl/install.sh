#!/bin/bash
# Run as root inside WSL2 Ubuntu 24.04:
#   wsl -d Ubuntu -u root -- bash /mnt/c/Upwork/KiCad/prototypes/display/xpra-wsl/wsl/install.sh
# Installs KiCad 10.0.6 (PPA), xpra 6.x + html5 client (xpra.org repo; Ubuntu's own xpra is 3.1.5 and ships no
# html5 package), GTK Broadway (broadwayd, in libgtk-3-bin), x11vnc + noVNC, Xvfb and X tooling. ~1.5 GB.
# No 3D model packages. Verified 2026-09-19.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
add-apt-repository -y ppa:kicad/kicad-10.0-releases
wget -qO /usr/share/keyrings/xpra.asc https://xpra.org/xpra.asc
wget -qO /etc/apt/sources.list.d/xpra.sources https://raw.githubusercontent.com/Xpra-org/xpra/master/packaging/repos/noble/xpra.sources
apt-get update -q
apt-get install -y -q --no-install-recommends \
  kicad kicad-symbols kicad-footprints kicad-templates kicad-demos \
  mesa-utils libgtk-3-bin gtk-3-examples xvfb xdotool x11-utils wmctrl x11vnc novnc websockify xclip gcc
apt-get install -y -q xpra xpra-html5
# The xpra package installs a systemd socket unit on port 14500 (proxy). This prototype binds 14600 instead.
xpra --version
kicad-cli version
dpkg -s xpra-html5 | grep ^Version
