# Raspberry Pi → Teensy 4.1 streaming spec

For *Laments of Calypso / Orkideakaiho* — 84-fan LED wall.

This is everything the Pi side needs to know. The Teensy handles all
LED addressing; the Pi's only job is to produce an **84 × 56 RGB image,
30 times a second, and send it row by row over UDP.**

---

## 1. The short version

Send a plain raster image. Row 0 is the top of the wall, column 0 the
left. Don't worry about fans, hubs, panels, or wiring order — the Teensy
maps pixels to LEDs internally. If you render it as a normal 84 × 56
bitmap, it comes out right way up on the wall.

---

## 2. Network

| | |
|---|---|
| Teensy IP | `192.168.60.50` |
| Teensy port | `5005` (UDP) |
| Pi IP | `192.168.60.20` |
| Netmask | `255.255.255.0` |
| Gateway | none — direct cable, no router |

Direct Ethernet link between Pi and Teensy. Measured RTT is 0.18 ms
median, 0% loss over 800 packets. Bandwidth at 30 fps is about 3.5 Mbps
on a 100 Mbps link, so there is no throughput concern.

Pi interface setup (Debian 13 / NetworkManager):

```
sudo nmcli con add type ethernet ifname eth0 con-name teensy-link \
  ipv4.method manual ipv4.addresses 192.168.60.20/24 \
  ipv4.never-default yes ipv6.method disabled
sudo nmcli con up teensy-link
```

`never-default yes` matters — without it this link steals the default
route and the Pi loses internet over WiFi.

---

## 3. Frame geometry

```
FRAME_W = 84    # columns
FRAME_H = 56    # rows
```

Origin is **top-left**, row-major, same as any image library.

Pixel (0,0) is the top-left corner of the wall as the viewer sees it.

### Dead zones

The wall is not a full rectangle. Four corner regions have no fans:

```
       0        21        42        63       84
   0   +--------+---------+---------+--------+
       | DEAD   |         |         | DEAD   |
  14   +--------+---------+---------+--------+
       |        |         |         |        |
  28   +--------+---------+---------+--------+
       |        |         |         |        |
  42   +--------+---------+---------+--------+
       | DEAD   |         |         | DEAD   |
  56   +--------+---------+---------+--------+
```

- Top-left dead zone: x 0–20, y 0–6
- Top-right dead zone: x 63–83, y 0–6
- Bottom-left dead zone: x 0–20, y 49–55
- Bottom-right dead zone: x 63–83, y 49–55

**Send full 84 × 56 frames anyway.** Pixels in dead zones are discarded
by the Teensy. Don't try to skip them — the packet layout is fixed.

But do keep them in mind when composing: anything important placed in a
corner is invisible. The lit area is a rectangle with four notches cut
out of it.

---

## 4. Packet format

One UDP datagram per row. **255 bytes**, fixed size.

| Offset | Size | Contents |
|---|---|---|
| 0 | 1 | Sync byte, always `0xAA` |
| 1 | 1 | Frame sequence number, 0–255, wraps |
| 2 | 1 | Row index, 0–55 |
| 3–254 | 252 | 84 pixels × 3 bytes, `R,G,B` order |

Notes:

- **RGB, not BGR.** OpenCV gives BGR by default; convert.
- The sequence number must be **the same for every row of one frame**
  and increment by 1 for the next frame. The Teensy uses the change in
  sequence number to know a frame is finished.
- Packets of any other size are discarded silently.
- Rows may arrive in any order. The Teensy reassembles by row index.
- If a row is lost, the Teensy shows the frame anyway once the next
  sequence number arrives, or after 120 ms, whichever is first. A
  dropped packet costs one slightly stale row, not a stall.

Send all 56 rows of a frame back to back, then pace to the next frame.

---

## 5. Frame rate

Target **30 fps**. The Teensy's LED output takes about 3 ms per frame,
so there is roughly 10× headroom — 30 is a content choice, not a limit.

The Teensy prints `fps N torn M` on Serial once a second. `torn` counting
above zero means rows are being lost or the Pi is sending faster than it
can reassemble.

---

## 6. Image processing — this part matters

The wall has 84 × 56 cells but only **16 LEDs per fan**, arranged as a
ring. Effective perceived resolution is closer to **12 × 8 soft blobs**
than to 84 × 56 pixels. Content has to be composed for that.

### Blur before downsampling, not after

This is the single most important step. Downsampling a detailed frame
straight to 84 × 56 produces aliasing that reads as flicker and crawling
noise on the wall.

```python
blurred = cv2.GaussianBlur(frame, (0, 0), sigmaX=source_width / 120)
small   = cv2.resize(blurred, (84, 56), interpolation=cv2.INTER_AREA)
rgb     = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
```

Tune the sigma by eye. Too little and it shimmers; too much and
everything turns to mush. `source_width / 120` is a starting point.

### Progressive colour rendering

Apply on the Pi, not the Teensy. The LEDs are not perceptually linear:
greens read much brighter than reds and pinks at the same digital value.
Scale greens down and reds/pinks up so the palette reads as intended.

This also cuts power draw substantially — an all-red frame draws roughly
a third of what an all-white frame does.

### What reads well

Large masses, slow movement, tonal shifts, silhouettes. Per-fan
structure — rotation within the ring, radial pulses, phase offsets
between fans — reads as intentional. Fine detail and fast cuts do not
survive the downsample.

---

## 7. Reference sender

`rpi_video_streamer.py` in this folder is the working prototype, but it
is written for the **old 42 × 63 geometry and the old 128-byte packet**.
It needs updating to 84 × 56 and the 255-byte format above. Use it as a
structural reference, not as correct code.

---

## 8. Test sequence

Worth doing in this order:

1. **Solid colours.** Send all-red, all-green, all-blue full frames.
   Confirms colour order and that every hub lights.
2. **Single row.** Light row 0 only. It should appear along the top edge
   of the main block, with the corners dark.
3. **Single column.** Light column 0 only. Should appear on the left edge
   of the left wing, dark at top and bottom.
4. **Corner probe.** Light pixel (3, 11). That is LED 0 — the first fan
   of hub 0, the top-left visible fan of the left wing. If a different
   fan lights, the hub-to-pin order is wrong.
5. **Moving gradient.** Confirms frame pacing and that `torn` stays at 0.

Steps 2 and 3 are the ones that catch a rotated or mirrored frame, which
is the most likely mistake.
