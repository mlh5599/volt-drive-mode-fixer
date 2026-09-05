#!/usr/bin/env python3
"""Diff bus traffic across a headlights off -> on -> off cycle.

Passive discovery for an exterior-lighting status signal (goal: dim the LCD
backlight automatically when the headlights are on). Captures three windows
and reports, per arbitration ID and byte position, bytes that are constant
*within* the two off windows but differ while the lights are on. Requiring
the value to revert in the second off window rules out both noise and a
rolling counter, either of which would keep changing across all three
windows rather than settling back to where it started.

If nothing turns up: this rig only taps HS-CAN (OBD-II pins 6/14), and on
GM Global A, exterior lighting is normally owned by the BCM on the separate
low-speed single-wire GMLAN -- the signal may simply not be present here.

Car stationary, ignition on. Usage:
  ./headlight_diff.py [--channel can0] [--window 5.0]
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict

import can


def capture(bus: can.BusABC, seconds: float) -> dict[int, set[bytes]]:
    seen: dict[int, set[bytes]] = defaultdict(set)
    end = time.time() + seconds
    while time.time() < end:
        msg = bus.recv(timeout=max(0.0, end - time.time()))
        if msg is not None:
            seen[msg.arbitration_id].add(bytes(msg.data))
    return seen


def _byte_values(payloads: set[bytes], index: int) -> set[int]:
    return {p[index] for p in payloads if len(p) > index}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--window", type=float, default=5.0)
    args = ap.parse_args()

    bus = can.Bus(interface="socketcan", channel=args.channel)
    try:
        input("Headlights OFF -- leave them off, press Enter...")
        off1 = capture(bus, args.window)
        print(f"  {len(off1)} IDs seen")

        input("Turn headlights ON now, then press Enter immediately...")
        on = capture(bus, args.window)
        print(f"  {len(on)} IDs seen")

        input("Turn headlights back OFF now, then press Enter immediately...")
        off2 = capture(bus, args.window)
        print(f"  {len(off2)} IDs seen\n")

        print("Candidates (byte steady off/off, different while on):")
        found = False
        for addr in sorted(set(off1) & set(on) & set(off2)):
            width = max(len(p) for p in off1[addr] | on[addr] | off2[addr])
            for i in range(width):
                v_off1 = _byte_values(off1[addr], i)
                v_on = _byte_values(on[addr], i)
                v_off2 = _byte_values(off2[addr], i)
                if v_off1 and v_off1 == v_off2 and v_on and v_on != v_off1:
                    found = True
                    print(f"  {hex(addr):>6} byte {i}: "
                          f"off={sorted(v_off1)} on={sorted(v_on)} "
                          f"off={sorted(v_off2)}")
        if not found:
            print("  none -- try a longer --window, or it may live on the "
                  "low-speed GMLAN body bus this rig isn't wired to")
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
