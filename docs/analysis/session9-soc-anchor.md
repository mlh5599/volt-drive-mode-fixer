# Session 9 — the SOC anchor drive

Visual write-up of the Session-9 calibration drive (2026-08-31). Pairs with
the narrative in [`../field-session-log.md`](../field-session-log.md) and the
procedure in [`../phase-c-field-checklist.md`](../phase-c-field-checklist.md).
Session 8's candidate hunt is in
[`session8-soc-candidates.md`](session8-soc-candidates.md).

Charts and summary data regenerate from the capture with
[`../../tools/soc_report9.py`](../../tools/soc_report9.py); the small JSON
([`data/session9-soc.json`](data/session9-soc.json)) is committed so the SVGs
rebuild without the 228 MB `candump` log:

```
tools/soc_report9.py --data docs/analysis/data/session9-soc.json \
    --out-dir docs/analysis/img --prefix session9-
```

## The drive

One continuous run, **~31 min**. The pack started at **89.8 %** (first diag
read, engine-on, rolling) and was driven down a steady highway pace to the
**2-bar** gauge mark; at that point the driver put the car in **HOLD** and
kept driving another ~13 min. Unlike the Session-8 plan there was **no
engine-on Park idle at 100 %**, so there is no true full-charge raw anchor —
the 89.8 % start and the HOLD plateau serve instead.

- gauge marks + inline diag SOC: `captures/vdmf-soclog-20260830-212037.log`
- raw bus: `captures/candump-2026-08-30_212037.log` (228 MB; carries a
  ~4 600-frame burst of stale pre-NTP frames at the head from a boot clock
  jump — `soc_report9.py` and any `mine_capture.py` run must slice to
  `epoch >= 1788198000`)

Both files are kept local (git-ignored); this write-up is the committed
product.

## The SOC source — UDS `22 005B`

This Gen 1 dash shows **no state-of-charge percent**, and Session 8 found no
broadcast frame that cleanly carries it. Session 9 therefore polled the
diagnostic PID the GM Volt RE wiki lists for pack charge:

```
request   7E0 # 03 22 00 5B 55 55 55 55
response  7E8 # 04 62 00 5B <XX> AA AA AA        SOC% = XX * 100 / 255
```

`soc_log.py --diag-soc` polled it every 10 s. Result over the drive:

- **185 replies / 186 requests, 1 NRC**; `can0` stayed `ERROR-ACTIVE`
  throughout. Active TX on this bus is reliable for a full drive.
- decode is exact and linear: `0xE5` → 89.8 %, `0xC2` → 76.1 %, `0x54` →
  32.9 %. Matches `soc_log`'s own decode.
- EV-drive depletion rate at 70–82 mph ≈ **3.1 % / min** (89.8 % → ~30 % in
  ~19 min).

![Diagnostic SOC across the whole drive, with the eight gauge-bar drops and
the HOLD region marked, vehicle speed as a faint underlay](img/session9-timeline.svg)

The trace falls smoothly to a **minimum of 29.8 %** at +1162 s, then — once
HOLD is engaged — **stops falling and drifts back up** to 33.7 % by the end
of the drive. The gauge held at 2 bars for that whole tail.

## Calibration — gauge bars vs SOC

Diagnostic SOC at each gauge-bar drop (values from the `soc_log`
`GAUGE-DOWN` marks; the chart samples the capture, hence ±0.1):

| drop | diag SOC | raw | speed at drop |
|-----:|---------:|:---:|--------------:|
| 10→9 | 83.5 % | `0xD5` | ~21 mph (decel) |
| 9→8  | 76.1 % | `0xC2` | ~80 mph |
| 8→7  | 69.8 % | `0xB2` | ~82 mph |
| 7→6  | 60.0 % | `0x99` | ~82 mph |
| 6→5  | 55.3 % | `0x8D` | ~82 mph |
| 5→4  | 47.8 % | `0x7A` | ~74 mph |
| 4→3  | 41.2 % | `0x69` | ~73 mph |
| 3→2  | 33.7 % | `0x56` | ~68 mph |
| 2→1  | — not reached (SOC bottomed at 29.8 %, then HOLD) | | |

![Diagnostic SOC at each gauge-bar drop with the least-squares line through
the eight points and the 2-bar HOLD-floor band](img/session9-calibration.svg)

Least-squares through the eight points:

```
SOC% ≈ 7.07 · (bars shown) + 19.6        r = 0.999
```

≈ **7 % SOC per bar**, near-perfectly linear. Extrapolating the unseen
`2→1` edge puts the **2-bar band at ≈ 27–34 % SOC**. So a HOLD floor that
targets *"don't drop below 2 bars"* is:

> **force HOLD when diag SOC ≤ ~33 %** (raw ≤ `0x54`).

One bar of margin (target 3 bars) would be ~41 % (`0x69`).

## HOLD behaviour

The driver toggled HOLD by hand near the 2-bar mark (no button gesture for
it, so the exact instant isn't logged — the chart marks it at the `3→2`
drop, +1066 s). From there:

- diag SOC **stopped declining**: 29.8 % minimum at +1162 s, then a slow
  climb to 33.7 % over the next ~11 min of driving;
- the gauge stayed pinned at **2 bars** the whole time.

This is the design assumption confirmed on-road: at ~30 % SOC, HOLD is
charge-sustaining (slightly charge-*positive* here), so a reconciler that
forces HOLD at the floor will actually hold the line rather than just slow
the bleed.

## Candidate check — do the Session-8 broadcasts track charge?

Session 8 nominated `0x3E3` b0/b1/b6, `0x228` b2 and `0x186` b6 as SOC
candidates but had no absolute reference. Plotting each against the diag SOC
settles it — a signal that *is* a function of charge traces the dashed
corner-to-corner guide; width or back-tracking means it is not.

![Each candidate's raw value plotted against diagnostic SOC; 0x3E3/0x228/0x186
collapse into a vertical smear at the HOLD plateau, 0x096 b3 is a monotone
staircase](img/session9-candidates.svg)

- **`0x3E3` b0 (and b1, b6), `0x228` b2, `0x186` b6 — not SOC.** They drift
  down over the drain (which inflates a naive correlation to r ≈ +0.45) but
  at the HOLD plateau, where SOC sits at ~31 %, each one takes *every value
  it held earlier at 60–85 % SOC* — a vertical smear, not a curve. `0x3E3`
  b0 reads ~188 at both 76 % and 33 %. These are powertrain
  (load / current / torque) signals; the `soc_log` `cand[…]` column already
  showed them swinging with speed and accel. **The Session-8 SOC hypothesis
  is falsified.**
- **`0x096` byte 3 — monotone, but far too coarse.** In the `x F0 0A xx` mux
  frame it steps `13 → 9` across the whole usable pack (r = 0.95 vs diag
  SOC), and it flattens during HOLD like SOC does. But that's **~13 % SOC
  per count** — useful only as a passive sanity check, not as a control
  input. Likely an integer kWh-remaining or coarse energy estimate.

**No usable passive broadcast SOC was found.** The reconciler's SOC-HOLD
floor will run off the `22 005B` poll.

## What this changes

- `tools/signals.py` — `22 005B` is the SOC reader (`SOC% = raw·100/255`).
  `SOC_KWH_PER_COUNT` / `GEN1_PACK_USABLE_KWH` stay unset: the floor is a
  percent threshold (≤ ~33 % = 2 bars), not an energy budget, so they may
  not be needed at all.
- The [reconciler model](../../DESIGN.md) SOC-HOLD floor now has a concrete
  trip point: **diag SOC ≤ 33 %** (2 gauge bars), HOLD verified
  charge-sustaining there.

## Still open

- **Passive broadcast SOC** — still none. `mine_capture.py --monotonic` on
  this capture surfaces only timers/odometers and the coarse `0x096` b3.
  Not worth another dedicated drive; the poll works.
- **No 100 % Park anchor** — the raw value at a known full charge was never
  logged. Only matters if a kWh mapping is ever needed.
- **Speed decode** (`0x3E9` b0-1 BE ÷ 64 km/h) is internally consistent this
  session (80–82 mph sustained, 0 at stops) but still not speedo-verified.
