"""Hand-computed rational time-base conversions (independent references)."""
from fractions import Fraction

import pytest

from app.core.timebase import rescale, to_seconds, to_ticks
from app.errors import FailureCategory, PlannerError

T90K = Fraction(1, 90000)
T48K = Fraction(1, 48000)
T25 = Fraction(1, 25)


def test_seconds_round_trip():
    assert to_seconds(3000, T90K) == Fraction(1, 30)
    assert to_seconds(1024, T48K) == Fraction(1024, 48000)


def test_rescale_hand_computed():
    # 3000 ticks @1/90000 = 1/30 s = 1600 ticks @1/48000
    assert rescale(3000, T90K, T48K) == 1600
    # one video frame at 30fps expressed in audio ticks
    assert rescale(30000, T90K, T48K) == 16000
    assert rescale(0, T90K, T48K) == 0


def test_to_ticks_exact():
    assert to_ticks(Fraction(2, 15), T48K) == 6400      # trim case
    assert to_ticks(Fraction(1, 5), T90K) == 18000      # open-GOP cut
    assert to_ticks(Fraction(4, 15), T90K) == 24000


def test_inexact_conversion_is_an_error():
    with pytest.raises(PlannerError) as exc:
        to_ticks(Fraction(1, 1000), Fraction(1, 44100))
    assert exc.value.category is FailureCategory.INPUT_ERROR


def test_25fps_time_base_values():
    assert to_seconds(1, T25) == Fraction(1, 25)
    assert to_ticks(Fraction(1, 25), T25) == 1
