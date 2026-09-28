"""服务配置。

配置来源（优先级从高到低）:
1. 环境变量，前缀 ``LTXP_``，嵌套字段用 ``__`` 分隔
   （例如 ``LTXP_POOL__MAX_TRANSACTIONS=2000``）。
2. YAML 配置文件（默认 ``config.yaml``，可用环境变量 ``LTXP_CONFIG_FILE`` 指定）。
3. 代码内默认值。

所有值均为本地合成链参数，不依赖任何生产网络。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_VERSION = "1.0.0"
SCHEMA_VERSION = 1
# 本地合成链的 EIP-155 chain id（31337 是常见的本地 Anvil/Hardhat 约定值）。
LOCAL_CHAIN_ID = 31337


class ChainConfig(BaseModel):
    chain_id: int = LOCAL_CHAIN_ID
    genesis_number: int = 0


class GasConfig(BaseModel):
    block_gas_limit: int = 30_000_000
    tx_intrinsic_gas: int = 21_000
    data_gas_per_byte: int = 16
    max_data_bytes: int = 16 * 1024
    min_gas_price_wei: int = 1_000_000_000  # 1 gwei，低于该值的交易在入口被拒绝


class PoolConfig(BaseModel):
    # 交易池总容量（pending + queued 有效交易条数）
    max_transactions: int = 2_048
    # 单账户 queued 槽位上限（超出 nonce 跨度）
    max_queued_per_sender: int = 16
    # 单账户总有效交易上限（pending+queued），防止单账户占满全局池
    max_transactions_per_sender: int = 64
    # RBF 替换时，新交易 gas_price 相对旧交易的最小涨幅（百分比）
    replacement_price_bump_pct: int = 10
    # pending 交易存活秒数（模拟时钟；0 或负数表示不过期）
    pending_ttl_seconds: int = 300
    # 淘汰时先驱逐 queued；queued 为空仍不足时才驱逐 pending
    evict_pending_as_last_resort: bool = True


class FinalityConfig(BaseModel):
    # 一个区块在链头下方多少深度即视为最终确定，不可回滚
    confirmation_depth: int = 3


class Config(BaseSettings):
    """根配置。"""

    model_config = SettingsConfigDict(
        env_prefix="LTXP_",
        env_nested_delimiter="__",
        env_file=None,
        extra="ignore",
    )

    database_path: str = "data/txpool.db"
    log_level: str = "INFO"
    chain: ChainConfig = Field(default_factory=ChainConfig)
    gas: GasConfig = Field(default_factory=GasConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    finality: FinalityConfig = Field(default_factory=FinalityConfig)


def _deep_update(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(config_path: str | os.PathLike[str] | None = None) -> Config:
    """从 YAML（若存在）+ 环境变量加载配置。"""

    path: str | None = config_path or os.environ.get(
        "LTXP_CONFIG_FILE", "config.yaml"
    )
    raw: dict[str, Any] = {}
    if path and Path(path).is_file():
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件 {path} 顶层必须是映射结构")
        raw = loaded
    return Config(**raw)
