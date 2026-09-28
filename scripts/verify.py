#!/usr/bin/env python3
"""
verify.py — 分层位图集合二进制格式 (.rbs) 的独立验证器（参考实现）。

本脚本完全独立于 Rust 内核：
  - 自行解析 RBS1 二进制布局（目录 + 负载 + CRC32C 尾校验）；
  - 用 Python `set` 重算基数与并/交/差；
  - 重新实现 rank / select 并验证二者互逆；
  - 校验魔数/版本/头部与体 CRC32C/标签/键序/偏移/长度/有序唯一/阈值/基数；
  - 与同名 .json 夹具（纯整数数组）交叉核对元素集合。

用法：
  python3 scripts/verify.py fixtures/                # 校验目录下全部 *.rbs
  python3 scripts/verify.py fixtures/foo.rbs [...]   # 校验指定文件
  python3 scripts/verify.py --pair a.rbs b.rbs --op intersect
       独立计算两个文件的交/并/差（仅打印基数，供与 HTTP 结果人工/脚本对照）

退出码：0 = 全部通过；1 = 校验失败（失败原因逐条打印为 FAIL ...）。
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

MAGIC = b"RBS1"
FORMAT_VERSION = 0x00010000
HEADER_LEN = 16
ENTRY_LEN = 16
TAG_ARRAY = 1
TAG_BITMAP = 2
ARRAY_MAX_CARDINALITY = 4096
BITMAP_WORDS = 1024
CONTAINER_BITS = 1 << 16
MAX_U32 = 0xFFFFFFFF


class CorruptFile(Exception):
    """带稳定错误代码的损坏异常（代码与 Rust/HTTP 侧同源）。"""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------- CRC32C（独立的表驱动实现） ----------------

def _build_crc_table() -> List[int]:
    poly = 0x82F63B78
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ poly if (crc & 1) else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _build_crc_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


# ---------------- 解析（严格校验） ----------------

class Container:
    __slots__ = ("key", "tag", "values")

    def __init__(self, key: int, tag: int, values: List[int]):
        self.key = key
        self.tag = tag
        self.values = values  # 已排序、去重的容器内（低16位）值

    @property
    def cardinality(self) -> int:
        return len(self.values)

    @property
    def kind(self) -> str:
        return "array" if self.tag == TAG_ARRAY else "bitmap"


def parse_rbs(data: bytes) -> Tuple[List[Container], int]:
    """返回 (容器列表, 总基数)。任何不符均抛 CorruptFile（稳定 code）。"""
    if len(data) < HEADER_LEN + 4:
        raise CorruptFile("corrupt_truncated",
                          f"need >= {HEADER_LEN + 4} bytes, have {len(data)}")

    magic = data[0:4]
    if magic != MAGIC:
        raise CorruptFile("corrupt_bad_magic", f"found {magic!r}")

    version = struct.unpack_from("<I", data, 4)[0]
    if version != FORMAT_VERSION:
        raise CorruptFile("corrupt_unsupported_version", f"{version:#010x}")

    count = struct.unpack_from("<I", data, 8)[0]
    stored_hcrc = struct.unpack_from("<I", data, 12)[0]
    if crc32c(data[0:12]) != stored_hcrc:
        raise CorruptFile("corrupt_header_checksum",
                          f"stored={stored_hcrc:#x} computed={crc32c(data[0:12]):#x}")

    dir_len = count * ENTRY_LEN
    body_len = len(data) - HEADER_LEN - 4
    if body_len < 0 or body_len < dir_len:
        raise CorruptFile("corrupt_truncated",
                          f"body {body_len} < directory {dir_len}")

    body = data[HEADER_LEN:HEADER_LEN + body_len]

    # 先读目录，做长度/偏移的结构边界校验（截断优先于 CRC 分类）。
    entries = []
    prev_key: Optional[int] = None
    expected_off = dir_len
    for i in range(count):
        base = i * ENTRY_LEN
        key, tag, reserved = struct.unpack_from("<HBB", body, base)
        plen, off, card = struct.unpack_from("<III", body, base + 4)

        if reserved != 0 or tag not in (TAG_ARRAY, TAG_BITMAP):
            raise CorruptFile("corrupt_unknown_container_tag", f"tag={tag} key={key}")
        if prev_key is not None and key <= prev_key:
            raise CorruptFile("corrupt_keys_not_sorted",
                              f"{prev_key} -> {key}")
        prev_key = key

        if tag == TAG_ARRAY:
            if plen % 2 != 0:
                raise CorruptFile("corrupt_bad_payload_length",
                                  f"key={key} odd array len {plen}")
            allowed = plen
        else:
            allowed = BITMAP_WORDS * 8
        if plen != allowed:
            raise CorruptFile("corrupt_bad_payload_length",
                              f"key={key} declared={plen} allowed={allowed}")

        end = off + plen
        if end > body_len:
            raise CorruptFile("corrupt_truncated",
                              f"key={key} payload end {end} > body {body_len}")
        if off < dir_len:
            raise CorruptFile("corrupt_bad_offset", f"key={key} offset={off}")
        if off != expected_off:
            raise CorruptFile("corrupt_bad_offset",
                              f"key={key} offset={off} expected={expected_off}")
        expected_off = end
        entries.append((key, tag, plen, off, card))

    if expected_off != body_len:
        raise CorruptFile("corrupt_bad_offset",
                          f"declared payloads end {expected_off} != body {body_len}")

    stored_bcrc = struct.unpack_from("<I", data, HEADER_LEN + body_len)[0]
    if crc32c(body) != stored_bcrc:
        raise CorruptFile("corrupt_body_checksum",
                          f"stored={stored_bcrc:#x} computed={crc32c(body):#x}")

    containers: List[Container] = []
    for key, tag, plen, off, card in entries:
        payload = body[off:off + plen]
        if tag == TAG_ARRAY:
            n = plen // 2
            values = list(struct.unpack_from(f"<{n}H", payload, 0))
            for idx in range(1, n):
                if values[idx] <= values[idx - 1]:
                    raise CorruptFile("corrupt_array_not_sorted",
                                      f"key={key} index={idx}")
            if n > ARRAY_MAX_CARDINALITY:
                raise CorruptFile("corrupt_threshold_violation",
                                  f"array key={key} card={n} > {ARRAY_MAX_CARDINALITY}")
            if card != n:
                raise CorruptFile("corrupt_cardinality_mismatch",
                                  f"key={key} declared={card} actual={n}")
            containers.append(Container(key, TAG_ARRAY, values))
        else:
            words = list(struct.unpack_from(f"<{BITMAP_WORDS}Q", payload, 0))
            actual = sum(w.bit_count() for w in words)
            if actual != card:
                raise CorruptFile("corrupt_cardinality_mismatch",
                                  f"key={key} declared={card} actual={actual}")
            if actual <= ARRAY_MAX_CARDINALITY:
                raise CorruptFile("corrupt_threshold_violation",
                                  f"bitmap key={key} card={actual} <= threshold")
            values = []
            for wi, w in enumerate(words):
                while w:
                    low = wi * 64 + (w & -w).bit_length() - 1
                    values.append(low)
                    w &= w - 1
            containers.append(Container(key, TAG_BITMAP, values))

    return containers, sum(c.cardinality for c in containers)


def full_value_set(containers: List[Container]) -> set:
    out = set()
    for c in containers:
        base = c.key << 16
        for low in c.values:
            out.add(base | low)
    return out


# ---------------- rank / select 独立语义 ----------------

def rank(values: List[int], x: int) -> int:
    """已序列表中严格小于 x 的个数（二分）。"""
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def container_rank(c: Container, x: int) -> int:
    if x >= CONTAINER_BITS:
        return c.cardinality
    return rank(c.values, x)


def set_rank(containers: List[Container], x: int) -> int:
    key = (x >> 16) & 0xFFFF
    low = x & 0xFFFF
    total = 0
    for c in containers:
        if c.key < key:
            total += c.cardinality
        elif c.key == key:
            total += container_rank(c, low)
        else:
            break
    return total


def set_select(containers: List[Container], i: int) -> Optional[int]:
    for c in containers:
        if i < c.cardinality:
            return (c.key << 16) | c.values[i]
        i -= c.cardinality
    return None


# ---------------- 单文件校验 ----------------

def verify_file(path: str, json_path: Optional[str] = None) -> Tuple[bool, dict]:
    with open(path, "rb") as f:
        data = f.read()
    containers, total = parse_rbs(data)
    values = sorted(full_value_set(containers))

    assert len(values) == total, "cardinality sum disagrees with value set"
    # 全局有序唯一（容器间键升序已保证，这里再显式核对）。
    assert all(values[i] < values[i + 1] for i in range(len(values) - 1))

    # 1) rank/select 互逆（对每个真实元素）。
    for i, v in enumerate(values):
        r = set_rank(containers, v)
        if r != i:
            raise AssertionError(f"rank({v})={r}, want {i}")
        s = set_select(containers, i)
        if s != v:
            raise AssertionError(f"select({i})={s}, want {v}")

    # 边界 rank：rank(0) 恒为 0；rank(MAX) 排除 MAX 自身。
    if values:
        assert set_rank(containers, 0) == 0
        assert set_rank(containers, MAX_U32) == total - (1 if MAX_U32 in values else 0)
    # select 越界
    assert set_select(containers, total) is None

    # 2) 与 JSON 夹具交叉核对。
    if json_path and os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            expected = set(json.load(f))
        if any(v < 0 or v > MAX_U32 for v in expected):
            raise AssertionError("json fixture contains out-of-range values")
        if set(values) != expected:
            miss = expected - set(values)
            extra = set(values) - expected
            raise AssertionError(
                f"binary/json mismatch: missing={sorted(miss)[:5]} extra={sorted(extra)[:5]}"
            )

    kinds = {"array": sum(c.tag == TAG_ARRAY for c in containers),
             "bitmap": sum(c.tag == TAG_BITMAP for c in containers)}
    return True, {
        "file": path,
        "cardinality": total,
        "containers": len(containers),
        "kinds": kinds,
        "min": values[0] if values else None,
        "max": values[-1] if values else None,
    }


def iter_rbs(targets: List[str]) -> List[str]:
    files = []
    for t in targets:
        if os.path.isdir(t):
            for name in sorted(os.listdir(t)):
                if name.endswith(".rbs"):
                    files.append(os.path.join(t, name))
        else:
            files.append(t)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="Independent .rbs verifier")
    ap.add_argument("targets", nargs="*", help="files or directories")
    ap.add_argument("--pair", nargs=2, metavar=("A", "B"),
                    help="independently compute set ops for two files")
    ap.add_argument("--op", choices=["union", "intersect", "difference"],
                    default="union")
    args = ap.parse_args()

    if not args.targets and not args.pair:
        ap.print_help()
        return 2

    failures = 0
    checked = 0
    for path in iter_rbs(args.targets):
        json_path = path[:-4] + ".json"
        try:
            _, summary = verify_file(path, json_path)
            checked += 1
            print(f"PASS {os.path.basename(path)}: "
                  f"card={summary['cardinality']} containers={summary['containers']} "
                  f"{summary['kinds']} range=[{summary['min']},{summary['max']}]")
        except (CorruptFile, AssertionError, OSError, struct.error, json.JSONDecodeError) as e:
            failures += 1
            print(f"FAIL {path}: {e}")

    if args.pair:
        try:
            a = full_value_set(parse_rbs(open(args.pair[0], "rb").read())[0])
            b = full_value_set(parse_rbs(open(args.pair[1], "rb").read())[0])
            res = {"union": a | b, "intersect": a & b, "difference": a - b}[args.op]
            print(f"PAIR {os.path.basename(args.pair[0])} {args.op} "
                  f"{os.path.basename(args.pair[1])}: cardinality={len(res)}")
        except CorruptFile as e:
            failures += 1
            print(f"FAIL pair: {e}")

    print(f"\nsummary: {checked} file(s) verified, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
