#!/usr/bin/env bash
# One-shot setup: mirrors fracture5's video onto a 3.5" SPI TFT (XPT2046
# touch, 480x320) in addition to the existing HDMI/mpv output from
# video-fracture-install.sh -- run that first, this is purely additive and
# doesn't touch play-loop.sh, labwc, or anything on the HDMI side.
#
# Run once, from the repo root on fracture5:
#   sudo bash scripts/video-fracture-tft-install.sh
#
# Orientation: edit the `rotate=` value in the dtoverlay line this script
# adds to /boot/firmware/config.txt (0/90/180/270), then `sudo reboot`.
# play-tft.sh reads the framebuffer's own reported width/height every loop,
# so it always matches whatever rotation is set there -- that's the one
# knob to turn, no script edits needed.
#
# This assumes the common "waveshare35a" clone (ILI9486 + XPT2046, the
# overlay Raspberry Pi OS ships built-in). If colors or geometry look wrong
# after reboot, it may be a different clone needing its own vendor install
# script instead -- tell me what you see (photo helps) and I'll adjust.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt
TFT_OVERLAY="${1:-waveshare35a}"
TFT_ROTATE="${2:-0}"

echo "== packages =="
apt update
apt install -y fbset

echo "== boot config (SPI TFT overlay: $TFT_OVERLAY, rotate=$TFT_ROTATE) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=spi=on"
if grep -q '^dtoverlay=waveshare35a\|^dtoverlay=mhs35' "$CONFIG"; then
    echo "a TFT dtoverlay line already exists in $CONFIG -- edit it directly to change rotate/overlay, not this script"
else
    add_line "dtoverlay=${TFT_OVERLAY}:rotate=${TFT_ROTATE}"
fi

echo "== systemd service (mirrors current.mp4 onto the TFT framebuffer) =="
cat > /etc/systemd/system/video-fracture-tft.service <<EOF
[Unit]
Description=Play fracture5 video onto the SPI TFT framebuffer
After=local-fs.target

[Service]
Type=simple
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/video-fracture/play-tft.sh
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable video-fracture-tft.service

echo
echo "done. reboot for the dtoverlay to take effect: sudo reboot"
echo "then check:"
echo "  ls /dev/fb1                     # should exist"
echo "  fbset -fb /dev/fb1 -s           # confirm geometry/orientation"
echo "  sudo systemctl start video-fracture-tft   # start it now, no reboot needed after that"
echo "  journalctl -u video-fracture-tft -f        # playback logs"
echo
echo "to change orientation later: edit the rotate=N value in $CONFIG"
echo "(0/90/180/270), then sudo reboot -- play-tft.sh adapts automatically."
