#!/usr/bin/env bash
# Loops fracture5's current video onto the SPI TFT framebuffer, independent
# of play-loop.sh (which drives the HDMI output via mpv/labwc). Started by
# video-fracture-tft.service, a plain systemd service -- no desktop session
# needed, works even before anyone logs in.
#
# Orientation is controlled by the dtoverlay's own `rotate=` param in
# config.txt (0/90/180/270), not here -- the kernel driver reports the
# already-rotated width/height, and this script just reads that and scales
# into it. Change orientation there, reboot, done.

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
FB_DEVICE="${TFT_FB_DEVICE:-/dev/fb1}"

# Wait for both a video (fetch-video.sh) and the TFT driver to be ready.
while [ ! -s "$CURRENT" ] || [ ! -e "$FB_DEVICE" ]; do
    sleep 2
done

while :; do
    read -r FB_W FB_H < <(fbset -fb "$FB_DEVICE" -s | awk '/geometry/{print $2, $3; exit}')
    FB_W="${FB_W:-480}"
    FB_H="${FB_H:-320}"

    ffmpeg -hide_banner -loglevel error \
        -stream_loop -1 -re -i "$CURRENT" \
        -vf "scale=${FB_W}:${FB_H}:force_original_aspect_ratio=decrease,pad=${FB_W}:${FB_H}:(ow-iw)/2:(oh-ih)/2,format=rgb565le" \
        -f fbdev "$FB_DEVICE"

    # ffmpeg exits if current.mp4 is swapped mid-loop (fetch-video.sh writes a
    # new file) or on any error -- loop re-reads the fb geometry and restarts.
    sleep 2
done
