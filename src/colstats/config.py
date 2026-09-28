"""独立配置模块。

配置来源（优先级从高到低）：
1. 环境变量 ``COLSTATS_<SECTION>_<KEY>``（布尔值接受 1/true/yes/on）；
2. 显式传入的 TOML 文件路径；
3. 仓库根目录下的 ``config.toml``；
4. 代码内默认值。

本模块不依赖项目内任何其它模块，可单独导入测试。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

_ENV_PREFIX = "COLSTATS_"
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: str = "data/audit.db"


@dataclass(frozen=True)
class AuditConfig:
    expose_values: bool = False
    max_pages_per_file: int = 100_000
    supported_physical_types: tuple[str, ...] = (
        "BOOLEAN",
        "INT32",
        "INT64",
        "FLOAT",
        "DOUBLE",
        "BYTE_ARRAY",
    )


@dataclass(frozen=True)
class LogConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    service: ServiceConfig = field(default_factory=ServiceConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def with_overrides(self, **changes: object) -> "Config":
        """返回覆盖部分字段后的副本（支持 service.port 这类点分键）。"""
        sections: dict[str, dict[str, object]] = {}
        for dotted, value in changes.items():
            section, _, key = dotted.partition(".")
            sections.setdefault(section if key else "service", {})[
                key if key else section
            ] = value
        current = {
            "service": replace(self.service, **sections.get("service", {})),
            "audit": replace(self.audit, **sections.get("audit", {})),
            "log": replace(self.log, **sections.get("log", {})),
        }
        return replace(self, **current)


def _coerce(current: object, raw: str) -> object:
    """把字符串环境变量转换成与当前字段相同的类型。"""
    if isinstance(current, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"无法把 {raw!r} 解析为布尔值")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, tuple):
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    return raw


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置：TOML + 环境变量覆盖。"""
    cfg = Config()
    toml_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    raw: dict[str, dict[str, object]] = {}
    if toml_path.is_file():
        with toml_path.open("rb") as fh:
            loaded = tomllib.load(fh)
        raw.update(loaded)

    cfg = Config(
        service=ServiceConfig(**{**vars(cfg.service), **raw.get("service", {})}),
        audit=AuditConfig(
            **{
                **vars(cfg.audit),
                **{
                    k: tuple(v) if k == "supported_physical_types" else v
                    for k, v in raw.get("audit", {}).items()
                },
            }
        ),
        log=LogConfig(**{**vars(cfg.log), **raw.get("log", {})}),
    )

    # 环境变量覆盖
    overrides: dict[str, object] = {}
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        parts = env_key[len(_ENV_PREFIX):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, key = parts
        current = getattr(getattr(cfg, section, None), key, None)
        if current is None:
            continue
        overrides[f"{section}.{key}"] = _coerce(current, env_val)
    if overrides:
        cfg = cfg.with_overrides(**overrides)
    return cfg
