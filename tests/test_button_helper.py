"""tools/button_helper.py -- the gesture state machine and the arg parser.

``_Gestures`` is side-effect free, so the whole gesture map is exercised here
without GPIO: feed it a stream of ``(sw1, sw2)`` samples at a fixed tick and
assert which actions come back. ``ButtonHelper`` construction is checked too
(it is lazy about gpiozero, so it builds fine off-Pi).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import button_helper  # noqa: E402

_DT = 0.05


def _mk(*, min_tap=0.04, single_hold=1.0, launch_hold=1.0):
    return button_helper._Gestures(min_tap=min_tap, single_hold=single_hold,
                                   launch_hold=launch_hold)


def _frames(*segments):
    """(a, b, seconds) segments -> flat list of (a, b) ticks at _DT spacing."""
    out = []
    for a, b, secs in segments:
        out += [(a, b)] * max(1, round(secs / _DT))
    return out


def _play(g, frames, start=100.0):
    """Feed frames one per tick; return [(elapsed, action), ...] for hits."""
    hits = []
    for i, (a, b) in enumerate(frames):
        t = start + i * _DT
        act = g.feed(a, b, t)
        if act:
            hits.append((round(t - start, 3), act))
    return hits


def _acts(hits):
    return [a for _, a in hits]


# --- SW1 tap -> setpoint --------------------------------------------------
def test_sw1_tap_emits_setpoint_once():
    hits = _play(_mk(), _frames((True, False, 0.2), (False, False, 0.2)))
    assert _acts(hits) == ["setpoint"]


def test_sw1_blip_below_min_tap_ignored():
    g = _mk(min_tap=0.20)
    hits = _play(g, _frames((True, False, 0.10), (False, False, 0.1)))
    assert hits == []


def test_sw1_long_hold_alone_is_not_a_tap():
    # held past launch_hold with SW2 untouched -> neither a tap nor a combo
    hits = _play(_mk(), _frames((True, False, 2.0), (False, False, 0.2)))
    assert hits == []


# --- SW2 solo hold -> charge capture ------------------------------------
def test_sw2_solo_hold_emits_launch_charge_on_release():
    hits = _play(_mk(), _frames((False, True, 1.3), (False, False, 0.2)))
    assert _acts(hits) == ["launch_charge"]
    # fires on release (~1.3 s), not when the threshold is first crossed
    assert hits[0][0] >= 1.25


def test_sw2_solo_hold_too_short_does_nothing():
    hits = _play(_mk(), _frames((False, True, 0.6), (False, False, 0.2)))
    assert hits == []


def test_sw2_held_then_released_without_reaching_threshold_rearms():
    hits = _play(_mk(), _frames(
        (False, True, 0.6), (False, False, 0.2),   # too short
        (False, True, 1.3), (False, False, 0.2)))  # long enough
    assert _acts(hits) == ["launch_charge"]


# --- SW1 + SW2 -> SOC capture -----------------------------------------
def test_both_held_emits_launch_soc_once_while_held():
    hits = _play(_mk(), _frames((True, True, 2.0)))
    assert _acts(hits) == ["launch_soc"]
    assert 0.95 <= hits[0][0] <= 1.15


def test_launch_soc_rearms_only_after_full_release():
    hits = _play(_mk(), _frames(
        (True, True, 2.0), (False, False, 0.2), (True, True, 1.3)))
    assert _acts(hits) == ["launch_soc", "launch_soc"]


def test_hand_moving_into_the_combo_never_trips_charge():
    # SW2 down first, SW1 joins, both held long -> SOC, and the later SW2
    # release must NOT also emit launch_charge
    hits = _play(_mk(), _frames(
        (False, True, 0.8), (True, True, 1.3),
        (False, True, 0.2), (False, False, 0.2)))
    assert _acts(hits) == ["launch_soc"]


def test_reset_clears_the_soc_latch():
    g = _mk()
    _play(g, _frames((True, True, 1.3)))   # fires, sets the latch, no release
    g.reset()
    hits = _play(g, _frames((True, True, 1.3)))
    assert _acts(hits) == ["launch_soc"]


# --- parser / wiring --------------------------------------------------
def test_parser_defaults_carry_the_new_flags():
    ns = button_helper._build_parser().parse_args([])
    assert ns.single_hold_secs == 5.0
    assert ns.chargelog_unit == "voltdmf-chargelog.service"
    assert ns.soclog_unit == "voltdmf-soclog.service"
    assert ns.launch_hold_secs == 5.0


def test_parser_help_renders_and_lists_the_new_gesture():
    txt = button_helper._build_parser().format_help()
    assert "--single-hold-secs" in txt
    assert "--chargelog-unit" in txt


def test_helper_wires_the_chargelog_unit_into_the_fsm():
    ns = button_helper._build_parser().parse_args(
        ["--chargelog-unit", "x.service", "--single-hold-secs", "3"])
    h = button_helper.ButtonHelper(ns)
    assert h._chargelog_unit == "x.service"
    assert h._single_hold == 3.0
    assert h._gestures._single_hold == 3.0
