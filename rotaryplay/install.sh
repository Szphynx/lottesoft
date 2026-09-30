#!/usr/bin/env bash
# One-shot Pi setup for Rotaryplay. Run from anywhere:
#   sudo bash rotaryplay/install.sh
# Uses ETH_IF / PI_IP from rotaryplay.conf. Safe to run again.
# Optional remote access: set TS_AUTHKEY in the environment (sudo -E keeps
# it) and Tailscale is installed and joined as "rotaryplay".
#
# Afterwards:
#   sudo systemctl restart fan-wall    # after editing rotaryplay.conf
#   journalctl -u fan-wall -f          # log (and stats, when on)
#   sudo systemctl stop fan-wall       # stop (runs the close sequence if on)

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/rotaryplay.conf"
chmod +x "$DIR/run.sh"

echo "== packages =="
dpkg --configure -a   # finish any install that was interrupted earlier, or apt refuses to run
apt update
apt install -y python3-opencv python3-numpy python3-pil \
    fonts-dejavu-core fonts-vlgothic fonts-noto-cjk

echo "== $ETH_IF -> Teensy: $PI_IP =="
# never-default matters: without it this link steals the default route and
# the Pi loses internet over Wi-Fi.
if nmcli -t -f NAME con show | grep -qx teensy-link; then
    nmcli con mod teensy-link ifname "$ETH_IF" ipv4.addresses "$PI_IP"
else
    nmcli con add type ethernet ifname "$ETH_IF" con-name teensy-link \
        ipv4.method manual ipv4.addresses "$PI_IP" \
        ipv4.never-default yes ipv6.method disabled
fi
nmcli --wait 10 con up teensy-link || echo "$ETH_IF not up yet -- plug the Teensy in, it'll connect"

if [ -n "${TS_AUTHKEY:-}" ]; then
    # Remote access. The key only ever comes from the environment -- never
    # put it in this file, rotaryplay.conf or anything else in the repo.
    echo "== tailscale =="
    command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
    tailscale up --authkey "$TS_AUTHKEY" --hostname "${TS_HOSTNAME:-rotaryplay}"
    unset TS_AUTHKEY
fi

echo "== systemd service fan-wall =="
cat > /etc/systemd/system/fan-wall.service <<EOF
[Unit]
Description=Rotaryplay fan wall player
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/run.sh
Restart=on-failure
RestartSec=2
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fan-wall
systemctl restart fan-wall
echo "control page: http://$(hostname -I | awk '{print $1}'):${WEB_PORT:-8099}/"
