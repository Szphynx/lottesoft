#!/usr/bin/env bash
# Pulls the currently-checked-out branch if origin has new commits, and
# marks that a reboot is needed to apply it -- does NOT restart anything
# or reboot itself, since the LED display shouldn't blink out without you
# knowing. The control page (see UPDATE_PENDING_FILE in player.py) shows
# a banner once this has pulled, with a Reboot button to apply it when
# convenient. Meant to be run periodically by
# video-fracture-led-autoupdate.timer, not by hand.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

# git refuses to touch a repo it doesn't own (e.g. cloned as a regular
# user, this script running as root via systemd) unless told it's fine.
git config --global --add safe.directory "$REPO_DIR"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch --quiet origin "$BRANCH"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$(date -Is) up to date ($BRANCH @ ${LOCAL:0:7})"
    exit 0
fi

echo "$(date -Is) new commits on $BRANCH (${LOCAL:0:7} -> ${REMOTE:0:7}), pulling"
git pull --ff-only origin "$BRANCH"

install -d -m 755 /var/lib/video-fracture
git rev-parse --short HEAD > /var/lib/video-fracture/led-update-pending
echo "$(date -Is) pulled -- reboot needed to apply"
