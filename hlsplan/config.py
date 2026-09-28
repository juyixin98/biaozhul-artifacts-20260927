"""配置：从环境变量加载，带本地默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    db_path: str = "./hlsplan.db"
    # 时长冲突判定容差（秒）：不同版本对同一序号可能重述 EXTINF 精度
    duration_tolerance: float = 0.001
    # 播放列表正文大小上限（字节），防止异常大输入
    max_playlist_bytes: int = 4 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("HLSPLAN_DB_PATH", "./hlsplan.db"),
            duration_tolerance=float(os.environ.get("HLSPLAN_DURATION_TOLERANCE", "0.001")),
            max_playlist_bytes=int(os.environ.get("HLSPLAN_MAX_PLAYLIST_BYTES", str(4 * 1024 * 1024))),
        )
