"""测试夹具与独立参考实现。

参考实现刻意不使用 acstream 包内任何代码：用 Python ``bytes.find`` 逐模式、
逐起点朴素搜索，作为 Aho-Corasick 结果的对照预言机（oracle）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让测试在不安装包的情况下也能导入 acstream。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def naive_find_all(patterns: list[tuple[str, bytes]], data: bytes) -> list[tuple[str, int, int]]:
    """逐模式朴素搜索（包含重叠命中），返回 (pattern_id, start, end) 列表。

    与实现无关：直接使用 bytes.find 从每个可能起点继续扫描。
    """
    result: list[tuple[str, int, int]] = []
    for pid, needle in patterns:
        assert len(needle) > 0
        start = 0
        while True:
            idx = data.find(needle, start)
            if idx == -1:
                break
            result.append((pid, idx, idx + len(needle)))
            # 前进 1 字节而非整词长度，以保留自重叠（如 'aa' in 'aaa'）。
            start = idx + 1
    result.sort(key=lambda t: (t[2], -t[1], t[0]))
    return result


def split_into_chunks(data: bytes, sizes: list[int]) -> list[bytes]:
    """按给定块大小序列切分；最后一块吃掉剩余字节（可能为空）。"""
    chunks: list[bytes] = []
    pos = 0
    for size in sizes:
        chunks.append(data[pos : pos + size])
        pos += size
    chunks.append(data[pos:])
    return chunks


# 一组固定的、风格各异的分块方案（覆盖 1 字节块、质数块、大尾块等）。
CHUNK_PLANS: dict[str, list[int]] = {
    "whole": [10**9],
    "single_bytes": [1] * 64,
    "twos": [2] * 32,
    "primes": [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31],
    "odd_tail": [4, 4, 4, 3],
    "empty_first": [0, 3, 5],
}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch) -> Path:
    db = tmp_path / "test.db"
    monkeypatch.setenv("AC_DB_PATH", str(db))
    # 每个测试用独立游标密钥，验证篡改检测更稳定。
    monkeypatch.setenv("AC_CURSOR_SECRET", "unit-test-secret-key-0123456789ab")
    return db
