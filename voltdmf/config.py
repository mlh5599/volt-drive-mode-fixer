"""Config loading and validation.

Schema mirrors the YAML sketch in DESIGN.md ("Trigger strategies"). See
``config.example.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .signals import DriveMode


class ConfigError(ValueError):
    """Raised for a malformed or invalid config file."""


@dataclass(frozen=True)
class OnStartConfig:
    enabled: bool
    target_mode: DriveMode


@dataclass(frozen=True)
class SocThresholdConfig:
    enabled: bool
    target_mode: DriveMode
    threshold_percent: float
    reset_percent: float


@dataclass(frozen=True)
class TripModeConfig:
    enabled: bool


@dataclass(frozen=True)
class Config:
    on_start: OnStartConfig
    soc_threshold: SocThresholdConfig
    trip_mode: TripModeConfig


def _require(mapping: dict, key: str, section: str):
    if key not in mapping:
        raise ConfigError(f"{section}: missing required key '{key}'")
    return mapping[key]


def _as_mode(value, section: str) -> DriveMode:
    try:
        return DriveMode(str(value).lower())
    except ValueError:
        allowed = ", ".join(m.value for m in DriveMode)
        raise ConfigError(
            f"{section}: target_mode '{value}' is not one of: {allowed}"
        ) from None


def _as_percent(value, section: str, key: str) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{section}: '{key}' must be a number") from None
    if not 0.0 <= pct <= 100.0:
        raise ConfigError(f"{section}: '{key}' must be between 0 and 100")
    return pct


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("top level must be a mapping")

    os_raw = _require(raw, "on_start", "on_start")
    on_start = OnStartConfig(
        enabled=bool(_require(os_raw, "enabled", "on_start")),
        target_mode=_as_mode(_require(os_raw, "target_mode", "on_start"), "on_start"),
    )

    st_raw = _require(raw, "soc_threshold", "soc_threshold")
    threshold = _as_percent(
        _require(st_raw, "threshold_percent", "soc_threshold"),
        "soc_threshold", "threshold_percent",
    )
    reset = _as_percent(
        _require(st_raw, "reset_percent", "soc_threshold"),
        "soc_threshold", "reset_percent",
    )
    if reset <= threshold:
        raise ConfigError(
            "soc_threshold: reset_percent must be greater than threshold_percent "
            f"(got reset={reset}, threshold={threshold})"
        )
    soc_threshold = SocThresholdConfig(
        enabled=bool(_require(st_raw, "enabled", "soc_threshold")),
        target_mode=_as_mode(
            _require(st_raw, "target_mode", "soc_threshold"), "soc_threshold"
        ),
        threshold_percent=threshold,
        reset_percent=reset,
    )

    tm_raw = raw.get("trip_mode", {"enabled": False})
    if bool(_require(tm_raw, "enabled", "trip_mode")):
        raise ConfigError(
            "trip_mode: not implemented yet (DR5, roadmap) -- set enabled: false"
        )
    trip_mode = TripModeConfig(enabled=False)

    return Config(on_start=on_start, soc_threshold=soc_threshold, trip_mode=trip_mode)


def load_config(path: str | Path) -> Config:
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse YAML: {exc}") from exc
    return parse_config(raw)
