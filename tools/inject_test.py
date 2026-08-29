#!/usr/bin/env python3
"""Bench-safe injection test (DESIGN.md Phase C.5).  THIS TRANSMITS.

Replays the discovered mode-button press through the exact production TX path
(voltdmf.canio.CanInterface.send_mode_button_press), one press at a time, with
a confirmation between each. Confirm that:

  * each press advances the mode exactly one step (NORMAL->SPORT->MOUNTAIN->HOLD),
  * the order is as expected, and
  * nothing else misbehaves (watch `candump can0` for error frames in another
    shell, then scan for new DTCs afterward).

Car MUST be stationary: ignition on, engine off or on jack stands. Do not run
this while driving, and do not proceed to on-road testing until it passes.

Usage:
  ./inject_test.py --yes-stationary [--channel can0] [--presses 3]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf.canio import (  # noqa: E402
    MODE_BUTTON_ADDR_UNCONFIRMED,
    MODE_BUTTON_PAYLOAD_UNCONFIRMED,
    SEND_CLUSTER_SIZE,
    CanInterface,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-stationary", action="store_true",
                    help="required: you confirm the vehicle is stationary")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--presses", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.yes_stationary:
        sys.exit("refusing to transmit without --yes-stationary")

    print(f"TX frame: {hex(MODE_BUTTON_ADDR_UNCONFIRMED)} "
          f"#{MODE_BUTTON_PAYLOAD_UNCONFIRMED.hex(' ')}  "
          f"({SEND_CLUSTER_SIZE} frames per press)")
    print("If this address/payload is still the UNCONFIRMED Gen 2 value, run "
          "mode_diff.py first.\n")

    with CanInterface(args.channel, dry_run=args.dry_run) as can_if:
        for i in range(1, args.presses + 1):
            input(f"[{i}/{args.presses}] Press Enter to send one button press...")
            can_if.send_mode_button_press()
            print("  sent. Check the dash: did the mode advance exactly one step?")

    print("\nDone. Now scan for new DTCs (e.g. an OBD reader) before any driving.")


if __name__ == "__main__":
    main()
