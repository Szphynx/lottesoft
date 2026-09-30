# Fan wall (Rotaryplay): Pi video/text player for the Teensy wall

## Quick start

1. **Flash the Teensy 4.1:** open `teensy_globalxy_udp_control.ino` in the Arduino IDE
   (Teensyduino + the FastLED library installed) and upload it. The stripe animation
   runs when it's alive.
2. **Cable it:** Pi `eth0` to the Teensy's Ethernet port. The Teensy is `192.168.60.50`.
3. **Install on the Pi:**
   ```bash
   git clone -b Rotaryplay https://github.com/Szphynx/lottesoft && cd lottesoft
   sudo bash fan_wall/install-fan-wall.sh
   ```
   This installs the packages, gives `eth0` the address `192.168.60.10`, and starts the
   `fan-wall` service. Check the link with `python3 fan_wall/blink_control.py`.
4. **Open `http://<pi-ip>:8099/`:** pick a calibration mode (`fans`, then `clusters`),
   match the wall to the preview, then upload a video or type text. Press Save config so
   it resumes after a reboot.

    Pi --eth0, UDP 5005--> Teensy --16 pins--> Corsair hubs --> 6 fans each

- `fan_wall.py` plays a video/image queue and scrolling text, and sends
  whole LED frames to the Teensy. The web control page is on :8099 and has
  a live preview of every fan's 16 LEDs.
- The wall layout is read from `teensy_globalxy_udp_control.ino` (XYTable
  and the k* constants), so the sketch stays the single source of truth.
- The calibration page has these parts:
  - **Fan pattern:** set once and every cluster follows it.
  - **Cluster grid:** where each pin's cluster sits on the wall.
  - **Grid size:** set with the Apply button.
  - **Calibration modes:** `fans`, `clusters` and `grid`.
- `install-fan-wall.sh`: packages, eth0 at 192.168.60.10, systemd
  service `fan-wall`.
- `python test_fan_wall.py`: checks that the default mapping equals the
  sketch's XY() for all 1536 LEDs, plus calibration and packet framing.
- `blink_control.py --test`: packet-loss check on the link.

Adapted from lottesoft `double_matrix.py` (branch
`claude/caveman-ultra-ponytail-vshfzk`).

Files here come from Jaakko-cyber/Teensy_RGB_videostream (the `.ino`,
`blink_control.py`, the wall wiring) plus the player. The `.ino` here has one
addition over the original: it accepts `'F'` frame packets. `fan_wall.py`
reads its wall layout from the `.ino` next to it, so keep them together.
