import time

import pytest

from voltdmf.reconciler import Reconciler, build_reconciler
from voltdmf.config import parse_config
from voltdmf.signals import DriveMode
from voltdmf.state import VehicleState


def _rec(**over) -> Reconciler:
    kw = dict(
        hold_threshold_percent=33.0,
        hold_reset_percent=41.0,
        bar_failsafe_raw=9,
        default_setpoint=DriveMode.HOLD,
        poll_stale_s=45.0,
    )
    kw.update(over)
    return Reconciler(**kw)


def _state(*, pct=None, pct_age_s=0.0, bar=None) -> VehicleState:
    st = VehicleState()
    if pct is not None:
        st.soc_percent = pct
        st.soc_raw = round(pct * 255 / 100)
        st.soc_percent_monotonic = time.monotonic() - pct_age_s
        st.soc_source = "poll"
    st.soc_bar_raw = bar
    st.mark_signal_seen()
    return st


# -- construction --------------------------------------------------------

def test_reset_must_exceed_threshold():
    with pytest.raises(ValueError):
        _rec(hold_threshold_percent=40, hold_reset_percent=40)


def test_default_setpoint_must_be_hold_mountain_or_none():
    with pytest.raises(ValueError):
        _rec(default_setpoint=DriveMode.NORMAL)
    with pytest.raises(ValueError):
        _rec(default_setpoint=DriveMode.SPORT)


# -- passive (auto) default -------------------------------------------

def test_passive_default_has_no_target_on_a_healthy_pack():
    rec = _rec(default_setpoint=None)
    assert rec.setpoint is None
    assert rec.setpoint_label == "auto"
    assert rec.desired_mode(_state(pct=80)) is None
    assert rec.snapshot()["setpoint"] == "auto"


def test_passive_default_still_yields_to_the_soc_floor():
    rec = _rec(default_setpoint=None)
    assert rec.desired_mode(_state(pct=30)) is DriveMode.HOLD
    assert rec.floor_latched is True
    # floor releases -> back to no target, not to HOLD
    assert rec.desired_mode(_state(pct=45)) is None


def test_passive_default_falls_back_to_bar_failsafe_when_poll_stale():
    rec = _rec(default_setpoint=None)
    assert rec.desired_mode(_state(bar=9)) is DriveMode.HOLD


def test_driver_selection_activates_enforcement_from_passive():
    rec = _rec(default_setpoint=None)
    assert rec.desired_mode(_state(pct=80)) is None
    rec.set_setpoint(DriveMode.MOUNTAIN)
    assert rec.desired_mode(_state(pct=80)) is DriveMode.MOUNTAIN


def test_build_from_config():
    cfg = parse_config({
        "policy": {"default_setpoint": "mountain", "hold_threshold_percent": 30,
                   "hold_reset_percent": 42, "bar_failsafe_raw": 8},
        "soc_poll": {"enabled": True, "period_seconds": 10},
    })
    rec = build_reconciler(cfg)
    assert rec.setpoint is DriveMode.MOUNTAIN
    assert rec.desired_mode(_state(pct=50)) is DriveMode.MOUNTAIN


def test_build_from_config_auto():
    cfg = parse_config({
        "policy": {"default_setpoint": "auto", "hold_threshold_percent": 33,
                   "hold_reset_percent": 41, "bar_failsafe_raw": 9},
        "soc_poll": {"enabled": True, "period_seconds": 10},
    })
    rec = build_reconciler(cfg)
    assert rec.setpoint is None
    assert rec.desired_mode(_state(pct=50)) is None


# -- setpoint toggle ---------------------------------------------------

def test_setpoint_toggle():
    rec = _rec()
    assert rec.setpoint is DriveMode.HOLD
    rec.set_setpoint(DriveMode.MOUNTAIN)
    assert rec.setpoint is DriveMode.MOUNTAIN
    assert rec.desired_mode(_state(pct=60)) is DriveMode.MOUNTAIN


@pytest.mark.parametrize("bad", [DriveMode.NORMAL, DriveMode.SPORT])
def test_setpoint_rejects_non_setpoint_modes(bad):
    rec = _rec()
    with pytest.raises(ValueError):
        rec.set_setpoint(bad)
    assert rec.setpoint is DriveMode.HOLD


# -- the SOC-HOLD floor (poll path) ----------------------------------

def test_floor_engages_at_threshold_and_overrides_mountain_setpoint():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    assert rec.desired_mode(_state(pct=50)) is DriveMode.MOUNTAIN
    assert rec.desired_mode(_state(pct=33)) is DriveMode.HOLD
    assert rec.floor_latched is True


def test_floor_hysteresis_holds_between_thresholds():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    rec.desired_mode(_state(pct=32))            # engage
    # 38% is above the engage line but below the release line -> still latched
    assert rec.desired_mode(_state(pct=38)) is DriveMode.HOLD
    # back above the reset line -> releases
    assert rec.desired_mode(_state(pct=41)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


def test_healthy_pack_leaves_setpoint_untouched():
    rec = _rec()
    rec.set_setpoint(DriveMode.MOUNTAIN)
    assert rec.desired_mode(_state(pct=80)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


# -- failsafe (0x096 b3) when the poll is stale ---------------------

def test_bar_failsafe_engages_when_poll_stale():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    # no fresh poll; b3 at the failsafe count
    st = _state(bar=9)
    assert rec.desired_mode(st) is DriveMode.HOLD
    assert rec.floor_latched is True


def test_bar_failsafe_ignored_while_poll_is_fresh():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    # fresh poll says healthy, b3 says low -> trust the poll
    st = _state(pct=70, bar=9)
    assert rec.desired_mode(st) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


def test_stale_poll_falls_back_to_failsafe():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    st = _state(pct=70, pct_age_s=120.0, bar=9)  # poll too old
    assert rec.desired_mode(st) is DriveMode.HOLD


def test_bar_failsafe_does_not_release_on_bar_alone():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    rec.desired_mode(_state(bar=9))              # engage off the bar
    assert rec.floor_latched is True
    # bar recovers but there is still no fresh poll -> stays latched (safe)
    assert rec.desired_mode(_state(bar=13)) is DriveMode.HOLD
    # a real poll reading above the reset line is what clears it
    assert rec.desired_mode(_state(pct=45)) is DriveMode.MOUNTAIN


def test_no_soc_info_leaves_latch_unchanged():
    rec = _rec(default_setpoint=DriveMode.MOUNTAIN)
    assert rec.desired_mode(_state()) is DriveMode.MOUNTAIN
    rec.desired_mode(_state(pct=20))            # latch on
    assert rec.desired_mode(_state()) is DriveMode.HOLD  # unchanged, still latched


# -- snapshot --------------------------------------------------------

def test_snapshot_shape():
    rec = _rec()
    rec.desired_mode(_state(pct=20))
    snap = rec.snapshot()
    assert snap["setpoint"] == "hold"
    assert snap["floor_latched"] is True
    assert snap["floor_source"] == "poll"
    assert snap["hold_threshold_percent"] == 33.0
