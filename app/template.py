"""Restricted replacement templates.

Grammar (intentionally tiny -- no eval, no nested expansion, no format spec)::

    template  := token*
    token     := '$$'                      -> literal '$'
               | '$' DIGIT                 -> group by 1-based number ($0 = whole match)
               | '${' NAME '}'             -> group by Python-identifier name
               | '${' DIGITS '}'           -> group by number (>= 10 supported)
               | any other byte            -> literal verbatim
    NAME      := [A-Za-z_][A-Za-z0-9_]*
    DIGIT     := '0'..'9'
    DIGITS    := [0-9]+

Anything else is a *parse* failure:
* a dangling ``${`` (no closing brace), empty braces, or a non-identifier
  brace body -> :class:`~app.errors.InvalidTemplateError`;
* ``$`` followed by a non-digit, non-``$``, non-``{`` character is **not**
  magic: it is emitted literally (shell-style).  This keeps templates over
  prose readable; use ``$$`` only for a literal dollar before digits/braces;
* ``$1`` where the pattern has no group 1, or ``${x}`` where no group is named
  ``x`` -> :class:`~app.errors.UnknownCaptureError` (checked against the
  compiled pattern's static group table *before* any text is scanned).

Rendering a parsed template against a concrete match raises
:class:`~app.errors.CaptureMissingError` only when a referenced *existing*
optional group did not participate in that match (and the rule did not opt in
to empty substitution).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidTemplateError, UnknownCaptureError, CaptureMissingError
from .engine.compiler import CompiledPattern
from .engine.scanner import Candidate


@dataclass(frozen=True, slots=True)
class Literal:
    data: bytes


@dataclass(frozen=True, slots=True)
class GroupRef:
    number: int
    label: str  # original spelling for diagnostics


Token = Literal | GroupRef


def parse_template(template: str | bytes, compiled: CompiledPattern) -> tuple[Token, ...]:
    """Parse and statically validate a template against a compiled pattern."""
    raw = template.encode("utf-8") if isinstance(template, str) else bytes(template)
    tokens: list[Token] = []
    buf = bytearray()

    def flush() -> None:
        if buf:
            tokens.append(Literal(bytes(buf)))
            buf.clear()

    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c != 0x24:  # '$'
            buf.append(c)
            i += 1
            continue

        # '$' forms
        if i + 1 >= n:
            # trailing '$' is a literal
            buf.append(c)
            i += 1
            continue

        nxt = raw[i + 1]
        if nxt == 0x24:  # '$$'
            buf.append(0x24)
            i += 2
            continue
        if nxt == 0x7B:  # '${'
            close = raw.find(b"}", i + 2)
            if close == -1:
                raise InvalidTemplateError(
                    "template has '${' without closing '}'",
                    at=i,
                )
            body = raw[i + 2:close]
            if not body:
                raise InvalidTemplateError("template contains empty '${}'", at=i)
            label = _decode_body(body, i)
            number = _resolve_ref(label, compiled, i)
            flush()
            tokens.append(GroupRef(number=number, label=label))
            i = close + 1
            continue
        if 0x30 <= nxt <= 0x39:  # '$' digit
            label = chr(nxt)
            number = _resolve_ref(label, compiled, i)
            flush()
            tokens.append(GroupRef(number=number, label=label))
            i += 2
            continue

        # '$' followed by anything else: literal '$'
        buf.append(c)
        i += 1

    flush()
    return tuple(tokens)


def _decode_body(body: bytes, at: int) -> str:
    try:
        label = body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InvalidTemplateError(
            "group reference must be ASCII (number or Python identifier)",
            at=at,
        ) from exc
    if label[0].isdigit():
        if not label.isdigit():
            raise InvalidTemplateError(
                "numeric group reference must contain only digits",
                ref=label,
                at=at,
            )
        return label
    if not _is_identifier(label):
        raise InvalidTemplateError(
            "named group reference is not a valid Python identifier",
            ref=label,
            at=at,
        )
    return label


def _is_identifier(label: str) -> bool:
    if not label or not (label[0].isalpha() or label[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in label)


def _resolve_ref(label: str, compiled: CompiledPattern, at: int) -> int:
    if label.isdigit():
        number = int(label)
    else:
        number = compiled.group_number(label)  # type: ignore[arg-type]
        if number is None:
            raise UnknownCaptureError(
                f"template references undefined named group {label!r}",
                ref=label,
                known_names=sorted(compiled.group_names_str()),
                at=at,
            )
        return number
    if not (0 <= number <= compiled.ngroups):
        raise UnknownCaptureError(
            f"template references group {number}, pattern has "
            f"{compiled.ngroups} group(s)",
            ref=label,
            known_groups=compiled.ngroups,
            at=at,
        )
    return number


@dataclass(frozen=True, slots=True)
class RenderResult:
    output: bytes
    missing: tuple[int, ...]
    """Group numbers referenced but unmatched (empty tuple on full success)."""


def render(
    tokens: tuple[Token, ...],
    candidate: Candidate,
    *,
    missing_capture: str = "error",
) -> RenderResult:
    """Render parsed tokens against one match.

    ``missing_capture``:
      * ``"error"`` (default) -> :class:`CaptureMissingError` if an optional
        referenced group did not participate;
      * ``"empty"`` -> substitute ``b""`` for it and record in ``missing``.
    """
    if missing_capture not in ("error", "empty"):
        raise ValueError(f"invalid missing_capture policy: {missing_capture!r}")

    out = bytearray()
    missing: list[int] = []
    for tok in tokens:
        if isinstance(tok, Literal):
            out += tok.data
            continue
        g = candidate.group(tok.number)
        if g.value is None:
            missing.append(tok.number)
            if missing_capture == "error":
                raise CaptureMissingError(
                    f"capture group {tok.number} (${tok.label}) did not "
                    "participate in this match",
                    group=tok.number,
                    label=tok.label,
                    start=candidate.start,
                    end=candidate.end,
                )
            # empty substitution; append nothing
        else:
            out += g.value
    return RenderResult(bytes(out), tuple(missing))
