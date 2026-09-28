"""查询编排：规范化 -> 校验 -> 候选生成 -> 下界剪枝 -> DP 评估 -> 阈值过滤 -> 稳定排序。

每个阶段记录计数与耗时，进入响应的 diagnostics.stages；uncertain
（距离落在阈值边缘）与失败原因单列。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .costs import CostProfile
from .editdistance import edit
from .index import IndexEntry, LexiconIndex, combined_lower_bound
from .normalization import NormalizationResult, normalize


class QueryRejected(Exception):
    """输入被拒绝（空串、超长等）。code 用于稳定分类。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Stage:
    name: str
    detail: str
    elapsed_ms: float
    counts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 4),
            "counts": self.counts,
        }


@dataclass(frozen=True)
class CandidateResult:
    word: str
    freq: int
    distance: float
    lower_bound: float
    within_threshold: bool
    moves: tuple

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "freq": self.freq,
            "distance": self.distance,
            "lower_bound": self.lower_bound,
            "within_threshold": self.within_threshold,
            "edit_path": [m.to_dict() for m in self.moves],
            "path_length": len(self.moves),
        }


def _stage(name: str, detail: str, started: float, counts: dict) -> Stage:
    return Stage(name=name, detail=detail, elapsed_ms=(time.perf_counter() - started) * 1000.0,
                 counts=counts)


def correct(
    raw_query: str,
    *,
    profile: CostProfile,
    index: LexiconIndex,
    threshold: Optional[float],
    max_results: Optional[int],
    max_query_length: int,
    max_candidates_evaluated: int,
    default_max_results: int,
    uncertainty_margin: float,
) -> dict:
    stages: list[Stage] = []
    notes: list[str] = []
    query: Optional[str] = None
    norm: Optional[NormalizationResult] = None

    # 1. 规范化
    t0 = time.perf_counter()
    norm = normalize(raw_query)
    query = norm.normalized
    stages.append(_stage(
        "normalize",
        f"{len(norm.steps)} 个规范化步骤",
        t0,
        {"raw_length": len(raw_query), "normalized_length": len(query)},
    ))

    # 2. 输入校验（失败类别明确、单列）
    t0 = time.perf_counter()
    if norm.empty:
        stages.append(_stage("validate", "规范化后为空，拒绝", t0, {}))
        raise QueryRejected("empty_query", "规范化后查询为空，无法纠错")
    if len(query) > max_query_length:
        stages.append(_stage(
            "validate", f"长度 {len(query)} 超过上限 {max_query_length}，拒绝", t0, {}
        ))
        raise QueryRejected(
            "query_too_long",
            f"规范化后查询长度 {len(query)} 超过上限 {max_query_length}",
        )
    if threshold is None:
        threshold = float("inf")  # 表示无阈值，最终按 max_results 截断
        threshold_repr = "infinite"
    else:
        if threshold < 0:
            raise QueryRejected("invalid_threshold", "threshold 必须非负")
        threshold_repr = threshold
    limit = max_results if max_results is not None else default_max_results
    if limit <= 0:
        raise QueryRejected("invalid_max_results", "max_results 必须为正整数")
    stages.append(_stage("validate", "输入校验通过", t0, {
        "query_length": len(query),
        "threshold": threshold_repr,
        "max_results": limit,
    }))

    # 3. 候选生成（长度桶 + 长度差下界）
    t0 = time.perf_counter()
    if threshold == float("inf"):
        length_candidates = index.all_entries()
        gen_detail = "无阈值：扫描全部词条"
    else:
        length_candidates = index.length_bucket_candidates(
            len(query), threshold, profile.min_indel_cost()
        )
        gen_detail = "按长度差下界取长度桶"
    stages.append(_stage("generate", gen_detail, t0, {
        "candidates_after_length_bound": len(length_candidates),
    }))

    # 4. 字符计数下界剪枝
    t0 = time.perf_counter()
    bounded: list[tuple[IndexEntry, float]] = []
    pruned_by_counts = 0
    for entry in length_candidates:
        lb = combined_lower_bound(query, entry.word, profile)
        if threshold != float("inf") and lb > threshold + 1e-9:
            pruned_by_counts += 1
            continue
        bounded.append((entry, lb))
    stages.append(_stage(
        "prune",
        "字符计数 L1 下界剪枝",
        t0,
        {"kept": len(bounded), "pruned": pruned_by_counts},
    ))

    # 5. 候选展开上限：展开顺序确定（下界升序，再按词序），截断而非随机丢弃
    t0 = time.perf_counter()
    bounded.sort(key=lambda pair: (pair[1], pair[0].word))
    truncated = len(bounded) > max_candidates_evaluated
    if truncated:
        bounded = bounded[:max_candidates_evaluated]
        notes.append(
            f"达到候选展开上限 {max_candidates_evaluated}，仅评估下界最低的候选；"
            "阈值内其他候选可能未被评估（结果不确定）"
        )
    stages.append(_stage("cap", "候选展开上限截断", t0, {
        "evaluated": len(bounded),
        "cap": max_candidates_evaluated,
        "truncated": truncated,
    }))

    # 6. DP 评估
    t0 = time.perf_counter()
    evaluated: list[CandidateResult] = []
    for entry, lb in bounded:
        er = edit(query, entry.word, profile)
        evaluated.append(CandidateResult(
            word=entry.word,
            freq=entry.freq,
            distance=er.distance,
            lower_bound=lb,
            within_threshold=(er.distance <= threshold + 1e-9),
            moves=er.moves,
        ))
    stages.append(_stage("evaluate", "加权非限制性 DL 动态规划评估", t0, {
        "evaluated": len(evaluated),
    }))

    # 7. 阈值过滤 + 稳定排序
    t0 = time.perf_counter()
    within = [c for c in evaluated if c.within_threshold]
    # 稳定排序键：距离 -> 词（Unicode 码位序）-> 频次降序作为次级展示序。
    # 距离与词完全确定排序，频次只影响并列时的展示，不影响“谁在阈值内”。
    within.sort(key=lambda c: (c.distance, c.word, -c.freq))
    uncertain = [
        c for c in within
        if threshold != float("inf") and c.distance > threshold - uncertainty_margin
    ]
    returned = within[:limit]
    stages.append(_stage("rank", "阈值过滤与稳定排序", t0, {
        "within_threshold": len(within),
        "returned": len(returned),
        "near_threshold_uncertain": len(uncertain),
    }))

    exact_match = any(c.word == query for c in returned)

    return {
        "query": query,
        "version_id": index.version_id,
        "threshold": None if threshold == float("inf") else threshold,
        "uncertainty_margin": uncertainty_margin,
        "result_count": len(returned),
        "exact_match": exact_match,
        "candidates": [c.to_dict() for c in returned],
        "uncertain": [
            {"word": c.word, "distance": c.distance}
            for c in uncertain if c in returned
        ],
        "normalization": {
            "original": norm.original,
            "normalized": norm.normalized,
            "steps": [s.to_dict() for s in norm.steps],
        },
        "diagnostics": {
            "stages": [s.to_dict() for s in stages],
            "notes": notes,
            "index_size": index.size,
        },
    }
