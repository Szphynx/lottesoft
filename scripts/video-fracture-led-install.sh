#!/usr/bin/env bash
# Add a HUB75 LED matrix ("looper led") to a Pi that's already running the
# HDMI video-fracture looper (scripts/video-fracture-install.sh). Purely
# additive: a new video-fracture-led.service shows the SAME video
# (/var/lib/video-fracture/current.mp4 -- fetched by the existing
# video-fracture-fetch.timer, unchanged) on a Joy-IT RB-MatrixCtrl + HUB75
# panel, via its own live web control page. Nothing here touches the
# existing video-fracture fetch/play-loop files or services.
#
# Uses the same controller/library as thermal_matrix.py (rgbmatrix +
# Joy-IT RB-MatrixCtrl), so this Pi ends up with the identical driver
# stack the thermal-camera and media-matrix projects already use --
# nothing reinvented.
#
# IMPORTANT: driving a HUB75 panel requires the Pi's PWM-conflicting audio
# disabled (dtparam=audio=off + blacklisting snd_bcm2835) -- same
# requirement thermal_matrix.py has. If the existing HDMI video loop plays
# sound through this Pi, THIS WILL MUTE IT after the reboot below. There's
# no way around that with this hardware combo -- if you need audio on the
# HDMI side, run the LED matrix from a different Pi instead.
#
# Requires: this Pi has already run scripts/video-fracture-install.sh
# (video-fracture-fetch.timer needs to already be fetching a video).
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/video-fracture-led-install.sh
#
# Then:
#   1. sudo reboot                                    # audio-disable needs a reboot
#   2. sudo systemctl enable --now video-fracture-led
#   3. control page: http://<tailscale-ip>:8099/
#
# Useful commands:
#   sudo systemctl status video-fracture-led
#   journalctl -u video-fracture-led -f

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt

echo "== git safe.directory =="
git config --global --add safe.directory "$REPO_DIR"

if ! systemctl list-unit-files 2>/dev/null | grep -q '^video-fracture-fetch.timer'; then
    echo "warning: video-fracture-fetch.timer not found on this Pi." >&2
    echo "         run scripts/video-fracture-install.sh first so there's a video to show." >&2
fi

echo "== system packages =="
apt update
apt install -y python3-opencv python3-pip python3-numpy python3-pil

echo "== python packages (rgbmatrix, same as thermal_matrix.py) =="
pip3 install --break-system-packages "git+https://github.com/hzeller/rpi-rgb-led-matrix"

echo "== boot config (audio off for the panel PWM -- see warning above) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=audio=off"
echo "blacklist snd_bcm2835" > /etc/modprobe.d/blacklist-rgb-matrix.conf

install -d -m 755 /var/lib/video-fracture

echo "== config =="
FLAGS_FILE=/etc/default/video-fracture-led
if [ ! -f "$FLAGS_FILE" ]; then
    cat > "$FLAGS_FILE" <<'EOF'
# Panel calibration -- every HUB75 panel/chipset needs its own values here.
# A blue/wrong-color tint means --led-rgb-sequence is wrong (try RBG, GRB,
# BGR, ...). A scrambled/checkerboard image means --multiplexing is wrong
# (try 1 through 17). After changing this file:
#   sudo systemctl restart video-fracture-led
FLAGS="--led-rgb-sequence RGB --multiplexing 0 --row-address-type 0"
EOF
fi

echo "== systemd service =="
cat > /etc/systemd/system/video-fracture-led.service <<EOF
[Unit]
Description=video-fracture looper on HUB75 LED matrix
After=network.target video-fracture-fetch.service

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/scripts/video-fracture-led/player.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo
echo "done. now:"
echo "  1. sudo reboot                                    # audio-disable needs this"
echo "  2. sudo systemctl enable --now video-fracture-led"
echo "  3. control page: http://\$(tailscale ip -4):8099/"
echo
echo "if the image looks wrong (color tint, scrambled/checkerboard), edit"
echo "$FLAGS_FILE and restart the service -- see the comments in that file."
