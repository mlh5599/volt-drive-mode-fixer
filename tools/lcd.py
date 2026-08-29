#!/usr/bin/env python3
"""Drive the SparkFun serial 4x20 LCD -- selftest, one-off message, or a
live dashboard off ``can0`` for use while driving.

Read-only w.r.t. the CAN bus (it never transmits). Wiring and the board
command set are in ``voltdmf/lcd.py``. Default port ``/dev/serial0`` at
9600 baud -- the SparkFun factory default.

The ``--watch`` dashboard is meant to ride along with ``tools/drive_log.py``
on a drive: it shows a shared ``t+<seconds>`` clock so a voice memo of the
EV-range / battery-bar readout lines up with the log and the raw capture,
the committed drive mode, the ``can0`` state, and -- if you pass a candidate
``--soc-field`` -- that frame's raw value so you can watch it move against
the dash. Every ``--mark-every`` seconds it flashes a SAY prompt.

Examples:
  # confirm every row/column is alive (no CAN needed)
  ./lcd.py --selftest

  # park a message on the screen
  ./lcd.py --message "volt dmf\\nready"

  # ride-along dashboard, watching a SOC candidate frame
  ./lcd.py --watch --soc-field 0x3F1:2:2 --mark-every 90

  # see the layout with no hardware at all
  ./lcd.py --watch --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf.lcd import SerLcd  # noqa: E402

_STATE_ABBR = {
    "ERROR-ACTIVE": "ACTIVE", "ERROR-WARNING": "WARN",
    "ERROR-PASSIVE": "PASSIVE", "BUS-OFF": "BUS-OFF", "STOPPED": "STOPPED",
}


def can_state(channel: str) -> str:
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"can state (\S+)", out)
    return m.group(1) if m else "?"


def parse_field(spec: str):
    """'0x3F1:2:2[:le]' -> (id:int, off:int, width:int, little_endian:bool)."""
    m = re.fullmatch(r"(?:0x)?([0-9A-Fa-f]+):(\d+):(\d+)(?::(le|be))?", spec)
    if not m:
        raise argparse.ArgumentTypeError(
            "want ID:OFF:WIDTH[:le|be], e.g. 0x3F1:2:2 or 3F1:2:2:le")
    return (int(m.group(1), 16), int(m.group(2)), int(m.group(3)),
            m.group(4) == "le")


def latest_payload(bus, addr: int, timeout: float = 0.3) -> bytes | None:
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


def field_value(bus, field) -> int | None:
    addr, off, width, le = field
    data = latest_payload(bus, addr)
    if data is None or len(data) < off + width:
        return None
    return int.from_bytes(data[off:off + width], "little" if le else "big")


# -- modes -----------------------------------------------------------------
def do_selftest(lcd: SerLcd, seconds: float) -> None:
    lcd.clear()
    lcd.lines(
        "SparkFun 4x20  OK",
        "row1 ....:....1....:..",
        "row2  abcdefghijklmno",
        "row3  0123456789=+-*/",
    )
    print(lcd.render())
    # sweep a marker across row 0 so every column is proven
    end = time.time() + seconds
    pos = 0
    while time.time() < end:
        lcd.write_at(0, lcd.cols - 1, "*" if pos % 2 else " ")
        lcd.write_at(3, min(pos, lcd.cols - 1), "#")
        time.sleep(0.25)
        pos = (pos + 1) % lcd.cols
    lcd.lines("selftest done", "", "", "")
    print(lcd.render())


def do_message(lcd: SerLcd, text: str) -> None:
    text = text.replace("\\n", "\n")
    lcd.clear()
    lcd.lines(*text.split("\n"))
    print(lcd.render())


def do_watch(lcd: SerLcd, args) -> None:
    from voltdmf.canio import CanInterface

    t0 = time.time()
    next_mark = args.mark_every
    say_until = 0.0

    def paint(mode, cursor, state, soc):
        el = int(time.time() - t0)
        clock = _dt.datetime.now().strftime("%H:%M:%S")
        lcd.line(0, f"MODE {mode:<8} {_STATE_ABBR.get(state, state)[:6]:>6}")
        lcd.line(1, f"t+{el:>5}s   {clock}")
        if args.soc_field is not None:
            sv = "----" if soc is None else str(soc)
            addr, off = args.soc_field[0], args.soc_field[1]
            lcd.line(2, f"SOC {addr:03X}:{off}={sv:>10}")
        else:
            lcd.line(2, f"cursor {cursor}")
        if time.time() < say_until:
            lcd.line(3, ">> SAY: range + bars")
        else:
            lcd.line(3, f"sample {args.interval:.0f}s  mk {args.mark_every:.0f}s")
        if args.dry_run:
            print(lcd.render())

    if args.dry_run:
        for _ in range(3):
            paint("sport", "mountain", "ERROR-ACTIVE", 65280)
            time.sleep(args.interval)
        return

    with CanInterface(args.channel, dry_run=False) as can_if:
        bus = can_if._bus
        field = args.soc_field
        ticks = 0
        try:
            while True:
                ticks += 1
                if ticks % 15 == 0:
                    lcd.refresh()  # recover if the board reset under us
                now = time.time() - t0
                if args.mark_every > 0 and now >= next_mark:
                    say_until = time.time() + args.mark_hold
                    while next_mark <= now:
                        next_mark += args.mark_every
                m = can_if.read_drive_mode(timeout=1.0)
                cur = can_if.read_menu_cursor(timeout=0.3)
                soc = field_value(bus, field) if field is not None else None
                paint(m.value if m else "?", cur.value if cur else "-",
                      can_state(args.channel), soc)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            lcd.lines("watch stopped", "", "", "")
            print("\nstopped")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selftest", action="store_true",
                      help="write a known pattern + sweep a marker")
    mode.add_argument("--message", metavar="TEXT",
                      help="show TEXT (\\n splits rows) and exit")
    mode.add_argument("--watch", action="store_true",
                      help="live dashboard off can0 (ride-along with drive_log)")

    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--backlight", type=int, default=60, metavar="PCT",
                    help="primary backlight 0..100 (default 60; drop it if "
                         "the board keeps resetting -- that is a power sag)")
    ap.add_argument("--boot-wait", type=float, default=1.2, metavar="SECS",
                    help="pause after opening the port before the first write, "
                         "to clear the LCD's power-on splash (default 1.2)")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="--watch: seconds between refreshes (default 2)")
    ap.add_argument("--soc-field", type=parse_field, default=None,
                    metavar="ID:OFF:WIDTH[:le]",
                    help="--watch: show this raw frame field instead of the "
                         "menu cursor")
    ap.add_argument("--mark-every", type=float, default=90.0, metavar="SECS",
                    help="--watch: flash a SAY prompt this often (0 = off)")
    ap.add_argument("--mark-hold", type=float, default=8.0, metavar="SECS",
                    help="--watch: how long the SAY prompt stays up (default 8)")
    ap.add_argument("--selftest-seconds", type=float, default=6.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="no serial port, no CAN: just print the screen image")
    args = ap.parse_args()

    lcd = SerLcd(args.port, args.baud, backlight=args.backlight,
                 boot_wait=args.boot_wait, dry_run=args.dry_run)
    try:
        lcd.open()
    except OSError as exc:
        sys.exit(f"cannot open {args.port}: {exc}\n"
                 f"(enable the UART, free it from the serial console, and "
                 f"check wiring -- see voltdmf/lcd.py)")
    try:
        if args.selftest:
            do_selftest(lcd, args.selftest_seconds)
        elif args.message is not None:
            do_message(lcd, args.message)
        else:
            do_watch(lcd, args)
    finally:
        lcd.close()


if __name__ == "__main__":
    main()
