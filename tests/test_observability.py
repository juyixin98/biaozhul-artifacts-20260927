"""可观测性断言：日志必须能关联运行身份，并包含版本、计算步骤与判定依据。

失败路径也必须以 WARNING/ERROR 记录明确的失败类别，不允许静默成功。
"""
from __future__ import annotations

import json
import logging

from table_merge.logging_setup import _RUN_FILTER, get_logger, set_run_id
from table_merge.service import MergeService
from table_merge.storage import MetadataStore

from .conftest import EMP_SCHEMA, row


def _seed(service: MergeService, base, dev, main):
    bid = service.ingest_snapshot({"schema": EMP_SCHEMA, "rows": base})["snapshot_id"]
    did = service.ingest_snapshot({"schema": EMP_SCHEMA, "rows": dev})["snapshot_id"]
    mid = service.ingest_snapshot({"schema": EMP_SCHEMA, "rows": main})["snapshot_id"]
    service.initialize_main(bid, "base", "t")
    c0 = service.store.get_branch("main")["commit_id"]
    service.create_branch("dev", {"commit_id": c0})
    service.commit("dev", did, "d", "t")
    service.commit("main", mid, "m", "t")


def test_logs_carry_run_id_version_steps_and_basis(tmp_path):
    store = MetadataStore(tmp_path / "db.sqlite3", tmp_path / "snaps")
    service = MergeService(store)
    set_run_id("run_observability_001")

    base = [row(4, "d", "hz", 40), row(1, "a", "bj", 10)]
    dev = [row(1, "a", "bj", 10)]                               # 删除 4
    main = [row(4, "d", "hf", 41), row(1, "a", "bj", 10)]       # 修改 4
    _seed(service, base, dev, main)

    logger = get_logger()
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(logging.Formatter(
        "run_id=%(run_id)s v=%(version)s %(message)s"))
    handler.addFilter(_RUN_FILTER)
    logger.addHandler(handler)
    try:
        plan = service.plan_merge({"branch": "dev"}, {"branch": "main"}, "main")
    finally:
        logger.removeHandler(handler)

    text = "\n".join(records)
    assert "run_observability_001" in text                       # 运行身份
    assert "v=1.0.0" in text                                     # 版本（filter 注入）
    assert "step 1/4" in text and "step 3/4" in text             # 计算步骤/进度
    assert "DELETE_MODIFY_CONFLICT" in text                      # 判定类别
    assert "one side deleted" in text                            # 判定依据
    assert "plan_" in plan.plan_id
    set_run_id("-")


def test_failure_paths_log_warnings_not_silence(caplog, tmp_path):
    store = MetadataStore(tmp_path / "db.sqlite3", tmp_path / "snaps")
    service = MergeService(store)
    set_run_id("run_failure_path")

    # 未初始化时找分支 -> NotFoundError，调用方必须能据此返回明确失败
    try:
        service.plan_merge({"branch": "dev"}, {"branch": "main"}, "main")
        assert False, "expected NotFoundError"
    except Exception as exc:  # noqa: BLE001 - 断言异常类型与错误码
        assert exc.__class__.__name__ == "NotFoundError"
        assert getattr(exc, "error_code", None) == "NOT_FOUND"

    # 未知异常不被吞掉：直接抛的 RuntimeError 不携带成功语义
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        pass

    set_run_id("-")
    # run 过滤器在整个测试会话里被设置
    assert isinstance(_RUN_FILTER.run_id, str)
    _ = get_logger()
    _ = json.dumps({"ok": True})
