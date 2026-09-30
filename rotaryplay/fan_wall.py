#!/usr/bin/env python3
"""
Video + scrolling text on the 84-fan wall: Raspberry Pi renders, Teensy 4.1
drives the LEDs.

    Pi --eth0, UDP 5005--> Teensy (teensy.ino) --16 pins--> Corsair hubs --> fans

The Teensy owns the LED addressing: the Pi sends a plain 84 x 56 RGB image,
one 255-byte datagram per row (see pi_streamer_spec.md). Everything this
script knows about the wall -- frame size, clusters, fans, the 16-LED ring,
which cells are live, address/port/sync byte -- it reads out of teensy.ino
at startup, so the sketch stays the source of truth.

Web page on :8099, tabs:
    Calibration  just in case. Starts at the Teensy's own mapping (no
                 remap); if the wall disagrees, move fans/clusters and the
                 Pi shifts pixels so they land right -- the Teensy never
                 changes. Modes: "fans"/"clusters" (each cluster its own
                 colour, white LED count = fan / cluster number, straight on
                 the wire), "grid" (gradient + dot on top of every fan), and
                 the spec's test sequence (solid R/G/B, row 0, column 0,
                 corner probe).
    Content      text, playback queue, media transforms.
    Colour       colour correction on/off, R/G/B levels, softness (blur
                 before downsampling).
    Settings     brightness, active clusters, save/load, restart.
Save settings stores everything; it's reloaded on startup.

Video/text/queue/web UI come from lottesoft's double_matrix.py (MAX7219
version), with the 1-bit output stage swapped for the Teensy's RGB rows.

Run with:
    python3 fan_wall.py                     # web UI on :8099
    python3 fan_wall.py --media clip.mp4 --text "hello"

Needs: python3-opencv python3-numpy python3-pil, fonts-dejavu-core fonts-vlgothic fonts-noto-cjk
(install.sh does all of it plus the systemd service; run.sh starts it).
"""

import argparse
import colorsys
import http.server
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from html import escape

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
    # Don't take the whole wall down over a font: warn, use Pillow's own.
    print("no usable font found -- using Pillow's built-in one (install fonts-dejavu-core, "
          "or pass --font /path/to/font.ttf)")
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1: fixed-size bitmap font only
        return ImageFont.load_default()


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


def fit_frame(frame, fit, out_w, out_h, softness=1.0):
    """Resize an RGB frame to out_w x out_h, either cropping to fill or
    letterboxing to fit the whole frame. `softness` (1.0 = the spec's
    sigma of source_width/120 at 84 px wide) blurs before downsampling --
    16-LED rings alias into flicker otherwise; 0 turns it off."""
    h, w = frame.shape[:2]
    if fit == "fill":
        scale = max(out_w / w, out_h / h)
    else:  # letterbox
        scale = min(out_w / w, out_h / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    if softness > 0:
        if w > 4 * nw:  # ponytail: blur at 4x output size, not full source -- same look, far less CPU
            frame = cv2.resize(frame, (4 * nw, max(1, round(h * 4 * nw / w))),
                               interpolation=cv2.INTER_AREA)
        frame = cv2.GaussianBlur(frame, (0, 0), softness * 0.7 * frame.shape[1] / nw)
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
        self.path = item["path"]
        self.start = max(0.0, item.get("start") or 0.0)
        self.end = item.get("end")
        self.loop = bool(item.get("loop"))
        self.cap = None
        self.raw = self.fitted = self.key = None

        if self.kind == "image":
            # exif_transpose: phone photos are stored sideways + a rotate tag
            self.raw = np.array(ImageOps.exif_transpose(Image.open(self.path)).convert("RGB"))
            self.total_s = self.end if self.end and self.end > 0 else 5.0
            return

        self._rewind()
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open {self.path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
        # ponytail: missing/absurd fps metadata (some webm/gif/ts) -> assume 30
        self.fps = fps if 1 <= fps <= 240 else 30.0
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

    def _rewind(self):
        """(Re)open from the trim start. Reopening instead of seeking back
        to 0: seeking is unreliable in some containers (gif, webm, ts)."""
        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.path)
        if self.start:
            self.cap.set(cv2.CAP_PROP_POS_MSEC, self.start * 1000)
        self.t, self.shown = 0.0, -1

    def get_frame(self, fit, out_w, out_h, softness=1.0, dt=0.0):
        """Returns (frame, eof) -- eof means playback ended and won't loop.
        Video advances by its own clock, not one frame per call: 24, 25 or
        60 fps files play at real speed on the 30 fps loop (frames held or
        skipped; skipped ones are grabbed, not decoded). The fitted frame
        is cached until the source frame or the fit settings change."""
        key = (fit, out_w, out_h, softness)
        fresh = False
        if self.kind == "video":
            self.t += dt
            want = int(self.t * self.fps)
            while self.shown < want:
                if not self.cap.grab():
                    if self.shown == -1 or not self.loop:  # empty clip, or the end
                        last = self.fitted if self.fitted is not None and self.key == key \
                            else np.zeros((out_h, out_w, 3), dtype=np.uint8)
                        return last, True
                    self._rewind()
                    want = 0
                    continue
                self.shown += 1
                fresh = True
            if fresh:
                ok, frame = self.cap.retrieve()
                if ok:
                    self.raw = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if self.raw is None:
            return np.zeros((out_h, out_w, 3), dtype=np.uint8), False
        if fresh or self.key != key:
            self.fitted, self.key = fit_frame(self.raw, fit, out_w, out_h, softness), key
        return self.fitted, False

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
        self.softness = 1.0  # set from the live state each frame

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

        if self.current_id not in {it["id"] for it in queue}:
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
        frame_a, eof_a = self.current_clip.get_frame(self.fit, canvas_w, out_h, self.softness, dt) \
            if self.current_clip else (blank, True)  # unopenable item: skip it, don't stall
        done = eof_a or self.elapsed >= self.current_clip.total_s

        cur_item = self._find(queue, self.current_id)
        frame_a = adjust_frame(frame_a, cur_item.get("brightness", 100) if cur_item else 100,
                                cur_item.get("contrast", 100) if cur_item else 100)

        if self.transitioning and self.next_clip:
            t = 1.0 - max(0.0, min(1.0, remaining / self.transition_s)) if self.transition_s else 1.0
            t = t * t * (3 - 2 * t)  # smoothstep ease in/out
            frame_b, _ = self.next_clip.get_frame(self.fit, canvas_w, out_h, self.softness, dt)
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



# Decoding is OpenCV's FFmpeg backend (video) and Pillow (images); this list
# only decides what the upload accepts -- every upload is test-decoded
# anyway, so a codec the Pi's FFmpeg lacks is refused with a clear message.
KIND_BY_EXT = {
    **dict.fromkeys((".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".gif", ".mpg", ".mpeg",
                     ".ts", ".mts", ".m2ts", ".wmv", ".flv", ".3gp", ".ogv", ".mxf"), "video"),
    **dict.fromkeys((".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"), "image"),
}


def probe_media(path, kind):
    """None if `path` decodes as `kind`, else why not."""
    try:
        clip = ClipSource({"kind": kind, "path": path})
    except (RuntimeError, OSError, ValueError, cv2.error) as e:
        return str(e)
    try:
        if kind == "video" and not clip.cap.grab():
            return "no decodable frames (codec missing from this Pi's FFmpeg?)"
    finally:
        clip.close()
    return None
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



# ---- wall geometry, read from teensy.ino ------------------------------------

LEDS_PER_FAN = 16  # Corsair LL-style fan: 4 inner + 12 outer LEDs

# Calibration colour per cluster (pin), golden-ratio hue steps so chain and
# grid neighbours never look alike. Same list drives the wall and the page.
CLUSTER_COLORS = [tuple(round(v * 255) for v in colorsys.hsv_to_rgb(i * 0.618 % 1, 1, 1))
                  for i in range(32)]

# Calibration modes sent straight to the wire (the Teensy's own addressing,
# no remap) vs. drawn on the canvas like content (remap applies).
WIRE_MODES = ("fans", "clusters", "cluster", "red", "green", "blue", "row0", "col0", "probe")
# "clusterscreen" is content, not a wire pattern: the whole picture shrunk onto one cluster.
CAL_MODES = ("off", "grid", "clusterscreen") + WIRE_MODES

# Per-fan colour for the single-cluster test (chain position 1..6).
FAN_COLORS = [(255, 0, 0), (255, 128, 0), (255, 255, 0), (0, 255, 0), (0, 128, 255), (200, 0, 255)]


def load_sketch(path):
    """Everything the wall's wiring says, read out of teensy.ino: panel (=
    cluster = one pin) size in cells, kCols x kRows of them,
    kLEDS_PER_PANEL LEDs each, XYTable (which LED of the pin's chain sits in
    which cell), network settings and sync byte. From that: fan cell size,
    the 16-LED ring pattern, and `led_at` -- the Teensy's XY() for every
    cell of the frame, -1 where no LED is lit."""
    with open(path, encoding="utf-8") as f:
        src = f.read()

    def const(name):
        return int(re.search(rf"\b{name}\b\s*=?\s*(0x[0-9A-Fa-f]+|\d+)", src).group(1), 0)

    body = re.search(r"XYTable\[\]\s*=\s*\{(.*?)\};", src, re.S).group(1)
    table = [None if t in ("NL", "65535") else int(t) for t in re.findall(r"\bNL\b|\d+", body)]
    mw, mh = const("kMatrixWidth"), const("kMatrixHeight")
    cols, rows = const("kCols"), const("kRows")
    lpp = const("kLEDS_PER_PANEL")
    fans = lpp // LEDS_PER_FAN
    if len(table) != mw * mh:
        sys.exit(f"{path}: XYTable has {len(table)} cells, expected {mw}x{mh}")

    cells = {}
    for k, j in enumerate(table):
        if j is not None:
            cells[divmod(j, LEDS_PER_FAN)] = (k % mw, k // mw)
    cluster_w = len({min(cells[f, i][0] for i in range(LEDS_PER_FAN)) for f in range(fans)})
    p = mw // cluster_w
    cluster_h = mh // p
    if cluster_w * cluster_h != fans or p * cluster_w != mw or p * cluster_h != mh:
        sys.exit(f"{path}: XYTable fans don't tile the {mw}x{mh} panel in square cells")
    ring = [(cells[0, i][0] % p, cells[0, i][1] % p) for i in range(LEDS_PER_FAN)]
    for f in range(fans):
        if [(cells[f, i][0] % p, cells[f, i][1] % p) for i in range(LEDS_PER_FAN)] != ring:
            sys.exit(f"{path}: fan {f + 1} has a different LED pattern than fan 1")

    # teensy.ino XYPanel's corner exemptions, mirrored by hand -- they're
    # code, not table data. test_fan_wall.py pins them to the spec (1344
    # live LEDs, dead corners, the corner probe).
    corners = "Corner exemptions" in src
    half = lpp // 2
    w, h = mw * cols, mh * rows
    led_at = np.full((h, w), -1, dtype=np.int32)
    for y in range(h):
        for x in range(w):
            j = table[(y % mh) * mw + x % mw]
            if j is None:
                continue
            panel = (y // mh) * cols + x // mw
            if corners and panel in (0, cols - 1):
                if j < half:
                    continue      # top fan row missing
                j -= half         # bottom-row fans sit on ports 1-3
            if corners and panel in ((rows - 1) * cols, rows * cols - 1) and j >= half:
                continue          # bottom fan row missing
            led_at[y, x] = panel * lpp + j

    ip = ".".join(re.search(r"teensyIP\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", src).groups())
    return dict(teensy_ip=ip, teensy_port=const("LISTEN_PORT"), sync=const("SYNC_BYTE"),
                pins=cols * rows, lpp=lpp, fans=fans, fan_px=p, ring=ring,
                cluster_w=cluster_w, cluster_h=cluster_h, grid_w=cols, grid_h=rows,
                mw=mw, mh=mh, w=w, h=h, led_at=led_at)


def ring_cell(x, y, p, rot, mirror):
    if mirror:
        x = p - 1 - x
    for _ in range(rot // 90):
        x, y = p - 1 - y, x
    return x, y


def build_remap(geo, snap):
    """Every live cell as the Teensy addresses it -> where that LED really
    sits on the wall, per calibration. cluster_order[s] / fan_order[s] =
    the slot the cluster / fan the Teensy puts at slot s really occupies;
    fan_rot / fan_mirror turn that fan's ring. One fan pattern for every
    cluster. Defaults are identity: the Teensy's own mapping."""
    p, mw, mh, cw, gw = geo["fan_px"], geo["mw"], geo["mh"], geo["cluster_w"], geo["grid_w"]
    ys, xs = np.nonzero(geo["led_at"] >= 0)
    real = np.empty((len(xs), 2), dtype=np.int32)
    for k, (x, y) in enumerate(zip(xs, ys)):
        lx, ly = x % mw, y % mh
        g = (y // mh) * gw + x // mw
        q = (ly // p) * cw + lx // p
        rg, rq = snap["cluster_order"][g], snap["fan_order"][q]
        rx, ry = ring_cell(lx % p, ly % p, p, snap["fan_rot"][q], snap["fan_mirror"][q])
        real[k] = ((rg % gw) * mw + (rq % cw) * p + rx, (rg // gw) * mh + (rq // cw) * p + ry)
    return np.stack([xs, ys], axis=1).astype(np.int32), real


def wire_pattern(geo, snap, cells, mode):
    """A frame exactly as it goes on the wire, no calibration -- what the
    wiring really does. fans/clusters: each fan in its cluster's colour
    (half level) plus a run of white LEDs from LED 0 in wiring order; the
    count is the fan's chain number (1-6) or the cluster's pin number
    (1-16), where the run starts shows how the ring is turned. The rest is
    the spec's test sequence. "cluster": only the chosen cluster (cal_cluster,
    default 1) is lit, its six fans each in their own colour with a white
    count of their chain number; with cal_fan set, only that fan, all 16 LEDs
    on and LED 0 white."""
    img = np.zeros((geo["h"], geo["w"], 3), dtype=np.uint8)
    if mode in ("red", "green", "blue"):
        img[..., ("red", "green", "blue").index(mode)] = 255
    elif mode == "row0":
        img[0] = 255
    elif mode == "col0":
        img[:, 0] = 255
    elif mode == "probe":
        img[11, 3] = 255  # LED 0: first fan of hub 0
    elif mode == "cluster":
        led = geo["led_at"][cells[:, 1], cells[:, 0]]
        pin, fan, i = led // geo["lpp"], (led % geo["lpp"]) // LEDS_PER_FAN, led % LEDS_PER_FAN
        base = np.array(FAN_COLORS, dtype=np.uint8)[fan % len(FAN_COLORS)]
        if snap["cal_fan"] >= 0:
            col = base.copy()
            col[i == 0] = 255
            col[fan != snap["cal_fan"]] = 0
        else:
            col = base // 2
            col[i < fan + 1] = 255
        col[pin != max(0, snap["cal_cluster"])] = 0
        img[cells[:, 1], cells[:, 0]] = col
    else:
        led = geo["led_at"][cells[:, 1], cells[:, 0]]
        pin, fan, i = led // geo["lpp"], (led % geo["lpp"]) // LEDS_PER_FAN, led % LEDS_PER_FAN
        col = np.array(CLUSTER_COLORS, dtype=np.uint8)[pin] // 2
        col[i < (fan + 1 if mode == "fans" else pin + 1)] = 255
        if snap["cal_cluster"] >= 0:
            col[pin != snap["cal_cluster"]] //= 8
        img[cells[:, 1], cells[:, 0]] = col
    return img


def cluster_screen(frame, geo, snap):
    """Whole picture shrunk to one cluster's size and put where that cluster
    really sits; everything else dark. Lets one cluster's six fans be judged
    on real content."""
    mw, mh = geo["mw"], geo["mh"]
    gy, gx = divmod(snap["cluster_order"][max(0, snap["cal_cluster"])], geo["grid_w"])
    out = np.zeros_like(frame)
    out[gy * mh:(gy + 1) * mh, gx * mw:(gx + 1) * mw] = cv2.resize(frame, (mw, mh), interpolation=cv2.INTER_AREA)
    return out


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


def rev_pattern(geo, cells, t, dur, closing, rpm, color):
    """Startup / close sequence: a bright head with a fading tail runs
    round every fan's ring, in wiring order (LEDs 4-15 outer, 0-3 inner
    -- both run round the ring). Startup: speed ramps 0 -> rpm, fading in,
    then out into the content. Close: rpm -> 0 while fading to black.
    Above ~900 rpm the 30 fps loop can't follow it (wagon-wheel effect)."""
    u = min(1.0, t / dur)
    turns = rpm / 60.0 * dur / 3.0 * ((1 - (1 - u) ** 3) if closing else u ** 3)  # integral of the speed ramp
    fade = (1.0 - u) if closing else max(0.0, min(1.0, u / 0.15, (1.0 - u) / 0.15))
    i = geo["led_at"][cells[:, 1], cells[:, 0]] % LEDS_PER_FAN
    angle = np.where(i < 4, i / 4.0, (i - 4) / 12.0)
    level = np.exp(-6.0 * ((turns - angle) % 1.0)) * fade
    img = np.zeros((geo["h"], geo["w"], 3), dtype=np.uint8)
    img[cells[:, 1], cells[:, 0]] = (level[:, None] * np.array(color, dtype=np.float32)).astype(np.uint8)
    return img


def color_correct(frame, snap):
    """Per-channel levels, percent. The LEDs aren't perceptually even --
    greens read brighter than reds/pinks at the same value -- so the usual
    move is green down, red up. Also cuts power on green-heavy content."""
    if not snap["cc_on"] or (snap["cc_r"], snap["cc_g"], snap["cc_b"]) == (100, 100, 100):
        return frame
    gain = np.array([snap["cc_r"], snap["cc_g"], snap["cc_b"]], dtype=np.float32) / 100.0
    return np.clip(frame * gain, 0, 255).astype(np.uint8)


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
    bitmap, queue); the calibration remap is rebuilt whenever its own
    inputs change (see remap_key in main).

    Every field here except queue is also a command-line flag (see
    parse_args): defaults below < saved settings file < flags given at
    startup < live edits from the page."""

    def __init__(self, geo, state_file=None, media=None):
        self.lock = threading.Lock()
        self.geo = geo
        self.state_file = state_file
        self.text = ""
        self.color = (255, 255, 255)
        self.bold = False
        self.italic = False
        self.scroll_speed = 20.0
        self.text_direction = "left"
        self.text_stacked = False
        self.text_glyph_rotate = 0
        self.brightness = 255
        self.trail_wet = 0
        self.media_brightness = 100.0
        self.media_contrast = 100.0
        self.media_rotation = 0.0
        self.media_scale = 100.0
        self.media_pos_x = 0
        self.media_pos_y = 0
        self.cc_on = False
        self.cc_r = self.cc_g = self.cc_b = 100
        self.softness = 100
        self.viz = "a"              # preview style: a = LED grid, b = fan rings
        self.stats_on = False
        self.seq_start_on = False   # rev the fans up at startup
        self.seq_close_on = False   # spin them down on stop
        self.seq_start_s = 3.0
        self.seq_close_s = 2.0
        self.seq_rpm = 480
        self.seq_color = (255, 255, 255)
        self.seq_test = None        # "start"/"close" from the page's test buttons; never saved
        self.fan_order = list(range(geo["fans"]))
        self.fan_rot = [0] * geo["fans"]
        self.fan_mirror = [False] * geo["fans"]
        self.cluster_order = list(range(geo["pins"]))
        self.active = geo["pins"]
        self.rotate180 = False
        self.calibrate = "off"
        self.cal_cluster = -1
        self.cal_fan = -1           # cluster test: one fan (0-5), -1 = all six
        self.queue = []
        self.version = 0
        if state_file and os.path.isfile(state_file):
            self._load(state_file)
        if media:  # --media goes on the end of the saved queue
            self.add_media(media, KIND_BY_EXT.get(os.path.splitext(media)[1].lower(), "video"),
                           os.path.basename(media))

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
        """Persist so `_load` restores it next start. Called from the page's
        Save settings button, not on every edit."""
        if not self.state_file:
            return False
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.to_wire(), f, indent=2)
            os.replace(tmp, self.state_file)
            return True
        except OSError:
            return False

    def snapshot(self):
        with self.lock:
            snap = {k: v for k, v in vars(self).items() if k not in ("lock", "geo", "state_file")}
            for k in ("fan_order", "fan_rot", "fan_mirror", "cluster_order"):
                snap[k] = list(snap[k])
            snap["queue"] = [dict(q) for q in self.queue]
            return snap

    def to_wire(self):
        snap = self.snapshot()
        del snap["version"], snap["seq_test"]
        for key in ("color", "seq_color"):
            snap[key] = "#%02x%02x%02x" % snap[key]
        return snap

    def take_seq_test(self):
        with self.lock:
            seq, self.seq_test = self.seq_test, None
            return seq

    def add_media(self, path, kind, name):
        with self.lock:
            self.queue.append({
                "id": uuid.uuid4().hex[:8], "path": path, "kind": kind, "name": name,
                "start": 0.0, "end": None, "loop": False,
                "brightness": 100.0, "contrast": 100.0,
            })
            self.version += 1

    def apply_wire(self, data):
        """Bulk-update from a JSON dict -- the page's live edits or a saved
        settings file. Missing or invalid fields keep their current value."""
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
            if "seq_color" in data:
                try:
                    self.seq_color = hex_to_rgb(str(data["seq_color"]))
                except ValueError:
                    pass
            if data.get("seq_test") in ("start", "close"):
                self.seq_test = data["seq_test"]
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
                                      ("cc_r", 0, 200, int), ("cc_g", 0, 200, int),
                                      ("cc_b", 0, 200, int), ("softness", 0, 300, int),
                                      ("seq_start_s", 0.5, 30, float),
                                      ("seq_close_s", 0.5, 30, float),
                                      ("seq_rpm", 30, 1800, int),
                                      ("active", 1, geo["pins"], int),
                                      ("cal_cluster", -1, geo["pins"] - 1, int),
                                      ("cal_fan", -1, geo["fans"] - 1, int)):
                v = _clamp(data, key, lo, hi, cast)
                if v is not None:
                    setattr(self, key, v)
            for key in ("rotate180", "cc_on", "stats_on", "seq_start_on", "seq_close_on"):
                if key in data:
                    setattr(self, key, bool(data[key]))
            if data.get("viz") in ("a", "b"):
                self.viz = data["viz"]
            if data.get("calibrate") in CAL_MODES:
                self.calibrate = data["calibrate"]

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
    """teensy.ino's protocol: one datagram per row -- sync byte, frame
    sequence number, row index, the row's RGB bytes. Same sequence number on
    every row of a frame; the Teensy shows a frame once all rows are in, or
    the next sequence number arrives, or 120 ms pass."""

    def __init__(self, ip, port, sync):
        self.addr = (ip, port)
        self.sync = sync
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.warned = False
        self.errors = 0  # frames that failed to send, for the stats

    def send_frame(self, img):
        self.seq = (self.seq + 1) & 0xFF
        try:
            for y, row in enumerate(img):
                self.sock.sendto(bytes((self.sync, self.seq, y)) + row.tobytes(), self.addr)
            self.warned = False
        except OSError as e:
            self.errors += 1
            if not self.warned:  # cable out / wrong subnet: say so once, keep rendering
                print(f"teensy link: {e}")
                self.warned = True


class Stats:
    """Once-a-second numbers for the Stats tab, gathered only while stats
    are on. The render loop just bumps counters (a few adds per frame);
    the Teensy's own `fps N torn M` line is read from its USB serial by a
    thread that blocks on the port, so it costs nothing between lines."""

    def __init__(self, serial_path):
        self.serial_path = serial_path
        self.data = {}
        self.teensy = None
        self.reader = None
        self.retry_at = 0.0
        self.last = time.monotonic()
        self._reset(self.last)

    def _reset(self, now):
        self.t0, self.frames, self.work, self.work_max = now, 0, 0.0, 0.0

    def frame(self, now, work_s, link):
        if now - self.last > 1.0:  # stats were off (or startup): start a fresh window
            self._reset(now)
        self.last = now
        self.frames += 1
        self.work += work_s
        self.work_max = max(self.work_max, work_s)
        if now - self.t0 >= 1.0:
            n = max(1, self.frames)
            self.data = {"pi_fps": round(self.frames / (now - self.t0), 1),
                         "work_ms_avg": round(self.work / n * 1000, 1),
                         "work_ms_max": round(self.work_max * 1000, 1),
                         "send_errors": link.errors, "teensy": self.teensy}
            print("stats", self.data, flush=True)  # one line a second in the journal
            self._reset(now)
        if self.reader is None and now >= self.retry_at and self.serial_path \
                and os.path.exists(self.serial_path):
            self.retry_at = now + 5.0  # a port that won't open is retried every 5 s, not every frame
            self.reader = threading.Thread(target=self._read_serial, daemon=True)
            self.reader.start()

    def _read_serial(self):
        try:
            with open(self.serial_path, "rb", buffering=0) as port:
                for line in port:
                    m = re.match(rb"fps (\d+)\s+torn (\d+)", line.strip())
                    if m:
                        self.teensy = {"fps": int(m[1]), "torn": int(m[2]), "at": time.time()}
        except OSError as e:
            self.teensy = {"error": str(e)}
        self.reader = None  # port gone (unplugged): retry on a later frame


def pi_health():
    """CPU temperature and load -- read on request only, Linux-only (None elsewhere)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        temp = None
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"cpu_temp_c": temp, "load_1m": load}


# ---- web control -----------------------------------------------------------

class Preview:
    """Colour of every live LED just sent (before brightness) and where it
    really sits on the wall, for the page's fan-ring emulator."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None

    def update(self, colors, real, fan_labels):
        with self.lock:
            self.data = (colors, real, fan_labels)

    def snapshot(self):
        with self.lock:
            return self.data


class ControlHandler(http.server.BaseHTTPRequestHandler):
    state = geo = upload_dir = preview = stats = startup = None  # bound by make_control_server

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
            self._send(json.dumps(self.state.to_wire(), indent=2), "application/json",
                       extra_headers={"Content-Disposition": 'attachment; filename="fan-wall-config.json"'})
        elif path == "/frame.json":
            self._send(json.dumps(self._frame_data()), "application/json")
        elif path == "/stats.json":
            on = self.state.snapshot()["stats_on"]
            body = dict(self.stats.data, **pi_health(), on=True) if on else {"on": False}
            self._send(json.dumps(body), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _frame_data(self):
        snap, g = self.state.snapshot(), self.geo
        out = {"w": g["w"], "h": g["h"], "p": g["fan_px"], "grid_w": g["grid_w"],
               "cw": g["mw"], "ch": g["mh"], "cluster_w": g["cluster_w"],
               "calibrate": snap["calibrate"], "viz": snap["viz"], "active": snap["active"],
               "clusters": chain_labels(snap["cluster_order"]),
               "fans": chain_labels(snap["fan_order"]),
               "fan_rot": snap["fan_rot"], "fan_mirror": snap["fan_mirror"],
               "cal_cluster": snap["cal_cluster"], "leds": [], "fan_labels": []}
        data = self.preview.snapshot()
        if data:
            colors, real, fan_labels = data
            out["leds"] = np.concatenate([real, colors], axis=1).reshape(-1).tolist()
            out["fan_labels"] = fan_labels
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
        elif self.path == "/save":
            ok = self.state.save()
            self._send("saved" if ok else "not saved (--state-file disabled or not writable)",
                       "text/plain", code=200 if ok else 500)
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
        problem = probe_media(dest, kind)
        if problem:
            os.remove(dest)
            self._send(f"can't play {filename}: {problem}", "text/plain", code=400)
            return
        self.state.add_media(dest, kind, filename)
        self._send(_queue_item_row(self.state.snapshot()["queue"][-1]), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass

    def _render_page(self):
        snap, g = self.state.snapshot(), self.geo
        info = dict(pins=g["pins"], fans=g["fans"], startup=self.startup,
                    fan_colors=["#%02x%02x%02x" % c for c in FAN_COLORS],
                    colors=["#%02x%02x%02x" % c for c in CLUSTER_COLORS])
        live = int((g["led_at"] >= 0).sum())
        return (PAGE.replace("__STATE__", json.dumps(self.state.to_wire()).replace("</", "<\\/"))
                    .replace("__GEO__", json.dumps(info))
                    .replace("__EXTS__", ",".join(KIND_BY_EXT))
                    .replace("__QUEUE__", "".join(_queue_item_row(i) for i in snap["queue"]))
                    .replace("__SKETCH__", f"{g['w']}x{g['h']} frame, {g['grid_w']}x{g['grid_h']} "
                                           f"clusters of {g['cluster_w']}x{g['cluster_h']} fans, "
                                           f"{live} live LEDs"))


PAGE = r"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fan wall control</title>
<style>
  body{font:15px monospace;background:#111;color:#eee;max-width:44rem;margin:1.5rem auto;padding:0 1rem}
  h2{font-size:1rem;margin:1.2rem 0 .4rem}
  .note{color:#888;font-size:.8rem} label{display:block;margin:.35rem 0}
  input[type=range]{width:100%} button,.btn{background:#234;color:#eee;border:none;border-radius:4px;
  padding:.35rem .7rem;cursor:pointer;display:inline-block;font:inherit}
  nav{margin:1rem 0 .5rem;border-bottom:1px solid #333}
  nav.sub{margin:0 0 .8rem;border:0} nav.sub button{border-radius:4px;margin-right:.3rem}
  nav button{background:none;border-radius:4px 4px 0 0;padding:.45rem .9rem}
  nav button.on{background:#234}
  .tiles{display:inline-grid;gap:4px;margin:.4rem 0}
  .tile{position:relative;width:64px;height:44px;box-sizing:border-box;border:2px solid #6cf;border-radius:4px;
  background:#123;cursor:grab;display:flex;align-items:center;justify-content:center;user-select:none;font:bold 16px monospace}
  .chip{position:absolute;top:1px;cursor:pointer;font:9px monospace;padding:0 3px;background:rgba(255,255,255,.15)}
</style>
<h1 style="font-size:1.1rem">Fan wall
  <span id="svc" style="font-size:.6rem;padding:.15rem .5rem;border-radius:3px;background:#2a4;color:#012">ONLINE</span></h1>
<p class="note">Live preview -- every live LED as sent to the Teensy (__SKETCH__), clusters outlined with their pin number.</p>
<p style="margin:.3rem 0">Visualization
  <select data-k="viz"><option value="a">A: LED grid (as addressed)</option><option value="b">B: fan rings</option></select></p>
<canvas id="preview" style="background:#000;border:1px solid #444;max-width:100%"></canvas>
<p id="statsWarn" hidden style="background:#542;color:#fd9;padding:.4rem .6rem;border-radius:4px">
  Stats are ON -- measuring and polling costs a little CPU and may lower the frame rate. Turn off in the Stats tab when done.</p>

<nav>
  <button data-tab="cal">Calibration</button><button data-tab="content">Content</button>
  <button data-tab="colour">Colour</button><button data-tab="seq">Startup / Close</button>
  <button data-tab="stats">Stats</button><button data-tab="settings">Settings</button>
</nav>

<section data-tab="cal">
<nav class="sub"><button data-sub="wall">Wall</button><button data-sub="cluster">Cluster</button></nav>

<div data-sub="cluster">
<p class="note">Test one cluster on its own: pick it, and only its 6 fans light. The Teensy's own mapping is used, so
this shows the real wiring of that hub.</p>
<p>Cluster
  <button onclick="stepCluster(-1)">&laquo;</button>
  <select data-k="cal_cluster" data-num id="calClusterSel" onchange="clusterMode()"></select>
  <button onclick="stepCluster(1)">&raquo;</button></p>
<p><button onclick="clusterMode(-1)">All 6 fans</button>
  <span id="fanBtns"></span></p>
<p class="note"><b>All 6 fans:</b> each fan its own colour (1 red, 2 orange, 3 yellow, 4 green, 5 blue, 6 purple)
with white LEDs counting its number. <b>Single fan:</b> that fan fully lit, LED 0 white -- checks every LED of the
ring and which end the ring starts. A fan that doesn't light is on the wrong port or unplugged.</p>
<p><button onclick="clusterScreen()">Whole screen on this cluster</button>
  <button onclick="setMode('off')">Back to content</button></p>
<p class="note"><b>Whole screen:</b> the full picture (video, text) shrunk onto this one cluster, the rest dark --
judge the six fans on real content.</p>
</div>

<div data-sub="wall">
<label>Mode <select data-k="calibrate">
  <option value="off">off -- show content</option>
  <option value="fans">fans -- cluster colours, white LEDs = fan number (1-6)</option>
  <option value="clusters">clusters -- cluster colours, white LEDs = cluster number</option>
  <option value="grid">grid -- gradient + white dot on top of every fan</option>
  <option value="cluster">cluster -- one cluster, its six fans (see the Cluster sub-tab)</option>
  <option value="clusterscreen">clusterscreen -- whole picture on one cluster</option>
  <option value="red">test: all red</option><option value="green">test: all green</option>
  <option value="blue">test: all blue</option><option value="row0">test: row 0 only</option>
  <option value="col0">test: column 0 only</option><option value="probe">test: corner probe (3,11) = LED 0</option>
</select></label>
<label>Highlight cluster <select data-k="cal_cluster" data-num id="calCluster"></select></label>
<p class="note">Starts at the Teensy's own mapping -- no remap. Only touch the tiles if the wall disagrees with the
preview: the Pi then shifts pixels so they land right, the Teensy stays as it is. Each cluster has its own colour;
the white LEDs count up from LED 0 in wiring order, so the count is the number and where it starts shows how the
ring is turned. <span id="legend"></span></p>
<p class="note">1. <b>Fan pattern</b> (one cluster, every cluster follows; the 3-fan corner clusters use the
matching half): in "fans" mode, drag tiles so each fan number sits where it is on the wall. <b>R</b> turns that
fan 90&deg;, <b>M</b> mirrors it.</p>
<div id="fanTiles" class="tiles"></div>
<button onclick="resetFans()">reset to Teensy</button>
<p class="note">2. <b>Cluster grid</b>: drag tiles until each pin's cluster sits where it is on the wall.
Click a tile to highlight that cluster.</p>
<div id="clusterTiles" class="tiles"></div>
<button onclick="state.cluster_order=state.cluster_order.map((_,i)=>i);send()">reset to Teensy</button>
<p class="note">Grid size comes from teensy.ino -- change it there and restart.</p>
</div>
</section>

<section data-tab="content">
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
<label>Position X <span id="v-media_pos_x"></span><input type="range" data-k="media_pos_x" min="-84" max="84"></label>
<label>Position Y <span id="v-media_pos_y"></span><input type="range" data-k="media_pos_y" min="-56" max="56"></label>
<label class="btn">+ Add image/video<input type="file" accept="video/*,image/*,__EXTS__" style="display:none" onchange="uploadFile(this)"></label>
<div id="queue">__QUEUE__</div>
</section>

<section data-tab="colour">
<label><input type="checkbox" data-k="cc_on" style="display:inline"> Colour correction on</label>
<p class="note">The LEDs aren't perceptually even: greens read brighter than reds and pinks at the same value.
Usual move: green down, red up. Applies to content only -- calibration and test patterns stay raw.</p>
<label>Red <span id="v-cc_r"></span>%<input type="range" data-k="cc_r" min="0" max="200"></label>
<label>Green <span id="v-cc_g"></span>%<input type="range" data-k="cc_g" min="0" max="200"></label>
<label>Blue <span id="v-cc_b"></span>%<input type="range" data-k="cc_b" min="0" max="200"></label>
<button onclick="state.cc_r=state.cc_g=state.cc_b=100;send();setTimeout(()=>location.reload(),200)">reset levels</button>
<h2>Softness</h2>
<label>Blur before downsampling <span id="v-softness"></span>%<input type="range" data-k="softness" min="0" max="300"></label>
<p class="note">100% = the spec's starting point (sigma = source width / 120). Too little shimmers, too much is mush.</p>
<button onclick="saveSettings()">Save settings</button> <span class="note saveMsg"></span>
</section>

<section data-tab="seq">
<p class="note">Fan "rev": a light runs round every fan's ring, speeding up at startup and slowing down on close.
Only runs when switched on here. Close runs on stop/restart of the service (not on a power cut).</p>
<label><input type="checkbox" data-k="seq_start_on" style="display:inline"> Rev up at startup</label>
<label>Startup length <span id="v-seq_start_s"></span> s<input type="range" data-k="seq_start_s" min="0.5" max="10" step="0.5"></label>
<label><input type="checkbox" data-k="seq_close_on" style="display:inline"> Spin down on close</label>
<label>Close length <span id="v-seq_close_s"></span> s<input type="range" data-k="seq_close_s" min="0.5" max="10" step="0.5"></label>
<label>Top speed <span id="v-seq_rpm"></span> rpm<input type="range" data-k="seq_rpm" min="30" max="1800" step="30"></label>
<p class="note">Above ~900 rpm the 30 fps output can't keep up and the spin may look like it runs backwards.</p>
<label>Colour <input type="color" data-k="seq_color"></label>
<button onclick="testSeq('start')">Test startup</button> <button onclick="testSeq('close')">Test close</button>
<p><button onclick="saveSettings()">Save settings</button> <span class="note saveMsg"></span></p>
</section>

<section data-tab="stats">
<label><input type="checkbox" data-k="stats_on" style="display:inline"> Enable stats</label>
<p class="note">Off by default. While on, the render loop counts frames and times its work (a few additions per
frame), the page asks for numbers once a second, the Teensy's USB serial is read if it's plugged into the Pi,
and a line a second goes to the log. Small, but it can lower the frame rate on a busy Pi.</p>
<pre id="statsBox" style="background:#000;padding:.6rem;border:1px solid #333">off</pre>
</section>

<section data-tab="settings">
<label>Brightness <span id="v-brightness"></span>/255<input type="range" data-k="brightness" min="0" max="255"></label>
<p class="note">Software level, under the sketch's own FastLED brightness and power cap.</p>
<label>Active clusters <span id="v-active"></span><input type="range" data-k="active" min="1" id="activeRange"></label>
<label><input type="checkbox" data-k="rotate180" style="display:inline"> Whole wall mounted upside down</label>
<p><button onclick="saveSettings()">Save settings</button> <span class="note saveMsg"></span></p>
<p class="note">Saved settings (everything: calibration, colour, content, startup/close, stats) come back on every
start. Every setting on this page is also a command-line flag (<code>./run.sh --help</code>): a flag given
at startup (run.sh, or EXTRA_FLAGS in rotaryplay.conf) wins over the saved file, and the page can still
change it afterwards.</p>
<h2>Startup config</h2>
<p class="note">Network and files -- set in rotaryplay.conf, applied on restart:</p>
<pre id="startupBox" style="background:#000;padding:.6rem;border:1px solid #333"></pre>
<a href="/config.json" download="fan-wall-config.json" class="btn" style="text-decoration:none">Download settings</a>
<label class="btn" style="display:inline-block">Load settings<input type="file" accept="application/json" style="display:none" onchange="loadConfig(this)"></label>
<p><button onclick="restartService()" style="background:#622;color:#fdd">restart service</button>
<span id="restartMsg" class="note"></span></p>
</section>

<script>
const state = __STATE__;
const GEO = __GEO__;
let debounceTimer;
function send() {
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(state)});
}
function sendDebounced() { clearTimeout(debounceTimer); debounceTimer = setTimeout(send, 200); }
function saveSettings() {
  clearTimeout(debounceTimer);
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(state)})
    .then(() => fetch('/save', {method: 'POST'})).then(r => r.text())
    .then(msg => document.querySelectorAll('.saveMsg').forEach(el => el.textContent = msg));
}

// Tabs; the open one is remembered per browser.
function showTab(name) {
  document.querySelectorAll('section[data-tab]').forEach(s => s.hidden = s.dataset.tab !== name);
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  try { localStorage.setItem('tab', name); } catch (e) {}
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
let startTab = 'cal';
try { startTab = localStorage.getItem('tab') || 'cal'; } catch (e) {}
showTab(startTab);

// Calibration sub-tabs (Wall / Cluster), remembered per browser.
function showSub(name) {
  document.querySelectorAll('div[data-sub]').forEach(d => d.hidden = d.dataset.sub !== name);
  document.querySelectorAll('nav.sub button').forEach(b => b.classList.toggle('on', b.dataset.sub === name));
  try { localStorage.setItem('sub', name); } catch (e) {}
}
document.querySelectorAll('nav.sub button').forEach(b => b.onclick = () => showSub(b.dataset.sub));
let startSub = 'wall';
try { startSub = localStorage.getItem('sub') || 'wall'; } catch (e) {}
showSub(startSub);

// Cluster test: cluster picker, per-fan buttons, whole-screen mode.
function setMode(mode) {
  state.calibrate = mode;
  document.querySelector('select[data-k=calibrate]').value = mode;
  send();
}
function clusterMode(fan) {
  if (fan !== undefined) state.cal_fan = fan;
  if (state.cal_cluster < 0) state.cal_cluster = 0;
  document.getElementById('calClusterSel').value = state.cal_cluster;
  setMode('cluster');
}
function clusterScreen() {
  if (state.cal_cluster < 0) state.cal_cluster = 0;
  setMode('clusterscreen');
}
function stepCluster(d) {
  state.cal_cluster = ((Math.max(0, state.cal_cluster) + d) % GEO.pins + GEO.pins) % GEO.pins;
  document.getElementById('calClusterSel').value = state.cal_cluster;
  state.calibrate === 'clusterscreen' ? send() : clusterMode();
}
document.getElementById('calClusterSel').innerHTML =
  Array.from({length: GEO.pins}, (_, i) => `<option value="${i}">${i + 1}</option>`).join('');
document.getElementById('fanBtns').innerHTML = Array.from({length: GEO.fans}, (_, i) =>
  `<button onclick="clusterMode(${i})" style="color:${GEO.fan_colors[i]}">fan ${i + 1}</button>`).join(' ');

// Every [data-k] control is bound to state[k]; a matching #v-k shows its value.
const calSel = document.getElementById('calCluster');
calSel.innerHTML = '<option value="-1">all</option>' +
  Array.from({length: GEO.pins}, (_, i) => `<option value="${i}">${i + 1}</option>`).join('');
document.getElementById('activeRange').max = GEO.pins;
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
document.getElementById('legend').innerHTML = GEO.colors.slice(0, GEO.pins)
  .map((c, i) => `<span style="color:${c}">&#9679;${i + 1}</span>`).join(' ');

function resetFans() {
  state.fan_order = state.fan_order.map((_, i) => i);
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

// Drag-to-swap tile grid (from double_matrix): labels[slot] = 1-based
// Teensy slot now shown in that slot; state[key][teensySlot] = slot.
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

const S = 6;  // preview px per frame cell
// Visualization B: each fan as a round fan -- a ring outline with its 16 LEDs
// on a true circle (12 outer, 4 inner) instead of at their 7x7 cell positions,
// so it shows the fan you'd see, not a pixel grid.
function drawRings(g, d) {
  const p = d.p, half = (p - 1) / 2, R = p * S * 0.4, r0 = R * 0.38, dot = S * 0.5;
  const seen = new Set();
  for (let i = 0; i < d.leds.length; i += 5) {
    const [x, y, r, gr, b] = d.leds.slice(i, i + 5);
    const fx = Math.floor(x / p) * p, fy = Math.floor(y / p) * p;
    const cx = (fx + half + .5) * S, cy = (fy + half + .5) * S;
    const key = fx + ',' + fy;
    if (!seen.has(key)) {
      seen.add(key);
      g.strokeStyle = '#2a2a2a';
      g.beginPath();
      g.arc(cx, cy, R + dot + 1, 0, 7);
      g.stroke();
    }
    const dx = x - fx - half, dy = y - fy - half, a = Math.atan2(dy, dx);
    const rad = Math.hypot(dx, dy) < 1.5 ? r0 : R;
    g.fillStyle = r + gr + b ? `rgb(${r},${gr},${b})` : '#222';
    g.beginPath();
    g.arc(cx + Math.cos(a) * rad, cy + Math.sin(a) * rad, dot, 0, 7);
    g.fill();
  }
}
function drawPreview(d) {
  const cv = document.getElementById('preview');
  if (cv.width !== d.w * S || cv.height !== d.h * S) { cv.width = d.w * S; cv.height = d.h * S; }
  const g = cv.getContext('2d');
  g.fillStyle = '#000';
  g.fillRect(0, 0, cv.width, cv.height);
  if (d.viz === 'b') drawRings(g, d); else
  for (let i = 0; i < d.leds.length; i += 5) {
    const [x, y, r, gr, b] = d.leds.slice(i, i + 5);
    g.fillStyle = r + gr + b ? `rgb(${r},${gr},${b})` : '#1c1c1c';
    g.beginPath();
    g.arc(x * S + S / 2, y * S + S / 2, S / 2 - .5, 0, 7);
    g.fill();
  }
  // Cluster outline + pin number in its calibration colour; while
  // calibrating, every fan also gets its chain number in its cell's corner
  // (no LED there).
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
  });
  g.setLineDash([]);
  g.lineWidth = 1;
  if (d.calibrate === 'off') return;
  g.font = 'bold 12px monospace';
  g.fillStyle = '#fff';
  const F = d.p * S;
  d.fan_labels.forEach(([x, y, n]) => g.fillText(n, x * S + F - 7, y * S + F - 7));
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

document.getElementById('startupBox').textContent = Object.entries(GEO.startup)
  .map(([k, v]) => k.padEnd(15) + v).join('\n');

function testSeq(kind) {
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({}, state, {seq_test: kind}))});
}
// Stats: polled once a second, and only while they're on.
function pollStats() {
  document.getElementById('statsWarn').hidden = !state.stats_on;
  if (!state.stats_on) {
    document.getElementById('statsBox').textContent = 'off';
    return setTimeout(pollStats, 1000);
  }
  fetch('/stats.json').then(r => r.json()).then(s => {
    const t = s.teensy;
    const teensy = !t ? 'no USB serial line yet (Teensy USB not on this Pi?)'
      : t.error ? 'serial: ' + t.error
      : `${t.fps} fps shown, ${t.torn} torn ${t.torn ? '<- rows being lost' : ''}`;
    document.getElementById('statsBox').textContent = s.pi_fps === undefined ? 'collecting...' :
      `Pi render   ${s.pi_fps} fps\n` +
      `frame work  ${s.work_ms_avg} ms avg, ${s.work_ms_max} ms max (budget ${(1000 / 30).toFixed(1)} ms at 30 fps)\n` +
      `send errors ${s.send_errors}\n` +
      `Teensy      ${teensy}\n` +
      `CPU         ${s.cpu_temp_c ?? '?'} °C, load ${s.load_1m ?? '?'}`;
  }).catch(() => {}).finally(() => setTimeout(pollStats, 1000));
}
pollStats();
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


def make_control_server(state, geo, port, upload_dir, preview, stats, startup):
    handler = type("BoundControlHandler", (ControlHandler,), {
        "state": state, "geo": geo, "upload_dir": upload_dir, "preview": preview, "stats": stats,
        "startup": startup})
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- main ------------------------------------------------------------------

# Settings whose flag takes a fixed set of values.
FLAG_CHOICES = {"calibrate": CAL_MODES, "viz": ("a", "b"), "text_direction": ("left", "right", "up", "down"),
                "text_glyph_rotate": (0, 90, 270)}


def parse_args():
    """Startup-only flags, plus one flag per page setting, generated from
    State so the two can't drift: --brightness 120, --cc-on, --cc-g 70,
    --calibrate fans, --seq-start-on, --stats-on, --fan-order '[0,1,2,3,4,5]'
    ... A setting flag given at startup overrides the saved settings file;
    the page can still change it live afterwards."""
    here = os.path.dirname(os.path.abspath(__file__))
    # --help added last, so it lists the setting flags too. No abbreviations:
    # in the first pass --text would otherwise be read as --text-height.
    p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    p.add_argument("--sketch", default=os.path.join(here, "teensy.ino"),
                   help="Teensy sketch the wall geometry and link settings are read from")
    p.add_argument("--teensy-ip", help="default: teensyIP from the sketch")
    p.add_argument("--teensy-port", type=int, help="default: LISTEN_PORT from the sketch")
    p.add_argument("--teensy-serial", default="/dev/ttyACM0",
                   help="Teensy USB serial, read for its fps/torn line while stats are on")
    p.add_argument("--media", help="video/image file added to the end of the queue")
    p.add_argument("--text-height", type=int, default=14,
                   help="frame rows for the text strip when media is also playing "
                        "(default 14 = two fan rows)")
    p.add_argument("--font", help="primary .ttf/.otf; CJK/kana fall back to VL Gothic / Noto CJK")
    p.add_argument("--font-size", type=int)
    p.add_argument("--fit", default="fill", choices=["letterbox", "fill"])
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--web-port", type=int, default=8099, help="0 disables the control page")
    p.add_argument("--upload-dir", default=os.path.join(here, "uploads"))
    p.add_argument("--state-file", default=os.path.join(here, "fan_wall_state.json"),
                   help="written by Save settings, loaded on startup; '' disables")
    p.add_argument("--transition-s", type=float, default=0.6)

    known, _ = p.parse_known_args()
    geo = load_sketch(known.sketch)
    defaults = State(geo).to_wire()
    g = p.add_argument_group("settings (the page's tabs; override the saved settings file)")
    for key, val in defaults.items():
        if key == "queue":
            continue
        flag, extra = "--" + key.replace("_", "-"), {"dest": key, "default": None}
        if isinstance(val, bool):
            g.add_argument(flag, action=argparse.BooleanOptionalAction, **extra)
        elif isinstance(val, list):
            g.add_argument(flag, type=json.loads, metavar="JSON", help=f"e.g. '{json.dumps(val)}'", **extra)
        else:
            g.add_argument(flag, type=type(val), choices=FLAG_CHOICES.get(key),
                           help=f"default {val}", **extra)
    p.add_argument("-h", "--help", action="help", help="show this help and exit")
    args = p.parse_args()
    args.settings = {k: getattr(args, k) for k in defaults
                     if k != "queue" and getattr(args, k) is not None}
    return args, geo


def main():
    args, geo = parse_args()
    W, H = geo["w"], geo["h"]

    def _on_sigterm(signum, frame):  # systemctl stop -> same cleanup as ctrl-c
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _on_sigterm)

    def rebuild_scroller(snap, text_h):
        if text_h <= 0 or not snap["text"]:
            return None
        size = args.font_size or max(8, text_h - 2)
        fonts = [load_font(args.font, size, snap["bold"], snap["italic"])] + load_fallback_fonts(size)
        return TextScroller(snap["text"], fonts, W, text_h, snap["color"],
                            snap["text_direction"], snap["text_stacked"], snap["text_glyph_rotate"])

    state = State(geo, args.state_file or None, args.media)
    if args.state_file and os.path.isfile(args.state_file):
        print(f"resumed saved settings from {args.state_file}")
    state.apply_wire(args.settings)
    os.makedirs(args.upload_dir, exist_ok=True)
    player = QueuePlayer(lambda: state.snapshot()["queue"], args.fit, args.transition_s)
    link = TeensyLink(args.teensy_ip or geo["teensy_ip"], args.teensy_port or geo["teensy_port"],
                      geo["sync"])
    preview = Preview()
    stats = Stats(args.teensy_serial)
    server = None
    if args.web_port:
        startup = {"teensy": f"{link.addr[0]}:{link.addr[1]}", "teensy serial": args.teensy_serial,
                   "web port": args.web_port, "sketch": args.sketch,
                   "settings file": args.state_file or "(saving off)", "uploads": args.upload_dir,
                   "fps": args.fps, "flags given": " ".join(sys.argv[1:]) or "(none)"}
        server = make_control_server(state, geo, args.web_port, args.upload_dir, preview, stats, startup)
        print(f"control panel: http://{local_ip()}:{args.web_port}/")
    print(f"sketch: {W}x{H} frame, {int((geo['led_at'] >= 0).sum())} live LEDs, "
          f"teensy {link.addr[0]}:{link.addr[1]}")

    built_version = remap_key = None
    scroll_offset = 0.0
    trail = np.zeros((H, W, 3), dtype=np.float32)
    last_t = time.monotonic()
    # Startup / close sequence in progress: (kind, start time) or None.
    seq = ("start", last_t) if state.snapshot()["seq_start_on"] else None
    try:
        while True:
            t0 = time.monotonic()
            dt, last_t = t0 - last_t, t0
            snap = state.snapshot()
            test = state.take_seq_test()
            if test:
                seq = (test, t0)

            if snap["version"] != built_version:
                has_media, has_text = bool(snap["queue"]), bool(snap["text"])
                text_h = min(args.text_height, H - 1) if has_media and has_text \
                    else (0 if has_media else H)
                video_h = H - text_h
                scroller = rebuild_scroller(snap, text_h)
                built_version = snap["version"]
            key = tuple(str(snap[k]) for k in ("cluster_order", "fan_order", "fan_rot", "fan_mirror"))
            if key != remap_key:
                cells, real = build_remap(geo, snap)
                cell_pin = geo["led_at"][cells[:, 1], cells[:, 0]] // geo["lpp"]
                led0 = geo["led_at"][cells[:, 1], cells[:, 0]] % LEDS_PER_FAN == 0
                p = geo["fan_px"]
                fan_labels = [[int(rx // p * p), int(ry // p * p), int(led % geo["lpp"] // LEDS_PER_FAN + 1)]
                              for (rx, ry), led in zip(real[led0],
                                                       geo["led_at"][cells[led0, 1], cells[led0, 0]])]
                remap_key = key

            seq_dur = snap["seq_start_s" if seq and seq[0] == "start" else "seq_close_s"]
            if seq and t0 - seq[1] >= seq_dur:
                seq = None
            if seq:
                send = rev_pattern(geo, cells, t0 - seq[1], seq_dur, seq[0] == "close",
                                   snap["seq_rpm"], snap["seq_color"])
            elif snap["calibrate"] in WIRE_MODES:
                send = wire_pattern(geo, snap, cells, snap["calibrate"])
            else:
                if snap["calibrate"] == "grid":
                    frame = grid_test_canvas(W, H, geo["fan_px"])
                else:
                    player.softness = snap["softness"] / 100.0
                    frame = np.zeros((H, W, 3), dtype=np.uint8)
                    if video_h > 0:
                        frame[:video_h] = player.next_frame(
                            dt, W, video_h, snap["media_brightness"], snap["media_contrast"],
                            snap["media_rotation"], snap["media_scale"],
                            snap["media_pos_x"], snap["media_pos_y"])
                    if scroller:
                        sign = -1.0 if snap["text_direction"] in ("right", "down") else 1.0
                        scroll_offset += sign * dt * snap["scroll_speed"]
                        frame[H - text_h:] = scroller.frame(scroll_offset)
                    trail = apply_trail(trail, frame, snap["trail_wet"])
                    frame = color_correct(trail.astype(np.uint8), snap)
                    if snap["calibrate"] == "clusterscreen":
                        frame = cluster_screen(frame, geo, snap)
                if snap["rotate180"]:
                    frame = frame[::-1, ::-1]
                # Each cell the Teensy addresses gets the canvas pixel where
                # its LED really is -- identity until calibration says otherwise.
                send = np.zeros((H, W, 3), dtype=np.uint8)
                send[cells[:, 1], cells[:, 0]] = frame[real[:, 1], real[:, 0]]
            if snap["active"] < geo["pins"]:
                send[cells[cell_pin >= snap["active"], 1], cells[cell_pin >= snap["active"], 0]] = 0

            preview.update(send[cells[:, 1], cells[:, 0]], real, fan_labels)
            out = send if snap["brightness"] == 255 else \
                (send.astype(np.uint16) * snap["brightness"] // 255).astype(np.uint8)
            link.send_frame(out)

            work = time.monotonic() - t0
            if snap["stats_on"]:
                stats.frame(t0, work, link)
            slack = 1.0 / args.fps - work
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        snap = state.snapshot()
        if snap["seq_close_on"] and remap_key is not None:  # spin down, then dark
            t_end = time.monotonic()
            while (t := time.monotonic() - t_end) < snap["seq_close_s"]:
                send = rev_pattern(geo, cells, t, snap["seq_close_s"], True,
                                   snap["seq_rpm"], snap["seq_color"])
                link.send_frame((send.astype(np.uint16) * snap["brightness"] // 255).astype(np.uint8))
                time.sleep(1.0 / args.fps)
        link.send_frame(np.zeros((H, W, 3), dtype=np.uint8))
        print("\nstopped")


if __name__ == "__main__":
    main()
