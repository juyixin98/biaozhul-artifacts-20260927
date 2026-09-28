"""Shamir 秘密分享核心（GF(2^8)，逐字节多项式）。

约定：
- 秘密的每个字节独立构造一个 (threshold-1) 次随机多项式，
  常数项为该明文字节，份额点为 (x, y_bytes)。
- 横坐标 x ∈ [1, 255]，0 预留给秘密（截距）。
- 同一集合内横坐标唯一；恢复时按**去重后的横坐标数量**计阈值，
  重复横坐标不重复计数（见 policy 模块）。

注意：原始 Shamir 只提供保密性，不提供完整性/真实性——
恶意份额可让插值得到任意错误常数项。完整性由 integrity 模块独立追加。
"""
from __future__ import annotations

import secrets

from .gf import MAX_SHARES, FieldParams, add, div, get_field, mul


class ShareError(ValueError):
    """分片/重组参数层面的错误。"""


def split_secret(
    secret: bytes,
    threshold: int,
    share_count: int,
    params: FieldParams,
    *,
    xs: list[int] | None = None,
    rng: secrets.SystemRandom | None = None,
) -> list[tuple[int, bytes]]:
    """把 secret 切成 share_count 个 (x, y) 份额，门限为 threshold。

    xs 可显式指定横坐标（测试用）；否则安全随机选取且互不相同。
    """
    if not isinstance(secret, (bytes, bytearray)) or len(secret) == 0:
        raise ShareError("secret must be a non-empty byte string")
    if threshold < 2:
        raise ShareError("threshold must be >= 2 (1-of-n is not a threshold scheme)")
    if share_count < threshold:
        raise ShareError("share_count must be >= threshold")
    if share_count > MAX_SHARES:
        raise ShareError(f"share_count must be <= {MAX_SHARES} in GF(2^8)")

    rng = rng or secrets.SystemRandom()
    if xs is None:
        chosen_xs = rng.sample(range(1, MAX_SHARES + 1), share_count)
    else:
        chosen_xs = list(xs)
        if len(chosen_xs) != share_count or len(set(chosen_xs)) != share_count:
            raise ShareError("xs must contain exactly share_count distinct values")
        if not all(1 <= x <= MAX_SHARES for x in chosen_xs):
            raise ShareError(f"every x must be in [1, {MAX_SHARES}]")

    field = get_field(params)
    shares: list[tuple[int, bytes]] = []
    for x in chosen_xs:
        shares.append((x, bytearray()))
    for byte in secret:
        # 系数：常数项为秘密字节，其余 threshold-1 个随机。
        coeffs = [byte] + [rng.randrange(256) for _ in range(threshold - 1)]
        for idx, x in enumerate(chosen_xs):
            y = _evaluate(field, coeffs, x)
            shares[idx][1].append(y)
    return [(x, bytes(y)) for x, y in shares]


def interpolate_at_zero(
    points: list[tuple[int, bytes]], params: FieldParams
) -> bytes:
    """拉格朗日插值求 f(0)。points 中横坐标必须互不相同。

    调用方（policy 内核）负责：阈值检查、去重、集合一致性、完整性校验。
    本函数不做任何信任判断——喂给它什么点，它就机械地恢复出什么常数项。
    """
    if not points:
        raise ShareError("need at least one point")
    xs = [p[0] for p in points]
    if len(set(xs)) != len(xs):
        raise ShareError("duplicate x among interpolation points")
    width = len(points[0][1])
    if any(len(y) != width for _, y in points):
        raise ShareError("all y vectors must have equal length")
    field = get_field(params)

    # 预计算每个点的拉格朗日权重 l_i(0)，对所有字节复用。
    weights = []
    for i, (xi, _) in enumerate(points):
        numerator = 1
        denominator = 1
        for j, (xj, _) in enumerate(points):
            if i == j:
                continue
            numerator = field.Multiply(numerator, xj)
            denominator = field.Multiply(denominator, add(params, xi, xj))  # xi XOR xj
        weights.append(div(params, numerator, denominator))

    secret = bytearray(width)
    for col in range(width):
        acc = 0
        for weight, (_, y) in zip(weights, points):
            acc = field.Add(acc, mul(params, weight, y[col]))
        secret[col] = acc
    return bytes(secret)


def _evaluate(field, coeffs: list[int], x: int) -> int:
    """Horner 法求多项式在 x 处的值。"""
    result = 0
    for coeff in reversed(coeffs):
        result = field.Add(field.Multiply(result, x), coeff)
    return result
