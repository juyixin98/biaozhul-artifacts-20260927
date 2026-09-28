"""Decode / round-trip verification.

``decode_encoding`` expands a :class:`GlobalEncoding` back to per-batch
(typed value | NULL) rows.  ``verify_roundtrip`` checks each batch decodes
identically to its original rows -- the assertion the tests depend on:
*every decoded row must equal the row of the batch that was encoded*.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import BatchInput, GlobalEncoding


@dataclass(frozen=True)
class BatchDecode:
    batch_id: str
    # Each row is either None (NULL) or the typed dictionary value.
    rows: tuple[object | None, ...]


def decode_encoding(enc: GlobalEncoding) -> list[BatchDecode]:
    out: list[BatchDecode] = []
    for rb in enc.batches:
        rows: list[object | None] = []
        for code, is_valid in zip(rb.global_indices, rb.valid):
            if not is_valid:
                rows.append(None)
            else:
                rows.append(enc.global_values[code])
        out.append(BatchDecode(batch_id=rb.batch_id, rows=tuple(rows)))
    return out


def original_rows(b: BatchInput) -> tuple[object | None, ...]:
    rows: list[object | None] = []
    for idx, is_valid in zip(b.indices, b.valid):
        rows.append(None if not is_valid else b.values[idx])
    return tuple(rows)


@dataclass(frozen=True)
class Mismatch:
    batch_id: str
    row: int
    expected: object
    actual: object


@dataclass(frozen=True)
class RoundtripReport:
    all_match: bool
    mismatches: tuple[Mismatch, ...]
    checked_rows: int


def verify_roundtrip(enc: GlobalEncoding,
                     originals: dict[str, BatchInput]) -> RoundtripReport:
    """Decoded rows must match originals; mismatch rows are reported exactly."""
    decoded = {d.batch_id: d.rows for d in decode_encoding(enc)}
    mismatches: list[Mismatch] = []
    checked = 0
    for b in originals.values():
        expected = original_rows(b)
        actual = decoded.get(b.batch_id)
        if actual is None or len(actual) != len(expected):
            mismatches.append(Mismatch(
                batch_id=b.batch_id, row=-1,
                expected=f"{len(expected)} rows",
                actual="missing batch" if actual is None
                       else f"{len(actual)} rows"))
            continue
        checked += len(expected)
        for row, (e, a) in enumerate(zip(expected, actual)):
            if e != a or (e is None) is not (a is None):
                mismatches.append(Mismatch(b.batch_id, row, e, a))
    return RoundtripReport(
        all_match=not mismatches,
        mismatches=tuple(mismatches),
        checked_rows=checked,
    )
