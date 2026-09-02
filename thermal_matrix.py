#!/usr/bin/env python3
"""
MLX90640 thermal camera -> 64x64 HUB75 RGB LED matrix on a Raspberry Pi 4.

Architecture:
    - A capture thread owns the I2C bus and blocks on the sensor.
    - A free-running render loop reads the two most recent frames and blends
      between them, so the display updates far faster than the sensor does.

Run with:
    sudo python3 thermal_matrix.py

Useful flags:
    --palette ironbow|whitehot|blackhot|inferno|rainbow
    --bodyheat                fixed absolute-temp palette (see thresholds below)
    --colorwise                like --bodyheat, but the cold end fades to
                                near-black/grey instead of navy, so an empty
                                scene reads as almost invisible on the panel
    --subpage-hz 8|16|32     (image rate is HALF this, see README notes)
    --fit letterbox|fill
    --brightness 50
    --no-blend               (disable temporal interpolation)
    --preview                (also show the image in a window on this display)
    --stats                  (print capture/render rates once a second)
"""

import argparse
import sys
import threading
import time

import numpy as np
import cv2
from PIL import Image

from rgbmatrix import RGBMatrix, RGBMatrixOptions


# ----------------------------------------------------------------------------
# Tunables. These are the knobs worth playing with first.
# ----------------------------------------------------------------------------

SENSOR_W, SENSOR_H = 32, 24
PANEL = 64

I2C_FREQ = 400_000       # raise to 1_000_000 only with 2.2k pull-ups + short wires
GAMMA = 0.70             # <1 lifts the cold end out of the panel's crushed blacks
AGC_LOW_PCT = 2.0        # percentile clipped to black
AGC_HIGH_PCT = 98.0      # percentile clipped to white
AGC_ALPHA = 0.05         # EMA on the range itself; lower = steadier, slower to adapt
MIN_SPAN_C = 4.0         # never stretch a span narrower than this (degrees C)
MEDIAN_FILTER = True     # 3x3 median on the raw array; kills salt-and-pepper noise
SHARPEN_AMOUNT = 0.35    # unsharp mask after upscale; 0 disables
SHARPEN_RADIUS = 1.0
RENDER_FPS_CAP = 60      # set to 0 to run flat out

# Fixed-threshold "body heat" mode: bypasses the percentile auto-range and
# maps absolute temperature straight to color, so the cutoffs stay put
# regardless of what's in the scene. Below COLD_MAX is a blue-white
# monochrome ramp; from HOT_MIN to HOT_MAX is a saturated rainbow. The two
# are only 0.5C apart by default, so the transition reads as a hard edge.
BODYHEAT_LUT_MIN_C = 10.0    # coldest temp the whole ramp represents
BODYHEAT_LUT_MAX_C = 34.0    # hottest temp represented; hotter clamps here
BODYHEAT_COLD_MAX_C = 25.0   # top of the monochrome cold ramp
BODYHEAT_HOT_MIN_C = 25.5    # bottom of the rainbow hot ramp
BODYHEAT_HOT_MAX_C = 27.0    # top of the rainbow; hotter clamps to red


# ----------------------------------------------------------------------------
# Palettes. Anchors are (position 0-1, (R, G, B)).
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
        # Classic hunting-scope look: dark red-black background, the subject
        # rendered in saturated red/orange, hottest points warming toward a
        # pale highlight. Stays in the red family throughout -- no blue,
        # green, or cyan at any point.
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
    return build_cv2_lut(name, gamma)


def build_bodyheat_lut(lut_min=BODYHEAT_LUT_MIN_C, lut_max=BODYHEAT_LUT_MAX_C,
                        cold_max=BODYHEAT_COLD_MAX_C, hot_min=BODYHEAT_HOT_MIN_C,
                        hot_max=BODYHEAT_HOT_MAX_C):
    """
    Fixed-threshold LUT: cold blue-grey below `cold_max`, then a
    near-vertical jump into a red-dominant hot ramp between `hot_min` and
    `hot_max`. Anchors are given in absolute Celsius and converted to the
    0-1 position space build_lut() expects. Temperatures past `hot_max`
    clamp to the last anchor's color (np.interp holds the boundary value),
    and temperatures below `lut_min` clamp to the first anchor.
    """
    def pos(celsius):
        return float(np.clip((celsius - lut_min) / (lut_max - lut_min), 0.0, 1.0))

    anchors_celsius = [
        (lut_min,          (14,  18,  30)),   # coldest: near-black navy
        (cold_max,         (140, 155, 180)),  # top of cold ramp: steel blue-grey
        (hot_min,          (200, 45,  10)),   # hard jump: deep orange-red
        (hot_min + (hot_max - hot_min) * 0.33, (255, 25,  0)),    # red
        (hot_min + (hot_max - hot_min) * 0.66, (255, 90,  10)),   # red-orange
        (hot_max,          (255, 200, 130)),  # hottest: warm highlight, then clamps
    ]
    position_anchors = [(pos(c), rgb) for c, rgb in anchors_celsius]
    return build_lut(position_anchors, gamma=1.0)


def build_colorwise_lut(lut_min=BODYHEAT_LUT_MIN_C, lut_max=BODYHEAT_LUT_MAX_C,
                         cold_max=BODYHEAT_COLD_MAX_C, hot_min=BODYHEAT_HOT_MIN_C,
                         hot_max=BODYHEAT_HOT_MAX_C):
    """
    Same fixed-threshold layout as build_bodyheat_lut(), but the cold ramp
    stays black/near-black grey instead of navy blue -- on a black LED
    panel bezel an empty scene ends up close to indistinguishable from the
    panel being off, and only the hot ramp reads as visible color. The hot
    side is unchanged from body-heat mode so calibration (--cold-max,
    --hot-min, --hot-max) carries over directly.
    """
    def pos(celsius):
        return float(np.clip((celsius - lut_min) / (lut_max - lut_min), 0.0, 1.0))

    anchors_celsius = [
        (lut_min,          (0,   0,   0)),    # coldest: true black, blends into bezel
        (cold_max,         (32,  32,  34)),   # top of cold ramp: faint dark grey
        (hot_min,          (200, 45,  10)),   # hard jump: deep orange-red
        (hot_min + (hot_max - hot_min) * 0.33, (255, 25,  0)),    # red
        (hot_min + (hot_max - hot_min) * 0.66, (255, 90,  10)),   # red-orange
        (hot_max,          (255, 200, 130)),  # hottest: warm highlight, then clamps
    ]
    position_anchors = [(pos(c), rgb) for c, rgb in anchors_celsius]
    return build_lut(position_anchors, gamma=1.0)


# ----------------------------------------------------------------------------
# Sensor backend.
#
# The Adafruit driver is used by default because it installs cleanly with pip.
# It does the calibration math in pure Python, which costs you roughly 3x the
# throughput. Once this script is working, swap in PimoroniSource for real
# speed -- see the note in that class.
# ----------------------------------------------------------------------------

class AdafruitSource:
    name = "adafruit (pure python, slow)"

    def __init__(self, subpage_hz, i2c_freq):
        import board
        import busio
        import adafruit_mlx90640

        rates = {
            1: adafruit_mlx90640.RefreshRate.REFRESH_1_HZ,
            2: adafruit_mlx90640.RefreshRate.REFRESH_2_HZ,
            4: adafruit_mlx90640.RefreshRate.REFRESH_4_HZ,
            8: adafruit_mlx90640.RefreshRate.REFRESH_8_HZ,
            16: adafruit_mlx90640.RefreshRate.REFRESH_16_HZ,
            32: adafruit_mlx90640.RefreshRate.REFRESH_32_HZ,
            64: adafruit_mlx90640.RefreshRate.REFRESH_64_HZ,
        }
        if subpage_hz not in rates:
            raise ValueError(f"unsupported subpage rate: {subpage_hz}")

        # I2C0 on pins 27/28 (GPIO0/GPIO1) — the RB-MatrixCtrl blocks
        # the normal I2C1 on pins 3/5.  SCL=GPIO1=D1, SDA=GPIO0=D0.
        i2c = busio.I2C(board.D1, board.D0, frequency=i2c_freq)
        self._mlx = adafruit_mlx90640.MLX90640(i2c)
        self._mlx.refresh_rate = rates[subpage_hz]
        self._scratch = [0.0] * (SENSOR_W * SENSOR_H)

    def read_into(self, out):
        """Fill `out` (768 float32) with one subpage. Raises on a bad read."""
        self._mlx.getFrame(self._scratch)
        out[:] = self._scratch


class PimoroniSource:
    """
    Backend for pimoroni/mlx90640-library -- the C++ port with Python bindings.
    Roughly 3-4x the throughput of the Adafruit driver because the frame
    assembly and calibration run compiled.

    The module and method names below follow that project's Python example, but
    check its README before enabling -- the binding's surface has changed
    between releases. This class is here as the scaffold, not as tested code.
    """
    name = "pimoroni (compiled)"

    def __init__(self, subpage_hz, i2c_freq):
        import MLX90640 as mlx  # noqa: N813
        self._mlx = mlx
        self._mlx.setup(subpage_hz)

    def read_into(self, out):
        out[:] = self._mlx.get_frame()


BACKENDS = {"adafruit": AdafruitSource, "pimoroni": PimoroniSource}


# ----------------------------------------------------------------------------
# Capture thread.
# ----------------------------------------------------------------------------

class Capture(threading.Thread):
    """Owns the sensor. Keeps the two most recent frames plus their timestamps."""

    def __init__(self, source, subpage_hz):
        super().__init__(daemon=True)
        self.source = source
        self.period = 1.0 / subpage_hz
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        zero = np.zeros((SENSOR_H, SENSOR_W), dtype=np.float32)
        self.prev = zero.copy()
        self.curr = zero.copy()
        self.t_curr = time.monotonic()

        self.reads = 0
        self.errors = 0
        self.ready = threading.Event()

    def run(self):
        raw = np.zeros(SENSOR_W * SENSOR_H, dtype=np.float32)
        while not self.stop_event.is_set():
            try:
                self.source.read_into(raw)
            except (ValueError, RuntimeError, OSError) as e:
                # Checksum failures are routine, especially at high bus speeds.
                self.errors += 1
                if self.errors <= 5:
                    print(f"capture error #{self.errors}: {type(e).__name__}: {e}")
                continue

            frame = raw.reshape(SENSOR_H, SENSOR_W)
            if MEDIAN_FILTER:
                # ksize=3 is the only median OpenCV supports on float32.
                frame = cv2.medianBlur(frame, 3)

            with self.lock:
                self.prev = self.curr
                self.curr = frame.copy()
                self.t_curr = time.monotonic()
                self.reads += 1
            self.ready.set()

    def snapshot(self):
        with self.lock:
            return self.prev, self.curr, self.t_curr


# ----------------------------------------------------------------------------
# Image pipeline.
# ----------------------------------------------------------------------------

class Pipeline:
    def __init__(self, lut, fit, blend, period, mode="agc",
                 fixed_lo=BODYHEAT_LUT_MIN_C, fixed_hi=BODYHEAT_LUT_MAX_C, rotate=0):
        self.lut = lut
        self.fit = fit
        self.blend = blend
        self.period = period
        self.rotate_k = (rotate // 90) % 4
        self.mode = mode              # "agc" (relative, default) or "fixed"
        self.fixed_lo = fixed_lo
        self.fixed_hi = fixed_hi
        self.lo = None
        self.hi = None
        self.scene_min = None
        self.scene_mean = None
        self.scene_max = None

    def autorange(self, frame):
        """Percentile AGC with an EMA on the range, so it doesn't pulse."""
        lo, hi = np.percentile(frame, [AGC_LOW_PCT, AGC_HIGH_PCT])
        if hi - lo < MIN_SPAN_C:
            mid = 0.5 * (hi + lo)
            lo, hi = mid - MIN_SPAN_C / 2.0, mid + MIN_SPAN_C / 2.0

        if self.lo is None:
            self.lo, self.hi = lo, hi
        else:
            self.lo += AGC_ALPHA * (lo - self.lo)
            self.hi += AGC_ALPHA * (hi - self.hi)
        return self.lo, self.hi

    def resize(self, norm):
        """32x24 normalised float -> 64x64, preserving or filling the frame."""
        if self.fit == "fill":
            # Centre-crop to square, then scale up. Fills the panel but throws
            # away a third of the horizontal field of view.
            x0 = (SENSOR_W - SENSOR_H) // 2
            square = norm[:, x0:x0 + SENSOR_H]
            return cv2.resize(square, (PANEL, PANEL), interpolation=cv2.INTER_CUBIC)

        # Letterbox: full field of view, 8 blank rows top and bottom.
        scaled = cv2.resize(norm, (PANEL, 48), interpolation=cv2.INTER_CUBIC)
        out = np.zeros((PANEL, PANEL), dtype=np.float32)
        out[8:56, :] = scaled
        return out

    def rotate(self, square):
        """Rotate the (already square) 64x64 frame; k=1 is 90 deg CCW."""
        return np.rot90(square, self.rotate_k) if self.rotate_k else square

    def render(self, prev, curr, t_curr, now):
        if self.blend:
            # Interpolate between the last two frames. Costs one frame period
            # of latency -- the tradeoff for smooth motion.
            w = float(np.clip((now - t_curr) / self.period, 0.0, 1.0))
            frame = prev + (curr - prev) * w
        else:
            frame = curr

        # What the sensor is actually seeing, regardless of color-mapping
        # mode -- this is the number to check when the picture looks wrong.
        self.scene_min = float(np.min(frame))
        self.scene_mean = float(np.mean(frame))
        self.scene_max = float(np.max(frame))

        if self.mode == "fixed":
            # Absolute thresholds -- deliberately NOT scene-relative, so a
            # cold room and a warm one map to the same colors.
            self.lo, self.hi = self.fixed_lo, self.fixed_hi
            norm = np.clip((frame - self.lo) / (self.hi - self.lo), 0.0, 1.0)
        else:
            lo, hi = self.autorange(frame)
            norm = np.clip((frame - lo) / max(hi - lo, 1e-6), 0.0, 1.0)

        up = self.resize(norm)
        up = self.rotate(up)

        if SHARPEN_AMOUNT > 0:
            blur = cv2.GaussianBlur(up, (0, 0), SHARPEN_RADIUS)
            up = cv2.addWeighted(up, 1.0 + SHARPEN_AMOUNT, blur, -SHARPEN_AMOUNT, 0)
            up = np.clip(up, 0.0, 1.0)

        idx = (up * 255.0).astype(np.uint8)
        return self.lut[idx]


# ----------------------------------------------------------------------------
# Main.
# ----------------------------------------------------------------------------

def build_matrix(args):
    opts = RGBMatrixOptions()
    opts.rows = PANEL
    opts.cols = PANEL
    opts.chain_length = 1
    opts.parallel = 1
    opts.hardware_mapping = args.gpio_mapping
    opts.gpio_slowdown = args.gpio_slowdown
    opts.pwm_bits = args.pwm_bits
    opts.brightness = args.brightness
    opts.pwm_lsb_nanoseconds = 130
    opts.disable_hardware_pulsing = args.no_hardware_pulse

    # Critical: the library drops to an unprivileged user after init by
    # default, which revokes access to /dev/i2c-1 and kills the capture
    # thread with a permission error a few seconds in.
    opts.drop_privileges = False

    return RGBMatrix(options=opts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--palette", default="ironbow",
                   help="ironbow, whitehot, blackhot, rainbow, or any OpenCV "
                        "colormap name such as inferno / magma / turbo")
    p.add_argument("--backend", default="adafruit", choices=BACKENDS.keys())
    p.add_argument("--subpage-hz", type=int, default=16,
                   help="sensor subpage rate; complete images arrive at half this")
    p.add_argument("--i2c-freq", type=int, default=I2C_FREQ)
    p.add_argument("--fit", default="letterbox", choices=["letterbox", "fill"])
    p.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                   help="rotate the panel image, e.g. 180 if it's upside down")
    p.add_argument("--gamma", type=float, default=GAMMA)
    p.add_argument("--brightness", type=int, default=50)
    p.add_argument("--pwm-bits", type=int, default=8)
    p.add_argument("--gpio-slowdown", type=int, default=4)
    p.add_argument("--gpio-mapping", default="regular",
                   help="'regular' for direct wiring, 'adafruit-hat' for a bonnet")
    p.add_argument("--no-blend", action="store_true")
    p.add_argument("--no-hardware-pulse", action="store_true",
                   help="workaround if snd_bcm2835 audio module is still "
                        "loaded; causes more flicker, fix the module instead")
    heat_mode = p.add_mutually_exclusive_group()
    heat_mode.add_argument("--bodyheat", action="store_true",
                   help="fixed absolute-temperature palette instead of "
                        "scene-relative auto-ranging: blue-grey below "
                        "--cold-max, red-dominant between --hot-min and --hot-max")
    heat_mode.add_argument("--colorwise", action="store_true",
                   help="like --bodyheat (same fixed absolute-temperature "
                        "thresholds), but the cold end fades to black/dark "
                        "grey instead of navy blue, so an empty scene reads "
                        "as nearly invisible against the panel's black bezel")
    p.add_argument("--cold-max", type=float, default=BODYHEAT_COLD_MAX_C)
    p.add_argument("--hot-min", type=float, default=BODYHEAT_HOT_MIN_C)
    p.add_argument("--hot-max", type=float, default=BODYHEAT_HOT_MAX_C)
    p.add_argument("--lut-min", type=float, default=BODYHEAT_LUT_MIN_C)
    p.add_argument("--lut-max", type=float, default=BODYHEAT_LUT_MAX_C)
    p.add_argument("--preview", action="store_true",
                   help="also show the image in a window on this machine's "
                        "display -- requires opencv-python (not headless) "
                        "and a reachable X display")
    p.add_argument("--preview-scale", type=int, default=8,
                   help="preview window is PANEL*scale pixels square")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    fixed_thresholds = args.bodyheat or args.colorwise
    if args.colorwise:
        lut = build_colorwise_lut(args.lut_min, args.lut_max,
                                   args.cold_max, args.hot_min, args.hot_max)
    elif args.bodyheat:
        lut = build_bodyheat_lut(args.lut_min, args.lut_max,
                                  args.cold_max, args.hot_min, args.hot_max)
    else:
        try:
            lut = get_lut(args.palette, args.gamma)
        except AttributeError:
            sys.exit(f"unknown palette: {args.palette}")

    print(f"starting sensor backend: {args.backend}")
    source = BACKENDS[args.backend](args.subpage_hz, args.i2c_freq)
    print(f"  {source.name}, {args.subpage_hz} Hz subpage "
          f"({args.subpage_hz / 2:g} complete images/sec)")

    capture = Capture(source, args.subpage_hz)
    capture.start()

    print("waiting for first frame...")
    if not capture.ready.wait(timeout=10.0):
        sys.exit("no frames from the sensor -- check wiring and i2cdetect -y 1")

    matrix = build_matrix(args)
    canvas = matrix.CreateFrameCanvas()
    pipeline = Pipeline(lut, args.fit, not args.no_blend, 1.0 / args.subpage_hz,
                         mode="fixed" if fixed_thresholds else "agc",
                         fixed_lo=args.lut_min, fixed_hi=args.lut_max,
                         rotate=args.rotate)

    frame_budget = 1.0 / RENDER_FPS_CAP if RENDER_FPS_CAP else 0.0
    rendered = 0
    last_report = time.monotonic()
    last_reads = 0

    print("running -- ctrl-c to stop")
    if args.preview:
        preview_window = "thermal preview (q or Esc to close)"
        cv2.namedWindow(preview_window, cv2.WINDOW_NORMAL)

    try:
        running = True
        while running:
            t0 = time.monotonic()

            prev, curr, t_curr = capture.snapshot()
            rgb = pipeline.render(prev, curr, t_curr, t0)

            canvas.SetImage(Image.fromarray(rgb, "RGB"))
            canvas = matrix.SwapOnVSync(canvas)
            rendered += 1

            if args.preview:
                size = PANEL * args.preview_scale
                big = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_NEAREST)
                bgr = cv2.cvtColor(big, cv2.COLOR_RGB2BGR)
                cv2.imshow(preview_window, bgr)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    running = False

            if args.stats and t0 - last_report >= 1.0:
                span = t0 - last_report
                reads = capture.reads
                print(f"sensor {(reads - last_reads) / span:5.1f} subpage/s   "
                      f"render {rendered / span:5.1f} fps   "
                      f"errors {capture.errors}   "
                      f"mapped {pipeline.lo:.1f}-{pipeline.hi:.1f} C   "
                      f"scene {pipeline.scene_min:.1f}/{pipeline.scene_mean:.1f}"
                      f"/{pipeline.scene_max:.1f} C (min/mean/max)")
                rendered = 0
                last_reads = reads
                last_report = t0

            if frame_budget:
                slack = frame_budget - (time.monotonic() - t0)
                if slack > 0:
                    time.sleep(slack)

    except KeyboardInterrupt:
        pass
    finally:
        capture.stop_event.set()
        matrix.Clear()
        if args.preview:
            cv2.destroyAllWindows()
        print("\nstopped")


if __name__ == "__main__":
    main()
