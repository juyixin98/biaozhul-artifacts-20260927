"""运行日志边界 (run journal)。

每次离线回放/测试运行对应一个 :class:`RunJournal`：
* ``run_id`` 固定为 UTC 时间戳 + 短 uuid，用于在问题复现时唯一定位；
* 事件逐条 JSONL 落盘（UTF-8），含阶段、关键中间状态（sum_in/sum_out/fee、
  utxo_root、计数）与判断理由（category/code/message/details）；
* 最终写一条 ``run_summary``：accepted/rejected、失败类别、逐交易判定汇总、
  提交前后的 UTXO 计数与根 —— 失败时 before==after 必须成立。

JSONL 结构本身可直接驱动重放：每条都带 run_id、序号（seq）与 ISO-8601 时间。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable


def new_run_id() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class RunJournal:
    def __init__(
        self,
        run_id: str | None = None,
        *,
        log_dir: str = "logs",
        scenario: str = "unlabeled",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.scenario = scenario
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"{self.run_id}.jsonl")
        self._lock = threading.Lock()
        self._seq = 0
        self.events: list[dict[str, Any]] = []
        self._fh = open(self.path, "w", encoding="utf-8")
        self._write(
            {
                "stage": "run_start",
                "scenario": scenario,
                "metadata": metadata or {},
            }
        )

    def _write(self, event: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        record = {
            "run_id": self.run_id,
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with self._lock:
            self._fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            self._fh.flush()
            self.events.append(record)
        return record

    # 与内核 event_sink 对接
    def kernel_event(self, event: dict[str, Any]) -> None:
        self._write({"source": "kernel", **event})

    def info(self, stage: str, **payload: Any) -> None:
        self._write({"source": "runner", "stage": stage, **payload})

    def state_snapshot(self, stage: str, snap: dict[str, Any]) -> None:
        """记录一次链状态快照（直接接收 :func:`snapshot` 的产物）。"""
        self._write({"source": "runner", "stage": stage, **snap})

    def failure(self, error_dict: dict[str, Any], *, stage: str = "block_rejected") -> None:
        self._write({"source": "runner", "stage": stage, "error": error_dict})

    def finish(
        self,
        *,
        accepted: bool,
        expected_accepted: bool | None,
        match: bool | None,
        before_snapshot: dict[str, Any],
        after_snapshot: dict[str, Any],
        failure: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tx_verdicts = [
            {
                "tx_index": e["tx_index"],
                "txid": e.get("txid"),
                "verdict": "rejected" if e["stage"] == "tx_rejected" else "planned",
                "category": e.get("category"),
                "code": e.get("code"),
                "reason": e.get("reason"),
            }
            for e in self.events
            if e.get("stage") in ("tx_planned", "tx_rejected")
        ]
        comparable_keys = ("height", "tip_block_id", "utxo_count", "utxo_root", "block_count")
        preserved: bool | None
        if not accepted:
            preserved = all(
                before_snapshot.get(k) == after_snapshot.get(k)
                for k in comparable_keys
            )
        else:
            preserved = None
        summary = {
            "stage": "run_summary",
            "accepted": accepted,
            "expected_accepted": expected_accepted,
            "oracle_match": match,
            "failure": failure,
            "before": before_snapshot,
            "after": after_snapshot,
            "state_preserved_on_failure": preserved,
            "tx_verdicts": tx_verdicts,
            "extra": extra or {},
        }
        self._write({"source": "runner", **summary})
        with self._lock:
            self._fh.close()
        return summary

    @staticmethod
    def read(path: str) -> list[dict[str, Any]]:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def snapshot(store: Any, label: str) -> dict[str, Any]:
    """采集可比较的链状态快照（计数 + UTXO 根 + tip + 块数）。"""
    tip = store.tip()
    return {
        "label": label,
        "height": None if tip is None else tip[0],
        "tip_block_id": None if tip is None else tip[1].hex(),
        "utxo_count": store.utxo_count(),
        "utxo_root": store.utxo_root().hex(),
        "block_count": store.block_count(),
    }


def assert_state_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    """失败场景的核心断言：比对除 label 外全部状态字段。"""
    keys = ("height", "tip_block_id", "utxo_count", "utxo_root", "block_count")
    for k in keys:
        assert before[k] == after[k], f"拒绝后状态发生变化: {k}: {before[k]} -> {after[k]}"


def iter_summary_records(records: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    return (r for r in records if r.get("stage") == "run_summary")
