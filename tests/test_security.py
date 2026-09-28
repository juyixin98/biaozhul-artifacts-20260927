"""安全测试：畸形/恶意输入必须被拒绝并归类，且不触发越界或巨大分配。

每个用例断言**具体失败类别**，不是仅检查“接口能调用/抛了异常”。
"""

from __future__ import annotations

import resource
import tracemalloc

import pytest

from app.abi import decode, encode
from app.abi.errors import (
    ABIError,
    AllocationLimitError,
    NonCanonicalLayoutError,
    NonCanonicalPaddingError,
    OffsetOutOfBoundsError,
    OverlapError,
    UnsupportedTypeError,
)
from tests.conftest import untag

pytestmark = pytest.mark.security


def test_each_malformed_vector_has_exact_category(golden):
    """18 个手工恶意向量，逐一断言失败类别。"""
    failures = []
    for case in golden["malformed"]:
        blob = bytes.fromhex(case["blob_hex"])
        try:
            decode(case["types"], blob)
        except ABIError as e:
            if e.category != case["expected_category"]:
                failures.append(
                    f"{case['name']}: 类别 {e.category} != 期望 {case['expected_category']}"
                )
        except Exception as e:  # 非 ABI 异常也算失败（可能是崩溃）
            failures.append(f"{case['name']}: 抛出非受控异常 {type(e).__name__}: {e}")
        else:
            failures.append(f"{case['name']}: 恶意输入未被拒绝！")
    assert not failures, "恶意向量处理不符:\n" + "\n".join(failures)


# ---- 逐类别的精确断言（可读的失败类别）----

def _blob(hexstr):
    return bytes.fromhex(hexstr)


def test_uint_noncanonical_high_padding():
    # 高位字节非零
    with pytest.raises(NonCanonicalPaddingError):
        decode(["uint8"], _blob("01" + "00" * 31))


def test_uint8_256_out_of_range():
    with pytest.raises(NonCanonicalPaddingError):
        decode(["uint8"], (256).to_bytes(32, "big"))


def test_int_bad_sign_extension():
    with pytest.raises(NonCanonicalPaddingError):
        decode(["int8"], _blob("00" + "ff" * 31))


def test_bytesN_nonzero_tail():
    with pytest.raises(NonCanonicalPaddingError):
        decode(["bytes1"], _blob("ab" + "00" * 30 + "01"))


def test_offset_into_head_is_overlap():
    with pytest.raises(OverlapError):
        decode(["string"], _blob("00" * 64))


def test_offset_out_of_bounds():
    with pytest.raises(OffsetOutOfBoundsError):
        decode(["string"], _blob("00" * 31 + "ff"))


def test_huge_length_is_allocation_limit():
    # 2^256-1 的长度，必须命中硬上限而非尝试分配
    blob = _blob("00" * 31 + "20") + b"\xff" * 32
    with pytest.raises(AllocationLimitError):
        decode(["bytes"], blob)


def test_huge_array_count_is_allocation_limit():
    blob = _blob("00" * 31 + "20") + b"\xff" * 32
    with pytest.raises(AllocationLimitError):
        decode(["uint256[]"], blob)


def test_unsupported_types_rejected():
    with pytest.raises(UnsupportedTypeError):
        decode(["address"], b"\x00" * 32)
    with pytest.raises(UnsupportedTypeError):
        decode(["bool"], b"\x00" * 32)


def test_trailing_bytes_rejected():
    with pytest.raises(NonCanonicalLayoutError):
        decode(["uint256"], (1).to_bytes(32, "big") + b"\xff")


def test_truncated_word_oob():
    with pytest.raises(OffsetOutOfBoundsError):
        decode(["uint256"], b"\x00" * 31)


# ---- 资源安全：恶意 length 不触发巨大分配 ----

def test_huge_length_does_not_allocate():
    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()
    blob = _blob("00" * 31 + "20") + b"\xff" * 32  # 64 字节输入
    try:
        with pytest.raises(ABIError):
            decode(["bytes"], blob)
        snap_after = tracemalloc.take_snapshot()
        growth = sum(stat.size_diff for stat in
                     snap_after.compare_to(snap_before, "filename"))
        # 解码一个 64 字节输入，净堆增长必须远小于 1 MiB（硬上限）
        assert growth < 64 * 1024, f"疑似巨大分配：净增长 {growth} 字节"
    finally:
        tracemalloc.stop()


def test_negative_like_unsigned_is_padding_error():
    # 把 ff..ff 当作窄无符号类型 uint8 解码：高 255 位必须全 0 → 非规范
    with pytest.raises(NonCanonicalPaddingError):
        decode(["uint8"], b"\xff" * 32)
