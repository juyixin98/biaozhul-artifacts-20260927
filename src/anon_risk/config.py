"""配置层：TOML 默认值 + 环境变量覆盖（12-factor 风格），独立可测。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "ANON_RISK_"
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.toml"


@dataclass(frozen=True)
class AppCfg:
    name: str = "anon-risk"
    version: str = "1.0.0"
    metric_version: str = "1.0.0"
    schema_version: int = 1


@dataclass(frozen=True)
class StorageCfg:
    data_dir: str = "data"
    runs_subdir: str = "runs"
    audit_db: str = "data/audit.db"
    log_dir: str = "logs"


@dataclass(frozen=True)
class SecurityCfg:
    master_key_env: str = "ANON_RISK_MASTER_KEY"
    admin_token_env: str = "ANON_RISK_ADMIN_TOKEN"
    allow_ephemeral_key: bool = True
    hkdf_salt: str = "anon-risk-run-v1"


@dataclass(frozen=True)
class KernelCfg:
    lattice_combo_cap: int = 10000
    risk_medium_factor: int = 2


@dataclass(frozen=True)
class LoggingCfg:
    level: str = "INFO"
    jsonl: bool = True


@dataclass(frozen=True)
class Settings:
    app: AppCfg = field(default_factory=AppCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)
    kernel: KernelCfg = field(default_factory=KernelCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    # 配置来源文件，便于 /version 与日志交代“按什么配置运行”
    source_path: str | None = None
    # 主密钥是否为进程内临时密钥（重启后旧运行不可解密），必须对外可见
    key_ephemeral: bool = False

    def log_level_int(self) -> int:
        import logging
        return getattr(logging, self.logging.level.upper(), logging.INFO)


_SECTIONS = {
    "app": AppCfg,
    "storage": StorageCfg,
    "security": SecurityCfg,
    "kernel": KernelCfg,
    "logging": LoggingCfg,
}


def _coerce(value: str, target_type: type) -> object:
    if target_type is bool:
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"无法把 {value!r} 解析为布尔值")
    if target_type is int:
        return int(value)
    return value


def _apply_env_overrides(data: dict) -> dict:
    """把 ANON_RISK_<SECTION>__<KEY>=... 覆盖到配置字典（就地更新副本）。"""
    data = {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
    for env_name, raw in os.environ.items():
        if not env_name.startswith(ENV_PREFIX):
            continue
        tail = env_name[len(ENV_PREFIX):]
        if "__" not in tail:
            continue
        section, key = tail.split("__", 1)
        section = section.lower()
        key = key.lower()
        if section not in _SECTIONS:
            continue
        datacls = _SECTIONS[section]
        hints = getattr(datacls, "__dataclass_fields__", {})
        if key not in hints:
            continue
        target_type = hints[key].type
        if isinstance(target_type, str):  # python 3.11 下注解可能是字符串
            target_type = {"str": str, "int": int, "bool": bool}.get(target_type, str)
        data.setdefault(section, {})[key] = _coerce(raw, target_type)
    return data


def load_settings(config_path: str | Path | None = None) -> Settings:
    """加载配置：显式路径 > ANON_RISK_CONFIG > 内置默认 TOML。"""
    path = Path(config_path or os.environ.get(ENV_PREFIX + "CONFIG") or DEFAULT_CONFIG)
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    raw = _apply_env_overrides(raw)

    kwargs = {}
    for section, datacls in _SECTIONS.items():
        section_data = raw.get(section, {})
        fields_ = getattr(datacls, "__dataclass_fields__", {})
        unknown = set(section_data) - set(fields_)
        if unknown:
            raise ValueError(f"配置节 [{section}] 含未知键: {sorted(unknown)}")
        kwargs[section] = datacls(**{k: v for k, v in section_data.items() if k in fields_})

    return Settings(source_path=str(path), **kwargs)
