"""Text specification: encoding and normalization of payloads and patterns.

Design decision — matching operates on **bytes**, not Unicode code points:

* All reported positions are raw byte offsets into the concatenated payload.
* Patterns are transported as base64 so arbitrary binary patterns are first
  class citizens (``\\x00`` included).
* For ``binary`` mode the pattern/payload bytes are matched verbatim.
* For text modes (``utf-8`` / ``ascii``) the bytes must decode under the
  declared encoding — a mismatch is rejected with ``invalid_encoding`` rather
  than silently matched as raw bytes.
* ``casefold`` is supported for ASCII only. ASCII case folding is a pure
  byte-wise transform (``a-z`` <-> ``A-Z`` differ by exactly one bit in every
  ASCII-compatible encoding), so byte offsets remain exact. Full Unicode
  case folding would change byte lengths and cannot preserve raw offsets, so
  it is intentionally refused rather than approximated.
"""
from __future__ import annotations

import base64
import binascii
import codecs
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from .errors import EncodingError, InvalidBase64Error, UnsupportedEncodingError

SUPPORTED_ENCODINGS = ("utf-8", "ascii", "latin-1", "binary")
# "binary" means: no decoding contract at all; match raw bytes.
_TEXT_ENCODINGS = {"utf-8", "ascii", "latin-1"}


class CaseMode(str, Enum):
    SENSITIVE = "sensitive"
    ASCII_CASEFOLD = "ascii_casefold"


def decode_base64(data: str, *, what: str = "payload") -> bytes:
    """Decode canonical base64, rejecting slop that lenient decoders accept.

    Rejected: missing padding, embedded whitespace, non-alphabet characters.
    """
    if not isinstance(data, str):
        raise InvalidBase64Error(f"{what} must be a base64 string")
    # validate=False lets us raise our own typed error; add_data would also
    # accept truncated streams, so require exact canonical length first.
    if len(data) % 4 != 0:
        raise InvalidBase64Error(
            f"{what} is not valid base64: length must be a multiple of 4",
            details={"length": len(data)},
        )
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    )
    bad = [c for c in data if c not in allowed]
    if bad:
        raise InvalidBase64Error(
            f"{what} is not valid base64: {len(bad)} non-alphabet character(s)",
            details={"examples": sorted(set(bad))[:5]},
        )
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidBase64Error(f"{what} is not valid base64: {exc}") from exc


def encode_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def ascii_casefold(buf: bytes) -> bytes:
    """Lowercase ASCII letters only. All other bytes pass through untouched."""
    return bytes(b + 0x20 if 0x41 <= b <= 0x5A else b for b in buf)


@dataclass(frozen=True)
class TextSpec:
    """A concrete text specification shared by a pattern version and its scans."""

    encoding: str = "utf-8"
    case_mode: CaseMode = CaseMode.SENSITIVE

    def __post_init__(self) -> None:
        if self.encoding not in SUPPORTED_ENCODINGS:
            raise UnsupportedEncodingError(
                f"encoding {self.encoding!r} is not supported",
                details={"supported": list(SUPPORTED_ENCODINGS)},
            )
        if not isinstance(self.case_mode, CaseMode):
            # Allow plain strings coming from Pydantic validators.
            try:
                object.__setattr__(self, "case_mode", CaseMode(self.case_mode))
            except ValueError as exc:
                raise UnsupportedEncodingError(
                    f"unknown case mode {self.case_mode!r}"
                ) from exc

    @property
    def is_binary(self) -> bool:
        return self.encoding == "binary"

    def to_dict(self) -> Dict[str, str]:
        return {"encoding": self.encoding, "case_mode": self.case_mode.value}

    def normalize_pattern(self, raw: bytes) -> bytes:
        """Validate + transform one pattern's raw bytes."""
        if not raw:
            # Caller (version service) turns this into EmptyPatternError with
            # the pattern index; the spec layer only says "zero bytes".
            raise ValueError("empty pattern")
        if not self.is_binary:
            try:
                raw.decode(self.encoding)
            except UnicodeDecodeError as exc:
                raise EncodingError(
                    f"pattern bytes are not valid {self.encoding}: "
                    f"invalid byte at offset {exc.start}",
                    details={"bad_offset": exc.start, "encoding": self.encoding},
                ) from exc
        if self.case_mode is CaseMode.ASCII_CASEFOLD:
            return ascii_casefold(raw)
        return raw

    def normalize_chunk(self, raw: bytes) -> bytes:
        """Validate + transform one streamed chunk's raw bytes.

        Empty chunks are legal for streaming (they simply emit nothing); the
        empty-*pattern* rule lives in the version service.

        Caveat: this one-shot validation is correct for ``binary`` and
        single-byte text encodings (``ascii``/``latin-1``). Multi-byte UTF-8
        sequences may straddle a chunk boundary, so streaming scans must use
        :class:`StreamingTextDecoder` instead (see matcher).
        """
        if not self.is_binary:
            try:
                raw.decode(self.encoding)
            except UnicodeDecodeError as exc:
                raise EncodingError(
                    f"chunk bytes are not valid {self.encoding}: "
                    f"invalid byte at byte {exc.start} of this chunk",
                    details={"bad_offset_in_chunk": exc.start,
                             "encoding": self.encoding},
                ) from exc
        if self.case_mode is CaseMode.ASCII_CASEFOLD:
            return ascii_casefold(raw)
        return raw


class StreamingTextDecoder:
    """Incremental decoder for streamed chunks.

    UTF-8 characters may be split across chunk boundaries; this buffers an
    incomplete trailing multibyte sequence and validates with the *incremental*
    codec. Invalid bytes are still rejected the moment they are provably
    invalid (not deferred forever). Byte offsets reported by the matcher always
    refer to raw payload bytes; buffering only delays when held-back bytes are
    *fed* to the automaton, it never changes their offsets because the matcher
    accounts for held-back bytes in its base offset.

    For single-byte encodings and binary mode nothing is buffered.
    """

    def __init__(self, spec: TextSpec):
        self._spec = spec
        self._carry = b""
        # We validate with a FRESH strict incremental decoder over
        # carry+chunk on every feed: a decoder that has seen an error is
        # unusable afterwards, and a rejected chunk must leave no residue.
        self._incremental = spec.encoding == "utf-8"

    @property
    def held_back(self) -> int:
        return len(self._carry)

    def feed(self, raw: bytes) -> bytes:
        """Return the bytes now releasable to the automaton.

        Raises :class:`EncodingError` on a definitively invalid byte *before*
        mutating carry, so a rejected chunk leaves stream state unchanged.
        Released bytes are always a prefix of the total valid bytes; held
        bytes are released (and matched) once the rest of their character
        arrives, so raw byte offsets stay exact — the matcher counts held
        bytes in the base offset when they are eventually released.
        """
        spec = self._spec
        if spec.is_binary:
            data = raw
        elif self._incremental:
            combined = self._carry + raw
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            try:
                decoder.decode(combined, final=False)
            except UnicodeDecodeError as exc:
                raise EncodingError(
                    f"chunk bytes are not valid utf-8: invalid byte at "
                    f"stream offset {exc.start}",
                    details={"bad_offset": exc.start,
                             "encoding": "utf-8"},
                ) from exc
            releasable = _utf8_complete_prefix_len(combined)
            self._carry = combined[releasable:]
            data = combined[:releasable]
        else:
            # Single-byte encodings: every byte stands alone.
            try:
                raw.decode(spec.encoding)
            except UnicodeDecodeError as exc:
                raise EncodingError(
                    f"chunk bytes are not valid {spec.encoding}: invalid "
                    f"byte at byte {exc.start} of this chunk",
                    details={"bad_offset_in_chunk": exc.start,
                             "encoding": spec.encoding},
                ) from exc
            data = raw

        if spec.case_mode is CaseMode.ASCII_CASEFOLD:
            return ascii_casefold(data)
        return data

    def finish(self) -> None:
        """At stream end, any carry is an unterminated sequence -> invalid."""
        if self._incremental and self._carry:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            try:
                decoder.decode(self._carry, final=True)
            except UnicodeDecodeError as exc:
                raise EncodingError(
                    "stream ends in the middle of a utf-8 sequence",
                    details={"bad_offset": exc.start,
                             "held_bytes": len(self._carry)},
                ) from exc


def _utf8_complete_prefix_len(buf: bytes) -> int:
    """Length of the longest prefix of ``buf`` ending on a char boundary.

    A valid UTF-8 stream may end with 1-3 lead/continuity bytes that start a
    new character but do not finish it. Such a tail is kept as carry rather
    than declared invalid.
    """
    i = len(buf)
    # Walk back over trailing continuation bytes (10xxxxxx).
    cont = 0
    while cont < i and (buf[i - 1 - cont] & 0xC0) == 0x80:
        cont += 1
    if cont == 0:
        # Last byte is ASCII or a lead byte; a lone lead at the end is carry.
        if i and buf[i - 1] >= 0x80:
            lead = buf[i - 1]
            need = _utf8_seq_len(lead)
            if need is not None and need >= 2:
                return i - 1
        return i
    # There are `cont` continuation bytes; find the lead byte before them.
    lead_pos = i - 1 - cont
    if lead_pos < 0 or (buf[lead_pos] & 0xC0) != 0xC0:
        # Continuations without a lead in this buffer: whole tail is suspect.
        # The strict decoder already validated decode; treat as carry.
        return 0
    lead = buf[lead_pos]
    need = _utf8_seq_len(lead)
    if need is None:
        return i  # decoder would have raised
    have = cont + 1  # lead + continuations
    if have < need:
        # Incomplete multibyte sequence at the tail: hold it all back.
        return lead_pos
    return i


def _utf8_seq_len(lead: int) -> Optional[int]:
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return None
