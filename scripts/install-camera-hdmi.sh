#!/usr/bin/env bash
# One-shot Pi setup for the CSI-camera -> HDMI thermal-style display project.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-camera-hdmi.sh [--autostart]
#
# Then try it by hand first, logged into the Pi's desktop (needs a display,
# not just SSH):
#   python3 thermal_camera_hdmi.py --stats
#
# --autostart writes a desktop autostart entry so it launches automatically
# next time you log into the desktop. That still requires desktop
# auto-login to be turned on if you want it to come up with no one
# touching the keyboard: sudo raspi-config -> System Options -> Boot / Auto
# Login -> Desktop Autologin.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DO_AUTOSTART=0
[ "${1:-}" = "--autostart" ] && DO_AUTOSTART=1

echo "== system packages =="
apt update
apt install -y python3-picamera2 python3-opencv python3-numpy --no-install-recommends

echo "== camera check =="
if command -v rpicam-hello >/dev/null; then
    rpicam-hello --list-cameras || echo "no camera detected -- check the ribbon cable orientation"
else
    echo "rpicam-hello not found (rpicam-apps) -- can't sanity-check the camera here, try it after reboot"
fi

if [ "$DO_AUTOSTART" -eq 1 ]; then
    echo "== desktop autostart =="
    TARGET_USER="${SUDO_USER:-$USER}"
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/thermal-camera-hdmi.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Thermal camera HDMI display
Exec=/usr/bin/python3 $REPO_DIR/thermal_camera_hdmi.py --stats
X-GNOME-Autostart-enabled=true
EOF
    chown "$TARGET_USER:$TARGET_USER" "$AUTOSTART_DIR/thermal-camera-hdmi.desktop"
    echo "wrote $AUTOSTART_DIR/thermal-camera-hdmi.desktop"
fi

echo
echo "done. try it now, from the Pi's desktop:"
echo "  python3 $REPO_DIR/thermal_camera_hdmi.py --stats"
