#!/usr/bin/env bash
# One-shot Pi setup: deps, SPI config, Tailscale, systemd service.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-double-matrix.sh [tailscale-authkey]
#
# Then, after the reboot it asks for:
#   sudo systemctl enable --now double-matrix
#   sudo systemctl status double-matrix    # is it running
#   journalctl -u double-matrix -f         # live logs / --stats output
#   sudo systemctl stop double-matrix      # stop it
#
# Flags (--layout, --media, --text, --web-port, ...) live in
# /etc/default/double-matrix -- edit that file, then
# `sudo systemctl restart double-matrix` to pick it up. Control panel is at
# http://<tailnet-ip>:8099/ once the service is running.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt
AUTHKEY="${1:-}"

echo "== system packages =="
apt update
apt install -y python3-opencv python3-pip python3-numpy python3-pil git \
    fonts-dejavu-core fonts-vlgothic

echo "== python packages =="
pip3 install --break-system-packages luma.led_matrix luma.core spidev

echo "== boot config (SPI0 for the MAX7219 chain) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=spi=on"

echo "== systemd service =="
FLAGS_FILE=/etc/default/double-matrix
[ -f "$FLAGS_FILE" ] || echo 'FLAGS="--layout grid --web-port 8099 --stats"' > "$FLAGS_FILE"

cat > /etc/systemd/system/double-matrix.service <<EOF
[Unit]
Description=Double matrix LED video/text player
After=network.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/double_matrix.py \$FLAGS'
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
echo "done. SPI needs a reboot to take effect: sudo reboot"
echo "then: sudo systemctl enable --now double-matrix"
echo "control panel: http://\$(tailscale ip -4):8099/  (add it to scripts/dashboard.html)"
