#!/usr/bin/env bash
# Start Rotaryplay with rotaryplay.conf applied:
#   ./run.sh                       # as configured
#   ./run.sh --calibrate fans      # any extra fan_wall.py flag, wins over the conf
#   ./run.sh --help                # every flag, incl. one per page setting
# The fan-wall service runs this same script.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/rotaryplay.conf"

args=()
add() { [ -n "$2" ] && args+=("$1" "$2") || true; }
add --teensy-ip "${TEENSY_IP:-}"
add --teensy-port "${TEENSY_PORT:-}"
add --teensy-serial "${TEENSY_SERIAL:-}"
add --web-port "${WEB_PORT:-}"
add --sketch "${SKETCH:-}"
add --state-file "${STATE_FILE:-}"
add --upload-dir "${UPLOAD_DIR:-}"
add --media "${MEDIA:-}"
add --font "${FONT:-}"
add --fps "${FPS:-}"
eval "extra=(${EXTRA_FLAGS:-})"   # keeps quoted values like --text "hello world" together

exec "${PYTHON:-python3}" "$DIR/fan_wall.py" "${args[@]}" "${extra[@]}" "$@"
