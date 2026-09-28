"""Paging tests: pages never split a parent record across the boundary."""
from __future__ import annotations

import pytest

from app.core.errors import ErrorCode, StructuredError
from app.core.kernel import encode_table
from app.core.pager import (
    Page,
    PagedColumn,
    paginate_table,
    verify_pages,
)
from app.core.schema import Schema


def _schema():
    return Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "list",
         "item": {"name": "element", "type": "int32"}},
        {"name": "b", "type": "struct", "children": [
            {"name": "x", "type": "int64"},
            {"name": "y", "type": "string"},
        ]},
    ]})


def test_every_page_starts_at_record_boundary():
    sch = _schema()
    recs = [{"a": [i, i + 1, i + 2] if i % 5 else [],
             "b": {"x": i, "y": f"v{i}"}} for i in range(100)]
    table = encode_table(sch, recs)
    for pc in paginate_table(table, target=7):
        covered: set[int] = set()
        for p in pc.pages:
            assert p.slots[0].r == 0, "page must begin on a new record"
            covered.update(range(p.record_start, p.record_end + 1))
        assert covered == set(range(100))


def test_records_not_duplicated_or_lost():
    sch = _schema()
    recs = [{"a": [i, i + 1], "b": {"x": i, "y": "z"}} for i in range(50)]
    table = encode_table(sch, recs)
    for pc in paginate_table(table, target=3):
        all_slots = [s for p in pc.pages for s in p.slots]
        # exact same ordered slots as the source column
        src = next(c for c in table.columns
                   if ".".join(c.node.path[1:]) == pc.column)
        assert [(s.d, s.r) for s in all_slots] == \
               [(s.d, s.r) for s in src.slots]


def test_oversized_record_flagged_not_split():
    sch = _schema()
    # One huge record larger than the target -> single oversized page.
    recs = [{"a": list(range(50)), "b": {"x": 0, "y": "z"}},
            {"a": [1], "b": {"x": 1, "y": "q"}}]
    table = encode_table(sch, recs)
    a_pages = next(pc for pc in paginate_table(table, target=5)
                   if pc.column == "a.list.element")
    first = a_pages.pages[0]
    assert first.oversized is True
    # the oversized page is still one atomic record
    assert first.record_start == 0 and first.record_end == 0
    assert first.slots[0].r == 0


def test_verify_pages_rejects_mid_record_start():
    sch = _schema()
    recs = [{"a": [1, 2, 3, 4], "b": {"x": 0, "y": "z"}},
            {"a": [5], "b": {"x": 1, "y": "q"}}]
    table = encode_table(sch, recs)
    src = next(c for c in table.columns
               if ".".join(c.node.path[1:]) == "a.list.element")
    starts = [i for i, s in enumerate(src.slots) if s.r == 0]
    n_starts = len(starts)
    # Build an invalid plan whose second page begins at a continuation slot.
    cut = next(i for i, s in enumerate(src.slots) if s.r == 1)
    bad = PagedColumn(column="a.list.element", pages=[
        Page(index=0, column="a", slots=src.slots[:cut]),
        Page(index=1, column="a", slots=src.slots[cut:]),
    ])
    with pytest.raises(StructuredError) as ei:
        verify_pages(bad, expected_records=n_starts)
    assert ei.value.code == ErrorCode.PAGE_TRUNCATES_RECORD
    # The error identifies the offending page and slot.
    assert ei.value.location["column"] == "a.list.element"
    assert ei.value.location["page"] == 1


def test_empty_table_pages():
    sch = _schema()
    table = encode_table(sch, [])
    paged = paginate_table(table, target=10)
    assert all(pc.pages == [] for pc in paged)
