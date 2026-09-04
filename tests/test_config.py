import pytest

from voltdmf.config import ConfigError, load_config, parse_config
from voltdmf.reconciler import Position


def _valid() -> dict:
    return {
        "policy": {
            "default_position": "hold-soc",
            "hold_threshold_percent": 30,
            "bar_failsafe_raw": 9,
        },
        "soc_poll": {"enabled": True, "period_seconds": 10},
    }


def test_parse_valid():
    cfg = parse_config(_valid())
    assert cfg.policy.default_position is Position.HOLD_SOC
    assert cfg.policy.hold_threshold_percent == 30
    assert cfg.policy.bar_failsafe_raw == 9
    assert cfg.soc_poll.enabled is True
    assert cfg.soc_poll.period_seconds == 10


@pytest.mark.parametrize("name,want", [("hold-now", Position.HOLD_NOW),
                                       ("mountain", Position.MOUNTAIN),
                                       ("off", Position.OFF)])
def test_every_detent_parses(name, want):
    raw = _valid()
    raw["policy"]["default_position"] = name
    assert parse_config(raw).policy.default_position is want


def test_legacy_default_setpoint_key_still_read():
    """A config.yaml deployed before the three-position selector must load."""
    raw = _valid()
    del raw["policy"]["default_position"]
    raw["policy"]["default_setpoint"] = "mountain"
    assert parse_config(raw).policy.default_position is Position.MOUNTAIN


@pytest.mark.parametrize("legacy", ["auto", "hold"])
def test_legacy_names_map_to_hold_soc(legacy):
    """`auto` (two-way toggle) and `hold` (three-position cycle) both named
    what is now `hold-soc`, so a config.yaml from either era keeps loading."""
    raw = _valid()
    del raw["policy"]["default_position"]
    raw["policy"]["default_setpoint"] = legacy
    assert parse_config(raw).policy.default_position is Position.HOLD_SOC


def test_both_position_keys_is_an_error():
    raw = _valid()
    raw["policy"]["default_setpoint"] = "mountain"
    with pytest.raises(ConfigError, match="only one of"):
        parse_config(raw)


def test_missing_position_key():
    raw = _valid()
    del raw["policy"]["default_position"]
    with pytest.raises(ConfigError, match="default_position"):
        parse_config(raw)


def test_retired_hold_reset_percent_is_warned_about_not_rejected(caplog):
    """The floor latches for the key cycle now, so the release percent is
    gone. A host that has not converged yet must still boot."""
    raw = _valid()
    raw["policy"]["hold_reset_percent"] = 41
    with caplog.at_level("WARNING", logger="voltdmf.config"):
        cfg = parse_config(raw)
    assert cfg.policy.default_position is Position.HOLD_SOC
    assert not hasattr(cfg.policy, "hold_reset_percent")
    assert "hold_reset_percent" in caplog.text


@pytest.mark.parametrize("bad", ["normal", "sport", "ludicrous"])
def test_bad_default_position(bad):
    raw = _valid()
    raw["policy"]["default_position"] = bad
    with pytest.raises(ConfigError, match="default_position"):
        parse_config(raw)


def test_missing_key():
    raw = _valid()
    del raw["policy"]["hold_threshold_percent"]
    with pytest.raises(ConfigError, match="hold_threshold_percent"):
        parse_config(raw)


def test_missing_section():
    raw = _valid()
    del raw["soc_poll"]
    with pytest.raises(ConfigError, match="soc_poll"):
        parse_config(raw)


@pytest.mark.parametrize("bad", [-1, 101, "abc"])
def test_percent_out_of_range(bad):
    raw = _valid()
    raw["policy"]["hold_threshold_percent"] = bad
    with pytest.raises(ConfigError):
        parse_config(raw)


@pytest.mark.parametrize("bad", [-1, 256, "abc"])
def test_bar_failsafe_out_of_range(bad):
    raw = _valid()
    raw["policy"]["bar_failsafe_raw"] = bad
    with pytest.raises(ConfigError, match="bar_failsafe_raw"):
        parse_config(raw)


@pytest.mark.parametrize("bad", [0, -5, "soon"])
def test_bad_poll_period(bad):
    raw = _valid()
    raw["soc_poll"]["period_seconds"] = bad
    with pytest.raises(ConfigError, match="period_seconds"):
        parse_config(raw)


def test_load_the_example_config():
    # The repo's own example must always be valid.
    import pathlib

    example = pathlib.Path(__file__).parent.parent / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.policy.default_position is Position.HOLD_SOC
    assert 0 < cfg.policy.hold_threshold_percent < 100
    assert cfg.soc_poll.enabled is True


def test_load_missing_file():
    with pytest.raises(OSError):
        load_config("/no/such/config.yaml")
