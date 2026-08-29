#!/usr/bin/env python3
"""Offline miner for a `candump -l` capture (see tools/drive_log.py).

Read-only. No CAN hardware, stdlib only. Point it at one or more
``candump-*.log`` files and let it find the signals the parked car could
not confirm:

  * SOC / battery energy -- ``--monotonic``. This dash shows no %, so we
    cannot decode SOC directly; instead scan every id/offset/width/endianness
    for a field whose value only ever trends one way across the drive (the
    battery went from near-full to well-down). Ranked by how cleanly it is
    monotonic times how much of its range it swept. Cross-check the top hits
    against your voice-memo EV-range / battery-bar narration and the
    ``SOC-MARK`` offsets in the drive log.

  * Shifter / PRNDL -- ``--shift-window START END``. Over the parked
    ``--shift-routine`` phase (offsets in seconds from the first frame, or
    HH:MM:SS), list ids that took on a small number of discrete payloads and
    print every transition with its timestamp. Line them up with the
    P->R->N->D->L->P order you said out loud.

  * ``--series ID:OFF:WIDTH`` -- dump one candidate field as a timestamped
    value series (``:le`` for little-endian) to eyeball or plot.

  * ``--ids`` -- inventory: every id, frame count, rate, distinct payloads,
    which bytes ever change.

Offsets/times are relative to the first frame across all files given, so
pass the files in order (shell glob sorts correctly for the timestamped
names).

Examples:
  ./mine_capture.py ~/candump-*.log --ids
  ./mine_capture.py ~/candump-*.log --monotonic --top 15
  ./mine_capture.py ~/candump-*.log --shift-window 0 80
  ./mine_capture.py ~/candump-*.log --series 0x1F4:1:1 --every 5
"""

from __future__ import annotations

import argparse
import re
import sys

_LINE = re.compile(
    r"\((?P<ts>\d+\.\d+)\)\s+(?P<if>\S+)\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-fRr]*)"
)


class Frame:
    __slots__ = ("t", "id", "data")

    def __init__(self, t: float, ident: int, data: bytes) -> None:
        self.t = t
        self.id = ident
        self.data = data


def parse(paths: list[str]) -> list[Frame]:
    frames: list[Frame] = []
    for p in paths:
        try:
            fh = open(p)
        except OSError as exc:
            sys.exit(f"cannot open {p}: {exc}")
        with fh:
            for ln in fh:
                m = _LINE.search(ln)
                if not m:
                    continue
                raw = m.group("data")
                if not raw or raw[0] in "Rr" or len(raw) % 2:
                    continue
                frames.append(Frame(float(m.group("ts")),
                                    int(m.group("id"), 16),
                                    bytes.fromhex(raw)))
    frames.sort(key=lambda f: f.t)
    if not frames:
        sys.exit("no usable frames parsed -- wrong file format?")
    t0 = frames[0].t
    for f in frames:
        f.t -= t0
    return frames


def _by_id(frames: list[Frame]) -> dict[int, list[Frame]]:
    out: dict[int, list[Frame]] = {}
    for f in frames:
        out.setdefault(f.id, []).append(f)
    return out


def _parse_time(s: str) -> float:
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        h, m, sec = parts
        return h * 3600 + m * 60 + sec
    return float(s)


# -- --ids -----------------------------------------------------------------
def cmd_ids(frames: list[Frame]) -> None:
    span = frames[-1].t or 1.0
    print(f"{len(frames)} frames over {span:.1f}s\n")
    print(f"{'id':>6}  {'count':>7}  {'Hz':>6}  {'dlc':>3}  {'uniq':>5}  changing bytes")
    for ident, fs in sorted(_by_id(frames).items()):
        payloads = {f.data for f in fs}
        dlc = len(fs[0].data)
        changing = []
        for i in range(dlc):
            vals = {f.data[i] for f in fs if len(f.data) > i}
            if len(vals) > 1:
                changing.append(i)
        print(f"{ident:>6X}  {len(fs):>7}  {len(fs) / span:>6.1f}  {dlc:>3}  "
              f"{len(payloads):>5}  {changing}")


# -- --monotonic ---------------------------------------------------------
def _fields(dlc: int, maxw: int):
    for width in range(1, maxw + 1):
        for off in range(0, dlc - width + 1):
            for le in (False, True):
                if width == 1 and le:
                    continue
                yield off, width, le


def _val(data: bytes, off: int, width: int, le: bool) -> int | None:
    if len(data) < off + width:
        return None
    return int.from_bytes(data[off:off + width], "little" if le else "big")


def cmd_monotonic(frames: list[Frame], bucket: float, top: int,
                  min_span: float) -> None:
    total_span = frames[-1].t
    results = []
    for ident, fs in _by_id(frames).items():
        dlc = max(len(f.data) for f in fs)
        # bucket to one sample per `bucket` seconds (last value wins)
        buckets: dict[int, bytes] = {}
        for f in fs:
            buckets[int(f.t // bucket)] = f.data
        ordered = [buckets[k] for k in sorted(buckets)]
        if len(ordered) < 5:
            continue
        for off, width, le in _fields(dlc, 2):
            series = [_val(d, off, width, le) for d in ordered]
            series = [v for v in series if v is not None]
            if len(series) < 5:
                continue
            lo, hi = min(series), max(series)
            if hi == lo:
                continue
            full = (1 << (8 * width)) - 1
            swept = (hi - lo) / full
            if swept < min_span:
                continue
            deltas = [b - a for a, b in zip(series, series[1:])]
            down = sum(1 for d in deltas if d <= 0) / len(deltas)
            up = sum(1 for d in deltas if d >= 0) / len(deltas)
            mono, direction = (down, "DOWN") if down >= up else (up, "UP")
            if mono < 0.75:
                continue
            # net move as a fraction of the total excursion -- punishes
            # fields that wander back
            net = abs(series[-1] - series[0]) / (hi - lo)
            score = mono * net * (0.5 + swept / 2)
            results.append((score, ident, off, width, le, direction, mono,
                            swept, net, series[0], series[-1]))
    results.sort(reverse=True)
    print(f"monotonic-field scan  (bucket={bucket:.0f}s, {total_span:.0f}s total, "
          f"score = mono-frac x net-move x span)\n")
    print(f"{'score':>6}  {'id':>5} {'off':>3} {'w':>1} {'end':>3}  {'dir':>4}  "
          f"{'mono':>5} {'span':>5} {'net':>4}   first -> last")
    for r in results[:top]:
        (score, ident, off, width, le, direction, mono, swept, net,
         v0, v1) = r
        end = "le" if le else "be"
        print(f"{score:>6.3f}  {ident:>5X} {off:>3} {width:>1} {end:>3}  "
              f"{direction:>4}  {mono:>5.2f} {swept:>5.2f} {net:>4.2f}   "
              f"{v0} -> {v1}")
    if not results:
        print("(nothing cleared the thresholds -- loosen --min-span or "
              "--mono, or take a bigger battery swing next drive)")


# -- --shift-window ----------------------------------------------------
def cmd_shift(frames: list[Frame], start: float, end: float,
              max_states: int) -> None:
    win = [f for f in frames if start <= f.t <= end]
    if not win:
        sys.exit(f"no frames in window {start}..{end}s "
                 f"(capture is {frames[-1].t:.0f}s long)")
    print(f"discrete-state scan  window {start:.0f}..{end:.0f}s  "
          f"({len(win)} frames)\n")
    hits = []
    for ident, fs in sorted(_by_id(win).items()):
        payloads = [f.data for f in fs]
        distinct = list(dict.fromkeys(payloads))
        if not 2 <= len(distinct) <= max_states:
            continue
        # per-byte: which bytes carry the state
        dlc = len(payloads[0])
        state_bytes = [i for i in range(dlc)
                       if len({p[i] for p in payloads if len(p) > i}) > 1]
        hits.append((ident, distinct, state_bytes, fs))
    if not hits:
        print(f"(no id had between 2 and {max_states} distinct payloads here)")
        return
    for ident, distinct, state_bytes, fs in hits:
        print(f"id {ident:X}  -- {len(distinct)} states, "
              f"changing bytes {state_bytes}")
        prev = None
        for f in fs:
            key = tuple(f.data[i] for i in state_bytes)
            if key != prev:
                print(f"    +{f.t:7.2f}s   {f.data.hex(' ')}")
                prev = key
        print()


# -- --series --------------------------------------------------------
def cmd_series(frames: list[Frame], spec: str, every: float) -> None:
    m = re.fullmatch(r"(?:0x)?([0-9A-Fa-f]+):(\d+):(\d+)(?::(le|be))?", spec)
    if not m:
        sys.exit("--series wants ID:OFF:WIDTH[:le|be], e.g. 0x1F4:1:1 or 206:2:2:le")
    ident = int(m.group(1), 16)
    off, width = int(m.group(2)), int(m.group(3))
    le = m.group(4) == "le"
    fs = _by_id(frames).get(ident)
    if not fs:
        sys.exit(f"id {ident:X} not in capture")
    print(f"id {ident:X}  bytes[{off}:{off + width}] {'LE' if le else 'BE'}  "
          f"every ~{every:.0f}s\n")
    nxt = 0.0
    for f in fs:
        if f.t < nxt:
            continue
        v = _val(f.data, off, width, le)
        if v is None:
            continue
        print(f"  +{f.t:8.2f}s   {v:>10}   0x{v:X}   [{f.data.hex(' ')}]")
        nxt = f.t + every


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", help="candump -l file(s), in time order")
    ap.add_argument("--ids", action="store_true",
                    help="inventory every arbitration id")
    ap.add_argument("--monotonic", action="store_true",
                    help="rank id/offset/width fields by how cleanly they "
                         "trend one way (SOC hunt)")
    ap.add_argument("--shift-window", nargs=2, metavar=("START", "END"),
                    help="discrete-state scan between two times (s or HH:MM:SS)")
    ap.add_argument("--series", metavar="ID:OFF:WIDTH[:le|be]",
                    help="dump one field as a timestamped value series")
    ap.add_argument("--bucket", type=float, default=10.0,
                    help="--monotonic: seconds per sample bucket (default 10)")
    ap.add_argument("--top", type=int, default=20,
                    help="--monotonic: rows to print (default 20)")
    ap.add_argument("--min-span", type=float, default=0.02,
                    help="--monotonic: min fraction of full range swept "
                         "(default 0.02)")
    ap.add_argument("--max-states", type=int, default=12,
                    help="--shift-window: max distinct payloads to count as "
                         "a state machine (default 12)")
    ap.add_argument("--every", type=float, default=2.0,
                    help="--series: min seconds between printed rows (default 2)")
    args = ap.parse_args()

    frames = parse(args.logs)

    did = False
    if args.ids:
        cmd_ids(frames)
        did = True
    if args.monotonic:
        cmd_monotonic(frames, args.bucket, args.top, args.min_span)
        did = True
    if args.shift_window:
        s = _parse_time(args.shift_window[0])
        e = _parse_time(args.shift_window[1])
        cmd_shift(frames, s, e, args.max_states)
        did = True
    if args.series:
        cmd_series(frames, args.series, args.every)
        did = True
    if not did:
        cmd_ids(frames)


if __name__ == "__main__":
    main()
