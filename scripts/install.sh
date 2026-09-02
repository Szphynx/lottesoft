#!/usr/bin/env bash
# One-shot Pi setup: deps, I2C0 config, Tailscale, systemd service.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install.sh [tailscale-authkey]
#
# Then, after the reboot it asks for:
#   sudo systemctl start thermal-matrix     # run it
#   sudo systemctl enable thermal-matrix    # run it on every boot
#   sudo systemctl status thermal-matrix    # is it running
#   journalctl -u thermal-matrix -f         # live logs / --stats output
#   sudo systemctl stop thermal-matrix      # stop it
#
# Flags (--colorwise, --rotate, calibration, ...) live in
# /etc/default/thermal-matrix -- edit that file, then
# `sudo systemctl restart thermal-matrix` to pick it up.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt
AUTHKEY="${1:-}"

echo "== system packages =="
apt update
apt install -y python3-opencv python3-pip python3-numpy python3-pil git i2c-tools

echo "== python packages =="
pip3 install --break-system-packages adafruit-circuitpython-mlx90640
pip3 install --break-system-packages "git+https://github.com/hzeller/rpi-rgb-led-matrix"

echo "== boot config (I2C0 for the camera, audio off for the panel PWM) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=audio=off"
add_line "dtoverlay=i2c0,pins_0_1"
add_line "dtparam=i2c0_baudrate=400000"
echo "blacklist snd_bcm2835" > /etc/modprobe.d/blacklist-rgb-matrix.conf

echo "== systemd service =="
FLAGS_FILE=/etc/default/thermal-matrix
[ -f "$FLAGS_FILE" ] || echo 'FLAGS="--bodyheat --stats"' > "$FLAGS_FILE"

cat > /etc/systemd/system/thermal-matrix.service <<EOF
[Unit]
Description=Thermal camera LED matrix
After=network.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/thermal_matrix.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "== tailscale (remote access) =="
if [ -n "$AUTHKEY" ]; then
    bash "$REPO_DIR/scripts/setup-tailscale.sh" "$AUTHKEY"
else
    echo "no authkey given, skipping -- run scripts/setup-tailscale.sh <key> later"
fi

echo
echo "done. i2c0 needs a reboot to take effect: sudo reboot"
echo "then: sudo systemctl enable --now thermal-matrix"
