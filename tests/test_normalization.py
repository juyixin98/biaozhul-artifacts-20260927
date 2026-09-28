"""Normalization/tokenization: transformations and failure classes."""
from __future__ import annotations

import pytest

from app.errors import EmptyQueryError, TooManyTokensError, UnsupportedCharacterError
from app.normalization import normalize_and_tokenize, normalize_char

ALPHA = frozenset("abcdefghijklmnopqrstuvwxyz0123456789'-")


def test_nfkc_fullwidth_and_ligature():
    # Full-width latin -> ascii, ﬁ ligature -> "fi".
    assert normalize_char("Ａ") == "a"
    assert normalize_char("ﬁ") == "fi"
    normalized, tokens = normalize_and_tokenize("Ｈello ﬁle", alphabet=ALPHA, max_tokens=8)
    assert normalized == "hello file"
    assert [t.text for t in tokens] == ["hello", "file"]


def test_case_folding_and_combining_mark_stripping():
    normalized, tokens = normalize_and_tokenize("CAFÉ", alphabet=ALPHA, max_tokens=8)
    assert normalized == "cafe"
    assert tokens[0].text == "cafe"


def test_offsets_point_into_normalized_text():
    _, tokens = normalize_and_tokenize("a  bb", alphabet=ALPHA, max_tokens=8)
    assert tokens[0].start == 0 and tokens[0].end == 1
    assert tokens[1].start == 3 and tokens[1].end == 5


@pytest.mark.parametrize("q", ["", "   ", None])
def test_empty_query_category(q):
    with pytest.raises(EmptyQueryError) as exc:
        normalize_and_tokenize(q, alphabet=ALPHA, max_tokens=8)
    assert exc.value.code == "empty_query"


def test_unsupported_character_category_and_position():
    with pytest.raises(UnsupportedCharacterError) as exc:
        normalize_and_tokenize("ab€c", alphabet=ALPHA, max_tokens=8)
    assert exc.value.code == "unsupported_character"
    assert exc.value.details["char"] == "€"
    assert exc.value.details["position"] == 2


def test_too_many_tokens_category():
    with pytest.raises(TooManyTokensError) as exc:
        normalize_and_tokenize("a b c d", alphabet=ALPHA, max_tokens=3)
    assert exc.value.code == "too_many_tokens"
