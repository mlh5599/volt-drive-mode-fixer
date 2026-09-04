"""The idle *watch* screen the daemon paints on the LCD.

When the daemon is up and nothing else has claimed the panel (see
:mod:`voltdmf.lcdlock`), a background thread mirrors a small status screen
to the SparkFun 4x20:

    +--------------------+
    |DMF C-  SOC   84.3% |
    |sel 1/4 HOLD-SOC    |
    |gear D  GM MOUNTAIN |
    |on-start -> MOUNTAIN |
    +--------------------+

Row 0  a live bus-state glyph (see :func:`bus_icon`) + diag SOC % from the
       22 005B poll -- the one number the instrument cluster never shows.
       A trailing ``~`` marks a % that is not a live poll reading (poll
       gone stale, or the coarse bar failsafe took over).
Row 1  the VDMF selector: which of the four SW1 detents is live, and
       whether the SOC-HOLD floor has latched on top of it.
Row 2  committed drive mode (0x1F4 byte 1) + PRNDL gear (0x1F5 byte 3).
Row 3  a one-line status the daemon supplies (what it is armed to do /
       the last switch it requested).

Everything here is fail-soft: the thread never raises into the daemon, a
missing / unusable serial port just means the screen stays dark, and a
purpose-driven tool taking :func:`voltdmf.lcdlock.hold` makes the thread
close the port and wait rather than fight for it.
"""

from __future__ import annotations

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

#: SocketCAN "can state <x>" -> a tag :func:`bus_icon` knows how to draw.
_BUS_ABBR = {
    "ERROR-ACTIVE": "ACTIVE", "ERROR-WARNING": "WARN",
    "ERROR-PASSIVE": "PASV", "BUS-OFF": "BUSOFF", "STOPPED": "STOP",
}
_GEAR_LETTER = {
    ShiftPosition.PARK: "P", ShiftPosition.REVERSE: "R",
    ShiftPosition.NEUTRAL: "N", ShiftPosition.DRIVE: "D",
    ShiftPosition.LOW: "L", ShiftPosition.UNKNOWN: "?",
}

#: 2-char glyph for every bus tag except ACTIVE, which spins instead (see
#: bus_icon). WARN/BUSOFF also blink -- a fault should catch the eye, not
#: blend into the row.
_BUS_GLYPH = {
    "WARN": "W!", "PASV": "P~", "BUSOFF": "X!", "STOP": "ST", "QUIET": "Qz",
}
_SPINNER = "-\\|/"
_BLINKING_TAGS = ("WARN", "BUSOFF")

#: How often the thread repaints, and how often it forces a full refresh
#: (to recover a panel that browned out under us).
_REPAINT_S = 2.0
_REFRESH_EVERY = 15


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


def bus_icon(tag: str, ticks: int) -> str:
    """2-3 char glyph for a bus tag from :func:`_bus_tag`.

    ACTIVE spins a new frame every tick -- a heartbeat that a hung watch
    thread would visibly stop, unlike static text. WARN/BUSOFF blink instead,
    so a fault is something you notice, not read.
    """
    if tag == "ACTIVE":
        return "C" + _SPINNER[ticks % len(_SPINNER)]
    glyph = _BUS_GLYPH.get(tag, (tag or "?")[:2])
    if tag in _BLINKING_TAGS and ticks % 2:
        return "  "
    return glyph


def _soc_label(state: VehicleState) -> str:
    if state.soc_percent is None:
        return "--"
    return f"{state.soc_percent:.1f}%"


def render_screen(state: VehicleState, *, bus_icon: str, position_label: str,
                  position_index: int, cycle_len: int, floor_latched: bool,
                  soc_fresh: bool, status: str) -> list[str]:
    """Compose the four watch-screen rows. Pure -- easy to unit-test.

    ``bus_icon`` is a short string, not the :func:`bus_icon` function --
    the caller resolves the tag and animation tick before calling this.
    ``soc_fresh`` marks the displayed % with ``~`` when it is not a live
    poll reading (stale poll, or the coarse bar failsafe took over) -- the
    number stays on screen either way, but a stale one should not be trusted
    the way a live one is.
    """
    mode = state.drive_mode.value.upper() if state.drive_mode else "no 1F4"
    gear = _GEAR_LETTER.get(state.shift, "?")
    sel = f"sel {position_index}/{cycle_len} {position_label.upper()}"
    if floor_latched:
        sel += " FLR"
    stale_mark = "" if (state.soc_percent is None or soc_fresh) else "~"
    rows = [
        f"DMF {bus_icon:<3} SOC {_soc_label(state):>6}{stale_mark}",
        sel,
        f"gear {gear}  GM {mode}",
        status,
    ]
    return [r[:20] for r in rows]


def _default_selector() -> dict:
    return {"label": "--", "index": 0, "floor_latched": False,
            "soc_fresh": False}


class LcdDashboard:
    """Background thread that paints :func:`render_screen` on a real LCD.

    Parameters
    ----------
    state:        the shared :class:`VehicleState` the RX loop updates.
    status_fn:    ``() -> str`` the daemon uses to describe what it is doing.
    selector_fn:  ``() -> dict`` with ``label``, ``index``, ``floor_latched``
                  -- the daemon's live reconciler position. Defaults to a
                  placeholder so callers that don't care (most tests) don't
                  have to supply one.
    """

    def __init__(self, state: VehicleState, status_fn, *, selector_fn=None,
                 channel: str = "can0", port: str = "/dev/serial0",
                 baud: int = 9600, backlight: int = 45,
                 interval: float = _REPAINT_S, dry_run: bool = False) -> None:
        self._state = state
        self._status_fn = status_fn
        self._selector_fn = selector_fn or _default_selector
        self._channel = channel
        self._lcd_kwargs = dict(port=port, baud=baud, backlight=backlight,
                                dry_run=dry_run)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lcd: SerLcd | None = None
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
        sel = self._selector_fn()
        rows = render_screen(
            self._state,
            bus_icon=bus_icon(_bus_tag(self._channel, self._state), ticks),
            position_label=sel["label"],
            position_index=sel["index"],
            cycle_len=sel.get("cycle_len", 4),
            floor_latched=sel["floor_latched"],
            soc_fresh=sel.get("soc_fresh", False),
            status=self._status_fn(),
        )
        for i, text in enumerate(rows):
            self._lcd.line(i, text)
        if ticks % _REFRESH_EVERY == 0:
            self._lcd.refresh()
