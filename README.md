# Volt Drive Mode Fixer

A CAN-bus device for a Gen 1 Chevy Volt that automatically switches drive
mode to avoid fully depleting the EV battery, which otherwise triggers
reduced-propulsion mode.

A single **level-triggered reconciler** continuously drives the car toward a
desired mode:

- **Three-position selector** — one panel button (SW1), tapped forward, with
  a fourth tap returning to the first position. Not persisted; every boot
  starts back at position 1.
  1. **`hold`** (default) — hold the pack at 30 %. Passive above that: the car
     drives exactly as it normally would until the SOC-HOLD floor engages.
  2. **`mountain`** — enforce Mountain Mode continuously.
  3. **`off`** — do nothing at all, SOC floor included: the car behaves as if
     the device were not plugged in.
- **SOC-HOLD floor** — live in positions 1 and 2, and it always wins there.
  When SOC falls to a configurable percentage (30 %, ≈ 2 gauge bars) the
  reconciler holds the car in Hold Mode until the pack recovers.
- *(Roadmap)* **Trip Mode** — a speed-based detent that banks EV range on
  the highway, based on
  [vix597/chevy-volt-trip-mode](https://github.com/vix597/chevy-volt-trip-mode).

See [DESIGN.md](DESIGN.md) for the full hardware/software design, config
schema, phased plan, and safety model.

Mode selection on this car is a single button that cycles
Normal → Sport → Mountain → Hold, so reaching a target mode means computing
how many presses to send from the current mode — see DESIGN.md for why that
makes a current-mode status signal a hard requirement.

**Status (2026-08-31):** deployed on the Pi (`voltpi`), field testing in
Phase C. Confirmed on-road: the mode-button input (`0x1E1`), the current-mode
status signal (`0x1F4` byte 1, the daemon's mode source), and shift/PRNDL
(`0x1F5` byte 3); the closed-loop menu walk has an on-road PASS. Session 9
resolved SOC — the `22 005B` UDS poll gives exact pack percent and the
gauge↔SOC curve is near-linear — so the reconciler and its SOC-HOLD floor are
now **implemented**. `--dry-run` is gone: the daemon boots **armed** on the
first detent of the selector (`default_position: hold` — passive until the
SOC floor engages), and `voltdmf-ctl disarm` is the mid-drive stop. Still to
do: migrate the out-of-repo `roles/voltdmf` ExecStart / `config.yaml`, then
validate the 30 % floor timing over several drives.
Progress and next steps:
[`docs/phase-c-field-checklist.md`](docs/phase-c-field-checklist.md) and
[`docs/field-session-log.md`](docs/field-session-log.md).

## Repository layout

| Path | What |
|---|---|
| `DESIGN.md` | Full hardware/software design, phased plan, safety model. |
| `voltdmf/` | The daemon: `config`, `signals`, `state`, `reconciler`, `modecycle`, `safety`, `canio`, `daemon`, `control`/`ctl`. `python -m voltdmf --config ... [--start-disarmed]`. |
| `tools/` | Phase C signal-discovery scripts (see `tools/README.md`), incl. `soc_log.py` (drive capture) and `soc_report.py` (analysis → `docs/analysis/`). |
| `host/` | Pi `config.txt` snippet + `systemd-networkd` unit to bring up `can0`. |
| `systemd/` | `voltdmf.service` + `voltdmf.socket` (control socket); `voltdmf-btn.service` (panel gestures) launching `voltdmf-soclog.service` (SW1+SW2 → SOC capture) / `voltdmf-chargelog.service` (SW2 solo-hold → charge-mode capture). |
| `docs/signals-confirmed.md` | Phase C deliverable — the verified signal table. |
| `docs/field-session-log.md` | Running narrative of the on-vehicle sessions. |
| `docs/phase-c-field-checklist.md` | Phase C progress tracker and per-drive procedures. |
| `docs/analysis/` | In-repo write-ups + regenerable charts. |
| `tests/` | `pytest` unit tests (hardware-free). |

## Developing

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

`--start-disarmed` boots with transmission suppressed (the reconciler still
runs and logs what it *would* do, and the `22 005B` SOC poll still transmits)
— the safe mode for bench work. Normally the daemon boots **armed** but
passive: with `default_position: hold` it has no target and walks the car
nowhere until the SOC-HOLD floor engages or the driver taps SW1 round to
`mountain`.

The daemon runs permanently as root under systemd; change modes and daemon
state from an unprivileged account with `voltdmf-ctl` (`status` / `arm` /
`disarm` / `setpoint <hold|mountain|off|next>` / `set-mode <mode>` / `reload` /
`walk-test` / `test-mode <on|off>` / `probe <mode>`) over its control socket.
`voltdmf-ctl disarm` is the mid-drive
stop. See `host/README.md` §"Runtime control" and DESIGN.md §"Runtime
control". The panel SW1 tap sends `setpoint next` — one detent forward — and
an SW1 solo-hold (≥ 8 s) drives `walk-test`, a closed-loop mode-walk self-test
that cycles every mode and restores the start mode. The SOC-HOLD floor
overrides to HOLD whenever the pack is low, in every position but `off`.

**Hardware:** PiCAN2 (Raspberry Pi CAN-bus HAT) + Raspberry Pi 3B, connected
to the OBD-II port via an off-the-shelf OBD-II-to-DB9 cable, powered from
the car's (switched) accessory socket.

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), SavvyCAN/`can-utils` for logging,
`python-can` (SocketCAN backend) for the daemon.
