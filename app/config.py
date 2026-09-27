"""配置层：固定 Unicode 数据版本、限制项与运行参数。

Unicode 版本是本服务的核心契约：
- GCB_TABLE_VERSION：分段库 grapheme 0.6.0 使用的 GraphemeClusterBreak 属性表版本（13.0.0）
- UNIDATA_VERSION  ：Python 内置 unicodedata 的数据版本（3.12 → 15.0.0），
                     用于字符名/类别等诊断信息
构建索引时把这两个版本写入存储；加载时若运行环境不匹配则拒绝，避免“同原文不同边界”。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 构建期钉死（requirements.lock 中锁定 grapheme==0.6.0）
PINNED_GCB_TABLE_VERSION = "13.0.0"
PINNED_UNIDATA_VERSION = "15.0.0"


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("TEXTIDX_DB", "data/textindex.db")
    log_level: str = os.environ.get("TEXTIDX_LOG_LEVEL", "INFO")

    # 资源限制（资源耗尽类别用）
    max_bytes: int = _env_int("TEXTIDX_MAX_BYTES", 2 * 1024 * 1024)          # 单文档 2 MiB
    max_codepoints: int = _env_int("TEXTIDX_MAX_CODEPOINTS", 1_000_000)
    max_clusters: int = _env_int("TEXTIDX_MAX_CLUSTERS", 200_000)
    max_documents: int = _env_int("TEXTIDX_MAX_DOCUMENTS", 10_000)

    # 增量更新后是否强制与完整重建逐数组比对（测试与生产默认均开启）
    verify_incremental: bool = os.environ.get("TEXTIDX_VERIFY_INCREMENTAL", "1") != "0"

    gcb_table_version: str = PINNED_GCB_TABLE_VERSION
    unidata_version: str = PINNED_UNIDATA_VERSION


settings = Settings()
