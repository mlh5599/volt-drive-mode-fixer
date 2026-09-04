"""The daemon's idle LCD watch screen."""

import pytest

from voltdmf import lcdlock
from voltdmf.lcddash import LcdDashboard, bus_label, render_screen
from voltdmf.signals import DriveMode, ShiftPosition
from voltdmf.state import VehicleState


@pytest.fixture(autouse=True)
def _isolated_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(lcdlock, "LOCK_PATH", tmp_path / "lcd.lock")


def _state(mode=DriveMode.MOUNTAIN, gear=ShiftPosition.DRIVE, active=True,
           soc=None):
    st = VehicleState()
    st.drive_mode = mode
    st.shift = gear
    st.soc_percent = soc
    if active:
        st.mark_signal_seen()
    return st


def _selector(label="hold-soc", index=1, floor_latched=False, soc_fresh=True,
             flashing=False):
    return {"label": label, "index": index, "floor_latched": floor_latched,
            "soc_fresh": soc_fresh, "flashing": flashing}


# -- bus_label --------------------------------------------------------------
def test_bus_label_active_spins_every_tick():
    frames = {bus_label("ACTIVE", t) for t in range(4)}
    assert len(frames) == 4  # a distinct frame per tick -- a visible heartbeat
    assert all(f.startswith("ACTIVE") for f in frames)


def test_bus_label_fault_blinks():
    on = bus_label("BUS-OFF", 0)
    off = bus_label("BUS-OFF", 1)
    assert on != off
    assert off == ""


def test_bus_label_quiet_is_static():
    assert bus_label("QUIET", 0) == bus_label("QUIET", 1) == "QUIET"


def test_bus_label_spells_out_the_full_word():
    assert bus_label("WARNING", 0) == "WARNING"


# -- render_screen --------------------------------------------------------
def test_render_shows_bus_selector_mode_gear_and_soc():
    rows = render_screen(_state(soc=84.3), bus_label="ACTIVE-",
                         position_label="hold-soc", position_index=1,
                         cycle_len=4, floor_latched=False, soc_fresh=True)
    assert rows[0] == "ACTIVE-"
    assert rows[1] == "sel 1/4 HOLD-SOC"
    assert "MOUNTAIN" in rows[2]
    assert "gear D" in rows[2]
    assert "DM" in rows[2]
    assert "84.3%" in rows[3]
    assert all(len(r) <= 20 for r in rows)


def test_render_shows_floor_latched_flag():
    rows = render_screen(_state(soc=25.0), bus_label="ACTIVE-",
                         position_label="hold-now", position_index=2,
                         cycle_len=4, floor_latched=True, soc_fresh=True)
    assert rows[1] == "sel 2/4 HOLD-NOW FLR"


def test_render_flashes_the_new_setting_instead_of_the_sel_line():
    rows = render_screen(_state(), bus_label="ACTIVE-", position_label="mountain",
                         position_index=3, cycle_len=4, floor_latched=False,
                         soc_fresh=True, flashing=True)
    assert rows[1] == "-> MOUNTAIN"


def test_render_flash_ignores_the_floor_flag():
    """The flash is about the setting the driver just picked, not the SOC
    floor -- that only matters once the row settles back to steady state."""
    rows = render_screen(_state(), bus_label="ACTIVE-", position_label="hold-now",
                         position_index=2, cycle_len=4, floor_latched=True,
                         soc_fresh=True, flashing=True)
    assert rows[1] == "-> HOLD-NOW"


def test_render_marks_a_stale_soc_reading():
    """A live poll reading gets no mark; a stale one (or a bar-failsafe
    reading, same flag) does -- the number stays up, but is not to be
    trusted the same way."""
    fresh = render_screen(_state(soc=42.0), bus_label="ACTIVE-",
                          position_label="hold-soc", position_index=1,
                          cycle_len=4, floor_latched=False, soc_fresh=True)
    stale = render_screen(_state(soc=42.0), bus_label="ACTIVE-",
                          position_label="hold-soc", position_index=1,
                          cycle_len=4, floor_latched=False, soc_fresh=False)
    assert "~" not in fresh[3]
    assert stale[3].rstrip().endswith("~")


def test_render_does_not_mark_a_missing_soc_reading():
    """No reading at all shows "--", not a stale-looking "--~ "."""
    rows = render_screen(_state(soc=None), bus_label="QUIET",
                         position_label="off", position_index=4, cycle_len=4,
                         floor_latched=False, soc_fresh=False)
    assert "~" not in rows[3]


def test_render_shows_dashes_when_soc_unknown():
    rows = render_screen(_state(soc=None), bus_label="QUIET",
                         position_label="off", position_index=4, cycle_len=4,
                         floor_latched=False, soc_fresh=False)
    assert "--" in rows[3]


def test_render_handles_unknown_mode_and_gear():
    rows = render_screen(_state(mode=None, gear=ShiftPosition.UNKNOWN),
                         bus_label="QUIET", position_label="off",
                         position_index=4, cycle_len=4, floor_latched=False,
                         soc_fresh=False)
    assert "no 1F4" in rows[2]
    assert "gear ?" in rows[2]


def test_render_clips_a_long_bus_label():
    rows = render_screen(_state(), bus_label="x" * 40, position_label="hold-soc",
                         position_index=1, cycle_len=4, floor_latched=False,
                         soc_fresh=True)
    assert rows[0] == "x" * 20


# -- LcdDashboard._tick (drives a dry-run SerLcd, no serial port) --------
def test_dashboard_drives_a_real_panel_by_default():
    # regression: a disarmed daemon does no CAN TX, but the watch screen must
    # still write to /dev/serial0, not just an in-memory image. LcdDashboard's
    # own dry_run (the LCD serial port) is independent of daemon arm state.
    dash = LcdDashboard(_state())
    assert dash._lcd_kwargs["dry_run"] is False


def test_tick_paints_the_watch_screen():
    dash = LcdDashboard(_state(), selector_fn=lambda: _selector(),
                        dry_run=True)
    dash._tick(0)
    img = dash._lcd.snapshot()
    assert "MOUNTAIN" in img[2]
    assert "84.3" not in img[3]  # this state has no soc set -> "--"
    assert "--" in img[3]


def test_tick_uses_a_placeholder_selector_when_none_supplied():
    dash = LcdDashboard(_state(), dry_run=True)
    dash._tick(0)  # must not raise even with no selector_fn given
    assert dash._lcd is not None


def test_tick_releases_panel_when_another_process_holds_it():
    dash = LcdDashboard(_state(), dry_run=True)
    dash._tick(0)
    assert dash._lcd is not None

    lcdlock.LOCK_PATH.write_text("1: soc_log.py")  # pid 1 -> a live "other"
    dash._tick(1)
    assert dash._lcd is None
    assert dash._yielded is True

    lcdlock.LOCK_PATH.unlink()
    dash._tick(2)
    assert dash._lcd is not None
    assert dash._yielded is False


def test_selector_fn_exceptions_do_not_escape_the_run_loop():
    dash = LcdDashboard(_state(), interval=0.0, dry_run=True)

    def boom():
        dash._stop.set()  # let the loop exit after this pass
        raise RuntimeError("selector blew up")

    dash._selector_fn = boom
    dash._run()  # the tick raises; _run must swallow it and return
    assert dash._lcd is None  # closed on the way out of the failed tick
