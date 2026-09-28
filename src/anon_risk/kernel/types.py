"""内核领域类型与常量。

- ``NULL`` 是显式缺失标记：NULL 行不会被悄悄移出样本，在等价类、计数与
  审计中单独可见。
- 所有内核函数只处理普通 dict/list，便于独立测试与脱离 Web 使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: 缺失值的规范标记（输入中空字符串/纯空白都会规范化为它）
NULL: Optional[str] = None

#: 全泛化标签：层级某一级可把值映射为该标签，表示“整体合并”
GENERAL = "*"


@dataclass(frozen=True)
class ColumnSpec:
    """显式声明的列角色。"""

    name: str
    kind: str  # "quasi_identifier" | "sensitive"


@dataclass(frozen=True)
class LevelSpec:
    """单个泛化层级。

    三种规则：

    - ``map``：``mapping`` 给出“上一级标签 -> 本级标签”的显式映射
      （第 1 级的键必须覆盖原始值集合）。
    - ``prefix``：对（规范化后的）字符串取前 ``keep`` 个字符；各级
      ``keep`` 必须严格递减，天然保持包含关系。
    - ``range``：数值区间分箱，``bins`` 为 ``[lo, hi)`` 端点列表，
      外加字符串标签，映射为整数箱编号后与 map 同等校验包含。
    """

    rule: str
    mapping: Optional[dict[str, str]] = None
    keep: Optional[int] = None
    bins: Optional[list[float]] = None
    labels: Optional[list[str]] = None
    # 本级人读名称（纯整数级也可，这里便于报告解释），不含数据
    name: Optional[str] = None


@dataclass
class HierarchySpec:
    """一个准标识符列的完整泛化层级声明（level 0 = 原始值）。"""

    column: str
    levels: list[LevelSpec] = field(default_factory=list)

    @property
    def depth(self) -> int:
        return len(self.levels)


@dataclass
class Dataset:
    """解析后的数据集与显式角色声明。

    ``rows`` 中每个取值为字符串或 :data:`NULL`；列顺序按声明保留。
    """

    columns: list[str]
    qi_columns: list[str]
    sensitive_columns: list[str]
    rows: list[dict[str, Optional[str]]]
    hierarchies: dict[str, HierarchySpec]
    # 解析证据：每列 NULL 行数等聚合信息（无原始值）
    null_counts: dict[str, int] = field(default_factory=dict)
