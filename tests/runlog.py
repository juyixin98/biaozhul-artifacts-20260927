"""可重放测试日志：运行编号、关键中间状态、判定理由。

每个测试 run 记录：
* run_id（稳定可复算的编号）
* 输入（基线文本、操作对/序列、接收顺序）
* 关键中间状态（每步两侧文档的字符串与 token 身份序列）
* 判定（PASS/FAIL）与理由

失败时在 AssertionError 里附完整 JSONL，可直接重放。
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field

_LOG_PATH = os.environ.get("OT_TEST_LOG", "test_runs.jsonl")


@dataclass
class RunLogger:
    path: str | None = None
    _buf: io.StringIO = field(default_factory=io.StringIO)
    count: int = 0

    def __post_init__(self):
        if self.path is None:
            self.path = _LOG_PATH

    def record(self, *, suite: str, inputs: dict, steps: list[dict],
               verdict: str, reason: str, expect: object = None,
               actual: object = None) -> str:
        self.count += 1
        run_id = f"{suite}-{self.count:05d}"
        entry = {
            "run_id": run_id,
            "suite": suite,
            "inputs": inputs,
            "steps": steps,
            "expect": expect,
            "actual": actual,
            "verdict": verdict,
            "reason": reason,
        }
        self._buf.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return run_id

    def flush(self) -> str:
        data = self._buf.getvalue()
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(data)
        return data
