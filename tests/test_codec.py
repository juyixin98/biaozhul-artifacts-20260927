"""解析器/编解码白盒测试：白名单穷举、最小编码、脚本小整数往返。"""

from __future__ import annotations

import pytest

from rsv.config import Limits
from rsv.encoding import SUPPORTED, encode_push
from rsv.encoding.script_codec import parse_script
from rsv.errors import VerificationFailure
from rsv.vm import decode_smallint, encode_smallint


def test_supported_opcode_list_is_explicit_and_small():
    # 白名单数量固定（变更时必须显式更新文档与本测试）
    assert len(SUPPORTED) == 48
    # 不存在任何“完整链”操作码（如 OP_CHECKSIGADD、OP_CLTV 等编号）
    for forbidden in (0xB2, 0xB1, 0x41 + 0x80, 0xBA, 0xC0, 0xFF):
        assert forbidden not in SUPPORTED


@pytest.mark.parametrize("data", [b"", b"\x01", b"a" * 0x4B, b"a" * 0x4C,
                                  b"a" * 0xFF, b"a" * 0x100, b"a" * 0xFFFF])
def test_push_encoding_roundtrip_and_minimality(data):
    # 大元素用放宽的资源预算；默认预算下 521B 元素单独在向量中测试
    relaxed = Limits(max_script_bytes=200_000, max_element_size=70_000)
    enc = encode_push(data)
    ins = parse_script(enc, relaxed)
    assert len(ins) == 1
    assert ins[0].data == data


def test_nonminimal_pushdata_rejected():
    # 5 字节本该直接推送，却用 PUSHDATA1
    with pytest.raises(VerificationFailure) as ei:
        parse_script(b"\x4c\x05abcde", Limits())
    assert ei.value.code.code == "input.malformed_push"


def test_truncated_push_rejected():
    with pytest.raises(VerificationFailure) as ei:
        parse_script(b"\x05ab", Limits())
    assert ei.value.code.code == "input.malformed_push"


def test_unknown_and_reserved_distinguished():
    with pytest.raises(VerificationFailure) as ei:
        parse_script(b"\xef", Limits())
    assert ei.value.code.code == "input.unknown_opcode"
    with pytest.raises(VerificationFailure) as ei:
        parse_script(b"\x50", Limits())
    assert ei.value.code.code == "input.reserved_opcode"


def test_script_too_large():
    with pytest.raises(VerificationFailure) as ei:
        parse_script(b"\x61" * 2049, Limits())
    assert ei.value.code.code == "input.script_too_large"


@pytest.mark.parametrize("n", [-1, 0, 1, 2, 16, 127, 128, 255, 256, 16384, -128])
def test_smallint_roundtrip(n):
    enc = encode_smallint(n)
    assert decode_smallint(enc) == n


def test_branch_depth_static_check():
    deep = b"".join(b"\x51\x63" for _ in range(9)) + b"\x68" * 9
    with pytest.raises(VerificationFailure) as ei:
        parse_script(deep, Limits())
    assert ei.value.code.code == "resource.script_depth_exceeded"


def test_unbalanced_if_static_check():
    with pytest.raises(VerificationFailure) as ei:
        parse_script(b"\x51\x63\x51", Limits())
    assert ei.value.code.code == "compute.unbalanced_if"
