#!/usr/bin/env python3
"""
Cycle test patterns (letters -> numbers -> symbols) across up to three
displays: the RB-TFT3.2-V3 (via its kernel framebuffer, /dev/fb1) and two
Joy-IT SBC-OLED01.3 units. Both OLEDs are the same part at the same fixed
address (0x3C), so instead of a mux, OLED #2 lives on a second, software
("bit-banged") I2C bus on two spare GPIO pins -- no extra hardware needed.
Add this to config.txt and reboot:

    dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=22

That gives OLED #2 its own /dev/i2c-3, wired to pins 16 (SDA/GPIO23) and
15 (SCL/GPIO22) -- both free, neither claimed by the TFT HAT. OLED #1
stays on the Pi's normal hardware bus, /dev/i2c-1 (pins 3/5).

Each display is probed independently at startup. Any display that isn't
wired up yet, or doesn't answer, is skipped -- the rest keep cycling. This
is meant to be run as-is right after wiring one more screen on, with no
code changes needed to "turn off" the ones that aren't there yet.

Dependencies:
    pip install pillow numpy luma.oled

Run with (needs root for /dev/fb1 and /dev/i2c-*):
    sudo python3 display_test.py
    sudo python3 display_test.py --interval 1.5 --rotate 90
"""

import argparse
import glob
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OLED_ADDR = 0x3C
OLED_BUSES = {"OLED-1": 1, "OLED-2": 3}

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
# OLEDs: same part, same fixed address (0x3C) on both -- kept apart by
# living on two different I2C bus devices (/dev/i2c-1 hardware, /dev/i2c-3
# bit-banged) rather than by address, so there's no collision to arbitrate.
# ----------------------------------------------------------------------------

class OLEDScreen:
    def __init__(self, bus_port, width=128, height=64):
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106

        self.device = sh1106(i2c(port=bus_port, address=OLED_ADDR), width=width, height=height)
        self.size = (width, height)

    def show(self, img):
        self.device.display(img.convert("1").resize(self.size))


def find_oleds():
    screens = []
    for name, bus_port in OLED_BUSES.items():
        try:
            oled = OLEDScreen(bus_port)
            print(f"{name} detected on /dev/i2c-{bus_port}")
            screens.append((name, oled))
        except Exception as e:
            print(f"{name} not detected on /dev/i2c-{bus_port} ({e}) -- skipping")
    return screens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds per glyph")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                     help="rotate the TFT image, e.g. if it's mounted sideways")
    args = ap.parse_args()

    screens = []
    try:
        tft = TFTScreen(rotate=args.rotate)
        print(f"TFT detected: {tft.path} {tft.fb_w}x{tft.fb_h}")
        screens.append(("TFT", tft))
    except Exception as e:
        print(f"TFT not detected ({e}) -- skipping")

    screens.extend(find_oleds())

    if not screens:
        sys.exit("no displays detected at all -- check wiring and interfaces")

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
