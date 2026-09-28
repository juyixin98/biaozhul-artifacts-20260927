"""黄金向量对照测试：被测核心 vs 成熟 eth_abi 预言机。

参考答案不是由被测核心生成的：有效向量的规范字节由 eth_abi 编码，
测试断言我们的编码逐字节相等、且能解码预言机字节得到相同值。
"""

from __future__ import annotations

import pytest

from app.abi import decode, encode
from tests.conftest import normalize, untag

pytestmark = pytest.mark.oracle


def test_golden_valid_encode_byte_exact(golden):
    """我们的编码必须与 eth_abi 预言机字节级一致。"""
    mismatches = []
    for i, case in enumerate(golden["valid"]):
        value = untag(case["value"])
        expected = bytes.fromhex(case["encoded_hex"])
        got = encode(case["types"], tuple(value))
        if got != expected:
            mismatches.append(
                f"#{i} {case['types']} value={value!r}\n  got={got.hex()}\n  exp={expected.hex()}"
            )
    assert not mismatches, "编码与预言机不一致:\n" + "\n".join(mismatches)


def test_golden_valid_decode_oracle_bytes(golden):
    """解码 eth_abi 产生的字节，必须还原出夹具钉死的值。"""
    mismatches = []
    for i, case in enumerate(golden["valid"]):
        blob = bytes.fromhex(case["encoded_hex"])
        expected = normalize(untag(case["value"]))
        try:
            got = normalize(decode(case["types"], blob))
        except Exception as e:  # 不应有任何异常
            mismatches.append(f"#{i} {case['types']} 解码异常 {type(e).__name__}: {e}")
            continue
        if got != expected:
            mismatches.append(f"#{i} {case['types']}\n  got={got}\n  exp={expected}")
    assert not mismatches, "解码预言机字节失败:\n" + "\n".join(mismatches)


def test_roundtrip_self(golden):
    """对每个有效向量：编码→解码→再编码 必须稳定（自洽性补充）。"""
    bad = []
    for i, case in enumerate(golden["valid"]):
        value = tuple(untag(case["value"]))
        blob1 = encode(case["types"], value)
        decoded = decode(case["types"], blob1)
        blob2 = encode(case["types"], decoded)
        if blob1 != blob2:
            bad.append(f"#{i} {case['types']} 二次编码不稳定")
    assert not bad, "\n".join(bad)


def test_empty_and_negatives_present(golden):
    """显式保证空串/空数组/负数等关键黄金向量确实存在并通过。"""
    tags = {
        ("bytes",): b"",
        ("string",): "",
        ("string[]",): [],
    }
    seen = set()
    for case in golden["valid"]:
        t = tuple(case["types"])
        v = untag(case["value"])
        if t in tags and normalize(v) == normalize([tags[t]]):
            seen.add(t)
        if t == ("int8",) and v == (-1,):
            seen.add(("int8-neg",))
    assert seen == {("bytes",), ("string",), ("string[]",), ("int8-neg",)}
