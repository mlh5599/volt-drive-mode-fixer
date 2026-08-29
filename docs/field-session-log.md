# On-Vehicle Field Session Log

Running narrative of the field sessions against the real car. Pairs with
`phase-c-field-checklist.md` (the procedure) and `signals-confirmed.md` (the
decoded-signal reference). Newest session first.

---

## Session 2 — 2026-08-29 (Phase C.5 — injection reproduces the menu walk; not yet an automated PASS)

Car in Park, brake set, full READY. Added a scoped sudoers drop-in on the Pi
(`/etc/sudoers.d/50-voltdmf-canlink`,
`mike ALL=(root) NOPASSWD: /usr/bin/ip link set can0 *`) so the bus can be
cycled remotely; `rm` it when field work is truly done (still present — we
paused, not finished).

### The "blocked on hardware" call from earlier in the session was WRONG

An earlier pass this session diagnosed a marginal PiCAN2 TX solder joint.
The owner rejected that ("Soldering is fine"), which forced a re-diagnosis.
The real causes of the failed injection were all in software/approach:

1. **Wrong ID.** `0x1F4` is the drive-mode **status/echo** (byte 1 = latched
   mode — that part is still confirmed). It is *not* the button input.
   Time-paced frames we transmitted on `0x1F4` collided destructively with
   the body module's own 40 Hz `0x1F4` stream (same arbitration ID, different
   payload → bit errors → TEC ran to ERROR-PASSIVE). That "intermittent TX"
   was **same-ID contention**, not a bad joint.
2. **`0x1E1` is the button after all — it was retired on a shallow look.**
   Session 1 / early Session 2 checked only bytes 0–3 (always `00`). The full
   per-byte diff of a physical press shows **byte 4 bit 7 (`0x80`) = drive-mode
   button pressed**. This is the same ID/bit the Gen 2 prior art
   (`vix597/chevy-volt-trip-mode`) injects.
3. **A static `0x1E1` frame is ignored.** `0x1E1` low 2 bits of byte 4 are a
   rolling counter and bytes 5–6 are a counter-derived tail. A fixed
   `00 00 00 00 80 00 00` reaches the bus but the cluster drops it (only the
   drive-mode screen woke).

### `0x1E1` "ASCMSteeringButton" — captured physical press

7-byte frame, ~40 Hz. Idle: `00 00 00 00 0c YY ZZ` with counter `c` = 0..3 in
byte 4's low bits and `(YY,ZZ)` = `(1C,C0)/(10,F0)/(14,E0)/(18,D0)` for
counter `0/1/2/3`. A **real single press** is just **~14 consecutive frames
with byte 4 bit 7 set** (`83 82 81 80 …` — the counter keeps advancing, tail
still tracks it), then bit 7 clears. ~350 ms total. No change to bytes 0–3.

### Injection method that works — TRACKING ECHO

`voltdmf/canio.py::send_mode_button_press()` rewritten: for
`PRESS_TRACK_FRAMES` (≈16) iterations, wait for the module's next live
`0x1E1`, OR `0x80` into its byte 4, and send that back into the ~24 ms gap
before the module's next frame. Result on the wire: a near-exact replica of
the 14-frame physical press, counter valid and advancing, **no collisions**
(we transmit between the module's frames, never on top of them).

- Across ~10 on-vehicle runs `can0` stayed **ERROR-ACTIVE**, **zero error
  frames**, every tracked frame made it onto the wire (verified by
  `candump can0,1E1:7FF` — e.g. 176/176 across a full cycle).
- The old blind back-to-back blast is gone: long blasts get punched through
  by module frames at random points, so the cluster saw an unpredictable
  0–2 presses per burst.

### Drive-mode MENU model (owner watched the dash and described it)

- **Menu CLOSED** → any single press opens the menu and **selects NORMAL**,
  whatever the current mode is.
- **Menu OPEN** (next press within ~2 s) → each press **walks the cursor**
  `NORMAL → SPORT → MOUNTAIN → HOLD → NORMAL → …`.
- **~3 s with no press** → the menu times out, the cursor commits, and the
  next press starts over from NORMAL.

So reaching a mode is a **walk**: `index + 1` rapid presses (NORMAL 1, SPORT
2, MOUNTAIN 3, HOLD 4), each < ~2 s apart, then stop and let it commit. This
also means "reset to NORMAL" for the daemon is a *single* isolated press.

### Result

- **A full injected walk `NORMAL → SPORT → MOUNTAIN → HOLD → NORMAL` was
  reproduced and visually confirmed on the dash by the owner** ("the walk
  looked right"), with `can0` ERROR-ACTIVE and zero error frames. The
  `0x1F4` byte-1 timeline for that run stepped `00 → 80 → 20 → 08 → 00` in
  lock-step.
- **Not yet a clean automated PASS.** Two things stop `tools/inject_test.py`
  from self-verifying on a parked car:
  - `0x1F4` byte 1 **lags the commit by ~7–9 s** and, with the car stationary
    in Park, the cluster **reverts toward NORMAL** a few seconds after
    committing a non-NORMAL mode. So byte 1 is an unreliable commit signal
    here — real verification needs a drive (where the modes hold).
  - Walk **timing is sensitive**: the ~400 ms tracking press + `--walk-gap`
    can exceed the ~2 s menu window, so a 4-press walk sometimes times out
    mid-walk and lands short (e.g. on SPORT instead of HOLD). Needs a
    known-good physical-walk capture to tune against.

### Code / tooling changed this session

| File | Change |
|---|---|
| `voltdmf/canio.py` | TX path retargeted `0x1F4` → **`0x1E1`**. `send_mode_button_press()` is now the tracking-echo press (`PRESS_TRACK_FRAMES`, `_next_button_frame()` helper, `can.CanError` back-off, static fallback). `read_drive_mode()` still reads `0x1F4`. Blast/pacing knobs kept only as back-compat aliases. |
| `voltdmf/signals.py` | `drive_mode_button` addr `0x1F4` → `0x1E1`, `confirmed=False`, note rewritten (address confirmed, injection efficacy still unproven). `drive_mode_status` `0x1F4` `confirmed=True` unchanged. |
| `tools/inject_test.py` | Rebuilt around the menu-walk model: `--target <mode>` / `--cycle`, `--walk-gap`, `--commit` (polls `0x1F4`), `--frames`, `--repeat`. Old `--steps` / `--rate-hz` / `--burst-ms` removed. |

Local `pytest`: **59 passed**. No signal-decode behaviour changed, so no test
edits were needed.

### Next session — pick up here

1. **Capture a known-good physical walk.** `candump -l 'can0,1E1:7FF,1F4:7FF'`,
   then owner does NORMAL → … → HOLD as 4 fast presses. Measure: the real
   menu-open window, the physical inter-press interval, and how long byte 1
   takes to latch. Set `--walk-gap` (and `PRESS_TRACK_FRAMES` if needed) so
   all 4 injected presses land inside one menu session.
2. Re-run `inject_test.py --yes-stationary --cycle` and get the owner to
   confirm every step on the dash. `can0` must stay ERROR-ACTIVE, zero error
   frames.
3. **Real verification is the first dry-run drive** — that is where modes
   actually commit and hold, so the daemon (still `--dry-run`) can be checked
   against `journalctl` while someone else drives. Do the OBD-II DTC scan
   here.
4. When the walk is reliable: bake `PRESS_TRACK_FRAMES` / `RELEASE_GAP_S` /
   the walk-gap into `voltdmf/canio.py` + `voltdmf/modecycle.py`, flip
   `drive_mode_button.confirmed = True`, and record the numbers here.

### `voltdmf/modecycle.py` needs the menu model too (back at a keyboard)

`ModeCycleController.switch_to()` currently assumes "N presses = N cursor
steps from the current mode". The real model is: one isolated press → NORMAL;
to reach a non-NORMAL mode, fire `index+1` presses < 2 s apart then stop.
`BUTTON_PRESS_COOLDOWN_S` (0.75) is the *walk-gap*, not a post-switch cooldown.

### Left running / state

- Daemon (`/opt/voltdmf/repo`, systemd) still **`--dry-run`**;
  `voltdmf` + `voltdmf-can0-up` both `active`.
- `can0` up, ERROR-ACTIVE, but **`restart-ms` got reset to 0** at some point
  (a `systemctl` touch of `voltdmf-can0-up`). Next session bring it up with
  `sudo ip link set can0 down && sudo ip link set can0 up type can bitrate
  500000 restart-ms 100` before any TX.
- Dash last seen **SPORT** on `0x1F4` (bouncing NORMAL↔SPORT on the parked
  car's own revert). No new DTCs observed; proper OBD-II scan still owed.
- `~/vdmf` on the Pi = plain clone + scp'd `voltdmf/canio.py` and
  `tools/inject_test.py`.

---

## Session 1 — 2026-08-29 (Phase A + Phase C partial + Phase C.5 started)

### Done

**Phase A — `can0` bring-up: PASS.**

- MCP2515 / PiCAN2 comes up clean at **500 kbit/s** on OBD-II pins 6/14
  (GM Global A HS powertrain bus).
- `ip -details link show can0` → `ERROR-ACTIVE`, error counters at zero, no
  fallback to a lower bitrate needed.
- `candump can0` streams steadily; RX decode path is solid all session.

**Phase C — current drive-mode STATUS signal: CONFIRMED.**

- Method that worked: 20 s stationary dwell in each mode
  (NORMAL → SPORT → MOUNTAIN → HOLD, Park + brake) plus 5 timestamped button
  taps, all in one `candump -l` log, then an offline per-byte
  "steady within a mode / differs across modes" scan.
- **`0x1F4`** is the drive-mode message on the 500k HS bus, 6-byte payload
  `00 <mode> 00 00 <btn_ramp> <btn_down>`, ~40 Hz from a body module.
- **Byte 1 = latched current mode** — 720/720 frames steady in each mode:

  | Mode | `0x1F4` byte 1 |
  |---|---|
  | NORMAL | `0x00` |
  | SPORT | `0x80` |
  | MOUNTAIN | `0x20` |
  | HOLD | `0x08` |

- Byte 5 = `0x80` while the button is physically held, else `0x00`.
- Byte 4 = a decay ramp `0x80 → 0x40 → 0x20` (~0.27 s/step) the button module
  runs *after* release; the mode latches ~2–3 s later.
- Secondary echoes: `0x287` byte 1 tracks mode (`00/80/08/10`); `0x3D1` byte 0
  only separates SPORT/MOUNTAIN. Not needed — `0x1F4` byte 1 is authoritative.
- The earlier failure was the wrong candidate ID: `0x1E1` (from the Gen 2
  reference project) does not exist on this bus, and diffing on a running car
  was too noisy. `0x1E1` is fully retired now.

**Phase C.5 — injection: STARTED, not passed.**

- Our injected `0x1F4` frames **do reach the cluster** — every shot woke the
  drive-mode screen on the dash. The car listens to us.
- Got the mode to actually move: one shot from NORMAL landed on HOLD
  (three steps) with the byte4 ramp included.

### Findings

1. **The press is duration-gated, not edge-gated.**
   - ~0.3 s of `byte5=0x80` → only wakes the screen, no mode change.
   - ~1.2 s (down-hold + the 3-value byte4 ramp) → counted as ~3 presses.
   - Working theory: the consumer counts one press per ~0.4 s that the button
     reads active, i.e. it auto-repeats like a held key.
2. **The byte4 release ramp auto-repeats when injected** — each of
   `0x80 / 0x40 / 0x20` lands as another press. So we do **not** reproduce it;
   `PRESS_RAMP_VALUES` is now empty.
3. **Transmitting is hard on this MCP2515 while RX stays perfectly clean.**
   - The controller walks `ERROR-ACTIVE → ERROR-PASSIVE → BUS-OFF` over a
     handful of injection shots. BUS-OFF count went 0 → 3 across the session.
   - `restart-ms 0` (what the `voltdmf-can0-up` unit currently sets) means no
     auto-recovery, so later shots fired into a dead interface (thousands of
     TX drops).
   - RX-clean + TX-degrading points at the **physical layer** — most likely a
     marginal solder joint on the CAN-H / CAN-L tap. Reflow when the Pi is
     next out of the car.
   - Compounding factor: the real body module keeps streaming its 40 Hz idle
     `0x1F4` while we inject, and an over-aggressive flood (200 Hz / 250+
     frames) both looked like one solid press *and* couldn't keep the bus
     alive.
4. The car kept dropping out of READY during the session (bus goes quiet →
   our TX gets no ACK → TEC runs away → BUS-OFF). Needs the car in **full
   READY** for every injection attempt, not just accessory/RUN.

### Mitigations applied this session

- Manual bring-up with recovery: `sudo ip link set can0 up type can bitrate
  500000 restart-ms 100` (self-heals BUS-OFF in 0.1 s).
- Dialed injection down; made rate / duration / ramp tunable from the CLI
  (`tools/inject_test.py --rate-hz --down-ms --no-ramp`).
- Added a pre-flight + per-shot guard to `inject_test.py`: it refuses to
  transmit unless `can0` is `ERROR-ACTIVE`, and aborts the run if the state
  degrades mid-sequence.

### Code changes committed this session

| File | Change |
|---|---|
| `voltdmf/signals.py` | `drive_mode_status` → `0x1F4`, `confirmed=True`. `drive_mode_button` → `0x1F4` (address confirmed, injection unproven). New `decode_drive_mode()` (byte-1 lookup) + `_DRIVE_MODE_BY_BYTE1`. |
| `voltdmf/canio.py` | Single TX path retargeted to `0x1F4`. New press model: one solid `byte5=0x80` block, **100 Hz, 0.45 s, no byte4 ramp**. New `read_drive_mode()` RX helper. `_tx_frame` / `*_UNCONFIRMED` constants removed. |
| `tools/inject_test.py` | Rewritten for on-vehicle use: before/after mode read-back per shot, `can0` health guard, `--presses/--gap/--shots/--settle/--rate-hz/--down-ms/--ramp` knobs. |
| `tests/test_signals.py` | Dropped `test_nothing_is_confirmed_yet`; added `test_confirmed_signal_set`, `test_decode_drive_mode`, `test_decode_drive_mode_unknown_and_short`. |
| `docs/signals-confirmed.md` | Status signal section filled in; button/injection section rewritten with the Phase C.5 findings. |

Local `pytest`: **59 passed**.

### Post-session — reference-project review (`vix597/chevy-volt-trip-mode`)

Compared our approach against the Gen 2 prior art. It solved an easier
problem (different message `0x1e1`, no hold-timer ramp) but its *structure*
is the fix for both our symptoms:

- **One press = a short burst of identical "button down" frames, then STOP
  transmitting.** It never sends a release frame — the real steering
  module's own idle `0x1e1` frames are the "button up". No ramp.
- **`BUTTON_PRESS_COOLDOWN = 0.75 s` between bursts.** Multi-step switches
  are discrete bursts 0.75 s apart, never a continuous hold. That gap both
  separates one counted press from the next (our overshoot) and gives the
  transmit-error counter room to recover (our BUS-OFF).
- It blind-counts with a 60 s cooldown and no status signal; we have
  `0x1F4` byte 1, so we can close the loop instead.

**Applied to the code (committed this session, on the branch):**

- `voltdmf/canio.py` — `send_mode_button_press()` is now burst-and-release:
  `PRESS_BURST_FRAMES` frames of byte5=0x80 at `PRESS_FRAME_INTERVAL_S`,
  then return, no release frame. New `RELEASE_GAP_S = 0.75` (callers must
  leave that much silence before the next press). `PRESS_RAMP_*` /
  `PRESS_IDLE_FRAMES` gone.
- `tools/inject_test.py` — rewritten closed-loop: reads `0x1F4` after every
  press, prints the transition + `can0` state, stops when the dash reaches
  the goal or hits `--max-presses`. `--steps N` (advance N single steps) or
  `--target <mode>`. Knobs: `--burst-ms`, `--rate-hz`, `--gap` (floored at
  `RELEASE_GAP_S`), `--settle`, `--step-confirm`.

### Next session — pick up here

1. **Car in full READY**, foot on brake, Park. Confirm `candump can0` streams
   steadily *before* touching anything.
2. Redeploy to the Pi:
   ```
   scp voltdmf/canio.py    voltpi.haguehome.lan:~/vdmf/voltdmf/
   scp tools/inject_test.py voltpi.haguehome.lan:~/vdmf/tools/
   ```
   (`~/vdmf` on the Pi is a plain clone with our files scp'd over; the systemd
   daemon at `/opt/voltdmf/repo` is untouched and still dry-run.)
3. Bring the bus up **with** auto-recovery — do **not** use
   `systemctl restart voltdmf-can0-up` (that unit sets `restart-ms 0`):
   ```
   sudo ip link set can0 down
   sudo ip link set can0 up type can bitrate 500000 restart-ms 100
   ip -details link show can0     # expect: can state ERROR-ACTIVE
   ```
4. Second shell: `candump can0 | grep -iE 'err'`.
5. Closed-loop sweep — one step at a time, watching the dash and `can0`:
   ```
   /opt/voltdmf/venv/bin/python ~/vdmf/tools/inject_test.py \
       --yes-stationary --steps 1 --burst-ms 450
   ```
   - Overshoots (one run advances >1 step) → lower `--burst-ms` (350, 300).
   - Wake-only / "no change" → raise `--burst-ms` (550) or add `--rate-hz 150`.
   - Once single steps are clean, do a full lap: `--steps 4` should return to
     the start in exactly 4 presses, `can0` staying `ERROR-ACTIVE`, zero
     error frames. Or target a specific mode: `--target hold`.
6. When a config wins: set `voltdmf/canio.py` `PRESS_BURST_FRAMES` /
   `PRESS_FRAME_INTERVAL_S` to the winning values, flip
   `drive_mode_button.confirmed = True`, and record the numbers here.
7. OBD-II DTC scan; clear only DTCs we caused and understand.

### Still needs a real drive (deferred, not attempted)

- **SOC signal** — `0x206` never appears on this bus (Gen-2 ID is wrong for
  Gen 1). Needs a discharge drive with `tools/watch_soc.py` to find the real
  ID + scaling for `SOC_KWH_PER_COUNT` / `GEN1_PACK_USABLE_KWH`.
- **Shift / PRNDL** — `0x135` bytes 0–6 constant in Park (byte 7 is a free
  counter), `0x1F5` static. Nothing to decode without moving the shifter.
  Keeps `SafetyGate(allow_unknown_shift=True)` for now.
- **Ignition behaviour** — does the bus go quiet with the car off and how
  long after; is the drive mode remembered or reset to NORMAL on restart.
  Run `tools/ignition_check.py`.

### Follow-ups for "back at a keyboard" (no car needed)

- Wire `signals.decode_drive_mode` into `voltdmf/daemon.py` as
  `current_mode_source`, replacing `PressCountingModeTracker`.
- Add `0x1F4` to `signals.is_signal_frame` and the RX `_DecodeListener`
  (left out on purpose this session to keep the running daemon untouched).
- homelab-ansible `roles/voltdmf`: change
  `templates/voltdmf-can0-up.service.j2` to `restart-ms 100` and add a
  `voltdmf_can_restart_ms` default — an in-car CAN device must self-recover
  from BUS-OFF.
- Once injection passes: bump `voltdmf_version` in
  `inventories/production/host_vars/voltpi.haguehome.lan/vars.yml` to the
  confirmed-signals commit. Keep `voltdmf_dry_run: true`.
