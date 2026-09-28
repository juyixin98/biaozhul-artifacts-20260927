"""审计执行内核。

不依赖 FastAPI / SQLite，纯函数式，便于独立测试。输入是适配层的
:class:`FileModel`，输出 :class:`AuditResult`。

四条验收规则在这里落地：

1. **逻辑类型比较**：min/max 重算与相等判定走 :mod:`colstats.ordering`，
   浮点 NaN（区分符号）与有符号零单独定义；
2. **截断标志影响可信范围**：``MIN/MAX_IS_TRUNCATED`` 为真时只在"严格
   外侧"可用，等号落在边界上的剪枝被禁止（``can_prune`` 返回 UNDECIDABLE）；
3. **页级↔列块↔行组聚合关系校验**：列块统计必须等于其全部数据页的
   total-order 聚合（NULL 计数必须等于各页之和），不一致即拒绝；
4. **坏统计不得继续剪枝**：列一旦出现 ERROR，其 ``trusted`` 标志为
   False，``can_prune`` 强制返回 ``SCAN``。
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Literal

from . import ordering
from .models import (
    AuditResult,
    ChunkClaim,
    Claim,
    FileModel,
    Finding,
    GroundTruth,
    RowGroupClaim,
    Severity,
)
from .parquet_adapter import page_value_slices, read_column_values
from .ordering import (
    as_bytes,
    is_nan,
    is_negative_zero,
    order_key,
    total_max,
    total_min,
    values_equal,
)

# 失败类别（测试按这些具体 code 断言）
NULL_COUNT_MISSING = "NULL_COUNT_MISSING"
NULL_COUNT_MISMATCH = "NULL_COUNT_MISMATCH"
MIN_MAX_MISSING = "MIN_MAX_MISSING"
MIN_MISMATCH = "MIN_MISMATCH"
MAX_MISMATCH = "MAX_MISMATCH"
PAGE_AGGREGATION_MISMATCH = "PAGE_AGGREGATION_MISMATCH"
PAGE_NULL_SUM_MISMATCH = "PAGE_NULL_SUM_MISMATCH"
PAGE_VALUE_COUNT_MISMATCH = "PAGE_VALUE_COUNT_MISMATCH"
TRUNCATED_BUT_SHORTER = "TRUNCATED_BUT_SHORTER"
TRUNCATED_BOUND_OUTSIDE = "TRUNCATED_BOUND_OUTSIDE"
TRUSTED_AS_EXACT = "TRUSTED_AS_EXACT"
UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
SORTING_DECLARATION_VIOLATED = "SORTING_DECLARATION_VIOLATED"
SORTING_ORDER_UNDECIDABLE = "SORTING_ORDER_UNDECIDABLE"
GOOD_STATS = "GOOD_STATS"

# 剪枝决定
PruneDecision = Literal["PRUNE", "SCAN", "UNDECIDABLE"]

_PREDICATES = {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "not_null", "between"}


def compute_truth(values: list[Any], physical_type: str) -> GroundTruth:
    """从实际值独立重算真值（完全不看文件自报统计）。

    ``values`` 为 Python 列表，NULL 用 None 表示。

    浮点语义（Parquet stats 规范）：NaN 不参与 min/max，但单独记录
    ``contains_nan``（含 NaN 的列其范围统计只覆盖非 NaN 部分）；
    -0.0 与 +0.0 按 total order 区分。
    """
    non_null = [v for v in values if v is not None]
    null_count = len(values) - len(non_null)
    gt = GroundTruth(
        num_values=len(values),
        null_count=null_count,
        has_values=bool(non_null),
    )
    for v in non_null:
        if is_nan(v):
            gt.contains_nan = True
        if is_negative_zero(v):
            gt.contains_negative_zero = True
    finite_like = [v for v in non_null if not is_nan(v)]
    if finite_like:
        gt.min_value = total_min(finite_like, physical_type)
        gt.max_value = total_max(finite_like, physical_type)
        keys = [order_key(v, physical_type) for v in finite_like]
        gt.values_sorted_asc = all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1))
        gt.values_sorted_desc = all(keys[i] >= keys[i + 1] for i in range(len(keys) - 1))
    return gt


def _locator(rg: int, column: str, page: int | None = None) -> dict:
    loc = {"row_group": rg, "column": column}
    if page is not None:
        loc["page"] = page
    return loc


def _evidence(value: Any, physical_type: str, expose: bool) -> dict:
    """构造脱敏证据：默认只给类型/长度/指纹，不打印真实值。"""
    if not expose:
        import hashlib

        if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
            b = as_bytes(value)
            digest = hashlib.sha256(b).hexdigest()[:12]
            return {"kind": "bytes", "length": len(b), "sha256_12": digest}
        kind = (
            "f64" if physical_type == "DOUBLE" else
            "f32" if physical_type == "FLOAT" else
            "bool" if physical_type == "BOOLEAN" else
            physical_type.lower()
        )
        return {"kind": kind, "redacted": True}
    shown: Any = value
    if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        shown = as_bytes(value).hex()
    elif is_nan(value):
        shown = "-NaN" if is_negative_zero(value) else "NaN"
    return {"value": shown}


def _check_truncation(
    claim: Claim, truth: GroundTruth, physical_type: str, locator: dict,
    expose: bool, request_id: str | None,
) -> list[Finding]:
    """规则 2：校验截断标志与边界的可信范围。

    Parquet 规范（is_min/max_value_exact=false 时）的保证方向是：
    - 截断下界必须 **<= 真实最小值**（它只能把下界往左推）；
    - 截断上界必须 **>= 真实最大值**（它只能把上界往右推）。
    反方向越界会让外侧剪枝漏掉真实行，属于错误统计。

    非字节列出现截断标志同样视为错误（规范仅允许 BYTE_ARRAY 截断）。
    """
    findings: list[Finding] = []
    if not claim.has_min_max or not truth.has_values:
        return findings
    if physical_type not in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        if claim.min_truncated or claim.max_truncated:
            findings.append(
                Finding(
                    code=TRUNCATED_BOUND_OUTSIDE,
                    severity=Severity.ERROR,
                    locator=locator,
                    message="非字节列声明了截断标志，无法保证边界可信范围",
                    expected={"truncated": False},
                    observed={
                        "min_truncated": claim.min_truncated,
                        "max_truncated": claim.max_truncated,
                    },
                    request_id=request_id,
                )
            )
        return findings

    tminb, tmaxb = as_bytes(truth.min_value), as_bytes(truth.max_value)
    minb, maxb = as_bytes(claim.min_claim), as_bytes(claim.max_claim)

    if claim.min_truncated and not (minb <= tminb):
        findings.append(
            Finding(
                code=TRUNCATED_BOUND_OUTSIDE,
                severity=Severity.ERROR,
                locator={**locator, "bound": "min"},
                message=(
                    "截断的 min 声明大于真实最小值（应 <= 真实 min），"
                    "外侧剪枝会漏行"
                ),
                expected={"<=": _evidence(tminb, physical_type, expose)},
                observed=_evidence(minb, physical_type, expose),
                request_id=request_id,
            )
        )
    if claim.max_truncated and not (maxb >= tmaxb):
        findings.append(
            Finding(
                code=TRUNCATED_BOUND_OUTSIDE,
                severity=Severity.ERROR,
                locator={**locator, "bound": "max"},
                message=(
                    "截断的 max 声明小于真实最大值（应 >= 真实 max），"
                    "外侧剪枝会漏行"
                ),
                expected={">=": _evidence(tmaxb, physical_type, expose)},
                observed=_evidence(maxb, physical_type, expose),
                request_id=request_id,
            )
        )
    return findings


def _check_claim_against_truth(
    claim: Claim,
    truth: GroundTruth,
    physical_type: str,
    locator: dict,
    expose: bool,
    request_id: str | None,
) -> list[Finding]:
    """把一份统计声明与独立重算的真值逐项比对。"""
    findings: list[Finding] = []

    if not claim.has_null_count:
        findings.append(
            Finding(
                code=NULL_COUNT_MISSING,
                severity=Severity.WARNING,
                locator=locator,
                message="缺少 null_count 统计；IS NULL / IS NOT NULL 无法剪枝",
                expected={"null_count": truth.null_count},
                observed=None,
                request_id=request_id,
            )
        )
    elif claim.null_count != truth.null_count:
        findings.append(
            Finding(
                code=NULL_COUNT_MISMATCH,
                severity=Severity.ERROR,
                locator=locator,
                message=(
                    f"null_count 声明 {claim.null_count} 与实际 "
                    f"{truth.null_count} 不一致"
                ),
                expected={"null_count": truth.null_count},
                observed={"null_count": claim.null_count},
                request_id=request_id,
            )
        )

    if truth.has_values and not claim.has_min_max:
        findings.append(
            Finding(
                code=MIN_MAX_MISSING,
                severity=Severity.WARNING,
                locator=locator,
                message=(
                    "存在非 NULL 值但缺少 min/max 统计；范围与等值剪枝均不可用"
                ),
                expected={"min": _evidence(truth.min_value, physical_type, expose),
                          "max": _evidence(truth.max_value, physical_type, expose)},
                observed=None,
                request_id=request_id,
            )
        )
        return findings

    if claim.has_min_max and truth.has_values:
        # 精确边界必须等于真值；截断边界只校验覆盖方向（见 _check_truncation）
        if not claim.min_truncated and not values_equal(
            claim.min_claim, truth.min_value, physical_type
        ):
            findings.append(
                Finding(
                    code=MIN_MISMATCH,
                    severity=Severity.ERROR,
                    locator=locator,
                    message="min 统计与实际数据的 total-order 最小值不一致",
                    expected=_evidence(truth.min_value, physical_type, expose),
                    observed=_evidence(claim.min_claim, physical_type, expose),
                    request_id=request_id,
                )
            )
        if not claim.max_truncated and not values_equal(
            claim.max_claim, truth.max_value, physical_type
        ):
            findings.append(
                Finding(
                    code=MAX_MISMATCH,
                    severity=Severity.ERROR,
                    locator=locator,
                    message="max 统计与实际数据的 total-order 最大值不一致",
                    expected=_evidence(truth.max_value, physical_type, expose),
                    observed=_evidence(claim.max_claim, physical_type, expose),
                    request_id=request_id,
                )
            )
        findings.extend(
            _check_truncation(claim, truth, physical_type, locator, expose, request_id)
        )
    return findings


def _aggregate_page_claims(
    pages: list, physical_type: str
) -> Claim | None:
    """由页级声明聚合出"应有的列块声明"。

    任一相邻页缺少 min/max，则聚合结果没有 min/max；NULL 计数求和。
    聚合遵循 total order（NaN / 有符号零按逻辑序）。
    """
    if not pages:
        return None
    null_total = 0
    have_nulls = True
    mins: list[Any] = []
    maxs: list[Any] = []
    have_mm = True
    min_trunc = False
    max_trunc = False
    for page in pages:
        c = page.claim
        if c.has_null_count:
            null_total += c.null_count
        else:
            have_nulls = False
        if c.has_min_max:
            mins.append(c.min_claim)
            maxs.append(c.max_claim)
            min_trunc = min_trunc or c.min_truncated
            max_trunc = max_trunc or c.max_truncated
        else:
            have_mm = False
    agg = Claim(
        has_min_max=have_mm and bool(mins),
        has_null_count=have_nulls,
        null_count=null_total if have_nulls else None,
        min_truncated=min_trunc,
        max_truncated=max_trunc,
        num_values=sum(p.num_values for p in pages),
    )
    if agg.has_min_max:
        agg.min_claim = total_min(mins, physical_type)
        agg.max_claim = total_max(maxs, physical_type)
    return agg


def audit_file(
    model: FileModel,
    *,
    supported_types: tuple[str, ...] = (
        "BOOLEAN", "INT32", "INT64", "FLOAT", "DOUBLE", "BYTE_ARRAY",
    ),
    expose_values: bool = False,
    request_id: str | None = None,
) -> AuditResult:
    """对整个文件执行审计，给出 ACCEPTED / REJECTED / UNDECIDABLE。"""
    findings: list[Finding] = []
    trusted: dict[str, bool] = {}
    truncated_cols: set[str] = set()
    column_states: dict[str, list[Severity]] = {}

    def note(col: str, sev: Severity) -> None:
        column_states.setdefault(col, []).append(sev)

    for rg in model.row_groups:
        for chunk in rg.chunks:
            col = chunk.path
            loc = _locator(rg.row_group_index, col)
            if chunk.physical_type not in supported_types:
                findings.append(
                    Finding(
                        code=UNSUPPORTED_TYPE,
                        severity=Severity.WARNING,
                        locator=loc,
                        message=(
                            f"物理类型 {chunk.physical_type} 不在审计支持范围，"
                            "该列不允许统计剪枝"
                        ),
                        request_id=request_id,
                    )
                )
                note(col, Severity.WARNING)
                continue

            values = read_column_values(model.path, rg.row_group_index, col)
            truth = compute_truth(values, chunk.physical_type)

            # 1) 列块声明 vs 实际数据
            for f in _check_claim_against_truth(
                chunk.claim, truth, chunk.physical_type, loc,
                expose_values, request_id,
            ):
                findings.append(f)
                note(col, f.severity)
            if chunk.claim.min_truncated or chunk.claim.max_truncated:
                truncated_cols.add(col)

            # 2) 页级声明：每页对真值；页声明聚合后必须等于块声明（规则 3）
            slices = (
                page_value_slices(model, chunk, values)
                if all(c.max_repetition == 0 for c in model.schema if c.path == col)
                else None
            )
            page_null_sum = 0
            page_value_sum = 0
            have_all_page_null_counts = True
            for page in chunk.pages:
                ploc = _locator(rg.row_group_index, col, page.page_index)
                if slices is None:
                    findings.append(
                        Finding(
                            code=UNSUPPORTED_TYPE,
                            severity=Severity.WARNING,
                            locator=ploc,
                            message=(
                                "嵌套/重复列无法逐页切分真值，跳过页级聚合校验"
                            ),
                            request_id=request_id,
                        )
                    )
                    note(col, Severity.WARNING)
                    break
                page_truth = compute_truth(
                    slices[page.page_index], chunk.physical_type
                )
                page_value_sum += page_truth.num_values
                if page.claim.has_null_count:
                    page_null_sum += page.claim.null_count
                else:
                    have_all_page_null_counts = False
                for f in _check_claim_against_truth(
                    page.claim, page_truth, chunk.physical_type, ploc,
                    expose_values, request_id,
                ):
                    findings.append(f)
                    note(col, f.severity)
                if page.claim.min_truncated or page.claim.max_truncated:
                    truncated_cols.add(col)

            if slices is not None and chunk.pages:
                # num_values 之和必须与块 num_values 一致
                if page_value_sum != chunk.num_values:
                    findings.append(
                        Finding(
                            code=PAGE_VALUE_COUNT_MISMATCH,
                            severity=Severity.ERROR,
                            locator=loc,
                            message=(
                                f"各数据页 num_values 之和 {page_value_sum} 与列块 "
                                f"num_values {chunk.num_values} 不一致"
                            ),
                            expected={"sum_page_num_values": chunk.num_values},
                            observed={"sum_page_num_values": page_value_sum},
                            request_id=request_id,
                        )
                    )
                    note(col, Severity.ERROR)
                # NULL 计数聚合
                if have_all_page_null_counts and chunk.claim.has_null_count:
                    if page_null_sum != chunk.claim.null_count:
                        findings.append(
                            Finding(
                                code=PAGE_NULL_SUM_MISMATCH,
                                severity=Severity.ERROR,
                                locator=loc,
                                message=(
                                    f"各页 null_count 之和 {page_null_sum} 与列块 "
                                    f"null_count {chunk.claim.null_count} 不一致"
                                ),
                                expected={"page_null_sum": chunk.claim.null_count},
                                observed={"page_null_sum": page_null_sum},
                                request_id=request_id,
                            )
                        )
                        note(col, Severity.ERROR)
                # min/max 聚合关系（规则 3）：
                # 精确块边界必须等于各页聚合边界；截断块边界必须覆盖
                # 页聚合结果（trunc_min <= agg_min，trunc_max >= agg_max）。
                agg = _aggregate_page_claims(chunk.pages, chunk.physical_type)
                if agg is not None and agg.has_min_max and chunk.claim.has_min_max:
                    ptype = chunk.physical_type
                    k_amin = order_key(agg.min_claim, ptype)
                    k_amax = order_key(agg.max_claim, ptype)
                    k_cmin = order_key(chunk.claim.min_claim, ptype)
                    k_cmax = order_key(chunk.claim.max_claim, ptype)
                    if chunk.claim.min_truncated:
                        min_ok = k_cmin <= k_amin
                    else:
                        min_ok = k_cmin == k_amin
                    if chunk.claim.max_truncated:
                        max_ok = k_cmax >= k_amax
                    else:
                        max_ok = k_cmax == k_amax
                    if not (min_ok and max_ok):
                        findings.append(
                            Finding(
                                code=PAGE_AGGREGATION_MISMATCH,
                                severity=Severity.ERROR,
                                locator=loc,
                                message=(
                                    "列块 min/max 与各数据页统计的 total-order "
                                    "聚合结果不一致"
                                ),
                                expected={
                                    "aggregated_min": _evidence(
                                        agg.min_claim, chunk.physical_type, expose_values
                                    ),
                                    "aggregated_max": _evidence(
                                        agg.max_claim, chunk.physical_type, expose_values
                                    ),
                                },
                                observed={
                                    "chunk_min": _evidence(
                                        chunk.claim.min_claim,
                                        chunk.physical_type, expose_values,
                                    ),
                                    "chunk_max": _evidence(
                                        chunk.claim.max_claim,
                                        chunk.physical_type, expose_values,
                                    ),
                                },
                                request_id=request_id,
                            )
                        )
                        note(col, Severity.ERROR)
                elif agg is not None and agg.has_min_max and not chunk.claim.has_min_max:
                    # 页有统计而块缺统计：聚合可恢复 -> 警告
                    findings.append(
                        Finding(
                            code=MIN_MAX_MISSING,
                            severity=Severity.WARNING,
                            locator=loc,
                            message="列块缺少 min/max，但各页统计完整，仅允许页级/全扫",
                            request_id=request_id,
                        )
                    )
                    note(col, Severity.WARNING)

        # 3) 排序声明校验
        for sorting in rg.sorting:
            col_path = model.schema[sorting.column_idx].path
            values = read_column_values(
                model.path, rg.row_group_index, col_path
            )
            truth = compute_truth(
                values,
                model.schema[sorting.column_idx].physical_type,
            )
            sloc = _locator(rg.row_group_index, col_path)
            if not truth.has_values:
                continue  # 全 NULL 行组对排序无约束
            expected_asc = not sorting.descending
            ok = truth.values_sorted_asc if expected_asc else truth.values_sorted_desc
            if not ok:
                findings.append(
                    Finding(
                        code=SORTING_DECLARATION_VIOLATED,
                        severity=Severity.ERROR,
                        locator={**sloc, "sorting": "sorting_columns"},
                        message=(
                            "页脚 sorting_columns 声明 "
                            f"{'降序' if sorting.descending else '升序'}，"
                            "但实际数据不满足该顺序"
                        ),
                        expected={
                            "order": "desc" if sorting.descending else "asc",
                            "nulls_first": sorting.nulls_first,
                        },
                        observed={"order": "unsorted"},
                        request_id=request_id,
                    )
                )
                note(col_path, Severity.ERROR)

    # 列可信标志：任何 ERROR 都禁用该列剪枝（规则 4）；WARNING 降级为
    # UNDECIDABLE（不剪枝但不算文件损坏）。
    for col_schema in model.schema:
        path = col_schema.path
        sevs = column_states.get(path, [])
        trusted[path] = Severity.ERROR not in sevs
    has_error = any(
        f.severity == Severity.ERROR for f in findings
    )
    has_warning = any(
        f.severity == Severity.WARNING for f in findings
    )
    if has_error:
        verdict: str = "REJECTED"
    elif has_warning:
        verdict = "UNDECIDABLE"
    else:
        verdict = "ACCEPTED"

    summary = {
        "num_rows": model.num_rows,
        "num_row_groups": len(model.row_groups),
        "num_columns": len(model.schema),
        "num_findings": len(findings),
        "num_errors": sum(f.severity == Severity.ERROR for f in findings),
        "num_warnings": sum(f.severity == Severity.WARNING for f in findings),
        "trusted_columns": [c for c, ok in trusted.items() if ok],
        "untrusted_columns": [c for c, ok in trusted.items() if not ok],
        "truncated_columns": sorted(truncated_cols),
        "accepted_columns": [],
    }
    if verdict == "ACCEPTED":
        summary["accepted_columns"] = [c.path for c in model.schema]
        findings.append(
            Finding(
                code=GOOD_STATS,
                severity=Severity.INFO,
                locator={"file": model.path},
                message=(
                    "所有列块与数据页的 min/max/null_count 与实际数据一致，"
                    "页→块聚合关系成立，排序声明有效"
                ),
                request_id=request_id,
            )
        )

    return AuditResult(
        path=model.path,
        verdict=verdict,  # type: ignore[arg-type]
        audit_id=uuid.uuid4().hex,
        findings=findings,
        trusted=trusted,
        truncated_columns=sorted(truncated_cols),
        summary=summary,
    )


# ---------------------------------------------------------------- 剪枝


def can_prune(
    claim: Claim,
    physical_type: str,
    predicate: str,
    value: Any = None,
    *,
    trusted: bool = True,
    value_high: Any = None,
) -> PruneDecision:
    """根据一份统计声明判断能否跳过该数据范围。

    返回：
    - ``PRUNE``：可安全跳过（范围严格不相交）；
    - ``SCAN``：确定需要扫描（统计无法排除匹配行）；
    - ``UNDECIDABLE``：统计缺失/被截断/不可信，规则 2、4 下不得剪枝。

    规则 2 的截断语义：被截断的一侧只能用于 *严格外侧* 判断，谓词值
    恰好等于声明边界时一律 UNDECIDABLE。规则 4：``trusted=False`` 时
    立即 UNDECIDABLE，绝不基于坏统计剪枝。
    """
    if predicate not in _PREDICATES:
        raise ValueError(f"未知谓词 {predicate!r}，支持 {sorted(_PREDICATES)}")
    if not trusted:
        return "UNDECIDABLE"

    if predicate == "is_null":
        if not claim.has_null_count:
            return "UNDECIDABLE"
        return "PRUNE" if claim.null_count == 0 else "SCAN"
    if predicate == "not_null":
        if not claim.has_null_count:
            return "UNDECIDABLE"
        return "PRUNE" if claim.null_count == claim.num_values else "SCAN"

    if not claim.has_min_max:
        return "UNDECIDABLE"

    kmin = order_key(claim.min_claim, physical_type)
    kmax = order_key(claim.max_claim, physical_type)

    def trunc_low(k: Any) -> PruneDecision:
        """k == kmin 且下界被截断 -> 无法判定；否则可安全剪枝。"""
        if k == kmin and claim.min_truncated:
            return "UNDECIDABLE"
        return "PRUNE"

    def trunc_high(k: Any) -> PruneDecision:
        """k == kmax 且上界被截断 -> 无法判定；否则可安全剪枝。"""
        if k == kmax and claim.max_truncated:
            return "UNDECIDABLE"
        return "PRUNE"

    if predicate == "between":
        if value_high is None:
            raise ValueError("between 谓词需要 value_high")
        klo = order_key(value, physical_type)
        khi = order_key(value_high, physical_type)
        # 查询区间 [lo,hi] 与数据范围 [kmin,kmax] 不相交才可剪
        if khi < kmin:
            return "PRUNE"
        if khi == kmin:
            return trunc_low(khi)
        if klo > kmax:
            return "PRUNE"
        if klo == kmax:
            return trunc_high(klo)
        return "SCAN"

    kv = order_key(value, physical_type)

    if predicate == "eq":
        # 精确边界上的值可能真实存在 -> 必须 SCAN；严格外侧才剪。
        if kv < kmin:
            return "PRUNE"
        if kv == kmin:
            return trunc_low(kv) if claim.min_truncated else "SCAN"
        if kv > kmax:
            return "PRUNE"
        if kv == kmax:
            return trunc_high(kv) if claim.max_truncated else "SCAN"
        return "SCAN"
    if predicate == "ne":
        # 仅当整个精确范围恒等于 value 时 ne 才无匹配
        if claim.exact and kmin == kmax == kv:
            return "PRUNE"
        return "SCAN"
    if predicate == "lt":
        # 保留 x < value：value <= min 时无匹配（value==min 亦无 x<min）
        if kv < kmin:
            return "PRUNE"
        if kv == kmin:
            return trunc_low(kv)
        return "SCAN"
    if predicate == "le":
        # 保留 x <= value：value < min 时无匹配；value==min 可能命中
        if kv < kmin:
            return "PRUNE"
        if kv == kmin and claim.min_truncated:
            return "UNDECIDABLE"
        return "SCAN"
    if predicate == "gt":
        # 保留 x > value：value >= max 时无匹配（value==max 亦无 x>max）
        if kv > kmax:
            return "PRUNE"
        if kv == kmax:
            return trunc_high(kv)
        return "SCAN"
    if predicate == "ge":
        # 保留 x >= value：value > max 时无匹配；value==max 可能命中
        if kv > kmax:
            return "PRUNE"
        if kv == kmax and claim.max_truncated:
            return "UNDECIDABLE"
        return "SCAN"
    return "UNDECIDABLE"
