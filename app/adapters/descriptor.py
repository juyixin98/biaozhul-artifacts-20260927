"""JSON descriptor adapter.

External clients POST buffer contents as standard base64 (with optional
padding) inside a JSON descriptor. Decoding failures become a categorised
``malformed_payload`` rather than a raw exception.
"""
from __future__ import annotations

import base64
import binascii

from app.core.layout import RawColumnBuffers
from app.core import types as tt
from app.errors import ErrorCategory, LayoutError


def _b64_decode(field_name: str, value: str | None, *, allow_none: bool = False) -> bytes | None:
    if value is None:
        if allow_none:
            return None
        raise LayoutError(ErrorCategory.MISSING_BUFFER, f"field {field_name!r} is required")
    if not isinstance(value, str):
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                          f"field {field_name!r} must be a base64 string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LayoutError(
            ErrorCategory.MALFORMED_PAYLOAD,
            f"field {field_name!r} is not valid base64: {exc}",
        ) from exc


def descriptor_to_raw(payload: dict) -> RawColumnBuffers:
    required = ("type", "length", "data")
    for key in required:
        if key not in payload:
            raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                              f"descriptor missing required field {key!r}")
    type_name = payload["type"]
    if not isinstance(type_name, str):
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD, "'type' must be a string")
    type_name = tt.canonical(type_name)
    length = payload["length"]
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                          "'length' must be a non-negative integer",
                          detail={"received": repr(length)})
    null_count = payload.get("null_count")
    if null_count is not None and (not isinstance(null_count, int) or isinstance(null_count, bool)
                                   or null_count < 0 or null_count > length):
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                          "'null_count' must be an int in [0, length] or null",
                          detail={"null_count": null_count, "length": length})

    data = _b64_decode("data", payload["data"])
    validity = _b64_decode("validity", payload.get("validity"), allow_none=True)
    offsets = _b64_decode("offsets", payload.get("offsets"), allow_none=True)
    return RawColumnBuffers(
        type_name=type_name,
        length=length,
        validity=validity,
        offsets=offsets,
        data=data,
        null_count=null_count,
        logical_offset=0,
    )
