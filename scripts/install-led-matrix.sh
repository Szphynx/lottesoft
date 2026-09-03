#!/usr/bin/env bash
# One-shot setup for the WS2812 LED-matrix Pi (media_matrix.py).
# Unrelated to install.sh / thermal_matrix.py, which set up a different Pi.
#
# Run once, from the repo root on the Pi:
#   sudo bash scripts/install-led-matrix.sh
#
# Then:
#   sudo reboot                     # the audio-disable config needs this
#   sudo python3 media_matrix.py --text "hello" --panel-width 32 --panel-height 8

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

echo
echo "done. reboot for the audio change to take effect: sudo reboot"
echo "then: sudo python3 $REPO_DIR/media_matrix.py --text \"hello\" --panel-width 32 --panel-height 8"
