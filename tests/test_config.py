import pytest

from voltdmf.config import ConfigError, load_config, parse_config
from voltdmf.signals import DriveMode


def _valid() -> dict:
    return {
        "policy": {
            "default_setpoint": "hold",
            "hold_threshold_percent": 33,
            "hold_reset_percent": 41,
            "bar_failsafe_raw": 9,
        },
        "soc_poll": {"enabled": True, "period_seconds": 10},
    }


def test_parse_valid():
    cfg = parse_config(_valid())
    assert cfg.policy.default_setpoint is DriveMode.HOLD
    assert cfg.policy.hold_threshold_percent == 33
    assert cfg.policy.hold_reset_percent == 41
    assert cfg.policy.bar_failsafe_raw == 9
    assert cfg.soc_poll.enabled is True
    assert cfg.soc_poll.period_seconds == 10


def test_default_setpoint_mountain_is_ok():
    raw = _valid()
    raw["policy"]["default_setpoint"] = "mountain"
    assert parse_config(raw).policy.default_setpoint is DriveMode.MOUNTAIN


def test_reset_must_exceed_threshold():
    raw = _valid()
    raw["policy"]["hold_reset_percent"] = 33
    with pytest.raises(ConfigError, match="hold_reset_percent"):
        parse_config(raw)


@pytest.mark.parametrize("bad", ["normal", "sport", "ludicrous"])
def test_bad_default_setpoint(bad):
    raw = _valid()
    raw["policy"]["default_setpoint"] = bad
    with pytest.raises(ConfigError, match="default_setpoint"):
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
    assert cfg.policy.default_setpoint in (DriveMode.HOLD, DriveMode.MOUNTAIN)
    assert cfg.policy.hold_reset_percent > cfg.policy.hold_threshold_percent
    assert cfg.soc_poll.enabled is True


def test_load_missing_file():
    with pytest.raises(OSError):
        load_config("/no/such/config.yaml")
