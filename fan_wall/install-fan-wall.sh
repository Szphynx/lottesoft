#!/usr/bin/env bash
# One-shot Pi setup: packages, static address on the Teensy link, systemd service.
#
# Run once, from the repo root:
#   sudo bash fan_wall/install-fan-wall.sh
#
# Then:
#   sudo systemctl status fan-wall     # is it running
#   journalctl -u fan-wall -f          # logs / --stats output
#
# Flags (--text, --media, --web-port, ...) live in /etc/default/fan-wall --
# edit, then `sudo systemctl restart fan-wall`. Control page: http://<pi>:8099/

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== system packages =="
apt update
apt install -y python3-opencv python3-numpy python3-pil fonts-dejavu-core fonts-vlgothic

echo "== eth0 -> Teensy (192.168.60.0/24, same subnet as the sketch) =="
# Pi gets .20, Teensy is .50 (pi_streamer_spec.md). never-default matters:
# without it this link steals the default route and the Pi loses internet
# over Wi-Fi.
if ! nmcli -t -f NAME con show | grep -qx teensy-link; then
    nmcli con add type ethernet ifname eth0 con-name teensy-link \
        ipv4.method manual ipv4.addresses 192.168.60.20/24 \
        ipv4.never-default yes ipv6.method disabled
fi
nmcli con up teensy-link || echo "eth0 not up yet -- plug the Teensy in, it'll connect"

echo "== systemd service =="
FLAGS_FILE=/etc/default/fan-wall
[ -f "$FLAGS_FILE" ] || echo 'FLAGS="--web-port 8099 --stats"' > "$FLAGS_FILE"

cat > /etc/systemd/system/fan-wall.service <<EOF
[Unit]
Description=Fan wall video/text player
After=network.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/fan_wall.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now fan-wall
echo "control page: http://$(hostname -I | awk '{print $1}'):8099/"
