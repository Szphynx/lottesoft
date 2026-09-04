#!/usr/bin/env bash
# One-shot setup for a video-loop kiosk Pi (fracture5): fullscreen
# looping video player, sourced from a public Google Drive folder (always
# plays the newest video in it), plus Tailscale for remote access.
#
# Requires Raspberry Pi OS with Desktop already booting to a screen.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/video-fracture-install.sh [tailscale-authkey]
#
# Then:
#   1. Edit /etc/default/video-fracture and set:
#      - VIDEO_DRIVE_FOLDER_ID: from the folder's share link
#        (https://drive.google.com/drive/folders/<FOLDER_ID> -- copy
#        <FOLDER_ID>). The folder must be shared as "Anyone with the link".
#      - GOOGLE_API_KEY: an API key with the Drive API enabled, from
#        https://console.cloud.google.com/apis/credentials (no OAuth
#        needed -- an API key can list/read publicly-shared files).
#   2. sudo systemctl start video-fracture-fetch.service   # fetch it now
#   3. reboot (or log out/in) so the player autostarts:  sudo reboot
#
# Afterwards, dropping a new video into the folder (or removing the old
# one) is picked up automatically within 15 minutes (see
# video-fracture-fetch.timer) -- it always plays whichever video in the
# folder has the newest Drive modified time. No action needed.
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
# Google Drive folder to play the newest video from.
# From the share link: https://drive.google.com/drive/folders/<FOLDER_ID>
# Copy just the <FOLDER_ID> part below. Must be shared as "Anyone with the link".
VIDEO_DRIVE_FOLDER_ID=REPLACE_WITH_FOLDER_ID

# API key with the Drive API enabled: https://console.cloud.google.com/apis/credentials
# No OAuth needed -- an API key alone can list/read publicly-shared files.
GOOGLE_API_KEY=REPLACE_WITH_API_KEY
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
echo "  1. edit $CONFIG_FILE and set VIDEO_DRIVE_FOLDER_ID and GOOGLE_API_KEY"
echo "  2. sudo systemctl start video-fracture-fetch   # fetch it now"
echo "  3. sudo reboot                              # player autostarts on desktop login"
echo
echo "also recommended: raspi-config -> Display Options -> Screen Blanking -> Disable"
