"""Restricted pattern language and exhaustive region enumeration.

Only one wildcard form is allowed on purpose:

* a literal string, or
* a literal prefix terminated by a single ``*`` (which matches any continuation,
  including the empty string).

Embedded or multiple wildcards are rejected at parse time.  This restriction is
what makes *exhaustive* boundary analysis possible: membership of a string in a
set of such patterns changes only at prefix boundaries, and those boundaries are
finite.  A general glob/regex language would make the "did the accessible set
grow?" question undecidable by enumeration -- the analyzer refuses such input
instead of silently approximating it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sentinel appended at a trie node to represent "continuation on a character
# that is not an outgoing edge here". It is filtered out of candidate use if it
# actually occurs in a policy literal (see enumerate_regions).
_OTHER_CANDIDATES = ("~other-edge~", "#other-edge#")


class PatternError(ValueError):
    """Raised when a pattern uses syntax the analyzer cannot handle exhaustively."""


@dataclass(frozen=True)
class GlobPattern:
    raw: str
    prefix: str
    wildcard: bool  # True => raw == prefix + "*"

    @classmethod
    def parse(cls, raw: object, *, what: str = "pattern") -> "GlobPattern":
        if not isinstance(raw, str):
            raise PatternError(f"{what} must be a string, got {type(raw).__name__}")
        if raw == "":
            raise PatternError(f"{what} must not be empty")
        stars = raw.count("*")
        if stars == 0:
            return cls(raw=raw, prefix=raw, wildcard=False)
        if stars == 1 and raw.endswith("*"):
            return cls(raw=raw, prefix=raw[:-1], wildcard=True)
        raise PatternError(
            f"{what} {raw!r}: only a single trailing '*' is supported "
            "(embedded or multiple wildcards cannot be analyzed exhaustively)"
        )

    def matches(self, s: str) -> bool:
        if self.wildcard:
            return s.startswith(self.prefix)
        return s == self.prefix


def _build_trie(literals: list[str]) -> dict:
    """Build a plain char-trie from literal prefix strings."""
    root: dict = {"children": {}, "terminal": False}
    for word in literals:
        node = root
        for ch in word:
            node = node["children"].setdefault(ch, {"children": {}, "terminal": False})
        node["terminal"] = True
    return root


def enumerate_regions(patterns: list[GlobPattern], *, continuation_alphabet: str = "") -> list[str]:
    """Return a finite set of representative strings covering every membership
    vector against ``patterns``.

    Guarantee (the exhaustiveness argument the tests check):

        For any string ``s`` there is a returned representative ``r`` such that
        ``p.matches(s) == p.matches(r)`` for every pattern ``p``.

    Sketch: membership in literal-exact and prefix patterns is determined by
    the string's position relative to the pattern prefixes.  We build a trie of
    all prefixes; at every trie node with literal prefix ``u`` we emit
    representatives for the regions "exactly u", "u continued by a character on
    no outgoing edge", and "u continued by one outgoing edge c" (recursively).
    The "other" character is selected to be on no outgoing edge at that node.
    """
    literals = sorted({p.prefix for p in patterns})
    trie = _build_trie(literals)

    seen = sorted({ch for word in literals for ch in word})
    for cand in list(_OTHER_CANDIDATES) + list(continuation_alphabet):
        if cand not in seen:
            other_char = cand
            break
    else:
        # Extremely pathological: every candidate occurs in literals. Fall back
        # to a Unicode private-use codepoint that does not.
        for cp in range(0xE000, 0xF900):
            if chr(cp) not in seen:
                other_char = chr(cp)
                break
        else:  # pragma: no cover
            raise PatternError("cannot find an 'other-edge' continuation character")

    reps: set[str] = set()

    def walk(prefix_acc: str, node: dict) -> None:
        # Region 1: the exact prefix. Exact-only vs prefix patterns differ here.
        reps.add(prefix_acc)
        if node["children"]:
            # Region 2: continuation along an edge character.
            for ch, child in sorted(node["children"].items()):
                walk(prefix_acc + ch, child)
            # Region 3: a continuation character that is NOT an edge here.
            reps.add(prefix_acc + other_char)

    walk("", trie)
    reps.discard("")
    if not reps:
        # Universal "*" alone (empty prefix, no children): still need a probe.
        reps.add(other_char)
    return sorted(reps)
