"""流式匹配器：自动机状态 + 流偏移的绑定。

状态合法性的关键约束：``state`` 节点号只有在与构建它的 ``Automaton`` 配对时
才有意义。因此状态不单独暴露为可序列化的“不透明串”随客户端漫游，而是由
服务端持久化，并在版本切换（显式边界）时重置为根节点 0 —— 自动机节点
绝不允许跨版本混用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .automaton import Automaton, ChunkResult, Hit


class StreamStateError(ValueError):
    """状态与自动机不配套等本地编程错误。"""


@dataclass(frozen=True)
class StreamSnapshot:
    state: int
    offset: int  # 已消费的总字节数（下一块的 base_offset）

    def to_dict(self) -> dict:
        return {"state": self.state, "offset": self.offset}


class StreamMatcher:
    """一个打开的字节流；跨块推进，版本切换时在显式边界重置。"""

    def __init__(self, automaton: Automaton, state: int = 0, offset: int = 0) -> None:
        if not (0 <= state < automaton.node_count):
            raise StreamStateError("初始状态节点不属于给定自动机")
        if offset < 0:
            raise StreamStateError("offset 不能为负")
        self._automaton = automaton
        self._state = state
        self._offset = offset

    @property
    def automaton(self) -> Automaton:
        return self._automaton

    @property
    def fingerprint(self) -> str:
        return self._automaton.fingerprint

    @property
    def state(self) -> int:
        return self._state

    @property
    def offset(self) -> int:
        return self._offset

    def snapshot(self) -> StreamSnapshot:
        return StreamSnapshot(self._state, self._offset)

    def feed(self, chunk: bytes | bytearray | memoryview) -> tuple[Hit, ...]:
        result: ChunkResult = self._automaton.feed(
            self._state, chunk, self._offset
        )
        self._state = result.state
        self._offset += result.length
        return result.hits

    def reset_at_boundary(self, automaton: Automaton) -> None:
        """显式版本边界：切换自动机并把节点重置为根。

        注意：流偏移（原始字节偏移）不归零 —— 切换前已消费的字节位置不变，
        切换后新自动机从根状态继续，但旧自动机的节点号一律丢弃，杜绝混用。
        跨边界本身不会产生任何命中（边界不插入字节）。
        """
        self._automaton = automaton
        self._state = 0
