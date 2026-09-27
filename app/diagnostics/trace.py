"""执行轨迹（trace）留存。

每次查询在内存环形缓冲中保存一棵执行轨迹：版本、AST、每个节点的
关键步骤/输入输出/跳过块统计、失败原因与“不确定结论”分区。
失败复现时可凭 request_id 取回，也可通过 HTTP /diagnostics/traces 查看。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


class Trace:
    """一次请求的可解释轨迹容器。"""

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.started_at = time.time()
        self.steps: List[Dict[str, Any]] = []
        self.failures: List[Dict[str, Any]] = []  # 失败原因单列
        self.uncertainties: List[Dict[str, Any]] = []  # 不确定结论单列
        self.summary: Dict[str, Any] = {}

    def add_step(self, stage: str, **detail: Any) -> None:
        self.steps.append({"stage": stage, **detail})

    def add_failure(self, category: str, reason: str, **detail: Any) -> None:
        self.failures.append({"category": category, "reason": reason, **detail})

    def add_uncertainty(self, reason: str, **detail: Any) -> None:
        self.uncertainties.append({"reason": reason, **detail})

    def set_summary(self, **summary: Any) -> None:
        self.summary.update(summary)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "started_at": self.started_at,
            "elapsed_ms": round((time.time() - self.started_at) * 1000, 3),
            "summary": self.summary,
            "steps": self.steps,
            "failures": self.failures,
            "uncertainties": self.uncertainties,
        }


class TraceStore:
    """有界环形缓冲；线程安全。"""

    def __init__(self, capacity: int = 256):
        self._ring: Deque[Trace] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def save(self, trace: Trace) -> None:
        with self._lock:
            self._ring.append(trace)

    def get(self, request_id: str) -> Optional[Trace]:
        with self._lock:
            for trace in reversed(self._ring):
                if trace.request_id == request_id:
                    return trace
        return None

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            traces = list(self._ring)[-limit:]
        return [
            {
                "request_id": t.request_id,
                "summary": t.summary,
                "failure_categories": [f["category"] for f in t.failures],
            }
            for t in reversed(traces)
        ]
