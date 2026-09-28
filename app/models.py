"""文本规范：操作（Op）与组件（Component）。

字符索引
--------
所有位置/长度都按 **Unicode 码点（Python ``str`` 的索引单位）** 计算，
多字节字符（中文、emoji、组合序列的基础码点）与 ASCII 一样是 1 个单位。
不做字形簇（grapheme cluster）归一化——这是已知限制，见 README。

操作表示
--------
一个操作是从左到右作用于基线文档的组件流（经典 text-OT 游标模型），
组件类型只有三种：

  retain(n)   保留基线中的 n 个码点不动
  insert(s)   在当前游标处插入码点串 s（不消耗基线字符）
  delete(n)   删除基线中接下来的 n 个码点

规范化（canonical）操作额外保证：
  * 相邻两个 retain 合并、相邻两个 delete 合并；
  * 相邻两个 insert 仅在 **来源相同** 时合并——不同来源必须保持分界，
    因为同点并发插入的排序依赖来源键；
  * **末尾 retain 必须保留**：操作必须消费整个基线文档，``base_len`` 恒等于
    基线码点长度，这是 apply/transform 游标对齐的前提；
  * 纯 no-op（只有 retain、没有任何插入/删除）不允许作为客户端编辑提交；
  * insert 组件携带 ``origin = (client_id, seq)``，标记插入内容的原始
    发起者与该客户端内的单调序号；同点并发插入的稳定排序依赖该来源键。
    retain/delete 不携带 origin；客户端的每次编辑（含纯删除）都由请求层
    单独带 ``client_seq`` 做去重，与操作内部表示解耦。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .errors import EmptyInsert, MalformedOperation

ComponentKind = Literal["retain", "insert", "delete"]


@dataclass(frozen=True, slots=True)
class Component:
    kind: ComponentKind
    # retain/delete：码点数（正整数）；insert：码点串
    value: int | str
    # 仅 insert：(client_id, 客户端单调序号)
    origin: tuple[str, int] | None = None

    # ------------------------------------------------------------ 构造
    @staticmethod
    def retain(n: int) -> "Component":
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            raise MalformedOperation(f"retain length must be positive int, got {n!r}")
        return Component("retain", n)

    @staticmethod
    def insert(s: str, origin: tuple[str, int]) -> "Component":
        if not isinstance(s, str):
            raise MalformedOperation("insert payload must be a string")
        if not isinstance(origin, tuple) or len(origin) != 2:
            raise MalformedOperation("insert origin must be (client_id, seq)")
        client_id, seq = origin
        if not isinstance(client_id, str) or not client_id:
            raise MalformedOperation("insert origin client_id must be non-empty str")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise MalformedOperation("insert origin seq must be positive int")
        if len(s) == 0:
            raise EmptyInsert("insert payload is empty")
        return Component("insert", s, (client_id, seq))

    @staticmethod
    def delete(n: int) -> "Component":
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            raise MalformedOperation(f"delete length must be positive int, got {n!r}")
        return Component("delete", n)

    # ------------------------------------------------------------ 性质
    @property
    def is_retain(self) -> bool:
        return self.kind == "retain"

    @property
    def is_insert(self) -> bool:
        return self.kind == "insert"

    @property
    def is_delete(self) -> bool:
        return self.kind == "delete"

    @property
    def base_len(self) -> int:
        """消耗/保留的基线码点数；insert 为 0。"""
        return self.value if self.kind in ("retain", "delete") else 0

    @property
    def target_len(self) -> int:
        """在结果文档中占的码点数；delete 为 0。"""
        if self.kind == "retain":
            return self.value
        if self.kind == "insert":
            return len(self.value)
        return 0

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        d: dict = {"type": self.kind}
        if self.kind == "insert":
            d["text"] = self.value
            d["client_id"] = self.origin[0]
            d["seq"] = self.origin[1]
        else:
            d["n"] = self.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Component":
        if not isinstance(d, dict):
            raise MalformedOperation("component must be an object")
        kind = d.get("type")
        if kind == "retain":
            return Component.retain(d.get("n"))
        if kind == "delete":
            return Component.delete(d.get("n"))
        if kind == "insert":
            return Component.insert(d.get("text"), (d.get("client_id"), d.get("seq")))
        raise MalformedOperation(f"unknown component type: {kind!r}")


@dataclass(frozen=True, slots=True)
class Op:
    """规范化的操作。构造后不可变；请使用 :meth:`build` 生成。"""

    components: tuple[Component, ...]

    # ------------------------------------------------------------ 构造
    @staticmethod
    def build(raw: list[Component]) -> "Op":
        """规范化：合并相邻同类（insert 需同源）、保留末尾 retain。

        允许结果为纯 retain（no-op）——并发删除重叠区间时对侧的合法变换
        结果就是 no-op；客户端提交 no-op 编辑由服务层单独拒绝。
        """
        merged: list[Component] = []
        for c in raw:
            if not isinstance(c, Component):
                raise MalformedOperation(f"not a component: {c!r}")
            if merged:
                prev = merged[-1]
                if prev.kind == "retain" and c.is_retain:
                    merged[-1] = Component.retain(prev.value + c.value)
                    continue
                if prev.kind == "delete" and c.is_delete:
                    merged[-1] = Component.delete(prev.value + c.value)
                    continue
                if prev.kind == "insert" and c.is_insert and prev.origin == c.origin:
                    merged[-1] = Component.insert(prev.value + c.value, prev.origin)
                    continue
            merged.append(c)
        return Op(tuple(merged))

    @staticmethod
    def build_allow_noop(raw: list[Component]) -> "Op":
        """兼容别名：``build`` 本身已允许 no-op。"""
        return Op.build(raw)

    def is_noop(self) -> bool:
        return all(c.is_retain for c in self.components)

    @staticmethod
    def insert_at(pos: int, text: str, origin: tuple[str, int], doc_len: int) -> "Op":
        """便捷构造：在长度为 ``doc_len`` 的基线文档 ``pos`` 处插入。"""
        if not isinstance(pos, int) or isinstance(pos, bool) or not (0 <= pos <= doc_len):
            raise MalformedOperation(f"insert pos {pos} out of [0,{doc_len}]")
        raw: list[Component] = []
        if pos:
            raw.append(Component.retain(pos))
        raw.append(Component.insert(text, origin))
        if doc_len - pos:
            raw.append(Component.retain(doc_len - pos))
        return Op.build(raw)

    @staticmethod
    def delete_range(pos: int, n: int, doc_len: int) -> "Op":
        """便捷构造：在长度为 ``doc_len`` 的基线文档上删除 [pos, pos+n)。"""
        if not isinstance(pos, int) or isinstance(pos, bool) or not (0 <= pos <= doc_len):
            raise MalformedOperation(f"delete pos {pos} out of [0,{doc_len}]")
        if not isinstance(n, int) or n <= 0 or pos + n > doc_len:
            raise MalformedOperation(f"delete range [{pos},{pos + n}) exceeds doc_len {doc_len}")
        raw: list[Component] = []
        if pos:
            raw.append(Component.retain(pos))
        raw.append(Component.delete(n))
        if pos + n < doc_len:
            raw.append(Component.retain(doc_len - pos - n))
        return Op.build(raw)

    # ------------------------------------------------------------ 长度
    @property
    def base_len(self) -> int:
        return sum(c.base_len for c in self.components)

    @property
    def target_len(self) -> int:
        return sum(c.target_len for c in self.components)

    @property
    def inserted(self) -> int:
        return sum(len(c.value) for c in self.components if c.is_insert)

    @property
    def deleted(self) -> int:
        return sum(c.value for c in self.components if c.is_delete)

    def origins(self) -> frozenset[tuple[str, int]]:
        return frozenset(c.origin for c in self.components if c.is_insert)

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict:
        return {"components": [c.to_dict() for c in self.components]}

    @staticmethod
    def from_dict(d: dict) -> "Op":
        if not isinstance(d, dict) or "components" not in d:
            raise MalformedOperation("op must be an object with 'components'")
        return Op.build([Component.from_dict(c) for c in d["components"]])
