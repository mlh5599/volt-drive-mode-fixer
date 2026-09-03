# Session 10 — focused interactive walk probe

**Why:** the closed-loop mode-walk self-test (`voltdmf-ctl walk-test`) has
failed or "passed only after a bunch of searching" on every in-car run since
the ungated-cursor fix (`03daa48`). The 5-leg auto-cycle is too coarse to
debug: it blind-taps up to 8 times per leg, scores off `0x1F4` byte 1 four
seconds later, and never shows what the byte-4 cursor did in between. The
byte-4 map and the "cursor steps ~40 ms after a tap" timing were both derived
from **physical** button presses (sessions 3–4), never from a frame-by-frame
log of an *injected* daemon walk.

This adds a way to probe **one** operator-chosen target at a time, with the
whole cursor trajectory on the record, driven entirely over SSH.

## The two commands

| command | effect |
| --- | --- |
| `voltdmf-ctl test-mode on` | suspend the reconciler — no setpoint is re-asserted between probes. In memory: a daemon restart resumes protection. SOC poll + trip log keep running. |
| `voltdmf-ctl test-mode off` | resume the reconciler. |
| `voltdmf-ctl probe <normal\|sport\|mountain\|hold>` | queue one closed-loop walk to `<mode>`. Fire-and-forget; the loop thread runs it on the next pass. |

`probe` runs the same machinery as one walk-test leg — its own
`SafetyGate(cooldown_s=0, allow_park=True)`, `force=True` — plus:

- the **per-tap trace** (`state.menu_cursor_raw` / `menu_cursor` / `drive_mode`
  / `menu_open_hint` logged after every tap's 0.2 s settle), and
- a **dense background sampler** (`_CursorSampler`) that snapshots the same
  fields every ~50 ms and logs a line on every change (+ 1 s heartbeat),
  timestamped from probe start. This is the ground truth the 0.2 s post-tap
  reads can't show — e.g. whether byte 4 is still stepping when the closed
  loop reads it.

### Verdicts

| verdict | meaning |
| --- | --- |
| `LANDED` | cursor reached the target during the walk **and** `0x1F4` byte 1 reads the target after the ~4 s commit-watch window |
| `CURSOR_ONLY` | cursor reached the target, byte 1 did not commit / reverted — **expected on a parked car** (byte 1 drifts back toward NORMAL a few seconds after a commit) |
| `MISS` | `MAX_WALK_TAPS` (8) taps, the cursor never read the target |
| `BLOCKED` | a `SafetyGate` precondition stopped the walk (bus quiet, shift R/N, implausible speed) |

For refining the **walk** on a parked car, `CURSOR_ONLY` is a pass — "cursor
reached target" is the signal. A byte-1 `LANDED` needs the car in Ready/Drive.

## Reading a result

```
ssh voltpi voltdmf-ctl --json status | jq .state.probe
```

gives `{verdict, target, origin, taps, cursor_reached, cursor_at_walk_end,
byte1_after, taps_trace:[...], samples:[...]}`. The journal has the same, live:

```
ssh voltpi journalctl -u voltdmf --since -2min --no-pager | grep -iE 'probe|tap'
```

## Administration protocol (Claude drives it over SSH)

1. `ssh voltpi voltdmf-ctl arm` if not already armed; `ssh voltpi voltdmf-ctl test-mode on`.
2. Per round:
   - operator names a target in chat, physically sets the car to a known
     start mode (cluster), confirms "go";
   - `ssh voltpi voltdmf-ctl probe <target>`;
   - wait ~25 s, then pull `status .probe` + the `probe`/`tap`/`sample`
     journal lines; report the verdict and the full trace;
   - operator says what the cluster actually did; compare, refine
     (`CURSOR_SETTLE_S`, `WALK_GAP_S`, the byte-4 map, `MAX_WALK_TAPS`).
3. `ssh voltpi voltdmf-ctl test-mode off` (or let the next key cycle restore it).

## Refinement log

_(filled in as rounds run)_
