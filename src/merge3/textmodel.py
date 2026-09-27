"""Text normalization primitives.

Boundary semantics
------------------
* Text is an arbitrary ``str`` decoded by the caller (the API layer enforces
  UTF-8).  Internally text is split into *lines* where every line carries its
  own terminator, so ``CRLF``, ``LF``, ``CR`` and *no trailing newline* are
  all first-class and never silently rewritten.
* All offsets used across the package are character offsets (Python ``str``
  indexing), half-open: ``[start, end)``.
* Line indexes are 0-based and half-open as well.  A pure insertion between
  two lines has ``line_start == line_end`` (a *point* edit at a boundary).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Recognized line terminators, longest first so ``\\r\\n`` wins over ``\\r``.
EOLS = ("\r\n", "\r", "\n")


@dataclass(frozen=True)
class Line:
    """One line: its content without terminator plus the exact terminator."""

    text: str
    eol: str  # "", "\n", "\r\n" or "\r"

    @property
    def raw(self) -> str:
        return self.text + self.eol

    @property
    def has_eol(self) -> bool:
        return self.eol != ""


def split_lines(text: str) -> list[Line]:
    """Split *text* into :class:`Line` objects without losing any byte.

    ``join_lines(split_lines(t)) == t`` for every input, including ``""``,
    a lone terminator, mixed terminators and a missing final newline.
    """
    lines: list[Line] = []
    i = 0
    n = len(text)
    while i < n:
        j = i
        while j < n and text[j] != "\r" and text[j] != "\n":
            j += 1
        content = text[i:j]
        if j == n:
            eol = ""
            k = j
        elif text[j] == "\r" and j + 1 < n and text[j + 1] == "\n":
            eol = "\r\n"
            k = j + 2
        elif text[j] == "\r":
            eol = "\r"
            k = j + 1
        else:
            eol = "\n"
            k = j + 1
        lines.append(Line(content, eol))
        i = k
    return lines


def join_lines(lines: list[Line]) -> str:
    """Inverse of :func:`split_lines`."""
    return "".join(line.raw for line in lines)


def line_offsets(lines: list[Line]) -> list[tuple[int, int]]:
    """Return ``[(content_start, content_end_exclusive), ...]``.

    The terminator (if any) occupies ``[content_end, content_end + len(eol))``.
    A final sentinel equal to the total document length is appended, so the
    boundary offset *after* line ``k`` is ``line_offsets(lines)[k][1] +
    len(lines[k].eol)``; convenience: use :func:`boundary_offsets`.
    """
    offsets: list[tuple[int, int]] = []
    pos = 0
    for line in lines:
        offsets.append((pos, pos + len(line.text)))
        pos += len(line.raw)
    return offsets


def boundary_offsets(lines: list[Line]) -> list[int]:
    """Offset of every line boundary, length ``len(lines) + 1``.

    ``boundary_offsets(lines)[k]`` is the character position between line
    ``k-1`` and line ``k`` (0 = document start, last = document length).
    """
    bounds = [0]
    pos = 0
    for line in lines:
        pos += len(line.raw)
        bounds.append(pos)
    return bounds


def has_trailing_newline(text: str) -> bool:
    return text.endswith(("\n", "\r"))


def detect_dominant_eol(text: str) -> str:
    """Return the most frequent terminator in *text* (``"\\n"`` if none)."""
    counts = {"\r\n": 0, "\r": 0, "\n": 0}
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                counts["\r\n"] += 1
                i += 2
                continue
            counts["\r"] += 1
        elif ch == "\n":
            counts["\n"] += 1
        i += 1
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] else "\n"


def normalize_eol(text: str, mode: str) -> str:
    """Explicit EOL conversion.

    .. warning::
        Never called anywhere inside the merge core: merging preserves
        terminators exactly.  This exists for callers that *ask* for a
        conversion after the fact.
    """
    if mode not in ("preserve", "lf", "crlf", "cr"):
        raise ValueError(f"unknown eol mode: {mode!r}")
    if mode == "preserve":
        return text
    target = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}[mode]
    out: list[str] = []
    for line in split_lines(text):
        out.append(line.text + (target if line.has_eol else ""))
    return "".join(out)
