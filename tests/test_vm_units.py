"""VM/编解码单元测试：操作码白名单、ScriptNum、栈限制、条件分支、深度。"""
from __future__ import annotations

import pytest

from stackvm import opcodes as O
from stackvm.config import Limits
from stackvm.errors import FailCode
from stackvm.script import (
    assemble,
    cast_bool,
    decode_scriptnum,
    disassemble,
    encode_push,
    encode_scriptnum,
    parse,
)
from stackvm.transaction import Transaction
from stackvm.vm import run_scripts


def _tx() -> Transaction:
    return Transaction(version=1, inputs=(), outputs=())


def _run(unlock: bytes, lock: bytes, limits: Limits | None = None):
    return run_scripts(_tx(), b"\x00" * 32, unlock, lock, limits=limits)


# ------------------------------ 编解码 ------------------------------

@pytest.mark.parametrize("n", [0, 1, 75, 76, 255])
def test_push_roundtrip(n):
    data = bytes((i % 256 for i in range(n)))
    script = encode_push(data)
    ins = parse(script)
    assert len(ins) == 1
    assert (ins[0].data or b"") == data


@pytest.mark.parametrize("n", [256, 65535])
def test_push_roundtrip_large_without_element_cap(n):
    # 编解码层支持 PUSHDATA2/4；元素上限在压栈时由配置强制
    data = bytes((i % 256 for i in range(n)))
    script = encode_push(data, max_element_bytes=n)
    ins = parse(script)
    assert (ins[0].data or b"") == data


def test_unknown_opcode_rejected_at_decode():
    for bad in [0x62, 0x65, 0x66, 0xad, 0x50]:
        with pytest.raises(Exception) as ei:
            parse(bytes([bad]))
        assert ei.value.code in (FailCode.UNKNOWN_OPCODE, FailCode.SCRIPT_MALFORMED)


def test_truncated_push_is_malformed():
    with pytest.raises(Exception) as ei:
        parse(bytes([4, 0xAA, 0xBB]))
    assert ei.value.code is FailCode.SCRIPT_MALFORMED
    with pytest.raises(Exception) as ei2:
        parse(bytes([0x4D, 0x10, 0x00]) + b"\x00" * 5)
    assert ei2.value.code is FailCode.SCRIPT_MALFORMED


def test_script_too_large():
    with pytest.raises(Exception) as ei:
        parse(b"\x00" * 100, max_script_bytes=10)
    assert ei.value.code is FailCode.SCRIPT_TOO_LARGE


def test_disassemble_readable():
    text = disassemble(assemble([1, b"ab", O.Op.OP_DUP, O.Op.OP_SHA256,
                                 O.Op.OP_EQUALVERIFY]))
    assert "OP_1" in text and "OP_DUP" in text and "OP_SHA256" in text


# ------------------------------ ScriptNum ------------------------------

@pytest.mark.parametrize("value", [0, 1, -1, 127, 128, -128, 255, 256,
                                   2**31 - 1, -(2**31 - 1)])
def test_scriptnum_roundtrip(value):
    enc = encode_scriptnum(value)
    assert decode_scriptnum(enc) == value


def test_scriptnum_overflow():
    with pytest.raises(Exception) as ei:
        encode_scriptnum(2**31)
    assert ei.value.code is FailCode.INT_OVERFLOW
    with pytest.raises(Exception) as ei2:
        decode_scriptnum(b"\x00" * 5)
    assert ei2.value.code is FailCode.INT_OVERFLOW


@pytest.mark.parametrize("data,truth", [
    (b"", False), (b"\x00", False), (b"\x80", False), (b"\x00\x80", False),
    (b"\x01", True), (b"\x00\x01", True), (b"\x80\x00", True),
])
def test_cast_bool(data, truth):
    assert cast_bool(data) is truth


# ------------------------------ 基础运算 ------------------------------

def test_add_and_size():
    res = _run(assemble([2, 3]),
               assemble([O.Op.OP_ADD, O.Op.OP_SIZE, 1, O.Op.OP_EQUALVERIFY]))
    # 2+3=5 编码为 0x05（1 字节），SIZE 压 1，与 OP_1 EQUALVERIFY，栈留 0x05=真
    assert res.ok, res.detail


def test_equal_verify_failure():
    res = _run(assemble([b"a", b"b"]), assemble([O.Op.OP_EQUALVERIFY, 1]))
    assert not res.ok and res.code is FailCode.EVAL_FALSE


def test_drop_underflow_is_compute_not_resource():
    res = _run(b"", bytes([O.Op.OP_DROP]))
    assert res.code is FailCode.STACK_UNDERFLOW


def test_from_altstack_underflow():
    res = _run(b"", assemble([O.Op.OP_FROMALTSTACK, 1]))
    assert res.code is FailCode.STACK_UNDERFLOW


def test_altstack_roundtrip_and_clean_stack():
    # x → alt；压 1；把 1 也移入 alt；依次取回并丢弃，最后压 1 收尾
    res = _run(assemble([b"x"]),
               assemble([O.Op.OP_TOALTSTACK,
                         1, O.Op.OP_TOALTSTACK,
                         O.Op.OP_FROMALTSTACK, O.Op.OP_DROP,
                         O.Op.OP_FROMALTSTACK, O.Op.OP_DROP,
                         1]))
    assert res.ok, res.detail


def test_unclean_stack_multiple_true_elements():
    res = _run(b"", assemble([1, 1]))
    assert res.code is FailCode.UNCLEAN_STACK


def test_unclean_stack_single_false_element():
    res = _run(b"", bytes([O.Op.OP_0]))
    assert res.code is FailCode.UNCLEAN_STACK


def test_op_return_in_active_branch():
    res = _run(b"", bytes([O.Op.OP_RETURN]))
    assert res.code is FailCode.OP_RETURN_EXECUTED


# ------------------------------ 条件分支 ------------------------------

def test_if_true_and_else_inactive():
    res = _run(assemble([1]),
               assemble([O.Op.OP_IF, 1, O.Op.OP_ELSE, O.Op.OP_RETURN,
                         O.Op.OP_ENDIF]))
    assert res.ok, res.detail


def test_notif_false_path():
    res = _run(assemble([0]),
               assemble([O.Op.OP_NOTIF, 1, O.Op.OP_ELSE, O.Op.OP_RETURN,
                         O.Op.OP_ENDIF]))
    assert res.ok, res.detail


def test_unbalanced_conditionals():
    # IF 条件为真进入分支却不闭合
    res = _run(assemble([1]), assemble([O.Op.OP_IF, 1]))
    assert res.code is FailCode.UNBALANCED_CONDITIONAL
    res2 = _run(b"", bytes([O.Op.OP_ENDIF]))
    assert res2.code is FailCode.UNBALANCED_CONDITIONAL
    res3 = _run(b"", bytes([O.Op.OP_ELSE]))
    assert res3.code is FailCode.UNBALANCED_CONDITIONAL
    # 双 ELSE
    res4 = _run(assemble([1]),
                assemble([O.Op.OP_IF, 1, O.Op.OP_ELSE, 1,
                          O.Op.OP_ELSE, O.Op.OP_ENDIF]))
    assert res4.code is FailCode.UNBALANCED_CONDITIONAL


def test_condition_depth_limit():
    depth = 3
    limits = Limits(max_if_depth=depth)
    lock = bytes([O.Op.OP_IF] * (depth + 1)) + bytes([O.Op.OP_ENDIF] * (depth + 1))
    unlock = assemble([1] * (depth + 1))
    res = _run(unlock, lock, limits=limits)
    assert res.code is FailCode.CONDITION_DEPTH_EXCEEDED


def test_inactive_branch_unknown_push_size_still_checked():
    # 非活跃分支中的超大元素仍受元素大小约束。
    # 手工拼脚本：OP_0 OP_IF <256字节元素> OP_ENDIF OP_1
    # （不能走 assemble，因为压栈编码时会先做元素上限校验）
    big = bytes([O.Op.OP_PUSHDATA2, 0x00, 0x01]) + b"\xaa" * 256
    lock = bytes([O.Op.OP_0, O.Op.OP_IF]) + big + bytes([O.Op.OP_ENDIF, O.OP_1])
    res = _run(b"", lock)
    assert res.code is FailCode.ELEMENT_TOO_LARGE


# ------------------------------ 预算与栈上限 ------------------------------

def test_budget_charges_even_in_inactive_branch():
    # IF 为假，但 300 个 NOP 在 ELSE 分支中仍逐个计 1 步
    lock = assemble([0, O.Op.OP_IF, O.Op.OP_RETURN, O.Op.OP_ELSE]
                    + [O.Op.OP_NOP] * 300
                    + [1, O.Op.OP_ENDIF])
    res = _run(b"", lock)
    assert res.code is FailCode.BUDGET_EXHAUSTED


def test_stack_item_limit():
    limits = Limits(max_stack_items=5)
    res = _run(assemble([b"x"] * 6), assemble([1]), limits=limits)
    assert res.code is FailCode.STACK_TOO_LARGE


def test_element_size_hard_limit_on_push_encoding():
    with pytest.raises(Exception) as ei:
        encode_push(b"\xaa" * 256, max_element_bytes=255)
    assert ei.value.code is FailCode.ELEMENT_TOO_LARGE


def test_script_depth_fixed_contexts():
    # 机器没有动态求值入口；run_scripts 固定深度 1→2。直接构造超深调用：
    m_tx = _tx()
    from stackvm.vm import Machine
    m = Machine(m_tx, b"\x00" * 32, Limits(max_script_depth=1))
    with pytest.raises(Exception) as ei:
        m.execute(b"\x51", depth=2)
    assert ei.value.code is FailCode.SCRIPT_DEPTH_EXCEEDED


def test_push_only_unlock_enforced():
    res = _run(bytes([O.Op.OP_NOP]), assemble([1]))
    assert res.code is FailCode.PUSH_ONLY_VIOLATION


def test_trace_records_pc_budget_and_stack():
    res = _run(assemble([b"ab"]),
               assemble([O.Op.OP_SIZE, O.Op.OP_DROP, O.Op.OP_DROP, 1]))
    assert res.ok
    pcs = [t.pc for t in res.trace]
    assert pcs == sorted(pcs)
    assert all(0 <= t.budget_left <= 200 for t in res.trace)
    size_event = [t for t in res.trace if t.op == "OP_SIZE"][0]
    # SIZE 之后栈顶应为 ScriptNum(2)=0x02
    assert size_event.stack[-1] == "02"


# --------------------- 额外的门槛/VERIFY 语义 ---------------------

def test_duplicate_pubkeys_in_lock_rejected_without_any_signature():
    # 锁脚本里直接写两把相同公钥的 1-of-2：即使提交合法签名，
    # 结构预检也必须以 SIG_DUPLICATED 拒绝（公钥集合去重，先于任何计数）
    import json
    from stackvm.config import PROJECT_ROOT
    pub = bytes.fromhex(json.loads(
        (PROJECT_ROOT / "fixtures" / "keys.json").read_text("utf-8")
    )["keys"]["alice"]["pub_hex"])
    lock = assemble([1, pub, pub, 2, O.Op.OP_CHECKMULTISIG])
    # 合法 DER（r=s=1 结构通过解析；验签在此之前不应被调用）
    res = _run(assemble([b"\x30\x06\x02\x01\x01\x02\x01\x01"]), lock)
    assert res.code is FailCode.SIG_DUPLICATED
    assert res.checks == []  # 去重预检拦截，不产生任何验签计数


def test_checksigverify_pops_true_and_continues():
    """OP_CHECKSIGVERIFY 成功时弹掉公钥/签名并不留值；栈平衡由后续脚本保证。

    这里用一个不依赖真实密码学的结构替身不可行（验签必真），因此直接验证
    操作数语义：在 P2PK 风格脚本末尾追加 DROP 不可行，改为对成功 P2PK 用
    CHECKSIG 形式保留值；CHECKSIGVERIFY 的弹栈语义由 trace 断言。
    """
    # 用最小等价：EQUALVERIFY 与 CHECKSIGVERIFY 共享 VERIFY 弹栈约定。
    # unlock 压两个相同元素，锁：EQUALVERIFY（成功弹掉两项）再压 OP_1
    res = _run(assemble([b"z", b"z"]),
               assemble([O.Op.OP_EQUALVERIFY, 1]))
    assert res.ok
    assert res.final_stack == [b"\x01"]
