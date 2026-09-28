"""独立参考实现 + 全量筛选对照。

这个模块是测试预言（test oracle），**独立于被测核心**编写：

- :func:`ref_normalize` 直接用标准库 ``unicodedata`` 重新实现规范化管线，
  不 import 被测的 ``app.normalize``；另用硬编码字面值（``HARDCODED``）
  锁定 Unicode 15.0 下的具体结果 —— 预期值不是由被测代码生成的；
- :class:`ReferenceStore` 是一个朴素字典存储，:meth:`ref_top_k` 对全量
  词条做筛选 + 排序（O(N) 遍历再 sort），作为“精确 top-k”的朴素答案；
- 所有 Trie 查询都与该朴素答案逐项比对。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


def ref_normalize(s: str) -> str:
    """参考规范化：独立重写 v1 管线（NFKC 后 casefold）。"""
    return unicodedata.normalize("NFKC", s).casefold()


#: 硬编码预期（手工依据 Unicode 15.0 NFKC/casefold 语义确定），
#: 若标准库 Unicode 数据版本变化导致失败，属于真实的环境不一致，必须暴露。
HARDCODED = [
    # (原文, 规范化键, 说明)
    ("Hello", "hello", "ASCII 大小写折叠"),
    ("ＣＡＦＥ", "cafe", "全角拉丁 -> 半角 (NFKC) 再小写"),
    ("Straße", "strasse", "德语 sharp s 的 casefold 展开为 ss"),
    ("ﬁle", "file", "LATIN SMALL LIGATURE FI 经 NFKC 分解为 fi"),
    ("①", "1", "带圈数字 1 的 NFKC 兼容映射"),
    ("Ｈｅｌｌｏ", "hello", "全角 ASCII 规范化碰撞到 hello"),
    ("カフェ", "カフェ", "片假名 NFKC 后保持（已是规范形式）"),
    ("Ω", "ω", "希腊大写欧米伽 casefold"),
    ("Å", "å", "Angstrom/NFC A-ring 的 NFKC+casefold"),
    ("ABC123", "abc123", "字母数字混合"),
    ("İ", "i̇", "土耳其 İ casefold 为 i + combining dot above (U+0307)"),
]

#: 规范化碰撞组：不同原文 -> 同一规范键
COLLISION_GROUPS = [
    ["ＣＡＦＥ", "CAFE", "cafe"],          # -> cafe
    ["ﬁle", "FILE", "File"],               # -> file
    ["Ｈｅｌｌｏ", "HELLO", "hello"],      # -> hello
]


@dataclass
class RefEntry:
    id: str
    display: str
    term_norm: str
    score: float


class ReferenceStore:
    """朴素内存词典；查询时全量筛选再排序。"""

    def __init__(self) -> None:
        self.entries: dict[str, RefEntry] = {}

    def upsert(self, entry_id: str, display: str, score: float) -> None:
        self.entries[entry_id] = RefEntry(entry_id, display, ref_normalize(display), float(score))

    def delete(self, entry_id: str) -> bool:
        if entry_id in self.entries:
            del self.entries[entry_id]
            return True
        return False

    def ref_top_k(
        self, prefix_raw: str, k: int, version_entries: dict[str, RefEntry] | None = None
    ) -> list[tuple[str, str, str, float]]:
        """全量筛选 + 排序的朴素精确答案。

        返回 ``[(term_norm, display, id, score), ...]``，
        排序：score 降序；同分 (term_norm, display, id) 升序。
        """
        pool = self.entries if version_entries is None else version_entries
        pfx = ref_normalize(prefix_raw)
        hits = [
            (e.term_norm, e.display, e.id, e.score)
            for e in pool.values()
            if e.term_norm.startswith(pfx)
        ]
        hits.sort(key=lambda r: (-r[3], r[0], r[1], r[2]))
        return hits[:k]

    def all_term_norms(self) -> list[str]:
        return sorted({e.term_norm for e in self.entries.values()})
