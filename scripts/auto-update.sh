#!/usr/bin/env bash
# Pulls the currently-checked-out branch if origin has new commits, then
# restarts the media-matrix service so the change takes effect. Meant to
# be run periodically by media-matrix-autoupdate.timer, not by hand.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# git refuses to touch a repo it doesn't own (e.g. cloned as a regular user,
# this script running as root via systemd) unless told it's fine.
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
systemctl restart media-matrix
echo "$(date -Is) restarted media-matrix"
