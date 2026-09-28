"""Text normalization with a faithful original-offset mapping.

Normalization policy (applied per original character):

    normalized = remove(ch) or unicodedata.normalize("NFKC", ch).casefold()

where ``remove`` drops an explicit, documented set of zero-width/format
characters (``_REMOVED_CHARS``: ZWSP, ZWNJ, ZWJ, WORD JOINER, SOFT HYPHEN,
BOM, ...). NFKC alone does **not** delete ZWSP/SOFT HYPHEN, so the removal is
an intentional, bounded policy rather than an accident of Unicode tables.

This deliberately supports *variable-length* mappings:

* expansion:  one original char -> several normalized chars ("ﬁ" -> "fi",
  "ß" -> "ss" after case folding);
* removal:    characters in ``_REMOVED_CHARS`` disappear entirely;
* 1:1 folding: fullwidth/compatibility forms map to their standard form.

The original half-open offsets returned for every token are guaranteed to
**tile** the original string exactly: no gaps, no overlaps, so joining
``original[start:end]`` over the tokens reconstructs the input verbatim.
Format-only characters attach to the *following* real character when present
(so "研<ZWSP>究" yields one token spanning all three original characters),
otherwise to the preceding token.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Explicit removal set. Kept small and named on purpose -- these are the
# characters this service treats as non-content.
_REMOVED_CHARS = frozenset(
    chr(cp)
    for cp in (
        0x00AD,  # SOFT HYPHEN
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
        0x2060,  # WORD JOINER
    )
)


def fold_character(ch: str) -> str:
    """Normalize a single original character into a normalized fragment ("" = removed)."""
    if ch in _REMOVED_CHARS:
        return ""
    return unicodedata.normalize("NFKC", ch).casefold()


@dataclass(frozen=True)
class NormalizedText:
    """Result of normalization.

    Attributes:
        text: normalized string used by the dictionary / DAG.
        owner: ``owner[i]`` is the index of the original character that
            produced normalized character ``i`` (repeated on expansion).
        source_length: length of the original string.
        removed_chars: count of original characters normalizing to "".
    """

    text: str
    owner: tuple[int, ...]
    source_length: int
    removed_chars: int

    @property
    def length(self) -> int:
        return len(self.text)

    def orig_span(self, norm_start: int, norm_end: int) -> tuple[int, int]:
        """Map a normalized half-open range to an original half-open range.

        Consecutive spans tile ``[0, source_length)``; see module docstring.
        Spans inside an expansion (e.g. between "f" and "i" from "ﬁ") map to an
        empty slice at a single offset point -- joining slices still rebuilds
        the original exactly.
        """
        n = len(self.text)
        orig_n = self.source_length
        if orig_n == 0:
            return (0, 0)
        if n == 0:
            # Everything normalized away (format-only input).
            return (0, orig_n)
        if not (0 <= norm_start <= norm_end <= n):
            raise ValueError(f"normalized range out of bounds: [{norm_start}, {norm_end}) / {n}")

        # Original offset of normalized boundary k:
        #   k == 0 -> 0 (leading format chars attach to the first token);
        #   k == n -> source_length (trailing format chars attach to last);
        #   otherwise -> owner[k-1] + 1 (a removed char between survivors
        #                attaches forward, since owner[k-1] was skipped).
        def boundary(k: int) -> int:
            if k == 0:
                return 0
            if k == n:
                return orig_n
            return self.owner[k - 1] + 1

        start = boundary(norm_start)
        end = max(start, boundary(norm_end))
        return (start, end)


def normalize_text(source: str) -> NormalizedText:
    """Normalize a piece of text, retaining the per-character provenance."""
    pieces: list[str] = []
    owner: list[int] = []
    removed = 0
    for idx, ch in enumerate(source):
        folded = fold_character(ch)
        if folded == "":
            removed += 1
            continue
        pieces.append(folded)
        owner.extend([idx] * len(folded))
    return NormalizedText(
        text="".join(pieces),
        owner=tuple(owner),
        source_length=len(source),
        removed_chars=removed,
    )


def normalize_surface(surface: str) -> str:
    """Normalize a dictionary surface to its lookup key."""
    return "".join(fold_character(ch) for ch in surface)
