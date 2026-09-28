"""gitignore-style ignore matching (hand-rolled, dependency-free).

Supported pattern shapes (matching is against POSIX-style relative paths):
  * ``foo/``        -> prune any directory whose basename equals ``foo``
  * ``vendor/lib/`` -> prune a directory at that root-relative path
  * ``build/*.tmp`` -> anchored glob (``/`` is not crossed by ``*``)
  * ``*.log``       -> basename glob at any depth (files or directories)
  * ``a/**/b``      -> ``**`` crosses any number of directories

A trailing slash restricts a pattern to directories; without one the pattern
matches both files and directories, like gitignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _translate_glob(pat: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                # '**' crosses directory separators
                i += 2
                if i < len(pat) and pat[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class _CompiledPattern:
    dir_only: bool
    basename_match: bool
    body_regex: re.Pattern[str]


def _compile(pattern: str) -> _CompiledPattern:
    if not pattern.strip():
        raise ValueError(f"empty ignore pattern: {pattern!r}")
    dir_only = pattern.endswith("/")
    body = pattern.rstrip("/")
    return _CompiledPattern(
        dir_only=dir_only,
        basename_match=("/" not in body),
        body_regex=re.compile("^" + _translate_glob(body) + "$"),
    )


class IgnorePolicy:
    def __init__(self, patterns: tuple[str, ...] | list[str]):
        self._patterns = tuple(_compile(p) for p in patterns)
        self.raw_patterns = tuple(patterns)

    @staticmethod
    def _target(rel_posix: str, basename_match: bool) -> str:
        if basename_match:
            return rel_posix.rsplit("/", 1)[-1]
        return rel_posix

    def _hits(self, cp: _CompiledPattern, rel_posix: str) -> bool:
        return cp.body_regex.match(self._target(rel_posix, cp.basename_match)) is not None

    def is_dir_ignored(self, rel_posix: str) -> bool:
        for cp in self._patterns:
            if self._hits(cp, rel_posix):
                return True
        return False

    def is_file_ignored(self, rel_posix: str) -> bool:
        parts = rel_posix.split("/")
        ancestors = parts[:-1]
        for cp in self._patterns:
            # Direct match (only non-dir patterns match files directly).
            if not cp.dir_only and self._hits(cp, rel_posix):
                return True
            # File sits beneath a matched (pruned) directory.
            if cp.dir_only:
                if cp.basename_match:
                    name = cp.body_regex.pattern[1:-1]
                    if any(re.fullmatch(name, a) for a in ancestors):
                        return True
                else:
                    expr = cp.body_regex.pattern[1:-1]
                    if re.match("^(?:" + expr + ")/", rel_posix) is not None:
                        return True
        return False
