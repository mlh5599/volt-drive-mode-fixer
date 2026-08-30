"""``python -m voltdmf`` entrypoint."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .config import ConfigError, load_config
from .daemon import Daemon


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="voltdmf", description=__doc__)
    p.add_argument("--config", required=True, help="path to config YAML")
    p.add_argument("--channel", default="can0", help="SocketCAN channel (default: can0)")
    p.add_argument(
        "--dry-run", action="store_true",
        help="read and evaluate against the live bus but transmit nothing",
    )
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    ctl = p.add_argument_group("runtime control socket")
    ctl.add_argument(
        "--control-socket", metavar="PATH", default=None,
        help="bind the control socket at PATH (bench use). Under systemd the "
             "socket is passed by voltdmf.socket and this is not needed.",
    )
    ctl.add_argument(
        "--no-control", dest="control", action="store_false",
        help="disable the runtime control socket entirely",
    )
    ctl.add_argument(
        "--armed", action="store_true",
        help="start with transmission enabled instead of disarmed (ignored "
             "under --dry-run)",
    )
    p.set_defaults(control=True)

    lcd = p.add_argument_group("LCD watch screen")
    lcd.add_argument("--no-lcd", dest="lcd", action="store_false",
                     help="do not drive the SparkFun LCD watch screen")
    lcd.add_argument("--lcd-port", default="/dev/serial0",
                     help="serial port for the LCD (default: /dev/serial0)")
    lcd.add_argument("--lcd-baud", type=int, default=9600)
    lcd.add_argument("--lcd-backlight", type=int, default=45, metavar="PCT",
                     help="LCD backlight 0..100 (default 45; drop it if the "
                          "panel resets under a sagging 5V feed)")
    p.set_defaults(lcd=True)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    daemon = Daemon(config, channel=args.channel, dry_run=args.dry_run,
                    lcd=args.lcd, lcd_port=args.lcd_port, lcd_baud=args.lcd_baud,
                    lcd_backlight=args.lcd_backlight,
                    control_enabled=args.control,
                    control_socket_path=args.control_socket,
                    config_path=args.config,
                    start_armed=args.armed)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: daemon.request_stop())

    try:
        daemon.run()
    except OSError as exc:
        print(f"CAN interface '{args.channel}' unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
