#!/usr/bin/env python3
"""
Loop the shared video-fracture video onto a HUB75 RGB LED matrix (Joy-IT
RB-MatrixCtrl + rgbmatrix) -- the same controller/library thermal_matrix.py
uses -- with a live web control page for image controls
(brightness/rotation/fit/scale/position) and panel hardware calibration
(led-rgb-sequence/multiplexing/row-address-type/panel-type/pixel-mapper),
all real-time and all saveable/loadable as named presets. The current
state autosaves on every change and autoloads on startup, so whatever was
last in effect (including the last preset you loaded) is what comes back
after a restart/reboot -- no SSH needed for day-to-day calibration.

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
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
from PIL import Image

from rgbmatrix import RGBMatrix, RGBMatrixOptions

CURRENT_VIDEO = "/var/lib/video-fracture/current.mp4"
CONFIG_FILE = "/var/lib/video-fracture/led-config.json"       # current/last-loaded state, autosaved
CONFIGS_DIR = "/var/lib/video-fracture/led-configs"            # named, explicitly-saved presets

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

# Fields that only change how an already-open frame is drawn -- applied
# live every frame, no rebuild.
IMAGE_FIELDS = {
    "brightness": 60,     # 1-100
    "rotation": 0,        # 0/90/180/270
    "fit": "letterbox",   # letterbox (full frame, may letterbox) | fill (crop to fill)
    "scale_pct": 100,
    "offset_x": 0,
    "offset_y": 0,
}

# Fields that are RGBMatrixOptions construction-time-only in the underlying
# library -- changing one of these can't be live-applied to the existing
# RGBMatrix object, so the main loop tears it down and builds a fresh one
# (see main()'s hw-change check) whenever any of these actually change.
HW_FIELDS = {
    "led_rgb_sequence": "RGB",   # e.g. RGB/RBG/GRB/BGR -- wrong value = color tint/swap
    "multiplexing": 0,            # 0-17 -- wrong value = scrambled/checkerboard image
    "row_address_type": 0,        # 0-4
    "panel_type": "",             # e.g. "FM6126A" for some clone chipsets, else blank
    "pixel_mapper": "",           # e.g. "Rotate:180", passed straight through
}

DEFAULTS = {**IMAGE_FIELDS, **HW_FIELDS}


class State:
    """Lock-protected live config. Autosaved to CONFIG_FILE on every change
    and autoloaded from it at startup -- so whatever was in effect last
    (including a named preset you Load, since loading just applies it into
    this same State) is what comes back after a restart/reboot. Named
    presets under CONFIGS_DIR are separate, explicit snapshots you can
    save/load on top of that -- same idea as media_matrix.py's State,
    minus the parts (queue, text scroller, panel wiring) that don't apply
    here."""

    def __init__(self, seed=None):
        self.lock = threading.Lock()
        self.values = dict(DEFAULTS)
        if seed:
            self.values.update({k: v for k, v in seed.items() if k in DEFAULTS})
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
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
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
            if "led_rgb_sequence" in data:
                seq = str(data["led_rgb_sequence"]).upper()
                if sorted(seq) == ["B", "G", "R"]:
                    self.values["led_rgb_sequence"] = seq
            if "multiplexing" in data:
                self.values["multiplexing"] = max(0, min(17, int(data["multiplexing"])))
            if "row_address_type" in data:
                self.values["row_address_type"] = max(0, min(4, int(data["row_address_type"])))
            if "panel_type" in data:
                self.values["panel_type"] = str(data["panel_type"])[:32]
            if "pixel_mapper" in data:
                self.values["pixel_mapper"] = str(data["pixel_mapper"])[:64]
        self.save()

    def hw_snapshot(self):
        with self.lock:
            return {k: self.values[k] for k in HW_FIELDS}

    def save_preset(self, name):
        if not NAME_RE.match(name):
            raise ValueError("bad name")
        os.makedirs(CONFIGS_DIR, exist_ok=True)
        with open(os.path.join(CONFIGS_DIR, f"{name}.json"), "w") as f:
            json.dump(self.snapshot(), f, indent=2)

    def load_preset(self, name):
        if not NAME_RE.match(name):
            raise ValueError("bad name")
        with open(os.path.join(CONFIGS_DIR, f"{name}.json")) as f:
            data = json.load(f)
        self.apply(data)  # also autosaves -- this becomes what autoloads next boot

    @staticmethod
    def list_presets():
        if not os.path.isdir(CONFIGS_DIR):
            return []
        return sorted(f[:-5] for f in os.listdir(CONFIGS_DIR) if f.endswith(".json"))


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


def build_matrix(panel, gpio_mapping, gpio_slowdown, pwm_bits, brightness, hw):
    """Same base RGBMatrixOptions as thermal_matrix.py's build_matrix() --
    single 64x64 panel, chain=1, parallel=1, root-owned
    (drop_privileges=False) -- plus the panel-identity options
    thermal_matrix.py doesn't need to set because its panel happens to
    match the library defaults.

    `hw` (a dict shaped like HW_FIELDS) is the part that's live-adjustable
    from the control page -- a wrong color order (blue tint) or wrong
    multiplexing (checkerboard of black squares, scrambled image) is a
    panel/wiring calibration mismatch that varies per panel/chipset, so
    these are exposed as controls rather than hardcoded. They're
    construction-time-only in the underlying library, so the caller is
    expected to rebuild the whole RGBMatrix object (see main()) whenever
    any of them changes, instead of mutating it live."""
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
    opts.led_rgb_sequence = hw["led_rgb_sequence"]
    opts.multiplexing = hw["multiplexing"]
    opts.row_address_type = hw["row_address_type"]
    if hw["panel_type"]:
        opts.panel_type = hw["panel_type"]
    if hw["pixel_mapper"]:
        opts.pixel_mapper_config = hw["pixel_mapper"]
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

<h2 style="margin-top:2rem;font-size:1rem;color:#aaa">image</h2>
<div style="margin-top:1rem">
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

<h2 style="margin-top:2rem;font-size:1rem;color:#aaa">panel hardware</h2>
<p style="color:#888;font-size:.8rem">
  Wrong colors (tint/swap) or a scrambled/checkerboard image is a panel
  calibration mismatch, not a video problem -- these apply live (the
  panel briefly blanks while it rebuilds) so you can dial them in by eye.
</p>
<div style="margin-top:1rem">
  <label>led-rgb-sequence</label><br>
  <select id="led_rgb_sequence" style="width:100%">
    <option>RGB</option><option>RBG</option><option>GRB</option>
    <option>GBR</option><option>BRG</option><option>BGR</option>
  </select>
</div>
<div style="margin-top:1rem">
  <label>multiplexing <span id="multiplexing-v"></span></label><br>
  <input id="multiplexing" type="range" min="0" max="17" style="width:100%">
</div>
<div style="margin-top:1rem">
  <label>row-address-type <span id="row_address_type-v"></span></label><br>
  <input id="row_address_type" type="range" min="0" max="4" style="width:100%">
</div>
<div style="margin-top:1rem">
  <label>panel-type (blank unless needed, e.g. FM6126A)</label><br>
  <input id="panel_type" type="text" style="width:100%;box-sizing:border-box">
</div>
<div style="margin-top:1rem">
  <label>pixel-mapper (blank unless needed, e.g. Rotate:180)</label><br>
  <input id="pixel_mapper" type="text" style="width:100%;box-sizing:border-box">
</div>

<h2 style="margin-top:2rem;font-size:1rem;color:#aaa">presets</h2>
<p style="color:#888;font-size:.8rem">
  Save the settings above under a name; Load applies a saved preset (and
  it becomes what auto-loads next boot, same as any other change here).
</p>
<div style="display:flex;gap:.5rem;margin-top:.5rem">
  <input id="preset_name" type="text" placeholder="preset name" style="flex:1">
  <button id="save_btn">Save</button>
</div>
<div style="display:flex;gap:.5rem;margin-top:.5rem">
  <select id="preset_list" style="flex:1"></select>
  <button id="load_btn">Load</button>
</div>
<p id="preset_msg" style="color:#888;font-size:.8rem;min-height:1.2em"></p>

<script>
const ctx = document.getElementById('prev').getContext('2d');
const RANGE_FIELDS = ['brightness', 'scale_pct', 'offset_x', 'offset_y', 'multiplexing', 'row_address_type'];
const SELECT_FIELDS = ['rotation', 'fit', 'led_rgb_sequence'];
const TEXT_FIELDS = ['panel_type', 'pixel_mapper'];
const ALL_FIELDS = [...RANGE_FIELDS, ...SELECT_FIELDS, ...TEXT_FIELDS];
const state = {};
let applying = false;
let debounceTimer;

// Whole-state POST, debounced 200ms -- same pattern as the double-panel
// (media_matrix.py) control page: one consolidated request per pause in
// dragging, instead of one request per field per input event, and no risk
// of two field updates racing each other out of order.
function send() {
  fetch('/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
                     body: JSON.stringify(state)}).then(pollStatus);
}
function sendDebounced() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(send, 200);
}

for (const id of RANGE_FIELDS) {
  const el = document.getElementById(id);
  el.oninput = () => {
    const v = document.getElementById(id + '-v');
    if (v) v.textContent = el.value;
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
document.getElementById('led_rgb_sequence').onchange = e => {
  state.led_rgb_sequence = e.target.value;
  if (!applying) sendDebounced();
};
for (const id of TEXT_FIELDS) {
  document.getElementById(id).onchange = e => {
    state[id] = e.target.value;
    if (!applying) sendDebounced();
  };
}

async function pollStatus() {
  const s = await (await fetch('/status.json')).json();
  document.getElementById('video').textContent = s.video || '(none found yet)';
  applying = true;
  for (const id of ALL_FIELDS) {
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

async function refreshPresets() {
  const names = await (await fetch('/configs.json')).json();
  const sel = document.getElementById('preset_list');
  sel.innerHTML = names.length
    ? names.map(n => `<option>${n}</option>`).join('')
    : '<option disabled>(no presets saved yet)</option>';
}

document.getElementById('save_btn').onclick = async () => {
  const name = document.getElementById('preset_name').value.trim();
  const msg = document.getElementById('preset_msg');
  if (!name) { msg.textContent = 'enter a name first'; return; }
  const r = await fetch('/save-config', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({name})});
  msg.textContent = r.ok ? `saved "${name}"` : 'save failed (invalid name?)';
  if (r.ok) refreshPresets();
};
document.getElementById('load_btn').onclick = async () => {
  const sel = document.getElementById('preset_list');
  const msg = document.getElementById('preset_msg');
  if (!sel.value) { msg.textContent = 'no preset selected'; return; }
  const r = await fetch('/load-config', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({name: sel.value})});
  msg.textContent = r.ok ? `loaded "${sel.value}"` : 'load failed';
  if (r.ok) pollStatus();
};

pollStatus();
refreshPresets();
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
        elif self.path == "/configs.json":
            self._send(json.dumps(self.state.list_presets()), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode()) if length else {}

    def do_POST(self):
        try:
            data = self._read_json()
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return

        if self.path == "/update":
            self.state.apply(data)
            self._send("ok", "text/plain")
        elif self.path == "/save-config":
            try:
                self.state.save_preset(str(data.get("name", "")).strip())
                self._send("ok", "text/plain")
            except (ValueError, OSError):
                self._send("bad name", "text/plain", code=400)
        elif self.path == "/load-config":
            try:
                self.state.load_preset(str(data.get("name", "")).strip())
                self._send("ok", "text/plain")
            except (ValueError, OSError, json.JSONDecodeError):
                self._send("not found", "text/plain", code=404)
        else:
            self.send_response(404)
            self.end_headers()

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
    p.add_argument("--led-rgb-sequence", default="RGB",
                   help="first-boot seed only -- once the control page has "
                        "saved a config, that wins; edit it there instead")
    p.add_argument("--multiplexing", type=int, default=0, help="first-boot seed only")
    p.add_argument("--row-address-type", type=int, default=0, help="first-boot seed only")
    p.add_argument("--panel-type", default="", help="first-boot seed only")
    p.add_argument("--pixel-mapper", default="", help="first-boot seed only")
    args = p.parse_args()

    seed = {
        "led_rgb_sequence": args.led_rgb_sequence,
        "multiplexing": args.multiplexing,
        "row_address_type": args.row_address_type,
        "panel_type": args.panel_type,
        "pixel_mapper": args.pixel_mapper,
    }
    state = State(seed=seed)
    preview = Preview()

    hw = state.hw_snapshot()
    matrix = build_matrix(args.panel, args.gpio_mapping, args.gpio_slowdown,
                           args.pwm_bits, state.snapshot()["brightness"], hw)
    canvas = matrix.CreateFrameCanvas()

    source = VideoSource(args.video)
    make_control_server(state, preview, args.video, args.web_port)

    print(f"running -- control page on :{args.web_port}, ctrl-c to stop")
    frame_budget = 1.0 / args.fps_cap if args.fps_cap else 0.0
    try:
        while True:
            t0 = time.monotonic()
            snap = state.snapshot()

            new_hw = state.hw_snapshot()
            if new_hw != hw:
                # A calibration field changed on the control page -- these
                # are construction-time-only in the underlying library, so
                # the only way to apply them live is to tear down and
                # rebuild the whole RGBMatrix object. Briefly blanks the
                # panel; that's expected and fine for a rare calibration
                # tweak, not something that happens during normal playback.
                hw = new_hw
                matrix.Clear()
                matrix = build_matrix(args.panel, args.gpio_mapping, args.gpio_slowdown,
                                       args.pwm_bits, snap["brightness"], hw)
                canvas = matrix.CreateFrameCanvas()

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
