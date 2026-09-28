"""Byte/text decoding. UTF-8 only (BOM tolerated); anything else is an explicit
error, never a silent lossy decode."""
import codecs

from app.errors import SubtitleParseError


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_subtitle_bytes(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return normalize_newlines(data.decode("utf-8-sig"))
    try:
        return normalize_newlines(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SubtitleParseError(
            "INVALID_ENCODING",
            f"content is not valid UTF-8: {exc}",
            details={"position": exc.start},
        ) from exc
