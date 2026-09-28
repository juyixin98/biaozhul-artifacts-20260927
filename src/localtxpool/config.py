"""TOML 配置加载（Python 3.11+ 内置 tomllib，零额外依赖）。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ChainConfig:
    chain_id: int = 31337
    block_gas_limit: int = 10_000_000


@dataclass(frozen=True)
class PoolConfig:
    max_account_pending: int = 16
    max_account_queued: int = 64
    max_global: int = 4096
    max_global_queued: int = 1024
    max_future_nonce: int = 64
    price_bump_pct: int = 10
    ttl_seconds: int = 3600
    sweep_interval_seconds: int = 0


@dataclass(frozen=True)
class StorageConfig:
    path: str = "./data/txpool.db"


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class LogConfig:
    level: str = "INFO"
    json: bool = True


@dataclass(frozen=True)
class Config:
    chain: ChainConfig = field(default_factory=ChainConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path is None:
            return cls()
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return cls(
            chain=ChainConfig(**raw.get("chain", {})),
            pool=PoolConfig(**raw.get("pool", {})),
            storage=StorageConfig(**raw.get("storage", {})),
            api=ApiConfig(**raw.get("api", {})),
            log=LogConfig(**raw.get("log", {})),
        )
