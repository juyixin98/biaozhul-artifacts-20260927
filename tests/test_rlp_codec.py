"""RLP 编解码测试。

独立性：被测对象是 ``localtxpool.encoding`` 中**手写**的 RLP；
这里引入第三方 ``rlp`` 包作为**独立预言机**（它不被任何运行时代码导入），
与手写实现互编互解。若两者答案不一致则测试失败——参考答案不来自被测核心。
"""

from __future__ import annotations

import pytest

rlp_lib = pytest.importorskip("rlp")  # 仅测试期需要的独立实现

from localtxpool.encoding import (
    rlp_decode_exact,
    rlp_encode,
    _decode_uint,
    TxDecodeError,
)


# 手写实现与第三方 rlp 库对同一输入必须编码一致
@pytest.mark.parametrize("item", [
    b"",
    b"\x00",
    b"dog",
    b"\x7f",
    b"\x80",
    b"a" * 55,
    b"a" * 56,
    b"a" * 1024,
    [b"", [], [b"cat", b"dog"], [[]], b"Lorem"],
    [b"a", b"", b"b", [b"c", [b"d", b"e"]], b"f" * 57],
])
def test_handwritten_matches_third_party_rlp(item):
    mine = rlp_encode(item)
    theirs = rlp_lib.encode(item)
    assert mine == theirs


@pytest.mark.parametrize("item", [
    b"", b"\x00", b"dog", b"a" * 55, b"a" * 56, b"a" * 1000,
    [b"cat", b"dog", [b"x"], []],
])
def test_decode_roundtrip_matches_third_party(item):
    encoded = rlp_lib.encode(item)
    assert rlp_decode_exact(encoded) == item
    # 第三方库解码我们的编码，结果也必须一致
    assert rlp_lib.decode(rlp_encode(item)) == item


def test_uint_canonical_zero():
    assert _decode_uint(b"", name="x") == 0


@pytest.mark.parametrize("bad", [
    b"\x00",            # 整数 0 的非规范单零
    b"\x00\x01",        # 前导零
    b"\x00\xff",
])
def test_uint_rejects_leading_zero(bad):
    with pytest.raises(TxDecodeError):
        _decode_uint(bad, name="n")


# 严格解码必须拒绝的非规范 / 畸形字节流
@pytest.mark.parametrize("hexstr", [
    "817f",                 # 单字节 <0x80 却用了短字符串形式
    "b800",                 # 长字符串前缀但长度字段为 0（空串应编码为 0x80）
    "b80038" + "00" * 56,   # 长度字节前导零
    "c000",                 # 空列表后多余字节
    "830102",               # 声明 3 字节但只有 2
    "f800",                 # 长列表长度字段为 0（空列表应编码为 0xc0）
])
def test_decode_rejects_malformed(hexstr):
    with pytest.raises(TxDecodeError):
        rlp_decode_exact(bytes.fromhex(hexstr))


def test_long_string_length_56_uses_and_decodes_long_form():
    # 56 字节恰好必须使用长字符串形式，且必须能严格解码
    encoded = rlp_encode(b"a" * 56)
    assert encoded[:1] == b"\xb8"
    assert rlp_decode_exact(encoded) == b"a" * 56

