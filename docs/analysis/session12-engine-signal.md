# Session 12 — finding the range-extender engine on the bus

No drive was needed for this one. The three `candump` captures already in
`captures/` contain two labelled engine starts between them, so the engine
signal was mined offline on **2026-09-05** and the decoders in
`voltdmf/signals.py` were written straight from that evidence.

Everything below regenerates with
[`../../tools/engine_check.py`](../../tools/engine_check.py), which replays a
capture through the shipped decoders:

```
tools/engine_check.py captures/candump-2026-08-30_131932.log
tools/engine_check.py captures/candump-2026-08-30_181250.log
tools/engine_check.py captures/candump-2026-08-30_212037.log --since 1788198000
```

(Session 9's capture carries a burst of stale pre-NTP frames at the head from
a boot clock jump; `--since 1788198000` slices them off, same as
`soc_report9.py`.)

## Why

From the 2026-09-04 drive, in the owner's words:

> SOC hold worked ok but after parking for a while then restarting, the
> battery had drained enough that the car switched itself over to the
> gasoline engine. When this happens the hold option isn't available and the
> drive mode fixer tried, but wasn't able to set hold.

Once the car gives up on the pack for a key cycle it runs the engine and sits
in **NORMAL**, and the centre-stack energy-mode menu stops offering HOLD and
MOUNTAIN at all. The reconciler is level-triggered, so it saw NORMAL ≠ HOLD,
walked the menu, failed, and did it again on the next 10 s cooldown for the
rest of the drive. Taps on a menu that has nothing on it are the one kind of
transmission this project has no excuse for, so the fix needs the daemon to
*see* the engine.

## The captures, and why they are enough

The trick is that these captures are already labelled — not by a marks file,
but by physics.

| Capture | Engine | Evidence |
|---|---|---|
| `candump-2026-08-30_181250.log` (35 MB) | never on | 4.7 min EV-only errand — the negative control |
| `candump-2026-08-30_131932.log` (214 MB, session 8) | **starts at +1694 s** | full drain 10/10 → 0/10 bars; the car forced itself into charge-sustaining at the end |
| `candump-2026-08-30_212037.log` (228 MB, session 9) | **starts at +1076 s** | driver selected HOLD; SOC then sat pinned at 32.9 % for 13 min at 70 mph, which only happens with the engine turning |

Two starts, two long engine-off legs, and one whole capture with no engine at
all. Any candidate byte has to be quiet across ~50 min of EV driving and move
at both starts.

## `0x4C5` byte 2 — the engine leg

Five-byte frame at ~2 Hz. **Byte 2 is the only byte that ever changes**; the
other four are always `00`.

| byte 2 | frames (all three captures) | meaning |
|---|---|---|
| `0x49` | 6 309 | off — the car is an EV |
| `0x59` / `0x87` / `0xB5` | 4 each | the ~1.5 s ramp between the two states |
| `0xDD` | 1 414 | on an engine leg |

```
1788115698.926 4C5#0000590000     session 8, 0-bar mark
1788115699.436 4C5#0000870000
1788115699.929 4C5#0000B50000
1788115700.439 4C5#0000DD0000     <- and it stays here to the end of the capture
```

Two things worth being precise about, because the first draft of the decoder
got both wrong:

* The three intermediate values are **not** a "starting" state. Session 9
  contains the ramp running the other way (`0xDD → 0xB5 → 0x87 → 0x59 →
  0x49`, 1788199956–199958) when an early engine leg gave up after 8 s. They
  are a transition at either edge, which is why `EngineState.TRANSITION` is
  what they decode to.
* `0xDD` means **"on an engine leg"**, not "the crankshaft is turning this
  instant". It held for the full 701 s of the session-9 HOLD leg, straight
  through five stretches where the engine was demonstrably not burning
  anything (below). That is the broader of the two readings, and the useful
  one here: the menu entry stays missing across those gaps too.

## `0x3F9` bytes 1–2 — a fuelling accumulator

Eight-byte frame at ~4 Hz. Bytes 1–2 big-endian are a monotone counter; byte
0 was `0x00` in **all 15 478 frames**, so it is left out of the decode (an
unrelated flag byte creeping in there would read as a false "engine
running"). Bytes 3–7 are a static `28 51 59 89 6x` pattern.

| Capture | frames | counter steps |
|---|---|---|
| EV-only | 1 123 | **0** |
| session 8 | 6 936 | 160, all in the 40 s tail after the start |
| session 9 | 7 419 | 2 349, all after +1076 s |

Zero movement across ~50 minutes of EV driving is what makes this an engine
signal rather than a timer. Step rate ranges 4–159 counts/s (median 60) and
tracks load.

It is **not** a revolution counter, though, and this is where the offline
mining paid for itself:

* It **leads `0x4C5` by 32.8 s (session 8) and 41.9 s (session 9)** at the
  head of a leg. The counter is already stepping while `0x4C5` still reads
  `0x49`.
* It **freezes mid-leg** for long stretches while `0x4C5` holds `0xDD` —
  five windows in session 9 of 12.7 s, 23.0 s, 38.7 s, 40.0 s and 43.3 s. The
  40 s one starts at +1326.8 s, and cross-referencing `0x3E9` (speed) over
  that window gives the answer:

  ```
  +1310s  70.3 mph          counter stepping
  +1330s  47.5 mph          counter frozen  <- overrun, no fuel
  +1352s   0.0 mph          counter frozen  <- stopped
  +1366s  26.5 mph          counter stepping again
  ```

  A deceleration from 70 mph to a standstill and away again. The counter
  stops on overrun fuel cut-off and at rest — it counts fuel (or work), not
  revolutions.

## Reading them together

Neither signal alone covers a whole engine leg, and they are blind in
opposite places, so `VehicleState.engine_running` takes the **union**: a
moving counter overrides an `off` `0x4C5`, and a `running` `0x4C5` stands on
its own.

```
                     +1076s                                        +1855s
counter (0x3F9)  ────┤████████│░░░░│███│░░░░│██████████████████████├────
0x4C5            ────────────┤███████████████████████████████████████├──
                             +1118s
union            ────┤███████████████████████████████████████████████├──
```

Both errors of the union point the same way — it calls the engine running a
little early and a little late — and the only cost of that is a mode walk
that waits. Getting it wrong the other way is what put taps on a dead menu in
the first place.

`tools/engine_check.py` times both disagreements. Session 9: **74 s** with the
counter moving while `0x4C5` read off (the 42 s lead-in plus the 32 s gap
between the abandoned 8 s leg and the real start) and **173 s** with `0x4C5`
running while the counter was frozen. Session 8: **33 s** and **0 s** — its
engine leg is only 40 s long and never lifts off the throttle. The EV-only
capture: **0 s** and **0 s**, as it must be. Every second of disagreement sits
inside an engine leg; the union is never wider than the leg itself.

## The failure state, on the wire

The session-8 start is the exact case the owner hit:

```
+1694.0s   0x3F9 counter starts stepping
+1728.4s   0x4C5 byte 2 reaches 0xDD
           0x1F4 byte 1 == 0x00  (NORMAL)  ← for the whole leg
```

Engine running, car in NORMAL, and the pack at the 0-bar mark. Session 9's
leg, by contrast, shows `0x1F4` byte 1 at `0x08` (HOLD) throughout — the
driver had selected HOLD *before* the pack was written off, and the car
honoured it. Same engine, entirely different menu.

That difference is the whole gate:

> engine running **and** car in NORMAL **and** no fresh SOC reading above
> `DEPLETED_PERCENT` (24 %) ⇒ HOLD and MOUNTAIN are not on the menu, so do
> not walk to them.

The 24 % release matters as much as the block. A running engine on a healthy
pack means cabin heat (ERDTT) in the cold, an engine-maintenance cycle, or
our own HOLD doing its job — in all of those the menu is complete, and
blocking would break the feature to fix a symptom. Session 9's calibration
puts 0 bars at ≈19.6 % and 1 bar at ≈26.7 %, so 24 % sits between them: below
the 30 % floor the reconciler works at, above the point the car gives up.

## What shipped

- `voltdmf/signals.py` — `EngineState`, `decode_engine_state` (`0x4C5` b2),
  `decode_engine_run_counter` (`0x3F9` b1–2), both `confirmed=True` in
  `SIGNAL_IDS`, both in `is_signal_frame`.
- `voltdmf/state.py` — the union in `VehicleState.engine_running`, with
  `engine_counter_advancing()` returning a tri-state so "not watched long
  enough yet" never reads as "off".
- `voltdmf/reconciler.py` — `charge_sustaining_block()`, consumed by
  `Reconciler.desired_mode` (withholds the target, leaves the reason in
  `ice_block`) and by `SafetyGate._precondition_failure` (so a hand-typed
  `voltdmf-ctl set-mode hold` gets a sentence instead of twelve taps).
- `voltdmf/daemon.py` — logs the block once on each edge, not once a second;
  `voltdmf-ctl status` prints the engine read and `NOT ACTING: …`; the LCD
  SOC row gains an `ICE` / `NO HOLD` tag.

## Open

- **No third capture with an engine start.** Both starts here are on the same
  car in the same week. A cold-weather ERDTT start (engine on, pack full)
  would exercise the 24 % release, which so far is reasoned from the SOC
  calibration rather than observed.
- **`0x4C5`'s 33–42 s lag is unexplained.** It is consistent across both
  starts, so something real is being reported late — catalyst light-off and a
  cluster telltale filter are both plausible and neither is confirmed.
- **The counter's unit is unpinned.** Only "did it change recently" is used,
  which is deliberate: a wrap or a rescale cannot break that.
- ~~**Retry backoff is still missing.**~~ Built the same day:
  `safety.AttemptBudget` gives up on a target after three walks that reached
  the wire, whatever the reason they failed. This gate stays worth having —
  it costs *zero* taps and says why, where the budget spends 36 first — but
  the unknown-reason case is no longer unbounded.
