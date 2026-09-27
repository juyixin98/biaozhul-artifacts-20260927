"""文本规范模块。

职责:把原始文本切分为「保留行结束符的行」,并显式建模行结束符风格与
末尾换行状态。本模块不做任何隐式改写——split_lines 与 join_lines 严格互逆,
只有 "\\r\\n"、"\\n"、"\\r" 被视为行结束符(不像 str.splitlines 那样把
\\x0b、\\u2028 等也当作行边界,避免静默重构文本)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

TERMINATORS = ("\r\n", "\n", "\r")


def split_lines(text: str) -> list[str]:
    """把文本切分为保留行结束符的行。split_lines(join_lines(x)) == x。"""
    lines: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                lines.append(text[start : i + 2])
                i += 2
            else:
                lines.append(text[start : i + 1])
                i += 1
            start = i
        elif ch == "\n":
            lines.append(text[start : i + 1])
            i += 1
            start = i
        else:
            i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def join_lines(lines: list[str] | tuple[str, ...]) -> str:
    return "".join(lines)


def line_terminator(line: str) -> str:
    """返回该行的行结束符("\\r\\n"/"\\n"/"\\r"),没有则返回 ""。"""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def line_body(line: str) -> str:
    """返回去掉行结束符后的内容。"""
    return line[: len(line) - len(line_terminator(line))] if line_terminator(line) else line


def has_terminator(line: str) -> bool:
    return line_terminator(line) != ""


@dataclass(frozen=True)
class TextProfile:
    """一份文本的规范化画像。只含元数据,不含内容本身。"""

    line_ending: str  # "lf" | "crlf" | "cr" | "mixed" | "none"
    ends_with_newline: bool
    line_count: int
    sha256: str


def profile(text: str) -> TextProfile:
    lines = split_lines(text)
    terms = {line_terminator(l) for l in lines if has_terminator(l)}
    if not terms:
        ending = "none"
    elif terms == {"\n"}:
        ending = "lf"
    elif terms == {"\r\n"}:
        ending = "crlf"
    elif terms == {"\r"}:
        ending = "cr"
    else:
        ending = "mixed"
    return TextProfile(
        line_ending=ending,
        ends_with_newline=bool(lines) and has_terminator(lines[-1]),
        line_count=len(lines),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def dominant_terminator(text: str) -> str:
    """返回文本中最常见的行结束符,用于生成冲突标记行;无行结束符时用 "\\n"。"""
    counts = {"\r\n": 0, "\n": 0, "\r": 0}
    for line in split_lines(text):
        term = line_terminator(line)
        if term:
            counts[term] += 1
    best = max(counts, key=lambda t: counts[t])
    return best if counts[best] > 0 else "\n"
