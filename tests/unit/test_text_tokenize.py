"""文本分词测试：具体 token 断言，而非“能调用”。"""
from __future__ import annotations

import pytest

from app.text.tokenize import tokenize, unique_terms


def test_tokenize_lowercases_english_and_keeps_cjk():
    assert tokenize("Hello, 世界! Fast-Index") == ["hello", "世界", "fast", "index"]


def test_tokenize_numbers_underscores():
    assert tokenize("doc_42 v2") == ["doc_42", "v2"]


def test_tokenize_punctuation_only_is_empty():
    assert tokenize("--- !!! ...") == []


def test_unique_terms_preserves_first_occurrence_order():
    assert unique_terms("a b a c b") == ["a", "b", "c"]


def test_tokenize_rejects_non_string():
    with pytest.raises(TypeError):
        tokenize(123)
