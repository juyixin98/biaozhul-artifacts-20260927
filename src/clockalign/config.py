"""Application configuration loading.

Configuration lives in a standalone YAML file (``config/default.yaml``) so the
service can be reconfigured without touching code. The file location is taken
from ``CLOCKALIGN_CONFIG`` when set, otherwise the shipped default is used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"


def _home() -> Path:
    return Path(os.environ.get("CLOCKALIGN_HOME", REPO_ROOT / "data"))


def load_config(path: str | os.PathLike[str] | None = None) -> "Config":
    cfg_path = Path(path or os.environ.get("CLOCKALIGN_CONFIG", DEFAULT_CONFIG_PATH))
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    return Config.from_raw(raw, source=cfg_path)


@dataclass(frozen=True)
class PulseCfg:
    frequency_hz: float
    duration_s: float
    window: str
    score_threshold: float
    min_spacing_s: float


@dataclass(frozen=True)
class CorrelationCfg:
    window_s: float
    hop_s: float
    search_half_window_s: float
    min_score: float


@dataclass(frozen=True)
class SyncCfg:
    mode: str
    pulse: PulseCfg
    pair_max_offset_s: float
    correlation: CorrelationCfg


@dataclass(frozen=True)
class RansacCfg:
    iterations: int
    seed: int
    inlier_threshold_s: float
    min_inlier_fraction: float
    min_inliers: int


@dataclass(frozen=True)
class SegmentCfg:
    min_points: int
    discontinuity_jump_s: float
    min_span_s: float


@dataclass(frozen=True)
class FitCfg:
    min_points: int
    min_span_s: float
    ransac: RansacCfg
    max_drift_ppm: float
    segments: SegmentCfg


@dataclass(frozen=True)
class ResampleCfg:
    method: str
    edge_guard_s: float


@dataclass(frozen=True)
class StorageCfg:
    database: str
    artifacts_dir: str
    home: Path = field(default_factory=_home)

    @property
    def database_path(self) -> Path:
        return self.home / self.database

    @property
    def artifacts_path(self) -> Path:
        return self.home / self.artifacts_dir


@dataclass(frozen=True)
class ValidationCfg:
    residual_rms_max_s: float
    drift_ppm_tol: float
    offset_ms_tol: float
    drop_time_tol_s: float


@dataclass(frozen=True)
class MediaCfg:
    stereo_channel_role: tuple[str, str]
    max_sample_rate: int
    sample_rate_mismatch_ppm_max: float


@dataclass(frozen=True)
class AppCfg:
    name: str
    version: str | None
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    app: AppCfg
    media: MediaCfg
    sync: SyncCfg
    fit: FitCfg
    resample: ResampleCfg
    storage: StorageCfg
    validation: ValidationCfg
    source: Path

    @staticmethod
    def from_raw(raw: dict[str, Any], source: Path) -> "Config":
        app = raw.get("app", {})
        media = raw.get("media", {})
        sync = raw.get("sync", {})
        fit = raw.get("fit", {})
        resample = raw.get("resample", {})
        storage = raw.get("storage", {})
        validation = raw.get("validation", {})

        return Config(
            app=AppCfg(
                name=app.get("name", "clockalign"),
                version=app.get("version"),
                host=app.get("host", "127.0.0.1"),
                port=int(app.get("port", 8080)),
            ),
            media=MediaCfg(
                stereo_channel_role=tuple(media.get("stereo_channel_role",
                                                    ["reference", "slave"])),
                max_sample_rate=int(media.get("max_sample_rate", 192000)),
                sample_rate_mismatch_ppm_max=float(
                    media.get("sample_rate_mismatch_ppm_max", 50000)),
            ),
            sync=SyncCfg(
                mode=sync.get("mode", "auto"),
                pulse=PulseCfg(**sync["pulse"]),
                pair_max_offset_s=float(sync.get("pair_max_offset_s", 1.0)),
                correlation=CorrelationCfg(**sync["correlation"]),
            ),
            fit=FitCfg(
                min_points=int(fit.get("min_points", 4)),
                min_span_s=float(fit.get("min_span_s", 3.0)),
                ransac=RansacCfg(**fit["ransac"]),
                max_drift_ppm=float(fit.get("max_drift_ppm", 1000.0)),
                segments=SegmentCfg(**fit["segments"]),
            ),
            resample=ResampleCfg(
                method=resample.get("method", "linear"),
                edge_guard_s=float(resample.get("edge_guard_s", 0.005)),
            ),
            storage=StorageCfg(
                database=storage.get("database", "jobs.db"),
                artifacts_dir=storage.get("artifacts_dir", "artifacts"),
            ),
            validation=ValidationCfg(
                residual_rms_max_s=float(validation.get("residual_rms_max_s", 0.0015)),
                drift_ppm_tol=float(validation.get("drift_ppm_tol", 15.0)),
                offset_ms_tol=float(validation.get("offset_ms_tol", 3.0)),
                drop_time_tol_s=float(validation.get("drop_time_tol_s", 0.05)),
            ),
            source=source,
        )
