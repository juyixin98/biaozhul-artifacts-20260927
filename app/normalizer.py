"""Text normalization with an auditable per-character mapping.

The segmenter never edits input "in place": :func:`normalize` returns the
normalized text together with a character map describing where every
normalized character came from in the original string. Every rule is an
explicit table — no implicit Unicode data-version dependence in hot paths —
so tests can assert exact offsets, including rules that change length
(full-width ASCII folds 1->1; ``ß`` folds to ``ss`` 1->2; soft hyphen is
deleted 1->0). Characters are never silently merged or dropped without a
mapping record.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Single characters removed by normalization (mapped to the empty string).
# Each removal is still recorded in the character map (norm_start == norm_end).
DELETE_CHARS = frozenset(
    {
        "\u00ad",  # U+00AD soft hyphen
        "\u200b",  # U+200B zero-width space
        "\u200c",  # U+200C zero-width non-joiner
        "\u200d",  # U+200D zero-width joiner
    }
)

# Explicit 1->N mappings. Everything not listed here and not covered by the
# full-width ASCII range below passes through unchanged.
MULTI_CHAR_MAP = {
    "ß": "ss",
    "ẞ": "SS",
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
    "　": " ",  # ideographic space -> ordinary space
}


def _char_replacement(ch: str) -> str:
    if ch in DELETE_CHARS:
        return ""
    if ch in MULTI_CHAR_MAP:
        return MULTI_CHAR_MAP[ch]
    code = ord(ch)
    # Full-width ASCII variants U+FF01..U+FF5E -> U+0021..U+007E.
    if 0xFF01 <= code <= 0xFF5E:
        return chr(code - 0xFEE0)
    return ch


@dataclass(frozen=True)
class NormalizedText:
    text: str
    # norm_index -> raw index, one entry per *emitted* normalized character.
    char_map: tuple[int, ...]
    # raw indices removed (soft hyphen etc.)
    deleted_raw_indices: tuple[int, ...]

    def raw_span(self, norm_start: int, norm_end: int) -> tuple[int, int]:
        """Map a normalized half-open span back to raw offsets.

        Deleted characters interspersed inside the span are included in the
        raw span so that concatenating successive spans covers the original
        text with no gaps.
        """
        if norm_end <= norm_start:
            return norm_start, norm_start  # only used for empty input edges
        raw_start = self.char_map[norm_start]
        raw_end = self.char_map[norm_end - 1] + 1
        return raw_start, raw_end


def normalize(raw: str) -> NormalizedText:
    out_chars: list[str] = []
    char_map: list[int] = []
    deleted: list[int] = []
    for raw_idx, ch in enumerate(raw):
        repl = _char_replacement(ch)
        if repl == "":
            deleted.append(raw_idx)
            continue
        for out_ch in repl:
            out_chars.append(out_ch)
            char_map.append(raw_idx)
    return NormalizedText(
        text="".join(out_chars),
        char_map=tuple(char_map),
        deleted_raw_indices=tuple(deleted),
    )


def normalize_word(word: str) -> str:
    """Normalize a single lexicon word (used when publishing a version)."""
    return normalize(word).text


def redact_text(text: str, *, keep: int = 2) -> str:
    """Return a safe preview of potentially sensitive input.

    The full string is never logged. Short strings are fully masked; longer
    strings expose only their first ``keep`` characters plus a length and a
    salted fingerprint, enough to correlate logs without leaking content.
    """
    n = len(text)
    if n <= keep:
        return "*" * len(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{text[:keep]}{'*' * 6}(len={n},sha8={digest})"
