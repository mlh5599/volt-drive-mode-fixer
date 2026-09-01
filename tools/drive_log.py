#!/usr/bin/env python3
"""Unattended drive session log: mode-hold + SOC/shift discovery in one run.

    THIS TRANSMITS on 0x1E1 ("ASCMSteeringButton", byte 4 bit 7 = pressed).

Start it stationary and in READY, then drive normally. It runs autonomously
-- no live SSH -- and produces two files to review at home:

  * ~/vdmf-drivelog-<ts>.log   -- this tool's own event log (shared clock)
  * candump-<ts>.log           -- a full raw `candump -l` capture of the
                                  whole session (mine offline)

Three things it gathers:

1. MODE HOLD (primary). For each mode in --sequence: walk to it through the
   production closed loop (voltdmf.modecycle + voltdmf.canio, same path as
   tools/set_mode.py), poll 0x1F4 byte 1 for the commit, then sample byte 1
   for a couple of minutes. The parked car selects every mode but MOUNTAIN
   / HOLD reverted toward NORMAL after the commit; this shows whether they
   hold while moving.

2. SHIFT / PRNDL (--shift-routine SECS, done parked before the drive). Logs
   a banner, then samples 0x135 / 0x1F5 and any frame that moved while you
   walk the shifter P->R->N->D->L->P with the brake pressed. Say each
   position out loud for your voice memo.

3. SOC. This dash has no % -- only EV range (miles) and a coarse battery
   bar. So there is no live SOC decode here; the raw capture is the whole
   point. Every --mark-every seconds the log prints a "SOC-MARK" line as a
   timeline anchor -- narrate your EV range + battery bars into a voice memo
   as you drive, and align it to the capture offline. Take the battery from
   near full to well down so the SOC frame has a clear trend to find.

NOT covered: ignition behavior. Key-off kills power to the Pi, so it can't
observe an ignition cycle -- that stays a separate manual check.

Mine the capture afterward with tools/mine_capture.py (monotonic-frame scan
for SOC, discrete-state scan for the shifter).

  DO NOT touch the Pi while driving. Start before you pull away (use
  --start-delay), then leave it alone. Stop only when parked (Ctrl-C, or let
  it finish). Estimate printed before it starts.

Add --lcd to mirror the shared t+<s> clock, the current phase, the
committed mode and the can0 state to a SparkFun serial 4x20 LCD, with the
SAY prompt flashed on each SOC-MARK -- so the voice-memo readout lines up
with the log without guesswork. Missing/unwired LCD just logs a warning.

Usage:
  # full session: 75 s parked shifter routine, 90 s to pull away, all modes
  ./drive_log.py --yes-unattended --shift-routine 75 --start-delay 90 --lcd

  # mode-hold only, SPORT vs NORMAL, 3 min each
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
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf import canio, lcdlock, modecycle, signals  # noqa: E402
from voltdmf.canio import CanInterface  # noqa: E402
from voltdmf.lcd import SerLcd  # noqa: E402
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


def latest_payload(bus, addr: int, timeout: float = 0.4) -> bytes | None:
    """Newest raw payload for one arbitration id, draining the RX backlog."""
    end = time.time() + timeout
    latest: bytes | None = None
    while True:
        wait = 0.0 if latest is not None else max(0.0, end - time.time())
        msg = bus.recv(timeout=wait)
        if msg is None:
            if latest is not None or time.time() >= end:
                return latest
            continue
        if msg.arbitration_id == addr:
            latest = bytes(msg.data)


def snapshot_ids(bus, seconds: float = 2.0) -> dict[int, bytes]:
    """One (latest) payload per arbitration id seen over ``seconds``."""
    end = time.time() + seconds
    seen: dict[int, bytes] = {}
    while time.time() < end:
        msg = bus.recv(timeout=max(0.0, end - time.time()))
        if msg is not None:
            seen[msg.arbitration_id] = bytes(msg.data)
    return seen


def start_capture(channel: str, outdir: str, log):
    """Spawn `candump -l` for the whole session. Returns (Popen|None, path|None)."""
    d = pathlib.Path(outdir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    before = set(d.glob("candump-*.log"))
    try:
        proc = subprocess.Popen(["candump", "-l", channel], cwd=d,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"  candump did not start ({exc}); continuing without raw capture")
        return None, None
    time.sleep(0.8)
    if proc.poll() is not None:
        log("  candump exited immediately; continuing without raw capture")
        return None, None
    new = sorted(set(d.glob("candump-*.log")) - before)
    path = new[-1] if new else None
    log(f"  raw capture: candump -l {channel} -> {path or (str(d) + '/candump-*.log')}")
    return proc, path


def stop_capture(proc, path, log) -> None:
    if proc is None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    sz = (path.stat().st_size / 1e6) if path and path.exists() else 0.0
    log(f"raw capture closed: {path} ({sz:.1f} MB)")


class Marks:
    """Periodic 'note your EV range + battery bars' timeline anchors.

    ``t0`` is the shared clock origin: SOC-MARK log lines, the LCD ``t+``
    readout, and your voice memo all reference it.
    """

    def __init__(self, every: float) -> None:
        self.every = every
        self.t0 = time.time()
        self.next = every
        self.dash: "Dash | None" = None

    def tick(self, log) -> None:
        if self.every <= 0:
            return
        dt = time.time() - self.t0
        if dt >= self.next:
            log(f"  SOC-MARK +{dt:6.1f}s  --> SAY: EV range (mi) + battery bars")
            if self.dash is not None:
                self.dash.say()
            while self.next <= dt:
                self.next += self.every


class Dash:
    """Optional 4x20 LCD mirror of the session clock. All calls are no-ops
    when no LCD is wired (``lcd is None``), so callers never have to guard.

    Row 0: shared ``t+<s>`` clock + wall time   Row 1: current phase
    Row 2: committed mode + can0 state          Row 3: SAY prompt when a
    SOC-MARK just fired (see :meth:`Marks.tick`).
    """

    def __init__(self, lcd, marks: "Marks") -> None:
        self.lcd = lcd
        self.marks = marks
        self.phase = "starting"
        self.mode = "?"
        self.state = "?"
        self.say_until = 0.0
        self._paints = 0
        if lcd is not None:
            self.paint()

    def set_phase(self, text: str) -> None:
        self.phase = text
        self.paint()

    def update(self, phase: str, mode: str, state: str) -> None:
        """Set phase + mode + state and repaint once."""
        self.phase = phase
        self.paint(mode=mode, state=state)

    def say(self, seconds: float = 8.0) -> None:
        self.say_until = time.time() + seconds
        self.paint()

    def paint(self, mode: str | None = None, state: str | None = None) -> None:
        if self.lcd is None:
            return
        if mode is not None:
            self.mode = mode
        if state is not None:
            self.state = state
        el = int(time.time() - self.marks.t0)
        self.lcd.line(0, f"t+{el:>5}s  {_dt.datetime.now():%H:%M:%S}")
        self.lcd.line(1, self.phase[:20])
        self.lcd.line(2, f"{self.mode:<9} {self.state[:10]}")
        self.lcd.line(3, ">> SAY: range + bars"
                      if time.time() < self.say_until else "")
        self._paints += 1
        if self._paints % 20 == 0:
            self.lcd.refresh()  # recover the panel if it browned out mid-drive


def shift_routine_phase(bus, secs: float, marks: "Marks", dash: "Dash",
                        log) -> None:
    """Parked phase: log 0x135 / 0x1F5 + movers while the driver walks PRNDL."""
    log("")
    log("========== SHIFT ROUTINE (car parked, foot on brake) ==========")
    log("Walk the shifter slowly, pausing ~8 s in each detent:")
    log("   P -> R -> N -> D -> L -> D -> N -> R -> P")
    log("Say each position out loud for the voice memo.")
    base = snapshot_ids(bus, 2.0)
    log(f"  baseline: {len(base)} ids seen  "
        f"135={base.get(0x135, b'').hex(' ') or 'n/a'}  "
        f"1F5={base.get(0x1F5, b'').hex(' ') or 'n/a'}")
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(2.0)
        marks.tick(log)
        dt = time.time() - t0
        dash.set_phase(f"SHIFT P-R-N-D-L {secs - dt:.0f}s")
        p135 = latest_payload(bus, 0x135, 0.3)
        p1f5 = latest_payload(bus, 0x1F5, 0.3)
        now = snapshot_ids(bus, 0.4)
        movers = sorted(i for i, v in now.items()
                        if i in base and v != base[i] and i not in (0x135, 0x1F5))
        log(f"  SHIFT +{dt:5.1f}s  "
            f"135={(p135.hex(' ') if p135 else '-'):<23} "
            f"1F5={(p1f5.hex(' ') if p1f5 else '-'):<17} "
            f"movers={[hex(i) for i in movers[:10]]}")
    log("===== shift routine done =====")


def make_logger(path: pathlib.Path):
    fh = open(path, "a", buffering=1)  # line-buffered

    def log(msg: str = "") -> None:
        line = f"{_dt.datetime.now():%H:%M:%S}  {msg}" if msg else ""
        print(line, flush=True)
        fh.write(line + "\n")

    return log, fh


def run_mode(controller, can_if, target, args, marks, dash, log) -> dict:
    """Walk to ``target``, then watch byte 1 hold. Returns a summary dict."""
    bus = can_if._bus  # throwaway tool: direct read for the tentative speed
    dash.set_phase(f"-> {target.value.upper()}  walking")
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
    dash.set_phase(f"-> {target.value.upper()}  commit?")
    t0 = time.time()
    last = None
    while time.time() - t0 < args.commit_verify:
        m = can_if.read_drive_mode(timeout=1.0)
        if m is not None and m != last:
            dt = time.time() - t0
            log(f"    +{dt:5.1f}s  commit-watch  byte1 -> {m.value}")
            dash.update(f"-> {target.value.upper()}  commit?",
                        m.value, can_state(args.channel))
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
        marks.tick(log)
        dt = time.time() - h0
        m = can_if.read_drive_mode(timeout=1.0)
        sp = read_speed(bus)
        cur = can_if.read_menu_cursor(timeout=0.4)
        tag = "" if m == target else "  <-- LEFT TARGET"
        held_s = args.hold - dt
        dash.update(f"{target.value.upper()} hold {held_s:.0f}s"
                    + ("" if m == target else " LEFT!"),
                    m.value if m else "?", can_state(args.channel))
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
    ap.add_argument("--shift-routine", type=float, default=0.0, metavar="SECS",
                    help="parked phase before the drive: SECS of 0x135/0x1F5 "
                         "logging while you walk the shifter P-R-N-D-L-P "
                         "(0 = skip; try 75)")
    ap.add_argument("--mark-every", type=float, default=90.0, metavar="SECS",
                    help="seconds between SOC-MARK timeline anchors in the log "
                         "(0 = off; default 90)")
    ap.add_argument("--capture", dest="capture", action="store_true",
                    default=True, help="run `candump -l` for the whole session "
                                       "(default on)")
    ap.add_argument("--no-capture", dest="capture", action="store_false",
                    help="do not spawn the raw candump capture")
    ap.add_argument("--capture-dir", default="~",
                    help="where candump -l writes its log (default ~)")
    ap.add_argument("--lcd", action="store_true",
                    help="mirror the session clock / phase / mode to a "
                         "SparkFun serial 4x20 LCD (see tools/lcd.py)")
    ap.add_argument("--lcd-port", default="/dev/serial0")
    ap.add_argument("--lcd-baud", type=int, default=9600)
    ap.add_argument("--lcd-backlight", type=int, default=60, metavar="PCT",
                    help="LCD backlight 0..100 (default 60; lower it if the "
                         "panel resets -- that is a 5 V sag, not software)")
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

    est = (args.shift_routine + args.start_delay
           + len(seq) * (args.commit_verify + args.hold) + 20)
    plan = " -> ".join(m.value for m in seq)
    print(f"plan: {plan}")
    print(f"estimate: ~{est/60:.1f} min "
          f"({args.shift_routine:.0f}s shift routine + {args.start_delay:.0f}s "
          f"pull-away + {len(seq)} modes x ~{(args.commit_verify + args.hold):.0f}s)")
    cap_desc = f"candump -l -> {args.capture_dir}" if args.capture else "OFF"
    mark_desc = (f"every {args.mark_every:.0f}s" if args.mark_every > 0 else "off")
    print(f"capture: {cap_desc}   SOC-MARK: {mark_desc}")

    modecycle.WALK_GAP_S = args.walk_gap

    if args.dry_run:
        print("\n[DRY RUN] no bus, no TX.")
        if args.shift_routine > 0:
            print(f"  shift routine: {args.shift_routine:.0f}s parked, logging "
                  f"0x135/0x1F5 + movers while you walk P-R-N-D-L-P")
        else:
            print("  shift routine: skipped (--shift-routine 0)")
        print(f"  raw capture:   {cap_desc}")
        print(f"  SOC-MARK:      {mark_desc}")
        print(f"  LCD mirror:    {args.lcd_port + ' @ ' + str(args.lcd_baud) if args.lcd else 'OFF'}")
        print("  tap counts from a fake NORMAL source:")
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

    marks = Marks(args.mark_every)
    lcd = None
    if args.lcd:
        lcdlock.claim("drive_log.py")  # daemon watch screen yields the panel
        try:
            lcd = SerLcd(args.lcd_port, args.lcd_baud,
                         backlight=args.lcd_backlight).open()
            log(f"LCD mirror on {args.lcd_port} @ {args.lcd_baud} "
                f"(backlight {args.lcd_backlight}%)")
        except OSError as exc:
            log(f"LCD open failed ({exc}); continuing without the mirror")
    dash = Dash(lcd, marks)
    marks.dash = dash
    dash.set_phase("bus up; waiting")

    cap_proc = cap_path = None
    if args.capture:
        cap_proc, cap_path = start_capture(args.channel, args.capture_dir, log)

    summaries: list[dict] = []
    try:
        with CanInterface(args.channel) as can_if:
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

            if args.shift_routine > 0:
                shift_routine_phase(can_if._bus, args.shift_routine, marks,
                                    dash, log)

            end = time.time() + args.start_delay
            log(f"start-delay {args.start_delay:.0f}s -- pull away now.")
            while time.time() < end:
                dash.set_phase(f"PULL AWAY  {end - time.time():.0f}s")
                time.sleep(min(15.0, max(0.0, end - time.time())))
                marks.tick(log)
                if time.time() < end:
                    log(f"  ...{end - time.time():.0f}s to first walk")

            for i, m in enumerate(seq, 1):
                log("")
                log(f"===== {i}/{len(seq)}  ->  {m.value.upper()} =====")
                if args.bounce_between and i > 1:
                    bounce(args.channel, log)
                s = run_mode(controller, can_if, m, args, marks, dash, log)
                summaries.append(s)
                if s["error"] and "can0 went" in s["error"]:
                    break
    except KeyboardInterrupt:
        log("\ninterrupted -- stopping (hope you're parked).")
        dash.set_phase("interrupted")
    finally:
        stop_capture(cap_proc, cap_path, log)
        if lcd is not None:
            n_held = sum(1 for s in summaries if s.get("held_full"))
            dash.update("DONE -- review log",
                        f"{n_held}/{len(summaries)} held", "")
            lcd.close()
        if args.lcd:
            lcdlock.release()  # hand the panel back to the daemon watch screen
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
        if cap_path:
            log(f"raw capture:  {cap_path}   (mine with tools/mine_capture.py)")
        fh.close()


if __name__ == "__main__":
    main()
