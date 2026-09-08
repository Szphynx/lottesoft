#!/usr/bin/env bash
# One-shot Pi setup for the CSI-camera -> HDMI security cam viewer.
#
# Run once per Pi, from the repo root:
#   sudo bash scripts/install-security-cam-hdmi.sh
#
# Same pattern as a fullscreen video-loop player: launches when you log
# into the desktop, no systemd involved. Writes to both of the autostart
# mechanisms current Raspberry Pi OS desktops actually use -- the classic
# XDG ~/.config/autostart (older X11/LXDE sessions) AND labwc's own
# ~/.config/labwc/autostart shell script (current default Wayland
# compositor, which does NOT read ~/.config/autostart at all) -- since
# which one is active isn't detected here, and writing the one it doesn't
# use is harmless.
# Requires desktop auto-login to come up with nobody touching the keyboard:
#   sudo raspi-config -> System Options -> Boot / Auto Login -> Desktop Autologin
#
# To change startup flags (--palette, --mono, --rotate, ...) -- most of
# these are also live controls on the web page and don't need this --
# edit scripts/run-security-cam-hdmi.sh (the python3 line) and reboot, or
# log out and back in.
#
# Note: testing security_cam_hdmi.py by hand over SSH won't show anything
# -- SSH has no route to the desktop's screen. Run it from a terminal
# opened on the Pi's own desktop instead.
#
# The live control page (http://<this Pi>:8790/) needs a login -- its
# CAM_USER/CAM_PASS are auto-generated into ~/.config/security-cam-hdmi.env
# on first install (`sudo cat` that file to see them), same pattern as the
# LED-matrix branch's status page.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
ENV_FILE="$TARGET_HOME/.config/security-cam-hdmi.env"

echo "== system packages =="
apt update
apt install -y python3-picamera2 python3-opencv python3-numpy python3-pygame --no-install-recommends

echo "== camera check =="
if command -v rpicam-hello >/dev/null; then
    rpicam-hello --list-cameras || echo "no camera detected -- check the ribbon cable orientation"
else
    echo "rpicam-hello not found (rpicam-apps) -- can't sanity-check the camera here, try it after reboot"
fi

echo "== control page login =="
mkdir -p "$TARGET_HOME/.config"
if [ ! -f "$ENV_FILE" ]; then
    GENERATED_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
    printf 'CAM_USER=admin\nCAM_PASS=%s\n' "$GENERATED_PASS" > "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
chown "$TARGET_USER:$TARGET_USER" "$ENV_FILE"

# A tiny wrapper script instead of embedding a compound shell command
# straight into the .desktop Exec= line -- the Desktop Entry spec has its
# own field-quoting rules (not POSIX shell quoting), so nested quotes
# there are fragile. Both autostart mechanisms below just point at this.
RUN_SCRIPT="$REPO_DIR/scripts/run-security-cam-hdmi.sh"
cat > "$RUN_SCRIPT" <<EOF
#!/bin/sh
. "$ENV_FILE"
exec /usr/bin/python3 $REPO_DIR/security_cam_hdmi.py --stats
EOF
chmod +x "$RUN_SCRIPT"

echo "== desktop autostart (XDG, for X11/LXDE sessions) =="
AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/security-cam-hdmi.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Security cam HDMI display
Exec=$RUN_SCRIPT
X-GNOME-Autostart-enabled=true
EOF
chown "$TARGET_USER:$TARGET_USER" "$AUTOSTART_DIR/security-cam-hdmi.desktop"

echo "== desktop autostart (labwc, current default Wayland compositor) =="
LABWC_DIR="$TARGET_HOME/.config/labwc"
LABWC_AUTOSTART="$LABWC_DIR/autostart"
mkdir -p "$LABWC_DIR"
[ -f "$LABWC_AUTOSTART" ] || printf '#!/bin/sh\n' > "$LABWC_AUTOSTART"
# Drop any line from a previous install (old formats included) before
# re-adding one, so re-running this script never launches two copies.
grep -v "security[-_]cam[-_]hdmi" "$LABWC_AUTOSTART" > "$LABWC_AUTOSTART.tmp" || true
mv "$LABWC_AUTOSTART.tmp" "$LABWC_AUTOSTART"
echo "$RUN_SCRIPT &" >> "$LABWC_AUTOSTART"
chmod +x "$LABWC_AUTOSTART"
chown -R "$TARGET_USER:$TARGET_USER" "$LABWC_DIR"

echo
echo "done. wrote:"
echo "  $AUTOSTART_DIR/security-cam-hdmi.desktop"
echo "  $LABWC_AUTOSTART"
echo "control page login: cat $ENV_FILE"
echo "make sure desktop auto-login is on (sudo raspi-config -> Boot / Auto Login -> Desktop Autologin), then:"
echo "  sudo reboot"
echo "it'll launch fullscreen automatically after login, control page at http://<this Pi>:8790/"
