#!/usr/bin/env bash
# One-shot Pi setup for the CSI-camera -> HDMI security cam viewer.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-security-cam-hdmi.sh
#
# Same pattern as a fullscreen video-loop player: a desktop autostart
# entry launches it when you log into the desktop, no systemd involved.
# Requires desktop auto-login to come up with nobody touching the keyboard:
#   sudo raspi-config -> System Options -> Boot / Auto Login -> Desktop Autologin
#
# To change flags (--palette, --mono, --rotate, ...), edit the Exec= line
# in ~/.config/autostart/security-cam-hdmi.desktop, then log out and back
# in (or reboot) to pick it up.
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

echo "== desktop autostart =="
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

echo
echo "done. wrote $AUTOSTART_DIR/security-cam-hdmi.desktop"
echo "make sure desktop auto-login is on (sudo raspi-config -> Boot / Auto Login -> Desktop Autologin), then:"
echo "  sudo reboot"
echo "it'll launch fullscreen automatically after login."
