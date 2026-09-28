"""Transform-layer tests: negative-safe month arithmetic, zone boundaries,
pinned versions, and the anti-pattern of treating bucket ids as values.
"""

from __future__ import annotations

import datetime as dt

import pytest

from prune import values as V
from prune.transforms import (MonthTransform, TRANSFORM_SPEC_VERSION,
                              tzdb_version)


def test_tzdb_is_pinned_tzdata_2024_2():
    # A changed IANA database can shift historical offsets; the plan refuses
    # to silently mix versions. Assert the exact pinned version string.
    assert tzdb_version() == "tzdata2024.2"


def test_transform_spec_version_constant():
    assert TRANSFORM_SPEC_VERSION == "month-tz-v1"


def test_negative_epoch_month_label_shanghai():
    t = MonthTransform("ts", "Asia/Shanghai")
    # 1969-12-31 23:30 UTC == 1970-01-01 07:30 Shanghai: crosses into Jan bucket
    d = dt.datetime(1969, 12, 31, 23, 30, tzinfo=dt.timezone.utc)
    assert t.apply(d) == "1970-01"
    # 1969-12-15 00:00 UTC == 1969-12-15 08:00 Shanghai
    d2 = dt.datetime(1969, 12, 15, tzinfo=dt.timezone.utc)
    assert t.apply(d2) == "1969-12"


def test_month_index_arithmetic_negative_indices():
    mi = MonthTransform.month_index
    lab = MonthTransform.label_of
    assert mi("1969-12") == 1969 * 12 + 11
    assert lab(mi("1969-12")) == "1969-12"
    assert lab(mi("1970-01") - 1) == "1969-12"
    assert lab(0) == "0000-01"
    # adjacent month labels ordered by index (lex order == chronological
    # order for zero-padded labels, including before year 0000 padding edge)
    assert mi("1969-12") < mi("1970-01")


def test_label_range_covers_zone_boundary():
    t = MonthTransform("ts", "Asia/Shanghai")
    lo = V.canonical("2024-03-31T23:30:00+08:00", V.DATETIME)
    hi = V.canonical("2024-04-01T00:30:00+08:00", V.DATETIME)
    a, b = t.month_span_for_bounds(lo, hi)
    assert (a, b) == ("2024-03", "2024-04")


def test_utc_vs_shanghai_partition_difference():
    # Same instant is bucketed differently in different zones — this is why
    # the tz version + name are part of the transform identity.
    instant = dt.datetime(2024, 3, 4, 16, 30, tzinfo=dt.timezone.utc)  # 00:30 SH
    assert MonthTransform("ts", "UTC").apply(instant) == "2024-03"
    assert MonthTransform("ts", "Asia/Shanghai").apply(instant) == "2024-03"
    edge = dt.datetime(2024, 3, 31, 16, 30, tzinfo=dt.timezone.utc)  # 00:30 Apr 1 SH
    assert MonthTransform("ts", "UTC").apply(edge) == "2024-03"
    assert MonthTransform("ts", "Asia/Shanghai").apply(edge) == "2024-04"


def test_null_source_maps_to_null_not_to_a_bucket():
    t = MonthTransform("ts", "Asia/Shanghai")
    assert t.apply(None) is None


def test_bucket_index_must_not_be_compared_to_values():
    # Documented invariant guard: an integer epoch must never be tested
    # against month-index numbers. Canonical timestamp and label live on
    # different domains; values.cmp reports incomparability for stray cases.
    epoch = V.canonical(0, V.DATETIME)
    assert V.same_domain(epoch, epoch)
    assert V.same_domain(epoch, "1970-01") is False
    assert V.cmp(epoch, "1970-01") is None
