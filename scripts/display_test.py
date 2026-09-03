#!/usr/bin/env python3
"""
Cycle test patterns (letters -> numbers -> symbols) across up to three
displays: the RB-TFT3.2-V3 (via its kernel framebuffer, /dev/fb1) and two
Joy-IT SBC-OLED01.3 units.

The OLEDs are the 7-pin SPI variant (GND/VCC/CLK/MOSI/RES/DC/CS), not I2C.
They have no MISO line, so they can't sit on the TFT's hardware SPI0 bus
(that's real kernel-driven SPI hardware, and sharing SCLK/MOSI with a
software-driven device invites contention). Instead each OLED gets its own
five GPIO, bit-banged in software via luma.core's `bitbang` serial
interface -- no overlay, no /dev/spidev device, no config.txt changes
beyond whatever the TFT itself already needs.

Wiring (BCM GPIO numbers; physical header pin in parentheses):

               VCC      GND      CLK        MOSI       RES        DC         CS
    OLED #1    1        9        29 (GPIO5) 31 (GPIO6) 33 (GPIO13) 35 (GPIO19) 36 (GPIO16)
    OLED #2    17       14       37 (GPIO26) 38 (GPIO20) 40 (GPIO21) 15 (GPIO22) 32 (GPIO12)

OLED #2's CS originally sat on physical pin 16 (GPIO23). Joy-IT's own docs
for the RB-TFT3.2-V3 assign GPIO23/24/25 to its three onboard buttons and
GPIO18 to backlight -- GPIO23 collided directly with that CS line, so it
was moved to GPIO12 (physical pin 32), which sits outside the TFT's 26-pin
(13x2) header footprint entirely and is free either way. Every other OLED
GPIO above is untouched by the TFT.


Caveat: SPI is a write-only bus for these modules (no MISO), so an OLED
that isn't physically wired doesn't raise an error -- the script just
writes into nothing. Use --displays to tell it what's actually plugged in
rather than have it guess wrong.

Dependencies:
    pip install pillow numpy luma.oled RPi.GPIO

Run with (needs root for /dev/fb1 and GPIO):
    sudo python3 display_test.py
    sudo python3 display_test.py --displays tft,oled-1
    sudo python3 display_test.py --interval 1.5 --rotate 90
"""

import argparse
import glob
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OLED_PINS = {
    "OLED-1": dict(SCLK=5, SDA=6, CE=16, DC=19, RST=13),
    # CE moved off GPIO23 (physical pin 16) -- that's one of the TFT's
    # three onboard buttons, not free.
    "OLED-2": dict(SCLK=26, SDA=20, CE=12, DC=22, RST=21),
}
ALL_DISPLAYS = ["tft", "oled-1", "oled-2"]

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
# OLEDs: bit-banged SPI, one dedicated set of 5 GPIO per unit (see OLED_PINS
# above). No hardware /dev/spidev involved -- these modules have no MISO,
# so they can't use a normal spidev hardware-CS scheme.
# ----------------------------------------------------------------------------

class OLEDScreen:
    def __init__(self, pins, width=128, height=64):
        from luma.core.interface.serial import bitbang
        from luma.oled.device import sh1106

        serial = bitbang(**pins)
        self.device = sh1106(serial, width=width, height=height)
        self.size = (width, height)

    def show(self, img):
        self.device.display(img.convert("1").resize(self.size))


def find_oleds(requested):
    screens = []
    for name, pins in OLED_PINS.items():
        if name.lower() not in requested:
            continue
        try:
            oled = OLEDScreen(pins)
            gpio_summary = f"CLK={pins['SCLK']} MOSI={pins['SDA']} RES={pins['RST']} DC={pins['DC']} CS={pins['CE']}"
            print(f"{name}: bit-banged SPI on GPIO ({gpio_summary}) -- requested via --displays, "
                  f"presence can't be verified (write-only bus, no MISO)")
            screens.append((name, oled))
        except Exception as e:
            print(f"{name}: failed to initialize ({e}) -- skipping")
    return screens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds per glyph")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                     help="rotate the TFT image, e.g. if it's mounted sideways")
    ap.add_argument("--displays", default=",".join(ALL_DISPLAYS),
                     help="comma-separated list of what's actually wired right now: "
                          "tft,oled-1,oled-2 (default: all). The OLEDs are on a write-only "
                          "SPI bus with no MISO, so an unwired one can't be auto-detected -- "
                          "leave it out of this list rather than let the script guess wrong.")
    args = ap.parse_args()

    requested = {d.strip().lower() for d in args.displays.split(",") if d.strip()}
    unknown = requested - set(ALL_DISPLAYS)
    if unknown:
        sys.exit(f"unknown --displays entries: {', '.join(sorted(unknown))} (valid: {', '.join(ALL_DISPLAYS)})")

    screens = []
    if "tft" in requested:
        try:
            tft = TFTScreen(rotate=args.rotate)
            print(f"TFT detected: {tft.path} {tft.fb_w}x{tft.fb_h}")
            screens.append(("TFT", tft))
        except Exception as e:
            print(f"TFT not detected ({e}) -- skipping")

    screens.extend(find_oleds(requested))

    if not screens:
        sys.exit("no displays requested/detected -- check --displays and wiring")

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
