"""测试共享辅助：生产 ``Op`` 与预言机裸元组操作的桥接。

桥接只发生在测试层——生产核心与预言机各自独立解析/构造，不共享实现。
"""
from __future__ import annotations

from app.models import Component, Op


def tuple_to_op(t_ops: list[tuple]) -> Op:
    comps = []
    for c in t_ops:
        if c[0] == "r":
            comps.append(Component.retain(c[1]))
        elif c[0] == "i":
            comps.append(Component.insert(c[1], c[2]))
        elif c[0] == "d":
            comps.append(Component.delete(c[1]))
        else:
            raise ValueError(c)
    return Op.build(comps)


def op_to_tuples(op: Op) -> list[tuple]:
    out = []
    for c in op.components:
        if c.is_retain:
            out.append(("r", c.value))
        elif c.is_insert:
            out.append(("i", c.value, c.origin))
        else:
            out.append(("d", c.value))
    return out
