#!/usr/bin/env bash
# One-shot setup for a video-loop kiosk Pi (fracture5): fullscreen
# looping video player, sourced from either a local folder (e.g. a
# mounted USB drive) or a public Google Drive folder, plus Tailscale for
# remote access.
#
# Either source picks "which video plays" the same way: highest filename
# sorted alphabetically (Drive doesn't expose modified-time without an
# API key, so local uses the same convention for consistency). Name
# files so the one you want playing sorts last, e.g. 01.mp4/02.mp4 or
# date-prefixed names.
#
# Requires Raspberry Pi OS with Desktop already booting to a screen.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/video-fracture-install.sh [tailscale-authkey]
#
# Then, pick ONE source in /etc/default/video-fracture:
#   - Local folder: set VIDEO_LOCAL_DIR to a path on the Pi (a mounted
#     USB drive works well). Takes priority over Drive when set.
#   - Google Drive: set VIDEO_DRIVE_FOLDER_ID to the folder's ID (from
#     its share link: https://drive.google.com/drive/folders/<FOLDER_ID>
#     -- copy <FOLDER_ID>). The folder must be shared as "Anyone with
#     the link".
# Then:
#   1. sudo systemctl start video-fracture-fetch.service   # fetch it now
#   2. reboot (or log out/in) so the player autostarts:  sudo reboot
#
# Afterwards, dropping a new video into the folder (with a filename that
# sorts after the current one) is picked up automatically within 15
# minutes (see video-fracture-fetch.timer). No action needed.
#
# Useful commands:
#   cat /var/lib/video-fracture/status          # last fetch result
#   sudo systemctl start video-fracture-fetch   # force a re-check now
#   journalctl -u video-fracture-fetch -f       # fetch logs

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTHKEY="${1:-}"

echo "== system packages =="
apt update
apt install -y mpv ffmpeg socat python3-pip

echo "== python packages =="
pip3 install --break-system-packages gdown

echo "== fracture5 data dir =="
install -d -m 755 /var/lib/video-fracture

echo "== config =="
CONFIG_FILE=/etc/default/video-fracture
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<'EOF'
# Pick ONE source -- VIDEO_LOCAL_DIR takes priority over
# VIDEO_DRIVE_FOLDER_ID when both are set. Either way, plays whichever
# video's filename sorts last alphabetically -- name files so the one
# you want playing sorts last (01.mp4/02.mp4, or date-prefixed names).

# A folder on the Pi itself, e.g. a mounted USB drive: /media/pi/USBNAME
VIDEO_LOCAL_DIR=REPLACE_WITH_LOCAL_DIR

# A public Google Drive folder (no API key needed). From the share link
# https://drive.google.com/drive/folders/<FOLDER_ID> -- copy just the
# <FOLDER_ID> part below. Must be shared as "Anyone with the link".
VIDEO_DRIVE_FOLDER_ID=REPLACE_WITH_FOLDER_ID
EOF
fi

echo "== fetch service + timer (periodic Drive check) =="
cat > /etc/systemd/system/video-fracture-fetch.service <<EOF
[Unit]
Description=Fetch latest fracture video from Google Drive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$CONFIG_FILE
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/video-fracture/fetch-video.sh
EOF

cat > /etc/systemd/system/video-fracture-fetch.timer <<'EOF'
[Unit]
Description=Periodically fetch latest fracture video from Google Drive

[Timer]
OnBootSec=30s
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now video-fracture-fetch.timer

echo "== player autostart (any desktop user, X11 or Wayland/labwc) =="
install -d -m 755 /etc/xdg/autostart
cat > /etc/xdg/autostart/video-fracture.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Video Fracture
Exec=bash $REPO_DIR/scripts/video-fracture/play-loop.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF

echo "== tailscale (remote access) =="
if [ -n "$AUTHKEY" ]; then
    bash "$REPO_DIR/scripts/setup-tailscale.sh" "$AUTHKEY"
else
    echo "no authkey given, skipping -- run scripts/setup-tailscale.sh <key> later"
fi

echo
echo "done. now:"
echo "  1. edit $CONFIG_FILE and set VIDEO_LOCAL_DIR or VIDEO_DRIVE_FOLDER_ID"
echo "  2. sudo systemctl start video-fracture-fetch   # fetch it now"
echo "  3. sudo reboot                              # player autostarts on desktop login"
echo
echo "also recommended: raspi-config -> Display Options -> Screen Blanking -> Disable"
