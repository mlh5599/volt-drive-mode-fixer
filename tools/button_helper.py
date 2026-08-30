#!/usr/bin/env python3
"""Panel-button helper -- one long-lived process, dispatch by gesture.

Runs as ``voltdmf-btn.service`` (see ``systemd/voltdmf-btn.service``). Owns
the two PiCAN2 switch pads whenever no capture is running and turns presses
into two actions:

    SW1 tap  (short press, SW2 untouched)
        -> ``voltdmf-ctl setpoint <hold|mountain>`` -- toggles the reconciler
           setpoint (DESIGN.md design item 4). A no-op until `setpoint` lands
           in voltdmf-ctl; the failing call is logged and swallowed.

    SW1 + SW2 held >= --launch-hold-secs
        -> launch the SOC-discovery capture:
             1. claim the LCD hand-off lock (daemon watch screen yields)
             2. close() both Button objects -- free the GPIO lines
             3. `systemctl start voltdmf-soclog.service` and block until it
                finishes (soc_log.py runs ~90 min, or stops early on its own
                hold-BOTH gesture)
             4. re-acquire SW1/SW2, release the LCD lock, resume

During a capture this helper is dormant and ``soc_log.py`` owns the buttons
(A = gauge-down, B = gauge-up, hold BOTH --stop-hold s = stop). On its own
startup, if the capture unit is already active the helper waits it out
instead of fighting for the GPIO lines -- so a helper restart mid-capture is
harmless.

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

#: `systemctl is-active` outputs that mean "the capture is still going".
#: A Type=oneshot/RemainAfterExit=no unit sits in `activating` for its whole
#: run, so `active` alone is not enough.
_RUNNING_STATES = {"active", "activating", "reloading"}


class ButtonHelper:
    def __init__(self, args: argparse.Namespace) -> None:
        self._sw1_gpio = args.sw1_gpio
        self._sw2_gpio = args.sw2_gpio
        self._bounce = args.bounce_ms / 1000.0
        self._min_tap = args.min_tap_ms / 1000.0
        self._launch_hold = args.launch_hold_secs
        self._unit = args.soclog_unit
        self._ctl = args.ctl
        self._setpoints = args.setpoints
        self._setpoint_idx = (self._setpoints.index(args.setpoint_start)
                              if args.setpoint_start in self._setpoints else 0)
        self._lcd_opts = dict(port=args.lcd_port, baud=args.lcd_baud,
                              backlight=args.lcd_backlight)

        self._stop = threading.Event()
        self._sw1 = None  # gpiozero.Button once acquired
        self._sw2 = None

    # -- lifecycle -------------------------------------------------------
    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._stop.set())

    def run(self) -> None:
        if self._unit_running():
            log.info("%s already active at startup -- attaching, not grabbing GPIO",
                     self._unit)
            self._wait_out_unit()
        self._acquire_with_retry()
        log.info("watching: SW1 tap = setpoint toggle (%s), "
                 "SW1+SW2 hold %.0fs = launch %s",
                 "/".join(self._setpoints), self._launch_hold, self._unit)
        try:
            self._loop()
        finally:
            self._release_buttons()
            log.info("stopped")

    # -- the gesture loop ---------------------------------------------------
    def _loop(self) -> None:
        both_since: float | None = None
        launched = False          # latched after a launch until both release
        sw1_down_at: float | None = None
        sw1_saw_sw2 = False       # SW2 joined during this SW1 hold -> not a tap

        while not self._stop.wait(_POLL_S):
            a = self._sw1.is_pressed
            b = self._sw2.is_pressed
            now = time.monotonic()

            # --- SW1 tap -> setpoint toggle -------------------------------
            if a and sw1_down_at is None:
                sw1_down_at, sw1_saw_sw2 = now, b
            if sw1_down_at is not None and b:
                sw1_saw_sw2 = True
            if not a and sw1_down_at is not None:
                held = now - sw1_down_at
                if (not sw1_saw_sw2 and not launched
                        and self._min_tap <= held < self._launch_hold):
                    self._toggle_setpoint()
                sw1_down_at, sw1_saw_sw2 = None, False

            # --- SW1 + SW2 hold -> launch capture -----------------------
            if a and b:
                if both_since is None:
                    both_since = now
                elif not launched and now - both_since >= self._launch_hold:
                    launched = True
                    self._do_launch()      # blocks; re-acquires buttons before return
                    both_since = None
            else:
                both_since = None
                if not a and not b:
                    launched = False       # armed again once fully released

    # -- action: setpoint toggle -----------------------------------------
    def _toggle_setpoint(self) -> None:
        self._setpoint_idx ^= 1
        target = self._setpoints[self._setpoint_idx]
        argv = [self._ctl, "setpoint", target]
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
            log.warning("  voltdmf-ctl rc=%d: %s  "
                        "(expected until `setpoint` lands in voltdmf-ctl)",
                        r.returncode, msg)

    # -- action: launch the SOC capture --------------------------------
    def _do_launch(self) -> None:
        log.info("launch gesture -> starting SOC capture (%s)", self._unit)
        claimed = lcdlock.claim("button_helper: launching soc_log")
        if claimed:
            time.sleep(0.3)          # let the daemon watch screen close the port
        self._flash_lcd("SOC CAPTURE", "starting candump...", "", "hold BOTH = stop")
        self._release_buttons()      # soc_log.py --buttons needs these same pins
        self._stop.wait(0.5)         # let lgpio free the lines before soc_log grabs
        try:
            self._wait_out_unit(start=True)
        finally:
            if claimed:
                lcdlock.release()    # no-op if soc_log already cleaned it up
            self._acquire_with_retry()
        log.info("SOC capture finished -- back to watching for gestures")

    # -- systemd unit plumbing -------------------------------------------
    def _unit_running(self) -> bool:
        try:
            r = subprocess.run(["systemctl", "is-active", self._unit],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("systemctl is-active failed (%s); assuming not running", exc)
            return False
        return r.stdout.strip() in _RUNNING_STATES

    def _wait_out_unit(self, *, start: bool = False) -> None:
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
            r = self._run_systemctl(["start", "--no-block", self._unit])
            if r is not None and r.returncode != 0:
                log.error("could not start %s: %s", self._unit,
                          (r.stderr or r.stdout).strip())
                return
            # Wait out the pre-start window so the leave-watch below does not
            # see the stale `inactive` and return immediately.
            deadline = time.monotonic() + 20.0
            while not self._stop.is_set() and time.monotonic() < deadline:
                if self._unit_running():
                    break
                self._stop.wait(0.25)
            else:
                if not self._stop.is_set():
                    log.warning("%s did not come up within 20s -- watching anyway",
                                self._unit)

        while not self._stop.is_set():
            if not self._unit_running():
                log.info("%s finished", self._unit)
                return
            self._stop.wait(1.0)
        log.info("helper stopping -- leaving %s running", self._unit)

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
                    help="hold BOTH buttons this long to launch the capture "
                         "(default 5; must exceed soc_log.py's own 3s stop hold)")
    ap.add_argument("--bounce-ms", type=float, default=50.0)
    ap.add_argument("--min-tap-ms", type=float, default=40.0,
                    help="ignore an SW1 blip shorter than this as noise")
    ap.add_argument("--soclog-unit", default="voltdmf-soclog.service",
                    help="systemd unit the launch gesture starts")
    ap.add_argument("--ctl", default="/usr/local/bin/voltdmf-ctl",
                    help="path to the voltdmf-ctl entry point")
    ap.add_argument("--setpoints", nargs=2, default=["hold", "mountain"],
                    metavar=("A", "B"),
                    help="the two reconciler setpoints SW1 toggles between "
                         "(default: hold mountain)")
    ap.add_argument("--setpoint-start", default="hold",
                    help="setpoint assumed current at boot; the first tap "
                         "selects the other (default: hold)")
    ap.add_argument("--lcd-port", default="/dev/serial0")
    ap.add_argument("--lcd-baud", type=int, default=9600)
    ap.add_argument("--lcd-backlight", type=int, default=45, metavar="PCT")
    ap.add_argument("--log-level", default="INFO",
                    help="DEBUG/INFO/WARNING (default INFO)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved plan and exit; touch no hardware")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s")

    if args.dry_run:
        print("button_helper plan (nothing started):")
        print(f"  SW1 = BCM {args.sw1_gpio}   SW2 = BCM {args.sw2_gpio}   "
              f"debounce {args.bounce_ms:.0f} ms")
        print(f"  SW1 tap        -> {args.ctl} setpoint "
              f"<{'/'.join(args.setpoints)}>  (start {args.setpoint_start})")
        print(f"  SW1+SW2 {args.launch_hold_secs:.0f}s -> systemctl start "
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
