"""Raw column buffer description crossing the trust boundary.

An external participant (or a test fixture) hands in *raw, untrusted* bytes for
the three Arrow array buffers: validity bitmap (optional), int32 offsets (for
utf8 strings), and the data/values buffer. Nothing about these bytes is trusted
until :mod:`app.validation.checks` has run.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RawColumnBuffers:
    type_name: str
    length: int
    # Raw, little-endian buffer contents; validity may be None (all valid).
    validity: bytes | None
    offsets: bytes | None  # only present for utf8
    data: bytes
    # Claimed null count; None means "not stated" and validation must compute it.
    null_count: int | None = None
    # Logical offset inside the buffers; imported buffers normally start at 0,
    # the field exists so validation of sliced re-imports is expressible.
    logical_offset: int = 0
