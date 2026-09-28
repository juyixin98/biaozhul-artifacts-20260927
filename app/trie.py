"""算法索引层：压缩 Radix Trie + 子树可靠上界 + 精确 top-k。

核心数据结构
============

:class:`RadixNode`
    压缩前缀树节点。入边带一个字符串标签 ``edge_label``（单字符
    :class:`RadixTrie.edges` 映射指向它），节点上保存：

    - ``terminals``：该节点结束的词条 ``entry_id -> (term_norm, display, score)``；
      同一个规范化键允许挂多条原文不同的词条（规范化碰撞）。
    - ``max_score``：**子树最大分值上界** = max(本节点词条分, 所有孩子 max_score)。
    - ``best``：子树最优排序元组 ``(score, canonical_key)``。

不变量（参见 :meth:`RadixTrie.check_invariants`）
=================================================

对每个节点 n：

* ``n.max_score == max(本节点最大词条分, 所有孩子的 max_score)``；
* ``n.best == max(本节点最优词条排序元组, 所有孩子的 best)``；
* 每个孩子的 ``max_score <= n.max_score``；
* 压缩性：非根内部节点只要存在父节点，孩子数 != 1（度为 1 的无词条节点
  在删除后会被合并回父边）。

上界可靠性（热词删改后不能错误剪枝）
====================================

任何变更（插入、降权、删除）后，沿受影响节点向根重新计算
``max_score`` / ``best``（:meth:`RadixTrie._recompute_to_root`），
因此剪枝上界永远是当前子树的真实最优值。剪枝判定是严格比较
``subtree_best > threshold``（见 :func:`top_k`）：

* 被剪掉的子树，其最优候选也严格弱于当前第 k 名，**不可能**翻盘；
* 等于当前阈值的子树绝不剪枝，避免同分漏召回，从而得到精确 top-k。

查询不会遍历全词典再排序：先定位前缀子树，再用按子树上界排序的
best-first 堆展开，上界低于阈值的整棵子树一次剪枝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from .normalize import canonical_key

# 排序元组：(-score, (term_norm, display, entry_id))
# heapq 是最小堆，取负分让“分高者先出”；其后的规范键保证同分稳定全序。
RankTuple = tuple[float, tuple[str, str, str]]
# 词条记录：规范化键, 展示原文, 词频分值
EntryValue = tuple[str, str, float]


@dataclass
class PruneRecord:
    """一次剪枝判定的审计记录（诊断/测试用）。"""

    node_id: int
    edge_label: str
    subtree_best_score: float
    threshold_score: Optional[float]
    decision: str  # "expand" | "prune"
    reason: str


@dataclass
class TopKTrace:
    """top-k 查询的计算步骤轨迹（diagnostics=true 时返回）。"""

    prefix_norm: str
    matched: bool
    node_id: Optional[int]
    pushed: int = 0
    expanded: int = 0
    terminals_seen: int = 0
    terminal_skipped_by_bound: int = 0
    pruned_children: int = 0
    heap_pops: int = 0
    decisions: list[PruneRecord] = field(default_factory=list)


class RadixNode:
    """压缩 Trie 节点。"""

    __slots__ = (
        "nid",
        "edges",
        "terminals",
        "max_score",
        "best_key",
        "best_score",
        "best_id",
        "edge_label",
        "parent",
    )

    def __init__(self, nid: int, edge_label: str = "", parent: "Optional[RadixNode]" = None) -> None:
        #: 节点稳定 ID（诊断与剪枝记录引用），根为 0
        self.nid = nid
        #: 首字符 -> (边标签, 子节点)
        self.edges: dict[str, tuple[str, RadixNode]] = {}
        #: 在此终止的词条：entry_id -> (term_norm, display, score)
        self.terminals: dict[str, EntryValue] = {}
        #: 子树最大分值（可靠上界）；空子树为 -inf
        self.max_score: float = float("-inf")
        # best 排序元组缓存（子树最优候选），空为 None
        self.best_score: float = float("-inf")
        self.best_key: Optional[tuple[str, str, str]] = None
        self.best_id: Optional[str] = None
        #: 父节点指向本节点的边标签（根为 ""）
        self.edge_label = edge_label
        self.parent = parent

    @property
    def best(self) -> Optional[RankTuple]:
        """子树最优排序元组 ``(-score, canonical_key)``，空节点返回 None。"""
        if self.best_key is None:
            return None
        return (-self.best_score, self.best_key)

class RadixTrie:
    """压缩 Radix Trie 索引。"""

    def __init__(self) -> None:
        self.root = RadixNode(0)
        self._next_id = 1
        #: entry_id -> 终止节点（支持 O(深度) 删除与规范化键迁移）
        self._terminal_node: dict[str, RadixNode] = {}

    # ------------------------------------------------------------------ 构建

    def _new_node(self, edge_label: str, parent: RadixNode) -> RadixNode:
        node = RadixNode(self._next_id, edge_label=edge_label, parent=parent)
        self._next_id += 1
        return node

    @staticmethod
    def _common_prefix_len(a: str, b: str) -> int:
        n = min(len(a), len(b))
        i = 0
        while i < n and a[i] == b[i]:
            i += 1
        return i

    def upsert(self, entry_id: str, term_norm: str, display: str, score: float) -> str:
        """插入或更新一个词条；变更后沿父链重算上界。

        同 ``entry_id`` 再次写入即原地替换（词频更新/热词降权走此路径）。
        若新的规范化键与旧位置不同（词条改名导致规范化键变化），先从旧
        终止节点移除（必要时压缩结构），再插入新位置 —— 旧位置上界必须
        同步收缩，否则会错误剪枝。

        :returns: "inserted" | "updated" | "relocated"
        """
        old_node = self._terminal_node.get(entry_id)
        if old_node is not None:
            old_term = old_node.terminals[entry_id][0]
            if old_term == term_norm:
                # 键不变：原地替换分值/原文，树结构不动，沿父链重算。
                old_node.terminals[entry_id] = (term_norm, display, score)
                self._recompute_to_root(old_node)
                return "updated"
            # 键改变：先从旧位置删除（含压缩），再走插入。
            del old_node.terminals[entry_id]
            del self._terminal_node[entry_id]
            self._recompute_to_root(old_node)
            if old_node is not self.root and not old_node.terminals:
                self._compact(old_node)
            relocated = True
        else:
            relocated = False

        node = self.root
        key = term_norm
        i = 0
        while i < len(key):
            ch = key[i]
            edge = node.edges.get(ch)
            if edge is None:
                # 新建一条叶子边承载剩余后缀
                leaf = self._new_node(key[i:], parent=node)
                node.edges[ch] = (key[i:], leaf)
                leaf.terminals[entry_id] = (term_norm, display, score)
                self._terminal_node[entry_id] = leaf
                self._recompute_to_root(leaf)
                return "relocated" if relocated else "inserted"
            label, child = edge
            cp = self._common_prefix_len(label, key[i:])
            if cp == len(label):
                # 整条边匹配，继续向下
                node = child
                i += cp
                continue
            # 部分匹配：分裂这条边
            #   parent --label--> child
            # 变为
            #   parent --head--> mid --tail--> child
            #                     \--suffix--> leaf
            head = label[:cp]
            tail = label[cp:]
            suffix = key[i + cp:]
            mid = self._new_node(head, parent=node)
            child.edge_label = tail
            child.parent = mid
            mid.edges[tail[0]] = (tail, child)
            node.edges[ch] = (head, mid)
            if suffix:
                leaf = self._new_node(suffix, parent=mid)
                mid.edges[suffix[0]] = (suffix, leaf)
                leaf.terminals[entry_id] = (term_norm, display, score)
                self._terminal_node[entry_id] = leaf
                deepest: RadixNode = leaf
            else:
                mid.terminals[entry_id] = (term_norm, display, score)
                self._terminal_node[entry_id] = mid
                deepest = mid
            self._recompute_to_root(deepest)
            return "relocated" if relocated else "inserted"
        # key 在当前 node 结束
        node.terminals[entry_id] = (term_norm, display, score)
        self._terminal_node[entry_id] = node
        self._recompute_to_root(node)
        return "relocated" if relocated else "inserted"

    def delete(self, entry_id: str, term_norm: Optional[str] = None) -> bool:
        """删除词条；不存在返回 False。删除后重算上界并压缩度-1链。

        ``term_norm`` 可省略（内部有 entry_id -> 终止节点索引）；传入时仅作
        防御性校验：若与记录的规范化键不一致则拒绝删除（避免误删）。
        """
        node = self._terminal_node.pop(entry_id, None)
        if node is None:
            return False
        if term_norm is not None and node.terminals[entry_id][0] != term_norm:
            # 调用方给的键与实际记录不符：恢复索引并拒绝
            self._terminal_node[entry_id] = node
            return False
        del node.terminals[entry_id]

        # 先沿父链重算（即使下面压缩了节点，上界也先保持正确）。
        self._recompute_to_root(node)

        # 压缩：终止位置失去全部词条且不是根时，尝试合并/删除。
        if node is not self.root and not node.terminals:
            self._compact(node)
        return True

    def _compact(self, start: RadixNode) -> None:
        """删除/迁移后维护压缩性（自底向上，沿祖先链一路处理）。

        循环不变式：进入 ``cur`` 时它满足“非根、无终止词条、度数 <= 1”。

        - 度 0：从父边移除该节点；父可能因此变为新的待处理节点；
        - 度 1：把入边标签与唯一孩子的边标签拼接，父直接连孩子
          （terminals 全在孩子一侧，无需搬运终止索引）；
        - 度 >= 2 或自身有终止词条：压缩停止。
        """
        cur: Optional[RadixNode] = start
        while cur is not None and cur is not self.root:
            if cur.terminals:
                break
            degree = len(cur.edges)
            if degree >= 2:
                break
            parent = cur.parent
            assert parent is not None
            incoming = cur.edge_label
            if degree == 0:
                parent.edges.pop(incoming[0], None)
                cur.parent = None
                cur = parent
                continue
            # degree == 1：与唯一孩子合并
            (clabel, grand), = cur.edges.values()
            merged = incoming + clabel
            grand.edge_label = merged
            grand.parent = parent
            parent.edges[incoming[0]] = (merged, grand)
            cur.parent = None
            cur = parent
        # 结构变化后自底向上全量重算（树规模与编辑路径同阶）。
        self._recompute_all()

    # ------------------------------------------------------------------ 上界维护

    def _recompute(self, node: RadixNode) -> None:
        """根据本节点词条与直接孩子重新计算 max_score / best。

        两段式，避免候选相互污染：

        1. 先在**本节点终止词条**中选出排序最优（rank 最小）者；
        2. 再与每个孩子的子树最优 rank 比较，取整棵子树最优；
           max_score 单独取“本节点最高分 / 孩子最高分”的最大值。
        """
        # 本节点最优 rank：(-score, canonical_key)
        own_rank: Optional[RankTuple] = None
        for eid, (term, display, score) in node.terminals.items():
            rank: RankTuple = (-score, canonical_key(eid, term, display))
            if own_rank is None or rank < own_rank:
                own_rank = rank

        max_score = float("-inf")
        best_rank: Optional[RankTuple] = own_rank
        if own_rank is not None:
            max_score = -own_rank[0]

        for _label, child in node.edges.values():
            if child.max_score > max_score:
                max_score = child.max_score
            child_rank = child.best
            if child_rank is not None and (best_rank is None or child_rank < best_rank):
                best_rank = child_rank

        node.max_score = max_score
        if best_rank is None:
            node.best_score = float("-inf")
            node.best_key = None
            node.best_id = None
        else:
            neg_score, key = best_rank
            node.best_score = -neg_score
            node.best_key = key
            node.best_id = key[2]

    def _recompute_to_root(self, start: RadixNode) -> None:
        node: Optional[RadixNode] = start
        while node is not None:
            self._recompute(node)
            node = node.parent

    def _recompute_all(self) -> None:
        """自底向上全量重算（删除压缩后调用）。"""
        order: list[RadixNode] = []
        stack = [self.root]
        while stack:
            n = stack.pop()
            order.append(n)
            for _, child in n.edges.values():
                stack.append(child)
        for n in reversed(order):
            self._recompute(n)

    # ------------------------------------------------------------------ 查询

    def locate(self, prefix: str) -> tuple[bool, RadixNode]:
        """定位前缀对应的子树根。

        :returns: (是否匹配, 节点)。不匹配时节点为根（调用方应忽略）。
        前缀是某条边标签的 **真前缀** 也算匹配：该边子树全部属于结果，
        返回该边的孩子节点（孩子子树即前缀子树）。
        """
        node = self.root
        i = 0
        while i < len(prefix):
            ch = prefix[i]
            edge = node.edges.get(ch)
            if edge is None:
                return False, self.root
            label, child = edge
            cp = self._common_prefix_len(label, prefix[i:])
            if cp == len(label):
                node = child
                i += cp
                continue
            # 边比剩余前缀长：prefix 是 label 的真前缀 => 整个 child 子树匹配；
            # 若 label 是剩余 prefix 的真前缀则不可能（cp<len(label)），
            # 这里 cp < len(prefix剩余) 意味着 prefix 更长，查无此键。
            if cp == len(prefix) - i:
                return True, child
            return False, self.root
        return True, node

    # ----------------------------------------------------------------- 遍历/诊断

    def iter_terminals(
        self, node: Optional[RadixNode] = None
    ) -> Iterator[tuple[str, str, str, float]]:
        """遍历某子树下全部终止词条（参考实现/诊断用，查询热路径不用）。"""
        root = node if node is not None else self.root
        stack = [root]
        while stack:
            n = stack.pop()
            for eid, (term, display, score) in n.terminals.items():
                yield term, display, eid, score
            for _, child in n.edges.values():
                stack.append(child)

    def walk_nodes(self) -> Iterator[RadixNode]:
        stack = [self.root]
        while stack:
            n = stack.pop()
            yield n
            for _, child in n.edges.values():
                stack.append(child)

    def check_invariants(self, limit: int = 50) -> list[str]:
        """全量校验结构与上界不变量，返回违规描述列表（空列表=健康）。

        独立重算每个节点的上界并与缓存值比对，用于诊断接口和测试，
        任何“热词删改后上界失效”都会在这里暴露。
        """
        violations: list[str] = []

        def postorder(n: RadixNode) -> tuple[float, Optional[RankTuple]]:
            own_score = float("-inf")
            own_best: Optional[tuple[float, tuple[str, str, str]]] = None
            for eid, (term, display, score) in n.terminals.items():
                key = canonical_key(eid, term, display)
                pair = (-score, key)
                if own_best is None or pair < own_best:
                    own_best = pair
                    own_score = score
            sub_max = own_score
            sub_best: Optional[RankTuple] = own_best
            for ch, (label, child) in n.edges.items():
                if child.parent is not n:
                    violations.append(f"node {n.nid}: 孩子 {child.nid} 的 parent 指针错误")
                if not label or child.edge_label != label:
                    violations.append(
                        f"node {n.nid}: 边标签不一致 map={label!r} child={child.edge_label!r}"
                    )
                if label[0] != ch:
                    violations.append(f"node {n.nid}: edges 键 {ch!r} 与边首字符不符")
                cmax, cbest = postorder(child)
                if cmax > sub_max:
                    sub_max = cmax
                if cbest is not None and (sub_best is None or cbest < sub_best):
                    sub_best = cbest
            # 压缩性检查（根豁免）
            if n is not self.root and not n.terminals and len(n.edges) == 1:
                violations.append(f"node {n.nid}: 存在未压缩的度-1无词条节点")
            if n.edges and (len({id(c) for _, c in n.edges.values()}) != len(n.edges)):
                violations.append(f"node {n.nid}: edges 中存在重复子节点")
            # 与缓存上界比对
            cached_max = n.max_score
            if cached_max != sub_max and not (
                cached_max == float("-inf") and sub_max == float("-inf")
            ):
                violations.append(
                    f"node {n.nid}: max_score 缓存 {cached_max!r} 与重算 {sub_max!r} 不一致"
                )
            cached_best = n.best
            if cached_best != sub_best:
                violations.append(
                    f"node {n.nid}: best 缓存 {cached_best!r} 与重算 {sub_best!r} 不一致"
                )
            return sub_max, sub_best

        postorder(self.root)
        if self.root.edge_label != "" or self.root.parent is not None:
            violations.append("root: 根节点 edge_label/parent 不为空")
        # entry_id -> 终止节点 索引一致性
        indexed: dict[str, RadixNode] = {}
        for n in self.walk_nodes():
            for eid in n.terminals:
                if eid in indexed:
                    violations.append(f"词条 {eid!r} 同时挂在多个终止节点")
                indexed[eid] = n
        for eid, n in self._terminal_node.items():
            if eid not in n.terminals:
                violations.append(f"_terminal_node[{eid!r}] 指向的节点已无该词条")
        if set(indexed) != set(self._terminal_node):
            missing = set(indexed) - set(self._terminal_node)
            stale = set(self._terminal_node) - set(indexed)
            violations.append(
                f"终止索引不一致 漏索引={sorted(missing)[:5]} 陈旧={sorted(stale)[:5]}"
            )
        return violations[:limit]

    # ----------------------------------------------------------------- top-k

    def top_k(
        self,
        prefix: str,
        k: int,
        trace: Optional[TopKTrace] = None,
    ) -> list[tuple[str, str, str, float]]:
        """精确 top-k 前缀补全。

        返回 ``[(term_norm, display, entry_id, score), ...]``，按
        分值降序、同分按稳定规范键（:func:`app.normalize.canonical_key`）升序。

        算法：best-first 分支限界。

        1. :meth:`locate` 定位前缀子树（O(前缀长度)，不碰其他词条）；
        2. 堆里放“子树根引用”，堆序为子树可靠上界 best（含规范键）；
        3. 每次弹出当前全局最优的子树：

           - 节点终止词条与当前第 k 名阈值比较（按完整排序元组，**精确**）；
           - 展开孩子入堆；入堆前若该孩子整棵子树的上界已严格弱于阈值，
             整枝剪枝（``child_best <= threshold``，因为最小堆语义
             ``-score`` 越大越差，等于阈值绝不剪 —— 同分保留）；
           - 堆顶若已不可能进入结果则终止。
        """
        import heapq

        matched, node = self.locate(prefix)
        if trace is not None:
            trace.prefix_norm = prefix
            trace.matched = matched
            trace.node_id = node.nid if matched else None
        if not matched:
            return []

        # 堆元素：(子树best, 种类, 节点)；种类 0=节点。best 是 (-score, key)。
        heap: list[tuple[RankTuple, int, RadixNode]] = []
        root_best = node.best
        if root_best is None:
            return []  # 空子树（理论上不会出现）
        heapq.heappush(heap, (root_best, 0, node))
        if trace is not None:
            trace.pushed += 1

        # 已入选的 k 个结果，最大堆语义用 sorted list 维护阈值；
        # 阈值即“当前第 k 名的排序元组下界”，用最小堆保存 -rank。
        results: list[tuple[RankTuple, str, str, str, float]] = []  # (rank, term,disp,eid,score)
        # threshold_rank: 当前第 k 名的 rank（最小堆顶）；rank 越小越优
        threshold_rank: Optional[RankTuple] = None

        def note(rec: PruneRecord) -> None:
            if trace is not None:
                trace.decisions.append(rec)

        while heap:
            bound, _kind, n = heapq.heappop(heap)
            if trace is not None:
                trace.heap_pops += 1
            # 堆按上界排序：若整棵子树的上界都已不优于阈值，则堆中剩余更差，结束。
            if threshold_rank is not None and bound >= threshold_rank:
                # bound == threshold 时仍需检查同分（key 可能更小）——
                # 但 bound 是“子树最优可能值”，bound >= threshold 意味着
                # 子树最优也严格差或完全相同；完全相同的排序元组不可能，
                # 因为 rank 含唯一 entry_id。故 >= 可安全终止。
                note(
                    PruneRecord(
                        n.nid, n.edge_label, -bound[0],
                        -threshold_rank[0] if threshold_rank else None,
                        "prune",
                        f"堆顶子树上界 {bound} 不优于阈值 {threshold_rank}（含唯一ID，无同分翻盘可能）",
                    )
                )
                if trace is not None:
                    trace.pruned_children += 1
                    # 堆中剩余子树的上界都 >= 当前堆顶，同样可整批剪枝；
                    # 逐条记录以便审计“每次剪枝的上界依据”。
                    for rem_bound, _rk, rem_node in heap:
                        trace.decisions.append(
                            PruneRecord(
                                rem_node.nid, rem_node.edge_label, -rem_bound[0],
                                -threshold_rank[0],
                                "prune",
                                f"堆内剩余子树 {rem_node.edge_label!r} 上界 {rem_bound}"
                                f" >= 阈值 {threshold_rank}，随堆顶终止一并整枝剪枝",
                            )
                        )
                        trace.pruned_children += 1
                break
            if trace is not None:
                trace.expanded += 1

            # 终止词条：逐个按完整 rank 精确比较
            for eid, (term, display, score) in n.terminals.items():
                if trace is not None:
                    trace.terminals_seen += 1
                key = canonical_key(eid, term, display)
                rank: RankTuple = (-score, key)
                if threshold_rank is not None and rank >= threshold_rank:
                    if trace is not None:
                        trace.terminal_skipped_by_bound += 1
                    note(
                        PruneRecord(
                            n.nid, n.edge_label, score,
                            -threshold_rank[0] if threshold_rank else None,
                            "prune",
                            f"词条 {eid!r} rank {rank} 不优于当前第k名阈值 {threshold_rank}",
                        )
                    )
                    continue
                note(
                    PruneRecord(
                        n.nid, n.edge_label, score,
                        -threshold_rank[0] if threshold_rank else None,
                        "expand",
                        f"词条 {eid!r} rank {rank} 入选候选",
                    )
                )
                results.append((rank, term, display, eid, score))
                results.sort(key=lambda r: r[0])
                if len(results) > k:
                    results.pop()
                if len(results) == k:
                    threshold_rank = results[-1][0]

            # 孩子入堆；入堆前按“整棵子树可靠上界”剪枝
            for label, child in n.edges.values():
                cb = child.best
                if cb is None:
                    continue
                if threshold_rank is not None and cb >= threshold_rank:
                    if trace is not None:
                        trace.pruned_children += 1
                    note(
                        PruneRecord(
                            child.nid, label, -cb[0],
                            -threshold_rank[0] if threshold_rank else None,
                            "prune",
                            f"子树 {label!r} 上界 rank {cb} 不优于阈值 {threshold_rank}；"
                            f"max_score={child.max_score} 为可靠上界，整枝不可能翻盘",
                        )
                    )
                    continue
                note(
                    PruneRecord(
                        child.nid, label, -cb[0],
                        -threshold_rank[0] if threshold_rank else None,
                        "expand",
                        f"子树 {label!r} 上界 rank {cb} 优于阈值，入堆展开",
                    )
                )
                heapq.heappush(heap, (cb, 0, child))
                if trace is not None:
                    trace.pushed += 1

        results.sort(key=lambda r: r[0])
        return [(term, display, eid, score) for _rank, term, display, eid, score in results]
