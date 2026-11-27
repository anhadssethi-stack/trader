"""Load settings from config/settings.yaml (+ optional settings.local.yaml)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "settings.yaml"
LOCAL_PATH = ROOT / "config" / "settings.local.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings(dict):
    """A dict with attribute access for top-level sections: s.profile['capital']."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def path(self, key: str) -> Path:
        """Resolve a data path (relative paths are relative to the project root)."""
        p = Path(self["data"][key]).expanduser()
        return p if p.is_absolute() else ROOT / p

    # Derived profile values, kept here so every module computes them the same way.
    @property
    def risk_amount(self) -> float:
        p = self["profile"]
        return p["capital"] * p["risk_per_trade_pct"] / 100.0

    @property
    def max_position_value(self) -> float:
        p = self["profile"]
        return p["capital"] * p["max_position_pct"] / 100.0


def load_settings(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Settings:
    path = Path(path or os.environ.get("NSE_RESEARCH_SETTINGS", DEFAULT_PATH))
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if path == DEFAULT_PATH and LOCAL_PATH.exists():
        with open(LOCAL_PATH) as f:
            data = _merge(data, yaml.safe_load(f) or {})
    if overrides:
        data = _merge(data, overrides)
    return Settings(data)
