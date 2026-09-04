import time

import pytest

from voltdmf.reconciler import CYCLE, Position, Reconciler, build_reconciler
from voltdmf.config import parse_config
from voltdmf.signals import DriveMode
from voltdmf.state import VehicleState


def _rec(**over) -> Reconciler:
    kw = dict(
        hold_threshold_percent=30.0,
        hold_reset_percent=41.0,
        bar_failsafe_raw=9,
        default_position=Position.HOLD,
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


def test_default_position_must_be_a_detent():
    with pytest.raises(ValueError):
        _rec(default_position=DriveMode.NORMAL)
    with pytest.raises(ValueError):
        _rec(default_position="sport")


def test_boots_on_hold():
    """The shipped default: position 1, passive, floor live."""
    rec = _rec()
    assert rec.position is Position.HOLD
    assert rec.setpoint_label == "hold"


# -- the SW1 cycle -------------------------------------------------------

def test_cycle_order_is_hold_mountain_off():
    assert CYCLE == (Position.HOLD, Position.MOUNTAIN, Position.OFF)


def test_advance_walks_the_cycle_and_wraps():
    rec = _rec()
    assert rec.advance() is Position.MOUNTAIN
    assert rec.advance() is Position.OFF
    assert rec.advance() is Position.HOLD      # the fourth tap starts over
    assert rec.position is Position.HOLD


def test_advance_wraps_from_any_starting_detent():
    rec = _rec(default_position=Position.OFF)
    assert rec.advance() is Position.HOLD


def test_set_position_jumps_straight_to_a_detent():
    rec = _rec()
    rec.set_position(Position.OFF)
    assert rec.position is Position.OFF
    assert rec.setpoint_label == "off"


def test_set_position_rejects_a_non_detent():
    rec = _rec()
    with pytest.raises(ValueError):
        rec.set_position(DriveMode.SPORT)
    assert rec.position is Position.HOLD


# -- position 1: hold (passive, floor live) ------------------------------

def test_hold_position_has_no_target_on_a_healthy_pack():
    rec = _rec()
    assert rec.desired_mode(_state(pct=80)) is None
    assert rec.snapshot()["setpoint"] == "hold"


def test_hold_position_yields_to_the_soc_floor():
    rec = _rec()
    assert rec.desired_mode(_state(pct=29)) is DriveMode.HOLD
    assert rec.floor_latched is True
    # floor releases -> back to no target, not to a standing HOLD
    assert rec.desired_mode(_state(pct=45)) is None


def test_hold_position_falls_back_to_bar_failsafe_when_poll_stale():
    rec = _rec()
    assert rec.desired_mode(_state(bar=9)) is DriveMode.HOLD


# -- position 2: mountain ------------------------------------------------

def test_mountain_position_enforces_from_the_first_pass():
    rec = _rec()
    assert rec.desired_mode(_state(pct=80)) is None
    rec.advance()
    assert rec.position is Position.MOUNTAIN
    assert rec.desired_mode(_state(pct=80)) is DriveMode.MOUNTAIN


def test_healthy_pack_leaves_mountain_untouched():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state(pct=80)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


# -- position 3: off -----------------------------------------------------

def test_off_never_has_a_target():
    rec = _rec(default_position=Position.OFF)
    assert rec.desired_mode(_state(pct=80)) is None
    assert rec.desired_mode(_state(pct=5)) is None
    assert rec.desired_mode(_state(bar=0)) is None


def test_off_does_not_even_evaluate_the_floor():
    """A pack under the threshold must not leave a latch behind in OFF."""
    rec = _rec(default_position=Position.OFF)
    rec.desired_mode(_state(pct=5))
    assert rec.floor_latched is False
    assert rec.snapshot()["floor_source"] is None


def test_entering_off_clears_a_latched_floor():
    rec = _rec()
    assert rec.desired_mode(_state(pct=20)) is DriveMode.HOLD
    assert rec.floor_latched is True
    rec.advance()                                # -> mountain, still latched
    assert rec.floor_latched is True
    rec.advance()                                # -> off
    assert rec.floor_latched is False
    # and coming back round does not resurrect it on a stale reading
    rec.advance()                                # -> hold
    assert rec.desired_mode(_state()) is None


# -- the SOC-HOLD floor (poll path) ----------------------------------

def test_floor_engages_at_threshold_and_overrides_mountain():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state(pct=50)) is DriveMode.MOUNTAIN
    assert rec.desired_mode(_state(pct=30)) is DriveMode.HOLD
    assert rec.floor_latched is True


def test_floor_hysteresis_holds_between_thresholds():
    rec = _rec(default_position=Position.MOUNTAIN)
    rec.desired_mode(_state(pct=29))            # engage
    # 38% is above the engage line but below the release line -> still latched
    assert rec.desired_mode(_state(pct=38)) is DriveMode.HOLD
    # back above the reset line -> releases
    assert rec.desired_mode(_state(pct=41)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


# -- failsafe (0x096 b3) when the poll is stale ---------------------

def test_bar_failsafe_engages_when_poll_stale():
    rec = _rec(default_position=Position.MOUNTAIN)
    # no fresh poll; b3 at the failsafe count
    st = _state(bar=9)
    assert rec.desired_mode(st) is DriveMode.HOLD
    assert rec.floor_latched is True


def test_bar_failsafe_ignored_while_poll_is_fresh():
    rec = _rec(default_position=Position.MOUNTAIN)
    # fresh poll says healthy, b3 says low -> trust the poll
    st = _state(pct=70, bar=9)
    assert rec.desired_mode(st) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


def test_stale_poll_falls_back_to_failsafe():
    rec = _rec(default_position=Position.MOUNTAIN)
    st = _state(pct=70, pct_age_s=120.0, bar=9)  # poll too old
    assert rec.desired_mode(st) is DriveMode.HOLD


def test_bar_failsafe_does_not_release_on_bar_alone():
    rec = _rec(default_position=Position.MOUNTAIN)
    rec.desired_mode(_state(bar=9))              # engage off the bar
    assert rec.floor_latched is True
    # bar recovers but there is still no fresh poll -> stays latched (safe)
    assert rec.desired_mode(_state(bar=13)) is DriveMode.HOLD
    # a real poll reading above the reset line is what clears it
    assert rec.desired_mode(_state(pct=45)) is DriveMode.MOUNTAIN


def test_no_soc_info_leaves_latch_unchanged():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state()) is DriveMode.MOUNTAIN
    rec.desired_mode(_state(pct=20))            # latch on
    assert rec.desired_mode(_state()) is DriveMode.HOLD  # unchanged, still latched


# -- build from config -----------------------------------------------

def test_build_from_config():
    cfg = parse_config({
        "policy": {"default_position": "mountain", "hold_threshold_percent": 30,
                   "hold_reset_percent": 42, "bar_failsafe_raw": 8},
        "soc_poll": {"enabled": True, "period_seconds": 10},
    })
    rec = build_reconciler(cfg)
    assert rec.position is Position.MOUNTAIN
    assert rec.desired_mode(_state(pct=50)) is DriveMode.MOUNTAIN


def test_build_from_config_off():
    cfg = parse_config({
        "policy": {"default_position": "off", "hold_threshold_percent": 30,
                   "hold_reset_percent": 41, "bar_failsafe_raw": 9},
        "soc_poll": {"enabled": True, "period_seconds": 10},
    })
    rec = build_reconciler(cfg)
    assert rec.position is Position.OFF
    assert rec.desired_mode(_state(pct=5)) is None


def test_build_from_config_accepts_the_legacy_auto_name():
    """`auto` was the old name for what position 1 does; keep it working."""
    cfg = parse_config({
        "policy": {"default_setpoint": "auto", "hold_threshold_percent": 30,
                   "hold_reset_percent": 41, "bar_failsafe_raw": 9},
        "soc_poll": {"enabled": True, "period_seconds": 10},
    })
    rec = build_reconciler(cfg)
    assert rec.position is Position.HOLD
    assert rec.desired_mode(_state(pct=50)) is None


# -- snapshot --------------------------------------------------------

def test_snapshot_shape():
    rec = _rec()
    rec.desired_mode(_state(pct=20))
    snap = rec.snapshot()
    assert snap["setpoint"] == "hold"
    assert snap["position"] == "hold"
    assert snap["cycle"] == ["hold", "mountain", "off"]
    assert snap["floor_latched"] is True
    assert snap["floor_source"] == "poll"
    assert snap["hold_threshold_percent"] == 30.0
