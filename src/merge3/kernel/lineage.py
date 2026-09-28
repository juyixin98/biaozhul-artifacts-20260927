"""快照血缘：父引用 DAG 上的共同祖先（LCA）与可达性。

提交后保留两条父引用（开发头与主头），因此快照历史是 DAG。
自动找共同祖先时：取两个头（含自身）祖先集合的交集，再剔除"是另一个
共同祖先的真祖先"的节点，剩下的即最低共同祖先；若存在多个互不可达的 LCA
（交织历史），显式报错要求调用方指定 base——不允许在祖先不唯一时静默猜测。
"""
from __future__ import annotations

from collections import deque
from typing import Mapping

from ..errors import NotFoundError, ValidationError


def ancestors_of(snapshot_id: str, parents: Mapping[str, list[str]]) -> set[str]:
    """返回包含自身的全部祖先（沿父 DAG BFS）。"""
    seen: set[str] = set()
    dq: deque[str] = deque([snapshot_id])
    while dq:
        cur = dq.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        dq.extend(parents.get(cur, ()))
    return seen


def find_lca(
    ours_id: str,
    theirs_id: str,
    parents: Mapping[str, list[str]],
) -> str:
    if ours_id not in parents:
        raise NotFoundError(f"快照不存在或无血缘记录: {ours_id}")
    if theirs_id not in parents:
        raise NotFoundError(f"快照不存在或无血缘记录: {theirs_id}")

    a_ours = ancestors_of(ours_id, parents)
    a_theirs = ancestors_of(theirs_id, parents)
    common = a_ours & a_theirs
    if not common:
        raise ValidationError(
            f"快照 {ours_id} 与 {theirs_id} 没有共同祖先；"
            "请用 base_snapshot_id 显式指定"
        )

    # 最低候选：不是任何其他共同祖先的真祖先。
    lowest = [
        c
        for c in common
        if not any(c != o and c in ancestors_of(o, parents) for o in common)
    ]
    if len(lowest) > 1:
        raise ValidationError(
            f"存在多个最低共同祖先 {sorted(lowest)}；请用 base_snapshot_id 显式指定"
        )
    return lowest[0]


def is_ancestor(maybe_ancestor: str, snapshot_id: str, parents: Mapping[str, list[str]]) -> bool:
    """maybe_ancestor 是否可沿父边到达 snapshot_id（含相等）。"""
    return maybe_ancestor in ancestors_of(snapshot_id, parents)
