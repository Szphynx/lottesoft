#!/usr/bin/env bash
# Get a Pi onto the tailnet with SSH, non-interactively.
#
# 1. Get key: https://login.tailscale.com/admin/settings/keys
#    -> Generate auth key -> Reusable OFF, Ephemeral OFF, Expiry 1 day.
# 2. Run:  sudo bash setup-tailscale.sh tskey-auth-XXXXXXXXXXXX
# 3. Note the printed IP. Repeat per Pi (one key per Pi).

set -euo pipefail

AUTHKEY="${1:-}"

if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi

if tailscale status >/dev/null 2>&1; then
    echo "already connected, skipping tailscale up"
else
    : "${AUTHKEY:?usage: sudo bash setup-tailscale.sh <authkey>}"
    tailscale up --authkey="$AUTHKEY" --ssh --hostname="$(hostname)"
fi

systemctl enable --now ssh

echo "done. tailnet IP:"
tailscale ip -4
