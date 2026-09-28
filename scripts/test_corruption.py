#!/usr/bin/env python3
"""
test_corruption.py — 用 Python 独立构造损坏样本，验证 verify.py / rbtool
对每一类损坏给出稳定的错误代码。损坏样本由 Python 手工拼装（不经过 Rust 内核），
因此这是对“参考实现自身的拒绝路径”和 Rust 解码器分类一致性的交叉检查。

用法：python3 scripts/test_corruption.py fixtures/
退出码：0 全部按预期分类；1 有偏差。
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify import (  # noqa: E402
    BITMAP_WORDS,
    ENTRY_LEN,
    HEADER_LEN,
    TAG_ARRAY,
    TAG_BITMAP,
    crc32c,
    parse_rbs,
)


# parse_rbs 抛 verify.CorruptFile，直接复用其 code 断言。
def expect(path_code: str, data: bytes, expected_code: str) -> str:
    try:
        parse_rbs(data)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        if code == expected_code:
            return f"PASS {path_code}: rejected as {code}"
        return f"FAIL {path_code}: expected {expected_code}, got {code} ({e})"
    return f"FAIL {path_code}: corrupt input was ACCEPTED"


def make(entries, tamper_header=lambda b: None, tamper_body=lambda b: None):
    """entries: list of (key, tag, payload_bytes, card)。目录偏移自动连续。"""
    count = len(entries)
    dir_len = count * ENTRY_LEN
    out = bytearray()
    out += b"RBS1"
    out += struct.pack("<I", 0x00010000)
    out += struct.pack("<I", count)
    out += struct.pack("<I", crc32c(bytes(out[:12])))
    assert len(out) == HEADER_LEN

    cursor = dir_len
    payloads = []
    for key, tag, payload, card in entries:
        out += struct.pack("<HBB", key, tag, 0)
        out += struct.pack("<III", len(payload), cursor, card)
        payloads.append(payload)
        cursor += len(payload)
    body_start = HEADER_LEN
    for p in payloads:
        out += p
    body = bytes(out[body_start:])
    out += struct.pack("<I", crc32c(body))
    tamper_header(out)
    tamper_body(out)
    return bytes(out)


def arr_payload(vals):
    return struct.pack(f"<{len(vals)}H", *vals)


def bit_payload(cardinality):
    # 任意基数 <=65536 的位图：放最小的 cardinality 个值
    words = [0] * BITMAP_WORDS
    for i in range(cardinality):
        words[i // 64] |= 1 << (i % 64)
    return struct.pack(f"<{BITMAP_WORDS}Q", *words)


def main() -> int:
    good_arr = (1, TAG_ARRAY, arr_payload([1, 2, 3]), 3)
    good_bit = (400, TAG_BITMAP, bit_payload(5000), 5000)
    good = make([good_arr, good_bit])

    results = []

    # 截断
    for cut in (0, 4, 15, 19, len(good) - 1):
        results.append(expect(f"truncate@{cut}", good[:cut], "corrupt_truncated"))

    # 魔数
    bad = bytearray(good)
    bad[0] = ord("X")
    results.append(expect("magic", bytes(bad), "corrupt_bad_magic"))

    # 版本
    bad = bytearray(good)
    struct.pack_into("<I", bad, 4, 99)
    results.append(expect("version", bytes(bad), "corrupt_unsupported_version"))

    # 头 CRC
    bad = bytearray(good)
    bad[8] = (bad[8] + 1) & 0xFF
    results.append(expect("header_crc", bytes(bad), "corrupt_header_checksum"))

    # 体 CRC（翻转一个负载字节）
    bad = bytearray(good)
    bad[HEADER_LEN + 2 * ENTRY_LEN + 5] ^= 0xFF
    results.append(expect("body_crc", bytes(bad), "corrupt_body_checksum"))

    # 未知标签
    results.append(expect(
        "unknown_tag", make([(1, 9, arr_payload([1]), 1)]), "corrupt_unknown_container_tag"))

    # 键重复 / 逆序
    results.append(expect(
        "dup_key", make([(5, TAG_ARRAY, arr_payload([1]), 1),
                         (5, TAG_ARRAY, arr_payload([2]), 1)]),
        "corrupt_keys_not_sorted"))
    results.append(expect(
        "desc_key", make([(9, TAG_ARRAY, arr_payload([1]), 1),
                          (2, TAG_ARRAY, arr_payload([2]), 1)]),
        "corrupt_keys_not_sorted"))

    # 数组未排序 / 重复
    results.append(expect(
        "array_unsorted", make([(1, TAG_ARRAY, arr_payload([1, 5, 3]), 3)]),
        "corrupt_array_not_sorted"))
    results.append(expect(
        "array_dup", make([(1, TAG_ARRAY, arr_payload([4, 4]), 2)]),
        "corrupt_array_not_sorted"))

    # 数组超阈值（4097 个元素）
    results.append(expect(
        "array_over_threshold",
        make([(1, TAG_ARRAY, arr_payload(list(range(4097))), 4097)]),
        "corrupt_threshold_violation"))

    # 位图低于阈值
    results.append(expect(
        "bitmap_under_threshold",
        make([(1, TAG_BITMAP, bit_payload(10), 10)]),
        "corrupt_threshold_violation"))

    # 基数不一致
    results.append(expect(
        "array_card_mismatch",
        make([(1, TAG_ARRAY, arr_payload([1, 2, 3]), 4)]),
        "corrupt_cardinality_mismatch"))
    results.append(expect(
        "bitmap_card_mismatch",
        make([(1, TAG_BITMAP, bit_payload(5000), 4999)]),
        "corrupt_cardinality_mismatch"))

    # 长度不符
    results.append(expect(
        "bitmap_bad_len", make([(1, TAG_BITMAP, b"\x00" * 100, 0)]),
        "corrupt_bad_payload_length"))
    results.append(expect(
        "array_odd_len", make([(1, TAG_ARRAY, b"\x00" * 5, 0)]),
        "corrupt_bad_payload_length"))

    # 偏移指向目录区
    def off_to_dir(out):
        # 第一个目录项偏移（体区起点 HEADER_LEN+8）置 0
        struct.pack_into("<I", out, HEADER_LEN + 8, 0)

    results.append(expect(
        "bad_offset", make([good_arr], tamper_body=off_to_dir),
        "corrupt_bad_offset"))

    # 对照组：合法文件必须成功解析
    try:
        cont, card = parse_rbs(good)
        assert card == 5003 and len(cont) == 2
        results.append("PASS well_formed_control: accepted, card=5003")
    except Exception as e:  # noqa: BLE001
        results.append(f"FAIL well_formed_control: {e}")

    failures = 0
    for r in results:
        print(r)
        if r.startswith("FAIL"):
            failures += 1
    print(f"\nsummary: {len(results) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
