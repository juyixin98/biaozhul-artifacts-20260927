"""结构化诊断日志：JSONL，一行一个事件。

每条记录都带 run_id（关联同一次查询的全部阶段）、
searchdsl 版本、阶段名与判定依据；失败不会被吞掉——
失败事件照常落盘，异常继续向上抛。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


class Diagnostics:
    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, run_id: str, stage: str, status: str,
             detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": run_id,
            "searchdsl_version": __version__,
            "stage": stage,
            "status": status,
        }
        if detail:
            record["detail"] = detail
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
