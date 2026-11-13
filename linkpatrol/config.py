"""TOML configuration loading, defaults, and validation."""

from __future__ import annotations

import copy
import re
import tomllib
from pathlib import Path
from typing import Any

class ConfigError(ValueError):
    """Raised when a configuration file is missing or invalid."""

DEFAULTS: dict[str, Any] = {
    "seed": "",
    "scope": {
        "allow_subdomains": False,
        "include": [],
        "exclude": [],
    },
    "limits": {
        "max_depth": 3,
        "max_pages": 100,
        "timeout_s": 5.0,
        "retries": 2,
        "delay_ms": 0,
        "concurrency": 1,
        "max_redirects": 5,
    },
    "robots": {
        "respect": True,
        "user_agent": "linkpatrol/0.1",
    },
    "external": {"check": True},
    "normalize": {"drop_params": []},
    "output": {
        "dir": "reports",
        "database": "out/linkpatrol.db",
        "sitemap": "out/sitemap.xml",
    },
    "retention": {
        "keep_runs": None,
        "retain_days": None,
    },
}

def _merge(defaults: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in supplied.items():
        if key not in merged:
            raise ConfigError(f"unknown configuration key: {key}")
        if isinstance(merged[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"configuration key {key!r} must be a table")
            for child_key, child_value in value.items():
                if child_key not in merged[key]:
                    raise ConfigError(f"unknown configuration key: {key}.{child_key}")
                merged[key][child_key] = child_value
        else:
            merged[key] = value
    return merged

def _as_int(section: dict[str, Any], name: str, low: int, high: int) -> int:
    value = section[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value

def _as_float(section: dict[str, Any], name: str, low: float, high: float) -> float:
    value = section[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    if not low <= float(value) <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return float(value)

def _validate_regexes(patterns: list[str], label: str) -> None:
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid {label} regular expression {pattern!r}: {exc}") from exc

def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate an already merged configuration and normalise scalar values."""
    seed = config.get("seed")
    if not isinstance(seed, str) or not seed.strip():
        raise ConfigError("seed must be a non-empty http(s) URL")
    if not re.match(r"^https?://[^/\s]+", seed, re.IGNORECASE):
        raise ConfigError("seed must start with http:// or https://")

    scope = config["scope"]
    for key in ("allow_subdomains",):
        if not isinstance(scope[key], bool):
            raise ConfigError(f"scope.{key} must be a boolean")
    for key in ("include", "exclude"):
        if not isinstance(scope[key], list) or not all(
            isinstance(item, str) for item in scope[key]
        ):
            raise ConfigError(f"scope.{key} must be a list of strings")
        _validate_regexes(scope[key], f"scope.{key}")

    limits = config["limits"]
    _as_int(limits, "max_depth", 0, 20)
    _as_int(limits, "max_pages", 1, 100000)
    _as_int(limits, "retries", 0, 10)
    _as_int(limits, "delay_ms", 0, 60000)
    _as_int(limits, "concurrency", 1, 8)
    _as_int(limits, "max_redirects", 1, 20)
    _as_float(limits, "timeout_s", 0.05, 120.0)

    robots = config["robots"]
    if not isinstance(robots["respect"], bool):
        raise ConfigError("robots.respect must be a boolean")
    if not isinstance(robots["user_agent"], str) or not robots["user_agent"].strip():
        raise ConfigError("robots.user_agent must be a non-empty string")

    if not isinstance(config["external"]["check"], bool):
        raise ConfigError("external.check must be a boolean")

    drop_params = config["normalize"]["drop_params"]
    if not isinstance(drop_params, list) or not all(isinstance(x, str) for x in drop_params):
        raise ConfigError("normalize.drop_params must be a list of strings")

    output = config["output"]
    for key in ("dir", "database", "sitemap"):
        if not isinstance(output[key], str) or not output[key].strip():
            raise ConfigError(f"output.{key} must be a non-empty path")

    retention = config["retention"]
    if retention["keep_runs"] is not None:
        _as_int(retention, "keep_runs", 0, 1000000)
    retain_days = retention["retain_days"]
    if retain_days is not None:
        if isinstance(retain_days, bool) or not isinstance(retain_days, (int, float)):
            raise ConfigError("retention.retain_days must be a number")
        if retain_days < 0:
            raise ConfigError("retention.retain_days must be >= 0")
        retention["retain_days"] = float(retain_days)
    return config

def load_config(path: str | Path) -> dict[str, Any]:
    """Read a TOML file, merge it with defaults, and validate the result."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            supplied = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    if not isinstance(supplied, dict):
        raise ConfigError("configuration root must be a TOML table")
    return validate_config(_merge(DEFAULTS, supplied))

def default_config(seed: str) -> dict[str, Any]:
    """Return a validated default configuration for a seed URL."""
    config = copy.deepcopy(DEFAULTS)
    config["seed"] = seed
    return validate_config(config)
