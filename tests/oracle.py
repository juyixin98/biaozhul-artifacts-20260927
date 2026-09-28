"""Independent reference oracle (stdlib ``re`` only).

This module deliberately imports **none** of the service's core packages.  It
re-implements the expected leftmost, zero-width-aware, priority-resolved
planning semantics on top of the standard library ``re`` module so test
expectations do not come from the code under test.

Where the service reasons in raw UTF-8 byte offsets, the oracle works in
codepoint offsets (``str`` indices) and maps to byte offsets with a direct
prefix-byte table.  Both views are returned so tests can assert on either.

The stdlib constructs the oracle relies on:
  * ``re.compile`` / ``pattern.finditer`` -- Python's documented
    leftmost-first non-overlapping iteration, including the zero-width advance
    rule (empty match advances one character; an empty match at the end fires
    once);
  * plain Python for templates ($-syntax) and overlap resolution.

RE2 and Python ``re`` are different engines, so equivalence is asserted only
for the RE2-safe subset (no backrefs/lookaround, ASCII-aware classes), which is
exactly the contract the service exposes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RefEdit:
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    replacement: str
    rule_id: str
    zero_width: bool


_FLAG_MAP = {"i": re.I, "s": re.S, "m": re.M}


def _compile(pattern: str, flags: str = "", longest_match: bool = False):
    val = 0
    for f in flags or "":
        val |= _FLAG_MAP[f]
    return re.compile(pattern, val)


def _char_to_byte_prefix(text: str) -> list[int]:
    """prefix[k] = UTF-8 byte offset of codepoint k; prefix[len] = byte len."""
    pref = [0]
    for ch in text:
        pref.append(pref[-1] + len(ch.encode("utf-8")))
    return pref


# --------------------------------------------------------------------------- #
# Template rendering, independently specified from app.template.
# Syntax: $$ -> $ ; $0..$9 digit; ${name} / ${12}
# --------------------------------------------------------------------------- #
def render_template(template: str, match: re.Match, *, missing: str = "error") -> str:
    out = []
    i = 0
    while i < len(template):
        c = template[i]
        if c != "$":
            out.append(c)
            i += 1
            continue
        if i + 1 >= len(template):
            out.append("$")
            i += 1
        elif template[i + 1] == "$":
            out.append("$")
            i += 2
        elif template[i + 1] == "{":
            end = template.find("}", i + 2)
            if end == -1:
                raise ValueError("bad template: unclosed brace")
            body = template[i + 2:end]
            out.append(_ref(match, body, missing))
            i = end + 1
        elif template[i + 1].isdigit():
            out.append(_ref(match, template[i + 1], missing))
            i += 2
        else:
            out.append("$")
            i += 1
    return "".join(out)


def _ref(match: re.Match, body: str, missing: str) -> str:
    if body.isdigit():
        idx = int(body)
    else:
        idx = match.re.groupindex[body]
    val = match.group(idx)
    if val is None:
        if missing == "error":
            raise LookupError(f"group {body} did not participate")
        return ""
    return val


@dataclass(frozen=True)
class RefCandidate:
    rule_id: str
    priority: int
    cs: int
    ce: int
    match: re.Match
    replacement: str | None = None  # None until rendered (may raise)


def reference_plan(
    text: str, rules: list[dict], *, render_now: bool = True
) -> list[RefEdit]:
    """Compute the expected edit list independently.

    Conflict contract (mirrors the documented service contract):
      * rules considered in (-priority, rule_id) order;
      * same start offset conflicts (all pairs, incl. zero-width);
      * non-empty candidates conflict on half-open interval overlap;
      * a zero-width candidate strictly INSIDE a claimed non-empty range
        (cs < point < ce) is suppressed; a point exactly at an end boundary is
        adjacent and allowed.
    """
    pref = _char_to_byte_prefix(text)
    ordered = sorted(rules, key=lambda r: (-int(r.get("priority", 0)), r["rule_id"]))

    import bisect

    points: dict[int, str] = {}      # char offset -> owner
    iv_starts: list[int] = []        # sorted disjoint non-empty intervals
    iv_ends: list[int] = []
    iv_owners: list[str] = []
    accepted: list[RefCandidate] = []

    def interval_owner(s: int, e: int) -> str | None:
        i = bisect.bisect_right(iv_starts, s) - 1
        if i >= 0 and s < iv_ends[i]:
            return iv_owners[i]
        j = i + 1
        if j < len(iv_starts) and iv_starts[j] < e:
            return iv_owners[j]
        return None

    def point_inside_owner(p: int) -> str | None:
        # A zero-width candidate strictly inside a claimed non-empty range is
        # suppressed; at the exact end boundary it is adjacent (allowed).
        i = bisect.bisect_right(iv_starts, p) - 1
        if i >= 0 and p < iv_ends[i]:
            return iv_owners[i]
        return None

    for r in ordered:
        pat = _compile(
            r["pattern"],
            flags=r.get("flags", ""),
            longest_match=r.get("longest_match", False),
        )
        policy = r.get("missing_capture", "error")
        # Python re.finditer gives the documented zero-width advance behavior.
        for m in pat.finditer(text):
            cs, ce = m.start(0), m.end(0)
            if cs in points:
                continue
            if ce > cs:
                owner = interval_owner(cs, ce)
            else:
                owner = point_inside_owner(cs)
            if owner is not None:
                continue
            repl = render_template(r["template"], m, missing=policy) if render_now else None
            accepted.append(
                RefCandidate(
                    rule_id=r["rule_id"], priority=int(r.get("priority", 0)),
                    cs=cs, ce=ce, match=m, replacement=repl,
                )
            )
            points[cs] = r["rule_id"]
            if ce > cs:
                k = bisect.bisect_left(iv_starts, cs)
                iv_starts.insert(k, cs)
                iv_ends.insert(k, ce)
                iv_owners.insert(k, r["rule_id"])

    accepted.sort(key=lambda c: (c.cs, c.ce))
    edits: list[RefEdit] = []
    for c in accepted:
        repl = c.replacement if c.replacement is not None else ""
        edits.append(
            RefEdit(
                char_start=c.cs,
                char_end=c.ce,
                byte_start=pref[c.cs],
                byte_end=pref[c.ce],
                replacement=repl,
                rule_id=c.rule_id,
                zero_width=(c.cs == c.ce),
            )
        )
    return edits


def reference_apply(text: str, rules: list[dict]) -> str:
    """Expected final document for text under rules (render-eager)."""
    edits = reference_plan(text, rules)
    out: list[str] = []
    cursor = 0
    for e in edits:
        out.append(text[cursor:e.char_start])
        out.append(e.replacement)
        cursor = e.char_end
    out.append(text[cursor:])
    return "".join(out)
