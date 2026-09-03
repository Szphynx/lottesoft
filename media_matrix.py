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

Useful flags:
    --panel-width / --panel-height   pixels per panel -- defaults (32x8)
                                       match the WS2812ECO 8x32 panel
    --num-panels                       panels chained together (default 2)
    --layout horizontal|vertical       how the chain forms one image: side
                                       by side (wider) or stacked (taller).
                                       Default horizontal.
    --serpentine / --no-serpentine    most cheap matrix panels wire rows (or
                                       columns, see below) back and forth
                                       rather than all in one direction;
                                       default on.
    --serpentine-axis row|column      row snakes across each row (default);
                                       column snakes down each column, common
                                       on narrow tall-pixel-count panels. If a
                                       horizontal scroll bounces vertically,
                                       try column.
    --led-pin (BCM, default 18) / --led-freq-hz / --led-dma / --led-invert
    --brightness N                      0-255, default 80 -- start low,
                                       these draw a lot of current at 255
    --media PATH             video file to loop (anything OpenCV can decode)
    --text STRING             text to scroll; with --media it scrolls in a
                                strip along the bottom, otherwise it fills
                                the whole canvas
    --text-height N            rows reserved for the strip when --media is
                                also given (default 4)
    --font PATH                 .ttf/.otf font (default: DejaVu Sans --
                                `sudo apt install fonts-dejavu-core`); bold/
                                italic below only affect the default font,
                                a custom --font is used as-is
    --font-size N
    --text-color R,G,B
    --bold / --italic
    --scroll-speed N            pixels/second
    --rotate 0|180              flip the panel image, e.g. if it's mounted
                                upside down
    --fit letterbox|fill        how the video fills its area (default fill)
    --web-port N                 live control panel at http://<pi>:N/ to
                                edit text/color/bold/italic/speed/brightness
                                while it's running (default 8098, 0 disables)
    --stats
"""

import argparse
import http.server
import socket
import sys
import threading
import time
import urllib.parse
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


class VideoSource:
    """Loops a video file, fitted to an out_w x out_h RGB frame."""

    def __init__(self, path, fit, out_w, out_h):
        self.fit = fit
        self.out_w, self.out_h = out_w, out_h
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            sys.exit(f"could not open video: {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 1 else 24.0

    def next_frame(self):
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
            if not ok:
                sys.exit("failed to read a frame even after rewinding")
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return self._fit_frame(frame)

    def _fit_frame(self, frame):
        h, w = frame.shape[:2]
        out_w, out_h = self.out_w, self.out_h
        if self.fit == "fill":
            scale = max(out_w / w, out_h / h)
        else:  # letterbox
            scale = min(out_w / w, out_h / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)

        if self.fit == "fill":
            x0, y0 = (nw - out_w) // 2, (nh - out_h) // 2
            return resized[y0:y0 + out_h, x0:x0 + out_w]

        canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
        x0, y0 = (out_w - nw) // 2, (out_h - nh) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        return canvas


class TextScroller:
    """Renders `text` once, then hands back a canvas_w-wide sliding window
    of it (with a canvas-width gap before it repeats) for any pixel offset."""

    def __init__(self, text, font, height, color, canvas_w):
        dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        bbox = dummy.textbbox((0, 0), text, font=font)
        strip_w = max(1, bbox[2] - bbox[0])

        img = Image.new("RGB", (strip_w, height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((-bbox[0], (height - (bbox[3] - bbox[1])) // 2 - bbox[1]),
                   text, font=font, fill=color)

        gap = np.zeros((height, canvas_w, 3), dtype=np.uint8)
        self.loop = np.concatenate([np.array(img), gap], axis=1)
        self.width = self.loop.shape[1]
        self.canvas_w = canvas_w

    def frame(self, offset_px):
        start = int(offset_px) % self.width
        end = start + self.canvas_w
        if end <= self.width:
            return self.loop[:, start:end]
        wrap = end - self.width
        return np.concatenate([self.loop[:, start:], self.loop[:, :wrap]], axis=1)


def build_index_map(panel_w, panel_h, num_panels, layout, serpentine, serpentine_axis):
    """canvas (y, x) -> pixel index in the chain. One panel's local index
    snakes along rows (0,0 -> right along row 0 -> down -> left along row 1 --
    most panels) or along columns (0,0 -> down column 0 -> right -> up column 1
    -- common on narrow tall-pixel-count panels) if `serpentine`; panels then
    extend the chain side by side or stacked."""
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
            if serpentine_axis == "column":
                yy = (panel_h - 1 - ly) if (serpentine and lx % 2 == 1) else ly
                local = lx * panel_h + yy
            else:
                xx = (panel_w - 1 - lx) if (serpentine and ly % 2 == 1) else lx
                local = ly * panel_w + xx
            idx_map[y, x] = panel_idx * panel_w * panel_h + local
    return idx_map, canvas_w, canvas_h


def build_strip(args, num_pixels):
    strip = PixelStrip(num_pixels, args.led_pin, args.led_freq_hz, args.led_dma,
                        args.led_invert, args.brightness, args.led_channel)
    strip.begin()
    return strip


class State:
    """Live-editable display settings, shared between the render loop and
    the web control server. `version` bumps only on changes that require
    re-rendering the text bitmap (rebuilding a TextScroller is not free)."""

    def __init__(self, args):
        self.lock = threading.Lock()
        self.text = args.text or ""
        self.color = tuple(int(c) for c in args.text_color.split(","))
        self.bold = args.bold
        self.italic = args.italic
        self.scroll_speed = args.scroll_speed
        self.brightness = args.brightness
        self.version = 0

    def snapshot(self):
        with self.lock:
            return dict(text=self.text, color=self.color, bold=self.bold,
                        italic=self.italic, scroll_speed=self.scroll_speed,
                        brightness=self.brightness, version=self.version)

    def update(self, **fields):
        rebuild_keys = {"text", "color", "bold", "italic"}
        with self.lock:
            for key, value in fields.items():
                setattr(self, key, value)
            if rebuild_keys & fields.keys():
                self.version += 1


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % rgb


class ControlHandler(http.server.BaseHTTPRequestHandler):
    state = None  # bound per-instance by make_control_server

    def _send_html(self, body, code=200):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path not in ("/", ""):
            self.send_response(404)
            self.end_headers()
            return
        snap = self.state.snapshot()
        html = f"""<!doctype html><meta charset="utf-8">
<title>LED matrix control</title>
<body style="font:16px monospace;background:#111;color:#eee;
             max-width:32rem;margin:2rem auto;padding:0 1rem">
<h1 style="font-size:1.1rem">LED matrix control</h1>
<form method="POST">
  <label>Text<br>
    <input name="text" value="{escape(snap['text'])}" style="width:100%;padding:.4rem">
  </label><br><br>
  <label>Color <input type="color" name="color" value="{rgb_to_hex(snap['color'])}"></label>
  &nbsp; <label><input type="checkbox" name="bold" {"checked" if snap['bold'] else ""}> Bold</label>
  &nbsp; <label><input type="checkbox" name="italic" {"checked" if snap['italic'] else ""}> Italic</label>
  <br><br>
  <label>Scroll speed (px/s)
    <input type="number" name="scroll_speed" value="{snap['scroll_speed']:g}" step="1" style="width:6rem">
  </label><br><br>
  <label>Brightness (0-255)
    <input type="number" name="brightness" min="0" max="255" value="{snap['brightness']}" style="width:6rem">
  </label><br><br>
  <button type="submit" style="padding:.5rem 1rem">Apply</button>
</form>
</body>"""
        self._send_html(html)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
        updates = {"bold": "bold" in fields, "italic": "italic" in fields}
        if "text" in fields:
            updates["text"] = fields["text"][0]
        if "color" in fields:
            try:
                updates["color"] = hex_to_rgb(fields["color"][0])
            except ValueError:
                pass
        if "scroll_speed" in fields:
            try:
                updates["scroll_speed"] = float(fields["scroll_speed"][0])
            except ValueError:
                pass
        if "brightness" in fields:
            try:
                updates["brightness"] = max(0, min(255, int(fields["brightness"][0])))
            except ValueError:
                pass
        self.state.update(**updates)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):
        pass


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


def make_control_server(state, port):
    handler = type("BoundControlHandler", (ControlHandler,), {"state": state})
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
    p.add_argument("--rotate", type=int, default=0, choices=[0, 180],
                   help="flip the panel image, e.g. if it's mounted upside down")
    p.add_argument("--fit", default="fill", choices=["letterbox", "fill"])
    p.add_argument("--web-port", type=int, default=8098,
                   help="live control panel port, 0 to disable")
    p.add_argument("--panel-width", type=int, default=32)
    p.add_argument("--panel-height", type=int, default=8)
    p.add_argument("--num-panels", type=int, default=2)
    p.add_argument("--layout", default="horizontal", choices=["horizontal", "vertical"])
    p.add_argument("--serpentine", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--serpentine-axis", default="row", choices=["row", "column"],
                   help="which direction the panel's wiring snakes in -- try "
                        "'column' if a horizontal scroll bounces vertically")
    p.add_argument("--led-pin", type=int, default=18, help="BCM GPIO number")
    p.add_argument("--led-freq-hz", type=int, default=800000)
    p.add_argument("--led-dma", type=int, default=10)
    p.add_argument("--led-invert", action="store_true")
    p.add_argument("--led-channel", type=int, default=0)
    p.add_argument("--brightness", type=int, default=80, help="0-255")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    if not args.media and not args.text:
        sys.exit("nothing to show -- pass --media and/or --text")
    return args


def main():
    args = parse_args()
    idx_map, canvas_w, canvas_h = build_index_map(
        args.panel_width, args.panel_height, args.num_panels,
        args.layout, args.serpentine, args.serpentine_axis)

    if args.media and args.text:
        text_h, video_h = args.text_height, canvas_h - args.text_height
    elif args.media:
        text_h, video_h = 0, canvas_h
    else:
        text_h, video_h = canvas_h, 0

    if args.media and video_h <= 0:
        sys.exit("--text-height leaves no room for video -- "
                  "shrink it or use a taller panel chain")

    video = VideoSource(args.media, args.fit, canvas_w, video_h) if args.media else None

    state = State(args)

    def rebuild_scroller(snap):
        if text_h <= 0 or not snap["text"]:
            return None
        font = load_font(args.font, args.font_size or max(8, text_h - 2),
                          bold=snap["bold"], italic=snap["italic"])
        return TextScroller(snap["text"], font, text_h, snap["color"], canvas_w)

    scroller = None
    built_version = None
    if text_h > 0:
        snap0 = state.snapshot()
        scroller = rebuild_scroller(snap0)
        built_version = snap0["version"]

    server = None
    if args.web_port:
        server = make_control_server(state, args.web_port)
        print(f"control panel: http://{local_ip()}:{args.web_port}/")
        if text_h <= 0:
            print("note: no text region reserved (no --text at startup), so "
                  "the text/color/style fields won't do anything -- "
                  "brightness still works")

    num_pixels = args.panel_width * args.panel_height * args.num_panels
    strip = build_strip(args, num_pixels)
    flat_idx = idx_map.reshape(-1)

    render_fps_cap = video.fps if video else 30.0
    frame_budget = 1.0 / render_fps_cap
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
            if text_h > 0 and snap["version"] != built_version:
                scroller = rebuild_scroller(snap)
                built_version = snap["version"]

            frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
            if video:
                frame[0:video_h] = video.next_frame()
            if scroller:
                scroll_offset += dt * snap["scroll_speed"]
                frame[canvas_h - text_h:canvas_h] = scroller.frame(scroll_offset)

            if args.rotate == 180:
                frame = np.rot90(frame, 2)

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
