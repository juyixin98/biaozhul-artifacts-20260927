"""序号 16 位 / 时间戳 32 位独立展开与回绕计数。"""

from __future__ import annotations

import pytest

from app.timekit.unwrap import Unwrapper


def test_seq_forward_wrap_65535_to_0() -> None:
    u = Unwrapper(16)
    assert u.update(65534) == 65534
    assert u.update(65535) == 65535
    assert u.update(0) == 65536
    assert u.update(1) == 65537
    assert u.wrap_forward_events == 1


def test_ts_32bit_wrap_independent_of_seq() -> None:
    seq = Unwrapper(16)
    ts = Unwrapper(32)
    top = 0xFFFFFFFF
    assert ts.update(top) == top
    assert ts.update(0) == 1 << 32
    assert ts.wrap_forward_events == 1
    # 序号空间完全不受时间戳回绕影响
    assert seq.update(5) == 5


def test_first_wire_value_is_baseline_not_zero() -> None:
    u = Unwrapper(16)
    assert u.update(60000) == 60000
    # 60000 -> 60001 正常递增，保持原值
    assert u.update(60001) == 60001
    # 65535 -> 0 在同一基线上正向回绕
    assert u.update(65535) == 65535
    assert u.update(0) == 65536
    assert u.wrap_forward_events == 1


def test_backward_half_ring_is_recorded() -> None:
    u = Unwrapper(16)
    u.update(10)
    u.update(20)
    # 20 -> 10 的线差 -10 正常；这里构造真正跨半环的反向跳变
    val = u.update(60000)
    assert val < 20
    assert u.wrap_backward_events == 1


def test_wire_out_of_range_raises() -> None:
    u = Unwrapper(16)
    with pytest.raises(ValueError):
        u.update(1 << 16)
