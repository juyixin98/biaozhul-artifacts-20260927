"""运行配置。

全部配置来自环境变量（可由 configs/dev.env 加载），均为本地合成环境配置，
不依赖任何生产账号。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: str
    signing_key_path: str
    signing_key_hex: str | None
    key_len: int
    depth: int
    host: str
    port: int
    log_redact: bool

    def validate(self) -> None:
        if self.key_len <= 0:
            raise ValueError("SMT_KEY_LEN 必须为正整数")
        if self.depth <= 0 or self.depth > self.key_len * 8:
            # 键的位宽必须能覆盖树深：深度不得超过键本身提供的位数
            raise ValueError(
                f"SMT_DEPTH={self.depth} 超出键位宽 {self.key_len * 8}（SMT_KEY_LEN={self.key_len}）"
            )
        if not self.db_path:
            raise ValueError("SMT_DB_PATH 不能为空")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def get_settings(env: dict[str, str] | None = None) -> Settings:
    """从环境变量读取配置（测试可注入 env 字典）。"""
    src = os.environ if env is None else env
    settings = Settings(
        db_path=src.get("SMT_DB_PATH", "data/smt.db"),
        signing_key_path=src.get("SMT_SIGNING_KEY_PATH", "configs/dev_signing_key.pem"),
        signing_key_hex=(src.get("SMT_SIGNING_KEY_HEX") or None),
        key_len=_env_int("SMT_KEY_LEN", 32) if env is None else int(src.get("SMT_KEY_LEN", 32)),
        depth=_env_int("SMT_DEPTH", 256) if env is None else int(src.get("SMT_DEPTH", 256)),
        host=src.get("SMT_HOST", "127.0.0.1"),
        port=int(src.get("SMT_PORT", "8080")),
        log_redact=src.get("SMT_LOG_REDACT", "1") not in ("0", "", "false", "False"),
    )
    settings.validate()
    return settings


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
