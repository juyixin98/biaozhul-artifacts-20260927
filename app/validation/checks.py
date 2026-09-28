"""Structural checks over raw, untrusted Arrow column buffers.

The three Arrow buffer families are checked **separately and independently**
(as required by the review):

* validity  - bitmap length vs logical span, trailing padding bits, null count
* offsets   - int32 offset table length, zero start, monotonicity, data range
* data      - values-buffer length and alignment vs element width/span

Every check returns a :class:`CheckResult` carrying concrete evidence (sizes,
indices, values), and the aggregated :class:`ValidationReport` records the
explicit failure category. Unknown exceptions are caught and recorded as
``internal_error`` checks, never collapsed into "ok".

The code is deliberately written in pure Python with ``struct`` — it does not
call PyArrow for any judgement, so it can serve as an independent reference.
"""
from __future__ import annotations

import struct
import traceback
from dataclasses import dataclass, field

from app.core import bitmath
from app.core.layout import RawColumnBuffers
from app.core import types as tt
from app.errors import ErrorCategory


@dataclass
class CheckResult:
    group: str  # "schema" | "validity" | "offsets" | "data" | "semantic"
    name: str
    ok: bool
    category: str | None = None
    message: str = ""
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "check": self.name,
            "ok": self.ok,
            "category": self.category,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class ValidationReport:
    type_name: str
    length: int
    logical_offset: int
    checks: list[CheckResult] = field(default_factory=list)
    computed_null_count: int | None = None
    values: list[object] | None = None  # independent semantic decode when ok

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def failure_categories(self) -> list[str]:
        return [c.category for c in self.failures if c.category is not None]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "type": self.type_name,
            "length": self.length,
            "logical_offset": self.logical_offset,
            "computed_null_count": self.computed_null_count,
            "failure_categories": self.failure_categories,
            "checks": [c.to_dict() for c in self.checks],
            "values_preview": None if self.values is None else self.values[:8],
        }


_FIXED_FORMATS = {
    "int8": ("<b", 1), "uint8": (None, 1),
    "int16": ("<h", 2), "uint16": ("<H", 2),
    "int32": ("<i", 4), "uint32": ("<I", 4),
    "int64": ("<q", 8), "uint64": ("<Q", 8),
    "float": ("<f", 4), "double": ("<d", 8),
}


def validate(raw: RawColumnBuffers) -> ValidationReport:
    """Run every applicable check against ``raw`` and return the full report."""
    report = ValidationReport(
        type_name=raw.type_name, length=raw.length, logical_offset=raw.logical_offset
    )

    def run(group: str, name: str, fn) -> None:
        try:
            result = fn()
            if result is None:
                result = CheckResult(group, name, True, message="passed")
            report.checks.append(result)
        except _AbortCheckSuite:
            raise
        except Exception as exc:  # never hide an unexpected failure as success
            report.checks.append(
                CheckResult(
                    group, name, False, ErrorCategory.INTERNAL_ERROR.value,
                    f"unexpected error: {type(exc).__name__}: {exc}",
                    evidence={"traceback": traceback.format_exc(limit=3)},
                )
            )

    try:
        run("schema", "type_supported", lambda: _check_type(raw))
        type_ok = tt.is_supported(raw.type_name)
        run("schema", "logical_window", lambda: _check_window(raw))
        window_ok = 0 <= raw.logical_offset and raw.length >= 0

        if type_ok and window_ok:
            run("validity", "validity_buffer_present_and_sized", lambda: _check_validity_size(raw, report))
            run("validity", "validity_trailing_bits_zero", lambda: _check_validity_trailing(raw, report))
            run("validity", "null_count_matches_bitmap", lambda: _check_null_count(raw, report))

            if tt.is_string(raw.type_name):
                run("offsets", "offsets_buffer_present", lambda: _check_offsets_present(raw))
                run("offsets", "offsets_buffer_alignment", lambda: _check_offsets_alignment(raw))
                run("offsets", "offsets_length_covers_elements", lambda: _check_offsets_size(raw))
                decoded = _safe_decode_offsets(raw)
                run("offsets", "first_offset_is_zero", lambda: _check_first_offset_zero(raw, decoded))
                run("offsets", "offsets_non_negative", lambda: _check_offsets_nonnegative(raw, decoded))
                run("offsets", "offsets_monotonic", lambda: _check_offsets_monotonic(raw, decoded))
                run("offsets", "final_offset_within_data", lambda: _check_final_offset(raw, decoded))
                run("data", "data_buffer_present", lambda: _check_data_present(raw))
                run("semantic", "all_non_null_strings_decode_as_utf8",
                    lambda: _check_semantic_strings(raw, report, decoded))
            else:
                run("data", "data_buffer_present", lambda: _check_data_present(raw))
                run("data", "data_buffer_alignment", lambda: _check_data_alignment_fixed(raw))
                run("data", "data_buffer_covers_elements", lambda: _check_data_size_fixed(raw))
                run("semantic", "all_fixed_width_values_decode",
                    lambda: _check_semantic_fixed(raw, report))
    except _AbortCheckSuite:
        pass

    return report


class _AbortCheckSuite(Exception):
    pass


# --------------------------------------------------------------------- schema
def _check_type(raw: RawColumnBuffers) -> CheckResult:
    if not tt.is_supported(raw.type_name):
        return CheckResult(
            "schema", "type_supported", False, ErrorCategory.UNSUPPORTED_TYPE.value,
            f"type {raw.type_name!r} not supported",
            evidence={"supported": sorted(tt.SUPPORTED_TYPES)},
        )
    return CheckResult("schema", "type_supported", True,
                       message=f"supported type {raw.type_name}", evidence={"type": raw.type_name})


def _check_window(raw: RawColumnBuffers) -> CheckResult:
    if raw.length < 0 or raw.logical_offset < 0:
        return CheckResult(
            "schema", "logical_window", False, ErrorCategory.MALFORMED_PAYLOAD.value,
            "length and logical_offset must be non-negative",
            evidence={"length": raw.length, "logical_offset": raw.logical_offset},
        )
    if raw.logical_offset != 0:
        # Import path only accepts offset-0 buffers; slicing is a kernel op.
        return CheckResult(
            "schema", "logical_window", False, ErrorCategory.MALFORMED_PAYLOAD.value,
            "raw import requires logical_offset=0; use the slice kernel for offset views",
            evidence={"logical_offset": raw.logical_offset},
        )
    return CheckResult("schema", "logical_window", True,
                       message=f"window offset=0 length={raw.length}")


# ----------------------------------------------------------------- validity
def _required_bitmap_bytes(raw: RawColumnBuffers) -> int:
    return bitmath.ceil_div8(raw.logical_offset + raw.length)


def _check_validity_size(raw: RawColumnBuffers, report: ValidationReport) -> CheckResult:
    need = _required_bitmap_bytes(raw)
    ev = {"required_bytes_for_logical_span": need, "received_bytes": None if raw.validity is None else len(raw.validity)}
    if raw.length == 0:
        if raw.validity in (None, b""):
            return CheckResult("validity", "validity_buffer_present_and_sized", True,
                               message="empty array: bitmap absent/empty as required", evidence=ev)
        return CheckResult("validity", "validity_buffer_present_and_sized", False,
                           ErrorCategory.VALIDITY_TOO_SHORT.value,
                           "zero-length array must not carry a validity bitmap", evidence=ev)
    if raw.validity is None:
        # No bitmap is legal: means all valid.
        return CheckResult("validity", "validity_buffer_present_and_sized", True,
                           message="validity bitmap omitted -> all slots valid", evidence=ev)
    if len(raw.validity) < need:
        return CheckResult("validity", "validity_buffer_present_and_sized", False,
                           ErrorCategory.VALIDITY_TOO_SHORT.value,
                           f"validity buffer {len(raw.validity)} bytes cannot cover "
                           f"{raw.length} elements needing {need} bytes", evidence=ev)
    return CheckResult("validity", "validity_buffer_present_and_sized", True,
                       message=f"{len(raw.validity)} bitmap bytes cover {raw.length} elements", evidence=ev)


def _check_validity_trailing(raw: RawColumnBuffers, report: ValidationReport) -> CheckResult:
    if raw.validity is None or raw.length == 0:
        return CheckResult("validity", "validity_trailing_bits_zero", True,
                           message="no bitmap: nothing to check")
    span = raw.logical_offset + raw.length
    if not bitmath.trailing_padding_bits_are_zero(raw.validity, span):
        last = raw.validity[(span - 1) >> 3]
        return CheckResult("validity", "validity_trailing_bits_zero", False,
                           ErrorCategory.VALIDITY_TRAILING_BITS_SET.value,
                           f"padding bits past logical span {span} are set in last bitmap byte {last:#04x}",
                           evidence={"last_byte": last, "logical_span": span})
    return CheckResult("validity", "validity_trailing_bits_zero", True,
                       message="trailing padding bits are zero",
                       evidence={"logical_span": span})


def _check_null_count(raw: RawColumnBuffers, report: ValidationReport) -> CheckResult | None:
    if raw.validity is None:
        computed = 0
    elif raw.length == 0:
        computed = 0
    else:
        computed = raw.length - bitmath.count_set_bits(raw.validity, raw.logical_offset, raw.length)
    report.computed_null_count = computed
    if raw.null_count is not None and raw.null_count != computed:
        return CheckResult("validity", "null_count_matches_bitmap", False,
                           ErrorCategory.NULL_COUNT_MISMATCH.value,
                           f"claimed null_count={raw.null_count} but bitmap encodes {computed} NULL slots",
                           evidence={"claimed": raw.null_count, "computed": computed,
                                     "length": raw.length})
    return CheckResult("validity", "null_count_matches_bitmap", True,
                       message=f"null_count={computed}" + (
                           "" if raw.null_count is None else f" matches claim {raw.null_count}"),
                       evidence={"computed": computed, "claimed": raw.null_count})


# ------------------------------------------------------------------ offsets
def _safe_decode_offsets(raw: RawColumnBuffers) -> list[int] | None:
    if raw.offsets is None:
        return None
    n = len(raw.offsets) // 4
    return list(struct.unpack(f"<{n}i", raw.offsets[: n * 4]))


def _check_offsets_present(raw: RawColumnBuffers) -> CheckResult:
    if raw.offsets is None:
        return CheckResult("offsets", "offsets_buffer_present", False,
                           ErrorCategory.MISSING_BUFFER.value,
                           "utf8 column requires an int32 offsets buffer")
    return CheckResult("offsets", "offsets_buffer_present", True,
                       message=f"offsets buffer present ({len(raw.offsets)} bytes)")


def _check_offsets_alignment(raw: RawColumnBuffers) -> CheckResult:
    if raw.offsets is None:
        return CheckResult("offsets", "offsets_buffer_alignment", True, message="no offsets buffer")
    rem = len(raw.offsets) % 4
    if rem:
        return CheckResult("offsets", "offsets_buffer_alignment", False,
                           ErrorCategory.BUFFER_NOT_ALIGNED.value,
                           f"offsets buffer length {len(raw.offsets)} is not a multiple of int32 (4 bytes)",
                           evidence={"remainder": rem, "size": len(raw.offsets)})
    return CheckResult("offsets", "offsets_buffer_alignment", True, message="4-byte aligned")


def _check_offsets_size(raw: RawColumnBuffers) -> CheckResult:
    need = (raw.length + 1) * 4
    have = 0 if raw.offsets is None else len(raw.offsets)
    if have < need:
        return CheckResult("offsets", "offsets_length_covers_elements", False,
                           ErrorCategory.OFFSETS_TOO_SHORT.value,
                           f"{raw.length} elements need {need} offset bytes ({raw.length + 1} int32s); got {have}",
                           evidence={"required_bytes": need, "received_bytes": have,
                                     "elements": raw.length})
    trailing = have - need
    return CheckResult("offsets", "offsets_length_covers_elements", True,
                       message=f"{have} bytes provide {raw.length + 1} offsets"
                               + (f" ({trailing} harmless trailing bytes)" if trailing else ""),
                       evidence={"required_bytes": need, "received_bytes": have,
                                 "trailing_bytes": trailing})


def _check_first_offset_zero(raw: RawColumnBuffers, offsets: list[int] | None) -> CheckResult:
    if not offsets:
        return CheckResult("offsets", "first_offset_is_zero", True,
                           message="empty array: single zero offset")
    if offsets[0] != 0:
        return CheckResult("offsets", "first_offset_is_zero", False,
                           ErrorCategory.OFFSET_NOT_ZERO.value,
                           f"first offset must be 0, got {offsets[0]}",
                           evidence={"first_offset": offsets[0]})
    return CheckResult("offsets", "first_offset_is_zero", True, message="offsets[0]=0")


def _check_offsets_nonnegative(raw: RawColumnBuffers, offsets: list[int] | None) -> CheckResult:
    if not offsets:
        return CheckResult("offsets", "offsets_non_negative", True, message="no offsets")
    table = offsets[: raw.length + 1]
    negatives = [(i, v) for i, v in enumerate(table) if v < 0]
    if negatives:
        first = negatives[0]
        return CheckResult("offsets", "offsets_non_negative", False,
                           ErrorCategory.OFFSET_OUT_OF_RANGE.value,
                           f"offset[{first[0]}]={first[1]} is negative",
                           evidence={"index": first[0], "value": first[1],
                                     "all_negative_indices": [i for i, _ in negatives][:8]})
    return CheckResult("offsets", "offsets_non_negative", True, message="all offsets >= 0")


def _check_offsets_monotonic(raw: RawColumnBuffers, offsets: list[int] | None) -> CheckResult:
    if not offsets:
        return CheckResult("offsets", "offsets_monotonic", True, message="no offsets")
    # Only the (length + 1) offsets that describe elements are normative;
    # any extra aligned trailing bytes are not part of the offset table.
    table = offsets[: raw.length + 1]
    for i in range(1, len(table)):
        if table[i] < table[i - 1]:
            return CheckResult("offsets", "offsets_monotonic", False,
                               ErrorCategory.OFFSETS_NOT_MONOTONIC.value,
                               f"offset[{i}]={table[i]} < offset[{i - 1}]={table[i - 1]} "
                               "(decreasing offsets)",
                               evidence={"index": i, "previous": table[i - 1], "value": table[i],
                                         "offsets_head": table[: min(len(table), 9)]})
    return CheckResult("offsets", "offsets_monotonic", True,
                       message="offsets are non-decreasing",
                       evidence={"last_offset": table[-1]})


def _check_final_offset(raw: RawColumnBuffers, offsets: list[int] | None) -> CheckResult:
    if not offsets:
        if len(raw.data) != 0:
            return CheckResult("offsets", "final_offset_within_data", False,
                               ErrorCategory.DATA_TOO_SHORT.value,
                               "empty offsets but data buffer non-empty",
                               evidence={"data_size": len(raw.data)})
        return CheckResult("offsets", "final_offset_within_data", True,
                           message="empty column, empty data")
    final = offsets[raw.length]
    if final > len(raw.data):
        return CheckResult("offsets", "final_offset_within_data", False,
                           ErrorCategory.OFFSET_OUT_OF_RANGE.value,
                           f"final offset {final} exceeds data buffer size {len(raw.data)}",
                           evidence={"final_offset": final, "data_size": len(raw.data)})
    return CheckResult("offsets", "final_offset_within_data", True,
                       message=f"final offset {final} <= data size {len(raw.data)}",
                       evidence={"final_offset": final, "data_size": len(raw.data),
                                 "data_bytes_used": final,
                                 "data_bytes_unused": len(raw.data) - final})


# -------------------------------------------------------------------- data
def _check_data_present(raw: RawColumnBuffers) -> CheckResult:
    # b"" is a legitimate data buffer (all-empty strings / zero elements).
    return CheckResult("data", "data_buffer_present", True,
                       message=f"data buffer present ({len(raw.data)} bytes)")


def _check_data_alignment_fixed(raw: RawColumnBuffers) -> CheckResult:
    width = tt.byte_width(raw.type_name)
    rem = len(raw.data) % width
    if rem:
        return CheckResult("data", "data_buffer_alignment", False,
                           ErrorCategory.BUFFER_NOT_ALIGNED.value,
                           f"data buffer {len(raw.data)} bytes is not a multiple of {raw.type_name} width {width}",
                           evidence={"remainder": rem, "width": width, "size": len(raw.data)})
    return CheckResult("data", "data_buffer_alignment", True,
                       message=f"aligned to {width} bytes")


def _check_data_size_fixed(raw: RawColumnBuffers) -> CheckResult:
    width = tt.byte_width(raw.type_name)
    need = raw.length * width
    if len(raw.data) < need:
        return CheckResult("data", "data_buffer_covers_elements", False,
                           ErrorCategory.DATA_TOO_SHORT.value,
                           f"{raw.length} x {raw.type_name} need {need} data bytes; got {len(raw.data)}",
                           evidence={"required_bytes": need, "received_bytes": len(raw.data),
                                     "missing_bytes": need - len(raw.data)})
    return CheckResult("data", "data_buffer_covers_elements", True,
                       message=f"{len(raw.data)} data bytes cover {raw.length} elements "
                               f"({len(raw.data) - need} trailing)",
                       evidence={"required_bytes": need, "received_bytes": len(raw.data),
                                 "trailing_bytes": len(raw.data) - need})


# ---------------------------------------------------------------- semantic
def _is_valid(raw: RawColumnBuffers, i: int) -> bool:
    if raw.validity is None:
        return True
    return bitmath.bit_get(raw.validity, raw.logical_offset + i) == 1


def _check_semantic_strings(raw: RawColumnBuffers, report: ValidationReport,
                            offsets: list[int] | None) -> CheckResult:
    if offsets is None:
        return CheckResult("semantic", "all_non_null_strings_decode_as_utf8", True,
                           message="skipped: no offsets")
    values: list[object] = []
    decoded_ok = 0
    empty_strings = 0
    nulls = 0
    for i in range(raw.length):
        if not _is_valid(raw, i):
            values.append(None)
            nulls += 1
            continue
        start, end = offsets[i], offsets[i + 1]
        chunk = raw.data[start:end]
        try:
            value = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            return CheckResult("semantic", "all_non_null_strings_decode_as_utf8", False,
                               ErrorCategory.SEMANTIC_SCAN_FAILED.value,
                               f"element {i} bytes [{start},{end}) are not valid UTF-8: {exc}",
                               evidence={"index": i, "start": start, "end": end,
                                         "byte_preview_hex": chunk[:16].hex()})
        values.append(value)
        decoded_ok += 1
        if value == "":
            empty_strings += 1
    report.values = values
    return CheckResult("semantic", "all_non_null_strings_decode_as_utf8", True,
                       message=f"decoded {decoded_ok} strings ({empty_strings} empty), {nulls} NULL",
                       evidence={"decoded": decoded_ok, "empty_strings": empty_strings,
                                 "nulls": nulls})


def _check_semantic_fixed(raw: RawColumnBuffers, report: ValidationReport) -> CheckResult:
    fmt, width = _FIXED_FORMATS[raw.type_name]
    values: list[object] = []
    nulls = 0
    for i in range(raw.length):
        if not _is_valid(raw, i):
            values.append(None)
            nulls += 1
            continue
        chunk = raw.data[i * width:(i + 1) * width]
        if fmt is None:  # uint8
            values.append(chunk[0])
        else:
            values.append(struct.unpack(fmt, chunk)[0])
    report.values = values
    return CheckResult("semantic", "all_fixed_width_values_decode", True,
                       message=f"decoded {raw.length - nulls} values, {nulls} NULL",
                       evidence={"elements": raw.length, "nulls": nulls})
