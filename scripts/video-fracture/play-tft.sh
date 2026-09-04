#!/usr/bin/env bash
# Loops fracture5's current video onto the SPI TFT framebuffer, independent
# of play-loop.sh (which drives the HDMI output via mpv/labwc). Started by
# video-fracture-tft.service, a plain systemd service -- no desktop session
# needed, works even before anyone logs in.
#
# Orientation is handled entirely here, in software, via ffmpeg's transpose
# filter -- the boot overlay's own `rotate=` stays fixed at 0. Set
# TFT_ROTATE_DEG (0/90/180/270) in /etc/default/video-fracture-tft, then
# `sudo systemctl restart video-fracture-tft` -- no reboot needed.
#
# TFT_FPS throttles how often a frame is written to the panel. This panel
# has no vsync/double-buffering, so writing faster than the SPI bus can
# push a full frame shows up as a torn/doubled image -- lower TFT_FPS
# trades motion smoothness for fewer visible tears. Also set via
# /etc/default/video-fracture-tft.

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
FB_DEVICE="${TFT_FB_DEVICE:-/dev/fb1}"
TFT_FPS="${TFT_FPS:-12}"
TFT_ROTATE_DEG="${TFT_ROTATE_DEG:-0}"

# Wait for both a video (fetch-video.sh) and the TFT driver to be ready.
while [ ! -s "$CURRENT" ] || [ ! -e "$FB_DEVICE" ]; do
    sleep 2
done

while :; do
    read -r FB_W FB_H < <(fbset -fb "$FB_DEVICE" -s | awk '/geometry/{print $2, $3; exit}')
    FB_W="${FB_W:-480}"
    FB_H="${FB_H:-320}"

    # 90/270 swap the frame's dimensions before the transpose puts it back
    # to the panel's native FB_W x FB_H; 180 just flips in place.
    case "$TFT_ROTATE_DEG" in
        90)  ROT_FILTER="transpose=1"; SCALE_W="$FB_H"; SCALE_H="$FB_W" ;;
        270) ROT_FILTER="transpose=2"; SCALE_W="$FB_H"; SCALE_H="$FB_W" ;;
        180) ROT_FILTER="hflip,vflip"; SCALE_W="$FB_W"; SCALE_H="$FB_H" ;;
        *)   ROT_FILTER="";            SCALE_W="$FB_W"; SCALE_H="$FB_H" ;;
    esac

    VF="fps=${TFT_FPS},scale=${SCALE_W}:${SCALE_H}:force_original_aspect_ratio=decrease,pad=${SCALE_W}:${SCALE_H}:(ow-iw)/2:(oh-ih)/2"
    [ -n "$ROT_FILTER" ] && VF="${VF},${ROT_FILTER}"
    VF="${VF},format=rgb565le"

    ffmpeg -hide_banner -loglevel error \
        -stream_loop -1 -re -i "$CURRENT" \
        -vf "$VF" \
        -f fbdev "$FB_DEVICE"

    # ffmpeg exits if current.mp4 is swapped mid-loop (fetch-video.sh writes a
    # new file), TFT_ROTATE_DEG/TFT_FPS changed (restart), or on any error --
    # loop re-reads env/fb geometry and restarts.
    sleep 2
done
