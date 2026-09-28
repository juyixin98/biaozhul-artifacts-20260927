import pytest

from app.normalizer import normalize, normalize_word, redact_text


def test_identity_passthrough_ascii_and_cjk():
    n = normalize("abc 研究")
    assert n.text == "abc 研究"
    assert n.char_map == (0, 1, 2, 3, 4, 5)
    assert n.deleted_raw_indices == ()


def test_fullwidth_ascii_folds_one_to_one():
    n = normalize("ｓｔｒａｓｓｅ")  # full-width lowercase Latin letters
    assert n.text == "strasse"
    # 1->1 mapping: each output char points at the same raw index
    assert n.char_map == tuple(range(7))
    assert n.deleted_raw_indices == ()
    # full-width folding preserves case (uppercase stays uppercase)
    assert normalize("Ｓ").text == "S"


def test_variable_length_expansion_eszett():
    # ß -> ss is a 1->2 rule: one raw character produces two output chars.
    n = normalize("aßb")
    assert n.text == "assb"
    assert n.char_map == (0, 1, 1, 2)
    # raw span of the expansion covers exactly the one raw character
    assert n.raw_span(1, 3) == (1, 2)


def test_fullwidth_then_expansion():
    n = normalize("ＳＴＲＡßＥ")
    assert n.text == "STRAssE"
    assert n.text[4:6] == "ss"
    assert n.char_map[4] == n.char_map[5] == 4  # both 's' from raw ß
    assert len(n.text) == 7


def test_soft_hyphen_is_deleted_but_recorded():
    n = normalize("研­究")
    assert n.text == "研究"
    assert n.deleted_raw_indices == (1,)
    # no output character maps onto raw index 1
    assert 1 not in n.char_map


def test_zero_width_space_deleted():
    n = normalize("a\u200bb")
    assert n.text == "ab"
    assert n.deleted_raw_indices == (1,)


def test_ideographic_space_to_normal_space():
    n = normalize("a　b")
    assert n.text == "a b"
    assert n.char_map == (0, 1, 2)  # 1->1


def test_normalize_word_matches_full_normalize():
    assert normalize_word("ＣＡＦＥ") == "CAFE"
    assert normalize_word("ｓｔｒａｓｓｅ") == "strasse"


def test_empty_string():
    n = normalize("")
    assert n.text == ""
    assert n.char_map == ()
    assert n.deleted_raw_indices == ()


def test_redaction_never_contains_full_input():
    secret = "super_secret_sentence_12345"
    masked = redact_text(secret)
    assert secret not in masked
    assert "len=" in masked and "sha8=" in masked
    # short inputs fully masked
    assert redact_text("ab") == "**"
