#!/usr/bin/env python3
"""
Pi Camera Module 3 (CSI ribbon slot) -> live display over HDMI, on a
Raspberry Pi. Emulates the classic analog security/IR-cam look
(blown-out monochrome, CRT scanlines, a subtle lens vignette) by default,
with motion-blob detection boxing anything that moves.

Runs as a normal fullscreen app inside the desktop session (X11 or
Wayland) -- same technique as a fullscreen video-loop player: launched via
a desktop autostart entry so it comes up after login, but the desktop
itself stays usable (switch away with alt-tab, ssh in, etc). See
scripts/install-security-cam-hdmi.sh, which installs the autostart entry.

Run by hand with:
    python3 security_cam_hdmi.py

Tuned to be cheap enough for a Raspberry Pi 3 with no extra hardware (no
Coral/TPU), not just the Pi 4. There's no real person detector: a
moving-blob box is a cheap stand-in, not a person classifier, so it'll
also box pets, shadows, and waving branches.

Also serves a small live control page (like scripts/status_server.py's
status page, same bind-to-tailscale-IP + basic-auth pattern) with a
low-fps preview image and sliders/toggles for everything below that isn't
resolution/rotation -- those still need a camera reconfigure, so they stay
CLI-only. Set CAM_USER / CAM_PASS env vars to require a login; unset means
anyone who can reach the port can control the camera, so set them if the
Pi is reachable beyond your own tailnet.

Useful flags:
    --resolution 640x480    camera capture size (lower = faster on a Pi 3)
    --rotate 0|90|180|270
    --hflip / --vflip       initial flip state (also a live control-page toggle)
    --color                 full color instead of the default infrared look
    --mono                  grayscale instead of the default infrared look
    --palette ironbow|whitehot|blackhot|rainbow|redhot|bodyheat|colorwise|infrared|<opencv colormap name>
                            false-color look (default: infrared)
    --gamma 0.7             only affects --mono / --palette
    --no-agc                disable auto-contrast in --mono / --palette modes
    --no-fisheye            disable the fisheye warp + vignette (on by
                            default) -- toggle this to check how much it
                            costs on your hardware
    --fisheye-strength 0.03    0 = none, higher = more barrel distortion
    --vignette-strength 0.35   0 = none, higher = darker corners
    --no-motion             disable motion-blob detection (on by default)
    --no-crt-lines          disable the fine scanline overlay (on by default)
    --no-web                disable the control page
    --web-port 8790
    --stats                 print render fps once a second
    q or Esc in the window quits

All of the above except --resolution/--rotate/--stats/--no-web/--web-port
are also live controls on the web page, and take effect immediately
without restarting.
"""

import argparse
import base64
import hmac
import http.server
import json
import os
import subprocess
import threading
import time

import numpy as np
import cv2


# ----------------------------------------------------------------------------
# Tunables (only used by --mono / --palette).
# ----------------------------------------------------------------------------

GAMMA = 0.70
AGC_LOW_PCT = 2.0
AGC_HIGH_PCT = 98.0
AGC_ALPHA = 0.1          # EMA on the range; lower = steadier, slower to adapt
MIN_SPAN = 20.0          # never stretch a span narrower than this (0-255 units)

DEFAULT_PALETTE = "infrared"

# Power law (not r^2) so the warp stays nearly flat near the center and
# only bends noticeably toward the corners, like a mild real lens rather
# than a fully spherical fisheye. dx/dy are normalized by each axis's own
# half-extent (not a shared min(cx,cy) radius), so r=1 lands evenly on
# all four edge midpoints regardless of the frame's aspect ratio -- that's
# what keeps the bulge reading as circular instead of stretched
# horizontally on a wide rectangular frame.
FISHEYE_STRENGTH = 0.03
FISHEYE_POWER = 4
FISHEYE_STRENGTH_MAX = 0.2

VIGNETTE_STRENGTH = 0.35
VIGNETTE_MIN = 0.25     # corners never darken past this fraction of brightness

SCANLINE_STRENGTH = 0.15   # scanline darkness, applied at native display resolution
SCANLINE_STRENGTH_MAX = 0.5

# Motion/blob detection runs on a downscaled copy to stay cheap on a Pi 3;
# boxes are scaled back up to full frame size afterward.
MOTION_SCALE = 0.35
MOTION_MIN_AREA_FRAC = 0.02   # fraction of the *downscaled* frame area
MOTION_HISTORY = 300
MOTION_VAR_THRESHOLD = 24

CAM_CONTROL_PORT = 8790
PREVIEW_INTERVAL = 0.35   # ~3 fps -- deliberately slow, this is a monitor page, not the display
PREVIEW_WIDTH = 320
PREVIEW_JPEG_QUALITY = 70


# ----------------------------------------------------------------------------
# False-color palettes for --palette. Anchors are (position 0-1, (R, G, B)).
# ----------------------------------------------------------------------------

PALETTES = {
    "ironbow": [
        (0.00, (0, 0, 0)),       (0.13, (28, 0, 73)),
        (0.25, (72, 0, 124)),    (0.38, (135, 0, 131)),
        (0.50, (186, 25, 102)),  (0.63, (222, 73, 53)),
        (0.75, (245, 131, 10)),  (0.88, (253, 197, 7)),
        (1.00, (255, 255, 255)),
    ],
    "whitehot": [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))],
    "blackhot": [(0.0, (255, 255, 255)), (1.0, (0, 0, 0))],
    "rainbow": [
        (0.00, (0, 0, 0)),       (0.15, (43, 31, 143)),
        (0.30, (26, 114, 212)),  (0.45, (13, 191, 192)),
        (0.60, (123, 236, 70)),  (0.75, (246, 200, 31)),
        (0.90, (238, 79, 60)),   (1.00, (255, 255, 255)),
    ],
    "redhot": [
        (0.00, (0, 0, 0)),       (0.15, (40, 0, 0)),
        (0.35, (120, 10, 5)),    (0.55, (200, 30, 10)),
        (0.72, (230, 80, 15)),   (0.85, (250, 140, 40)),
        (1.00, (255, 235, 180)),
    ],
    "bodyheat": [
        (0.000, (14, 18, 30)),   (0.625, (140, 155, 180)),
        (0.646, (200, 45, 10)),  (0.667, (255, 25, 0)),
        (0.704, (255, 90, 10)),  (0.708, (255, 200, 130)),
    ],
    "colorwise": [
        (0.000, (0, 0, 0)),      (0.625, (32, 32, 34)),
        (0.646, (200, 45, 10)),  (0.667, (255, 25, 0)),
        (0.704, (255, 90, 10)),  (0.708, (255, 200, 130)),
    ],
    "infrared": [
        # Classic analog IR security-cam look: mostly black, but brightness
        # rises fast and clips to blown-out white well before full scale,
        # so reflective/warm surfaces bloom out white instead of just
        # looking bright grey.
        (0.00, (0, 0, 0)),       (0.30, (60, 60, 60)),
        (0.50, (150, 150, 150)), (0.65, (220, 220, 220)),
        (0.80, (255, 255, 255)), (1.00, (255, 255, 255)),
    ],
}

CV2_COLORMAPS = ["inferno", "magma", "turbo", "jet", "hot", "bone", "ocean"]


def build_lut(anchors, gamma=1.0):
    """256-entry uint8 RGB lookup table with gamma folded in."""
    pos = np.array([a[0] for a in anchors], dtype=np.float64)
    cols = np.array([a[1] for a in anchors], dtype=np.float64)
    x = np.linspace(0.0, 1.0, 256) ** gamma
    lut = np.stack([np.interp(x, pos, cols[:, c]) for c in range(3)], axis=1)
    return np.clip(lut, 0, 255).astype(np.uint8)


def build_cv2_lut(name, gamma=1.0):
    """Pull one of OpenCV's built-in colormaps into the same LUT format."""
    ramp = np.arange(256, dtype=np.uint8).reshape(256, 1)
    bgr = cv2.applyColorMap(ramp, getattr(cv2, f"COLORMAP_{name.upper()}"))
    rgb = bgr[:, 0, ::-1].astype(np.float64)
    x = (np.linspace(0.0, 1.0, 256) ** gamma) * 255.0
    src = np.arange(256, dtype=np.float64)
    out = np.stack([np.interp(x, src, rgb[:, c]) for c in range(3)], axis=1)
    return np.clip(out, 0, 255).astype(np.uint8)


def get_lut(name, gamma):
    if name in PALETTES:
        return build_lut(PALETTES[name], gamma)
    return build_cv2_lut(name, gamma)


def valid_palette(name):
    try:
        get_lut(name, 1.0)
        return True
    except AttributeError:
        return False


# ----------------------------------------------------------------------------
# Camera (CSI ribbon slot, via picamera2/libcamera).
# ----------------------------------------------------------------------------

class Camera:
    def __init__(self, size):
        from picamera2 import Picamera2

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(main={"size": size, "format": "RGB888"})
        self.picam2.configure(config)
        self.picam2.start()
        time.sleep(1.0)  # let AE/AWB settle before the first read

    def read_bgr(self):
        # picamera2's "RGB888" format is actually laid out BGR (a
        # long-standing quirk kept for OpenCV compatibility).
        return self.picam2.capture_array()

    def close(self):
        self.picam2.stop()


# ----------------------------------------------------------------------------
# Auto-contrast (percentile stretch with an EMA, so it doesn't pulse).
# ----------------------------------------------------------------------------

class Agc:
    def __init__(self, low_pct=AGC_LOW_PCT, high_pct=AGC_HIGH_PCT,
                 alpha=AGC_ALPHA, min_span=MIN_SPAN):
        self.low_pct = low_pct
        self.high_pct = high_pct
        self.alpha = alpha
        self.min_span = min_span
        self.lo = None
        self.hi = None

    def normalize(self, gray):
        lo, hi = np.percentile(gray, [self.low_pct, self.high_pct])
        if hi - lo < self.min_span:
            mid = 0.5 * (hi + lo)
            lo, hi = mid - self.min_span / 2.0, mid + self.min_span / 2.0

        if self.lo is None:
            self.lo, self.hi = lo, hi
        else:
            self.lo += self.alpha * (lo - self.lo)
            self.hi += self.alpha * (hi - self.hi)

        return np.clip((gray.astype(np.float32) - self.lo) / max(self.hi - self.lo, 1e-6), 0.0, 1.0)


# ----------------------------------------------------------------------------
# Fake fisheye lens effect + vignette -- purely cosmetic, both built once
# per (frame size, strength) combination, not per frame.
# ----------------------------------------------------------------------------

def _radial_grid(size):
    """Normalised (dx, dy, r) grids. dx/dy are each divided by their OWN
    half-extent (cx/cy independently, not a shared min(cx,cy) radius), so
    r=1 lands evenly on all four edge midpoints and r=sqrt(2) on all four
    corners regardless of the frame's aspect ratio -- an isotropic, evenly
    "circular" falloff instead of one that reaches further on the wide
    axis of a rectangular frame.
    """
    w, h = size
    cx, cy = w / 2.0, h / 2.0

    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = (xs - cx) / cx
    dy = (ys - cy) / cy
    r = np.sqrt(dx * dx + dy * dy)
    return dx, dy, r, cx, cy


def build_fisheye_maps(size, strength, power=FISHEYE_POWER):
    dx, dy, r, cx, cy = _radial_grid(size)
    factor = 1.0 + strength * (r ** power)

    map_x = (dx * factor) * cx + cx
    map_y = (dy * factor) * cy + cy
    return map_x.astype(np.float32), map_y.astype(np.float32)


def build_vignette(size, strength=VIGNETTE_STRENGTH, min_mult=VIGNETTE_MIN):
    """(h, w) float32 brightness multiplier, 1.0 at center, darker at corners."""
    _, _, r, _, _ = _radial_grid(size)
    mask = 1.0 - strength * (r ** 2)
    return np.clip(mask, min_mult, 1.0).astype(np.float32)


# ----------------------------------------------------------------------------
# Motion/blob detection: background subtraction on a downscaled copy, so
# it stays cheap enough for a Pi 3. Not a person detector -- any moving
# blob above the size threshold gets boxed.
# ----------------------------------------------------------------------------

class MotionDetector:
    def __init__(self, frame_size, scale=MOTION_SCALE,
                 min_area_frac=MOTION_MIN_AREA_FRAC):
        w, h = frame_size
        self.small_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        self.scale_back = 1.0 / scale
        self.min_area = min_area_frac * self.small_size[0] * self.small_size[1]
        self.bgsub = cv2.createBackgroundSubtractorMOG2(
            history=MOTION_HISTORY, varThreshold=MOTION_VAR_THRESHOLD,
            detectShadows=False,
        )
        self.kernel = np.ones((3, 3), np.uint8)

    def detect(self, gray):
        small = cv2.resize(gray, self.small_size, interpolation=cv2.INTER_AREA)
        mask = self.bgsub.apply(small)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.dilate(mask, self.kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for c in contours:
            if cv2.contourArea(c) < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            boxes.append((int(x * self.scale_back), int(y * self.scale_back),
                          int(w * self.scale_back), int(h * self.scale_back)))
        return boxes


# ----------------------------------------------------------------------------
# Display (a plain fullscreen window via pygame/SDL -- x11/wayland when run
# inside a desktop session).
# ----------------------------------------------------------------------------

class Display:
    def __init__(self):
        import pygame
        self.pygame = pygame
        pygame.display.init()
        pygame.mouse.set_visible(False)
        self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        self.size = self.screen.get_size()
        self.scanlines = None
        self._scanline_strength = 0.0

    def set_scanlines(self, strength):
        """Rebuilds the overlay only when the strength actually changed --
        built once at the display's real pixel resolution (not the
        camera's, which is usually much smaller and would otherwise scale
        each source row into a chunky multi-pixel band), so a single
        alpha-blended blit is far cheaper per frame than darkening rows on
        every source frame before the upscale.
        """
        if strength == self._scanline_strength:
            return
        self._scanline_strength = strength
        if strength <= 0:
            self.scanlines = None
            return
        w, h = self.size
        overlay = self.pygame.Surface(self.size, self.pygame.SRCALPHA)
        alpha = int(255 * strength)
        for y in range(1, h, 2):
            self.pygame.draw.line(overlay, (0, 0, 0, alpha), (0, y), (w, y))
        self.scanlines = overlay

    def show(self, rgb):
        h, w = rgb.shape[:2]
        surf = self.pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")
        if (w, h) != self.size:
            surf = self.pygame.transform.scale(surf, self.size)
        self.screen.blit(surf, (0, 0))
        if self.scanlines is not None:
            self.screen.blit(self.scanlines, (0, 0))
        self.pygame.display.flip()

    def quit_requested(self):
        for event in self.pygame.event.get():
            if event.type == self.pygame.QUIT:
                return True
            if event.type == self.pygame.KEYDOWN and event.key in (
                self.pygame.K_ESCAPE, self.pygame.K_q,
            ):
                return True
        return False

    def close(self):
        self.pygame.quit()


# ----------------------------------------------------------------------------
# Live, thread-safe settings shared between the render loop and the web
# control page. CLI flags set the starting values; everything here except
# resolution/rotation can change while running.
# ----------------------------------------------------------------------------

class Settings:
    def __init__(self, args):
        self.lock = threading.Lock()
        self.mode = "mono" if args.mono else ("palette" if args.palette else "color")
        self.palette_name = args.palette or DEFAULT_PALETTE
        self.gamma = args.gamma
        self.agc = not args.no_agc
        self.fisheye = not args.no_fisheye
        self.fisheye_strength = args.fisheye_strength
        self.vignette_strength = args.vignette_strength
        self.crt_lines = not args.no_crt_lines
        self.crt_strength = SCANLINE_STRENGTH
        self.motion = not args.no_motion
        self.hflip = args.hflip
        self.vflip = args.vflip
        self.hue = 0.0
        self.saturation = 1.0
        self.brightness = 1.0

    def snapshot(self):
        with self.lock:
            return dict(vars(self))  # vars() includes .lock itself, harmless to copy the ref

    def update(self, **kwargs):
        with self.lock:
            if "mode" in kwargs and kwargs["mode"] in ("color", "mono", "palette"):
                self.mode = kwargs["mode"]
            if "palette_name" in kwargs and valid_palette(kwargs["palette_name"]):
                self.palette_name = kwargs["palette_name"]
            if "gamma" in kwargs:
                self.gamma = _clamp(float(kwargs["gamma"]), 0.2, 3.0)
            if "fisheye_strength" in kwargs:
                self.fisheye_strength = _clamp(float(kwargs["fisheye_strength"]), 0.0, FISHEYE_STRENGTH_MAX)
            if "vignette_strength" in kwargs:
                self.vignette_strength = _clamp(float(kwargs["vignette_strength"]), 0.0, 1.0)
            if "crt_strength" in kwargs:
                self.crt_strength = _clamp(float(kwargs["crt_strength"]), 0.0, SCANLINE_STRENGTH_MAX)
            if "hue" in kwargs:
                self.hue = _clamp(float(kwargs["hue"]), -180.0, 180.0)
            if "saturation" in kwargs:
                self.saturation = _clamp(float(kwargs["saturation"]), 0.0, 3.0)
            if "brightness" in kwargs:
                self.brightness = _clamp(float(kwargs["brightness"]), 0.0, 3.0)
            for flag in ("agc", "fisheye", "crt_lines", "motion", "hflip", "vflip"):
                if flag in kwargs:
                    setattr(self, flag, bool(kwargs[flag]))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ----------------------------------------------------------------------------
# Low-fps JPEG preview shared with the web page, and the page itself.
# ----------------------------------------------------------------------------

class PreviewBuffer:
    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = b""

    def set(self, jpeg_bytes):
        with self.lock:
            self.jpeg = jpeg_bytes

    def get(self):
        with self.lock:
            return self.jpeg


CONTROL_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>security cam control</title>
<style>
  body { font: 14px/1.4 monospace; background:#111; color:#eee; margin:0; padding:1rem;
         display:flex; flex-wrap:wrap; gap:1.5rem; }
  h1 { font-size:1rem; margin:0 0 .75rem; color:#8f8; }
  #preview { width:480px; max-width:90vw; background:#000; border:1px solid #333; display:block; }
  fieldset { border:1px solid #333; border-radius:6px; min-width:260px; }
  legend { color:#8f8; padding:0 .4rem; }
  label { display:flex; align-items:center; gap:.5rem; margin:.4rem 0; justify-content:space-between; }
  label span:first-child { flex:0 0 auto; }
  input[type=range] { flex:1; }
  select { background:#222; color:#eee; border:1px solid #444; }
  .row { display:flex; gap:.5rem; align-items:center; }
  output { min-width:3.5em; text-align:right; color:#9cf; }
</style>
<div>
  <h1>live preview (low fps, doesn't affect the HDMI display)</h1>
  <img id="preview" alt="preview">
</div>
<form id="controls">
  <fieldset>
    <legend>look</legend>
    <label><span>mode</span>
      <select name="mode" id="mode">
        <option value="color">color</option>
        <option value="mono">mono</option>
        <option value="palette">palette</option>
      </select>
    </label>
    <label><span>palette</span>
      <select name="palette_name" id="palette_name"></select>
    </label>
    <label><span>gamma</span><input type="range" name="gamma" min="0.2" max="3" step="0.05"><output></output></label>
    <label><span>agc</span><input type="checkbox" name="agc"></label>
  </fieldset>
  <fieldset>
    <legend>lens</legend>
    <label><span>fisheye</span><input type="checkbox" name="fisheye"></label>
    <label><span>fisheye strength</span><input type="range" name="fisheye_strength" min="0" max="0.2" step="0.005"><output></output></label>
    <label><span>vignette</span><input type="range" name="vignette_strength" min="0" max="1" step="0.02"><output></output></label>
  </fieldset>
  <fieldset>
    <legend>monitor</legend>
    <label><span>crt lines</span><input type="checkbox" name="crt_lines"></label>
    <label><span>crt strength</span><input type="range" name="crt_strength" min="0" max="0.5" step="0.01"><output></output></label>
    <label><span>motion boxes</span><input type="checkbox" name="motion"></label>
  </fieldset>
  <fieldset>
    <legend>orientation</legend>
    <label><span>flip horizontal</span><input type="checkbox" name="hflip"></label>
    <label><span>flip vertical</span><input type="checkbox" name="vflip"></label>
  </fieldset>
  <fieldset>
    <legend>final image</legend>
    <label><span>hue</span><input type="range" name="hue" min="-180" max="180" step="1"><output></output></label>
    <label><span>saturation</span><input type="range" name="saturation" min="0" max="3" step="0.05"><output></output></label>
    <label><span>brightness</span><input type="range" name="brightness" min="0" max="3" step="0.05"><output></output></label>
  </fieldset>
</form>
<script>
const PALETTES = __PALETTES__;
const paletteSel = document.getElementById("palette_name");
PALETTES.forEach(p => paletteSel.add(new Option(p, p)));

const form = document.getElementById("controls");
let applying = false;

function setFormValues(state) {
  applying = true;
  for (const el of form.elements) {
    if (!(el.name in state)) continue;
    if (el.type === "checkbox") el.checked = !!state[el.name];
    else el.value = state[el.name];
    const out = el.parentElement.querySelector("output");
    if (out) out.textContent = el.value;
  }
  applying = false;
}

async function refreshState() {
  const r = await fetch("/state");
  setFormValues(await r.json());
}

async function sendChange(name, value) {
  await fetch("/set", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({[name]: value}),
  });
}

form.addEventListener("input", (e) => {
  if (applying) return;
  const el = e.target;
  const out = el.parentElement.querySelector("output");
  if (out) out.textContent = el.value;
  const value = el.type === "checkbox" ? el.checked
              : el.type === "range" ? parseFloat(el.value)
              : el.value;
  sendChange(el.name, value);
});

function tickPreview() {
  document.getElementById("preview").src = "/preview.jpg?t=" + Date.now();
}
setInterval(tickPreview, __PREVIEW_MS__);
tickPreview();
refreshState();
</script>
"""


class ControlHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if not self._authorized():
            return self._unauthorized()
        if self.path.startswith("/preview.jpg"):
            self._send(self.server.preview.get(), "image/jpeg")
        elif self.path.startswith("/state"):
            state = {k: v for k, v in self.server.settings.snapshot().items() if k != "lock"}
            self._send(json.dumps(state).encode(), "application/json")
        elif self.path == "/" or self.path.startswith("/index"):
            palettes = sorted(PALETTES.keys()) + CV2_COLORMAPS
            html = (CONTROL_PAGE
                    .replace("__PALETTES__", json.dumps(palettes))
                    .replace("__PREVIEW_MS__", str(int(PREVIEW_INTERVAL * 1000))))
            self._send(html.encode(), "text/html")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._authorized():
            return self._unauthorized()
        if self.path.startswith("/set"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                data = {}
            self.server.settings.update(**data)
            state = {k: v for k, v in self.server.settings.snapshot().items() if k != "lock"}
            self._send(json.dumps(state).encode(), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _authorized(self):
        user = os.environ.get("CAM_USER", "")
        if not user:
            return True
        password = os.environ.get("CAM_PASS", "")
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="security-cam"')
        self.end_headers()

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ControlServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, settings, preview):
        super().__init__(addr, ControlHandler)
        self.settings = settings
        self.preview = preview


def tailscale_or_local_ip():
    try:
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        ip = ""
    return ip or "127.0.0.1"


# ----------------------------------------------------------------------------
# Main.
# ----------------------------------------------------------------------------

def parse_resolution(s):
    w, h = s.lower().split("x")
    return (int(w), int(h))


def apply_hsb(rgb, hue_deg, saturation, brightness):
    if hue_deg == 0 and saturation == 1.0 and brightness == 1.0:
        return rgb
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[:, :, 0] = (hsv[:, :, 0] + hue_deg / 2.0) % 180.0  # OpenCV hue is 0-179
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 255)
    hsv[:, :, 2] = np.clip(hsv[:, :, 2] * brightness, 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)


def main():
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--color", action="store_true",
                       help="full color instead of the default infrared look")
    mode.add_argument("--mono", action="store_true",
                       help="grayscale instead of the default infrared look, with auto-contrast")
    mode.add_argument("--palette", default=None,
                       help="false-color look instead of the default infrared: "
                            "ironbow, whitehot, blackhot, rainbow, redhot, "
                            "bodyheat, colorwise, infrared, or any "
                            "OpenCV colormap name such as inferno / magma / turbo")
    p.add_argument("--resolution", type=parse_resolution, default=(640, 480),
                   help="lower is faster -- default is chosen to run in "
                        "real time on a Pi 3 with motion+fisheye on")
    p.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    p.add_argument("--hflip", action="store_true")
    p.add_argument("--vflip", action="store_true")
    p.add_argument("--gamma", type=float, default=GAMMA,
                   help="only affects --mono / --palette")
    p.add_argument("--no-agc", action="store_true",
                   help="disable auto-contrast in --mono / --palette modes")
    p.add_argument("--no-fisheye", action="store_true",
                   help="disable the fisheye warp + vignette (on by default)")
    p.add_argument("--fisheye-strength", type=float, default=FISHEYE_STRENGTH)
    p.add_argument("--vignette-strength", type=float, default=VIGNETTE_STRENGTH)
    p.add_argument("--no-motion", action="store_true",
                   help="disable motion-blob detection (on by default)")
    p.add_argument("--no-crt-lines", action="store_true",
                   help="disable the scanline overlay (on by default)")
    p.add_argument("--no-web", action="store_true",
                   help="disable the live control page")
    p.add_argument("--web-port", type=int, default=CAM_CONTROL_PORT)
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    if not args.mono and not args.palette and not args.color:
        args.palette = DEFAULT_PALETTE

    rotate_k = (args.rotate // 90) % 4
    settings = Settings(args)

    print(f"starting camera at {args.resolution[0]}x{args.resolution[1]}...")
    camera = Camera(args.resolution)

    print("opening display...")
    display = Display()
    print(f"  {display.size[0]}x{display.size[1]}")

    # Frame size after rotation -- rot90 swaps width/height on a 90/270
    # turn, and the fisheye map / vignette / motion detector are all built
    # for that fixed size (resolution and rotation stay CLI-only, not
    # live-editable -- changing them means reconfiguring the camera).
    w, h = args.resolution
    frame_size = (h, w) if rotate_k in (1, 3) else (w, h)
    motion = MotionDetector(frame_size)
    agc = Agc()

    preview = PreviewBuffer()
    server = None
    if not args.no_web:
        bind_ip = tailscale_or_local_ip()
        try:
            server = ControlServer((bind_ip, args.web_port), settings, preview)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"control page: http://{bind_ip}:{args.web_port}/")
            if not os.environ.get("CAM_USER"):
                print("  (no CAM_USER/CAM_PASS set -- anyone who can reach this port can control the camera)")
        except OSError as e:
            print(f"control page disabled: couldn't bind {bind_ip}:{args.web_port} ({e})")

    # Caches keyed by the settings that actually require rebuilding
    # something expensive -- most controls are cheap to apply per-frame,
    # but the fisheye/vignette maps are full-frame-sized arrays.
    cache = {"lut_key": None, "lut": None, "fisheye_key": None, "fisheye_maps": None,
             "vignette_key": None, "vignette_3ch": None}

    frames = 0
    last_report = time.monotonic()
    last_preview = 0.0

    print("running -- ctrl-c (or q/Esc with a keyboard attached) to stop")
    try:
        while True:
            snap = settings.snapshot()

            bgr = camera.read_bgr()
            if rotate_k:
                bgr = np.rot90(bgr, rotate_k, axes=(0, 1))
            if snap["hflip"] or snap["vflip"]:
                axes = tuple(a for a, f in ((0, snap["vflip"]), (1, snap["hflip"])) if f)
                bgr = np.ascontiguousarray(np.flip(bgr, axis=axes))

            if snap["fisheye"]:
                if cache["fisheye_key"] != snap["fisheye_strength"]:
                    cache["fisheye_key"] = snap["fisheye_strength"]
                    cache["fisheye_maps"] = build_fisheye_maps(frame_size, snap["fisheye_strength"])
                if cache["vignette_key"] != snap["vignette_strength"]:
                    cache["vignette_key"] = snap["vignette_strength"]
                    cache["vignette_3ch"] = build_vignette(frame_size, snap["vignette_strength"])[:, :, None]
                map_x, map_y = cache["fisheye_maps"]
                bgr = cv2.remap(bgr, map_x, map_y, interpolation=cv2.INTER_LINEAR)

            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            boxes = motion.detect(gray) if snap["motion"] else ()

            processed = snap["mode"] in ("mono", "palette")
            lut = None
            if snap["mode"] == "palette":
                lut_key = (snap["palette_name"], snap["gamma"])
                if cache["lut_key"] != lut_key:
                    cache["lut_key"] = lut_key
                    cache["lut"] = get_lut(*lut_key)
                lut = cache["lut"]

            if processed:
                norm = agc.normalize(gray) if snap["agc"] else gray.astype(np.float32) / 255.0
                if lut is not None:
                    idx = (norm * 255.0).astype(np.uint8)
                    rgb = lut[idx]
                else:
                    g = (norm * 255.0).astype(np.uint8)
                    rgb = np.dstack([g, g, g])
            else:
                rgb = bgr[:, :, ::-1].copy()  # BGR -> RGB

            if snap["fisheye"]:
                rgb = (rgb.astype(np.float32) * cache["vignette_3ch"]).astype(np.uint8)

            rgb = apply_hsb(rgb, snap["hue"], snap["saturation"], snap["brightness"])

            box_color = tuple(int(c) for c in lut[255]) if lut is not None else (255, 255, 255)
            for x, y, bw, bh in boxes:
                cv2.rectangle(rgb, (x, y), (x + bw, y + bh), box_color, 2)

            display.set_scanlines(snap["crt_strength"] if snap["crt_lines"] else 0.0)
            display.show(rgb)
            frames += 1

            now = time.monotonic()
            if server is not None and now - last_preview >= PREVIEW_INTERVAL:
                last_preview = now
                small_w = min(PREVIEW_WIDTH, rgb.shape[1])
                small_h = int(rgb.shape[0] * small_w / rgb.shape[1])
                small_bgr = cv2.resize(rgb, (small_w, small_h), interpolation=cv2.INTER_AREA)[:, :, ::-1]
                ok, jpeg = cv2.imencode(".jpg", small_bgr, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
                if ok:
                    preview.set(jpeg.tobytes())

            if display.quit_requested():
                break

            if args.stats and now - last_report >= 1.0:
                span = now - last_report
                rng = f"{agc.lo:.0f}-{agc.hi:.0f}" if (processed and snap["agc"] and agc.lo is not None) else "n/a"
                print(f"render {frames / span:5.1f} fps   range {rng}   motion boxes {len(boxes)}")
                frames = 0
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        camera.close()
        display.close()
        if server is not None:
            server.shutdown()
        print("\nstopped")


if __name__ == "__main__":
    main()
