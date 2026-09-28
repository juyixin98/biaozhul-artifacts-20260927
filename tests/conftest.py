"""测试日志：为每次测试运行分配单调运行编号，记录可重放的关键信息。

产物：
  test-runs/latest.jsonl              本次运行的逐条事件流
  test-runs/run-<编号>.jsonl          归档副本（按运行编号）
  test-runs/index.json                历次运行摘要

每条事件至少含：run_id、seq、name、phase、输入（短串操作）、关键中间状态、
断言理由、结果（pass/fail/error）与失败类别。失败用例因此可用 run_id + name
在日志中重放。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_RUNS_DIR = Path(__file__).resolve().parent.parent / "test-runs"
_LOCK = threading.Lock()


class RunLogger:
    def __init__(self, runs_dir: Path | None = None):
        self.dir = runs_dir or _RUNS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = self._allocate_run_id()
        self.started = time.time()
        self.events: list[dict] = []
        self.counts = {"pass": 0, "fail": 0, "error": 0}
        self.path = self.dir / f"run-{self.run_id:04d}.jsonl"

    def _allocate_run_id(self) -> int:
        with _LOCK:
            index = self.dir / "index.json"
            data = {}
            if index.exists():
                data = json.loads(index.read_text(encoding="utf-8"))
            nxt = int(data.get("last_run_id", 0)) + 1
            data["last_run_id"] = nxt
            data.setdefault("runs", [])
            data["runs"].append({"run_id": nxt, "started_at": time.time()})
            data["runs"] = data["runs"][-200:]
            index.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return nxt

    def event(self, name: str, phase: str, *, verdict: str, reason: str,
              inputs: dict | None = None, states: dict | None = None,
              category: str | None = None, error: str | None = None) -> None:
        rec = {
            "run_id": self.run_id,
            "ts": round(time.time() - self.started, 6),
            "seq": len(self.events) + 1,
            "name": name,
            "phase": phase,
            "verdict": verdict,
            "reason": reason,
            "inputs": inputs or {},
            "states": states or {},
        }
        if category:
            rec["category"] = category
        if error:
            rec["error"] = error
        self.events.append(rec)
        if verdict in self.counts:
            self.counts[verdict] += 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def finalize(self) -> dict:
        summary = {
            "run_id": self.run_id,
            "duration_s": round(time.time() - self.started, 3),
            "counts": dict(self.counts),
            "events": len(self.events),
            "log": os.path.basename(str(self.path)),
        }
        latest = self.dir / "latest.jsonl"
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as src, \
                    latest.open("w", encoding="utf-8") as dst:
                dst.write(src.read())
        else:
            latest.write_text("", encoding="utf-8")
        return summary


# pytest 插件钩子：每个 session 一个 logger
import pytest  # noqa: E402

_LOGGER: "RunLogger | None" = None


def pytest_configure(config):
    global _LOGGER
    _LOGGER = RunLogger()
    config._ot_logger = _LOGGER
    print(f"\n[ot-log] run_id={_LOGGER.run_id} log={_LOGGER.path}")


def pytest_runtest_logreport(report):
    if _LOGGER is None or report.when != "call":
        return
    if report.passed:
        _LOGGER.event(report.nodeid, "call", verdict="pass",
                      reason="assertions held")
    elif report.failed:
        _LOGGER.event(report.nodeid, "call", verdict="fail",
                      reason="assertion failed", error=str(report.longrepr))


def pytest_sessionfinish(session, exitstatus):
    if _LOGGER is not None:
        summary = _LOGGER.finalize()
        print(f"[ot-log] summary={json.dumps(summary, ensure_ascii=False)}")


import pytest  # noqa: E402


@pytest.fixture
def otlog(request):
    """用例向运行日志写入"关键中间状态 + 判定理由"。"""
    def _emit(phase, *, reason, inputs=None, states=None, verdict="info",
              category=None):
        if _LOGGER is None:
            return
        _LOGGER.event(request.node.nodeid, phase, verdict=verdict,
                      reason=reason, inputs=inputs, states=states,
                      category=category)
    return _emit
