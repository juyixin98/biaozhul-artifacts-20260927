"""有理数 timescale 换算测试：精确性（不经过浮点）。"""

from fractions import Fraction

import pytest

from mp4timeline.timeline.rational import format_fraction, rescale, to_seconds


class TestRescale:
    def test_exact_division(self):
        assert rescale(48000, 48000, 1000) == Fraction(1000)

    def test_non_divisible_keeps_fraction(self):
        # 3000/90000 s -> 100/3 ms，必须保持为精确分数
        result = rescale(3000, 90000, 1000)
        assert result == Fraction(100, 3)
        assert (result.numerator, result.denominator) == (100, 3)

    def test_audio_frame_exact(self):
        assert rescale(960, 48000, 1000) == Fraction(20)

    def test_sign_preserved(self):
        assert rescale(-100, 1000, 1000) == Fraction(-100)

    def test_zero(self):
        assert rescale(0, 90000, 1000) == Fraction(0)

    def test_invalid_timescale(self):
        with pytest.raises(ValueError):
            rescale(1, 0, 1000)
        with pytest.raises(ValueError):
            rescale(1, 1000, -5)

    def test_to_seconds(self):
        assert to_seconds(45000, 90000) == Fraction(1, 2)
        assert to_seconds(Fraction(100, 3), 1000) == Fraction(1, 30)

    def test_roundtrip_identity(self):
        # 90000 -> 1000 -> 90000 往返必须恒等（分数不丢精度）
        v = Fraction(123456789)
        assert rescale(rescale(v, 90000, 1000), 1000, 90000) == v

    def test_format(self):
        assert format_fraction(Fraction(3, 1)) == "3"
        assert format_fraction(Fraction(100, 3)) == "100/3"
