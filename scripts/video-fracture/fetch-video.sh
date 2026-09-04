#!/usr/bin/env bash
# Pull the fracture video from a public Google Drive file and hot-swap the
# player if it changed. Run periodically by video-fracture-fetch.timer.
#
# Behavior:
#  - download succeeds and differs from what's playing -> install it, tell
#    the running player to switch (no restart, no black screen)
#  - download fails (no network, file not shared, file deleted) -> leave
#    whatever is currently playing alone
#  - never downloaded anything successfully -> log/mark that clearly

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
TMP="$FRACTURE_DIR/.download.tmp"
STATUS="$FRACTURE_DIR/status"
MPV_SOCKET="/tmp/mpv-fracture.sock"

mkdir -p "$FRACTURE_DIR"

status() {
    echo "$1" | tee "$STATUS"
}

if [ -z "${VIDEO_DRIVE_FILE_ID:-}" ] || [ "$VIDEO_DRIVE_FILE_ID" = "REPLACE_WITH_FILE_ID" ]; then
    status "not configured -- set VIDEO_DRIVE_FILE_ID in /etc/default/video-fracture"
    exit 0
fi

if ! command -v gdown >/dev/null; then
    status "error: gdown not installed"
    exit 1
fi

rm -f "$TMP"
if ! gdown "https://drive.google.com/uc?id=${VIDEO_DRIVE_FILE_ID}" -O "$TMP" --quiet; then
    rm -f "$TMP"
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): download failed, still playing previously cached video"
    else
        status "$(date -Is): file not on Drive (or not shared publicly) -- no video available yet"
    fi
    exit 0
fi

if [ ! -s "$TMP" ] || ! ffprobe -v error "$TMP" -show_entries format=duration -of default=noprint_wrappers=1 >/dev/null 2>&1; then
    rm -f "$TMP"
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): downloaded file isn't a valid video (private/removed on Drive?), still playing previously cached video"
    else
        status "$(date -Is): file not on Drive (or not shared publicly) -- no video available yet"
    fi
    exit 0
fi

if [ -s "$CURRENT" ] && cmp -s "$TMP" "$CURRENT"; then
    rm -f "$TMP"
    status "$(date -Is): checked, no change"
    exit 0
fi

mv "$TMP" "$CURRENT"
chmod 644 "$CURRENT"
status "$(date -Is): new video installed from Drive, switching player"

if [ -S "$MPV_SOCKET" ]; then
    echo '{"command": ["loadfile", "'"$CURRENT"'", "replace"]}' | socat - "$MPV_SOCKET" >/dev/null 2>&1 || true
fi
