import pytest

from voltdmf.config import parse_config
from voltdmf.signals import DriveMode
from voltdmf.state import VehicleState
from voltdmf.triggers import (
    OnStartTrigger,
    SocThresholdTrigger,
    build_triggers,
)


def _active_state(**kw) -> VehicleState:
    st = VehicleState(**kw)
    st.mark_signal_seen()  # makes bus_active True
    return st


# --- OnStartTrigger ---------------------------------------------------------
def test_on_start_waits_for_bus():
    trig = OnStartTrigger(DriveMode.MOUNTAIN)
    assert trig.evaluate(VehicleState()) is None  # bus not active yet


def test_on_start_fires_once():
    trig = OnStartTrigger(DriveMode.MOUNTAIN)
    st = _active_state()
    assert trig.evaluate(st) is DriveMode.MOUNTAIN
    assert trig.evaluate(st) is None
    assert trig.evaluate(st) is None


# --- SocThresholdTrigger -------------------------------------------------
def test_soc_trigger_noop_without_soc():
    trig = SocThresholdTrigger(DriveMode.HOLD, 25, 40)
    assert trig.evaluate(_active_state()) is None


def test_soc_trigger_fires_on_cross_and_latches():
    trig = SocThresholdTrigger(DriveMode.HOLD, 25, 40)
    assert trig.evaluate(_active_state(soc_percent=30)) is None
    assert trig.evaluate(_active_state(soc_percent=25)) is DriveMode.HOLD
    # still low -> latched, must not re-fire
    assert trig.evaluate(_active_state(soc_percent=10)) is None
    assert trig.evaluate(_active_state(soc_percent=24)) is None


def test_soc_trigger_rearms_after_reset():
    trig = SocThresholdTrigger(DriveMode.HOLD, 25, 40)
    trig.evaluate(_active_state(soc_percent=20))          # fire + latch
    assert trig.evaluate(_active_state(soc_percent=41)) is None   # re-arm only
    assert trig.evaluate(_active_state(soc_percent=20)) is DriveMode.HOLD


def test_soc_trigger_rejects_bad_bounds():
    with pytest.raises(ValueError):
        SocThresholdTrigger(DriveMode.HOLD, 40, 25)


# --- build_triggers ----------------------------------------------------
def test_build_triggers_respects_enabled_flags():
    raw = {
        "on_start": {"enabled": False, "target_mode": "mountain"},
        "soc_threshold": {
            "enabled": True, "target_mode": "hold",
            "threshold_percent": 20, "reset_percent": 35,
        },
        "trip_mode": {"enabled": False},
    }
    triggers = build_triggers(parse_config(raw))
    assert [t.name for t in triggers] == ["soc_threshold"]
