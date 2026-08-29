#!/usr/bin/env python3
"""Unattended SOC-discovery drive log. PASSIVE -- never transmits.

The Gen 1 dash shows no state-of-charge percentage, only EV range (miles)
and a coarse battery bar, so SOC cannot be read off one frame directly.
This tool does the only thing that works: capture the whole bus for a long
drive that takes the pack from near-full to well down, drop timestamped
anchors you can narrate against, then find the monotonically-falling field
offline with ``tools/mine_capture.py --monotonic``.

What it does, all read-only:

  * spawns ``candump -l`` for the entire session -> ``candump-<ts>.log``
    (this is the actual data; everything else just helps you align it)
  * writes its own event log -> ``~/vdmf-soclog-<ts>.log`` on a shared
    ``t+<seconds>`` clock
  * every ``--mark-every`` s logs a ``SOC-MARK`` line -- say your EV range +
    battery-bar count out loud into a voice memo when you see it / hear the
    LCD prompt
  * optional: two panel buttons wired to the Pi's GPIO (``--buttons``) for
    dropping a mark by hand the instant the range ticks down a mile or a
    bar drops -- more precise than the periodic anchors. Hold BOTH for
    ``--stop-hold`` s to end the session cleanly without SSH.
  * optional: ``--lcd`` mirrors the ``t+<s>`` clock, marks-so-far and the
    SAY prompt to a SparkFun serial 4x20 (see ``tools/lcd.py``)

Runs for ``--minutes`` then stops itself; Ctrl-C (parked) also stops it
cleanly and closes the capture.

  DO NOT touch the Pi while driving. Start it before you pull away.

Afterwards:
  tools/mine_capture.py ~/candump-<ts>.log --ids
  tools/mine_capture.py ~/candump-<ts>.log --monotonic --top 25
  tools/mine_capture.py ~/candump-<ts>.log --series <ID>:<OFF>:<W>[:le] --every 20

Usage:
  # 90 min capture, periodic marks every 2 min, two GPIO buttons, LCD
  ./soc_log.py --yes --minutes 90 --mark-every 120 --buttons --lcd

  # minimal: just capture + periodic marks
  ./soc_log.py --yes --minutes 60

  # show the plan, touch no hardware
  ./soc_log.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf.lcd import SerLcd  # noqa: E402
from voltdmf.signals import SIGNAL_IDS  # noqa: E402

_SPEED_ADDR = SIGNAL_IDS["speed"].addr  # 0x3E9 -- UNCONFIRMED Gen-1, may be absent


# -- can0 state (read-only, via iproute2) ---------------------------------
def can_state(channel: str) -> str:
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


# -- raw candump -l capture for the whole session -----------------------
def start_capture(channel: str, outdir: str, log):
    d = pathlib.Path(outdir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    before = set(d.glob("candump-*.log"))
    try:
        proc = subprocess.Popen(["candump", "-l", channel], cwd=d,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"  candump did not start ({exc}); NO RAW CAPTURE -- abort and fix")
        return None, None
    time.sleep(0.8)
    if proc.poll() is not None:
        log("  candump exited immediately; NO RAW CAPTURE -- abort and fix")
        return None, None
    new = sorted(set(d.glob("candump-*.log")) - before)
    path = new[-1] if new else None
    log(f"  raw capture: candump -l {channel} -> "
        f"{path or (str(d) + '/candump-*.log')}")
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


# -- tentative speed context (best-effort, decode UNCONFIRMED) ----------
class SpeedProbe:
    """Non-blocking peek at 0x3E9 for context in the log. Optional -- if
    python-can or the bus is unavailable it just returns None forever."""

    def __init__(self, channel: str, log) -> None:
        self._bus = None
        try:
            import can  # noqa: PLC0415
            self._bus = can.Bus(interface="socketcan", channel=channel)
        except Exception as exc:  # noqa: BLE001  (broad: this is optional)
            log(f"  speed probe off ({exc}); marks will show spd~n/a")

    def mph(self, timeout: float = 0.3):
        if self._bus is None:
            return None
        import struct  # noqa: PLC0415
        end = time.time() + timeout
        latest = None
        while time.time() < end:
            msg = self._bus.recv(timeout=max(0.0, end - time.time()))
            if msg is None:
                break
            if msg.arbitration_id == _SPEED_ADDR and len(msg.data) >= 2:
                latest = struct.unpack_from(">H", bytes(msg.data), 0)[0] / 100.0
        return latest

    def close(self) -> None:
        if self._bus is not None:
            self._bus.shutdown()


# -- LCD mirror (optional; no-ops when lcd is None) ---------------------
class Dash:
    def __init__(self, lcd, t0: float, labels: tuple[str, str]) -> None:
        self.lcd = lcd
        self.t0 = t0
        self.labels = labels
        self.marks = 0
        self.last_mark_s = None
        self.phase = "starting"
        self.say_until = 0.0
        self._paints = 0
        if lcd is not None:
            self.paint()

    def set_phase(self, text: str) -> None:
        self.phase = text
        self.paint()

    def note_mark(self, n: int, t_s: float) -> None:
        self.marks = n
        self.last_mark_s = t_s
        self.say_until = time.time() + 8.0
        self.paint()

    def paint(self) -> None:
        if self.lcd is None:
            return
        el = int(time.time() - self.t0)
        last = "--" if self.last_mark_s is None else f"+{self.last_mark_s:.0f}s"
        self.lcd.line(0, f"t+{el:>5}s  {_dt.datetime.now():%H:%M:%S}")
        self.lcd.line(1, self.phase[:20])
        self.lcd.line(2, f"marks {self.marks:<3} last {last}")
        self.lcd.line(3, ">> SAY: range + bars" if time.time() < self.say_until
                      else f"A={self.labels[0][:6]}  B={self.labels[1][:7]}")
        self._paints += 1
        if self._paints % 20 == 0:
            self.lcd.refresh()  # recover a browned-out panel


# -- the session ------------------------------------------------------
class Session:
    """Shared clock + mark recorder. Thread-safe: GPIO button callbacks and
    the main loop both call :meth:`mark`."""

    def __init__(self, log, speed: "SpeedProbe", dash: "Dash",
                 channel: str) -> None:
        self.log = log
        self.speed = speed
        self.dash = dash
        self.channel = channel
        self.t0 = time.time()
        self.n = 0
        self._lock = threading.Lock()

    def mark(self, kind: str, note: str = "") -> None:
        with self._lock:
            self.n += 1
            dt = time.time() - self.t0
            sp = self.speed.mph()
            spd = "n/a" if sp is None else f"{sp:.0f}?"
            st = can_state(self.channel)
            tail = f"  {note}" if note else ""
            self.log(f"  {kind:<11} +{dt:7.1f}s  spd~{spd:<5} can0={st}"
                     f"  (#{self.n}){tail}")
            self.dash.note_mark(self.n, dt)


# -- GPIO buttons (optional; lazy import so this runs on any box) -------
class Buttons:
    """Two momentary buttons to GND on the Pi header, internal pull-ups.
    ``gpiozero`` only; if it is missing the tool runs without them."""

    def __init__(self, session: "Session", gpio_a: int, gpio_b: int,
                 labels: tuple[str, str], stop_hold: float, log) -> None:
        self._session = session
        self._labels = labels
        self._stop_hold = stop_hold
        self._both_since: float | None = None
        self.stop_requested = False
        self._btns = None
        try:
            from gpiozero import Button  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            log(f"  buttons off (no gpiozero: {exc})")
            return
        try:
            a = Button(gpio_a, pull_up=True, bounce_time=0.05)
            b = Button(gpio_b, pull_up=True, bounce_time=0.05)
        except Exception as exc:  # noqa: BLE001
            log(f"  buttons off (GPIO {gpio_a}/{gpio_b} unavailable: {exc})")
            return
        a.when_pressed = lambda: self._press(0)
        b.when_pressed = lambda: self._press(1)
        self._btns = (a, b)
        log(f"  buttons: GPIO{gpio_a}=A ({labels[0]})  GPIO{gpio_b}=B "
            f"({labels[1]})  hold both {stop_hold:.0f}s = stop")

    def _press(self, idx: int) -> None:
        self._session.mark(f"BTN-{self._labels[idx]}")

    def poll_stop(self) -> bool:
        """Call from the main loop. True once both buttons have been held
        for ``stop_hold`` seconds."""
        if self._btns is None or self._stop_hold <= 0:
            return False
        a, b = self._btns
        if a.is_pressed and b.is_pressed:
            now = time.time()
            if self._both_since is None:
                self._both_since = now
            elif now - self._both_since >= self._stop_hold:
                self.stop_requested = True
        else:
            self._both_since = None
        return self.stop_requested

    def close(self) -> None:
        if self._btns:
            for btn in self._btns:
                btn.close()


def make_logger(path: pathlib.Path):
    fh = open(path, "a", buffering=1)

    def log(msg: str = "") -> None:
        line = f"{_dt.datetime.now():%H:%M:%S}  {msg}" if msg else ""
        print(line, flush=True)
        fh.write(line + "\n")

    return log, fh


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true",
                    help="required for a live run: you are set up and about "
                         "to drive; this tool is passive (never transmits)")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--minutes", type=float, default=60.0,
                    help="session length before it stops itself (default 60)")
    ap.add_argument("--mark-every", type=float, default=120.0, metavar="SECS",
                    help="seconds between periodic SOC-MARK anchors "
                         "(0 = only button marks; default 120)")
    ap.add_argument("--capture-dir", default="~",
                    help="where candump -l writes its log (default ~)")
    ap.add_argument("--buttons", action="store_true",
                    help="enable two GPIO panel buttons for manual marks")
    ap.add_argument("--button-a-gpio", type=int, default=5, metavar="BCM",
                    help="BCM pin for button A (default 5 = header pin 29)")
    ap.add_argument("--button-b-gpio", type=int, default=6, metavar="BCM",
                    help="BCM pin for button B (default 6 = header pin 31)")
    ap.add_argument("--button-a-label", default="RANGE",
                    help="tag button A marks with this (default RANGE)")
    ap.add_argument("--button-b-label", default="BAR",
                    help="tag button B marks with this (default BAR)")
    ap.add_argument("--stop-hold", type=float, default=3.0, metavar="SECS",
                    help="hold BOTH buttons this long to end the session "
                         "(0 = disable; default 3)")
    ap.add_argument("--lcd", action="store_true",
                    help="mirror the clock / marks / SAY prompt to a "
                         "SparkFun serial 4x20 LCD (see tools/lcd.py)")
    ap.add_argument("--lcd-port", default="/dev/serial0")
    ap.add_argument("--lcd-baud", type=int, default=9600)
    ap.add_argument("--lcd-backlight", type=int, default=45, metavar="PCT",
                    help="LCD backlight 0..100 (default 45; lower it if the "
                         "panel resets -- that is a 5 V sag, not software)")
    ap.add_argument("--logfile", default=None,
                    help="log path (default ~/vdmf-soclog-<timestamp>.log)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and exit; no hardware touched")
    args = ap.parse_args()

    labels = (args.button_a_label, args.button_b_label)
    mark_desc = (f"every {args.mark_every:.0f}s"
                 if args.mark_every > 0 else "button marks only")
    btn_desc = (f"GPIO{args.button_a_gpio}/{args.button_b_gpio} "
                f"({labels[0]}/{labels[1]})" if args.buttons else "OFF")

    print("SOC-discovery drive log (PASSIVE -- no TX)")
    print(f"plan: {args.minutes:.0f} min capture on {args.channel}")
    print(f"  SOC-MARK: {mark_desc}   buttons: {btn_desc}   "
          f"LCD: {'on' if args.lcd else 'off'}")
    print(f"  raw capture -> {args.capture_dir}/candump-<ts>.log "
          f"(mine with tools/mine_capture.py)")

    if args.dry_run:
        print("\n[DRY RUN] nothing started.")
        return

    if not args.yes:
        sys.exit("refusing to start without --yes (or pass --dry-run)")

    st = can_state(args.channel)
    if st in ("STOPPED", "?"):
        sys.exit(f"{args.channel} is {st} -- bring the bus up first "
                 f"(sudo ip link set {args.channel} up type can bitrate 500000)")

    logpath = pathlib.Path(
        args.logfile or pathlib.Path.home() /
        f"vdmf-soclog-{_dt.datetime.now():%Y%m%d-%H%M%S}.log")
    log, fh = make_logger(logpath)
    log(f"soc_log start  channel={args.channel}  minutes={args.minutes:.0f}  "
        f"mark-every={args.mark_every:.0f}s  logfile={logpath}")
    log(f"can0 state: {st}")

    cap_proc, cap_path = start_capture(args.channel, args.capture_dir, log)
    if cap_proc is None:
        log("no raw capture -- aborting (that IS the deliverable).")
        fh.close()
        sys.exit(1)

    lcd = None
    if args.lcd:
        try:
            lcd = SerLcd(args.lcd_port, args.lcd_baud,
                         backlight=args.lcd_backlight).open()
            log(f"LCD mirror on {args.lcd_port} @ {args.lcd_baud} "
                f"(backlight {args.lcd_backlight}%)")
        except OSError as exc:
            log(f"LCD open failed ({exc}); continuing without the mirror")

    t0 = time.time()
    dash = Dash(lcd, t0, labels)
    speed = SpeedProbe(args.channel, log)
    session = Session(log, speed, dash, args.channel)
    session.t0 = t0  # share the exact origin with the Dash

    buttons = Buttons(session, args.button_a_gpio, args.button_b_gpio,
                      labels, args.stop_hold, log) if args.buttons else None

    log("")
    log("logging -- drive the pack down. Say EV range + battery bars on every "
        "SOC-MARK / LCD prompt. Ctrl-C (parked) or hold both buttons to stop.")
    session.mark("START")

    end = t0 + args.minutes * 60
    next_mark = args.mark_every
    stopped_by = "duration"
    last_tick = 0  # whole-second gate for the LCD repaint + can0 poll
    try:
        while time.time() < end:
            time.sleep(0.2)  # keep button-hold detection responsive
            now = time.time() - t0

            if args.mark_every > 0 and now >= next_mark:
                session.mark("SOC-MARK")
                while next_mark <= now:
                    next_mark += args.mark_every

            if buttons is not None and buttons.poll_stop():
                stopped_by = "button hold"
                break

            sec = int(now)
            if sec != last_tick:  # once a second is plenty for these
                last_tick = sec
                dash.set_phase(f"LOGGING  {(end - time.time()) / 60:.0f}min left")
                if sec % 30 == 0:
                    cs = can_state(args.channel)
                    if cs in ("BUS-OFF", "STOPPED"):
                        log(f"  can0 went {cs} -- capture continues, "
                            f"but check the bus")
    except KeyboardInterrupt:
        stopped_by = "Ctrl-C"
        log("\ninterrupted -- stopping (hope you're parked).")

    session.mark("END")
    log("")
    log(f"================ SUMMARY  (stopped: {stopped_by}) ================")
    log(f"  {session.n} marks over {(time.time() - t0) / 60:.1f} min")
    stop_capture(cap_proc, cap_path, log)
    speed.close()
    if buttons is not None:
        buttons.close()
    if lcd is not None:
        dash.set_phase("DONE -- review log")
        lcd.close()
    if cap_path:
        log(f"  raw capture:  {cap_path}")
        log(f"  next:  tools/mine_capture.py {cap_path} --monotonic --top 25")
    log(f"  log written to {logpath}")
    log("========================================================")
    fh.close()


if __name__ == "__main__":
    main()
