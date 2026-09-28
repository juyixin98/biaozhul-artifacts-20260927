"""Partition transforms: column value -> partition value.

Only one transform ships today: **month partitioning in a fixed IANA zone**
(``month_tz``). Partition *values* are strings ``"YYYY-MM"`` — they are
transforms of the original timestamp column, never the raw column. The kernel's
job is to invert that transform conservatively: given a predicate on the
original column, derive candidate month labels such that every label that
*could* contain a matching value is included. Bucket numbers/indices are an
internal optimization only and must never be compared against literal values.

Versions (bumped on any semantic change; stored in the catalog and reported on
every plan):

  * ``TRANSFORM_SPEC_VERSION`` — this code's transform definition.
  * tzdb version — the IANA time-zone database the offsets were computed with.
"""

from __future__ import annotations

import datetime as _dt
import importlib.metadata as _md
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import values as V

# Bump whenever the transform math / label format / null policy changes.
TRANSFORM_SPEC_VERSION = "month-tz-v1"


def tzdb_version() -> str:
    """IANA tzdb version in effect (pinned via tzdata==2024.2)."""
    try:
        ver = _md.version("tzdata")
    except _md.PackageNotFoundError:  # system zoneinfo: surface that fact
        return "system-zoneinfo"
    return f"tzdata{ver}"


@dataclass(frozen=True)
class MonthTransform:
    """Partition spec: bucket = calendar month of ``source_column`` in ``tz``.

    NULL source values go into the explicit ``null_label`` directory.
    """

    source_column: str
    tz_name: str
    kind: str = "month_tz"
    null_label: str = "__null__"

    @property
    def spec_version(self) -> str:
        return TRANSFORM_SPEC_VERSION

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown time zone {self.tz_name!r}") from None

    # ---- forward direction: original value -> partition label -------------

    def apply(self, value: object) -> str | None:
        if value is None:
            return None
        d = V.canonical(value, V.DATETIME)
        local = d.astimezone(self.zone())
        return f"{local.year:04d}-{local.month:02d}"

    # ---- inversion: month index arithmetic (safe for negative epochs) -----

    @staticmethod
    def month_index(label: str) -> int:
        y, m = label.split("-")
        return int(y) * 12 + (int(m) - 1)

    @staticmethod
    def label_of(index: int) -> str:
        y, m0 = divmod(index, 12)
        return f"{y:04d}-{m0 + 1:02d}"

    def label_range(self, lo: _dt.datetime, hi: _dt.datetime) -> tuple[str, str]:
        """Conservative month-label span covering every ts in [lo, hi].

        Both endpoints are converted into the partition zone. Because a month
        boundary in local time is a well-defined wall-clock instant, the first
        candidate is lo's month and the last is hi's month; this stays exact
        (and trivially conservative) for closed ranges.
        """
        a = lo.astimezone(self.zone())
        b = hi.astimezone(self.zone())
        ia = a.year * 12 + (a.month - 1)
        ib = b.year * 12 + (b.month - 1)
        if ib < ia:
            ia, ib = ib, ia
        return self.label_of(ia), self.label_of(ib)

    def month_span_for_bounds(
        self, lower: object, upper: object
    ) -> tuple[str | None, str | None]:
        """Map canonical datetime bounds (inclusive) to an inclusive label span."""
        if lower is not None:
            lo_lab = self.apply(lower)
        else:
            lo_lab = None
        if upper is not None:
            hi_lab = self.apply(upper)
        else:
            hi_lab = None
        if lo_lab and hi_lab and self.month_index(hi_lab) < self.month_index(lo_lab):
            lo_lab, hi_lab = hi_lab, lo_lab
        return lo_lab, hi_lab
