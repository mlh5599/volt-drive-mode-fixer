"""The daemon's idle LCD watch screen."""

import pytest

from voltdmf import lcdlock
from voltdmf.lcddash import LcdDashboard, bus_icon, render_screen
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


def _selector(label="hold-soc", index=1, floor_latched=False, soc_fresh=True):
    return {"label": label, "index": index, "floor_latched": floor_latched,
            "soc_fresh": soc_fresh}


# -- bus_icon --------------------------------------------------------------
def test_bus_icon_active_spins_every_tick():
    frames = {bus_icon("ACTIVE", t) for t in range(4)}
    assert len(frames) == 4  # a distinct glyph per tick -- a visible heartbeat
    assert all(f.startswith("C") for f in frames)


def test_bus_icon_fault_blinks():
    on = bus_icon("BUSOFF", 0)
    off = bus_icon("BUSOFF", 1)
    assert on != off
    assert off.strip() == ""


def test_bus_icon_quiet_is_static():
    assert bus_icon("QUIET", 0) == bus_icon("QUIET", 1)


# -- render_screen --------------------------------------------------------
def test_render_shows_soc_selector_mode_gear_and_status():
    rows = render_screen(_state(soc=84.3), bus_icon="C-",
                         position_label="hold-soc", position_index=1,
                         cycle_len=4, floor_latched=False, soc_fresh=True,
                         status="arm MOUNTAIN @start")
    assert "84.3%" in rows[0]
    assert "C-" in rows[0]
    assert rows[1] == "sel 1/4 HOLD-SOC"
    assert "MOUNTAIN" in rows[2]
    assert "gear D" in rows[2]
    assert rows[3] == "arm MOUNTAIN @start"
    assert all(len(r) <= 20 for r in rows)


def test_render_shows_floor_latched_flag():
    rows = render_screen(_state(soc=25.0), bus_icon="C-",
                         position_label="hold-now", position_index=2,
                         cycle_len=4, floor_latched=True, soc_fresh=True,
                         status="idle")
    assert rows[1] == "sel 2/4 HOLD-NOW FLR"


def test_render_marks_a_stale_soc_reading():
    """A live poll reading gets no mark; a stale one (or a bar-failsafe
    reading, same flag) does -- the number stays up, but is not to be
    trusted the same way."""
    fresh = render_screen(_state(soc=42.0), bus_icon="C-",
                          position_label="hold-soc", position_index=1,
                          cycle_len=4, floor_latched=False, soc_fresh=True,
                          status="idle")
    stale = render_screen(_state(soc=42.0), bus_icon="C-",
                          position_label="hold-soc", position_index=1,
                          cycle_len=4, floor_latched=False, soc_fresh=False,
                          status="idle")
    assert "~" not in fresh[0]
    assert stale[0].rstrip().endswith("~")


def test_render_does_not_mark_a_missing_soc_reading():
    """No reading at all shows "--", not a stale-looking "--~ "."""
    rows = render_screen(_state(soc=None), bus_icon="Qz",
                         position_label="off", position_index=4, cycle_len=4,
                         floor_latched=False, soc_fresh=False, status="idle")
    assert "~" not in rows[0]


def test_render_shows_dashes_when_soc_unknown():
    rows = render_screen(_state(soc=None), bus_icon="Qz",
                         position_label="off", position_index=4, cycle_len=4,
                         floor_latched=False, soc_fresh=False, status="idle")
    assert "--" in rows[0]


def test_render_handles_unknown_mode_and_gear():
    rows = render_screen(_state(mode=None, gear=ShiftPosition.UNKNOWN),
                         bus_icon="Qz", position_label="off",
                         position_index=4, cycle_len=4, floor_latched=False,
                         soc_fresh=False, status="idle")
    assert "no 1F4" in rows[2]
    assert "gear ?" in rows[2]


def test_render_clips_a_long_status_line():
    rows = render_screen(_state(), bus_icon="C-", position_label="hold-soc",
                         position_index=1, cycle_len=4, floor_latched=False,
                         soc_fresh=True, status="x" * 40)
    assert rows[3] == "x" * 20


# -- LcdDashboard._tick (drives a dry-run SerLcd, no serial port) --------
def test_dashboard_drives_a_real_panel_by_default():
    # regression: a disarmed daemon does no CAN TX, but the watch screen must
    # still write to /dev/serial0, not just an in-memory image. LcdDashboard's
    # own dry_run (the LCD serial port) is independent of daemon arm state.
    dash = LcdDashboard(_state(), lambda: "idle")
    assert dash._lcd_kwargs["dry_run"] is False


def test_tick_paints_the_watch_screen():
    dash = LcdDashboard(_state(), lambda: "arm MOUNTAIN @start",
                        selector_fn=lambda: _selector(), dry_run=True)
    dash._tick(0)
    img = dash._lcd.snapshot()
    assert "MOUNTAIN" in img[2]
    assert img[3].startswith("arm MOUNTAIN @start")


def test_tick_uses_a_placeholder_selector_when_none_supplied():
    dash = LcdDashboard(_state(), lambda: "idle", dry_run=True)
    dash._tick(0)  # must not raise even with no selector_fn given
    assert dash._lcd is not None


def test_tick_releases_panel_when_another_process_holds_it():
    dash = LcdDashboard(_state(), lambda: "arm MOUNTAIN @start", dry_run=True)
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


def test_status_fn_exceptions_do_not_escape_the_run_loop():
    dash = LcdDashboard(_state(), None, interval=0.0, dry_run=True)

    def boom() -> str:
        dash._stop.set()  # let the loop exit after this pass
        raise RuntimeError("status blew up")

    dash._status_fn = boom
    dash._run()  # the tick raises; _run must swallow it and return
    assert dash._lcd is None  # closed on the way out of the failed tick
