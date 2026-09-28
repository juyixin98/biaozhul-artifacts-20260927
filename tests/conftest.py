"""测试共享辅助。

参考真值一律在测试里以独立的纯 Python 方式计算（集合/排序/计数），
不调用被测的 colstats.kernel，避免"用被测代码给自己出题"。
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures"


def f64_bits(v: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", v))[0]


def ref_nan_sign(v: float) -> int:
    """NaN 符号：-1 表示 -NaN，+1 表示 +NaN；非 NaN 返回 0。"""
    if not isinstance(v, float) or not math.isnan(v):
        return 0
    return -1 if f64_bits(v) >> 63 else 1


def ref_signed_zero(v: float) -> int | None:
    if isinstance(v, float) and v == 0.0:
        return -1 if math.copysign(1.0, v) < 0 else 1
    return None


def _f_total_key(v: float) -> int:
    """独立的 IEEE-754 total-order 整数键（标准映射，非被测代码）。"""
    b = f64_bits(v)
    if b >> 63:
        return 0xFFFFFFFFFFFFFFFF ^ b
    return (1 << 63) | b


def ref_min(values, physical_type):
    """独立参考最小：NaN 按 Parquet 规范排除，-0.0 与 +0.0 区分。"""
    vals = [
        v for v in values
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]
    if not vals:
        return None
    if physical_type == "DOUBLE":
        return min(vals, key=_f_total_key)
    if physical_type == "FLOAT":
        return min(
            vals,
            key=lambda v: struct.unpack("<I", struct.pack("<f", v))[0]
            if not struct.unpack("<I", struct.pack("<f", v))[0] >> 31
            else 0xFFFFFFFF ^ struct.unpack("<I", struct.pack("<f", v))[0],
        )
    return min(vals)


def ref_max(values, physical_type):
    vals = [
        v for v in values
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]
    if not vals:
        return None
    if physical_type == "DOUBLE":
        return max(vals, key=_f_total_key)
    if physical_type == "FLOAT":
        return max(
            vals,
            key=lambda v: struct.unpack("<I", struct.pack("<f", v))[0]
            if not struct.unpack("<I", struct.pack("<f", v))[0] >> 31
            else 0xFFFFFFFF ^ struct.unpack("<I", struct.pack("<f", v))[0],
        )
    return max(vals)


def ref_null_count(values):
    return sum(1 for v in values if v is None)


def codes_for(result, severity=None):
    out = [f.code for f in result.findings]
    if severity:
        want = severity.value if hasattr(severity, "value") else severity
        out = [f.code for f in result.findings if f.severity.value == want]
    return out


def find_locations(result, code):
    return [f.locator for f in result.findings if f.code == code]


@pytest.fixture(scope="session")
def fixture_models():
    """解析一次全部夹具，供多个测试复用。"""
    from colstats.parquet_adapter import parse_file

    return {
        "good": parse_file(FIXTURES / "good_stats" / "data.parquet"),
        "wrong": parse_file(FIXTURES / "wrong_stats" / "data.parquet"),
        "nostats": parse_file(FIXTURES / "no_stats" / "data.parquet"),
        "allnull": parse_file(FIXTURES / "all_null" / "data.parquet"),
        "nan": parse_file(FIXTURES / "mixed_nan" / "data.parquet"),
        "nan_bad": parse_file(FIXTURES / "mixed_nan" / "bad_nan_stats.parquet"),
        "trunc": parse_file(FIXTURES / "truncated_strings" / "data.parquet"),
        "trunc_bad": parse_file(
            FIXTURES / "truncated_strings" / "bad_truncated.parquet"
        ),
        "sort_bad": parse_file(FIXTURES / "sorting_wrong" / "data.parquet"),
    }
