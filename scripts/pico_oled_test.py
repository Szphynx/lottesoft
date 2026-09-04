# MicroPython, Raspberry Pi Pico 2 (RP2350).
# Two SH1106 OLEDs over software SPI: O1 cycles letters, O2 cycles numbers.
# Driver: https://github.com/robert-hh/SH1106 (copy sh1106.py onto the Pico
# alongside this file, or `mip.install("github:robert-hh/SH1106")`).
#
# Wiring (GPxx = Pico GPIO number, arbitrary free pins -- no fixed hardware
# SPI pin constraint since this is software/bit-banged SPI):
#
#            SCK    MOSI   CS     DC     RES
#   OLED-1   GP2    GP3    GP5    GP6    GP7
#   OLED-2   GP10   GP11   GP13   GP14   GP15
#
# VCC -> 3V3 (pin 36), GND -> any GND pin.

import framebuf
import time
from machine import Pin, SoftSPI

import sh1106

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
NUMBERS = [str(n) for n in range(10)]

SCREENS = {
    "O1": dict(sck=2, mosi=3, cs=5, dc=6, res=7, glyphs=LETTERS),
    "O2": dict(sck=10, mosi=11, cs=13, dc=14, res=15, glyphs=NUMBERS),
}
INTERVAL = 1.0
SCALE = 6  # 8px font * 6 = 48px tall glyph on a 64px-tall panel


def make_oled(cfg):
    spi = SoftSPI(baudrate=1_000_000, sck=Pin(cfg["sck"]), mosi=Pin(cfg["mosi"]), miso=Pin(28))
    return sh1106.SH1106_SPI(128, 64, spi, Pin(cfg["dc"]), Pin(cfg["res"]), Pin(cfg["cs"]))


def show_big_char(oled, ch, tag):
    """Scale the built-in 8x8 font up by SCALE, plus a small ID tag."""
    glyph_buf = bytearray(8)
    glyph = framebuf.FrameBuffer(glyph_buf, 8, 8, framebuf.MONO_VLSB)
    glyph.text(ch, 0, 0, 1)

    oled.fill(0)
    ox, oy = (128 - 8 * SCALE) // 2, (64 - 8 * SCALE) // 2
    for y in range(8):
        for x in range(8):
            if glyph.pixel(x, y):
                oled.fill_rect(ox + x * SCALE, oy + y * SCALE, SCALE, SCALE, 1)
    oled.text(tag, 2, 2, 1)
    oled.show()


led = Pin("LED", Pin.OUT)


def flash_missing_forever():
    """2 quick flashes + pause, repeated -- an OLED is missing/failed. Never returns."""
    while True:
        for _ in range(2):
            led.on()
            time.sleep(0.15)
            led.off()
            time.sleep(0.15)
        time.sleep(0.7)


oleds = {}
missing = []
for name, cfg in SCREENS.items():
    try:
        oleds[name] = make_oled(cfg)
    except Exception:
        missing.append(name)

if missing:
    flash_missing_forever()

led.on()  # steady on = both OLEDs up and running

i = 0
while True:
    try:
        for name, cfg in SCREENS.items():
            show_big_char(oleds[name], cfg["glyphs"][i % len(cfg["glyphs"])], name)
    except Exception:
        flash_missing_forever()
    i += 1
    time.sleep(INTERVAL)
