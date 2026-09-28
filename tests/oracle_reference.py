"""独立测试预言机（reference oracle）。

关键独立性要求：参考答案**不能**全部由被测核心自身生成。
本模块与 threshold_service.shamir / gf 没有任何代码复用：
- GF(2^8) 乘法用教科书式的 xtime（俄罗斯农民乘法）手写实现，
  并带自校验 FIPS-197 已知答案；
- 多项式求值、拉格朗日插值独立重写；
- 用确定性的独立 LCG 生成测试多项式（不使用 secrets/被测 RNG）。

它只被测试使用，生产代码绝不导入。
"""
from __future__ import annotations

from typing import Iterable

# AES 不可约多项式低 8 位（x^8 项隐含在移位处理中）。
_RIJNDAEL_LOW = 0x1B


def gf_mul(a: int, b: int) -> int:
    """GF(2^8)/0x11B 乘法：逐位异或 + 条件归约（独立实现）。"""
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= _RIJNDAEL_LOW
        b >>= 1
    return product


def gf_inv(a: int) -> int:
    """穷举找乘法逆元（仅测试用，故意走与库不同的路径）。"""
    if a == 0:
        raise ZeroDivisionError
    for candidate in range(1, 256):
        if gf_mul(a, candidate) == 1:
            return candidate
    raise AssertionError("unreachable in a field")


def gf_div(a: int, b: int) -> int:
    return gf_mul(a, gf_inv(b))


# ---- FIPS-197 §4.2 已知答案，证明本预言机的域运算是标准 GF(2^8) ----
FIPS197_VECTORS = {
    (0x57, 0x83): 0xC1,
    (0x57, 0x13): 0xFE,
    (0x53, 0xCA): 0x01,
    (0x02, 0x87): 0x15,
}


def assert_field_self_consistent() -> None:
    for (a, b), expected in FIPS197_VECTORS.items():
        assert gf_mul(a, b) == expected, (hex(a), hex(b))
    for a in range(1, 256):
        assert gf_mul(a, gf_inv(a)) == 1
        assert gf_mul(a, 1) == a
        assert gf_mul(a, 0) == 0


# ---- 独立的确定性伪随机（绝不与被测实现共享随机源） ----
class _LCG:
    """Numerical Recipes LCG；仅用于测试数据，无安全用途。"""

    def __init__(self, seed: int = 0x1234ABCD):
        self.state = seed & 0xFFFFFFFF

    def byte(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return (self.state >> 16) & 0xFF


def oracle_split(secret: bytes, threshold: int, xs: Iterable[int],
                 seed: int = 0x1234ABCD) -> list[tuple[int, bytes]]:
    """独立分片：每字节一个由 LCG 系数构成的多项式。"""
    rng = _LCG(seed)
    xs = list(xs)
    out: list[tuple[int, list[int]]] = [(x, []) for x in xs]
    for byte in secret:
        coeffs = [byte] + [rng.byte() for _ in range(threshold - 1)]
        for slot, x in enumerate(xs):
            value = 0
            for coeff in reversed(coeffs):  # Horner
                value = gf_mul(value, x) ^ coeff
            out[slot][1].append(value)
    return [(x, bytes(values)) for x, values in out]


def oracle_interpolate_zero(points: list[tuple[int, bytes]]) -> bytes:
    """独立拉格朗日 f(0)；要求 x 互不相同。"""
    xs = [p[0] for p in points]
    if len(set(xs)) != len(xs):
        raise ValueError("duplicate x")
    width = len(points[0][1])
    weights = []
    for i, xi in enumerate(xs):
        num = den = 1
        for j, xj in enumerate(xs):
            if i != j:
                num = gf_mul(num, xj)
                den = gf_mul(den, xi ^ xj)  # GF(2^m) 减法即异或
        weights.append(gf_div(num, den))
    secret = bytearray(width)
    for col in range(width):
        acc = 0
        for weight, (_, y) in zip(weights, points):
            acc ^= gf_mul(weight, y[col])
        secret[col] = acc
    return bytes(secret)
