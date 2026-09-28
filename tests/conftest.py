"""pytest 插件：为每个测试运行与每个用例记录可重放的结构化日志。

产物（``test-logs/`` 下，可用 scripts/replay.py 重放）：
* ``run-<ts>-<uuid8>/meta.json``        运行编号、时间、包版本
* ``run-.../cases/<nodeid-slug>.jsonl`` 每用例：开始/关键状态/断言理由/结果
* ``run-.../summary.json``              汇总：通过/失败/跳过及失败明细

测试通过 ``record`` fixture 记录关键中间状态（候选、排序、渲染、字节范围、
异常 code），而不是只写“接口能调用”。
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path

import pytest

_LOG_ROOT = Path(os.environ.get("NRP_TEST_LOG_DIR", "test-logs"))


def _slug(nodeid: str) -> str:
    return (
        nodeid.replace("/", "__")
        .replace("::", "--")
        .replace("[", "_")
        .replace("]", "_")
        .replace(" ", "_")
    )[:180]


class CaseRecorder:
    def __init__(self, path: Path, nodeid: str):
        self.path = path
        self.nodeid = nodeid
        self._fh = path.open("w", encoding="utf-8")
        self.steps = 0
        self.write("case_start", message=nodeid)

    def write(self, event: str, message: str = "", **data) -> None:
        self.steps += 1
        rec = {
            "seq": self.steps,
            "event": event,
            "message": message,
            "ts": round(time.time(), 6),
            "data": data,
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def state(self, name: str, value, reason: str = "") -> None:
        """记录关键中间状态与它为什么重要（重放时据此判断对错）。"""
        self.write("intermediate_state", reason or name, name=name, value=_safe(value))

    def check(self, claim: str, *, ok: bool, expected=None, actual=None) -> None:
        self.write(
            "assertion",
            claim,
            ok=bool(ok),
            expected=_safe(expected),
            actual=_safe(actual),
        )

    def fail_category(self, code: str, category: str, http_status: int) -> None:
        self.write(
            "failure_category",
            f"expect failure {code}",
            code=code,
            category=category,
            http_status=http_status,
        )

    def close(self, status: str, detail: str = "") -> None:
        if getattr(self, "_closed", False):
            return
        self.write("case_end", status=status, detail=detail)
        self._fh.close()
        self._closed = True


def _safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return repr(value)


@pytest.fixture
def record(request) -> CaseRecorder:
    # 显式请求时复用 autouse 的 _case_log 已创建的 recorder
    return request.node._nrp_recorder  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _case_log(request):
    """每个用例自动挂一个 recorder（显式请求 record 时拿到的是同一个）。"""
    run_dir: Path = request.config._nrp_run_dir  # type: ignore[attr-defined]
    cases = run_dir / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    rec = CaseRecorder(cases / f"{_slug(request.node.nodeid)}.jsonl", request.node.nodeid)
    request.node._nrp_recorder = rec  # type: ignore[attr-defined]
    yield rec
    if not getattr(rec, "_closed", False):
        status = getattr(rec, "_status", None) or "error"
        rec.close(status, detail=getattr(rec, "_detail", ""))


def pytest_configure(config) -> None:
    run_id = "testrun-" + time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = _LOG_ROOT / run_id
    (run_dir / "cases").mkdir(parents=True, exist_ok=True)
    config._nrp_run_dir = run_dir  # type: ignore[attr-defined]
    config._nrp_run_id = run_id  # type: ignore[attr-defined]
    versions = {}
    for mod in ("fastapi", "pydantic", "pytest", "httpx", "re2"):
        try:
            m = __import__(mod)
            versions[mod] = getattr(m, "__version__", "unknown")
        except Exception as exc:  # noqa: BLE001
            versions[mod] = f"unavailable: {exc}"
    meta = {
        "run_id": run_id,
        "started_at": time.time(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cwd": os.getcwd(),
        "package_versions": versions,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    rec = getattr(item, "_nrp_recorder", None)
    if rec is None:
        return
    if report.when == "call":
        if report.passed:
            rec._status, rec._detail = "passed", ""
            rec.close("passed")
        elif report.failed:
            detail = str(call.excinfo.value if call.excinfo else report.longrepr)
            rec._status, rec._detail = "failed", detail
            rec.close("failed", detail=detail)
        elif report.skipped:
            rec._status, rec._detail = "skipped", str(report.longrepr)
            rec.close("skipped", detail=str(report.longrepr))
    elif report.when == "setup" and report.failed:
        detail = "setup failed: " + str(
            call.excinfo.value if call.excinfo else report.longrepr
        )
        rec._status, rec._detail = "error", detail
        rec.close("error", detail=detail)


def pytest_sessionfinish(session, exitstatus) -> None:
    config = session.config
    run_dir: Path = config._nrp_run_dir  # type: ignore[attr-defined]
    run_id: str = config._nrp_run_id  # type: ignore[attr-defined]

    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    case_files = sorted((run_dir / "cases").glob("*.jsonl"))
    failed_cases = []
    for f in case_files:
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[-1])
        status = rec.get("data", {}).get("status", "error")
        counts[status] = counts.get(status, 0) + 1
        if status in ("failed", "error"):
            failed_cases.append(
                {"case": f.stem, "status": status,
                 "detail": rec.get("data", {}).get("detail", "")}
            )

    summary = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "exit_status": exitstatus,
        "counts": counts,
        "total": len(case_files),
        "failed_cases": failed_cases,
        "log_note": "cases/*.jsonl 含 seq 编号的关键中间状态与判断理由，可用 scripts/replay.py 重放",
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _LOG_ROOT.mkdir(parents=True, exist_ok=True)
    (_LOG_ROOT / "latest").write_text(run_id, encoding="utf-8")
    print(f"\n[nrp] test run: {run_id}  logs: {run_dir}")
