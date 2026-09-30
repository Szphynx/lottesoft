# Fan wall (Rotaryplay): Pi video/text player for the 84-fan Teensy wall

## Quick start

1. **Flash the Teensy 4.1:** open `teensy.ino` in the Arduino IDE (Teensyduino and
   FastLED 3.9.8 or newer installed) and upload it.
2. **Cable it:** connect the Pi's `eth0` to the Teensy's Ethernet port. The Teensy is at
   `192.168.60.50`.
3. **Install on the Pi:**
   ```bash
   git clone -b Rotaryplay https://github.com/Szphynx/lottesoft && cd lottesoft
   sudo bash fan_wall/install-fan-wall.sh
   ```
   This installs the packages, gives `eth0` the address `192.168.60.20` (without
   stealing the Wi-Fi route), and starts the `fan-wall` service.
4. **Open `http://<pi-ip>:8099/`:**
   - Run the tests under Calibration: solid colours, row 0, column 0, then the probe.
   - Tune the Colour tab.
   - Add a video or text under Content.
   - Press **Save settings** so it all comes back after a reboot.

## How it fits together

    Pi --eth0, UDP 5005--> Teensy (teensy.ino) --16 pins--> Corsair hubs --> fans

- **The Teensy owns LED addressing.** The Pi sends a plain 84×56 RGB image, one
  255-byte datagram per row (see `pi_streamer_spec.md`).
- **`fan_wall.py` reads the wall from `teensy.ino`:**
  - frame size, clusters, and fans
  - which cells are live (1344 LEDs, the corners are dead)
  - the IP address, port, and sync byte

  Change the sketch and restart, and the Pi follows.
- **Calibration** (just in case) starts at the Teensy's own mapping, so nothing is
  remapped until you change it.
  - If the wall disagrees with the preview, drag fan tiles (one pattern for every
    cluster) or cluster tiles. The Pi then shifts pixels so they land right, and the
    Teensy stays as it is.
  - The `fans` and `clusters` modes give each cluster its own colour. A run of white
    LEDs counts the fan's number or the cluster's number.
- **Colour tab:**
  - correction on/off, with R/G/B levels (green down, red up is the usual move)
  - softness, which blurs the video before it is downsampled, as the spec
    recommends
- **Save settings** stores everything in `fan_wall_state.json`, which is loaded at
  startup.
- **`python test_fan_wall.py`** checks the Pi against the sketch and the spec:
  - the 1344 live LEDs, dead corners, and corner probe
  - calibration starting as an identity (no remap)
  - the 255-byte row packets
  - saving and reloading settings

Adapted from lottesoft `double_matrix.py` (branch `claude/caveman-ultra-ponytail-vshfzk`).
`teensy.ino` and `pi_streamer_spec.md` are copied unchanged from
Jaakko-cyber/84fan_teensy_udp.
