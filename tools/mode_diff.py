#!/usr/bin/env python3
"""Diff bus traffic before/after a single mode-button press (DESIGN.md Phase C).

Captures a baseline window, waits for you to press the physical mode button
once, captures a second window, then reports:

  * whether 0x1E1 carried bit 39 set during the press window (the Gen 2
    DriveModeButton candidate -- confirm or deny it here), and
  * every other arbitration ID whose payload set changed or that newly
    appeared -- candidates for the current-mode *status* signal.

Car stationary, ignition on. Usage:
  ./mode_diff.py [--channel can0] [--window 4.0]
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict

import can

BUTTON_ADDR = 0x1E1
BUTTON_BIT39_BYTE, BUTTON_BIT39_MASK = 4, 0x80


def capture(bus: can.BusABC, seconds: float) -> dict[int, set[bytes]]:
    seen: dict[int, set[bytes]] = defaultdict(set)
    end = time.time() + seconds
    while time.time() < end:
        msg = bus.recv(timeout=max(0.0, end - time.time()))
        if msg is not None:
            seen[msg.arbitration_id].add(bytes(msg.data))
    return seen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--window", type=float, default=4.0)
    args = ap.parse_args()

    bus = can.Bus(interface="socketcan", channel=args.channel)
    try:
        input("Baseline capture -- leave the button ALONE, press Enter...")
        before = capture(bus, args.window)
        print(f"  {len(before)} IDs seen")

        input("Now press the mode button ONCE, then press Enter immediately...")
        after = capture(bus, args.window)
        print(f"  {len(after)} IDs seen\n")

        # 0x1E1 bit 39 check
        hits = [d for d in after.get(BUTTON_ADDR, set())
                if len(d) > BUTTON_BIT39_BYTE and d[BUTTON_BIT39_BYTE] & BUTTON_BIT39_MASK]
        if hits:
            print(f"0x1E1 bit39 SET in {len(hits)} payload(s): "
                  f"{', '.join(h.hex(' ') for h in hits)}  <-- matches Gen 2 candidate")
        else:
            print("0x1E1 bit39 NOT seen set -- Gen 1 button press is elsewhere; "
                  "look at the changed IDs below")

        print("\nIDs that changed or newly appeared after the press:")
        for addr in sorted(set(before) | set(after)):
            b, a = before.get(addr, set()), after.get(addr, set())
            if a and a != b:
                added = a - b
                tag = "NEW" if not b else "changed"
                sample = ", ".join(x.hex(" ") for x in list(added)[:3]) or "(same set)"
                print(f"  {hex(addr):>6}  {tag:<7} +{sample}")
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
