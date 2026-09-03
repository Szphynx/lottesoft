# RPi3 multi-display wiring — status &amp; handoff

Branch: `claude/rpi3-multi-display-spi-ek6kyo`
Goal: drive three displays off one Raspberry Pi 3B — an **RB-TFT3.2-V3** SPI
touchscreen and two **Joy-IT SBC-OLED01.3** OLEDs — at the same time, using
only header risers and jumper wire the user already owns (no mux, no new
parts).

## Correction from an earlier pass

An earlier version of this plan (branch `claude/rpi3-multi-display-wiring-iyu759`)
assumed the OLEDs were the I2C variant and split them across two I2C buses
to dodge an address collision. The user's units are actually the **7-pin
SPI variant** — GND/VCC/CLK/MOSI/RES/DC/CS — so that whole plan doesn't
apply. This doc and `scripts/display_test.py` supersede it.

SPI has no shared-address problem to begin with: each OLED is told apart
by its own dedicated CS (chip-select) line, not a bus address. What it
costs instead is GPIO count — 5 dedicated pins per OLED (CLK, MOSI, RES,
DC, CS) instead of 2 (SDA, SCL), and none of the five can be shared.

## Why the OLEDs can't share the TFT's SPI0

The TFT already uses the Pi's hardware SPI0 (MOSI/MISO/SCLK/CE0/CE1, driven
by the kernel's spi-bcm2835 + fbtft). The OLED modules have no MISO line,
so they can't be added as extra hardware chip-selects on that same bus the
normal way — and running one device on real SPI hardware while
software-bit-banging another device on the *same* physical CLK/MOSI wires
is asking for contention. So each OLED instead gets a **fully separate**
set of 5 GPIO, bit-banged in software via `luma.core`'s `bitbang` serial
interface — no overlay, no `/dev/spidev` device, no config.txt change
beyond what the TFT already needs.

## The TFT is a real Joy-IT RB-TFT3.2-V3, not a generic clone

The board is a **13×2 (26-pin)** female-header HAT — physical pins 1–26
only, not the full 40. Seated directly on the Pi it blocks jumper access to
everything in that span, including the 3V3/GND taps the OLEDs need (pins 1,
9, 14, 17) and, as it turns out, one of OLED #2's own signal pins (see
below) — which is why this build runs off a GPIO ribbon breakout into a
breadboard instead: it mirrors all 40 pins 1:1 onto rows that stay reachable
with the HAT seated.

Joy-IT's own product docs for the RB-TFT3.2-V3 (SSD1289 display driver +
XPT2046 touch controller) confirm:
- **Backlight** — GPIO18 (physical pin 12)
- **Three onboard buttons** — GPIO23, GPIO24, GPIO25 (physical pins 16, 18, 22), active-low

That supersedes an earlier, wrong guess in this doc that treated GPIO24/25
as DC/RST for a generic ILI9341 clone — this display is SSD1289-based, and
those two pins are actually buttons. LCD DC/RST for the real SSD1289 panel
aren't confirmed from what's available here (see caveat below).

## Pin conflict found — and fixed

**GPIO23 (physical pin 16) was double-booked.** It's one of the TFT's three
buttons *and* it was OLED #2's CS line in the original plan. Since OLED #2
is already physically wired per that original plan, its **CS line moves to
GPIO12 (physical pin 32)** instead — that pin sits outside the TFT's 26-pin
footprint entirely, so it was never contested by anything. This is a
one-wire change on the breadboard (move the OLED #2 CS jumper from pin 16
to pin 32) and is already reflected in `scripts/display_test.py`.

If the TFT's buttons aren't going to be wired/used, GPIO23 sitting idle on
the TFT's silkscreen is harmless on its own — but any driver or overlay
that configures GPIO23 as a button input would still collide with OLED #2's
software-SPI code driving that same physical pin, so the CS move is the
safe fix either way, not just a precaution.

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
| TFT | LCD DC | — | not confirmed, see caveat |
| TFT | LCD RST | — | not confirmed, see caveat |
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
and the three buttons are confirmed from Joy-IT's own docs. **LCD DC/RST
are not confirmed** — this session couldn't reach Joy-IT's manual PDF or
any mirror of it to read the rest of the pin table (network egress to those
hosts was blocked). If the display came with its own install script,
check the `dtoverlay=flexfb,...` (or similar) line it wrote into
`config.txt` — the `dc-gpio=` and `reset-gpio=` (or `rst-gpio=`) parameters
on that line are the authoritative answer. Whatever they turn out to be,
they'll land on two of the still-free pins (GPIO2, GPIO3, GPIO4, GPIO14,
GPIO15, or GPIO27 — physical pins 3, 5, 7, 8, 10, 13) since nothing else in
this plan claims them.

## config.txt

```
dtparam=spi=on
```

That's it — `dtparam=spi=on` is what the TFT's hardware SPI0 needs (usually
already the default). The OLEDs need **no overlay and no config.txt change
at all**: they're driven entirely over plain GPIO in software, not through
a kernel SPI device.

## Test tool: `scripts/display_test.py`

Cycles LETTERS → NUMBERS → SYMBOLS as one big glyph, synchronized across
whatever displays are passed via `--displays`.

```
pip install pillow numpy luma.oled RPi.GPIO
sudo python3 scripts/display_test.py
sudo python3 scripts/display_test.py --displays tft,oled-1
sudo python3 scripts/display_test.py --interval 1.5 --rotate 90
```

**Regression versus the old I2C plan, worth knowing:** over I2C, writing to
an absent device raises an error the script could catch and report as "not
detected." SPI here is write-only (no MISO on these modules), so an OLED
that isn't physically wired doesn't raise anything — the script just writes
into nothing and looks like it's running fine. There is no way to
auto-detect OLED presence on this bus. Use `--displays` to say what's
actually plugged in; it defaults to all three, which is *not* safe to trust
blindly if fewer than three are wired.

The TFT is still auto-detected (via its framebuffer showing up under
`/sys/class/graphics/`), since that failure mode is a normal Python
exception either way.

Key implementation details for whoever picks this up:
- `find_tft_fb()` walks `/sys/class/graphics/fb*`, skips `fb0` (that's
  always the Pi's own HDMI/composite output), and takes the first other
  framebuffer it finds as the TFT. Assumes 16bpp RGB565, which is standard
  for fbtft-driven boards but should be confirmed against the real device.
- `TFTScreen.show()` writes raw RGB565 bytes via `os.pwrite` — no SDL, no
  X server required.
- `OLEDScreen` wraps `luma.oled.device.sh1106` on top of
  `luma.core.interface.serial.bitbang`, one instance per OLED, each
  constructed with its own SCLK/SDA/CE/DC/RST GPIO numbers from
  `OLED_PINS`. Uses SH1106 at 128×64 — confirm this matches the actual
  SBC-OLED01.3 controller if the display comes out corrupted/offset
  (Joy-IT has sold both SSD1306 and SH1106 variants under similar naming;
  wrong controller choice shows as extra column/row offset, not a crash).

## Status

Both OLEDs are physically wired per the pin plan above (OLED #2's CS on the
new pin 32/GPIO12, not the original pin 16). The TFT's 26-pin footprint
blocks direct jumper access to pins 1–26 wherever it's seated, so this
build runs everything through a GPIO ribbon breakout to a breadboard
instead.

## Suggested next steps

1. Run `sudo python3 scripts/display_test.py --displays oled-1,oled-2` and
   confirm both OLEDs draw correctly off their current wiring, including
   OLED #2 on its moved CS pin.
2. Confirm the TFT's actual DC/RST GPIO (install script's `dtoverlay=`
   line, or the manual) and fill them into the pin plan above.
3. Run `sudo python3 scripts/display_test.py --displays tft,oled-1,oled-2`
   and confirm all three cycle in sync.
4. If the OLED controller isn't actually SH1106 (image looks shifted),
   swap `sh1106` for `ssd1306` in `scripts/display_test.py`.
