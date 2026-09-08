#!/usr/bin/env bash
# One-shot Pi setup for the CSI-camera -> HDMI security cam viewer.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-security-cam-hdmi.sh
#
# Then:
#   sudo systemctl enable --now security-cam-hdmi
#   sudo systemctl status security-cam-hdmi   # is it running
#   journalctl -u security-cam-hdmi -f        # live logs / --stats output
#   sudo systemctl stop security-cam-hdmi     # stop it
#
# It renders straight to the HDMI output via KMS/DRM (no desktop, X11, or
# autologin needed) so it comes up on boot with nobody logged in -- same
# pattern as the LED-matrix branch's thermal-matrix service.
#
# Flags (--palette, --mono, --rotate, ...) live in
# /etc/default/security-cam-hdmi -- edit that file, then
# `sudo systemctl restart security-cam-hdmi` to pick it up.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== system packages =="
apt update
apt install -y python3-picamera2 python3-opencv python3-numpy python3-pygame --no-install-recommends

echo "== camera check =="
if command -v rpicam-hello >/dev/null; then
    rpicam-hello --list-cameras || echo "no camera detected -- check the ribbon cable orientation"
else
    echo "rpicam-hello not found (rpicam-apps) -- can't sanity-check the camera here, try it after reboot"
fi

echo "== systemd service =="
FLAGS_FILE=/etc/default/security-cam-hdmi
[ -f "$FLAGS_FILE" ] || echo 'FLAGS="--stats"' > "$FLAGS_FILE"

cat > /etc/systemd/system/security-cam-hdmi.service <<EOF
[Unit]
Description=Security cam HDMI display
After=multi-user.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/security_cam_hdmi.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo
echo "done. enable it with:"
echo "  sudo systemctl enable --now security-cam-hdmi"
echo "or try it by hand first:"
echo "  python3 $REPO_DIR/security_cam_hdmi.py --stats"
