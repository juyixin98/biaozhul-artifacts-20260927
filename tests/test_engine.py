"""引擎层测试：零宽前进、相邻匹配、线性安全、不支持特性的分类拒绝。"""
from __future__ import annotations

import pytest

from app import engine
from app.errors import (
    InvalidFlagError,
    RegexCompileError,
    RegexProgramTooLargeError,
)


def test_bump_along_empty_pattern_explicit_positions(record):
    p = engine.compile_pattern("", "")
    hits = engine.scan_nonoverlapping(p, "ab")
    spans = [(h.start, h.end) for h in hits]
    record.state("empty_pattern_spans", spans, "空模式应在 0,1,2 各命中一次（含串尾）")
    record.check("span list equals [(0,0),(1,1),(2,2)]", ok=spans == [(0, 0), (1, 1), (2, 2)],
                 expected=[(0, 0), (1, 1), (2, 2)], actual=spans)
    assert spans == [(0, 0), (1, 1), (2, 2)]
    assert all(h.is_zero_width for h in hits)


def test_bump_along_adjacent_star_matches(record):
    # a* on aab -> [0,2) 然后零宽 [2,2),[3,3)；不重复吃
    p = engine.compile_pattern("a*", "")
    spans = [(h.start, h.end) for h in engine.scan_nonoverlapping(p, "aab")]
    record.state("star_spans", spans, "消耗后从 end 继续；零宽前进一码点")
    assert spans == [(0, 2), (2, 2), (3, 3)]


def test_zero_width_advance_is_one_codepoint_multibyte(record):
    p = engine.compile_pattern("", "")
    spans = [(h.start, h.end) for h in engine.scan_nonoverlapping(p, "你好")]
    record.state("mb_empty_spans", spans, "多字节文本前进单位是码点而非字节")
    assert spans == [(0, 0), (1, 1), (2, 2)]


def test_adjacent_literal_matches_are_both_kept(record):
    p = engine.compile_pattern("cat|dog", "")
    spans = [(h.start, h.end) for h in engine.scan_nonoverlapping(p, "catdogcat")]
    record.state("adjacent_spans", spans, "cat 与 dog 首尾相接，互不重叠，应各自保留")
    assert spans == [(0, 3), (3, 6), (6, 9)]


def test_groups_and_optional_unmatched(record):
    p = engine.compile_pattern(r"(?P<a>a)|(?P<b>b)", "")
    hits = engine.scan_nonoverlapping(p, "b")
    g = hits[0].groups
    record.state("alternation_groups", [(x.name, x.text) for x in g],
                 "备选分支未参与的组应为 None")
    assert len(g) == 2
    assert g[0].name == "a" and g[0].text is None
    assert g[1].name == "b" and g[1].text == "b"
    assert g[0].char_start == -1 and g[0].char_end == -1


def test_backreference_rejected_at_compile(record):
    with pytest.raises(RegexCompileError) as ei:
        engine.compile_pattern(r"(\w)\1", "")
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "COMPUTE_REGEX_COMPILE"
    assert "backtrack" not in ei.value.details["reason"]  # 是“不支持”而非超时


@pytest.mark.parametrize("pat", [r"(?=x)", r"(?!x)", r"(?<=x)y", r"(?P<n>a)(?P=n)"])
def test_lookaround_rejected(record, pat):
    with pytest.raises(RegexCompileError) as ei:
        engine.compile_pattern(pat, "")
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.category == "compute"


def test_bad_syntax_rejected_with_reason(record):
    with pytest.raises(RegexCompileError) as ei:
        engine.compile_pattern("[[", "")
    assert ei.value.details["reason"]
    record.state("compile_reason", ei.value.details["reason"])


def test_unknown_flag_is_input_error(record):
    with pytest.raises(InvalidFlagError) as ei:
        engine.compile_pattern("x", "x")
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "INPUT_INVALID_FLAG"
    assert set(ei.value.details["supported"]) == {"i", "s", "m"}


def test_regex_program_budget_exhaustion_is_distinct(record, monkeypatch):
    # 极小预算下合法语法也编译不过 -> 专门的“程序过大”类别，区别于语法错误
    from app import config
    monkeypatch.setattr(engine, "LIMITS", config.Limits(regex_mem_budget=256))
    with pytest.raises(RegexProgramTooLargeError) as ei:
        engine.compile_pattern("(?:a|b|c|d|e|f|g|h)" * 40, "")
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "COMPUTE_REGEX_PROGRAM_TOO_LARGE"
    assert ei.value.details["budget_bytes"] == 256
