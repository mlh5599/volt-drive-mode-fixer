# Signal-discovery tools (DESIGN.md Phase C)

Run these on the Pi (or a laptop with the PiCAN2) with the car **stationary**,
ignition on, engine off or on jack stands. They are throwaway investigation
aids, not part of the `voltdmf` package.

Prereqs: `can0` up at the right bitrate (see `../host/`), `can-utils` and
`python-can` installed.

| Script | DESIGN.md step | What it does |
|---|---|---|
| `logcan.sh` | Phase C | Timestamped `candump -l` capture to a file. |
| `watch_soc.py` | Phase C | Live raw + candidate decodings of `0x206`; correlate against the dash to calibrate `decode_soc`. **Effectively obsolete:** `0x206` is confirmed absent from this car's bus (0 frames in a 213 MB capture). SOC hunting has moved to the broadcast candidates + `22 005B` poll in `soc_log.py` / `soc_report.py`. |
| `mode_diff.py` | Phase C | Diffs bus traffic before/after one mode-button press; checks the `0x1E1` bit-39 candidate and flags any other frame that moved. |
| `cycle_modes.py` | Phase C | Walks all four modes, tabulating candidate status frames per mode -> finds the current-mode status signal. |
| `ignition_check.py` | Phase C | Whether the bus goes quiet with the car off, and whether a chosen status frame resets to Normal across an ignition cycle. |
| `inject_test.py` | Phase C.5 | **Transmits.** Lower-level walk/timing sweep (`--cycle`, `--repeat`, `--frames`) with its own copy of the walk loop. Use for tuning frames/gap. |
| `set_mode.py` | Phase C.5 | **Transmits.** One walk to `--target <mode>` through the real `ModeCycleController.switch_to()` + `canio` TX path. Closed loop: taps, reads the live menu cursor (`0x1F4` byte 4), stops when it's on the target, then polls byte 1 for the commit and prints the timeline. `--dry-run` needs no CAN hardware (open-loop `switch_to()` against a fake presser). Use to exercise the shipping menu-walk code. |
| `drive_log.py` | Phase C.5 | **Transmits.** Unattended: start it parked, drive normally, review at home (no live SSH). Three jobs in one run: (1) **mode hold** -- walks each `--sequence` mode through the `set_mode.py` closed loop, then samples `0x1F4` byte 1 for `--hold` s ("do MOUNTAIN/HOLD hold while moving?"); (2) **shift** -- `--shift-routine SECS` parked phase logging `0x135`/`0x1F5` + movers while you walk PRNDL; (3) **SOC** -- spawns a full `candump -l` capture and drops `SOC-MARK` anchors every `--mark-every` s to line up against a voice memo of EV-range / battery-bar (no dash %). Writes `~/vdmf-drivelog-<ts>.log` + `candump-<ts>.log`. `--lcd` mirrors the `t+<s>` clock / phase / mode / `can0` state to the SparkFun 4x20 (see `lcd.py`). `--dry-run` prints the plan, capture/shift/LCD status, and per-mode tap counts. |
| `soc_log.py` | Phase C.5 | **Passive by default** (one opt-in transmit path, below). SOC-only successor to `drive_log.py` (no mode walk, no shift routine). Start it parked, `--minutes N`, drive the pack down. Spawns a full `candump -l` capture (`~/candump-<ts>.log` -- the deliverable). With `--buttons`, the two PiCAN2 switch pads SW1 / SW2 (`--button-a-gpio` 24 / `--button-b-gpio` 23, to GND, `gpiozero`) track the dash battery gauge hands-free -- **A = an increment just dropped, B = it climbed back one** -- so every log line carries the absolute level (`gauge=7/10`); no voice memo needed. `--bars` (default 10) / `--bars-start`. `SOC-MARK` time anchors every `--mark-every` s are the backstop between drops. Taps are read by a ~5 Hz poll and commit on button *release*. Hold **both** buttons `--stop-hold` s to end cleanly without SSH; Ctrl-C (parked) also works. Every line also carries live speed + derived accel from `0x3E9` and the raw byte of every broadcast SOC candidate (`cand[3E3.0=161 …]`, passive). **`--diag-soc`** (the one transmit path; still needs `--yes`) also polls UDS `22 005B` every `--diag-soc-every` s (default 10), auto-detecting the ECU (`--diag-req-id` to pin), and stamps `soc22=61.2%(0x9C@7EC)` -- default session / mode-22 only so it won't suppress broadcasts. `--lcd` mirrors the `t+<s>` clock + live gauge level. `--dry-run` prints the plan. Analyse the capture afterward with `soc_report.py` (below) and/or `mine_capture.py --monotonic`. |
| `soc_report.py` | Phase C.5 | Read-only, stdlib only. Turns a `soc_log.py` capture + marks-log pair into the in-repo SOC analysis. `extract` (`--capture <glob>` + `--marks <soclog.log>` → `--data <json>`) parses the gauge-drop timeline and every candidate / timer / speed field, median-bins it, and computes the 9-segment timer-rejection `r` per field. `render` (`--data <json>` + `--out-dir`) draws the normalized-overlay and native-scale SVGs. A small summary JSON is committed (`../docs/analysis/data/`) so the charts rebuild without the multi-hundred-MB capture. Output lives at `../docs/analysis/session8-soc-candidates.md`. |
| `button_check.py` | Phase C.5 | Does **not** transmit. Bench check for the two `soc_log.py --buttons` panel buttons (PiCAN2 switch pads SW1 = BCM 24 / SW2 = BCM 23, to GND). Prints press/release events with a running count and tests the hold-both stop gesture; `--scan` ignores the pin args and watches every free BCM line to find where the buttons are actually wired. No CAN, no LCD. |
| `button_helper.py` | Phase C.5 | Does **not** transmit. Long-lived panel-button daemon (`systemd/voltdmf-btn.service`), dispatch by gesture: an **SW1 tap** runs `voltdmf-ctl setpoint <hold/mountain>` (toggles the reconciler setpoint -- a logged no-op until `setpoint` lands in `voltdmf-ctl`); **SW1+SW2 held `--launch-hold-secs`** (default 5, above `soc_log.py`'s 3s stop hold) claims the LCD hand-off lock, tears down the gpiozero pin factory to actually free the GPIO lines (a bare `Button.close()` leaves lgpio's `gpiochip` handle open and the kernel lines claimed), `systemctl start`s `voltdmf-soclog.service`, watches it via `systemctl is-active` until the capture ends, then re-acquires and resumes. Skips the GPIO grab if the capture unit is already active at startup (safe restart mid-run). Needs `gpiozero` -> system interpreter, not the daemon venv. `--dry-run` prints the resolved plan. |
| `lcd.py` | Phase C.5 | Does **not** transmit. Drives the SparkFun serial 4x20 LCD on the Pi UART (`/dev/serial0` @ 9600; wiring + board command set in `voltdmf/lcd.py`). `--selftest` proves every row/column; `--message` parks text; `--watch` is a ride-along dashboard for `drive_log.py` -- shared `t+<s>` clock (so a voice memo lines up with the log + capture), committed mode, `can0` state, an optional `--soc-field ID:OFF:WIDTH` raw value to eyeball against the dash, and a periodic SAY prompt. `--dry-run` prints the screen as an ASCII box, no hardware. While running it takes the LCD hand-off lock so the daemon's watch screen (below) yields the panel. |
| `mine_capture.py` | Phase C.5 | Read-only, stdlib only. Offline miner for `drive_log.py`'s `candump -l` capture. `--ids` inventories every frame; `--monotonic` ranks id/offset/width/endian fields by how cleanly they trend one way (SOC hunt); `--shift-window START END` lists ids that took discrete states in the parked shifter phase and prints every transition; `--series ID:OFF:WIDTH[:le]` dumps one field as a timestamped value series. |

## Daemon LCD watch screen

The daemon (`python -m voltdmf`) now brings the SparkFun 4x20 up on its own in
a background thread and paints an idle *watch* screen: uptime + wall clock
(row 0, proof it's alive), committed drive mode from `0x1F4` byte 1 (row 1),
PRNDL gear + `can0` error state (row 2), and a one-line summary of what the
fixer is armed to do or last did (row 3). Pass `--no-lcd` to leave the panel
alone; `--lcd-port` / `--lcd-baud` / `--lcd-backlight` tune it.

Hand-off is advisory: the tools above that drive the LCD (`lcd.py`,
`drive_log.py --lcd`, `soc_log.py --lcd`) write a small lock file
(`/run/lock/voltdmf-lcd.lock`, or `~/.voltdmf-lcd.lock` off-Pi; override with
`VOLTDMF_LCD_LOCK`); the watch thread sees it, closes its port, and idles
until the lock is dropped, then resumes. A lock left by a dead process is
ignored. `/run/lock` is used because the deployed daemon runs as `voltdmf`
under `ProtectHome=` and shares no `$HOME` with the login user's tools.

Deliverable of Phase C: fill the confirmed values into `voltdmf/signals.py` and
`voltdmf/canio.py`, flip the `confirmed` flags, and write
`../docs/signals-confirmed.md`.
