"""Independent buffer validation ("format inspection").

The validator never asks the kernel under test whether data is valid and does
not import the buffers into Arrow for the verdict: it inspects raw bytes with
plain Python/numpy. Its three structural checks are deliberately separate so a
failure names the exact layer:

* validity bitmap: presence, covering length, padding bits zero
* offsets (utf8 only): integer count, first offset zero, non-decreasing,
  within data bounds
* data buffer: covering length (fixed width) or final offset (utf8), plus
  UTF-8 well-formedness of every present string span
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pyarrow as pa

from arrowzero.kernel import OFFSET_WIDTH, STRING_TYPE, is_supported, primitive_spec, validity_byte_size
from arrowzero.kernel.bitmap import count_padding_errors, count_set_bits


class ViolationCode(str, Enum):
    TYPE_UNSUPPORTED = "TYPE_UNSUPPORTED"
    INVALID_LENGTH = "INVALID_LENGTH"
    BUFFER_LAYOUT = "BUFFER_LAYOUT"
    MISSING_BUFFER = "MISSING_BUFFER"
    BUFFER_TOO_SHORT = "BUFFER_TOO_SHORT"
    INVALID_PADDING = "INVALID_PADDING"
    INVALID_FIRST_OFFSET = "INVALID_FIRST_OFFSET"
    DECREASING_OFFSET = "DECREASING_OFFSET"
    OFFSET_OUT_OF_BOUNDS = "OFFSET_OUT_OF_BOUNDS"
    UTF8_INVALID = "UTF8_INVALID"
    NULL_COUNT_MISMATCH = "NULL_COUNT_MISMATCH"


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    layer: str  # "validity" | "offsets" | "data" | "type"
    message: str
    index: int | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "layer": self.layer,
            "message": self.message,
            "index": self.index,
        }


class ValidationError(ValueError):
    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        summary = "; ".join(
            f"{v.code.value}[{v.layer}]" + (f"@{v.index}" if v.index is not None else "")
            for v in violations
        )
        super().__init__(f"{len(violations)} buffer violation(s): {summary}")

    def to_dicts(self) -> list[dict]:
        return [v.to_dict() for v in self.violations]


def _as_bytes(buf: object) -> bytes | None:
    if buf is None:
        return None
    if isinstance(buf, (bytes, bytearray, memoryview)):
        return bytes(buf)
    if isinstance(buf, pa.Buffer):
        return buf.to_pybytes()
    raise TypeError(f"cannot inspect buffer object of type {type(buf).__name__}")


def _check_validity(
    raw: bytes | None, length: int, logical_offset: int, claimed_null_count: int | None
) -> list[Violation]:
    out: list[Violation] = []
    covered = logical_offset + length
    if raw is None:
        if claimed_null_count not in (None, 0):
            out.append(
                Violation(
                    ViolationCode.NULL_COUNT_MISMATCH,
                    "validity",
                    f"validity buffer absent but claimed null_count={claimed_null_count}",
                )
            )
        return out
    need = validity_byte_size(covered)
    if len(raw) < need:
        out.append(
            Violation(
                ViolationCode.BUFFER_TOO_SHORT,
                "validity",
                f"validity buffer {len(raw)} bytes < required {need} "
                f"(offset={logical_offset}, length={length})",
            )
        )
        return out
    # Padding of the producer array: tail bits beyond `covered` and any extra
    # bytes beyond the required region must be zero.
    padding_bits = count_padding_errors(raw, covered)
    if padding_bits:
        out.append(
            Violation(
                ViolationCode.INVALID_PADDING,
                "validity",
                f"non-zero padding bits {padding_bits} after {covered} elements",
            )
        )
    if len(raw) > need and any(raw[need:]):
        out.append(
            Violation(
                ViolationCode.INVALID_PADDING,
                "validity",
                f"{len(raw) - need} trailing validity byte(s) contain non-zero data",
            )
        )
    if claimed_null_count is not None:
        present = count_set_bits(raw, length, logical_offset)
        actual_nulls = length - present
        if actual_nulls != claimed_null_count:
            out.append(
                Violation(
                    ViolationCode.NULL_COUNT_MISMATCH,
                    "validity",
                    f"bitmap gives {actual_nulls} null(s) but claimed {claimed_null_count}",
                )
            )
    return out


def _check_offsets(raw: bytes | None, length: int, logical_offset: int) -> tuple[list[Violation], int]:
    """Return violations and the final offset (0 when undeterminable)."""
    out: list[Violation] = []
    count = logical_offset + length + 1
    need = count * OFFSET_WIDTH
    if raw is None:
        out.append(
            Violation(ViolationCode.MISSING_BUFFER, "offsets", "offset buffer is required for utf8")
        )
        return out, 0
    if len(raw) < need:
        out.append(
            Violation(
                ViolationCode.BUFFER_TOO_SHORT,
                "offsets",
                f"offset buffer {len(raw)} bytes < required {need} for {count} int32 values",
            )
        )
        return out, 0
    offsets = np.frombuffer(raw[:need], dtype=np.int32)
    if logical_offset == 0 and offsets[0] != 0:
        out.append(
            Violation(
                ViolationCode.INVALID_FIRST_OFFSET,
                "offsets",
                f"first offset must be 0 for an array start, got {int(offsets[0])}",
                0,
            )
        )
    for i in range(1, count):
        if offsets[i] < offsets[i - 1]:
            out.append(
                Violation(
                    ViolationCode.DECREASING_OFFSET,
                    "offsets",
                    f"offset decreased at slot {i}: {int(offsets[i])} < {int(offsets[i - 1])}",
                    i,
                )
            )
    final = int(offsets[-1])
    return out, final


def _check_string_data(
    raw: bytes | None,
    validity: bytes | None,
    offsets_raw: bytes | None,
    length: int,
    logical_offset: int,
    *,
    check_utf8: bool,
) -> list[Violation]:
    out: list[Violation] = []
    if offsets_raw is None:
        return out  # already reported as MISSING_BUFFER
    count = logical_offset + length + 1
    need = count * OFFSET_WIDTH
    if len(offsets_raw) < need:
        return out  # length violation already reported
    offsets = np.frombuffer(offsets_raw[:need], dtype=np.int32)
    final = int(offsets[-1])
    data_len = len(raw) if raw is not None else 0
    if raw is None and final > 0:
        out.append(
            Violation(
                ViolationCode.MISSING_BUFFER,
                "data",
                f"data buffer absent but offsets span {final} byte(s)",
            )
        )
        return out
    if final > data_len:
        out.append(
            Violation(
                ViolationCode.OFFSET_OUT_OF_BOUNDS,
                "data",
                f"final offset {final} exceeds data buffer size {data_len}",
                count - 1,
            )
        )
    # Per-slot bounds checks (catch any span overflow even when final happens
    # to be in range, and negative offsets other than the structural scan).
    for i in range(count):
        if int(offsets[i]) < 0:
            out.append(
                Violation(
                    ViolationCode.OFFSET_OUT_OF_BOUNDS,
                    "offsets",
                    f"negative offset {int(offsets[i])} at slot {i}",
                    i,
                )
            )
            break
        if int(offsets[i]) > data_len:
            out.append(
                Violation(
                    ViolationCode.OFFSET_OUT_OF_BOUNDS,
                    "data",
                    f"offset {int(offsets[i])} at slot {i} exceeds data buffer size {data_len}",
                    i,
                )
            )
    if not check_utf8 or raw is None:
        return out
    for i in range(logical_offset, logical_offset + length):
        if validity is not None and not (validity[i >> 3] >> (i & 7)) & 1:
            continue  # NULL slots own no bytes and need no decoding
        start, end = int(offsets[i]), int(offsets[i + 1])
        if start > end or end > data_len:
            continue  # structural violations already reported
        span = raw[start:end]
        try:
            span.decode("utf-8")
        except UnicodeDecodeError:
            out.append(
                Violation(
                    ViolationCode.UTF8_INVALID,
                    "data",
                    f"invalid utf-8 in value slot {i} at data bytes [{start},{end})",
                    i,
                )
            )
    return out


def validate_buffers(
    pa_type: pa.DataType,
    length: int,
    buffers: list | tuple,
    *,
    logical_offset: int = 0,
    claimed_null_count: int | None = None,
    check_utf8: bool = True,
) -> list[Violation]:
    """Inspect raw Arrow buffers independently of the kernel.

    ``buffers`` follows Arrow order: [validity, data] for primitives and
    [validity, offsets, data] for utf8 strings; entries may be bytes-like or
    None (None validity means "all present").
    """
    violations: list[Violation] = []
    if not isinstance(length, int) or length < 0:
        return [
            Violation(ViolationCode.INVALID_LENGTH, "type", f"length must be >= 0, got {length!r}")
        ]
    if logical_offset < 0:
        return [
            Violation(
                ViolationCode.INVALID_LENGTH,
                "type",
                f"logical_offset must be >= 0, got {logical_offset!r}",
            )
        ]
    if not is_supported(pa_type):
        return [
            Violation(
                ViolationCode.TYPE_UNSUPPORTED,
                "type",
                f"type {pa_type!s} is not supported (primitives and utf8 only)",
            )
        ]

    expected = 3 if pa_type == STRING_TYPE else 2
    if len(buffers) != expected:
        violations.append(
            Violation(
                ViolationCode.BUFFER_LAYOUT,
                "type",
                f"{pa_type!s} expects {expected} buffers [validity, "
                f"{'offsets, data' if expected == 3 else 'data'}], got {len(buffers)}",
            )
        )
        return violations

    raw = [_as_bytes(b) for b in buffers]
    violations += _check_validity(raw[0], length, logical_offset, claimed_null_count)

    if pa_type == STRING_TYPE:
        offset_violations, _ = _check_offsets(raw[1], length, logical_offset)
        violations += offset_violations
        violations += _check_string_data(
            raw[2], raw[0], raw[1], length, logical_offset, check_utf8=check_utf8
        )
    else:
        spec = primitive_spec(pa_type)
        data = raw[1]
        need = (logical_offset + length) * spec.byte_width
        if data is None:
            violations.append(
                Violation(ViolationCode.MISSING_BUFFER, "data", "data buffer is required")
            )
        elif len(data) < need:
            violations.append(
                Violation(
                    ViolationCode.BUFFER_TOO_SHORT,
                    "data",
                    f"data buffer {len(data)} bytes < required {need} "
                    f"({logical_offset + length} x {spec.byte_width})",
                )
            )
    return violations


def ensure_valid(
    pa_type: pa.DataType,
    length: int,
    buffers: list | tuple,
    *,
    logical_offset: int = 0,
    claimed_null_count: int | None = None,
    check_utf8: bool = True,
) -> None:
    violations = validate_buffers(
        pa_type,
        length,
        buffers,
        logical_offset=logical_offset,
        claimed_null_count=claimed_null_count,
        check_utf8=check_utf8,
    )
    if violations:
        raise ValidationError(violations)
