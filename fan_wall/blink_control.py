#!/usr/bin/env python3
"""
Controller for the Teensy blink-over-Ethernet test.

Sends a blink interval to the Teensy over UDP and waits for the
acknowledgement, so you can see whether the link actually works both ways
before building the video pipeline on top of it.

Usage:
    python blink_control.py                    # interactive
    python blink_control.py --set 250          # one-shot, then exit
    python blink_control.py --sweep            # ramp 70 <-> 2000 continuously
    python blink_control.py --test 200         # send 200 packets, report loss

Interactive commands:
    <number>   set the interval in ms (70-2000)
    +  / -     nudge by 50 ms
    ++ / --    nudge by 200 ms
    min / max  jump to 70 / 2000
    ping       round-trip check
    get        ask the Teensy what it currently has
    sweep      ramp up and down until you press ctrl-c
    test       packet-loss test
    q          quit

Only needs the standard library.
"""

import argparse
import socket
import sys
import time

TEENSY_IP = "192.168.60.50"   # must match the sketch (or DHCP-assigned address)
TEENSY_PORT = 5005
TIMEOUT = 1.0

MIN_MS = 70
MAX_MS = 2000


class Link:
    def __init__(self, ip, port, timeout=TIMEOUT):
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def send(self, message, expect_reply=True):
        """Send a command, return the reply text or None on timeout."""
        self.sock.sendto(message.encode("ascii"), self.addr)
        if not expect_reply:
            return None
        try:
            data, _ = self.sock.recvfrom(256)
            return data.decode("ascii", errors="replace")
        except socket.timeout:
            return None

    def close(self):
        self.sock.close()


def clamp(value):
    return max(MIN_MS, min(MAX_MS, value))


def set_interval(link, ms, quiet=False):
    ms = clamp(int(ms))
    t0 = time.perf_counter()
    reply = link.send(str(ms))
    rtt = (time.perf_counter() - t0) * 1000
    if reply is None:
        print(f"  {ms:>4} ms  ->  no reply (timeout)")
        return None
    if not quiet:
        print(f"  {ms:>4} ms  ->  {reply}   [{rtt:.1f} ms round trip]")
    return ms


def sweep(link, step=25, dwell=0.04):
    """Ramp the interval up and down until interrupted."""
    print("Sweeping 70 <-> 2000 ms. Ctrl-C to stop.")
    value = MIN_MS
    direction = step
    try:
        while True:
            link.send(str(value), expect_reply=False)
            sys.stdout.write(f"\r  {value:>4} ms   ")
            sys.stdout.flush()
            value += direction
            if value >= MAX_MS:
                value, direction = MAX_MS, -step
            elif value <= MIN_MS:
                value, direction = MIN_MS, step
            time.sleep(dwell)
    except KeyboardInterrupt:
        print("\nStopped.")


def loss_test(link, count=200, gap=0.005):
    """Send many commands and count how many come back.

    This is the number worth knowing before the video pipeline: 63 packets
    per frame at 30 fps is ~1890/sec, so even a low loss rate shows up.
    """
    print(f"Sending {count} packets, {gap * 1000:.0f} ms apart...")
    replies = 0
    rtts = []
    for i in range(count):
        ms = MIN_MS + (i * 7) % (MAX_MS - MIN_MS)
        t0 = time.perf_counter()
        reply = link.send(str(ms))
        if reply is not None:
            replies += 1
            rtts.append((time.perf_counter() - t0) * 1000)
        time.sleep(gap)

    lost = count - replies
    print(f"  replies : {replies}/{count}")
    print(f"  lost    : {lost} ({lost / count * 100:.1f}%)")
    if rtts:
        rtts.sort()
        print(f"  rtt min : {rtts[0]:.2f} ms")
        print(f"  rtt med : {rtts[len(rtts) // 2]:.2f} ms")
        print(f"  rtt max : {rtts[-1]:.2f} ms")


def interactive(link):
    current = MIN_MS
    print(f"Talking to {link.addr[0]}:{link.addr[1]}")
    reply = link.send("ping")
    if reply is None:
        print("No reply to ping. Check the IP, the cable, and that both "
              "machines are on the same subnet.")
    else:
        print(f"  {reply}")
    print("Type a number 70-2000, or 'q' to quit. '?' for commands.\n")

    while True:
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue
        if line in ("q", "quit", "exit"):
            return
        if line == "?":
            print("  <number> | + | - | ++ | -- | min | max | ping | get | "
                  "sweep | test | q")
            continue
        if line == "ping":
            print(f"  {link.send('ping') or 'no reply (timeout)'}")
            continue
        if line == "get":
            print(f"  {link.send('get') or 'no reply (timeout)'}")
            continue
        if line == "sweep":
            sweep(link)
            continue
        if line == "test":
            loss_test(link)
            continue
        if line == "min":
            current = set_interval(link, MIN_MS) or current
            continue
        if line == "max":
            current = set_interval(link, MAX_MS) or current
            continue
        if line in ("+", "-", "++", "--"):
            delta = {"+": 50, "-": -50, "++": 200, "--": -200}[line]
            current = set_interval(link, current + delta) or current
            continue

        try:
            current = set_interval(link, int(line)) or current
        except ValueError:
            print("  Not a number. '?' for commands.")


def main():
    ap = argparse.ArgumentParser(description="Teensy blink-over-UDP controller.")
    ap.add_argument("--ip", default=TEENSY_IP, help=f"Teensy address (default {TEENSY_IP})")
    ap.add_argument("--port", type=int, default=TEENSY_PORT, help=f"UDP port (default {TEENSY_PORT})")
    ap.add_argument("--set", type=int, metavar="MS", help="set the interval once and exit")
    ap.add_argument("--sweep", action="store_true", help="ramp continuously")
    ap.add_argument("--test", type=int, nargs="?", const=200, metavar="N",
                    help="packet-loss test with N packets (default 200)")
    args = ap.parse_args()

    link = Link(args.ip, args.port)
    try:
        if args.set is not None:
            set_interval(link, args.set)
        elif args.sweep:
            sweep(link)
        elif args.test is not None:
            loss_test(link, args.test)
        else:
            interactive(link)
    finally:
        link.close()


if __name__ == "__main__":
    main()
