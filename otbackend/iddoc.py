"""字符身份（id）文档模型与顺序无关的操作集成。

为什么需要它
------------
纯整数位置的 TP1 只在“一对”操作上保证收敛；三个以上并发插入若用
链式位置变换，它们锚定的字符一旦被别的操作删除，就会被压到同一
整数位置，相对先后随*接收顺序*漂移。解决办法是给插入字符一个与
提交顺序无关的全局身份，并把它锚定到一个稳定的“槽位”上。

模型
----
文档由 ``n+1`` 个槽位（slot）界定，槽位 ``i`` 位于基字符 ``i``
左侧，槽位 ``n`` 在文末。基字符可被删除（打墓碑）。每个插入字符
挂在某个槽位上；同一槽位上的插入字符按其全局 id 字符串总序排列。

* 基字符 id：``"base:<i>"``
* 插入 id：``"<client_id>#<client_op_id>:<消息内序号>"``，全局唯一

在某版本位置 ``p`` 插入 = 先把该位置映射到（那时的）槽位；之后
无论其他操作如何增删，槽位身份不变，故落点与接收顺序无关。
删除按基字符 id 命中，重叠删除天然只生效一次。

线上协议仍是整数位置；本模块只在服务端引擎提交时使用（引擎持有
当前 HEAD 的 id 结构）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import StateConflict
from .textmodel import Comp

BOUND = ""  # 槽位本身无字符；用下标即可


def base_id(i: int) -> str:
    return f"base:{i}"


def is_base(cid: str) -> bool:
    return cid.startswith("base:")


def insert_ids_of(comp: Comp) -> list[str]:
    if comp.char_ids:
        return list(comp.char_ids)
    return [comp.char_id_at(k) for k in range(len(comp.text))]


@dataclass(slots=True)
class IdDoc:
    """顺序无关文档。

    * ``n_base``：初始（r0）基字符总数；槽位 0..n_base。
    * ``base_alive``：长度 n_base，基字符是否存活。
    * ``base_ch``：长度 n_base，基字符码点。
    * ``hang[i]``：挂在槽位 i 的插入字符 ``[(char_id, ch)]``，
      始终按 char_id 排序。
    """

    n_base: int
    base_alive: list[bool]
    base_ch: list[str]
    hang: list[list[tuple[str, str]]]

    @classmethod
    def initial(cls, text: str) -> "IdDoc":
        ch = list(text)
        n = len(ch)
        return cls(n, [True] * n, ch, [[] for _ in range(n + 1)])

    # ---- 物化 ----------------------------------------------------
    def render(self) -> str:
        return "".join(ch for ch in self.ids_text()[1])

    def ids_text(self) -> tuple[list[str], list[str]]:
        ids: list[str] = []
        text: list[str] = []
        for i in range(self.n_base):
            for cid, ch in self.hang[i]:
                ids.append(cid)
                text.append(ch)
            if self.base_alive[i]:
                ids.append(base_id(i))
                text.append(self.base_ch[i])
        for cid, ch in self.hang[self.n_base]:
            ids.append(cid)
            text.append(ch)
        return ids, text

    def ids(self) -> list[str]:
        return self.ids_text()[0]

    # ---- 位置映射 ------------------------------------------------
    def position_to_slot(self, pos: int) -> int:
        """把当前物化文档的码点位置映射到槽位号。"""
        cur = 0
        for i in range(self.n_base + 1):
            if pos <= cur + len(self.hang[i]):
                return i
            cur += len(self.hang[i])
            if i < self.n_base and self.base_alive[i]:
                cur += 1
        return self.n_base

    # ---- 提交（顺序无关）----------------------------------------
    def commit(self, comps: list[Comp]) -> "IdDoc":
        """把基于*当前物化文档*坐标的一条消息集成进来。

        返回新 IdDoc（不改 self）。插入字符的全局 id 必须唯一；
        同槽位按 id 排序，因此结果与历史接收顺序无关。
        """
        nxt = IdDoc(
            self.n_base,
            list(self.base_alive),
            list(self.base_ch),
            [list(h) for h in self.hang],
        )
        # 删除：右到左按当前物化坐标解析为基字符/插入字符
        material_ids, _ = self.ids_text()
        if not (0 <= len(material_ids)):
            pass
        # 先校验坐标并收集目标 id
        dead_inserts: set[str] = set()
        dead_bases: set[int] = set()
        for c in comps:
            if c.is_del:
                end = c.pos + c.length
                if c.pos < 0 or end > len(material_ids):
                    raise StateConflict(
                        "删除区间超出文档", reason="DELETE_OUT_OF_RANGE",
                        details={"pos": c.pos, "length": c.length,
                                 "doc_len": len(material_ids)})
                for cid in material_ids[c.pos:end]:
                    if is_base(cid):
                        dead_bases.add(int(cid.split(":", 1)[1]))
                    else:
                        dead_inserts.add(cid)
        # 应用删除
        for i in dead_bases:
            nxt.base_alive[i] = False
        for i in range(nxt.n_base + 1):
            nxt.hang[i] = [(cid, ch) for cid, ch in nxt.hang[i]
                           if cid not in dead_inserts]

        # 插入：用删除前的坐标映射槽位（与客户端编辑时所见一致）。
        # 同一消息内从右到左放置，使同点多插入的相对意图保持；
        # 但跨消息的同槽位顺序一律由 char_id 总序决定。
        # 注意：消息内的插入 id 互不相同，右到左插入后最终再排序
        # 仍会得到按 id 的唯一结果——这保证了顺序无关性。
        for c in sorted((c for c in comps if c.is_ins),
                        key=lambda c: c.pos, reverse=True):
            if c.pos < 0 or c.pos > len(material_ids):
                raise StateConflict(
                    "插入位置超出文档", reason="INSERT_OUT_OF_RANGE",
                    details={"pos": c.pos, "doc_len": len(material_ids)})
            slot = self.position_to_slot(c.pos)
            cids = insert_ids_of(c)
            for k, ch in zip(cids, c.text):
                nxt.hang[slot].append((k, ch))
        for i in range(nxt.n_base + 1):
            nxt.hang[i].sort(key=lambda t: t[0])
        return nxt

    # ---- 在旧基线上解析、向当前 HEAD 重放（顺序无关核心）----------
    def rebase_change(self, base_doc: "IdDoc", comps: list[Comp]) -> "IdDoc":
        """把基于 ``base_doc``（self 的历史版本）的操作重放到 self。

        1) 在 base_doc 上按其坐标解析：删除命中字符 id，插入落到槽位；
        2) 删除按 id 在 self 命中（重叠删除幂等，不重复扣）；
        3) 插入挂到同槽位（基字符槽位跨版本稳定），按 char_id 排序。

        因此结果只取决于操作本身与身份，与中间提交顺序无关。
        """
        if self.n_base != base_doc.n_base or self.base_ch != base_doc.base_ch:
            raise StateConflict("基线与 HEAD 不属于同一初始文档",
                                reason="BASE_MISMATCH")
        base_material, _ = base_doc.ids_text()
        dead_bases: set[int] = set()
        dead_inserts: set[str] = set()

        # 同一条消息内所有组件坐标相对“编辑前”文档。删除互不重叠
        # （parse_message 已校验）。先收集删除命中的身份。
        for c in comps:
            if c.is_del:
                end = c.pos + c.length
                if c.pos < 0 or end > len(base_material):
                    raise StateConflict(
                        "删除区间超出基线文档", reason="DELETE_OUT_OF_RANGE",
                        details={"pos": c.pos, "length": c.length,
                                 "doc_len": len(base_material)})
                for cid in base_material[c.pos:end]:
                    if is_base(cid):
                        dead_bases.add(int(cid.split(":", 1)[1]))
                    else:
                        dead_inserts.add(cid)

        # 构造“应用删除后”的槽位视图（只含既有插入，不含本消息插入）。
        # temp_hang[i] = 该槽位当前 token 数；插入从右到左放置时，把
        # 本消息已放置的插入也计入，从而把“编辑前坐标”精确定位到槽位。
        temp_alive = [a and (i not in dead_bases)
                      for i, a in enumerate(base_doc.base_alive)]
        temp_hang: list[list[str]] = [
            [cid for cid, _ch in base_doc.hang[i] if cid not in dead_inserts]
            for i in range(base_doc.n_base + 1)
        ]

        def slot_of_pos(pos: int) -> int:
            cur = 0
            for i in range(base_doc.n_base + 1):
                cnt = len(temp_hang[i])
                if pos <= cur + cnt:
                    return i
                cur += cnt
                if i < base_doc.n_base and temp_alive[i]:
                    cur += 1
            return base_doc.n_base

        new_items: list[tuple[int, str, str]] = []
        for c in sorted((c for c in comps if c.is_ins),
                        key=lambda c: c.pos, reverse=True):
            if c.pos < 0 or c.pos > len(base_material):
                raise StateConflict(
                    "插入位置超出基线文档", reason="INSERT_OUT_OF_RANGE",
                    details={"pos": c.pos, "doc_len": len(base_material)})
            slot = slot_of_pos(c.pos)
            cids = insert_ids_of(c)
            # 从右到左放置：把这些字符登记到临时槽位，供更早（更左）
            # 的插入定位；它们在该槽位的相对次序最后由 char_id 总序决定。
            for k, (cid, ch) in enumerate(zip(cids, c.text)):
                new_items.append((slot, cid, ch))
            temp_hang[slot] = list(cids) + temp_hang[slot]

        nxt = IdDoc(
            self.n_base, list(self.base_alive), list(self.base_ch),
            [list(h) for h in self.hang],
        )
        for i in dead_bases:
            nxt.base_alive[i] = False
        for i in range(nxt.n_base + 1):
            nxt.hang[i] = [(cid, ch) for cid, ch in nxt.hang[i]
                           if cid not in dead_inserts]
        for slot, cid, ch in new_items:
            nxt.hang[slot].append((cid, ch))
        for i in range(nxt.n_base + 1):
            nxt.hang[i].sort(key=lambda t: t[0])
        return nxt

    # ---- 差异导出（供落库与坐标版客户端使用）--------------------
    def diff_components(self, prev: "IdDoc") -> list[Comp]:
        """生成把 prev 物化文本变为 self 物化文本的规范组件流。

        公共部分按 id 的*最长公共子序列*对齐（重复字符也能正确配对），
        LCS 之外的旧 id 计为删除、新 id 计为插入。这里公共字符的相对
        顺序不变，故用一个贪心“顺序匹配”即可得到保持顺序的公共子序列；
        为在重复 id 下也对齐最多，使用 O(n*m) LCS（操作通常很小）。
        """
        prev_ids, _ = prev.ids_text()
        new_ids, new_text = self.ids_text()
        n, m = len(prev_ids), len(new_ids)

        # LCS 长度表
        dp = [[0] * (m + 1) for _ in range(n + 1)]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                if prev_ids[i] == new_ids[j]:
                    dp[i][j] = dp[i + 1][j + 1] + 1
                else:
                    dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

        # 回溯出标签，再在 prev 的可变副本上边改边记录真实坐标。
        tags: list[str] = []
        i = j = 0
        while i < n or j < m:
            if i < n and j < m and prev_ids[i] == new_ids[j]:
                tags.append("keep"); i += 1; j += 1
            elif j < m and (i >= n or dp[i][j + 1] >= dp[i + 1][j]):
                tags.append("ins"); j += 1
            else:
                tags.append("del"); i += 1

        # 被删除的是标签为 del 的 prev 字符（按 prev 顺序），其在
        # 原始 prev 序列中的位置即删除坐标；右到左执行，坐标天然正确。
        del_prev_positions: list[int] = []
        ii = jj = 0
        for t in tags:
            if t == "keep":
                ii += 1; jj += 1
            elif t == "del":
                del_prev_positions.append(ii)
                ii += 1
            else:
                jj += 1

        work = list(prev_ids)
        ops: list[Comp] = []
        for p in sorted(del_prev_positions, reverse=True):
            ops.append(Comp.del_(p, 1))
            del work[p]

        # 插入位置相对“删除后的中间文档”：apply_stream 先执行所有删除
        # （右到左），再执行插入（右到左）。因此插入坐标 = 最终序列中
        # 该插入*左侧保留(keep)的 prev 字符数*；被删(del)字符不计。
        # 连续插入合并为一个组件。
        ops_ins: list[Comp] = []
        kept_before = 0
        jj = 0
        pending_ids: list[str] = []
        pending_ch: list[str] = []
        pending_pos = 0

        def emit_pending():
            if pending_ids:
                cid, opid = _origin(pending_ids[0])
                ops_ins.append(Comp.ins(pending_pos, "".join(pending_ch),
                                        cid, opid, tuple(pending_ids)))
                pending_ids.clear()
                pending_ch.clear()

        for t in tags:
            if t == "keep":
                emit_pending()
                kept_before += 1
                jj += 1
            elif t == "del":
                emit_pending()
            else:  # ins
                if not pending_ids:
                    pending_pos = kept_before
                pending_ids.append(new_ids[jj])
                pending_ch.append(new_text[jj])
                jj += 1
        emit_pending()

        from .textmodel import normalize
        return normalize(ops + ops_ins)


def _origin(cid: str) -> tuple[str, int]:
    if "#" not in cid:
        return "", 0
    client, rest = cid.split("#", 1)
    try:
        return client, int(rest.split(":", 1)[0])
    except ValueError:
        return client, 0
