"""加权非限制性 Damerau-Levenshtein 距离（Lowrance-Wagner 递推）。

明确的变体选择
==============
本模块实现 **非限制性（unrestricted / true Damerau-Levenshtein，Lowrance &
Wagner 1969）** 递推，允许对同一子串多次编辑（交换链 CA→ABC 距离为 2，而
限制性最优串对齐 OSA 给出 3）。**不与限制性 OSA 递推混用**。

在「插入代价统一、删除代价统一」的前提下，使用 last-occurrence O(nm) 递推
（维基百科 “Damerau-Levenshtein distance” 的最优串递推形式）：

    d[i][j] = min(
        d[i-1][j-1] + 1[a_i != b_j] * sub(a_i, b_j),
        d[i-1][j]   + w_del,
        d[i][j-1]   + w_ins,
        d[k-1][l-1] + (i-k-1)*w_del + w_trans + (j-l-1)*w_ins   (k,l > 0)
    )

其中 k = 最近的使 a_k == b_j 的行，l = 最近的使 b_l == a_i 的列。最后一项
对应宏操作：删除 a_{k+1..i-1}（i-k-1 个删除）→ 交换此时已相邻的 a_k 与 a_i
（1 次交换，a_i 对齐 b_l，a_k 对齐 b_j）→ 在二者间插入 b_{l+1..j-1}
（j-l-1 个插入）。该 O(nm) 形式与完整 O(n^2 m^2) 枚举所有 (k,l) 对的
Lowrance-Wagner 递推在均匀插入/删除代价下等价（更近的 k/l 支配更远的对）。

边界：d[i][0] = i*w_del，d[0][j] = j*w_ins。所有代价非负（见 app.costs）。

平局打破顺序固定（diag > delete > insert > transpose），保证路径确定性。

输出分两层：
- AlignedStep：源串消费顺序的对齐脚本（回溯直接产物）；
- Move：作用在“当前字符串”上的具体操作（insert/delete/substitute/swap 的
  具体下标），由 compile_moves 从对齐脚本编译，可供调用方逐步重放、独立重算
  成本，而无需信任本模块给出的距离。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .costs import CostProfile

EPS = 1e-9

# 回溯来源类型
_DIAG = "diag"
_DELETE = "delete"
_INSERT = "insert"
_TRANS = "transpose"


@dataclass(frozen=True)
class AlignedStep:
    """源串消费顺序的单步对齐动作（位置均为 0-indexed）。"""

    op: str  # match | substitute | delete | inner_delete | insert | transpose_macro
    src_pos: Optional[int] = None
    src_pos2: Optional[int] = None  # transpose_macro 的第二个源位置 (i-1)
    tgt_pos: Optional[int] = None
    tgt_pos2: Optional[int] = None  # transpose_macro 中 a_i 对齐的目标位置 (l-1)


@dataclass(frozen=True)
class Move:
    """作用在可变字符串上的具体编辑操作（响应返回的编辑路径元素）。"""

    type: str  # insert | delete | substitute | swap
    index: int
    char: Optional[str] = None  # insert: 插入字符；substitute: 新字符；delete: 被删字符
    detail: Optional[str] = None  # swap: 交换后的两个字符，如 "ab<->ba"

    def to_dict(self) -> dict:
        out = {"type": self.type, "index": self.index}
        if self.char is not None:
            out["char"] = self.char
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class EditResult:
    source: str
    target: str
    distance: float
    aligned_steps: tuple[AlignedStep, ...]
    moves: tuple[Move, ...]


def _dp(source: str, target: str, profile: CostProfile, with_path: bool):
    n, m = len(source), len(target)
    # d[(i,j)]；用 list[list] 全矩阵（n,m<=32，规模可接受）
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    parent: list[list[Optional[str]]]
    trans_kl: list[list[Optional[tuple[int, int]]]]
    if with_path:
        parent = [[None] * (m + 1) for _ in range(n + 1)]
        trans_kl = [[None] * (m + 1) for _ in range(n + 1)]
    else:
        parent = []
        trans_kl = []

    for i in range(1, n + 1):
        d[i][0] = i * profile.delete
        if with_path:
            parent[i][0] = _DELETE
    for j in range(1, m + 1):
        d[0][j] = j * profile.insert
        if with_path:
            parent[0][j] = _INSERT

    last_row: dict[str, int] = {}  # da：字符 -> 行内最近出现行（行末更新）

    for i in range(1, n + 1):
        ai = source[i - 1]
        db = 0  # db：进入列 j 之前，最近的使 b_db == a_i 的列（行内延迟更新）
        for j in range(1, m + 1):
            bj = target[j - 1]
            k = last_row.get(bj, 0)
            l = db

            if ai == bj:
                diag_cost = d[i - 1][j - 1]
                chosen = _DIAG
            else:
                diag_cost = d[i - 1][j - 1] + profile.sub_cost(ai, bj)
                chosen = _DIAG
            best = diag_cost

            del_cost = d[i - 1][j] + profile.delete
            if del_cost < best - EPS:
                best = del_cost
                chosen = _DELETE

            ins_cost = d[i][j - 1] + profile.insert
            if ins_cost < best - EPS:
                best = ins_cost
                chosen = _INSERT

            chosen_kl: Optional[tuple[int, int]] = None
            if k > 0 and l > 0:
                trans_cost = (
                    d[k - 1][l - 1]
                    + (i - k - 1) * profile.delete
                    + profile.transpose
                    + (j - l - 1) * profile.insert
                )
                if trans_cost < best - EPS:
                    best = trans_cost
                    chosen = _TRANS
                    chosen_kl = (k, l)

            d[i][j] = best
            if with_path:
                parent[i][j] = chosen
                trans_kl[i][j] = chosen_kl

            if ai == bj:
                # 延迟更新：db 只在当前列处理完后才成为 j，保证交换候选的
                # l 严格早于 j（同位置匹配走对角，不应被当成交换）。
                db = j
        last_row[ai] = i

    if not with_path:
        return d[n][m], None, None
    return d[n][m], parent, trans_kl


def _backtrace(source: str, target: str, parent, trans_kl) -> tuple[AlignedStep, ...]:
    i, j = len(source), len(target)
    steps: list[AlignedStep] = []
    while i > 0 or j > 0:
        kind = parent[i][j]
        if kind == _DIAG:
            op = "match" if source[i - 1] == target[j - 1] else "substitute"
            segment = [AlignedStep(op=op, src_pos=i - 1, tgt_pos=j - 1)]
            steps[0:0] = segment
            i -= 1
            j -= 1
        elif kind == _DELETE:
            steps[0:0] = [AlignedStep(op="delete", src_pos=i - 1)]
            i -= 1
        elif kind == _INSERT:
            steps[0:0] = [AlignedStep(op="insert", tgt_pos=j - 1)]
            j -= 1
        elif kind == _TRANS:
            k, l = trans_kl[i][j]
            # 严格按源消费顺序发出：
            # 1) 删除源中间字符 a_{k+1..i-1}：它们在锚点 a_k 之后，
            #    编译期下标是锚点下标 + 1，逐次前移；
            # 2) transpose_macro：交换锚点 a_k 与 a_i（此时二者已相邻）；
            # 3) 插入目标中间字符 b_{l+1..j-1}（编译器放到交换后两字符之间）。
            segment: list[AlignedStep] = [
                AlignedStep(op="inner_delete", src_pos=p - 1)
                for p in range(k + 1, i)
            ]
            segment.append(
                AlignedStep(
                    op="transpose_macro",
                    src_pos=k - 1,
                    src_pos2=i - 1,
                    tgt_pos=j - 1,
                    tgt_pos2=l - 1,
                )
            )
            segment.extend(
                AlignedStep(op="insert", tgt_pos=p - 1)
                for p in range(l + 1, j)
            )
            steps[0:0] = segment
            i, j = k - 1, l - 1
        else:  # pragma: no cover - 防御性分支
            raise RuntimeError(f"回溯遇到非法来源 {kind!r} @ ({i},{j})")
    return tuple(steps)


def compile_moves(source: str, target: str, steps: tuple[AlignedStep, ...]) -> tuple[Move, ...]:
    """把对齐脚本编译为作用在具体字符串上的 Move 序列。

    cursor 始终指向“下一个未消费源字符”在当前可变字符串中的下标。
    编译时对源字符身份做断言，脚本与字符串不一致会直接报错（而不是产出
    看似合理的错误路径）。
    """
    work = list(source)
    moves: list[Move] = []
    cursor = 0
    for step in steps:
        if step.op in ("match", "substitute"):
            assert work[cursor] == source[step.src_pos], "对齐脚本与源串不一致(match)"
            if step.op == "substitute":
                new_char = target[step.tgt_pos]
                moves.append(Move("substitute", cursor, new_char))
                work[cursor] = new_char
            cursor += 1
        elif step.op == "delete":
            assert work[cursor] == source[step.src_pos], "对齐脚本与源串不一致(delete)"
            moves.append(Move("delete", cursor, work[cursor]))
            del work[cursor]
        elif step.op == "inner_delete":
            # transpose 宏内、锚点之后的删除：编译期下标恒为 cursor+1
            assert work[cursor + 1] == source[step.src_pos], "对齐脚本与源串不一致(inner_delete)"
            moves.append(Move("delete", cursor + 1, work[cursor + 1]))
            del work[cursor + 1]
        elif step.op == "insert":
            ch = target[step.tgt_pos]
            moves.append(Move("insert", cursor, ch))
            work.insert(cursor, ch)
            cursor += 1
        elif step.op == "transpose_macro":
            k0 = step.src_pos       # a_k 的源位置 (0-indexed)
            i0 = step.src_pos2      # a_i 的源位置
            # a_{k+1..i-1} 已由前面独立的 delete 步骤删除（严格源顺序），
            # 此时锚点 a_k 位于 cursor，a_i 紧随其后，二者相邻。
            assert work[cursor] == source[k0], "对齐脚本与源串不一致(transpose anchor)"
            assert work[cursor + 1] == source[i0], "对齐脚本与源串不一致(transpose pair)"
            # 交换相邻的 a_k 与 a_i：对齐要求 a_i -> b_j（前）、a_k -> b_l（后），
            # 即把后字符移到前、前字符移到后。
            pair = work[cursor + 1] + work[cursor]
            moves.append(Move("swap", cursor, detail=f"{work[cursor]}{work[cursor+1]}<->{pair}"))
            work[cursor], work[cursor + 1] = work[cursor + 1], work[cursor]
            # 交换对齐：a_k=b_l（左源字符对左目标列），a_i=b_j（右源字符
            # 对右目标列）。交换后两字符之间需要中间目标字符 b_{l+1..j-1}
            # （1-indexed，不含 b_l 与 b_j；0-indexed 位置 [l, j-2]）。
            middle = [target[p] for p in range(step.tgt_pos2 + 1, step.tgt_pos)]
            ins_pos = cursor + 1
            for ch in middle:
                moves.append(Move("insert", ins_pos, ch))
                work.insert(ins_pos, ch)
                ins_pos += 1
            cursor = cursor + 2 + len(middle)
        else:  # pragma: no cover
            raise RuntimeError(f"未知对齐动作 {step.op!r}")

    assert "".join(work) == target, "编译后的编辑路径不能生成目标串"
    assert cursor == len(work), "编辑路径消费后游标未到达串尾"
    return tuple(moves)


def score_moves(moves: tuple[Move, ...], profile: CostProfile) -> float:
    """按代价模型独立重算 Move 序列成本（不信任 DP 给出的距离）。"""
    total = 0.0
    for mv in moves:
        if mv.type == "insert":
            total += profile.insert
        elif mv.type == "delete":
            total += profile.delete
        elif mv.type == "swap":
            total += profile.transpose
        elif mv.type == "substitute":
            # Move 不携带旧字符，无法查字符对表；substitute 表属于优化项，
            # 重算时取默认替换价。默认价 >= 表内优惠价，因此该重算值是
            # 路径真实成本的上界；真实重算见 rescore_moves（带源串上下文）。
            total += profile.substitute
        else:  # pragma: no cover
            raise ValueError(f"未知 Move 类型 {mv.type!r}")
    return total


def rescore_moves(source: str, moves: tuple[Move, ...], profile: CostProfile) -> float:
    """在源串上下文中逐步重放并重算成本（含字符对替换表，精确值）。"""
    work = list(source)
    total = 0.0
    for mv in moves:
        if mv.type == "insert":
            total += profile.insert
            work.insert(mv.index, mv.char)
        elif mv.type == "delete":
            total += profile.delete
            assert work[mv.index] == mv.char, "delete 重放字符不一致"
            del work[mv.index]
        elif mv.type == "substitute":
            old = work[mv.index]
            total += profile.sub_cost(old, mv.char)
            work[mv.index] = mv.char
        elif mv.type == "swap":
            total += profile.transpose
            work[mv.index], work[mv.index + 1] = work[mv.index + 1], work[mv.index]
        else:  # pragma: no cover
            raise ValueError(f"未知 Move 类型 {mv.type!r}")
    return total


def replay(source: str, moves: tuple[Move, ...]) -> str:
    """逐步应用 Move，返回最终字符串（独立验证用）。"""
    work = list(source)
    for mv in moves:
        if mv.type == "insert":
            work.insert(mv.index, mv.char)
        elif mv.type == "delete":
            assert work[mv.index] == mv.char, "delete 重放字符不一致"
            del work[mv.index]
        elif mv.type == "substitute":
            work[mv.index] = mv.char
        elif mv.type == "swap":
            work[mv.index], work[mv.index + 1] = work[mv.index + 1], work[mv.index]
        else:  # pragma: no cover
            raise ValueError(f"未知 Move 类型 {mv.type!r}")
    return "".join(work)


def distance(source: str, target: str, profile: CostProfile) -> float:
    """仅计算距离（不保留回溯指针）。"""
    d, _, _ = _dp(source, target, profile, with_path=False)
    return d


def edit(source: str, target: str, profile: CostProfile) -> EditResult:
    """计算距离并返回可重放、可独立重算成本的编辑路径。"""
    dist, parent, trans_kl = _dp(source, target, profile, with_path=True)
    steps = _backtrace(source, target, parent, trans_kl)
    moves = compile_moves(source, target, steps)
    # 自检：重放必须得到目标串；精确重算成本必须与 DP 距离一致。
    replayed = replay(source, moves)
    if replayed != target:
        raise RuntimeError("内部错误：编辑路径重放结果不等于目标串")
    rescored = rescore_moves(source, moves, profile)
    if abs(rescored - dist) > EPS:
        raise RuntimeError(
            f"内部错误：重算成本 {rescored} 与 DP 距离 {dist} 不一致"
        )
    return EditResult(
        source=source,
        target=target,
        distance=dist,
        aligned_steps=steps,
        moves=moves,
    )
