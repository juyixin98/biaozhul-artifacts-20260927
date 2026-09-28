"""离线回放边界 (offline replay)。

输入：夹具文件（schema_version=1，见 fixtures/），由若干"前置块"与"用例"组成。
处理：
  1. 前置块必须全部被独立 oracle 与本内核同时接受，否则夹具本身无效；
  2. 每个用例独立挂载到前置块重放后的链状态上（前置状态不被用例污染）；
  3. 对每个用例：拍快照看板 -> 内核 plan -> store.apply_block -> oracle 对照
     -> 拍快照后板，失败用例必须前后完全一致（UTXO 集原样）；
  4. 全程写 :class:`RunJournal`（run_id、关键中间状态、逐笔判定与理由）。

CLI：``python -m utxo_ledger.replay <fixture.json> [--db PATH] [--log-dir DIR]``
退出码：0 全部通过；1 存在用例不匹配或夹具无效；2 参数/IO 错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from . import encoding
from .errors import InternalError, LedgerError
from .journal import RunJournal, assert_state_unchanged, snapshot
from .kernel import Kernel
from .store import SqliteStore

FIXTURE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    accepted: bool
    expected_accepted: bool
    error: dict[str, Any] | None
    oracle_match: bool
    state_preserved: bool | None
    run_id: str


def _load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"夹具不是合法 JSON: {path}: {exc}") from exc


def _parse_block(raw: Any):
    if not isinstance(raw, dict):
        raise ValueError("块必须为 JSON 对象")
    return encoding.block_from_json(raw)


def _kernel_accept(store: SqliteStore, block, journal: RunJournal) -> dict[str, Any]:
    """对一个块执行 内核规划 + 存储提交，返回结果字典（不抛 LedgerError）。"""
    before = snapshot(store, "before_block")
    journal.state_snapshot("snapshot_before", before)
    try:
        plan = Kernel(store, event_sink=journal.kernel_event).plan_block(block)
        root_after = store.apply_block(plan)
        after = snapshot(store, "after_block")
        journal.state_snapshot("snapshot_after", after)
        journal.info(
            "block_accepted",
            height=plan.height,
            block_id=plan.block_id.hex(),
            total_fee=plan.total_fee,
            utxo_root_after=root_after.hex(),
        )
        return {"accepted": True, "before": before, "after": after}
    except LedgerError as exc:
        after = snapshot(store, "after_rejected_block")
        journal.state_snapshot("snapshot_after_reject", after)
        journal.failure(exc.to_dict())
        preserved = True
        try:
            assert_state_unchanged(before, after)
        except AssertionError as mismatch:
            preserved = False
            journal.info("state_preservation_violated", detail=str(mismatch))
        return {
            "accepted": False,
            "error": exc.to_dict(),
            "before": before,
            "after": after,
            "state_preserved": preserved,
        }
    except Exception as exc:  # 未预期故障：COMPUTATION_FAILED/INTERNAL_ERROR
        wrapped = InternalError(f"未预期异常: {type(exc).__name__}: {exc}")
        after = snapshot(store, "after_internal_error")
        journal.failure(wrapped.to_dict())
        return {
            "accepted": False,
            "error": wrapped.to_dict(),
            "before": before,
            "after": after,
            "state_preserved": None,
        }


def replay_fixture(
    fixture: dict[str, Any],
    *,
    db_path: str = ":memory:",
    log_dir: str = "logs",
    oracle: Any = None,
) -> list[CaseResult]:
    """回放一个夹具对象。oracle 参数注入独立参考模块（由 CLI/测试提供）。"""
    if fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError(
            f"夹具 schema_version 必须为 {FIXTURE_SCHEMA_VERSION}，"
            f"得到 {fixture.get('schema_version')!r}"
        )
    if oracle is None:
        raise ValueError("必须注入独立 oracle 模块（不得由内核生成参考结果）")

    prefix_raw = fixture.get("prefix_blocks", [])
    cases = fixture.get("cases", [])

    # --- 前置块：内核与 oracle 双接受才允许进入用例 ----------------------
    setup_journal = RunJournal(
        scenario=f"{fixture.get('name', 'fixture')}::prefix",
        log_dir=log_dir,
        metadata={"phase": "prefix"},
    )
    store = SqliteStore(db_path)
    oracle_state = oracle.genesis_state()
    parsed_prefix: list[Any] = []
    try:
        for pos, raw in enumerate(prefix_raw):
            block = _parse_block(raw)
            parsed_prefix.append(block)
            result = _kernel_accept(store, block, setup_journal)
            verdict = oracle.evaluate_block(raw, oracle_state)
            if not verdict["accepted"]:
                raise ValueError(
                    f"前置块 #{pos} 被独立 oracle 拒绝（{verdict['code']}），夹具无效"
                )
            if not result["accepted"]:
                raise ValueError(
                    f"前置块 #{pos} 被内核拒绝（{result['error']['code']}），夹具无效"
                )
            oracle_state = oracle.apply_block(raw, oracle_state, verdict)
        setup_journal.finish(
            accepted=True,
            expected_accepted=True,
            match=True,
            before_snapshot={},
            after_snapshot=snapshot(store, "prefix_done"),
        )

        # 用例挂载到"仅由前置块重建"的隔离状态，用例之间互不污染
        results: list[CaseResult] = []
        for case in cases:
            store.close()
            store = SqliteStore(":memory:")
            ostate = oracle.genesis_state()
            journal = RunJournal(
                scenario=f"{fixture.get('name', 'fixture')}::{case['name']}",
                log_dir=log_dir,
                metadata={"case": case["name"]},
            )
            for raw, block in zip(prefix_raw, parsed_prefix):
                plan = Kernel(store).plan_block(block)
                store.apply_block(plan)
                v = oracle.evaluate_block(raw, ostate)
                assert v["accepted"], "前置块在隔离重放中失败（不应发生）"
                ostate = oracle.apply_block(raw, ostate, v)

            block = _parse_block(case["block"])
            outcome = _kernel_accept(store, block, journal)
            verdict = oracle.evaluate_block(case["block"], ostate)

            expected = bool(case.get("expected_accepted"))
            error = outcome.get("error")
            match = True
            mismatch_reasons: list[str] = []
            if outcome["accepted"] != expected:
                match = False
                mismatch_reasons.append(
                    f"accepted={outcome['accepted']} expected={expected}"
                )
            if verdict["accepted"] != outcome["accepted"]:
                match = False
                mismatch_reasons.append(
                    f"oracle accepted={verdict['accepted']} kernel accepted={outcome['accepted']}"
                )
            if not outcome["accepted"] and error is not None:
                if verdict["code"] != error["code"]:
                    match = False
                    mismatch_reasons.append(
                        f"code kernel={error['code']} oracle={verdict['code']}"
                    )
                if verdict["category"] != error["category"]:
                    match = False
                    mismatch_reasons.append(
                        f"category kernel={error['category']} oracle={verdict['category']}"
                    )
                expected_code = case.get("expected_code")
                if expected_code and expected_code != error["code"]:
                    match = False
                    mismatch_reasons.append(
                        f"expected_code={expected_code} actual={error['code']}"
                    )
                expected_category = case.get("expected_category")
                if expected_category and expected_category != error["category"]:
                    match = False
                    mismatch_reasons.append(
                        f"expected_category={expected_category} actual={error['category']}"
                    )
                tx_index_exp = case.get("expected_tx_index")
                if tx_index_exp is not None and tx_index_exp != error.get("tx_index"):
                    match = False
                    mismatch_reasons.append(
                        f"expected_tx_index={tx_index_exp} actual={error.get('tx_index')}"
                    )

            state_preserved = outcome.get("state_preserved") if not outcome["accepted"] else None
            journal.finish(
                accepted=outcome["accepted"],
                expected_accepted=expected,
                match=match,
                before_snapshot=outcome["before"],
                after_snapshot=outcome["after"],
                failure=error,
                extra={
                    "oracle": {
                        "accepted": verdict["accepted"],
                        "code": verdict["code"],
                        "category": verdict["category"],
                    },
                    "mismatch_reasons": mismatch_reasons,
                },
            )
            results.append(
                CaseResult(
                    name=case["name"],
                    accepted=outcome["accepted"],
                    expected_accepted=expected,
                    error=error,
                    oracle_match=match,
                    state_preserved=state_preserved,
                    run_id=journal.run_id,
                )
            )
        return results
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="UTXO 账本夹具离线回放")
    ap.add_argument("fixture", help="夹具 JSON 路径")
    ap.add_argument("--db", default=":memory:", help="前缀链使用的 sqlite 路径")
    ap.add_argument("--log-dir", default="logs", help="JSONL 日志目录")
    args = ap.parse_args(argv)

    if not os.path.exists(args.fixture):
        print(f"夹具文件不存在: {args.fixture}", file=sys.stderr)
        return 2

    # 以文件路径方式加载独立 oracle（不经由被测包，保证参考实现独立）
    root_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    oracle_path = os.path.join(root_dir, "reference", "oracle.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("independent_oracle", oracle_path)
    if spec is None or spec.loader is None:
        print("无法加载独立 oracle", file=sys.stderr)
        return 2
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)

    fixture = _load_json(args.fixture)
    try:
        results = replay_fixture(
            fixture, db_path=args.db, log_dir=args.log_dir, oracle=oracle
        )
    except ValueError as exc:
        print(f"夹具无效: {exc}", file=sys.stderr)
        return 2

    failed = 0
    for r in results:
        status = "PASS" if r.oracle_match and (r.state_preserved is not False) else "FAIL"
        if status == "FAIL":
            failed += 1
        detail = (
            f"{r.error['category']}/{r.error['code']}@tx{r.error.get('tx_index')}"
            if r.error
            else "accepted"
        )
        print(
            f"[{status}] {r.name}: kernel_accepted={r.accepted} "
            f"expected={r.expected_accepted} {detail} run={r.run_id}"
        )
    print(f"\n{len(results) - failed}/{len(results)} 用例通过，日志目录: {args.log_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
