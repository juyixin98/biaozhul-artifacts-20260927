"""Paging layer.

A *page* is a contiguous slice of a leaf column's slot stream. The contract
verified here is the Parquet record-boundary rule:

    A data page MUST begin on a top-level record boundary (repetition level 0)
    and MUST NOT split the slots of one record across two pages.

The pager therefore treats the configured ``page_slot_target`` as a *soft*
target: it cuts only before an ``R == 0`` slot. A single record larger than
the target is carried in one oversized page, which is reported explicitly
(``oversized=True``) rather than silently truncating the record.

The pager also re-verifies the invariant on the already-produced pages, so a
corrupted / externally constructed paging plan is rejected with
``PAGE_TRUNCATES_RECORD`` including the offending page index and position.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ErrorCode, error
from .kernel import EncodedTable, LeafColumn, Slot


@dataclass
class Page:
    index: int
    column: str
    slots: list[Slot] = field(default_factory=list)
    record_start: int = -1       # global record index of the first R=0 slot
    record_end: int = -1         # global record index of the last record
    oversized: bool = False

    @property
    def num_slots(self) -> int:
        return len(self.slots)

    def starts_new_record(self) -> bool:
        return bool(self.slots) and self.slots[0].r == 0


@dataclass
class PagedColumn:
    column: str
    pages: list[Page]


def _record_starts(slots: list[Slot]) -> list[int]:
    return [i for i, s in enumerate(slots) if s.r == 0]


def paginate_column(col: LeafColumn, target: int) -> PagedColumn:
    if target < 1:
        raise error(ErrorCode.INVALID_REQUEST, "page_slot_target must be >= 1")
    name = ".".join(col.node.path[1:])
    starts = _record_starts(col.slots)
    if not starts:
        # An empty table (zero records) legitimately has no slots/pages.
        if not col.slots:
            return PagedColumn(column=name, pages=[])
        raise error(
            ErrorCode.PAGE_INVARIANT_VIOLATION,
            "non-empty leaf column has no R=0 slot; cannot determine record "
            "boundaries",
            column=name,
        )
    boundaries = [0]
    cur_len = 0
    rec_no = -1
    for k, start in enumerate(starts):
        rec_no += 1
        end = starts[k + 1] if k + 1 < len(starts) else len(col.slots)
        rec_len = end - start
        if cur_len > 0 and cur_len + rec_len > target:
            boundaries.append(start)
            cur_len = 0
        cur_len += rec_len
    boundaries.append(len(col.slots))

    pages: list[Page] = []
    for p, (b, e) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        slots = col.slots[b:e]
        oversized = len(slots) > target
        first_rec = sum(1 for s in col.slots[:b] if s.r == 0)
        last_rec = first_rec + sum(1 for s in slots if s.r == 0) - 1
        pages.append(Page(index=p, column=name, slots=slots,
                          record_start=first_rec, record_end=last_rec,
                          oversized=oversized))
    result = PagedColumn(column=name, pages=pages)
    verify_pages(result, expected_records=len(starts))
    return result


def paginate_table(table: EncodedTable, target: int) -> list[PagedColumn]:
    return [paginate_column(c, target) for c in table.columns]


def verify_pages(paged: PagedColumn, expected_records: int) -> None:
    """Structural page invariant check; usable on externally built plans."""
    total_records = 0
    for pg in paged.pages:
        if not pg.slots:
            raise error(
                ErrorCode.PAGE_INVARIANT_VIOLATION, "empty page is not allowed",
                column=paged.column, page=pg.index,
            )
        if pg.slots[0].r != 0:
            raise error(
                ErrorCode.PAGE_TRUNCATES_RECORD,
                f"page {pg.index} starts with R={pg.slots[0].r}, not 0: "
                "the page truncates the parent record across a page boundary",
                column=paged.column, page=pg.index, slot=0,
                observed_r=pg.slots[0].r,
            )
        # No slot inside the page may carry R pointing to a record started in
        # a previous page while the page's own first record would then be
        # incomplete; equivalently the first slot is R=0 and every slot
        # belongs to a record that also has its R=0 within this page.
        seen_zero = False
        for i, s in enumerate(pg.slots):
            if s.r == 0:
                seen_zero = True
            elif not seen_zero:
                raise error(
                    ErrorCode.PAGE_TRUNCATES_RECORD,
                    f"page {pg.index} slot {i} repeats (R={s.r}) before any "
                    "record starts inside the page",
                    column=paged.column, page=pg.index, slot=i, observed_r=s.r,
                )
        total_records += sum(1 for s in pg.slots if s.r == 0)
    if total_records != expected_records:
        raise error(
            ErrorCode.PAGE_TRUNCATES_RECORD,
            f"pages cover {total_records} record starts, expected {expected_records}",
            column=paged.column, observed=total_records, expected=expected_records,
        )
