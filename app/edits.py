"""区间编辑模块:由共同基线生成半开区间编辑。

编辑表示为 Edit(start, end, replacement):把 base_lines[start:end] 替换为
replacement(保留行结束符的行)。start == end 表示在该位置插入;
replacement 为空表示删除。

比较键的规则(见 docs/semantics.md):
- 行内容(body)相同即视为同一行,行结束符口味(\\n vs \\r\\n)不参与比较,
  因此「只把 LF 改成 CRLF」不会产生编辑,也就不会被静默合并进去;
  调用方会收到一条诊断说明。
- 例外:没有行结束符的最后一行,其比较键带 EOF 哨兵。这样
  「末尾无换行 -> 有换行」会被识别为一次真实编辑,否则在末尾追加行时
  会把未终止的最后一行和新行粘在一起,造成静默改写。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .textnorm import has_terminator, line_body

EOF_SENTINEL = "\x00eof"


@dataclass(frozen=True)
class Edit:
    start: int  # 基线行号,半开区间起点
    end: int  # 基线行号,半开区间终点(不含)
    replacement: tuple[str, ...]  # 替换内容,保留行结束符

    @property
    def is_insert(self) -> bool:
        return self.start == self.end

    @property
    def is_delete(self) -> bool:
        return not self.is_insert and len(self.replacement) == 0


def diff_keys(lines: list[str] | tuple[str, ...]) -> list[str]:
    """每行的比较键。未终止的行(只可能是最后一行)带 EOF 哨兵。"""
    keys = []
    for line in lines:
        body = line_body(line)
        keys.append(body if has_terminator(line) else body + EOF_SENTINEL)
    return keys


def compute_edits(
    base_lines: list[str] | tuple[str, ...],
    other_lines: list[str] | tuple[str, ...],
) -> list[Edit]:
    """计算把 base_lines 变成 other_lines 所需的区间编辑,按基线位置升序。"""
    matcher = difflib.SequenceMatcher(
        a=diff_keys(base_lines), b=diff_keys(other_lines), autojunk=False
    )
    edits: list[Edit] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        edits.append(Edit(i1, i2, tuple(other_lines[j1:j2])))
    return edits
