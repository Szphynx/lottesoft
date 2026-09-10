# HTML control-service standard

Reference implementation: `scripts/video-fracture-led/player.py`.

## Required features

| # | Feature | Spec |
|---|---|---|
| 1 | Tailnet-only bind | `tailscale ip -4`, fallback `127.0.0.1`. Never `0.0.0.0`. |
| 2 | Auth | HTTP Basic, all routes, `hmac.compare_digest`. Creds in `/etc/default/<service>-auth` (600), loaded as 2nd `EnvironmentFile=`. |
| 3 | Realtime control | Whole client `state` object POSTed to `/update`, debounced 200ms. Server clamps every field. |
| 4 | Autosaved state | Every change written to disk (tmp + `os.replace`), autoloaded on start. |
| 5 | Named presets | Save/list/load. Names must match `^[A-Za-z0-9_-]{1,50}$`. Load applies live + becomes next-boot state. |
| 6 | Self-update | `<service>-autoupdate.timer` (~2min) → `git pull --ff-only`. Page checkbox toggles it. **No auto-restart** — writes pending marker, page shows banner + reboot button. Marker cleared on process start. |
| 7 | Restart button | `systemctl restart <service>`. Must reply to HTTP request *before* restarting. |
| 8 | Reboot button | `systemctl reboot`, confirm-gated. |
| 9 | Liveness badge | ONLINE/OFFLINE from existing polls. Flips OFFLINE after 2 consecutive failures. "back online" on recovery. |
| 10 | Service uptime | `systemctl show <service> -p ActiveEnterTimestamp`. |
| 11 | Page shell | `font:16px monospace;background:#111;color:#eee;max-width:32rem;margin:2rem auto;padding:0 1rem`. `<h1>` 1.1rem + inline badge. `<h2>` 1rem `#aaa`. Rare controls in collapsed `<details>`. |
| 12 | `/status.json` fields | `host`, `active`, `version`, `service_uptime`, `cpu_temp_c` + service-specific. `version` = `<branch>@<short-sha>`, computed once at start. |
| 13 | PNG preview | `/preview.png` binary. Not JSON pixel arrays — fleet dashboard can't consume those. |

## Minimum remote-readable set

What a command center must be able to read from any node. Everything here is
read-only — control is separate.

| Field | Source | Status |
|---|---|---|
| Preview | `/preview.png` | ❌ only `main`. Others serve JSON pixels — unusable by a fleet view. |
| Service running | `/status.json` `active` + endpoint answering at all | ✅ |
| Version | `/status.json` `version` (`<branch>@<sha>`) | ✅ video-fracture-led only |
| Uptime | `/status.json` `service_uptime` | ✅ where present |
| Last seen | **Hub-side, not a Pi field.** Hub records the timestamp of each node's last successful poll/post. | n/a until hub exists |

`last_seen` deliberately doesn't live on the Pi: a node that's down can't
report that it's down. It has to be derived by whatever is polling.

## Services with no web page at all

| Service | Where | Deployed | Control today |
|---|---|---|---|
| `video-fracture` (HDMI looper, mpv) | `tailscale-rpi-install-itd96p` | ~3-4 Pis | SSH only |
| `video-fracture-tft` (SPI TFT looper, ffmpeg→fb) | `tailscale-rpi-install-itd96p` | 1 Pi (fracture5) | SSH: edit `/etc/default/video-fracture-tft`, restart service |

Largest deployment in the fleet, zero visibility. Highest-value gap.

**HDMI looper is cheap to do** — `play-loop.sh` already runs mpv with
`--input-ipc-server=/tmp/mpv-fracture.sock`, which gives for free:

| Standard requirement | mpv IPC |
|---|---|
| PNG preview | `screenshot-to-file <path> video` — captures the real HDMI output |
| Realtime image control | `brightness`/`contrast`/`saturation`/`gamma`/`hue` properties, -100..100, live |
| Elapsed/remaining | `time-pos` / `duration` |
| Current file | `filename` |
| Pause/seek | `pause`, `seek` |

No re-encode, no new render loop — the page is a thin shell over the
socket that already exists.

**TFT variant is harder**: ffmpeg pipe to framebuffer, no IPC. Controls
(`TFT_ROTATE_DEG`/`TFT_FPS`/`TFT_RED_TINT`) are env vars needing a service
restart, so "realtime" means restart-on-change. Preview would come from
reading `/dev/fb1` directly rather than from the player.

**Wrinkle for both**: neither is a systemd service in the normal sense —
HDMI looper is an XDG autostart desktop app, so `service_uptime` needs to
come from process start time, not `ActiveEnterTimestamp`. TFT one *is* a
systemd service (`video-fracture-tft.service`) and is fine as-is.

## Current state

✅ present · ⚠️ partial · ❌ missing · — n/a

| | status_server<br>(main) | status_server<br>(other branches) | media_matrix | double_matrix | video-fracture-led |
|---|---|---|---|---|---|
| 1 Tailnet bind | ✅ | ✅ | ❌ | ❌ | ✅ |
| 2 Auth | ✅ | ✅ | ❌ | ❌ | ✅ |
| 3 Realtime | — | — | ✅ | ✅ | ✅ |
| 4 Autosave | — | — | ⚠️ on-demand | ⚠️ on-demand | ✅ |
| 5 Presets | — | — | ❌ | ❌ | ✅ |
| 6 Self-update | ❌ | ❌ | ⚠️ auto-restarts | ❌ | ✅ |
| 7 Restart btn | ✅ | ❌ | ❌ | ✅ | ✅ |
| 8 Reboot btn | ✅ | ❌ | ❌ | ❌ | ✅ |
| 9 Badge | ✅ | ❌ | ❌ | ✅ | ✅ |
| 10 Uptime | ✅ | ✅ | ❌ | ❌ | ✅ |
| 11 Shell | ⚠️ variant | ⚠️ variant | ✅ | ✅ | ✅ |
| 12 status.json | ⚠️ no version | ⚠️ no version/cpu_temp | ❌ | ❌ | ✅ |
| 13 PNG preview | ✅ | ❌ | ❌ | ❌ | ❌ |

## Work needed, per branch

**All branches except `main`** — `status_server.py` + `dashboard.html` are stale (87/12 lines vs main's 164/389). Missing: preview, restart, reboot, CORS, fleet map. Fix: backport both files from `main`.

**`rp3-led-matrices-gff4ft`** (`media_matrix.py`): add bind_addr + auth, autosave-on-change, named presets, restart btn, reboot btn, liveness badge, uptime, status.json fields, PNG preview. Change autoupdate to notify-only.

**`caveman-ultra-ponytail-vshfzk`** (`double_matrix.py`): add bind_addr + auth, autosave-on-change, named presets, self-update, reboot btn, uptime, status.json fields, PNG preview.

**`tailscale-rpi-install-itd96p`** (`video-fracture-led`): add PNG preview. (status.json contract done.)

**`tailscale-rpi-install-itd96p`** (`video-fracture` HDMI + TFT): build a control page from scratch — nothing exists. See "Services with no web page at all". Port assignment needed (8101/8102 suggested).

**`main`**: add self-update, add `version` to status.json. Align page shell.

**`rpi3-multi-display-*`, `tailscale-matrix-led-fracture-2`, `bodyheat-color-mode-led`**: no control service of their own — only the stale status_server backport applies.

## Ports: band per service + host number

Port = **service band + the trailing number in the hostname**.
`fracture4` → `8104`, `fracture5` → `8105`. No registry to keep in sync,
no collisions possible, and the port says which Pi you're on.
`--web-port` always overrides. Hostname with no trailing number, or one
≥100, falls back to the bare band.

| Band | Service | Status |
|---|---|---|
| 8787 (fixed) | `status_server.py` | live — **stays fixed**, `main`'s `dashboard.html` defaults to it |
| 8100 + N | `video-fracture-led` | live |
| 8200 + N | `video-fracture` HDMI looper | not built |
| 8300 + N | `video-fracture-tft` | not built |
| 8400 + N | `media_matrix.py` | live on 8098, needs migrating |
| 8500 + N | `double_matrix.py` | live on 8099, needs migrating |

Resolves the old 8099 collision: `video-fracture-led` and `double_matrix`
are now in different bands.

## Page identity

`<title>` and `<h1>` lead with the **hostname**, not the service name —
several of these end up open in tabs at once and the hostname is the
distinguishing part. Service name goes after, small and dim.
`status_server.py` already does this; everything else should match.

## Conflicts

| Conflict | Detail | Decision needed |
|---|---|---|
| ~~Port 8099~~ | Resolved by the band scheme — `video-fracture-led` now 8100+N, `double_matrix` moves to 8500+N. | Done for video-fracture-led; double_matrix still to migrate. Live bookmarks change (`:8099` → `:8104` on fracture4). |
| Autoupdate behaviour | `media_matrix` pulls + restarts silently; `video-fracture-led` pulls + waits for human. | Pick one. Notify-only recommended for live installations. |
| Preset vs config-file | `media_matrix`/`double_matrix` use a single `state_file` + `/config.json` download. `video-fracture-led` uses autosaved current state + named presets dir. | Converge on the latter; keep `/config.json` download as export. |
| Password in git | `video-fracture-led` auth password is hardcoded (`conejo`) in the install script, not generated per-Pi. | Fine for private repo; revisit if repo is shared. |
| Backport vs divergence | Branches have unmerged, un-cross-pollinated work. Backporting `main`'s files may conflict with local edits. | Decide whether branches merge to `main` first, or files are copied over. |

## Future: Commander Lotte

Art installation command control. Separate session.

Goal: one UI for all installations, reachable without a Tailscale client.

Recommended: **hub-and-spoke**. Pis dial out to one hub (post status, poll for commands). Hub is the only public surface; serves the command-center UI.

- vs. Tailscale Funnel per Pi: one public surface instead of N.
- vs. port-forward/DDNS: no public IP, no inbound port, no router config — works on venue wifi.
- Per-Pi tokens → revoke individually.

Shape:
- Pi side: post `/status.json` verbatim on an interval + consume a command queue mapping to existing endpoints.
- Hub side: accept posts, hold last-known state per node, queue commands, serve UI.
- Additive — per-Pi pages unchanged, hub failure leaves direct tailnet control working.

The API already exists: `/status.json`, `/update`, `/save-config`, `/load-config`, `/configs.json`, `/restart`, `/reboot`, `/autoupdate`. Standardizing it across services (this doc) is the API work. Only the hub is new.
