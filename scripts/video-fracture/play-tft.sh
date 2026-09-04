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
# trades motion smoothness for fewer visible tears (tearing persisted even
# down to 3fps on fracture5's actual wiring, so it's treated here as an
# inherent limit of this panel/driver, not something to fully chase away).
# Also set via /etc/default/video-fracture-tft.
#
# TFT_RED_TINT (0-1, default 0) pushes the picture toward a strong red
# cast -- boosts red and pulls down green/blue by the same amount across
# shadows/mid/highlights. 1 is a heavy red tint.

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
FB_DEVICE="${TFT_FB_DEVICE:-/dev/fb1}"
TFT_FPS="${TFT_FPS:-12}"
TFT_ROTATE_DEG="${TFT_ROTATE_DEG:-0}"
TFT_RED_TINT="${TFT_RED_TINT:-0}"

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
    if [ "$TFT_RED_TINT" != "0" ]; then
        NEG_TINT="-${TFT_RED_TINT}"
        VF="${VF},colorbalance=rs=${TFT_RED_TINT}:rm=${TFT_RED_TINT}:rh=${TFT_RED_TINT}:gs=${NEG_TINT}:gm=${NEG_TINT}:gh=${NEG_TINT}:bs=${NEG_TINT}:bm=${NEG_TINT}:bh=${NEG_TINT}"
    fi
    VF="${VF},format=rgb565le"

    # No -stream_loop here on purpose: ffmpeg's own internal loop hit NAL
    # corruption at the wrap-around point on this file and crash-looped
    # every ~15-20s. Playing the file once per invocation and letting this
    # outer loop restart it is more reliable, at the cost of a brief gap
    # between plays.
    ffmpeg -hide_banner -loglevel error \
        -re -i "$CURRENT" \
        -vf "$VF" \
        -f fbdev "$FB_DEVICE"

    # Also re-loops here when current.mp4 is swapped mid-play (fetch-video.sh
    # writes a new file), TFT_ROTATE_DEG/TFT_FPS changed (restart), or ffmpeg
    # errors -- loop re-reads env/fb geometry each time.
    sleep 0.2
done
