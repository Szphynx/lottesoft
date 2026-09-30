# Rotaryplay: Pi video/text player for the 84-fan Teensy wall

    Raspberry Pi --eth0, UDP 5005--> Teensy 4.1 --16 data pins--> 16 Corsair RGB hubs --> 84 fans

The Pi renders an 84×56 RGB image and streams it. The Teensy maps pixels to LEDs.
`teensy.ino` is the source of truth; `fan_wall.py` reads the wall from it.

## Files

Everything for Rotaryplay lives in this `rotaryplay/` folder; nothing outside it is used.

| File | What it does |
|---|---|
| `install.sh` | One-shot Pi setup: packages and fonts, static `eth0`, optional Tailscale, and the systemd service `fan-wall` (enabled and started). |
| `run.sh` | Starts the player with `rotaryplay.conf` applied. Extra arguments are passed through as flags. The service runs this script. |
| `rotaryplay.conf` | Startup config: Teensy IP/port/serial, Pi interface and IP, web port, file locations, and `EXTRA_FLAGS`. |
| `fan_wall.py` | Pi player and web control page. |
| `teensy.ino` | Teensy 4.1 firmware, copied unchanged from Jaakko-cyber/84fan_teensy_udp. |
| `pi_streamer_spec.md` | Protocol and geometry spec from the same repo, unchanged. It mentions `rpi_video_streamer.py`, which is obsolete and replaced by `fan_wall.py`. |
| `test_fan_wall.py` | Self-check against `teensy.ino`, the spec, formats and flags. Prints `ok`. |

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
| Serial | USB, 115200 baud, prints `fps N torn M` every second. `torn` above 0 means rows are being lost. |

**Pi files** (in `rotaryplay/` unless moved in the conf):

| Path | Contents |
|---|---|
| `fan_wall_state.json` | Saved settings: every tab, plus the queue. Written by **Save settings** and loaded at startup. |
| `uploads/` | Uploaded media |
| `/etc/systemd/system/fan-wall.service` | Service unit. Runs `run.sh` as root. |

## Install

1. **Teensy:** open `teensy.ino` in the Arduino IDE (Teensyduino and FastLED ≥ 3.9.8), then upload.
2. **Cable:** Pi `eth0` to the Teensy's Ethernet port. Optionally, also plug the Teensy's USB into the Pi so the Stats tab can read it.
3. **Pi** (Raspberry Pi OS / Debian with NetworkManager):
   ```bash
   git clone -b Rotaryplay https://github.com/Szphynx/lottesoft
   cd lottesoft
   sudo bash rotaryplay/install.sh
   ```
   - Different interface or address? Edit `ETH_IF` / `PI_IP` in `rotaryplay.conf` first.
   - Remote access: export `TS_AUTHKEY` and run `sudo -E bash rotaryplay/install.sh`. It installs Tailscale and joins as `rotaryplay` (override with `TS_HOSTNAME`). Keep the key out of the repo.
   - Safe to run again.

## Run

- **Service:**
  ```bash
  sudo systemctl restart fan-wall
  journalctl -u fan-wall -f
  sudo systemctl stop fan-wall
  ```
  Restart after editing `rotaryplay.conf`. Stopping plays the close sequence if it's on.
- **By hand:**
  ```bash
  ./rotaryplay/run.sh
  ```
  - `./rotaryplay/run.sh --calibrate fans --brightness 120` adds or overrides flags.
  - `./rotaryplay/run.sh --help` lists every flag.
  - Set `PYTHON=/path/to/python` to use a venv.
- **Self-check** (needs numpy, opencv, pillow):
  ```bash
  cd rotaryplay
  python3 test_fan_wall.py
  ```
- **First run on the wall:** go to Calibration, then run `red`, `green`, `blue`, `row0`, `col0`, `probe`. `row0` and `col0` catch a rotated or mirrored frame. Keep the Teensy's `torn` at 0 (Stats tab).

### Settings, flags and the conf

- **Every setting on the page's tabs is also a flag**, generated from the same code, so they can't drift. Some examples:
  - `--brightness 120`
  - `--cc-on --cc-g 70`
  - `--calibrate fans`
  - `--seq-start-on --seq-rpm 600`
  - `--stats-on`
  - `--text "hello"`
  - `--fan-order '[0,1,2,3,4,5]'`

  Booleans also have `--no-...` forms.
- **Precedence:** built-in defaults < saved settings (`fan_wall_state.json`) < flags given at start (conf `EXTRA_FLAGS`, `run.sh` arguments) < live edits on the page.
  - Note: a flag in `EXTRA_FLAGS` re-applies at every start, even over a value saved later from the page.
- **Startup-only values** (network, files, ports, fps) live in `rotaryplay.conf` and need a restart. The Settings tab shows the values currently in use.

## Features

- **Content tab:**
  - Video and image queue: upload, per-item start/end/loop/brightness/contrast, and crossfade.
  - Scrolling text: direction, stacked letters, glyph rotation, colour, bold/italic, trail.
  - Media brightness, contrast, rotation, scale, and position.
- **Video formats:** `.mp4 .m4v .mov .mkv .webm .avi .gif .mpg .mpeg .ts .mts .m2ts .wmv .flv .3gp .ogv .mxf`.
  - Codec support is whatever the Pi's FFmpeg decodes: H.264, HEVC, VP8/9, MPEG-2/4, MJPEG and more.
  - Every upload is test-decoded first; one the Pi can't decode is refused with the reason.
  - Each video plays at its own frame rate (24, 25, 60 fps...), holding or skipping frames on the 30 fps output.
  - A file that breaks later is skipped instead of stalling the queue.
- **Image formats:** `.jpg .jpeg .png .bmp .webp .tif .tiff`. Phone photos are auto-rotated from their EXIF tag.
- **Fonts:** DejaVu Sans (regular/bold/italic). Characters it lacks (Japanese, CJK, kana) fall back per character to VL Gothic, then Noto CJK. If no font is found at all, Pillow's built-in font is used instead of crashing.
- **Live preview:** every LED drawn as the wall shows it, with cluster outlines and numbers.
- **Calibration tab:**
  - Starts at the Teensy's own mapping; nothing is remapped by default.
  - Fan pattern: 6 tiles, drag to swap, R rotates a fan 90°, M mirrors it. One pattern applies to every cluster.
  - Cluster grid: 16 tiles, drag to swap. Clicking one highlights that cluster.
  - Remapping is done on the Pi; the Teensy is never changed.
  - Modes:
    - `fans`: each cluster in its own colour, and the white LED count is the fan number (1-6).
    - `clusters`: the white LED count is the cluster number (1-16).
    - `grid`: a colour gradient with a dot on top of each fan.
    - The spec's test sequence: solid R/G/B, row 0, column 0, probe.
- **Colour tab:**
  - Correction on/off, with R/G/B levels from 0 to 200%. It applies to content only.
  - Softness: blur before downsampling, where 100% is the spec's sigma of source width / 120.
- **Startup / Close tab:**
  - Fan "rev": a light runs round every ring, speeding up at startup and spinning down on stop.
  - Each is switched on separately. Length, top rpm and colour are adjustable, and there are test buttons.
- **Stats tab:**
  - Off by default. A warning banner shows while stats are on.
  - Shows Pi render fps, per-frame work time (average and maximum), send errors, CPU temperature and load, and the Teensy's own `fps`/`torn` line read over USB serial.
  - Cost while on: counters in the render loop, one poll a second, a serial read thread that blocks when idle, and one log line a second.
- **Settings tab:**
  - Software brightness, active clusters (the first N pins), and a whole-wall 180° flip.
  - Save, download, or load settings, and restart the service.
  - Shows the startup config in use.

## Missing features

- Web page has no login. Keep it on a trusted network or Tailscale.
- Grid size can't be changed live; it's fixed by `teensy.ino` and the packet layout.
- No per-cluster calibration exceptions. The fan pattern is shared, and in the 3-fan corner clusters a swap into an empty slot makes that fan go dark.
- Fans rotate only in 90° steps (the ring allows 30°).
- Colour correction is linear levels only: no gamma, no per-hue fix for pinks. No values are preset.
- Settings are saved only when the Save button is pressed.
- Startup-only values (IPs, ports, files) are edited in `rotaryplay.conf`, not on the page.
- No font picker on the page; the font is set with `--font` / `FONT=`.
- Queue items can't be reordered in the UI. Removing an item leaves its file in `uploads/`.
- Inputs are files only: no camera, HDMI, or network stream.
- The close sequence doesn't run on a power cut or `kill -9`.
- Dropped from `double_matrix.py`: auto-update from git.

## Untested

- `teensy.ino` hasn't been compiled or flashed by me. Nothing has run on a real Pi, Teensy, or wall.
- `install.sh` has never run (apt, nmcli, Tailscale, systemd). `run.sh` was only run under Git Bash on Windows.
- The Pi's frame rate is unmeasured; a Windows PC does about 29 fps with about 1.5 ms of work per frame. Big or HEVC/4K files may be too slow to decode on a Pi, so check the Stats tab.
- Formats actually decoded in tests: mp4, mov, m4v, mkv, webm, avi (MJPG, XVID), gif, png, jpg, bmp, webp, tiff. Others depend on the Pi's FFmpeg build.
- Reading the Teensy's USB serial (`/dev/ttyACM0`) is untested.
- The restart button (`sudo systemctl restart fan-wall`) is untested.
- Physical meaning of the calibration controls: whether R turns a fan the same way as the real fan, and what cluster/fan swaps look like on the wall.
- What the rev animation looks like on real fans, and its direction, which follows the wiring order.
- Fonts were tested with Pillow's fallback on Windows; the DejaVu, VL Gothic and Noto CJK paths are unverified on the Pi.
- Large uploads (the limit is 200 MB) and long-running stability are untested.
- The page was checked only in a Chromium-based browser.
- `test_fan_wall.py` checks the Python copy of the Teensy's `XY()` against the spec, not against the running firmware.
