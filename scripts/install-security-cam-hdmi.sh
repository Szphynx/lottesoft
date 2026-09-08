#!/usr/bin/env bash
# One-shot Pi setup for the CSI-camera -> HDMI security cam viewer.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-security-cam-hdmi.sh
#
# This keeps the Pi booting to its normal desktop (unlike the LED-matrix
# Pi's headless setup) -- the viewer needs a live desktop session (X11 or
# Wayland) to draw into, so it's installed as a systemd --user service
# instead of a system-wide one, tied to the desktop session starting
# rather than the machine booting. Requires desktop auto-login:
#   sudo raspi-config -> System Options -> Boot / Auto Login -> Desktop Autologin
#
# Then, logged in as the desktop user (not root):
#   systemctl --user enable --now security-cam-hdmi
#   systemctl --user status security-cam-hdmi   # is it running
#   journalctl --user -u security-cam-hdmi -f   # live logs / --stats output
#   systemctl --user stop security-cam-hdmi     # stop it
#
# Flags (--palette, --mono, --rotate, ...) live in
# ~/.config/security-cam-hdmi.env -- edit that file, then
# `systemctl --user restart security-cam-hdmi` to pick it up.
#
# Note: testing security_cam_hdmi.py by hand over SSH won't show anything
# -- SSH has no route to the desktop's screen. Run it from a terminal
# opened on the Pi's own desktop, or just use the service above.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_UID="$(id -u "$TARGET_USER")"
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

echo "== systemd --user service =="
# Lets the user's systemd instance run even without an active login, so
# the service is reachable right after this script runs, not just after
# the next desktop login.
loginctl enable-linger "$TARGET_USER"

FLAGS_FILE="$TARGET_HOME/.config/security-cam-hdmi.env"
[ -f "$FLAGS_FILE" ] || echo 'FLAGS="--stats"' > "$FLAGS_FILE"

mkdir -p "$TARGET_HOME/.config/systemd/user"
cat > "$TARGET_HOME/.config/systemd/user/security-cam-hdmi.service" <<EOF
[Unit]
Description=Security cam HDMI display
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/security_cam_hdmi.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
EOF

chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/systemd" "$FLAGS_FILE"

sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" systemctl --user daemon-reload
sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" systemctl --user enable security-cam-hdmi

echo
echo "done. it'll start automatically next time the desktop session starts."
echo "make sure desktop auto-login is on (sudo raspi-config -> Boot / Auto Login -> Desktop Autologin), then:"
echo "  sudo reboot"
echo "after that, as $TARGET_USER (not root):"
echo "  systemctl --user status security-cam-hdmi"
echo "  journalctl --user -u security-cam-hdmi -f"
