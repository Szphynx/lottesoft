#!/usr/bin/env python3
"""
Video + scrolling text on the fan wall: Raspberry Pi renders, Teensy 4.1
drives the LEDs.

    Pi --ethernet/UDP--> Teensy (teensy_globalxy_udp_control.ino)
                           --16 data pins--> Corsair RGB hubs, one per cluster
                                               --> 6 fans in series, 16 LEDs each

The wall's wiring is whatever the Teensy sketch says it is: this script
reads kMatrixWidth/kMatrixHeight/kCols/kRows/kLEDS_PER_PANEL and XYTable
straight out of the .ino at startup, so the two never drift. From that it
knows the 16-LED fan pattern, how many fans each pin carries, and the
default fan arrangement inside a cluster.

On top of that, calibration from the web page, two levels:
    Fan pattern     how the fans of ONE cluster sit (which chain position is
                    in which spot, plus per-fan rotation/mirror). Every
                    cluster follows it -- configure one, all follow.
    Cluster grid    which pin's cluster sits where in the wall. Grid size
                    (clusters across/down, fans per cluster across/down) is
                    set with the Apply button.
    Calibration     every cluster in its own colour, plus a count of white
                    LEDs per fan: "fans" counts the fan's chain position
                    (1..6), "clusters" the cluster's pin number (1..16).
                    Optionally one cluster highlighted. The preview labels
                    every cluster and fan -- make the wall match it. "grid": a hue/brightness
                    gradient with a white dot at the top of every fan -- any
                    break in the gradient or a dot off the top is a wrong
                    mapping.

Video/text/queue/web UI come from lottesoft's double_matrix.py (MAX7219
version), with the 1-bit output stage swapped for RGB-over-UDP.

Frame packets to the Teensy: b"F" + uint16 LE byte offset + RGB bytes, in
LED-buffer order (pin 0's 96 LEDs first). The Teensy shows the frame when
the last chunk lands.

Run with:
    sudo python3 fan_wall.py                     # web UI on :8099
    sudo python3 fan_wall.py --media clip.mp4 --text "hello"

Needs: python3-opencv python3-numpy python3-pil fonts-dejavu-core
(scripts/install-fan-wall.sh does all of it plus the systemd service).
"""

import argparse
import colorsys
import http.server
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from html import escape

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Per-frame decay at full ("wet") trail -- a pixel fades to ~10% in about a
# second at the 20-30fps the loop runs at.
TRAIL_MAX_DECAY = 0.89

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


# Tried, in order, for any character the primary font doesn't actually
# have -- kaomoji and similar mixed-script text pull in Japanese kana that
# a Latin-focused font like DejaVu doesn't cover. Any/all of these can be
# missing (just fewer fallbacks); only the primary font is required.
FALLBACK_FONT_PATHS = [
    "/usr/share/fonts/truetype/vlgothic/VL-Gothic-Regular.ttf",  # apt: fonts-vlgothic
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",    # apt: fonts-noto-cjk
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",       # some distros symlink this
]


def load_fallback_fonts(size):
    fonts = []
    for path in FALLBACK_FONT_PATHS:
        try:
            fonts.append(ImageFont.truetype(path, size))
        except OSError:
            continue
    return fonts


def _render_char(font, ch, dummy):
    bbox = dummy.textbbox((0, 0), ch, font=font)
    w, h = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).text((-bbox[0], -bbox[1]), ch, font=font, fill=255)
    return np.array(img)


def glyph_font_for(ch, fonts, notdef_sigs, dummy):
    """First font in `fonts` that actually has a real glyph for `ch`, not
    just its own "tofu" placeholder for anything it can't render -- PIL
    doesn't fall back between fonts on its own, so this picks per
    character. `notdef_sigs` is each font's own rendering of a
    guaranteed-unmapped private-use codepoint: whatever a font draws for
    that IS its tofu box, so comparing a real character's rendering
    against it (not just checking that *something* got drawn -- the tofu
    box itself is plenty of ink) is how a missing glyph is actually
    detected. Falls back to the last font if none of them have it (still
    tofu, but no crash)."""
    if ch.isspace():
        return fonts[0]
    for font in fonts:
        arr = _render_char(font, ch, dummy)
        sig = notdef_sigs[id(font)]
        if arr.shape != sig.shape or not np.array_equal(arr, sig):
            return font
    return fonts[-1]


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



def apply_trail(trail, gray, wet):
    """Blend the current frame into a persistent trail buffer for the
    "echo" effect: 0 (dry) makes decay 0, so trail = max(0, gray) = gray
    exactly -- a fresh pixel always wins immediately, nothing lingers,
    not a special case. Higher `wet` raises the decay, so already-lit
    pixels fade out instead of cutting off the instant content moves past
    them -- a soft trail behind whatever's scrolling. Returns the new
    trail buffer (float32); render it with `.astype(np.uint8)`."""
    decay = (wet / 100.0) * TRAIL_MAX_DECAY
    return np.maximum(trail * decay, gray.astype(np.float32))


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
    (with `glyph_rotate` additionally rotating each character in place).

    `fonts` is the primary font plus any fallback fonts (see
    load_fallback_fonts) -- every character is looked up in each font in
    turn and drawn with the first one that actually has it, since PIL
    doesn't do that fallback on its own within a single draw call."""

    def __init__(self, text, fonts, out_w, out_h, color, direction="left",
                 stacked=False, glyph_rotate=0):
        self.direction = direction
        self.out_w, self.out_h = out_w, out_h
        horizontal = direction in ("left", "right")
        dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        notdef_sigs = {id(f): _render_char(f, "", dummy) for f in fonts}

        def font_for(ch):
            return glyph_font_for(ch, fonts, notdef_sigs, dummy)

        def layout_glyphs(chars):
            """Each character rendered with whichever font actually has
            it, placed at its own natural advance width (side bearings
            included) so mixed-font text still spaces out like real
            text, not like tofu-tight-bbox packing."""
            data, pen_x, max_h = [], 0.0, 1
            for ch in chars:
                f = font_for(ch)
                bbox = dummy.textbbox((0, 0), ch, font=f)
                cw, chh = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
                glyph = Image.new("RGB", (cw, chh), (0, 0, 0))
                ImageDraw.Draw(glyph).text((-bbox[0], -bbox[1]), ch, font=f, fill=color)
                data.append((glyph, bbox[0], pen_x))
                pen_x += f.getlength(ch)
                max_h = max(max_h, chh)
            return data, pen_x, max_h

        if not stacked:
            chars = list(text) if text else [" "]
            glyph_data, pen_w, max_h = layout_glyphs(chars)
            if horizontal:
                content_w, content_h = max(1, round(pen_w)), out_h
                img = Image.new("RGB", (content_w, content_h), (0, 0, 0))
                for glyph, left_bearing, gx in glyph_data:
                    gy = (out_h - glyph.height) // 2
                    img.paste(glyph, (round(gx + left_bearing), gy))
            else:
                content_w, content_h = out_w, max_h
                img = Image.new("RGB", (content_w, content_h), (0, 0, 0))
                x0 = (out_w - round(pen_w)) // 2
                for glyph, left_bearing, gx in glyph_data:
                    img.paste(glyph, (round(x0 + gx + left_bearing), 0))
        else:
            chars = list(text) if text else [" "]
            ascent, descent = fonts[0].getmetrics()
            line_h = max(1, ascent + descent)
            glyphs = []
            for ch in chars:
                f = font_for(ch)
                bbox = dummy.textbbox((0, 0), ch, font=f)
                cw, chh = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
                glyph = Image.new("RGB", (cw, chh), (0, 0, 0))
                ImageDraw.Draw(glyph).text((-bbox[0], -bbox[1]), ch, font=f, fill=color)
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



def chain_labels(order):
    """order[c] = which grid slot chain position c drives. Inverted: for
    each grid slot, the 1-based chain position feeding it -- i.e. the number
    that slot's module shows in calibration mode's counterpart, and the
    label on both the diagram and the live preview overlay."""
    labels = [0] * len(order)
    for c, slot in enumerate(order):
        labels[slot] = c + 1
    return labels



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



# ---- wall geometry, read from the Teensy sketch -----------------------------

LEDS_PER_FAN = 16  # Corsair LL-style fan: 4 inner + 12 outer LEDs
NO_LED = 65535

# Calibration colour per cluster (pin), golden-ratio hue steps so chain and
# grid neighbours never look alike. Same list drives the wall and the page.
CLUSTER_COLORS = [tuple(round(v * 255) for v in colorsys.hsv_to_rgb(i * 0.618 % 1, 1, 1))
                  for i in range(32)]


def load_sketch(path):
    """Everything the wiring already says, read out of the .ino: one panel
    (= one cluster = one pin) is kMatrixWidth x kMatrixHeight cells,
    kCols x kRows of them, kLEDS_PER_PANEL LEDs each, and XYTable says
    which LED of the pin's chain sits in which cell. From that: the fan
    cell size, the 16-LED ring pattern inside it, and where each fan of
    the chain sits in the cluster."""
    with open(path, encoding="utf-8") as f:
        src = f.read()

    def const(name):
        return int(re.search(rf"\b{name}\b\s*=?\s*(\d+)", src).group(1))

    body = re.search(r"XYTable\[\]\s*=\s*\{(.*?)\}", src, re.S).group(1)
    table = [int(v) for v in re.findall(r"\d+", body)]
    mw, mh = const("kMatrixWidth"), const("kMatrixHeight")
    leds_per_pin = const("kLEDS_PER_PANEL")
    fans = leds_per_pin // LEDS_PER_FAN

    cells = {}
    for k, j in enumerate(table):
        if j != NO_LED:
            cells[divmod(j, LEDS_PER_FAN)] = (k % mw, k // mw)
    origin = [(min(cells[f, i][0] for i in range(LEDS_PER_FAN)),
               min(cells[f, i][1] for i in range(LEDS_PER_FAN))) for f in range(fans)]
    cluster_w = len({ox for ox, _ in origin})
    cluster_h = fans // cluster_w
    p = mw // cluster_w
    if p * cluster_w != mw or p * cluster_h != mh:
        sys.exit(f"{path}: XYTable fans don't tile the {mw}x{mh} panel in square cells")

    fan_order, ring = [], None
    for f in range(fans):
        col, row = origin[f][0] // p, origin[f][1] // p
        fan_order.append(row * cluster_w + col)
        r = [(cells[f, i][0] - col * p, cells[f, i][1] - row * p) for i in range(LEDS_PER_FAN)]
        if ring is None:
            ring = r
        elif r != ring:
            sys.exit(f"{path}: fan {f + 1} has a different LED pattern than fan 1")

    ip = ".".join(re.search(r"teensyIP\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", src).groups())
    return dict(teensy_ip=ip, teensy_port=const("LISTEN_PORT"),
                pins=const("kCols") * const("kRows"), leds_per_pin=leds_per_pin,
                fans=fans, fan_px=p, ring=ring, cluster_w=cluster_w, cluster_h=cluster_h,
                fan_order=fan_order, grid_w=const("kCols"), grid_h=const("kRows"))


def canvas_size(geo, snap):
    p = geo["fan_px"]
    return snap["grid_w"] * snap["cluster_w"] * p, snap["grid_h"] * snap["cluster_h"] * p


def ring_cell(x, y, p, rot, mirror):
    if mirror:
        x = p - 1 - x
    for _ in range(rot // 90):
        x, y = p - 1 - y, x
    return x, y


def build_led_map(geo, snap):
    """LED-buffer index -> canvas (x, y); -1 where that LED shows nothing
    (pins past the grid, or past `active`). Chain side: pin c, fan f,
    LED i is buffer index (c * leds_per_pin + f * 16 + i), the same order
    the Teensy's leds[] has. Wall side: cluster_order puts pin c at a grid
    slot, fan_order puts fan f at a slot inside the cluster, fan_rot/
    fan_mirror turn the ring -- the same fan pattern for every cluster."""
    p, per_pin = geo["fan_px"], geo["leds_per_pin"]
    cw, ch, gw = snap["cluster_w"], snap["cluster_h"], snap["grid_w"]
    pos = np.full((geo["pins"] * per_pin, 2), -1, dtype=np.int32)
    for c, slot in enumerate(snap["cluster_order"][:snap["active"]]):
        gy, gx = divmod(slot, gw)
        for f, fslot in enumerate(snap["fan_order"]):
            fy, fx = divmod(fslot, cw)
            ox, oy = (gx * cw + fx) * p, (gy * ch + fy) * p
            for i, (x, y) in enumerate(geo["ring"]):
                x, y = ring_cell(x, y, p, snap["fan_rot"][f], snap["fan_mirror"][f])
                pos[c * per_pin + f * LEDS_PER_FAN + i] = (ox + x, oy + y)
    return pos


def sample_canvas(canvas, pos):
    buf = np.zeros((len(pos), 3), dtype=np.uint8)
    used = pos[:, 0] >= 0
    buf[used] = canvas[pos[used, 1], pos[used, 0]]
    return buf


def chain_test_buffer(geo, snap, count):
    """Chain-space pattern, no mapping involved -- what's on the wire:
    every fan in its cluster's colour (half level), plus a count of white LEDs
    starting at LED 0 in wiring order. count="fan": N = the fan's position
    on the chain (1..6); count="cluster": N = the cluster's pin number
    (1..16). The white run also shows where each ring starts and which way
    it's wired. With a cluster picked, only that pin is bright. The preview
    draws this through the current mapping -- when the wall matches the
    preview, wiring and clustering are right."""
    buf = np.zeros((geo["pins"], geo["fans"], LEDS_PER_FAN, 3), dtype=np.uint8)
    for c in range(geo["pins"]):
        buf[c] = [v // 2 for v in CLUSTER_COLORS[c]]
        for f in range(geo["fans"]):
            n = f + 1 if count == "fan" else c + 1
            buf[c, f, :n] = 255
    if snap["cal_cluster"] >= 0:
        dim = np.arange(geo["pins"]) != snap["cal_cluster"]
        buf[dim] //= 8
    buf[snap["active"]:] = 0
    return buf.reshape(-1, 3)


def grid_test_canvas(w, h, p):
    """Canvas-space pattern: hue runs left to right, brightness top to
    bottom, white dot on the top LED of every fan. A cluster in the wrong
    slot breaks the gradient; a fan turned wrong has its dot off the top."""
    hsv = np.zeros((h, w, 3), dtype=np.uint8)
    hsv[..., 0] = (np.arange(w) * 150 // max(1, w - 1))[None, :]
    hsv[..., 1] = 255
    hsv[..., 2] = np.linspace(255, 40, h).astype(np.uint8)[:, None]
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    rgb[0::p, p // 2::p] = 255
    return rgb


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


# ---- live state ------------------------------------------------------------

def _clamp(data, key, lo, hi, cast=float):
    try:
        return max(lo, min(hi, cast(data[key])))
    except (KeyError, TypeError, ValueError):
        return None


class State:
    """Live-editable settings shared between the render loop and the web
    server. `version` bumps only on changes that need a rebuild (text
    bitmap, canvas size); the LED map is rebuilt whenever its own inputs
    change (see map_key in main)."""

    def __init__(self, args, geo, state_file=None):
        self.lock = threading.Lock()
        self.geo = geo
        self.state_file = state_file
        self.text = args.text or ""
        self.color = hex_to_rgb(args.text_color)
        self.bold = args.bold
        self.italic = args.italic
        self.scroll_speed = args.scroll_speed
        self.text_direction = args.text_direction
        self.text_stacked = False
        self.text_glyph_rotate = 0
        self.brightness = args.brightness
        self.trail_wet = 0
        self.media_brightness = 100.0
        self.media_contrast = 100.0
        self.media_rotation = 0.0
        self.media_scale = 100.0
        self.media_pos_x = 0
        self.media_pos_y = 0
        self.grid_w, self.grid_h = geo["grid_w"], geo["grid_h"]
        self.cluster_w, self.cluster_h = geo["cluster_w"], geo["cluster_h"]
        self.fan_order = list(geo["fan_order"])
        self.fan_rot = [0] * geo["fans"]
        self.fan_mirror = [False] * geo["fans"]
        self.cluster_order = list(range(self.grid_w * self.grid_h))
        self.active = len(self.cluster_order)
        self.rotate180 = False
        self.calibrate = "off"
        self.cal_cluster = -1
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
        """Persist so `_load` restores it next boot. Called when "Save
        config" is pressed, not on every edit."""
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
            snap = {k: v for k, v in vars(self).items() if k not in ("lock", "geo", "state_file")}
            for k in ("fan_order", "fan_rot", "fan_mirror", "cluster_order"):
                snap[k] = list(snap[k])
            snap["queue"] = [dict(q) for q in self.queue]
            return snap

    def to_wire(self):
        snap = self.snapshot()
        del snap["version"]
        snap["color"] = "#%02x%02x%02x" % snap["color"]
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
        """Bulk-update from a JSON dict -- the page's live edits or a loaded
        config file. Missing or invalid fields keep their current value."""
        geo = self.geo
        with self.lock:
            rebuild = False
            for key, cast in (("text", str), ("bold", bool), ("italic", bool),
                              ("text_stacked", bool)):
                if key in data and cast(data[key]) != getattr(self, key):
                    setattr(self, key, cast(data[key]))
                    rebuild = True
            if "color" in data:
                try:
                    color = hex_to_rgb(str(data["color"]))
                    if color != self.color:
                        self.color, rebuild = color, True
                except ValueError:
                    pass
            if data.get("text_direction") in ("left", "right", "up", "down") \
                    and data["text_direction"] != self.text_direction:
                self.text_direction, rebuild = data["text_direction"], True
            if str(data.get("text_glyph_rotate")) in ("0", "90", "270") \
                    and int(data["text_glyph_rotate"]) != self.text_glyph_rotate:
                self.text_glyph_rotate, rebuild = int(data["text_glyph_rotate"]), True

            for key, lo, hi, cast in (("scroll_speed", 0, 500, float), ("brightness", 0, 255, int),
                                      ("trail_wet", 0, 100, int),
                                      ("media_brightness", 0, 200, float),
                                      ("media_contrast", 0, 200, float),
                                      ("media_rotation", -180, 180, float),
                                      ("media_scale", 10, 400, float),
                                      ("media_pos_x", -9999, 9999, int),
                                      ("media_pos_y", -9999, 9999, int),
                                      ("cal_cluster", -1, geo["pins"] - 1, int)):
                v = _clamp(data, key, lo, hi, cast)
                if v is not None:
                    setattr(self, key, v)
            if "rotate180" in data:
                self.rotate180 = bool(data["rotate180"])
            if data.get("calibrate") in ("off", "fans", "clusters", "grid"):
                self.calibrate = data["calibrate"]

            # Grid shape -- only as a full set, from the Apply button. Fans
            # per cluster and the pin count are the sketch's, not ours.
            shape_keys = ("grid_w", "grid_h", "cluster_w", "cluster_h")
            if all(k in data for k in shape_keys):
                try:
                    gw, gh, cw, ch = (int(data[k]) for k in shape_keys)
                except (TypeError, ValueError):
                    gw = 0
                if gw > 0 and gh > 0 and cw > 0 and ch > 0 and cw * ch == geo["fans"] \
                        and gw * gh <= geo["pins"] \
                        and (gw, gh, cw, ch) != (self.grid_w, self.grid_h, self.cluster_w, self.cluster_h):
                    if gw * gh != len(self.cluster_order):
                        self.cluster_order = list(range(gw * gh))
                        self.active = gw * gh
                    self.grid_w, self.grid_h, self.cluster_w, self.cluster_h = gw, gh, cw, ch
                    rebuild = True

            # Orders only take a complete permutation -- a half-finished
            # edit never scrambles the wall.
            for key in ("fan_order", "cluster_order"):
                try:
                    cand = [int(v) for v in data[key]]
                except (KeyError, TypeError, ValueError):
                    continue
                if sorted(cand) == list(range(len(getattr(self, key)))):
                    setattr(self, key, cand)
            try:
                rot = [int(v) for v in data["fan_rot"]]
                if len(rot) == geo["fans"] and all(r in (0, 90, 180, 270) for r in rot):
                    self.fan_rot = rot
            except (KeyError, TypeError, ValueError):
                pass
            try:
                mir = [bool(v) for v in data["fan_mirror"]]
                if len(mir) == geo["fans"]:
                    self.fan_mirror = mir
            except (KeyError, TypeError):
                pass
            v = _clamp(data, "active", 1, len(self.cluster_order), int)
            if v is not None:
                self.active = v

            if "queue" in data and isinstance(data["queue"], list):
                by_id = {item["id"]: item for item in self.queue}
                new_queue, seen_ids = [], set()
                for entry in data["queue"]:
                    iid = entry.get("id")
                    if iid not in by_id:
                        continue  # only /upload creates new items, ignore fabricated ones
                    seen_ids.add(iid)
                    cur = by_id[iid]
                    updated = dict(cur)
                    for key, lo, hi in (("start", 0, 1e9), ("brightness", 0, 200), ("contrast", 0, 200)):
                        v = _clamp(entry, key, lo, hi)
                        if v is not None:
                            updated[key] = v
                    if "end" in entry:
                        updated["end"] = (None if entry["end"] in (None, "", "null")
                                          else _clamp(entry, "end", 0, 1e9))
                    if "loop" in entry:
                        updated["loop"] = bool(entry["loop"])
                    if updated != cur:
                        rebuild = True
                    new_queue.append(updated)
                if seen_ids != set(by_id):
                    rebuild = True  # an item was removed
                self.queue = new_queue
            if rebuild:
                self.version += 1


# ---- Teensy link -----------------------------------------------------------

class TeensyLink:
    # ponytail: fixed chunk size, 4 packets per 1536-LED frame; lower it if
    # blink_control.py --test shows the Teensy dropping packets.
    CHUNK = 1152

    def __init__(self, ip, port):
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.warned = False

    def send_frame(self, data):
        try:
            for off in range(0, len(data), self.CHUNK):
                self.sock.sendto(b"F" + struct.pack("<H", off) + data[off:off + self.CHUNK],
                                 self.addr)
            self.warned = False
        except OSError as e:
            if not self.warned:  # cable out / wrong subnet: say so once, keep rendering
                print(f"teensy link: {e}")
                self.warned = True


# ---- web control -----------------------------------------------------------

class Preview:
    """The LED buffer just sent (before brightness) plus where each LED
    sits on the canvas, for the page's fan-ring emulator."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None

    def update(self, buf, pos, w, h):
        with self.lock:
            self.data = (buf, pos, w, h)

    def snapshot(self):
        with self.lock:
            return self.data


class ControlHandler(http.server.BaseHTTPRequestHandler):
    state = geo = upload_dir = preview = None  # bound by make_control_server

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
        return json.loads(self.rfile.read(length).decode() if length else "{}")

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path in ("/", ""):
            self._send(self._render_page(), "text/html; charset=utf-8")
        elif path == "/config.json":
            self.state.save()
            self._send(json.dumps(self.state.to_wire(), indent=2), "application/json",
                       extra_headers={"Content-Disposition": 'attachment; filename="fan-wall-config.json"'})
        elif path == "/frame.json":
            self._send(json.dumps(self._frame_data()), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _frame_data(self):
        snap = self.state.snapshot()
        p = self.geo["fan_px"]
        out = {"p": p, "grid_w": snap["grid_w"], "grid_h": snap["grid_h"],
               "cw": snap["cluster_w"] * p, "ch": snap["cluster_h"] * p,
               "cluster_w": snap["cluster_w"], "calibrate": snap["calibrate"],
               "clusters": chain_labels(snap["cluster_order"]), "active": snap["active"],
               "fans": chain_labels(snap["fan_order"]),
               "fan_rot": snap["fan_rot"], "fan_mirror": snap["fan_mirror"],
               "cal_cluster": snap["cal_cluster"], "w": 0, "h": 0, "leds": []}
        data = self.preview.snapshot()
        if data:
            buf, pos, out["w"], out["h"] = data
            used = pos[:, 0] >= 0
            out["leds"] = np.concatenate([pos[used], buf[used]], axis=1).reshape(-1).tolist()
        return out

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
            self._send("restarting", "text/plain")

            def restart_after_reply():  # the restart kills this process -- reply first
                time.sleep(0.3)
                subprocess.Popen(["sudo", "systemctl", "restart", "fan-wall"])

            threading.Thread(target=restart_after_reply, daemon=True).start()
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
        dest = os.path.join(self.upload_dir, f"{uuid.uuid4().hex[:8]}{ext}")
        with open(dest, "wb") as f:
            f.write(content)
        self.state.add_media(dest, kind, filename)
        self._send(_queue_item_row(self.state.snapshot()["queue"][-1]), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass

    def _render_page(self):
        snap = self.state.snapshot()
        g = self.geo
        info = dict(pins=g["pins"], fans=g["fans"], sketch_fan_order=g["fan_order"],
                    colors=["#%02x%02x%02x" % c for c in CLUSTER_COLORS])
        return (PAGE.replace("__STATE__", json.dumps(self.state.to_wire()).replace("</", "<\\/"))
                    .replace("__GEO__", json.dumps(info))
                    .replace("__QUEUE__", "".join(_queue_item_row(i) for i in snap["queue"]))
                    .replace("__SKETCH__", f"{g['pins']} pins x {g['fans']} fans x {LEDS_PER_FAN} LEDs"))


PAGE = r"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fan wall control</title>
<style>
  body{font:15px monospace;background:#111;color:#eee;max-width:40rem;margin:1.5rem auto;padding:0 1rem}
  h2{font-size:1rem;margin:1.4rem 0 .4rem;border-top:1px solid #333;padding-top:1rem}
  .note{color:#888;font-size:.8rem} label{display:block;margin:.35rem 0}
  input[type=range]{width:100%} button,.btn{background:#234;color:#eee;border:none;border-radius:4px;
  padding:.35rem .7rem;cursor:pointer;display:inline-block} input[type=number]{width:4rem}
  .tiles{display:inline-grid;gap:4px;margin:.4rem 0}
  .tile{position:relative;width:64px;height:44px;box-sizing:border-box;border:2px solid #6cf;border-radius:4px;
  background:#123;cursor:grab;display:flex;align-items:center;justify-content:center;user-select:none;font:bold 16px monospace}
  .chip{position:absolute;top:1px;cursor:pointer;font:9px monospace;padding:0 3px;background:rgba(255,255,255,.15)}
</style>
<h1 style="font-size:1.1rem">Fan wall
  <span id="svc" style="font-size:.6rem;padding:.15rem .5rem;border-radius:3px;background:#2a4;color:#012">ONLINE</span></h1>
<button onclick="restartService()" style="background:#622;color:#fdd">restart service</button>
<span id="restartMsg" class="note"></span>
<p class="note">Live preview -- every fan's 16 LEDs as sent to the Teensy (__SKETCH__), clusters outlined with their pin number.</p>
<canvas id="preview" style="background:#000;border:1px solid #444;max-width:100%"></canvas>

<h2>Calibration</h2>
<label>Mode <select data-k="calibrate">
  <option value="off">off -- show media/text</option>
  <option value="fans">fans -- cluster colours, white LEDs = fan number (1-6)</option>
  <option value="clusters">clusters -- cluster colours, white LEDs = cluster number</option>
  <option value="grid">grid -- gradient + white dot on top of every fan</option></select></label>
<label>Highlight cluster <select data-k="cal_cluster" data-num id="calCluster"></select></label>
<p class="note">Each cluster has its own colour; the white LEDs count up from LED 0 in wiring order, so the
count is the number and where it starts shows how the ring is turned. Same colours and numbers on the
preview and the tiles below. <span id="legend"></span></p>
<p class="note">1. <b>Fan pattern</b> (one cluster, every cluster follows): in "fans" mode, drag tiles so each
fan number sits where it is on the wall. <b>R</b> turns that fan 90&deg;, <b>M</b> mirrors it -- until the white
run starts and turns the same way as on the preview.</p>
<div id="fanTiles" class="tiles"></div>
<button onclick="resetFans()">reset to sketch</button>
<p class="note">2. <b>Cluster grid</b>: drag tiles until each pin's cluster sits where it is on the wall.
Click a tile to highlight that cluster.</p>
<div id="clusterTiles" class="tiles"></div>
<button onclick="state.cluster_order=state.cluster_order.map((_,i)=>i);send()">reset clusters</button>
<p class="note">3. <b>Grid size</b> -- clusters across x down, fans per cluster across x down
(fans per cluster and pin count come from the sketch).</p>
<label>Clusters <input type="number" id="gw" min="1"> x <input type="number" id="gh" min="1">
&nbsp; Fans/cluster <input type="number" id="cw" min="1"> x <input type="number" id="ch" min="1">
<button onclick="applyGrid()">Apply</button> <span id="gridMsg" class="note"></span></label>
<label>Active clusters <span id="v-active"></span><input type="range" data-k="active" min="1" id="activeRange"></label>
<label><input type="checkbox" data-k="rotate180"> Whole wall mounted upside down</label>

<h2>Text</h2>
<label><input type="text" data-k="text" style="width:100%;padding:.4rem"></label>
<label>Colour <input type="color" data-k="color"> &nbsp;
  <input type="checkbox" data-k="bold" style="display:inline"> Bold
  <input type="checkbox" data-k="italic" style="display:inline"> Italic</label>
<label>Scroll speed <span id="v-scroll_speed"></span> px/s<input type="range" data-k="scroll_speed" min="0" max="200"></label>
<label>Direction <select data-k="text_direction"><option>left</option><option>right</option><option>up</option><option>down</option></select>
  &nbsp; <input type="checkbox" data-k="text_stacked" style="display:inline"> Stack letters
  &nbsp; Rotate letters <select data-k="text_glyph_rotate" data-num><option>0</option><option>90</option><option>270</option></select></label>
<label>Trail <span id="v-trail_wet"></span><input type="range" data-k="trail_wet" min="0" max="100"></label>

<h2>Media</h2>
<label>Brightness <span id="v-media_brightness"></span>%<input type="range" data-k="media_brightness" min="0" max="200"></label>
<label>Contrast <span id="v-media_contrast"></span>%<input type="range" data-k="media_contrast" min="0" max="200"></label>
<label>Rotation <span id="v-media_rotation"></span>&deg;<input type="range" data-k="media_rotation" min="-180" max="180"></label>
<label>Scale <span id="v-media_scale"></span>%<input type="range" data-k="media_scale" min="10" max="400"></label>
<label>Position X <span id="v-media_pos_x"></span><input type="range" data-k="media_pos_x" min="-100" max="100"></label>
<label>Position Y <span id="v-media_pos_y"></span><input type="range" data-k="media_pos_y" min="-100" max="100"></label>
<label class="btn">+ Add image/video<input type="file" accept="video/*,image/*" style="display:none" onchange="uploadFile(this)"></label>
<div id="queue">__QUEUE__</div>

<h2>Output</h2>
<label>Brightness <span id="v-brightness"></span>/255<input type="range" data-k="brightness" min="0" max="255"></label>
<p class="note">Software level, on top of the sketch's FastLED.setBrightness cap.</p>
<a href="/config.json" download="fan-wall-config.json" class="btn" style="text-decoration:none">Save config</a>
<label class="btn" style="display:inline-block">Load config<input type="file" accept="application/json" style="display:none" onchange="loadConfig(this)"></label>
<p class="note">Save also makes it the config the wall resumes with after a reboot.</p>

<script>
const state = __STATE__;
const GEO = __GEO__;
let debounceTimer;
function send() {
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(state)});
}
function sendDebounced() { clearTimeout(debounceTimer); debounceTimer = setTimeout(send, 200); }

// Every [data-k] control is bound to state[k]; a matching #v-k shows its value.
const calSel = document.getElementById('calCluster');
calSel.innerHTML = '<option value="-1">all</option>' +
  Array.from({length: GEO.pins}, (_, i) => `<option value="${i}">${i + 1}</option>`).join('');
document.getElementById('activeRange').max = state.cluster_order.length;
document.querySelectorAll('[data-k]').forEach(el => {
  const k = el.dataset.k, out = document.getElementById('v-' + k);
  if (el.type === 'checkbox') el.checked = state[k]; else el.value = state[k];
  if (out) out.textContent = state[k];
  const live = el.type === 'range' || el.type === 'text' || el.type === 'color';
  el.addEventListener(live ? 'input' : 'change', () => {
    let v = el.type === 'checkbox' ? el.checked : el.value;
    if (el.type === 'range' || 'num' in el.dataset) v = parseFloat(v);
    state[k] = v;
    if (out) out.textContent = v;
    live ? sendDebounced() : send();
  });
});
['gw', 'gh', 'cw', 'ch'].forEach((id, i) =>
  document.getElementById(id).value = state[['grid_w', 'grid_h', 'cluster_w', 'cluster_h'][i]]);
document.getElementById('legend').innerHTML = GEO.colors.slice(0, GEO.pins)
  .map((c, i) => `<span style="color:${c}">&#9679;${i + 1}</span>`).join(' ');

function applyGrid() {
  const v = ['gw', 'gh', 'cw', 'ch'].map(id => parseInt(document.getElementById(id).value));
  if (v[2] * v[3] !== GEO.fans) return gridMsg.textContent = `fans/cluster must multiply to ${GEO.fans}`;
  if (v[0] * v[1] > GEO.pins) return gridMsg.textContent = `at most ${GEO.pins} clusters (one per pin)`;
  [state.grid_w, state.grid_h, state.cluster_w, state.cluster_h] = v;
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(state)}).then(() => location.reload());
}
function resetFans() {
  state.fan_order = GEO.sketch_fan_order.slice();
  state.fan_rot = state.fan_rot.map(() => 0);
  state.fan_mirror = state.fan_mirror.map(() => false);
  send();
}
function loadConfig(input) {
  const file = input.files[0];
  if (!file) return;
  file.text().then(text => fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: text}).then(() => location.reload()));
}
function setItem(id, key, value) {
  const item = state.queue.find(q => q.id === id);
  if (item) item[key] = value;
  send();
}
function removeItem(id) {
  state.queue = state.queue.filter(q => q.id !== id);
  const row = document.getElementById('qi-' + id);
  if (row) row.remove();
  send();
}
function uploadFile(input) {
  const file = input.files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  fetch('/upload', {method: 'POST', body}).then(r => {
    if (!r.ok) return r.text().then(msg => alert('Upload failed: ' + msg));
    return r.text().then(html => {
      document.getElementById('queue').insertAdjacentHTML('beforeend', html);
      return fetch('/config.json').then(r2 => r2.json()).then(s => { state.queue = s.queue; });
    });
  });
  input.value = '';
}

// Drag-to-swap tile grid (from double_matrix): labels[slot] = 1-based chain
// position sitting in that slot; state[key][chainPos] = slot.
let dragging = null;
function renderTiles(elId, key, cols, labels, decorate) {
  const el = document.getElementById(elId);
  if (dragging) return;
  const sig = JSON.stringify([cols, labels, decorate.sig]);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.innerHTML = '';
  el.style.gridTemplateColumns = `repeat(${cols}, 64px)`;
  labels.forEach((chainPos, slot) => {
    const t = document.createElement('div');
    t.className = 'tile';
    t.draggable = true;
    t.textContent = chainPos;
    decorate(t, chainPos - 1);
    t.addEventListener('dragstart', () => { dragging = {elId, slot}; t.style.opacity = '.4'; });
    t.addEventListener('dragend', () => { dragging = null; t.style.opacity = '1'; });
    t.addEventListener('dragover', e => e.preventDefault());
    t.addEventListener('drop', e => {
      e.preventDefault();
      if (!dragging || dragging.elId !== elId || dragging.slot === slot) return;
      const order = state[key].slice();
      order[labels[dragging.slot] - 1] = slot;
      order[chainPos - 1] = dragging.slot;
      state[key] = order;
      dragging = null;
      send();
    });
    el.appendChild(t);
  });
}
function chip(tile, text, on, right, onclick) {
  const b = document.createElement('span');
  b.className = 'chip';
  b.textContent = text;
  b.style.right = right;
  if (on) { b.style.background = '#6cf'; b.style.color = '#012'; }
  b.onclick = e => { e.stopPropagation(); onclick(); };
  tile.appendChild(b);
}
function renderCalibration(d) {
  const fanDeco = (t, f) => {
    chip(t, 'R' + (d.fan_rot[f] || ''), d.fan_rot[f], '14px',
      () => { state.fan_rot[f] = (state.fan_rot[f] + 90) % 360; send(); });
    chip(t, 'M', d.fan_mirror[f], '1px', () => { state.fan_mirror[f] = !state.fan_mirror[f]; send(); });
  };
  fanDeco.sig = [d.fan_rot, d.fan_mirror];
  renderTiles('fanTiles', 'fan_order', d.cluster_w, d.fans, fanDeco);
  const clDeco = (t, c) => {
    t.style.borderColor = t.style.color = GEO.colors[c];
    if (c + 1 > d.active) { t.style.borderStyle = 'dashed'; t.style.opacity = '.4'; }
    if (c === d.cal_cluster) t.style.background = '#346';
    t.onclick = () => {
      state.cal_cluster = state.cal_cluster === c ? -1 : c;
      calSel.value = state.cal_cluster;
      send();
    };
  };
  clDeco.sig = [d.active, d.cal_cluster];
  renderTiles('clusterTiles', 'cluster_order', d.grid_w, d.clusters, clDeco);
}

const S = 6;  // preview px per canvas cell
function drawPreview(d) {
  const cv = document.getElementById('preview');
  if (cv.width !== d.w * S || cv.height !== d.h * S) { cv.width = d.w * S; cv.height = d.h * S; }
  const g = cv.getContext('2d');
  g.fillStyle = '#000';
  g.fillRect(0, 0, cv.width, cv.height);
  for (let i = 0; i < d.leds.length; i += 5) {
    const [x, y, r, gr, b] = d.leds.slice(i, i + 5);
    g.fillStyle = r + gr + b ? `rgb(${r},${gr},${b})` : '#1c1c1c';
    g.beginPath();
    g.arc(x * S + S / 2, y * S + S / 2, S / 2 - .5, 0, 7);
    g.fill();
  }
  // Cluster outline + number in its calibration colour; while calibrating,
  // every fan also gets its chain number at its centre.
  const cal = d.calibrate !== 'off', F = d.p * S;
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.lineWidth = 2;
  d.clusters.forEach((pin, slot) => {
    const x = (slot % d.grid_w) * d.cw * S, y = Math.floor(slot / d.grid_w) * d.ch * S;
    const on = pin <= d.active;
    g.setLineDash(on ? [] : [4, 4]);
    g.strokeStyle = on ? GEO.colors[pin - 1] : '#444';
    g.strokeRect(x + 1, y + 1, d.cw * S - 2, d.ch * S - 2);
    g.font = 'bold 14px monospace';
    g.fillStyle = on ? GEO.colors[pin - 1] : '#666';
    g.fillText(pin, x + 11, y + 11);
    if (!cal || !on) return;
    g.font = 'bold 12px monospace';
    g.fillStyle = '#fff';
    d.fans.forEach((fan, fslot) => g.fillText(fan,  // corner of the fan cell: no LED there
      x + (fslot % d.cluster_w) * F + F - 7, y + Math.floor(fslot / d.cluster_w) * F + F - 7));
  });
  g.setLineDash([]);
  g.lineWidth = 1;
}

let fails = 0;
function setSvc(online) {
  const el = document.getElementById('svc');
  if (online) {
    if (fails >= 2) document.getElementById('restartMsg').textContent = 'back online';
    fails = 0;
    el.textContent = 'ONLINE'; el.style.background = '#2a4'; el.style.color = '#012';
  } else if (++fails >= 2) {
    el.textContent = 'OFFLINE'; el.style.background = '#a22'; el.style.color = '#fdd';
  }
}
function restartService() {
  if (!confirm('Restart the fan-wall service now?')) return;
  document.getElementById('restartMsg').textContent = 'restarting...';
  fetch('/restart', {method: 'POST'}).catch(() => {});
}
function poll() {
  fetch('/frame.json').then(r => r.json()).then(d => {
    setSvc(true);
    drawPreview(d);
    renderCalibration(d);
  }).catch(() => setSvc(false)).finally(() => setTimeout(poll, 150));
}
poll();
</script>
"""


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_control_server(state, geo, port, upload_dir, preview):
    handler = type("BoundControlHandler", (ControlHandler,), {
        "state": state, "geo": geo, "upload_dir": upload_dir, "preview": preview})
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- main ------------------------------------------------------------------

def parse_args():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser()
    p.add_argument("--sketch", default=os.path.join(here, "teensy_globalxy_udp_control.ino"),
                   help="Teensy sketch the wall geometry is read from")
    p.add_argument("--teensy-ip", help="default: teensyIP from the sketch")
    p.add_argument("--teensy-port", type=int, help="default: LISTEN_PORT from the sketch")
    p.add_argument("--media", help="video file to seed the queue with")
    p.add_argument("--text", help="text to scroll")
    p.add_argument("--text-height", type=int, default=14,
                   help="canvas rows for the text strip when media is also playing "
                        "(default 14 = two fan rows)")
    p.add_argument("--text-color", default="#ffffff")
    p.add_argument("--font")
    p.add_argument("--font-size", type=int)
    p.add_argument("--bold", action="store_true")
    p.add_argument("--italic", action="store_true")
    p.add_argument("--scroll-speed", type=float, default=20.0, help="canvas px/second")
    p.add_argument("--text-direction", default="left", choices=["left", "right", "up", "down"])
    p.add_argument("--fit", default="fill", choices=["letterbox", "fill"])
    p.add_argument("--brightness", type=int, default=255,
                   help="0-255 software level, under the sketch's FastLED cap")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--web-port", type=int, default=8099, help="0 disables the control page")
    p.add_argument("--upload-dir", default=os.path.join(here, "uploads"))
    p.add_argument("--state-file", default=os.path.join(here, "fan_wall_state.json"),
                   help="saved by the page's Save button, resumed on startup; '' disables")
    p.add_argument("--transition-s", type=float, default=0.6)
    p.add_argument("--stats", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    geo = load_sketch(args.sketch)
    args.teensy_ip = args.teensy_ip or geo["teensy_ip"]
    args.teensy_port = args.teensy_port or geo["teensy_port"]

    def _on_sigterm(signum, frame):  # systemctl stop -> same cleanup as ctrl-c
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _on_sigterm)

    def compute_geometry(snap):
        canvas_w, canvas_h = canvas_size(geo, snap)
        has_media, has_text = bool(snap["queue"]), bool(snap["text"])
        if has_media and has_text:
            text_h = min(args.text_height, canvas_h - 1)
        else:
            text_h = 0 if has_media else canvas_h
        return canvas_w, canvas_h, text_h, canvas_h - text_h

    def rebuild_scroller(snap, canvas_w, text_h):
        if text_h <= 0 or not snap["text"]:
            return None
        size = args.font_size or max(8, text_h - 2)
        fonts = [load_font(args.font, size, snap["bold"], snap["italic"])] + load_fallback_fonts(size)
        return TextScroller(snap["text"], fonts, canvas_w, text_h, snap["color"],
                            snap["text_direction"], snap["text_stacked"], snap["text_glyph_rotate"])

    state = State(args, geo, args.state_file or None)
    if args.state_file and os.path.isfile(args.state_file):
        print(f"resumed saved config from {args.state_file}")
    os.makedirs(args.upload_dir, exist_ok=True)
    player = QueuePlayer(lambda: state.snapshot()["queue"], args.fit, args.transition_s)
    link = TeensyLink(args.teensy_ip, args.teensy_port)
    preview = Preview()
    server = None
    if args.web_port:
        server = make_control_server(state, geo, args.web_port, args.upload_dir, preview)
        print(f"control panel: http://{local_ip()}:{args.web_port}/")
    print(f"sketch: {geo['pins']} pins x {geo['fans']} fans, teensy {args.teensy_ip}:{args.teensy_port}")

    built_version = map_key = None
    scroll_offset = 0.0
    last_t = last_report = time.monotonic()
    rendered = 0
    try:
        while True:
            t0 = time.monotonic()
            dt, last_t = t0 - last_t, t0
            snap = state.snapshot()

            if snap["version"] != built_version:
                canvas_w, canvas_h, text_h, video_h = compute_geometry(snap)
                scroller = rebuild_scroller(snap, canvas_w, text_h)
                trail = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
                built_version = snap["version"]
            key = tuple(str(snap[k]) for k in ("grid_w", "grid_h", "cluster_w", "cluster_h",
                                               "fan_order", "fan_rot", "fan_mirror",
                                               "cluster_order", "active"))
            if key != map_key:
                pos = build_led_map(geo, snap)
                map_key = key

            if snap["calibrate"] in ("fans", "clusters"):
                buf = chain_test_buffer(geo, snap, snap["calibrate"][:-1])
            else:
                if snap["calibrate"] == "grid":
                    frame = grid_test_canvas(canvas_w, canvas_h, geo["fan_px"])
                else:
                    frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
                    if video_h > 0:
                        frame[:video_h] = player.next_frame(
                            dt, canvas_w, video_h, snap["media_brightness"], snap["media_contrast"],
                            snap["media_rotation"], snap["media_scale"],
                            snap["media_pos_x"], snap["media_pos_y"])
                    if scroller:
                        sign = -1.0 if snap["text_direction"] in ("right", "down") else 1.0
                        scroll_offset += sign * dt * snap["scroll_speed"]
                        frame[canvas_h - text_h:] = scroller.frame(scroll_offset)
                    trail = apply_trail(trail, frame, snap["trail_wet"])
                    # ponytail: only 16 of a fan's 49 cells hold an LED, so a
                    # 1px text stroke can fall between them -- a 3x3 max
                    # spreads it onto the ring. Supersample the canvas if
                    # video looks too blocky.
                    frame = cv2.dilate(trail.astype(np.uint8), np.ones((3, 3), np.uint8))
                if snap["rotate180"]:
                    frame = np.ascontiguousarray(frame[::-1, ::-1])
                buf = sample_canvas(frame, pos)

            preview.update(buf, pos, canvas_w, canvas_h)
            out = buf if snap["brightness"] == 255 else \
                (buf.astype(np.uint16) * snap["brightness"] // 255).astype(np.uint8)
            link.send_frame(out.tobytes())
            rendered += 1

            if args.stats and t0 - last_report >= 1.0:
                print(f"render {rendered / (t0 - last_report):5.1f} fps")
                rendered, last_report = 0, t0
            slack = 1.0 / args.fps - (time.monotonic() - t0)
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        link.send_frame(bytes(geo["pins"] * geo["leds_per_pin"] * 3))
        print("\nstopped")


if __name__ == "__main__":
    main()
