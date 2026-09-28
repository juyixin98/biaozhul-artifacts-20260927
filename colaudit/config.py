"""独立配置: 全部参数可被 YAML 文件或 COLAUDIT_* 环境变量覆盖。

环境变量优先级: 环境变量 > YAML 配置文件 > 代码默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

try:  # PyYAML 是可选依赖, 缺失时仅禁用 YAML 配置
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    #: 运行时根目录 (SQLite 数据库等)
    home: Path = Path(".colaudit")
    #: 夹具/数据集根目录, 其下每个子目录是一个数据集
    fixtures_dir: Path = Path("fixtures")
    #: SQLite 数据库路径 (默认落在 home 下)
    db_path: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    #: 诊断与审计报告中的敏感字段是否脱敏
    mask_sensitive: bool = True
    log_level: str = "INFO"

    @property
    def effective_db_path(self) -> Path:
        return self.db_path or self.home / "audit.db"

    @classmethod
    def load(cls, config_path: str | os.PathLike[str] | None = None) -> "Settings":
        raw: dict[str, Any] = {}

        path = config_path or os.environ.get("COLAUDIT_CONFIG")
        if path and yaml is None:
            raise RuntimeError(
                f"配置文件 {path} 需要 PyYAML, 但未安装 PyYAML"
            )
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件 {path} 顶层必须是映射")
            raw.update(loaded)

        # 环境变量覆盖, 布尔/路径/整型做对应转换
        bool_keys = {"mask_sensitive"}
        int_keys = {"port"}
        path_keys = {"home", "fixtures_dir", "db_path"}
        for f in fields(cls):
            env = os.environ.get(f"COLAUDIT_{f.name.upper()}")
            if env is None:
                continue
            if f.name in bool_keys:
                raw[f.name] = _as_bool(env)
            elif f.name in int_keys:
                raw[f.name] = int(env)
            elif f.name in path_keys:
                raw[f.name] = Path(env)
            else:
                raw[f.name] = env

        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"未知配置项: {sorted(unknown)}")
        return cls(**raw)

    def ensure_dirs(self) -> "Settings":
        self.home.mkdir(parents=True, exist_ok=True)
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        return self
