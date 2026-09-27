#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立黄金向量预言机（independent oracle）。

用途：以 Python + hashlib（OpenSSL 后端）独立实现 Cuckoo 哈希内核的定位口径，
生成固定测试向量。Rust 侧 tests/kernel_golden.rs 把这些向量当作**外部夹具**读入并
逐项断言——参考答案不允许由被测的 Rust 核心实现自己生成，从而能抓住「测试与实现
犯了同一个错误」这类问题。

口径必须与 src/hashing.rs 的模块文档逐字节一致：
  H(ctx, seed, body...) = SHA256(u16le(len(ctx)) || ctx || u16le(32) || seed || body)
  H_index       body = u32le(len(key)) || key
  H_fingerprint body = u32le(len(key)) || key
  H_alt         body = u32le(f_bits) || u64le(m) || u32le(fp)   # 不含当前桶号！
  i1  = le64(H_index) & (m-1)
  fp  = 1 + (le64(H_fp) mod (2^f - 1))
  i2  = (i1 XOR le64(H_alt)) & (m-1)

用法：
  python3 tests/golden/golden_oracle.py            # 生成/刷新 tests/golden/golden_vectors.json
  python3 tests/golden/golden_oracle.py --print    # 打印到 stdout

该脚本不依赖任何 Rust 代码。
"""

import argparse
import hashlib
import json
import os
import struct
import sys

CTX_INDEX = b"cf1.index.v1"
CTX_FP = b"cf1.fingerprint.v1"
CTX_ALT = b"cf1.alt.v1"

# 与 Rust 测试内核固定种子一致（合成夹具，非生产秘密）。
SEED = bytes.fromhex("0123456789abcdef" * 4)


def _h(ctx: bytes, seed: bytes, body: bytes) -> bytes:
    pre = struct.pack("<H", len(ctx)) + ctx + struct.pack("<H", len(seed)) + seed
    return hashlib.sha256(pre + body).digest()


def primary_index(key: bytes, seed: bytes, m: int) -> int:
    d = _h(CTX_INDEX, seed, struct.pack("<I", len(key)) + key)
    return struct.unpack("<Q", d[:8])[0] & (m - 1)


def fingerprint(key: bytes, seed: bytes, f_bits: int) -> int:
    modulus = (1 << f_bits) - 1
    d = _h(CTX_FP, seed, struct.pack("<I", len(key)) + key)
    return 1 + (struct.unpack("<Q", d[:8])[0] % modulus)


def alt_index(i: int, fp: int, seed: bytes, m: int, f_bits: int) -> int:
    # 哈希只依赖指纹，不依赖 i：保证 (i XOR h(fp)) 对合。
    body = struct.pack("<IQI", f_bits, m, fp)
    d = _h(CTX_ALT, seed, body)
    return (i ^ struct.unpack("<Q", d[:8])[0]) & (m - 1)


# 参数集：覆盖不同桶数/指纹宽度；key 包含 ASCII、UTF-8 多字节与二进制样式输入。
PARAM_SETS = [
    {"name": "m16_f8", "num_buckets": 16, "fingerprint_bits": 8},
    {"name": "m64_f12", "num_buckets": 64, "fingerprint_bits": 12},
    {"name": "m256_f8", "num_buckets": 256, "fingerprint_bits": 8},
    {"name": "m1024_f16", "num_buckets": 1024, "fingerprint_bits": 16},
]

KEYS = [
    "",
    "a",
    "alpha",
    "user:1001",
    "订单-2026-0001",  # UTF-8 多字节
    "モーニング",
    "🦀-cuckoo",
    "x" * 64,
    "edge:  spaces  ",
    "binary-ish:\x00\x01\x02",
]


def generate() -> dict:
    vectors = []
    for ps in PARAM_SETS:
        m, f = ps["num_buckets"], ps["fingerprint_bits"]
        for idx, key in enumerate(KEYS):
            kb = key.encode("utf-8")
            i1 = primary_index(kb, SEED, m)
            fp = fingerprint(kb, SEED, f)
            i2 = alt_index(i1, fp, SEED, m, f)
            # 对合性：Python 侧先自检，i1 必须能从 i2 反推。
            assert alt_index(i2, fp, SEED, m, f) == i1, "oracle 自身对合性失败"
            assert 1 <= fp <= (1 << f) - 1, "指纹越界"
            assert 0 <= i1 < m and 0 <= i2 < m, "桶号越界"
            vectors.append(
                {
                    "set": ps["name"],
                    "key_index": idx,
                    "key_utf8": key,
                    "key_bytes_len": len(kb),
                    "num_buckets": m,
                    "bucket_size": 4,
                    "fingerprint_bits": f,
                    "seed_hex": SEED.hex(),
                    "i1": i1,
                    "i2": i2,
                    "fingerprint": fp,
                }
            )
    return {
        "schema": "deletable-cuckoo-golden-v1",
        "kernel_version": 1,
        "hash": "sha256",
        "note": "由独立 Python 预言机生成；Rust 测试不得生成这些期望值。",
        "vectors": vectors,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="do_print", action="store_true")
    args = ap.parse_args()
    doc = generate()
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False)
    if args.do_print:
        print(text)
        return 0
    out = os.path.join(os.path.dirname(__file__), "golden_vectors.json")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(f"wrote {len(doc['vectors'])} vectors -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
