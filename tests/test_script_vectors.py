"""脚本级测试向量：独立参考 JSON 对拍栈机结果。

每个用例断言：
- 接受/拒绝（具体布尔值）
- 拒绝时的顶层类别（input/state/resource/compute）
- 拒绝时的具体失败码（不仅是“能调用”）
- 接受时栈上唯一真值、消耗步数在预算内
"""

from __future__ import annotations

import pytest

from rsv.config import Limits
from rsv.errors import VerificationFailure
from rsv.vm import RunContext, StackMachine


def _run_vector(v: dict):
    machine = StackMachine(Limits(), require_clean_stack=True)
    ctx = RunContext(
        message32=bytes.fromhex(v["message32"]),
        network=v["network"],
        domain=v["domain"],
    )
    return machine.execute(
        bytes.fromhex(v["unlock_script"]),
        bytes.fromhex(v["lock_script"]),
        ctx,
    )


def test_all_script_vectors_match_independent_expectations(script_vectors):
    assert len(script_vectors) >= 40  # 防止夹具意外缩水
    by_id = {}
    for v in script_vectors:
        by_id[v["id"]] = v
        exp = v["expected"]
        if exp["accepted"]:
            res = _run_vector(v)
            assert res.ok is True, v["id"]
            assert len(res.final_stack) == 1, f"{v['id']}: clean stack"
            assert res.final_stack[0] == bytes([1]), f"{v['id']}: top must be OP_1"
            assert res.steps_used <= Limits.max_op_steps
        else:
            with pytest.raises(VerificationFailure) as ei:
                _run_vector(v)
            vf = ei.value
            assert vf.code.category == exp["category"], (
                f"{v['id']}: category {vf.code.category} != {exp['category']}"
            )
            assert vf.code.code == exp["code"], (
                f"{v['id']}: code {vf.code.code} != {exp['code']}"
            )
    # 关键用例存在性（任务点名的五类异常路径）
    for required in [
        "ms_duplicate_signature",
        "ms_2of3_order_ca",
        "ms_2of3_order_ac",
        "p2pk_wrong_domain_msg",
        "stack_underflow_drop",
        "budget_exhausted",
    ]:
        assert required in by_id


# ---- 任务点名的五类异常：逐类精确断言 ------------------------------------

def test_duplicate_signature_not_counted(script_vectors):
    v = next(x for x in script_vectors if x["id"] == "ms_duplicate_signature")
    with pytest.raises(VerificationFailure) as ei:
        _run_vector(v)
    assert ei.value.code.category == "compute"
    assert ei.value.code.code == "compute.crypto.threshold_invalid"


def test_signature_order_does_not_change_result(script_vectors):
    ca = next(x for x in script_vectors if x["id"] == "ms_2of3_order_ca")
    ac = next(x for x in script_vectors if x["id"] == "ms_2of3_order_ac")
    r_ca = _run_vector(ca)
    r_ac = _run_vector(ac)
    assert r_ca.ok and r_ac.ok
    assert r_ca.final_stack == r_ac.final_stack == [bytes([1])]
    assert r_ca.steps_used == r_ac.steps_used  # 顺序无关，步数确定性一致


def test_wrong_transaction_domain_fails_sig(script_vectors):
    v = next(x for x in script_vectors if x["id"] == "p2pk_wrong_domain_msg")
    with pytest.raises(VerificationFailure) as ei:
        _run_vector(v)
    assert ei.value.code.code == "compute.crypto.sig"


def test_stack_underflow_classified(script_vectors):
    v = next(x for x in script_vectors if x["id"] == "stack_underflow_drop")
    with pytest.raises(VerificationFailure) as ei:
        _run_vector(v)
    assert ei.value.code.category == "compute"
    assert ei.value.code.code == "compute.stack_underflow"


def test_budget_exhaustion_classified(script_vectors):
    v = next(x for x in script_vectors if x["id"] == "budget_exhausted")
    with pytest.raises(VerificationFailure) as ei:
        _run_vector(v)
    assert ei.value.code.category == "resource"
    assert ei.value.code.code == "resource.op_budget_exhausted"


# ---- 阈值边界 -------------------------------------------------------------

@pytest.mark.parametrize("vid,ok", [
    ("ms_1of3_one_valid", True),
    ("ms_2of3_ab", True),
    ("ms_2of3_order_ca", True),
    ("ms_threshold_not_met", False),
    ("ms_m_zero", False),
    ("ms_m_gt_n", False),
    ("ms_duplicate_pubkey_policy", False),
])
def test_threshold_boundaries(script_vectors, vid, ok):
    v = next(x for x in script_vectors if x["id"] == vid)
    if ok:
        assert _run_vector(v).ok
    else:
        with pytest.raises(VerificationFailure):
            _run_vector(v)
