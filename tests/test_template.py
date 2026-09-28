"""受限捕获模板测试：先验证、后渲染；缺失组严格/宽松两态。"""
from __future__ import annotations

import pytest

from app import engine
from app.errors import CaptureUnavailableError, InvalidTemplateError
from app.template import parse_template, render


def _compiled(pattern):
    return engine.compile_pattern(pattern, "")


def test_named_and_numeric_refs_render(record):
    cp = _compiled(r"(?P<user>\w+)@(?P<host>\w+)")
    parsed = parse_template(r"<\g<user>|\\2=\2\\\\>", frozenset({"user", "host"}), 2)
    hit = engine.scan_nonoverlapping(cp, "alice@example")[0]
    out = render(parsed, hit)
    record.state("rendered", out)
    # 模板内 \\ -> 单个反斜杠，\\2 是字面 "\2"，末尾 \\\\ -> 两个反斜杠
    assert out == r"<alice|\2=example\\>"


def test_undefined_named_ref_rejected_before_matching(record):
    cp = _compiled(r"(?P<a>x)")
    with pytest.raises(InvalidTemplateError) as ei:
        parse_template(r"\g<missing>", frozenset({"a"}), 1)
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "INPUT_INVALID_TEMPLATE"
    assert ei.value.details["name"] == "missing"


def test_undefined_numeric_ref_rejected(record):
    with pytest.raises(InvalidTemplateError) as ei:
        parse_template(r"\3", frozenset(), 2)
    assert ei.value.details["ref"] == "3"
    assert ei.value.details["group_count"] == 2


def test_dangling_backslash_and_bad_escape_rejected(record):
    with pytest.raises(InvalidTemplateError) as ei:
        parse_template("abc\\", frozenset(), 0)
    assert ei.value.details["position"] == 3
    with pytest.raises(InvalidTemplateError):
        parse_template(r"\n", frozenset(), 0)  # \n 不是规格内转义


def test_optional_group_missing_strict_vs_lenient(record):
    cp = _compiled(r"(?P<a>a)?b")
    hit = engine.scan_nonoverlapping(cp, "b")[0]
    parsed = parse_template(r"[\g<a>]", frozenset({"a"}), 1)
    with pytest.raises(CaptureUnavailableError) as ei:
        render(parsed, hit, strict_captures=True)
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "COMPUTE_CAPTURE_UNAVAILABLE"
    assert ei.value.details["char_span"] == [0, 1]
    # 宽松模式：缺失渲染为空串
    assert render(parsed, hit, strict_captures=False) == "[]"


def test_g_numeric_form(record):
    cp = _compiled(r"(x)(y)")
    parsed = parse_template(r"\g<2>-\g<1>", frozenset(), 2)
    hit = engine.scan_nonoverlapping(cp, "xy")[0]
    assert render(parsed, hit) == "y-x"


def test_g_zero_is_refused(record):
    # 整串引用 \g<0>/\0 明确不在受限语法内
    with pytest.raises(InvalidTemplateError) as ei:
        parse_template(r"\g<0>", frozenset(), 0)
    assert ei.value.code == "INPUT_INVALID_TEMPLATE"
    with pytest.raises(InvalidTemplateError):
        parse_template(r"\0", frozenset(), 0)
