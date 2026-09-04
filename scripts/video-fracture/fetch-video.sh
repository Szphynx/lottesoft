#!/usr/bin/env bash
# Pull the newest video from a public Google Drive folder and hot-swap the
# player if it changed. Run periodically by video-fracture-fetch.timer.
#
# Uses the Drive API (with an API key, no OAuth) to list the folder and
# find the most recently modified video -- this needs real Drive metadata,
# which a plain download link can't give us. The file itself is still
# fetched via the public uc?id= link, same as a single shared file.
#
# Behavior:
#  - folder's newest video differs from what's playing -> download it,
#    install it, tell the running player to switch (no restart, no black
#    screen)
#  - listing/download fails (no network, folder not shared, API key bad)
#    -> leave whatever is currently playing alone
#  - folder has no video in it, or nothing ever downloaded -> log/mark
#    that clearly

set -uo pipefail

FRACTURE_DIR="/var/lib/video-fracture"
CURRENT="$FRACTURE_DIR/current.mp4"
TMP="$FRACTURE_DIR/.download.tmp"
LISTING="$FRACTURE_DIR/.folder-list.json"
SOURCE_STATE="$FRACTURE_DIR/source-id"
STATUS="$FRACTURE_DIR/status"
MPV_SOCKET="/tmp/mpv-fracture.sock"

mkdir -p "$FRACTURE_DIR"

status() {
    echo "$1" | tee "$STATUS"
}

FOLDER_ID="${VIDEO_DRIVE_FOLDER_ID:-}"
API_KEY="${GOOGLE_API_KEY:-}"

if [ -z "$FOLDER_ID" ] || [ "$FOLDER_ID" = "REPLACE_WITH_FOLDER_ID" ] \
    || [ -z "$API_KEY" ] || [ "$API_KEY" = "REPLACE_WITH_API_KEY" ]; then
    status "not configured -- set VIDEO_DRIVE_FOLDER_ID and GOOGLE_API_KEY in /etc/default/video-fracture"
    exit 0
fi

if ! command -v gdown >/dev/null; then
    status "error: gdown not installed"
    exit 1
fi

Q="'${FOLDER_ID}' in parents and mimeType contains 'video/' and trashed = false"
if ! curl -sS --fail --get "https://www.googleapis.com/drive/v3/files" \
    --data-urlencode "q=${Q}" \
    --data-urlencode "orderBy=modifiedTime desc" \
    --data-urlencode "fields=files(id,name,modifiedTime)" \
    --data-urlencode "pageSize=1" \
    --data-urlencode "key=${API_KEY}" \
    -o "$LISTING"; then
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): couldn't list Drive folder (bad API key / folder not shared / no network), still playing previously cached video"
    else
        status "$(date -Is): couldn't list Drive folder (bad API key / folder not shared / no network) -- no video available yet"
    fi
    exit 0
fi

FILE_INFO=$(python3 - "$LISTING" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as fh:
    data = json.load(fh)
files = data.get("files") or []
if files:
    f = files[0]
    print(f["id"] + "\t" + f.get("modifiedTime", "") + "\t" + f.get("name", ""))
PYEOF
)

if [ -z "$FILE_INFO" ]; then
    if [ -s "$CURRENT" ]; then
        status "$(date -Is): no video in the Drive folder anymore, still playing previously cached video"
    else
        status "$(date -Is): no video in the Drive folder -- no video available yet"
    fi
    exit 0
fi

IFS=$'\t' read -r FILE_ID FILE_MTIME FILE_NAME <<< "$FILE_INFO"
SOURCE_KEY="${FILE_ID}	${FILE_MTIME}"

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
