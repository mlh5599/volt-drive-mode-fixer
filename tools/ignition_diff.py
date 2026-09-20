#!/usr/bin/env python3
"""Diff bus traffic across an ignition on -> off (RAP) -> on cycle.

Passive discovery for a real ignition-state signal, as opposed to the
existing ``bus_active`` heuristic (``voltdmf/state.py``), which only tracks
whether *any* known frame has arrived recently and does not go False in the
window this tool targets.

2026-09-19 field report: the car was switched off *without* opening a door.
The instrument cluster stayed lit, the Pi stayed powered, and the bus kept
carrying 0x1F4/0x1F5/0x3E9/etc -- a retained-power-like window ("RAP") that
this project has no confirmed signal to distinguish from true ignition-on.
The interim mitigation (``voltdmf/modecycle.py`` ``MENU_UNRESPONSIVE_TAPS``)
only shortens a doomed walk; it does not know the ignition is off. This tool
is how the next drive nails down a real signal, the same way
``soc_log.py``/``soc_report.py`` nailed down SOC.

Two things get reported, per the three-window capture:

1. **IDs that appear/disappear** between the ON and OFF windows -- e.g. does
   0x1E1 (module heartbeat / button frame) go quiet, even though 0x1F4 does
   not? An ID present in both ON windows but absent OFF (or vice versa) is
   promising by itself, no byte-level analysis needed.
2. **Byte-level candidates** (as ``headlight_diff.py``) -- for IDs present in
   all three windows, a byte steady within *both* ON windows but different
   OFF. Requiring reversion in the second ON window rules out noise and
   rolling counters.

Procedure -- ignition ON, then OFF **without opening a door** (this is the
whole point: replicate the RAP window, not a normal power-down), then ON
again:
  ./ignition_diff.py [--channel can0] [--window 8.0]

A longer --window than headlight_diff's default helps here: some of this may
show up as slower frame *rate* rather than a payload change, and an 8 s
sample gives more of the slower-cadence IDs a chance to appear at all.

**Power the Pi from an external battery/UPS for this run, not the stock
accessory-socket USB charger.** As of 2026-09-19 (owner-observed, not yet
root-caused): the ignition-off transition puts a momentary blip/brownout on
that supply that resets the Pi. It comes back up seconds later into the RAP
window with a fresh boot -- which is a fine trigger for the daemon's own
behavior, but fatal for this script: it is one Python process holding one
`can.Bus` open across all three prompts, and a reset kills it mid-capture
with no `off` window recorded. An external supply that rides through the
blip is what keeps the on1/off/on2 capture in one unbroken run.
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


def _report_appear_disappear(
    on1: dict[int, set[bytes]], off: dict[int, set[bytes]], on2: dict[int, set[bytes]]
) -> bool:
    print("IDs present in both ON windows but absent OFF (goes quiet with "
          "ignition off):")
    vanished = sorted((set(on1) & set(on2)) - set(off))
    if vanished:
        for addr in vanished:
            print(f"  {hex(addr)}")
    else:
        print("  none")

    print("\nIDs absent in both ON windows but present OFF (RAP-only traffic):")
    appeared = sorted(set(off) - set(on1) - set(on2))
    if appeared:
        for addr in appeared:
            print(f"  {hex(addr)}")
    else:
        print("  none")

    print("\n0x1E1 (mode-button frame) specifically:", end=" ")
    on1_has, off_has, on2_has = 0x1E1 in on1, 0x1E1 in off, 0x1E1 in on2
    print(f"on1={on1_has} off={off_has} on2={on2_has}")

    return bool(vanished or appeared)


def _report_byte_candidates(
    on1: dict[int, set[bytes]], off: dict[int, set[bytes]], on2: dict[int, set[bytes]]
) -> bool:
    print("\nByte-level candidates (steady on/on, different while off):")
    found = False
    for addr in sorted(set(on1) & set(off) & set(on2)):
        width = max(len(p) for p in on1[addr] | off[addr] | on2[addr])
        for i in range(width):
            v_on1 = _byte_values(on1[addr], i)
            v_off = _byte_values(off[addr], i)
            v_on2 = _byte_values(on2[addr], i)
            if v_on1 and v_on1 == v_on2 and v_off and v_off != v_on1:
                found = True
                print(f"  {hex(addr):>6} byte {i}: "
                      f"on={sorted(v_on1)} off={sorted(v_off)} "
                      f"on={sorted(v_on2)}")
    if not found:
        print("  none")
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--window", type=float, default=8.0)
    args = ap.parse_args()

    bus = can.Bus(interface="socketcan", channel=args.channel)
    try:
        input("Ignition ON, car settled -- press Enter...")
        on1 = capture(bus, args.window)
        print(f"  {len(on1)} IDs seen")

        input("Now turn the car OFF WITHOUT opening a door (replicate the "
              "RAP window), then press Enter immediately...")
        off = capture(bus, args.window)
        print(f"  {len(off)} IDs seen")
        if not off:
            print("  (bus went fully quiet -- this was NOT the RAP window; "
                  "bus_active already covers this case, nothing new to find "
                  "here)")

        input("Turn the ignition back ON now, then press Enter immediately...")
        on2 = capture(bus, args.window)
        print(f"  {len(on2)} IDs seen\n")

        found_ids = _report_appear_disappear(on1, off, on2)
        found_bytes = _report_byte_candidates(on1, off, on2)
        if not (found_ids or found_bytes):
            print("\nnothing turned up -- try a longer --window, or the "
                  "signal may live on a bus this rig isn't wired to")
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
