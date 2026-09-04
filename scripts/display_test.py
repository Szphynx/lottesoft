#!/usr/bin/env python3
"""
Cycle test patterns (letters -> numbers -> symbols) across up to three
displays: the RB-TFT3.2-V3 (via its kernel framebuffer, /dev/fb1) and two
Joy-IT SBC-OLED01.3 units.

CORRECTED from an earlier version of this file: the OLEDs turned out to be
the 7-pin SPI variant (GND/VCC/CLK/MOSI/RES/DC/CS), not I2C -- there's no
address to collide on, but each display needs 5 dedicated GPIO of its own.
They're bit-banged in software (via luma.core's `bitbang` serial
interface, since these modules have no MISO and so no real spidev bus).

The TFT is a real Joy-IT RB-TFT3.2-V3: a 26-pin (13x2) header board, SSD1289
LCD driver + XPT2046 touch, with backlight on GPIO18 (pin 12) and three
onboard buttons on GPIO23/24/25 (pins 16/18/22) per Joy-IT's own docs.
OLED #2's CS line originally landed on GPIO23 (pin 16) -- that's one of the
TFT's buttons, not a free pin -- so it now lives on GPIO12 (pin 32)
instead, outside the TFT's 26-pin footprint entirely.

    OLED-1: SCLK=GPIO5 (pin29)  SDA=GPIO6 (pin31)  RST=GPIO13 (pin33)
            DC=GPIO19 (pin35)   CE=GPIO16 (pin36)
    OLED-2: SCLK=GPIO26 (pin37) SDA=GPIO20 (pin38)  RST=GPIO21 (pin40)
            DC=GPIO22 (pin15)   CE=GPIO12 (pin32)   -- moved off GPIO23/pin16

LCD DC/RST for the TFT's own SSD1289 panel aren't confirmed here -- if the
board came with an install script, its `dtoverlay=flexfb,...` (or similar)
line's `dc-gpio=`/`reset-gpio=` params are the authoritative answer.

No config.txt changes needed for the OLEDs beyond what the TFT already
requires -- this is plain GPIO, not a kernel SPI device.

The TFT is auto-detected (absent framebuffer -> skipped cleanly). The
OLEDs can't be: bit-banged SPI has no read-back, so writing to one that
isn't actually wired succeeds without error instead of raising one. Use
--displays to say what's really connected rather than relying on
autodetection for those two.

Dependencies:
    pip install pillow numpy luma.oled RPi.GPIO

Run with (needs root for /dev/fb1 and GPIO):
    sudo python3 display_test.py
    sudo python3 display_test.py --displays tft,oled-1 --interval 1.5
"""

import argparse
import glob
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ALL_NAMES = ["TFT", "OLED-1", "OLED-2"]
OLED_PINS = {
    "OLED-1": dict(SCLK=5, SDA=6, RST=13, DC=19, CE=16),
    # CE moved off GPIO23 (pin16) -- that's one of the TFT's onboard buttons.
    "OLED-2": dict(SCLK=26, SDA=20, RST=21, DC=22, CE=12),
}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
NUMBERS = [str(n) for n in range(10)]
SYMBOLS = list("☺☹★♥☀☁☂✓⚡♪")  # ☺☹★♥☀☁☂✓⚡♪
CATEGORIES = [("L", LETTERS), ("N", NUMBERS), ("S", SYMBOLS)]


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def build_frame(size, glyph, tag):
    """One big centered glyph plus a small category tag in the corner."""
    w, h = size
    img = Image.new("RGB", size, "black")
    draw = ImageDraw.Draw(img)

    font = load_font(int(min(w, h) * 0.7))
    x0, y0, x1, y1 = draw.textbbox((0, 0), glyph, font=font)
    draw.text(((w - (x1 - x0)) / 2 - x0, (h - (y1 - y0)) / 2 - y0), glyph, font=font, fill="white")
    draw.text((4, 2), tag, font=ImageFont.load_default(), fill="white")
    return img


# ----------------------------------------------------------------------------
# TFT: written directly as raw RGB565 to whatever framebuffer the vendor's
# fbtft driver/overlay created (typically /dev/fb1 -- fb0 is the Pi's own
# HDMI/composite output and is never the add-on panel).
# ----------------------------------------------------------------------------

def find_tft_fb():
    for path in sorted(glob.glob("/sys/class/graphics/fb*")):
        name = os.path.basename(path)
        if name == "fb0":
            continue
        try:
            w, h = (int(v) for v in open(f"{path}/virtual_size").read().strip().split(","))
            bpp = int(open(f"{path}/bits_per_pixel").read().strip())
        except OSError:
            continue
        return f"/dev/{name}", w, h, bpp
    return None


class TFTScreen:
    def __init__(self, rotate=0):
        found = find_tft_fb()
        if not found:
            raise RuntimeError("no secondary framebuffer found")
        self.path, self.fb_w, self.fb_h, bpp = found
        if bpp != 16:
            raise RuntimeError(f"{self.path} is {bpp}bpp, expected 16bpp RGB565")
        self.rotate = rotate
        # logical canvas size handed to callers, before rotation is applied
        self.size = (self.fb_h, self.fb_w) if rotate in (90, 270) else (self.fb_w, self.fb_h)
        self.fd = os.open(self.path, os.O_WRONLY)

    def show(self, img):
        if self.rotate:
            img = img.rotate(-self.rotate, expand=True)
        if img.size != (self.fb_w, self.fb_h):
            img = img.resize((self.fb_w, self.fb_h))
        arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
        rgb565 = (((arr[:, :, 0] >> 3) << 11) | ((arr[:, :, 1] >> 2) << 5) | (arr[:, :, 2] >> 3))
        os.pwrite(self.fd, rgb565.astype("<u2").tobytes(), 0)


# ----------------------------------------------------------------------------
# OLEDs: SPI, no MISO, each on its own 5 dedicated GPIO -- bit-banged via
# luma.core's `bitbang` serial interface, fully separate from the TFT's
# real (kernel-driven) SPI0 bus and from each other. Nothing shared, so
# there's nothing to arbitrate -- but also no read-back, see the module
# docstring's detection caveat.
# ----------------------------------------------------------------------------

class OLEDScreen:
    def __init__(self, pins, width=128, height=64):
        from luma.core.interface.serial import bitbang
        from luma.oled.device import sh1106

        # NB: kwarg names (SCLK/SDA/CE/DC/RST) follow luma.core's documented
        # bitbang examples; double-check against your installed luma.core
        # version if this raises a TypeError at construction.
        self.device = sh1106(bitbang(**pins), width=width, height=height)
        self.size = (width, height)

    def show(self, img):
        self.device.display(img.convert("1").resize(self.size))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds per glyph")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                     help="rotate the TFT image, e.g. if it's mounted sideways")
    ap.add_argument("--displays", default=None,
                     help="comma-separated subset to attempt, e.g. 'tft,oled-1' "
                          "(default: all three). The OLEDs can't be auto-detected "
                          "as absent (see module docstring) -- list only what's "
                          "actually wired if fewer than three are connected.")
    args = ap.parse_args()

    requested = None
    if args.displays:
        requested = {d.strip().upper() for d in args.displays.split(",")}
        unknown = requested - set(ALL_NAMES)
        if unknown:
            sys.exit(f"unknown display name(s): {', '.join(sorted(unknown))} "
                      f"(choose from {', '.join(ALL_NAMES)})")

    def wanted(name):
        return requested is None or name in requested

    screens = []
    if wanted("TFT"):
        try:
            tft = TFTScreen(rotate=args.rotate)
            print(f"TFT detected: {tft.path} {tft.fb_w}x{tft.fb_h}")
            screens.append(("TFT", tft))
        except Exception as e:
            print(f"TFT not detected ({e}) -- skipping")

    for name, pins in OLED_PINS.items():
        if not wanted(name):
            continue
        try:
            oled = OLEDScreen(pins)
            print(f"{name} initialized on GPIO {pins}")
            screens.append((name, oled))
        except Exception as e:
            print(f"{name} failed to initialize ({e}) -- skipping")

    if not screens:
        sys.exit("no displays detected at all -- check wiring, interfaces, and --displays")

    sequence = [(tag, glyph) for tag, glyphs in CATEGORIES for glyph in glyphs]
    print(f"running with {len(screens)} display(s): " + ", ".join(n for n, _ in screens))

    try:
        i = 0
        while True:
            tag, glyph = sequence[i % len(sequence)]
            for name, screen in screens:
                try:
                    screen.show(build_frame(screen.size, glyph, tag))
                except Exception as e:
                    print(f"{name}: write failed ({e})")
            i += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
