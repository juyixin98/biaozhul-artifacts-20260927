"""timescale 之间的精确换算。

所有换算用 :class:`fractions.Fraction` 完成，不经过浮点，
保证 90000/44100 等非整除比例下结果精确、可逐项断言。
"""

from __future__ import annotations

from fractions import Fraction


def rescale(value: int | Fraction, from_timescale: int, to_timescale: int) -> Fraction:
    """把 ``value``（``from_timescale`` 单位）换算为 ``to_timescale`` 单位。

    返回最简分数；整除时分母为 1。
    """

    if from_timescale <= 0 or to_timescale <= 0:
        raise ValueError(f"timescale 必须为正: {from_timescale}, {to_timescale}")
    return Fraction(value) * to_timescale / from_timescale


def to_seconds(value: int | Fraction, timescale: int) -> Fraction:
    """把 timescale 单位换算为秒（精确分数）。"""

    return rescale(value, timescale, 1)


def format_fraction(frac: Fraction) -> str:
    """分数的可读形式：整数直接写，否则 ``num/den``。"""

    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"
