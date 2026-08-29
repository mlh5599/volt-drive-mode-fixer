#!/usr/bin/env python3
"""Find the current-mode status signal by walking all four modes.

For each mode (you set it with the physical button, in cycle order
NORMAL -> SPORT -> MOUNTAIN -> HOLD), capture a short window and record the
distinct payloads per arbitration ID. Then print, for every ID whose payload
differs across modes, the payload seen in each mode -- the status signal is
the ID (and byte) that changes by exactly one step per mode in cycle order.

Car stationary, ignition on. Usage:
  ./cycle_modes.py [--channel can0] [--window 3.0] [--only 0x1F5,0x135,...]
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict

import can

MODES = ["NORMAL", "SPORT", "MOUNTAIN", "HOLD"]


def capture(bus: can.BusABC, seconds: float, only: set[int] | None) -> dict[int, set[bytes]]:
    seen: dict[int, set[bytes]] = defaultdict(set)
    end = time.time() + seconds
    while time.time() < end:
        msg = bus.recv(timeout=max(0.0, end - time.time()))
        if msg is None:
            continue
        if only is None or msg.arbitration_id in only:
            seen[msg.arbitration_id].add(bytes(msg.data))
    return seen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--window", type=float, default=3.0)
    ap.add_argument("--only", default="", help="comma-separated IDs to restrict to")
    args = ap.parse_args()
    only = {int(x, 0) for x in args.only.split(",") if x.strip()} or None

    bus = can.Bus(interface="socketcan", channel=args.channel)
    per_mode: dict[str, dict[int, set[bytes]]] = {}
    try:
        for mode in MODES:
            input(f"\nSet the car to {mode} with the button, then press Enter...")
            per_mode[mode] = capture(bus, args.window, only)
            print(f"  captured {len(per_mode[mode])} IDs in {mode}")
    finally:
        bus.shutdown()

    all_ids = sorted({a for m in per_mode.values() for a in m})
    print("\nIDs whose payload set differs across modes "
          "(look for one that steps once per mode):")
    for addr in all_ids:
        sets = [frozenset(per_mode[m].get(addr, set())) for m in MODES]
        if len(set(sets)) == 1:
            continue
        print(f"\n  {hex(addr)}:")
        for m, s in zip(MODES, sets):
            shown = ", ".join(x.hex(" ") for x in list(s)[:4]) or "(none)"
            print(f"    {m:<9} {shown}")


if __name__ == "__main__":
    main()
