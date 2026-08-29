#!/usr/bin/env python3
"""Live view of frame 0x206 with several candidate decodings (DESIGN.md Phase C).

DESIGN.md says SOC is "bytes 1-2, ~0.25 kWh/count per OVMS notes", but byte
order, offset and scaling are all unverified for this pack. Run this while
driving the pack down and write down the dash %/kWh next to the printed raw
value at a few points -- that gives you the real scale/offset to put in
voltdmf/signals.py (SOC_KWH_PER_COUNT / GEN1_PACK_USABLE_KWH).

Usage:  ./watch_soc.py [--channel can0] [--addr 0x206] [--every 1.0]
"""

from __future__ import annotations

import argparse
import struct
import time

import can


def decodings(data: bytes) -> dict[str, int]:
    out: dict[str, int] = {}
    if len(data) >= 3:
        out["b1-2 BE"] = struct.unpack_from(">H", data, 1)[0]
        out["b1-2 LE"] = struct.unpack_from("<H", data, 1)[0]
    if len(data) >= 2:
        out["b0-1 BE"] = struct.unpack_from(">H", data, 0)[0]
    if len(data) >= 4:
        out["b2-3 BE"] = struct.unpack_from(">H", data, 2)[0]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--addr", default="0x206")
    ap.add_argument("--every", type=float, default=1.0,
                    help="seconds between printed lines")
    args = ap.parse_args()
    addr = int(args.addr, 0)

    bus = can.Bus(interface="socketcan", channel=args.channel)
    print(f"watching {hex(addr)} on {args.channel}; Ctrl-C to stop")
    print("wall-clock            raw-bytes             " +
          "  ".join(f"{k:>9}" for k in ("b1-2 BE", "b1-2 LE", "b0-1 BE", "b2-3 BE")))
    last = 0.0
    try:
        for msg in bus:
            if msg.arbitration_id != addr:
                continue
            now = time.time()
            if now - last < args.every:
                continue
            last = now
            d = decodings(bytes(msg.data))
            cols = "  ".join(f"{d.get(k, ''):>9}" for k in
                             ("b1-2 BE", "b1-2 LE", "b0-1 BE", "b2-3 BE"))
            stamp = time.strftime("%H:%M:%S", time.localtime(now))
            print(f"{stamp}   {bytes(msg.data).hex(' '):<20}  {cols}")
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
