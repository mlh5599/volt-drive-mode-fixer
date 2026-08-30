# Volt Drive Mode Fixer

A CAN-bus device for a Gen 1 Chevy Volt that automatically switches drive
mode to avoid fully depleting the EV battery, which otherwise triggers
reduced-propulsion mode.

A single **level-triggered reconciler** continuously drives the car toward a
desired mode:

- **SOC-HOLD floor** — always active. When SOC falls to a configurable
  percentage (target: ~2 gauge bars) the reconciler holds the car in Hold
  Mode until the pack recovers. This floor always wins.
- **Setpoint** — a panel button toggles the reconciler between **HOLD**
  (leave the car alone above the floor) and **MOUNTAIN** (hold Mountain Mode
  above the floor). Not persisted; every boot starts in HOLD.
- *(Roadmap)* **Trip Mode** — a speed-based setpoint that banks EV range on
  the highway, based on
  [vix597/chevy-volt-trip-mode](https://github.com/vix597/chevy-volt-trip-mode).

See [DESIGN.md](DESIGN.md) for the full hardware/software design, config
schema, phased plan, and safety model.

Mode selection on this car is a single button that cycles
Normal → Sport → Mountain → Hold, so reaching a target mode means computing
how many presses to send from the current mode — see DESIGN.md for why that
makes a current-mode status signal a hard requirement.

**Status (2026-08-30):** deployed on the Pi (`voltpi`), field testing in
Phase C, `--dry-run`. Confirmed on-road: the mode-button input (`0x1E1`), the
current-mode status signal (`0x1F4` byte 1, the daemon's mode source), and
shift/PRNDL (`0x1F5` byte 3); the closed-loop menu walk has an on-road PASS.
**SOC is the one open signal** and the last blocker on a non-dry-run daemon —
narrowed to three broadcast candidates but not yet scaled to a percentage.
The reconciler itself is designed (see DESIGN.md §"Mode policy") but not yet
implemented, blocked on the same SOC calibration. Progress and next steps:
[`docs/phase-c-field-checklist.md`](docs/phase-c-field-checklist.md) and
[`docs/field-session-log.md`](docs/field-session-log.md).

## Repository layout

| Path | What |
|---|---|
| `DESIGN.md` | Full hardware/software design, phased plan, safety model. |
| `voltdmf/` | The daemon: `config`, `signals`, `state`, `triggers`, `modecycle`, `safety`, `canio`, `daemon`, `control`/`ctl`. `python -m voltdmf --config ... [--dry-run]`. |
| `tools/` | Phase C signal-discovery scripts (see `tools/README.md`), incl. `soc_log.py` (drive capture) and `soc_report.py` (analysis → `docs/analysis/`). |
| `host/` | Pi `config.txt` snippet + `systemd-networkd` unit to bring up `can0`. |
| `systemd/` | `voltdmf.service` + `voltdmf.socket` (control socket); `voltdmf-soclog.service` + `voltdmf-btn.service` (panel-launched SOC capture). |
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

`--dry-run` reads and evaluates against a live bus but transmits nothing — the
safe mode for early on-vehicle testing.

The daemon runs permanently as root under systemd; change modes and daemon
state from an unprivileged account with `voltdmf-ctl` (`status` / `arm` /
`disarm` / `set-mode <mode>` / `reload`) over its control socket. A non-dry-run
daemon boots **disarmed** until `voltdmf-ctl arm`. See `host/README.md`
§"Runtime control" and DESIGN.md §"Runtime control". (The reconciler's
`setpoint <hold|mountain>` control and the panel button that drives it are
designed but not yet implemented; blocked on SOC.)

**Hardware:** PiCAN2 (Raspberry Pi CAN-bus HAT) + Raspberry Pi 3B, connected
to the OBD-II port via an off-the-shelf OBD-II-to-DB9 cable, powered from
the car's (switched) accessory socket.

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), SavvyCAN/`can-utils` for logging,
`python-can` (SocketCAN backend) for the daemon.
