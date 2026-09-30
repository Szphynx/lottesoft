"""Self-check for fan_wall.py's mapping: python test_fan_wall.py"""
import os
import socket
import types

import numpy as np

import fan_wall as fw

HERE = os.path.dirname(os.path.abspath(__file__))
geo = fw.load_sketch(os.path.join(HERE, "teensy_globalxy_udp_control.ino"))
args = types.SimpleNamespace(text="", text_color="#ffffff", bold=False, italic=False,
                             scroll_speed=20, text_direction="left", brightness=255, media=None)


def fresh():
    return fw.State(args, geo)


def sketch_xy(src, x, y):
    """The sketch's own XY(), transcribed: global cell -> LED index or None."""
    import re
    table = [int(v) for v in re.findall(r"\d+", re.search(r"XYTable\[\]\s*=\s*\{(.*?)\}", src, re.S).group(1))]
    j = table[(y % 21) * 14 + x % 14]
    return None if j == 65535 else ((y // 21) * 4 + x // 14) * 96 + j


# Sketch facts come through.
assert (geo["teensy_ip"], geo["teensy_port"]) == ("192.168.60.50", 5005)
assert (geo["pins"], geo["fans"], geo["fan_px"], geo["leds_per_pin"]) == (16, 6, 7, 96)
assert (geo["cluster_w"], geo["cluster_h"], geo["fan_order"]) == (2, 3, [0, 2, 4, 1, 3, 5])

# Default mapping == the sketch's XY(), LED for LED.
src = open(os.path.join(HERE, "teensy_globalxy_udp_control.ino"), encoding="utf-8").read()
pos = fw.build_led_map(geo, fresh().snapshot())
assert (pos[:, 0] >= 0).all()
for idx, (x, y) in enumerate(pos):
    assert sketch_xy(src, x, y) == idx, (idx, x, y)
assert len({tuple(p) for p in pos}) == len(pos)  # no two LEDs share a cell

# Rotation/mirror keep every LED on the ring; 4 turns / 2 mirrors are identity.
ring = set(geo["ring"])
for rot in (0, 90, 180, 270):
    for m in (False, True):
        assert {fw.ring_cell(x, y, 7, rot, m) for x, y in geo["ring"]} == ring
x, y = 1, 2
for _ in range(4):
    x, y = fw.ring_cell(x, y, 7, 90, False)
assert (x, y) == (1, 2)

# One fan pattern for all clusters: putting chain fan 1 in slot 3 (column 1,
# row 1) moves it there in every cluster.
s = fresh()
s.apply_wire({"fan_order": [3, 0, 1, 4, 2, 5]})
p2 = fw.build_led_map(geo, s.snapshot())
for c in range(16):
    assert ((p2[c * 96] // 7) % [2, 3]).tolist() == [1, 1]

# Bad edits are ignored, good grid reshape resets cluster order.
s = fresh()
s.apply_wire({"fan_order": [0, 0, 1, 2, 3, 4], "cluster_order": [1, 2]})
assert s.fan_order == geo["fan_order"] and s.cluster_order == list(range(16))
s.apply_wire({"grid_w": 4, "grid_h": 4, "cluster_w": 4, "cluster_h": 2})  # 8 != 6 fans
assert (s.cluster_w, s.cluster_h) == (2, 3)
s.apply_wire({"grid_w": 4, "grid_h": 3, "cluster_w": 3, "cluster_h": 2})
assert (s.grid_w, s.grid_h, s.cluster_w, s.cluster_h, s.active) == (4, 3, 3, 2, 12)
assert fw.canvas_size(geo, s.snapshot()) == (84, 42)
p3 = fw.build_led_map(geo, s.snapshot())
assert (p3[12 * 96:] == -1).all() and (p3[:12 * 96] >= 0).all()

# Calibration: each cluster its own colour, white count = fan / cluster number.
snap = fresh().snapshot()
for mode, number in (("fan", lambda c, f: f + 1), ("cluster", lambda c, f: c + 1)):
    buf = fw.chain_test_buffer(geo, snap, mode).reshape(16, 6, 16, 3)
    for c in range(16):
        for f in range(6):
            white = (buf[c, f] == 255).all(axis=1)
            assert white.sum() == number(c, f) and white[:number(c, f)].all()
            assert (buf[c, f, number(c, f):] == [v // 2 for v in fw.CLUSTER_COLORS[c]]).all() \
                or number(c, f) == 16
assert len({fw.CLUSTER_COLORS[c] for c in range(16)}) == 16

# Frames go out as F + offset chunks that reassemble to the buffer.
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.bind(("127.0.0.1", 0))
rx.settimeout(1)
data = bytes(range(256)) * 18  # 4608 bytes
fw.TeensyLink("127.0.0.1", rx.getsockname()[1]).send_frame(data)
got = bytearray(len(data))
while True:
    pkt = rx.recv(2048)
    assert pkt[0:1] == b"F"
    off = int.from_bytes(pkt[1:3], "little")
    got[off:off + len(pkt) - 3] = pkt[3:]
    if off + len(pkt) - 3 == len(data):
        break
assert bytes(got) == data

print("ok")
