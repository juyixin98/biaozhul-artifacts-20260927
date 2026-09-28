"""Aho-Corasick 自动机（字节级）。

算法要点：
- 字符集为完整字节域 0..255，模式与文本均为 ``bytes``，位置是原始字节偏移；
- 构建期 BFS 计算失败指针（failure link），并把终止词沿失败指针汇总为
  ``output`` 列表（字典后缀链），因此后缀模式不会漏报；
- ``feed`` 按块增量推进，不要求块在任何边界对齐，跨块命中位置仍按流的
  原始字节偏移报告；
- 自动机一旦构建即为不可变，状态仅用一个整数节点号表示，禁止跨版本混用
  （节点号只对构建它的那棵 trie 有意义）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ApiError, ErrorCode


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    data: bytes


@dataclass(frozen=True)
class Hit:
    """单次命中。[start, end) 为半开区间，单位是流内的原始字节偏移。"""

    pattern_id: str
    start: int
    end: int


@dataclass(frozen=True)
class ChunkResult:
    hits: tuple[Hit, ...]
    state: int
    base_offset: int  # 喂入前已消费字节数
    length: int  # 本块长度


class AutomatonBuildError(ApiError):
    pass


class Automaton:
    """不可变的 Aho-Corasick 自动机。

    内部节点结构（构建后不再变更）：
        _go[u][c]    : 转移目标（完整转移表，查询 O(1)）
        _fail[u]     : 失败指针
        _output[u]   : 该节点输出的全部 (pattern_id, length)，
                       含通过失败链汇总的后缀模式
    """

    def __init__(self, patterns: list[Pattern] | tuple[Pattern, ...]) -> None:
        if len(patterns) == 0:
            raise AutomatonBuildError(
                ErrorCode.EMPTY_PATTERN_SET,
                422,
                "模式集合为空：空集合的自动机对任何输入都没有确定的匹配语义，拒绝",
            )
        seen_ids: set[str] = set()
        for p in patterns:
            if not isinstance(p.data, (bytes, bytearray, memoryview)):
                raise TypeError("pattern data must be bytes-like")
            if len(p.data) == 0:
                raise AutomatonBuildError(
                    ErrorCode.EMPTY_PATTERN,
                    422,
                    f"pattern {p.pattern_id!r} 为空模式，空模式无法定义匹配位置，拒绝",
                    details={"pattern_id": p.pattern_id},
                )
            if p.pattern_id in seen_ids:
                raise AutomatonBuildError(
                    ErrorCode.DUPLICATE_PATTERN_ID,
                    422,
                    f"pattern_id {p.pattern_id!r} 在同一模式集合中重复",
                    details={"pattern_id": p.pattern_id},
                )
            seen_ids.add(p.pattern_id)

        # 规范化顺序，保证同一模式多重集构建出的节点布局/指纹完全一致。
        ordered = sorted(patterns, key=lambda p: (p.data, p.pattern_id))

        # 节点 0 为根。
        self._go: list[dict[int, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[list[tuple[str, int]]] = [[]]

        for pat in ordered:
            u = 0
            for byte in pat.data:
                nxt = self._go[u].get(byte)
                if nxt is None:
                    nxt = len(self._go)
                    self._go[u][byte] = nxt
                    self._go.append({})
                    self._fail.append(0)
                    self._output.append([])
                u = nxt
            self._output[u].append((pat.pattern_id, len(pat.data)))

        self._build_failure_links()
        self._pattern_count = len(seen_ids)
        # 指纹：对规范化后的模式行做 SHA-256，用于检测版本与状态是否属于同一自动机。
        self._fingerprint = self._compute_fingerprint(ordered)

    # ------------------------------------------------------------------ 构建

    def _build_failure_links(self) -> None:
        """BFS 补全失败指针、完整转移表与输出链。

        转移表补全规则（标准 AC 的“确定化”形式）：
            根的缺失边回到根；
            非根节点 u 缺边时 go[u][c] = go[fail[u]][c]（构建期 BFS 时父链已确定）。
        输出链：output[u] 自身终止词 + output[fail[u]] 的全部后缀终止词。
        """
        from collections import deque

        queue: deque[int] = deque()

        for byte in range(256):
            child = self._go[0].get(byte)
            if child is not None:
                self._fail[child] = 0
                queue.append(child)
            else:
                self._go[0][byte] = 0

        while queue:
            u = queue.popleft()
            f = self._fail[u]
            # 后缀输出链：失败节点的输出（已含它的整条失败链输出）直接并入。
            self._output[u].extend(self._output[f])
            # 同长模式之间的顺序按 (data, id) 构建顺序稳定，保证结果可复现。
            self._output[u].sort(key=lambda t: (-t[1], t[0]))

            for byte in range(256):
                child = self._go[u].get(byte)
                if child is not None:
                    self._fail[child] = self._go[f][byte]
                    queue.append(child)
                else:
                    self._go[u][byte] = self._go[f][byte]

    @staticmethod
    def _compute_fingerprint(ordered: list[Pattern]) -> str:
        import hashlib

        h = hashlib.sha256()
        for pat in ordered:
            # 长度前缀避免分隔符歧义；hex 编码是确定性的字节表示。
            h.update(len(pat.data).to_bytes(8, "big"))
            h.update(pat.data)
            h.update(b"\x00")
            h.update(pat.pattern_id.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    # ------------------------------------------------------------------ 属性

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def pattern_count(self) -> int:
        return self._pattern_count

    @property
    def node_count(self) -> int:
        return len(self._go)

    # ------------------------------------------------------------------ 查询

    def transition(self, state: int, byte: int) -> int:
        return self._go[state][byte]

    def output(self, state: int) -> tuple[tuple[str, int], ...]:
        """节点 state 的全部输出（pattern_id, 模式长度），含后缀链。"""
        return tuple(self._output[state])

    def search(self, data: bytes | bytearray | memoryview) -> tuple[Hit, ...]:
        """对单块完整文本匹配（便捷方法，等价于从根开始的一次 feed）。"""
        state = 0
        hits: list[Hit] = []
        for index, byte in enumerate(data):
            state = self._go[state][byte]
            end = index + 1
            for pattern_id, length in self._output[state]:
                hits.append(Hit(pattern_id, end - length, end))
        return tuple(hits)

    def feed(
        self,
        state: int,
        data: bytes | bytearray | memoryview,
        base_offset: int,
    ) -> ChunkResult:
        """从已有状态继续匹配一块数据。

        ``base_offset`` 是本块第一个字节在整个流中的原始字节偏移
        （= 之前所有块的长度之和）。跨块命中由状态自动衔接，
        命中位置一律换算为绝对字节偏移。
        """
        if not (0 <= state < len(self._go)):
            raise ValueError(f"state 节点号 {state} 不属于本自动机")
        if base_offset < 0:
            raise ValueError("base_offset 不能为负")

        hits: list[Hit] = []
        u = state
        for rel, byte in enumerate(data):
            u = self._go[u][byte]
            end = base_offset + rel + 1
            for pattern_id, length in self._output[u]:
                hits.append(Hit(pattern_id, end - length, end))
        return ChunkResult(tuple(hits), u, base_offset, len(data))
