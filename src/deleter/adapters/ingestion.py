"""载入格式适配：把外部输入（inline JSON / 本地 JSON / 本地 Parquet）归一化。

只接受两类来源：
- inline：请求体内直接给行数据（合成夹具）；
- inbox：服务工作区 inbox/ 目录下的本地文件，文件名需事先存在。

不访问网络，不接受任意绝对路径。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..errors import InputError, ResourceLimitError
from . import parquet_format as pf


class IngestionService:
    def __init__(self, inbox_dir: str | os.PathLike[str], max_rows_per_load: int = 200_000) -> None:
        self.inbox_dir = Path(inbox_dir)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows_per_load

    def load_rows(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        kind = source.get("kind")
        if kind == "inline":
            rows = source.get("rows")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise InputError("inline 来源要求 rows 为对象数组")
        elif kind == "inbox":
            name = source.get("name")
            if not isinstance(name, str) or not name or os.path.isabs(name) or ".." in Path(name).parts:
                raise InputError("inbox 来源要求 name 为 inbox 目录下的相对文件名")
            path = (self.inbox_dir / name).resolve()
            if self.inbox_dir.resolve() not in path.parents and path.parent != self.inbox_dir.resolve():
                raise InputError("文件必须位于 inbox 目录内")
            if not path.exists():
                raise InputError("inbox 文件不存在", name=name)
            rows = self._read_file(path)
        else:
            raise InputError("未知来源类型", allowed=["inline", "inbox"])

        if len(rows) > self.max_rows:
            raise ResourceLimitError(
                "单次载入行数超过配额",
                limit=self.max_rows, actual=len(rows),
            )
        return rows

    def _read_file(self, path: Path) -> list[dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise InputError("JSON 文件解析失败", cause=str(exc)) from exc
            if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
                raise InputError("JSON 文件内容必须是对象数组")
            return data
        if suffix in (".parquet", ".pq"):
            return pf.table_to_pylist(pf.read_parquet(path))
        raise InputError("不支持的文件格式（支持 .json/.parquet）", suffix=suffix)

    def validate_against_schema(self, rows: list[dict[str, Any]], columns: dict[str, str]) -> None:
        """列集合必须与表 schema 完全一致（少列/多列均拒绝）。"""
        declared = set(columns)
        for i, row in enumerate(rows):
            extra = set(row) - declared
            missing = declared - set(row)
            if extra:
                raise InputError(f"第 {i} 行存在 schema 外的列", extra=sorted(extra))
            if missing:
                raise InputError(f"第 {i} 行缺少列", missing=sorted(missing))
        # 构造一遍 Arrow，触发逐值类型校验
        pf.pylist_to_table(rows, columns)
