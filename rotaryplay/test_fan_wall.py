"""Self-check for fan_wall.py against teensy.ino and its spec: python test_fan_wall.py"""
import os
import socket
import sys
import tempfile

import numpy as np
from PIL import Image

import fan_wall as fw

HERE = os.path.dirname(os.path.abspath(__file__))
geo = fw.load_sketch(os.path.join(HERE, "teensy.ino"))


def fresh():
    return fw.State(geo)


# Sketch facts come through.
assert (geo["teensy_ip"], geo["teensy_port"], geo["sync"]) == ("192.168.60.50", 5005, 0xAA)
assert (geo["w"], geo["h"], geo["pins"], geo["fans"], geo["fan_px"]) == (84, 56, 16, 6, 7)
assert (geo["cluster_w"], geo["cluster_h"], geo["grid_w"], geo["grid_h"]) == (3, 2, 4, 4)

# The Teensy's XY() as the spec describes it: 1344 live LEDs, each LED once,
# dead corners exactly where the spec draws them, corner probe = LED 0.
led = geo["led_at"]
live = led[led >= 0]
assert len(live) == 1344 and len(set(live.tolist())) == 1344
dead = np.zeros((56, 84), bool)
dead[0:7, 0:21] = dead[0:7, 63:84] = dead[49:56, 0:21] = dead[49:56, 63:84] = True
assert not (led[dead] >= 0).any()
assert led[11, 3] == 0
# Corner hubs carry their 3 fans on ports 1-3 (LEDs 0-47), nothing above.
for panel in (0, 3, 12, 15):
    got = sorted(v - panel * 96 for v in live.tolist() if v // 96 == panel)
    assert got == list(range(48)), panel

# Calibration starts at the Teensy's mapping: identity remap.
cells, real = fw.build_remap(geo, fresh().snapshot())
assert (cells == real).all() and len(cells) == 1344

# Rotation/mirror keep every LED on the ring; 4 turns are identity.
ring = set(geo["ring"])
for rot in (0, 90, 180, 270):
    for m in (False, True):
        assert {fw.ring_cell(x, y, 7, rot, m) for x, y in geo["ring"]} == ring
x, y = 1, 2
for _ in range(4):
    x, y = fw.ring_cell(x, y, 7, 90, False)
assert (x, y) == (1, 2)

# One fan pattern for every cluster: swapping slots 0 and 1 moves the fan
# the Teensy puts at slot 0 one fan to the right in every full cluster.
s = fresh()
s.apply_wire({"fan_order": [1, 0, 2, 3, 4, 5]})
_, r2 = fw.build_remap(geo, s.snapshot())
moved = (cells[:, 1] % 14 < 7) & (cells[:, 0] % 21 < 7)
assert (r2[moved, 0] == cells[moved, 0] + 7).all() and (r2[moved, 1] == cells[moved, 1]).all()

# Bad edits are ignored.
s = fresh()
s.apply_wire({"fan_order": [0, 0, 1, 2, 3, 4], "cluster_order": [1, 2], "calibrate": "nope"})
assert s.fan_order == list(range(6)) and s.cluster_order == list(range(16)) and s.calibrate == "off"

# Wire calibration: each cluster its colour, white count = fan / cluster number.
snap = fresh().snapshot()
for mode in ("fans", "clusters"):
    img = fw.wire_pattern(geo, snap, cells, mode)
    for (x, y) in cells:
        v = led[y, x]
        pin, fan, i = v // 96, v % 96 // 16, v % 16
        n = fan + 1 if mode == "fans" else pin + 1
        want = [255] * 3 if i < n else [c // 2 for c in fw.CLUSTER_COLORS[pin]]
        assert img[y, x].tolist() == want, (mode, x, y)
assert fw.wire_pattern(geo, snap, cells, "probe").sum() == 3 * 255

# Colour correction: off = untouched, on = per-channel levels.
frame = np.full((2, 2, 3), 100, np.uint8)
snap = dict(cc_on=False, cc_r=150, cc_g=50, cc_b=100)
assert fw.color_correct(frame, snap) is frame
snap["cc_on"] = True
assert fw.color_correct(frame, snap)[0, 0].tolist() == [150, 50, 100]

# Settings survive a save/load round trip.
path = os.path.join(HERE, "_test_state.json")
s = fw.State(geo, path)
s.apply_wire({"cc_on": True, "cc_g": 70, "softness": 150, "fan_rot": [90, 0, 0, 0, 0, 0]})
assert s.save()
s2 = fw.State(geo, path)
os.remove(path)
assert (s2.cc_on, s2.cc_g, s2.softness, s2.fan_rot[0]) == (True, 70, 150, 90)

# Blur-before-downsample keeps the output size.
big = np.random.randint(0, 255, (720, 1280, 3), np.uint8)
assert fw.fit_frame(big, "fill", 84, 42, 1.0).shape == (42, 84, 3)
assert fw.fit_frame(big, "letterbox", 84, 56, 0).shape == (56, 84, 3)

# Frames go out as 56 rows of 255 bytes, one sequence number, reassembling
# to the image -- the format teensy.ino accepts.
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.bind(("127.0.0.1", 0))
rx.settimeout(1)
img = np.random.randint(0, 255, (56, 84, 3), np.uint8)
fw.TeensyLink("127.0.0.1", rx.getsockname()[1], 0xAA).send_frame(img)
got, seqs = np.zeros_like(img), set()
for _ in range(56):
    pkt = rx.recv(2048)
    assert len(pkt) == 255 and pkt[0] == 0xAA
    seqs.add(pkt[1])
    got[pkt[2]] = np.frombuffer(pkt[3:], np.uint8).reshape(84, 3)
assert len(seqs) == 1 and (got == img).all()


# Video formats: every container this OpenCV build can write here decodes,
# plays at its own fps, and passes the upload probe.
import cv2

tmp = tempfile.mkdtemp()


def write_clip(name, fourcc, fps, n=20):
    path = os.path.join(tmp, name)
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, (160, 90))
    if not vw.isOpened():
        return None
    for i in range(n):
        vw.write(np.full((90, 160, 3), i * 10, np.uint8))  # frame index as brightness
    vw.release()
    return path if os.path.getsize(path) > 0 else None


tested = []
for name, fourcc in (("a.mp4", "mp4v"), ("a.mov", "mp4v"), ("a.m4v", "mp4v"), ("a.mkv", "mp4v"),
                     ("a.avi", "MJPG"), ("b.avi", "XVID"), ("a.webm", "VP80"), ("a.ogv", "THEO")):
    path = write_clip(name, fourcc, 25)
    if path:
        assert fw.probe_media(path, "video") is None, name
        tested.append(name)
gif = os.path.join(tmp, "a.gif")
Image.new("RGB", (40, 30), (200, 0, 0)).save(gif, save_all=True, duration=40,
                                              append_images=[Image.new("RGB", (40, 30), (0, 200, 0))])
assert fw.probe_media(gif, "video") is None
tested.append("a.gif")
for ext in (".png", ".jpg", ".bmp", ".webp", ".tiff"):
    path = os.path.join(tmp, "img" + ext)
    Image.new("RGB", (40, 30), (0, 0, 200)).save(path)
    assert fw.probe_media(path, "image") is None, ext
    tested.append("img" + ext)
assert all(ext in fw.KIND_BY_EXT for ext in (".mp4", ".mov", ".mkv", ".webm", ".ts", ".mpg", ".tiff"))
print("formats decoded:", " ".join(tested))

# A 25 fps clip plays at 25 fps on the 30 fps loop, not 30.
clip = fw.ClipSource({"kind": "video", "path": os.path.join(tmp, "a.avi")})
for _ in range(15):  # 0.5 s
    clip.get_frame("fill", 84, 56, 0, 1 / 30)
assert 11 <= clip.shown <= 13, clip.shown  # ~12.5 frames in 0.5 s at 25 fps
clip.close()

# Broken files: refused at upload, skipped (not stalled on) in the queue.
bad = os.path.join(tmp, "bad.mp4")
with open(bad, "wb") as f:
    f.write(b"not a video" * 100)
assert fw.probe_media(bad, "video")
queue = [{"id": "bad", "kind": "video", "path": bad, "name": "bad"},
         {"id": "ok", "kind": "video", "path": os.path.join(tmp, "a.avi"), "name": "ok"}]
qp = fw.QueuePlayer(lambda: [dict(q) for q in queue], "fill")
for _ in range(3):
    qp.next_frame(1 / 30, 84, 56)
assert qp.current_id == "ok"

# Startup/close rev: dark at the start of startup and the end of close, lit mid-way.
def rev(t, closing):
    return fw.rev_pattern(geo, cells, t, 2.0, closing, 480, (255, 255, 255))
assert rev(0, False).max() == 0 and rev(1.0, False).max() > 200
assert rev(2.0, True).max() == 0 and rev(0.2, True).max() > 150

# Every page setting is a flag; flags win over the saved settings file.
path = os.path.join(tmp, "state.json")
s = fw.State(geo, path)
s.apply_wire({"brightness": 50, "cc_g": 70})
assert s.save()
sys.argv = ["fan_wall.py", "--brightness", "120", "--cc-on", "--calibrate", "fans",
            "--fan-rot", "[90,0,0,0,0,0]", "--seq-start-on", "--stats-on", "--color", "#ff0000"]
args, _ = fw.parse_args()
s = fw.State(geo, path)
s.apply_wire(args.settings)
assert (s.brightness, s.cc_g, s.cc_on, s.calibrate, s.fan_rot[0], s.seq_start_on, s.stats_on,
        s.color) == (120, 70, True, "fans", 90, True, True, (255, 0, 0))

# Stats: numbers after a second, nothing collected before.
st = fw.Stats(None)
link = type("L", (), {"errors": 0})()
t0 = st.last
for k in range(31):
    st.frame(t0 + k / 30, 0.004, link)
assert st.data["pi_fps"] > 25 and st.data["work_ms_avg"] == 4.0

print("ok")
