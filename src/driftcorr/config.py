"""Application configuration loading.

Configuration lives in a JSON file (default: config/default.json) so that
detection/fit/resample behaviour is tunable without touching code. The path
can be overridden with the DRIFTCORR_CONFIG environment variable; individual
keys can be overridden with DRIFTCORR_DB_PATH / DRIFTCORR_OUTPUT_DIR.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.json"


@dataclass(frozen=True)
class DetectionConfig:
    correlation_threshold: float = 0.55
    max_pairing_offset_s: float = 0.4
    min_peak_separation_s: float = 0.05


@dataclass(frozen=True)
class FitConfig:
    min_inliers: int = 3
    min_residual_threshold_s: float = 0.004
    mad_multiplier: float = 6.0
    max_inlier_residual_s: float = 0.05


@dataclass(frozen=True)
class ResampleConfig:
    half_width_taps: int = 16


@dataclass(frozen=True)
class ReportConfig:
    alignment_check_enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    db_path: str = "data/jobs.sqlite3"
    output_dir: str = "data/outputs"
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    fit: FitConfig = field(default_factory=FitConfig)
    resample: ResampleConfig = field(default_factory=ResampleConfig)
    report: ReportConfig = field(default_factory=ReportConfig)


def _merge(raw: dict[str, Any]) -> AppConfig:
    return AppConfig(
        db_path=os.environ.get("DRIFTCORR_DB_PATH", raw.get("db_path", "data/jobs.sqlite3")),
        output_dir=os.environ.get("DRIFTCORR_OUTPUT_DIR", raw.get("output_dir", "data/outputs")),
        detection=DetectionConfig(**raw.get("detection", {})),
        fit=FitConfig(**raw.get("fit", {})),
        resample=ResampleConfig(**raw.get("resample", {})),
        report=ReportConfig(**raw.get("report", {})),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration from JSON; missing file falls back to defaults."""
    cfg_path = Path(path or os.environ.get("DRIFTCORR_CONFIG", _DEFAULT_CONFIG_PATH))
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    return _merge(raw)
