#!/usr/bin/env python3
"""Build the in-repo SOC-candidate analysis: SVG charts + a summary JSON.

Read-only, stdlib only, no CAN hardware. Turns a discharge-drive capture
(``candump -l`` log + the matching ``soc_log.py`` event log) into committable
artefacts under ``docs/analysis/`` -- so the visual write-up lives in the
repo as Markdown + images, not as a hosted link.

Two phases, run together or apart:

  extract   --capture <candump.log> [--capture ...] --marks <soclog.log>
            -> parse the gauge-drop timeline and every candidate/timer/speed
               field, bin to --bin seconds, compute the per-bar values and
               the timer-rejection correlation, write --data <json>.

  render    --data <json> --out-dir <dir>
            -> write overlay.svg (normalised completion of every field on
               one axis, speed + gauge-drops + turnaround for context) and
               native.svg (each candidate on its own raw scale).

With --capture and --out-dir both given it does extract then render in one
pass. The JSON is small and is committed too, so `render` alone regenerates
the charts without the multi-hundred-MB capture.

  ./soc_report.py --capture captures/candump-*.log \\
      --marks captures/vdmf-soclog-*.log \\
      --data docs/analysis/data/session8-soc.json \\
      --out-dir docs/analysis/img --prefix session8-
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import pathlib
import re
import statistics
import sys

# (addr, byte, width, endianness, kind, label). kind drives styling and
# whether the field counts as a SOC candidate in the scorecard.
FIELDS: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("3E3", 0, 1, "be", "cand", "0x3E3 b0"),
    ("228", 2, 1, "be", "cand", "0x228 b2"),
    ("186", 6, 1, "be", "cand", "0x186 b6"),
    ("2C7", 1, 2, "be", "volt", "0x2C7 b1-2"),
    ("4CB", 1, 1, "be", "timer", "0x4CB b1"),
    ("137", 0, 1, "be", "timer", "0x137 b0"),
    ("3E9", 0, 2, "be", "speed", "0x3E9 speed"),
)

_LINE = re.compile(r"\((?P<ts>\d+\.\d+)\)\s+\S+\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)")
_MARK = re.compile(r"(?P<kind>GAUGE-DOWN|GAUGE-UP|START|END)\s+\+\s*(?P<t>[\d.]+)s.*gauge=(?P<lvl>\d+)/(?P<tot>\d+)")

_KMH_PER_COUNT = 1.0 / 64.0
_MPH_PER_KMH = 0.621371


# ---- extract --------------------------------------------------------------
def parse_marks(path: str) -> tuple[list[float], list[str], float]:
    """Return (gauge_down_times, labels, drive_len) from a soc_log event log.

    Only the *first* time the gauge reaches each level on the way down is
    kept, so a regen bump near the end (a GAUGE-UP then a re-drop to a level
    already visited) does not add a spurious bar -- the canonical timeline
    is one drop per bar, N-1 .. 0."""
    events: list[tuple[float, str, int]] = []
    drive_len = 0.0
    for line in pathlib.Path(path).read_text().splitlines():
        m = _MARK.search(line)
        if not m:
            continue
        t = float(m["t"])
        drive_len = max(drive_len, t)
        if m["kind"] in ("GAUGE-DOWN", "GAUGE-UP"):
            events.append((t, m["kind"], int(m["lvl"])))
    events.sort()
    times: list[float] = []
    labels: list[str] = []
    seen_min: int | None = None
    for t, kind, lvl in events:
        if kind == "GAUGE-DOWN" and (seen_min is None or lvl < seen_min):
            seen_min = lvl
            times.append(t)
            labels.append(f"{lvl + 1}>{lvl}")
    return times, labels, drive_len


def parse_captures(paths: list[str]) -> tuple[dict[str, list[tuple[float, bytes]]], float]:
    want = {f[0].upper() for f in FIELDS}
    raw: dict[str, list[tuple[float, bytes]]] = {i: [] for i in want}
    t0: float | None = None
    for path in paths:
        with open(path) as fh:
            for line in fh:
                m = _LINE.match(line)
                if not m:
                    continue
                cid = m["id"].upper()
                if cid not in want:
                    continue
                try:
                    data = bytes.fromhex(m["data"])
                except ValueError:
                    continue
                ts = float(m["ts"])
                if t0 is None:
                    t0 = ts
                raw[cid].append((ts - t0, data))
    return raw, (t0 or 0.0)


def _decode(data: bytes, off: int, width: int, endian: str) -> int | None:
    if len(data) < off + width:
        return None
    if width == 1:
        return data[off]
    hi, lo = data[off], data[off + 1]
    return (hi << 8 | lo) if endian == "be" else (lo << 8 | hi)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (sx * sy)


def extract(captures: list[str], marks: str, bin_s: float) -> dict:
    gtimes, glabels, mark_len = parse_marks(marks)
    raw, _ = parse_captures(captures)

    bar_durations = ([round(gtimes[0], 1)]
                     + [round(gtimes[k] - gtimes[k - 1], 1)
                        for k in range(1, len(gtimes))]) if gtimes else []

    series = []
    for cid, off, width, endian, kind, label in FIELDS:
        pts = [(dt, _decode(b, off, width, endian))
               for dt, b in raw.get(cid.upper(), [])]
        pts = [(dt, v) for dt, v in pts if v is not None]
        if not pts:
            continue
        # median-bin
        binned: list[list[float]] = []
        cur: list[int] = []
        edge = bin_s
        for dt, v in pts:
            while dt > edge:
                if cur:
                    binned.append([round(edge - bin_s / 2, 1),
                                   round(statistics.median(cur), 2)])
                cur = []
                edge += bin_s
            cur.append(v)
        if cur:
            binned.append([round(edge - bin_s / 2, 1),
                           round(statistics.median(cur), 2)])
        if kind == "speed":
            binned = [[t, round(v * _KMH_PER_COUNT * _MPH_PER_KMH, 1)]
                      for t, v in binned]

        # value at each gauge-drop (last sample at or before the drop)
        gvals: list[float | None] = []
        for g in gtimes:
            vv = None
            for dt, v in pts:
                if dt > g + 0.5:
                    break
                vv = v
            if kind == "speed" and vv is not None:
                vv = round(vv * _KMH_PER_COUNT * _MPH_PER_KMH, 1)
            gvals.append(vv)

        entry = {
            "id": cid, "label": label, "kind": kind,
            "start": binned[0][1], "end": binned[-1][1],
            "vmin": min(p[1] for p in binned), "vmax": max(p[1] for p in binned),
            "gauge_vals": gvals, "data": binned,
        }
        # Timer-rejection test: over the N-1 segments *between* consecutive
        # gauge-bar drops, correlate each segment's value-delta with its
        # wall-clock duration. r -> +1 means the field is paced by the clock
        # (an elapsed-time counter); r -> 0 means it is paced by energy use
        # (SOC-like). The pre-drive segment is excluded (it has no
        # start-of-drive reading and includes stationary idle time).
        if kind in ("cand", "timer") and len(gvals) >= 4 \
                and all(v is not None for v in gvals):
            seg_dur = [gtimes[k] - gtimes[k - 1] for k in range(1, len(gtimes))]
            seg_delta = [abs(gvals[k] - gvals[k - 1])
                         for k in range(1, len(gvals))]
            entry["timer_r"] = round(_pearson(seg_dur, seg_delta) or 0.0, 3)
        series.append(entry)

    sp = next((s for s in series if s["kind"] == "speed"), None)
    turn = {}
    if sp:
        lo = min((p for p in sp["data"] if 200 < p[0] < gtimes[-1]),
                 key=lambda p: p[1], default=None)
        if lo:
            turn = {"turnaround_t": lo[0], "turnaround_low_mph": lo[1],
                    "turnaround": [round(lo[0] - 35), round(lo[0] + 45)]}
    drive_len = round(max(mark_len, sp["data"][-1][0] if sp else 0), 1)
    return {
        "gauge_times": [round(t, 1) for t in gtimes],
        "gauge_labels": glabels,
        "bar_durations": bar_durations,
        "drive_len": drive_len,
        **turn,
        "series": series,
    }


# ---- render -------------------------------------------------------------
PAL = {
    "3E3": "#4ea6ff", "228": "#ffb454", "186": "#c98bff",
    "2C7": "#7bd88f", "4CB": "#8a94a6", "137": "#8a94a6",
}
INK = "#c9d3e0"
DIM = "#7c8698"
GRID = "#2a2f3a"
BG = "#12151b"
PANEL = "#171b22"


def _completion(s: dict, v: float) -> float:
    lo, hi = min(s["start"], s["end"]), max(s["start"], s["end"])
    if hi - lo < 1e-9:
        return 0.0
    f = (v - lo) / (hi - lo)
    return 1 - f if s["start"] > s["end"] else f


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _poly(pts: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def svg_overlay(d: dict, W: int = 920, H: int = 470) -> str:
    mL, mR, mT, mB = 46, 120, 20, 44
    x0, x1 = mL, W - mR
    y0, y1 = H - mB, mT
    T = d["drive_len"]
    X = lambda t: x0 + (t / T) * (x1 - x0)
    Y = lambda f: y0 + f * (y1 - y0)
    by_id = {s["id"]: s for s in d["series"]}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'font-family="IBM Plex Sans, -apple-system, Segoe UI, sans-serif" '
           f'font-size="12">',
           f'<rect width="{W}" height="{H}" fill="{BG}"/>',
           f'<rect x="{x0}" y="{y1}" width="{x1 - x0}" height="{y0 - y1}" '
           f'fill="{PANEL}"/>']

    # turnaround band
    if "turnaround" in d:
        a, b = d["turnaround"]
        out.append(f'<rect x="{X(a):.1f}" y="{y1}" width="{X(b) - X(a):.1f}" '
                   f'height="{y0 - y1}" fill="#ffffff" opacity="0.05"/>')
        out.append(f'<text x="{X((a + b) / 2):.1f}" y="{y1 + 12}" fill="{DIM}" '
                   f'text-anchor="middle" font-size="10">turnaround '
                   f'~{d.get("turnaround_low_mph", "?")} mph</text>')

    # speed area (right axis 0..spMax)
    sp = by_id.get("3E9")
    spMax = 70
    if sp:
        pts = [(X(t), y0 + (min(v, spMax) / spMax) * (y1 - y0))
               for t, v in sp["data"]]
        area = f'{x0},{y0} ' + _poly(pts) + f' {x1},{y0}'
        out.append(f'<polygon points="{area}" fill="{PAL["3E3"]}" opacity="0.08"/>')
        for gy in (0, 35, 70):
            yy = y0 + (gy / spMax) * (y1 - y0)
            out.append(f'<text x="{x1 + 6}" y="{yy + 3:.1f}" fill="{DIM}" '
                       f'font-size="10">{gy}</text>')
        out.append(f'<text x="{x1 + 6}" y="{y1 - 6}" fill="{DIM}" '
                   f'font-size="10">mph</text>')

    # left grid + axis (completion 0..1)
    for f in (0, .25, .5, .75, 1):
        yy = Y(f)
        out.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" '
                   f'stroke="{GRID}"/>')
        out.append(f'<text x="{x0 - 8}" y="{yy + 3:.1f}" fill="{DIM}" '
                   f'text-anchor="end" font-size="10">{int(f * 100)}%</text>')

    # gauge-drop verticals
    for t, lab in zip(d["gauge_times"], d["gauge_labels"]):
        out.append(f'<line x1="{X(t):.1f}" y1="{y1}" x2="{X(t):.1f}" y2="{y0}" '
                   f'stroke="{INK}" stroke-opacity="0.28" stroke-dasharray="2 3"/>')
    out.append(f'<text x="{x0}" y="{y0 + 26}" fill="{DIM}" font-size="10">'
               f'0</text>')
    out.append(f'<text x="{x1}" y="{y0 + 26}" fill="{DIM}" font-size="10" '
               f'text-anchor="end">{T / 60:.0f} min · ticks = the 10 gauge-bar '
               f'drops</text>')

    # equal-energy reference dots (a true SOC field passes near these)
    n = len(d["gauge_times"])
    for i, t in enumerate(d["gauge_times"], 1):
        out.append(f'<circle cx="{X(t):.1f}" cy="{Y(i / n):.1f}" r="2" '
                   f'fill="{INK}" fill-opacity="0.3"/>')

    # traces
    ly = y1 + 6
    for s in d["series"]:
        if s["kind"] in ("speed",):
            continue
        col = PAL.get(s["id"], INK)
        dash = ' stroke-dasharray="5 4"' if s["kind"] == "timer" else ""
        wdt = 1.4 if s["kind"] == "timer" else 2.0
        if s["kind"] == "volt":
            continue  # V-shaped, not a completion curve; noted in the doc
        pts = [(X(t), Y(_completion(s, v))) for t, v in s["data"]]
        out.append(f'<polyline points="{_poly(pts)}" fill="none" '
                   f'stroke="{col}" stroke-width="{wdt}"{dash}/>')
        tag = "timer (decoy)" if s["kind"] == "timer" else "SOC candidate"
        r = s.get("timer_r")
        rtxt = f'  r={r:+.2f}' if r is not None else ""
        out.append(f'<text x="{x1 + 10}" y="{ly + 4:.1f}" fill="{col}" '
                   f'font-size="11">{_esc(s["label"])}</text>')
        out.append(f'<text x="{x1 + 10}" y="{ly + 17:.1f}" fill="{DIM}" '
                   f'font-size="9">{tag}{rtxt}</text>')
        ly += 34

    out.append(f'<text x="{x0}" y="{y1 - 6}" fill="{INK}" font-size="11">'
               f'Every field normalised to its own full-drive travel '
               f'(0 % start &#8594; 100 % end)</text>')
    out.append("</svg>")
    return "\n".join(out)


def svg_native(d: dict, W: int = 920) -> str:
    cands = [s for s in d["series"] if s["kind"] in ("cand", "volt")]
    rowH, gap, mT, mB, mL, mR = 96, 26, 24, 30, 58, 20
    H = mT + mB + len(cands) * rowH + (len(cands) - 1) * gap
    T = d["drive_len"]
    x0, x1 = mL, W - mR
    X = lambda t: x0 + (t / T) * (x1 - x0)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'font-family="IBM Plex Sans, -apple-system, Segoe UI, sans-serif" '
           f'font-size="12">',
           f'<rect width="{W}" height="{H}" fill="{BG}"/>',
           f'<text x="{x0}" y="15" fill="{INK}" font-size="11">'
           f'Each candidate on its own raw scale &#8212; the actual shape, '
           f'not normalised</text>']
    for i, s in enumerate(cands):
        top = mT + i * (rowH + gap)
        bot = top + rowH
        col = PAL.get(s["id"], INK)
        lo = min(p[1] for p in s["data"])
        hi = max(p[1] for p in s["data"])
        pad = (hi - lo) * 0.08 or 1
        lo -= pad
        hi += pad
        Y = lambda v, t=top, b=bot, l=lo, h=hi: b + (v - l) / (h - l) * (t - b)
        out.append(f'<rect x="{x0}" y="{top}" width="{x1 - x0}" '
                   f'height="{rowH}" fill="{PANEL}"/>')
        for t in d["gauge_times"]:
            out.append(f'<line x1="{X(t):.1f}" y1="{top}" x2="{X(t):.1f}" '
                       f'y2="{bot}" stroke="{INK}" stroke-opacity="0.22" '
                       f'stroke-dasharray="2 3"/>')
        pts = [(X(t), Y(v)) for t, v in s["data"]]
        out.append(f'<polyline points="{_poly(pts)}" fill="none" '
                   f'stroke="{col}" stroke-width="1.8"/>')
        kind = "pack voltage (V-shaped)" if s["kind"] == "volt" else "SOC candidate"
        out.append(f'<text x="{x0}" y="{top - 6}" fill="{col}" font-size="11">'
                   f'{_esc(s["label"])}  <tspan fill="{DIM}" font-size="9">'
                   f'{kind}  {s["start"]:g} &#8594; {s["end"]:g}</tspan></text>')
        out.append(f'<text x="{x0 - 6}" y="{top + 8}" fill="{DIM}" '
                   f'text-anchor="end" font-size="9">{hi:.0f}</text>')
        out.append(f'<text x="{x0 - 6}" y="{bot}" fill="{DIM}" '
                   f'text-anchor="end" font-size="9">{lo:.0f}</text>')
    out.append(f'<text x="{x0}" y="{H - 8}" fill="{DIM}" font-size="9">'
               f'x = 0&#8230;{T / 60:.0f} min · dashed verticals = gauge-bar '
               f'drops</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---- cli --------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture", action="append", default=[], metavar="GLOB",
                    help="candump -l log(s); repeat or glob. Order matters.")
    ap.add_argument("--marks", metavar="PATH",
                    help="matching soc_log.py event log (for the gauge-drop times)")
    ap.add_argument("--bin", type=float, default=3.0, metavar="SECS",
                    help="median-bin width for the plotted series (default 3)")
    ap.add_argument("--data", metavar="PATH",
                    help="summary JSON: written by extract, read by render")
    ap.add_argument("--out-dir", metavar="DIR", help="where the SVGs go")
    ap.add_argument("--prefix", default="", help="filename prefix for the SVGs")
    args = ap.parse_args()

    payload = None
    if args.capture:
        if not args.marks:
            ap.error("--capture needs --marks")
        caps: list[str] = []
        for g in args.capture:
            caps.extend(sorted(glob.glob(g)) or [g])
        payload = extract(caps, args.marks, args.bin)
        if args.data:
            p = pathlib.Path(args.data)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
            print(f"wrote {p}  ({p.stat().st_size} B)")
    elif args.data:
        payload = json.loads(pathlib.Path(args.data).read_text())

    if payload is None:
        ap.error("nothing to do: give --capture/--marks, or --data")

    for s in payload["series"]:
        r = s.get("timer_r")
        print(f"  {s['label']:12} {s['kind']:5} {s['start']:>8g} -> "
              f"{s['end']:<8g}" + (f"  timer_r={r:+.3f}" if r is not None else ""))

    if args.out_dir:
        d = pathlib.Path(args.out_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{args.prefix}overlay.svg").write_text(svg_overlay(payload))
        (d / f"{args.prefix}native.svg").write_text(svg_native(payload))
        print(f"wrote {d}/{args.prefix}overlay.svg and {args.prefix}native.svg")


if __name__ == "__main__":
    sys.exit(main())
