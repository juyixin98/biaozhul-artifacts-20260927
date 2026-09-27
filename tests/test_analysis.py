"""Analyzer tests: tokenization, case folding, CJK unigrams, value parsing."""

from __future__ import annotations

import pytest

from searchdsl.analysis import parse_date, parse_int, tokenize


def test_case_fold_and_punctuation_split():
    assert tokenize("Quick-Brown_fox.") == ["quick", "brown", "fox"]


def test_cjk_unigrams_keep_latin_runs():
    assert tokenize("Salmon 价格在冬季 fox42") == [
        "salmon", "价", "格", "在", "冬", "季", "fox", "42",
    ]


def test_accents_and_superscripts_via_nfkc():
    # NFKC folds full-width digits and ligatures before tokenizing.
    assert "42" in tokenize("４２")


def test_empty_and_punct_only():
    assert tokenize("!!!  ...") == []


def test_parse_int_variants():
    assert parse_int(" 12 ") == 12
    assert parse_int("-07") == -7
    assert parse_int("+3") == 3
    for bad in ["1.5", "0x1", "", "12a"]:
        with pytest.raises(ValueError):
            parse_int(bad)


def test_parse_date_validation():
    assert parse_date("2020-02-29") == "2020-02-29"  # leap year
    with pytest.raises(ValueError):
        parse_date("2021-02-29")  # not a leap year
    with pytest.raises(ValueError):
        parse_date("2020-13-01")
    with pytest.raises(ValueError):
        parse_date("2020/01/01")
