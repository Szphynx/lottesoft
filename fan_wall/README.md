# Fan wall (Rotaryplay): Pi video/text player for the Teensy wall

    Pi --eth0, UDP 5005--> Teensy --16 pins--> Corsair hubs --> 6 fans each

- `fan_wall.py` plays a video/image queue and scrolling text, and sends
  whole LED frames to the Teensy. The web control page is on :8099 and has
  a live preview of every fan's 16 LEDs.
- The wall layout is read from `teensy_globalxy_udp_control.ino` (XYTable
  and the k* constants), so the sketch stays the single source of truth.
- The calibration page has four parts:
  - **Fan pattern:** set once and every cluster follows it.
  - **Cluster grid:** where each pin's cluster sits on the wall.
  - **Grid size:** set with the Apply button.
  - **Calibration modes:** `chain` and `grid`.
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
