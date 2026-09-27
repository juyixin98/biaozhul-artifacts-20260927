"""Restricted-subset inline markup handling.

Text encoding and markup are *preserved* verbatim on the cue. This module only
inspects payload lines to emit diagnostics; it never rewrites text.

Allowed inline tags (subset shared by SRT and VTT):
    <b>...</b> <i>...</i> <u>...</u> <font ...>...</font>

Not allowed in the subset:
    * ruby/ruby-position tags, class/annotation tags (<c.*>), lang tags,
      voice tags (<v>), karaoke tags (<k> / <K>), timestamp tags <HH:MM:SS.mmm>,
      any other unknown tag.

Tags must be properly nested and closed. Entity references (&amp; etc.) are
kept as literal text and not decoded.
"""
from __future__ import annotations

import re

from .models import Cue, Diagnostic, Severity

# Pairs we accept. Attribute content on <font> is tolerated (e.g. color/face).
_OPEN_ALLOWED = {"b", "i", "u"}
_TAG_RE = re.compile(r"</?([a-zA-Z][\w.-]*)((?:\s[^<>]*?)?)>")
# VTT inline timestamp tag: <HH?:MM:SS.mmm>, e.g. <00:00:02.000> or <01.500>
_VTT_CLOCK_TAG_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}\.\d{3}$")
_VTT_SECONDS_TAG_RE = re.compile(r"^\d{1,2}\.\d{3}$")

_VOID_OR_NONPAIRED = {"br"}  # <br> not allowed in our subset either


def scan_markup(cue: Cue, fmt: str) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    stack: list[tuple[str, str]] = []  # (lower_name, raw)
    if not any(line.strip() for line in cue.raw_lines):
        diags.append(
            Diagnostic(
                code="empty_text",
                severity=Severity.WARNING,
                message=f"cue #{cue.index + 1} has no visible text",
                cue_index=cue.index,
            )
        )

    # VTT inline timestamp tags look like named tags to the generic regex
    # (e.g. <00:00:02.000>); detect them explicitly first.
    text = cue.text
    if fmt == "vtt":
        for m in re.finditer(r"<([^<>]+)>", text):
            inner = m.group(1)
            if _VTT_CLOCK_TAG_RE.match(inner) or _VTT_SECONDS_TAG_RE.match(inner):
                diags.append(_unsupported(
                    cue, m.group(0), "inline timestamp tag"))

    for m in _TAG_RE.finditer(cue.text):
        raw = m.group(0)
        name = m.group(1).lower()
        # VTT class/annotation tags carry dot/hyphen suffixes: <c.foo>,
        # <lang.es-419>. Classify by the token before the first separator.
        base = re.split(r"[.\-:]", name, maxsplit=1)[0]
        attrs = m.group(2) or ""
        is_close = raw.startswith("</")

        # Skip tokens already classified as inline timestamps above.
        if fmt == "vtt" and (_VTT_CLOCK_TAG_RE.match(name + attrs.strip())
                             or re.match(r"^\d{1,2}\.\d{3}$", name)):
            continue

        if is_close:
            if base not in _OPEN_ALLOWED and base != "font":
                # Closing an element outside the subset: pairing error, not a
                # second "unsupported" complaint.
                diags.append(
                    Diagnostic(
                        code="unpaired_tag",
                        severity=Severity.ERROR,
                        message=f"cue #{cue.index + 1}: closing tag {raw} closes "
                        f"an element outside the subset (stack: "
                        f"{[s[0] for s in stack]})",
                        cue_index=cue.index,
                        detail={"tag": raw},
                    )
                )
                continue
            if not stack or stack[-1][0] != base:
                diags.append(
                    Diagnostic(
                        code="unpaired_tag",
                        severity=Severity.ERROR,
                        message=f"cue #{cue.index + 1}: closing tag {raw} has no "
                        f"matching open tag (stack: {[s[0] for s in stack]})",
                        cue_index=cue.index,
                        detail={"tag": raw},
                    )
                )
                continue
            stack.pop()
            continue

        # Opening tag.
        if base in _OPEN_ALLOWED:
            if attrs.strip() or base != name:
                diags.append(_unsupported(
                    cue, raw, f"attributes/annotation on <{base}>"))
                continue
            stack.append((base, raw))
        elif base == "font":
            stack.append((base, raw))
        else:
            kind = ("voice/karaoke/lang/ruby/class"
                    if base in {"v", "k", "lang", "c", "ruby", "rt", "rp"}
                    else f"unknown tag <{name}>")
            diags.append(_unsupported(cue, raw, kind))

    for name, raw in stack:
        diags.append(
            Diagnostic(
                code="unpaired_tag",
                severity=Severity.ERROR,
                message=f"cue #{cue.index + 1}: opening tag {raw} was never closed",
                cue_index=cue.index,
                detail={"tag": raw},
            )
        )
    return diags


def _unsupported(cue: Cue, raw: str, what: str) -> Diagnostic:
    return Diagnostic(
        code="unsupported_markup",
        severity=Severity.ERROR,
        message=f"cue #{cue.index + 1}: {what} {raw} is outside the supported subset",
        cue_index=cue.index,
        detail={"tag": raw},
    )
