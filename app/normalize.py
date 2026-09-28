"""文本规范化层。

规范化版本一旦发布即 **固定**（参见 :data:`NORMALIZER_VERSION`）：
同一个词在任何时间、任何进程里规范化结果必须一致。索引键使用规范化
结果，而展示值始终保留用户写入的原文（display text preserved）。

v1 管线（Unicode 15.0 语义）::

    原文 -> unicodedata.normalize("NFKC", s) -> str.casefold()

设计要点：

- NFKC 是兼容性分解 + 规范组合，会把全角字符、兼容等价形式映射到规范形式
  （例如 ``"ＣＡＦＥ" -> "cafe"``、``"①" -> "1"``、连字 ``"ﬁ" -> "fi"``），
  因而不同原文可能规范化到同一个键 —— 这就是“规范化碰撞”，索引层允许
  同一个键上挂多条原文不同的词条。
- ``casefold`` 比 ``lower`` 更激进（例如德语 ``"Straße" -> "strasse"``），
  用于大小写不敏感前缀匹配。
- 规范化结果仅作为**索引键与稳定排序键**，永远不覆盖原文。
"""

from __future__ import annotations

import unicodedata

#: 当前规范化管线版本。修改管线必须新建版本号，禁止原地改写已发布版本。
NORMALIZER_VERSION = "norm-v1"

SUPPORTED_VERSIONS = frozenset({NORMALIZER_VERSION})


def normalize_text(text: str, version: str = NORMALIZER_VERSION) -> str:
    """把原文规范化为索引键。

    :param text: 原始词条文本。
    :param version: 规范化版本，必须是受支持的固定版本。
    :returns: 规范化字符串。
    :raises ValueError: 版本未知（旧版本数据不能用新管线悄悄重解释）。
    """
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"不支持的规范化版本: {version!r}，支持: {sorted(SUPPORTED_VERSIONS)}")
    # v1: NFKC -> casefold。两者都是幂等的，重复调用结果不变。
    return unicodedata.normalize("NFKC", text).casefold()


def canonical_key(entry_id: str, term: str, display: str) -> tuple[str, str, str]:
    """同分决胜用的稳定规范键。

    排序三元组为 ``(规范化键, 原文, 词条ID)``：

    1. 规范化键：让 ``"ＣＡＦＥ"`` 与 ``"cafe"`` 同分时有确定相对次序；
    2. 原文：规范化键相同（规范化碰撞）时按输入原文 Unicode 码位序；
    3. 词条ID：原文也相同时（重复写入）仍保证全序、结果稳定。
    """
    return (term, display, entry_id)
