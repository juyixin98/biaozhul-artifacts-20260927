"""Page planning for leaf columns.

Parquet's repetition/definition levels only make sense relative to record
boundaries, and a single record's nested list can span many leaf slots. A
data page must therefore never end in the middle of a record. This module
plans page boundaries in units of *leaf events*, grouping whole records.

Two modes:

* ``target_size_bytes`` (production default): close the current page once the
  encoded byte estimate reaches the target AND at least one complete record
  has been placed; a single record larger than the target still stays whole in
  its own oversized page (page boundaries never split it).
* ``force_page_after_records`` (test only): deterministically place a boundary
  after every N records so tests can exercise reassembly of deeply nested
  records across pages, including cases deliberately larger than the target.
"""
from __future__ import annotations

from dataclasses import dataclass

from .levels import EncodedColumn, LeafEvent


@dataclass(frozen=True)
class PagePlan:
    # Start/end event indices for the page (end exclusive).
    event_start: int
    event_end: int
    # Index of the first record on the page and number of records it contains.
    first_record_index: int
    record_count: int


def plan_pages(column: EncodedColumn,
               target_size_bytes: int | None = None,
               force_page_after_records: int | None = None) -> list[PagePlan]:
    """Split one leaf column's events into record-aligned pages.

    Every leaf column is split identically by record index: all columns must
    agree on record boundaries (RL == 0 on the first event of each record).
    """
    events = column.events
    # Record start event indices and record index per event.
    record_starts = [i for i, ev in enumerate(events) if ev.repetition_level == 0]
    if not record_starts:
        return []
    record_starts.append(len(events))
    total_records = len(record_starts) - 1

    plans: list[PagePlan] = []
    page_first_record = 0
    current_size = 0

    for rec in range(total_records):
        start = record_starts[rec]
        end = record_starts[rec + 1]
        record_events = events[start:end]
        record_size = _estimate_record_bytes(record_events, column)

        boundary = False
        if force_page_after_records is not None:
            placed = rec - page_first_record + 1
            if placed >= force_page_after_records and rec < total_records - 1:
                boundary = True
        elif target_size_bytes is not None and rec > page_first_record:
            if current_size + record_size >= target_size_bytes:
                boundary = True

        current_size += record_size
        if boundary:
            plans.append(PagePlan(
                event_start=record_starts[page_first_record],
                event_end=end,
                first_record_index=page_first_record,
                record_count=rec - page_first_record + 1,
            ))
            page_first_record = rec + 1
            current_size = 0

    plans.append(PagePlan(
        event_start=record_starts[page_first_record],
        event_end=len(events),
        first_record_index=page_first_record,
        record_count=total_records - page_first_record,
    ))
    return plans


def _estimate_record_bytes(events: list[LeafEvent],
                           column: EncodedColumn) -> int:
    """Rough per-record byte cost for page sizing (values dominate)."""
    leaf = column.leaf
    # two level streams (~2 bytes each after framing overhead) plus value bytes
    value_bytes = 0
    for ev in events:
        if ev.value is None:
            continue
        phys = leaf.node.physical.value
        if phys in ("INT32", "FLOAT"):
            value_bytes += 4
        elif phys in ("INT64", "DOUBLE"):
            value_bytes += 8
        elif phys == "BOOLEAN":
            value_bytes += 1
        elif phys == "BYTE_ARRAY":
            value_bytes += 4 + len(str(ev.value).encode("utf-8"))
    return 8 + value_bytes
