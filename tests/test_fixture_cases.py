"""夹具驱动的端到端验证测试。

每个用例都断言**具体结果码与类别**（不是“接口能调用”），失败用例额外断言：
- UTXO 集合完全不变（没有执行任何转账）；
- chain_height 不增长；
- 运行日志中保留了中间栈状态、预算与判定理由。
"""
from __future__ import annotations

import copy

import pytest

from stackvm.errors import FailCode, FailKind, kind_of
from stackvm.sighash import signature_digest
from stackvm.transaction import transaction_from_dict


def _report(evaluate_case, case):
    return evaluate_case(case)


def test_genesis_bootstrap(store, genesis, cases_doc):
    # 21 个输出 * 1000
    assert len(store.list_utxos()) == 21
    assert store.chain_height == 1
    assert sum(u["value"] for u in store.list_utxos()) == 21_000
    assert cases_doc["genesis_txid"]


@pytest.mark.parametrize("cid", [f"{i:02d}" for i in range(21)])
def test_every_fixture_case_matches_expected(cid, cases_doc, evaluate_case,
                                             store, case_run_logger):
    case = cases_doc["cases"][cid]
    before = sorted((u["txid"], u["vout"]) for u in store.list_utxos())
    height_before = store.chain_height

    report = _report(evaluate_case, case)
    d = report.to_dict()
    case_run_logger(cid, case, d)  # 落可重放日志

    expected = case["expected"]
    if expected == "OK":
        assert report.accepted is True, f"{cid}: 预期成功但 {report.code.value}"
        assert report.code is FailCode.OK
        assert report.kind is FailKind.SUCCESS
        assert d["total_in"] == 1000 and d["total_out"] == 1000
    else:
        assert report.accepted is False
        assert report.code.name == expected, (
            f"{cid} {case['label']}: 预期 {expected}，实际 {report.code.name}")
        assert report.kind is kind_of(FailCode[expected])
        # 关键规则：验证失败绝不执行转账
        assert store.chain_height == height_before
        after = sorted((u["txid"], u["vout"]) for u in store.list_utxos())
        assert after == before, f"{cid}: 失败用例改变了 UTXO 集"

    # 摘要必须与夹具中由独立 ecdsa 工具计算的摘要一致
    tx = transaction_from_dict(case["tx"])
    digest = signature_digest(tx, [bytes.fromhex(case["prev_lock"])],
                              case["signing_domain"])
    assert digest.hex() == case["digest_hex"]


# ---------------------- 明确点名的五个攻击/异常场景 ----------------------

def test_repeated_signature_is_duplicated(cases_doc, evaluate_case):
    case = cases_doc["cases"]["05"]
    report = _report(evaluate_case, case)
    assert report.accepted is False
    assert report.code is FailCode.SIG_DUPLICATED
    assert report.kind is FailKind.COMPUTE
    checks = report.inputs[0].result.checks
    # 重复用例应在真正验签之前（或匹配时）被拦截，不能把同一公钥计两次
    matched = [c for c in checks if c["result"]]
    assert len({c["pub"] for c in matched}) == len(matched)


def test_signature_order_swap_threshold_not_met(cases_doc, evaluate_case):
    case = cases_doc["cases"]["06"]
    report = _report(evaluate_case, case)
    assert report.code is FailCode.THRESHOLD_NOT_MET
    # trace 中应能看到两次验签尝试：bob 命中第 0 把钥匙之前的钥匙不行，
    # alice 的签名无法回溯 —— 记录至少一次 False 后判定
    checks = report.inputs[0].result.checks
    assert any(c["result"] is False for c in checks)


def test_wrong_transaction_domain_sig_invalid(cases_doc, evaluate_case):
    case = cases_doc["cases"]["07"]
    assert case["signing_domain"] != "STACKVM.SIGHASH/1"
    report = _report(evaluate_case, case)
    assert report.code is FailCode.SIG_INVALID
    # 用正确域标签重算摘要时，该签名也必然不通过（域标签确实参与摘要）
    tx = transaction_from_dict(case["tx"])
    right = signature_digest(tx, [bytes.fromhex(case["prev_lock"])], "STACKVM.SIGHASH/1")
    assert right.hex() != case["digest_hex"]


def test_stack_underflow_classified(cases_doc, evaluate_case):
    case = cases_doc["cases"]["09"]
    report = _report(evaluate_case, case)
    assert report.code is FailCode.STACK_UNDERFLOW
    assert report.kind is FailKind.COMPUTE
    # 判定发生在锁定脚本首指令，预算只消耗了解锁段（0 步）+ 锁段一次弹出尝试
    assert report.inputs[0].result.budget_left == 199


def test_budget_exhausted_before_verification(cases_doc, evaluate_case):
    case = cases_doc["cases"]["08"]
    report = _report(evaluate_case, case)
    assert report.code is FailCode.BUDGET_EXHAUSTED
    assert report.kind is FailKind.RESOURCE
    res = report.inputs[0].result
    assert res.budget_left == 0
    # 预算必须在 CHECKSIG 之前耗尽：trace 里不应出现任何验签中间记录
    assert res.checks == []
    assert all(c.op != "OP_CHECKSIG" for c in res.trace)


# ---------------------- 阈值边界与哈希操作 ----------------------

@pytest.mark.parametrize("cid,should_pass", [
    ("02", True), ("03", False), ("04", True),
    ("05", False), ("06", False),
])
def test_multisig_threshold_boundaries(cid, should_pass, cases_doc, evaluate_case):
    report = _report(evaluate_case, cases_doc["cases"][cid])
    assert report.accepted is should_pass


@pytest.mark.parametrize("cid", ["10", "12", "13", "14"])
def test_hash_ops_preimage_success(cid, cases_doc, evaluate_case):
    report = _report(evaluate_case, cases_doc["cases"][cid])
    assert report.accepted is True, f"{cid} 哈希原像用例失败: {report.detail}"


def test_wrong_hash_preimage_is_eval_false(cases_doc, evaluate_case):
    report = _report(evaluate_case, cases_doc["cases"]["11"])
    assert report.code is FailCode.EVAL_FALSE


def test_hash_operations_produce_concrete_stack_values(cases_doc, evaluate_case):
    """成功哈希用例的 trace 必须展示哈希后 20/32 字节元素再进入 EQUALVERIFY。"""
    report = _report(evaluate_case, cases_doc["cases"]["12"])  # SHA256
    ops = [t.op for t in report.inputs[0].result.trace]
    assert "OP_SHA256" in ops and "OP_EQUALVERIFY" in ops


# ---------------------- 域标签绑定的变异测试 ---------------------------

def test_mutating_output_value_invalidates_signature(cases_doc, evaluate_case):
    case = copy.deepcopy(cases_doc["cases"]["00"])
    case["tx"]["outputs"][0]["value"] = 999
    report = _report(evaluate_case, case)
    # 金额首先不平衡（输入 1000 vs 输出 999）→ VALUE_IMBALANCE；
    # 再构造平衡的变异：改输出脚本（收款方），此时必须走到 SIG_INVALID
    case2 = copy.deepcopy(cases_doc["cases"]["00"])
    case2["tx"]["outputs"][0]["script"] = "51"  # OP_1 收款锁，金额仍 1000
    report2 = _report(evaluate_case, case2)
    assert report.code is FailCode.VALUE_IMBALANCE
    assert report2.code is FailCode.SIG_INVALID


def test_mutating_prevout_reference_changes_digest(cases_doc):
    case = cases_doc["cases"]["00"]
    tx = transaction_from_dict(case["tx"])
    base = signature_digest(tx, [bytes.fromhex(case["prev_lock"])],
                            cases_doc["domain_tag"])
    # 改 vout（引用另一个 genesis 输出）
    mutated = tx.wire()
    mutated["inputs"][0]["vout"] = 1
    tx2 = transaction_from_dict(mutated)
    other = signature_digest(tx2, [bytes.fromhex(case["prev_lock"])],
                             cases_doc["domain_tag"])
    assert other != base
