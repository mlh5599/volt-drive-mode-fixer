# Dash enclosure — Pi 3B+ / PiCAN2 / SparkFun 20×4 LCD

Parametric 3D-printable case for the in-car drive-mode-fixer stack, sized to
mount on the dash **left of the steering wheel, between the wheel and the door,
just above the vent**, with the screen pitched back toward the driver.

Model: [`volt_dash_case.scad`](volt_dash_case.scad) (OpenSCAD, fully
parametric via the Customizer panel).

```
   ┌────────────────────────────┐   ← wedge lid: LCD + 2 buttons, tilted 20°
   │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓   ●        │      back toward the driver
   │  ▓  20×4 CHAR  ▓   ●        │      ● = SW1 / SW2  (fore/aft column,
   │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓            │        wheel side)
  ┌┴────────────────────────────┴┐  ← lid skirt laps down over the tub
DB9╡    Pi 3B+  +  PiCAN2 HAT    ╞→   ← 4× horizontal M3 through the skirt,
 ◄─╡    (shifted toward DB9)     ╞      front + back, into wall-boss inserts
  └──────────────────────────────┘
        (optional) dash foot  →  VHB to the dash, adds ~10° yaw toward driver
```

Three parts, all printed:

| part       | what it is                                                        |
|------------|-----------------------------------------------------------------|
| `base`     | upright tub — holds the Pi/PiCAN2 stack, DB9 + power + SD cutouts, vents |
| `lid`      | flat plate + hollow wedge — carries the LCD (20° pitch back) and the two panel buttons |
| `foot`     | **optional** — VHB-taped foot that also yaws the whole case ~10° so its front face aims at the driver |

The lid has an outer **skirt** that laps ~11 mm down over the tub. Four
horizontal M3 screws pass through the skirt on the front and back faces, near
the X-ends and above the board stack, into heat-set inserts in bosses on the
tub's inner wall. Nothing penetrates the driver-facing face or the wedge top —
to open it, back the four screws out and lift the lid straight up.

---

## 1. Measure first — do not print the real thing off the defaults

Several defaults are **photo estimates**. Set these in the OpenSCAD Customizer
(or edit the file) from calipers on your actual hardware, then print
`part="fitcheck"` (a thin frame of just the LCD pocket, button holes and DB9
window, ~30 min) and confirm everything lines up.

| variable            | default | what to measure |
|---------------------|--------:|-----------------|
| `stack_gap`         | 16      | Pi PCB **top** face → PiCAN2 PCB **bottom** face — i.e. the stacking header you actually fitted. Must clear the ~15.5 mm USB/Ethernet stack. |
| `pican_top`         | 13      | tallest thing on the PiCAN2 top face (DB9 shell / electrolytic cap), up from its PCB |
| `db9_axis_z`        | 9       | DB9 connector centre-line height above the PiCAN2 PCB |
| `db9_reach`         | 10      | how far the DB9 shell sticks out past the PiCAN2 PCB edge — sets how far the board stack is shoved toward that wall so the OBD plug can actually mate |
| `lcd_hole_grid`     | 98 × 53 | **centre-to-centre of the 4 LCD mounting holes** — the estimate is the least trustworthy number here |
| `lcd_screw_d`       | 2.3     | LCD screw size — the board takes small screws (≈ 2‑56 / M2), *not* 4‑40 |
| `lcd_back_depth`    | 16      | PIC backpack + JST + wiring sticking out behind the LCD PCB |
| `lcd_window` / `lcd_window_off` | 79×27 / [0,4] | active-glass size, and its offset from the PCB centre (glass usually sits high) |
| `button_hole_d`     | 12.2    | your buttons' panel thread Ø (16 mm-body buttons need ~16.2 **and** a wider case) |
| `heatset_d`         | 4.0     | pilot bore your M3 brass heat-set inserts want |

The Customizer also echoes three checks to the console on F5 — outer size,
overall height front/back, and **LCD backpack clearance** (want > 3 mm; raise
`headroom` or shrink `lcd_back_depth` if it goes negative).

With the defaults the tub is roughly **118 × 73 mm** and **53 mm** to the rim;
with the lid on, the case stands about **68 mm** at the front lip rising to
**97 mm** at the back. Most of that height is `stack_gap` + `pican_top` +
`headroom`; a shorter stacking header shrinks the whole thing. The console
echoes the exact outer size and front/back height on F5.

---

## 2. Render & export

1. Open `volt_dash_case.scad` in OpenSCAD (≥ 2021).
2. **Window → Customizer** to get the parameter panel. Set the measured values.
3. Set **What to render → `part`**:
   - `fitcheck` → print this first
   - `base`, `lid`, `foot` → the real parts
   - `lens` → 2D outline; **Design → Export as DXF/SVG**, laser/knife-cut from
     `lens_t` clear acrylic or PETG
   - `assembly` → visual sanity check, do **not** print
4. F5 to preview, F6 to render, **File → Export → Export as STL**.

> This model F6-renders headless (OpenSCAD 2021.01): `base` / `lid` / `foot` /
> `fitcheck` all come out as valid 2-manifolds with bounding boxes matching the
> echoed dimensions. It has **not** been visually inspected — open the
> `assembly` view (F5) and eyeball the LCD pocket, button strip and DB9 window
> against the mock stack before committing print time.

---

## 3. Print settings

| | |
|---|---|
| **Material** | **ASA or PETG. Not PLA** — a closed case on a dark dash in sun will exceed PLA's glass transition and the wedge will droop. ASA is best; PETG acceptable; PC overkill. |
| Layer height | 0.2 mm |
| Walls / perimeters | 4 (≈ 1.6 mm; the shell params assume ≥ 3) |
| Top/bottom | 5 layers |
| Infill | 25–30 % gyroid |
| Supports | **none needed** — the wedge underside is the flat plate, the pocket/button walls stay within ~20° of vertical, and the window is a through-hole. Add a support blocker if your slicer wants to bridge the DB9 cutout. |

**Orientation**

- `base` — as modelled, floor on the bed.
- `lid` — **flip it screen-face-down on the bed** (the wedge points up at an
  angle; the flat plate is the top surface as printed). This puts the visible
  LCD face against the smooth bed and needs no support. `part="plate"` places
  base + lid side by side but does **not** flip the lid — rotate it in the
  slicer.
- `foot` — big flat tape face on the bed.

---

## 4. Bill of materials

| qty | item | notes |
|----:|------|-------|
| 4 | M3 socket-cap screws, 10–12 mm | horizontal, through the lid skirt into the tub wall bosses. Need ≈ `skirt_wall` + `skirt_gap` + insert ≈ 9 mm engaged, so 10–12 mm. |
| 4 | M3 brass heat-set inserts | into the bosses on the tub's inner front/back walls. Melt in from the outside face with a soldering iron + insert tip. |
| 4 | screws to suit `lcd_screw_d` (≈ M2 / 2‑56), 6–8 mm | LCD → bosses. Self-tap into the printed bosses, or add M2 heat-sets. |
| 2 | screws to suit `pi_screw_d` (M2.5), ~6 mm | *optional* — Pi → floor standoffs, up through the floor. The Pi also just sits captive on the standoffs under the lid. |
| 2 | panel-mount momentary buttons, `button_hole_d` thread | pre-wire ~150 mm leads: one leg each to PiCAN2 **SW1** (BCM24) and **SW2** (BCM23), other legs commoned to **GND**. |
| — | 3M VHB / automotive foam tape | under the `foot` (or straight under the `base` if you skip the foot). Degrease the dash first; foam takes up mild dash curvature. |
| 1 | clear acrylic/PETG lens, `lens_t` mm, from `part="lens"` | *optional* — press-fits into the rebate over the window. |
| 1 | OBD-II → DB9 cable | must wire OBD pins **6/14** (HS-CAN) to the DB9. Right-angle DB9 already on the PiCAN2. |
| 1 | USB power lead for the Pi | micro-USB; exits the front (`-Y`) wall low, next to the DB9 corner. |

---

## 5. Assembly order

1. Heat-set the 4× M3 inserts into the tub wall bosses, from the outside face.
   The LCD bosses in the lid self-tap — no inserts needed there.
2. Fit the two buttons into the lid from outside; tighten their nuts in the
   finger dishes. Route their leads.
3. Drop the LCD into the wedge pocket, screw it to the bosses. (Lens, if used,
   press-fits into the rebate on top afterwards.)
4. Seat the Pi on the base standoffs; stack the PiCAN2 on it. Feed the DB9 out
   through its wall cutout and the USB power lead out the front.
5. Connect the button leads to SW1 / SW2 / GND and the LCD to `/dev/serial0`
   + 5 V + GND.
6. Lower the lid — the skirt slides down over the tub and locates it — and
   drive the 4 M3 screws in horizontally through the skirt, two per long face.
7. If using the `foot`: VHB the foot to the dash, then drop the assembled case
   onto it — the raised lip on the foot captures the base floor so it can't
   slide off. Add a strip of VHB between foot and base if you want it fixed.

---

## 6. Install / cable routing

- Mount spot: dash, **left of the wheel, between wheel and door, above the
  vent**. Screen pitched back `tilt_deg` (20°); the optional `foot` adds
  `foot_yaw` (10°) so the front face turns toward the driver. Tune both to your
  seating position — dry-fit with the lid loose before taping anything down.
- **DB9 → OBD-II**: `db9_edge` defaults to `"left"` (toward the steering
  column / under-dash OBD-II port). Route the cable down the left of the
  column. Change to `"front"` / `"right"` / `"back"` if your run is cleaner
  another way.
- The **board stack is shifted** toward the DB9 wall by `db9_reach` so the
  right-angle DB9 nearly meets its window — measure `db9_reach` or the OBD
  plug may bottom out on air short of the connector. The LCD in the lid stays
  centred; only the Pi/PiCAN2 mount holes move.
- **USB power**: exits the front wall low. Run to a fused 12 V→5 V supply or
  the accessory socket. Keep a service loop.
- Vents are on the back wall and the non-DB9 short wall only — never the
  driver-facing face — so glare and dust stay down.
- The Pi's journald is volatile (RAM); a key-off cuts power with no clean
  shutdown. That's expected for this build — nothing in the case changes it.

---

## 7. Known constraints — check if you re-parameterise

- **Buttons beside an 80 mm window**: there's only ~18 mm of face between the
  window edge and the side wall, so the two buttons default to a **fore/aft
  column** (`button_stack="y"`). Side-by-side (`"x"`) needs a wider case
  (raise `slack_x`). 16 mm-body buttons need both a bigger `button_hole_d` and
  a wider case.
- The lid screws run through the skirt on the front/back faces, well clear of
  the button column — but if you switch to `button_stack="x"` (side-by-side),
  re-check the button spacing against `screw_x_in`.
- `roof_t` (4.5) must stay ≥ `lcd_pocket_d` + ~1.5 mm or the pocket floor gets
  too thin — the model echoes a warning if not.
- If `stack_gap` comes out much larger than 16, re-check the DB9 window height
  (`z_db9_axis` is derived from it) and reprint `fitcheck`.
