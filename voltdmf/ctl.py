"""``voltdmf-ctl`` -- talk to a running daemon over its control socket.

    voltdmf-ctl status
    voltdmf-ctl set-mode hold [--force]
    voltdmf-ctl setpoint hold | mountain
    voltdmf-ctl arm
    voltdmf-ctl disarm
    voltdmf-ctl reload

Socket path: ``--socket`` > ``$VOLTDMF_CONTROL_SOCKET`` > the systemd default
(:data:`voltdmf.control.DEFAULT_SOCKET_PATH`).

Exit codes: ``0`` ok, ``1`` the daemon replied ``ok: false``, ``2`` bad usage,
``3`` could not reach the daemon (not running / socket missing / connrefused).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from .config import SETPOINT_MODES
from .control import DEFAULT_SOCKET_PATH
from .signals import DriveMode

_MODES = [m.value for m in DriveMode]
_SETPOINTS = [m.value for m in SETPOINT_MODES]
_CONNECT_TIMEOUT_S = 3.0
_REPLY_TIMEOUT_S = 25.0   # > the server's own reply timeout


class _Unreachable(Exception):
    """The daemon socket could not be reached."""


def _roundtrip(path: str, request: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_CONNECT_TIMEOUT_S)
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(_REPLY_TIMEOUT_S)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise _Unreachable(
            f"voltdmf is not accepting on {path} ({exc.__class__.__name__}); "
            "is the service running?"
        ) from exc
    except (socket.timeout, OSError) as exc:
        raise _Unreachable(f"control connection to {path} failed: {exc}") from exc

    line = bytes(buf).split(b"\n", 1)[0].strip()
    if not line:
        raise _Unreachable("daemon closed the connection without replying")
    try:
        reply = json.loads(line)
    except ValueError as exc:
        raise _Unreachable(f"unparseable reply from daemon: {exc}") from exc
    if not isinstance(reply, dict):
        raise _Unreachable("daemon reply was not a JSON object")
    return reply


def _default_socket() -> str:
    """Socket path when ``--socket`` is not given: env override, else the
    systemd default. Resolved per call so tests can set the env var."""
    return os.environ.get("VOLTDMF_CONTROL_SOCKET", DEFAULT_SOCKET_PATH)


def _build_parser() -> argparse.ArgumentParser:
    # --socket / --json are carried on a shared parent so they parse the same
    # whether they land before or after the subcommand. default=SUPPRESS keeps
    # the subparser copy from clobbering a value the top-level parser already
    # set (the classic argparse subparser-defaults gotcha); main() fills the
    # real defaults in with getattr().
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--socket", default=argparse.SUPPRESS,
                        help="control socket path (default: "
                             f"$VOLTDMF_CONTROL_SOCKET or {DEFAULT_SOCKET_PATH})")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the raw JSON reply instead of a summary")

    ap = argparse.ArgumentParser(prog="voltdmf-ctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show daemon + vehicle state", parents=[common])
    sm = sub.add_parser("set-mode", help="request a drive-mode switch now",
                        parents=[common])
    sm.add_argument("mode", choices=_MODES)
    sm.add_argument("--force", action="store_true",
                    help="walk the menu even if already reading that mode")
    sp = sub.add_parser("setpoint",
                        help="move the HOLD<->MOUNTAIN setpoint the reconciler holds",
                        parents=[common])
    sp.add_argument("mode", choices=_SETPOINTS)
    sub.add_parser("arm", help="allow transmission (the daemon boots armed)",
                   parents=[common])
    sub.add_parser("disarm", help="suppress transmission; keep reading/evaluating",
                   parents=[common])
    sub.add_parser("reload",
                   help="re-read the config file and rebuild the reconciler "
                        "(keeps the setpoint, drops the SOC-floor latch)",
                   parents=[common])
    return ap


def _request_for(args: argparse.Namespace) -> dict:
    req: dict = {"cmd": args.cmd}
    if args.cmd == "set-mode":
        req["mode"] = args.mode
        req["force"] = args.force
    elif args.cmd == "setpoint":
        req["mode"] = args.mode
    return req


def _print_status(state: dict) -> None:
    def g(key: str, default: str = "?") -> str:
        val = state.get(key, default)
        return default if val is None else str(val)

    tx = "ARMED" if state.get("transmit_enabled") else "disarmed"
    print(f"transmit:   {tx}")
    sp = g("setpoint")
    floor = "  [SOC-HOLD floor latched]" if state.get("floor_latched") else ""
    print(f"setpoint:   {sp}{floor}")
    print(f"drive mode: {g('drive_mode')}"
          + (f"  (manual override -> {state['manual_override']})"
             if state.get("manual_override") else ""))
    print(f"shift:      {g('shift')}")
    soc_line = f"soc:        {g('soc_percent')}%"
    extras = []
    if state.get("soc_source"):
        extras.append(f"src={state['soc_source']}")
    if state.get("soc_age_s") is not None:
        extras.append(f"age={state['soc_age_s']}s")
    if state.get("soc_bar_raw") is not None:
        extras.append(f"b3={state['soc_bar_raw']}")
    if extras:
        soc_line += "   (" + ", ".join(extras) + ")"
    print(soc_line)
    if state.get("uds_replies") is not None:
        print(f"uds poll:   {g('uds_replies')} ok / {g('uds_nrcs')} nrc")
    if state.get("speed_mph") is not None:
        print(f"speed:      {g('speed_mph')} mph")
    print(f"bus:        {'active' if state.get('bus_active') else 'quiet'}")
    cd = state.get("cooldown_remaining_s")
    if cd:
        print(f"cooldown:   {cd}s left")
    if state.get("last_action"):
        print(f"last action:{state['last_action']}")
    if state.get("uptime_s") is not None:
        print(f"uptime:     {state['uptime_s']}s")


def _print_human(cmd: str, reply: dict) -> None:
    if not reply.get("ok"):
        print(f"error: {reply.get('error', 'unknown error')}", file=sys.stderr)
        if reply.get("would_switch_to"):
            print(f"  (would switch to {reply['would_switch_to']} once armed)",
                  file=sys.stderr)
        return
    if cmd == "status":
        _print_status(reply.get("state", {}))
    elif cmd == "set-mode":
        print(reply.get("result", "ok"))
    elif cmd == "setpoint":
        print(f"setpoint = {reply.get('setpoint', '?')}")
    elif cmd in ("arm", "disarm"):
        print(f"armed={reply.get('armed')}")
    elif cmd == "reload":
        print(f"reloaded; setpoint = {reply.get('setpoint', '?')} "
              f"(SOC-floor latch cleared)")
    else:
        print("ok")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    socket_path = getattr(args, "socket", None) or _default_socket()
    want_json = getattr(args, "json", False)
    try:
        reply = _roundtrip(socket_path, _request_for(args))
    except _Unreachable as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if want_json:
        print(json.dumps(reply))
    else:
        _print_human(args.cmd, reply)
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
