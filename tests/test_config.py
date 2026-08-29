import pytest

from voltdmf.config import ConfigError, load_config, parse_config
from voltdmf.signals import DriveMode


def _valid() -> dict:
    return {
        "on_start": {"enabled": True, "target_mode": "mountain"},
        "soc_threshold": {
            "enabled": True,
            "target_mode": "hold",
            "threshold_percent": 25,
            "reset_percent": 40,
        },
        "trip_mode": {"enabled": False},
    }


def test_parse_valid():
    cfg = parse_config(_valid())
    assert cfg.on_start.enabled is True
    assert cfg.on_start.target_mode is DriveMode.MOUNTAIN
    assert cfg.soc_threshold.target_mode is DriveMode.HOLD
    assert cfg.soc_threshold.threshold_percent == 25
    assert cfg.soc_threshold.reset_percent == 40
    assert cfg.trip_mode.enabled is False


def test_trip_mode_defaults_to_disabled_when_absent():
    raw = _valid()
    del raw["trip_mode"]
    assert parse_config(raw).trip_mode.enabled is False


def test_reset_must_exceed_threshold():
    raw = _valid()
    raw["soc_threshold"]["reset_percent"] = 25
    with pytest.raises(ConfigError, match="reset_percent"):
        parse_config(raw)


def test_bad_target_mode():
    raw = _valid()
    raw["on_start"]["target_mode"] = "ludicrous"
    with pytest.raises(ConfigError, match="target_mode"):
        parse_config(raw)


def test_trip_mode_enabled_is_rejected():
    raw = _valid()
    raw["trip_mode"]["enabled"] = True
    with pytest.raises(ConfigError, match="trip_mode"):
        parse_config(raw)


def test_missing_key():
    raw = _valid()
    del raw["soc_threshold"]["threshold_percent"]
    with pytest.raises(ConfigError, match="threshold_percent"):
        parse_config(raw)


@pytest.mark.parametrize("bad", [-1, 101, "abc"])
def test_percent_out_of_range(bad):
    raw = _valid()
    raw["soc_threshold"]["threshold_percent"] = bad
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_load_the_example_config(tmp_path):
    # The repo's own example must always be valid.
    import pathlib

    example = pathlib.Path(__file__).parent.parent / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.on_start.target_mode is DriveMode.MOUNTAIN
    assert cfg.soc_threshold.reset_percent > cfg.soc_threshold.threshold_percent


def test_load_missing_file():
    with pytest.raises(OSError):
        load_config("/no/such/config.yaml")
