#!/usr/bin/env bash
# One-shot setup: mirrors fracture5's video onto its 3.5" SPI TFT (TRU
# Components 480x320, XPT2046 touch, ILI9486 controller) in addition to the
# existing HDMI/mpv output from video-fracture-install.sh -- run that
# first, this is purely additive and doesn't touch play-loop.sh, labwc, or
# anything on the HDMI side.
#
# Run once, from the repo root on fracture5:
#   sudo bash scripts/video-fracture-tft-install.sh [rotate]
#
# rotate defaults to 0. Confirmed on fracture5's actual hardware: no
# built-in "waveshare35a"-style overlay matched this board (Raspberry Pi's
# firmware doesn't ship one for it) -- the generic `fbtft` overlay with an
# explicit `ili9486` controller name is what worked, with dc_pin=24,
# reset_pin=25, led_pin=18 (the common pinout shared by most no-name
# 480x320+XPT2046 clones of this form factor).
#
# Orientation: edit the `rotate=` value in the dtoverlay line this script
# adds to /boot/firmware/config.txt (0/90/180/270), then `sudo reboot`.
# play-tft.sh reads the framebuffer's own reported width/height every loop,
# so it always matches whatever rotation is set there -- that's the one
# knob to turn, no script edits needed. If colors look swapped (red/blue),
# add `,bgr` to the end of the dtoverlay line.
#
# Playback speed: this panel has no vsync/double-buffering, so pushing
# frames faster than the SPI bus can transfer shows up as a torn/doubled
# image. Tune /etc/default/video-fracture-tft's TFT_FPS (default 12) to
# trade motion smoothness for fewer tears, then
# `sudo systemctl restart video-fracture-tft` to pick it up.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt
TFT_ROTATE="${1:-0}"
TFT_DTOVERLAY_PREFIX="dtoverlay=fbtft,spi0-0,ili9486"

echo "== packages =="
apt update
apt install -y fbset

echo "== boot config (SPI TFT: ili9486 via fbtft, rotate=$TFT_ROTATE) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=spi=on"
if grep -q "^${TFT_DTOVERLAY_PREFIX}" "$CONFIG"; then
    echo "a TFT dtoverlay line already exists in $CONFIG -- edit it directly to change rotate/params, not this script"
else
    add_line "${TFT_DTOVERLAY_PREFIX},width=480,height=320,rotate=${TFT_ROTATE},dc_pin=24,reset_pin=25,led_pin=18,speed=32000000,fps=30"
fi

echo "== playback speed config =="
FLAGS_FILE=/etc/default/video-fracture-tft
[ -f "$FLAGS_FILE" ] || echo 'TFT_FPS=12' > "$FLAGS_FILE"

echo "== systemd service (plays current.mp4 onto the TFT framebuffer) =="
cat > /etc/systemd/system/video-fracture-tft.service <<EOF
[Unit]
Description=Play fracture5 video onto the SPI TFT framebuffer
After=local-fs.target

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
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
echo "to reduce tearing: edit TFT_FPS in $FLAGS_FILE, then"
echo "sudo systemctl restart video-fracture-tft"
