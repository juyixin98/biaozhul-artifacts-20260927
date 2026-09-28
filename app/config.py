"""应用配置加载。

配置优先级：环境变量 > JSON 文件 > 内置默认值。
- SPELLCHECK_SETTINGS: settings JSON 的路径
- SPELLCHECK_COSTS:    代价配置 JSON 的路径
- SPELLCHECK_DB_PATH:  覆盖 SQLite 数据库路径（测试与临时部署使用）
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DEFAULTS = {
    "db_path": "data/spellcheck.db",
    "seed_path": "data/seed_lexicon.jsonl",
    "default_threshold": 2.0,
    "uncertainty_margin": 0.5,
    "max_query_length": 32,
    "max_candidates_evaluated": 2000,
    "max_results": 25,
}


@dataclass(frozen=True)
class Settings:
    db_path: str
    seed_path: str
    default_threshold: float
    uncertainty_margin: float
    max_query_length: int
    max_candidates_evaluated: int
    max_results: int

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def db_file(self) -> Path:
        return self.resolve(os.environ.get("SPELLCHECK_DB_PATH", self.db_path))

    @property
    def seed_file(self) -> Path:
        return self.resolve(self.seed_path)

    def to_public_dict(self) -> dict:
        return asdict(self)


def load_settings() -> Settings:
    raw = dict(_DEFAULTS)
    env_path = os.environ.get("SPELLCHECK_SETTINGS")
    if env_path:
        with open(env_path, "r", encoding="utf-8") as fh:
            raw.update(json.load(fh))
    else:
        local = PROJECT_ROOT / "config" / "settings.json"
        if local.exists():
            with open(local, "r", encoding="utf-8") as fh:
                raw.update(json.load(fh))

    threshold = float(raw["default_threshold"])
    margin = float(raw["uncertainty_margin"])
    if threshold < 0:
        raise ValueError("default_threshold 必须非负")
    if margin < 0:
        raise ValueError("uncertainty_margin 必须非负")
    for key in ("max_query_length", "max_candidates_evaluated", "max_results"):
        if int(raw[key]) <= 0:
            raise ValueError(f"{key} 必须为正整数")

    return Settings(
        db_path=str(raw["db_path"]),
        seed_path=str(raw["seed_path"]),
        default_threshold=threshold,
        uncertainty_margin=margin,
        max_query_length=int(raw["max_query_length"]),
        max_candidates_evaluated=int(raw["max_candidates_evaluated"]),
        max_results=int(raw["max_results"]),
    )
