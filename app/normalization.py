"""文本规范化（纯函数、可解释）。

刻意只做不依赖任何外部数据的确定性处理：
1. Unicode NFKC 兼容分解/组合（全角字母数字 → 半角等）；
2. 大小写折叠（str.casefold，比 lower 更稳）；
3. 折叠两端及连续空白。

每一步都记录到 steps 中，供诊断展示“处理位置”。空结果单独标记。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizationStep:
    stage: str
    detail: str
    before: str
    after: str

    def to_dict(self) -> dict:
        return {"stage": self.stage, "detail": self.detail, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class NormalizationResult:
    original: str
    normalized: str
    steps: tuple[NormalizationStep, ...]
    empty: bool


def normalize(text: str) -> NormalizationResult:
    original = text
    steps: list[NormalizationStep] = []

    nfkc = unicodedata.normalize("NFKC", text)
    if nfkc != text:
        steps.append(
            NormalizationStep("nfkc", "Unicode NFKC 兼容规范化", text, nfkc)
        )
    text = nfkc

    folded = text.casefold()
    if folded != text:
        steps.append(
            NormalizationStep("casefold", "大小写折叠", text, folded)
        )
    text = folded

    stripped = text.strip()
    if stripped != text:
        steps.append(
            NormalizationStep("strip", "去除两端空白", text, stripped)
        )
    text = stripped

    collapsed = " ".join(text.split())
    if collapsed != text:
        steps.append(
            NormalizationStep("collapse_ws", "折叠连续空白", text, collapsed)
        )

    return NormalizationResult(
        original=original,
        normalized=collapsed,
        steps=tuple(steps),
        empty=(collapsed == ""),
    )
