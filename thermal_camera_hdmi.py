#!/usr/bin/env python3
"""
Pi Camera Module 3 (CSI ribbon slot) -> false-color "thermal-style" display
over HDMI, on a Raspberry Pi 4.

This is a stand-in for a real thermal sensor: it takes the CSI camera's
live grayscale luminance and runs it through the same kind of auto-contrast
+ false-color palette pipeline used for the LED-matrix thermal project, so
the display/controls can be built and tuned before a radiometric sensor is
wired in. Swap out the Camera class for a real thermal source later --
everything downstream (AGC, palette, display) stays the same.

Needs a desktop session running on the Pi (X11 or Wayland) -- this opens a
plain fullscreen window on whatever's plugged into HDMI. See
scripts/install-camera-hdmi.sh for one-shot dependency setup and an
optional autostart-on-login entry.

Run with:
    python3 thermal_camera_hdmi.py

Useful flags:
    --palette ironbow|whitehot|blackhot|rainbow|redhot|<opencv colormap name>
    --mono                  skip the palette, show raw grayscale (camera sanity check)
    --resolution 1280x720   camera capture size
    --rotate 0|90|180|270
    --hflip / --vflip
    --gamma 0.7
    --no-agc                disable auto-contrast, map the fixed 0-255 range
    --stats                 print capture/render fps once a second
    q or Esc in the window quits
"""

import argparse
import time

import numpy as np
import cv2


# ----------------------------------------------------------------------------
# Tunables.
# ----------------------------------------------------------------------------

GAMMA = 0.70
AGC_LOW_PCT = 2.0
AGC_HIGH_PCT = 98.0
AGC_ALPHA = 0.1          # EMA on the range; lower = steadier, slower to adapt
MIN_SPAN = 20.0          # never stretch a span narrower than this (0-255 units)


# ----------------------------------------------------------------------------
# Palettes -- same anchor format as the LED-matrix project. Anchors are
# (position 0-1, (R, G, B)).
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

    def read_gray(self):
        # picamera2's "RGB888" format is actually laid out BGR (a
        # long-standing quirk kept for OpenCV compatibility), so this is
        # the right conversion despite the name.
        frame = self.picam2.capture_array()
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

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
# Main.
# ----------------------------------------------------------------------------

def parse_resolution(s):
    w, h = s.lower().split("x")
    return (int(w), int(h))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--palette", default="ironbow",
                   help="ironbow, whitehot, blackhot, rainbow, redhot, or any "
                        "OpenCV colormap name such as inferno / magma / turbo")
    p.add_argument("--mono", action="store_true",
                   help="skip the palette, show raw auto-contrasted grayscale")
    p.add_argument("--resolution", type=parse_resolution, default=(1280, 720))
    p.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    p.add_argument("--hflip", action="store_true")
    p.add_argument("--vflip", action="store_true")
    p.add_argument("--gamma", type=float, default=GAMMA)
    p.add_argument("--no-agc", action="store_true",
                   help="disable auto-contrast, map the fixed 0-255 range as-is")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    rotate_k = (args.rotate // 90) % 4
    lut = None if args.mono else get_lut(args.palette, args.gamma)
    agc = None if args.no_agc else Agc()

    print(f"starting camera at {args.resolution[0]}x{args.resolution[1]}...")
    camera = Camera(args.resolution, args.hflip, args.vflip)

    window = "thermal camera (q or Esc to quit)"
    cv2.namedWindow(window, cv2.WND_PROP_FULLSCREEN)
    cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    frames = 0
    last_report = time.monotonic()

    print("running -- q or Esc in the window to stop")
    try:
        while True:
            gray = camera.read_gray()
            if rotate_k:
                gray = np.rot90(gray, rotate_k)

            if args.mono:
                disp = gray
            else:
                norm = agc.normalize(gray) if agc else gray.astype(np.float32) / 255.0
                idx = (norm * 255.0).astype(np.uint8)
                disp = cv2.cvtColor(lut[idx], cv2.COLOR_RGB2BGR)

            cv2.imshow(window, disp)
            frames += 1

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break

            now = time.monotonic()
            if args.stats and now - last_report >= 1.0:
                span = now - last_report
                rng = f"{agc.lo:.0f}-{agc.hi:.0f}" if agc else "0-255"
                print(f"render {frames / span:5.1f} fps   range {rng}")
                frames = 0
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        camera.close()
        cv2.destroyAllWindows()
        print("\nstopped")


if __name__ == "__main__":
    main()
