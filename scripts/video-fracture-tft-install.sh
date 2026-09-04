#!/usr/bin/env bash
# One-shot setup: mirrors fracture5's video onto its 3.5" SPI TFT (TRU
# Components 480x320, XPT2046 touch, ILI9486 controller) in addition to the
# existing HDMI/mpv output from video-fracture-install.sh -- run that
# first, this is purely additive and doesn't touch play-loop.sh, labwc, or
# anything on the HDMI side.
#
# Run once, from the repo root on fracture5:
#   sudo bash scripts/video-fracture-tft-install.sh
#
# Confirmed on fracture5's actual hardware: no built-in "waveshare35a"-style
# overlay matched this board (Raspberry Pi's firmware doesn't ship one for
# it) -- the generic `fbtft` overlay with an explicit `ili9486` controller
# name is what worked, with dc_pin=24, reset_pin=25, led_pin=18 (the common
# pinout shared by most no-name 480x320+XPT2046 clones of this form
# factor). If colors look swapped (red/blue), add `,bgr` to the end of the
# dtoverlay line this script adds to config.txt.
#
# Orientation and playback speed are NOT boot settings -- both are handled
# in play-tft.sh (software transpose / frame-rate throttle) and controlled
# via /etc/default/video-fracture-tft, so changing them is just an env-file
# edit + `sudo systemctl restart video-fracture-tft`, no reboot:
#   TFT_ROTATE_DEG   0/90/180/270 (default 0)
#   TFT_FPS          frames written to the panel per second (default 12) --
#                     this panel has no vsync/double-buffering, so pushing
#                     frames faster than the SPI bus can transfer shows up
#                     as a torn/doubled image; lower trades motion
#                     smoothness for fewer visible tears.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt
TFT_DTOVERLAY="dtoverlay=fbtft,spi0-0,ili9486,width=480,height=320,rotate=0,dc_pin=24,reset_pin=25,led_pin=18,speed=32000000,fps=30"

echo "== packages =="
apt update
apt install -y fbset

echo "== boot config (SPI TFT: ili9486 via fbtft; orientation is handled in software, not here) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=spi=on"
add_line "$TFT_DTOVERLAY"

echo "== playback settings =="
FLAGS_FILE=/etc/default/video-fracture-tft
if [ ! -f "$FLAGS_FILE" ]; then
    cat > "$FLAGS_FILE" <<'EOF'
TFT_FPS=12
TFT_ROTATE_DEG=0
EOF
fi

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
echo "done. reboot once for the dtoverlay to take effect: sudo reboot"
echo "then check:"
echo "  ls /dev/fb1                     # should exist"
echo "  sudo systemctl start video-fracture-tft   # start it now, no reboot needed after that"
echo "  journalctl -u video-fracture-tft -f        # playback logs"
echo
echo "to change orientation or tearing: edit $FLAGS_FILE"
echo "(TFT_ROTATE_DEG, TFT_FPS), then sudo systemctl restart video-fracture-tft"
echo "-- no reboot needed for either."
