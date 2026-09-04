#!/usr/bin/env bash
# Fullscreen looping video player for fracture5. Started by the desktop
# session's autostart. Keeps an IPC socket open so fetch-video.sh can
# hot-swap the file live, without restarting the player.

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
MPV_SOCKET="/tmp/mpv-fracture.sock"

mkdir -p "$FRACTURE_DIR"
rm -f "$MPV_SOCKET"

# X11 sessions only: stop the screen from blanking/sleeping over the video.
if [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null; then
    xset s off -dpms s noblank || true
fi

# Wait for the first video to land -- the fetch timer may not have run yet.
while [ ! -s "$CURRENT" ]; do
    sleep 2
done

exec mpv \
    --fullscreen \
    --loop-file=inf \
    --no-terminal \
    --no-osc \
    --no-input-default-bindings \
    --cursor-autohide=always \
    --idle=yes \
    --input-ipc-server="$MPV_SOCKET" \
    "$CURRENT"
