#!/usr/bin/env python3
"""Replay a candump capture through the shipped engine decoders.

Read-only, stdlib only (plus ``voltdmf.signals``). This is the offline check
behind ``docs/analysis/session12-engine-signal.md``: it re-derives, from any
capture, the evidence that ``0x4C5`` byte 2 and the ``0x3F9`` byte-1/2 run
counter say the same thing about the range-extender.

Per capture it prints:

* every ``0x4C5`` byte-2 value with the state ``decode_engine_state`` gives
  it -- an unmapped value shows as ``UNKNOWN`` and is the thing to look for
  on a new car or a new capture;
* the ``0x3F9`` frame count, its byte-0 histogram (the byte deliberately
  *not* read into the counter -- if it ever moves, revisit that decision),
  how often the counter stepped, and the step rate;
* a merged engine timeline: contiguous runs of "off" / "running", with the
  committed drive mode from ``0x1F4`` byte 1 at each transition. That last
  column is the whole point -- an engine start with the mode still ``normal``
  is the state where the centre stack stops offering HOLD.
* the two ways the signals disagree, timed separately (sampled on the 0x3F9
  frame, ~4 Hz): time with the counter moving while 0x4C5 still reads off
  (the 33-42 s lead-in at the head of an engine leg) and time with 0x4C5
  running while the counter is frozen (overrun fuel cut and standstill,
  mid-leg). Both are expected; they are why VehicleState reads the two as a
  union rather than trusting either alone.

Usage:
  tools/engine_check.py captures/candump-*.log [--since 1788198000]

``--since`` drops frames before an epoch, for a capture whose head carries
stale pre-NTP timestamps (session 9 needs ``--since 1788198000``).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voltdmf import signals  # noqa: E402
from voltdmf.signals import EngineState  # noqa: E402

#: 0x1F4 -- taken from SIGNAL_IDS rather than canio.MODE_STATUS_ADDR so this
#: stays importable without python-can installed.
MODE_STATUS_ADDR = signals.SIGNAL_IDS["drive_mode_status"].addr

LINE_RE = re.compile(r"^\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)")

#: How long the counter may sit still before this report calls it stopped.
#: Matches ``voltdmf.state.ENGINE_COUNTER_IDLE_S``; kept as a plain number so
#: the tool stays usable against an older checkout of the package.
IDLE_S = 3.0


class _Report:
    def __init__(self) -> None:
        self.b2 = Counter()          # 0x4C5 byte 2
        self.b0_3f9 = Counter()      # 0x3F9 byte 0 -- expected all 0x00
        self.dlc_3f9 = Counter()
        self.frames_3f9 = 0
        self.deltas = Counter()
        self.rates: list[float] = []
        self.runs: list[tuple[float, float, str, str | None]] = []
        self.lead_in_s = 0.0    # counter moving, 0x4C5 off
        self.no_fuel_s = 0.0    # 0x4C5 running, counter frozen
        self.mode: str | None = None

def _scan(path: str, since: float | None) -> _Report:
    r = _Report()
    prev_val: int | None = None
    prev_t = 0.0
    last_move = float("-inf")
    last_3f9 = 0.0
    state = EngineState.UNKNOWN
    run_state: str | None = None
    run_start = 0.0
    last_t = 0.0

    def merged(t: float) -> str:
        """What the pair says at ``t``: the same rule VehicleState uses."""
        advancing = (t - last_move) <= IDLE_S
        if state is EngineState.UNKNOWN:
            return "running" if advancing else "unknown"
        if state is EngineState.OFF:
            return "running" if advancing else "off"
        return "running"

    with open(path, errors="ignore") as fh:
        for line in fh:
            m = LINE_RE.match(line)
            if not m:
                continue
            t = float(m.group(1))
            if since is not None and t < since:
                continue
            addr = int(m.group(2), 16)
            data = bytes.fromhex(m.group(3))
            last_t = t
            if addr == MODE_STATUS_ADDR and len(data) > 1:
                mode = signals.decode_drive_mode(data)
                r.mode = mode.value if mode is not None else None
            elif addr == signals.ENGINE_STATE_ADDR:
                if len(data) > 2:
                    r.b2[data[2]] += 1
                state = signals.decode_engine_state(data)
            elif addr == signals.ENGINE_RUN_COUNTER_ADDR:
                r.frames_3f9 += 1
                r.dlc_3f9[len(data)] += 1
                if data:
                    r.b0_3f9[data[0]] += 1
                val = signals.decode_engine_run_counter(data)
                if val is not None:
                    if prev_val is not None and val != prev_val:
                        r.deltas[(val - prev_val) & 0xFFFF] += 1
                        dt = t - prev_t
                        if 0 < dt < 1.0:
                            r.rates.append(((val - prev_val) & 0xFFFF) / dt)
                        last_move = t
                    prev_val, prev_t = val, t
            else:
                continue

            now = merged(t)
            if now != run_state:
                if run_state is not None:
                    r.runs.append((run_start, t, run_state, r.mode))
                run_state, run_start = now, t
            if addr == signals.ENGINE_RUN_COUNTER_ADDR:
                # Time each disagreement on this frame's own ~4 Hz cadence,
                # so the numbers are seconds rather than "however often the
                # 40 Hz frames happened to tick".
                dt = min(t - last_3f9, 1.0) if last_3f9 else 0.0
                advancing = (t - last_move) <= IDLE_S
                if state is EngineState.OFF and advancing:
                    r.lead_in_s += dt
                elif state is EngineState.RUNNING and not advancing:
                    r.no_fuel_s += dt
                last_3f9 = t

    if run_state is not None:
        r.runs.append((run_start, last_t, run_state, r.mode))
    return r


def _print(path: str, r: _Report) -> None:
    print(f"\n{pathlib.Path(path).name}")
    print("  0x4C5 byte 2:")
    for value, n in sorted(r.b2.items()):
        decoded = signals.decode_engine_state(bytes((0, 0, value)))
        print(f"    0x{value:02X}  {n:>8} frames  -> {decoded.value}")
    if not r.b2:
        print("    (no 0x4C5 frames)")
    print(f"  0x3F9: {r.frames_3f9} frames, dlc={dict(r.dlc_3f9)}, "
          f"byte0={ {f'0x{v:02X}': n for v, n in r.b0_3f9.items()} }")
    print(f"    counter steps: {sum(r.deltas.values())}, "
          f"top deltas={r.deltas.most_common(5)}")
    if r.rates:
        rates = sorted(r.rates)
        print(f"    step rate counts/s: min={rates[0]:.0f} "
              f"p50={rates[len(rates) // 2]:.0f} max={rates[-1]:.0f}")
    print(f"  disagreements: {r.lead_in_s:.0f}s counter-moving with 0x4C5 off "
          f"(leg lead-in), {r.no_fuel_s:.0f}s 0x4C5 running with the counter "
          f"frozen (no-fuel stretches)")
    print("  engine timeline (start, duration, state, drive mode at entry):")
    t0 = r.runs[0][0] if r.runs else 0.0
    for start, end, state, mode in r.runs:
        if end - start < 1.0:
            continue          # decode settling, not a real run
        print(f"    +{start - t0:8.1f}s  {end - start:7.1f}s  {state:<7}"
              f"  mode={mode}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--since", type=float, default=None,
                    help="drop frames before this epoch (stale pre-NTP head)")
    args = ap.parse_args()
    for path in args.captures:
        _print(path, _scan(path, args.since))


if __name__ == "__main__":
    main()
