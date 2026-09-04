# RPi3 multi-display wiring — status &amp; handoff

Branch: `claude/rpi3-multi-display-wiring-iyu759`
Goal: drive three displays off one Raspberry Pi 3B — an **RB-TFT3.2-V3**
touchscreen and two **Joy-IT SBC-OLED01.3** OLEDs — at the same time, using
only header risers and jumper wire the user already owns (no mux, no new
parts).

## Heads up: a parallel branch exists for this same task

`origin/claude/rpi3-multi-display-spi-ek6kyo` tackled this same feature in a
separate session and got further: it independently found the same OLED
wiring correction below, then also confirmed real hardware facts about the
TFT (Joy-IT's own product docs) and pushed a fix for a genuine pin conflict
this branch's original plan had (see "Pin correction" below). That branch
also carries extra display-content features (dedicated content per screen,
crossfade transitions) this branch doesn't have and was never asked for
here. If you're picking this repo up cold: check whether the user wants to
consolidate onto one branch before doing more work on both.

## Corrected from an earlier pass

An earlier version of this plan assumed the OLEDs were the I2C variant and
split them across two I2C buses to dodge an address collision. **The
user's units are actually the 7-pin SPI variant** — GND/VCC/CLK/MOSI/RES/
DC/CS — so that whole I2C plan doesn't apply. This revision supersedes it.

SPI has no shared-address problem to begin with: each OLED is told apart
by its own dedicated CS (chip-select) line, not a bus address. What it
costs instead is GPIO count — 5 dedicated pins per OLED (CLK, MOSI, RES,
DC, CS) instead of 2 (SDA, SCL), none of which can be shared.

## Why the OLEDs can't share the TFT's SPI0

The TFT already uses the Pi's hardware SPI0 (MOSI/MISO/SCLK/CE0/CE1, driven
by the kernel's spi-bcm2835 + fbtft). The OLED modules have no MISO line,
so they can't be added as extra hardware chip-selects on that bus the
normal way — and running one device on real SPI hardware while
software-bit-banging another on the *same* physical CLK/MOSI wires invites
contention. So each OLED gets a fully separate set of 5 GPIO, bit-banged in
software via `luma.core`'s `bitbang` serial interface — no overlay, no
`/dev/spidev` device, no config.txt change beyond what the TFT itself needs.

## Pin correction: OLED #2's CS conflicted with a real TFT button

The sibling branch confirmed (Joy-IT's own docs) that the RB-TFT3.2-V3 is a
**26-pin (13×2)** header board — not the full 40 — with an SSD1289 LCD
driver + XPT2046 touch, backlight on **GPIO18 (pin 12)**, and **three
onboard buttons on GPIO23, GPIO24, GPIO25 (pins 16, 18, 22)**. That
supersedes an earlier guess in this doc's history that treated GPIO24/25 as
DC/RST for a generic ILI9341 clone.

**GPIO23 (pin 16)** was double-booked in the original plan below — it's a
TFT button *and* it was OLED #2's CS line. **OLED #2's CS now lives on
GPIO12 (pin 32)** instead, which sits outside the TFT's 26-pin footprint
entirely and was never contested by anything. Already reflected in
`scripts/display_test.py`.

LCD DC/RST for the TFT's actual SSD1289 panel are **not confirmed** — if it
came with an install script, its `dtoverlay=flexfb,...` (or similar)
line's `dc-gpio=`/`reset-gpio=` params are the authoritative answer.

## Current state

- **TFT**: wired and expected to work standalone. This is the only display
  physically connected in this branch's context so far.
- **OLEDs**: wiring plan below is corrected but this session has no direct
  hardware access to confirm it (see the sibling branch, which reports both
  OLEDs physically wired against this same corrected plan).
- **Test tool**: `scripts/display_test.py` is written and committed here,
  with the corrected pin plan. Not run against real hardware from this
  session.

## Pin plan

| Consumer | Signal | Physical pin | BCM GPIO |
|---|---|---|---|
| TFT (SPI0, shared) | MOSI | 19 | GPIO10 |
| TFT (SPI0, shared) | MISO | 21 | GPIO9 |
| TFT (SPI0, shared) | SCLK | 23 | GPIO11 |
| TFT | LCD CS (CE0) | 24 | GPIO8 |
| TFT | Touch CS (CE1) | 26 | GPIO7 |
| TFT | Backlight | 12 | GPIO18 |
| TFT | Button 1 | 16 | GPIO23 |
| TFT | Button 2 | 18 | GPIO24 |
| TFT | Button 3 | 22 | GPIO25 |
| TFT | LCD DC | — | not confirmed, see above |
| TFT | LCD RST | — | not confirmed, see above |
| OLED #1 | VCC | 1 | 3V3 |
| OLED #1 | GND | 9 | GND |
| OLED #1 | CLK | 29 | GPIO5 |
| OLED #1 | MOSI | 31 | GPIO6 |
| OLED #1 | RES | 33 | GPIO13 |
| OLED #1 | DC | 35 | GPIO19 |
| OLED #1 | CS | 36 | GPIO16 |
| OLED #2 | VCC | 17 | 3V3 |
| OLED #2 | GND | 14 | GND |
| OLED #2 | CLK | 37 | GPIO26 |
| OLED #2 | MOSI | 38 | GPIO20 |
| OLED #2 | RES | 40 | GPIO21 |
| OLED #2 | DC | 15 | GPIO22 |
| OLED #2 | CS | 32 | GPIO12 |

**Caveat:** SPI0 itself (MOSI/MISO/SCLK/CE0/CE1) is fixed in silicon and
never varies — those five rows are as solid as this table gets. Backlight
and the three buttons are confirmed from Joy-IT's own docs. LCD DC/RST are
the one open item — see above.

## config.txt

```
dtparam=spi=on
```

That's it. The OLEDs need **no overlay and no config.txt change at all** —
they're driven entirely over plain GPIO in software, not through a kernel
SPI device.

## Test tool: `scripts/display_test.py`

Cycles LETTERS → NUMBERS → SYMBOLS as one big glyph, synchronized across
whatever displays are passed via `--displays`.

```
pip install pillow numpy luma.oled RPi.GPIO
sudo python3 scripts/display_test.py
sudo python3 scripts/display_test.py --displays tft,oled-1
sudo python3 scripts/display_test.py --interval 1.5 --rotate 90
```

**Detection caveat:** over I2C, writing to an absent device raises an error
the script could catch and report as "not detected." SPI here is
write-only (no MISO on these modules), so an OLED that isn't physically
wired doesn't raise anything — the script just writes into nothing and
looks like it's running fine. There is no way to auto-detect OLED presence
on this bus. Use `--displays` to say what's actually plugged in; it
defaults to all three, which is *not* safe to trust blindly if fewer than
three are wired. The TFT is still auto-detected (via its framebuffer
showing up under `/sys/class/graphics/`), since that failure mode is a
normal Python exception either way.

Key implementation details for whoever picks this up:
- `find_tft_fb()` walks `/sys/class/graphics/fb*`, skips `fb0` (that's
  always the Pi's own HDMI/composite output), and takes the first other
  framebuffer it finds as the TFT. Assumes 16bpp RGB565, standard for
  fbtft-driven boards but worth confirming against the real device.
- `TFTScreen.show()` writes raw RGB565 bytes via `os.pwrite` — no SDL, no
  X server required.
- `OLEDScreen` wraps `luma.oled.device.sh1106` on top of
  `luma.core.interface.serial.bitbang`, one instance per OLED, each
  constructed from its own entry in `OLED_PINS`. Uses SH1106 at 128×64 —
  confirm this matches the actual SBC-OLED01.3 controller if the display
  comes out corrupted/offset (Joy-IT has sold both SSD1306 and SH1106
  variants under similar naming; wrong controller choice shows as extra
  column/row offset, not a crash).
- The `bitbang` interface's kwarg names (`SCLK`/`SDA`/`CE`/`DC`/`RST`)
  follow luma.core's documented examples but aren't verified against a
  real install from this session — check them against your installed
  `luma.core` version if construction raises a `TypeError`.

## Suggested next steps

1. If consolidating with the sibling branch (`claude/rpi3-multi-display-spi-ek6kyo`),
   decide that first — it's ahead on hardware verification and has extra
   features this branch doesn't.
2. Run `sudo python3 scripts/display_test.py --displays oled-1,oled-2` and
   confirm both OLEDs draw correctly, including OLED #2 on its corrected
   CS pin (GPIO12, not GPIO23).
3. Confirm the TFT's actual DC/RST GPIO (install script's `dtoverlay=` line,
   or the Joy-IT manual) and fill them into the pin plan above.
4. Run with all three displays and confirm they cycle in sync.
5. If the OLED controller isn't actually SH1106 (image looks shifted), swap
   `sh1106` for `ssd1306` in `scripts/display_test.py`.
