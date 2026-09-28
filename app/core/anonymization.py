"""匿名化安全内核：等价类计数、k/l 判定与信息损失最优泛化搜索。

设计要点
========
1. **真实计数**：所有等价类都从实际数据行实时分组计数得到；优化过程
   只选择泛化层级，绝不修改、估算或缓存伪造计数。
2. **单调性保证**：层级已验证包含关系，故泛化加深时类数不增，
   k/l 可行性单调（越泛化越可能满足），信息损失单调不减。
3. **完全穷举 + 安全剪枝**：搜索空间为各 QI 层级选择的笛卡尔积；
   先验证顶层（全部最粗）可达性，再以逐列损失下界做分支限界。
   剪枝只跳过"损失下界 ≥ 当前最优且枚举次序更靠后"的分支，
   数学上不可能改变最优解（见 README §算法）。
4. **判定留证**：不可达时返回最粗层级下仍然违规的真实等价类证据
   （仅规模/敏感值计数，不含任何真实取值）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.core.hierarchies import Hierarchy, row_generalization_loss
from app.core.logging_setup import StepLogger
from app.core.parsing import Dataset
from app.models import ColumnRole

_EPS = 1e-12


@dataclass
class ClassInfo:
    class_index: int
    members: list[int]  # 行下标（仅内部使用，不随 API 输出）
    size: int
    distinct_sensitive: int
    max_sensitive_frequency: int
    contains_null_qi: bool

    def meets_k(self, k: int) -> bool:
        return self.size >= k

    def meets_l(self, l: int) -> bool:
        return self.distinct_sensitive >= l


@dataclass
class Evaluation:
    """某一层级向量下，基于真实数据计算出的完整判定结果。"""

    levels: tuple[int, ...]
    loss: float
    classes: list[ClassInfo]
    n_classes: int
    min_size: int
    max_size: int
    below_k: int
    below_l_after_k: int
    rows_below_k: int
    k_feasible: bool
    l_feasible: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "levels": list(self.levels),
            "info_loss": round(self.loss, 6),
            "n_classes": self.n_classes,
            "min_class_size": self.min_size,
            "classes_below_k": self.below_k,
            "classes_below_l": self.below_l_after_k,
            "k_feasible": self.k_feasible,
            "l_feasible": self.l_feasible,
        }


@dataclass
class SearchResult:
    status: str  # succeeded | k_unreachable | l_unreachable
    failure_code: str | None
    message: str
    best: Evaluation | None
    top: Evaluation
    n_combinations_explored: int
    n_subtrees_pruned: int
    trace: list[dict[str, Any]] = field(default_factory=list)


def sensitive_signatures(ds: Dataset) -> list[tuple[str, ...]]:
    sens = [c.name for c in ds.columns if c.role is ColumnRole.SENSITIVE]
    return [tuple(r[s] for s in sens) for r in ds.rows]


def _qi_materials(ds: Dataset) -> tuple[list[str], list[Hierarchy]]:
    qi = ds.qi_columns
    names = [c.name for c in qi]
    hiers = [c.hierarchy for c in qi]
    assert all(h is not None for h in hiers)
    return names, hiers  # type: ignore[return-value]


def _qi_hierarchies(ds: Dataset) -> list[Hierarchy]:
    return _qi_materials(ds)[1]


def generalized_keys(
    ds: Dataset, level_vec: tuple[int, ...]
) -> list[tuple[str, ...]]:
    names, hiers = _qi_materials(ds)
    keys: list[tuple[str, ...]] = []
    for r in ds.rows:
        keys.append(tuple(h.apply(r[n], lv) for n, h, lv in zip(names, hiers, level_vec)))
    return keys


def evaluate(ds: Dataset, level_vec: tuple[int, ...]) -> Evaluation:
    """对给定层级向量，基于真实行执行分组与 k/l 判定。"""
    names, _ = _qi_materials(ds)
    keys = generalized_keys(ds, level_vec)
    sigs = sensitive_signatures(ds)

    # 分组（保持首次出现顺序，输出稳定）
    grouped: dict[tuple[str, ...], list[int]] = {}
    for idx, key in enumerate(keys):
        grouped.setdefault(key, []).append(idx)

    # 按规模降序、首次出现升序排列，class_index 稳定可复现
    ordered_keys = sorted(grouped, key=lambda key: (-len(grouped[key]), grouped[key][0]))

    classes: list[ClassInfo] = []
    below_k = 0
    below_l_after_k = 0
    rows_below_k = 0
    sizes: list[int] = []
    for ci, key in enumerate(ordered_keys):
        members = grouped[key]
        size = len(members)
        sizes.append(size)
        sig_counts: dict[tuple[str, ...], int] = {}
        for mi in members:
            sig = sigs[mi]
            sig_counts[sig] = sig_counts.get(sig, 0) + 1
        distinct = len(sig_counts)
        max_freq = max(sig_counts.values())
        has_null = any(ds.null_row_flags[mi] for mi in members)
        info = ClassInfo(
            class_index=ci,
            members=members,
            size=size,
            distinct_sensitive=distinct,
            max_sensitive_frequency=max_freq,
            contains_null_qi=has_null,
        )
        classes.append(info)
        if size < ds.k:
            below_k += 1
            rows_below_k += size
        elif distinct < ds.l:
            below_l_after_k += 1

    # 信息损失：逐列平均损失的平均（逐列可加，支撑下界剪枝）
    total = 0.0
    for qi_name, hier, lv in zip(names, _qi_hierarchies(ds), level_vec):
        for r in ds.rows:
            total += row_generalization_loss(hier, r[qi_name], lv)
    loss = total / (len(ds.rows) * len(names))

    k_ok = below_k == 0
    l_ok = k_ok and below_l_after_k == 0
    return Evaluation(
        levels=level_vec,
        loss=loss,
        classes=classes,
        n_classes=len(classes),
        min_size=min(sizes),
        max_size=max(sizes),
        below_k=below_k,
        below_l_after_k=below_l_after_k,
        rows_below_k=rows_below_k,
        k_feasible=k_ok,
        l_feasible=l_ok,
    )


def column_loss_table(ds: Dataset) -> dict[str, list[float]]:
    """每列在每个层级的平均损失（独立于其它列，用于搜索下界与可解释性）。"""
    table: dict[str, list[float]] = {}
    n = len(ds.rows)
    for c in ds.qi_columns:
        assert c.hierarchy is not None
        vals = [r[c.name] for r in ds.rows]
        table[c.name] = [
            sum(row_generalization_loss(c.hierarchy, v, h) for v in vals) / n
            for h in range(c.hierarchy.height + 1)
        ]
    return table


def find_best_generalization(ds: Dataset, steps: StepLogger | None = None) -> SearchResult:
    """搜索满足 (k,l) 的信息损失最小泛化层级向量。

    枚举次序固定（QI 声明顺序，层级升序，后列变化最快），
    同等损失取枚举序最小向量，保证结果确定、可复现。
    """
    steps = steps or StepLogger()
    qi_names = [c.name for c in ds.qi_columns]
    heights = [c.hierarchy.height for c in ds.qi_columns]  # type: ignore[union-attr]
    col_loss = column_loss_table(ds)

    n_combos = math.prod(h + 1 for h in heights)
    steps.step(
        "search_init",
        f"exhaustive search over {n_combos} level combination(s)",
        basis="cartesian product of per-QI hierarchy levels",
        qi_columns=qi_names,
        heights=heights,
        space_size=n_combos,
        k=ds.k,
        l=ds.l,
    )

    top_vec = tuple(heights)
    top = evaluate(ds, top_vec)
    steps.step(
        "evaluate_top",
        "evaluated coarsest generalization (all max levels) for reachability",
        basis="monotonicity: if thresholds fail at the top, no finer level can satisfy them",
        **{k: v for k, v in top.as_dict().items() if k != "levels"},
        levels=dict(zip(qi_names, top.levels)),
    )

    trace: list[dict[str, Any]] = []
    trace.append({"event": "evaluate", **_trace_entry(qi_names, top)})

    # --- 可达性判定（顶层最粗；不可达即明确失败，绝不返回伪成功）---
    if not top.k_feasible:
        blockers = [
            {
                "class_index": c.class_index,
                "size": c.size,
                "distinct_sensitive": c.distinct_sensitive,
                "contains_null_qi": c.contains_null_qi,
            }
            for c in top.classes
            if c.size < ds.k
        ]
        steps.verdict(
            "k_unreachable",
            f"{top.below_k} class(es) below k={ds.k} even at the coarsest level",
            blocking_classes=blockers,
        )
        trace.append(
            {
                "event": "unreachable",
                "code": "K_UNREACHABLE",
                "basis": "top-level evaluation still has classes with size < k",
                "blocking_classes": blockers,
            }
        )
        return SearchResult(
            status="k_unreachable",
            failure_code="K_UNREACHABLE",
            message=(
                f"k={ds.k} unreachable: {top.below_k} equivalence class(es) remain "
                f"smaller than k even at the coarsest available generalization."
            ),
            best=None,
            top=top,
            n_combinations_explored=1,
            n_subtrees_pruned=0,
            trace=trace,
        )

    if not top.l_feasible:
        blockers = [
            {
                "class_index": c.class_index,
                "size": c.size,
                "distinct_sensitive": c.distinct_sensitive,
                "max_sensitive_frequency": c.max_sensitive_frequency,
            }
            for c in top.classes
            if c.size >= ds.k and c.distinct_sensitive < ds.l
        ]
        steps.verdict(
            "l_unreachable",
            f"{top.below_l_after_k} k-passing class(es) below l={ds.l} at the coarsest level",
            blocking_classes=blockers,
        )
        trace.append(
            {
                "event": "unreachable",
                "code": "L_UNREACHABLE",
                "basis": "k holds at the top, but classes still have fewer than l distinct sensitive signatures",
                "blocking_classes": blockers,
            }
        )
        return SearchResult(
            status="l_unreachable",
            failure_code="L_UNREACHABLE",
            message=(
                f"l={ds.l} unreachable: {top.below_l_after_k} k-passing class(es) have "
                f"fewer than l distinct sensitive value(s) even at the coarsest level."
            ),
            best=None,
            top=top,
            n_combinations_explored=1,
            n_subtrees_pruned=0,
            trace=trace,
        )

    # --- 顶层可行 => 对全部组合做分支限界穷举。
    # 最优初始为空（None），枚举按"QI 声明顺序、层级升序、后列变化最快"，
    # 仅在严格更小时更新，因此平局自然落在先枚举（更细）的层级向量上。---
    best: Evaluation | None = None
    best_vec: tuple[int, ...] | None = None
    explored = 0
    pruned = 0
    n_qi = len(qi_names)
    current = [0] * n_qi

    def loss_lower_bound(fixed: list[int], depth: int) -> float:
        """已定列取选定层级，未定列取层级 0 的逐列平均损失下界。"""
        total = 0.0
        for j, name in enumerate(qi_names):
            h = fixed[j] if j < depth else 0
            total += col_loss[name][h]
        return total / n_qi

    def dfs(depth: int) -> None:
        nonlocal best, best_vec, explored, pruned
        if depth == n_qi:
            vec = tuple(current)
            ev = evaluate(ds, vec)
            explored += 1
            trace.append({"event": "evaluate", **_trace_entry(qi_names, ev)})
            steps.step(
                "evaluate",
                f"levels={dict(zip(qi_names, vec))} loss={ev.loss:.6f} "
                f"classes={ev.n_classes} below_k={ev.below_k} below_l={ev.below_l_after_k}",
                basis="exhaustive leaf evaluation over real equivalence classes",
                levels=dict(zip(qi_names, vec)),
                info_loss=round(ev.loss, 6),
                feasible=ev.l_feasible,
            )
            # 严格更小才替换；平局保留先枚举（更细）的向量。
            if ev.l_feasible and (best is None or ev.loss < best.loss - _EPS):
                best = ev
                best_vec = vec
                steps.step(
                    "new_best",
                    f"new best levels={dict(zip(qi_names, vec))} loss={ev.loss:.6f}",
                    basis="feasible and strictly lower information loss than incumbent",
                )
            return

        # 安全剪枝：该子树所有叶子损失 ≥ 下界。已有可行最优且下界已不优于它，
        # 加上枚举次序保证此处任何解都更靠后（平局也输），故整枝跳过。
        # best 为 None（尚未找到可行解）时绝不剪枝，避免把唯一可行解剪掉。
        if best is not None:
            lb = loss_lower_bound(current, depth)
            if lb >= best.loss - _EPS:
                pruned += 1
                trace.append(
                    {
                        "event": "prune",
                        "prefix": dict(zip(qi_names[:depth], current[:depth])),
                        "loss_lower_bound": round(lb, 6),
                        "incumbent_loss": round(best.loss, 6),
                        "basis": "monotone per-column loss; subtree cannot beat incumbent",
                    }
                )
                return

        for h in range(heights[depth] + 1):
            current[depth] = h
            dfs(depth + 1)

    dfs(0)
    assert best is not None and best_vec is not None  # 顶层已可行，必有解

    steps.verdict(
        "succeeded",
        f"optimal levels={dict(zip(qi_names, best_vec))} loss={best.loss:.6f} "
        f"(explored {explored}/{n_combos}, pruned {pruned} subtrees)",
        explored=explored,
        space_size=n_combos,
        pruned=pruned,
    )
    trace.append(
        {
            "event": "optimum",
            "levels": dict(zip(qi_names, best.levels)),
            "info_loss": round(best.loss, 6),
            "explored": explored,
            "space_size": n_combos,
            "pruned_subtrees": pruned,
            "basis": "minimum information loss among all feasible level vectors",
        }
    )
    return SearchResult(
        status="succeeded",
        failure_code=None,
        message="found feasible optimum",
        best=best,
        top=top,
        n_combinations_explored=explored,
        n_subtrees_pruned=pruned,
        trace=trace,
    )


def _trace_entry(qi_names: list[str], ev: Evaluation) -> dict[str, Any]:
    d = ev.as_dict()
    d["levels"] = dict(zip(qi_names, ev.levels))
    return d
