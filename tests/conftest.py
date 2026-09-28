"""pytest 共享夹具与配置。"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def untag(v):
    """把黄金向量的 JSON 标签还原为 Python 值。"""
    if isinstance(v, dict):
        if "__int__" in v:
            return int(v["__int__"])
        if "__bytes__" in v:
            return bytes.fromhex(v["__bytes__"])
        if "__tuple__" in v:
            return tuple(untag(x) for x in v["__tuple__"])
        raise AssertionError(f"未知标签 {v}")
    if isinstance(v, list):
        return [untag(x) for x in v]
    return v


def normalize(v):
    """list/tuple 统一成 list，便于与 eth_abi 结果比较。"""
    if isinstance(v, (list, tuple)):
        return [normalize(x) for x in v]
    return v


# ---- 会话级结构化日志：逐测试记录身份/判定/失败类别，绝不把失败记成功 ----

_run_logger = None
_results = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}


def pytest_configure(config):
    import os
    from app.runlog import RunLogger

    global _run_logger
    # 固定独立 run_id/文件，避免与被测 API lifespan 的日志互相覆盖结尾记录。
    os.environ.setdefault("ABI_RUN_ID", "pytest-golden")
    _run_logger = RunLogger("test-logs", "pytest")  # (log_dir, stage)


def pytest_runtest_logreport(report):
    # 每个用例只在 call 阶段记录一次判定（setup/teardown 不重复计数）。
    if report.when != "call":
        return
    identity = {"nodeid": report.nodeid, "phase": report.when}
    if report.passed:
        _results["passed"] += 1
        _run_logger.step("test", identity=identity,
                         verdict_detail="passed", duration_ms=round(report.duration * 1000, 2))
    elif report.skipped:
        _results["skipped"] += 1
        _run_logger.step("test", identity=identity, verdict_detail="skipped")
    elif report.failed:
        _results["failed"] += 1
        category = "assertion"
        tb = report.longreprtext
        for marker in ("NonCanonicalPaddingError", "OffsetOutOfBoundsError",
                       "OverlapError", "AllocationLimitError",
                       "NonCanonicalLayoutError", "UnsupportedTypeError",
                       "ReplayError"):
            if marker in tb:
                category = marker
                break
        _run_logger.failure("test", category=category,
                            message=tb[-500:], identity=identity)


def pytest_sessionfinish(session, exitstatus):
    if _run_logger is not None:
        verdict = "completed" if exitstatus == 0 else "failed"
        _run_logger.close(verdict=verdict,
                          summary={**_results, "exitstatus": exitstatus})


@pytest.fixture(scope="session")
def golden():
    path = ROOT / "tests" / "fixtures" / "golden_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def chain_fixture():
    path = ROOT / "tests" / "fixtures" / "chain_fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))
