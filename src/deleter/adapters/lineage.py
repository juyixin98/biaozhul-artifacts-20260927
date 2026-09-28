"""文件血缘侧录（lineage sidecar）。

重写不修改用户列，insert_seq 也不允许成为业务列，因此每个文件版本的
行序列号与来源映射保存在 sqlite 之外的 JSON 侧录中：

    data/tables/<table>/lineage/<file>.v<version>.json
        {
          "file_id", "version", "insert_seqs": [..],
          "source": {"kind": "load|rewrite",
                     "parents": [[file_id, version, row_number], ...]}
        }

parents[i] 表示当前版本第 i 行来自哪个父版本的哪一行（重写压缩了
物理行号，幸存行行号保持不变，但侧录仍逐行留痕，便于复核）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class LineageStore:
    def __init__(self, lineage_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(lineage_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, file_id: str, version: int) -> Path:
        return self.dir / f"{file_id}.v{version}.json"

    def write(
        self,
        file_id: str,
        version: int,
        insert_seqs: list[int],
        source: dict[str, Any],
    ) -> None:
        payload = {
            "file_id": file_id,
            "version": version,
            "insert_seqs": insert_seqs,
            "source": source,
        }
        path = self._path(file_id, version)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def read(self, file_id: str, version: int) -> dict[str, Any]:
        return json.loads(self._path(file_id, version).read_text(encoding="utf-8"))

    def insert_seqs(self, file_id: str, version: int) -> list[int]:
        return self.read(file_id, version)["insert_seqs"]

    def row_count(self, file_id: str, version: int) -> int:
        return len(self.read(file_id, version)["insert_seqs"])
