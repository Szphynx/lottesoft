#!/usr/bin/env python3
"""
Video + scrolling text on one or two HUB75 RGB LED matrices, on a Raspberry Pi 3.

Wiring -- daisy chain (easiest option, recommended default):
    Pi 40-pin header -> Panel 1 IN    (identical to a single-panel setup)
    Panel 1 OUT       -> Panel 2 IN    (a second ribbon cable between the panels)
    5V+GND to EACH panel's power input from your PSU -- the ribbon only
    carries data/clock signals, never rely on it to pass power between panels.
This is --chain-length 2 (the default below), and turns the pair into one
wide canvas twice the width of a single panel. Refresh rate roughly halves
versus a single panel since the same GPIO pins now shift out twice the
pixels, but that's unnoticeable for video/text (unlike a fast camera feed).

Wiring -- parallel chains (more wiring, keeps full refresh rate per panel):
    Each panel gets its own ribbon back to the Pi (a Pi 3 can drive up to
    3 independent chains this way), or use a breakout board (e.g. an
    Adafruit RGB Matrix Bonnet/HAT) to route it cleanly by hand. Pass
    --parallel 2 --chain-length 1 instead if you wire it this way.

Run with:
    sudo python3 media_matrix.py --media clip.mp4 --text "hello world"
    sudo python3 media_matrix.py --text "SPECIALS TODAY: ..."   # text only
    sudo python3 media_matrix.py --media clip.mp4                # video only

Useful flags:
    --media PATH             video file to loop (anything OpenCV can decode)
    --text STRING             text to scroll; with --media it scrolls in a
                                strip along the bottom, otherwise it fills
                                the whole canvas
    --text-height N            rows reserved for the strip when --media is
                                also given (default 8)
    --font PATH                 .ttf/.otf font (default: DejaVu Sans Bold --
                                `sudo apt install fonts-dejavu-core`)
    --font-size N
    --text-color R,G,B
    --scroll-speed N            pixels/second
    --fit letterbox|fill        how the video fills its area (default fill)
    --panel-rows / --panel-cols default 32x64, set to your panels' size
    --chain-length / --parallel see wiring notes above (default 2 / 1)
    --brightness / --pwm-bits / --gpio-slowdown / --gpio-mapping
    --no-hardware-pulse         workaround if snd_bcm2835 audio is still
                                 loaded; causes more flicker, fix the module
                                 instead (see scripts/install.sh)
    --stats
"""

import argparse
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from rgbmatrix import RGBMatrix, RGBMatrixOptions


DEFAULT_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]


def load_font(path, size):
    for candidate in ([path] if path else DEFAULT_FONT_CANDIDATES):
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


def build_matrix(args):
    opts = RGBMatrixOptions()
    opts.rows = args.panel_rows
    opts.cols = args.panel_cols
    opts.chain_length = args.chain_length
    opts.parallel = args.parallel
    opts.hardware_mapping = args.gpio_mapping
    opts.gpio_slowdown = args.gpio_slowdown
    opts.pwm_bits = args.pwm_bits
    opts.brightness = args.brightness
    opts.pwm_lsb_nanoseconds = 130
    opts.disable_hardware_pulsing = args.no_hardware_pulse
    return RGBMatrix(options=opts)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--media", help="video file to loop")
    p.add_argument("--text", help="text to scroll")
    p.add_argument("--text-height", type=int, default=8,
                   help="rows for the text strip when --media is also given")
    p.add_argument("--font")
    p.add_argument("--font-size", type=int)
    p.add_argument("--text-color", default="255,255,255")
    p.add_argument("--scroll-speed", type=float, default=40.0,
                   help="pixels/second")
    p.add_argument("--fit", default="fill", choices=["letterbox", "fill"])
    p.add_argument("--panel-rows", type=int, default=32)
    p.add_argument("--panel-cols", type=int, default=64)
    p.add_argument("--chain-length", type=int, default=2)
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--brightness", type=int, default=60)
    p.add_argument("--pwm-bits", type=int, default=8)
    p.add_argument("--gpio-slowdown", type=int, default=4)
    p.add_argument("--gpio-mapping", default="regular",
                   help="'regular' for direct wiring, 'adafruit-hat' for a bonnet")
    p.add_argument("--no-hardware-pulse", action="store_true")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    if not args.media and not args.text:
        sys.exit("nothing to show -- pass --media and/or --text")
    return args


def main():
    args = parse_args()
    canvas_w = args.panel_cols * args.chain_length
    canvas_h = args.panel_rows * args.parallel

    if args.media and args.text:
        text_h, video_h = args.text_height, canvas_h - args.text_height
    elif args.media:
        text_h, video_h = 0, canvas_h
    else:
        text_h, video_h = canvas_h, 0

    if video_h <= 0:
        sys.exit("--text-height leaves no room for video -- "
                  "shrink it or use a taller panel chain")

    video = VideoSource(args.media, args.fit, canvas_w, video_h) if args.media else None
    scroller = None
    if args.text:
        font = load_font(args.font, args.font_size or max(8, text_h - 2))
        color = tuple(int(c) for c in args.text_color.split(","))
        scroller = TextScroller(args.text, font, text_h, color, canvas_w)

    matrix = build_matrix(args)
    canvas = matrix.CreateFrameCanvas()

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

            frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
            if video:
                frame[0:video_h] = video.next_frame()
            if scroller:
                scroll_offset += dt * args.scroll_speed
                frame[canvas_h - text_h:canvas_h] = scroller.frame(scroll_offset)

            canvas.SetImage(Image.fromarray(frame, "RGB"))
            canvas = matrix.SwapOnVSync(canvas)
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
        matrix.Clear()
        print("\nstopped")


if __name__ == "__main__":
    main()
