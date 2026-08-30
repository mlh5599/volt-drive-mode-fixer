#!/usr/bin/env python3
"""Bench check for the two SOC-log panel buttons. Touches no CAN, no LCD.

Same wiring soc_log.py expects: the two PiCAN2 switch pads (SW1 / SW2),
each a momentary button from a BCM pin to GND, internal pull-ups, 50 ms
debounce.

    A = gauge-down   BCM 24   (PiCAN2 SW1)
    B = gauge-up     BCM 23   (PiCAN2 SW2)

Prints an event line on every press and release with a running count, and
watches for the "hold BOTH" stop gesture. Runs for --seconds then prints a
summary; Ctrl-C stops early.

    ./button_check.py                 # 30 s, BCM 24 / 23
    ./button_check.py --seconds 60
    ./button_check.py --scan          # find which pins the buttons are on
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time


def _stamp() -> str:
    return f"{_dt.datetime.now():%H:%M:%S.%f}"[:-3]


# BCM lines safe to probe as inputs on a PiCAN2 rig: skip I2C (2,3), the
# SPI/CAN pins the PiCAN2 uses (7,8,9,10,11,25), and the UART (14,15).
_SCAN_PINS = [4, 5, 6, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 26, 27]


def _scan(seconds: float) -> None:
    from gpiozero import Button  # noqa: PLC0415

    held = []
    for pin in _SCAN_PINS:
        try:
            held.append((pin, Button(pin, pull_up=True, bounce_time=0.02)))
        except Exception:  # noqa: BLE001  (pin in use / unavailable)
            continue
    print(f"{_stamp()}  scan: watching BCM {[p for p, _ in held]}")
    print(f"{_stamp()}  press each button now; any pin that dips to GND prints")
    seen = set()
    for pin, btn in held:
        btn.when_pressed = (lambda p=pin: (
            seen.add(p),
            print(f"{_stamp()}  BCM {p}  PRESS  <-- this pin is wired to a button",
                  flush=True)))
        btn.when_released = (lambda p=pin:
                             print(f"{_stamp()}  BCM {p}  release", flush=True))
    end = time.time() + seconds
    try:
        while time.time() < end:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    for _, btn in held:
        btn.close()
    print(f"{_stamp()}  scan done; button pins seen: "
          f"{sorted(seen) if seen else 'NONE (check the GND leg / solder joints)'}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--button-a-gpio", type=int, default=24, metavar="BCM")
    ap.add_argument("--button-b-gpio", type=int, default=23, metavar="BCM")
    ap.add_argument("--bounce-ms", type=float, default=50.0)
    ap.add_argument("--stop-hold", type=float, default=3.0, metavar="SECS",
                    help="report when BOTH are held this long (0 = off)")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="run this long then summarize (default 30)")
    ap.add_argument("--scan", action="store_true",
                    help="ignore --button-*-gpio: watch every free BCM line "
                         "and report any that change (finds miswired pins)")
    args = ap.parse_args()

    if args.scan:
        _scan(args.seconds)
        return

    try:
        from gpiozero import Button
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"gpiozero unavailable: {exc}")

    try:
        a = Button(args.button_a_gpio, pull_up=True,
                   bounce_time=args.bounce_ms / 1000.0)
        b = Button(args.button_b_gpio, pull_up=True,
                   bounce_time=args.bounce_ms / 1000.0)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"could not claim GPIO {args.button_a_gpio}/"
                 f"{args.button_b_gpio}: {exc}")

    counts = {"A": 0, "B": 0}

    def on_press(name: str):
        def _cb() -> None:
            counts[name] += 1
            print(f"{_stamp()}  {name} PRESS   (A={counts['A']} B={counts['B']})",
                  flush=True)
        return _cb

    def on_release(name: str):
        def _cb() -> None:
            print(f"{_stamp()}  {name} release", flush=True)
        return _cb

    a.when_pressed = on_press("A")
    b.when_pressed = on_press("B")
    a.when_released = on_release("A")
    b.when_released = on_release("B")

    print(f"{_stamp()}  ready -- BCM {args.button_a_gpio} = A (gauge-down), "
          f"BCM {args.button_b_gpio} = B (gauge-up)")
    print(f"{_stamp()}  press each button a few times; hold BOTH for "
          f"{args.stop_hold:.0f}s to test the stop gesture")
    print(f"{_stamp()}  idle levels: A={'up' if not a.is_pressed else 'DOWN'} "
          f"B={'up' if not b.is_pressed else 'DOWN'}  "
          f"(both should read 'up' at rest)")

    both_since: float | None = None
    stop_seen = False
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            time.sleep(0.05)
            if args.stop_hold > 0 and a.is_pressed and b.is_pressed:
                now = time.time()
                if both_since is None:
                    both_since = now
                elif not stop_seen and now - both_since >= args.stop_hold:
                    stop_seen = True
                    print(f"{_stamp()}  BOTH held {args.stop_hold:.0f}s -> "
                          f"stop gesture OK", flush=True)
            else:
                both_since = None
    except KeyboardInterrupt:
        print(f"\n{_stamp()}  interrupted")

    a.close()
    b.close()
    print(f"{_stamp()}  summary: A pressed {counts['A']}x, "
          f"B pressed {counts['B']}x, stop gesture "
          f"{'seen' if stop_seen else 'not tested'}")


if __name__ == "__main__":
    main()
