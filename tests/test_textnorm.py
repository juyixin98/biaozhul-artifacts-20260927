"""文本规范模块测试:切分/拼接互逆、行结束符与末尾换行不被改写。"""

from app.textnorm import (
    dominant_terminator,
    join_lines,
    line_body,
    line_terminator,
    profile,
    split_lines,
)


def test_split_join_roundtrip_lf():
    text = "a\nb\nc\n"
    assert split_lines(text) == ["a\n", "b\n", "c\n"]
    assert join_lines(split_lines(text)) == text


def test_split_join_roundtrip_no_trailing_newline():
    text = "a\nb"
    assert split_lines(text) == ["a\n", "b"]
    assert join_lines(split_lines(text)) == text


def test_split_join_roundtrip_crlf_and_cr():
    for text in ("a\r\nb\r\n", "a\rb\r", "a\r\nb"):
        assert join_lines(split_lines(text)) == text


def test_mixed_terminators_preserved():
    text = "a\nb\r\nc\rd"
    lines = split_lines(text)
    assert [line_terminator(l) for l in lines] == ["\n", "\r\n", "\r", ""]
    assert join_lines(lines) == text


def test_empty_text():
    assert split_lines("") == []
    assert join_lines([]) == ""


def test_unicode_line_boundaries_not_split():
    # \x0b、  不是本系统的行结束符,不得被当作行边界静默重构
    text = "a\x0bb c\n"
    assert split_lines(text) == [text]


def test_line_body_and_terminator():
    assert line_body("abc\r\n") == "abc"
    assert line_body("abc") == "abc"
    assert line_terminator("abc\n") == "\n"
    assert line_terminator("abc") == ""


def test_profile_classification():
    assert profile("a\nb\n").line_ending == "lf"
    assert profile("a\r\nb\r\n").line_ending == "crlf"
    assert profile("a\rb\r").line_ending == "cr"
    assert profile("a\nb\r\n").line_ending == "mixed"
    assert profile("abc").line_ending == "none"
    assert profile("a\nb").ends_with_newline is False
    assert profile("a\nb\n").ends_with_newline is True
    assert profile("a\nb\n").line_count == 2


def test_dominant_terminator():
    assert dominant_terminator("a\r\nb\r\nc\n") == "\r\n"
    assert dominant_terminator("no newlines") == "\n"
