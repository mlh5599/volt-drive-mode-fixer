"""The daemon's idle LCD watch screen."""

import pytest

from voltdmf import lcdlock
from voltdmf.lcddash import LcdDashboard, _fmt_uptime, render_screen
from voltdmf.signals import DriveMode, ShiftPosition
from voltdmf.state import VehicleState


@pytest.fixture(autouse=True)
def _isolated_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(lcdlock, "LOCK_PATH", tmp_path / "lcd.lock")


def _state(mode=DriveMode.MOUNTAIN, gear=ShiftPosition.DRIVE, active=True):
    st = VehicleState()
    st.drive_mode = mode
    st.shift = gear
    if active:
        st.mark_signal_seen()
    return st


# -- render_screen --------------------------------------------------------
def test_render_shows_mode_gear_and_status():
    rows = render_screen(_state(), bus_tag="ACTIVE", status="arm MOUNTAIN @start",
                         clock="14:23:07", uptime="2h03")
    assert rows[0] == "DMF   2h03 14:23:07"
    assert "MOUNTAIN" in rows[1]
    assert rows[2] == "gear D    bus ACTIVE"
    assert rows[3] == "arm MOUNTAIN @start"
    assert all(len(r) <= 20 for r in rows)


def test_render_handles_unknown_mode_and_gear():
    rows = render_screen(_state(mode=None, gear=ShiftPosition.UNKNOWN),
                         bus_tag="QUIET", status="idle", clock="00:00:00",
                         uptime="0s")
    assert "no 1F4" in rows[1]
    assert rows[2].startswith("gear ?")


def test_render_clips_a_long_status_line():
    rows = render_screen(_state(), bus_tag="ACTIVE",
                         status="x" * 40, clock="14:23:07", uptime="9s")
    assert rows[3] == "x" * 20


@pytest.mark.parametrize("secs, want", [
    (0, "0s"), (9, "9s"), (59, "59s"), (60, "1m"), (3599, "59m"),
    (3600, "1h00"), (2 * 3600 + 180, "2h03"),
])
def test_fmt_uptime(secs, want):
    assert _fmt_uptime(secs) == want


# -- LcdDashboard._tick (drives a dry-run SerLcd, no serial port) --------
def test_dashboard_drives_a_real_panel_by_default():
    # regression: the daemon runs --dry-run on the Pi (no CAN TX) but the
    # watch screen must still write to /dev/serial0, not just an in-memory
    # image. LcdDashboard must not inherit the daemon's dry_run.
    dash = LcdDashboard(_state(), lambda: "idle")
    assert dash._lcd_kwargs["dry_run"] is False


def test_tick_paints_the_watch_screen():
    dash = LcdDashboard(_state(), lambda: "arm MOUNTAIN @start", dry_run=True)
    dash._tick(0)
    img = dash._lcd.snapshot()
    assert "MOUNTAIN" in img[1]
    assert img[3].startswith("arm MOUNTAIN @start")


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
