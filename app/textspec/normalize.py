"""Source text normalization.

Contract
--------
* Input may arrive as ``str`` or ``bytes``.  Bytes MUST be valid UTF-8
  (``strict``); otherwise :class:`~app.errors.TextDecodeError` is raised and
  nothing downstream is touched.
* Normalization is intentionally minimal and **explicit**: this service edits
  byte ranges of a user's document, so silent NFC/NFKC transformations are
  unsafe.  The only mutation offered is ``normalize_newlines`` (CRLF/CR -> LF),
  which is opt-in and recorded in the result so plans can carry it.
* A BOM is preserved verbatim (it is a real byte sequence that may be part of
  an edit); callers who want it stripped do so before upload.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import TextDecodeError


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """The single canonical representation used by engine, planner and store."""

    data: bytes
    """UTF-8 bytes of the document.  All plan spans index this exact buffer."""

    normalize_newlines: bool = False

    @property
    def size(self) -> int:
        return len(self.data)

    def as_str(self) -> str:
        # Never fails: data was decoded on the way in.
        return self.data.decode("utf-8")


def normalize_source(
    text: str | bytes,
    *,
    normalize_newlines: bool = False,
) -> NormalizedText:
    """Decode and optionally canonicalize line endings.

    Raises :class:`~app.errors.TextDecodeError` on undecodable input.
    """
    if isinstance(text, str):
        data = text.encode("utf-8")
    elif isinstance(text, (bytes, bytearray, memoryview)):
        data = bytes(text)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TextDecodeError(
                "source is not valid UTF-8",
                start=exc.start,
                end=exc.end,
                reason=exc.reason,
            ) from exc
    else:
        raise TextDecodeError(
            f"source must be str or bytes, got {type(text).__name__}"
        )

    if normalize_newlines:
        # CR and CRLF -> LF. Done on decoded text to avoid touching the 0x0D
        # byte inside any multibyte sequence (UTF-8 continuation bytes never
        # equal 0x0A/0x0D, so bytes-level translation is also safe, but the
        # text form keeps the intent auditable).
        s = data.decode("utf-8")
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        data = s.encode("utf-8")

    return NormalizedText(data=data, normalize_newlines=normalize_newlines)
