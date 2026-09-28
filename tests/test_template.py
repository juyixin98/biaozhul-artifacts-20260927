"""Tests for restricted capture templates: parsing, unknown/missing captures."""

from __future__ import annotations

import pytest

from app.engine import compile_pattern
from app.errors import (
    CaptureMissingError,
    InvalidTemplateError,
    UnknownCaptureError,
)
from app.template import parse_template, render


def compiled(pattern=r"(?P<word>[a-z]+)([0-9]+)?"):
    return compile_pattern(pattern)


def test_literal_dollar_forms():
    toks = parse_template("price: $$5 ${word}", compiled())
    c = _capture(compiled(), b"abc", b"abcdef", b"def")
    out = render(toks, c).output
    assert out == b"price: $5 abcdef"


def test_numeric_and_named_refs_and_whole_match():
    cp = compiled()
    c = _capture(cp, b"abc12", b"abc", b"12")
    assert render(parse_template("$0", cp), c).output == b"abc12"
    assert render(parse_template("$1-$2", cp), c).output == b"abc-12"
    assert render(parse_template("${word}/${2}", cp), c).output == b"abc/12"


def test_double_digit_braced_ref():
    # Build a pattern with 10 groups; reference ${10}.
    pat = r"(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)"
    cp = compile_pattern(pat)
    m = _capture(cp, b"abcdefghij", *[ch.encode() for ch in "abcdefghij"])
    assert render(parse_template("${10}", cp), m).output == b"j"


def test_unknown_numeric_group_is_static_error():
    cp = compiled()  # 2 groups
    with pytest.raises(UnknownCaptureError) as exc:
        parse_template("$9", cp)
    assert exc.value.code == "unknown_capture"
    assert exc.value.details["known_groups"] == 2


def test_unknown_named_group_is_static_error():
    cp = compiled()
    with pytest.raises(UnknownCaptureError) as exc:
        parse_template("${nope}", cp)
    assert exc.value.code == "unknown_capture"
    assert exc.value.details["known_names"] == ["word"]


def test_unclosed_brace_and_empty_braces():
    cp = compiled()
    with pytest.raises(InvalidTemplateError) as e1:
        parse_template("${word", cp)
    assert e1.value.code == "invalid_template"
    with pytest.raises(InvalidTemplateError) as e2:
        parse_template("${}", cp)
    assert e2.value.code == "invalid_template"


def test_dollar_followed_by_text_is_literal():
    cp = compiled()
    c = _capture(cp, b"abc", b"abc", None)
    assert render(parse_template("$x$1", cp), c).output == b"$xabc"


def test_missing_optional_capture_fails_by_default(run_logger, request):
    cp = compiled()
    # group 2 (digits) did not participate in a match of just 'abc'
    c = _capture(cp, b"abc", b"abc", None)
    with pytest.raises(CaptureMissingError) as exc:
        render(parse_template("${word}[$2]", cp), c)
    assert exc.value.code == "capture_missing"
    assert exc.value.details["group"] == 2
    run_logger.check(
        request.node.nodeid,
        "missing optional capture rejected",
        expected="capture_missing",
        actual=exc.value.code,
        passed=True,
        reason="group 2 is optional and absent; default policy fails loudly",
        intermediate={"groups": [None, b"abc", None]},
    )


def test_missing_optional_capture_lenient_policy_records_and_empties():
    cp = compiled()
    c = _capture(cp, b"abc", b"abc", None)
    res = render(parse_template("${word}[$2]", cp), c, missing_capture="empty")
    assert res.output == b"abc[]"
    assert res.missing == (2,)


def test_backreference_style_construct_is_not_supported_by_engine():
    # A pattern with a raw numeric backref never compiles on RE2; template
    # substitution is capture-only, not regex re-evaluation.
    from app.errors import InvalidPatternError, UnsupportedSyntaxError

    with pytest.raises((InvalidPatternError, UnsupportedSyntaxError)):
        compile_pattern(r"(a)\1")


def _capture(cp, whole: bytes, *groups: bytes | None):
    """Build an engine Candidate shape without scanning (direct group values)."""
    from app.engine.scanner import Candidate, GroupSpan

    spans = [GroupSpan(0, 0, len(whole), whole)]
    pos = 0
    for i, g in enumerate(groups, start=1):
        if g is None:
            spans.append(GroupSpan(i, None, None, None))
        else:
            # locate group bytes inside whole for realistic offsets
            idx = whole.find(g, pos)
            spans.append(GroupSpan(i, idx, idx + len(g), g))
            pos = idx + len(g)
    return Candidate(start=0, end=len(whole), groups=tuple(spans))
