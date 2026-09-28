"""独立配置加载：读取 config/default.toml，允许 STACKVM_<SECTION>_<KEY> 覆盖。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.toml"


@dataclass(frozen=True)
class Limits:
    op_budget: int = 200
    max_element_bytes: int = 255
    max_stack_items: int = 100
    max_script_bytes: int = 4096
    max_if_depth: int = 16
    max_script_depth: int = 2
    max_multisig_n: int = 16
    push_only_unlock: bool = True
    require_clean_stack: bool = True
    max_script_num_bytes: int = 4


@dataclass(frozen=True)
class SighashCfg:
    domain_tag: str = "STACKVM.SIGHASH/1"


@dataclass(frozen=True)
class ChainCfg:
    mint_total_cap: int = 2_100_000_000
    balance_exact: bool = True


@dataclass(frozen=True)
class ServiceCfg:
    host: str = "127.0.0.1"
    port: int = 8332


@dataclass(frozen=True)
class StorageCfg:
    db_path: str = "data/stackvm.db"
    runlog_dir: str = "runlogs"
    genesis_fixture: str = "fixtures/genesis.json"


@dataclass(frozen=True)
class Settings:
    limits: Limits = field(default_factory=Limits)
    sighash: SighashCfg = field(default_factory=SighashCfg)
    chain: ChainCfg = field(default_factory=ChainCfg)
    service: ServiceCfg = field(default_factory=ServiceCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    root: Path = PROJECT_ROOT

    def abspath(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def _coerce(current, raw: str):
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    return raw


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with open(cfg_path, "rb") as fh:
        data = tomllib.load(fh)

    sections = {
        "limits": Limits,
        "sighash": SighashCfg,
        "chain": ChainCfg,
        "service": ServiceCfg,
        "storage": StorageCfg,
    }
    built = {}
    for section, cls in sections.items():
        kwargs = dict(data.get(section, {}))
        prefix = f"STACKVM_{section.upper()}_"
        for env_key, raw in os.environ.items():
            if env_key.startswith(prefix):
                key = env_key[len(prefix):].lower()
                if key in cls.__dataclass_fields__:
                    kwargs[key] = _coerce(cls.__dataclass_fields__[key].default, raw)
        built[section] = cls(**kwargs)

    return Settings(root=PROJECT_ROOT, **built)
