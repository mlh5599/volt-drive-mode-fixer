#!/usr/bin/env python3
"""Calibrate the 0x1E1 press burst and verify the menu-walk model on the car.

    THIS TRANSMITS on 0x1E1 ("ASCMSteeringButton", byte 4 bit 7 = pressed).
    It sends nothing else. Run with the daemon DISARMED (``voltdmf-ctl
    disarm``) so this is the only thing injecting.

This is the tool that produced the two numbers `voltdmf/canio.py` and
`voltdmf/modecycle.py` now depend on, and it exists so they can be re-measured
rather than re-guessed. It answers two questions:

  ``burst``  How many menu rows does an N-frame press actually move?
             The cluster key-REPEATS a held button, so a press that is too
             long walks several rows on what the caller thinks is one tap.
             Measured 2026-09-03, 3 reps each, cursor steps per press:
                 1 frame -> 1 step    2 frames -> 1 step    4 frames -> 1 step
                 3 frames -> 1-2 steps (marginal)           8 frames -> 2 steps
             PRESS_TRACK_FRAMES was 16 (~5-6 rows per "tap") until this run;
             that single value was the dominant mode-walk reliability bug.

  ``model``  Does ``presses = index(target) + 1`` hold from any start mode?
             It does: the menu ALWAYS opens with the cursor on NORMAL,
             whatever mode is latched, so a wake press plus k advances lands
             on ``MODE_CYCLE_ORDER[k]`` regardless of where you started
             (5/5 rows, 2026-09-03). The earlier "only correct from a cold
             NORMAL menu" premise -- the whole justification for the
             closed-loop walk -- was false.

Decoding comes from ``voltdmf.signals`` so this tool and the library cannot
drift; the press echo is a local copy of ``canio.send_mode_button_press``
(same reason ``tools/inject_test.py`` keeps one -- the shipping method drives
``self._bus.recv`` directly and cannot share a bus with a reader thread).

Car MUST be stationary and in full READY: Park, parking brake set. Keep runs
short: sustained sub-2 s injection drives the CAN error counter up and the
cluster stops committing until you bounce can0. ``burst`` with the default
sweep is ~2 minutes; ``model`` is ~1 minute.

Usage:
  ./press_calibrate.py burst --yes-stationary
  ./press_calibrate.py burst --yes-stationary --frames 1 2 4 --reps 5
  ./press_calibrate.py model --yes-stationary
  ./press_calibrate.py model --yes-stationary --steps 1 2 3 0
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import can  # noqa: E402

from voltdmf import canio, signals  # noqa: E402
from voltdmf.signals import DriveMode  # noqa: E402

BTN = canio.MODE_BUTTON_ADDR
STATUS = canio.MODE_STATUS_ADDR
CYCLE = signals.MODE_CYCLE_ORDER

#: Silence needed for the menu to close and the cursor to commit to byte 1.
COMMIT_WAIT_S = 4.0
#: Give up waiting for the module's next live 0x1E1 to echo.
ECHO_DEADLINE_S = 2.0


def _label(mode: DriveMode | None) -> str:
    return mode.value[0].upper() if mode is not None else "-"


class _Watch(threading.Thread):
    """Reader thread: tracks the live 0x1E1 to echo, and the 0x1F4 timeline."""

    daemon = True

    def __init__(self, bus: can.BusABC) -> None:
        super().__init__()
        self._bus = bus
        self.stop = False
        self.last_btn: bytes | None = None
        self.mode: DriveMode | None = None
        self.cursor: DriveMode | None = None
        self.path: list[DriveMode | None] = []

    def run(self) -> None:
        while not self.stop:
            msg = self._bus.recv(0.2)
            if msg is None:
                continue
            data = bytes(msg.data)
            if msg.arbitration_id == BTN and len(data) >= 7:
                self.last_btn = data
            elif msg.arbitration_id == STATUS:
                mode = signals.decode_drive_mode(data)
                if mode is not None:
                    self.mode = mode
                self.cursor = signals.decode_menu_cursor(data)
                # Record only transitions -- 0x1F4 runs at ~40 Hz.
                if not self.path or self.path[-1] is not self.cursor:
                    self.path.append(self.cursor)


def _press(bus: can.BusABC, watch: _Watch, frames: int) -> int:
    """One tracking-echo press: mirror the module's live 0x1E1 with bit 7 set.

    Local copy of ``canio.send_mode_button_press`` with the frame count as a
    parameter -- that is the whole point of ``burst``.
    """
    sent = 0
    seen = watch.last_btn
    deadline = time.monotonic() + ECHO_DEADLINE_S
    while sent < frames and time.monotonic() < deadline:
        base = watch.last_btn
        if base is None or base is seen:
            time.sleep(0.002)       # no new module frame yet
            continue
        seen = base
        buf = bytearray(base)
        buf[4] |= canio.PRESS_BYTE4
        try:
            bus.send(can.Message(arbitration_id=BTN, data=bytes(buf),
                                 is_extended_id=False), timeout=0.05)
            sent += 1
        except can.CanError:
            time.sleep(0.005)
    return sent


def _steps(path: list[DriveMode | None]) -> int:
    """Cursor rows moved: transitions to a real cursor, ignoring menu-close."""
    return sum(1 for i in range(1, len(path))
               if path[i] is not path[i - 1] and path[i] is not None)


def run_burst(bus: can.BusABC, watch: _Watch, frames: list[int], reps: int) -> None:
    print(f"{'N':>3} {'sent':>5} {'steps':>6}  {'cursor path':<28} mode")
    for n in frames:
        for _ in range(reps):
            time.sleep(COMMIT_WAIT_S)       # let the menu close and settle
            watch.path.clear()
            before = watch.mode
            sent = _press(bus, watch, n)
            time.sleep(2.5)                 # let the cursor settle
            path = "".join(_label(c) for c in watch.path)
            print(f"{n:>3} {sent:>5} {_steps(watch.path):>6}  {path[:28]:<28} "
                  f"{_label(before)}->{_label(watch.mode)}")


def run_model(bus: can.BusABC, watch: _Watch, steps: list[int], gap: float,
              frames: int) -> bool:
    """Wake press + k advances should land on CYCLE[k] from ANY start mode."""
    print(f"{'k':>2} {'start':>5} {'want':>5} {'got':>4} {'ok':>5}   cursor path")
    ok_all = True
    for k in steps:
        time.sleep(COMMIT_WAIT_S)
        start = watch.mode
        if start is None:
            print("no mode decoded off 0x1F4 -- is the car in READY?")
            return False
        watch.path.clear()
        _press(bus, watch, frames)          # wake press: opens the menu on NORMAL
        for _ in range(k):
            time.sleep(gap)
            _press(bus, watch, frames)      # each advances one row
        time.sleep(COMMIT_WAIT_S)
        want = CYCLE[k % len(CYCLE)]        # NOT relative to start -- see docstring
        got = watch.mode
        ok = got is want
        ok_all &= ok
        path = "".join(_label(c) for c in watch.path)
        print(f"{k:>2} {_label(start):>5} {_label(want):>5} {_label(got):>4} "
              f"{'OK' if ok else 'FAIL':>5}   {path[:40]}")
    print("\nMODEL CONFIRMED" if ok_all else
          "\nMODEL WRONG -- the cursor paths above say what it does instead")
    return ok_all


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("burst", "model"))
    ap.add_argument("--yes-stationary", action="store_true",
                    help="required: you confirm the car is stationary, in Park, "
                         "and the daemon is disarmed")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--frames", type=int, nargs="+", default=[1, 2, 3, 4, 8],
                    help="burst: frame counts to sweep (default 1 2 3 4 8)")
    ap.add_argument("--reps", type=int, default=3,
                    help="burst: repeats per frame count (default 3)")
    ap.add_argument("--steps", type=int, nargs="+", default=[1, 2, 3, 0],
                    help="model: advance counts to test after the wake press "
                         "(default 1 2 3 0)")
    ap.add_argument("--gap", type=float, default=1.4,
                    help="model: seconds between presses (default 1.4 = "
                         "modecycle.WALK_GAP_S)")
    ap.add_argument("--press-frames", type=int, default=canio.PRESS_TRACK_FRAMES,
                    help="model: frames per press (default "
                         f"canio.PRESS_TRACK_FRAMES = {canio.PRESS_TRACK_FRAMES})")
    args = ap.parse_args()

    if not args.yes_stationary:
        print("refusing to transmit without --yes-stationary", file=sys.stderr)
        return 2

    bus = can.Bus(interface="socketcan", channel=args.channel)
    watch = _Watch(bus)
    watch.start()
    try:
        time.sleep(1.5)                     # prime mode/cursor/last_btn
        print(f"start mode={_label(watch.mode)} cursor={_label(watch.cursor)}")
        if args.mode == "burst":
            run_burst(bus, watch, args.frames, args.reps)
            return 0
        return 0 if run_model(bus, watch, args.steps, args.gap,
                              args.press_frames) else 1
    finally:
        watch.stop = True
        watch.join(timeout=1.0)
        bus.shutdown()


if __name__ == "__main__":
    sys.exit(main())
