// ============================================================================
//  volt_dash_case.scad  --  parametric enclosure for the Volt drive-mode-fixer
// ----------------------------------------------------------------------------
//  Houses the stack:
//     Raspberry Pi 3B+   (85 x 56 mm)
//   + PiCAN2 HAT          (RSP-PICAN2 Rev B, right-angle DB9 on a short edge)
//   + SparkFun serial 20x4 LCD  (LCD-09568 / Canada Robotix 630, 105 x 60 mm PCB)
//
//  Two printed shell parts + one optional dash foot:
//     base   -- upright tub: Pi/PiCAN2 stack, cable exits, vents
//     lid    -- flat plate + hollow wedge; carries the LCD pitched back toward
//               the driver, plus two panel-mount buttons beside the screen
//     foot   -- OPTIONAL: VHB-taped dash foot that also yaws the whole case so
//               its front face aims at the driver. Mount point: dash, left of
//               the wheel, between wheel and door, above the vent.
//
//  The lid has an outer SKIRT that laps down over the tub. Four horizontal M3
//  screws pass through the skirt (front + back walls, near the X-ends, above the
//  board stack) into heat-set inserts in bosses on the tub's inner wall. No
//  screw breaks the driver-facing face or the wedge top; the visible faces stay
//  clean and you open the case by pulling the lid straight up.
//
//  >>> F6-renders clean and 2-manifold headless for base / lid / foot / fitcheck
//      (OpenSCAD 2021.01). Open in OpenSCAD for the F5 preview, then
//      File > Export > Export as STL.
//  >>> MEASURE every value tagged `*MEASURE` on your actual hardware first.
//      Print  part="fitcheck"  (a thin frame of just the mating features) to
//      confirm the LCD holes, button holes and DB9 window line up before you
//      commit ~10 h of printing.
// ============================================================================

/* [What to render] */
// base     = the tub
// lid      = plate + LCD/button wedge
// foot     = optional dash foot
// lens     = 2D outline for a cut clear window (Design > Export as DXF/SVG)
// fitcheck = thin test frame: LCD pocket + button holes + DB9 window
// plate    = base + lid side by side (flip the lid screen-down in your slicer)
// assembly = everything positioned together (visual check only, do not print)
part = "assembly";

/* [Quality] */
$fn = 64;          // 32 while editing; 96+ for the final F6 render
eps = 0.05;        // overlap so coincident faces don't z-fight
BIG = 600;         // "infinite" cutter

// ============================================================================
//  BOARD + COMPONENT DIMENSIONS   (`*MEASURE` = verify on your hardware)
// ============================================================================

/* [Raspberry Pi 3B+] */
pi_pcb        = [85, 56, 1.4];    // outline
pi_hole_grid  = [58, 49];         // centre-to-centre of the 4 mount holes
pi_screw_d    = 2.9;              // M2.5 clearance (screw up through the floor)
under_pi      = 3.0;              // gap under the Pi for solder tails

/* [PiCAN2 HAT] */
// *MEASURE stack_gap: Pi PCB TOP face -> PiCAN2 PCB BOTTOM face = whatever
// stacking header you fitted. The PiCAN2 must clear the ~15.5 mm USB/Ethernet
// stack, so real builds land around 15-18 mm.
stack_gap     = 16;
pican_pcb     = [65, 56, 1.4];
// *MEASURE pican_top: tallest thing on the PiCAN2 top face (DB9 shell or the
// electrolytic cap), measured up from the PiCAN2 PCB.
pican_top     = 13;

/* [PiCAN2 DB9 -> OBD-II cable] */
// Right-angle male DB9 on one SHORT (56 mm) edge of the PiCAN2.
// db9_edge: "left" (-X) / "right" (+X) / "front" (-Y) / "back" (+Y).
// "left" aims it at the steering column / under-dash OBD-II port.
db9_edge      = "left";
db9_axis_z    = 9;        // *MEASURE: DB9 centre-line above the PiCAN2 PCB
db9_win       = [42, 22]; // wall opening (DB9 body + mating-hood thumbscrews)
// *MEASURE db9_reach: DB9 shell protrusion past the PiCAN2 PCB edge (~8-12 mm).
// The board stack is shoved along the DB9 axis so the connector nearly meets the
// wall window -- a DB9 left recessed deep in the case will not mate and the
// cable thumbscrews will not reach the jack posts.
db9_reach     = 10;

/* [SparkFun LCD-09568  (serial 20x4)] */
lcd_pcb        = [105, 59.9, 1.6];
// *MEASURE lcd_hole_grid with calipers: centre-to-centre of the 4 corner
// mounting holes. 98 x 53 is a PHOTO ESTIMATE -- do not trust it.
lcd_hole_grid  = [98, 53];
lcd_screw_d    = 2.3;     // *MEASURE: LCD uses small screws (~2-56 / M2)
// *MEASURE lcd_back_depth: PIC backpack + JST + wiring behind the LCD PCB.
// Drops through the plate into `headroom`; keep headroom >= this + 3 (echo check).
lcd_back_depth = 12;
lcd_window     = [79, 27];    // through-cut for the character glass
                              // (a bit over the ~77 x 25 active area)
// *MEASURE lcd_window_off: active-area centre relative to the PCB centre.
// The glass usually sits high on the board -> +Y.
lcd_window_off = [0, 4];
lcd_lens       = true;    // rebate around the window for a press-fit lens
lens_t         = 1.5;     // lens stock thickness
lens_margin    = 3;       // lens overlaps the window by this much all round

/* [Panel buttons  (external, momentary, flying leads to SW1/SW2 + GND)] */
button_hole_d   = 12.2;  // *MEASURE. 12 mm thread is typical; 16 mm-body needs ~16.2
button_pitch    = 24;    // centre-to-centre of the two buttons
button_side     = -1;    // -1 = between screen and wheel (right thumb); +1 = door side
button_gap      = 9;     // LCD-window edge -> button-column centre
button_stack    = "y";   // "y" = one fore/aft column beside the screen (fits the
                         //       narrow strip left of an 80 mm window);
                         // "x" = side-by-side -- only if you widen the case
button_recess_d = 18;    // shallow finger dish around each button
button_recess_h = 1.6;

// ============================================================================
//  ENCLOSURE SHELL
// ============================================================================

/* [Shell] */
wall        = 2.4;       // side walls (6 perimeters @ 0.4 nozzle)
floor_t     = 2.4;
lid_plate_t = 3.0;       // flat part of the lid, spans the tub mouth
roof_t      = 4.5;       // wedge roof thickness (>= LCD pocket depth + a real floor)
// headroom: gap from the top of the PiCAN2 stack to the underside of the lid
// plate. The LCD backpack (`lcd_back_depth`) drops through the plate into this
// space, so keep headroom >= lcd_back_depth + 3. One of the four size drivers
// (with stack_gap, pican_top, tilt_deg) -- see README section 7.
headroom    = 16;
tilt_deg    = 20;        // screen pitch-back toward a seated driver
lid_front_h = 12;        // wedge thickness at its thin (front / -Y) edge
r_out       = 3;         // outer corner radius
r_in        = 1.5;
fit_gap     = 0.35;      // slip fit for inserted parts
slack_x     = 8;         // spare interior length beyond the widest board (X)
slack_y     = 8;         // spare interior width  beyond the deepest board (Y)

/* [Fasteners  --  lid held by 4 horizontal M3 through an outer skirt] */
screw_d      = 3.3;      // M3 clearance
heatset_d    = 4.0;      // *CHECK your inserts: M3 brass heat-set pilot bore
heatset_l    = 5.0;
skirt_h      = 11;       // how far the lid skirt laps down over the tub outside
skirt_wall   = 3.0;
skirt_gap    = 0.3;      // clearance, skirt inner face -> tub outer wall
screw_z      = 6;        // screw axis, measured DOWN from the tub rim
screw_x_in   = 15;       // screw axis, in from the tub end wall (X)
boss_d       = 9;        // wall boss OD around each heat-set insert
boss_len     = 9;        // how far the boss reaches in from the inner wall

/* [Vents] */
vent_slot   = [3, 16];   // one slot: width x height
vent_count  = 4;
vent_pitch  = 7;

/* [Dash foot  (part="foot")] */
foot_yaw    = 10;        // twist so the case front face aims at the driver
foot_rise   = 14;        // foot height at its tall edge
foot_pad    = 10;        // extra tape footprint all round
foot_wall   = 3;

// ============================================================================
//  DERIVED GEOMETRY
// ============================================================================
stack_h = under_pi + pi_pcb[2] + stack_gap + pican_pcb[2] + pican_top;

in_x = max(pi_pcb[0], pican_pcb[0], lcd_pcb[0]) + slack_x;   // interior X
in_y = max(pi_pcb[1], pican_pcb[1], lcd_pcb[1]) + slack_y;   // interior Y
OW   = in_x + 2*wall;                                        // tub outer X
OD   = in_y + 2*wall;                                        // tub outer Y
case_h = floor_t + stack_h + headroom;                       // tub rim height

// lid plate + skirt wrap the OUTSIDE of the tub
lid_ow = OW + 2*skirt_gap + 2*skirt_wall;
lid_od = OD + 2*skirt_gap + 2*skirt_wall;

z_pi_bottom = floor_t + under_pi;
z_pican_pcb = z_pi_bottom + pi_pcb[2] + stack_gap;
z_db9_axis  = z_pican_pcb + pican_pcb[2] + db9_axis_z;
z_plate_top = case_h + lid_plate_t;

// wedge top: plane tilted `tilt_deg` about X, `lid_front_h` above the plate at
// the front (-Y) outer edge.
z_wedge_ref  = z_plate_top + lid_front_h + (lid_od/2)*tan(tilt_deg);
lcd_pocket_d = lcd_pcb[2] + 0.6;

// horizontal lid screws: axis along Y, at two X positions, through front + back
screw_x_pos = [ -(in_x/2 - screw_x_in), (in_x/2 - screw_x_in) ];
screw_z_abs = case_h - screw_z;

// shove the board stack toward the DB9 wall so the connector meets the window
stack_dx = (db9_edge == "left")  ? -max(0, in_x/2 - wall - pican_pcb[0]/2 - db9_reach - 2)
         : (db9_edge == "right") ?  max(0, in_x/2 - wall - pican_pcb[0]/2 - db9_reach - 2)
         : 0;
stack_dy = (db9_edge == "front") ? -max(0, in_y/2 - wall - pican_pcb[1]/2 - db9_reach - 2)
         : (db9_edge == "back")  ?  max(0, in_y/2 - wall - pican_pcb[1]/2 - db9_reach - 2)
         : 0;

// sanity echoes ----------------------------------------------------------------
echo(str("tub  outer  W x D x H = ", OW, " x ", OD, " x ", case_h, " mm"));
echo(str("lid footprint (skirt) = ", lid_ow, " x ", lid_od, " mm"));
echo(str("overall height   front / back = ", z_plate_top + lid_front_h, " / ",
         z_plate_top + lid_front_h + lid_od*tan(tilt_deg), " mm"));
lcd_clear = (case_h - (z_pican_pcb + pican_pcb[2] + pican_top)) - lcd_back_depth;
echo(str("LCD backpack clearance = ", lcd_clear,
         " mm  (want > 3; raise `headroom` or cut `lcd_back_depth` if <= 0)"));
echo(str("board stack shifted [", stack_dx, ", ", stack_dy,
         "] mm toward the DB9 wall  (set db9_reach from calipers)"));
if (roof_t < lcd_pocket_d + 1.5)
    echo("WARNING: roof_t too thin -- LCD pocket leaves < 1.5 mm floor");

// ============================================================================
//  PRIMITIVE HELPERS
// ============================================================================

// rounded-corner box on Z=0, extending +Z
module rbox(w, d, h, r) {
    linear_extrude(height = h)
        offset(r = r) square([max(w - 2*r, eps), max(d - 2*r, eps)], center = true);
}

// heat-set / self-tap pilot, drilled -Z from z0
module pilot(z0, dia, depth) {
    translate([0, 0, z0 - depth]) cylinder(h = depth + eps, d = dia);
}

// place children flat on the tilted wedge-top plane; local +Z = plane normal
module on_wedge(z_ref = z_wedge_ref) {
    translate([0, 0, z_ref]) rotate([tilt_deg, 0, 0]) children();
}

// solid half-space under the wedge-top plane, plane lowered by `o` in world Z
module under_wedge(o = 0) {
    on_wedge(z_wedge_ref - o)
        translate([-BIG/2, -BIG/2, -BIG]) cube([BIG, BIG, BIG]);
}

// lid-screw features: axis along Y, s = -1 front wall / +1 back wall.
// A cylinder pointing +Z is turned to point INWARD from the wall by rotate([s*90..]).
module base_screw_bosses() {                 // thicken the inner wall for the insert
    for (s = [-1, 1], x = screw_x_pos)
        translate([x, s * (in_y/2 + wall), screw_z_abs]) rotate([s * 90, 0, 0])
            cylinder(h = boss_len + wall, d = boss_d);   // overlaps the wall, then in
}
module base_screw_pilots() {                 // heat-set bore, from the outer face in
    for (s = [-1, 1], x = screw_x_pos)
        translate([x, s * (OD/2 + eps), screw_z_abs]) rotate([s * 90, 0, 0])
            cylinder(h = wall + heatset_l + 2, d = heatset_d);
}
module lid_screw_clear() {                   // clearance through the skirt
    for (s = [-1, 1], x = screw_x_pos)
        translate([x, s * (lid_od/2 + eps), screw_z_abs]) rotate([s * 90, 0, 0])
            cylinder(h = skirt_wall + skirt_gap + 2, d = screw_d);
}

// cutter poking through the named wall, centred, at height z, opening w x h
module wall_cut(edge, z, w, h) {
    if (edge == "left")
        translate([-OW/2 - 1, 0, z]) rotate([0, 90, 0])
            linear_extrude(wall + 2) square([h, w], center = true);
    else if (edge == "right")
        translate([OW/2 + 1, 0, z]) rotate([0, -90, 0])
            linear_extrude(wall + 2) square([h, w], center = true);
    else if (edge == "front")
        translate([0, -OD/2 - 1, z]) rotate([-90, 0, 0])
            linear_extrude(wall + 2) square([w, h], center = true);
    else if (edge == "back")
        translate([0, OD/2 + 1, z]) rotate([90, 0, 0])
            linear_extrude(wall + 2) square([w, h], center = true);
}

// strip of vent slots along the named wall, centred, around height z
module vents(edge, z) {
    span = (vent_count - 1) * vent_pitch;
    for (i = [0 : vent_count - 1]) {
        off = -span/2 + i * vent_pitch;
        if (edge == "left" || edge == "right")
            translate([0, off, 0]) wall_cut(edge, z, vent_slot[0], vent_slot[1]);
        else
            translate([off, 0, 0]) wall_cut(edge, z, vent_slot[0], vent_slot[1]);
    }
}

// Pi mount-hole positions (children rendered at each)
module pi_holes() {
    for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * pi_hole_grid[0]/2, sy * pi_hole_grid[1]/2, 0]) children();
}

// LCD mount-hole positions on the tilted face; children get $lx / $ly.
// The PCB sits centred on the wedge origin (the character window is what's
// offset, by lcd_window_off), so the holes are symmetric about [0,0].
module lcd_holes() {
    for (sx = [-1, 1], sy = [-1, 1])
        let ($lx = sx * lcd_hole_grid[0]/2,
             $ly = sy * lcd_hole_grid[1]/2)
            children();
}

// ============================================================================
//  BASE  (the tub)
// ============================================================================
module base() {
    difference() {
        union() {
            // shell, hollowed, with the wall cuts
            difference() {
                rbox(OW, OD, case_h, r_out);
                translate([0, 0, floor_t]) rbox(in_x, in_y, case_h + BIG, r_in);

                wall_cut(db9_edge, z_db9_axis, db9_win[0], db9_win[1]);         // DB9
                wall_cut("front", floor_t + under_pi + 4, 14, 10);             // micro-USB power
                // microSD access, short edge opposite the DB9
                wall_cut(db9_edge == "left" ? "right" : "left",
                         floor_t + under_pi + 1.5, 14, 4);
                let (vent_z = case_h - skirt_h - vent_slot[1]/2 - 3) {  // clear of the skirt
                    vents("back", vent_z);
                    vents(db9_edge == "left" ? "right" : "left", vent_z);
                }
            }
            // wall bosses for the 4 lid screws (front + back walls, near the
            // X-ends, above the board stack)
            base_screw_bosses();
            // Pi standoffs (stack shoved toward the DB9 wall)
            translate([stack_dx, stack_dy, 0])
                pi_holes() translate([0, 0, floor_t]) cylinder(h = under_pi, d = 6);
        }
        // heat-set bores into the wall bosses, from the outer face
        base_screw_pilots();
        // optional screw-up-through-the-floor pilots for the Pi
        translate([stack_dx, stack_dy, 0])
            pi_holes() translate([0, 0, -eps])
                cylinder(h = floor_t + under_pi + 1, d = pi_screw_d);
    }
}

// ============================================================================
//  LID  (flat plate + hollow LCD/button wedge)
// ============================================================================
module lid() {
    difference() {
        union() {
            // flat plate, full lid footprint, over the tub mouth
            translate([0, 0, case_h]) rbox(lid_ow, lid_od, lid_plate_t, r_out);
            // skirt: laps down over the OUTSIDE of the tub (locates the lid,
            // blocks light and dust, carries the 4 screw holes)
            translate([0, 0, case_h - skirt_h])
                difference() {
                    rbox(lid_ow, lid_od, skirt_h + eps, r_out);
                    translate([0, 0, -eps])
                        rbox(OW + 2*skirt_gap, OD + 2*skirt_gap, skirt_h + 3*eps, r_in);
                }
            // wedge = footprint prism sliced by the tilted top plane
            // (starts eps below the plate top so the union isn't a coincident face)
            intersection() {
                translate([0, 0, z_plate_top - eps]) rbox(lid_ow, lid_od, BIG, r_out);
                under_wedge(0);
            }
            // LCD support bosses -- unioned here so the pocket + screw-clearance
            // cuts below carve them in one pass (no coincident-face union). Top
            // overlaps 1 mm into the pocket; the pocket cut trims it flush.
            lcd_holes() on_wedge() translate([$lx, $ly, -lcd_pocket_d - 8])
                cylinder(h = 8 + 1, d = lcd_screw_d + 3.5);
        }

        // hollow the wedge, keep a `roof_t` roof; interior lines up with the tub
        intersection() {
            translate([0, 0, z_plate_top - eps]) rbox(in_x, in_y, BIG, r_in);
            under_wedge(roof_t);
        }

        // 4 lid screws: clearance through the skirt
        lid_screw_clear();

        // LCD pocket: PCB drops into the tilted face
        on_wedge() translate([0, 0, -lcd_pocket_d])
            linear_extrude(lcd_pocket_d + eps)
            offset(r = 1) square([lcd_pcb[0] + 2*fit_gap - 2,
                                  lcd_pcb[1] + 2*fit_gap - 2], center = true);

        // LCD screw: shank clearance through the roof floor + the fused boss
        // top, then a narrow self-tap pilot down the hanging boss stub
        lcd_holes() on_wedge() translate([$lx, $ly, -roof_t - eps])
            cylinder(h = roof_t - lcd_pocket_d + 2*eps, d = lcd_screw_d + 0.6);
        lcd_holes() on_wedge() translate([$lx, $ly, -lcd_pocket_d - 8 - eps])
            cylinder(h = 8 - (roof_t - lcd_pocket_d) + 2*eps, d = lcd_screw_d - 0.3);

        // character window straight through roof, hollow and plate
        on_wedge() translate([lcd_window_off[0], lcd_window_off[1], -BIG + 1])
            linear_extrude(BIG)
            offset(r = 1.5) square([lcd_window[0] - 3, lcd_window[1] - 3], center = true);

        // optional lens rebate at the surface
        if (lcd_lens)
            on_wedge() translate([lcd_window_off[0], lcd_window_off[1], -lens_t])
                linear_extrude(lens_t + 1)
                offset(r = 1.5) square([lcd_window[0] + 2*lens_margin - 3,
                                        lcd_window[1] + 2*lens_margin - 3], center = true);

        // two panel-button holes + finger dishes, beside the screen
        for (i = [-1, 1])
            let (col = button_side * (lcd_window[0]/2 + button_gap),
                 bx  = (button_stack == "x") ? col + i * button_pitch/2 : col,
                 by  = lcd_window_off[1] + ((button_stack == "x") ? 0 : i * button_pitch/2)) {
                on_wedge() translate([bx, by, -BIG + 1])
                    cylinder(h = BIG, d = button_hole_d);
                on_wedge() translate([bx, by, -button_recess_h])
                    cylinder(h = button_recess_h + 1, d = button_recess_d);
            }

        // weep / pressure-equalising hole out through the wedge back wall
        translate([0, lid_od/2 - wall, z_plate_top + 2]) rotate([-90, 0, 0])
            cylinder(h = 3 * wall, d = 2.5);
    }
}

// ============================================================================
//  DASH FOOT  (optional -- VHB to the dash, case sits/screws on top)
// ============================================================================
module dash_foot() {
    difference() {
        hull() {
            linear_extrude(eps) offset(r = r_out)
                square([OW + 2*foot_pad - 2*r_out, OD + 2*foot_pad - 2*r_out], center = true);
            translate([0, 0, foot_rise]) rotate([0, 0, foot_yaw])
                linear_extrude(eps) offset(r = r_out)
                square([OW - 2*r_out, OD - 2*r_out], center = true);
        }
        hull() {
            translate([0, 0, -eps]) linear_extrude(eps) offset(r = r_out)
                square([OW + 2*foot_pad - 2*foot_wall - 2*r_out,
                        OD + 2*foot_pad - 2*foot_wall - 2*r_out], center = true);
            translate([0, 0, foot_rise - foot_wall]) rotate([0, 0, foot_yaw])
                linear_extrude(eps) offset(r = r_out)
                square([OW - 2*foot_wall - 2*r_out, OD - 2*foot_wall - 2*r_out], center = true);
        }
    }
    // shallow lip on top so the case floor nests and cannot slide off
    translate([0, 0, foot_rise]) rotate([0, 0, foot_yaw])
        difference() {
            rbox(OW + 4, OD + 4, 4, r_out);
            translate([0, 0, -eps]) rbox(OW + 0.8, OD + 0.8, 4 + 2*eps, r_out);
        }
}

// ============================================================================
//  PREVIEW-ONLY MOCKS  (never printed; eyeball fit in `assembly`)
// ============================================================================
module mock_stack() {
    translate([stack_dx, stack_dy, 0]) {
        color("green")  translate([0, 0, z_pi_bottom])
            cube([pi_pcb[0], pi_pcb[1], pi_pcb[2]], center = true);
        color("darkred") translate([0, 0, z_pican_pcb])
            cube([pican_pcb[0], pican_pcb[1], pican_pcb[2]], center = true);
        let (dbx = (db9_edge == "left")  ? -(pican_pcb[0]/2 + 5)
                 : (db9_edge == "right") ?  (pican_pcb[0]/2 + 5) : 0)
            color("silver") translate([dbx, 0, z_db9_axis])
                cube([10, 31, 17], center = true);
    }
}
module mock_lcd() {
    on_wedge() {
        color("navy") translate([0, 0, -lcd_pocket_d + lcd_pcb[2]/2])
            cube([lcd_pcb[0], lcd_pcb[1], lcd_pcb[2]], center = true);
        color("black") translate([lcd_window_off[0], lcd_window_off[1],
                                  -lcd_pocket_d - lcd_back_depth/2])
            cube([60, 40, lcd_back_depth], center = true);
    }
}

// ============================================================================
//  FITCHECK  (thin frame of the mating features -- print this first)
// ============================================================================
module fitcheck() {
    // ~14 mm-thick slab hugging the tilted face: LCD pocket + window + lens
    // rebate + button holes + boss tops, so you can trial-fit the LCD and buttons
    difference() {
        intersection() { lid(); under_wedge(0); }
        under_wedge(14);
    }
    // + a slice of the base carrying the DB9 window
    intersection() {
        base();
        translate([0, 0, z_db9_axis - db9_win[1]/2 - 3])
            rbox(OW + 10, OD + 10, db9_win[1] + 6, r_out);
    }
}

// ============================================================================
//  PART SELECTOR
// ============================================================================
if (part == "base")          base();
else if (part == "lid")      lid();
else if (part == "foot")     dash_foot();
else if (part == "fitcheck") fitcheck();
else if (part == "lens")
    // Design > Export as DXF/SVG; cut flat from `lens_t` mm clear acrylic / PETG.
    // True (un-foreshortened) size -- the rebate in the lid carries the tilt.
    offset(r = 1.5) square([lcd_window[0] + 2*lens_margin - 3,
                            lcd_window[1] + 2*lens_margin - 3], center = true);
else if (part == "plate") {
    translate([-(OW/2 + 8), 0, 0]) base();
    translate([OW/2 + 8, 0, 0]) lid();     // flip screen-down in the slicer
}
else {                                     // "assembly"
    base();
    color("gray", 0.35) lid();
    %mock_stack();
    %mock_lcd();
}
