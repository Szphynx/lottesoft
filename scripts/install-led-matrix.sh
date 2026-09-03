#!/usr/bin/env bash
# One-shot setup for the WS2812 LED-matrix Pi (media_matrix.py).
# Unrelated to install.sh / thermal_matrix.py, which set up a different Pi.
#
# Run once, from the repo root on the Pi:
#   sudo bash scripts/install-led-matrix.sh
#
# Sets up media-matrix as a systemd service (so it survives reboots and can
# be cleanly restarted) plus a timer that checks GitHub every 2 minutes and
# pulls + restarts automatically when this branch gets new commits pushed --
# no manual `git pull` on the Pi needed after the first setup.
#
# Then:
#   sudo reboot                                # the audio-disable config needs this
#   sudo systemctl enable --now media-matrix media-matrix-autoupdate.timer
#   sudo systemctl status media-matrix         # is it running
#   journalctl -u media-matrix -f              # live logs
#   journalctl -u media-matrix-autoupdate -f   # autoupdate check log
#
# Flags live in /etc/default/media-matrix -- edit that file, then
# `sudo systemctl restart media-matrix` to pick it up. Default just opens
# the web control panel with nothing loaded yet; add text/media from there.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt

echo "== system packages =="
apt update
apt install -y python3-pip python3-numpy python3-pil python3-opencv git fonts-dejavu-core

echo "== python packages =="
pip3 install --break-system-packages rpi_ws281x

echo "== boot config (disable onboard audio -- it uses the same PWM hardware as GPIO18) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=audio=off"
echo "blacklist snd_bcm2835" > /etc/modprobe.d/blacklist-ws2812.conf

echo "== systemd service =="
FLAGS_FILE=/etc/default/media-matrix
[ -f "$FLAGS_FILE" ] || echo 'FLAGS="--web-port 8098"' > "$FLAGS_FILE"

cat > /etc/systemd/system/media-matrix.service <<EOF
[Unit]
Description=WS2812 LED matrix media/text display
After=network.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/media_matrix.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

echo "== auto-update timer (polls GitHub, pulls + restarts on new commits) =="
cat > /etc/systemd/system/media-matrix-autoupdate.service <<EOF
[Unit]
Description=Pull latest media-matrix code and restart if changed
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash $REPO_DIR/scripts/auto-update.sh
EOF

cat > /etc/systemd/system/media-matrix-autoupdate.timer <<'EOF'
[Unit]
Description=Check for media-matrix updates periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload

echo
echo "done. i2c/audio changes need a reboot to take effect: sudo reboot"
echo "then: sudo systemctl enable --now media-matrix media-matrix-autoupdate.timer"
echo "control panel: http://$(hostname -I | awk '{print $1}'):8098/"
echo "flags: sudo nano $FLAGS_FILE  (then: sudo systemctl restart media-matrix)"
