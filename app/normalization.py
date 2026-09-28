"""Text normalization and tokenization.

Pipeline per raw character:

1. Lower-case (Unicode-aware), done first so uppercase accented letters
   acquire a decomposable accent.
2. Unicode compatibility decomposition NFKD (full-width latin -> plain ASCII;
   ligatures like ``ﬁ`` -> ``fi``; ``é`` -> ``e`` + combining acute).
3. Drop non-spacing combining marks (``Mn``).

``NFKD`` rather than ``NFKC`` is used so that accents actually split off a
strippable mark (``NFKC`` would recompose ``e`` + acute back into ``é``).

Tokens are maximal runs of *allowed* characters separated by whitespace.
Any character that is not whitespace and not in the configured alphabet after
normalization raises :class:`UnsupportedCharacterError` with its position, so
the caller can reject the request explicitly rather than silently edit-distance
on junk.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .errors import EmptyQueryError, TooManyTokensError, UnsupportedCharacterError


@dataclass(frozen=True)
class Token:
    text: str
    start: int  # offset inside the normalized query
    end: int


def normalize_char(ch: str) -> str:
    # Lower FIRST (so uppercase accented letters acquire a decomposable
    # accent), then compatibility-decompose, then drop combining marks.
    lowered = ch.lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def normalize_and_tokenize(
    query: str,
    *,
    alphabet: frozenset[str],
    max_tokens: int,
) -> tuple[str, list[Token]]:
    """Return ``(normalized_query, tokens)``; raise with failure category."""
    if query is None or query.strip() == "":
        raise EmptyQueryError("query is empty after trimming whitespace")

    # Normalize per character; a single input char can expand (ﬁ -> fi), so we
    # track output offsets, not input offsets, in the tokens we return.
    out: list[str] = []
    for ch in query:
        out.append(normalize_char(ch))
    normalized = "".join(out)

    tokens: list[Token] = []
    i = 0
    n = len(normalized)
    while i < n:
        ch = normalized[i]
        if ch.isspace():
            i += 1
            continue
        if ch not in alphabet:
            raise UnsupportedCharacterError(
                f"character {ch!r} at position {i} is not in the supported alphabet",
                char=ch,
                position=i,
            )
        start = i
        while i < n and not normalized[i].isspace():
            c = normalized[i]
            if c not in alphabet:
                raise UnsupportedCharacterError(
                    f"character {c!r} at position {i} is not in the supported alphabet",
                    char=c,
                    position=i,
                )
            i += 1
        tokens.append(Token(text=normalized[start:i], start=start, end=i))
        if len(tokens) > max_tokens:
            raise TooManyTokensError(
                f"query contains more than {max_tokens} whitespace-separated tokens",
                details={"limit": max_tokens},
            )

    if not tokens:
        raise EmptyQueryError("query is empty after normalization")
    return normalized, tokens
