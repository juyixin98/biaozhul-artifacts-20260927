"""计划的流式应用。

应用语义
========
* 计划只绑定 **源摘要**（sha256 + 长度）与源版本；应用前重新计算摘要，
  不一致即 :class:`SourceVersionMismatchError`，绝不基于错误文本应用。
* 输出 = 按已选替换有序拼接：原文片段 → 替换文本 → 原文片段 ……。
  替换文本来自计划本身，因此 **不会被再次匹配**（规划是单轮的）。
* 流式产出：生成器按 ``apply_chunk_chars`` 码点攒块，调用方可直接转发到
  HTTP 响应或落盘；调用方负责最后做一次输出摘要核对（存储层会做）。
"""
from __future__ import annotations

from typing import Iterator

from .errors import SourceVersionMismatchError
from .planning import Plan
from .textutil import make_spec


def verify_source(text: str, plan: Plan, source_version: int, expected_version: int) -> None:
    """应用前守卫：版本号与源摘要必须同时匹配。"""
    if source_version != expected_version:
        raise SourceVersionMismatchError(
            "source version does not match the version the plan was built against",
            details={
                "current_version": source_version,
                "plan_bound_version": expected_version,
            },
        )
    current = make_spec(text)
    if current.as_tuple() != plan.source_spec.as_tuple():
        raise SourceVersionMismatchError(
            "source digest does not match the plan binding",
            details={
                "current_sha256": current.sha256,
                "bound_sha256": plan.source_spec.sha256,
            },
        )


def apply_plan_stream(
    text: str,
    plan: Plan,
    *,
    source_version: int,
    expected_version: int,
    chunk_chars: int | None = None,
) -> Iterator[str]:
    """校验通过后，以字符串生成器形式流式产出应用结果。"""
    verify_source(text, plan, source_version, expected_version)
    chunk_chars = chunk_chars or 65_536
    buf: list[str] = []
    buf_len = 0

    def push(piece: str) -> Iterator[str]:
        nonlocal buf_len
        if not piece:
            return
        buf.append(piece)
        buf_len += len(piece)
        if buf_len >= chunk_chars:
            yield "".join(buf)
            buf.clear()
            buf_len = 0

    # chosen 在规划器中按 (start, 消耗优先) 排序。
    # 拼接按“动作发生的位置”归并：在位置 p 先吐原文到 p，再吐同位置的所有
    # 替换（消耗型在前、零宽在后），消耗型把文本游标推过被吃区间。这样在
    # 消耗区间终点紧贴零宽（如 [0,1) 与 [1,1)）时既不丢原文也不重复。
    ordered = plan.chosen
    i = 0
    cursor = 0
    while i < len(ordered):
        pos = ordered[i].hit.start
        if cursor < pos:
            yield from push(text[cursor:pos])
        cursor = pos
        while i < len(ordered) and ordered[i].hit.start == pos:
            cand = ordered[i]
            s, e = cand.hit.start, cand.hit.end
            yield from push(cand.replacement)
            if e > s:
                cursor = e  # 同位置最多一条消耗型（消解阶段已保证）
            i += 1
    yield from push(text[cursor:])
    if buf:
        yield "".join(buf)


def apply_plan(text: str, plan: Plan, *, source_version: int, expected_version: int) -> str:
    """非流式便捷封装（小文本/测试与参考实现对账用）。"""
    return "".join(
        apply_plan_stream(
            text,
            plan,
            source_version=source_version,
            expected_version=expected_version,
            chunk_chars=1 << 30,
        )
    )
