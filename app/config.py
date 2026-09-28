"""配置层：所有可调参数集中于此，可由环境变量覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"环境变量 {name} 必须是整数，得到 {raw!r}") from e


@dataclass(frozen=True)
class Settings:
    db_path: str
    host: str
    port: int
    log_dir: str
    fixture_path: str
    # 透传给 ABI 内核的硬上限（默认值与 app/abi/types.py 保持一致）
    max_decode_bytes: int
    max_array_elements: int

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("ABI_DB_PATH", "data/chain.db"),
            host=os.environ.get("ABI_HOST", "127.0.0.1"),
            port=_env_int("ABI_PORT", 8000),
            log_dir=os.environ.get("ABI_LOG_DIR", "test-logs"),
            fixture_path=os.environ.get(
                "ABI_FIXTURE_PATH", "tests/fixtures/golden_vectors.json"
            ),
            max_decode_bytes=_env_int("ABI_MAX_DECODE_BYTES", 1 << 20),
            max_array_elements=_env_int("ABI_MAX_ARRAY_ELEMENTS", (1 << 20) // 32),
        )


settings = Settings.from_env()
