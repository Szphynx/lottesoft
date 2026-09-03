#!/usr/bin/env python3
"""
Cycle test patterns (letters -> numbers -> symbols) across up to three
displays: the RB-TFT3.2-V3 (via its kernel framebuffer, /dev/fb1) and two
Joy-IT SBC-OLED01.3 units sitting behind a TCA9548A I2C mux on channels 0
and 1.

Each display is probed independently at startup. Any display that isn't
wired up yet, or doesn't answer, is skipped -- the rest keep cycling. This
is meant to be run as-is right after wiring one more screen on, with no
code changes needed to "turn off" the ones that aren't there yet.

Dependencies:
    pip install pillow numpy luma.oled smbus2

Run with (needs root for /dev/fb1 and /dev/i2c-1):
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

MUX_ADDR = 0x70
OLED_ADDR = 0x3C
OLED_CHANNELS = {"OLED-1": 0, "OLED-2": 1}

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
# OLEDs: both are the same part at the same fixed I2C address (0x3C), so
# each one only exists on the bus for as long as its mux channel is
# selected. Select-then-talk, every time -- the mux is a dumb hardware
# switch with no memory of who selected what.
# ----------------------------------------------------------------------------

class MuxedOLED:
    def __init__(self, bus, channel, width=128, height=64):
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106

        self.bus = bus
        self.channel = channel
        self._select()
        self.device = sh1106(i2c(port=1, address=OLED_ADDR), width=width, height=height)
        self.size = (width, height)

    def _select(self):
        self.bus.write_byte(MUX_ADDR, 1 << self.channel)

    def show(self, img):
        self._select()
        self.device.display(img.convert("1").resize(self.size))


def find_oleds():
    screens = []
    try:
        import smbus2
        bus = smbus2.SMBus(1)
    except Exception as e:
        print(f"I2C bus unavailable ({e}) -- both OLEDs skipped")
        return screens

    for name, channel in OLED_CHANNELS.items():
        try:
            oled = MuxedOLED(bus, channel)
            print(f"{name} detected on mux channel {channel}")
            screens.append((name, oled))
        except Exception as e:
            print(f"{name} not detected on mux channel {channel} ({e}) -- skipping")
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
