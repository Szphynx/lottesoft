# Devices

Update list detail: `docs/HTML_SERVICE_STANDARD.md`.

## Confirmed

| Host | Hardware | Software | Web page | Port |
|---|---|---|---|---|
| `fracture4` | Pi + HUB75 64x64 (Joy-IT RB-MatrixCtrl) | `video-fracture` (HDMI) + `video-fracture-led` | led: ✅ · HDMI: ❌ | 8104 (was 8099) |
| `fracture5` | Pi + SPI TFT panel | `video-fracture` (HDMI) + `video-fracture-tft` | ❌ none | — |

## Named in branches, hostname unconfirmed

| Ref | Hardware | Software | Web page | Port |
|---|---|---|---|---|
| "fracture 2" (branch `tailscale-matrix-led-fracture-2`) | Pi 4 + HUB75 64x64 | `thermal_matrix.py` | status only | 8787 |
| thermal cam (branch `bodyheat-color-mode-led`) | Pi 4 + MLX90640 + HUB75 64x64 | `thermal_matrix.py` | status only | 8787 |
| WS2812 rig (branch `rp3-led-matrices`) | Pi 3 + 2× WS2812 32x8 | `media_matrix.py` | ✅ control | 8098 |
| MAX7219 rig (branch `caveman-ultra-ponytail`) | Pi 3 + 6× MAX7219 8x32 SPI | `double_matrix.py` | ✅ control | 8099 |
| multi-display (branches `rpi3-multi-display-*`) | Pi 3 + SPI displays + Pico OLED | `display_test.py`, `pico_oled_test.py` | ❌ none (test scripts) | — |

Plus **1-2 more HDMI looper Pis** (user counts 3-4 total; only fracture4/fracture5 named in repo).

Repo contains no device registry — the above is inferred from branch names
and script comments. To make it authoritative: `tailscale status` on any
node, or https://login.tailscale.com/admin/machines.

## What needs updating

### P1 — security

| Device/role | Issue |
|---|---|
| WS2812 rig (`media_matrix.py`) | Binds `0.0.0.0`, **no auth**. Full control + uploads open to the LAN. |
| MAX7219 rig (`double_matrix.py`) | Binds `0.0.0.0`, **no auth**. Full control + service restart open to the LAN. |

### P2 — no visibility at all

| Device/role | Issue |
|---|---|
| All HDMI looper Pis (3-4, incl. fracture4/5) | No web page. SSH only. Largest deployment in the fleet. Cheap to fix — mpv IPC socket already open. |
| fracture5 TFT | No web page. Rotate/fps/red-tint need SSH + file edit + restart. |

### P3 — missing standard features

| Device/role | Missing |
|---|---|
| WS2812 rig | autosave-on-change, presets, restart btn, reboot btn, badge, uptime, status.json fields, PNG preview, notify-only autoupdate |
| MAX7219 rig | autosave-on-change, presets, self-update, reboot btn, uptime, status.json fields, PNG preview |
| All thermal Pis | stale `status_server.py`/`dashboard.html` (87/12 lines vs main's 164/389) — no preview, restart, reboot, fleet map |
| fracture4 (`video-fracture-led`) | PNG preview only |
| `main` | self-update, `version` in status.json, page shell |

### P4 — port migration

| Device/role | From | To |
|---|---|---|
| WS2812 rig | 8098 | 8400 + N |
| MAX7219 rig | 8099 | 8500 + N |
| fracture4 led | 8099 | 8104 ✅ done |

## Update commands, per device

Generic (repo is usually `~/lottesoft`, `/home/fracture/lottesoft` on fracture4):

```
cd ~/lottesoft
git fetch origin
git checkout <branch>      # only if switching branches
git pull
sudo systemctl restart <service>
```

| Device/role | Branch | Restart |
|---|---|---|
| `fracture4` — led | `claude/tailscale-rpi-install-itd96p` | `sudo systemctl restart video-fracture-led` |
| `fracture4` — HDMI looper | same | **no service** — see gotcha below |
| `fracture5` — TFT | `claude/tailscale-rpi-install-itd96p` | `sudo systemctl restart video-fracture-tft` |
| `fracture5` — HDMI looper | same | **no service** — see gotcha below |
| other HDMI loopers | `claude/tailscale-rpi-install-itd96p` | **no service** — see gotcha below |
| WS2812 rig | `claude/rp3-led-matrices` | `sudo systemctl restart media-matrix` — or just wait, autoupdate pulls + restarts every 2min |
| MAX7219 rig | `claude/caveman-ultra-ponytail` | `sudo systemctl restart double-matrix` |
| thermal Pis | ⚠️ unknown per device | `sudo systemctl restart thermal-matrix thermal-status` |

Which branch a device is actually on:
`git -C ~/lottesoft rev-parse --abbrev-ref HEAD`

### Gotchas

**HDMI looper has no systemd service.** `play-loop.sh` is started by
`/etc/xdg/autostart/video-fracture.desktop`, so `systemctl restart` does
nothing. To apply changes: log out/in on the desktop, or `sudo reboot`.
Only its fetch side is systemd (`video-fracture-fetch.service`/`.timer`).

**git safe.directory.** Repo is cloned as `fracture`, git runs as root via
sudo/systemd → "dubious ownership" error. Already handled inside
`video-fracture-led-install.sh` and `auto-update.sh`; elsewhere run once:
```
sudo git config --global --add safe.directory ~/lottesoft
```

**Port changes on restart.** `video-fracture-led` moves `:8099` → `:8104`
on fracture4 (8100 + hostname number) the moment it restarts. Update
bookmarks.

**Auth appears on restart.** `video-fracture-led` now requires a login
(`admin` / `conejo`) and binds tailnet-only. LAN access stops working.

**Reboot required (not just restart)** when the update touches
`/boot/firmware/config.txt` — i.e. re-running any install script that sets
`dtparam=audio=off` or overlays.

## Order of work

1. Auth + tailnet bind on the two open rigs (P1).
2. HDMI looper control page (P2) — one page, deploys to 3-4 Pis at once.
3. Backport `main`'s `status_server.py` + `dashboard.html` to all branches (P3, fleet-wide visibility).
4. Remaining per-service feature gaps + port migration.
