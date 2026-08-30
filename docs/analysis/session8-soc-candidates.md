# Session 8 — which broadcast frame carries SOC?

Visual write-up of the Session-8 full-drain drive (2026-08-30). Pairs with
the narrative in [`../field-session-log.md`](../field-session-log.md#session-8--2026-08-30)
and the procedure in [`../phase-c-field-checklist.md`](../phase-c-field-checklist.md).

The charts and the summary data are regenerated from the capture by
[`../../tools/soc_report.py`](../../tools/soc_report.py); the small summary
JSON ([`data/session8-soc.json`](data/session8-soc.json)) is committed so the
SVGs can be rebuilt without the 214 MB `candump` log:

```
tools/soc_report.py --data docs/analysis/data/session8-soc.json \
    --out-dir docs/analysis/img --prefix session8-
```

## The drive

One continuous run took the dash battery gauge from **10/10 → 0/10**. All ten
bar-drops were hand-marked on the panel buttons
(`captures/vdmf-soclog-20260830-131932.log`); the raw bus went to
`captures/candump-2026-08-30_131932.log` (213.9 MB, ~29 min). Both files are
kept local (git-ignored) — the analysis below is the committed product.

The dash shows **no state-of-charge percent** on this Gen 1 car, only EV
range in miles and the 10-increment gauge, so SOC has to be inferred: find
the field that falls in step with the gauge and is paced by *energy used*,
not by the *clock*.

## Method — the timer-rejection test

1. Scan every byte / 16-bit word on every id for one that moves monotonically
   across the whole drive (the pack went from near-full to well down).
2. For each monotonic field, take its value at each of the ten gauge-bar
   drops. Over the **nine segments between consecutive drops**, correlate
   each segment's value-delta with that segment's wall-clock duration
   (Pearson *r*).
   - `r → +1` — the field moves in lock-step with elapsed time → it is an
     **elapsed-time counter**, not SOC.
   - `r → 0` — the field's movement is unrelated to how long the segment
     took → it is paced by **energy**, SOC-like.
3. The **final bar is the discriminator**: `1→0` fell in **53 s** against a
   ~170 s average for the other nine. A real SOC field still travels a
   near-average amount there (the pack really did drain that last chunk
   fast); a clock barely advances.

![Normalised completion of every candidate and every rejected timer on one
axis, with vehicle speed and the ten gauge-bar drops for
context](img/session8-overlay.svg)

Every candidate visibly **flattens through the mid-drive turnaround**
(≈ +1240 s, where the car slowed to ~8 mph to reverse direction — grey band)
and **steepens again afterward**. The two timers march straight through it at
a constant slope. The faint stepped dots are the "equal energy per bar"
expectation — a true SOC trace should pass close to them.

## Per-bar readings

| bar | seg. duration | `0x3E3` b0 | `0x228` b2 | `0x186` b6 | `0x4CB` b1 (timer) | `0x137` b0 (timer) |
|----:|--------------:|-----------:|-----------:|-----------:|-------------------:|-------------------:|
| 10→9 | 288.8 s\* | 195 | 93 | 119 | 64 | 24 |
| 9→8  | 191.6 s | 189 | 91 | 109 | 70 | 27 |
| 8→7  | 165.1 s | 185 | 89 | 102 | 76 | 30 |
| 7→6  | 183.1 s | 184 | 88 | 99  | 82 | 33 |
| 6→5  | 155.1 s | 181 | 87 | 94  | 87 | 35 |
| 5→4  | 159.9 s | 178 | 86 | 88  | 92 | 38 |
| 4→3  | 187.4 s | 174 | 84 | 82  | 98 | 41 |
| 3→2  | 162.1 s | 170 | 82 | 75  | 104 | 44 |
| 2→1  | 153.7 s | 165 | 81 | 68  | 109 | 46 |
| 1→0  | **53.0 s** | 161 | 79 | 63  | 111 | 47 |

\* The 10→9 segment runs from log start and includes the ~2 min of engine-on
Park idle before pull-away; it is excluded from the correlation.

Note the last row: on the 53 s bar the two timers almost stop
(`0x4CB` +2, `0x137` +1) while all three candidates keep moving a normal
amount (`0x3E3` −4, `0x228` −2, `0x186` −5).

## Scorecard

| field | timer-rejection *r* | over the drain | verdict |
|---|---:|---|---|
| **`0x3E3` b0** (also b1, b6) | **−0.03** | 201 → 161  (−40) | **SOC candidate** — triple-redundant, best timer rejection |
| **`0x228` b2** | **−0.14** | 96 → 78  (−18) | **SOC candidate** — cleanest monotonicity, but coarse (~1.7 cnt/bar) |
| **`0x186` b6** | **+0.28** | 130 → 56  (−74) | **SOC candidate** — finest resolution, but noisy (±8), ticked *up* once at the turnaround |
| `0x2C7` b1–2 | — (V-shaped) | 49094 → 48792, **rebounds** | pack **voltage** under load — sags and recovers, not a charge count. Excluded. |
| `0x4CB` b1 | +0.97 | 54 → 112 | **elapsed-time counter** — per-bar move tracks wall-clock almost exactly |
| `0x137` b0 | +0.88 | 19 → 48 | **elapsed-time counter** — linear ramp, collapses on the 53 s bar |

`0x3DD` and `0x4E9` were also checked and rejected as elapsed-time counters
(same `r ≈ 0.9` signature); they are not plotted here.

## Native scale — the actual shapes

![Each candidate drawn on its own raw-value scale, unnormalised, with the
gauge-bar drops marked](img/session8-native.svg)

`0x3E3` b0/b1/b6 carry the **same value** frame to frame (a built-in
consistency check — require the three to agree to reject a glitch). `0x228`
b2 is the smoothest but only moves ~18 counts across the whole pack, so its
per-bar resolution is coarse. `0x186` b6 has the most counts to work with but
the most jitter.

## What's still missing

**No absolute scale.** No reading at a *known* SOC was logged on this drive,
so none of the three candidates can be turned into a raw→percent (or
raw→kWh) mapping yet. The GM Volt reverse-engineering wiki lists battery
charge *percent* only as a **diagnostic** PID (`22 005B`, `raw · 100/255`),
not a broadcast field — so a cleanly filtered SOC may be poll-only.

The **Session-9 anchor drive** (see the checklist §2b) fixes this: start at a
full charge, sit engine-on in Park ~2 min to pin the "100 % display" raw
value, drive a steady pace so d(candidate)/d(SOC) is a clean slope, then run
~10 min in HOLD from the 2-bar mark for a charge-sustaining second anchor.
`tools/soc_log.py` now stamps all three candidate raw values on every log
line, and `--diag-soc` polls `22 005B` alongside, so that drive's capture
calibrates the mapping directly.
