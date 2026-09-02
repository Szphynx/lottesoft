#!/usr/bin/env bash
# Get a Pi onto the tailnet with SSH, non-interactively.
#
# 1. Get key: https://login.tailscale.com/admin/settings/keys
#    -> Generate auth key -> Reusable OFF, Ephemeral OFF, Expiry 1 day.
# 2. Run:  sudo bash setup-tailscale.sh tskey-auth-XXXXXXXXXXXX
# 3. Note the printed IP. Repeat per Pi (one key per Pi).

set -euo pipefail

AUTHKEY="${1:?usage: sudo bash setup-tailscale.sh <authkey>}"

curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey="$AUTHKEY" --ssh --hostname="$(hostname)"
systemctl enable --now ssh

echo "done. tailnet IP:"
tailscale ip -4
