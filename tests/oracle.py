"""独立 OT 预言机（oracle）——测试专用，刻意不 import 任何 ``app.*`` 代码。

实现思路与生产核心（游标流 transform）完全不同：

1. 把基线文档的每个码点打上唯一身份标签（base atom）；
2. 每个插入字符也分配一个全局唯一 atom（同一 atom 对象被插入操作的
   执行结果视图与公共合并结果**共享**，因此身份用对象同一性即可）；
3. 删除 = 按"基线字符身份集合"过滤（重叠删除天然只扣一次）；
4. 同锚点的插入按 ``origin`` 字典序稳定排列；
5. ``merged`` 是 ``after_a`` / ``after_b`` 的公共超序列，a'、b' 用
   **原子身份差**两指针对齐直接读出：公共原子 retain、只在 merged 中
   的（对方路径上的）插入 insert、只在 after_* 中的（被本方删除的基线
   字符）delete。

参考答案由这第二套独立算法给出，而不是生产 transform 自己生成自己的期望。
操作表示用裸元组：``("r", n)`` / ``("i", text, (client, seq))`` / ``("d", n)``。
"""
from __future__ import annotations

from typing import Sequence

# atom: ("b", base_index, ch)  基线字符（身份=index）
#       ("x", uid, origin, ch) 插入字符（身份=uid，对象在各视图间共享）
Atom = tuple


class _AtomFactory:
    def __init__(self):
        self._n = 0

    def x(self, origin, ch) -> Atom:
        self._n += 1
        return ("x", self._n, origin, ch)


def _build(base: list[Atom], op: Sequence[tuple], factory: _AtomFactory,
           deleted: set[int], inserts: dict[int, list[Atom]]) -> list[Atom]:
    """执行 op，顺带收集删除集合与按锚点分组的插入 atom；返回执行结果视图。"""
    out: list[Atom] = []
    i = 0
    for comp in op:
        kind = comp[0]
        if kind == "r":
            out.extend(base[i : i + comp[1]])
            i += comp[1]
        elif kind == "i":
            text, origin = comp[1], comp[2]
            for ch in text:
                atom = factory.x(origin, ch)
                out.append(atom)
                inserts.setdefault(i, []).append(atom)
        elif kind == "d":
            for j in range(i, i + comp[1]):
                deleted.add(j)
            i += comp[1]
        else:
            raise ValueError(f"bad component {comp}")
    if i != len(base):
        raise ValueError("op base length mismatch")
    return out


def _merge_views(base, deleted_a, deleted_b, inserts_a, inserts_b):
    """由删除集合与插入分组构造公共合并结果（插入 atom 与视图共享）。"""
    deleted_all = deleted_a | deleted_b
    merged: list[Atom] = []
    for idx, atom in enumerate(base):
        if idx in inserts_a or idx in inserts_b:
            ins = inserts_a.get(idx, []) + inserts_b.get(idx, [])
            ins.sort(key=lambda t: t[2])  # origin
            merged.extend(ins)
        if idx not in deleted_all:
            merged.append(atom)
    end = len(base)
    if end in inserts_a or end in inserts_b:
        ins = inserts_a.get(end, []) + inserts_b.get(end, [])
        ins.sort(key=lambda t: t[2])
        merged.extend(ins)
    return merged


def _atom_id(atom: Atom):
    return ("b", atom[1]) if atom[0] == "b" else ("x", atom[1])


def _decode(from_atoms: Sequence[Atom], to_atoms: Sequence[Atom]) -> list[tuple]:
    """把 ``from_atoms`` 变成其超序列 ``to_atoms`` 的操作（身份对齐）。

    公共原子 retain；只在 to 中的插入 atom → insert；只在 from 中的
    基线原子（被删除）→ delete。插入按 origin 分组，基线 retain/delete
    按段计数。
    """
    from_ids = {_atom_id(a) for a in from_atoms}
    ops: list[tuple] = []
    pending_del = 0

    def flush_del():
        nonlocal pending_del
        if pending_del:
            ops.append(("d", pending_del))
            pending_del = 0

    i = 0
    for atom in to_atoms:
        if _atom_id(atom) in from_ids:
            while _atom_id(from_atoms[i]) != _atom_id(atom):
                if from_atoms[i][0] != "b":
                    raise AssertionError("oracle: non-base atom disappeared in diff")
                pending_del += 1
                i += 1
            flush_del()
            if ops and ops[-1][0] == "r":
                ops[-1] = ("r", ops[-1][1] + 1)
            else:
                ops.append(("r", 1))
            i += 1
        else:
            flush_del()
            # 收集该位置连续、同 origin 的插入
            origin = atom[2]
            text = atom[3]
            ops.append(("i", text, origin))
    while i < len(from_atoms):
        if from_atoms[i][0] != "b":
            raise AssertionError("oracle: trailing non-base atom disappeared")
        pending_del += 1
        i += 1
    flush_del()
    return _canonical(ops)


def _canonical(ops: list[tuple]) -> list[tuple]:
    merged: list[tuple] = []
    for c in ops:
        if merged:
            p = merged[-1]
            if p[0] == "r" and c[0] == "r":
                merged[-1] = ("r", p[1] + c[1])
                continue
            if p[0] == "d" and c[0] == "d":
                merged[-1] = ("d", p[1] + c[1])
                continue
            if p[0] == "i" and c[0] == "i" and p[2] == c[2]:
                merged[-1] = ("i", p[1] + c[1], p[2])
                continue
        merged.append(c)
    # 末尾 retain 保留（操作覆盖整个基线）；空基线无删除时 decode 不出组件
    return merged


def oracle_transform(base_text: str, a: Sequence[tuple], b: Sequence[tuple]):
    """独立参考实现：返回 (a', b', 公共结果文本)，并内部自检两路收敛。"""
    base = [("b", i, ch) for i, ch in enumerate(base_text)]
    factory = _AtomFactory()
    deleted_a: set[int] = set()
    deleted_b: set[int] = set()
    inserts_a: dict[int, list[Atom]] = {}
    inserts_b: dict[int, list[Atom]] = {}
    after_a = _build(base, a, factory, deleted_a, inserts_a)
    after_b = _build(base, b, factory, deleted_b, inserts_b)
    merged = _merge_views(base, deleted_a, deleted_b, inserts_a, inserts_b)

    a_prime = _decode(after_b, merged)
    b_prime = _decode(after_a, merged)

    ra = _apply(after_b, a_prime)
    rb = _apply(after_a, b_prime)
    assert _text_of(ra) == _text_of(merged), "oracle self-check failed (a path)"
    assert _text_of(rb) == _text_of(merged), "oracle self-check failed (b path)"
    return a_prime, b_prime, _text_of(merged)


def _apply(atoms: list[Atom], op: Sequence[tuple]) -> list[Atom]:
    """仅用于 oracle 自检：把元组操作应用到 atom 列表。"""
    out: list[Atom] = []
    i = 0
    for comp in op:
        kind = comp[0]
        if kind == "r":
            out.extend(atoms[i : i + comp[1]])
            i += comp[1]
        elif kind == "i":
            for ch in comp[1]:
                out.append(("x", -1, comp[2], ch))  # 自检结果不参与身份比较
        elif kind == "d":
            i += comp[1]
    if i != len(atoms):
        raise ValueError("oracle self-apply length mismatch")
    return out


def _text_of(atoms: Sequence[Atom]) -> str:
    return "".join(a[-1] for a in atoms)
