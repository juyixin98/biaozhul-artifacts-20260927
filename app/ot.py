"""算法索引：``apply`` / ``transform`` / ``global_merge``（仅插入/删除）。

``transform(a, b) -> (a', b')`` 满足 TP1 收敛方程：

    apply(apply(S, b), a') == apply(apply(S, a), b')

服务端把迟到操作依次 ``transform`` 穿过自其基线以来已落库的（变换后）
操作。客户端遵守"最多一个在途操作（inflight）"的同步纪律——确认前不发
下一笔——因此同一个并发区间里每个客户端至多贡献一个操作。在这一纪律下
逐对变换即收敛，且**同点并发插入按来源键 ``origin=(client_id, client_seq)``
字典序稳定排序**（键小者在合并文本中靠前），**删除重叠区只扣一次**
（双方都删的基线字符在对侧变换结果里不再删除）。

实现采用带**原子身份**的合并/反解（而不是易错的手写游标分支表）：

1. 基线码点原子 ``("b", index)``；插入字符原子带确定性身份
   ``("ins", client_id, seq, 字符序号)``，同一操作在任意次重放中身份稳定；
2. 收集两侧删除下标集合与按锚点分组的插入；
3. 公共结果：删除取并集（重叠只扣一次）；同锚点插入按 origin 排序后
   输出，再输出存活基线字符；
4. a'、b' 由公共结果与单方视图做**原子身份对齐**反解：两边都有的原子
   retain；只在公共结果里的本方插入 insert；只在单方视图里的（被对方删
   掉而本方仍存活的）基线字符 delete。

此外提供 :func:`global_merge`：对任意个**都基于同一版本**的操作做顺序无关
的 N 路全局合并（删除并集、锚点分组按位置序、同锚点按 origin 序）。它是
权威收敛规则的参考实现，并刻画了"≥3 客户端、删除把不同插入锚点之间字符
删光"这一逐对变换不再顺序无关（TP2 谜题）的边界；当前服务在单 inflight
纪律下使用 ``transform``，两客户端场景两者结果一致。

测试中的 ``tests.oracle`` 是另一套独立实现（刻意不 import 本模块），与
``transform`` 在短串上穷举对拍。
"""
from __future__ import annotations

from .errors import MalformedOperation, TransformInvariant
from .models import Component, Op


def apply(op: Op, text: str) -> str:
    """把操作应用到基线文本，返回新文本；操作必须恰好消费整个基线。"""
    if not isinstance(text, str):
        raise MalformedOperation("base text must be str")
    if op.base_len != len(text):
        raise MalformedOperation(
            f"op consumes {op.base_len} base chars but text has {len(text)}"
        )
    out: list[str] = []
    cursor = 0
    for c in op.components:
        if c.is_retain:
            if cursor + c.value > len(text):
                raise MalformedOperation("retain runs past end of text")
            out.append(text[cursor : cursor + c.value])
            cursor += c.value
        elif c.is_insert:
            out.append(c.value)
        else:  # delete
            if cursor + c.value > len(text):
                raise MalformedOperation("delete runs past end of text")
            cursor += c.value
    if cursor != len(text):
        raise MalformedOperation(f"op ended at {cursor}, text length {len(text)}")
    return "".join(out)


# ------------------------------------------------------------- 原子合并
def _collect(op: Op, deleted: set[int], groups: dict[int, list[tuple]],
             side: str = "x"):
    """扫描操作：收集删除下标与锚点处的插入原子。

    插入原子的身份是**确定性**的：``("x", origin, 字符在该插入内的序号)``，
    这样同一操作在多次重放（被 transform、被全局合并）中其原子身份稳定，
    已落库历史与新提交的相同插入能正确判为"同一个原子"。
    """
    i = 0
    for c in op.components:
        if c.is_retain:
            i += c.value
        elif c.is_delete:
            deleted.update(range(i, i + c.value))
            i += c.value
        else:  # insert
            for k, ch in enumerate(c.value):
                uid = ("ins", c.origin[0], c.origin[1], k)
                groups.setdefault(i, []).append(("x", uid, c.origin, ch, side))
    return i


def _aid(atom: tuple):
    return ("b", atom[1]) if atom[0] == "b" else ("x", atom[1])


def _decode(from_view: list[tuple], merged: list[tuple], self_side: str) -> Op:
    """构造"在 ``from_view``（peer 全局合并视图）上执行、得到 ``merged``"的操作。

    ``from_view`` 是 ``merged`` 去掉本方插入后的子序列（两者对塌缩插入都按
    origin 排序，相对顺序一致）。先把 merged 每个目标原子标注成对源视图的
    动作（retain / delete / insert），再交给 :class:`Op` 规范化合并。

      * 源视图里有、merged 里也有的原子 → retain；
      * 源视图里有、merged 里没有的基线原子 → delete；
      * merged 里有、源视图没有且属于本方 → insert（携带 origin）。
    """
    src_ids = {_aid(a) for a in from_view}
    merged_ids = {_aid(a) for a in merged}

    # 第一遍：merged 目标序列对应的动作（不含源尾部删除）
    tokens: list[tuple] = []  # ("r",) ("d",) ("i", text, origin)
    fi = 0
    for atom in merged:
        aid = _aid(atom)
        if aid in src_ids:
            while _aid(from_view[fi]) != aid:
                cur = from_view[fi]
                if _aid(cur) in merged_ids:
                    tokens.append(("r",))          # 顺序内 peer 插入
                elif cur[0] == "b":
                    tokens.append(("d",))          # 被本方删除的基线字符
                else:
                    raise TransformInvariant("self insert leaked into peer view")
                fi += 1
            tokens.append(("r",))                  # 该共享原子本身
            fi += 1
        else:
            if atom[0] != "x" or atom[4] != self_side:
                raise TransformInvariant("non-self atom absent from peer view")
            tokens.append(("i", atom[3], atom[2]))
    # 源视图尾部
    while fi < len(from_view):
        cur = from_view[fi]
        if _aid(cur) in merged_ids:
            tokens.append(("r",))
        elif cur[0] == "b":
            tokens.append(("d",))
        else:
            raise TransformInvariant("trailing self insert in peer view")
        fi += 1

    comps: list[Component] = []
    for tok in tokens:
        if tok[0] == "r":
            comps.append(Component.retain(1))
        elif tok[0] == "d":
            comps.append(Component.delete(1))
        else:
            # 相邻同 origin 插入由 Op.build 合并；先避免被 retain/delete 拆开
            comps.append(Component.insert(tok[1], tok[2]))
    return Op.build(comps)


# ------------------------------------------------------------- transform
def transform(op_a: Op, op_b: Op) -> tuple[Op, Op]:
    """对基于同一文档版本的两个并发操作做变换，返回 (a', b')。

    a' 作用于 ``apply(S,b)`` 之后，b' 作用于 ``apply(S,a)`` 之后；两路
    重放收敛到同一文本，同点并发插入按 origin 稳定排序。本质上就是 N=2
    的全局合并 + 两侧身份差反解。
    """
    if op_a.base_len != op_b.base_len:
        raise MalformedOperation(
            f"transform base length mismatch: {op_a.base_len} != {op_b.base_len}"
        )
    n = op_a.base_len
    base = [("b", i, "") for i in range(n)]
    da, ga = _collect_one(op_a, "a")
    db, gb = _collect_one(op_b, "b")

    merged_groups, merged_seen = {}, set()
    for gx in (ga, gb):
        for anchor, atoms in gx.items():
            bucket = merged_groups.setdefault(anchor, [])
            for atom in atoms:
                if atom[1] not in merged_seen:
                    merged_seen.add(atom[1])
                    bucket.append(atom)

    merged = _merge_unified(base, da | db, merged_groups)
    view_b = _merge_unified(base, db, gb)
    view_a = _merge_unified(base, da, ga)
    a_prime = _decode(view_b, merged, "a")
    b_prime = _decode(view_a, merged, "b")
    return a_prime, b_prime


def _collect_one(op: Op, side: str):
    do: set[int] = set()
    go: dict[int, list[tuple]] = {}
    if _collect(op, do, go, side) != op.base_len:
        raise MalformedOperation("op does not span the whole base")
    return do, go


# ------------------------------------------------------- N 路全局合并
def _collect_many(ops: list[Op], side: str):
    """统一收集一组操作的删除并集与按锚点分组（插入按身份去重）。"""
    deleted: set[int] = set()
    groups: dict[int, list[tuple]] = {}
    seen: set = set()
    for k, op in enumerate(ops):
        do: set[int] = set()
        go: dict[int, list[tuple]] = {}
        _collect(op, do, go, side)
        deleted |= do
        for anchor, atoms in go.items():
            bucket = groups.setdefault(anchor, [])
            for atom in atoms:
                if atom[1] not in seen:
                    seen.add(atom[1])
                    bucket.append(atom)
    return deleted, groups


def global_merge(base_text: str, ops: list[Op]) -> str:
    """把任意个基于同一版本 ``base_text`` 的操作做顺序无关的全局 N 路合并。

    删除取并集（重叠只扣一次）；存活基线字符保留；插入分组按锚点位置
    顺序输出，同锚点内按 ``origin`` 排序；相邻锚点间字符被删光时分组塌缩
    但仍保持锚点先后。这是服务端的权威收敛规则，与到达顺序无关。
    """
    n = len(base_text)
    for k, op in enumerate(ops):
        if op.base_len != n:
            raise MalformedOperation(
                f"global_merge op {k} base length {op.base_len} != {n}"
            )
    deleted, groups = _collect_many(ops, "g")
    base = [("b", i, ch) for i, ch in enumerate(base_text)]
    merged = _merge_unified(base, deleted, groups)
    return _chars(merged)


def _chars(atoms: list[tuple]) -> str:
    out: list[str] = []
    for a in atoms:
        out.append(a[2] if a[0] == "b" else a[3])
    return "".join(out)


def _merge_unified(base: list[tuple], deleted: set[int],
                   groups: dict[int, list[tuple]]) -> list[tuple]:
    """N 路公共合并。

    插入分组按**锚点位置**顺序输出；同一锚点内按 origin 排序。当相邻锚点
    之间的基线字符被删光时这些分组塌缩到一起，但仍保持"锚点 2 的分组在前、
    锚点 3 的分组在后"的相对次序——只有真正同锚点的并发插入才用 origin
    决胜。这与独立预言机的 ground truth 一致，且对到达顺序不敏感
    （锚点位置与 origin 都是操作的确定属性）。
    """
    merged: list[tuple] = []
    pending_anchors: list[int] = []  # 已塌缩、待输出的锚点（升序去重）

    def flush(anchor: int | None):
        # 按锚点升序依次输出；锚点内按 origin 排序
        for anc in sorted(set(pending_anchors)):
            grp = sorted(groups[anc], key=lambda t: t[2])
            merged.extend(grp)
        pending_anchors.clear()

    for i, atom in enumerate(base):
        if i in groups:
            pending_anchors.append(i)
        if i not in deleted:
            flush(i)
            merged.append(atom)
        # i 被删：pending_anchors 保留，与后续锚点塌缩
    end = len(base)
    if end in groups:
        pending_anchors.append(end)
    flush(end)
    return merged
