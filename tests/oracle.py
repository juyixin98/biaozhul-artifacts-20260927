"""独立 oracle：用“标记原子 + 独立服务模型”直接模拟集中式 OT 服务器。

本模块**不导入** otbackend.transform，参考答案与被测核心相互独立。

模型
----
* 文档是 :class:`~tests.oracle.Token` 序列（带身份的码点）；
* 客户端在某版本上产生操作（位置按 Token 下标）；
* 服务器按“接收顺序”串行处理：每个新操作针对所有已提交的并发
  操作做坐标变换，再应用。变换规则在此独立实现一遍
  （:func:`server_transform`），同点插入按
  ``(client_id, op_id, seq)`` 总序，删除重叠不重复扣。

两种接收顺序（ab 与 ba）若都收敛到同一个 Token 身份序列，
即验证了收敛性与意图保持。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Token:
    origin: str
    text: str


def tokenize_initial(text: str) -> list[Token]:
    return [Token(f"base:{i}", ch) for i, ch in enumerate(text)]


def render(tokens: list[Token]) -> str:
    return "".join(t.text for t in tokens)


@dataclass(slots=True)
class MarkedOp:
    kind: str
    pos: int
    text: str = ""
    length: int = 0
    client_id: str = ""
    client_op_id: int = 0
    seq: int = 0

    def key(self):
        return (self.client_id, self.client_op_id, self.seq)


def apply_marked(tokens: list[Token], ops: list[MarkedOp]) -> list[Token]:
    out = list(tokens)
    for op in sorted((o for o in ops if o.kind == "del"),
                     key=lambda o: o.pos, reverse=True):
        del out[op.pos:op.pos + op.length]
    for op in sorted((o for o in ops if o.kind == "ins"),
                     key=lambda o: (o.pos, o.client_id, o.client_op_id, o.seq),
                     reverse=True):
        new = [Token(f"{op.client_id}#{op.client_op_id}:{op.seq}", ch)
               for ch in op.text]
        out[op.pos:op.pos] = new
    return out


# ---------------------------------------------------------------
# 独立的服务端变换实现（不引用被测代码）
#
# 用“字符分类视图”：把已提交操作 h 对其文档的每个基字符标注
# ins/del/ret，把新操作 op 的基字符也标注；逐基字符归并。
# ---------------------------------------------------------------


def _span_map(ops: list[MarkedOp], n: int):
    """返回长度 n 的基字符分类：每个下标为 ('del', by_op) 或 ('ret',)。

    同点 ins 不占基字符，单独按位置收集。
    """
    kind = [("ret",) for _ in range(n)]
    for o in ops:
        if o.kind == "del":
            for i in range(o.pos, o.pos + o.length):
                kind[i] = ("del", o)
    inserts_at: dict[int, list[MarkedOp]] = {}
    for o in ops:
        if o.kind == "ins":
            inserts_at.setdefault(o.pos, []).append(o)
    for v in inserts_at.values():
        v.sort(key=lambda o: (o.client_id, o.client_op_id, o.seq))
    return kind, inserts_at


def server_transform(new_ops: list[MarkedOp], history_ops: list[MarkedOp],
                     base_len: int) -> list[MarkedOp]:
    """独立实现：把基于同一基文档的 new_ops 变换过 history_ops。

    返回可应用于 apply_marked(base, history_ops) 结果的操作。
    规则：逐基字符 + 位置点事件，与被测实现是两套独立代码。
    """
    h_kind, h_ins = _span_map(history_ops, base_len)
    n_kind, n_ins0 = _span_map(new_ops, base_len)

    out: list[MarkedOp] = []
    # y：当前基坐标 x 在“history 结果文档”中的位置
    y = 0
    x = 0
    # 累积 new 侧删除（可能被插入裂成多段）
    while x <= base_len:
        # 该点上的插入：history 的插入与 new 的插入共同决定落位
        h_here = h_ins.get(x, [])
        n_here = n_ins0.get(x, [])
        if h_here or n_here:
            tagged = [("h", o) for o in h_here] + [("n", o) for o in n_here]
            tagged.sort(key=lambda t: (t[1].client_id, t[1].client_op_id,
                                       t[1].seq, 0 if t[0] == "h" else 1))
            # new 插入在 history 结果文档中的位置 = y + 总序比它小的
            # history 插入总长（那些最终落在它左侧）。
            seen_h = 0
            seen_n = 0
            for _src, o in tagged:
                if _src == "n":
                    out.append(MarkedOp("ins", y + seen_h, o.text,
                                        client_id=o.client_id,
                                        client_op_id=o.client_op_id, seq=o.seq))
                    seen_n += len(o.text)
                else:
                    seen_h += len(o.text)
            y += sum(len(o.text) for o in h_here)
        if x == base_len:
            break

        hc = h_kind[x]
        nc = n_kind[x]
        if hc[0] == "del" and nc[0] == "del":
            pass  # 重叠删除：都不占结果
        elif hc[0] == "del":
            # history 删了、new 保留：y 不动（字符不在 history 结果中）
            pass
        elif nc[0] == "del":
            # new 要删一个 history 保留的字符：在 y 处删除
            out.append(MarkedOp("del", y, length=1))
            y += 1
        else:
            y += 1
        x += 1
    return out




class OracleServer:
    """独立的集中式服务器模拟（槽位 + 字符身份模型）。

    与被测生产代码 ``otbackend/iddoc.py`` 算法思想相同，但本类
    **不导入生产模块**，是独立实现的参考答案：

    * n+1 个跨版本稳定的槽位；基字符可被打墓碑；
    * 插入字符带全局身份 ``<client>#<op>:<seq>``，挂到提交时位置
      对应的槽位；同槽位插入按身份字符串总序排列；
    * 删除按字符身份命中，重叠删除只生效一次。

    测试消息都基于 r0，位置直接映射到 r0 物化文档的槽位。
    """

    def __init__(self, initial: str):
        self.base_ch = list(initial)
        n = len(self.base_ch)
        self.alive = [True] * n
        self.hang: list[list[tuple[str, str]]] = [[] for _ in range(n + 1)]

    def _material(self):
        ids: list[str] = []
        toks: list[Token] = []
        for i in range(len(self.base_ch)):
            for cid, ch in self.hang[i]:
                ids.append(cid)
                toks.append(Token(cid, ch))
            if self.alive[i]:
                ids.append(f"base:{i}")
                toks.append(Token(f"base:{i}", self.base_ch[i]))
        for cid, ch in self.hang[len(self.base_ch)]:
            ids.append(cid)
            toks.append(Token(cid, ch))
        return ids, toks

    def _slot_of(self, ids: list[str], pos: int) -> int:
        cur = 0
        for i in range(len(self.base_ch) + 1):
            if pos <= cur + len(self.hang[i]):
                return i
            cur += len(self.hang[i])
            if i < len(self.base_ch) and self.alive[i]:
                cur += 1
        return len(self.base_ch)

    @property
    def tokens(self) -> list[Token]:
        return self._material()[1]

    def submit(self, client_id: str, client_op_id: int,
               ops: list[MarkedOp], base_len: int) -> list[Token]:
        # 所有测试操作基于 r0。删除坐标针对“r0 文档 + 此前同消息效果”，
        # 但并发消息之间互不知晓，因此删除坐标先在 r0 物化视图上解析，
        # 再按基字符/插入身份在当前状态命中。为与被测 id 模型一致，
        # 这里维护“r0 的 token 序列”（含所有已挂插入，按槽位展开），
        # 但插入槽位用 r0 基字符下标直接定位。
        ids, _ = self._material()
        # 建立 r0 坐标 -> 当前身份 的映射：在有删除时，操作坐标仍以
        # r0 为准。删除按 r0 槽位区间命中基字符 id；插入位置 p 落在
        # 基字符 p 之前的槽位（p==n 为文末）。
        n0 = len(self.base_ch)
        dead: set[str] = set()
        for o in ops:
            if o.kind == "del":
                end = o.pos + o.length
                # 针对 r0：删除的是 r0 基字符 [pos,end)
                assert 0 <= o.pos and end <= n0, "oracle 删除越界(r0)"
                for i in range(o.pos, end):
                    dead.add(f"base:{i}")
        for cid in dead:
            self.alive[int(cid.split(":", 1)[1])] = False
        # 插入：r0 位置 p 即基字符 p 左侧的槽位
        for o in ops:
            if o.kind == "ins":
                assert 0 <= o.pos <= n0, "oracle 插入越界(r0)"
                cid = f"{o.client_id}#{o.client_op_id}:{o.seq}"
                self.hang[o.pos].append((cid, o.text))
        for i in range(len(self.hang)):
            self.hang[i].sort(key=lambda t: t[0])
        return self.tokens


def marked_from_components(comps, client_id: str = "", client_op_id: int = 0):
    out, k = [], 0
    for c in comps:
        if c.kind == "ins":
            out.append(MarkedOp("ins", c.pos, c.text,
                                client_id=c.client_id or client_id,
                                client_op_id=c.client_op_id or client_op_id, seq=k))
            k += 1
        else:
            out.append(MarkedOp("del", c.pos, length=c.length))
    return out
