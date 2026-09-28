"""独立参考实现：在“具体字符串”状态空间上跑 Dijkstra 最短路。

这个 oracle 完全独立于 app.editdistance：
- 状态是真实的字符串（tuple[character, ...]），不是 DP 表项；
- 边是 insert/delete/substitute/swap 四种具体操作，按 CostProfile 计价；
- 用 heapq Dijkstra 求全局最短路径。

仅适合短字符串（状态数随长度与字母表增长），这正是单元/对拍测试的场景。
参考答案不经过被测核心生成，满足“不能全部由被测核心实现自身生成”。
"""
from __future__ import annotations

import heapq
from typing import Optional

from app.costs import CostProfile
from app.editdistance import EPS, Move, replay, rescore_moves


def _neighbors(state: tuple[str, ...], alphabet: tuple[str, ...]):
    """生成 (new_state, move)。

    - insert：在每个间隙插入字母表中的字符；
    - delete：删除每个位置；
    - substitute：每个位置替换为不同字符；
    - swap：相邻交换（仅当两字符不同，相同交换无意义）。
    """
    n = len(state)
    for pos in range(n + 1):
        for ch in alphabet:
            yield state[:pos] + (ch,) + state[pos:], Move("insert", pos, ch)
    for pos in range(n):
        yield state[:pos] + state[pos + 1:], Move("delete", pos, state[pos])
    for pos in range(n):
        for ch in alphabet:
            if ch != state[pos]:
                yield state[:pos] + (ch,) + state[pos + 1:], Move(
                    "substitute", pos, ch
                )
    for pos in range(n - 1):
        if state[pos] != state[pos + 1]:
            a, b = state[pos], state[pos + 1]
            new = list(state)
            new[pos], new[pos + 1] = b, a
            yield tuple(new), Move("swap", pos, detail=f"{a}{b}<->{b}{a}")


def shortest_path(
    source: str,
    target: str,
    profile: CostProfile,
    *,
    max_states: int = 200_000,
    extra_alphabet: tuple[str, ...] = (),
) -> tuple[float, Optional[tuple[Move, ...]]]:
    """返回 (最短距离, 路径)。不可达返回 (inf, None)。"""
    start = tuple(source)
    goal = tuple(target)
    alphabet = tuple(sorted(set(source) | set(target) | set(extra_alphabet)))

    dist: dict[tuple[str, ...], float] = {start: 0.0}
    prev: dict[tuple[str, ...], tuple[tuple[str, ...], Move]] = {}
    pq: list[tuple[float, int, tuple[str, ...]]] = [(0.0, 0, start)]
    counter = 1
    settled = 0

    while pq:
        d, _, state = heapq.heappop(pq)
        if d > dist.get(state, float("inf")) + EPS:
            continue
        settled += 1
        if settled > max_states:
            raise RuntimeError(
                f"oracle 状态数超过 {max_states}（source={source!r}, target={target!r}）"
            )
        if state == goal:
            # 回溯
            path: list[Move] = []
            cur = goal
            while cur != start:
                parent, mv = prev[cur]
                path.append(mv)
                cur = parent
            path.reverse()
            return d, tuple(path)

        for nxt, mv in _neighbors(state, alphabet):
            if mv.type == "insert":
                w = profile.insert
            elif mv.type == "delete":
                w = profile.delete
            elif mv.type == "substitute":
                w = profile.sub_cost(state[mv.index], mv.char)
            else:
                w = profile.transpose
            nd = d + w
            if nd + EPS < dist.get(nxt, float("inf")):
                dist[nxt] = nd
                prev[nxt] = (state, mv)
                heapq.heappush(pq, (nd, counter, nxt))
                counter += 1

    return float("inf"), None


def assert_path_valid(source: str, target: str, path: tuple[Move, ...],
                      profile: CostProfile, claimed: float) -> None:
    """供测试使用：重放路径、独立重算成本，并与声称距离比对。"""
    assert replay(source, path) == target, "oracle 路径重放失败"
    rescored = rescore_moves(source, path, profile)
    assert abs(rescored - claimed) <= EPS, (
        f"oracle 路径重算成本 {rescored} 与距离 {claimed} 不一致"
    )
