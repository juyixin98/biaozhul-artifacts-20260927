#!/usr/bin/env python3
"""金标向量生成器：由*独立朴素参考实现*（tests/reference）生成，不由被测内核生成。

输出 samples/golden_vectors.json，测试在运行时断言被测服务与离线验证器都能
复现这些具体根哈希与证明判定。修改协议标签/深度会导致本文件需要重新生成，
而测试失败会明确指出“协议变更需同步更新金标”。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.reference.naive_smt import NaiveSMT  # noqa: E402

OUT = ROOT / "samples" / "golden_vectors.json"


def bitmap_hex(depth: int, levels: int) -> str:
    nbytes = (depth + 7) // 8
    bm = bytearray(nbytes)
    for d in range(levels):
        bm[d // 8] |= 1 << (7 - d % 8)
    return bm.hex()


def main() -> None:
    # 小参数（1 字节键、8 位深）金标：手工可读，覆盖所有分支形状
    small = NaiveSMT(key_len=1, depth=8)
    vectors: dict = {"schema": "smt-golden/v1", "small_depth8": {}, "full_depth256": {}}

    def entry(mapping, key):
        root = small.root_of(mapping)
        pf = small.prove(mapping, key)
        ok = small.verify(root, key, pf)
        assert ok
        return {
            "key": key.hex(),
            "root": root.hex(),
            "proof": {
                "schema": "smt-proof/v1",
                "key_len": 1,
                "depth": 8,
                "key": key.hex(),
                "root": root.hex(),
                "end": pf.end,
                "bitmap": bitmap_hex(8, pf.levels),
                "siblings": [s.hex() for s in pf.siblings],
                "value": None if pf.value is None else pf.value.hex(),
                "collision_key": None if pf.collision_key is None else pf.collision_key.hex(),
                "collision_value": None if pf.collision_value is None else pf.collision_value.hex(),
            },
        }

    vectors["small_depth8"]["empty_root"] = small.empty_root.hex()
    vectors["small_depth8"]["single_key_05"] = entry({b"\x05": b"v5"}, b"\x05")
    vectors["small_depth8"]["nonmember_06_against_05"] = entry({b"\x05": b"v5"}, b"\x06")
    m = {b"\x05": b"v5", b"\x07": b"v7"}  # 00000101 / 00000111 共享 5 位前缀
    vectors["small_depth8"]["shared_prefix_05_07_key05"] = entry(m, b"\x05")
    vectors["small_depth8"]["shared_prefix_05_07_key07"] = entry(m, b"\x07")
    vectors["small_depth8"]["empty_value_key00"] = entry({b"\x00": b""}, b"\x00")

    # 全尺寸（32 字节键、256 位深）：两键共享 255 位前缀（只差最后一位）
    full = NaiveSMT(key_len=32, depth=256)
    ka = b"\x11" * 31 + b"\x00"
    kb = b"\x11" * 31 + b"\x01"
    mapping256 = {ka: b"alpha", kb: b"beta"}
    root256 = full.root_of(mapping256)
    pfa = full.prove(mapping256, ka)
    pfb = full.prove(mapping256, kb)
    assert full.verify(root256, ka, pfa) and full.verify(root256, kb, pfb)
    vectors["full_depth256"] = {
        "key_a": ka.hex(),
        "key_b": kb.hex(),
        "shared_prefix_bits": 255,
        "root": root256.hex(),
        "levels_a": pfa.levels,
        "levels_b": pfb.levels,
        "proof_a": {
            "schema": "smt-proof/v1", "key_len": 32, "depth": 256,
            "key": ka.hex(), "root": root256.hex(), "end": pfa.end,
            "bitmap": bitmap_hex(256, pfa.levels),
            "siblings": [s.hex() for s in pfa.siblings],
            "value": pfa.value.hex(),
            "collision_key": None, "collision_value": None,
        },
        "proof_b": {
            "schema": "smt-proof/v1", "key_len": 32, "depth": 256,
            "key": kb.hex(), "root": root256.hex(), "end": pfb.end,
            "bitmap": bitmap_hex(256, pfb.levels),
            "siblings": [s.hex() for s in pfb.siblings],
            "value": pfb.value.hex(),
            "collision_key": None, "collision_value": None,
        },
        "empty_root": full.empty_root.hex(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    print("depth8 empty root:", small.empty_root.hex())
    print("depth256 root(255-shared pair):", root256.hex())


if __name__ == "__main__":
    main()
