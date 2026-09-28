"""压缩 Trie（Patricia/radix trie）索引与精确 top-k 查询。

不变量
------
1. 每个节点维护 ``subtree_max``：以该节点为根的整棵子树中，所有词条（含节点自身
   term 桶内）词频的最大值；空树为 ``None``。它是该子树中**任何**候选的可靠上界。
2. 边标签只在以下位置分裂：插入新键在标签中间产生分歧、删除后节点不再可达等。
3. 从根沿任意边序列拼出的字符串唯一；一个规范化键至多对应一个 term 桶节点。
4. 每次变更（增/删/改词频）后，受影响节点自底向上重算 ``subtree_max``，因此
   “热词降权/删除”后不会残留过期的高上界——剪枝依据始终成立。
   ``verify_integrity`` 会独立重算全部上界并逐节点比对，任何漂移都可被诊断抓到。

top-k
-----
维护大小为 k 的最小堆（最差候选在堆顶）。到达子树时若
``subtree_max`` 严格小于当前第 k 名分数，则整棵子树可安全剪枝（同分时不剪，
因为稳定规范键顺序下同分候选可能需要进入结果）。这是精确 top-k，不做近似。
每次剪枝都会在 trace 中记录 ``upper_bound`` 与 ``best_k_score``，便于核对依据。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterator, Literal


# ---------------------------------------------------------------------------
# 数据模型


@dataclass(frozen=True, slots=True)
class Entry:
    """一个词条。``key`` 是规范化键；显示原文 ``surface`` 原样保留。"""

    id: str
    surface: str
    key: str
    score: int


def entry_sort_tuple(e: Entry) -> tuple[int, str, str]:
    """稳定排序键：词频降序、规范化键升序、显示原文升序、id 升序。

    分数取负实现降序；其余字段升序。规范化碰撞（同 key）时由 surface、id
    提供全序，排序结果确定且可复现。
    """
    return (-e.score, e.key, e.surface, e.id)  # type: ignore[return-value]


@dataclass(slots=True)
class Edge:
    label: str
    target: "Node"
    seq: int  # 边插入序号：仅用于堆/遍历的确定性平局判定，不影响语义


@dataclass(slots=True)
class Node:
    seq: int
    depth: int = 0  # 从根到本节点拼出的规范化字符串长度
    edges: dict[str, Edge] = field(default_factory=dict)
    terms: list[Entry] = field(default_factory=list)
    subtree_max: int | None = None

    def recompute(self) -> tuple[bool, int | None]:
        """按 term 桶与直接孩子的上界重算本节点上界。返回 (是否变化, 新值)。"""
        m: int | None = max((t.score for t in self.terms), default=None)
        for edge in self.edges.values():
            c = edge.target.subtree_max
            if c is not None and (m is None or c > m):
                m = c
        changed = self.subtree_max != m
        self.subtree_max = m
        return changed, m


# ---------------------------------------------------------------------------
# 前缀定位结果


@dataclass(frozen=True, slots=True)
class PrefixLocation:
    kind: Literal["at_node", "inside_edge", "missing"]
    node: Node | None  # at_node: 对应节点；inside_edge: 该边目标子树；missing: None
    matched: int  # 已匹配的前缀长度
    prefix: str
    node_key: str | None  # 定位到的节点对应的完整规范化串（trace 用）
    edge_seq: int | None = None


# ---------------------------------------------------------------------------
# trace 结构


@dataclass(frozen=True, slots=True)
class PruneReason:
    upper_bound: int
    best_k_score: int  # 堆中第 k 名分数；不足 k 个时为 -1（永不剪枝）


@dataclass(frozen=True, slots=True)
class TraceEvent:
    step: int
    event: Literal["visit", "prune", "emit", "replace_worst", "prefix_missing"]
    node_seq: int | None
    edge_seq: int | None
    edge_label: str | None
    prefix: str
    upper_bound: int | None
    detail: str


@dataclass(slots=True)
class TopKStats:
    nodes_visited: int = 0
    subtrees_pruned: int = 0
    entries_seen: int = 0
    frontier_expansions: int = 0
    total_nodes: int = 0
    items_evicted: int = 0


@dataclass(slots=True)
class TopKTrace:
    prefix: str
    normalized_prefix: str
    k: int
    location_kind: str
    events: list[TraceEvent] = field(default_factory=list)
    prunes: list[tuple[str, PruneReason]] = field(default_factory=list)  # (子树前缀, 依据)
    stats: TopKStats = field(default_factory=TopKStats)
    prune_safe: bool = True


# 堆元素的排序方向必须与结果排序方向相反：
# 结果排序是 (分数降序, 规范化键升序, 原文升序, id 升序)，所以“最差候选”
# （应位于堆顶、最先被挤出）的定义是：分数更低；同分时规范键/原文/id 更大。
# 字符串无法取负，因此用自定义 __lt__ 的包装类实现“更差”全序。
class _HeapNode:
    __slots__ = ("entry",)

    def __init__(self, entry: Entry):
        self.entry = entry

    def __lt__(self, other: "_HeapNode") -> bool:
        # “更差”（排在堆顶）：分数低；同分时规范键/原文/id 字典序更大。
        a, b = self.entry, other.entry
        if a.score != b.score:
            return a.score < b.score
        return (a.key, a.surface, a.id) > (b.key, b.surface, b.id)


def _heap_less(worst: _HeapNode, candidate: Entry) -> bool:
    """候选是否严格优于堆顶最差候选（若是则应替换）。"""
    w = worst.entry
    if candidate.score != w.score:
        return candidate.score > w.score
    return (candidate.key, candidate.surface, candidate.id) < (w.key, w.surface, w.id)


# ---------------------------------------------------------------------------
# 压缩 Trie


class CompressedTrie:
    def __init__(self) -> None:
        self.root = Node(seq=0, depth=0)
        self._node_count = 1
        self._edge_seq = 0

    # ---- 基础读取 --------------------------------------------------------

    def count_nodes(self) -> int:
        return self._node_count

    def __len__(self) -> int:
        return self._count_terms(self.root)

    @staticmethod
    def _count_terms(node: Node) -> int:
        n = len(node.terms)
        for edge in node.edges.values():
            n += CompressedTrie._count_terms(edge.target)
        return n

    def get(self, key: str) -> list[Entry]:
        node = self._find_exact(key)
        return list(node.terms) if node is not None else []

    def _find_exact(self, key: str) -> Node | None:
        node = self.root
        i = 0
        n = len(key)
        while i < n:
            edge = node.edges.get(key[i])
            if edge is None:
                return None
            label = edge.label
            seg = key[i : i + len(label)]
            if seg != label:
                return None
            i += len(label)
            node = edge.target
        return node

    def __contains__(self, key: str) -> bool:
        node = self._find_exact(key)
        return node is not None and bool(node.terms)

    # ---- 变更 ------------------------------------------------------------

    def upsert(self, entry: Entry) -> None:
        """插入或整体替换一个 id 的词条。

        若该 id 原属另一个规范化键，先从旧桶摘除再放置新键（例如原文从
        ``"ABC"`` 改成 ``"xyz"``）。
        """
        old = self._find_by_id(entry.id)
        if old is not None:
            old_node, old_entry = old
            if old_entry.key == entry.key:
                # 同桶原地替换：沿祖先链自底向上重算上界。
                idx = next(i for i, t in enumerate(old_node.terms) if t.id == entry.id)
                old_node.terms[idx] = entry
                path = self._walk_path(entry.key)
                assert path and path[-1][0] is old_node
                for nd, _ed in reversed(path):
                    nd.recompute()
                return
            self._detach(old_entry)
        self._place(entry)

    def delete(self, entry_id: str) -> bool:
        found = self._find_by_id(entry_id)
        if found is None:
            return False
        node, entry = found
        key = entry.key
        path = self._walk_path(key)
        node.terms = [t for t in node.terms if t.id != entry_id]

        # 压缩：删除 term 后，若非根的内部节点既无 term 又只有一个孩子，
        # 与唯一孩子合并；空叶子摘除。自底向上处理。
        i = len(path) - 1
        while i > 0:
            nd = path[i][0]
            if nd.terms:
                break
            if not nd.edges:
                parent = path[i - 1][0]
                edge_to_me = path[i][1]
                assert edge_to_me is not None
                del parent.edges[edge_to_me.label[0]]
                i -= 1
                continue
            if len(nd.edges) == 1:
                only = next(iter(nd.edges.values()))
                parent = path[i - 1][0]
                edge_to_me = path[i][1]
                assert edge_to_me is not None
                # 合并标签：把“父->我”和“我->唯一孩子”合成一条边。
                merged_label = edge_to_me.label + only.label
                merged = Edge(label=merged_label, target=only.target, seq=edge_to_me.seq)
                only.target.depth = parent.depth + len(merged_label)
                parent.edges[merged_label[0]] = merged
                i -= 1
                continue
            break
        # 受影响的祖先链（含根）自底向上重算。
        for nd, _ed in reversed(path[: i + 1]):
            nd.recompute()
        return True

    def _find_by_id(self, entry_id: str) -> tuple[Node, Entry] | None:
        stack: list[Node] = [self.root]
        while stack:
            node = stack.pop()
            for t in node.terms:
                if t.id == entry_id:
                    return node, t
            for edge in node.edges.values():
                stack.append(edge.target)
        return None

    def _walk_path(self, key: str) -> list[tuple[Node, Edge | None]]:
        """返回 (根, None) ... (键对应节点, 入边) 的路径。键须存在。"""
        path: list[tuple[Node, Edge | None]] = [(self.root, None)]
        node = self.root
        i = 0
        while i < len(key):
            edge = node.edges[key[i]]
            path.append((edge.target, edge))
            i += len(edge.label)
            node = edge.target
        return path

    def _detach(self, entry: Entry) -> None:
        """从旧键桶摘除一个词条，随后做压缩清理与上界重算。"""
        node = self._find_exact(entry.key)
        assert node is not None
        node.terms = [t for t in node.terms if t.id != entry.id]
        key = entry.key
        path = self._walk_path(key)
        i = len(path) - 1
        while i > 0:
            nd = path[i][0]
            if nd.terms:
                break
            if not nd.edges:
                parent = path[i - 1][0]
                edge_to_me = path[i][1]
                assert edge_to_me is not None
                del parent.edges[edge_to_me.label[0]]
                i -= 1
                continue
            if len(nd.edges) == 1:
                only = next(iter(nd.edges.values()))
                parent = path[i - 1][0]
                edge_to_me = path[i][1]
                assert edge_to_me is not None
                merged_label = edge_to_me.label + only.label
                merged = Edge(label=merged_label, target=only.target, seq=edge_to_me.seq)
                only.target.depth = parent.depth + len(merged_label)
                parent.edges[merged_label[0]] = merged
                i -= 1
                continue
            break
        for nd, _ed in reversed(path[: i + 1]):
            nd.recompute()

    def _new_edge_seq(self) -> int:
        self._edge_seq += 1
        return self._edge_seq

    def _place(self, entry: Entry) -> None:
        """把词条放入其规范化键对应的节点（必要时新建/分裂边）。

        ``path`` 记录下行进过的 (节点, 入边)；任何结构改动后沿 path 自底向上
        重算上界，保证祖先的 ``subtree_max`` 立即反映新分数（增权或降权同理）。
        """
        key = entry.key
        node = self.root
        i = 0
        n = len(key)
        path: list[tuple[Node, Edge | None]] = [(self.root, None)]
        while i < n:
            edge = node.edges.get(key[i])
            if edge is None:
                # 新叶子。
                leaf = Node(seq=self._node_count, depth=n)
                self._node_count += 1
                leaf.terms.append(entry)
                leaf.subtree_max = entry.score
                node.edges[key[i]] = Edge(
                    label=key[i:], target=leaf, seq=self._new_edge_seq()
                )
                for nd, _ed in reversed(path):
                    nd.recompute()
                return
            label = edge.label
            end = min(i + len(label), n)
            j = i
            while j < end and key[j] == label[j - i]:
                j += 1
            if j == i + len(label):
                # 整条标签匹配，进入孩子继续。
                i = j
                node = edge.target
                path.append((node, edge))
                continue
            # 部分匹配 -> 在 j 处分裂。
            prefix_label = label[: j - i]
            suffix_old = label[j - i :]
            old_child = edge.target
            mid = Node(seq=self._node_count, depth=node.depth + len(prefix_label))
            self._node_count += 1
            # 旧孩子改挂在 mid 下，标签去掉公共前缀。
            old_child.depth = mid.depth + len(suffix_old)
            mid.edges[suffix_old[0]] = Edge(
                label=suffix_old, target=old_child, seq=edge.seq
            )
            node.edges[prefix_label[0]] = Edge(
                label=prefix_label, target=mid, seq=self._new_edge_seq()
            )
            if j == n:
                # 新键正好结束在分裂点：mid 成为 term 节点。
                mid.terms.append(entry)
            else:
                # 新键还有后缀：为它新建叶子。
                suffix_new = key[j:]
                leaf = Node(seq=self._node_count, depth=n)
                self._node_count += 1
                leaf.terms.append(entry)
                leaf.subtree_max = entry.score
                mid.edges[suffix_new[0]] = Edge(
                    label=suffix_new, target=leaf, seq=self._new_edge_seq()
                )
            # mid 自身（新结构下）与 path 上所有祖先自底向上重算。
            mid.recompute()
            for nd, _ed in reversed(path):
                nd.recompute()
            return
        # 落在已有节点（键是现有前缀，且该节点目前可能是纯内部节点）。
        node.terms.append(entry)
        for nd, _ed in reversed(path):
            nd.recompute()

    # ---- 前缀定位 --------------------------------------------------------

    def locate_prefix(self, prefix: str) -> PrefixLocation:
        """定位规范化前缀。

        返回三种状态：
        - ``at_node``：前缀恰好结束在某节点（空前缀落在根节点）；
        - ``inside_edge``：前缀结束在一条压缩边标签的内部，候选来自该边目标子树；
        - ``missing``：前缀与任何键都不共享此后的路径，候选为空。
        ``node_key`` 记录定位节点对应的完整串，供 trace 展示真实子树范围。
        """
        node = self.root
        i = 0
        n = len(prefix)
        node_key = ""
        last_edge: Edge | None = None
        while i < n:
            edge = node.edges.get(prefix[i])
            if edge is None:
                return PrefixLocation(
                    "missing", None, i, prefix, None,
                    last_edge.seq if last_edge else None,
                )
            label = edge.label
            j = i
            end = min(i + len(label), n)
            while j < end and prefix[j] == label[j - i]:
                j += 1
            if j < i + len(label):
                if j < n:
                    # 标签中间出现分歧：前缀不在 trie 中。
                    return PrefixLocation(
                        "missing", None, j, prefix, None, edge.seq
                    )
                # 前缀结束在一条边标签的内部：候选子树是该边目标。
                return PrefixLocation(
                    "inside_edge", edge.target, n, prefix, node_key + label, edge.seq
                )
            i = j
            node = edge.target
            node_key += label
            last_edge = edge
        return PrefixLocation(
            "at_node", node, n, prefix, node_key,
            last_edge.seq if last_edge else None,
        )

    # ---- 精确 top-k ------------------------------------------------------

    def top_k(
        self,
        prefix: str,
        k: int,
        *,
        collect_trace: bool = False,
    ) -> tuple[list[Entry], TopKTrace]:
        if k < 1:
            raise ValueError("k must be >= 1")
        trace = TopKTrace(
            prefix=prefix,
            normalized_prefix=prefix,
            k=k,
            location_kind="",
        )
        trace.stats.total_nodes = self._node_count

        loc = self.locate_prefix(prefix)
        trace.location_kind = loc.kind

        def record(
            event: str,
            nd: Node | None,
            ed: Edge | None,
            pfx: str,
            bound: int | None,
            detail: str,
        ) -> None:
            if collect_trace:
                trace.events.append(
                    TraceEvent(
                        step=len(trace.events),
                        event=event,  # type: ignore[arg-type]
                        node_seq=nd.seq if nd is not None else None,
                        edge_seq=ed.seq if ed is not None else (loc.edge_seq if event == "prefix_missing" else None),
                        edge_label=ed.label if ed is not None else None,
                        prefix=pfx,
                        upper_bound=bound,
                        detail=detail,
                    )
                )

        if loc.kind == "missing":
            record(
                "prefix_missing",
                None,
                None,
                prefix[: loc.matched],
                None,
                f"前缀在第 {loc.matched} 个字符后无共享边，零候选",
            )
            trace.stats.total_nodes = self._node_count
            return [], trace

        # _HeapNode 最小堆：堆顶始终是当前 top-k 集合里的“最差候选”。
        heap: list[_HeapNode] = []
        # frontier: (负上界, 节点seq, 边seq, 子树前缀, node)
        # 用 -subtree_max 让“上界最大”的子树先被展开（best-first），尽快拿到紧上界。
        # 子树前缀是“定位节点自身对应的完整串”：inside_edge 时它比查询前缀长，
        # 从该节点向下逐边拼 label 即可还原候选键。
        frontier: list[tuple[int, int, int, str, Node]] = []
        start = loc.node
        assert start is not None
        start_prefix = loc.node_key if loc.node_key is not None else prefix
        if start.subtree_max is not None:
            heapq.heappush(frontier, (-start.subtree_max, start.seq, 0, start_prefix, start))

        while frontier:
            neg_bound, _nseq, _eseq, pfx, node = heapq.heappop(frontier)
            bound = -neg_bound
            # 剪枝判定：堆已满，且该子树上界严格小于第 k 名分数。
            # 等于时绝不剪（同分要按稳定规范键排序，可能需要进入结果）。
            if len(heap) == k:
                best_k_score = heap[0].entry.score
                if bound < best_k_score:
                    trace.stats.subtrees_pruned += 1
                    reason = PruneReason(
                        upper_bound=bound,
                        best_k_score=best_k_score,
                    )
                    trace.prunes.append((pfx, reason))
                    record(
                        "prune",
                        node,
                        None,
                        pfx,
                        bound,
                        f"子树上界 {bound} < 当前第 {k} 名分数 {best_k_score}，整棵子树无候选可进入 top-{k}",
                    )
                    continue
            trace.stats.nodes_visited += 1
            trace.stats.frontier_expansions += 1
            record(
                "visit",
                node,
                None,
                pfx,
                bound,
                f"访问节点 seq={node.seq}，子树上界 {bound}，桶内 {len(node.terms)} 词",
            )
            # term 桶：桶内是同 key 碰撞词，必须全部按稳定键比较。
            for entry in node.terms:
                trace.stats.entries_seen += 1
                if len(heap) < k:
                    heapq.heappush(heap, _HeapNode(entry))
                    record(
                        "emit",
                        node,
                        None,
                        pfx,
                        bound,
                        f"候选 {entry.id!r}({entry.surface!r}, score={entry.score}) 入堆",
                    )
                elif _heap_less(heap[0], entry):
                    # 候选严格优于当前最差候选（同分按稳定规范键比较）才替换。
                    evicted = heapq.heapreplace(heap, _HeapNode(entry)).entry
                    trace.stats.items_evicted += 1
                    record(
                        "replace_worst",
                        node,
                        None,
                        pfx,
                        bound,
                        f"候选 {entry.id!r}(score={entry.score}) 替换最差候选 "
                        f"{evicted.id!r}(score={evicted.score})",
                    )
                else:
                    record(
                        "emit",
                        node,
                        None,
                        pfx,
                        bound,
                        f"候选 {entry.id!r}(score={entry.score}) 不优于当前最差候选，丢弃",
                    )
            # 孩子按确定顺序入队（首字符 + 边 seq 提供全序）。
            children = sorted(
                node.edges.values(), key=lambda e: (e.label[0], e.seq)
            )
            for edge in children:
                child = edge.target
                if child.subtree_max is None:
                    continue
                child_prefix = pfx + edge.label
                heapq.heappush(
                    frontier,
                    (
                        -child.subtree_max,
                        child.seq,
                        edge.seq,
                        child_prefix,
                        child,
                    ),
                )

        # heap 中的最终候选按稳定规范键全序输出（分数降序，其余升序）。
        ordered = sorted((node.entry for node in heap), key=entry_sort_tuple)
        trace.stats.total_nodes = self._node_count
        return ordered, trace

    # ---- 诊断：上界/结构完整性 -------------------------------------------

    def verify_integrity(self) -> list[dict[str, object]]:
        """独立遍历整棵 trie，重算全部上界并检查结构不变量。

        返回违规列表（空列表表示通过）。绝不返回布尔“假成功”：调用方可看到
        每条违规的节点、期望上界与实际值。
        """
        violations: list[dict[str, object]] = []
        # DFS，携带从根拼出的实际前缀，用它校验节点 depth 与标签拼接。
        stack: list[tuple[Node, str, Edge | None, Node | None]] = [
            (self.root, "", None, None)
        ]
        seen_seqs: set[int] = set()
        while stack:
            node, pfx, incoming, parent = stack.pop()
            if node.seq in seen_seqs:
                violations.append(
                    {
                        "type": "node_reachable_twice",
                        "node_seq": node.seq,
                        "prefix": pfx,
                    }
                )
                continue
            seen_seqs.add(node.seq)
            if node.depth != len(pfx):
                violations.append(
                    {
                        "type": "depth_mismatch",
                        "node_seq": node.seq,
                        "prefix": pfx,
                        "expected_depth": len(pfx),
                        "actual_depth": node.depth,
                    }
                )
            if incoming is not None:
                # 入边标签必须是 parent 前缀到本节点前缀之间的差。
                assert parent is not None
                parent_prefix = pfx[: -len(incoming.label)] if incoming.label else pfx
                if parent_prefix + incoming.label != pfx:
                    violations.append(
                        {
                            "type": "edge_label_mismatch",
                            "node_seq": node.seq,
                            "prefix": pfx,
                            "edge_label": incoming.label,
                        }
                    )
                # 压缩不变量：非根、非 term 的内部节点至少有两个孩子
                # （单孩子节点必须已与其孩子合并）。
                if not node.terms and node is not self.root:
                    if len(node.edges) < 2:
                        violations.append(
                            {
                                "type": "uncompressed_unary_node",
                                "node_seq": node.seq,
                                "prefix": pfx,
                                "children": len(node.edges),
                            }
                        )
            # 同桶内 key 必须相同，id 必须唯一。
            bucket_ids: set[str] = set()
            for t in node.terms:
                if t.key != pfx:
                    violations.append(
                        {
                            "type": "term_key_mismatch",
                            "node_seq": node.seq,
                            "node_prefix": pfx,
                            "entry_id": t.id,
                            "entry_key": t.key,
                        }
                    )
                if t.id in bucket_ids:
                    violations.append(
                        {
                            "type": "duplicate_id_in_bucket",
                            "node_seq": node.seq,
                            "entry_id": t.id,
                        }
                    )
                bucket_ids.add(t.id)
            # 重算上界。
            expected_max: int | None = max(
                (t.score for t in node.terms), default=None
            )
            for edge in node.edges.values():
                c = edge.target.subtree_max
                if c is not None and (expected_max is None or c > expected_max):
                    expected_max = c
            if expected_max != node.subtree_max:
                violations.append(
                    {
                        "type": "stale_subtree_max",
                        "node_seq": node.seq,
                        "prefix": pfx,
                        "expected_max": expected_max,
                        "actual_max": node.subtree_max,
                    }
                )
            for edge in node.edges.values():
                if not edge.label:
                    violations.append(
                        {
                            "type": "empty_edge_label",
                            "node_seq": node.seq,
                            "prefix": pfx,
                        }
                    )
                    continue
                if node.edges.get(edge.label[0]) is not edge:
                    violations.append(
                        {
                            "type": "edge_index_mismatch",
                            "node_seq": node.seq,
                            "edge_label": edge.label,
                        }
                    )
                stack.append((edge.target, pfx + edge.label, edge, node))
        return violations

    # ---- 调试遍历 --------------------------------------------------------

    def iter_entries(self) -> Iterator[Entry]:
        stack: list[Node] = [self.root]
        while stack:
            node = stack.pop()
            for t in node.terms:
                yield t
            for edge in sorted(node.edges.values(), key=lambda e: e.seq, reverse=True):
                stack.append(edge.target)
