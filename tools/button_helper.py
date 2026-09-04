#!/usr/bin/env python3
"""Panel-button helper -- one long-lived process, dispatch by gesture.

Runs as ``voltdmf-btn.service`` (see ``systemd/voltdmf-btn.service``). Owns
the two PiCAN2 switch pads whenever no capture is running and turns presses
into three actions:

    SW1 tap  (short press, SW2 untouched)
        -> ``voltdmf-ctl setpoint next`` -- advances the daemon's three-position
           selector one detent: hold (hold the pack at 30%) -> mountain -> off
           (car untouched) -> back to hold. The daemon owns the cycle; this
           helper keeps no index of its own. A failing call is logged and
           swallowed.

    SW1 held alone >= --selftest-hold-secs, then released  (SW2 never joined)
        -> ``voltdmf-ctl walk-test`` -- the daemon cycles the closed-loop mode
           walk through every drive mode, verifies each landing off 0x1F4, and
           walks back to the starting mode. Deliberately long (default 8 s, and
           the 5-8 s window is a dead zone) so a slow reach for the SW1+SW2
           combo cannot trip it. Progress/result show on the LCD watch screen.

    SW2 held alone >= --single-hold-secs, then released  (SW1 never joined)
        -> launch the charge-current-setpoint capture (phase-c checklist
           section 2e -- force 12 A Level 1 charging). Same launch / hand-off
           dance as the SOC combo below, but starts
           ``voltdmf-chargelog.service``. Fires on release, so a hand
           travelling toward the SW1+SW2 combo never trips it.

    SW1 + SW2 held >= --launch-hold-secs
        -> launch the SOC-discovery capture:
             1. claim the LCD hand-off lock (daemon watch screen yields)
             2. close() both Button objects -- free the GPIO lines
             3. `systemctl start voltdmf-soclog.service` and block until it
                finishes (soc_log.py runs ~90 min, or stops early on its own
                hold-BOTH gesture)
             4. re-acquire SW1/SW2, release the LCD lock, resume

During either capture this helper is dormant and ``soc_log.py`` owns the
buttons (A = gauge-down, B = gauge-up, hold BOTH --stop-hold s = stop). When
a capture ends the helper waits for both pads to be released before it reads
gestures again, so the stop-hold cannot roll straight into a fresh launch.
On its own startup, if either capture unit is already active the helper waits
it out instead of fighting for the GPIO lines -- so a helper restart
mid-capture is harmless.

Needs ``gpiozero`` (+ an lgpio/RPi.GPIO backend): run it under the system
interpreter, not the daemon venv (pi-deploy notes, "Python split on the Pi").

    ./button_helper.py --sw1-gpio 24 --sw2-gpio 23 --launch-hold-secs 5
    ./button_helper.py --dry-run          # print the resolved plan, touch nothing
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import pathlib
import signal
import subprocess
import sys
import threading
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from voltdmf import lcdlock  # noqa: E402

log = logging.getLogger("button_helper")

#: Main-loop poll period. Fast enough that a deliberate tap/hold never slips
#: between samples; slow enough to stay invisible on the CPU.
_POLL_S = 0.05

#: `systemctl is-active` outputs that mean "the capture is over". Everything
#: else -- `active`, `activating` (a Type=oneshot/RemainAfterExit=no unit sits
#: here for its whole run), `reloading`, `deactivating`, or an unreadable
#: answer -- is treated as "still going". Guessing "over" is the dangerous
#: direction: it makes the helper grab SW1/SW2 back mid-capture.
_DONE_STATES = {"inactive", "failed"}


class _Gestures:
    """Pure gesture state machine: fed ``(sw1, sw2, now)`` once per poll,
    returns at most one action string per call --

        ``"setpoint"``       SW1 tapped alone: released after
                             ``min_tap <= held < launch_hold`` with SW2 never
                             joined during the hold.
        ``"walk_test"``      SW1 held alone >= ``selftest_hold`` then released,
                             SW2 never joined. Fires on release. The
                             ``launch_hold..selftest_hold`` window is a dead
                             zone (neither a tap nor the test).
        ``"launch_charge"``  SW2 held alone >= ``single_hold`` then released,
                             SW1 never joined. Fires on release.
        ``"launch_soc"``     SW1 and SW2 both held >= ``launch_hold``. Fires
                             while still held (one-shot per continuous hold).

    Side-effect free so the gesture rules are unit-testable without GPIO. The
    caller drives the hardware and, after a blocking launch, calls ``reset()``.
    """

    def __init__(self, *, min_tap: float, single_hold: float,
                 launch_hold: float, selftest_hold: float = float("inf")) -> None:
        self._min_tap = min_tap
        self._single_hold = single_hold
        self._launch_hold = launch_hold
        self._selftest_hold = selftest_hold
        self.reset()

    def reset(self) -> None:
        self._both_since: float | None = None
        self._launched = False        # SOC one-shot latch, cleared on release
        self._sw1_at: float | None = None
        self._sw1_saw_sw2 = False     # SW2 joined this SW1 hold -> not a tap
        self._sw2_at: float | None = None
        self._sw2_saw_sw1 = False     # SW1 joined this SW2 hold -> not solo

    def feed(self, a: bool, b: bool, now: float) -> str | None:
        action: str | None = None

        # --- SW1 press: tap alone -> setpoint toggle --------------------
        if a and self._sw1_at is None:
            self._sw1_at, self._sw1_saw_sw2 = now, b
        if self._sw1_at is not None and b:
            self._sw1_saw_sw2 = True
        if not a and self._sw1_at is not None:
            held = now - self._sw1_at
            if not self._sw1_saw_sw2 and not self._launched:
                if self._min_tap <= held < self._launch_hold:
                    action = "setpoint"
                elif held >= self._selftest_hold:
                    action = "walk_test"
                # launch_hold..selftest_hold: dead zone, no action
            self._sw1_at, self._sw1_saw_sw2 = None, False

        # --- SW2 held alone -> charge-mode capture (fires on release) ---
        if b and self._sw2_at is None:
            self._sw2_at, self._sw2_saw_sw1 = now, a
        if self._sw2_at is not None and a:
            self._sw2_saw_sw1 = True
        if not b and self._sw2_at is not None:
            held = now - self._sw2_at
            if (not self._sw2_saw_sw1 and not self._launched
                    and held >= self._single_hold):
                action = "launch_charge"
            self._sw2_at, self._sw2_saw_sw1 = None, False

        # --- SW1 + SW2 held -> SOC-discovery capture (fires while held) -
        if a and b:
            if self._both_since is None:
                self._both_since = now
            elif (not self._launched
                    and now - self._both_since >= self._launch_hold):
                self._launched = True
                action = "launch_soc"
                self._both_since = None
        else:
            self._both_since = None
            if not a and not b:
                self._launched = False    # armed again once fully released

        return action


class ButtonHelper:
    def __init__(self, args: argparse.Namespace) -> None:
        self._sw1_gpio = args.sw1_gpio
        self._sw2_gpio = args.sw2_gpio
        self._bounce = args.bounce_ms / 1000.0
        self._min_tap = args.min_tap_ms / 1000.0
        self._launch_hold = args.launch_hold_secs
        self._single_hold = args.single_hold_secs
        self._selftest_hold = args.selftest_hold_secs
        self._unit = args.soclog_unit
        self._chargelog_unit = args.chargelog_unit
        self._ctl = args.ctl
        self._lcd_opts = dict(port=args.lcd_port, baud=args.lcd_baud,
                              backlight=args.lcd_backlight)

        self._gestures = _Gestures(min_tap=self._min_tap,
                                   single_hold=self._single_hold,
                                   launch_hold=self._launch_hold,
                                   selftest_hold=self._selftest_hold)
        self._stop = threading.Event()
        self._sw1 = None  # gpiozero.Button once acquired
        self._sw2 = None

    # -- lifecycle -------------------------------------------------------
    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._stop.set())

    def run(self) -> None:
        for unit in (self._unit, self._chargelog_unit):
            if self._unit_running(unit):
                log.info("%s already active at startup -- attaching, not "
                         "grabbing GPIO", unit)
                self._wait_out_unit(unit)
        self._acquire_with_retry()
        log.info("watching: SW1 tap = selector next (hold -> mountain -> off), "
                 "SW1 hold %.0fs = walk-test, "
                 "SW2 hold %.0fs = launch %s, "
                 "SW1+SW2 hold %.0fs = launch %s",
                 self._selftest_hold,
                 self._single_hold, self._chargelog_unit,
                 self._launch_hold, self._unit)
        try:
            self._loop()
        finally:
            self._release_buttons()
            log.info("stopped")

    # -- the gesture loop ---------------------------------------------------
    def _loop(self) -> None:
        self._gestures.reset()
        while not self._stop.wait(_POLL_S):
            action = self._gestures.feed(self._sw1.is_pressed,
                                         self._sw2.is_pressed,
                                         time.monotonic())
            if action == "setpoint":
                self._toggle_setpoint()
            elif action == "walk_test":
                self._run_walk_test()
            elif action == "launch_charge":
                self._do_launch(self._chargelog_unit, "CHARGE CAPTURE")
                self._drain_until_released()
                self._gestures.reset()
            elif action == "launch_soc":
                self._do_launch(self._unit, "SOC CAPTURE")
                self._drain_until_released()
                self._gestures.reset()

    def _drain_until_released(self) -> None:
        """A capture's stop gesture is hold-BOTH; when it exits the driver's
        fingers are still on both pads. Wait for a clean release before the
        FSM interprets gestures again so the stop-hold cannot roll straight
        into a fresh launch."""
        while not self._stop.wait(_POLL_S):
            if not self._sw1.is_pressed and not self._sw2.is_pressed:
                return

    # -- action: setpoint toggle -----------------------------------------
    def _toggle_setpoint(self) -> None:
        """SW1 tap: ask the daemon to advance its selector one detent.

        The helper deliberately keeps no idea of which position the car is in.
        It used to hold its own index and send an absolute target, which drifts
        the moment anything else moves the selector -- a daemon restart (the
        position is not persisted), or a `voltdmf-ctl setpoint` from the shell.
        After a drift the next tap re-sends the position the car is already in
        and looks like a dead button. `next` is evaluated by the one component
        that actually knows the current position.
        """
        argv = [self._ctl, "setpoint", "next"]
        log.info("SW1 tap -> %s", " ".join(argv))
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("  setpoint call did not run: %s", exc)
            return
        if r.returncode == 0:
            out = r.stdout.strip()
            log.info("  ok%s", f": {out}" if out else "")
        else:
            msg = (r.stderr or r.stdout).strip()
            log.warning("  voltdmf-ctl rc=%d: %s", r.returncode, msg)

    # -- action: closed-loop mode-walk self-test ------------------------
    def _run_walk_test(self) -> None:
        """SW1 solo hold: kick off ``voltdmf-ctl walk-test``. The daemon
        returns at once and runs the ~1 min cycle itself, reporting on the LCD
        watch screen -- so this just fires the command and logs the ack."""
        argv = [self._ctl, "walk-test"]
        log.info("SW1 hold >=%.0fs -> %s", self._selftest_hold, " ".join(argv))
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("  walk-test call did not run: %s", exc)
            return
        out = (r.stdout or r.stderr).strip()
        if r.returncode == 0:
            log.info("  %s", out or "started")
        else:
            log.warning("  voltdmf-ctl rc=%d: %s", r.returncode, out)

    # -- action: launch a capture -------------------------------------
    def _do_launch(self, unit: str, lcd_title: str) -> None:
        log.info("launch gesture -> starting capture (%s)", unit)
        claimed = lcdlock.claim(f"button_helper: launching {unit}")
        if claimed:
            time.sleep(0.3)          # let the daemon watch screen close the port
        self._flash_lcd(lcd_title, "starting candump...", "", "hold BOTH = stop")
        self._release_buttons()      # soc_log.py --buttons needs these same pins
        self._stop.wait(0.5)         # let lgpio free the lines before soc_log grabs
        try:
            self._wait_out_unit(unit, start=True)
        finally:
            if claimed:
                lcdlock.release()    # no-op if soc_log already cleaned it up
            self._acquire_with_retry()
        log.info("%s finished -- back to watching for gestures", unit)

    # -- systemd unit plumbing -------------------------------------------
    def _unit_running(self, unit: str) -> bool:
        """True while ``unit`` is up. On a transient ``systemctl`` error we
        keep saying "up" (up to three tries) rather than "finished": a false
        "finished" makes the caller re-grab SW1/SW2 while soc_log.py is still
        running, which is exactly the bug this whole dance avoids."""
        for _ in range(3):
            try:
                r = subprocess.run(["systemctl", "is-active", unit],
                                   capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("systemctl is-active %s failed (%s); assuming still up",
                            unit, exc)
                self._stop.wait(0.5)
                continue
            return r.stdout.strip() not in _DONE_STATES
        return True

    def _wait_out_unit(self, unit: str, *, start: bool = False) -> None:
        """Block until the capture unit is no longer running, tracking it by
        ``systemctl is-active`` -- never by the ``systemctl start`` exit.

        A blocking ``systemctl start`` only reliably waits out a Type=oneshot
        job when it is called as root on the system bus; over the polkit path
        the helper uses (User=voltdmf-btn) it can return as soon as the job is
        queued. Trusting its return code let the helper re-grab SW1/SW2
        seconds into a 90-minute capture, so soc_log.py came up with
        "GPIO busy" and ran the whole drive with its buttons dead. Poll
        instead.

        With ``start``: fire ``--no-block``, wait for the unit to actually come
        up, then fall through to the leave-watch. If we bail on ``self._stop``
        the unit keeps running under systemd -- we never stop the capture."""
        if start:
            r = self._run_systemctl(["start", "--no-block", unit])
            if r is not None and r.returncode != 0:
                log.error("could not start %s: %s", unit,
                          (r.stderr or r.stdout).strip())
                return
            # Wait out the pre-start window so the leave-watch below does not
            # see the stale `inactive` and return immediately.
            deadline = time.monotonic() + 20.0
            while not self._stop.is_set() and time.monotonic() < deadline:
                if self._unit_running(unit):
                    break
                self._stop.wait(0.25)
            else:
                if not self._stop.is_set():
                    log.warning("%s did not come up within 20s -- watching anyway",
                                unit)

        while not self._stop.is_set():
            if not self._unit_running(unit):
                log.info("%s finished", unit)
                return
            self._stop.wait(1.0)
        log.info("helper stopping -- leaving %s running", unit)

    def _run_systemctl(self, args: list[str]) -> "subprocess.CompletedProcess | None":
        try:
            return subprocess.run(["systemctl", *args],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("systemctl %s failed: %s", " ".join(args), exc)
            return None

    # -- GPIO ----------------------------------------------------------
    def _acquire_with_retry(self, tries: int = 12, delay: float = 0.5) -> None:
        last: Exception | None = None
        for i in range(1, tries + 1):
            if self._stop.is_set():
                return
            try:
                from gpiozero import Button  # noqa: PLC0415  (lazy: dev boxes / --help)
                self._sw1 = Button(self._sw1_gpio, pull_up=True,
                                   bounce_time=self._bounce)
                self._sw2 = Button(self._sw2_gpio, pull_up=True,
                                   bounce_time=self._bounce)
                log.info("buttons acquired: SW1=BCM%d  SW2=BCM%d",
                         self._sw1_gpio, self._sw2_gpio)
                return
            except Exception as exc:  # noqa: BLE001  (pin busy / no backend)
                last = exc
                self._release_buttons()
                log.warning("  GPIO %d/%d unavailable (%s); retry %d/%d",
                            self._sw1_gpio, self._sw2_gpio, exc, i, tries)
                self._stop.wait(delay)
        raise SystemExit(f"could not claim GPIO {self._sw1_gpio}/{self._sw2_gpio} "
                         f"after {tries} tries: {last}")

    def _release_buttons(self) -> None:
        for name in ("_sw1", "_sw2"):
            btn = getattr(self, name)
            if btn is not None:
                try:
                    btn.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, name, None)
        # gpiozero's Button.close() drops the Python object but leaves lgpio's
        # gpiochip handle open, so the kernel lines stay claimed and the next
        # owner (soc_log.py) comes up "GPIO busy" for the whole capture. Tear
        # the pin factory down too: closing the gpiochip fd frees every line
        # it holds. The next Button() call lazily rebuilds the factory.
        try:
            from gpiozero import Device  # noqa: PLC0415
            if Device.pin_factory is not None:
                Device.pin_factory.close()
                Device.pin_factory = None
        except Exception as exc:  # noqa: BLE001
            log.debug("pin-factory teardown skipped: %s", exc)

    # -- LCD (best-effort; the capture does not depend on it) -----------
    def _flash_lcd(self, *rows: str) -> None:
        try:
            from voltdmf.lcd import SerLcd  # noqa: PLC0415
            lcd = SerLcd(**self._lcd_opts).open()
            try:
                lcd.lines(*(list(rows) + ["", "", "", ""])[:4])
            finally:
                lcd.close()
        except Exception as exc:  # noqa: BLE001
            log.info("(LCD note skipped: %s)", exc)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sw1-gpio", type=int, default=24, metavar="BCM",
                    help="PiCAN2 SW1 -- setpoint-toggle button (default 24)")
    ap.add_argument("--sw2-gpio", type=int, default=23, metavar="BCM",
                    help="PiCAN2 SW2 -- second button for the launch combo "
                         "(default 23)")
    ap.add_argument("--launch-hold-secs", type=float, default=5.0, metavar="S",
                    help="hold BOTH buttons this long to launch the SOC "
                         "capture (default 5; must exceed soc_log.py's own 3s "
                         "stop hold)")
    ap.add_argument("--single-hold-secs", type=float, default=5.0, metavar="S",
                    help="hold SW2 ALONE this long (SW1 untouched), then "
                         "release, to launch the charge-mode capture "
                         "(default 5)")
    ap.add_argument("--selftest-hold-secs", type=float, default=8.0, metavar="S",
                    help="hold SW1 ALONE this long (SW2 untouched), then "
                         "release, to run the closed-loop mode-walk self-test "
                         "(`voltdmf-ctl walk-test`). Must exceed "
                         "--launch-hold-secs; the gap is a dead zone "
                         "(default 8)")
    ap.add_argument("--bounce-ms", type=float, default=50.0)
    ap.add_argument("--min-tap-ms", type=float, default=40.0,
                    help="ignore an SW1 blip shorter than this as noise")
    ap.add_argument("--soclog-unit", default="voltdmf-soclog.service",
                    help="systemd unit the SW1+SW2 launch gesture starts")
    ap.add_argument("--chargelog-unit", default="voltdmf-chargelog.service",
                    help="systemd unit the SW2-solo-hold gesture starts "
                         "(charge-current-setpoint discovery capture)")
    ap.add_argument("--ctl", default="/usr/local/bin/voltdmf-ctl",
                    help="path to the voltdmf-ctl entry point")
    # DEPRECATED, accepted so a unit file written before the three-position
    # selector still starts. SW1 now sends `setpoint next` and the daemon owns
    # the cycle, so there is nothing here for the helper to configure.
    ap.add_argument("--setpoints", nargs=2, default=None, metavar=("A", "B"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--setpoint-start", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--lcd-port", default="/dev/serial0")
    ap.add_argument("--lcd-baud", type=int, default=9600)
    ap.add_argument("--lcd-backlight", type=int, default=45, metavar="PCT")
    ap.add_argument("--log-level", default="INFO",
                    help="DEBUG/INFO/WARNING (default INFO)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved plan and exit; touch no hardware")
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.selftest_hold_secs <= args.launch_hold_secs:
        parser.error("--selftest-hold-secs must exceed --launch-hold-secs "
                     f"({args.selftest_hold_secs} <= {args.launch_hold_secs})")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s")

    if args.dry_run:
        print("button_helper plan (nothing started):")
        print(f"  SW1 = BCM {args.sw1_gpio}   SW2 = BCM {args.sw2_gpio}   "
              f"debounce {args.bounce_ms:.0f} ms")
        print(f"  SW1 tap         -> {args.ctl} setpoint next"
              "   (hold -> mountain -> off -> hold)")
        print(f"  SW1 solo {args.selftest_hold_secs:.0f}s  -> {args.ctl} "
              f"walk-test  (daemon runs the ~1 min cycle, LCD shows result)")
        print(f"  SW2 solo {args.single_hold_secs:.0f}s  -> systemctl start "
              f"{args.chargelog_unit}  (blocks until it finishes)")
        print(f"  SW1+SW2 {args.launch_hold_secs:.0f}s   -> systemctl start "
              f"{args.soclog_unit}  (blocks until it finishes)")
        print(f"  LCD note on {args.lcd_port} @ {args.lcd_baud} "
              f"(backlight {args.lcd_backlight}%, best-effort)")
        print(f"  LCD hand-off lock: {lcdlock.LOCK_PATH}")
        return 0

    helper = ButtonHelper(args)
    helper.install_signal_handlers()
    log.info("button_helper start  %s", _dt.datetime.now().isoformat(timespec="seconds"))
    try:
        helper.run()
    except SystemExit as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
