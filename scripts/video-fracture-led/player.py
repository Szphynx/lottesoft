#!/usr/bin/env python3
"""
Loop the shared video-fracture video onto a HUB75 RGB LED matrix (Joy-IT
RB-MatrixCtrl + rgbmatrix) -- the same controller/library thermal_matrix.py
uses -- with a live web control page for image controls
(hue/saturation/brightness/rotation/fit/scale/position) and, tucked away
in a collapsed "panel hardware" section since this panel doesn't need
them day to day, calibration knobs (led-rgb-sequence/row-address-type/
panel-type/pixel-mapper) plus service uptime and video elapsed/remaining.
All real-time and all saveable/loadable as named presets. The current
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

Control page: http://<tailscale-ip>:8099/ -- HTTP Basic auth, login in
/etc/default/video-fracture-led-auth (see scripts/video-fracture-led-install.sh).
Binds to the Tailscale IP only (falls back to localhost if Tailscale isn't
up), same posture as scripts/status_server.py -- this page can now reboot
the Pi and pull code from GitHub, so it doesn't get to be open on the LAN
with no login the way the WS2812/MAX7219 control pages are today.
"""

import argparse
import base64
import hmac
import json
import os
import re
import subprocess
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
UPDATE_PENDING_FILE = "/var/lib/video-fracture/led-update-pending"  # written by auto-update.sh
AUTOUPDATE_TIMER = "video-fracture-led-autoupdate.timer"
SERVICE_NAME = "video-fracture-led"

AUTH_USER = os.environ.get("STATUS_USER", "")
AUTH_PASS = os.environ.get("STATUS_PASS", "")

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

# Fields that only change how an already-open frame is drawn -- applied
# live every frame, no rebuild.
IMAGE_FIELDS = {
    "tint": False,         # off: hue/saturation adjust the source's existing
                           # color (no-op on grayscale source). on: colorize --
                           # force every pixel to hue/saturation, using only
                           # brightness from the source (works on grayscale).
    "hue": 0,              # -180..180 degrees
    "saturation": 100,     # 0-200%, meaning depends on tint (see adjust_hsb)
    "brightness": 60,     # 1-100, panel hardware brightness
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
# multiplexing is deliberately not exposed on the control page -- this
# panel doesn't need it (0, the default) -- but stays settable via
# --multiplexing at first boot in case a different panel ever does.
HW_FIELDS = {
    "led_rgb_sequence": "RGB",   # e.g. RGB/RBG/GRB/BGR -- wrong value = color tint/swap
    "multiplexing": 0,            # 0-17, first-boot-only (see above)
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
            if "tint" in data:
                self.values["tint"] = bool(data["tint"])
            if "hue" in data:
                self.values["hue"] = max(-180, min(180, int(data["hue"])))
            if "saturation" in data:
                self.values["saturation"] = max(0, min(200, int(data["saturation"])))
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


def update_pending():
    """Short commit sha auto-update.sh pulled but hasn't been rebooted into
    yet, or None if there's nothing pending."""
    try:
        with open(UPDATE_PENDING_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def autoupdate_status():
    """True/False if the systemd timer's state is known, None if systemd
    or the unit isn't there -- same pattern as media_matrix.py's
    autoupdate_status()."""
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


def reboot():
    """Fire-and-forget -- the process (and this HTTP response) doesn't
    need to survive systemctl actually completing the shutdown."""
    try:
        subprocess.Popen(["systemctl", "reboot"])
        return True
    except OSError:
        return False


def restart_after_reply():
    """Runs in a background thread, called only after the /restart response
    has already gone out -- the restart kills this very process, so the
    reply has to be sent first. Same pattern as double_matrix.py's
    _handle_restart(); this service already runs as root, so no sudo."""
    time.sleep(0.2)
    subprocess.Popen(["systemctl", "restart", SERVICE_NAME])


def bind_addr():
    """Tailscale IP if it's up, else localhost -- same as
    scripts/status_server.py's bind_addr(), so this page is reachable from
    the tailnet only, not the open LAN."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                              text=True, timeout=3).stdout.strip()
        return out or "127.0.0.1"
    except (OSError, subprocess.SubprocessError):
        return "127.0.0.1"


def service_uptime():
    """Seconds since video-fracture-led.service last (re)started, or None
    if that's not knowable (not running under systemd, or never started) --
    same approach as scripts/status_server.py's service_info()."""
    try:
        out = subprocess.run(["systemctl", "show", SERVICE_NAME, "-p", "ActiveEnterTimestamp",
                               "--value"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out == "n/a":
        return None
    try:
        started = time.mktime(time.strptime(out.split(" +")[0], "%a %Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    return time.time() - started


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


def adjust_hsb(frame, hue, saturation_pct, tint):
    """Hue/saturation on an RGB frame, via HSV. Two modes:
      - relative (tint=False, default): scales the frame's EXISTING
        saturation and shifts its EXISTING hue. A no-op on truly
        grayscale source, since there's no saturation there to scale --
        that's not a bug, a 0-saturation pixel has no hue to shift either.
      - tint (tint=True): colorize -- forces every pixel to the chosen
        hue/saturation, keeping only the source's brightness (V) for
        shading. This is how you add color to black & white footage (or
        deliberately flatten already-colorful video to one tone).
    No-op in relative mode at defaults (hue=0, saturation=100), so this
    costs nothing when unused. OpenCV's H channel is 0-179 (each unit =
    2 degrees), so the +-180 degree UI range maps to +-90 there."""
    if not tint and hue == 0 and saturation_pct == 100:
        return frame
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV).astype(np.int16)
    if tint:
        hsv[..., 0] = (hue // 2) % 180
        hsv[..., 1] = max(0, min(255, round(saturation_pct / 100 * 255)))
    else:
        hsv[..., 0] = (hsv[..., 0] + hue // 2) % 180
        hsv[..., 1] = np.clip(hsv[..., 1] * (saturation_pct / 100.0), 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)


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
    file. No IPC needed since we just poll mtime once per frame.

    elapsed/duration (seconds, or None before a video's loaded) are
    updated on every next_frame() call, read from the control-page HTTP
    threads without a lock -- CPython attribute assignment is atomic and
    this is a display-only value, so a rare read mid-update is harmless."""

    def __init__(self, path):
        self.path = path
        self.cap = None
        self.mtime = None
        self.elapsed = None
        self.duration = None
        self._open()

    def _open(self):
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.path) if os.path.exists(self.path) else None
        self.mtime = self._current_mtime()
        fps = self.cap.get(cv2.CAP_PROP_FPS) if self.cap else 0
        frame_count = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) if self.cap else 0
        self.duration = frame_count / fps if fps > 0 and frame_count > 0 else None

    def _current_mtime(self):
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def next_frame(self):
        if self._current_mtime() != self.mtime:
            self._open()
        if self.cap is None or not self.cap.isOpened():
            self.elapsed = None
            return None
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
            if not ok:
                self.elapsed = None
                return None
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.elapsed = self.cap.get(cv2.CAP_PROP_POS_FRAMES) / fps if fps > 0 else None
        return frame


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>video-fracture-led</title>
<body style="font:16px monospace;background:#111;color:#eee;
             max-width:32rem;margin:2rem auto;padding:0 1rem">
<h1 style="font-size:1.1rem">video-fracture-led
  <span id="svcStatus" style="font-size:.7rem;padding:.15rem .5rem;border-radius:1rem;
       vertical-align:middle;margin-left:.5rem;background:#2a4;color:#012">ONLINE</span>
</h1>
<button onclick="restartService()" title="systemctl restart video-fracture-led"
        style="background:#622;color:#fdd;border:none;border-radius:4px;padding:.35rem .7rem;
        cursor:pointer">restart service</button>
<span id="restartMsg" style="font-size:.8rem;color:#888;margin-left:.5rem"></span>

<div id="update_banner" hidden style="background:#3a2f00;border:1px solid #a87c00;border-radius:6px;padding:.75rem 1rem;margin:1rem 0">
  update pulled (<span id="update_sha"></span>) -- reboot to apply.
  <button id="banner_reboot_btn" style="margin-left:.5rem">Reboot now</button>
</div>
<p>video: <b id="video">-</b> &middot; <b id="video_time">-</b></p>
<p style="color:#888">service uptime: <b id="uptime">-</b></p>
<canvas id="prev" width="64" height="64" style="width:256px;height:256px;image-rendering:pixelated;border:1px solid #444;background:#000"></canvas>

<h2 style="margin-top:2rem;font-size:1rem;color:#aaa">image</h2>
<div style="margin-top:1rem">
  <label><input id="tint" type="checkbox"> colorize (tint)</label>
  <p style="color:#888;font-size:.8rem;margin:.25rem 0 0">
    Off: hue/saturation nudge whatever color the video already has (no
    effect on black &amp; white source -- there's no saturation there to
    adjust). On: forces the whole frame to the hue/saturation below,
    using only its brightness for shading -- this is what colorizes
    black &amp; white footage.
  </p>
</div>
<div style="margin-top:1rem">
  <label>hue <span id="hue-v"></span></label><br>
  <input id="hue" type="range" min="-180" max="180" style="width:100%">
</div>
<div style="margin-top:1rem">
  <label>saturation % <span id="saturation-v"></span></label><br>
  <input id="saturation" type="range" min="0" max="200" style="width:100%">
</div>
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

<details style="margin-top:2rem">
  <summary style="font-size:1rem;color:#aaa;cursor:pointer">panel hardware (rarely needed)</summary>
  <p style="color:#888;font-size:.8rem">
    Wrong colors (tint/swap) or a scrambled/checkerboard image is a panel
    calibration mismatch, not a video problem -- these apply live (the
    panel briefly blanks while it rebuilds) so you can dial them in by eye.
    This panel doesn't need multiplexing, so that's not here -- it's still
    settable via --multiplexing at first boot if a different panel ever does.
  </p>
  <div style="margin-top:1rem">
    <label>led-rgb-sequence</label><br>
    <select id="led_rgb_sequence" style="width:100%">
      <option>RGB</option><option>RBG</option><option>GRB</option>
      <option>GBR</option><option>BRG</option><option>BGR</option>
    </select>
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
</details>

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

<h2 style="margin-top:2rem;font-size:1rem;color:#aaa">system</h2>
<div style="margin-top:.5rem">
  <label><input id="autoupdate" type="checkbox"> check for code updates automatically</label>
  <p style="color:#888;font-size:.8rem;margin:.25rem 0 0">
    When on, this Pi checks GitHub every couple minutes and pulls new
    commits -- it does NOT restart or reboot on its own; a banner appears
    up top when a reboot is needed to apply what it pulled.
  </p>
</div>
<div style="margin-top:1rem">
  <button id="reboot_btn">Reboot this Pi</button>
</div>
<p id="system_msg" style="color:#888;font-size:.8rem;min-height:1.2em"></p>

<script>
const ctx = document.getElementById('prev').getContext('2d');
const RANGE_FIELDS = ['hue', 'saturation', 'brightness', 'scale_pct', 'offset_x', 'offset_y', 'row_address_type'];
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
document.getElementById('tint').onchange = e => {
  state.tint = e.target.checked;
  if (!applying) sendDebounced();
};

// Liveness badge, inferred from whether the regular polls succeed --
// same pattern as double_matrix.py's setSvcStatus(): only flips to
// OFFLINE after 2 consecutive failures (so one dropped request doesn't
// flash it red), and shows a "back online" message when it recovers.
// No separate health endpoint needed -- these polls already run anyway.
let svcFails = 0, svcWasDown = false;
function setSvcStatus(online) {
  const el = document.getElementById('svcStatus');
  if (online) {
    svcFails = 0;
    el.textContent = 'ONLINE'; el.style.background = '#2a4'; el.style.color = '#012';
    if (svcWasDown) {
      document.getElementById('restartMsg').textContent = 'back online';
      svcWasDown = false;
    }
  } else if (++svcFails >= 2) {
    el.textContent = 'OFFLINE'; el.style.background = '#a22'; el.style.color = '#fdd';
    svcWasDown = true;
  }
}

function restartService() {
  if (!confirm('Restart the video-fracture-led service now?')) return;
  document.getElementById('restartMsg').textContent = 'restarting...';
  fetch('/restart', {method: 'POST'}).catch(() => {});
}

function fmtDuration(totalSeconds) {
  if (totalSeconds == null) return '-';
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = n => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

async function pollStatus() {
  let s;
  try {
    s = await (await fetch('/status.json')).json();
    setSvcStatus(true);
  } catch {
    setSvcStatus(false);
    return;
  }
  document.getElementById('video').textContent = s.video || '(none found yet)';
  document.getElementById('uptime').textContent = fmtDuration(s.service_uptime);
  if (s.video_elapsed != null && s.video_duration != null) {
    const remaining = s.video_duration - s.video_elapsed;
    document.getElementById('video_time').textContent =
      `${fmtDuration(s.video_elapsed)} elapsed, ${fmtDuration(remaining)} remaining`;
  } else {
    document.getElementById('video_time').textContent = '';
  }
  applying = true;
  for (const id of ALL_FIELDS) {
    state[id] = s[id];
    document.getElementById(id).value = s[id];
    const v = document.getElementById(id + '-v');
    if (v) v.textContent = s[id];
  }
  state.tint = s.tint;
  document.getElementById('tint').checked = !!s.tint;
  applying = false;

  const banner = document.getElementById('update_banner');
  banner.hidden = !s.update_pending;
  if (s.update_pending) document.getElementById('update_sha').textContent = s.update_pending;

  const cb = document.getElementById('autoupdate');
  cb.disabled = s.autoupdate_enabled === null;
  if (!cb.matches(':focus')) cb.checked = !!s.autoupdate_enabled;
}

async function pollFrame() {
  let f;
  try {
    f = await (await fetch('/frame.json')).json();
    setSvcStatus(true);
  } catch {
    setSvcStatus(false);
    return;
  }
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

document.getElementById('autoupdate').onchange = async e => {
  const msg = document.getElementById('system_msg');
  const r = await fetch('/autoupdate', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify({enabled: e.target.checked})});
  const body = await r.json();
  msg.textContent = body.ok ? '' : 'failed to change -- check systemd/journalctl on the Pi';
  pollStatus();
};

async function doReboot() {
  if (!confirm('Reboot this Pi now? The display and this page will go down for a bit.')) return;
  await fetch('/reboot', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  document.getElementById('system_msg').textContent = 'rebooting...';
}
document.getElementById('reboot_btn').onclick = doReboot;
document.getElementById('banner_reboot_btn').onclick = doReboot;

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
    video_source: VideoSource = None

    def _send(self, body, content_type, code=200):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        """Same scheme as scripts/status_server.py: HTTP Basic against
        STATUS_USER/STATUS_PASS, constant-time compare. Empty AUTH_USER
        (no /etc/default/video-fracture-led-auth yet) leaves it open --
        matches status_server.py's own fallback, and fails safe towards
        "can't reach it" rather than "locked out during setup"."""
        if not AUTH_USER:
            return True
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="video-fracture-led"')
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self._unauthorized()
            return
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/status.json":
            snap = self.state.snapshot()
            snap["video"] = os.path.basename(self.source_path) if os.path.exists(self.source_path) else None
            snap["update_pending"] = update_pending()
            snap["autoupdate_enabled"] = autoupdate_status()
            snap["service_uptime"] = service_uptime()
            snap["video_elapsed"] = self.video_source.elapsed
            snap["video_duration"] = self.video_source.duration
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
        if not self._authorized():
            self._unauthorized()
            return
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
        elif self.path == "/autoupdate":
            ok = set_autoupdate(bool(data.get("enabled")))
            self._send(json.dumps({"ok": ok, "enabled": autoupdate_status()}), "application/json")
        elif self.path == "/reboot":
            self._send(json.dumps({"ok": reboot()}), "application/json")
        elif self.path == "/restart":
            # Reply first -- the restart kills this very process, so the
            # actual systemctl call happens in a thread after the response
            # is already on the wire (same pattern as double_matrix.py's
            # _handle_restart).
            self._send("restarting", "text/plain")
            threading.Thread(target=restart_after_reply, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def make_control_server(state, preview, video_source, port):
    ControlHandler.state = state
    ControlHandler.preview = preview
    ControlHandler.source_path = video_source.path
    ControlHandler.video_source = video_source
    server = ThreadingHTTPServer((bind_addr(), port), ControlHandler)
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

    # A fresh process start means we're already running whatever
    # auto-update.sh last pulled (a full reboot restarts this service too),
    # so any pending-update marker from before is stale.
    try:
        os.remove(UPDATE_PENDING_FILE)
    except FileNotFoundError:
        pass

    hw = state.hw_snapshot()
    matrix = build_matrix(args.panel, args.gpio_mapping, args.gpio_slowdown,
                           args.pwm_bits, state.snapshot()["brightness"], hw)
    canvas = matrix.CreateFrameCanvas()

    source = VideoSource(args.video)
    make_control_server(state, preview, source, args.web_port)

    print(f"running -- control page on http://{bind_addr()}:{args.web_port}/, ctrl-c to stop")
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
                rgb = adjust_hsb(rgb, snap["hue"], snap["saturation"], snap["tint"])
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
