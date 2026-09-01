#!/usr/bin/env python3
"""Build the Session-9 SOC-anchor analysis: SVG charts + a summary JSON.

Read-only, stdlib only, no CAN hardware. Companion to ``soc_report.py``
(Session 8). Session 9 is the *calibration* drive: it logged a real
battery SOC percent alongside the dash gauge by polling UDS ``22 005B``
every 10 s, so this report answers three things --

  * calibration -- diag SOC at each gauge-bar drop, and the line through
    them (``SOC% ~= slope*bars + intercept``);
  * HOLD behaviour -- diag SOC over the whole drive, showing the plateau
    after the driver forced HOLD at the 2-bar mark;
  * candidate check -- the Session-8 broadcast candidates (``0x3E3`` b0,
    ``0x228`` b2, ``0x186`` b6) and ``0x096`` b3 plotted against the diag
    SOC, i.e. do they actually track charge.

Two phases, same shape as ``soc_report.py``:

  extract  --capture <candump.log> --marks <soclog.log> --data <json>
  render   --data <json> --out-dir <dir> --prefix session9-

With --capture and --out-dir both given it does both in one pass. The
committed JSON lets ``render`` rebuild the SVGs without the ~228 MB log.

  ./soc_report9.py --capture captures/candump-2026-08-30_212037.log \\
      --marks captures/vdmf-soclog-20260830-212037.log \\
      --data docs/analysis/data/session9-soc.json \\
      --out-dir docs/analysis/img --prefix session9-
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

# Broadcast fields to trace against the diag SOC. (addr, byte, width,
# endian, kind, label). kind == "cand" is a Session-8 SOC candidate.
FIELDS: tuple[tuple[str, int, int, str, str, str], ...] = (
    ("3E3", 0, 1, "be", "cand", "0x3E3 b0"),
    ("228", 2, 1, "be", "cand", "0x228 b2"),
    ("186", 6, 1, "be", "cand", "0x186 b6"),
    ("096", 3, 1, "be", "shape", "0x096 b3"),
    ("3E9", 0, 2, "be", "speed", "0x3E9 speed"),
)

# UDS response: 7E8 # 04 62 00 5B <soc> AA AA AA   -> SOC% = soc * 100/255
_DIAG_ID = "7E8"
_DIAG_RE = re.compile(r"0462005b([0-9a-f]{2})", re.I)

_LINE = re.compile(r"\((?P<ts>\d+\.\d+)\)\s+\S+\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)")
_MARK = re.compile(
    r"(?P<kind>GAUGE-DOWN|GAUGE-UP|START|END)\s+\+\s*(?P<t>[\d.]+)s.*gauge=(?P<lvl>\d+)/(?P<tot>\d+)"
)
_SOC22 = re.compile(r"soc22=(?P<pct>[\d.]+)%\(0x(?P<raw>[0-9A-Fa-f]+)@")

_KMH_PER_COUNT = 1.0 / 64.0
_MPH_PER_KMH = 0.621371
_SOC_PER_RAW = 100.0 / 255.0


# ---- extract --------------------------------------------------------------
def parse_marks(path: str) -> dict:
    """Gauge-drop timeline + the inline soc22 diag marks from a soc_log log."""
    lines = pathlib.Path(path).read_text().splitlines()
    downs: list[tuple[float, int]] = []
    diag_marks: list[tuple[float, float, int]] = []
    drive_len = 0.0
    seen_min: int | None = None
    for line in lines:
        m = _MARK.search(line)
        if not m:
            continue
        t = float(m["t"])
        drive_len = max(drive_len, t)
        s = _SOC22.search(line)
        if s:
            diag_marks.append((round(t, 1), float(s["pct"]), int(s["raw"], 16)))
        if m["kind"] == "GAUGE-DOWN":
            lvl = int(m["lvl"])
            if seen_min is None or lvl < seen_min:
                seen_min = lvl
                downs.append((round(t, 1), lvl))
    return {
        "gauge_times": [t for t, _ in downs],
        "gauge_levels": [lvl for _, lvl in downs],
        "gauge_labels": [f"{lvl + 1}>{lvl}" for _, lvl in downs],
        "diag_marks": diag_marks,
        "drive_len": round(drive_len, 1),
    }


def parse_captures(paths: list[str], drive_len: float):
    """Return {id: [(dt, data)]}, diag_pts [(dt, pct)].

    The capture can carry a burst of stale pre-NTP frames at the head
    (clock jump at boot); keep only frames within drive_len + 600 s of the
    last frame, and take t0 from that window.
    """
    want = {f[0].upper() for f in FIELDS} | {_DIAG_ID}
    hits: dict[str, list[tuple[float, bytes]]] = {i: [] for i in want}
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
                hits[cid].append((float(m["ts"]), data))
    all_ts = [ts for v in hits.values() for ts, _ in v]
    if not all_ts:
        raise SystemExit("no frames of interest in the capture")
    last = max(all_ts)
    window = drive_len + 600.0
    t0 = min((ts for ts in all_ts if ts >= last - window), default=min(all_ts))

    out: dict[str, list[tuple[float, bytes]]] = {}
    diag_pts: list[tuple[float, float]] = []
    for cid, rows in hits.items():
        kept = [(round(ts - t0, 3), d) for ts, d in rows if ts >= t0]
        kept.sort()
        if cid == _DIAG_ID:
            for dt, d in kept:
                mm = _DIAG_RE.match(d.hex())
                if mm:
                    diag_pts.append((dt, round(int(mm.group(1), 16) * _SOC_PER_RAW, 2)))
        else:
            out[cid] = kept
    return out, diag_pts


def _decode(data: bytes, off: int, width: int, endian: str) -> int | None:
    if len(data) < off + width:
        return None
    if width == 1:
        return data[off]
    hi, lo = data[off], data[off + 1]
    return (hi << 8 | lo) if endian == "be" else (lo << 8 | hi)


def _median_bin(pts: list[tuple[float, float]], bin_s: float) -> list[list[float]]:
    binned: list[list[float]] = []
    cur: list[float] = []
    edge = bin_s
    for dt, v in pts:
        while dt > edge:
            if cur:
                binned.append([round(edge - bin_s / 2, 1), round(statistics.median(cur), 2)])
            cur = []
            edge += bin_s
        cur.append(v)
    if cur:
        binned.append([round(edge - bin_s / 2, 1), round(statistics.median(cur), 2)])
    return binned


def _sample_at(pts: list[tuple[float, float]], t: float) -> float | None:
    """Last value at or before t (+0.5 s slack)."""
    v = None
    for dt, val in pts:
        if dt > t + 0.5:
            break
        v = val
    return v


def _linfit(xs: list[float], ys: list[float]) -> dict:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    intercept = my - slope * mx
    r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
    return {"slope": round(slope, 3), "intercept": round(intercept, 2), "r": round(r, 4)}


def extract(captures: list[str], marks: str, bin_s: float) -> dict:
    md = parse_marks(marks)
    raw, diag_pts = parse_captures(captures, md["drive_len"])
    drive_len = round(max(md["drive_len"], diag_pts[-1][0] if diag_pts else 0.0), 1)

    # diag SOC series, lightly binned (poll is ~10 s so this stays faithful)
    diag_series = _median_bin(diag_pts, max(bin_s, 8.0))

    # SOC at each gauge-bar drop -- prefer the capture, fall back to the
    # inline soc22 mark if the capture has a gap there.
    mark_by_t = {t: pct for t, pct, _ in md["diag_marks"]}
    bar_soc: list[float | None] = []
    for gt in md["gauge_times"]:
        v = _sample_at(diag_pts, gt)
        if v is None:
            near = min(mark_by_t, key=lambda mt: abs(mt - gt), default=None)
            v = mark_by_t[near] if near is not None and abs(near - gt) < 30 else None
        bar_soc.append(round(v, 2) if v is not None else None)

    fit = None
    pred = {}
    xy = [(lvl, s) for lvl, s in zip(md["gauge_levels"], bar_soc) if s is not None]
    if len(xy) >= 3:
        fit = _linfit([x for x, _ in xy], [y for _, y in xy])
        f = lambda b: round(fit["slope"] * b + fit["intercept"], 1)
        pred = {"per_bar_pct": abs(fit["slope"]), "bar1": f(1), "bar2": f(2),
                "bar3": f(3), "bar10": f(10)}

    # HOLD: the driver forced it at the 2-bar mark (not logged as an event).
    # Region start = the last gauge-down; also report the SOC minimum.
    hold_t = md["gauge_times"][-1] if md["gauge_times"] else None
    soc_min_t, soc_min = min(diag_pts, key=lambda p: p[1]) if diag_pts else (None, None)
    hold = {
        "start_t": hold_t,
        "note": "driver forced HOLD near the 2-bar mark; exact toggle not logged",
        "soc_min": round(soc_min, 2) if soc_min is not None else None,
        "soc_min_t": round(soc_min_t, 1) if soc_min_t is not None else None,
        "soc_start": round(diag_pts[0][1], 2) if diag_pts else None,
        "soc_end": round(diag_pts[-1][1], 2) if diag_pts else None,
    }

    series = []
    for cid, off, width, endian, kind, label in FIELDS:
        pts = [(dt, _decode(b, off, width, endian)) for dt, b in raw.get(cid.upper(), [])]
        pts = [(dt, float(v)) for dt, v in pts if v is not None]
        if cid == "096":
            # 0x096 is multiplexed; the slow byte-3 value only lives in the
            # "x F0 0A xx" mux frames -- byte1 == 0xF0 AND byte2 == 0x0A.
            keep = {round(dt, 3) for dt, b in raw.get("096", [])
                    if len(b) > 2 and b[1] == 0xF0 and b[2] == 0x0A}
            # its real operating band is ~9..13; anything outside is residual
            # mux contamination, and the first few seconds are warm-up.
            pts = [(dt, v) for dt, v in pts
                   if round(dt, 3) in keep and 8 <= v <= 14 and dt > 20]
        if not pts:
            continue
        b = _median_bin(pts, 20.0 if cid == "096" else 8.0)
        if kind == "speed":
            b = [[t, round(v * _KMH_PER_COUNT * _MPH_PER_KMH, 1)] for t, v in b]
        entry = {
            "id": cid, "label": label, "kind": kind,
            "start": b[0][1], "end": b[-1][1],
            "vmin": min(p[1] for p in b), "vmax": max(p[1] for p in b),
            "data": b,
        }
        if kind in ("cand", "shape"):
            # value vs diag SOC: pair each binned candidate sample with the
            # diag SOC at that instant -> is it a function of charge?
            vs = []
            for t, v in b:
                s = _sample_at(diag_pts, t)
                if s is not None:
                    vs.append([round(s, 2), v])
            entry["vs_soc"] = vs
            if len(vs) >= 5:
                entry["soc_r"] = _linfit([p[0] for p in vs], [p[1] for p in vs])["r"]
        series.append(entry)

    return {
        "session": 9,
        "drive_len": drive_len,
        "soc_scale": "SOC% = raw * 100/255  (UDS 22 005B, resp 7E8)",
        "diag_poll": {"sent": None, "note": "185/186 replies, 1 NRC (see soc_log summary)"},
        "gauge_times": md["gauge_times"],
        "gauge_levels": md["gauge_levels"],
        "gauge_labels": md["gauge_labels"],
        "bar_soc": bar_soc,
        "fit": fit,
        "pred": pred,
        "hold": hold,
        "diag": diag_series,
        "series": series,
    }


# ---- render -------------------------------------------------------------
PAL = {
    "3E3": "#4ea6ff", "228": "#ffb454", "186": "#c98bff", "096": "#7bd88f",
}
SOC_COL = "#ff5d6c"
INK = "#c9d3e0"
DIM = "#7c8698"
GRID = "#2a2f3a"
BG = "#12151b"
PANEL = "#171b22"
BAND = "#ff5d6c"
FONT = ('font-family="IBM Plex Sans, -apple-system, Segoe UI, sans-serif" '
        'font-size="12"')


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _poly(pts) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def svg_timeline(d: dict, W: int = 920, H: int = 460) -> str:
    """Diag SOC over the whole drive: gauge drops, HOLD region, speed underlay."""
    mL, mR, mT, mB = 46, 54, 24, 46
    x0, x1, y0, y1 = mL, W - mR, H - mB, mT
    T = d["drive_len"]
    X = lambda t: x0 + (t / T) * (x1 - x0)
    Ysoc = lambda p: y0 + (p / 100.0) * (y1 - y0)
    spMax = 90
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" {FONT}>',
         f'<rect width="{W}" height="{H}" fill="{BG}"/>',
         f'<rect x="{x0}" y="{y1}" width="{x1 - x0}" height="{y0 - y1}" fill="{PANEL}"/>']

    # HOLD region
    hs = d["hold"].get("start_t")
    if hs is not None:
        o.append(f'<rect x="{X(hs):.1f}" y="{y1}" width="{X(T) - X(hs):.1f}" '
                 f'height="{y0 - y1}" fill="{BAND}" opacity="0.08"/>')
        o.append(f'<line x1="{X(hs):.1f}" y1="{y1}" x2="{X(hs):.1f}" y2="{y0}" '
                 f'stroke="{BAND}" stroke-opacity="0.55" stroke-dasharray="4 3"/>')
        o.append(f'<text x="{X(hs) + 6:.1f}" y="{y1 + 14}" fill="{BAND}" '
                 f'font-size="10">driver forces HOLD (2-bar mark)</text>')

    # speed underlay (right axis)
    sp = next((s for s in d["series"] if s["kind"] == "speed"), None)
    if sp:
        pts = [(X(t), y0 + (min(v, spMax) / spMax) * (y1 - y0)) for t, v in sp["data"]]
        o.append(f'<polygon points="{x0},{y0} {_poly(pts)} {x1},{y0}" '
                 f'fill="{PAL["3E3"]}" opacity="0.07"/>')
        for g in (0, 45, 90):
            yy = y0 + (g / spMax) * (y1 - y0)
            o.append(f'<text x="{x1 + 6}" y="{yy + 3:.1f}" fill="{DIM}" font-size="10">{g}</text>')
        o.append(f'<text x="{x1 + 6}" y="{y1 - 4}" fill="{DIM}" font-size="10">mph</text>')

    # SOC grid + axis
    for p in (0, 25, 50, 75, 100):
        yy = Ysoc(p)
        o.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="{GRID}"/>')
        o.append(f'<text x="{x0 - 8}" y="{yy + 3:.1f}" fill="{DIM}" text-anchor="end" '
                 f'font-size="10">{p}%</text>')

    # gauge-drop verticals + labels
    for t, lab, soc in zip(d["gauge_times"], d["gauge_labels"], d["bar_soc"]):
        o.append(f'<line x1="{X(t):.1f}" y1="{y1}" x2="{X(t):.1f}" y2="{y0}" '
                 f'stroke="{INK}" stroke-opacity="0.22" stroke-dasharray="2 3"/>')
        if soc is not None:
            o.append(f'<circle cx="{X(t):.1f}" cy="{Ysoc(soc):.1f}" r="3" fill="{SOC_COL}"/>')
            o.append(f'<text x="{X(t):.1f}" y="{Ysoc(soc) - 8:.1f}" fill="{INK}" '
                     f'font-size="9" text-anchor="middle">{lab}</text>')

    # diag SOC trace
    pts = [(X(t), Ysoc(v)) for t, v in d["diag"]]
    o.append(f'<polyline points="{_poly(pts)}" fill="none" stroke="{SOC_COL}" stroke-width="2.4"/>')

    h = d["hold"]
    o.append(f'<text x="{x0}" y="{y1 - 8}" fill="{INK}" font-size="11">'
             f'Diagnostic SOC (UDS 22 005B, raw&#215;100/255) &#8212; '
             f'{h["soc_start"]:.1f}% &#8594; min {h["soc_min"]:.1f}% &#8594; '
             f'{h["soc_end"]:.1f}% at drive end</text>')
    o.append(f'<text x="{x1}" y="{y0 + 30}" fill="{DIM}" font-size="10" text-anchor="end">'
             f'{T / 60:.0f} min &#183; red dots = gauge-bar drop</text>')
    o.append(f'<text x="{x0}" y="{y0 + 30}" fill="{DIM}" font-size="10">0</text>')
    o.append("</svg>")
    return "\n".join(o)


def svg_calibration(d: dict, W: int = 620, H: int = 430) -> str:
    """SOC% vs gauge bars: the drop points + the fitted line + the 2-bar floor."""
    mL, mR, mT, mB = 52, 20, 26, 44
    x0, x1, y0, y1 = mL, W - mR, H - mB, mT
    bars = list(range(1, 11))
    X = lambda b: x0 + ((b - 1) / 9.0) * (x1 - x0)
    Y = lambda p: y0 + (p / 100.0) * (y1 - y0)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" {FONT}>',
         f'<rect width="{W}" height="{H}" fill="{BG}"/>',
         f'<rect x="{x0}" y="{y1}" width="{x1 - x0}" height="{y0 - y1}" fill="{PANEL}"/>']

    fit, pred = d.get("fit"), d.get("pred", {})

    # 2-bar HOLD-floor band: from the observed 3->2 drop down to the
    # predicted 2->1 edge.
    if fit and pred:
        top = round(d["bar_soc"][-1], 1) if d["bar_soc"] and d["bar_soc"][-1] is not None else pred["bar2"]
        bot = pred["bar1"]
        o.append(f'<rect x="{x0}" y="{Y(top):.1f}" width="{x1 - x0}" '
                 f'height="{Y(bot) - Y(top):.1f}" fill="{BAND}" opacity="0.12"/>')
        o.append(f'<text x="{x1 - 6}" y="{Y(top) + 14:.1f}" fill="{BAND}" '
                 f'font-size="10" text-anchor="end">2-bar HOLD floor '
                 f'&#8776; {bot:g}&#8211;{top:g}%</text>')

    for p in (0, 25, 50, 75, 100):
        yy = Y(p)
        o.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="{GRID}"/>')
        o.append(f'<text x="{x0 - 8}" y="{yy + 3:.1f}" fill="{DIM}" text-anchor="end" '
                 f'font-size="10">{p}%</text>')
    for b in bars:
        o.append(f'<text x="{X(b):.1f}" y="{y0 + 16}" fill="{DIM}" text-anchor="middle" '
                 f'font-size="10">{b}</text>')
    o.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{y0 + 34}" fill="{DIM}" '
             f'text-anchor="middle" font-size="10">gauge bars shown</text>')

    # fitted line
    if fit:
        f = lambda b: fit["slope"] * b + fit["intercept"]
        o.append(f'<line x1="{X(1):.1f}" y1="{Y(f(1)):.1f}" x2="{X(10):.1f}" '
                 f'y2="{Y(f(10)):.1f}" stroke="{INK}" stroke-opacity="0.5" '
                 f'stroke-dasharray="5 4"/>')
        o.append(f'<text x="{X(6):.1f}" y="{Y(f(6)) - 10:.1f}" fill="{DIM}" '
                 f'font-size="10" text-anchor="middle">'
                 f'SOC% &#8776; {fit["slope"]:g}&#183;bars + {fit["intercept"]:g}  '
                 f'(r={fit["r"]:.3f})</text>')
        # predicted 2->1 edge, hollow
        o.append(f'<circle cx="{X(1):.1f}" cy="{Y(pred["bar1"]):.1f}" r="4" '
                 f'fill="none" stroke="{SOC_COL}" stroke-dasharray="2 2"/>')
        o.append(f'<text x="{X(1) + 8:.1f}" y="{Y(pred["bar1"]) + 3:.1f}" fill="{DIM}" '
                 f'font-size="9">2&#8594;1 not reached ({pred["bar1"]:g}%)</text>')

    # observed drop points
    for lvl, lab, soc in zip(d["gauge_levels"], d["gauge_labels"], d["bar_soc"]):
        if soc is None:
            continue
        o.append(f'<circle cx="{X(lvl):.1f}" cy="{Y(soc):.1f}" r="4" fill="{SOC_COL}"/>')
        o.append(f'<text x="{X(lvl):.1f}" y="{Y(soc) - 9:.1f}" fill="{INK}" '
                 f'font-size="9" text-anchor="middle">{soc:.1f}</text>')

    o.append(f'<text x="{x0}" y="{y1 - 8}" fill="{INK}" font-size="11">'
             f'Diag SOC at each gauge-bar drop &#8212; ~{d["pred"].get("per_bar_pct", 0):.1f}% per bar</text>')
    o.append("</svg>")
    return "\n".join(o)


def svg_candidates(d: dict, W: int = 920, H: int = 300) -> str:
    """Session-8 candidates & 0x096 b3 plotted against the diag SOC."""
    cands = [s for s in d["series"] if s["kind"] in ("cand", "shape")]
    n = len(cands)
    pad = 14
    pw = (W - pad * (n + 1)) / n
    ph = H - 58
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" {FONT}>',
         f'<rect width="{W}" height="{H}" fill="{BG}"/>',
         f'<text x="{pad}" y="16" fill="{INK}" font-size="11">'
         f'Raw value vs diagnostic SOC &#8212; a real SOC signal is a straight '
         f'line; a loop or a smear is not charge</text>']
    for i, s in enumerate(cands):
        px = pad + i * (pw + pad)
        py = 30
        vs = s.get("vs_soc", [])
        if not vs:
            continue
        socs = [p[0] for p in vs]
        vals = [p[1] for p in vs]
        smin, smax = 20, max(95, math.ceil(max(socs)))
        vlo, vhi = min(vals), max(vals)
        vpad = (vhi - vlo) * 0.1 or 1
        vlo -= vpad
        vhi += vpad
        # x: SOC high (left) -> low (right), matches time going forward
        X = lambda s_, a=px, b=px + pw: b + (s_ - smin) / (smax - smin) * (a - b)
        Y = lambda v, a=py, b=py + ph: b + (v - vlo) / (vhi - vlo) * (a - b)
        col = PAL.get(s["id"], INK)
        o.append(f'<rect x="{px:.1f}" y="{py}" width="{pw:.1f}" height="{ph}" fill="{PANEL}"/>')
        for gp in (30, 50, 70, 90):
            o.append(f'<line x1="{X(gp):.1f}" y1="{py}" x2="{X(gp):.1f}" y2="{py + ph}" '
                     f'stroke="{GRID}"/>')
            o.append(f'<text x="{X(gp):.1f}" y="{py + ph + 14}" fill="{DIM}" '
                     f'font-size="9" text-anchor="middle">{gp}%</text>')
        # corner-to-corner guide = what a signal that is purely a function
        # of SOC would trace (monotone, no width).
        o.append(f'<line x1="{X(smax):.1f}" y1="{Y(vhi):.1f}" x2="{X(smin):.1f}" '
                 f'y2="{Y(vlo):.1f}" stroke="{INK}" stroke-opacity="0.28" '
                 f'stroke-dasharray="4 4"/>')
        if i == 0:
            o.append(f'<text x="{X(smax) + 6:.1f}" y="{Y(vhi) + 12:.1f}" fill="{DIM}" '
                     f'font-size="9">&#8213; = pure SOC</text>')
        o.append(f'<polyline points="{_poly([(X(a), Y(b)) for a, b in vs])}" '
                 f'fill="none" stroke="{col}" stroke-width="1.6" opacity="0.9"/>')
        for a, b in vs[::4]:
            o.append(f'<circle cx="{X(a):.1f}" cy="{Y(b):.1f}" r="1.6" fill="{col}"/>')
        r = s.get("soc_r")
        rtxt = f"  vs-SOC r={r:+.2f}" if r is not None else ""
        verdict = ("monotone, but ~13% SOC / step" if s["kind"] == "shape"
                   else "not a function of SOC")
        o.append(f'<text x="{px + 2:.1f}" y="{py - 6}" fill="{col}" font-size="11">'
                 f'{_esc(s["label"])}<tspan fill="{DIM}" font-size="9">'
                 f'  {s["start"]:g}&#8594;{s["end"]:g}{rtxt}</tspan></text>')
        o.append(f'<text x="{px + pw / 2:.1f}" y="{py + ph - 6:.1f}" fill="{DIM}" '
                 f'font-size="9" text-anchor="middle">{verdict}</text>')
    o.append(f'<text x="{pad}" y="{H - 6}" fill="{DIM}" font-size="9">'
             f'x = diag SOC (high &#8594; low = drive start &#8594; end) &#183; '
             f'0x228 b2 / 0x186 b6 behave like 0x3E3 b0</text>')
    o.append("</svg>")
    return "\n".join(o)


# ---- cli --------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture", action="append", default=[], metavar="GLOB")
    ap.add_argument("--marks", metavar="PATH")
    ap.add_argument("--bin", type=float, default=5.0, metavar="SECS")
    ap.add_argument("--data", metavar="PATH")
    ap.add_argument("--out-dir", metavar="DIR")
    ap.add_argument("--prefix", default="session9-")
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

    fit = payload.get("fit")
    print(f"  drive {payload['drive_len'] / 60:.1f} min, "
          f"{len(payload['gauge_times'])} bar drops")
    for lvl, lab, soc in zip(payload["gauge_levels"], payload["gauge_labels"],
                             payload["bar_soc"]):
        print(f"    {lab:>5}  {'' if soc is None else f'{soc:5.1f}%'}")
    if fit:
        print(f"  fit: SOC% = {fit['slope']}*bars + {fit['intercept']}  r={fit['r']}")
        print(f"  pred: {payload['pred']}")
    print(f"  hold: {payload['hold']}")
    for s in payload["series"]:
        if "soc_r" in s:
            print(f"  {s['label']:12} vs-SOC r={s['soc_r']:+.3f}  "
                  f"{s['start']:g} -> {s['end']:g}")

    if args.out_dir:
        dd = pathlib.Path(args.out_dir)
        dd.mkdir(parents=True, exist_ok=True)
        (dd / f"{args.prefix}timeline.svg").write_text(svg_timeline(payload))
        (dd / f"{args.prefix}calibration.svg").write_text(svg_calibration(payload))
        (dd / f"{args.prefix}candidates.svg").write_text(svg_candidates(payload))
        print(f"wrote {dd}/{args.prefix}{{timeline,calibration,candidates}}.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
