#!/usr/bin/env python3
"""
Loop the shared video-fracture video onto a HUB75 RGB LED matrix (Joy-IT
RB-MatrixCtrl + rgbmatrix) -- the same controller/library thermal_matrix.py
uses -- with a live web control page for brightness/rotation/fit/position.

Reads /var/lib/video-fracture/current.mp4: the file
video-fracture-fetch.timer already keeps up to date from whatever
VIDEO_LOCAL_DIR/VIDEO_DRIVE_FOLDER_ID is set in /etc/default/video-fracture
(see scripts/video-fracture-install.sh). This script does NOT fetch
anything itself -- it just displays whatever's there and reopens the file
when its mtime changes. Strictly additive: nothing here touches the
existing HDMI video-fracture files/services, it just shows the same
video on a second, LED-matrix output.

Run with:
    sudo python3 scripts/video-fracture-led/player.py

Control page: http://<tailscale-ip>:8099/
"""

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
from PIL import Image

from rgbmatrix import RGBMatrix, RGBMatrixOptions

CURRENT_VIDEO = "/var/lib/video-fracture/current.mp4"
CONFIG_FILE = "/var/lib/video-fracture/led-config.json"

DEFAULTS = {
    "brightness": 60,     # 1-100, live-applied every frame
    "rotation": 0,        # 0/90/180/270
    "fit": "letterbox",   # letterbox (full frame, may letterbox) | fill (crop to fill)
    "scale_pct": 100,
    "offset_x": 0,
    "offset_y": 0,
}


class State:
    """Lock-protected live config, persisted to CONFIG_FILE so it survives
    a restart/reboot -- same idea as media_matrix.py's State, minus the
    parts (queue, text scroller, panel wiring) that don't apply here."""

    def __init__(self):
        self.lock = threading.Lock()
        self.values = dict(DEFAULTS)
        self._load()

    def _load(self):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
        except (FileNotFoundError, ValueError):
            return
        with self.lock:
            self.values.update({k: v for k, v in saved.items() if k in DEFAULTS})

    def save(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.values, f)
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def apply(self, data):
        with self.lock:
            for key in DEFAULTS:
                if key in data:
                    self.values[key] = data[key]
        self.save()


class Preview:
    """Last frame sent to the panel, for the control page's live pixel
    preview -- a plain copy-under-lock, same tradeoff as media_matrix.py's
    Preview class (debug aid, not the render hot path)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None

    def update(self, frame):
        with self.lock:
            self.frame = frame

    def snapshot(self):
        with self.lock:
            return self.frame


def build_matrix(gpio_mapping, gpio_slowdown, pwm_bits, brightness, panel):
    """Same RGBMatrixOptions as thermal_matrix.py's build_matrix() -- single
    64x64 panel, chain=1, parallel=1, root-owned (drop_privileges=False)."""
    opts = RGBMatrixOptions()
    opts.rows = panel
    opts.cols = panel
    opts.chain_length = 1
    opts.parallel = 1
    opts.hardware_mapping = gpio_mapping
    opts.gpio_slowdown = gpio_slowdown
    opts.pwm_bits = pwm_bits
    opts.brightness = brightness
    opts.pwm_lsb_nanoseconds = 130
    opts.drop_privileges = False
    return RGBMatrix(options=opts)


def fit_frame(frame_bgr, fit, size):
    """BGR frame (any size) -> size x size RGB. 'letterbox' keeps the whole
    frame (black bars); 'fill' center-crops to fill the panel."""
    h, w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    if fit == "fill":
        scale = max(size / w, size / h)
    else:
        scale = min(size / w, size / h)
    rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(rgb, (rw, rh), interpolation=cv2.INTER_AREA)

    if fit == "fill":
        x0, y0 = (rw - size) // 2, (rh - size) // 2
        return resized[y0:y0 + size, x0:x0 + size]

    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    x0, y0 = (size - rw) // 2, (size - rh) // 2
    canvas[y0:y0 + rh, x0:x0 + rw] = resized
    return canvas


def transform_frame(frame, rotation, scale_pct, offset_x, offset_y):
    """Rotate-about-center, scale, and X/Y pixel offset -- same knobs as
    media_matrix.py's transform_frame(), no-op when all at defaults."""
    out = frame
    if rotation:
        out = np.rot90(out, (rotation // 90) % 4).copy()
    if scale_pct != 100 or offset_x or offset_y:
        size = out.shape[0]
        m = cv2.getRotationMatrix2D((size / 2, size / 2), 0, scale_pct / 100.0)
        m[0, 2] += offset_x
        m[1, 2] += offset_y
        out = cv2.warpAffine(out, m, (size, size))
    return out


class VideoSource:
    """Loops `path` forever, transparently reopening it when its mtime
    changes -- i.e. whenever video-fracture-fetch.timer swaps in a new
    file. No IPC needed since we just poll mtime once per frame."""

    def __init__(self, path):
        self.path = path
        self.cap = None
        self.mtime = None
        self._open()

    def _open(self):
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.path) if os.path.exists(self.path) else None
        self.mtime = self._current_mtime()

    def _current_mtime(self):
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def next_frame(self):
        if self._current_mtime() != self.mtime:
            self._open()
        if self.cap is None or not self.cap.isOpened():
            return None
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
            if not ok:
                return None
        return frame


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>video-fracture-led</title>
<body style="font:16px sans-serif;background:#111;color:#eee;padding:2rem;max-width:640px;margin:auto">
<h1>video-fracture-led</h1>
<p>video: <b id="video">-</b></p>
<canvas id="prev" width="64" height="64" style="width:256px;height:256px;image-rendering:pixelated;border:1px solid #444;background:#000"></canvas>

<div style="margin-top:1.5rem">
  <label>brightness <span id="brightness-v"></span></label><br>
  <input id="brightness" type="range" min="1" max="100" style="width:100%">
</div>
<div style="margin-top:1rem">
  <label>rotation</label><br>
  <select id="rotation" style="width:100%">
    <option value="0">0</option><option value="90">90</option>
    <option value="180">180</option><option value="270">270</option>
  </select>
</div>
<div style="margin-top:1rem">
  <label>fit</label><br>
  <select id="fit" style="width:100%">
    <option value="letterbox">letterbox (show whole frame)</option>
    <option value="fill">fill (crop to fill panel)</option>
  </select>
</div>
<div style="margin-top:1rem">
  <label>scale % <span id="scale_pct-v"></span></label><br>
  <input id="scale_pct" type="range" min="50" max="200" style="width:100%">
</div>
<div style="margin-top:1rem">
  <label>offset x <span id="offset_x-v"></span></label><br>
  <input id="offset_x" type="range" min="-32" max="32" style="width:100%">
</div>
<div style="margin-top:1rem">
  <label>offset y <span id="offset_y-v"></span></label><br>
  <input id="offset_y" type="range" min="-32" max="32" style="width:100%">
</div>

<script>
const ctx = document.getElementById('prev').getContext('2d');
const fields = ['brightness', 'rotation', 'fit', 'scale_pct', 'offset_x', 'offset_y'];
let applying = false;

function post(partial) {
  fetch('/update', {method: 'POST', body: JSON.stringify(partial)});
}

for (const id of ['brightness', 'scale_pct', 'offset_x', 'offset_y']) {
  const el = document.getElementById(id);
  el.oninput = () => {
    document.getElementById(id + '-v').textContent = el.value;
    if (!applying) post({[id]: Number(el.value)});
  };
}
document.getElementById('rotation').onchange = e => post({rotation: Number(e.target.value)});
document.getElementById('fit').onchange = e => post({fit: e.target.value});

async function pollStatus() {
  const s = await (await fetch('/status.json')).json();
  document.getElementById('video').textContent = s.video || '(none found yet)';
  applying = true;
  for (const id of ['brightness', 'rotation', 'fit', 'scale_pct', 'offset_x', 'offset_y']) {
    document.getElementById(id).value = s[id];
    const v = document.getElementById(id + '-v');
    if (v) v.textContent = s[id];
  }
  applying = false;
}

async function pollFrame() {
  const f = await (await fetch('/frame.json')).json();
  if (f.w && f.h) {
    const img = ctx.createImageData(f.w, f.h);
    for (let i = 0; i < f.pixels.length; i++) {
      const [r, g, b] = f.pixels[i];
      img.data[i * 4] = r; img.data[i * 4 + 1] = g; img.data[i * 4 + 2] = b; img.data[i * 4 + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }
}

pollStatus();
setInterval(pollStatus, 2000);
setInterval(pollFrame, 200);
</script>
"""


class ControlHandler(BaseHTTPRequestHandler):
    state: State = None
    preview: Preview = None
    source_path = None

    def _send(self, body, content_type, code=200):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/status.json":
            snap = self.state.snapshot()
            snap["video"] = os.path.basename(self.source_path) if os.path.exists(self.source_path) else None
            self._send(json.dumps(snap), "application/json")
        elif self.path == "/frame.json":
            frame = self.preview.snapshot()
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
        if self.path != "/update":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length).decode()) if length else {}
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        self.state.apply(data)
        self._send("ok", "text/plain")

    def log_message(self, *a):
        pass


def make_control_server(state, preview, source_path, port):
    ControlHandler.state = state
    ControlHandler.preview = preview
    ControlHandler.source_path = source_path
    server = ThreadingHTTPServer(("0.0.0.0", port), ControlHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", default=CURRENT_VIDEO,
                   help="video file to loop -- defaults to the file "
                        "video-fracture-fetch.timer already maintains")
    p.add_argument("--panel", type=int, default=64)
    p.add_argument("--gpio-mapping", default="regular",
                   help="'regular' for direct wiring, 'adafruit-hat' for a bonnet")
    p.add_argument("--gpio-slowdown", type=int, default=4)
    p.add_argument("--pwm-bits", type=int, default=8)
    p.add_argument("--web-port", type=int, default=8099)
    p.add_argument("--fps-cap", type=float, default=30.0)
    args = p.parse_args()

    state = State()
    preview = Preview()

    matrix = build_matrix(args.gpio_mapping, args.gpio_slowdown, args.pwm_bits,
                           state.snapshot()["brightness"], args.panel)
    canvas = matrix.CreateFrameCanvas()

    source = VideoSource(args.video)
    make_control_server(state, preview, args.video, args.web_port)

    print(f"running -- control page on :{args.web_port}, ctrl-c to stop")
    frame_budget = 1.0 / args.fps_cap if args.fps_cap else 0.0
    try:
        while True:
            t0 = time.monotonic()
            snap = state.snapshot()
            frame = source.next_frame()
            if frame is not None:
                rgb = fit_frame(frame, snap["fit"], args.panel)
                rgb = transform_frame(rgb, snap["rotation"], snap["scale_pct"],
                                       snap["offset_x"], snap["offset_y"])
                matrix.brightness = snap["brightness"]
                preview.update(rgb)
                canvas.SetImage(Image.fromarray(rgb, "RGB"))
                canvas = matrix.SwapOnVSync(canvas)
            else:
                time.sleep(0.5)

            if frame_budget:
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
