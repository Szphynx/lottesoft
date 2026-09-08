#!/usr/bin/env python3
"""
Pi Camera Module 3 (CSI ribbon slot) -> live display over HDMI, on a
Raspberry Pi 4. A minimal security-cam viewer: full color by default, with
optional grayscale/false-color modes for low-light or night-vision-style
viewing.

Runs as a normal fullscreen app inside the desktop session (X11 or
Wayland) -- same technique as a fullscreen video-loop player: launched via
a desktop autostart entry so it comes up after login, but the desktop
itself stays usable (switch away with alt-tab, ssh in, etc). See
scripts/install-security-cam-hdmi.sh, which installs the autostart entry.

Run by hand with:
    python3 security_cam_hdmi.py

Runs with motion-blob detection and a fake fisheye lens effect ON by
default -- this is tuned to be cheap enough for a Raspberry Pi 3 with no
extra hardware (no Coral/TPU), not just the Pi 4. There's no real person
detector here: a moving-blob box is a cheap stand-in, not a person
classifier, so it'll also box pets, shadows, and waving branches.

Useful flags:
    --resolution 640x480    camera capture size (lower = faster on a Pi 3)
    --rotate 0|90|180|270
    --hflip / --vflip
    --mono                  grayscale instead of color (auto-contrast applied)
    --palette ironbow|whitehot|blackhot|rainbow|redhot|bodyheat|colorwise|infrared|<opencv colormap name>
                            false-color night-vision-style look instead of color
    --gamma 0.7             only affects --mono / --palette
    --no-agc                disable auto-contrast in --mono / --palette modes
    --no-fisheye            disable the fisheye warp (on by default) -- toggle
                            this to check how much it costs on your hardware
    --fisheye-strength 0.4  0 = none, higher = more barrel distortion
    --no-motion             disable motion-blob detection (on by default)
    --stats                 print render fps once a second
    q or Esc in the window quits
"""

import argparse
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

FISHEYE_STRENGTH = 0.4

# Motion/blob detection runs on a downscaled copy to stay cheap on a Pi 3;
# boxes are scaled back up to full frame size afterward.
MOTION_SCALE = 0.35
MOTION_MIN_AREA_FRAC = 0.02   # fraction of the *downscaled* frame area
MOTION_HISTORY = 300
MOTION_VAR_THRESHOLD = 24


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
    try:
        return build_cv2_lut(name, gamma)
    except AttributeError:
        raise SystemExit(f"unknown palette: {name}")


# ----------------------------------------------------------------------------
# Camera (CSI ribbon slot, via picamera2/libcamera).
# ----------------------------------------------------------------------------

class Camera:
    def __init__(self, size, hflip, vflip):
        from picamera2 import Picamera2
        from libcamera import Transform

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": size, "format": "RGB888"},
            transform=Transform(hflip=hflip, vflip=vflip),
        )
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
# Fake fisheye lens effect -- purely cosmetic barrel distortion, applied
# via a remap table built once per frame size (not per frame).
# ----------------------------------------------------------------------------

def build_fisheye_maps(size, strength):
    w, h = size
    cx, cy = w / 2.0, h / 2.0
    radius = min(cx, cy)

    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = (xs - cx) / radius
    dy = (ys - cy) / radius
    r = np.sqrt(dx * dx + dy * dy)
    factor = 1.0 + strength * (r ** 2)

    map_x = (dx * factor) * radius + cx
    map_y = (dy * factor) * radius + cy
    return map_x.astype(np.float32), map_y.astype(np.float32)


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

    def show(self, rgb):
        h, w = rgb.shape[:2]
        surf = self.pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")
        if (w, h) != self.size:
            surf = self.pygame.transform.scale(surf, self.size)
        self.screen.blit(surf, (0, 0))
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
# Main.
# ----------------------------------------------------------------------------

def parse_resolution(s):
    w, h = s.lower().split("x")
    return (int(w), int(h))


def main():
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--mono", action="store_true",
                       help="grayscale instead of color, with auto-contrast")
    mode.add_argument("--palette", default=None,
                       help="false-color night-vision-style look instead of color: "
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
                   help="disable the fisheye warp (on by default)")
    p.add_argument("--fisheye-strength", type=float, default=FISHEYE_STRENGTH)
    p.add_argument("--no-motion", action="store_true",
                   help="disable motion-blob detection (on by default)")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    rotate_k = (args.rotate // 90) % 4
    processed = args.mono or args.palette
    lut = get_lut(args.palette, args.gamma) if args.palette else None
    agc = Agc() if (processed and not args.no_agc) else None
    fisheye = not args.no_fisheye
    motion_on = not args.no_motion

    print(f"starting camera at {args.resolution[0]}x{args.resolution[1]}...")
    camera = Camera(args.resolution, args.hflip, args.vflip)

    print("opening display...")
    display = Display()
    print(f"  {display.size[0]}x{display.size[1]}")

    # Frame size after rotation -- rot90 swaps width/height on a 90/270
    # turn, and both the fisheye map and the motion detector are built
    # once for that fixed size rather than recomputed every frame.
    w, h = args.resolution
    frame_size = (h, w) if rotate_k in (1, 3) else (w, h)
    fisheye_maps = build_fisheye_maps(frame_size, args.fisheye_strength) if fisheye else None
    motion = MotionDetector(frame_size) if motion_on else None
    box_color = (255, 60, 60)

    frames = 0
    last_report = time.monotonic()

    print("running -- ctrl-c (or q/Esc with a keyboard attached) to stop")
    try:
        while True:
            bgr = camera.read_bgr()
            if rotate_k:
                bgr = np.rot90(bgr, rotate_k, axes=(0, 1))
            if fisheye:
                map_x, map_y = fisheye_maps
                bgr = cv2.remap(bgr, map_x, map_y, interpolation=cv2.INTER_LINEAR)

            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            boxes = motion.detect(gray) if motion_on else ()

            if processed:
                norm = agc.normalize(gray) if agc else gray.astype(np.float32) / 255.0
                if args.palette:
                    idx = (norm * 255.0).astype(np.uint8)
                    rgb = lut[idx]
                else:
                    g = (norm * 255.0).astype(np.uint8)
                    rgb = np.dstack([g, g, g])
            else:
                rgb = bgr[:, :, ::-1].copy()  # BGR -> RGB

            for x, y, bw, bh in boxes:
                cv2.rectangle(rgb, (x, y), (x + bw, y + bh), box_color, 2)

            display.show(rgb)
            frames += 1

            if display.quit_requested():
                break

            now = time.monotonic()
            if args.stats and now - last_report >= 1.0:
                span = now - last_report
                rng = f"{agc.lo:.0f}-{agc.hi:.0f}" if agc else "n/a"
                print(f"render {frames / span:5.1f} fps   range {rng}   motion boxes {len(boxes)}")
                frames = 0
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        camera.close()
        display.close()
        print("\nstopped")


if __name__ == "__main__":
    main()
