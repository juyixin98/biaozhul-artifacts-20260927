"""测试公共夹具与会话级运行日志。

每个 pytest 会话分配一个运行编号，并把：
- 配置、夹具 genesis txid；
- 每个用例的 VM trace/验签中间结果/判定理由（由 case_run_logger 写入）；
- 每个测试的通过/失败结果；
落到 runlogs/test-runs/<run_id>/，供离线重放问题。
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from pathlib import Path

import pytest

# 必须在导入 service 之前：禁用其模块级自动建 app（测试统一用 create_app 工厂）
os.environ["STACKVM_DISABLE_AUTO_APP"] = "1"

from chain import ChainKernel
from chain.store import Store
from stackvm.config import PROJECT_ROOT, load_settings
from stackvm.runlog import RunLogger, new_run_id
from stackvm.transaction import transaction_from_dict

FIX = PROJECT_ROOT / "fixtures"


def _load_json(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(scope="session")
def genesis():
    return _load_json("genesis.json")


@pytest.fixture(scope="session")
def cases_doc():
    return _load_json("cases.json")


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def store(tmp_db, genesis, settings):
    s = Store(tmp_db)
    s.bootstrap_genesis(genesis, settings.chain.mint_total_cap)
    yield s
    s.close()


@pytest.fixture
def kernel(store, settings):
    return ChainKernel(store, settings)


@pytest.fixture
def runlog_dir(tmp_path):
    d = tmp_path / "runlogs"
    d.mkdir()
    return d


@pytest.fixture
def evaluate_case(kernel):
    """返回 evaluate(case_dict) -> EvalReport。"""
    def _evaluate(case: dict):
        tx = transaction_from_dict(case["tx"])
        return kernel.evaluate(tx)
    return _evaluate


# --------------------------- 会话级运行日志 ---------------------------

@pytest.fixture(scope="session")
def session_run_id() -> str:
    return new_run_id("pytest")


@pytest.fixture(scope="session")
def session_logger(session_run_id) -> RunLogger:
    logger = RunLogger(session_run_id, kind="test-runs",
                       log_dir=PROJECT_ROOT / "runlogs")
    cases = _load_json("cases.json")
    logger.event("session_start",
                 python=sys.version.split()[0], platform=platform.platform(),
                 cwd=str(PROJECT_ROOT),
                 pid=os.getpid(),
                 genesis_txid=cases["genesis_txid"],
                 domain_tag=cases["domain_tag"],
                 case_count=len(cases["cases"]))
    yield logger
    logger.set_summary({"status": "completed"})
    logger.flush()


@pytest.fixture
def case_run_logger(session_logger):
    """把单个夹具用例的完整证据（trace/checks/判定）写成独立可重放日志。"""
    def _write(cid: str, case: dict, report_dict: dict) -> str:
        rid = f"{session_logger.run_id}-case{cid}"
        logger = RunLogger(rid, kind="test-runs",
                           log_dir=PROJECT_ROOT / "runlogs")
        logger.event("case", case_id=cid, label=case["label"],
                     expected=case["expected"], reason=case["reason"],
                     txid=case["txid"], digest=case["digest_hex"],
                     prev_lock=case["prev_lock"])
        logger.event("verdict", **{k: report_dict.get(k) for k in (
            "accepted", "kind", "code", "detail", "total_in", "total_out")})
        for inp in report_dict.get("inputs", []):
            for step in inp.get("trace", []):
                logger.event("trace_step", input=inp["index"], **step)
            for chk in inp.get("checks", []):
                logger.event("crypto_check", input=inp["index"], **chk)
        logger.set_summary({"case_id": cid, "expected": case["expected"],
                            "actual": report_dict.get("code")})
        path = logger.flush()
        session_logger.event("case_log", case_id=cid, path=path["summary"],
                             expected=case["expected"],
                             actual=report_dict.get("code"))
        return rid
    return _write


def pytest_runtest_logreport(report):
    """每个测试的判定也进入会话日志（失败时记录 longrepr 首行作为理由）。"""
    if report.when != "call":
        return
    logger = getattr(pytest_runtest_logreport, "_logger", None)
    if logger is None:
        return
    reason = ""
    if report.failed and isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
        reason = str(report.longrepr[2]).splitlines()[0][:300]
    logger.event("test_result", nodeid=report.nodeid,
                 outcome=report.outcome, duration_ms=round(report.duration * 1000, 1),
                 reason=reason)


@pytest.fixture(autouse=True)
def _attach_session_logger(session_logger):
    pytest_runtest_logreport._logger = session_logger
    yield
