"""配置。

关键不变量：``max_reorg_depth == finality_depth``——回滚深度边界以内（即不会
撤掉任何已最终确定区块）的重组允许，更深的重组由内核按模型明确拒绝
（见 consensus.is_reorg_allowed）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "data/reorgindex.sqlite3"
# 最终性深度 D：高度满足 tip_height - block_height >= D 的区块视为最终确定。
DEFAULT_FINALITY_DEPTH = 6
# 权重相等时保留当前权威链（不切换）。
DEFAULT_TIE_KEEP_CANONICAL = True


@dataclass(frozen=True)
class Settings:
    db_path: str = DEFAULT_DB_PATH
    finality_depth: int = DEFAULT_FINALITY_DEPTH
    tie_keep_canonical: bool = DEFAULT_TIE_KEEP_CANONICAL
    log_level: str = "INFO"
    # 诊断日志中地址/公钥保留前后各多少字符，其余掩码。
    redact_keep: int = 6

    @property
    def max_reorg_depth(self) -> int:
        return self.finality_depth

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        depth = int(env.get("REORG_FINALITY_DEPTH", DEFAULT_FINALITY_DEPTH))
        if depth < 1:
            raise ValueError("REORG_FINALITY_DEPTH 必须 >= 1")
        return Settings(
            db_path=env.get("REORG_DB_PATH", DEFAULT_DB_PATH),
            finality_depth=depth,
            tie_keep_canonical=env.get("REORG_TIE_KEEP_CANONICAL", "1") not in ("0", "false", "False", ""),
            log_level=env.get("REORG_LOG_LEVEL", "INFO"),
            redact_keep=int(env.get("REORG_REDACT_KEEP", "6")),
        )

    @staticmethod
    def from_file(path: str | Path) -> "Settings":
        """最小 KEY=VALUE 配置文件，# 开头为注释。"""

        values: dict[str, str] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip()
        return Settings.from_env(values)
