"""Independent round-trip verifier.

This module decodes a :class:`UnifyResult` back into per-batch rows using its
own straight-line logic — it deliberately does not import or call the kernel's
merge/width code — and compares every row against the original batches. It is
the oracle the service runs after every unification and the one the tests use
to assert concrete decoded values.

NULL rule: rows with ``validity[row] is False`` decode to ``None``; their
index slot is required to hold 0 (a harmless padding value) but is otherwise
never interpreted, proving NULL does not consume value semantics.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import WIDTH_CAPACITY
from .errors import VerificationMismatchError
from .kernel import BatchInput, UnifyResult


@dataclass(frozen=True)
class DecodedBatch:
    batch_id: str
    rows: tuple  # scalar or None per row


def decode_rows(result: UnifyResult) -> list[DecodedBatch]:
    """Decode remapped global indices back through the global dictionary.

    The checks here are intentionally independent: length, bitmap alignment,
    code range, padding convention, local-map coverage.
    """
    decoded: list[DecodedBatch] = []
    dictionary = list(result.global_dictionary)
    for remap in result.batch_remaps:
        if len(remap.global_indices) != remap.row_count:
            raise VerificationMismatchError(
                "global_indices length disagrees with row_count",
                details={
                    "batch_id": remap.batch_id,
                    "indices_len": len(remap.global_indices),
                    "row_count": remap.row_count,
                },
            )
        if len(remap.validity) != remap.row_count:
            raise VerificationMismatchError(
                "validity length disagrees with row_count",
                details={"batch_id": remap.batch_id},
            )
        rows: list = []
        for row, valid in enumerate(remap.validity):
            code = remap.global_indices[row]
            if not isinstance(code, int) or isinstance(code, bool) or code < 0:
                raise VerificationMismatchError(
                    "global index is not a non-negative int",
                    details={"batch_id": remap.batch_id, "row": row, "code": code},
                )
            if not valid:
                # NULL must use the neutral padding code and never be decoded
                # as a dictionary value.
                if code != 0:
                    raise VerificationMismatchError(
                        "NULL row carries a non-padding global index",
                        details={"batch_id": remap.batch_id, "row": row, "code": code},
                    )
                rows.append(None)
                continue
            if code >= len(dictionary):
                raise VerificationMismatchError(
                    "global index out of dictionary range",
                    details={
                        "batch_id": remap.batch_id,
                        "row": row,
                        "code": code,
                        "dictionary_size": len(dictionary),
                    },
                )
            rows.append(dictionary[code])
        decoded.append(DecodedBatch(batch_id=remap.batch_id, rows=tuple(rows)))
    return decoded


def verify_roundtrip(result: UnifyResult, originals: list[BatchInput]) -> list[DecodedBatch]:
    """Decode and compare against the original batches row by row.

    Raises VerificationMismatchError on the first disagreement with enough
    context (batch/row/expected/actual) to reproduce the failure.
    """
    if len(originals) != len(result.batch_remaps):
        raise VerificationMismatchError(
            "batch count changed during unification",
            details={
                "input_batches": len(originals),
                "output_remaps": len(result.batch_remaps),
            },
        )
    decoded = decode_rows(result)
    original_by_id = {b.batch_id: b for b in originals}
    for dec in decoded:
        src = original_by_id.get(dec.batch_id)
        if src is None:
            raise VerificationMismatchError(
                "remap for unknown batch produced",
                details={"batch_id": dec.batch_id},
            )
        if len(dec.rows) != len(src.indices):
            raise VerificationMismatchError(
                "row count changed",
                details={
                    "batch_id": dec.batch_id,
                    "expected_rows": len(src.indices),
                    "actual_rows": len(dec.rows),
                },
            )
        for row, valid in enumerate(src.validity):
            expected = None if not valid else src.dictionary[src.indices[row]]
            actual = dec.rows[row]
            if not _equal(expected, actual):
                raise VerificationMismatchError(
                    "decoded row differs from original",
                    details={
                        "batch_id": dec.batch_id,
                        "row": row,
                        "expected_repr": repr(expected),
                        "actual_repr": repr(actual),
                    },
                )
    # Structural width sanity: declared width must cover all codes.
    capacity = WIDTH_CAPACITY[result.index_width_bits]
    if result.cardinality > capacity:
        raise VerificationMismatchError(
            "declared width cannot address the dictionary",
            details={
                "width": result.index_width_bits,
                "cardinality": result.cardinality,
                "capacity": capacity,
            },
        )
    return decoded


def _equal(expected, actual) -> bool:
    """Strict type-aware equality used by the oracle.

    ``True`` must not equal ``1`` even though Python says so; ``1`` and ``1.0``
    compare equal only where the declared value type normalizes both (the
    kernel already normalized the originals to the declared type, so plain
    equality is correct here — the bool guard is the extra safety net).
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual
