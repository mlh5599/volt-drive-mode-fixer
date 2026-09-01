# Volt Drive Mode Fixer

A CAN-bus device for a Gen 1 Chevy Volt that automatically switches drive
mode to avoid fully depleting the EV battery, which otherwise triggers
reduced-propulsion mode.

A single **level-triggered reconciler** continuously drives the car toward a
desired mode:

- **SOC-HOLD floor** — always active. When SOC falls to a configurable
  percentage (target: ~2 gauge bars) the reconciler holds the car in Hold
  Mode until the pack recovers. This floor always wins.
- **Setpoint** — `auto` at boot (no target; the daemon leaves the car alone
  above the floor). A panel button then selects **HOLD** or **MOUNTAIN**,
  which the reconciler enforces above the floor. Not persisted; every boot
  starts back at `auto`.
- *(Roadmap)* **Trip Mode** — a speed-based setpoint that banks EV range on
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
now **implemented**. `--dry-run` is gone: the daemon boots **armed** but
passive (`default_setpoint: auto` — no target until the driver picks a
setpoint or the SOC floor engages), and `voltdmf-ctl disarm` is the mid-drive
stop. Still to do: migrate the out-of-repo `roles/voltdmf` ExecStart /
`config.yaml`, then validate the 33 % floor timing over several drives.
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
passive: with `default_setpoint: auto` it has no target and walks the car
nowhere until the driver selects HOLD/MOUNTAIN or the SOC-HOLD floor engages.

The daemon runs permanently as root under systemd; change modes and daemon
state from an unprivileged account with `voltdmf-ctl` (`status` / `arm` /
`disarm` / `setpoint <hold|mountain>` / `set-mode <mode>` / `reload`) over its
control socket. `voltdmf-ctl disarm` is the mid-drive stop. See `host/README.md`
§"Runtime control" and DESIGN.md §"Runtime control". The panel SW1 tap drives
`setpoint`; the SOC-HOLD floor overrides the setpoint to HOLD whenever the
pack is low.

**Hardware:** PiCAN2 (Raspberry Pi CAN-bus HAT) + Raspberry Pi 3B, connected
to the OBD-II port via an off-the-shelf OBD-II-to-DB9 cable, powered from
the car's (switched) accessory socket.

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), SavvyCAN/`can-utils` for logging,
`python-can` (SocketCAN backend) for the daemon.
