"""规范化层测试：版本固定、Unicode 行为、显示原文保留。

这些期望值是手写的具体结果（不是调用被测实现生成的），失败类别用具体断言。
"""
from __future__ import annotations

import pytest

from app.normalizer import NORMALIZER_VERSION, normalize


def test_normalizer_version_is_pinned(log):
    log("GIVEN", "GIVEN", rule="固定的规范化版本常量")
    assert NORMALIZER_VERSION == "norm-1.0.0", "规范化版本被改动：存储兼容性与快照语义会失效"
    log("PASS", "PASS", expected="norm-1.0.0", actual=NORMALIZER_VERSION)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ABC", "abc"),
        ("ＡＢＣ", "abc"),          # 全角拉丁 -> NFKC -> 半角
        ("ＭＵＬＴＩcast", "multicast"),
        ("Straße", "strasse"),       # casefold: ß -> ss
        ("STRASSE", "strasse"),
        ("  Hello   World  ", "hello world"),  # 空白折叠
        ("　CAFÉ　", "café"),  # 表意空格 + 带音符
        ("Ｃａｆé", "café"),
        ("ﬁle", "file"),  # U+FB01 fi 连字经 NFKC 兼容分解为 fi
        ("①②③", "123"),  # 带圈数字 NFKC -> ASCII 数字
    ],
)
def test_normalize_exact_values(raw, expected, log):
    got = normalize(raw)
    log("THEN", "GIVEN", input=raw, expected=expected, actual=got)
    assert got == expected, f"规范化碰撞预期失败：{raw!r} -> {got!r}，预期 {expected!r}"
    log("PASS", "PASS", input=raw, expected=expected)


def test_blank_becomes_empty_key_but_is_rejected_upstream(log):
    # 规范化层只负责产出键；空键由 engine 拒绝，两层职责分明。
    assert normalize("   　 ") == ""
    log("PASS", "PASS", case="空白输入规范化为空串，engine 必须以 E_INVALID_SURFACE 拒绝")


def test_normalize_is_deterministic(log):
    for _ in range(100):
        assert normalize("ＭＵＬＴＩcast") == "multicast"
    log("PASS", "PASS", iterations=100)
