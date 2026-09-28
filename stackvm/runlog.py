"""可重放的结构化运行日志。

每次测试/服务验证生成一个运行编号 run_id（UTC 时间 + 短随机后缀），记录：
- 请求的交易指纹、脚本反汇编；
- VM 每条指令的 PC、操作码、预算剩余、栈/备用栈状态、active 分支；
- 最终判定（OK / 失败类别）与判定理由；
- 用于跨库比对的验签中间结果。

日志写入 <runlog_dir>/test-runs/ 或 /service/ 下的 JSONL + 摘要 JSON，
足够“拿着日志重放同一个问题”。
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path


def new_run_id(prefix: str = "run") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}-{secrets.token_hex(3)}"


class RunLogger:
    """收集一次运行中的事件；失败不影响主流程（日志写入异常被吞掉并标记）。"""

    def __init__(self, run_id: str | None = None, *, kind: str = "test-runs",
                 log_dir: str | os.PathLike[str] = "runlogs", max_events: int = 4000):
        self.run_id = run_id or new_run_id()
        self.kind = kind
        self.log_dir = Path(log_dir)
        self.events: list[dict] = []
        self.max_events = max_events
        self.dropped = 0
        self.summary: dict = {}

    def event(self, name: str, **payload) -> None:
        if len(self.events) >= self.max_events:
            self.dropped += 1
            return
        rec = {"seq": len(self.events), "ts": datetime.now(timezone.utc).isoformat(),
               "event": name, **payload}
        self.events.append(rec)

    def set_summary(self, summary: dict) -> None:
        self.summary = dict(summary)

    def flush(self) -> dict:
        d = self.log_dir / self.kind / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        jsonl = d / "events.jsonl"
        with open(jsonl, "w", encoding="utf-8") as fh:
            for rec in self.events:
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        summary = dict(self.summary)
        summary["run_id"] = self.run_id
        summary["event_count"] = len(self.events)
        summary["events_dropped"] = self.dropped
        with open(d / "summary.json", "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        latest = self.log_dir / self.kind / "LATEST"
        try:
            Path(latest).write_text(self.run_id + "\n", encoding="utf-8")
        except OSError:
            pass
        return {"dir": str(d), "events": str(jsonl), "summary": str(d / "summary.json")}
