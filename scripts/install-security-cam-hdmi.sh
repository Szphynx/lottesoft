#!/usr/bin/env bash
# One-shot Pi setup for the CSI-camera -> HDMI security cam viewer.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-security-cam-hdmi.sh
#
# Same pattern as a fullscreen video-loop player: launches when you log
# into the desktop, no systemd involved. Writes to both of the autostart
# mechanisms current Raspberry Pi OS desktops actually use -- the classic
# XDG ~/.config/autostart (older X11/LXDE sessions) AND labwc's own
# ~/.config/labwc/autostart shell script (current default Wayland
# compositor, which does NOT read ~/.config/autostart at all) -- since
# which one is active isn't detected here, and writing the one it doesn't
# use is harmless.
# Requires desktop auto-login to come up with nobody touching the keyboard:
#   sudo raspi-config -> System Options -> Boot / Auto Login -> Desktop Autologin
#
# To change flags (--palette, --mono, --rotate, ...), edit both
# ~/.config/autostart/security-cam-hdmi.desktop (the Exec= line) and
# ~/.config/labwc/autostart (the python3 line), then reboot.
#
# Note: testing security_cam_hdmi.py by hand over SSH won't show anything
# -- SSH has no route to the desktop's screen. Run it from a terminal
# opened on the Pi's own desktop instead.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

echo "== system packages =="
apt update
apt install -y python3-picamera2 python3-opencv python3-numpy python3-pygame --no-install-recommends

echo "== camera check =="
if command -v rpicam-hello >/dev/null; then
    rpicam-hello --list-cameras || echo "no camera detected -- check the ribbon cable orientation"
else
    echo "rpicam-hello not found (rpicam-apps) -- can't sanity-check the camera here, try it after reboot"
fi

echo "== desktop autostart (XDG, for X11/LXDE sessions) =="
AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/security-cam-hdmi.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Security cam HDMI display
Exec=/usr/bin/python3 $REPO_DIR/security_cam_hdmi.py --stats
X-GNOME-Autostart-enabled=true
EOF
chown "$TARGET_USER:$TARGET_USER" "$AUTOSTART_DIR/security-cam-hdmi.desktop"

echo "== desktop autostart (labwc, current default Wayland compositor) =="
LABWC_DIR="$TARGET_HOME/.config/labwc"
LABWC_AUTOSTART="$LABWC_DIR/autostart"
LAUNCH_LINE="/usr/bin/python3 $REPO_DIR/security_cam_hdmi.py --stats &"
mkdir -p "$LABWC_DIR"
[ -f "$LABWC_AUTOSTART" ] || printf '#!/bin/sh\n' > "$LABWC_AUTOSTART"
grep -qxF "$LAUNCH_LINE" "$LABWC_AUTOSTART" || echo "$LAUNCH_LINE" >> "$LABWC_AUTOSTART"
chmod +x "$LABWC_AUTOSTART"
chown -R "$TARGET_USER:$TARGET_USER" "$LABWC_DIR"

echo
echo "done. wrote:"
echo "  $AUTOSTART_DIR/security-cam-hdmi.desktop"
echo "  $LABWC_AUTOSTART"
echo "make sure desktop auto-login is on (sudo raspi-config -> Boot / Auto Login -> Desktop Autologin), then:"
echo "  sudo reboot"
echo "it'll launch fullscreen automatically after login."
