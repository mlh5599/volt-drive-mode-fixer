#!/usr/bin/env python3
"""On-vehicle injection test (DESIGN.md Phase C.5).  THIS TRANSMITS.

Closed-loop mode-button injection on 0x1F4 through the production TX path
(voltdmf.canio.CanInterface.send_mode_button_press). Each press is one
burst-and-release: a short burst of byte5=0x80 frames, then silence so the
body module's own idle 0x1F4 stream is the "button up". After every press the
tool reads 0x1F4 back, prints the mode transition + can0 health, and decides
whether to press again -- it does NOT blind-count.

Goal of the sweep: find a --burst-ms / --rate-hz where one press advances the
dash exactly one step, all the way around NORMAL -> SPORT -> MOUNTAIN -> HOLD
-> NORMAL, with can0 staying ERROR-ACTIVE and zero error frames / new DTCs.

Car MUST be stationary and in full READY: in Park, parking brake set. Run
`candump can0 | grep -iE 'err'` in another shell; scan for DTCs afterward.

Usage:
  ./inject_test.py --yes-stationary [--channel can0]
                   (--steps 1 | --target sport)
                   [--burst-ms 450] [--rate-hz 100] [--gap 0.75]
                   [--settle 3.0] [--max-presses 6] [--step-confirm] [--dry-run]
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
from voltdmf.signals import MODE_CYCLE_ORDER, DriveMode  # noqa: E402

_MODE_BY_NAME = {m.value: m for m in MODE_CYCLE_ORDER}


def can_state(channel: str) -> str:
    """'ERROR-ACTIVE' / 'ERROR-WARNING' / 'BUS-OFF' / 'STOPPED' / '?' for the link."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def advance(mode: DriveMode, n: int = 1) -> DriveMode:
    """The mode ``n`` single button steps forward from ``mode``."""
    order = MODE_CYCLE_ORDER
    return order[(order.index(mode) + n) % len(order)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-stationary", action="store_true",
                    help="required: you confirm the vehicle is stationary and in Park")
    ap.add_argument("--channel", default="can0")

    goal = ap.add_mutually_exclusive_group()
    goal.add_argument("--steps", type=int, default=1,
                      help="advance this many single steps from the current mode (default 1)")
    goal.add_argument("--target", choices=sorted(_MODE_BY_NAME),
                      help="press until the dash reaches this mode")

    ap.add_argument("--burst-ms", type=int, default=None,
                    help="override the byte5=0x80 burst length (ms); default from canio")
    ap.add_argument("--rate-hz", type=float, default=None,
                    help="override the in-burst frame rate; default from canio")
    ap.add_argument("--gap", type=float, default=None,
                    help="seconds of silence between presses (default = canio.RELEASE_GAP_S)")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds after a press before reading the mode back")
    ap.add_argument("--max-presses", type=int, default=6,
                    help="hard cap on presses per run (safety)")
    ap.add_argument("--step-confirm", action="store_true",
                    help="pause for Enter before each press (default: run the loop through)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.yes_stationary:
        sys.exit("refusing to transmit without --yes-stationary")

    # Apply CLI overrides to the production TX constants before opening the bus.
    if args.rate_hz is not None:
        canio.PRESS_FRAME_INTERVAL_S = 1.0 / args.rate_hz
    if args.burst_ms is not None:
        canio.PRESS_BURST_FRAMES = max(1, round(
            args.burst_ms / 1000 / canio.PRESS_FRAME_INTERVAL_S))
    canio.SEND_CLUSTER_SIZE = canio.PRESS_BURST_FRAMES
    gap = canio.RELEASE_GAP_S if args.gap is None else max(args.gap, canio.RELEASE_GAP_S)

    burst_s = canio.PRESS_BURST_FRAMES * canio.PRESS_FRAME_INTERVAL_S
    print(f"burst-and-release: byte5=0x80 x{canio.PRESS_BURST_FRAMES} @ "
          f"{1 / canio.PRESS_FRAME_INTERVAL_S:.0f} Hz ({burst_s:.2f}s), then "
          f">={gap:.2f}s silence. byte1 mirrors the live bus.")

    st = can_state(args.channel)
    if not args.dry_run and st != "ERROR-ACTIVE":
        sys.exit(f"can0 state is {st!r}, not ERROR-ACTIVE -- bring the bus up "
                 f"cleanly first (ip link set {args.channel} up type can "
                 f"bitrate 500000 restart-ms 100). Refusing to transmit.")

    with CanInterface(args.channel, dry_run=args.dry_run) as can_if:
        start = can_if.read_drive_mode(timeout=2.0)
        if start is None and not args.dry_run:
            sys.exit("no decodable 0x1F4 seen -- is the car in full READY? "
                     "Cannot run closed-loop without a mode readback.")
        start = start or DriveMode.NORMAL

        # Two goal styles. --target: press until the dash shows that mode.
        # --steps N: do N presses, each of which should advance exactly one
        # step -- the tuning mode, since it catches both overshoot and
        # wake-only. cap presses either way.
        seek_target = args.target is not None
        if seek_target:
            final_goal = _MODE_BY_NAME[args.target]
            cap = args.max_presses
            print(f"start: {start.value}   goal: reach {final_goal.value}   "
                  f"(<= {cap} presses)   | can0: {st}\n")
        else:
            final_goal = advance(start, args.steps)  # informational
            cap = min(args.max_presses, args.steps)
            print(f"start: {start.value}   goal: {args.steps} single step(s) "
                  f"-> {final_goal.value}   | can0: {st}\n")

        def more() -> bool:
            if presses >= cap:
                return False
            return current != final_goal if seek_target else True

        presses = 0
        current = start
        clean_steps = 0
        while more():
            if args.step_confirm:
                input(f"[press {presses + 1}] Enter to inject one press "
                      f"(at {current.value}, want {final_goal.value})...")

            state_now = can_state(args.channel)
            if not args.dry_run and state_now != "ERROR-ACTIVE":
                print(f"  can0 is {state_now!r}, not ERROR-ACTIVE -- aborting. "
                      f"Recover the bus and restart.")
                break

            before = can_if.read_drive_mode(timeout=2.0) or current
            expected = advance(before, 1)
            can_if.send_mode_button_press()
            presses += 1
            time.sleep(args.settle)
            after = can_if.read_drive_mode(timeout=2.0)

            if args.dry_run:
                print(f"  press {presses}: [dry-run] would expect "
                      f"{before.value} -> {expected.value}")
                break
            if after is None:
                print(f"  press {presses}: lost the 0x1F4 readback -- stopping.")
                break

            if after == expected:
                verdict = "OK (one step)"
                clean_steps += 1
            elif after == before:
                verdict = "WAKE-ONLY / no movement"
            else:
                verdict = f"OVERSHOT (expected {expected.value})"
            print(f"  press {presses}: {before.value} -> {after.value}  "
                  f"[{verdict}]   | can0: {can_state(args.channel)}   "
                  f"(confirm against the dash)")

            current = after
            if current != final_goal:
                time.sleep(gap)

        if args.dry_run:
            print("\n[dry-run] no frames were sent.")
        else:
            reached = current == final_goal
            if reached and clean_steps == presses:
                tail = f"  ALL {presses} PRESS(ES) CLEAN -- one step each."
            elif reached:
                tail = (f"  reached, but only {clean_steps}/{presses} presses "
                        f"were clean single steps.")
            else:
                tail = ("  Tune: OVERSHOT -> lower --burst-ms (350, 300); "
                        "WAKE-ONLY -> raise --burst-ms (550) or --rate-hz 150.")
            print(f"\n{'REACHED' if reached else 'DID NOT REACH'} "
                  f"{final_goal.value} in {presses} press(es) (cap {cap}). "
                  f"can0: {can_state(args.channel)}")
            print(tail)

    print("\nScan for new DTCs before any driving; keep the daemon in --dry-run "
          "until one press reliably = one step around the full cycle.")


if __name__ == "__main__":
    main()
