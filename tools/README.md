# Signal-discovery tools (DESIGN.md Phase C)

Run these on the Pi (or a laptop with the PiCAN2) with the car **stationary**,
ignition on, engine off or on jack stands. They are throwaway investigation
aids, not part of the `voltdmf` package.

Prereqs: `can0` up at the right bitrate (see `../host/`), `can-utils` and
`python-can` installed.

| Script | DESIGN.md step | What it does |
|---|---|---|
| `logcan.sh` | Phase C | Timestamped `candump -l` capture to a file. |
| `watch_soc.py` | Phase C | Live raw + candidate decodings of `0x206`; correlate against the dash to calibrate `decode_soc`. |
| `mode_diff.py` | Phase C | Diffs bus traffic before/after one mode-button press; checks the `0x1E1` bit-39 candidate and flags any other frame that moved. |
| `cycle_modes.py` | Phase C | Walks all four modes, tabulating candidate status frames per mode -> finds the current-mode status signal. |
| `ignition_check.py` | Phase C | Whether the bus goes quiet with the car off, and whether a chosen status frame resets to Normal across an ignition cycle. |
| `inject_test.py` | Phase C.5 | **Transmits.** Lower-level walk/timing sweep (`--cycle`, `--repeat`, `--frames`) with its own copy of the walk loop. Use for tuning frames/gap. |
| `set_mode.py` | Phase C.5 | **Transmits.** One walk to `--target <mode>` through the real `ModeCycleController.switch_to()` + `canio` TX path. Closed loop: taps, reads the live menu cursor (`0x1F4` byte 4), stops when it's on the target, then polls byte 1 for the commit and prints the timeline. `--dry-run` needs no CAN hardware (open-loop `switch_to()` against a fake presser). Use to exercise the shipping menu-walk code. |
| `drive_log.py` | Phase C.5 | **Transmits.** Unattended: start it parked, drive normally, review at home (no live SSH). Three jobs in one run: (1) **mode hold** -- walks each `--sequence` mode through the `set_mode.py` closed loop, then samples `0x1F4` byte 1 for `--hold` s ("do MOUNTAIN/HOLD hold while moving?"); (2) **shift** -- `--shift-routine SECS` parked phase logging `0x135`/`0x1F5` + movers while you walk PRNDL; (3) **SOC** -- spawns a full `candump -l` capture and drops `SOC-MARK` anchors every `--mark-every` s to line up against a voice memo of EV-range / battery-bar (no dash %). Writes `~/vdmf-drivelog-<ts>.log` + `candump-<ts>.log`. `--lcd` mirrors the `t+<s>` clock / phase / mode / `can0` state to the SparkFun 4x20 (see `lcd.py`). `--dry-run` prints the plan, capture/shift/LCD status, and per-mode tap counts. |
| `soc_log.py` | Phase C.5 | Does **not** transmit. SOC-only successor to `drive_log.py` (no mode walk, no shift routine). Start it parked, `--minutes N`, drive the pack down. Spawns a full `candump -l` capture (`~/candump-<ts>.log` -- the deliverable). With `--buttons`, two panel buttons on the Pi header (`--button-a-gpio` 5 / `--button-b-gpio` 6, to GND, `gpiozero`) track the dash battery gauge hands-free -- **A = an increment just dropped, B = it climbed back one** -- so every log line carries the absolute level (`gauge=7/10`); no voice memo needed. `--bars` (default 10) / `--bars-start`. `SOC-MARK` time anchors every `--mark-every` s are the backstop between drops. Hold **both** buttons `--stop-hold` s to end cleanly without SSH; Ctrl-C (parked) also works. `--lcd` mirrors the `t+<s>` clock + live gauge level. `--dry-run` prints the plan. Mine the capture afterward with `mine_capture.py --monotonic` and anchor the top field to the `GAUGE-DOWN` timestamps. |
| `lcd.py` | Phase C.5 | Does **not** transmit. Drives the SparkFun serial 4x20 LCD on the Pi UART (`/dev/serial0` @ 9600; wiring + board command set in `voltdmf/lcd.py`). `--selftest` proves every row/column; `--message` parks text; `--watch` is a ride-along dashboard for `drive_log.py` -- shared `t+<s>` clock (so a voice memo lines up with the log + capture), committed mode, `can0` state, an optional `--soc-field ID:OFF:WIDTH` raw value to eyeball against the dash, and a periodic SAY prompt. `--dry-run` prints the screen as an ASCII box, no hardware. |
| `mine_capture.py` | Phase C.5 | Read-only, stdlib only. Offline miner for `drive_log.py`'s `candump -l` capture. `--ids` inventories every frame; `--monotonic` ranks id/offset/width/endian fields by how cleanly they trend one way (SOC hunt); `--shift-window START END` lists ids that took discrete states in the parked shifter phase and prints every transition; `--series ID:OFF:WIDTH[:le]` dumps one field as a timestamped value series. |

Deliverable of Phase C: fill the confirmed values into `voltdmf/signals.py` and
`voltdmf/canio.py`, flip the `confirmed` flags, and write
`../docs/signals-confirmed.md`.
