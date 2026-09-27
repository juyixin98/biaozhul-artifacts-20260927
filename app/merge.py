"""三路合并算法。

输入为共同基线 base 与两侧文本 local/remote。规则(详见 docs/semantics.md):

- 两侧编辑的基线区间不相交(含边界相邻)时自动合并。
- 同一位置的插入:内容相同则应用一次;内容不同则冲突(same-point-insert),
  不猜测先后顺序。
- 一方删除与另一方修改的区间相交:冲突(delete-vs-modify)。
- 两侧把同一区间改成相同内容但行结束符不同:冲突(line-ending-mismatch),
  不替用户选择行结束符。
- 其余区间相交:冲突(overlap)。
- 插入位于另一方编辑区间的边界(起点或终点)不算冲突;同一基线位置既有
  插入又有替换时,插入排在替换之前(确定性规则,与左右侧无关)。

冲突块携带三方源范围(基线/本地/远端各自的行号区间与原始行),合并结果
本身不猜测用户意图;resolve3 按显式选择("local"/"base"/"remote")重建。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .edits import Edit, compute_edits
from .textnorm import (
    dominant_terminator,
    has_terminator,
    join_lines,
    line_body,
    profile,
    split_lines,
)

DEFAULT_LABELS = ("local", "base", "remote")


class UnresolvedConflictError(Exception):
    """有冲突未给出显式选择。missing 为缺失选择的冲突下标。"""

    def __init__(self, missing: list[int]):
        self.missing = missing
        super().__init__(f"unresolved conflicts at indices {missing}")


class UnknownChoiceError(Exception):
    """选择了非法的一侧。"""

    def __init__(self, index: int, choice: str):
        self.index = index
        self.choice = choice
        super().__init__(f"unknown choice {choice!r} for conflict {index}")


@dataclass(frozen=True)
class SideSpan:
    """冲突在一侧文本中的源范围(行号半开区间)与原始行。"""

    start: int
    end: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Conflict:
    kind: str  # same-point-insert | delete-vs-modify | line-ending-mismatch | overlap
    base_start: int
    base_end: int
    local: SideSpan
    remote: SideSpan


@dataclass(frozen=True)
class Applied:
    side: str  # "local" | "remote" | "both"
    edit: Edit


@dataclass(frozen=True)
class ConflictDecision:
    conflict: Conflict
    local_edits: tuple[Edit, ...]
    remote_edits: tuple[Edit, ...]


Decision = Applied | ConflictDecision


@dataclass
class MergeOutcome:
    status: str  # "clean" | "conflicted"
    text: str
    conflicts: list[Conflict]
    notes: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list, repr=False)


# ---------------------------------------------------------------- 内部工具


def _bodies(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line_body(l) for l in lines)


def _same_range(a: Edit, b: Edit) -> bool:
    return a.start == b.start and a.end == b.end


def _overlaps_span(start: int, end: int, other_start: int, other_end: int) -> bool:
    """区间相交判定。空区间(插入)只与严格包含它的区间或同点空区间相交。"""
    a_empty = start == end
    b_empty = other_start == other_end
    if a_empty and b_empty:
        return start == other_start
    if a_empty:
        return other_start < start < other_end
    if b_empty:
        return start < other_start < end
    return start < other_end and other_start < end


def _overlaps(a: Edit, b: Edit) -> bool:
    return _overlaps_span(a.start, a.end, b.start, b.end)


def _side_offset(edits: list[Edit], pos: int) -> int:
    """基线位置 pos 在某侧文本中的行号偏移(只累计严格位于 pos 之前的编辑)。"""
    offset = 0
    for e in edits:
        if e.end < pos or (e.end == pos and not e.is_insert):
            offset += len(e.replacement) - (e.end - e.start)
    return offset


def _side_span(
    group_edits: tuple[Edit, ...],
    all_edits: list[Edit],
    side_lines: list[str],
    base_start: int,
    base_end: int,
) -> SideSpan:
    start = base_start + _side_offset(all_edits, base_start)
    length = (base_end - base_start) + sum(
        len(e.replacement) - (e.end - e.start) for e in group_edits
    )
    return SideSpan(start, start + length, tuple(side_lines[start : start + length]))


def _classify(local_edits: tuple[Edit, ...], remote_edits: tuple[Edit, ...]) -> str:
    if len(local_edits) == 1 and len(remote_edits) == 1:
        left, right = local_edits[0], remote_edits[0]
        if left.is_insert and right.is_insert:
            return "same-point-insert"
        if _same_range(left, right) and _bodies(left.replacement) == _bodies(
            right.replacement
        ):
            return "line-ending-mismatch"
    local_all_delete = bool(local_edits) and all(e.is_delete for e in local_edits)
    remote_all_delete = bool(remote_edits) and all(e.is_delete for e in remote_edits)
    if local_all_delete != remote_all_delete:
        return "delete-vs-modify"
    return "overlap"


# ---------------------------------------------------------------- 决策构建


def build_decisions(
    base_lines: list[str], local_lines: list[str], remote_lines: list[str]
) -> tuple[list[Decision], list[Edit], list[Edit]]:
    """把两侧编辑合并为按基线位置排序的决策序列(应用或冲突)。"""
    local_edits = compute_edits(base_lines, local_lines)
    remote_edits = compute_edits(base_lines, remote_lines)
    decisions: list[Decision] = []
    i = j = 0
    while i < len(local_edits) and j < len(remote_edits):
        left, right = local_edits[i], remote_edits[j]
        if _same_range(left, right) and _bodies(left.replacement) == _bodies(
            right.replacement
        ):
            if left.replacement == right.replacement:
                decisions.append(Applied("both", left))
            else:
                decisions.append(
                    _make_conflict(
                        (left,), (right,), local_edits, remote_edits,
                        base_lines, local_lines, remote_lines,
                    )
                )
            i += 1
            j += 1
        elif not _overlaps(left, right):
            if left.start != right.start:
                local_first = left.start < right.start
            else:
                # 同一位置:插入先于替换(两侧对称的确定性规则)
                local_first = left.is_insert
            if local_first:
                decisions.append(Applied("local", left))
                i += 1
            else:
                decisions.append(Applied("remote", right))
                j += 1
        else:
            gi, gj = i + 1, j + 1
            span_start = min(left.start, right.start)
            span_end = max(left.end, right.end)
            changed = True
            while changed:
                changed = False
                while gi < len(local_edits) and _overlaps_span(
                    local_edits[gi].start, local_edits[gi].end, span_start, span_end
                ):
                    span_start = min(span_start, local_edits[gi].start)
                    span_end = max(span_end, local_edits[gi].end)
                    gi += 1
                    changed = True
                while gj < len(remote_edits) and _overlaps_span(
                    remote_edits[gj].start, remote_edits[gj].end, span_start, span_end
                ):
                    span_start = min(span_start, remote_edits[gj].start)
                    span_end = max(span_end, remote_edits[gj].end)
                    gj += 1
                    changed = True
            decisions.append(
                _make_conflict(
                    tuple(local_edits[i:gi]), tuple(remote_edits[j:gj]),
                    local_edits, remote_edits,
                    base_lines, local_lines, remote_lines,
                )
            )
            i, j = gi, gj
    while i < len(local_edits):
        decisions.append(Applied("local", local_edits[i]))
        i += 1
    while j < len(remote_edits):
        decisions.append(Applied("remote", remote_edits[j]))
        j += 1
    return decisions, local_edits, remote_edits


def _make_conflict(
    local_group: tuple[Edit, ...],
    remote_group: tuple[Edit, ...],
    local_edits: list[Edit],
    remote_edits: list[Edit],
    base_lines: list[str],
    local_lines: list[str],
    remote_lines: list[str],
) -> ConflictDecision:
    base_start = min(e.start for e in local_group + remote_group)
    base_end = max(e.end for e in local_group + remote_group)
    conflict = Conflict(
        kind=_classify(local_group, remote_group),
        base_start=base_start,
        base_end=base_end,
        local=_side_span(local_group, local_edits, local_lines, base_start, base_end),
        remote=_side_span(remote_group, remote_edits, remote_lines, base_start, base_end),
    )
    return ConflictDecision(conflict, local_group, remote_group)


# ---------------------------------------------------------------- 渲染


def _emit_section(
    out: list[str], lines: tuple[str, ...], marker_term: str, notes: list[str], what: str
) -> None:
    """输出冲突块中某一方的小节。最后一行若无行结束符,显式补一个并记录,
    避免与后续标记行粘连(仅在冲突标记表示中如此;resolve 输出逐字节保留)。"""
    out.extend(lines)
    if lines and not has_terminator(lines[-1]):
        out.append(marker_term)
        notes.append(
            f"conflict section '{what}' had no trailing terminator; "
            "one was added inside conflict markers"
        )


def render(
    decisions: list[Decision],
    base_lines: list[str],
    choices: dict[int, str] | None = None,
    labels: tuple[str, str, str] = DEFAULT_LABELS,
    marker_term: str = "\n",
    notes: list[str] | None = None,
) -> str:
    """按决策序列渲染文本。choices 为 None 时冲突以标记块输出;否则按
    {冲突下标: "local"|"base"|"remote"} 重建,缺项抛 UnresolvedConflictError。"""
    notes = notes if notes is not None else []
    out: list[str] = []
    pos = 0
    missing: list[int] = []
    conflict_index = 0
    for decision in decisions:
        if isinstance(decision, Applied):
            out.extend(base_lines[pos : decision.edit.start])
            out.extend(decision.edit.replacement)
            pos = decision.edit.end
        else:
            conflict = decision.conflict
            out.extend(base_lines[pos : conflict.base_start])
            pos = conflict.base_end
            if choices is None:
                out.append(f"<<<<<<< {labels[0]}" + marker_term)
                _emit_section(out, conflict.local.lines, marker_term, notes, labels[0])
                out.append(f"||||||| {labels[1]}" + marker_term)
                _emit_section(
                    out,
                    tuple(base_lines[conflict.base_start : conflict.base_end]),
                    marker_term,
                    notes,
                    labels[1],
                )
                out.append("=======" + marker_term)
                _emit_section(out, conflict.remote.lines, marker_term, notes, labels[2])
                out.append(f">>>>>>> {labels[2]}" + marker_term)
            else:
                choice = choices.get(conflict_index)
                if choice is None:
                    missing.append(conflict_index)
                elif choice == "local":
                    out.extend(conflict.local.lines)
                elif choice == "base":
                    out.extend(base_lines[conflict.base_start : conflict.base_end])
                elif choice == "remote":
                    out.extend(conflict.remote.lines)
                else:
                    raise UnknownChoiceError(conflict_index, choice)
                conflict_index += 1
    out.extend(base_lines[pos:])
    if missing:
        raise UnresolvedConflictError(missing)
    return join_lines(out)


# ---------------------------------------------------------------- 诊断说明


def _notes(
    base: str,
    local: str,
    remote: str,
    decisions: list[Decision],
    local_edits: list[Edit],
    remote_edits: list[Edit],
    result: str,
    base_line_count: int,
) -> list[str]:
    notes: list[str] = []
    if local != base and not local_edits:
        notes.append(
            "local differs from base only in line-terminator flavor; "
            "base terminators preserved"
        )
    if remote != base and not remote_edits:
        notes.append(
            "remote differs from base only in line-terminator flavor; "
            "base terminators preserved"
        )
    base_profile, result_profile = profile(base), profile(result)
    if base_profile.ends_with_newline != result_profile.ends_with_newline:
        sides = sorted(
            {
                d.side
                for d in decisions
                if isinstance(d, Applied) and d.edit.end >= base_line_count
            }
        )
        verb = "added" if result_profile.ends_with_newline else "removed"
        who = ", ".join(sides) if sides else "unknown"
        notes.append(f"trailing newline {verb} by {who} edit(s)")
    return notes


# ---------------------------------------------------------------- 对外入口


def merge3(
    base: str,
    local: str,
    remote: str,
    labels: tuple[str, str, str] = DEFAULT_LABELS,
) -> MergeOutcome:
    """三方合并。冲突时 text 含冲突标记块,conflicts 携带三方源范围。"""
    base_lines = split_lines(base)
    local_lines = split_lines(local)
    remote_lines = split_lines(remote)
    decisions, local_edits, remote_edits = build_decisions(
        base_lines, local_lines, remote_lines
    )
    conflicts = [d.conflict for d in decisions if isinstance(d, ConflictDecision)]
    marker_term = dominant_terminator(base)
    notes: list[str] = []
    text = render(decisions, base_lines, labels=labels, marker_term=marker_term, notes=notes)
    notes.extend(
        _notes(base, local, remote, decisions, local_edits, remote_edits, text, len(base_lines))
    )
    return MergeOutcome(
        status="conflicted" if conflicts else "clean",
        text=text,
        conflicts=conflicts,
        notes=notes,
        decisions=decisions,
    )


def resolve3(
    base: str,
    local: str,
    remote: str,
    choices: dict[int, str],
    labels: tuple[str, str, str] = DEFAULT_LABELS,
) -> str:
    """按显式选择重建冲突。choices 必须覆盖全部冲突,否则抛
    UnresolvedConflictError;非法选择抛 UnknownChoiceError。"""
    base_lines = split_lines(base)
    local_lines = split_lines(local)
    remote_lines = split_lines(remote)
    decisions, _, _ = build_decisions(base_lines, local_lines, remote_lines)
    return render(decisions, base_lines, choices=choices, labels=labels)
