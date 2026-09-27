"""Text analysis and value normalization.

A single, deterministic analyzer is shared by (a) the inverted index when
documents are ingested and (b) query-side term/phrase handling, so a query
token can only match an indexed token if they analyze identically.

Rules (in order):
  1. Unicode NFKC normalization then ``casefold`` (case-insensitive text).
  2. Token characters are Unicode letters and decimal digits; everything
     else (whitespace, punctuation, ``_``, ``-`` ...) is a separator.
  3. Letter runs are kept together *except* CJK ideographs, which are
     emitted one character per token (unigram segmentation).
  4. Digit runs are decimal-number tokens.

Example::

    tokenize('Quick-Brown_fox 价格42!')
    # -> ['quick', 'brown', 'fox', '价', '格', '42']
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

# Runs of decimal digits OR letters (letters exclude underscores/digits).
_TOKEN_RE = re.compile(r"\d+|[^\W\d_]+", re.UNICODE)


def _is_cjk_ideograph(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF      # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF   # Extension A
        or 0xF900 <= cp <= 0xFAFF   # Compatibility Ideographs
    )


def normalize_text(text: str) -> str:
    """NFKC + casefold; used before tokenization."""
    return unicodedata.normalize("NFKC", text).casefold()


def _split_letters(run: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for ch in run:
        if _is_cjk_ideograph(ch):
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def tokenize(text: str) -> list[str]:
    """Analyze *text* into index terms. Pure and side-effect free."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalize_text(text)):
        piece = match.group(0)
        if piece[0].isdigit():
            tokens.append(piece)
        else:
            tokens.extend(_split_letters(piece))
    return tokens


def parse_int(value: str) -> int:
    """Parse a query-side integer literal.

    Accepts an optional sign and surrounding whitespace. Raises
    ``ValueError`` with a stable message otherwise.
    """
    s = value.strip()
    if not re.fullmatch(r"[+-]?\d+", s):
        raise ValueError(f"not an integer: {value!r}")
    return int(s)


def parse_date(value: str) -> str:
    """Validate an ISO calendar date and return its canonical form."""
    s = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        raise ValueError(f"not an ISO date (YYYY-MM-DD): {value!r}")
    year, month, day = (int(g) for g in m.groups())
    try:
        date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"not a calendar date: {value!r} ({exc})") from None
    return f"{year:04d}-{month:02d}-{day:02d}"
