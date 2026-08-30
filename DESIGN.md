# Volt Drive Mode Fixer — Design

**Status (2026-08-23):** hardware ordered (see BOM in "Hardware design"),
awaiting arrival. Next step on arrival: Phase 1 signal discovery (see
"Phased plan") — nothing to do until then.

## Problem

On an aging Gen 1 Chevy Volt (2011–2015), fully depleting the EV battery causes
the pack voltage to sag under load, which trips "reduced propulsion" mode: the
gas engine revs to near max RPM to generate power directly, and the car
becomes sluggish. The owner currently avoids this by manually switching
drive/charge modes before the battery runs low. This project automates that
with a device on the car's CAN bus that applies configurable, independent
trigger strategies (below) to switch modes before the pack gets low enough to
cause the problem.

## Design requirements

- **DR1 — Prevent reduced-propulsion mode.** Act on EV battery SOC/range
  before the pack depletes far enough to cause voltage sag and trigger
  reduced-propulsion mode.
- **DR2 — Configurable on-start trigger.** On every drive cycle (ignition
  on), optionally switch to Mountain Mode.
- **DR3 — Configurable SOC-threshold trigger.** When SOC drops to a
  configurable percentage, optionally switch to Hold Mode.
- **DR4 — Off-the-shelf hardware preferred.** Favor a proven, reusable CAN
  interface over custom PCB/firmware design; hobbyist-level electronics
  work (soldering, wiring, light scripting) is acceptable where needed.
- **DR5 — Extensible to a future Trip Mode trigger.** The trigger/injector
  architecture must accommodate adding a speed-based Hold trigger later
  without a redesign.

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
per mode. See "Trigger strategies" and "Software architecture" below.

## Trigger strategies (configurable)

Two independent trigger strategies, each individually enabled/disabled and
configured, plus a planned future third. They share the same underlying
injector — a trigger just decides *when* to fire and *which* mode to request.

1. **On-start → Mountain Mode (DR2).** Every drive cycle (ignition on), the
   device switches to Mountain Mode. Mountain Mode keeps a buffer in the
   pack rather than depleting it fully, so this is a simple, always-on
   hedge that satisfies the baseline on-start requirement.
2. **SOC threshold → Hold Mode (DR3).** When SOC drops to a configurable
   percentage, the device switches to Hold Mode. Hold maintains the pack at
   whatever SOC it's at when engaged (using the gas engine to avoid dropping
   further) — so triggering it right as SOC nears the danger zone should
   prevent the pack from ever reaching the voltage-sag point that causes
   reduced-propulsion mode. (Mountain has a similar backstop effect once the
   pack hits ~50%, but Hold is the one that can be engaged at *any* SOC, which
   is why it's the threshold target rather than Mountain.)
3. **Trip Mode (DR5, future work — not building this yet).** The
   [prior-art project](https://github.com/vix597/chevy-volt-trip-mode)'s
   actual approach: switch to Hold above a speed threshold (e.g. highway
   driving) to bank EV range for later city driving, then release it
   below the threshold. Worth adding once triggers 1 and 2 are working,
   reusing the same injector/config plumbing. Tracked, not scheduled.

**Mechanics of reaching a target mode.** Since the button cycles
Normal → Sport → Mountain → Hold, "switch to Hold" actually means "press the
button N times," where N depends on the *current* mode (e.g. 3 presses from
Normal, 1 press from Mountain). This makes reading back the current mode a
requirement, not a nice-to-have — without it, the device can't know how many
presses to send. See "Open items" for whether a current-mode status signal
exists; if not, a fallback is discussed there too.

Strategies 1 and 2 are meant to run **together** as the recommended default:
Mountain Mode conserves the pack proactively from the start of every drive,
and the SOC-threshold trigger is the backstop that engages Hold if the pack
still gets low anyway.

Config sketch (exact schema TBD when the daemon is written):

```yaml
on_start:
  enabled: true
  target_mode: mountain

soc_threshold:
  enabled: true
  target_mode: hold
  threshold_percent: 25       # calibrated against dash %, see "Open items"
  reset_percent: 40           # SOC must rise back above this to re-arm

trip_mode:
  enabled: false              # future work
```

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
| EV battery SOC | `0x206` | Bytes 1–2, ~0.25 kWh/count granularity per OVMS notes. **Needs calibration** against the vehicle's dash %/kWh readings — treat the raw value as relative until the scaling for this pack is confirmed. |
| Drive mode cycle button press | `0x1E1`, bit 39 (candidate) | `DriveModeButton` signal in `gm_global_a_powertrain.dbc`, documented on a 2017 (Gen 2) car. Gen 1 (this vehicle) uses the same style of cycling button (Normal→Sport→Mountain→Hold), so this is the first thing to test on the actual vehicle — **not yet confirmed on Gen 1**, but a strong candidate rather than an unknown. |
| Ignition/drive-cycle start | — | **Not documented as a single CAN signal, and no longer needed as one.** Since the Pi is powered from the switched accessory socket (see "Hardware design"), the daemon only runs while the car is on — its own process start *is* the on-start trigger, no separate ignition-sense signal required. Bus activity (Global A buses go quiet with the car off) remains a fallback cross-check if needed. |
| Vehicle speed | `0x3E9` | 16-bit big-endian, ÷100 for mph. Not needed for the SOC-triggered design but useful for bench testing/logging. |
| Shift/PRNDL position | `0x135` / `0x1F5` | Useful as a safety precondition (e.g., don't inject while not in Drive). |
| EV range remaining | — | **Not documented anywhere found.** Use SOC (`0x206`) as the trigger signal instead — satisfies the design requirement to trigger on range or battery % (DR1/DR3) and is the metric that's actually accessible. |
| Reduced-propulsion / limp-mode indicator | — | **Not documented.** Not needed for this design since the goal is to act *before* this state, using the SOC threshold instead. |
| Current drive mode (status, not button-press) | — | **Not documented, and now required rather than optional** — because mode selection is a 4-way cycle, the daemon needs to know the current mode to compute how many button presses reach the target (see "Trigger strategies"). Also lets the device avoid overriding a mode the driver deliberately chose. See "Open items" for a fallback if no such signal exists. |

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

1. **Config loader** — reads the YAML sketched above, validates it (e.g.
   `reset_percent > threshold_percent`), and produces the set of active
   triggers for the run.
2. **SOC monitor** — reads `0x206`, converts to a usable SOC value, applies
   a debounce/smoothing filter (raw CAN values can be noisy frame-to-frame).
3. **Trigger evaluators** — one per strategy, sharing the SOC monitor's
   output and a shared drive-cycle/ignition signal:
   - *on-start*: fires once per drive cycle as soon as ignition-on is
     detected.
   - *soc-threshold*: fires once when SOC crosses below
     `threshold_percent` (latch), and re-arms once SOC rises back above
     `reset_percent` (e.g. after charging) — this avoids repeatedly firing
     if SOC hovers near the threshold.
4. **Mode-cycle controller** — reads current mode (from the status signal,
   if one is found — see "Open items"), computes the number of button
   presses needed to reach a trigger's requested `target_mode` given the
   fixed Normal→Sport→Mountain→Hold cycle order, and sends that many
   presses (with realistic inter-press timing) through the safety wrapper
   below. This is the one place that needs the actual button-press CAN
   frame; every trigger just asks it for a `target_mode` and it doesn't
   care which trigger asked.
5. **Safety wrapper** (see next section) — the only code path allowed to
   transmit.

Don't write this until step 1 in "Phased plan" below has confirmed the
actual button-press signal and a way to read current mode (or the fallback
in "Open items" if no status signal exists) — a controller built against a
guessed CAN ID, or one that can't tell what mode it's starting from, is
worse than not having one.

## Safety model

CAN has no authentication, and the reference project's `SAFETY_ALLOUTPUT`
approach means the panda can transmit *anything*, not just the one message
this project needs. Tighten that:

- **Single narrow send function.** All transmission goes through one
  function that hard-codes the exact CAN ID + payload for the one
  button-press message this project needs, and nothing else — the cycling
  design means every mode transition reuses this same message. No
  general-purpose "send arbitrary frame" path in the daemon.
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
  process supervisor (e.g. `systemd` with restart-on-failure) but make sure
  a fresh process start doesn't immediately re-fire a stale latch.
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
  triggers use. A manual `set-mode` overrides only the trigger *arming*
  decision — it is still subject to preconditions, the press cap, the
  cooldown, and the menu-cursor readback. There is still no "send arbitrary
  frame" path.

### Runtime control

The daemon is a long-running root service, not a set of one-shot CLI
invocations. State changes come in over an `AF_UNIX` stream socket
(`voltdmf/control.py`), reached with the unprivileged `voltdmf-ctl` client:

| command | effect | path |
| --- | --- | --- |
| `status` | daemon + vehicle snapshot | answered read-only on the socket thread |
| `arm` / `disarm` | flip the runtime transmit-enable | queued → loop thread |
| `set-mode <mode>` | request one mode switch now | queued → `SafetyGate.request_verbose()` |
| `reload` | re-read the config file, rebuild triggers | queued → loop thread |

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
- **`--dry-run` is an immutable session lock.** A non-dry-run daemon still
  boots *disarmed*; an operator must `voltdmf-ctl arm` it (or start it with
  `--armed`). Under `--dry-run`, `arm` is refused outright and the CAN layer
  no-ops every press regardless.
- **Manual override is soft.** A successful `set-mode` records the target for
  display; the next real trigger edge clears it and reclaims control. Between
  edges nothing re-asserts, so the manually chosen mode simply persists.
- **`reload` resets trigger latches.** Rebuilding triggers means a
  `once-per-drive` trigger (on-start) can fire again — reload is an explicit
  operator action, treated as a re-arm.

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
   - Separately, check whether the car resets to Normal on every ignition
     cycle or remembers the last mode across power-off — this determines
     whether the on-start trigger can assume a fixed starting mode or also
     needs the status signal.
2. **Bench-safe injection test.** Using the discovered button-press message,
   replay presses from the laptop (still stationary) and confirm the mode
   actually advances one step per press, in the expected order, and that
   nothing else on the bus reacts badly (watch for new DTCs, warning
   lights).
3. **Daemon development.** Write the config loader, SOC monitor, trigger
   evaluators, and safety-wrapped injector(s) described above, using the
   confirmed CAN IDs. Test on the Pi with the overlay filesystem enabled
   (see "Hardware design") before final deployment.
4. **Deployment.** Mount the Pi + PiCAN2 in the car, power from the
   accessory socket, and test across several real drive cycles, tuning the
   SOC threshold based on how early the switch needs to happen to reliably
   avoid reduced-propulsion mode.

## Open items (pending on-vehicle verification)

- Whether the mode-cycle button press is `0x1E1` bit 39 (as on the Gen 2
  reference car) or a different message on this Gen 1 car — requires step 1
  above.
- Whether a "current drive mode" status signal exists on the bus. This is
  now a hard requirement for the mode-cycle controller to work correctly,
  not a nice-to-have. **Fallback if none is found**: infer mode from
  indirect evidence instead of a single dedicated signal — e.g., correlate
  known Mountain/Hold behavior (gas engine engaging at a specific SOC
  pattern) with other already-documented signals, or as a last resort,
  track mode in software by counting presses from a known reset point (e.g.
  right after confirming a fresh Normal-on-start) and accept that state can
  drift if the driver also uses the physical button — which is also a
  reason to prefer finding a real status signal over this fallback.
- Whether the car resets to Normal on every ignition cycle or remembers the
  last-used mode — affects whether the on-start trigger needs the status
  signal too, or can assume a fixed starting mode.
- The SOC (`0x206`) raw-value-to-percentage scaling for this specific
  pack/model year — calibrate against the dash reading.

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
