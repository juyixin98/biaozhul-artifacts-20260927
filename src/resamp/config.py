"""Runtime configuration.

Values are resolved with the following precedence (highest first):
environment variables ``RESAMP_<NAME>`` → TOML file pointed at by
``RESAMP_CONFIG`` → built-in defaults.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields


DEFAULTS = dict(
    db_path="./data/resamp.db",
    data_dir="./data/jobs",
    log_dir="./data/logs",
    max_input_chunk_samples=1_000_000,
    max_total_input_samples=50_000_000,
    max_filter_taps=2_000_001,
    json_list_sample_cap=8192,
    min_rate=1,
    max_rate=10_000_000,
    max_ratio_factor=4096,
    attenuation_db=80.0,
    transition_half_width=0.1,
)


@dataclass(frozen=True)
class Settings:
    db_path: str
    data_dir: str
    log_dir: str
    max_input_chunk_samples: int
    max_total_input_samples: int
    max_filter_taps: int
    json_list_sample_cap: int
    min_rate: int
    max_rate: int
    max_ratio_factor: int
    attenuation_db: float
    transition_half_width: float

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.log_dir):
            os.makedirs(path, exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)


def _coerce(name: str, raw: str):
    default = DEFAULTS[name]
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_settings(config_path: str | None = None) -> Settings:
    values = dict(DEFAULTS)
    path = config_path or os.environ.get("RESAMP_CONFIG")
    if path:
        with open(path, "rb") as fh:
            loaded = tomllib.load(fh)
        unknown = set(loaded) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        values.update(loaded)
    prefix = "RESAMP_"
    for key in os.environ:
        if key.startswith(prefix):
            name = key[len(prefix):].lower()
            if name in values:
                values[name] = _coerce(name, os.environ[key])
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in values.items() if k in known})
