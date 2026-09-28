"""集中式可配置项（12-factor 风格，全部带本地默认值）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("OT_DB_PATH", "ot.db")
    # 文档最大字符数（按 Unicode 码点计）
    max_doc_chars: int = _env_int("OT_MAX_DOC_CHARS", 1_000_000)
    # 单个操作最多插入/删除的字符数
    max_op_chars: int = _env_int("OT_MAX_OP_CHARS", 100_000)
    # 单次拉取最多返回多少历史操作
    max_pull_batch: int = _env_int("OT_MAX_PULL_BATCH", 1000)
    # 诊断开关：注入一次计算失败（测试 compute_failed 类别）
    fault_injection: bool = os.environ.get("OT_FAULT_INJECTION", "") not in ("", "0", "false")


settings = Settings()
