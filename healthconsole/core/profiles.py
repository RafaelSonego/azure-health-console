"""Curated per-type health profiles: built-in YAML, overridden by the user's own directory."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BUILT_IN_DIR = Path(__file__).resolve().parent.parent / "profiles"
OPERATORS = {"GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual"}
AGGREGATIONS = {"Average", "Maximum", "Minimum", "Total", "Count"}

_PROFILE_KEYS = {"type", "states", "metrics"}
_STATE_KEYS = {"path", "healthy", "warning", "critical"}
_METRIC_KEYS = {"name", "aggregation", "operator", "warn", "crit"}


class ProfileError(ValueError):
    pass


@dataclass
class MetricSpec:
    name: str
    aggregation: str
    operator: str = "GreaterThan"
    warn: float | None = None
    crit: float | None = None


@dataclass
class StateRule:
    path: str
    healthy: list = field(default_factory=list)
    warning: list = field(default_factory=list)
    critical: list = field(default_factory=list)


@dataclass
class Profile:
    type: str
    states: list = field(default_factory=list)
    metrics: list = field(default_factory=list)


def _check_keys(data, allowed, where):
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: expected a mapping.")
    unknown = set(data) - allowed
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) {sorted(unknown)}; allowed {sorted(allowed)}.")


def parse(data, source="profile"):
    _check_keys(data, _PROFILE_KEYS, source)
    if not data.get("type"):
        raise ProfileError(f"{source}: `type` is required.")
    profile = Profile(type=str(data["type"]).lower())

    for index, state in enumerate(data.get("states") or []):
        _check_keys(state, _STATE_KEYS, f"{source} states[{index}]")
        if not state.get("path"):
            raise ProfileError(f"{source} states[{index}]: `path` is required.")
        profile.states.append(StateRule(
            path=state["path"],
            healthy=[str(v).lower() for v in state.get("healthy") or []],
            warning=[str(v).lower() for v in state.get("warning") or []],
            critical=[str(v).lower() for v in state.get("critical") or []],
        ))

    for index, metric in enumerate(data.get("metrics") or []):
        where = f"{source} metrics[{index}]"
        _check_keys(metric, _METRIC_KEYS, where)
        if not metric.get("name") or metric.get("aggregation") not in AGGREGATIONS:
            raise ProfileError(f"{where}: needs `name` and `aggregation` in {sorted(AGGREGATIONS)}.")
        operator = metric.get("operator", "GreaterThan")
        if operator not in OPERATORS:
            raise ProfileError(f"{where}: operator must be one of {sorted(OPERATORS)}.")
        profile.metrics.append(MetricSpec(
            name=metric["name"], aggregation=metric["aggregation"], operator=operator,
            warn=metric.get("warn"), crit=metric.get("crit"),
        ))
    return profile


def load(user_dir=None):
    """Built-in profiles first; a user profile for the same type replaces it entirely."""
    profiles = {}
    for directory in (BUILT_IN_DIR, Path(user_dir).expanduser() if user_dir else None):
        if not directory or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            with path.open() as handle:
                profile = parse(yaml.safe_load(handle) or {}, source=path.name)
            profiles[profile.type] = profile
    return profiles
