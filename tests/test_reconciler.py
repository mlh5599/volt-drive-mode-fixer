import time

import pytest

from voltdmf.reconciler import (CYCLE, Position, Reconciler, build_reconciler,
                                resolve_position)
from voltdmf.config import parse_config
from voltdmf.signals import DriveMode
from voltdmf.state import VehicleState

#: Older than the reconciler's poll_stale_s, so the b3 failsafe is live.
_WARMED_UP = 1000.0


def _rec(**over) -> Reconciler:
    """A reconciler past its startup grace, so the b3 failsafe can act.

    Tests that care about the grace itself pass their own ``monotonic``.
    """
    kw = dict(
        hold_threshold_percent=30.0,
        bar_failsafe_raw=9,
        default_position=Position.HOLD_SOC,
        poll_stale_s=45.0,
        monotonic=_clock(start=0.0, now=_WARMED_UP),
    )
    kw.update(over)
    return Reconciler(**kw)


def _clock(*, start: float, now: float):
    """A monotonic that reads `start` on the first call, then `now` forever.

    The first call is the one __init__ makes to stamp the startup time.
    """
    calls = {"n": 0}

    def _now() -> float:
        calls["n"] += 1
        return start if calls["n"] == 1 else now
    return _now


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

def test_threshold_must_be_a_percentage():
    with pytest.raises(ValueError):
        _rec(hold_threshold_percent=140)


def test_default_position_must_be_a_detent():
    with pytest.raises(ValueError):
        _rec(default_position=DriveMode.NORMAL)
    with pytest.raises(ValueError):
        _rec(default_position="sport")


def test_boots_on_hold_soc():
    """The shipped default: detent 1, passive, floor live."""
    rec = _rec()
    assert rec.position is Position.HOLD_SOC
    assert rec.setpoint_label == "hold-soc"
    assert rec.position_index == 1
    assert rec.describe_position() == "hold the pack at 30%"


# -- the SW1 cycle -------------------------------------------------------

def test_cycle_order():
    assert CYCLE == (Position.HOLD_SOC, Position.HOLD_NOW,
                     Position.MOUNTAIN, Position.OFF)


def test_advance_walks_the_cycle_and_wraps():
    rec = _rec()
    assert rec.advance() is Position.HOLD_NOW
    assert rec.advance() is Position.MOUNTAIN
    assert rec.advance() is Position.OFF
    assert rec.advance() is Position.HOLD_SOC    # the fifth tap starts over
    assert rec.position_index == 1


def test_advance_wraps_from_any_starting_detent():
    rec = _rec(default_position=Position.OFF)
    assert rec.advance() is Position.HOLD_SOC


def test_set_position_jumps_straight_to_a_detent():
    rec = _rec()
    rec.set_position(Position.MOUNTAIN)
    assert rec.position is Position.MOUNTAIN
    assert rec.position_index == 3


def test_set_position_rejects_a_non_detent():
    rec = _rec()
    with pytest.raises(ValueError):
        rec.set_position(DriveMode.SPORT)
    assert rec.position is Position.HOLD_SOC


@pytest.mark.parametrize("name,want", [
    ("hold-soc", Position.HOLD_SOC),
    ("HOLD-NOW", Position.HOLD_NOW),
    (" off ", Position.OFF),
    ("auto", Position.HOLD_SOC),     # two-way-toggle era
    ("hold", Position.HOLD_SOC),     # three-position era
])
def test_resolve_position_accepts_current_and_legacy_names(name, want):
    assert resolve_position(name) is want


def test_resolve_position_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        resolve_position("ludicrous")


# -- detent 1: hold-soc (passive, then latch at the threshold) -----------

def test_hold_soc_has_no_target_on_a_healthy_pack():
    rec = _rec()
    assert rec.desired_mode(_state(pct=80)) is None


def test_hold_soc_forces_hold_at_the_threshold():
    rec = _rec()
    assert rec.desired_mode(_state(pct=30)) is DriveMode.HOLD
    assert rec.floor_latched is True


def test_hold_soc_latch_never_releases_within_the_key_cycle():
    """"Drain to 30%, then hold, and do not let it change."."""
    rec = _rec()
    rec.desired_mode(_state(pct=29))                     # engage
    assert rec.floor_latched is True
    # a long descent puts charge back in -- the floor does NOT release
    assert rec.desired_mode(_state(pct=45)) is DriveMode.HOLD
    assert rec.desired_mode(_state(pct=90)) is DriveMode.HOLD
    assert rec.floor_latched is True


def test_hold_soc_latch_survives_a_walk_round_to_mountain_and_back():
    rec = _rec()
    rec.desired_mode(_state(pct=20))
    rec.set_position(Position.MOUNTAIN)
    assert rec.desired_mode(_state(pct=80)) is DriveMode.HOLD   # floor wins
    rec.set_position(Position.HOLD_SOC)
    assert rec.desired_mode(_state(pct=80)) is DriveMode.HOLD


# -- detent 2: hold-now --------------------------------------------------

def test_hold_now_enforces_hold_from_the_first_pass():
    rec = _rec(default_position=Position.HOLD_NOW)
    assert rec.desired_mode(_state(pct=95)) is DriveMode.HOLD
    assert rec.floor_latched is False       # enforced, not floored
    assert rec.describe_position() == "hold the pack now"


def test_hold_now_needs_no_soc_reading_at_all():
    rec = _rec(default_position=Position.HOLD_NOW)
    assert rec.desired_mode(_state()) is DriveMode.HOLD


def test_one_tap_from_hold_soc_reaches_hold_now():
    rec = _rec()
    assert rec.desired_mode(_state(pct=80)) is None
    rec.advance()
    assert rec.desired_mode(_state(pct=80)) is DriveMode.HOLD


# -- detent 3: mountain --------------------------------------------------

def test_mountain_enforces_from_the_first_pass():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state(pct=80)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


def test_floor_engages_at_threshold_and_overrides_mountain():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state(pct=50)) is DriveMode.MOUNTAIN
    assert rec.desired_mode(_state(pct=30)) is DriveMode.HOLD
    assert rec.floor_latched is True
    # and it stays HOLD even once the pack recovers
    assert rec.desired_mode(_state(pct=60)) is DriveMode.HOLD


# -- detent 4: off -------------------------------------------------------

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


def test_off_is_the_only_way_to_clear_a_key_cycle_latch():
    rec = _rec()
    assert rec.desired_mode(_state(pct=20)) is DriveMode.HOLD
    rec.set_position(Position.HOLD_NOW)
    assert rec.floor_latched is True         # not cleared by a live detent
    rec.set_position(Position.MOUNTAIN)
    assert rec.floor_latched is True
    rec.set_position(Position.OFF)
    assert rec.floor_latched is False
    # and it does not come back on a stale reading when the driver taps round
    rec.set_position(Position.HOLD_SOC)
    assert rec.desired_mode(_state()) is None


# -- failsafe (0x096 b3) when the poll never answers ---------------------

def test_bar_failsafe_engages_once_the_poll_has_had_its_grace():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state(bar=9)) is DriveMode.HOLD
    assert rec.snapshot()["floor_source"] == "bar"


def test_bar_failsafe_holds_fire_during_the_startup_grace():
    """The latch is permanent now, so don't spend the drive on a coarse proxy
    before the exact reading has had a chance to arrive."""
    rec = _rec(default_position=Position.MOUNTAIN,
               monotonic=_clock(start=0.0, now=44.0))    # < poll_stale_s
    assert rec.desired_mode(_state(bar=9)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


def test_bar_failsafe_ignored_while_the_poll_is_fresh():
    rec = _rec(default_position=Position.MOUNTAIN)
    # fresh poll says healthy, b3 says low -> trust the poll
    assert rec.desired_mode(_state(pct=70, bar=9)) is DriveMode.MOUNTAIN
    assert rec.floor_latched is False


def test_stale_poll_falls_back_to_the_failsafe():
    rec = _rec(default_position=Position.MOUNTAIN)
    st = _state(pct=70, pct_age_s=120.0, bar=9)          # poll too old
    assert rec.desired_mode(st) is DriveMode.HOLD


def test_bar_failsafe_does_not_release_either():
    rec = _rec(default_position=Position.MOUNTAIN)
    rec.desired_mode(_state(bar=9))                      # engage off the bar
    assert rec.desired_mode(_state(bar=13)) is DriveMode.HOLD
    assert rec.desired_mode(_state(pct=45)) is DriveMode.HOLD


def test_no_soc_info_leaves_the_latch_unchanged():
    rec = _rec(default_position=Position.MOUNTAIN)
    assert rec.desired_mode(_state()) is DriveMode.MOUNTAIN
    rec.desired_mode(_state(pct=20))                     # latch on
    assert rec.desired_mode(_state()) is DriveMode.HOLD


# -- build from config -----------------------------------------------

def _cfg(position: str, **over) -> dict:
    policy = {"default_position": position, "hold_threshold_percent": 30,
              "bar_failsafe_raw": 9}
    policy.update(over)
    return {"policy": policy, "soc_poll": {"enabled": True, "period_seconds": 10}}


@pytest.mark.parametrize("name,want", [
    ("hold-soc", Position.HOLD_SOC), ("hold-now", Position.HOLD_NOW),
    ("mountain", Position.MOUNTAIN), ("off", Position.OFF),
])
def test_build_from_config(name, want):
    rec = build_reconciler(parse_config(_cfg(name)))
    assert rec.position is want


def test_build_from_config_carries_the_threshold():
    rec = build_reconciler(parse_config(_cfg("hold-soc",
                                             hold_threshold_percent=25)))
    assert rec.describe_position() == "hold the pack at 25%"
    assert rec.desired_mode(_state(pct=27)) is None
    assert rec.desired_mode(_state(pct=25)) is DriveMode.HOLD


@pytest.mark.parametrize("legacy", ["auto", "hold"])
def test_build_from_config_accepts_the_legacy_names(legacy):
    """Both older spellings of detent 1 must keep an un-converged host booting."""
    raw = _cfg("hold-soc")
    del raw["policy"]["default_position"]
    raw["policy"]["default_setpoint"] = legacy
    rec = build_reconciler(parse_config(raw))
    assert rec.position is Position.HOLD_SOC


# -- snapshot --------------------------------------------------------

def test_snapshot_shape():
    rec = _rec()
    rec.desired_mode(_state(pct=20))
    snap = rec.snapshot()
    assert snap["setpoint"] == "hold-soc"
    assert snap["position"] == "hold-soc"
    assert snap["position_index"] == 1
    assert snap["position_description"] == "hold the pack at 30%"
    assert snap["cycle"] == ["hold-soc", "hold-now", "mountain", "off"]
    assert snap["floor_latched"] is True
    assert snap["floor_source"] == "poll"
    assert snap["hold_threshold_percent"] == 30.0
    assert "hold_reset_percent" not in snap


def test_poll_stale_s_exposes_the_configured_threshold():
    """The LCD watch screen judges SOC freshness against this -- it must be
    the same number the floor logic itself uses, not a second guess at it."""
    assert _rec(poll_stale_s=45.0).poll_stale_s == 45.0
    assert _rec(poll_stale_s=12.0).poll_stale_s == 12.0
