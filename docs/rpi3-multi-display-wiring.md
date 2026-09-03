# RPi3 multi-display wiring — status &amp; handoff

Branch: `claude/rpi3-multi-display-wiring-iyu759`
Goal: drive three displays off one Raspberry Pi 3B — an **RB-TFT3.2-V3** SPI
touchscreen and two **Joy-IT SBC-OLED01.3** I2C OLEDs — at the same time,
using only header risers and jumper wire the user already owns (no mux, no
new parts).

## Current state

- **TFT**: wired and expected to work standalone (full 40-pin HAT, stacked
  directly on the Pi's header, optionally via a riser for clearance). This
  is the only display physically connected so far. The user is mid
  reorganization of their cable/header access before wiring the OLEDs.
- **OLEDs**: not physically wired yet. Wiring plan below is designed but
  unverified on real hardware.
- **Test tool**: `scripts/display_test.py` is written and committed. Not yet
  run against real hardware (this session has no access to the device).
- **Reference diagram**: a wiring-reference page with the full 40-pin header
  diagram and signal-flow diagram was published as a Claude Artifact during
  this session at
  <https://claude.ai/code/artifact/5c6f6ffc-648b-47d7-9198-83059c9cba45>.
  It's private to the user's account — not fetchable by another agent
  without the user sharing it. Treat this doc as the source of truth
  instead; the artifact is a visual companion, not the primary record.

## The key insight

The TFT and the OLEDs never compete for pins — the TFT is SPI, the OLEDs
are I2C. The only real conflict: both OLED units are the same part at the
same fixed I2C address (`0x3C`), so they can't share one bus. Fixed by
giving OLED #2 a **second, software (bit-banged) I2C bus** on two spare
GPIO — no multiplexer, no address-jumper soldering, no new components.

## Pin plan

| Consumer | Signal | Physical pin | BCM GPIO |
|---|---|---|---|
| TFT (SPI0, shared) | MOSI | 19 | GPIO10 |
| TFT (SPI0, shared) | MISO | 21 | GPIO9 |
| TFT (SPI0, shared) | SCLK | 23 | GPIO11 |
| TFT | LCD CS (CE0) | 24 | GPIO8 |
| TFT | Touch CS (CE1) | 26 | GPIO7 |
| TFT | LCD DC | 18 | GPIO24 |
| TFT | LCD RST | 22 | GPIO25 |
| TFT | Touch IRQ | 11 | GPIO17 |
| TFT | Backlight | 12 | GPIO18 |
| OLED #1 | VCC | 1 | 3V3 |
| OLED #1 | GND | 9 | GND |
| OLED #1 | SDA (hardware I2C1) | 3 | GPIO2 |
| OLED #1 | SCL (hardware I2C1) | 5 | GPIO3 |
| OLED #2 | VCC | 17 | 3V3 |
| OLED #2 | GND | 14 | GND |
| OLED #2 | SDA (software I2C, `/dev/i2c-3`) | 16 | GPIO23 |
| OLED #2 | SCL (software I2C, `/dev/i2c-3`) | 15 | GPIO22 |

**Caveat (unverified, flagged to the user already):** "RB-TFT3.2-V3" is a
generic clone name; the DC/RST/IRQ/backlight GPIO assignments above are the
mapping used by most ILI9341+XPT2046 3.2" clone HATs, but they vary a
little by seller/batch. SPI0 itself (MOSI/MISO/SCLK/CE0/CE1) is fixed in
silicon and never varies. **Before wiring anything new, check the board's
own silkscreen or the install script it shipped with** rather than trusting
this table blindly for those four control pins.

## config.txt additions

```
dtparam=spi=on
dtparam=i2c_arm=on
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=22
```

Reboot after editing. This creates `/dev/i2c-3` (OLED #2) alongside the
Pi's normal `/dev/i2c-1` (OLED #1). The TFT itself is expected to come from
whatever fbtft driver/overlay/install-script shipped with the board
(commonly exposes `/dev/fb1`) — not covered by these three lines.

## Verifying on the bus

```
i2cdetect -y 1   # OLED #1 should show 0x3C
i2cdetect -y 3   # OLED #2 should show 0x3C, on the other bus
```

## Test tool: `scripts/display_test.py`

Cycles LETTERS → NUMBERS → SYMBOLS as one big glyph, synchronized across
whatever displays are actually detected. Each display is probed
independently at startup (TFT via framebuffer glob, each OLED via its own
I2C bus/address) and skipped with a printed reason if absent — the script
is meant to run correctly with just the TFT connected today, and pick up
each OLED automatically once it's wired, no code changes needed.

```
pip install pillow numpy luma.oled
sudo python3 scripts/display_test.py
sudo python3 scripts/display_test.py --interval 1.5 --rotate 90
```

Key implementation details for whoever picks this up:
- `find_tft_fb()` walks `/sys/class/graphics/fb*`, skips `fb0` (that's
  always the Pi's own HDMI/composite output), and takes the first other
  framebuffer it finds as the TFT. Assumes 16bpp RGB565, which is standard
  for fbtft-driven boards but should be confirmed against the real device.
- `TFTScreen.show()` writes raw RGB565 bytes via `os.pwrite` — no SDL, no
  X server required.
- `OLEDScreen` is a thin wrapper around `luma.oled.device.sh1106`; the two
  instances differ only in which `i2c(port=...)` they're constructed with
  (1 vs 3). Uses SH1106 at 128×64 — confirm this matches the actual
  SBC-OLED01.3 controller if display comes out corrupted/offset (Joy-IT
  has sold both SSD1306 and SH1106 variants under similar naming; wrong
  controller choice will show as extra column/row offset, not a crash).

## Suggested next steps

1. Physically wire OLED #1 to the hardware I2C bus, confirm with
   `i2cdetect -y 1`.
2. Add the `dtoverlay=i2c-gpio` line, reboot, wire OLED #2, confirm with
   `i2cdetect -y 3`.
3. Run `scripts/display_test.py` and visually confirm all three displays
   cycle in sync.
4. If the TFT's DC/RST/IRQ pins turn out to differ from the table above,
   update this doc and the vendor overlay accordingly — that mapping is
   the least certain part of this plan.
5. If the OLED controller isn't actually SH1106 (image looks shifted),
   swap `sh1106` for `ssd1306` in `scripts/display_test.py`.
