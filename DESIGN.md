# Volt Drive Mode Fixer — Design

**Status (2026-08-31):** deployed on the Pi (`voltpi`), field testing (Phase
C). Mode-button input (`0x1E1`), current-mode status (`0x1F4` byte 1) and
shift/PRNDL (`0x1F5` byte 3) are confirmed on-road; the closed-loop menu walk
has an on-road PASS. Session 9 resolved SOC: the `22 005B` UDS poll gives
exact pack percent (`raw·100/255`) and the gauge↔SOC curve is near-linear
(`SOC% ≈ 7.07·bars + 19.6`, r = 0.999), so the level-triggered reconciler in
"Mode policy" — with the SOC-HOLD floor keyed off that poll and `0x096`
byte 3 as a coarse failsafe — is now **implemented**. `--dry-run` is gone:
the daemon boots **armed** but passive (`default_setpoint: auto` → no target
until a driver selection or the SOC floor), and `voltdmf-ctl disarm` is the
mid-drive stop. The out-of-repo `roles/voltdmf` still needs its ExecStart /
`config.yaml` migrated to match. Progress and per-drive procedures live in
`docs/phase-c-field-checklist.md`; the on-vehicle narrative in
`docs/field-session-log.md`.

## Problem

On an aging Gen 1 Chevy Volt (2011–2015), fully depleting the EV battery causes
the pack voltage to sag under load, which trips "reduced propulsion" mode: the
gas engine revs to near max RPM to generate power directly, and the car
becomes sluggish. The owner currently avoids this by manually switching
drive/charge modes before the battery runs low. This project automates that
with a device on the car's CAN bus that continuously reconciles the drive mode
(see "Mode policy" below) toward a desired mode, switching before the pack gets
low enough to cause the problem.

## Design requirements

- **DR1 — Prevent reduced-propulsion mode.** Act on EV battery SOC/range
  before the pack depletes far enough to cause voltage sag and trigger
  reduced-propulsion mode.
- **DR2 — Drive toward a chosen mode each drive cycle.** On every drive cycle
  (ignition on) hold a selected mode (MOUNTAIN or, by default, the SOC-HOLD
  floor). Realized as the reconciler's setpoint — see "Mode policy".
- **DR3 — SOC floor.** When SOC drops to a configurable percentage, switch to
  Hold Mode and keep it there until the pack recovers. Realized as the
  always-on floor in "Mode policy".
- **DR4 — Off-the-shelf hardware preferred.** Favor a proven, reusable CAN
  interface over custom PCB/firmware design; hobbyist-level electronics
  work (soldering, wiring, light scripting) is acceptable where needed.
- **DR5 — Extensible to a future Trip Mode input.** The reconciler/injector
  architecture must accommodate a speed-based Hold input feeding the desired
  mode later without a redesign.

## Vehicle context

The Volt (both Gen 1 and Gen 2, model years 2011–2019) uses GM's **Global A**
CAN architecture. Unlike newer GM "Global B" vehicles, there is no
authenticating central gateway blocking the OBD-II port — the standard
driver-side OBD-II connector gives direct read/write access to the relevant
CAN buses. The Volt also has a second, auxiliary OBD-style connector
(passenger side) exposing the HV/battery-management bus, but the standard
port is sufficient for this project.

Gen 1 (this car) has a single physical mode button that **cycles** through
four modes, in order: **Normal → Sport → Mountain → Hold** (then presumably
back to Normal). This is the same kind of cycling control opendbc documents
for Gen 2 — plausibly even the same underlying signal (`0x1E1`, see below),
though that's only confirmed on a 2017 (Gen 2) car and needs verification on
this one.

Each mode's behavior:
- **Normal** — uses the full EV battery before switching to gasoline.
- **Sport** — same battery behavior as Normal, snappier throttle response.
- **Mountain** — lets the pack drain to ~50% SOC, then maintains ~50% by
  engaging the gas engine as needed.
- **Hold** — maintains the pack at *whatever SOC it's at* when engaged, by
  engaging the gas engine as needed.

Because it's a cycle button rather than discrete per-mode controls, reaching
a specific target mode means sending the same one button-press message a
computed number of times from the current mode — not one distinct message
per mode. See "Mode policy" and "Software architecture" below.

## Mode policy — continuous reconciler

> **Why a reconciler, not edge triggers.** Ignition is effectively always on
> (the Pi only has power while the car runs), so there is no ignition edge to
> trigger on, and an edge-triggered switch does nothing if the driver later
> bumps the mode by hand or a switch doesn't take. A single
> **level-triggered reconciler** that continuously drives the car toward a
> *desired* mode is simpler and self-healing, and satisfies both DR2 and DR3
> without separate configurable triggers.

**Desired mode = f(setpoint, SOC).** One loop pass computes the mode the car
*should* be in. The result is concrete (HOLD / MOUNTAIN) **or `None`** — `None`
means "no target, leave the car alone", which is what the passive `auto`
setpoint yields above the floor. If a concrete desired mode differs from
`state.drive_mode` (read from `0x1F4` byte 1) and the daemon is armed, it asks
the safety gate to walk the menu there — so a hand-picked mode change gets
walked back, at most once per 60 s cooldown. The reconcile is also skipped
entirely until `0x1F4` decodes a current mode (a fresh boot on a quiet or
just-woken bus does nothing until it can see where the menu cursor is).

| | SOC above `hold_reset_percent` | SOC at/below `hold_threshold_percent` |
| --- | --- | --- |
| **setpoint = `auto`** (default) | desired = **None** — leave the car alone | desired = **HOLD** (floor wins) |
| **setpoint = HOLD** (driver-selected) | desired = **HOLD** — enforce it | desired = **HOLD** |
| **setpoint = MOUNTAIN** (driver-selected) | desired = **MOUNTAIN** | desired = **HOLD** (floor wins) |

- **Passive by default.** The shipped config boots at `default_setpoint: auto`
  — the daemon is armed but has no target, so a mid-drive restart on a healthy
  pack leaves the car exactly where the driver had it. Enforcement starts only
  when the driver picks HOLD or MOUNTAIN (panel SW1 / `voltdmf-ctl setpoint`),
  or the SOC-HOLD floor engages.
- **Armed = enforce a *selected* setpoint.** Once HOLD or MOUNTAIN is
  selected, above the floor the daemon actively holds the car in that mode; it
  does not "leave the car alone". Flip the panel button (or `voltdmf-ctl
  disarm`) to stop it asserting MOUNTAIN. There is no way back to `auto` at
  runtime — only a restart returns to the passive default.
- **The SOC-HOLD floor is always active** and always wins. There is no
  "off" / suspend — the whole point of the device is that the pack never
  sags, so the worst case is always "hold at the current SOC".
- **Hysteresis.** At/below `hold_threshold_percent` (**33** — diag SOC from
  the `22 005B` poll) the floor engages (desired = HOLD); it stays latched
  until a fresh poll reads back at/above `hold_reset_percent` (**41**). The
  latch lives in memory only and is dropped on `reload`.
- **Failsafe on a stale poll.** If the poll stops answering (older than
  ~45 s), `0x096` byte 3 ≤ `bar_failsafe_raw` (**9**, ≈ 2 gauge bars) forces
  HOLD. The b3 proxy can only *engage* the floor — releasing it takes a real
  poll reading above the reset percent, so a poll outage fails safe into
  charge-sustaining HOLD.
- **Floor target = 2 gauge bars remaining.** From the Session-8 full-drain
  drive the last bar (`1→0`) fell in **53 s** against a ~170 s average for
  the other nine — the bottom of the gauge is a cliff, so the floor engages
  while 2 bars still show. Session 9 pinned the numbers: `SOC% ≈
  7.07·bars + 19.6` (r = 0.999); the 3→2 bar drop is **33.7 %** (raw 0x56),
  the 4→3 bar mark **41.2 %** (raw 0x69) — hence 33 / 41.
- **Setpoint** is `auto` at boot, then a two-state toggle, **HOLD ⇄
  MOUNTAIN**, changed by one panel button (see "Runtime control" / "Hardware
  design"). It is *not* persisted — the Pi runs a read-only root with an
  overlay filesystem, so there is nothing to write to, and **every boot
  starts at `auto`** (no target). A fresh key cycle is a fresh drive;
  re-selecting HOLD or MOUNTAIN each time is acceptable.
- **Reconcile cadence vs. the driver.** The reconcile runs every loop but
  every switch still goes through `SafetyGate` (preconditions + the 60 s
  cooldown), so if the driver fights the setpoint the daemon re-asserts at
  most once a minute. The button is the intended override — flip it to HOLD
  and the daemon stops asserting MOUNTAIN.

**Mechanics of reaching a target mode** (unchanged). The button cycles
Normal → Sport → Mountain → Hold, so "go to HOLD" means "press N times" where
N depends on the current mode. The current-mode status signal is confirmed
(`0x1F4` byte 1), so the controller reads it directly and closes the loop on
the live menu cursor (`0x1F4` bytes 4+5) rather than blind-counting. The
blind count `index(target) + 1` is in fact correct from any mode — the menu
always opens on NORMAL (measured 2026-09-03) — so the closed loop buys early
exit and a hard failure on a dropped tap, not correctness.

**Trip Mode (DR5, future work — not building this yet).** The
[prior-art project](https://github.com/vix597/chevy-volt-trip-mode)'s
speed-based Hold (bank EV range on the highway, release it in the city) would
become a third setpoint or a modifier on the policy, reusing the same
reconcile + injector plumbing. Tracked, not scheduled.

Config (`config.example.yaml`; parsed by `voltdmf/config.py`):

```yaml
policy:
  default_setpoint: auto        # auto | hold | mountain — boot value, not persisted
                                # (auto = no target until a driver selection or the floor)
  hold_threshold_percent: 33    # floor forces HOLD at/below this diag SOC
  hold_reset_percent: 41        # floor releases at/above this (hysteresis)
  bar_failsafe_raw: 9           # 0x096 b3 <= this forces HOLD if the poll is stale

soc_poll:
  enabled: true                 # run the 22 005B UDS poll for ground-truth SOC
  period_seconds: 10            # request cadence
```

`parse_config` validates `hold_reset_percent > hold_threshold_percent`,
`default_setpoint ∈ {auto, hold, mountain}` (`auto` parses to `None`),
`0 ≤ bar_failsafe_raw ≤ 255`, and `period_seconds > 0`. The button GPIO pins live in the `button_helper.py`
unit, not here.

## Prior art (reuse this, don't rebuild it)

[`vix597/chevy-volt-trip-mode`](https://github.com/vix597/chevy-volt-trip-mode)
(blog writeup: [seanlaplante.com](https://seanlaplante.com/2021/11/13/hacking-my-chevy-volt-to-auto-switch-driving-modes-for-efficiency/))
is a 2017 Volt (Gen 2) project that does almost exactly this kind of
automation — it auto-switches modes based on speed rather than SOC, using a
comma.ai panda + Raspberry Pi. It's built directly on
[`commaai/opendbc`](https://github.com/commaai/opendbc)'s
`gm_global_a_powertrain.dbc`, which decodes the Volt/Bolt-era GM Global A
bus. Reuse this DBC file and its CAN signal IDs rather than
reverse-engineering the whole bus from scratch; this project reads/writes
CAN over PiCAN2's SocketCAN interface rather than panda, but the signal
discovery and injection logic transfer directly.

Its known gap, worth fixing rather than copying: it runs the panda in
`SAFETY_ALLOUTPUT` mode with no message whitelist, so the device is
technically capable of transmitting anything on the bus. This project has no
hardware-level safety firmware equivalent (PiCAN has none built in, unlike
panda), which makes the software-level "Safety model" below — the single
narrow send function and rate limiting — load-bearing rather than a second
layer of defense.

## Known CAN signals

| Signal | ID | Notes |
|---|---|---|
| EV battery SOC (exact) | `22 005B` (UDS PID) | **The daemon's SOC source.** `0x206` (the Gen 2 broadcast candidate) is not on this bus, and no passive frame carries SOC at usable resolution, so the daemon polls diagnostic PID `22 005B` ("Hybrid/EV Battery Pack Remaining Charge") every ~10 s: request `03 22 00 5B 55 55 55 55` to `0x7E4` then `0x7E0` (lock onto whichever answers), reply on `0x7E8..0x7EF`, `SOC% = d[4]·100/255`. Stays in the default diagnostic session, service-22 only — no session switch, no TesterPresent — so it can't suppress normal broadcasts. The poll is a second hard-coded TX path (`canio.send_soc_poll`), ungated by arm state, gated only by `soc_poll.enabled`. |
| EV battery SOC (coarse proxy / failsafe) | `0x096`, byte 3 | Only valid in the `x F0 0A xx` mux (`data[1]==0xF0 and data[2]==0x0A`). Steps ~13 % SOC per count — far too coarse to key the floor off, used only as the failsafe when the `22 005B` poll goes stale: b3 ≤ `bar_failsafe_raw` (9 ≈ 2 gauge bars ≈ 30 %) forces HOLD. Session-9 provenance; `signals.decode_soc_bar_raw`. |
| Drive mode cycle button press | `0x1E1`, byte 4 bit 7 | **CONFIRMED on Gen 1 (2026-08-29, on-road).** `ASCMSteeringButton`; byte 4 low bits are a rolling counter, bit 7 is the press flag. Same ID/bit the Gen 2 prior art injects. `voltdmf/canio.send_mode_button_press()` (tracking-echo press) — one of the daemon's two hard-coded TX frames (the other is the `22 005B` SOC poll); this is the only one gated by arm state. |
| Ignition/drive-cycle start | — | **Not documented as a single CAN signal, and no longer needed as one.** Since the Pi is powered from the switched accessory socket (see "Hardware design"), the daemon only runs while the car is on. The reconciler is level-triggered, so it needs no ignition edge and no separate ignition-sense signal. Bus activity (Global A buses go quiet with the car off) remains a fallback cross-check if needed. |
| Vehicle speed | `0x3E9` | Bytes 0-1 big-endian ÷ 64 → km/h (× 0.621371 → mph). Per the GM Volt reverse-engineering wiki, cross-checked against a full-drain capture; not yet speedo-verified. DLC 8, 10 Hz; bytes 2 & 6 are a mux/rolling counter. Not needed for the SOC-triggered design but useful for bench testing/logging (`tools/soc_log.py` logs it plus a derived accel). |
| Shift/PRNDL position | `0x1F5`, byte 3 | **CONFIRMED on Gen 1 (2026-08-29).** `1` PARK, `2` REVERSE, `3` NEUTRAL, `4` DRIVE, `5` LOW. Used as a safety precondition — `SafetyGate` blocks injection unless in DRIVE (and blocks on UNKNOWN / short frame). `0x135` byte 0 also tracks the shifter but with a messier non-sequential encoding — left undecoded. |
| EV range remaining | — | **Not documented anywhere found.** The `22 005B` SOC poll (above) is the trigger signal instead — satisfies the design requirement to trigger on range or battery % (DR1/DR3) and is the metric that's actually accessible. |
| Reduced-propulsion / limp-mode indicator | — | **Not documented.** Not needed for this design since the goal is to act *before* this state, using the SOC threshold instead. |
| Current drive mode (status, not button-press) | `0x1F4`, byte 1 | **CONFIRMED on Gen 1 (2026-08-29).** Latched mode: `0x00` NORMAL, `0x80` SPORT, `0x20` MOUNTAIN, `0x08` HOLD. byte 4 = live drive-mode menu cursor (steps ~40 ms after each tap; distinct byte codes), byte 5 bit 7 = menu-open hint. The daemon reads byte 1 as its current-mode source — the press-counting fallback is retired. |

## Hardware design

### Alternatives considered

DR1–DR3 require a device that can both read and write raw CAN traffic on
the vehicle's buses — not just standard OBD-II diagnostic PIDs. That rules
out generic ELM327-style OBD2 USB/Bluetooth adapters (e.g. the ones used
with Torque): they speak the OBD2 diagnostic request/response protocol
only, can't sniff non-standard manufacturer IDs like `0x206` (SOC) or the
mode-cycle button message, and generally can't transmit arbitrary frames at
all. Among devices that do speak raw CAN:

| Device | Read+Write | Approx. price | Why not chosen |
|---|---|---|---|
| **CANable / CANtact** | Yes | $30–60 | SocketCAN-compatible, works with generic `python-can`, but no GM-specific precedent or safety/ignition features — this project would own all the reverse-engineering and safety-model work with no reference to check against. |
| **PCAN-USB (PEAK-System)** | Yes | $200+ | Industrial-grade and very reliable, but priced and positioned for professional automotive tooling rather than the hobbyist car-hacking community — little overlap with existing GM Global A tribal knowledge. |
| **Kvaser Leaf Light** | Yes | $200–400+ | Similar tier to PCAN-USB — professional-grade and overkill for this project's scope. |
| **Macchina M2** | Yes | — | Purpose-built for vehicle hacking, similar spirit to panda, but reportedly hard to source or discontinued. |
| **ESP32 + SN65HVD230 transceiver** (DIY) | Yes | $20–25 | Cheapest option, but the whole CAN stack, message filtering, and safety logic would need to be written from scratch — the most custom-firmware work of any option, cutting against the off-the-shelf/light-scripting preference (DR4). |
| **Freematics ONE+** | Yes | — | Telematics-focused (built-in GPS/logging); CAN-capable but little presence in the GM/opendbc hobbyist community. |
| **comma.ai panda (red)** | Yes | $99 | Only option with a documented Volt-specific precedent ([`vix597/chevy-volt-trip-mode`](https://github.com/vix597/chevy-volt-trip-mode)) and comes with openpilot's safety-firmware rate-limiting built in. Ruled out on sourcing grounds: the red panda's vehicle-side port is a proprietary "OBD-C" connector, not standard OBD-II — reaching a vehicle's OBD-II port needs a $49 third-party adapter ([konik.ai OBD2C connector](https://konik.ai/shop/obd2c-connector/)) that ships only from an EU warehouse, with no US stock or reseller found. Older pandas with a native OBD-II connector (white/grey/black) are discontinued and only findable used. |
| **PiCAN2 / PiCAN3** (Raspberry Pi HAT) — **chosen** | Yes | $37–78 + a Pi | See below. |

**Why PiCAN2:** PiCAN2 and PiCAN3 use identical CAN silicon (MCP2515
controller / MCP2551 transceiver) — read/write capability is the same on
both. PiCAN3 adds Pi 4 support, an onboard automotive-range SMPS, reverse-polarity
protection, and an RTC, none of which this project needs: **PiCAN2** paired
with a Pi 3B is the simpler, cheaper choice. Both expose a DB9 connector
(jumper-selectable for an "OBD-II cable" pinout), and standard off-the-shelf
OBD-II-to-DB9 cables exist that carry exactly the three wires needed
(CAN-H, CAN-L, ground) — e.g. the
[iKKEGOL DB9-to-OBD2 cable](https://www.amazon.com/iKKEGOL-Adapter-Diagnostic-Extension-Connector/dp/B077SHQQ1D)
(~$10–15, ships from a US Amazon warehouse). This sidesteps the sourcing
problem that ruled out panda entirely — no proprietary connector, no
overseas shipping, no DIY fabrication required. It satisfies DR4 better
than panda on balance: slightly more wiring/config work (SocketCAN device-tree
overlay setup) in exchange for a fully off-the-shelf, US-available parts
list. Because PiCAN exposes standard Linux **SocketCAN** (`can0`), the same
interface `python-can`/`cansend` use everywhere, the reference project's
`opendbc`-based injection logic ports over by swapping the transport
backend (socketcan instead of panda), not rewriting the injection logic.
The read+write pattern itself is proven on this hardware generically (e.g.
[skpang/PiCAN-Python-examples](https://github.com/skpang/PiCAN-Python-examples)
transmits live CAN frames to drive a real instrument cluster gauge) —
there's no GM/Volt-specific PiCAN precedent, but none was needed once the
signal IDs are confirmed from the panda-based reference project's work.

Note: PiCAN2 draws its 5V entirely from the Pi's own 40-pin GPIO
header — it has no separate power input of its own. This matters for the
power design below.

**BOM — ordered 2026-08-23:**

| Part | Role | Price paid | Source |
|---|---|---|---|
| PiCAN2 (SK Pang, base version) | CAN read/write interface, DB9 connector | $59.95 | [Copperhill Technologies](https://copperhilltech.com/pican-2-can-bus-interface-for-raspberry-pi/) |
| Raspberry Pi 3B | Runs the monitor/injector daemon; PiCAN2 mounts directly on it | $35.00 | [Adafruit #3055](https://www.adafruit.com/product/3055) |
| OBD-II-to-DB9 cable (iKKEGOL) | Connects PiCAN2's DB9 port to the vehicle's OBD-II port | ~$10–15 | [Amazon](https://www.amazon.com/iKKEGOL-Adapter-Diagnostic-Extension-Connector/dp/B077SHQQ1D) — also Copperhill's own recommended replacement for their now-discontinued OBD2-DB9 cable, confirming the pinout matches PiCAN2's OBD-II jumper setting (CAN-H → OBD pin 6 → DB9 pin 3; CAN-L → OBD pin 14 → DB9 pin 5; ground → OBD pins 4/5 → DB9 pins 1/2) |
| 12V cigarette-lighter/accessory-socket USB car charger (5V) | Power for the Pi | ~$8–10 | any standard USB car charger |
| microSD card | OS storage | ~$8 | any Class 10/A1-rated card |

This is a smaller footprint than the reference project (which added a
touchscreen for a manual UI) since this design's whole point is to run
unattended — no display needed. Mount the Pi + PiCAN2 somewhere accessible
(e.g. under the dash near the OBD port) so it's easy to unplug entirely if
something needs to be debugged or disabled — that's the physical kill
switch, and it's free.

**Power.** This Volt's accessory (cigarette-lighter) socket is switched
with the car rather than always-hot, so a standard USB car charger plugged
into it is sufficient — no relay, no separate switched-12V tap, and no
ignition-sense circuitry needed to avoid draining the 12V battery while
parked. That still means power gets cut abruptly (no clean OS shutdown) on
every drive cycle, which is addressed at the OS level instead of with extra
hardware: configure Raspberry Pi OS's built-in **Overlay File System**
(`raspi-config` → Performance Options) so the root partition is mounted
read-only with a RAM (tmpfs) overlay for writes — nothing is physically
written to the SD card during normal operation, so an abrupt power cut
can't corrupt it. This requires also write-protecting `/boot/firmware`
(asked as a separate prompt in the same raspi-config flow) and disabling
swap, both easy to miss. The tradeoff: anything not explicitly persisted
outside the overlay (logs, runtime config changes) is lost on every power
cycle — acceptable here since drive-mode state is re-derived from the CAN
bus on each boot, but something to keep in mind if the daemon later needs
to persist SOC-threshold calibration or logs across drives.

## Software architecture

Reuse rather than rebuild:
- **`opendbc`**'s `gm_global_a_powertrain.dbc` for decoding/encoding CAN
  frames (SOC, speed, shift position, and — once discovered — the Gen 1 mode
  button signal).
- **`python-can`** (with its SocketCAN backend) for talking to PiCAN2's
  `can0` interface from the Pi.
- **SavvyCAN** (has native SocketCAN support on Linux) or **can-utils**'
  `candump`/`cansend` for the manual reverse-engineering phase (see below)
  — no need to write custom logging tools. (`cabana`, used in the panda-based
  reference project, is panda-specific and doesn't apply here.)

The custom code needed is small and specific to this project:

1. **Config loader** — reads the YAML above, validates it (e.g.
   `hold_reset_percent > hold_threshold_percent`), and produces the policy
   for the run.
2. **SOC poller** — an inline loop step (`daemon._service_soc_poll`) that
   sends `22 005B` every `soc_poll.period_seconds` (`canio.send_soc_poll`,
   ungated by arm state); the RX listener folds replies on `0x7E8..0x7EF`
   into `state.soc_percent` / `soc_raw` / `soc_percent_monotonic`. `0x096`
   byte 3 is decoded passively into `state.soc_bar_raw` as the failsafe
   proxy. `0x206` is absent on this bus.
3. **Reconciler** (`voltdmf/reconciler.py`, replaces `triggers.py`) — a pure
   object, `Reconciler.desired_mode(state) -> DriveMode` (always concrete),
   per the "Mode policy" table, plus the in-memory floor-latch hysteresis and
   the b3 failsafe. The loop calls it every pass and, when `desired` differs
   from `state.drive_mode` and the daemon is armed, hands `desired` to the
   safety gate. No per-drive edge state, no once-only latch.
4. **Button helper** — a tiny separate process (system Python, `gpiozero`)
   that owns the two PiCAN2 pads (SW1 = BCM 24, SW2 = BCM 23) and dispatches
   by gesture:
   - **SW1 tap** → `voltdmf-ctl setpoint …` to toggle HOLD ⇄ MOUNTAIN.
   - **SW1 held alone ≥ 8 s, then released** → `voltdmf-ctl walk-test`: the
     daemon cycles the closed-loop mode walk through every drive mode, scores
     each landing off `0x1F4`, and walks back to the starting mode. The 5–8 s
     SW1-solo window is a dead zone so a slow reach for the combo can't trip
     it; result shows on the LCD watch screen.
   - **SW2 held ~5 s alone, then released** → launch the charge-current
     setpoint capture (`voltdmf-chargelog.service`; checklist §2e). Fires on
     release with SW1 never joined, so a hand travelling toward the combo
     below can't trip it.
   - **SW1 + SW2 held ~5 s** → launch the SOC-discovery capture
     (`voltdmf-soclog.service`).

   Both launches free the GPIO lines, claim the LCD hand-off lock, `systemctl
   start` the oneshot and block until it exits (polling `is-active`, never
   trusting the queued `start`), then re-acquire — and wait for both pads to
   be released before reading gestures again so the capture's hold-BOTH stop
   can't roll into a fresh launch. The gesture rules live in a side-effect-free
   `_Gestures` state machine (unit-tested off-Pi). Kept out of the daemon so
   its venv gains no GPIO dependency and its privilege set is unchanged; it
   reaches the daemon only through the existing control socket. Its own
   systemd unit in `roles/voltdmf`.
5. **Mode-cycle controller** — reads current mode from the status signal
   (`0x1F4` byte 1, confirmed), walks the drive-mode menu to the requested
   `target_mode` along the fixed Normal→Sport→Mountain→Hold cycle order —
   closing the loop on the live menu cursor (`0x1F4` bytes 4+5) — and sends the
   taps through the safety wrapper below. This is the one place that needs the
   actual button-press CAN frame; the reconciler and `set-mode` both just ask
   it for a `target_mode` and it doesn't care which asked.
6. **Safety wrapper** (see next section) — the only code path allowed to
   transmit.

The button-press signal (`0x1E1`) and the current-mode readback (`0x1F4`)
are both confirmed on-vehicle, so this controller now exists
(`voltdmf/modecycle.py`). The original caution still holds as a principle: a
controller built against a guessed CAN ID, or one that can't tell what mode
it's starting from, is worse than not having one.

## Safety model

CAN has no authentication, and the reference project's `SAFETY_ALLOUTPUT`
approach means the panda can transmit *anything*, not just the one message
this project needs. Tighten that:

- **Two narrow send functions, no generic sender.** `canio` has exactly two
  functions that put a frame on the bus: `send_mode_button_press()`
  (hard-coded `0x1E1` press — every mode transition reuses this one message,
  gated by arm state) and `send_soc_poll()` (hard-coded `22 005B` request,
  ungated — a read-only diagnostic poll). Each hard-codes its ID + payload;
  there is no general-purpose "send arbitrary frame" path.
- **Rate limiting.** Cap both the size and rate of a press burst — at most
  3 presses (Normal→Hold, the longest possible cycle) with realistic
  inter-press spacing, never a sustained/looping transmission. Re-check the
  current mode (if readable) after sending to confirm it landed on the
  intended target rather than blindly trusting the press count.
- **Preconditions before injecting**: vehicle in Drive, speed within a
  sane range, ignition on — reduces the chance of triggering the switch in
  a state where it wouldn't make sense anyway.
- **Fail passive.** If the daemon crashes or hangs, it should simply stop
  transmitting — never get stuck mid-loop retrying a send. Run it under a
  process supervisor (e.g. `systemd` with restart-on-failure). The daemon
  keeps **no persisted state** (read-only overlay root): a fresh start boots
  armed at `default_setpoint` (`auto` on the shipped config → no target) with
  the floor latch clear and re-derives everything from the live bus, so there
  is no stale latch to re-fire and — absent a low pack — nothing to walk.
- **Physical kill switch**: unplugging the OBD connector fully removes the
  device from the bus — keep it physically easy to reach for exactly this
  reason during early testing.
- **Bench test before live testing.** Validate the discovered mode-switch
  message with the car safely stationary (ignition on, engine off, or on
  jack stands) before relying on it while driving.
- **Runtime control does not widen the transmit surface.** The daemon runs
  permanently as root under systemd; operators drive it from an unprivileged
  account through a control socket (see "Runtime control" below). Every
  command that could transmit still funnels through the *same*
  `SafetyGate.request*()` → single `send_mode_button_press()` path the
  reconciler uses. A manual `set-mode` is an out-of-band nudge that does not
  move the setpoint — it is still subject to preconditions, the press cap, the
  cooldown, and the menu-cursor readback, and the reconciler will pull the mode
  back on its next pass. There is still no "send arbitrary frame" path.

### Runtime control

The daemon is a long-running root service, not a set of one-shot CLI
invocations. State changes come in over an `AF_UNIX` stream socket
(`voltdmf/control.py`), reached with the unprivileged `voltdmf-ctl` client:

| command | effect | path |
| --- | --- | --- |
| `status` | daemon + vehicle snapshot | answered read-only on the socket thread |
| `arm` / `disarm` | flip the runtime transmit-enable | queued → loop thread |
| `setpoint <hold\|mountain>` | select the reconciler setpoint — first selection also leaves `auto` (the panel button is a `setpoint` caller) | queued → loop thread |
| `set-mode <mode>` | request one mode switch now, out of band | queued → `SafetyGate.request_verbose()` |
| `reload` | re-read the config file, rebuild the policy | queued → loop thread |
| `walk-test` | self-test the closed-loop mode walk: cycle every drive mode, score each landing, restore the start mode | queued → sets a flag; the loop thread runs the ~1 min cycle inline (reconciler skipped) and reports on the LCD + journal |
| `test-mode <on\|off>` | suspend / resume the reconciler for an interactive probe session (in memory — a restart brings protection back) | queued → loop thread sets a flag; the reconcile pass is skipped while on |
| `probe <mode>` | one operator-chosen closed-loop walk to `<mode>`, densely tracing the `0x1F4` cursor; records a `LANDED` / `CURSOR_ONLY` / `MISS` / `BLOCKED` verdict in `status` + the journal | queued → sets a flag; the loop thread runs it inline with a ~50 ms cursor sampler thread |

> **All commands are implemented.** `setpoint` and the reconciler it
> feeds landed in Session 9; `tools/button_helper.py`'s SW1 tap now drives a
> real toggle. `walk-test` (SW1 solo hold ≥ 8 s) drives its own cooldown-free,
> Park-tolerant `SafetyGate` around the shared controller, so the real 60 s
> gate and the single-threaded TX path are untouched; it needs the daemon
> armed and refuses if a run is already queued. `test-mode` + `probe`
> (Session 10) are the focused replacement for blind walk-test debugging: an
> operator suspends the reconciler, sets a known start mode by hand, asks for
> one target, and reads the per-tap + dense-sample cursor trace back — see
> `docs/analysis/session10-walk-probe.md`.

- **Privilege boundary = file mode.** The `.socket` unit binds it
  `0660 root:voltdmf`; operators join the `voltdmf` group. No setuid, no
  polkit, no D-Bus.
- **systemd socket activation.** `voltdmf.socket` passes an already-bound
  listening fd as fd 3; `control.inherited_listener()` picks it up with no
  `python-systemd` dependency. The service still runs continuously
  (`WantedBy=multi-user.target`) — activation only supplies the fd, it does
  not make the daemon on-demand.
- **Single-threaded transmit preserved.** `status` is answered directly from
  a state snapshot. Everything that can transmit or mutate state is put on a
  `queue.Queue` and executed by the daemon's one loop thread — the CAN TX
  path and `SafetyGate` are never touched from the socket thread.
- **One gate: armed / disarmed.** There is no `--dry-run`. The service boots
  **armed** — injection efficacy is on-road-confirmed, so there is no reason
  to hold it back a key cycle at a time — but with `default_setpoint: auto` it
  has no target and enforces nothing until the driver selects HOLD/MOUNTAIN or
  the SOC floor engages (see "Mode policy"). `voltdmf-ctl disarm` is the
  mid-drive stop; `voltdmf-ctl arm` undoes it. While disarmed the reconciler
  still runs and logs what it *would* do, and the `22 005B` SOC poll still
  transmits (it is a read-only diagnostic request, not a mode change).
  `--start-disarmed` boots into the stopped state for bench work.
- **`setpoint` is the normal control.** It selects the reconciler's HOLD ⇄
  MOUNTAIN toggle (the first selection also leaves the passive `auto` state);
  the reconciler then drives the car toward the desired mode and holds it
  there, self-healing if the driver bumps the stalk. The setpoint lives in
  memory only — every boot starts at `auto` (no persisted state; see "Mode
  policy").
- **`set-mode` is a soft, out-of-band nudge.** It asks the mode-cycle
  controller for one switch now without moving the setpoint, so the reconciler
  will pull the mode back on its next pass (after the cooldown). Use it for
  bench pokes, not steady-state control.
- **`reload` re-reads the config file.** It rebuilds the `policy:` /
  `soc_poll:` blocks and the reconciler. The live setpoint is preserved (it
  is the driver's choice, not a file value); the floor-latch hysteresis state
  is dropped, so the floor re-evaluates from the current SOC.

## Phased plan

1. **Signal discovery (on-vehicle, stationary).** Connect PiCAN2 + laptop
   (skip the Pi for now, or use the Pi itself with a monitor/SSH), log CAN
   traffic with SavvyCAN or `candump`.
   Confirm `0x206` tracks SOC sensibly on this car. Then, with the car
   safely stationary:
   - Press the mode button once and diff CAN traffic before/after. Check
     first whether it matches `0x1E1` bit 39 from `gm_global_a_powertrain.dbc`
     — if so, this signal transfers directly from the Gen 2 reference
     project and saves the rest of this reverse-engineering step.
   - Cycle through all four modes (Normal→Sport→Mountain→Hold) while
     logging, watching for any *other* message that changes value in sync
     with the mode change — that's the current-mode status signal the
     controller needs. A good place to check: whatever message drives the
     dash cluster's mode indicator icon, since that has to be broadcast on
     the bus regardless of what triggered the change.
   - Ignition-cycle mode persistence is no longer on the critical path: the
     reconciler is level-triggered and reads the live mode from `0x1F4` every
     pass, so it converges regardless of what mode the car powers up in.
2. **Bench-safe injection test.** Using the discovered button-press message,
   replay presses from the laptop (still stationary) and confirm the mode
   actually advances one step per press, in the expected order, and that
   nothing else on the bus reacts badly (watch for new DTCs, warning
   lights).
3. **Daemon development.** Write the config loader, the SOC poller, the
   reconciler, the button helper, and safety-wrapped injector(s) described
   above, using the confirmed CAN IDs. Test on the Pi with the overlay
   filesystem enabled (see "Hardware design") before final deployment — the
   read-only root is why the reconciler keeps no persisted state.
4. **Deployment.** Mount the Pi + PiCAN2 in the car, power from the
   accessory socket, and test across several real drive cycles, tuning the
   SOC threshold based on how early the switch needs to happen to reliably
   avoid reduced-propulsion mode.

## Open items

### Resolved on-vehicle (Gen 1, HS-CAN 500k)

Full detail in `docs/signals-confirmed.md`; decoders in `voltdmf/signals.py`.

- **Mode-cycle button press → `0x1E1` byte 4 bit 7** — same ID/bit as the Gen
  2 prior art; the one frame the daemon transmits. Drove a closed-loop walk
  to all four modes on the road.
- **Current drive mode → `0x1F4` byte 1** (`0x00`/`0x80`/`0x20`/`0x08` =
  N/S/M/H) — the daemon's current-mode source. Bytes 4+5 are one field, the
  live menu cursor used to close the loop on a walk (`00`/`80` = NORMAL,
  `80`/`00` SPORT, `40` MOUNTAIN, `20` HOLD, both `00` = menu closed).
- **Shift/PRNDL → `0x1F5` byte 3** (`1`–`5` = P/R/N/D/L). `SafetyGate` blocks
  injection unless in DRIVE, and blocks on UNKNOWN.
- **Ignition/drive-cycle start** — no dedicated signal needed. The Pi is
  powered only while the car is usable and the reconciler is level-triggered,
  so there is no ignition edge to catch.
- **SOC → `22 005B` UDS poll** (Session 9). Exact pack percent = `raw·100/255`
  from a ~10 s service-22 poll; the gauge↔SOC curve is `SOC% ≈
  7.07·bars + 19.6` (r = 0.999). The floor engages at 33 % (3→2 bar drop),
  releases at 41 % (4→3 bar). This unblocked the armed reconciler.

### Still open

- **Drop the poll for a passive signal.** The `22 005B` poll works but it is
  an active TX on the vehicle bus every 10 s; a passive broadcast field would
  be cleaner. `0x096` byte 3 (the failsafe proxy) is far too coarse
  (~13 % SOC/count). The three Session-8 broadcast candidates — `0x3E3` bytes
  0/1/6, `0x228` byte 2, `0x186` byte 6 — still have no raw→% scaling anchor;
  the running trip logs (SOC % vs. each candidate raw, stamped every line by
  `soc_log.py`) are the data set to calibrate one against and eventually
  retire the poll.
- **Validate the 33 / b3≤9 timing on the road.** Watch the trip logs over
  several drives — does the floor engage early enough that the pack never
  reaches the 1→0 bar cliff, and does the b3 failsafe hold if a poll gap
  lands at the wrong moment.

### Deferred (not blocking)

- **Drive-mode persistence across ignition cycles.** Off the critical path
  under the reconciler (it reads the live mode from `0x1F4` and converges from
  any starting mode). Still mildly interesting for tuning the first-pass delay,
  along with how long after key-off the HS-CAN goes quiet — `tools/ignition_check.py`,
  whenever it's convenient.

### Stretch goal — force 12 A Level 1 charging

Not part of the drive-mode mission; a low-risk add-on that would reuse the
same bus tap and the same arm-gated injector safety wrapper.

The center-stack setting that raises 120 V charging from **8 A to 12 A** can
be changed at any time (even while driving), but **reverts to 8 A every time
the car leaves Park** — GM's guard against an unknown / marginal wall circuit.
The owner only ever charges on a known-good home circuit, so the revert is
pure friction; the goal is to make the car *always* Level-1-charge at 12 A.

Approach: detect the Park-exit revert (or just assert continuously) and
re-send the "12 A" setpoint frame so the faster rate always sticks.

Unknowns, all answerable from **one capture**: the setpoint frame ID / byte /
encoding (likely `0x08`↔`0x0C` amps, or `0x28`↔`0x3C` in the 0.2 A units OVMS
documents for charger telemetry on `0x5EC`), whether it carries a rolling
counter / checksum, and whether the HMI re-asserts fast enough to need
continuous TX vs a single post-revert shot. Discovery is a **standalone
garage session** — car in READY and Park, toggle the menu setting while
capturing, no drive and no discharged pack needed — see
`docs/phase-c-field-checklist.md` §2e. A P→R→P shift in the garage exercises
the forced 8 A revert; it lands on the same timestamp as `0x1F5` byte 3
leaving `0x01` (Park), a free labeled edge for the offline analysis.

Safety: 12 A at 120 V is 1.44 kW — within the car's own menu option and the
Lear charger's ~12.7 A ceiling. The only real-world caveat (the wall circuit
being rated for 12 A continuous) is one the owner controls by only ever
plugging in at home. No safety-of-motion dimension. It is still a bus TX, so
it goes through the same arm gate, error-frame watch, and DTC scan as
mode injection.

## Sources

- [vix597/chevy-volt-trip-mode](https://github.com/vix597/chevy-volt-trip-mode)
- [seanlaplante.com writeup](https://seanlaplante.com/2021/11/13/hacking-my-chevy-volt-to-auto-switch-driving-modes-for-efficiency/)
- [commaai/opendbc](https://github.com/commaai/opendbc)
- [PiCAN2 (SK Pang Electronics)](https://www.skpang.co.uk/products/pican2-can-bus-board-for-raspberry-pi-2-3)
- [skpang/PiCAN-Python-examples](https://github.com/skpang/PiCAN-Python-examples)
- [Raspberry Pi Overlay File System (read-only root)](https://learn.adafruit.com/read-only-raspberry-pi/overview)
- [OVMS voltampera_canbusnotes.txt](https://github.com/openvehicles/Open-Vehicle-Monitoring-System/blob/master/vehicle/Car%20Module/VoltAmpera/voltampera_canbusnotes.txt)
- [GM Volt Reverse Engineering Wiki](https://vehicle-reverse-engineering.fandom.com/wiki/GM_Volt)
- [GM-Volt.com OBD2 FAQ](https://www.gm-volt.com/threads/obd2-obdii-obd-ii-device-faq.110641/)
- [Understanding the openpilot Safety Model](https://blog.comma.ai/understanding-the-openpilot-safety-model/)
