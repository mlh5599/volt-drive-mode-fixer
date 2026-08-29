#!/usr/bin/env python3
"""Unattended drive-log: walk each drive mode and record whether it HOLDS.

    THIS TRANSMITS on 0x1E1 ("ASCMSteeringButton", byte 4 bit 7 = pressed).

Purpose: the parked car proved the closed loop can *select* every mode, but
MOUNTAIN and HOLD reverted toward NORMAL a minute or so after the commit
(battery-management modes with engagement conditions). Whether they hold
needs the car moving -- and SSH-ing in from a moving car is not practical.

So this runs autonomously. Start it stationary and in READY, then drive
normally. For each mode in the sequence it:

  1. walks to the mode through the production closed loop
     (voltdmf.modecycle.ModeCycleController + voltdmf.canio, same path as
     tools/set_mode.py),
  2. polls 0x1F4 byte 1 for the commit,
  3. then samples byte 1 (and a tentative speed off 0x3E9) every few
     seconds for a couple of minutes and logs every change.

A parked-only revert vs a genuine moving-car hold is then plain in the log.
Review it at home over wifi; no live SSH during the drive.

  DO NOT touch the Pi while driving. Start the run before you pull away
  (use --start-delay to give yourself time), then leave it alone. Stop it
  only when parked again (Ctrl-C, or just let it finish).

Typical drive is ~ len(sequence) * (--commit-verify + --hold) + --start-delay
seconds -- the tool prints the estimate before it starts.

Usage:
  # start parked, 90 s to pull away, ~12 min of logging, default sequence
  ./drive_log.py --yes-unattended --start-delay 90

  # just SPORT vs NORMAL, 3 min hold each
  ./drive_log.py --yes-unattended --sequence sport,normal --hold 180

  # sanity-check the plan, no CAN hardware, no TX
  ./drive_log.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf import canio, modecycle, signals  # noqa: E402
from voltdmf.canio import CanInterface  # noqa: E402
from voltdmf.modecycle import (  # noqa: E402
    ModeCycleController,
    ModeSwitchFailed,
    ModeUnknownError,
)
from voltdmf.signals import MODE_CYCLE_ORDER, SIGNAL_IDS, DriveMode  # noqa: E402

_MODE_BY_NAME = {m.value: m for m in MODE_CYCLE_ORDER}
_SPEED_ADDR = SIGNAL_IDS["speed"].addr  # 0x3E9 -- UNCONFIRMED Gen-1, may be absent


def can_state(channel: str) -> str:
    """'ERROR-ACTIVE' / 'BUS-OFF' / 'STOPPED' / '?' for the link."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def bounce(channel: str, log) -> None:
    for cmd in (
        ["sudo", "ip", "link", "set", channel, "down"],
        ["sudo", "ip", "link", "set", channel, "up", "type", "can",
         "bitrate", "500000", "restart-ms", "100"],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True)
        log(f"  $ {' '.join(cmd)} -> rc={r.returncode} "
            f"{r.stderr.strip() or 'ok'}")
    for _ in range(20):
        if can_state(channel) == "ERROR-ACTIVE":
            return
        time.sleep(0.5)


def read_speed(bus, timeout: float = 0.4) -> float | None:
    """Tentative mph off 0x3E9 (UNCONFIRMED Gen-1 decode). None if absent."""
    end = time.time() + timeout
    latest: float | None = None
    while True:
        wait = 0.0 if latest is not None else max(0.0, end - time.time())
        msg = bus.recv(timeout=wait)
        if msg is None:
            if latest is not None or time.time() >= end:
                return latest
            continue
        if msg.arbitration_id == _SPEED_ADDR:
            v = signals.decode_speed_mph(bytes(msg.data))
            if v is not None:
                latest = v


def _spd(v: float | None) -> str:
    return "n/a" if v is None else f"{v:4.0f}?"  # '?' = unconfirmed decode


def make_logger(path: pathlib.Path):
    fh = open(path, "a", buffering=1)  # line-buffered

    def log(msg: str = "") -> None:
        line = f"{_dt.datetime.now():%H:%M:%S}  {msg}" if msg else ""
        print(line, flush=True)
        fh.write(line + "\n")

    return log, fh


def run_mode(controller, can_if, target, args, log) -> dict:
    """Walk to ``target``, then watch byte 1 hold. Returns a summary dict."""
    bus = can_if._bus  # throwaway tool: direct read for the tentative speed
    pre = can_if.read_drive_mode(timeout=2.0)
    log(f"--- {target.value.upper()} ---  (byte1 now: "
        f"{pre.value if pre else 'None'}, spd~{_spd(read_speed(bus))})")

    res = {"target": target.value, "taps": None, "committed": None,
           "commit_s": None, "held_full": None, "first_leave_s": None,
           "first_leave_to": None, "leave_speed": None, "error": None}

    try:
        res["taps"] = controller.switch_to(target, force=True)
        log(f"    walked in {res['taps']} tap(s); watching for the commit...")
    except ModeSwitchFailed as exc:
        res["error"] = f"cursor never reached target: {exc}"
        log(f"    FAIL: {res['error']}")
        return res
    except ModeUnknownError as exc:
        res["error"] = f"mode unreadable: {exc}"
        log(f"    ABORT-WORTHY: {res['error']}")
        return res

    # -- commit watch ---------------------------------------------------
    t0 = time.time()
    last = None
    while time.time() - t0 < args.commit_verify:
        m = can_if.read_drive_mode(timeout=1.0)
        if m is not None and m != last:
            dt = time.time() - t0
            log(f"    +{dt:5.1f}s  commit-watch  byte1 -> {m.value}")
            last = m
            if m == target:
                res["committed"] = m.value
                res["commit_s"] = round(dt, 1)
                break
    if res["committed"] is None:
        res["committed"] = last.value if last else None
        log(f"    commit not observed within {args.commit_verify:.0f}s "
            f"(byte1 = {res['committed']})")

    # -- hold watch ---------------------------------------------------
    log(f"    holding-watch for {args.hold:.0f}s, sampling every "
        f"{args.sample:.0f}s ...")
    h0 = time.time()
    prev = target
    left = False
    while time.time() - h0 < args.hold:
        time.sleep(args.sample)
        dt = time.time() - h0
        m = can_if.read_drive_mode(timeout=1.0)
        sp = read_speed(bus)
        cur = can_if.read_menu_cursor(timeout=0.4)
        tag = "" if m == target else "  <-- LEFT TARGET"
        if m != prev:
            log(f"    +{dt:5.1f}s  hold-watch  byte1={m.value if m else 'None'} "
                f"cursor={cur.value if cur else '-'} spd~{_spd(sp)}{tag}")
            prev = m
        if m != target and not left:
            left = True
            res["first_leave_s"] = round(dt, 1)
            res["first_leave_to"] = m.value if m else None
            res["leave_speed"] = None if sp is None else round(sp)
        st = can_state(args.channel)
        if st in ("BUS-OFF", "STOPPED", "?"):
            res["error"] = f"can0 went {st} during hold-watch"
            log(f"    ABORT-WORTHY: {res['error']}")
            return res
    res["held_full"] = not left
    log(f"    {target.value.upper()} held full {args.hold:.0f}s: "
        f"{res['held_full']}"
        + ("" if res["held_full"]
           else f" (left after {res['first_leave_s']}s to "
                f"{res['first_leave_to']}, spd~{res['leave_speed']})"))
    return res


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes-unattended", action="store_true",
                    help="required for a live run: you are stationary now and "
                         "will let this run autonomously while you drive")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--sequence", default="sport,mountain,hold,normal",
                    help="comma-separated modes to walk, in order "
                         "(default: sport,mountain,hold,normal)")
    ap.add_argument("--start-delay", type=float, default=60.0,
                    help="seconds to wait after the bus is up before the first "
                         "walk -- pull away during this (default 60)")
    ap.add_argument("--commit-verify", type=float, default=12.0,
                    help="seconds to poll byte 1 for the commit (default 12)")
    ap.add_argument("--hold", type=float, default=150.0,
                    help="seconds to watch byte 1 after each commit (default 150)")
    ap.add_argument("--sample", type=float, default=3.0,
                    help="hold-watch poll interval in seconds (default 3)")
    ap.add_argument("--walk-gap", type=float, default=modecycle.WALK_GAP_S,
                    help=f"seconds between taps (default {modecycle.WALK_GAP_S}; "
                         f"~1.2..2.5)")
    ap.add_argument("--no-bounce", action="store_true",
                    help="skip the can0 down/up at the start")
    ap.add_argument("--bounce-between", action="store_true",
                    help="also bounce can0 between modes (only if commits stop "
                         "landing -- normally not needed)")
    ap.add_argument("--logfile", default=None,
                    help="log path (default ~/vdmf-drivelog-<timestamp>.log)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no CAN hardware, no TX: print the plan and the "
                         "per-mode tap counts a fake source would give")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("can").setLevel(logging.WARNING)

    try:
        seq = [_MODE_BY_NAME[s.strip()] for s in args.sequence.split(",")
               if s.strip()]
    except KeyError as exc:
        sys.exit(f"bad --sequence entry {exc}; choose from "
                 f"{sorted(_MODE_BY_NAME)}")
    if not seq:
        sys.exit("--sequence is empty")
    if not 1.0 <= args.walk_gap <= 2.6:
        sys.exit(f"--walk-gap {args.walk_gap}s out of range (~1.2..2.5)")

    est = args.start_delay + len(seq) * (args.commit_verify + args.hold) + 20
    plan = " -> ".join(m.value for m in seq)
    print(f"plan: {plan}")
    print(f"estimate: ~{est/60:.1f} min "
          f"({args.start_delay:.0f}s pull-away + {len(seq)} modes x "
          f"~{(args.commit_verify + args.hold):.0f}s)")

    modecycle.WALK_GAP_S = args.walk_gap

    if args.dry_run:
        print("\n[DRY RUN] no bus, no TX. Tap counts from a fake NORMAL "
              "source:")
        for m in seq:
            gaps: list[float] = []

            class _FP:
                n = 0

                def send_mode_button_press(self) -> None:
                    _FP.n += 1

            ctl = ModeCycleController(_FP(), lambda: DriveMode.NORMAL,
                                      sleep=gaps.append)
            sent = ctl.switch_to(m, force=True)
            print(f"  {m.value:9s}  {sent} tap(s), {len(gaps)} gap(s)")
        return

    if not args.yes_unattended:
        sys.exit("refusing to transmit without --yes-unattended "
                 "(or pass --dry-run)")

    logpath = pathlib.Path(
        args.logfile or
        pathlib.Path.home() /
        f"vdmf-drivelog-{_dt.datetime.now():%Y%m%d-%H%M%S}.log")
    log, fh = make_logger(logpath)
    log(f"drive_log start  seq={plan}  logfile={logpath}")
    log(f"walk-gap={args.walk_gap}s  commit-verify={args.commit_verify}s  "
        f"hold={args.hold}s  sample={args.sample}s")

    if not args.no_bounce:
        log("bouncing can0 (car should still be parked here)...")
        bounce(args.channel, log)
    st = can_state(args.channel)
    log(f"can0 state: {st}")
    if st != "ERROR-ACTIVE":
        log("can0 not ERROR-ACTIVE -- aborting. Bring it up cleanly and retry.")
        fh.close()
        sys.exit(1)

    summaries: list[dict] = []
    try:
        with CanInterface(args.channel, dry_run=False) as can_if:
            source = lambda: can_if.read_drive_mode(timeout=2.0)  # noqa: E731
            cursor = lambda: can_if.read_menu_cursor(timeout=0.6)  # noqa: E731
            controller = ModeCycleController(
                can_if, source, menu_cursor_source=cursor)

            rest = source()
            log(f"resting mode: {rest.value if rest else 'None'} "
                f"(spd~{_spd(read_speed(can_if._bus))})")
            if rest is None:
                log("no decodable 0x1F4 -- car not in READY? aborting.")
                fh.close()
                sys.exit(1)

            end = time.time() + args.start_delay
            log(f"start-delay {args.start_delay:.0f}s -- pull away now.")
            while time.time() < end:
                time.sleep(min(30.0, max(0.0, end - time.time())))
                if time.time() < end:
                    log(f"  ...{end - time.time():.0f}s to first walk")

            for i, m in enumerate(seq, 1):
                log("")
                log(f"===== {i}/{len(seq)}  ->  {m.value.upper()} =====")
                if args.bounce_between and i > 1:
                    bounce(args.channel, log)
                s = run_mode(controller, can_if, m, args, log)
                summaries.append(s)
                if s["error"] and "can0 went" in s["error"]:
                    break
    except KeyboardInterrupt:
        log("\ninterrupted -- stopping (hope you're parked).")
    finally:
        log("")
        log("================ SUMMARY ================")
        for s in summaries:
            if s["error"]:
                log(f"  {s['target']:9s}  ERROR: {s['error']}")
                continue
            held = ("HELD" if s["held_full"]
                    else f"left @ {s['first_leave_s']}s -> "
                         f"{s['first_leave_to']} (spd~{s['leave_speed']})")
            log(f"  {s['target']:9s}  walk={s['taps']}tap  "
                f"commit={s['committed']}@{s['commit_s']}s  hold: {held}")
        log("========================================")
        log(f"log written to {logpath}")
        fh.close()


if __name__ == "__main__":
    main()
