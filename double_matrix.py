#!/usr/bin/env python3
"""
Video + scrolling text on six daisy-chained MAX7219 8x32 dot-matrix modules
(Youmile TS-YM-322, 4 cascaded MAX7219 chips per module), on a Raspberry Pi 3.

These are monochrome, SPI-chained modules (VCC/GND/DIN/CS/CLK) -- not the
WS2812B panels media_matrix.py drives, and not the HUB75 panel
thermal_matrix.py drives. Different Pi, different wiring, different
library. Do not touch those two files for this project.

Wiring (see the wiring diagram from this project's planning session):
    Pi GPIO10/MOSI (physical pin 19)  -> module 1 DIN
    Pi GPIO11/SCLK (physical pin 23)  -> CLK, bussed to all 6 modules
    Pi GPIO8/CE0   (physical pin 24)  -> CS,  bussed to all 6 modules
    module N DOUT -> module N+1 DIN, daisy-chained, module 6 DOUT unused.

    Power: external 5V supply feeds every module's VCC/GND rail directly,
    not through the Pi. Tie the supply's GND, the Pi's GND (physical pin
    6), and every module's GND together as the shared signal reference --
    required even though the Pi doesn't power the LEDs.

Physical layout -- pick whichever matches how the 6 modules are actually
mounted, or switch live from the control panel:
    grid   3 rows of 2 modules side by side (64 wide x 24 tall)
    strip  2 stacked rows of 3 chained modules (96 wide x 16 tall)
    half   a single row of 3 chained modules (96 wide x 8 tall) --
           --num-panels 3, for testing one block of 3 in isolation (e.g.
           narrowing down a wiring/power problem to one half of the chain)

Both assume the daisy chain runs top row first, left to right, then the
next row down, also left to right (module 1 = top-left, same direction
every row) -- that's the only chain order this hardware/library
combination can represent cleanly. If the real cabling snakes back and
forth (alternating direction row to row), re-cable to match this
convention rather than fighting it in software; there's no clean fix on
the software side for that case.

Dependencies (not yet in any install script -- add by hand for now):
    sudo apt install python3-pip python3-numpy python3-pil python3-opencv
    pip3 install --break-system-packages luma.led_matrix luma.core spidev
    sudo raspi-config   # Interface Options -> SPI -> Enable, then reboot

Run with:
    sudo python3 double_matrix.py --media clip.mp4 --text "hello world"
    sudo python3 double_matrix.py --text "SPECIALS TODAY: ..."   # text only
    sudo python3 double_matrix.py --media clip.mp4                # video only
    sudo python3 double_matrix.py --web-port 8099                 # nothing yet
                                                                    -- add
                                                                    everything
                                                                    from the
                                                                    browser

--media just seeds the first item of a playback queue -- add more images/
videos from the control panel (upload button), reorder is chain order, each
gets its own start/end trim (or hold duration for images/looping video) and
a loop toggle, with a short crossfade between items.

Useful flags:
    --panel-width / --panel-height   pixels per module -- defaults (32x8)
                                       match the Youmile TS-YM-322. Fixed at
                                       startup (physical wiring), not
                                       editable from the control panel.
    --num-panels                       modules chained together (default 6).
                                       Also fixed at startup.
    --layout grid|strip                 which of the two physical
                                       arrangements above -- live-editable.
    --block-orientation 0|90|180|270    rotate every 8x8 chip in the chain
                                       the same way -- global, not
                                       per-module (content spanning a
                                       module boundary only reads right
                                       when every module agrees). Live.

    Per-module mirror: drag a tile in the layout grid, same widget as
    --order. H/V flip that module horizontally/vertically, independently
    -- for the one module that's wired backwards relative to its
    neighbours (a whole-tile mirror doesn't cross chip boundaries, so
    unlike rotation it's safe to differ per module). Live, no CLI flag --
    calibrate on the wall.

    --rotate180                         the whole assembly is mounted
                                       upside down -- flips the final image
                                       before sending. Live-editable.
    --calibrate                         show each module's block number
                                       instead of the media/text, so you can
                                       read off the wall which module sits
                                       where. Live-editable.
    --order "1,3,2,4,5,6"              which block of the picture each
                                       module along the daisy chain shows,
                                       for when they aren't mounted in chain
                                       order. Turn on --calibrate, then edit
                                       this until the wall reads 1, 2, 3, ...
                                       in reading order. Live-editable.
    --active N                          only drive the first N modules
                                       along the chain, blanking the rest --
                                       for testing a subset (e.g. the first
                                       3 of 6, if only that half is powered
                                       right now) without changing
                                       --num-panels or the wiring.
                                       Live-editable.
    --threshold N                       0-255 luminance cutoff for a "lit"
                                       pixel (dimmer content needs a lower
                                       threshold to show up). Live-editable.
    --dither / --no-dither             ordered (Bayer) dithering instead of
                                       a flat cutoff -- makes video/photos
                                       readable as more than flat blobs on
                                       1-bit pixels. Default on.
    --brightness N                      0-255, MAX7219 hardware intensity
                                       register. Live-editable.

    Layout, block orientation, rotate180, threshold, dither and brightness
    are all live-editable from the control panel below -- it shows a
    diagram of the actual chain order that updates the moment you change
    layout, no page reload, no restart. Panel count/size stay CLI-only.

    --media-brightness / --media-contrast   percent, 100=neutral -- global
                                       software adjustment applied to the
                                       whole media queue on top of each
                                       item's own (set per-item from the
                                       control panel, next to its start/
                                       end/loop controls), before the
                                       threshold/dither step above.
    --media PATH             video file to seed the playback queue with
                                (anything OpenCV can decode); more items --
                                images too -- are added from the control
                                panel while it runs
    --text STRING             text to scroll; whenever the queue also has
                                items it scrolls in a strip along the
                                bottom, otherwise it fills the whole canvas.
    --text-height N            rows reserved for the strip when the queue
                                also has items (default 8)
    --transition-s N            crossfade duration between queue items,
                                seconds (default 0.6)
    --upload-dir PATH            where uploaded media is saved (default:
                                ./uploads next to this script)
    --font PATH                 .ttf/.otf font (default: DejaVu Sans --
                                `sudo apt install fonts-dejavu-core`); bold/
                                italic below only affect the default font,
                                a custom --font is used as-is
    --font-size N
    --bold / --italic
    --scroll-speed N            pixels/second
    --text-direction left|right|up|down   which axis it travels on and
                                which way -- independent of how it's drawn
                                (see --text-stacked below).
    --text-stacked              one character per row instead of one
                                normal line -- needs out_h tall enough to
                                show more than one row, i.e. text-only mode.
    --text-glyph-rotate 0|90|270   --text-stacked only: rotate each
                                character in place.
    --fit letterbox|fill        how the video fills its area (default fill)
    --spi-port / --spi-device    SPI bus/chip-select (default 0/0, i.e.
                                CE0, matching the wiring above)
    --spi-hz                     SPI clock speed (default 1000000 -- a
                                24-chip cascade (6 modules) is noisy at the
                                8MHz luma/library default over anything but
                                short, high-quality wiring; the modules
                                farthest down the chain are hit worst, so
                                flicker + dark tail-end modules means try
                                lowering this before suspecting anything
                                else. Raise it later once it's reliable if
                                you want snappier updates)
    --web-port N                 live control panel at http://<pi>:N/ --
                                edit everything above (except panel count/
                                size) while it's running, save/load the
                                whole config as JSON (default 8099, 0
                                disables)
    --stats
"""

import argparse
import http.server
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from html import escape

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from luma.core.interface.serial import spi, noop
from luma.led_matrix.device import max7219


FONT_VARIANTS = {
    (False, False): ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
    (True, False): ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
    (False, True): ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf"],
    (True, True): ["/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-BoldOblique.ttf"],
}

TEXT_COLOR = (255, 255, 255)  # only luminance reaches the hardware -- hue is moot

# rows, cols of modules for each named physical layout.
LAYOUTS = {"grid": (3, 2), "strip": (2, 3), "half": (1, 3)}

# luma's spi() asserts bus_speed_hz is exactly one of a fixed set of clock
# dividers (0.5/1/2/4/8/...MHz) -- anything else raises AssertionError deep
# inside device construction, before main()'s own try/except even starts,
# so a bad value here is an unrecoverable crash loop, not a soft failure.
# This is the practical low-speed subset for a long MAX7219 chain; luma
# itself also accepts 16-52MHz, not offered here as pointless for this.
SPI_HZ_CHOICES = (500_000, 1_000_000, 2_000_000, 4_000_000, 8_000_000)


def split_row_for(rows, dual_bus):
    """How many of `rows` grid-rows the first SPI bus drives when
    --spi-device2 is set -- the rest go to a second, independent bus (its
    own direct DIN/CLK/CS run from the Pi, not daisy-chained off the first
    bus's last chip). Single-bus mode (dual_bus False, the default) keeps
    everything on the one device, same as before dual-bus existed."""
    return (rows // 2) if dual_bus else rows


def load_font(path, size, bold=False, italic=False):
    candidates = [path] if path else FONT_VARIANTS[(bold, italic)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    sys.exit("no usable font found -- pass --font /path/to/font.ttf "
              "(try: sudo apt install fonts-dejavu-core)")


def fit_frame(frame, fit, out_w, out_h):
    """Resize an RGB frame to out_w x out_h, either cropping to fill or
    letterboxing to fit the whole frame."""
    h, w = frame.shape[:2]
    if fit == "fill":
        scale = max(out_w / w, out_h / h)
    else:  # letterbox
        scale = min(out_w / w, out_h / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)

    if fit == "fill":
        x0, y0 = (nw - out_w) // 2, (nh - out_h) // 2
        return resized[y0:y0 + out_h, x0:x0 + out_w]

    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
    x0, y0 = (out_w - nw) // 2, (out_h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def adjust_frame(frame, brightness_pct, contrast_pct):
    """brightness/contrast as percent, 100 = neutral. Brightness is a
    straight gain; contrast scales around the 128 midpoint."""
    if brightness_pct == 100 and contrast_pct == 100:
        return frame
    f = frame.astype(np.float32) * (brightness_pct / 100.0)
    f = (f - 128.0) * (contrast_pct / 100.0) + 128.0
    return np.clip(f, 0, 255).astype(np.uint8)


def transform_frame(frame, rotation_deg, scale_pct, offset_x, offset_y):
    """Rotate (about center) + scale + translate, in one affine warp."""
    if rotation_deg == 0 and scale_pct == 100 and offset_x == 0 and offset_y == 0:
        return frame
    h, w = frame.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), rotation_deg, scale_pct / 100.0)
    m[0, 2] += offset_x
    m[1, 2] += offset_y
    return cv2.warpAffine(frame, m, (w, h))


# 4x4 ordered (Bayer) dither matrix, scaled to 0-255. Spreads gray levels
# across neighbouring 1-bit pixels instead of a flat cutoff, so video/photos
# read as more than flat blobs on hardware with no real per-pixel brightness.
BAYER4 = np.array([[0, 8, 2, 10],
                    [12, 4, 14, 6],
                    [3, 11, 1, 9],
                    [15, 7, 13, 5]], dtype=np.float32) * (255.0 / 16.0)


def frame_to_bitmap(gray, threshold, dither):
    """gray: uint8 2D array -> bool array, True = lit pixel. `threshold`
    (0-255) is a flat cutoff without dithering; with dithering it instead
    biases the whole Bayer comparison, so 128 stays neutral either way."""
    if not dither:
        return gray >= threshold
    h, w = gray.shape
    tile = np.tile(BAYER4, (math.ceil(h / 4), math.ceil(w / 4)))[:h, :w]
    bias = 128 - threshold
    return (gray.astype(np.int16) + bias) > tile


class ClipSource:
    """One playlist item's decoder. `start`/`end` trim a video (seconds);
    with `loop` on, `end` instead means how long to keep looping before the
    item's turn ends. An image just holds a still and treats `end` as how
    long to display it (default 5s)."""

    def __init__(self, item):
        self.kind = item["kind"]
        self.start = max(0.0, item.get("start") or 0.0)
        self.end = item.get("end")
        self.loop = bool(item.get("loop"))
        self.cap = None

        if self.kind == "image":
            self.still = np.array(Image.open(item["path"]).convert("RGB"))
            self.total_s = self.end if self.end and self.end > 0 else 5.0
            return

        self.cap = cv2.VideoCapture(item["path"])
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open {item['path']}")
        if self.start:
            self.cap.set(cv2.CAP_PROP_POS_MSEC, self.start * 1000)
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
        frame_count = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        natural = (frame_count / fps) if fps > 0 and frame_count > 0 else None
        if self.loop:
            self.total_s = self.end if self.end and self.end > 0 else float("inf")
        elif self.end and self.end > self.start:
            self.total_s = self.end - self.start
        elif natural:
            self.total_s = max(0.1, natural - self.start)
        else:
            self.total_s = float("inf")  # unknown length -- rely on EOF instead

    def get_frame(self, fit, out_w, out_h):
        """Returns (frame, eof) -- eof means playback ended and won't loop."""
        if self.kind == "image":
            return fit_frame(self.still, fit, out_w, out_h), False

        ok, frame = self.cap.read()
        if not ok:
            if not self.loop:
                return np.zeros((out_h, out_w, 3), dtype=np.uint8), True
            self.cap.set(cv2.CAP_PROP_POS_MSEC, self.start * 1000)
            ok, frame = self.cap.read()
            if not ok:
                return np.zeros((out_h, out_w, 3), dtype=np.uint8), True
        return fit_frame(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), fit, out_w, out_h), False

    def close(self):
        if self.cap:
            self.cap.release()


class QueuePlayer:
    """Plays a live-editable queue of images/videos in sequence, with a
    short crossfade between items. `queue_getter()` returns a fresh copy of
    the current queue (list of item dicts) each call -- items can be
    added/removed/edited between frames without this needing to be rebuilt."""

    def __init__(self, queue_getter, fit, transition_s=0.6):
        self.queue_getter = queue_getter
        self.fit = fit
        self.transition_s = transition_s
        self.current_id = None
        self.current_clip = None
        self.next_id = None
        self.next_clip = None
        self.transitioning = False
        self.elapsed = 0.0

    @staticmethod
    def _open(item):
        try:
            return ClipSource(item)
        except (RuntimeError, OSError) as e:
            print(f"queue: failed to open {item.get('name', item.get('path'))}: {e}")
            return None

    @staticmethod
    def _next_item(queue, after_id):
        ids = [it["id"] for it in queue]
        if after_id in ids:
            return queue[(ids.index(after_id) + 1) % len(queue)]
        return queue[0]

    @staticmethod
    def _find(queue, iid):
        return next((it for it in queue if it["id"] == iid), None)

    def next_frame(self, dt, canvas_w, out_h, media_brightness=100.0, media_contrast=100.0,
                   media_rotation=0.0, media_scale=100.0, media_pos_x=0, media_pos_y=0):
        queue = self.queue_getter()
        if not queue:
            self._reset()
            return np.zeros((out_h, canvas_w, 3), dtype=np.uint8)

        if self.current_clip is None or self.current_id not in {it["id"] for it in queue}:
            self._switch_to(queue[0])

        self.elapsed += dt
        remaining = self.current_clip.total_s - self.elapsed if self.current_clip else 0.0

        if (not self.transitioning and self.current_clip and len(queue) > 1
                and remaining <= self.transition_s):
            nxt = self._next_item(queue, self.current_id)
            if nxt["id"] != self.current_id:
                self.next_clip = self._open(nxt)
                self.next_id = nxt["id"]
                self.transitioning = True

        blank = np.zeros((out_h, canvas_w, 3), dtype=np.uint8)
        frame_a, eof_a = self.current_clip.get_frame(self.fit, canvas_w, out_h) \
            if self.current_clip else (blank, False)
        done = eof_a or (self.current_clip and self.elapsed >= self.current_clip.total_s)

        cur_item = self._find(queue, self.current_id)
        frame_a = adjust_frame(frame_a, cur_item.get("brightness", 100) if cur_item else 100,
                                cur_item.get("contrast", 100) if cur_item else 100)

        if self.transitioning and self.next_clip:
            t = 1.0 - max(0.0, min(1.0, remaining / self.transition_s)) if self.transition_s else 1.0
            t = t * t * (3 - 2 * t)  # smoothstep ease in/out
            frame_b, _ = self.next_clip.get_frame(self.fit, canvas_w, out_h)
            next_item = self._find(queue, self.next_id)
            frame_b = adjust_frame(frame_b, next_item.get("brightness", 100) if next_item else 100,
                                    next_item.get("contrast", 100) if next_item else 100)
            frame = (frame_a.astype(np.float32) * (1 - t)
                     + frame_b.astype(np.float32) * t).astype(np.uint8)
        else:
            frame = frame_a

        frame = adjust_frame(frame, media_brightness, media_contrast)
        frame = transform_frame(frame, media_rotation, media_scale, media_pos_x, media_pos_y)

        if done:
            if self.transitioning and self.next_clip:
                if self.current_clip:
                    self.current_clip.close()
                self.current_clip, self.current_id = self.next_clip, self.next_id
                self.next_clip = self.next_id = None
                self.transitioning = False
                self.elapsed = 0.0
            else:
                self._switch_to(self._next_item(queue, self.current_id))

        return frame

    def _switch_to(self, item):
        if self.current_clip:
            self.current_clip.close()
        if self.next_clip:
            self.next_clip.close()
        self.current_clip = self._open(item)
        self.current_id = item["id"]
        self.next_clip = self.next_id = None
        self.transitioning = False
        self.elapsed = 0.0

    def _reset(self):
        if self.current_clip:
            self.current_clip.close()
        if self.next_clip:
            self.next_clip.close()
        self.current_clip = self.next_clip = None
        self.current_id = self.next_id = None
        self.transitioning = False
        self.elapsed = 0.0


class TextScroller:
    """Renders `text` once, then hands back an out_w x out_h sliding window
    of it (with a gap before it repeats) for any pixel offset. Two
    independent choices:

    `direction` (left/right/up/down) is purely which axis the content
    slides along and which way. `stacked` picks the drawing: False is one
    normal horizontal line; True stacks it one upright character per row
    (with `glyph_rotate` additionally rotating each character in place)."""

    def __init__(self, text, font, out_w, out_h, color, direction="left",
                 stacked=False, glyph_rotate=0):
        self.direction = direction
        self.out_w, self.out_h = out_w, out_h
        horizontal = direction in ("left", "right")
        dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        if not stacked:
            bbox = dummy.textbbox((0, 0), text, font=font)
            text_w, text_h = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
            content_w, content_h = (text_w, out_h) if horizontal else (out_w, text_h)
            img = Image.new("RGB", (content_w, content_h), (0, 0, 0))
            x = -bbox[0] if horizontal else (out_w - text_w) // 2 - bbox[0]
            y = (out_h - text_h) // 2 - bbox[1] if horizontal else -bbox[1]
            ImageDraw.Draw(img).text((x, y), text, font=font, fill=color)
        else:
            chars = list(text) if text else [" "]
            ascent, descent = font.getmetrics()
            line_h = max(1, ascent + descent)
            glyphs = []
            for ch in chars:
                bbox = dummy.textbbox((0, 0), ch, font=font)
                cw, chh = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
                glyph = Image.new("RGB", (cw, chh), (0, 0, 0))
                ImageDraw.Draw(glyph).text((-bbox[0], -bbox[1]), ch, font=font, fill=color)
                glyphs.append(glyph.rotate(glyph_rotate, expand=True) if glyph_rotate else glyph)
            if horizontal:
                cell = max(g.width for g in glyphs)
                content_w, content_h = cell * len(glyphs), out_h
                img = Image.new("RGB", (content_w, content_h), (0, 0, 0))
                for i, g in enumerate(glyphs):
                    gx = i * cell + (cell - g.width) // 2
                    gy = (out_h - g.height) // 2
                    img.paste(g, (gx, gy))
            else:
                cell = line_h
                content_w, content_h = out_w, cell * len(glyphs)
                img = Image.new("RGB", (content_w, content_h), (0, 0, 0))
                for i, g in enumerate(glyphs):
                    gx = (out_w - g.width) // 2
                    gy = i * cell + (cell - g.height) // 2
                    img.paste(g, (gx, gy))

        gap = np.zeros((out_h, out_w, 3), dtype=np.uint8)
        self.loop = np.concatenate([np.array(img), gap], axis=1 if horizontal else 0)
        self.axis_len = self.loop.shape[1 if horizontal else 0]

    def frame(self, offset_px):
        start = int(offset_px) % self.axis_len
        if self.direction in ("left", "right"):
            end = start + self.out_w
            if end <= self.axis_len:
                return self.loop[:, start:end]
            wrap = end - self.axis_len
            return np.concatenate([self.loop[:, start:], self.loop[:, :wrap]], axis=1)
        end = start + self.out_h
        if end <= self.axis_len:
            return self.loop[start:end, :]
        wrap = end - self.axis_len
        return np.concatenate([self.loop[start:, :], self.loop[:wrap, :]], axis=0)


class State:
    """Live-editable display settings, shared between the render loop and
    the web control server. `version` bumps only on changes that require a
    rebuild -- text bitmap, or geometry/device (layout, block orientation)."""

    def __init__(self, args, state_file=None):
        self.lock = threading.Lock()
        self.state_file = state_file
        self.text = args.text or ""
        self.bold = args.bold
        self.italic = args.italic
        self.scroll_speed = args.scroll_speed
        self.text_direction = args.text_direction
        self.text_stacked = args.text_stacked
        self.text_glyph_rotate = args.text_glyph_rotate
        self.brightness = args.brightness
        # Applied live (a plain attribute set on the underlying spidev
        # handle, no device rebuild) -- for tuning a marginal chain/wiring
        # run from the web UI instead of editing config + restarting.
        self.spi_hz = args.spi_hz
        self.media_brightness = args.media_brightness
        self.media_contrast = args.media_contrast
        self.media_rotation = 0.0
        self.media_scale = 100.0
        self.media_pos_x = 0
        self.media_pos_y = 0
        self.layout = args.layout
        # Global -- every chip in the chain rotated the same way, via the
        # device's own luma block_orientation. Has to be uniform: content
        # spanning a module boundary (video, scrolling text, the
        # calibration marker) only reads correctly when every module
        # agrees on this.
        self.block_orientation = args.block_orientation
        # Per-module mirror, unlike rotation, doesn't touch chip boundaries
        # so it's safe to differ module to module -- calibrated by dragging
        # that module's own tile in the layout grid.
        self.flip_h = [False] * args.num_panels
        self.flip_v = [False] * args.num_panels
        self.rotate180 = args.rotate180
        self.threshold = args.threshold
        self.dither = args.dither
        self.calibrate = args.calibrate
        self.order = list(args.order) if args.order else list(range(args.num_panels))
        self.active = args.active if args.active else args.num_panels
        self.queue = []
        if args.media:
            self.queue.append({
                "id": uuid.uuid4().hex[:8], "path": args.media, "kind": "video",
                "name": os.path.basename(args.media),
                "start": 0.0, "end": None, "loop": False,
                "brightness": 100.0, "contrast": 100.0,
            })
        self.version = 0
        if state_file and os.path.isfile(state_file):
            self._load(state_file)

    def _load(self, path):
        """Restore the config from the last time "Save config (JSON)" was
        pressed (see `save()`)."""
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        queue = data.pop("queue", None)
        self.apply_wire(data)
        if isinstance(queue, list):
            self.queue = [q for q in queue if os.path.isfile(q.get("path", ""))]

    def save(self):
        """Persist the current config so `_load` can restore it next boot.
        Called only when "Save config (JSON)" is pressed."""
        if not self.state_file:
            return
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.to_wire(), f, indent=2)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return dict(text=self.text, bold=self.bold, italic=self.italic,
                        scroll_speed=self.scroll_speed,
                        text_direction=self.text_direction,
                        text_stacked=self.text_stacked,
                        text_glyph_rotate=self.text_glyph_rotate,
                        brightness=self.brightness, spi_hz=self.spi_hz,
                        media_brightness=self.media_brightness,
                        media_contrast=self.media_contrast,
                        media_rotation=self.media_rotation,
                        media_scale=self.media_scale,
                        media_pos_x=self.media_pos_x, media_pos_y=self.media_pos_y,
                        layout=self.layout, block_orientation=self.block_orientation,
                        flip_h=list(self.flip_h), flip_v=list(self.flip_v),
                        rotate180=self.rotate180, threshold=self.threshold,
                        dither=self.dither, calibrate=self.calibrate,
                        order=list(self.order), active=self.active,
                        queue=[dict(q) for q in self.queue], version=self.version)

    def to_wire(self):
        """JSON-serializable snapshot for the web UI / a saved config file."""
        snap = self.snapshot()
        del snap["version"]
        return snap

    def add_media(self, path, kind, name):
        with self.lock:
            self.queue.append({
                "id": uuid.uuid4().hex[:8], "path": path, "kind": kind, "name": name,
                "start": 0.0, "end": None, "loop": False,
                "brightness": 100.0, "contrast": 100.0,
            })
            self.version += 1

    def apply_wire(self, data):
        """Bulk-update from a JSON dict -- the web UI's live edits, or a
        loaded config file. Missing fields keep their current value; only
        bumps `version` (triggering a rebuild) if something that actually
        needs one changed."""
        with self.lock:
            rebuild = False
            if "text" in data and str(data["text"]) != self.text:
                self.text = str(data["text"])
                rebuild = True
            if "bold" in data and bool(data["bold"]) != self.bold:
                self.bold = bool(data["bold"])
                rebuild = True
            if "italic" in data and bool(data["italic"]) != self.italic:
                self.italic = bool(data["italic"])
                rebuild = True
            if "scroll_speed" in data:
                self.scroll_speed = float(data["scroll_speed"])
            if "text_direction" in data and data["text_direction"] in \
                    ("left", "right", "up", "down") and data["text_direction"] != self.text_direction:
                self.text_direction = data["text_direction"]
                rebuild = True
            if "text_stacked" in data and bool(data["text_stacked"]) != self.text_stacked:
                self.text_stacked = bool(data["text_stacked"])
                rebuild = True
            if "text_glyph_rotate" in data and str(data["text_glyph_rotate"]) in ("0", "90", "270") \
                    and int(data["text_glyph_rotate"]) != self.text_glyph_rotate:
                self.text_glyph_rotate = int(data["text_glyph_rotate"])
                rebuild = True
            if "brightness" in data:
                self.brightness = max(0, min(255, int(data["brightness"])))
            if "spi_hz" in data:
                try:
                    hz = int(data["spi_hz"])
                    if hz in SPI_HZ_CHOICES:
                        self.spi_hz = hz
                except (TypeError, ValueError):
                    pass
            if "media_brightness" in data:
                try:
                    self.media_brightness = max(0.0, min(200.0, float(data["media_brightness"])))
                except (TypeError, ValueError):
                    pass
            if "media_contrast" in data:
                try:
                    self.media_contrast = max(0.0, min(200.0, float(data["media_contrast"])))
                except (TypeError, ValueError):
                    pass
            if "media_rotation" in data:
                try:
                    self.media_rotation = max(-180.0, min(180.0, float(data["media_rotation"])))
                except (TypeError, ValueError):
                    pass
            if "media_scale" in data:
                try:
                    self.media_scale = max(10.0, min(400.0, float(data["media_scale"])))
                except (TypeError, ValueError):
                    pass
            if "media_pos_x" in data:
                try:
                    self.media_pos_x = int(data["media_pos_x"])
                except (TypeError, ValueError):
                    pass
            if "media_pos_y" in data:
                try:
                    self.media_pos_y = int(data["media_pos_y"])
                except (TypeError, ValueError):
                    pass
            if "layout" in data and data["layout"] in LAYOUTS and data["layout"] != self.layout:
                self.layout = data["layout"]
                rebuild = True
            if "block_orientation" in data:
                try:
                    v = int(data["block_orientation"])
                except (TypeError, ValueError):
                    v = None
                if v in (0, 90, 180, 270) and v != self.block_orientation:
                    self.block_orientation = v
                    rebuild = True
            for key in ("flip_h", "flip_v"):
                if key in data:
                    cur = getattr(self, key)
                    try:
                        candidate = [bool(v) for v in data[key]]
                    except TypeError:
                        candidate = None
                    if candidate is not None and len(candidate) == len(cur):
                        setattr(self, key, candidate)
            if "rotate180" in data:
                self.rotate180 = bool(data["rotate180"])
            if "threshold" in data:
                try:
                    self.threshold = max(0, min(255, int(data["threshold"])))
                except (TypeError, ValueError):
                    pass
            if "dither" in data:
                self.dither = bool(data["dither"])
            if "calibrate" in data:
                self.calibrate = bool(data["calibrate"])
            if "order" in data:
                # Silently ignored unless it's a real permutation of the
                # existing slots -- a half-typed order in the web UI's text
                # field shouldn't scramble the wall mid-edit.
                try:
                    candidate = [int(v) for v in data["order"]]
                except (TypeError, ValueError):
                    candidate = None
                if candidate is not None and sorted(candidate) == list(range(len(self.order))):
                    self.order = candidate
            if "active" in data:
                try:
                    self.active = max(1, min(len(self.order), int(data["active"])))
                except (TypeError, ValueError):
                    pass
            if "queue" in data and isinstance(data["queue"], list):
                by_id = {item["id"]: item for item in self.queue}
                new_queue = []
                seen_ids = set()
                for entry in data["queue"]:
                    iid = entry.get("id")
                    if iid not in by_id:
                        continue  # only /upload creates new items, ignore fabricated ones
                    seen_ids.add(iid)
                    cur = by_id[iid]
                    updated = dict(cur)
                    if "start" in entry:
                        try:
                            updated["start"] = max(0.0, float(entry["start"]))
                        except (TypeError, ValueError):
                            pass
                    if "end" in entry:
                        try:
                            updated["end"] = (None if entry["end"] in (None, "", "null")
                                               else max(0.0, float(entry["end"])))
                        except (TypeError, ValueError):
                            pass
                    if "loop" in entry:
                        updated["loop"] = bool(entry["loop"])
                    if "brightness" in entry:
                        try:
                            updated["brightness"] = max(0.0, min(200.0, float(entry["brightness"])))
                        except (TypeError, ValueError):
                            pass
                    if "contrast" in entry:
                        try:
                            updated["contrast"] = max(0.0, min(200.0, float(entry["contrast"])))
                        except (TypeError, ValueError):
                            pass
                    if updated != cur:
                        rebuild = True
                    new_queue.append(updated)
                if seen_ids != set(by_id):
                    rebuild = True  # an item was removed
                self.queue = new_queue
            if rebuild:
                self.version += 1


KIND_BY_EXT = {
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video",
    ".webm": "video", ".gif": "video",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".bmp": "image", ".webp": "image",
}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def parse_multipart(content_type, body):
    """Minimal multipart/form-data parser -- just enough to pull one
    uploaded file out of a browser's FormData POST, no external deps."""
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
    if not boundary:
        raise ValueError("no multipart boundary")
    marker = ("--" + boundary).encode()
    fields, files = {}, {}
    for chunk in body.split(marker)[1:-1]:
        chunk = chunk.strip(b"\r\n")
        if not chunk:
            continue
        header_blob, _, content = chunk.partition(b"\r\n\r\n")
        name = filename = None
        for line in header_blob.decode(errors="replace").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = piece.split("=", 1)[1].strip('"')
                    elif piece.startswith("filename="):
                        filename = piece.split("=", 1)[1].strip('"')
        if filename is not None:
            files[name] = (filename, content)
        elif name is not None:
            fields[name] = content.decode(errors="replace")
    return fields, files


def parse_order(text, n):
    """"1,3,2" / "1 3 2" / "132" -> [0, 2, 1]. None if it isn't a complete
    permutation of the n blocks. The compact digit-run form only resolves
    while one block is one digit; past that it fails the permutation check
    rather than guessing where the boundaries were."""
    tokens = re.findall(r"\d+", text or "")
    if len(tokens) == 1 and len(tokens[0]) == n > 1:
        tokens = list(tokens[0])
    order = [int(t) - 1 for t in tokens]
    return order if sorted(order) == list(range(n)) else None


def chain_labels(order):
    """order[c] = which grid slot chain position c drives. Inverted: for
    each grid slot, the 1-based chain position feeding it -- i.e. the number
    that slot's module shows in calibration mode's counterpart, and the
    label on both the diagram and the live preview overlay."""
    labels = [0] * len(order)
    for c, slot in enumerate(order):
        labels[slot] = c + 1
    return labels


def apply_active_mask(bitmap, rows, cols, panel_w, panel_h, order, active):
    """Blank every module whose chain position is beyond `active` -- for
    testing a subset (e.g. the first 3 wired/powered modules) without
    touching --num-panels or the wiring: the device still spans the whole
    real chain, this just stops sending anything meaningful to the tail
    end of it. Grid-slot space, same as `apply_block_order`, applied
    before it -- masking is a chain-position decision (chain_labels), the
    darkened slots then get remapped like any other content."""
    n = rows * cols
    if active >= n:
        return bitmap
    out = bitmap.copy()
    for slot, chain_pos in enumerate(chain_labels(order)):
        if chain_pos > active:
            r, c = divmod(slot, cols)
            out[r * panel_h:(r + 1) * panel_h, c * panel_w:(c + 1) * panel_w] = False
    return out


def apply_module_transforms(bitmap, rows, cols, panel_w, panel_h, order, flip_h, flip_v):
    """Per-module mirror (independently horizontal and vertical), applied
    in grid-slot space but indexed by chain position (via `order`) -- a
    mirror is a property of the physical board, so it has to stay attached
    to that board's chain position when `order` drags its content to a
    different slot, not to whichever slot happened to hold it before.

    Rotation is NOT here -- it lives in --block-orientation (global, via
    the device's own luma block_orientation). A per-module rotation was
    tried and reverted: it only reads correctly when every module gets the
    *same* value (every chip in the chain rotated the same way keeps
    continuous content -- video, scrolling text, the calibration
    border -- reading correctly across module boundaries); the moment one
    module's rotation differs from its neighbours, content that's meant to
    flow across that boundary visibly tears there. That's not a bug to fix
    in software -- it's what a chip whose address mapping genuinely
    differs from its neighbours' looks like. A whole-tile mirror doesn't
    have this problem (it doesn't touch chip boundaries), so it's safe
    per-module."""
    if not any(flip_h) and not any(flip_v):
        return bitmap
    out = bitmap.copy()
    labels = chain_labels(order)
    for slot in range(rows * cols):
        chain_pos = labels[slot] - 1
        fh, fv = flip_h[chain_pos], flip_v[chain_pos]
        if not fh and not fv:
            continue
        r, c = divmod(slot, cols)
        y0, x0 = r * panel_h, c * panel_w
        tile = out[y0:y0 + panel_h, x0:x0 + panel_w]
        if fh:
            tile = tile[:, ::-1]
        if fv:
            tile = tile[::-1, :]
        out[y0:y0 + panel_h, x0:x0 + panel_w] = tile
    return out


def apply_block_order(frame, rows, cols, panel_w, panel_h, order):
    """Rearrange module-sized tiles so chain position c is handed the tile
    for grid slot order[c]. Everything upstream of this (compositing, text,
    calibration digits, the preview) works in grid-slot space -- the
    physical chain order only exists from here on down."""
    if list(order) == list(range(rows * cols)):
        return frame
    out = np.empty_like(frame)
    for c, slot in enumerate(order):
        cr, cc = divmod(c, cols)
        sr, sc = divmod(slot, cols)
        out[cr * panel_h:(cr + 1) * panel_h, cc * panel_w:(cc + 1) * panel_w] = \
            frame[sr * panel_h:(sr + 1) * panel_h, sc * panel_w:(sc + 1) * panel_w]
    return out


MAX7219_INTENSITY = 10  # luma.led_matrix.const.max7219.INTENSITY register address
MAX7219_NOOP = 0        # ...NOOP -- a chip ignores this pair entirely


def boot_sweep(device, canvas_w, canvas_h, rotate180, brightness,
                duration_s=1.0, fade_steps=2):
    """STARTUP animation: fade each 8x8 chip in turn, first physically
    wired chip to last, then blank -- one chip fully fades in and back out
    before the next one starts. Deliberately sequential (unlike the
    shutdown flash's overlapping randomness), so it reads as the chain
    visibly coming alive one board at a time.

    `duration_s` targets the WHOLE sweep regardless of chip count -- each
    chip's own ramp is scaled to fit, so a 12-chip bus and a 24-chip bus
    both finish in about the same time instead of the smaller one racing
    through early.

    Same per-chip INTENSITY trick as the shutdown flash (see there for
    why it has to be that and not contrast()); resets every chip's
    intensity back to `brightness` at the end, since the main loop only
    calls contrast() again when brightness *changes*."""
    cascaded = device.cascaded
    ramp_len = 2 * fade_steps + 1  # fade in to full, then back down to 0
    step_s = duration_s / (cascaded * ramp_len) if cascaded else 0
    up = [round(i * 15 / fade_steps) for i in range(fade_steps + 1)]
    ramp = up + up[-2::-1]
    blank = np.zeros((canvas_h, canvas_w), dtype=np.uint8)

    for y in range(0, canvas_h, 8):
        for x in range(0, canvas_w, 8):
            frame = blank.copy()
            frame[y:y + 8, x:x + 8] = 255
            img = Image.fromarray(frame, mode="L").convert("1")
            if rotate180:
                img = img.rotate(180)
            device.display(img)
            chip = device._offsets.index(y * canvas_w + x)
            for level in ramp:
                cmd = [MAX7219_NOOP, 0] * cascaded
                cmd[2 * chip:2 * chip + 2] = [MAX7219_INTENSITY, level]
                device.data(cmd)
                if step_s:
                    time.sleep(step_s)

    device.display(Image.fromarray(blank, mode="L").convert("1"))
    device.contrast(brightness)


def shutdown_flash_frame(chip_positions, active, canvas_w, canvas_h, cascaded, slot_for):
    """Pure per-tick math for shutdown_flash, split out so it's testable
    without real timing: given which chips are mid-fade right now
    (`active`, a dict {(gx, gy): level 0-15}), build the pixel frame (only
    active chips lit) and the per-chip INTENSITY command (NOOP everywhere
    else, so inactive chips' brightness is never touched)."""
    frame = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    cmd = [MAX7219_NOOP, 0] * cascaded
    for pos, level in active.items():
        if level <= 0:
            continue
        gx, gy = pos
        frame[gy * 8:(gy + 1) * 8, gx * 8:(gx + 1) * 8] = 255
        slot = slot_for[pos]
        cmd[2 * slot:2 * slot + 2] = [MAX7219_INTENSITY, level]
    return frame, cmd


def shutdown_flash_active_chips(t, starts, fade_s):
    """Which chips are mid-fade at time `t`, and their INTENSITY level
    (0-15, a triangular envelope that rises then falls across that chip's
    own fade_s-wide window). Pure function of the precomputed per-chip
    random start times, so the coverage guarantee -- every chip gets
    exactly one activation somewhere in [0, duration_s) -- and the
    envelope shape are both testable without any real timing or
    hardware."""
    active = {}
    for pos, start in starts.items():
        local_t = t - start
        if 0 <= local_t < fade_s:
            frac = local_t / fade_s
            active[pos] = round((1 - abs(2 * frac - 1)) * 15)
    return active


def shutdown_flash(device, canvas_w, canvas_h, rotate180, brightness,
                    duration_s=0.7, fade_s=0.09, fps=60, rng=None):
    """SHUTDOWN animation: every 8x8 chip gets its own randomly-timed
    fade-in-then-out within a short shared window, all overlapping in time
    -- unlike the startup chase (one chip at a time), several chips are
    mid-fade at any given instant, at random, so it reads as the wall
    flickering out in one chaotic burst rather than marching off in
    order. Every chip gets exactly one fade cycle somewhere in the
    window, so the whole assembly still gets touched once -- only the
    timing is randomized, not which chips participate.

    One display() + one data() call per animation tick, however many
    chips are simultaneously mid-fade -- that's what keeps ~24 chips'
    worth of independent, overlapping fades inside a real ~duration_s
    budget on actual SPI hardware, instead of one round-trip per chip.

    The fade uses the MAX7219's own per-chip INTENSITY register instead of
    device-wide contrast(): contrast() writes the same value to every
    cascaded chip in one shot, so it can't dim just one chip at a time.
    Sending a NOOP (opcode 0) for every chip except the ones mid-fade,
    whose slots carry the real INTENSITY command, changes only those
    chips' registers. `device._offsets` is the same chip-position table
    display() itself sends in, so looking a chip's image offset up in it
    names the right slot -- no separate addressing math to get wrong.

    Runs when the process is stopping (SIGTERM -- systemctl stop/restart,
    a reboot -- or Ctrl-C), right before the display is actually cleared.
    A bare `kill -9` can't run this or anything else: SIGKILL has no
    handler to catch. Resets every chip's intensity back to `brightness`
    at the end so device.clear()/show() right after doesn't clear a wall
    still stuck mid-fade in the caller's eyes (harmless in practice since
    it's about to go dark anyway, but keeps the device's own state sane
    if something else reads it first)."""
    rng = rng or random.Random()
    cascaded = device.cascaded
    offsets = list(device._offsets)
    n_x, n_y = canvas_w // 8, canvas_h // 8
    chip_positions = [(gx, gy) for gy in range(n_y) for gx in range(n_x)]
    slot_for = {(gx, gy): offsets.index(gy * 8 * canvas_w + gx * 8) for gx, gy in chip_positions}
    starts = {pos: rng.uniform(0, max(duration_s - fade_s, 0)) for pos in chip_positions}

    t0 = time.monotonic()
    dt_frame = 1.0 / fps
    while True:
        t = time.monotonic() - t0
        if t >= duration_s:
            break
        active = shutdown_flash_active_chips(t, starts, fade_s)
        frame, cmd = shutdown_flash_frame(chip_positions, active, canvas_w, canvas_h,
                                           cascaded, slot_for)
        img = Image.fromarray(frame, mode="L").convert("1")
        if rotate180:
            img = img.rotate(180)
        device.display(img)
        device.data(cmd)
        slack = dt_frame - (time.monotonic() - t0 - t)
        if slack > 0:
            time.sleep(slack)

    device.display(Image.fromarray(np.zeros((canvas_h, canvas_w), dtype=np.uint8),
                                    mode="L").convert("1"))
    device.contrast(brightness)


def run_concurrent(fn, devices, *args, **kwargs):
    """Runs `fn` (boot_sweep or shutdown_flash) on each (device, canvas_w,
    canvas_h) in `devices` at the same time, not one after another -- so a
    dual-bus assembly (--spi-device2) animates as one wall within the
    same window instead of each half animating in turn."""
    threads = [threading.Thread(target=fn, args=(d, cw, ch, *args), kwargs=kwargs)
               for d, cw, ch in devices]
    for th in threads:
        th.start()
    for th in threads:
        th.join()


# Classic 3x5 pixel numerals -- legible at any module height >=5px, unlike
# a TrueType font antialiased down this small: at 7-8px a curved digit like
# "2" or "3" degrades to noise once thresholded to 1-bit, while a straight
# one like "1" happens to survive, which is exactly the failure mode a
# calibration display can least afford (numbers becoming unreadable).
DIGIT_GLYPHS = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
}
DIGIT_GLYPH_W, DIGIT_GLYPH_H = 3, 5


def draw_digits(draw, x0, y0, text, color=(255, 255, 255)):
    """Draw `text` (digits only) as exact DIGIT_GLYPHS pixels at (x0, y0)."""
    cx = x0
    for ch in text:
        for gy, row in enumerate(DIGIT_GLYPHS[ch]):
            for gx, bit in enumerate(row):
                if bit == "1":
                    draw.point((cx + gx, y0 + gy), fill=color)
        cx += DIGIT_GLYPH_W + 1


def render_calibration_frame(rows, cols, panel_w, panel_h):
    """Calibration pattern: every module shows its grid-slot number inside a
    border, so you can read straight off the wall which physical module is
    sitting where. Adjust the block order until the wall reads 1, 2, 3, ...
    in normal reading order -- then the mapping matches the hardware.

    Border and digit are drawn inside a single 8x8 chip, not spanning the
    whole panel_w x panel_h module: a marker that crosses a chip boundary
    comes apart under --block-orientation (each chip's own 8x8 gets rotated
    independently by the driver), which made the calibration display itself
    unreadable under rotation. A marker confined to one chip rotates as one
    clean piece regardless."""
    img = Image.new("RGB", (cols * panel_w, rows * panel_h), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    chip_col = (panel_w // 8) // 2  # a chip fully inside the module
    for slot in range(rows * cols):
        r, c = divmod(slot, cols)
        x0, y0 = c * panel_w + chip_col * 8, r * panel_h
        draw.rectangle([x0, y0, x0 + 7, y0 + panel_h - 1], outline=(255, 255, 255))
        label = str(slot + 1)
        label_w = len(label) * (DIGIT_GLYPH_W + 1) - 1
        # Centered within the interior, inside the 1px border on every edge.
        tx = x0 + 1 + max(0, (8 - 2 - label_w) // 2)
        ty = y0 + 1 + max(0, (panel_h - 2 - DIGIT_GLYPH_H) // 2)
        draw_digits(draw, tx, ty, label)
    return np.array(img)


def _opt(value, current):
    return f'<option value="{value}" {"selected" if str(value) == str(current) else ""}>{value}</option>'


def _queue_item_row(item):
    end_val = "" if item["end"] is None else f"{item['end']:g}"
    brightness = item.get("brightness", 100)
    contrast = item.get("contrast", 100)
    return f"""
<div id="qi-{item['id']}" style="border:1px solid #333;border-radius:6px;padding:.6rem;margin-bottom:.5rem">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <strong>{escape(item['name'])}</strong> <span style="color:#888;font-size:.8rem">({item['kind']})</span>
    <button onclick="removeItem('{item['id']}')"
      style="background:none;border:none;color:#f66;font-size:1.1rem;cursor:pointer">&times;</button>
  </div>
  <label>Start (s) <input type="number" min="0" step="0.1" value="{item['start']:g}" style="width:5rem"
    onchange="setItem('{item['id']}','start',parseFloat(this.value))"></label>
  &nbsp; <label>End (s) <input type="number" min="0" step="0.1" value="{end_val}"
    placeholder="natural end" style="width:6rem"
    onchange="setItem('{item['id']}','end',this.value===''?null:parseFloat(this.value))"></label>
  &nbsp; <label><input type="checkbox" {"checked" if item['loop'] else ""}
    onchange="setItem('{item['id']}','loop',this.checked)"> Loop</label>
  <br>
  <label>Brightness (%) <input type="number" min="0" max="200" step="5" value="{brightness:g}" style="width:5rem"
    onchange="setItem('{item['id']}','brightness',parseFloat(this.value))"></label>
  &nbsp; <label>Contrast (%) <input type="number" min="0" max="200" step="5" value="{contrast:g}" style="width:5rem"
    onchange="setItem('{item['id']}','contrast',parseFloat(this.value))"></label>
</div>"""


class Preview:
    """The exact bitmap just sent to the panels, for the control page's
    live pixel-grid emulator."""

    def __init__(self):
        self.lock = threading.Lock()
        self.bitmap = None

    def update(self, bitmap):
        with self.lock:
            self.bitmap = bitmap

    def snapshot(self):
        with self.lock:
            return self.bitmap


class ControlHandler(http.server.BaseHTTPRequestHandler):
    state = None  # bound per-instance by make_control_server
    panel_w = panel_h = upload_dir = None
    preview = None

    def _send(self, body, content_type, code=200, extra_headers=None):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else "{}"
        return json.loads(raw)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", ""):
            self._send(self._render_page(), "text/html; charset=utf-8")
        elif path == "/config.json":
            self.state.save()
            body = json.dumps(self.state.to_wire(), indent=2)
            self._send(body, "application/json", extra_headers={
                "Content-Disposition": 'attachment; filename="double-matrix-config.json"'})
        elif path == "/frame.json":
            bitmap = self.preview.snapshot() if self.preview else None
            if bitmap is None:
                body = json.dumps({"w": 0, "h": 0, "bits": []})
            else:
                snap = self.state.snapshot()
                rows, cols = LAYOUTS[snap["layout"]]
                h, w = bitmap.shape[:2]
                # The block geometry rides along so the preview can draw
                # module boundaries and their chain numbers over the pixels.
                body = json.dumps({
                    "w": w, "h": h,
                    "bits": bitmap.astype(int).reshape(-1).tolist(),
                    "rows": rows, "cols": cols,
                    "pw": self.panel_w, "ph": self.panel_h,
                    "labels": chain_labels(snap["order"]), "active": snap["active"],
                    "flip_h": snap["flip_h"], "flip_v": snap["flip_v"],
                })
            self._send(body, "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/update":
            try:
                data = self._read_json()
            except (ValueError, TypeError):
                self.send_response(400)
                self.end_headers()
                return
            self.state.apply_wire(data)
            self._send("ok", "text/plain")
        elif self.path == "/upload":
            self._handle_upload()
        elif self.path == "/restart":
            self._handle_restart()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_restart(self):
        """sudo systemctl restart double-matrix -- for when the LED chain
        needs a hard reset (e.g. after replugging modules) and a page reload
        isn't enough. Reply first: the restart kills this very process, so
        the actual systemctl call happens on a short delay in a background
        thread, after the response has had time to reach the browser."""
        self._send("restarting", "text/plain")

        def restart_after_reply():
            time.sleep(0.3)
            subprocess.Popen(["sudo", "systemctl", "restart", "double-matrix"])

        threading.Thread(target=restart_after_reply, daemon=True).start()

    def _handle_upload(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self._send("file too large or empty", "text/plain", code=413)
            return
        body = self.rfile.read(length)
        try:
            _, files = parse_multipart(self.headers.get("Content-Type", ""), body)
            filename, content = next(iter(files.values()))
        except (ValueError, StopIteration):
            self._send("bad upload", "text/plain", code=400)
            return
        ext = os.path.splitext(filename)[1].lower()
        kind = KIND_BY_EXT.get(ext)
        if kind is None:
            self._send(f"unsupported file type: {ext or '(none)'}", "text/plain", code=400)
            return
        dest_name = f"{uuid.uuid4().hex[:8]}{ext}"
        with open(os.path.join(self.upload_dir, dest_name), "wb") as f:
            f.write(content)
        self.state.add_media(os.path.join(self.upload_dir, dest_name), kind, filename)
        new_item = self.state.snapshot()["queue"][-1]
        self._send(_queue_item_row(new_item), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass

    def _render_page(self):
        snap = self.state.snapshot()
        initial_json = json.dumps(self.state.to_wire()).replace("</", "<\\/")
        queue_rows = "".join(_queue_item_row(item) for item in snap["queue"])
        rows, cols = LAYOUTS[snap["layout"]]

        return f"""<!doctype html><meta charset="utf-8">
<title>Double matrix control</title>
<body style="font:16px monospace;background:#111;color:#eee;
             max-width:32rem;margin:2rem auto;padding:0 1rem">
<h1 style="font-size:1.1rem">Double matrix control
  <span id="svcStatus" style="font-size:.6rem;padding:.15rem .5rem;border-radius:3px;
       vertical-align:middle;margin-left:.5rem;background:#2a4;color:#012">ONLINE</span>
</h1>
<button onclick="restartService()" title="sudo systemctl restart double-matrix"
  style="background:#622;color:#fdd;border:none;border-radius:4px;padding:.35rem .7rem;
         cursor:pointer">restart service</button>
<span id="restartMsg" style="font-size:.8rem;color:#888;margin-left:.5rem"></span>

<p style="color:#888;font-size:.85rem;margin:.5rem 0 .3rem">
  Live preview -- the actual bitmap being sent to the panels right now:
</p>
<div id="previewWrap" style="position:relative;display:inline-block;
     background:#000;border:1px solid #444">
  <div id="preview"></div>
  <div id="overlay" style="position:absolute;left:0;top:0;pointer-events:none"></div>
</div>

<p style="color:#888;font-size:.85rem;margin:.8rem 0 .3rem">
  Turn on calibration mode below, then drag tiles here to match the numbers
  you actually see on the wall -- read 1, 2, 3, ... left to right, top to
  bottom once it matches. H/V (bottom-right) mirror that module
  horizontally/vertically, independently, and travel with the tile if you
  drag it elsewhere -- for the one module wired backwards relative to the
  rest. If every module reads rotated the same way instead, that's Block
  orientation below, not a per-tile fix (rotating just one module tears
  content at its edges -- see the note there). {rows}x{cols} modules,
  {self.panel_w}x{self.panel_h} each.
</p>
<label><input type="checkbox" {"checked" if snap['calibrate'] else ""}
  onchange="state.calibrate=this.checked; send();"> Calibration mode</label>
&nbsp;
<button onclick="resetOrder()"
  style="background:#234;color:#eee;border:none;border-radius:4px;padding:.35rem .7rem;
         cursor:pointer">reset layout</button>
<div id="blockGrid" style="margin:.5rem 0"></div>

<label>Text<br>
  <input value="{escape(snap['text'])}" style="width:100%;padding:.4rem"
    oninput="state.text=this.value; sendDebounced();">
</label><br><br>
<label><input type="checkbox" {"checked" if snap['bold'] else ""}
  onchange="state.bold=this.checked; send();"> Bold</label>
&nbsp; <label><input type="checkbox" {"checked" if snap['italic'] else ""}
  onchange="state.italic=this.checked; send();"> Italic</label>
<br><br>
<label>Scroll speed: <span id="sval">{snap['scroll_speed']:g}</span> px/s<br>
  <input type="range" min="0" max="200" value="{snap['scroll_speed']:g}" style="width:100%"
    oninput="state.scroll_speed=parseFloat(this.value); sval.textContent=this.value; sendDebounced();">
</label><br>
<label>Direction
  <select onchange="state.text_direction=this.value; send();">
    {_opt("left", snap["text_direction"])}{_opt("right", snap["text_direction"])}
    {_opt("up", snap["text_direction"])}{_opt("down", snap["text_direction"])}
  </select>
</label>
&nbsp; <label><input type="checkbox" {"checked" if snap['text_stacked'] else ""}
  onchange="state.text_stacked=this.checked; send();"> Stack letters</label>
&nbsp; <label>Rotate letters
  <select onchange="state.text_glyph_rotate=parseInt(this.value); send();">
    {_opt("0", str(snap["text_glyph_rotate"]))}
    {_opt("90", str(snap["text_glyph_rotate"]))}
    {_opt("270", str(snap["text_glyph_rotate"]))}
  </select>
</label>
<br><br>

<p style="color:#888;font-size:.85rem;margin-bottom:.3rem">
  Media brightness/contrast (global, software -- applies on top of each
  item's own, below):
</p>
<label>Brightness: <span id="mbval">{snap['media_brightness']:g}</span>%<br>
  <input type="range" min="0" max="200" value="{snap['media_brightness']:g}" style="width:100%"
    oninput="state.media_brightness=parseFloat(this.value); mbval.textContent=this.value; sendDebounced();">
</label><br>
<label>Contrast: <span id="mcval">{snap['media_contrast']:g}</span>%<br>
  <input type="range" min="0" max="200" value="{snap['media_contrast']:g}" style="width:100%"
    oninput="state.media_contrast=parseFloat(this.value); mcval.textContent=this.value; sendDebounced();">
</label><br>
<label>Rotation: <span id="mrval">{snap['media_rotation']:g}</span>&deg;<br>
  <input type="range" min="-180" max="180" value="{snap['media_rotation']:g}" style="width:100%"
    oninput="state.media_rotation=parseFloat(this.value); mrval.textContent=this.value; sendDebounced();">
</label><br>
<label>Scale: <span id="msval">{snap['media_scale']:g}</span>%<br>
  <input type="range" min="10" max="400" value="{snap['media_scale']:g}" style="width:100%"
    oninput="state.media_scale=parseFloat(this.value); msval.textContent=this.value; sendDebounced();">
</label><br>
<label>Position X: <span id="mxval">{snap['media_pos_x']}</span>px<br>
  <input type="range" min="-{cols * self.panel_w}" max="{cols * self.panel_w}" value="{snap['media_pos_x']}" style="width:100%"
    oninput="state.media_pos_x=parseInt(this.value); mxval.textContent=this.value; sendDebounced();">
</label><br>
<label>Position Y: <span id="myval">{snap['media_pos_y']}</span>px<br>
  <input type="range" min="-{rows * self.panel_h}" max="{rows * self.panel_h}" value="{snap['media_pos_y']}" style="width:100%"
    oninput="state.media_pos_y=parseInt(this.value); myval.textContent=this.value; sendDebounced();">
</label><br><br>

<hr style="border-color:#333">
<label>Layout
  <select onchange="state.layout=this.value; send();">
    {_opt("grid", snap["layout"])}{_opt("strip", snap["layout"])}
  </select>
</label>
<span style="color:#888;font-size:.8rem">grid = 3 rows x 2 modules, strip = 2 rows x 3 chained</span>
<br><br>
<label>Block orientation
  <select onchange="state.block_orientation=parseInt(this.value); send();">
    {_opt("0", str(snap["block_orientation"]))}
    {_opt("90", str(snap["block_orientation"]))}
    {_opt("180", str(snap["block_orientation"]))}
    {_opt("270", str(snap["block_orientation"]))}
  </select>
</label>
<span style="color:#888;font-size:.8rem">
  Global, not per-module -- rotates every chip in the chain the same way.
  Has to be uniform: content crossing a module boundary (video, scrolling
  text, the layout grid's own numbers) only reads correctly when every
  module agrees. One module wired backwards instead of rotated? Use its
  H/V buttons in the layout grid above -- that's safe per-module.
</span>
<br><br>
<label><input type="checkbox" {"checked" if snap['rotate180'] else ""}
  onchange="state.rotate180=this.checked; send();"> Assembly mounted upside down</label>
<br><br>
<label>SPI clock
  <select onchange="state.spi_hz=parseInt(this.value); send();">
    {"".join(_opt(str(hz), str(snap["spi_hz"])) for hz in SPI_HZ_CHOICES)}
  </select>
</label>
<span style="color:#888;font-size:.8rem">
  Applies live, no restart -- a long chain or marginal wiring run needs a
  slower clock; flicker or dark/glitchy modules at the far end of the
  chain means lower this before suspecting anything else.
</span>
<br><br>

<label>Active modules: <span id="aval">{snap['active']}</span> / {rows * cols}<br>
  <input type="range" min="1" max="{rows * cols}" value="{snap['active']}" style="width:100%"
    oninput="state.active=parseInt(this.value); aval.textContent=this.value; sendDebounced();">
</label>
<span style="color:#888;font-size:.8rem">
  Only the first N modules along the chain are driven -- the rest are
  blanked (dashed in the diagram/preview), for testing a subset (e.g. the
  first 3 of 6) without touching the wiring or --num-panels.
</span>
<br><br>
<label>Threshold: <span id="tval">{snap['threshold']}</span> / 255<br>
  <input type="range" min="0" max="255" value="{snap['threshold']}" style="width:100%"
    oninput="state.threshold=parseInt(this.value); tval.textContent=this.value; sendDebounced();">
</label>
&nbsp; <label><input type="checkbox" {"checked" if snap['dither'] else ""}
  onchange="state.dither=this.checked; send();"> Dither (recommended for video/photos)</label>
<br><br>
<label>Panel brightness: <span id="bval">{snap['brightness']}</span> / 255<br>
  <input type="range" min="0" max="255" value="{snap['brightness']}" style="width:100%"
    oninput="state.brightness=parseInt(this.value); bval.textContent=this.value; sendDebounced();">
</label>

<hr style="border-color:#333">
<p style="color:#888;font-size:.85rem;margin-bottom:.3rem">
  Playback queue -- items play in order with a short crossfade between them.
</p>
<label style="display:inline-block;padding:.5rem 1rem;background:#234;
              border-radius:4px;cursor:pointer;margin-bottom:.5rem">
  + Add image/video
  <input type="file" accept="video/*,image/*" style="display:none" onchange="uploadFile(this)">
</label>
<div id="queue">{queue_rows}</div>

<hr style="border-color:#333">
<a href="/config.json" download="double-matrix-config.json"
   style="display:inline-block;padding:.5rem 1rem;background:#234;color:#eee;
          text-decoration:none;border-radius:4px">Save config (JSON)</a>
&nbsp;
<label style="display:inline-block;padding:.5rem 1rem;background:#234;
              border-radius:4px;cursor:pointer">
  Load config (JSON)
  <input type="file" accept="application/json" style="display:none" onchange="loadConfig(this)">
</label>

<script>
  const state = {initial_json};
  let debounceTimer;
  function send() {{
    fetch('/update', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                       body: JSON.stringify(state)}});
  }}
  function sendDebounced() {{
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(send, 200);
  }}
  function loadConfig(input) {{
    const file = input.files[0];
    if (!file) return;
    file.text().then(text => {{
      Object.assign(state, JSON.parse(text));
      fetch('/update', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                         body: text}}).then(() => location.reload());
    }});
  }}
  function setItem(id, key, value) {{
    const item = state.queue.find(q => q.id === id);
    if (item) item[key] = value;
    send();
  }}
  function resetOrder() {{
    state.order = state.order.map((_, i) => i);
    send();
  }}
  function removeItem(id) {{
    state.queue = state.queue.filter(q => q.id !== id);
    const row = document.getElementById('qi-' + id);
    if (row) row.remove();
    send();
  }}
  function uploadFile(input) {{
    const file = input.files[0];
    if (!file) return;
    const body = new FormData();
    body.append('file', file);
    fetch('/upload', {{method: 'POST', body}}).then(r => {{
      if (!r.ok) return r.text().then(msg => alert('Upload failed: ' + msg));
      return r.text().then(html => {{
        document.getElementById('queue').insertAdjacentHTML('beforeend', html);
        return fetch('/config.json').then(r2 => r2.json()).then(s => {{ state.queue = s.queue; }});
      }});
    }});
    input.value = '';
  }}
  const PREVIEW_CELL_PX = 6;
  function drawOverlay(data) {{
    // Purely visual -- module boundaries and chain positions traced over
    // the actual pixels, for reference. The interactive controls live in
    // the layout grid below (renderBlockGrid), not here.
    const el = document.getElementById('overlay');
    const key = [data.rows, data.cols, data.pw, data.ph, data.labels, data.active].join('|');
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    el.innerHTML = '';
    const pitch = PREVIEW_CELL_PX + 1;
    data.labels.forEach((label, slot) => {{
      const r = Math.floor(slot / data.cols), c = slot % data.cols;
      const inactive = label > data.active;
      const box = document.createElement('div');
      box.style.cssText = 'position:absolute;box-sizing:border-box;' +
        `border:1px ${{inactive ? 'dashed rgba(255,255,255,.25)' : 'solid rgba(102,204,255,.75)'}};` +
        `font:10px monospace;color:${{inactive ? '#666' : '#6cf'}};padding:0 2px`;
      box.style.left = (c * data.pw * pitch) + 'px';
      box.style.top = (r * data.ph * pitch) + 'px';
      box.style.width = (data.pw * pitch - 1) + 'px';
      box.style.height = (data.ph * pitch - 1) + 'px';
      box.textContent = label;
      el.appendChild(box);
    }});
  }}
  let dragSlot = null;
  function swapSlots(labels, a, b) {{
    const cA = labels[a] - 1, cB = labels[b] - 1;
    const newOrder = state.order.slice();
    newOrder[cA] = b;
    newOrder[cB] = a;
    state.order = newOrder;
    send();
  }}
  function renderBlockGrid(data) {{
    // The actual control surface: one draggable tile per grid slot, showing
    // which chain position (physical module) currently sits there. Drag a
    // tile onto another to swap them. H/V mirror that module horizontally/
    // vertically, independently, and travel with the tile if dragged
    // elsewhere. No rotation here -- that's global (Block orientation),
    // not per-tile: see the note above the grid.
    const el = document.getElementById('blockGrid');
    if (dragSlot !== null) return; // don't rebuild out from under an active drag
    const key = [data.rows, data.cols, data.labels, data.active,
                 data.flip_h, data.flip_v].join('|');
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    el.innerHTML = '';
    el.style.display = 'inline-grid';
    el.style.gridTemplateColumns = `repeat(${{data.cols}}, 76px)`;
    el.style.gap = '4px';
    data.labels.forEach((chainPos, slot) => {{
      const inactive = chainPos > data.active;
      const c = chainPos - 1;
      const cell = document.createElement('div');
      cell.draggable = true;
      cell.style.cssText = 'position:relative;width:76px;height:48px;box-sizing:border-box;' +
        `border:2px ${{inactive ? 'dashed #444' : 'solid #6cf'}};border-radius:4px;` +
        `background:${{inactive ? '#181818' : '#123'}};cursor:grab;` +
        'display:flex;align-items:center;justify-content:center;user-select:none';
      const num = document.createElement('span');
      num.textContent = chainPos;
      num.style.cssText = `font:bold 18px monospace;color:${{inactive ? '#555' : '#eee'}}`;
      cell.appendChild(num);
      const chip = (label, on, toggle, pos) => {{
        const b = document.createElement('span');
        b.textContent = label;
        b.style.cssText = `position:absolute;${{pos}}cursor:pointer;font:9px monospace;` +
          `padding:0 3px;background:${{on ? '#6cf' : 'rgba(255,255,255,.15)'}};` +
          `color:${{on ? '#012' : '#eee'}}`;
        b.onclick = (e) => {{ e.stopPropagation(); toggle(); }};
        return b;
      }};
      const h = chip('H', data.flip_h[c],
        () => {{ state.flip_h[c] = !state.flip_h[c]; send(); }}, 'top:1px;right:14px;');
      const v = chip('V', data.flip_v[c],
        () => {{ state.flip_v[c] = !state.flip_v[c]; send(); }}, 'top:1px;right:1px;');
      cell.appendChild(h);
      cell.appendChild(v);
      cell.addEventListener('dragstart', () => {{ dragSlot = slot; cell.style.opacity = '.4'; }});
      cell.addEventListener('dragend', () => {{ dragSlot = null; cell.style.opacity = '1'; }});
      cell.addEventListener('dragover', (e) => e.preventDefault());
      cell.addEventListener('drop', (e) => {{
        e.preventDefault();
        if (dragSlot !== null && dragSlot !== slot) swapSlots(data.labels, dragSlot, slot);
      }});
      el.appendChild(cell);
    }});
  }}
  let svcFails = 0, svcWasDown = false;
  function setSvcStatus(online) {{
    const el = document.getElementById('svcStatus');
    if (online) {{
      svcFails = 0;
      el.textContent = 'ONLINE'; el.style.background = '#2a4'; el.style.color = '#012';
      if (svcWasDown) {{
        document.getElementById('restartMsg').textContent = 'back online';
        svcWasDown = false;
      }}
    }} else if (++svcFails >= 2) {{
      el.textContent = 'OFFLINE'; el.style.background = '#a22'; el.style.color = '#fdd';
      svcWasDown = true;
    }}
  }}
  function restartService() {{
    if (!confirm('Restart the double-matrix service now?')) return;
    document.getElementById('restartMsg').textContent = 'restarting...';
    fetch('/restart', {{method: 'POST'}}).catch(() => {{}});
  }}
  function pollPreview() {{
    fetch('/frame.json').then(r => r.json()).then(data => {{
      setSvcStatus(true);
      const el = document.getElementById('preview');
      if (!data.w || !data.h) return;
      if (el.dataset.w != data.w || el.dataset.h != data.h) {{
        el.innerHTML = '';
        el.style.display = 'grid';
        el.style.gridTemplateColumns = `repeat(${{data.w}}, ${{PREVIEW_CELL_PX}}px)`;
        el.style.gap = '1px';
        for (let i = 0; i < data.w * data.h; i++) {{
          const cell = document.createElement('div');
          cell.style.width = cell.style.height = PREVIEW_CELL_PX + 'px';
          el.appendChild(cell);
        }}
        el.dataset.w = data.w;
        el.dataset.h = data.h;
      }}
      const cells = el.children;
      for (let i = 0; i < data.bits.length; i++) {{
        cells[i].style.background = data.bits[i] ? '#f30' : '#200';
      }}
      drawOverlay(data);
      renderBlockGrid(data);
    }}).catch(() => {{ setSvcStatus(false); }}).finally(() => setTimeout(pollPreview, 150));
  }}
  pollPreview();
</script>
</body>"""


def local_ip():
    """Best-guess LAN IP: ask the OS which interface it'd use to reach the
    internet, without actually sending anything (UDP connect doesn't need
    the address to be reachable)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_control_server(state, port, panel_w, panel_h, upload_dir, preview):
    handler = type("BoundControlHandler", (ControlHandler,), {
        "state": state, "panel_w": panel_w, "panel_h": panel_h,
        "upload_dir": upload_dir, "preview": preview,
    })
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def build_device(serial_iface, canvas_w, canvas_h, block_orientation, brightness):
    # luma only accepts 0/90/-90/180 -- 270 and -90 are the same turn.
    luma_angle = -90 if block_orientation == 270 else block_orientation
    return max7219(serial_iface, width=canvas_w, height=canvas_h, rotate=0,
                    block_orientation=luma_angle, contrast=brightness)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--media", help="video file to loop")
    p.add_argument("--text", help="text to scroll")
    p.add_argument("--text-height", type=int, default=8,
                   help="rows for the text strip when --media is also given")
    p.add_argument("--font")
    p.add_argument("--font-size", type=int)
    p.add_argument("--bold", action="store_true")
    p.add_argument("--italic", action="store_true")
    p.add_argument("--scroll-speed", type=float, default=40.0,
                   help="pixels/second")
    p.add_argument("--text-direction", default="left",
                   choices=["left", "right", "up", "down"])
    p.add_argument("--text-stacked", action="store_true",
                   help="one character per row instead of one normal line")
    p.add_argument("--text-glyph-rotate", type=int, default=0, choices=[0, 90, 270],
                   help="--text-stacked only: rotate each character in place")
    p.add_argument("--fit", default="fill", choices=["letterbox", "fill"])
    p.add_argument("--web-port", type=int, default=8099,
                   help="live control panel port, 0 to disable")
    p.add_argument("--upload-dir", default=None,
                   help="where uploaded media is saved (default: ./uploads "
                        "next to this script)")
    p.add_argument("--state-file", default=None,
                   help="where the config is saved when \"Save config "
                        "(JSON)\" is pressed, and reloaded from on startup "
                        "(default: ./double_state.json next to this "
                        "script). Pass an empty string to disable.")
    p.add_argument("--transition-s", type=float, default=0.6,
                   help="crossfade duration between queue items, seconds")
    p.add_argument("--panel-width", type=int, default=32)
    p.add_argument("--panel-height", type=int, default=8)
    p.add_argument("--num-panels", type=int, default=6)
    p.add_argument("--layout", default="grid", choices=list(LAYOUTS),
                   help="grid = 3 rows x 2 modules, strip = 2 rows x 3 "
                        "chained, half = 1 row x 3 chained (pair with "
                        "--num-panels 3 to drive/test only half the chain)")
    p.add_argument("--block-orientation", type=int, default=0, choices=[0, 90, 180, 270],
                   help="rotate every 8x8 chip the same way -- global, not "
                        "per-module: content spanning a module boundary "
                        "only reads correctly when every module agrees on "
                        "this. Live-editable.")
    p.add_argument("--rotate180", action="store_true",
                   help="whole assembly mounted upside down")
    p.add_argument("--calibrate", action="store_true",
                   help="show each module's block number instead of the "
                        "media/text, for working out --order")
    p.add_argument("--order", default=None,
                   help="which block of the picture each module along the "
                        "daisy chain shows, 1-based, e.g. \"1,3,2,4,5,6\" -- "
                        "for when the modules aren't mounted in chain order "
                        "(default: chain order, 1,2,3,...)")
    p.add_argument("--active", type=int, default=None,
                   help="only drive the first N modules along the chain, "
                        "blanking the rest -- for testing a subset (e.g. "
                        "the first 3 of 6) without changing --num-panels or "
                        "the wiring (default: all of them)")
    p.add_argument("--threshold", type=int, default=128,
                   help="0-255 luminance cutoff for a lit pixel")
    p.add_argument("--dither", action=argparse.BooleanOptionalAction, default=True,
                   help="ordered (Bayer) dithering instead of a flat cutoff")
    p.add_argument("--brightness", type=int, default=128,
                   help="0-255, MAX7219 hardware intensity register")
    p.add_argument("--media-brightness", type=float, default=100.0,
                   help="global software brightness for the media queue, "
                        "percent, 100=neutral")
    p.add_argument("--media-contrast", type=float, default=100.0,
                   help="global software contrast for the media queue, "
                        "percent, 100=neutral")
    p.add_argument("--spi-port", type=int, default=0)
    p.add_argument("--spi-device", type=int, default=0, help="chip-select line (CE0=0, CE1=1)")
    p.add_argument("--spi-device2", type=int, default=None,
                   help="chip-select for a SECOND, independent SPI bus (e.g. "
                        "1 for CE1) -- splits the assembly at the halfway "
                        "row, each half driven by its own direct wiring run "
                        "from the Pi instead of daisy-chaining the second "
                        "half off the first half's DOUT. Use this when a "
                        "long/marginal DOUT-to-DIN run between the two "
                        "halves glitches no matter how the chain is routed "
                        "or how slow the clock is -- CLK and DIN can stay "
                        "the same physical wires as the first bus (only CS "
                        "differs), or be run fresh; either way the second "
                        "half no longer depends on the first half's chips "
                        "to relay it. Default: one bus, the whole chain "
                        "daisy-chained as before.")
    p.add_argument("--spi-hz", type=int, default=1000000, choices=SPI_HZ_CHOICES,
                   help="24 chips cascaded gets noisy at higher speeds over "
                        "anything but short, high-quality wiring -- flicker "
                        "or dark modules at the far end of the chain means "
                        "lower this before suspecting anything else. "
                        "Live-editable from the web UI.")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    rows, cols = LAYOUTS[args.layout]
    if rows * cols != args.num_panels:
        sys.exit(f"--layout {args.layout} is a {rows}x{cols} grid ({rows * cols} "
                  f"modules) but --num-panels is {args.num_panels}")

    if args.spi_device2 is not None:
        if args.spi_device2 == args.spi_device:
            sys.exit("--spi-device2 must differ from --spi-device -- they're "
                      "separate chip-selects (e.g. --spi-device 0 --spi-device2 1)")
        if rows < 2:
            sys.exit(f"--spi-device2 splits the assembly by row, so it needs "
                      f"a layout with at least 2 rows (--layout {args.layout} "
                      f"is {rows}x{cols}) -- use grid or strip, or drop --spi-device2")

    if args.order:
        args.order = parse_order(args.order, args.num_panels)
        if args.order is None:
            sys.exit(f"--order must list each of the {args.num_panels} blocks "
                      f"exactly once, 1-based (e.g. "
                      f"\"{','.join(str(i + 1) for i in range(args.num_panels))}\")")

    if args.active is not None and not (1 <= args.active <= args.num_panels):
        sys.exit(f"--active must be between 1 and --num-panels ({args.num_panels})")

    if not args.media and not args.text and not args.web_port and not args.calibrate:
        sys.exit("nothing to show and no way to add anything -- pass "
                  "--media/--text/--calibrate, or leave --web-port enabled so "
                  "you can upload/type content once it's running")
    return args


def main():
    args = parse_args()

    # systemctl stop/restart (and a reboot's shutdown sequence) send
    # SIGTERM, not Ctrl-C's SIGINT -- without a handler that's an
    # unconditional kill, no `finally` block, no shutdown_flash. Funnel it
    # into the same KeyboardInterrupt handling below. (A bare `kill -9`
    # sends SIGKILL, which no handler can catch -- nothing can run in
    # response to that, by design.)
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _on_sigterm)

    def compute_geometry(snap):
        rows, cols = LAYOUTS[snap["layout"]]
        canvas_w, canvas_h = cols * args.panel_width, rows * args.panel_height
        has_media = bool(snap["queue"])
        has_text = bool(snap["text"])
        if has_media and has_text:
            text_h, video_h = args.text_height, canvas_h - args.text_height
        elif has_media:
            text_h, video_h = 0, canvas_h
        else:
            text_h, video_h = canvas_h, 0
        return canvas_w, canvas_h, text_h, video_h

    state_file = args.state_file if args.state_file is not None else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "double_state.json")
    state = State(args, state_file or None)
    if state_file and os.path.isfile(state_file):
        print(f"resumed saved config from {state_file}")
    snap0 = state.snapshot()
    canvas_w, canvas_h, text_h, video_h = compute_geometry(snap0)

    if snap0["queue"] and video_h <= 0:
        sys.exit("--text-height leaves no room for video -- "
                  "shrink it or use a taller layout")

    upload_dir = args.upload_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    player = QueuePlayer(lambda: state.snapshot()["queue"], args.fit, args.transition_s)

    def rebuild_scroller(snap):
        if text_h <= 0 or not snap["text"]:
            return None
        font = load_font(args.font, args.font_size or max(8, text_h - 2),
                          bold=snap["bold"], italic=snap["italic"])
        return TextScroller(snap["text"], font, canvas_w, text_h, TEXT_COLOR,
                             snap["text_direction"], snap["text_stacked"],
                             snap["text_glyph_rotate"])

    dual_bus = args.spi_device2 is not None

    scroller = rebuild_scroller(snap0)
    built_version = snap0["version"]

    preview = Preview()
    server = None
    if args.web_port:
        server = make_control_server(state, args.web_port, args.panel_width,
                                      args.panel_height, upload_dir, preview)
        print(f"control panel: http://{local_ip()}:{args.web_port}/")
        if text_h <= 0:
            print("note: no text region reserved yet (no --text/no room), so "
                  "the text field won't show anything until there's a queue "
                  "item alongside the text, or text alone")

    rows0, cols0 = LAYOUTS[snap0["layout"]]
    split_row = split_row_for(rows0, dual_bus)

    serial_iface = spi(port=args.spi_port, device=args.spi_device, gpio=noop(),
                        bus_speed_hz=snap0["spi_hz"])
    device = build_device(serial_iface, canvas_w, split_row * args.panel_height,
                           snap0["block_orientation"], snap0["brightness"])
    boot_devices = [(device, canvas_w, split_row * args.panel_height)]

    serial_iface2 = device2 = None
    if dual_bus:
        serial_iface2 = spi(port=args.spi_port, device=args.spi_device2, gpio=noop(),
                             bus_speed_hz=snap0["spi_hz"])
        device2 = build_device(serial_iface2, canvas_w, (rows0 - split_row) * args.panel_height,
                                snap0["block_orientation"], snap0["brightness"])
        boot_devices.append((device2, canvas_w, (rows0 - split_row) * args.panel_height))

    run_concurrent(boot_sweep, boot_devices, snap0["rotate180"], snap0["brightness"])

    frame_budget = 1.0 / 20.0  # SPI + PIL conversion is slower than the WS2812 path
    scroll_offset = 0.0
    last_t = time.monotonic()
    rendered = 0
    last_report = last_t
    last_brightness = snap0["brightness"]
    last_block_orientation = snap0["block_orientation"]
    last_spi_hz = snap0["spi_hz"]

    print("running -- ctrl-c to stop")
    try:
        while True:
            t0 = time.monotonic()
            dt = t0 - last_t
            last_t = t0

            snap = state.snapshot()
            if snap["version"] != built_version:
                new_cw, new_ch, new_text_h, new_video_h = compute_geometry(snap)
                if snap["queue"] and new_video_h <= 0:
                    print("layout/text-height change rejected -- leaves no "
                          "room for the media queue at this layout")
                else:
                    if (new_cw, new_ch) != (canvas_w, canvas_h) or \
                            snap["block_orientation"] != last_block_orientation:
                        new_rows, _ = LAYOUTS[snap["layout"]]
                        split_row = split_row_for(new_rows, dual_bus)
                        device = build_device(serial_iface, new_cw, split_row * args.panel_height,
                                               snap["block_orientation"], snap["brightness"])
                        if dual_bus:
                            device2 = build_device(
                                serial_iface2, new_cw, (new_rows - split_row) * args.panel_height,
                                snap["block_orientation"], snap["brightness"])
                        last_brightness = snap["brightness"]
                        last_block_orientation = snap["block_orientation"]
                    canvas_w, canvas_h = new_cw, new_ch
                    text_h, video_h = new_text_h, new_video_h
                scroller = rebuild_scroller(snap)
                built_version = snap["version"]

            if snap["brightness"] != last_brightness:
                device.contrast(snap["brightness"])
                if device2:
                    device2.contrast(snap["brightness"])
                last_brightness = snap["brightness"]

            if snap["spi_hz"] != last_spi_hz:
                serial_iface._spi.max_speed_hz = snap["spi_hz"]
                if serial_iface2:
                    serial_iface2._spi.max_speed_hz = snap["spi_hz"]
                last_spi_hz = snap["spi_hz"]

            rows, cols = LAYOUTS[snap["layout"]]
            split_row = split_row_for(rows, dual_bus)
            if snap["calibrate"]:
                frame = render_calibration_frame(rows, cols, args.panel_width, args.panel_height)
            else:
                frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
                if video_h > 0:
                    frame[0:video_h] = player.next_frame(
                        dt, canvas_w, video_h, snap["media_brightness"], snap["media_contrast"],
                        snap["media_rotation"], snap["media_scale"],
                        snap["media_pos_x"], snap["media_pos_y"])
                if scroller:
                    sign = -1.0 if snap["text_direction"] in ("right", "down") else 1.0
                    scroll_offset += sign * dt * snap["scroll_speed"]
                    frame[canvas_h - text_h:canvas_h] = scroller.frame(scroll_offset)

            # Any bright channel counts as "lit" -- avoids the classic
            # luminance-formula surprise where pure red/blue content looks
            # nearly black on a 1-bit display.
            gray = frame.max(axis=2)
            # Dithering is for photographic gradients -- on calibration's
            # exact-pixel digits/borders it only adds noise, never helps.
            dither = snap["dither"] and not snap["calibrate"]
            bitmap = frame_to_bitmap(gray, snap["threshold"], dither)
            bitmap = apply_active_mask(bitmap, rows, cols, args.panel_width,
                                        args.panel_height, snap["order"], snap["active"])
            bitmap = apply_module_transforms(bitmap, rows, cols, args.panel_width,
                                              args.panel_height, snap["order"],
                                              snap["flip_h"], snap["flip_v"])
            # The preview shows grid-slot space, post per-module correction
            # (what you meant to see, already fixed up) -- the chain remap
            # below is the last step before the wire.
            preview.update(bitmap)
            bitmap = apply_block_order(bitmap, rows, cols, args.panel_width,
                                        args.panel_height, snap["order"])

            img = Image.fromarray((bitmap.astype(np.uint8) * 255), mode="L").convert("1")
            if snap["rotate180"]:
                img = img.rotate(180)
            if device2:
                split_h = split_row * args.panel_height
                device.display(img.crop((0, 0, canvas_w, split_h)))
                device2.display(img.crop((0, split_h, canvas_w, canvas_h)))
            else:
                device.display(img)
            rendered += 1

            if args.stats and t0 - last_report >= 1.0:
                print(f"render {rendered / (t0 - last_report):5.1f} fps")
                rendered = 0
                last_report = t0

            slack = frame_budget - (time.monotonic() - t0)
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        snap = state.snapshot()
        total_rows = canvas_h // args.panel_height
        shutdown_devices = [(device, canvas_w, split_row * args.panel_height)]
        if device2:
            shutdown_devices.append((device2, canvas_w, (total_rows - split_row) * args.panel_height))
        run_concurrent(shutdown_flash, shutdown_devices, snap["rotate180"], snap["brightness"])
        device.clear()
        device.show()
        if device2:
            device2.clear()
            device2.show()
        print("\nstopped")


if __name__ == "__main__":
    main()
