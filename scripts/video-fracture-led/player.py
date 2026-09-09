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
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.values, f)
            os.replace(tmp, CONFIG_FILE)
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def apply(self, data):
        """Bulk-update from a JSON dict (the whole `state` object the
        control page keeps client-side, same pattern as media_matrix.py's
        apply_wire) -- clamped/validated here rather than trusted from the
        client, since a bad value (e.g. a NaN from a stale slider) would
        otherwise reach the render loop and RGBMatrix calls directly."""
        with self.lock:
            if "brightness" in data:
                self.values["brightness"] = max(1, min(100, int(data["brightness"])))
            if "rotation" in data and int(data["rotation"]) in (0, 90, 180, 270):
                self.values["rotation"] = int(data["rotation"])
            if "fit" in data and data["fit"] in ("letterbox", "fill"):
                self.values["fit"] = data["fit"]
            if "scale_pct" in data:
                self.values["scale_pct"] = max(50, min(200, int(data["scale_pct"])))
            if "offset_x" in data:
                self.values["offset_x"] = max(-64, min(64, int(data["offset_x"])))
            if "offset_y" in data:
                self.values["offset_y"] = max(-64, min(64, int(data["offset_y"])))
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


def build_matrix(args, brightness):
    """Same base RGBMatrixOptions as thermal_matrix.py's build_matrix() --
    single 64x64 panel, chain=1, parallel=1, root-owned
    (drop_privileges=False) -- plus the panel-identity options
    thermal_matrix.py doesn't need to set because its panel happens to
    match the library defaults.

    A wrong color order (blue tint) or wrong multiplexing (checkerboard
    of black squares, image scrambled) is a panel/wiring calibration
    mismatch, not something the render loop or web page can cause --
    every panel batch/chip needs its own values here, found by trying
    the common options against the real hardware. Set them via
    /etc/default/video-fracture-led (FLAGS=...) and restart the service;
    no code change needed:
        --led-rgb-sequence RBG   (or BGR, GRB, ... -- fixes color-swapped output)
        --multiplexing 1         (try 1-17 -- fixes scrambled/checkerboard output)
        --row-address-type 1     (some panels need 1-4 instead of the default 0)
        --panel-type FM6126A     (some clone panels need an explicit init sequence)
    """
    opts = RGBMatrixOptions()
    opts.rows = args.panel
    opts.cols = args.panel
    opts.chain_length = 1
    opts.parallel = 1
    opts.hardware_mapping = args.gpio_mapping
    opts.gpio_slowdown = args.gpio_slowdown
    opts.pwm_bits = args.pwm_bits
    opts.brightness = brightness
    opts.pwm_lsb_nanoseconds = 130
    opts.drop_privileges = False
    opts.led_rgb_sequence = args.led_rgb_sequence
    opts.multiplexing = args.multiplexing
    opts.row_address_type = args.row_address_type
    if args.panel_type:
        opts.panel_type = args.panel_type
    if args.pixel_mapper:
        opts.pixel_mapper_config = args.pixel_mapper
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
<p style="color:#888;font-size:.8rem;margin-top:.5rem">
  panel hardware: <span id="hw">-</span><br>
  Wrong colors (e.g. blue tint) or a scrambled/checkerboard image is a
  panel calibration mismatch, not something this page can fix live --
  edit <code>/etc/default/video-fracture-led</code>
  (<code>--led-rgb-sequence</code>, <code>--multiplexing</code>,
  <code>--row-address-type</code>, <code>--panel-type</code>) and
  <code>sudo systemctl restart video-fracture-led</code> to try different
  values against the real hardware.
</p>

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
const state = {};
let applying = false;
let debounceTimer;

// Whole-state POST, debounced 200ms -- same pattern as the double-panel
// (media_matrix.py) control page: one consolidated request per pause in
// dragging, instead of one request per field per input event, and no risk
// of two field updates racing each other out of order.
function send() {
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
                     body: JSON.stringify(state)});
}
function sendDebounced() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(send, 200);
}

for (const id of ['brightness', 'scale_pct', 'offset_x', 'offset_y']) {
  const el = document.getElementById(id);
  el.oninput = () => {
    document.getElementById(id + '-v').textContent = el.value;
    state[id] = Number(el.value);
    if (!applying) sendDebounced();
  };
}
document.getElementById('rotation').onchange = e => {
  state.rotation = Number(e.target.value);
  if (!applying) sendDebounced();
};
document.getElementById('fit').onchange = e => {
  state.fit = e.target.value;
  if (!applying) sendDebounced();
};

async function pollStatus() {
  const s = await (await fetch('/status.json')).json();
  document.getElementById('video').textContent = s.video || '(none found yet)';
  document.getElementById('hw').textContent =
    `${s.hw.gpio_mapping}, rgb-sequence=${s.hw.led_rgb_sequence}, ` +
    `multiplexing=${s.hw.multiplexing}, row-address-type=${s.hw.row_address_type}` +
    (s.hw.panel_type ? `, panel-type=${s.hw.panel_type}` : '');
  applying = true;
  for (const id of ['brightness', 'rotation', 'fit', 'scale_pct', 'offset_x', 'offset_y']) {
    state[id] = s[id];
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
    hw_info = None

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
            snap["hw"] = self.hw_info
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


def make_control_server(state, preview, source_path, hw_info, port):
    ControlHandler.state = state
    ControlHandler.preview = preview
    ControlHandler.source_path = source_path
    ControlHandler.hw_info = hw_info
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
    p.add_argument("--led-rgb-sequence", default="RGB",
                   help="color-channel order the panel actually expects, e.g. "
                        "RBG/GRB/BGR -- wrong value shows as a color tint/swap")
    p.add_argument("--multiplexing", type=int, default=0,
                   help="0 for a normal/direct panel; some clone 64x64 panels "
                        "need 1-17 -- wrong value shows as a scrambled/"
                        "checkerboard image")
    p.add_argument("--row-address-type", type=int, default=0,
                   help="0 for most panels; some need 1-4 for correct row "
                        "addressing")
    p.add_argument("--panel-type", default="",
                   help="explicit init sequence for some clone chipsets, "
                        "e.g. FM6126A -- leave blank unless the panel needs it")
    p.add_argument("--pixel-mapper", default="",
                   help="e.g. 'Rotate:180' -- passed straight to "
                        "RGBMatrixOptions.pixel_mapper_config")
    args = p.parse_args()

    state = State()
    preview = Preview()

    matrix = build_matrix(args, state.snapshot()["brightness"])
    canvas = matrix.CreateFrameCanvas()

    source = VideoSource(args.video)
    hw_info = {
        "gpio_mapping": args.gpio_mapping,
        "led_rgb_sequence": args.led_rgb_sequence,
        "multiplexing": args.multiplexing,
        "row_address_type": args.row_address_type,
        "panel_type": args.panel_type,
    }
    make_control_server(state, preview, args.video, hw_info, args.web_port)

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
