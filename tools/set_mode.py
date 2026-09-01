#!/usr/bin/env python3
"""Drive ONE mode switch through the production path and watch the result.

    THIS TRANSMITS on 0x1E1 ("ASCMSteeringButton", byte 4 bit 7 = pressed).

Unlike ``tools/inject_test.py`` (a lower-level walk/timing sweep with its own
copy of the walk logic), this tool exercises the real shipping code:

    voltdmf.canio.CanInterface            -- the one TX path (tracking echo)
    voltdmf.modecycle.ModeCycleController -- the menu-walk model under test

It reads the current mode from 0x1F4 byte 1, then calls
``controller.switch_to(target)``. With a ``menu_cursor_source`` wired (this
tool does) that runs the **closed loop**: tap, read the live menu cursor
(0x1F4 byte 4), stop the instant it is on the target, let it commit. Then it
polls 0x1F4 byte 1 and prints what the cluster settled on. It does NOT route
through ``SafetyGate`` -- that gate's preconditions/cooldown are a daemon-loop
concern; here ``--yes-stationary`` plus the can0 health check are the safety
story.

MENU MODEL (owner-observed + on-car injection sweeps 2026-08-29):
  * menu closed -> any press opens it on NORMAL, whatever mode is latched
  * menu open   -> each press steps NORMAL->SPORT->MOUNTAIN->HOLD->..., ONE
                   step per press when presses are >= ~1.2 s apart (closer
                   coalesces into extra steps -- the old overshoot bug)
  * ~3 s idle   -> the cursor commits; 0x1F4 byte 1 updates then
  * 0x1F4 byte 4 = the live cursor (00 N / 80 S / 40 M / 20 H); byte 1 = the
                   committed mode (00 N / 80 S / 20 M / 08 H)

Car MUST be stationary and in full READY: Park, parking brake set. Capture the
bus in another shell (``candump -L can0 > /tmp/x.log``) and scan for DTCs
afterwards. On a PARKED car 0x1F4 byte 1 lags a commit by ~7-9 s and then
tends to revert toward NORMAL -- a "needs a drive" question, not an injection
failure. The closed loop confirms the *cursor* reached the target; keeping
the mode is a separate, moving-car question.

Keep runs short: sustained sub-2 s injection drives the CAN error counter up
and the cluster stops committing until you bounce can0 and let it settle.

Usage:
  ./set_mode.py --yes-stationary --target sport
  ./set_mode.py --yes-stationary --target hold --walk-gap 1.6
  ./set_mode.py --yes-stationary --target normal --force        # re-assert
  ./set_mode.py --target sport --dry-run                        # no TX, prints the plan
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf import canio, modecycle  # noqa: E402
from voltdmf.canio import CanInterface  # noqa: E402
from voltdmf.modecycle import (  # noqa: E402
    ModeCycleController,
    ModeSwitchFailed,
    ModeUnknownError,
)
from voltdmf.signals import MODE_CYCLE_ORDER, DriveMode  # noqa: E402

_MODE_BY_NAME = {m.value: m for m in MODE_CYCLE_ORDER}


def can_state(channel: str) -> str:
    """'ERROR-ACTIVE' / 'BUS-OFF' / 'STOPPED' / '?' for the link."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def poll_mode(can_if: CanInterface, target: DriveMode, seconds: float,
              ) -> tuple[DriveMode | None, list[tuple[float, DriveMode]]]:
    """Poll 0x1F4 for ``seconds``; return (last value, timeline of changes).

    Stops early once ``target`` is read. The timeline is every value change
    with its elapsed time, so a lag-then-revert shows up plainly.
    """
    start = time.time()
    last: DriveMode | None = None
    timeline: list[tuple[float, DriveMode]] = []
    while time.time() - start < seconds:
        m = can_if.read_drive_mode(timeout=1.0)
        if m is not None and m != last:
            timeline.append((time.time() - start, m))
            last = m
            if m == target:
                break
    return last, timeline


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, choices=sorted(_MODE_BY_NAME),
                    help="drive mode to walk to")
    ap.add_argument("--yes-stationary", action="store_true",
                    help="required for a live run: you confirm the car is "
                         "stationary and in Park")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--walk-gap", type=float, default=modecycle.WALK_GAP_S,
                    help=f"seconds between taps in the walk (default "
                         f"{modecycle.WALK_GAP_S}; >= ~1.2 or taps coalesce "
                         f"into extra steps, <= ~2.5 to stay in the menu window)")
    ap.add_argument("--frames", type=int, default=None,
                    help="bit-7-set frames per tap (default "
                         f"canio.PRESS_TRACK_FRAMES = {canio.PRESS_TRACK_FRAMES}; "
                         f"the 2026-08-29 sweep found 1..16 all fine -- spacing, "
                         f"not frame count, drove the overshoot)")
    ap.add_argument("--verify", type=float, default=12.0,
                    help="seconds to poll 0x1F4 byte 1 after the walk (it lags "
                         "a commit ~7-9 s on this car, then may revert parked)")
    ap.add_argument("--force", action="store_true",
                    help="walk even if 0x1F4 already reads the target "
                         "(parked readback is unreliable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no CAN hardware: run switch_to() against a fake "
                         "presser and print the walk it would do")
    ap.add_argument("--assume-current", choices=sorted(_MODE_BY_NAME),
                    default="normal",
                    help="--dry-run only: mode the fake status source reports "
                         "(default normal)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("can").setLevel(logging.WARNING)  # keep voltdmf.canio TX logs, drop socket noise
    target = _MODE_BY_NAME[args.target]
    presses = MODE_CYCLE_ORDER.index(target) + 1

    if not 1.0 <= args.walk_gap <= 2.6:
        sys.exit(f"--walk-gap {args.walk_gap}s is out of range; taps closer "
                 f"than ~1.2 s coalesce into extra steps and beyond ~2.5 s the "
                 f"menu closes mid-walk -- use ~1.2..2.5 s")
    if not args.dry_run and not args.yes_stationary:
        sys.exit("refusing to transmit without --yes-stationary "
                 "(or pass --dry-run)")

    modecycle.WALK_GAP_S = args.walk_gap
    if args.frames is not None:
        canio.PRESS_TRACK_FRAMES = max(1, args.frames)
        canio.PRESS_BURST_FRAMES = canio.SEND_CLUSTER_SIZE = canio.PRESS_TRACK_FRAMES

    print(f"target {target.value!r}: {presses} press(es), "
          f"walk-gap {args.walk_gap:.2f}s, {canio.PRESS_TRACK_FRAMES} frames/press"
          f"{'  [DRY RUN]' if args.dry_run else ''}")

    if args.dry_run:
        assumed = _MODE_BY_NAME[args.assume_current]
        gaps: list[float] = []
        hits: list[int] = []

        class _FakePresser:
            def send_mode_button_press(self) -> None:
                hits.append(1)
                print(f"  press {len(hits)}")

        ctl = ModeCycleController(_FakePresser(), lambda: assumed,
                                  sleep=gaps.append)
        print(f"before: status source reports {assumed.value}")
        try:
            sent = ctl.switch_to(target, force=args.force)
        except ModeUnknownError as exc:
            sys.exit(f"switch_to refused: {exc}")
        print(f"[dry-run] switch_to() sent {sent} press(es); "
              f"{len(gaps)} walk-gap sleep(s) of {sorted(set(gaps)) or '[]'}s. "
              f"No frames on the wire.")
        return

    st = can_state(args.channel)
    if st != "ERROR-ACTIVE":
        sys.exit(f"can0 state is {st!r}, not ERROR-ACTIVE. Bring the bus up "
                 f"cleanly first:\n  sudo ip link set {args.channel} down\n"
                 f"  sudo ip link set {args.channel} up type can bitrate 500000 "
                 f"restart-ms 100")

    with CanInterface(args.channel) as can_if:
        source = lambda: can_if.read_drive_mode(timeout=2.0)  # noqa: E731
        cursor = lambda: can_if.read_menu_cursor(timeout=0.6)  # noqa: E731
        controller = ModeCycleController(can_if, source, menu_cursor_source=cursor)

        before = source()
        if before is None:
            sys.exit("no decodable 0x1F4 -- is the car in full READY?")
        print(f"before: 0x1F4 reads {before.value}"
              f"   | can0 {can_state(args.channel)}")

        try:
            sent = controller.switch_to(target, force=args.force)
        except ModeUnknownError as exc:
            sys.exit(f"switch_to refused: {exc}")
        except ModeSwitchFailed as exc:
            print(f"\nclosed loop gave up: {exc}")
            print("  the live menu cursor (0x1F4 byte 4) never landed on the "
                  "target. Bounce can0 and let 0x1F4 rest at NORMAL, then "
                  "retry; keep runs short (the cluster rate-limits).")
            sys.exit(1)

        if sent == 0:
            print(f"already in {target.value} (0x1F4) and --force not set; "
                  f"nothing sent.")
            return

        print(f"closed loop reached the {target.value} cursor in {sent} tap(s); "
              f"polling 0x1F4 byte 1 up to {args.verify:.0f}s for the commit...",
              flush=True)
        final, timeline = poll_mode(can_if, target, args.verify)
        for dt, mode in timeline:
            print(f"  +{dt:4.1f}s  0x1F4 -> {mode.value}")
        if not timeline:
            print("  (0x1F4 never produced a decodable change)")
        print(f"final: {final.value if final else 'None'}   "
              f"| can0 {can_state(args.channel)}   (confirm against the dash)")

    if final == target:
        print(f"\nOK: walk reached {target.value}. Confirm on the dash, then "
              f"scan for new DTCs.")
        sys.exit(0)
    print(f"\nMISS: wanted {target.value}, 0x1F4 byte 1 settled on "
          f"{final.value if final else 'None'}.\n"
          f"  - the closed loop DID land the cursor on {target.value} (or it "
          f"would have raised). On a PARKED car byte 1 commonly latches then "
          f"reverts toward NORMAL in ~7-9 s -- check the timeline and the dash; "
          f"a real confirmation wants the car moving.\n"
          f"  - if byte 1 never moved at all: bounce can0, let 0x1F4 rest at "
          f"NORMAL, keep the run short (sustained <2 s injection makes the "
          f"cluster stop committing).")
    sys.exit(1)


if __name__ == "__main__":
    main()
