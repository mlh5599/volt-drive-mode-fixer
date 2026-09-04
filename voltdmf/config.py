"""Config loading and validation.

Two sections, both required (see ``config.example.yaml``):

* ``policy`` -- the reconciler's decision constants: the boot detent of the
  four-position selector (``hold-soc``, ``hold-now``, ``mountain``, ``off``)
  and the SOC-HOLD floor's engage / failsafe thresholds.
* ``soc_poll`` -- whether the daemon runs the ``22 005B`` UDS poll and how
  often. Enabled by default; the goal is to trust a passive SOC signal later
  and switch it off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .reconciler import CYCLE, LEGACY_POSITION_NAMES, Position, resolve_position

#: Policy keys that older config.yaml files carry and this one no longer has.
#: Ignored rather than rejected so a host that has not converged yet still
#: boots; the daemon logs them once at parse time.
_RETIRED_POLICY_KEYS = {
    "hold_reset_percent": "the SOC floor no longer releases mid-drive -- it "
                          "latches for the rest of the key cycle",
}


log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised for a malformed or invalid config file."""


@dataclass(frozen=True)
class PolicyConfig:
    #: Boot detent of the SW1 selector, not persisted. ``hold-soc`` (the
    #: shipped default) is passive until the pack reaches the threshold,
    #: ``hold-now`` enforces HOLD from loop 1, ``mountain`` enforces MOUNTAIN,
    #: ``off`` disables everything including the floor. See
    #: :mod:`voltdmf.reconciler` for the full position semantics.
    default_position: Position
    #: Diag SOC at/below which the floor forces HOLD -- for the rest of the
    #: key cycle; there is no release percent.
    hold_threshold_percent: float
    #: 0x096 byte 3 at/below which HOLD is forced when the poll is stale.
    bar_failsafe_raw: int


@dataclass(frozen=True)
class SocPollConfig:
    enabled: bool
    period_seconds: float


@dataclass(frozen=True)
class Config:
    policy: PolicyConfig
    soc_poll: SocPollConfig


def _require(mapping, key: str, section: str):
    if not isinstance(mapping, dict):
        raise ConfigError(f"{section}: must be a mapping")
    if key not in mapping:
        raise ConfigError(f"{section}: missing required key '{key}'")
    return mapping[key]


#: Preferred policy key for the boot detent. ``default_setpoint`` is the old
#: name and is still read, so a config.yaml deployed before the selector
#: existed keeps loading (its ``auto`` / ``hold`` map to ``hold-soc``).
_POSITION_KEY = "default_position"
_LEGACY_POSITION_KEY = "default_setpoint"


def _policy_position(p_raw: dict):
    """The boot-position value, from either key. Neither present is an error;
    both present is too, rather than silently preferring one."""
    has_new = _POSITION_KEY in p_raw
    has_old = _LEGACY_POSITION_KEY in p_raw
    if has_new and has_old:
        raise ConfigError(
            f"policy: set only one of {_POSITION_KEY} / {_LEGACY_POSITION_KEY} "
            f"({_LEGACY_POSITION_KEY} is the old name for the same setting)")
    if has_new:
        return p_raw[_POSITION_KEY]
    if has_old:
        return p_raw[_LEGACY_POSITION_KEY]
    raise ConfigError(f"policy: missing required key '{_POSITION_KEY}'")


def _as_position(value, section: str, key: str) -> Position:
    try:
        return resolve_position(value)
    except ValueError:
        allowed = ", ".join((*(p.value for p in CYCLE),
                             *sorted(LEGACY_POSITION_NAMES)))
        raise ConfigError(
            f"{section}: {key} '{value}' is not one of: {allowed}"
        ) from None


def _as_percent(value, section: str, key: str) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{section}: '{key}' must be a number") from None
    if not 0.0 <= pct <= 100.0:
        raise ConfigError(f"{section}: '{key}' must be between 0 and 100")
    return pct


def _as_byte(value, section: str, key: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{section}: '{key}' must be an integer") from None
    if not 0 <= n <= 255:
        raise ConfigError(f"{section}: '{key}' must be between 0 and 255")
    return n


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("top level must be a mapping")

    p_raw = _require(raw, "policy", "policy")
    for key, why in _RETIRED_POLICY_KEYS.items():
        if isinstance(p_raw, dict) and key in p_raw:
            log.warning("config: policy.%s is no longer used and is ignored "
                        "(%s); drop it from the file", key, why)
    threshold = _as_percent(
        _require(p_raw, "hold_threshold_percent", "policy"),
        "policy", "hold_threshold_percent",
    )
    policy = PolicyConfig(
        default_position=_as_position(
            _policy_position(p_raw), "policy", _POSITION_KEY
        ),
        hold_threshold_percent=threshold,
        bar_failsafe_raw=_as_byte(
            _require(p_raw, "bar_failsafe_raw", "policy"),
            "policy", "bar_failsafe_raw",
        ),
    )

    sp_raw = _require(raw, "soc_poll", "soc_poll")
    try:
        period = float(_require(sp_raw, "period_seconds", "soc_poll"))
    except (TypeError, ValueError):
        raise ConfigError("soc_poll: 'period_seconds' must be a number") from None
    if period <= 0:
        raise ConfigError("soc_poll: 'period_seconds' must be > 0")
    soc_poll = SocPollConfig(
        enabled=bool(_require(sp_raw, "enabled", "soc_poll")),
        period_seconds=period,
    )

    return Config(policy=policy, soc_poll=soc_poll)


def load_config(path: str | Path) -> Config:
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse YAML: {exc}") from exc
    return parse_config(raw)
