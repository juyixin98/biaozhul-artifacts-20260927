"""配置加载：读取扁平 config.yaml，并允许同名大写环境变量覆盖。

为避免引入额外依赖，这里只解析本项目配置文件使用的扁平 ``key: value`` 语法
（含 ``#`` 注释），不支持嵌套 YAML。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_BOOL_TRUE = {"1", "true", "yes", "on"}


def _coerce(value: str, old: object) -> object:
    """按配置文件里旧值的类型把字符串转成 int/float/bool/str。"""
    v = value.strip()
    if isinstance(old, bool):
        return v.lower() in _BOOL_TRUE
    if isinstance(old, int):
        return int(v)
    if isinstance(old, float):
        return float(v)
    return v


def _parse_simple_yaml(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    max_samples_per_job: int = 5_000_000
    max_upload_bytes: int = 32 * 1024 * 1024
    stream_chunk_samples: int = 4000
    default_min_silence_ms: int = 300
    default_min_activity_ms: int = 100
    default_pad_ms: int = 50
    default_merge_gap_ms: int = 120
    default_enter_threshold: float = 0.02
    default_exit_threshold: float = 0.05
    data_dir: str = "./data"
    runs_log_path: str = "./data/runs.jsonl"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Settings":
        kwargs: dict[str, object] = {}
        default = cls()
        if path is None:
            path = Path(__file__).resolve().parent.parent / "config.yaml"
        p = Path(path)
        if p.exists():
            for key, raw in _parse_simple_yaml(p.read_text(encoding="utf-8")).items():
                if hasattr(default, key):
                    kwargs[key] = _coerce(raw, getattr(default, key))
        # 环境变量优先级最高
        for field in default.__dataclass_fields__:
            env = os.environ.get(field.upper())
            if env is not None:
                kwargs[field] = _coerce(env, getattr(default, field))
        return cls(**kwargs)  # type: ignore[arg-type]
