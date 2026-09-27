"""Pin modular arithmetic independent of buffering policy."""
import pytest

from app.time_kernel import DriftModel, ms_to_ticks, wrap_delta


@pytest.mark.parametrize("bits", [16, 32])
def test_wrap_delta_no_crossing(bits):
    mod = 1 << bits
    assert wrap_delta(5, 1, bits) == 4
    assert wrap_delta(1, 5, bits) == -4
    assert wrap_delta(mod - 1, mod - 2, bits) == 1


def test_wrap_delta_crosses_boundary_16():
    # 2 - 65534 == 4 mod 65536
    assert wrap_delta(2, 0xFFFE, 16) == 4
    # 65534 - 2 == -4
    assert wrap_delta(0xFFFE, 2, 16) == -4


def test_wrap_delta_crosses_boundary_32():
    assert wrap_delta(3, 0xFFFFFFFE, 32) == 5
    assert wrap_delta(0xFFFFFFFE, 3, 32) == -5


def test_wrap_delta_half_space_is_negative():
    # exactly half a cycle resolves negative by the signed convention
    assert wrap_delta(0x8000, 0, 16) == -0x8000


def test_drift_model_slow_sender():
    # sender runs 2% slow: 100 sender-ms spans 102.04 receiver-ms
    m = DriftModel(anchor_sender_ms=0, anchor_wall_ms=1000, skew=0.98)
    assert m.to_wall_ms(100) == pytest.approx(1000 + 100 / 0.98)


def test_tick_ms_roundtrip():
    assert ms_to_ticks(10, 8000) == 80
    from app.time_kernel import ts_to_ms
    assert ts_to_ms(80, 8000) == pytest.approx(10.0)
