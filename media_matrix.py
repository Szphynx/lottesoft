#!/usr/bin/env python3
"""
Video + scrolling text on a pair of daisy-chained WS2812B ("NeoPixel") LED
matrix panels, on a Raspberry Pi 3.

These are addressable-LED panels -- a single DIN/DOUT data line plus 5V/GND,
not HUB75 -- so this is unrelated to thermal_matrix.py in this repo, which
drives a HUB75 panel over a completely different wiring scheme and library.

Wiring:
    Pi GPIO18 (physical pin 12) -- through a 3.3V->5V level shifter
    (74AHCT125 / 74HCT245; WS2812B wants ~5V logic and the Pi's 3.3V GPIO is
    out of spec on its own -- works sometimes, flickers/misfires once wires
    warm up or get longer) -- to Panel 1's DIN.

    Panel 1 DOUT -> Panel 2 DIN (daisy chain, same idea as an LED strip).

    Ground: tie together -- a Pi GND pin (e.g. physical pin 14), the 5V/60A
    supply's GND/- terminal, and both panels' GND. Required as the signal
    reference even though the panels are powered from the supply, not the Pi.

    Power: from the external 5V/60A supply, not the Pi. Each panel has its
    own separate 5V/GND solder pads (apart from the DIN/DOUT connectors) --
    feed those straight from the supply's terminal block, one pair per
    panel, so the thin DIN/DOUT wires only carry data. Fuse each panel's
    power leads near the supply, sized to that panel's max current.

Run with:
    sudo python3 media_matrix.py --media clip.mp4 --text "hello world"
    sudo python3 media_matrix.py --text "SPECIALS TODAY: ..."   # text only
    sudo python3 media_matrix.py --media clip.mp4                # video only
    sudo python3 media_matrix.py --web-port 8098                 # nothing yet
                                                                   -- add
                                                                   everything
                                                                   from the
                                                                   browser

--media just seeds the first item of a playback queue -- add more images/
videos from the control panel (upload button), reorder is chain order, each
gets its own start/end trim (or hold duration for images/looping video) and
a loop toggle, with a short crossfade between items. Remove one with the x
on its row. Nothing uploaded yet and no --media means a black media area
until you add something (or none reserved at all if there's no --text
either -- see --layout etc. below for how that space is decided).

Useful flags:
    --panel-width / --panel-height   pixels per panel -- defaults (32x8)
                                       match the WS2812ECO 8x32 panel. Fixed
                                       at startup (physical wiring), not
                                       editable from the control panel.
    --num-panels                       panels chained together (default 2).
                                       Also fixed at startup.
    --layout horizontal|vertical       how the chain forms one image: side
                                       by side (wider) or stacked (taller).
                                       Default horizontal.
    --serpentine / --no-serpentine    most cheap matrix panels wire rows (or
                                       columns, see below) back and forth
                                       rather than all in one direction;
                                       default on. Seeds every panel's
                                       initial setting.
    --serpentine-axis row|column      row snakes across each row (default);
                                       column snakes down each column, common
                                       on narrow tall-pixel-count panels. If a
                                       horizontal scroll bounces vertically,
                                       try column. Also just a startup seed.
    --rotate 0|180              per-panel startup seed for a panel mounted
                                upside down.

    Layout, and each panel's serpentine/axis/rotate individually, are
    live-editable from the control panel below -- it shows a diagram of the
    actual LED chain order (per panel) that updates the moment you change
    anything, no page reload, no restart. Panel count/size stay CLI-only.

    --led-pin (BCM, default 18) / --led-freq-hz / --led-dma / --led-invert
    --brightness N                      0-255, default 80 -- LED hardware
                                       level, start low, these draw a lot
                                       of current at 255
    --media-brightness / --media-contrast   percent, 100=neutral -- global
                                       software adjustment applied to the
                                       whole media queue on top of each
                                       item's own (set per-item from the
                                       control panel, next to its start/
                                       end/loop controls)
    --media PATH             video file to seed the playback queue with
                                (anything OpenCV can decode); more items --
                                images too -- are added from the control
                                panel while it runs
    --text STRING             text to scroll; whenever the queue also has
                                items it scrolls in a strip along the
                                bottom, otherwise it fills the whole canvas.
                                This split is live -- it reacts to the queue
                                being empty or not, not just what you passed
                                at startup.
    --text-height N            rows reserved for the strip when the queue
                                also has items (default 4)
    --transition-s N            crossfade duration between queue items,
                                seconds (default 0.6)
    --upload-dir PATH            where uploaded media is saved (default:
                                ./uploads next to this script)
    --font PATH                 .ttf/.otf font (default: DejaVu Sans --
                                `sudo apt install fonts-dejavu-core`); bold/
                                italic below only affect the default font,
                                a custom --font is used as-is
    --font-size N
    --text-color R,G,B
    --bold / --italic
    --scroll-speed N            pixels/second
    --text-direction left|right|up|down   which axis it travels on and
                                which way -- independent of how it's drawn
                                (see --text-stacked below). Vertical travel
                                needs real room, best in text-only mode
                                where text gets the full canvas height,
                                cramped in a thin strip alongside video.
    --text-stacked              one character per row instead of one
                                normal line -- independent of direction, so
                                a stacked column can still travel sideways
                                if that's what a panel's orientation needs.
                                Needs out_h tall enough to show more than
                                one row, i.e. text-only mode.
    --fit letterbox|fill        how the video fills its area (default fill)
    --web-port N                 live control panel at http://<pi>:N/ --
                                edit everything above (except panel count/
                                size) while it's running, save/load the
                                whole config as JSON (default 8098, 0
                                disables)
    --stats
"""

import argparse
import http.server
import json
import os
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

from rpi_ws281x import PixelStrip, Color


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

    def next_frame(self, dt, canvas_w, out_h, media_brightness=100.0, media_contrast=100.0):
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
    slides along and which way -- left/right slide horizontally, up/down
    vertically. It has nothing to do with how the text is drawn.

    `stacked` picks the drawing: False is one normal horizontal line
    (the usual case); True stacks it one upright character per row --

        t
        e
        x
        t

    -- with `glyph_rotate` (0/90/270) additionally rotating each character
    in place, e.g. 270 turns an upright "t" on its side facing right.

    So e.g. stacked text can still travel left/right (the whole rotated
    column marching sideways) if that's what a given panel's physical
    orientation needs -- pick whichever combination actually looks right
    on the hardware, there's no wrong answer here. Vertical travel only
    has real room to work when out_h gives it space, i.e. text-only mode,
    not a thin strip alongside video; likewise stacked text needs out_h
    tall enough to show more than one character."""

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
            block_w = max(g.width for g in glyphs)
            block_h = line_h * len(glyphs)
            content_w, content_h = (block_w, out_h) if horizontal else (out_w, block_h)
            img = Image.new("RGB", (content_w, content_h), (0, 0, 0))
            v_center = (out_h - block_h) // 2 if horizontal else 0
            for i, g in enumerate(glyphs):
                gx = (block_w - g.width) // 2 if horizontal else (out_w - g.width) // 2
                gy = i * line_h + (line_h - g.height) // 2 + v_center
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


def build_index_map(panel_w, panel_h, layout, panel_configs):
    """canvas (y, x) -> pixel index in the chain. Each panel gets its own
    config -- {"serpentine": bool, "serpentine_axis": "row"|"column",
    "rotate": 0|180} -- so panels mounted differently (e.g. one upside down)
    can each be described correctly. Panels then extend the chain side by
    side or stacked, in list order."""
    num_panels = len(panel_configs)
    if layout == "horizontal":
        canvas_w, canvas_h = panel_w * num_panels, panel_h
    else:
        canvas_w, canvas_h = panel_w, panel_h * num_panels

    idx_map = np.zeros((canvas_h, canvas_w), dtype=np.int32)
    for y in range(canvas_h):
        for x in range(canvas_w):
            if layout == "horizontal":
                panel_idx, lx, ly = x // panel_w, x % panel_w, y
            else:
                panel_idx, lx, ly = y // panel_h, x, y % panel_h
            cfg = panel_configs[panel_idx]
            if cfg["rotate"] == 180:
                lx, ly = panel_w - 1 - lx, panel_h - 1 - ly
            if cfg["serpentine_axis"] == "column":
                yy = (panel_h - 1 - ly) if (cfg["serpentine"] and lx % 2 == 1) else ly
                local = lx * panel_h + yy
            else:
                xx = (panel_w - 1 - lx) if (cfg["serpentine"] and ly % 2 == 1) else lx
                local = ly * panel_w + xx
            idx_map[y, x] = panel_idx * panel_w * panel_h + local
    return idx_map, canvas_w, canvas_h


def build_strip(args, num_pixels):
    strip = PixelStrip(num_pixels, args.led_pin, args.led_freq_hz, args.led_dma,
                        args.led_invert, args.brightness, args.led_channel)
    strip.begin()
    return strip


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


class State:
    """Live-editable display settings, shared between the render loop and
    the web control server. `version` bumps only on changes that require a
    rebuild -- text bitmap, or the whole geometry pipeline (index map,
    canvas size, video/text split) for layout/per-panel changes."""

    def __init__(self, args):
        self.lock = threading.Lock()
        self.text = args.text or ""
        self.color = tuple(int(c) for c in args.text_color.split(","))
        self.bold = args.bold
        self.italic = args.italic
        self.scroll_speed = args.scroll_speed
        self.text_direction = args.text_direction
        self.text_stacked = args.text_stacked
        self.text_glyph_rotate = args.text_glyph_rotate
        self.brightness = args.brightness
        self.media_brightness = args.media_brightness
        self.media_contrast = args.media_contrast
        self.layout = args.layout
        self.panels = [
            {"serpentine": args.serpentine, "serpentine_axis": args.serpentine_axis,
             "rotate": args.rotate}
            for _ in range(args.num_panels)
        ]
        self.queue = []
        if args.media:
            self.queue.append({
                "id": uuid.uuid4().hex[:8], "path": args.media, "kind": "video",
                "name": os.path.basename(args.media),
                "start": 0.0, "end": None, "loop": False,
                "brightness": 100.0, "contrast": 100.0,
            })
        self.version = 0

    def snapshot(self):
        """For the render loop -- color stays an RGB tuple."""
        with self.lock:
            return dict(text=self.text, color=self.color, bold=self.bold,
                        italic=self.italic, scroll_speed=self.scroll_speed,
                        text_direction=self.text_direction,
                        text_stacked=self.text_stacked,
                        text_glyph_rotate=self.text_glyph_rotate,
                        brightness=self.brightness,
                        media_brightness=self.media_brightness,
                        media_contrast=self.media_contrast, layout=self.layout,
                        panels=[dict(p) for p in self.panels],
                        queue=[dict(q) for q in self.queue], version=self.version)

    def to_wire(self):
        """JSON-serializable snapshot for the web UI / a saved config file."""
        snap = self.snapshot()
        snap["color"] = rgb_to_hex(snap["color"])
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
            if "color" in data:
                c = hex_to_rgb(data["color"])
                if c != self.color:
                    self.color = c
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
            if "layout" in data and data["layout"] in ("horizontal", "vertical") \
                    and data["layout"] != self.layout:
                self.layout = data["layout"]
                rebuild = True
            if "panels" in data and len(data["panels"]) == len(self.panels):
                for i, p in enumerate(data["panels"]):
                    cur = self.panels[i]
                    new = {
                        "serpentine": bool(p.get("serpentine", cur["serpentine"])),
                        "serpentine_axis": p["serpentine_axis"]
                            if p.get("serpentine_axis") in ("row", "column")
                            else cur["serpentine_axis"],
                        "rotate": int(p["rotate"])
                            if str(p.get("rotate")) in ("0", "180")
                            else cur["rotate"],
                    }
                    if new != cur:
                        self.panels[i] = new
                        rebuild = True
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


def render_wiring_svg(panel_w, panel_h, layout, panel_configs, cell=14):
    """Connect-the-dots diagram of the actual LED chain order for the
    current settings -- literally traces build_index_map's output, so it
    can't drift out of sync with what's actually being rendered."""
    idx_map, canvas_w, canvas_h = build_index_map(panel_w, panel_h, layout, panel_configs)
    num_panels = len(panel_configs)
    num_pixels = panel_w * panel_h * num_panels
    pos = [None] * num_pixels
    for y in range(canvas_h):
        for x in range(canvas_w):
            pos[int(idx_map[y, x])] = (x, y)

    w, h = canvas_w * cell, canvas_h * cell
    cells = "".join(
        f'<rect x="{x * cell}" y="{y * cell}" width="{cell}" height="{cell}" '
        f'fill="none" stroke="#333"/>'
        for y in range(canvas_h) for x in range(canvas_w)
    )
    if layout == "horizontal":
        boundaries = "".join(
            f'<line x1="{i * panel_w * cell}" y1="0" x2="{i * panel_w * cell}" '
            f'y2="{h}" stroke="#888" stroke-width="2"/>'
            for i in range(1, num_panels)
        )
    else:
        boundaries = "".join(
            f'<line x1="0" y1="{i * panel_h * cell}" x2="{w}" '
            f'y2="{i * panel_h * cell}" stroke="#888" stroke-width="2"/>'
            for i in range(1, num_panels)
        )
    points = " ".join(f"{(x + 0.5) * cell:.1f},{(y + 0.5) * cell:.1f}" for x, y in pos)
    (sx, sy), (ex, ey) = pos[0], pos[-1]

    return f"""<svg viewBox="0 0 {w} {h}" role="img" aria-label="LED chain order for the current layout and per-panel settings"
     style="width:100%;max-width:420px;height:auto;background:#000;border:1px solid #444;display:block">
  {cells}{boundaries}
  <polyline points="{points}" fill="none" stroke="#6cf" stroke-width="2"/>
  <circle cx="{(sx + 0.5) * cell:.1f}" cy="{(sy + 0.5) * cell:.1f}" r="4" fill="#3f6"/>
  <circle cx="{(ex + 0.5) * cell:.1f}" cy="{(ey + 0.5) * cell:.1f}" r="4" fill="#f63"/>
</svg>"""


def _opt(value, current):
    return f'<option value="{value}" {"selected" if value == current else ""}>{value}</option>'


def render_panel_svg(panel_w, panel_h, cfg, cell=10):
    """One panel's own local wiring diagram -- same chain-order tracer as
    render_wiring_svg, just for a single panel in isolation so it can sit
    next to that panel's own controls."""
    return render_wiring_svg(panel_w, panel_h, "horizontal", [cfg], cell=cell)


def _panel_row(index, cfg, panel_w, panel_h):
    diagram = render_panel_svg(panel_w, panel_h, cfg)
    return f"""
<div style="border:1px solid #333;border-radius:6px;padding:.6rem;margin-bottom:.5rem;
            display:flex;gap:.8rem;align-items:flex-start">
  <div id="panel-diagram-{index}" style="flex:0 0 auto;width:140px">{diagram}</div>
  <div>
    <strong>Panel {index + 1}</strong><br>
    <label><input type="checkbox" {"checked" if cfg['serpentine'] else ""}
      onchange="state.panels[{index}].serpentine=this.checked; send();"> Serpentine</label>
    &nbsp; <label>Axis
      <select onchange="state.panels[{index}].serpentine_axis=this.value; send();">
        {_opt("row", cfg["serpentine_axis"])}{_opt("column", cfg["serpentine_axis"])}
      </select>
    </label>
    &nbsp; <label>Rotate
      <select onchange="state.panels[{index}].rotate=parseInt(this.value); send();">
        {_opt("0", str(cfg["rotate"]))}{_opt("180", str(cfg["rotate"]))}
      </select>
    </label>
  </div>
</div>"""


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
    """The exact frame just sent to the strip, for the control page's live
    pixel-grid emulator. A plain copy-under-lock -- this is a debug/preview
    aid, not the render loop's hot path, so simplicity wins over avoiding
    the small per-frame copy."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None

    def update(self, frame):
        with self.lock:
            self.frame = frame

    def snapshot(self):
        with self.lock:
            return self.frame


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

    def _diagram_html(self):
        snap = self.state.snapshot()
        return render_wiring_svg(self.panel_w, self.panel_h, snap["layout"], snap["panels"])

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", ""):
            self._send(self._render_page(), "text/html; charset=utf-8")
        elif path == "/diagram":
            self._send(self._diagram_html(), "text/html; charset=utf-8")
        elif path == "/panel-diagram":
            try:
                i = int(urllib.parse.parse_qs(query).get("i", ["-1"])[0])
                cfg = self.state.snapshot()["panels"][i]
            except (ValueError, IndexError):
                self.send_response(404)
                self.end_headers()
                return
            self._send(render_panel_svg(self.panel_w, self.panel_h, cfg),
                       "text/html; charset=utf-8")
        elif path == "/config.json":
            body = json.dumps(self.state.to_wire(), indent=2)
            self._send(body, "application/json", extra_headers={
                "Content-Disposition": 'attachment; filename="led-config.json"'})
        elif path == "/autoupdate":
            self._send(json.dumps({"enabled": autoupdate_status()}), "application/json")
        elif path == "/frame.json":
            frame = self.preview.snapshot() if self.preview else None
            if frame is None:
                body = json.dumps({"w": 0, "h": 0, "pixels": []})
            else:
                h, w = frame.shape[:2]
                body = json.dumps({"w": w, "h": h, "pixels": frame.reshape(-1, 3).tolist()})
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
        elif self.path == "/autoupdate":
            try:
                data = self._read_json()
            except (ValueError, TypeError):
                self.send_response(400)
                self.end_headers()
                return
            ok = set_autoupdate(bool(data.get("enabled")))
            self._send(json.dumps({"ok": ok, "enabled": autoupdate_status()}),
                       "application/json")
        else:
            self.send_response(404)
            self.end_headers()

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
        panel_rows = "".join(_panel_row(i, p, self.panel_w, self.panel_h)
                             for i, p in enumerate(snap["panels"]))
        queue_rows = "".join(_queue_item_row(item) for item in snap["queue"])
        au_status = autoupdate_status()
        au_html = ('<span style="color:#888">not installed -- see '
                   'scripts/install-led-matrix.sh</span>' if au_status is None else
                   f'<label><input type="checkbox" {"checked" if au_status else ""} '
                   f'onchange="toggleAutoupdate(this.checked)"> Auto-update from GitHub</label>')

        return f"""<!doctype html><meta charset="utf-8">
<title>LED matrix control</title>
<body style="font:16px monospace;background:#111;color:#eee;
             max-width:32rem;margin:2rem auto;padding:0 1rem">
<h1 style="font-size:1.1rem">LED matrix control</h1>

<p style="color:#888;font-size:.85rem;margin-bottom:.3rem">
  Live preview -- the actual pixels being sent to the strip right now:
</p>
<div id="preview" style="background:#000;border:1px solid #444;display:inline-block"></div>

<div id="diagram">{self._diagram_html()}</div>
<p style="color:#888;font-size:.85rem">
  Blue line traces the actual LED chain order -- green dot is where data
  comes in (DIN), orange is the end of the chain. Updates the instant you
  change anything below. {len(snap['panels'])} panel(s), {self.panel_w}x{self.panel_h}
  each (panel count/size are set with --panel-width/--panel-height/
  --num-panels at startup, not here).
</p>

<label>Text<br>
  <input value="{escape(snap['text'])}" style="width:100%;padding:.4rem"
    oninput="state.text=this.value; sendDebounced();">
</label><br><br>
<label>Color <input type="color" value="{rgb_to_hex(snap['color'])}"
  onchange="state.color=this.value; send();"></label>
&nbsp; <label><input type="checkbox" {"checked" if snap['bold'] else ""}
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
<br><span style="color:#888;font-size:.8rem">
  Direction is purely which axis it travels on; Stack letters is purely how
  it's drawn (one line vs one character per row) -- mix and match. Vertical
  travel and stacked text both need the full canvas, i.e. text-only mode.
  Rotate letters only applies when stacked: 270 faces right, 90 faces left.
</span>
<br><br>
<label>LED brightness: <span id="bval">{snap['brightness']}</span> / 255<br>
  <input type="range" min="0" max="255" value="{snap['brightness']}" style="width:100%"
    oninput="state.brightness=parseInt(this.value); bval.textContent=this.value; sendDebounced();">
</label><br><br>

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
</label><br><br>

<hr style="border-color:#333">
<label>Layout
  <select onchange="state.layout=this.value; send();">
    {_opt("horizontal", snap["layout"])}{_opt("vertical", snap["layout"])}
  </select>
</label>
<p style="color:#888;font-size:.85rem;margin-bottom:.3rem">Per-panel wiring:</p>
{panel_rows}

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
<a href="/config.json" download="led-config.json"
   style="display:inline-block;padding:.5rem 1rem;background:#234;color:#eee;
          text-decoration:none;border-radius:4px">Save config (JSON)</a>
&nbsp;
<label style="display:inline-block;padding:.5rem 1rem;background:#234;
              border-radius:4px;cursor:pointer">
  Load config (JSON)
  <input type="file" accept="application/json" style="display:none" onchange="loadConfig(this)">
</label>
<p style="margin-top:.8rem">{au_html}</p>
<p style="color:#888;font-size:.8rem">
  When on, the Pi checks GitHub every couple minutes and pulls + restarts
  automatically on new commits. Off just pauses the checks -- it doesn't
  touch whatever code is already running.
</p>

<script>
  const state = {initial_json};
  let debounceTimer;
  function send() {{
    fetch('/update', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                       body: JSON.stringify(state)}}).then(refreshDiagrams);
  }}
  function sendDebounced() {{
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(send, 200);
  }}
  function refreshDiagrams() {{
    fetch('/diagram').then(r => r.text()).then(html => {{
      document.getElementById('diagram').innerHTML = html;
    }});
    state.panels.forEach((p, i) => {{
      fetch('/panel-diagram?i=' + i).then(r => r.text()).then(html => {{
        const el = document.getElementById('panel-diagram-' + i);
        if (el) el.innerHTML = html;
      }});
    }});
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
  function toggleAutoupdate(enabled) {{
    fetch('/autoupdate', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                          body: JSON.stringify({{enabled}})}});
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
  const PREVIEW_CELL_PX = 14;
  function pollPreview() {{
    fetch('/frame.json').then(r => r.json()).then(data => {{
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
      for (let i = 0; i < data.pixels.length; i++) {{
        const [r, g, b] = data.pixels[i];
        cells[i].style.background = `rgb(${{r}},${{g}},${{b}})`;
      }}
    }}).catch(() => {{}}).finally(() => setTimeout(pollPreview, 150));
  }}
  pollPreview();
</script>
</body>"""


AUTOUPDATE_TIMER = "media-matrix-autoupdate.timer"


def autoupdate_status():
    """True/False if the systemd timer's state is known, None if systemd
    or the unit isn't there (e.g. testing off the Pi, or not installed yet)."""
    try:
        out = subprocess.run(["systemctl", "is-active", AUTOUPDATE_TIMER],
                              capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return None


def set_autoupdate(enabled):
    """enable/disable --now so the choice also survives a reboot."""
    try:
        subprocess.run(["systemctl", "enable" if enabled else "disable", "--now",
                         AUTOUPDATE_TIMER], capture_output=True, text=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


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


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--media", help="video file to loop")
    p.add_argument("--text", help="text to scroll")
    p.add_argument("--text-height", type=int, default=4,
                   help="rows for the text strip when --media is also given")
    p.add_argument("--font")
    p.add_argument("--font-size", type=int)
    p.add_argument("--text-color", default="255,255,255")
    p.add_argument("--bold", action="store_true")
    p.add_argument("--italic", action="store_true")
    p.add_argument("--scroll-speed", type=float, default=40.0,
                   help="pixels/second")
    p.add_argument("--text-direction", default="left",
                   choices=["left", "right", "up", "down"],
                   help="left/right: one line, slides sideways (marquee). "
                        "up/down: one upright character per row, slides "
                        "vertically -- needs real vertical room (text-only "
                        "mode) to look right")
    p.add_argument("--text-stacked", action="store_true",
                   help="one character per row (upright, or rotated with "
                        "--text-glyph-rotate) instead of one normal line -- "
                        "independent of --text-direction, which is purely "
                        "which axis it travels on")
    p.add_argument("--text-glyph-rotate", type=int, default=0, choices=[0, 90, 270],
                   help="--text-stacked only: rotate each character in "
                        "place -- 270 turns an upright letter to face "
                        "right, 90 to face left. Try both, whichever reads "
                        "correctly on your panel")
    p.add_argument("--rotate", type=int, default=0, choices=[0, 180],
                   help="per-panel startup seed: flip a panel mounted upside down")
    p.add_argument("--fit", default="fill", choices=["letterbox", "fill"])
    p.add_argument("--web-port", type=int, default=8098,
                   help="live control panel port, 0 to disable")
    p.add_argument("--upload-dir", default=None,
                   help="where uploaded media is saved (default: ./uploads "
                        "next to this script)")
    p.add_argument("--transition-s", type=float, default=0.6,
                   help="crossfade duration between queue items, seconds")
    p.add_argument("--panel-width", type=int, default=32)
    p.add_argument("--panel-height", type=int, default=8)
    p.add_argument("--num-panels", type=int, default=2)
    p.add_argument("--layout", default="horizontal", choices=["horizontal", "vertical"])
    p.add_argument("--serpentine", action=argparse.BooleanOptionalAction, default=True,
                   help="per-panel startup seed")
    p.add_argument("--serpentine-axis", default="row", choices=["row", "column"],
                   help="per-panel startup seed -- which direction a panel's "
                        "wiring snakes in; try 'column' if a horizontal "
                        "scroll bounces vertically")
    p.add_argument("--led-pin", type=int, default=18, help="BCM GPIO number")
    p.add_argument("--led-freq-hz", type=int, default=800000)
    p.add_argument("--led-dma", type=int, default=10)
    p.add_argument("--led-invert", action="store_true")
    p.add_argument("--led-channel", type=int, default=0)
    p.add_argument("--brightness", type=int, default=80, help="0-255, LED hardware level")
    p.add_argument("--media-brightness", type=float, default=100.0,
                   help="global software brightness for the media queue, "
                        "percent, 100=neutral")
    p.add_argument("--media-contrast", type=float, default=100.0,
                   help="global software contrast for the media queue, "
                        "percent, 100=neutral")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    if not args.media and not args.text and not args.web_port:
        sys.exit("nothing to show and no way to add anything -- pass "
                  "--media/--text, or leave --web-port enabled so you can "
                  "upload/type content once it's running")
    return args


def main():
    args = parse_args()

    def compute_geometry(snap):
        idx_map, canvas_w, canvas_h = build_index_map(
            args.panel_width, args.panel_height, snap["layout"], snap["panels"])
        has_media = bool(snap["queue"])
        has_text = bool(snap["text"])
        if has_media and has_text:
            text_h, video_h = args.text_height, canvas_h - args.text_height
        elif has_media:
            text_h, video_h = 0, canvas_h
        else:
            text_h, video_h = canvas_h, 0
        return idx_map, canvas_w, canvas_h, text_h, video_h

    state = State(args)
    snap0 = state.snapshot()
    idx_map, canvas_w, canvas_h, text_h, video_h = compute_geometry(snap0)

    if snap0["queue"] and video_h <= 0:
        sys.exit("--text-height leaves no room for video -- "
                  "shrink it or use a taller panel chain")

    upload_dir = args.upload_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    player = QueuePlayer(lambda: state.snapshot()["queue"], args.fit, args.transition_s)

    def rebuild_scroller(snap):
        if text_h <= 0 or not snap["text"]:
            return None
        font = load_font(args.font, args.font_size or max(8, text_h - 2),
                          bold=snap["bold"], italic=snap["italic"])
        return TextScroller(snap["text"], font, canvas_w, text_h, snap["color"],
                             snap["text_direction"], snap["text_stacked"],
                             snap["text_glyph_rotate"])

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
                  "the text/color/style fields won't show anything until "
                  "there's a queue item alongside the text, or text alone")

    num_pixels = args.panel_width * args.panel_height * args.num_panels
    strip = build_strip(args, num_pixels)
    flat_idx = idx_map.reshape(-1)

    frame_budget = 1.0 / 30.0
    scroll_offset = 0.0
    last_t = time.monotonic()
    rendered = 0
    last_report = last_t

    print("running -- ctrl-c to stop")
    try:
        while True:
            t0 = time.monotonic()
            dt = t0 - last_t
            last_t = t0

            snap = state.snapshot()
            if snap["version"] != built_version:
                new_idx_map, new_cw, new_ch, new_text_h, new_video_h = compute_geometry(snap)
                if snap["queue"] and new_video_h <= 0:
                    print("layout/panel change rejected -- leaves no room "
                          "for the media queue at this panel size")
                else:
                    idx_map, canvas_w, canvas_h = new_idx_map, new_cw, new_ch
                    text_h, video_h = new_text_h, new_video_h
                    flat_idx = idx_map.reshape(-1)
                scroller = rebuild_scroller(snap)
                built_version = snap["version"]

            frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
            if video_h > 0:
                frame[0:video_h] = player.next_frame(
                    dt, canvas_w, video_h, snap["media_brightness"], snap["media_contrast"])
            if scroller:
                sign = -1.0 if snap["text_direction"] in ("right", "down") else 1.0
                scroll_offset += sign * dt * snap["scroll_speed"]
                frame[canvas_h - text_h:canvas_h] = scroller.frame(scroll_offset)

            preview.update(frame)
            strip.setBrightness(snap["brightness"])
            flat_rgb = frame.reshape(-1, 3)
            for pixel_pos, led_idx in enumerate(flat_idx):
                r, g, b = flat_rgb[pixel_pos]
                strip.setPixelColor(int(led_idx), Color(int(r), int(g), int(b)))
            strip.show()
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
        for i in range(num_pixels):
            strip.setPixelColor(i, Color(0, 0, 0))
        strip.show()
        print("\nstopped")


if __name__ == "__main__":
    main()
