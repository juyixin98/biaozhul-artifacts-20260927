"""教学链配置。

所有路径 / 参数都可通过环境变量覆盖，默认值面向本地教学使用，
不包含任何生产账号或真实业务数据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 协议版本：任何影响执行结果的语义变更都必须提升此版本。
# 收据中的 program_version 即取自这里，用于“执行结果绑定程序版本”。
PROGRAM_VERSION = "teaching-chain-vm/1.0.0"
# 收据规范版本（编码 / 字段布局）
RECEIPT_VERSION = 1

DEFAULT_HOME = Path(os.environ.get("TEACHING_CHAIN_HOME", "./.teaching-chain"))


def _as_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


@dataclass(frozen=True)
class Settings:
    """不可变运行配置。"""

    home: Path = field(default_factory=lambda: DEFAULT_HOME)
    db_path: Path | None = None
    program_version: str = PROGRAM_VERSION
    receipt_version: int = RECEIPT_VERSION
    # API 诊断中保留的最大原始载荷字节数（超出即截断），避免泄露完整敏感数据
    max_diagnostic_payload_bytes: int = 128
    host: str = field(default_factory=lambda: os.environ.get("TEACHING_CHAIN_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _as_int("TEACHING_CHAIN_PORT", 8000))

    def resolved_db_path(self) -> Path:
        if self.db_path is not None:
            return self.db_path
        return self.home / "index.db"


def get_settings(db_path: str | os.PathLike[str] | None = None) -> Settings:
    """构造配置；db_path 显式给出时优先（主要供测试使用临时库）。"""
    return Settings(db_path=Path(db_path) if db_path is not None else None)
