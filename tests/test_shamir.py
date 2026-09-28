"""Shamir 数学性质测试（对照独立预言机，而非自证）。"""
from __future__ import annotations

import itertools

import pytest

from threshold_service.gf import FieldParams
from threshold_service.shamir import (
    ShareError,
    interpolate_at_zero,
    split_secret,
)
from tests import oracle_reference as oracle

PARAMS = FieldParams()
SECRETS = [b"secret", bytes(range(32)), "你好，门槛分享".encode("utf-8"), b"\x00\xff" * 8]


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("threshold,share_count", [(2, 3), (3, 5), (4, 4)])
def test_every_qualifying_subset_reconstructs_the_same_secret(secret, threshold, share_count):
    """枚举小配置下所有门限子集：恢复结果必须全部等于原秘密（逐字节）。"""
    points = split_secret(secret, threshold, share_count, PARAMS)
    recovered_values = set()
    for combo in itertools.combinations(points, threshold):
        recovered = interpolate_at_zero(list(combo), PARAMS)
        assert recovered == secret
        recovered_values.add(recovered)
    assert recovered_values == {secret}


@pytest.mark.parametrize("threshold,share_count", [(2, 3), (3, 5)])
def test_below_threshold_does_not_reveal_secret(threshold, share_count):
    """门限以下的任何子集都不能恢复（对全零之外秘密，统计上应得错误值）。"""
    secret = b"top-secret-value"
    points = split_secret(secret, threshold, share_count, PARAMS)
    for size in range(1, threshold):
        for combo in itertools.combinations(points, size):
            candidate = interpolate_at_zero(list(combo), PARAMS)
            assert candidate != secret


def test_oracle_and_library_agree_on_split_points():
    """同一组 x、同一独立 LCG 系数：库插值与手写预言机对同一批点结论一致。

    这里让预言机自己造点，被测实现负责插值；再反向让被测实现造点、
    预言机插值——双向交叉，杜绝"答案全由被测核心生成"。
    """
    secret = b"cross-check-1234"
    xs = [17, 42, 99, 123, 200]
    threshold = 3

    # 方向 1：预言机造份额 -> 被测实现恢复
    oracle_points = oracle.oracle_split(secret, threshold, xs, seed=7)
    for combo in itertools.combinations(oracle_points, threshold):
        assert interpolate_at_zero(list(combo), PARAMS) == secret

    # 方向 2：被测实现造份额 -> 预言机恢复
    library_points = split_secret(secret, threshold, len(xs), PARAMS, xs=xs)
    for combo in itertools.combinations(library_points, threshold):
        assert oracle.oracle_interpolate_zero(list(combo)) == secret


def test_duplicate_x_rejected_at_interpolation_layer():
    points = split_secret(b"abc", 2, 2, PARAMS, xs=[1, 2])
    dup = [points[0], (1, b"zzz")]
    with pytest.raises(ShareError, match="duplicate x"):
        interpolate_at_zero(dup, PARAMS)


def test_input_validation():
    with pytest.raises(ShareError):
        split_secret(b"", 2, 3, PARAMS)
    with pytest.raises(ShareError):
        split_secret(b"x", 1, 3, PARAMS)
    with pytest.raises(ShareError):
        split_secret(b"x", 4, 3, PARAMS)
    with pytest.raises(ShareError):
        split_secret(b"x", 2, 3, PARAMS, xs=[1, 1, 2])
    with pytest.raises(ShareError):
        split_secret(b"x", 2, 3, PARAMS, xs=[0, 1, 2])
