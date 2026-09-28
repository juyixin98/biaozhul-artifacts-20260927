"""Strict bytes ↔ text boundary validation.

UTF-8 is decoded strictly: overlong forms, bad continuations, truncated
sequences and encodings of surrogate code points are all rejected.  This
module also answers "is offset *k* a lead-byte boundary?" — the property the
edit API needs to forbid edits landing inside a multi-byte sequence.

Input may arrive two ways:

* raw octets (``application/octet-stream``) — decoded here, so a malformed
  body produces :class:`InvalidUtf8` with byte offsets;
* JSON strings — already decoded by the JSON parser, but may still contain
  lone surrogates if the parser accepted ``\\ud800`` escapes; those are
  rejected with :class:`UnpairedSurrogate`.
"""

from __future__ import annotations

from .errors import InvalidUtf8, UnpairedSurrogate

#: First (lead) byte → expected total sequence length. 0xC0/0xC1 are overlong.
_UTF8_LEAD_LENGTH: dict[int, int] = {
    **{b: 1 for b in range(0x00, 0x80)},
    **{b: 2 for b in range(0xC2, 0xE0)},
    **{b: 3 for b in range(0xE0, 0xF0)},
    **{b: 4 for b in range(0xF0, 0xF5)},
}


def decode_utf8(data: bytes) -> str:
    """Strictly decode ``data``; raise :class:`InvalidUtf8` on any violation."""
    try:
        # errors="strict" rejects invalid starts, bad continuations,
        # truncation, overlong encodings and surrogate codepoints (ED A0–BF).
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # Keep the offending span small: the raw hex below is bounded, so a
        # huge truncated upload cannot flood diagnostics.
        span = data[exc.start:exc.end]
        raise InvalidUtf8(
            reason=exc.reason,
            start=exc.start,
            end=exc.end,
            raw_hex=span[:16].hex(),
        ) from None


def ensure_scalar_value(text: str) -> None:
    """Reject strings containing lone surrogates (possible via JSON escapes)."""
    for i, ch in enumerate(text):
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise UnpairedSurrogate(cp, i)


def encode_utf8(text: str) -> bytes:
    """Strictly encode; lone surrogates raise :class:`UnpairedSurrogate`."""
    try:
        return text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        # exc.start points at the offending surrogate codepoint index.
        cp = ord(text[exc.start])
        raise UnpairedSurrogate(cp, exc.start) from None


def is_lead_boundary(data: bytes, offset: int) -> bool:
    """True iff ``data[offset]`` begins a UTF-8 sequence (or offset == len)."""
    if offset < 0 or offset > len(data):
        return False
    if offset == len(data):
        return True
    return data[offset] in _UTF8_LEAD_LENGTH


def codepoint_byte_starts(data: bytes) -> list[int]:
    """Byte offsets of every UTF-8 lead byte, plus the final length.

    Encoded separately (no index object required) and used by tests as an
    independent oracle for byte boundaries.
    """
    starts: list[int] = []
    i = 0
    n = len(data)
    while i < n:
        starts.append(i)
        lead = data[i]
        length = _UTF8_LEAD_LENGTH.get(lead)
        if length is None:  # continuation byte at a lead position: malformed
            raise InvalidUtf8("invalid continuation byte", i, i + 1,
                              data[i:i + 1].hex())
        i += length
        if i > n:
            raise InvalidUtf8("unexpected end of data", starts[-1], n,
                              data[starts[-1]:][:16].hex())
    starts.append(n)
    return starts
