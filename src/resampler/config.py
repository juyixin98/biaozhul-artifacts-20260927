"""Runtime configuration.

Values can be provided via environment variables (prefix ``RESAMPLER_``,
e.g. ``RESAMPLER_DB_PATH=/tmp/x.db``) or a JSON file pointed to by
``RESAMPLER_CONFIG``; defaults match ``configs/default.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

DEFAULTS = {
    "db_path": "data/resampler.db",
    "log_dir": "logs",
    "max_samples_per_chunk": 1 << 20,      # 1,048,576 input samples / chunk
    "max_total_samples": 64 << 20,         # 67,108,864 input samples / job
    "max_jobs": 1000,
    "max_ratio_term": 1_000_000,           # L, M after reduction must be <= this
    "max_filter_taps": 1 << 20,            # K*L prototype length cap
    "default_atten_db": 80.0,              # Kaiser stopband attenuation
    "default_passband_edge": 0.9,          # fraction of min-Nyquist used as passband edge
    "min_taps_per_phase": 8,               # minimum K
}


@dataclass(frozen=True)
class Settings:
    db_path: str = DEFAULTS["db_path"]
    log_dir: str = DEFAULTS["log_dir"]
    max_samples_per_chunk: int = DEFAULTS["max_samples_per_chunk"]
    max_total_samples: int = DEFAULTS["max_total_samples"]
    max_jobs: int = DEFAULTS["max_jobs"]
    max_ratio_term: int = DEFAULTS["max_ratio_term"]
    max_filter_taps: int = DEFAULTS["max_filter_taps"]
    default_atten_db: float = DEFAULTS["default_atten_db"]
    default_passband_edge: float = DEFAULTS["default_passband_edge"]
    min_taps_per_phase: int = DEFAULTS["min_taps_per_phase"]

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        data = dict(DEFAULTS)

        cfg_path = path or os.environ.get("RESAMPLER_CONFIG")
        if cfg_path and os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data.update(json.load(fh))

        prefix = "RESAMPLER_"
        env_types = {f.name: f.type for f in fields(cls)}
        for key in list(data):
            env = prefix + key.upper()
            if env in os.environ:
                raw = os.environ[env]
                if env_types.get(key) is int:
                    data[key] = int(raw)
                elif env_types.get(key) is float:
                    data[key] = float(raw)
                else:
                    data[key] = raw
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)
