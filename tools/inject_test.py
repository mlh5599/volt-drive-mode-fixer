#!/usr/bin/env python3
"""On-vehicle injection test (DESIGN.md Phase C.5).  THIS TRANSMITS.

Mode-button injection on 0x1E1 ("ASCMSteeringButton", byte 4 bit 7 = pressed)
through the production TX path
(voltdmf.canio.CanInterface.send_mode_button_press). Each press is a tracking
echo: for ~PRESS_TRACK_FRAMES iterations the daemon waits for the module's
next live 0x1E1, sets byte 4 bit 7, and sends it back into the gap -- a
replica of the captured ~14-frame physical press -- then goes silent.

MENU MODEL (confirmed on-vehicle 2026-08-29, from the owner watching the dash):
  * Menu CLOSED -> any single press opens the menu and selects NORMAL,
    whatever the current mode is.
  * Menu OPEN (next press within ~2 s) -> each press walks the cursor
    NORMAL -> SPORT -> MOUNTAIN -> HOLD -> NORMAL -> ...
  * ~3 s with no press -> the menu times out, the cursor commits, and the
    next press starts over from NORMAL.

So reaching a mode is a WALK: press ``index+1`` times, each < ~2 s apart
(NORMAL index 0 -> 1 press, SPORT 1 -> 2, MOUNTAIN 2 -> 3, HOLD 3 -> 4), then
stop and let it commit. This tool does exactly that and reads 0x1F4 back.

Car MUST be stationary and in full READY: in Park, parking brake set. Run
`candump -L can0 > /tmp/x.log` in another shell; scan for DTCs afterward.
NOTE: with the car stationary in Park the cluster tends to revert to NORMAL a
few seconds after committing a non-NORMAL mode -- that is a "needs a drive"
question, not an injection failure. What this tool checks is that the walk
reaches the target cursor at all.

Usage:
  ./inject_test.py --yes-stationary [--channel can0]
                   (--target sport | --cycle)
                   [--frames 16]        # bit-7-set frames per press (~14 = real)
                   [--walk-gap 1.2]     # seconds between presses in a walk
                   [--commit 3.5]       # seconds to wait for timeout+commit
                   [--repeat 3]         # do the walk N times (--target only)
                   [--dry-run]
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
#: Walk model: presses to reach a mode from a closed menu = index + 1.
_PRESSES_TO_REACH = {m: i + 1 for i, m in enumerate(MODE_CYCLE_ORDER)}


def can_state(channel: str) -> str:
    """'ERROR-ACTIVE' / 'ERROR-WARNING' / 'BUS-OFF' / 'STOPPED' / '?' for the link."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def walk_to(can_if: CanInterface, target: DriveMode, *, walk_gap: float,
            commit: float, channel: str, dry_run: bool) -> DriveMode | None:
    """Do one menu walk to ``target``; return the mode read back from 0x1F4.

    0x1F4 byte 1 lags the commit by several seconds, so after the presses we
    poll it for up to ``commit`` seconds, returning as soon as it reaches the
    target (or whatever it settled on when the budget runs out).
    """
    n = _PRESSES_TO_REACH[target]
    before = can_if.read_drive_mode(timeout=2.0)
    print(f"  walk -> {target.value}: {n} press(es) @ {walk_gap:.1f}s "
          f"(from {before.value if before else '?'})", flush=True)
    for i in range(n):
        st = can_state(channel)
        if not dry_run and st != "ERROR-ACTIVE":
            print(f"    can0 is {st!r} -- aborting walk.")
            return None
        can_if.send_mode_button_press()
        if i < n - 1:
            time.sleep(walk_gap)
    if dry_run:
        print(f"    [dry-run] would now poll 0x1F4 for <= {commit:.1f}s")
        return None
    deadline = time.time() + commit
    after = before
    while time.time() < deadline:
        after = can_if.read_drive_mode(timeout=1.0) or after
        if after == target:
            break
    settled = commit - max(0.0, deadline - time.time())
    print(f"    -> 0x1F4 reads {after.value if after else 'None'} after "
          f"~{settled:.1f}s   | can0: {can_state(channel)}   "
          f"(confirm against the dash)", flush=True)
    return after


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-stationary", action="store_true",
                    help="required: you confirm the vehicle is stationary and in Park")
    ap.add_argument("--channel", default="can0")

    goal = ap.add_mutually_exclusive_group(required=True)
    goal.add_argument("--target", choices=sorted(_MODE_BY_NAME),
                      help="walk the menu to this mode")
    goal.add_argument("--cycle", action="store_true",
                      help="walk NORMAL->SPORT->MOUNTAIN->HOLD->NORMAL, one walk each")

    ap.add_argument("--frames", type=int, default=None,
                    help="bit-7-set frames per press (default canio.PRESS_TRACK_FRAMES "
                         "~16; a real physical press is ~14)")
    ap.add_argument("--walk-gap", type=float, default=1.2,
                    help="seconds between presses within a walk (must be < ~2s "
                         "or the menu times out mid-walk)")
    ap.add_argument("--commit", type=float, default=12.0,
                    help="max seconds to poll 0x1F4 after the last press "
                         "(byte 1 lags the commit ~7-9s on this car)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat the --target walk this many times")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.yes_stationary:
        sys.exit("refusing to transmit without --yes-stationary")
    if args.walk_gap >= 2.5:
        sys.exit(f"--walk-gap {args.walk_gap}s is too long; the menu times out "
                 f"around 3s -- use <= ~2s so the walk stays in one menu session")

    if args.frames is not None:
        canio.PRESS_TRACK_FRAMES = max(1, args.frames)
    canio.PRESS_BURST_FRAMES = canio.PRESS_TRACK_FRAMES
    canio.SEND_CLUSTER_SIZE = canio.PRESS_TRACK_FRAMES

    print(f"tracking echo press on 0x1E1: module frame + byte4|=0x80 x"
          f"{canio.PRESS_TRACK_FRAMES} (~{canio.PRESS_TRACK_FRAMES * 25}ms). "
          f"walk-gap {args.walk_gap:.1f}s, commit wait {args.commit:.1f}s.")

    st = can_state(args.channel)
    if not args.dry_run and st != "ERROR-ACTIVE":
        sys.exit(f"can0 state is {st!r}, not ERROR-ACTIVE -- bring the bus up "
                 f"cleanly first (ip link set {args.channel} up type can "
                 f"bitrate 500000 restart-ms 100). Refusing to transmit.")

    if args.cycle:
        plan = [DriveMode.NORMAL, DriveMode.SPORT, DriveMode.MOUNTAIN,
                DriveMode.HOLD, DriveMode.NORMAL]
    else:
        plan = [_MODE_BY_NAME[args.target]] * max(1, args.repeat)

    with CanInterface(args.channel, tx_gate=lambda: not args.dry_run) as can_if:
        if can_if.read_drive_mode(timeout=2.0) is None and not args.dry_run:
            sys.exit("no decodable 0x1F4 seen -- is the car in full READY?")

        ok = 0
        for target in plan:
            got = walk_to(can_if, target, walk_gap=args.walk_gap,
                          commit=args.commit, channel=args.channel,
                          dry_run=args.dry_run)
            if not args.dry_run:
                if got == target:
                    ok += 1
                else:
                    print(f"    MISS: wanted {target.value}, 0x1F4 said "
                          f"{got.value if got else 'None'}")
                time.sleep(2.0)  # let the cluster settle between walks

        if args.dry_run:
            print("\n[dry-run] no frames were sent.")
        else:
            print(f"\n{ok}/{len(plan)} walks landed on their target. "
                  f"can0: {can_state(args.channel)}")
            if ok == len(plan):
                print("  Injection reproduces the menu walk. Confirm each step "
                      "against the dash, then scan for DTCs.")
            else:
                print("  Tune: raise --frames (18, 20) if presses are missed; "
                      "adjust --walk-gap (1.0, 1.5) if the menu times out mid-walk "
                      "or presses merge. Check can0 for error frames.")

    print("\nScan for new DTCs before any driving; keep the daemon disarmed "
          "(voltdmf-ctl disarm) until the walk reliably reaches every mode.")


if __name__ == "__main__":
    main()
