#!/usr/bin/env python3
"""Two questions for DESIGN.md's "Open items" (Phase C):

1. Does the bus go quiet with the car off?  (Global A buses should.)
2. Does the car power up in NORMAL every ignition cycle, or remember the
   last-used mode?  -- decides whether the on-start trigger can assume a
   fixed starting mode or needs the status signal too.

Run once per ignition state as prompted. Pass --status-addr once cycle_modes.py
has identified the current-mode status frame.

Usage:
  ./ignition_check.py [--channel can0] [--status-addr 0x1F5] [--window 5.0]
"""

from __future__ import annotations

import argparse
import time

import can


def sample(bus: can.BusABC, seconds: float, status_addr: int | None):
    frames = 0
    status_payloads: set[bytes] = set()
    end = time.time() + seconds
    while time.time() < end:
        msg = bus.recv(timeout=max(0.0, end - time.time()))
        if msg is None:
            continue
        frames += 1
        if status_addr is not None and msg.arbitration_id == status_addr:
            status_payloads.add(bytes(msg.data))
    rate = frames / seconds
    return rate, status_payloads


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--status-addr", default="")
    ap.add_argument("--window", type=float, default=5.0)
    args = ap.parse_args()
    status_addr = int(args.status_addr, 0) if args.status_addr else None

    bus = can.Bus(interface="socketcan", channel=args.channel)
    try:
        input("IGNITION ON, in a known mode (e.g. NORMAL). Press Enter...")
        rate_on, s_on = sample(bus, args.window, status_addr)
        print(f"  frame rate: {rate_on:.0f}/s   status payloads: "
              f"{[p.hex(' ') for p in s_on] or 'n/a'}")

        input("Now turn the IGNITION OFF. Press Enter...")
        rate_off, _ = sample(bus, args.window, status_addr)
        print(f"  frame rate: {rate_off:.0f}/s  "
              f"({'bus goes quiet -- good' if rate_off < 1 else 'bus still active!'})")

        input("Turn the IGNITION back ON. Do NOT touch the mode button. Press Enter...")
        rate_on2, s_on2 = sample(bus, args.window, status_addr)
        print(f"  frame rate: {rate_on2:.0f}/s   status payloads: "
              f"{[p.hex(' ') for p in s_on2] or 'n/a'}")

        if status_addr is not None:
            if s_on2 and s_on2 == s_on:
                print("\n=> status frame identical before/after: mode is REMEMBERED "
                      "across ignition (on-start trigger needs the status signal).")
            elif s_on2:
                print("\n=> status frame differs: likely RESET to a default mode "
                      "on ignition (on-start trigger may assume a fixed start).")
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
