"""OT 核心：并发变换（xform）与本地组合（compose）。

两个操作流都表示为 :mod:`textmodel` 的规范组件流（按位置升序、
同点 ins 在 del 前、组件间隙即经典 OT 的 retain 段）。

``xform(a, b) -> (a', b')`` 满足收敛（TP1）::

    apply(a', apply(b, S)) == apply(b', apply(a, S))

关键契约：

* 并发同点插入按来源身份稳定排序，键为
  ``(client_id, client_op_id, text, 端序兜底)``；
* 并发删除的重叠区域只扣除一次——重叠区在各自变换结果中长度为 0；
* 插入落在对方删除区间内部时，删除在插入点裂成两段，插入不丢失；
* 插入与删除在同一点时，插入位于删除左侧（与单消息内规范序一致）。

``compose(a, b)``：``a`` 基于 S，``b`` 基于 T=apply(a,S)，结果直接
基于 S。实现为线性动作的单遍归并（retain/ins/del），与
``xform`` 采用同一套“无限 retain 哨兵”约定。
"""

from __future__ import annotations

from .errors import ComputationFailed
from .textmodel import Comp, normalize

_SIDE_A = 0
_SIDE_B = 1
INF = 10**18


def _ins_key(c: Comp, side: int) -> tuple[str, int, int, str]:
    """并发同点插入总序。

    主键是来源身份 ``(client_id, client_op_id)``——这是跨进程一致
    的裁决依据，绝不能让“操作位于变换的哪一侧”覆盖它（否则两个
    接收顺序会得到相反结果）。``side`` 只在身份完全相同时兜底
    （同消息同点插入在进入 xform 前已合并，正常不会走到），text
    是最后的确定性兜底。
    """
    return (c.client_id, c.client_op_id, side, c.text)


def _gather_inserts(comps: list[Comp], i: int, x: int) -> tuple[list[Comp], int]:
    out: list[Comp] = []
    n = len(comps)
    while i < n and comps[i].is_ins and comps[i].pos == x:
        out.append(comps[i])
        i += 1
    return out, i


class _Side:
    __slots__ = ("comps", "i")

    def __init__(self, comps: list[Comp]):
        self.comps = comps
        self.i = 0

    def head(self) -> Comp | None:
        return self.comps[self.i] if self.i < len(self.comps) else None

    def advance_past(self) -> None:
        self.i += 1

    def deleting_end(self, x: int) -> int | None:
        c = self.head()
        if c is not None and c.is_del and c.pos <= x < c.pos + c.length:
            return c.pos + c.length
        return None

    def next_start(self, x: int) -> int | None:
        c = self.head()
        if c is None:
            return None
        if c.is_del and c.pos <= x:
            if self.i + 1 < len(self.comps):
                return self.comps[self.i + 1].pos
            return None
        return c.pos


def xform(a: list[Comp], b: list[Comp]) -> tuple[list[Comp], list[Comp]]:
    """两个基于同一文档的规范流的对称变换（TP1）。"""
    A = _Side(a)
    B = _Side(b)
    x = 0
    ca = 0  # x 在 B 结果文档中的坐标
    cb = 0  # x 在 A 结果文档中的坐标
    ap: list[Comp] = []
    bp: list[Comp] = []

    def flush_open(out: list[Comp], open_del: list) -> None:
        if open_del:
            pos, length = open_del
            if length:
                out.append(Comp.del_(pos, length))
            open_del.clear()

    open_a: list = []
    open_b: list = []

    while True:
        hA = A.head()
        hB = B.head()
        if hA is None and hB is None:
            break

        # 1) x 处插入
        ains: list[Comp] = []
        bins: list[Comp] = []
        if hA is not None and hA.is_ins and hA.pos == x:
            ains, ni = _gather_inserts(A.comps, A.i, x)
            A.i = ni
        if hB is not None and hB.is_ins and hB.pos == x:
            bins, ni = _gather_inserts(B.comps, B.i, x)
            B.i = ni

        if ains or bins:
            flush_open(ap, open_a)
            flush_open(bp, open_b)
            tagged = [(_ins_key(c, _SIDE_A), c, _SIDE_A) for c in ains]
            tagged += [(_ins_key(c, _SIDE_B), c, _SIDE_B) for c in bins]
            tagged.sort(key=lambda t: t[0])
            # 最终文本按总序升序。normalize 会把同点插入按
            # (pos, client_id, op_id) 排序后*拼接成一个*插入组件，
            # 因此这里只需为每个插入计算它在对方结果文档中的正确位置：
            # 位置 = 锚点 + 总序比它小的对方插入总长（落在其左侧）。
            total_a = sum(len(c.text) for c in ains)
            total_b = sum(len(c.text) for c in bins)
            seen_a = 0
            seen_b = 0
            for _key, c, side in tagged:
                if side == _SIDE_A:
                    ap.append(Comp.ins(ca + seen_b, c.text,
                                       c.client_id, c.client_op_id))
                    seen_a += len(c.text)
                else:
                    bp.append(Comp.ins(cb + seen_a, c.text,
                                       c.client_id, c.client_op_id))
                    seen_b += len(c.text)
            ca += total_b
            cb += total_a
            continue

        # 2) 基文档范围段
        ae = A.deleting_end(x)
        be = B.deleting_end(x)
        nsa = A.next_start(x)
        nsb = B.next_start(x)
        a_del = ae is not None
        b_del = be is not None
        end_a = ae if a_del else nsa
        end_b = be if b_del else nsb
        if end_a is None and end_b is None:
            break
        if end_a is None:
            L = end_b - x
        elif end_b is None:
            L = end_a - x
        else:
            L = min(end_a, end_b) - x
        if L <= 0:
            raise ComputationFailed(
                "xform 扫描停滞", reason="XFORM_STALLED",
                details={"x": x, "end_a": end_a, "end_b": end_b},
            )

        was_a = a_del
        was_b = b_del
        if a_del and not b_del:
            if not open_a:
                open_a[:] = [ca, 0]
            open_a[1] += L
            ca += L
        elif b_del and not a_del:
            if not open_b:
                open_b[:] = [cb, 0]
            open_b[1] += L
            cb += L
        elif a_del and b_del:
            pass  # 重叠删除：双方都不计长度（不重复扣除）
        else:
            ca += L
            cb += L
        x += L

        now_a = A.deleting_end(x) is not None
        now_b = B.deleting_end(x) is not None
        if was_a and not now_a:
            flush_open(ap, open_a)
            A.advance_past()
        if was_b and not now_b:
            flush_open(bp, open_b)
            B.advance_past()

    flush_open(ap, open_a)
    flush_open(bp, open_b)
    return normalize(ap), normalize(bp)


def xform_against_history(op: list[Comp], history: list[list[Comp]]) -> list[Comp]:
    """把基于旧版本的 op 连续变换过其后所有已提交修订。"""
    cur = op
    for rev in history:
        cur, _ = xform(cur, rev)
    return cur


# ---------------------------------------------------------------- compose
#
# 线性动作：("ret", n) / ("ins", Comp) / ("del", n)，末尾 ("ret", INF)。
# 归并规则（A 基于 S，B 基于中间文档 T=apply(A,S)）：
#
#   A 动作   B 动作   输出        说明
#   ins      *        ins(若 B ret)/空(若 B del)   a 插入保留或被 b 删
#   del      del      错误 COMPOSE_DELETED_REF    b 不能引用已删字符
#   del      ret/INF  del         a 删除生效，B 游标停在边界不动
#   ret      ins      ins         b 的新插入落位
#   ret      del      del         b 删除基字符
#   ret      ret      （前进，无输出）
#
# 游标 A 推进基文档坐标；游标 B 推进中间文档坐标。ins 只占中间文档，
# del 只占基文档，ret 同时占两者。


def _linear(comps: list[Comp]) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    cursor = 0
    for c in comps:
        if c.pos > cursor:
            out.append(("ret", c.pos - cursor))
        if c.is_ins:
            out.append(("ins", c))
        else:
            out.append(("del", c.length))
            cursor = c.pos + c.length
    out.append(("ret", INF))
    return out


class _LCursor:
    """线性动作游标，支持按长度部分消费；ins 剩余量记录为文本切片。"""

    __slots__ = ("items", "i", "rem")

    def __init__(self, items):
        self.items = items
        self.i = 0
        self.rem: int | str | None = None

    def head(self):
        kind, payload = self.items[self.i]
        if kind == "ins":
            return kind, payload, (payload.text if self.rem is None else self.rem)
        return kind, payload, (payload if self.rem is None else self.rem)

    def take(self, n=None):
        kind, _p = self.items[self.i]
        if kind == "ret" and _p >= INF:
            return  # 无限 retain：永不耗尽
        rem = self.head()[2]
        if kind == "ins":
            if n is None or n >= len(rem):
                self.i += 1
                self.rem = None
            else:
                self.rem = rem[n:]
        else:
            if n is None or n >= rem:
                self.i += 1
                self.rem = None
            else:
                self.rem = rem - n


def compose(a: list[Comp], b: list[Comp]) -> list[Comp]:
    A = _LCursor(_linear(a))
    B = _LCursor(_linear(b))
    out: list[Comp] = []
    emit_seq: list[int] = []  # 与 out 并行：发射次序（同点插入的真实先后）
    seq = 0
    anchor = 0  # 输出锚点：只随“基字符且未被删”的长度推进

    def emit(c: Comp):
        nonlocal seq
        out.append(c)
        emit_seq.append(seq)
        seq += 1

    guard = 0
    while True:
        guard += 1
        if guard > 2_000_000:
            raise ComputationFailed("compose 步数超限", reason="COMPOSE_STEPS")
        ka, pa, ra = A.head()
        kb, pb, rb = B.head()
        a_inf = ka == "ret" and ra >= INF
        b_inf = kb == "ret" and rb >= INF
        if a_inf and b_inf:
            break

        # b 的插入：在中间文档当前位置落位。
        # 注意：若 A 头也是插入，b 的插入在中间文档中位于 a 插入之后
        #（线性序列里 a-ins 先被 B 的 retain 跨过才会轮到 b-ins），
        # 因此仍然先发射 b 的插入，再由下一轮处理 a 插入——但发射顺序
        # 不影响结果，normalize 会按 (pos, 来源) 重排。
        if kb == "ins":
            emit(Comp.ins(anchor, rb, pb.client_id, pb.client_op_id))
            B.take()
            continue

        if ka == "ins":
            n = len(ra)
            if kb == "ret":
                take = min(n, rb)
                if take == 0:
                    raise ComputationFailed("compose 零长推进", reason="COMPOSE_STALLED")
                emit(Comp.ins(anchor, ra[:take], pa.client_id, pa.client_op_id))
                A.take(take)
                B.take(take)
                continue
            if kb == "del":
                # b 删除 a 插入的字符（全删或部分删）
                take = min(n, rb)
                if take == 0:
                    raise ComputationFailed("compose 零长推进", reason="COMPOSE_STALLED")
                A.take(take)
                B.take(take)
                # a 插入被删掉的部分不产生输出；若还有剩余，下一轮
                # A.head 仍是该 ins 的切片，由 a-ins/b-ret 分支输出
                continue
            raise ComputationFailed("compose 非法组合", reason="COMPOSE_BAD_COMBO")

        # 至此两侧头部均为 ret/del（基字符动作）
        if ka == "del" and kb == "del":
            raise ComputationFailed(
                "compose：第二个操作引用了已被第一个操作删除的字符",
                reason="COMPOSE_DELETED_REF",
            )
        if ka == "del":
            if kb != "ret":
                raise ComputationFailed("compose：删除段未与 retain 对齐",
                                        reason="COMPOSE_MISALIGNED")
            # a 删除段在 T 中不可见。线性化保证到达这里时，B 的中间
            # 文档指针正停在删除段左边界（其前的共同基字符已消费）。
            # B 的 retain（无论有限还是无限）都从该边界起算，因此
            # 绝不在这里消费它——删除段后 B 自然指向段后第一个字符。
            # 唯一非法情形：B 的下一个动作是 del（已由 ka==del 与
            # kb==del 的检查在更上面拦截）。
            emit(Comp.del_(anchor, ra))
            A.take(ra)
            continue  # B、anchor 均不动
        if kb == "del":
            if ka != "ret":
                raise ComputationFailed("compose：删除段未与 retain 对齐",
                                        reason="COMPOSE_MISALIGNED")
            # a 侧即使已到无限 retain，也只是隐式保留后续基字符；
            # b 删除这些字符合法。是否真的越界由取 min 后长度决定：
            # b 声明删除的长度若超过文档实际长度，最后一轮 B 仍为
            # 有限 del 而没有可对齐的基字符——由 shared-ret 步的
            # INF/有限组合检测，这里正常输出。
            take = rb if a_inf else min(ra, rb)
            if take == 0:
                raise ComputationFailed("compose 零长推进", reason="COMPOSE_STALLED")
            emit(Comp.del_(anchor, take))
            A.take(take)
            B.take(take)
            continue  # anchor 不动

        # 双方都在 retain：共同保留基字符。
        # 两侧都无限 -> 正常结束；
        # b 侧无限（中间文档到尾）而 a 侧还有有限保留 -> 继续消费 a；
        # a 侧无限而 b 侧还有有限保留 -> 继续消费 b；
        # 真正的越界（b 还要 del，但已无基字符）在 kb=='del' 分支处理。
        if a_inf and b_inf:
            break
        ra_eff = ra if not a_inf else INF
        rb_eff = rb if not b_inf else INF
        take = min(ra_eff, rb_eff)
        if take == 0 or take >= INF:
            # 理论不可达
            raise ComputationFailed("compose 零长推进", reason="COMPOSE_STALLED")
        A.take(take)
        B.take(take)
        anchor += take

    return _merge_compose_out(list(zip(emit_seq, out)))


def _merge_compose_out(tagged: list[tuple[int, Comp]]) -> list[Comp]:
    """compose 专用收尾。

    与 :func:`textmodel.normalize` 的关键区别：同点插入是*顺序*关系，
    按发射次序（中间文档中的真实先后）拼接为**单个**插入组件，身份
    取该点首个插入——发送前整条消息还会被客户端重盖成统一消息号。
    同点 ins/del 并列时插入在左；零长组件丢弃。
    """
    if not tagged:
        return []
    ordered = sorted(tagged, key=lambda t: (t[1].pos, 0 if t[1].is_ins else 1, t[0]))
    merged: list[Comp] = []
    for _seq, c in ordered:
        if c.is_del and c.length == 0:
            continue
        if c.is_ins and c.text == "":
            continue
        if merged and c.is_ins and merged[-1].is_ins and merged[-1].pos == c.pos:
            prev = merged[-1]
            merged[-1] = Comp.ins(prev.pos, prev.text + c.text,
                                  prev.client_id, prev.client_op_id)
            continue
        merged.append(c)
    return merged
