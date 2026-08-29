# Volt Drive Mode Fixer

A CAN-bus device for a Gen 1 Chevy Volt that automatically switches drive
mode to avoid fully depleting the EV battery, which otherwise triggers
reduced-propulsion mode. Two configurable trigger strategies, meant to run
together:

- **On-start → Mountain Mode**, every drive cycle.
- **SOC threshold → Hold Mode**, once the battery drops to a configurable
  percentage.
- *(Roadmap)* **Trip Mode** — switch to Hold above a speed threshold to
  bank EV range for later, based on
  [vix597/chevy-volt-trip-mode](https://github.com/vix597/chevy-volt-trip-mode).

See [DESIGN.md](DESIGN.md) for the full hardware/software design, config
schema, phased plan, and safety model.

Mode selection on this car is a single button that cycles
Normal → Sport → Mountain → Hold, so reaching a target mode means computing
how many presses to send from the current mode — see DESIGN.md for why that
makes a current-mode status signal a hard requirement.

**Status:** hardware in hand; Raspberry Pi OS Lite (Bookworm) being set up on
the Pi 3B. The daemon, config, discovery tools, and host/systemd config are
scaffolded (see "Repository layout"). Every CAN address/scaling in the code is
still the Gen 2 reference-project *candidate* value, tagged `UNCONFIRMED` /
`TODO_CALIBRATE` — next step is on-vehicle signal discovery (DESIGN.md
Phase C, `tools/`): confirm the button-press message, find a current-mode
status signal, and calibrate SOC against the dash.

## Repository layout

| Path | What |
|---|---|
| `DESIGN.md` | Full hardware/software design, phased plan, safety model. |
| `voltdmf/` | The daemon: `config`, `signals`, `state`, `triggers`, `modecycle`, `safety`, `canio`, `daemon`. `python -m voltdmf --config ... [--dry-run]`. |
| `tools/` | Phase C signal-discovery scripts (see `tools/README.md`). |
| `host/` | Pi `config.txt` snippet + `systemd-networkd` unit to bring up `can0`. |
| `systemd/` | `voltdmf.service`. |
| `docs/signals-confirmed.md` | Phase C deliverable — the verified signal table (empty until then). |
| `tests/` | `pytest` unit tests (hardware-free). |

## Developing

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

`--dry-run` reads and evaluates against a live bus but transmits nothing — the
safe mode for early on-vehicle testing.

**Hardware:** PiCAN2 (Raspberry Pi CAN-bus HAT) + Raspberry Pi 3B, connected
to the OBD-II port via an off-the-shelf OBD-II-to-DB9 cable, powered from
the car's (switched) accessory socket.

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), SavvyCAN/`can-utils` for logging,
`python-can` (SocketCAN backend) for the daemon.
