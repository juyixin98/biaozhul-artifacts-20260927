"""文本规范化测试（断言具体结果与每一步）。"""
from __future__ import annotations

from app.normalization import normalize


def test_casefold_and_nfkc():
    result = normalize("  ＨｅＬｌｏ　WORLD  ")
    assert result.normalized == "hello world"
    stages = [s.stage for s in result.steps]
    assert "nfkc" in stages
    assert "casefold" in stages
    assert "strip" in stages or "collapse_ws" in stages
    assert not result.empty


def test_empty_after_normalization():
    result = normalize("   \t\n ")
    assert result.normalized == ""
    assert result.empty


def test_no_op_normalization():
    result = normalize("abc")
    assert result.normalized == "abc"
    assert result.steps == ()
    assert not result.empty


def test_collapse_internal_whitespace():
    result = normalize("a\t\tb\nc")
    assert result.normalized == "a b c"
    assert any(s.stage == "collapse_ws" for s in result.steps)
