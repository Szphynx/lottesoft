# Rotaryplay: Pi video/text player for the 84-fan Teensy wall

    Raspberry Pi --eth0, UDP 5005--> Teensy 4.1 --16 data pins--> 16 Corsair RGB hubs --> 84 fans

The Pi renders an 84×56 RGB image and streams it. The Teensy maps pixels to LEDs.
`teensy.ino` is the source of truth; `fan_wall.py` reads the wall from it.

## Files

Everything for Rotaryplay lives in this `rotaryplay/` folder; nothing outside it is used.

| File | What it does |
|---|---|
| `teensy.ino` | Teensy 4.1 firmware. Receives row packets and maps pixels to LEDs (`XY()`, corner exemptions). Drives 16 pins through FastLED. Copied unchanged from Jaakko-cyber/84fan_teensy_udp. |
| `pi_streamer_spec.md` | Protocol and geometry spec from the same repo, unchanged. It mentions `rpi_video_streamer.py`, which is obsolete and not included; `fan_wall.py` replaces it. |
| `fan_wall.py` | Pi player plus web control page. Plays video/images, scrolling text, calibration, colour correction, and saved settings. Adapted from lottesoft `double_matrix.py`. |
| `install-fan-wall.sh` | One-shot Pi setup: apt packages, static `eth0`, and the systemd service `fan-wall` (enabled and started). |
| `test_fan_wall.py` | Self-check of `fan_wall.py` against `teensy.ino` and the spec. Prints `ok`. |

## Important data

**Network**

| | |
|---|---|
| Teensy | `192.168.60.50`, UDP port `5005` |
| Pi `eth0` | `192.168.60.20/24`, `ipv4.never-default yes` (otherwise it steals the Wi-Fi default route) |
| Cable | direct Pi-to-Teensy, no router or gateway |
| Web page | `http://<pi-ip>:8099/` on all interfaces, **no login** |

**Packet** (one per row, 56 per frame, exactly 255 bytes):

| Byte | Contents |
|---|---|
| 0 | `0xAA` (sync) |
| 1 | frame sequence number, 0-255, wraps |
| 2 | row, 0-55 |
| 3-254 | 84 × RGB |

- Every row of a frame carries the same sequence number.
- The Teensy shows a frame once all rows are in, when the next sequence number arrives, or after 120 ms.
- Packets of any other size are silently dropped.

**Geometry** (84×56 cells, origin top-left):

| | |
|---|---|
| Clusters | 4×4 clusters (hubs/pins) of 21×14 cells each |
| Fans per cluster | 3 across × 2 down, each in a 7×7 cell |
| LEDs per fan | 16: LEDs 0-3 inner, 4-15 outer ring |
| Corner clusters | pins 1 and 4 have no top fan row; pins 13 and 16 have no bottom fan row |
| Totals | 84 fans, **1344 live LEDs** (1536 buffered) |
| Dead zones | x 0-20 and x 63-83, at y 0-6 and y 49-55 |
| Probe | pixel (3,11) is LED 0, the first fan of hub 0 |

**Teensy output**

| | |
|---|---|
| Brightness | `FastLED.setBrightness(170)` |
| Power cap | `setMaxPowerInVoltsAndMilliamps(5, 45000)`, 45 A of a 60 A supply |
| Timing | needs FastLED **≥ 3.9.8**, which drives the 16 pins in parallel, about 3 ms per `show()` |
| Pin order | 1, 0, 24, 25, 19, 18, 14, 15, 17, 16, 22, 23, 20, 21, 26, 27 (pin N-1 drives hub N) |
| Serial | 115200 baud, prints `fps N torn M` every second. `torn` above 0 means rows are being lost. |

**Pi files** (created next to `fan_wall.py`):

| Path | Contents |
|---|---|
| `fan_wall_state.json` | Saved settings: calibration, colour, text, queue. Written by **Save settings** and loaded at startup. |
| `uploads/` | Uploaded media |
| `/etc/default/fan-wall` | Service flags (`FLAGS="..."`) |
| `/etc/systemd/system/fan-wall.service` | The service unit. It runs as root. |

## Install

1. **Teensy:** open `teensy.ino` in the Arduino IDE (Teensyduino and FastLED ≥ 3.9.8), then upload.
2. **Cable:** Pi `eth0` to the Teensy's Ethernet port.
3. **Pi** (Raspberry Pi OS / Debian with NetworkManager):
   ```bash
   git clone -b Rotaryplay https://github.com/Szphynx/lottesoft
   cd lottesoft
   sudo bash rotaryplay/install-fan-wall.sh
   ```
   Packages installed: `python3-opencv python3-numpy python3-pil fonts-dejavu-core fonts-vlgothic`.

## Run

- **As a service** (after install):
  ```bash
  sudo systemctl restart fan-wall
  journalctl -u fan-wall -f
  ```
  Change flags in `/etc/default/fan-wall`, then restart.
- **By hand:**
  ```bash
  python3 rotaryplay/fan_wall.py --media clip.mp4 --text "hello" --stats
  ```
- **Flags:**
  - `--sketch` (default `teensy.ino` next to the script)
  - `--teensy-ip`, `--teensy-port` (default: taken from the sketch)
  - `--media`, `--text`, `--text-height 14`, `--text-color`, `--font`, `--font-size`, `--bold`, `--italic`, `--scroll-speed 20`, `--text-direction`
  - `--fit fill|letterbox`, `--brightness 255`, `--fps 30`, `--web-port 8099` (0 disables the page)
  - `--upload-dir`, `--state-file` (`''` disables saving), `--transition-s 0.6`, `--stats`
- **Self-check** (needs numpy, opencv, pillow):
  ```bash
  cd rotaryplay
  python3 test_fan_wall.py
  ```
- **First run on the wall:** go to Calibration, then run in order: `red`, `green`, `blue`, `row0`, `col0`, `probe`. `row0` and `col0` catch a rotated or mirrored frame. Keep the Teensy serial `torn 0`.

## Features

- **Content:**
  - Video and image queue with upload, per-item start/end/loop/brightness/contrast, and crossfade.
  - Scrolling text: direction, stacked letters, glyph rotation, colour, bold/italic, trail, and font fallback for non-Latin characters.
  - Media brightness, contrast, rotation, scale, and position.
- **Live preview:** every live LED drawn as the wall shows it, with cluster outlines and numbers.
- **Calibration** (Calibration tab):
  - Starts at the Teensy's own mapping; nothing is remapped by default.
  - Fan pattern: 6 tiles, drag to swap, R rotates a fan 90°, M mirrors it. One pattern applies to every cluster.
  - Cluster grid: 16 tiles, drag to swap. Clicking one highlights that cluster.
  - Remapping is done on the Pi; the Teensy is never changed.
  - Modes:
    - `fans`: each cluster in its own colour, and the white LED count is the fan number (1-6).
    - `clusters`: the white LED count is the cluster number (1-16).
    - `grid`: a colour gradient with a dot on top of each fan.
    - The spec's test sequence: solid R/G/B, row 0, column 0, probe.
  - Wire modes (fans, clusters, and the tests) bypass the remap and show the raw wiring.
- **Colour tab:**
  - Correction on/off, with R/G/B levels from 0 to 200%. It applies to content only.
  - Softness: Gaussian blur before downsampling, where 100% is the spec's sigma of source width / 120.
- **Settings tab:**
  - Software brightness, active clusters (the first N pins), and a whole-wall 180° flip.
  - Save, download, or load settings, and restart the service.

## Missing features

- Web page has no login. Keep it on a trusted network.
- Grid size can't be changed live; it's fixed by `teensy.ino` and the packet layout.
- No per-cluster calibration exceptions. The fan pattern is shared, and in the 3-fan corner clusters a swap into an empty slot makes that fan go dark.
- Fans rotate only in 90° steps (the ring allows 30°).
- Colour correction is linear levels only: no gamma, no per-hue fix for pinks. No values are preset.
- Settings are saved only when the Save button is pressed; changes are lost on restart otherwise.
- Queue items can't be reordered in the UI. Removing an item leaves its file in `uploads/`.
- The Teensy's `fps`/`torn` stats aren't shown on the page; they're on USB serial only.
- Inputs are files only: no camera, HDMI, or network stream.
- Dropped from `double_matrix.py`: boot/shutdown animations, auto-update from git, and Tailscale setup.

## Untested

- `teensy.ino` hasn't been compiled or flashed by me. Nothing has run on a real Pi, Teensy, or wall.
- The Pi's frame rate is unmeasured. It was about 30 fps on a Windows PC with a 640×360 MJPG test clip. Video decoding plus blur on a Pi may be slower, so run with `--stats`.
- `install-fan-wall.sh` has never been run (apt, nmcli, systemd).
- The restart button (`sudo systemctl restart fan-wall`) is untested.
- Physical meaning of the calibration controls: whether R turns a fan the same way as the real fan, and what cluster/fan swaps look like on the wall.
- Default font `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` is unverified on the Pi; testing used Arial on Windows.
- Only an MJPG `.avi` clip was tested. Other formats depend on the Pi's OpenCV build.
- Large uploads (the limit is 200 MB) and long-running stability are untested.
- The page was checked only in a Chromium-based browser.
- `test_fan_wall.py` checks the Python copy of the Teensy's `XY()` against the spec, not against the running firmware.
