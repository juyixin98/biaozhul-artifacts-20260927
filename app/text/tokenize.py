"""文本规范化与分词。

文档正文与查询 term 使用同一套 token 规则：
- 连续的字母/数字/下划线（\\w+，含中文等 unicode 字母数字）算一个 token；
- 统一小写，保证大小写不敏感；
- 其余字符（标点、空白）一律作为分隔符。

不使用任何停用词表，保持可预测、可复现。
"""
from __future__ import annotations

import re

# \w 在 re.UNICODE（默认）下包含中文、拉丁字母、数字、下划线。
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """把任意文本切成有序的小写 token 列表（保留重复，交给索引层去重）。"""
    if not isinstance(text, str):
        raise TypeError(f"tokenize 需要 str，收到 {type(text).__name__}")
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


def unique_terms(text: str) -> list[str]:
    """文档索引用：去重且保持稳定（按首次出现顺序）的 term 列表。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in tokenize(text):
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered
