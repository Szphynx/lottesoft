#!/usr/bin/env bash
# Pull a video from a public Google Drive folder and hot-swap the player
# if it changed. Run periodically by video-fracture-fetch.timer.
#
# No API key: public folders don't expose modified-time without one, so
# "latest" here means highest filename sorted alphabetically. Name files
# so the one you want last sorts last (01.mp4/02.mp4, or date-prefixed
# names both work).
#
# Behavior:
#  - folder's last-by-name video differs from what's playing -> download
#    it, install it, tell the running player to switch (no restart, no
#    black screen)
#  - listing/download fails (no network, folder not shared) -> leave
#    whatever is currently playing alone
#  - folder has no video in it, or nothing ever downloaded -> log/mark
#    that clearly

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
TMP="$FRACTURE_DIR/.download.tmp"
SOURCE_STATE="$FRACTURE_DIR/source-id"
STATUS="$FRACTURE_DIR/status"
MPV_SOCKET="/tmp/mpv-fracture.sock"

mkdir -p "$FRACTURE_DIR"

status() {
    echo "$1" | tee "$STATUS"
}

FOLDER_ID="${VIDEO_DRIVE_FOLDER_ID:-}"

if [ -z "$FOLDER_ID" ] || [ "$FOLDER_ID" = "REPLACE_WITH_FOLDER_ID" ]; then
    status "not configured -- set VIDEO_DRIVE_FOLDER_ID in /etc/default/video-fracture"
    exit 0
fi

if ! command -v gdown >/dev/null; then
    status "error: gdown not installed"
    exit 1
fi

FILE_INFO=$(python3 - "$FOLDER_ID" <<'PYEOF'
import sys
import gdown

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")

try:
    files = gdown.download_folder(id=sys.argv[1], skip_download=True, quiet=True)
except Exception:
    files = None

videos = [f for f in (files or []) if f.path.lower().endswith(VIDEO_EXTS)]
if videos:
    videos.sort(key=lambda f: f.path)
    newest = videos[-1]
    print(newest.id + "\t" + newest.path)
PYEOF
)

if [ -z "$FILE_INFO" ]; then
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): couldn't find a video in the Drive folder (not shared? no network?), still playing previously cached video"
    else
        status "$(date -Is): couldn't find a video in the Drive folder (not shared? no network?) -- no video available yet"
    fi
    exit 0
fi

IFS=$'\t' read -r FILE_ID FILE_NAME <<< "$FILE_INFO"
SOURCE_KEY="${FILE_ID}	${FILE_NAME}"

if [ -s "$CURRENT" ] && [ -f "$SOURCE_STATE" ] && [ "$(cat "$SOURCE_STATE")" = "$SOURCE_KEY" ]; then
    status "$(date -Is): checked, no change ($FILE_NAME)"
    exit 0
fi

rm -f "$TMP"
if ! gdown "https://drive.google.com/uc?id=${FILE_ID}" -O "$TMP" --quiet; then
    rm -f "$TMP"
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): download of '$FILE_NAME' failed, still playing previously cached video"
    else
        status "$(date -Is): download of '$FILE_NAME' failed -- no video available yet"
    fi
    exit 0
fi

if [ ! -s "$TMP" ] || ! ffprobe -v error "$TMP" -show_entries format=duration -of default=noprint_wrappers=1 >/dev/null 2>&1; then
    rm -f "$TMP"
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): '$FILE_NAME' isn't a valid video, still playing previously cached video"
    else
        status "$(date -Is): '$FILE_NAME' isn't a valid video -- no video available yet"
    fi
    exit 0
fi

mv "$TMP" "$CURRENT"
chmod 644 "$CURRENT"
echo "$SOURCE_KEY" > "$SOURCE_STATE"
status "$(date -Is): '$FILE_NAME' installed from Drive, switching player"

if [ -S "$MPV_SOCKET" ]; then
    echo '{"command": ["loadfile", "'"$CURRENT"'", "replace"]}' | socat - "$MPV_SOCKET" >/dev/null 2>&1 || true
fi
