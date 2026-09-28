"""配置层：全部可经环境变量覆盖，默认值即本地合成夹具配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "data/jobs.db"
    fixtures_dir: str = "fixtures"
    log_dir: str = "logs"
    # 是否允许 ffprobe 适配器解析真实媒体文件（默认仅 sidecar 合成夹具）
    allow_ffprobe: bool = False
    # 输出时间基分母硬上限（MP4 timescale 为 uint32）
    max_timescale: int = 0xFFFFFFFF
    # MPEG-TS 固定时钟
    mpegts_clock: int = 90000
    audio_default_timescale: int = 48000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        def as_bool(v: str | None) -> bool:
            return (v or "").strip().lower() in {"1", "true", "yes", "on"}

        def as_int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw not in (None, "") else default

        return cls(
            host=os.environ.get("MEDIACONCAT_HOST", cls.host),
            port=as_int("MEDIACONCAT_PORT", cls.port),
            db_path=os.environ.get("MEDIACONCAT_DB", cls.db_path),
            fixtures_dir=os.environ.get("MEDIACONCAT_FIXTURES", cls.fixtures_dir),
            log_dir=os.environ.get("MEDIACONCAT_LOG_DIR", cls.log_dir),
            allow_ffprobe=as_bool(os.environ.get("MEDIACONCAT_ALLOW_FFPROBE")),
            max_timescale=as_int("MEDIACONCAT_MAX_TIMESCALE", cls.max_timescale),
            mpegts_clock=as_int("MEDIACONCAT_MPEGTS_CLOCK", cls.mpegts_clock),
            audio_default_timescale=as_int(
                "MEDIACONCAT_AUDIO_TIMESCALE", cls.audio_default_timescale
            ),
            log_level=os.environ.get("MEDIACONCAT_LOG_LEVEL", cls.log_level).upper(),
        )


settings = Settings.from_env()
