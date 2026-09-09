#!/usr/bin/env bash
# Add a HUB75 LED matrix ("looper led") to a Pi that's already running the
# HDMI video-fracture looper (scripts/video-fracture-install.sh). Purely
# additive: a new video-fracture-led.service shows the SAME video
# (/var/lib/video-fracture/current.mp4 -- fetched by the existing
# video-fracture-fetch.timer, unchanged) on a Joy-IT RB-MatrixCtrl + HUB75
# panel, via its own live web control page. Nothing here touches the
# existing video-fracture fetch/play-loop files or services. The control
# page binds to the Tailscale IP only and requires a login (generated
# below), same posture as scripts/status_server.py -- it can restart the
# service, reboot the Pi, and pull code, so it doesn't get to be open on
# the LAN with no auth.
#
# Uses the same controller/library as thermal_matrix.py (rgbmatrix +
# Joy-IT RB-MatrixCtrl), so this Pi ends up with the identical driver
# stack the thermal-camera and media-matrix projects already use --
# nothing reinvented.
#
# IMPORTANT: driving a HUB75 panel requires the Pi's PWM-conflicting audio
# disabled (dtparam=audio=off + blacklisting snd_bcm2835) -- same
# requirement thermal_matrix.py has. If the existing HDMI video loop plays
# sound through this Pi, THIS WILL MUTE IT after the reboot below. There's
# no way around that with this hardware combo -- if you need audio on the
# HDMI side, run the LED matrix from a different Pi instead.
#
# Requires: this Pi has already run scripts/video-fracture-install.sh
# (video-fracture-fetch.timer needs to already be fetching a video).
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/video-fracture-led-install.sh
#
# Then:
#   1. sudo reboot                                    # audio-disable needs a reboot
#   2. sudo systemctl enable --now video-fracture-led
#   3. control page: http://<tailscale-ip>:8099/
#
# Useful commands:
#   sudo systemctl status video-fracture-led
#   journalctl -u video-fracture-led -f

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=/boot/firmware/config.txt

echo "== git safe.directory =="
git config --global --add safe.directory "$REPO_DIR"

if ! systemctl list-unit-files 2>/dev/null | grep -q '^video-fracture-fetch.timer'; then
    echo "warning: video-fracture-fetch.timer not found on this Pi." >&2
    echo "         run scripts/video-fracture-install.sh first so there's a video to show." >&2
fi

echo "== system packages =="
apt update
apt install -y python3-opencv python3-pip python3-numpy python3-pil

echo "== python packages (rgbmatrix, same as thermal_matrix.py) =="
pip3 install --break-system-packages "git+https://github.com/hzeller/rpi-rgb-led-matrix"

echo "== boot config (audio off for the panel PWM -- see warning above) =="
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=audio=off"
echo "blacklist snd_bcm2835" > /etc/modprobe.d/blacklist-rgb-matrix.conf

install -d -m 755 /var/lib/video-fracture

echo "== config =="
FLAGS_FILE=/etc/default/video-fracture-led
if [ ! -f "$FLAGS_FILE" ]; then
    cat > "$FLAGS_FILE" <<'EOF'
# First-boot-only seed for panel calibration (led-rgb-sequence,
# multiplexing, row-address-type, panel-type, pixel-mapper). Once the
# control page (http://<tailscale-ip>:8099/) saves any config -- which it
# does automatically on every change -- THIS FILE IS IGNORED from then on;
# use the "panel hardware" section of the control page instead, no SSH or
# restart needed. Only relevant again if you delete
# /var/lib/video-fracture/led-config.json to start over.
FLAGS="--led-rgb-sequence RGB --multiplexing 0 --row-address-type 0"
EOF
fi

echo "== control page login (same scheme as scripts/status_server.py) =="
AUTH_FILE=/etc/default/video-fracture-led-auth
if [ ! -f "$AUTH_FILE" ]; then
    printf 'STATUS_USER=admin\nSTATUS_PASS=conejo\n' > "$AUTH_FILE"
    chmod 600 "$AUTH_FILE"
fi

echo "== systemd service =="
cat > /etc/systemd/system/video-fracture-led.service <<EOF
[Unit]
Description=video-fracture looper on HUB75 LED matrix
After=network.target video-fracture-fetch.service

[Service]
Type=simple
EnvironmentFile=$FLAGS_FILE
EnvironmentFile=$AUTH_FILE
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash -c '/usr/bin/python3 $REPO_DIR/scripts/video-fracture-led/player.py \$FLAGS'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

echo "== autoupdate timer (off by default -- toggle from the control page) =="
cat > /etc/systemd/system/video-fracture-led-autoupdate.service <<EOF
[Unit]
Description=Pull latest video-fracture-led code (does not restart/reboot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/bin/bash $REPO_DIR/scripts/video-fracture-led/auto-update.sh
EOF

cat > /etc/systemd/system/video-fracture-led-autoupdate.timer <<'EOF'
[Unit]
Description=Check for video-fracture-led updates periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload

echo
echo "done. now:"
echo "  1. sudo reboot                                    # audio-disable needs this"
echo "  2. sudo systemctl enable --now video-fracture-led"
echo "  3. control page: http://\$(tailscale ip -4):8099/"
echo "     login: cat $AUTH_FILE"
echo
echo "the control page is reachable from your tailnet only (not the open"
echo "LAN), and requires that login -- same posture as scripts/status_server.py,"
echo "since this page can restart the service, reboot the Pi, and pull code."
echo
echo "if the image looks wrong (color tint, scrambled/checkerboard), fix it"
echo "live from the control page's 'panel hardware' section -- no SSH needed."
echo
echo "code auto-update (git pull, no auto-restart), a soft 'restart service'"
echo "button, and a full 'Reboot this Pi' button are all on the control page --"
echo "nothing to enable by hand."
