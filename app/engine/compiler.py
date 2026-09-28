"""Compile user patterns onto RE2 with a fixed, documented option surface.

Why RE2
-------
RE2 compiles patterns to a bounded DFA/NFA program and runs matching in linear
time: it never backtracks, so adversarial patterns cannot trigger exponential
work.  In exchange it omits backreferences and look-around.  Those constructs
cannot express "non-overlapping leftmost" planning any better than the
supported syntax, so the omission is part of the contract, not a workaround.

Supported user flags (a deliberately small, allow-listed subset)::

    "i" -> case-insensitive      (RE2 case_sensitive=False)
    "s" -> dot matches newline   (RE2 dot_nl=True)
    "m" -> ^ and $ at line edges (inline (?m) -- RE2 defaults one_line=False
           meaning ^/$ only anchor whole text, so we PREPEND (?m) when asked)

Inline ``(?i)``/``(?s)``/``(?m)`` groups inside the pattern remain valid RE2
syntax and are left untouched.

Budgets
-------
``max_mem`` is RE2's own program-memory ceiling (compile + cached program).
Hitting it produces the stable error code ``pattern_budget_exceeded`` rather
than leaking the C++ message.
"""

from __future__ import annotations

from dataclasses import dataclass

import re2

from ..errors import (
    InvalidPatternError,
    PatternBudgetExceededError,
    UnsupportedSyntaxError,
)

# Substrings RE2 emits for constructs it will never support.  Kept narrow on
# purpose: we only re-classify messages we can attribute to a known category;
# everything else stays ``invalid_pattern`` with the engine text attached.
_UNSUPPORTED_MARKERS = (
    "invalid perl operator: (?",
    "bad escape sequence",  # e.g. \1 style backrefs are caught elsewhere too
)
_BACKTRACK_MARKERS = (
    "invalid named capture",
)
_BUDGET_MARKERS = ("pattern too large", "too many")

_ALLOWED_FLAGS = {"i", "s", "m"}


@dataclass(frozen=True, slots=True)
class EngineOptions:
    """User-facing engine configuration (validated at the API boundary too)."""

    flags: str = ""
    max_mem: int = 8 * 1024 * 1024
    longest_match: bool = False
    """POSIX longest match instead of leftmost-first.  Exposed for tests and
    callers that want POSIX semantics; the planning rules default to
    leftmost-first (Perl-style), matching Python ``re`` so the independent
    oracle can agree."""

    def normalized_flags(self) -> str:
        flags = self.flags or ""
        unknown = sorted({c for c in flags if c not in _ALLOWED_FLAGS})
        if unknown:
            raise InvalidPatternError(
                f"unsupported pattern flags: {''.join(unknown)}",
                allowed=sorted(_ALLOWED_FLAGS),
            )
        # de-duplicate while keeping a stable order
        return "".join(c for c in "ism" if c in flags)


class CompiledPattern:
    """A validated RE2 pattern plus its static group metadata."""

    __slots__ = ("pattern", "flags", "regexp", "groupindex", "ngroups")

    def __init__(self, pattern: str, options: EngineOptions, regexp: "re2._Regexp") -> None:
        self.pattern = pattern
        self.flags = options.normalized_flags()
        self.regexp = regexp
        # bytes-compiled pattern -> group names arrive as bytes
        gi = regexp.groupindex
        self.groupindex: dict[str | bytes, int] = gi
        self.ngroups: int = regexp.groups

    def group_number(self, ref: int | str) -> int | None:
        """Resolve a template reference to a 1-based group number."""
        if isinstance(ref, int):
            return ref if 0 <= ref <= self.ngroups else None
        # name lookup accepts str or bytes
        if ref in self.groupindex:
            return self.groupindex[ref]
        b = ref.encode("utf-8")
        if b in self.groupindex:
            return self.groupindex[b]
        return None

    def group_names_str(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, idx in self.groupindex.items():
            if isinstance(name, bytes):
                out[name.decode("utf-8")] = idx
            else:
                out[name] = idx
        return out


def _build_re2(pattern: str, options: EngineOptions) -> "re2._Regexp":
    opts = re2.Options()
    flags = options.normalized_flags()
    opts.max_mem = int(options.max_mem)
    opts.longest_match = bool(options.longest_match)
    # Byte input is what the scanner feeds; compile the pattern as bytes too so
    # spans come back as raw UTF-8 offsets.  RE2 validates UTF-8 input itself
    # and never splits a codepoint.
    encoded = pattern.encode("utf-8")

    if "m" in flags:
        # RE2 default has one_line=False which means ^/$ match *only* text
        # edges; (?m) restores the familiar per-line anchoring.
        encoded = b"(?m)" + encoded
    if "i" in flags:
        opts.case_sensitive = False
    if "s" in flags:
        opts.dot_nl = True
    # perl_classes stays False on purpose: \w/\d stay ASCII-only.  This is the
    # RE2 default and a documented difference from Python re (UNICODE); the
    # tests pin it so callers relying on \w for non-ASCII get a clear result.
    # The constructor raises re2.error on a bad pattern (no separate ok() call
    # on the Python wrapper).
    return re2._Regexp(encoded, opts)  # type: ignore[attr-defined]


def compile_pattern(pattern: str, options: EngineOptions | None = None) -> CompiledPattern:
    """Compile and validate ``pattern``.

    Raises :class:`InvalidPatternError`, :class:`UnsupportedSyntaxError` or
    :class:`PatternBudgetExceededError`.
    """
    options = options or EngineOptions()
    if not isinstance(pattern, str):
        raise InvalidPatternError("pattern must be a string")
    # Normalize/validate flags first (raises on unknown).
    options.normalized_flags()
    if options.max_mem <= 0:
        raise PatternBudgetExceededError(
            "max_mem must be positive", max_mem=options.max_mem
        )

    try:
        regexp = _build_re2(pattern, options)
    except re2.error as exc:  # type: ignore[attr-defined]
        msg = _exc_text(exc)
        low = msg.lower()
        if any(m in low for m in _BUDGET_MARKERS):
            raise PatternBudgetExceededError(
                "pattern rejected by RE2 memory/program budget",
                engine_message=msg,
                max_mem=options.max_mem,
            ) from exc
        if "(?" in low and "invalid perl operator" in low:
            raise UnsupportedSyntaxError(
                "pattern uses a look-around or other backtracking-only "
                "construct that RE2 does not support",
                engine_message=msg,
            ) from exc
        if "\\" in pattern and _looks_like_backref(pattern):
            raise UnsupportedSyntaxError(
                "backreferences are not supported by the linear-time engine",
                engine_message=msg,
            ) from exc
        raise InvalidPatternError(
            "pattern failed to compile", engine_message=msg
        ) from exc

    return CompiledPattern(pattern, options, regexp)


def _exc_text(exc: BaseException) -> str:
    # pybind11 Error stringifies to b'...'; normalize to a plain str.
    s = str(exc)
    if s.startswith("b'") and s.endswith("'"):
        s = s[2:-1]
    elif s.startswith('b"') and s.endswith('"'):
        s = s[2:-1]
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def _looks_like_backref(pattern: str) -> bool:
    """Heuristic only used to enrich error messages; never affects matching."""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and pattern[i + 1].isdigit():
            # \0 octal / \1..\9 backrefs -- RE2 treats numeric escapes as
            # octal; flag them as unsupported-backref intent.
            return True
        i += 2 if c == "\\" else 1
    return False
