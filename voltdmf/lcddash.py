"""The idle *watch* screen the daemon paints on the LCD.

When the daemon is up and nothing else has claimed the panel (see
:mod:`voltdmf.lcdlock`), a background thread mirrors a small status screen
to the SparkFun 4x20:

    +--------------------+
    |DMF   2h03  14:23:07|
    |mode   MOUNTAIN     |
    |gear D    bus ACTIVE|
    |on-start -> MOUNTAIN |
    +--------------------+

Row 0  uptime + wall clock -- proof the daemon is alive.
Row 1  committed drive mode (0x1F4 byte 1).
Row 2  PRNDL gear (0x1F5 byte 3) + the SocketCAN error state.
Row 3  a one-line status the daemon supplies (what it is armed to do /
       the last switch it requested).

Everything here is fail-soft: the thread never raises into the daemon, a
missing / unusable serial port just means the screen stays dark, and a
purpose-driven tool taking :func:`voltdmf.lcdlock.hold` makes the thread
close the port and wait rather than fight for it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import subprocess
import threading
import time

from . import lcdlock
from .lcd import SerLcd
from .signals import DriveMode, ShiftPosition
from .state import VehicleState

log = logging.getLogger(__name__)

#: SocketCAN "can state <x>" -> a tag that fits the row.
_BUS_ABBR = {
    "ERROR-ACTIVE": "ACTIVE", "ERROR-WARNING": "WARN",
    "ERROR-PASSIVE": "PASV", "BUS-OFF": "BUSOFF", "STOPPED": "STOP",
}
_GEAR_LETTER = {
    ShiftPosition.PARK: "P", ShiftPosition.REVERSE: "R",
    ShiftPosition.NEUTRAL: "N", ShiftPosition.DRIVE: "D",
    ShiftPosition.LOW: "L", ShiftPosition.UNKNOWN: "?",
}

#: How often the thread repaints, and how often it forces a full refresh
#: (to recover a panel that browned out under us).
_REPAINT_S = 2.0
_REFRESH_EVERY = 15


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def can_state(channel: str) -> str:
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def _bus_tag(channel: str, state: VehicleState) -> str:
    if not state.bus_active:
        return "QUIET"
    raw = can_state(channel)
    return _BUS_ABBR.get(raw, raw[:6])


def render_screen(state: VehicleState, *, bus_tag: str, status: str,
                  clock: str, uptime: str) -> list[str]:
    """Compose the four watch-screen rows. Pure -- easy to unit-test.

    Rows may be shorter than the panel width; :meth:`SerLcd.line` pads and
    clips to 20.
    """
    mode = state.drive_mode.value.upper() if state.drive_mode else "-- (no 1F4)"
    gear = _GEAR_LETTER.get(state.shift, "?")
    return [
        f"DMF {uptime:>6} {clock}",
        f"mode   {mode}",
        f"gear {gear:<4} bus {bus_tag}",
        status[:20],
    ]


class LcdDashboard:
    """Background thread that paints :func:`render_screen` on a real LCD.

    Parameters
    ----------
    state:      the shared :class:`VehicleState` the RX loop updates.
    status_fn:  ``() -> str`` the daemon uses to describe what it is doing.
    """

    def __init__(self, state: VehicleState, status_fn, *, channel: str = "can0",
                 port: str = "/dev/serial0", baud: int = 9600,
                 backlight: int = 45, interval: float = _REPAINT_S,
                 dry_run: bool = False) -> None:
        self._state = state
        self._status_fn = status_fn
        self._channel = channel
        self._lcd_kwargs = dict(port=port, baud=baud, backlight=backlight,
                                dry_run=dry_run)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lcd: SerLcd | None = None
        self._started = time.monotonic()
        self._yielded = False
        self._open_warned = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lcd-dash",
                                        daemon=True)
        self._thread.start()
        log.info("LCD watch screen thread started (port %s)",
                 self._lcd_kwargs["port"])

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._lcd is not None:
            try:
                self._lcd.lines("VOLT DMF", "daemon stopped", "", "")
            except OSError:
                pass
            self._close_lcd()

    # -- internals ---------------------------------------------------------
    def _open_lcd(self) -> bool:
        if self._lcd is not None:
            return True
        try:
            self._lcd = SerLcd(**self._lcd_kwargs).open()
            self._lcd.lines("VOLT DMF", "starting up...", "", "")
            self._open_warned = False
            return True
        except OSError as exc:
            if not self._open_warned:
                log.warning("LCD unavailable (%s); watch screen idle, retrying",
                            exc)
                self._open_warned = True
            self._lcd = None
            return False

    def _close_lcd(self) -> None:
        if self._lcd is not None:
            try:
                self._lcd.close()
            finally:
                self._lcd = None

    def _run(self) -> None:
        ticks = 0
        while not self._stop.is_set():
            try:
                self._tick(ticks)
            except Exception:  # never take the daemon down
                log.exception("LCD watch tick failed; backing off 5s")
                self._close_lcd()
                self._stop.wait(5.0)
            ticks += 1
            self._stop.wait(self._interval)

    def _tick(self, ticks: int) -> None:
        if lcdlock.is_held_by_other():
            if self._lcd is not None or not self._yielded:
                log.info("LCD claimed by another process; releasing the panel")
            self._close_lcd()
            self._yielded = True
            return
        if self._yielded:
            log.info("LCD free again; resuming the watch screen")
            self._yielded = False
        if not self._open_lcd():
            return
        rows = render_screen(
            self._state,
            bus_tag=_bus_tag(self._channel, self._state),
            status=self._status_fn(),
            clock=_dt.datetime.now().strftime("%H:%M:%S"),
            uptime=_fmt_uptime(time.monotonic() - self._started),
        )
        for i, text in enumerate(rows):
            self._lcd.line(i, text)
        if ticks % _REFRESH_EVERY == 0:
            self._lcd.refresh()
