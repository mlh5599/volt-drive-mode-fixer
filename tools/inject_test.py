#!/usr/bin/env python3
"""On-vehicle injection test (DESIGN.md Phase C.5).  THIS TRANSMITS.

Injects full mode-button presses on 0x1F4 through the production TX path
(voltdmf.canio.CanInterface.send_mode_button_press) and reads 0x1F4 back to
report the mode before/after each shot.

On-vehicle 2026-08-29 the press turned out to be duration-gated: ~0.3 s of
byte5=0x80 only wakes the drive-mode screen, ~1.2 s counts as ~3 presses, and
the byte4 release ramp auto-repeats. Default now is a single solid ~0.45 s
byte5 block, no ramp (canio defaults); tune with --down-ms / --rate-hz. One
press should advance one mode ~3 s later. A "shot" can send --presses
back-to-back and you compare the net mode change.

Car MUST be stationary: ignition on, in Park, parking brake set. Run
`candump can0 | grep -iE 'err'` in another shell; scan for DTCs afterward.

Usage:
  ./inject_test.py --yes-stationary [--channel can0]
                   [--presses 1] [--gap 2.0] [--shots 1] [--settle 3.0]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf import canio  # noqa: E402
from voltdmf.canio import CanInterface  # noqa: E402


def can_state(channel: str) -> str:
    """'ERROR-ACTIVE' / 'ERROR-WARNING' / 'BUS-OFF' / 'STOPPED' / '?' for the link."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-stationary", action="store_true",
                    help="required: you confirm the vehicle is stationary")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--presses", type=int, default=1,
                    help="full presses per shot (1st may only wake the screen)")
    ap.add_argument("--gap", type=float, default=2.0,
                    help="seconds between presses within a shot")
    ap.add_argument("--shots", type=int, default=1,
                    help="how many shots to run (Enter between each)")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds after the last press before re-reading the mode")
    ap.add_argument("--rate-hz", type=float, default=None,
                    help="override injection frame rate (default from canio)")
    ap.add_argument("--down-ms", type=int, default=None,
                    help="override byte5=0x80 hold duration (ms)")
    ap.add_argument("--ramp", dest="ramp", action="store_true", default=None,
                    help="send the byte4 0x80/0x40/0x20 decay ramp after release")
    ap.add_argument("--no-ramp", dest="ramp", action="store_false",
                    help="button-down block only, no byte4 ramp (default for tuning)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.yes_stationary:
        sys.exit("refusing to transmit without --yes-stationary")

    if args.rate_hz is not None:
        canio.PRESS_FRAME_INTERVAL_S = 1.0 / args.rate_hz
    if args.down_ms is not None:
        canio.PRESS_DOWN_FRAMES = max(1, round(
            args.down_ms / 1000 / canio.PRESS_FRAME_INTERVAL_S))
    if args.ramp is False:
        canio.PRESS_RAMP_VALUES = ()
        canio.PRESS_IDLE_FRAMES = 0

    down_s = canio.PRESS_DOWN_FRAMES * canio.PRESS_FRAME_INTERVAL_S
    ramp_s = (canio.PRESS_RAMP_FRAMES * len(canio.PRESS_RAMP_VALUES)
              * canio.PRESS_FRAME_INTERVAL_S)
    ramp_desc = (f", then byte4 ramp {'/'.join(f'0x{v:02x}' for v in canio.PRESS_RAMP_VALUES)}"
                 f" over {ramp_s:.2f}s" if canio.PRESS_RAMP_VALUES else " (no ramp)")
    print(f"TX {hex(canio.MODE_BUTTON_ADDR)} @ {1 / canio.PRESS_FRAME_INTERVAL_S:.0f} Hz: "
          f"byte5=0x80 for {down_s:.2f}s{ramp_desc}. byte1 mirrors the live bus.\n")

    st = can_state(args.channel)
    if not args.dry_run and st != "ERROR-ACTIVE":
        sys.exit(f"can0 state is {st!r}, not ERROR-ACTIVE -- bring the bus up "
                 f"cleanly first (see the checklist). Refusing to transmit.")

    with CanInterface(args.channel, dry_run=args.dry_run) as can_if:
        start = can_if.read_drive_mode(timeout=2.0)
        print(f"current mode: {start.value if start else 'UNKNOWN (no 0x1F4?)'}  "
              f"| can0: {st}")
        for s in range(1, args.shots + 1):
            input(f"\n[shot {s}/{args.shots}] Enter to inject {args.presses} press(es)...")
            if not args.dry_run and can_state(args.channel) != "ERROR-ACTIVE":
                print("  can0 left ERROR-ACTIVE -- aborting this shot. Recover the "
                      "bus and restart.")
                break
            before = can_if.read_drive_mode(timeout=2.0)
            for k in range(args.presses):
                can_if.send_mode_button_press()
                print(f"  press {k + 1}/{args.presses} sent")
                if k != args.presses - 1:
                    time.sleep(args.gap)
            time.sleep(args.settle)
            after = can_if.read_drive_mode(timeout=2.0)
            b = before.value if before else "?"
            a = after.value if after else "?"
            tag = "->" if before != after else "== NO CHANGE"
            print(f"  {b} {tag} {a}   | can0: {can_state(args.channel)}   "
                  f"(confirm against the dash)")

    print("\nDone. Scan for new DTCs before any driving; keep the daemon in "
          "--dry-run until one press reliably = one step.")


if __name__ == "__main__":
    main()
