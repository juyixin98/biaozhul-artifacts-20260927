"""配置：所有核心参数集中在此，环境变量可覆盖，禁止在算法里散落魔数。

环境变量前缀 ``JITTER_``，例如 ``JITTER_MIN_DELAY_US=30000``。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(f"JITTER_{name}")
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(f"JITTER_{name}")
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class PlannerConfig:
    """抖动缓冲/播放计划参数（微秒为接收端时钟单位）。

    自适应目标延迟（每话峰首包更新一次）::

        need_i   = k * jitter_ewma          （本话峰观测到的需求）
        persist  = 平滑因子（delay_persistence）跨话峰保持：
                   D_0 = need_0
                   D_i = persist * D_{i-1} + (1-persist) * need_i
        target_i = clamp(D_i, min_delay_us, max_delay_us)

    冷启动（观测样本不足）时需求取 ``max(need, min_delay_us)``，避免目标坍缩到 0。
    其中 ``jitter_ewma`` 按 RFC 3550 A.8 维护：
    ``J += alpha * (|D| - J)``，``D`` 为相邻包间传输时间差。
    """

    clock_rate: int = 8000
    samples_per_packet: int = 160  # 8kHz / 50fps -> 每包 20ms
    min_delay_us: int = 20_000
    max_delay_us: int = 400_000
    jitter_multiplier: float = 4.0
    jitter_smoothing: float = 1.0 / 16.0  # RFC 3550 的 1/16
    drift_smoothing: float = 1.0 / 16.0  # 接收/发送时钟比 EWMA
    delay_persistence: float = 0.75  # 跨话峰保持已学到的延迟需求
    drift_warmup_packets: int = 8  # 漂移比预热期内不缩短目标延迟
    adaptive: bool = True
    fixed_delay_us: int = 40_000  # 固定延迟基线
    max_buffer_packets: int = 250  # 缓冲硬上界，超过按 overflow 丢弃

    @property
    def frame_us_nominal(self) -> int:
        return int(self.samples_per_packet * 1_000_000 / self.clock_rate)

    @classmethod
    def from_env(cls) -> "PlannerConfig":
        return cls(
            clock_rate=_env_int("CLOCK_RATE", 8000),
            samples_per_packet=_env_int("SAMPLES_PER_PACKET", 160),
            min_delay_us=_env_int("MIN_DELAY_US", 20_000),
            max_delay_us=_env_int("MAX_DELAY_US", 400_000),
            jitter_multiplier=_env_float("JITTER_MULTIPLIER", 4.0),
            jitter_smoothing=_env_float("JITTER_SMOOTHING", 1.0 / 16.0),
            drift_smoothing=_env_float("DRIFT_SMOOTHING", 1.0 / 16.0),
            delay_persistence=_env_float("DELAY_PERSISTENCE", 0.75),
            drift_warmup_packets=_env_int("DRIFT_WARMUP_PACKETS", 8),
            max_buffer_packets=_env_int("MAX_BUFFER_PACKETS", 250),
        )


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/jitter.db"
    log_level: str = "INFO"
    log_json: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.getenv("JITTER_DB_PATH", "data/jitter.db"),
            log_level=os.getenv("JITTER_LOG_LEVEL", "INFO").upper(),
            log_json=os.getenv("JITTER_LOG_JSON", "1") not in ("0", "false", "False"),
        )


def config_snapshot(cfg: PlannerConfig) -> dict:
    return asdict(cfg)
