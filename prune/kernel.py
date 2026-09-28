"""Pruning kernel: conservative two-level elimination.

Level 1 — directory partitions: the predicate is *inverted through the partition
transform* into a conservative set of candidate partition labels. Partition
values are transforms (e.g. month "2024-03"), never bucket numbers; label spans
are computed with month arithmetic that is safe for negative (pre-1970)
timestamps.

Level 2 — per-file statistics: each surviving file's min/max/null-count stats
are tested against the predicate. Missing or possibly-truncated stats keep the
file (UNKNOWN), never prune it.

Verdicts are tri-state:
  KEPT    — a match is possible (or certain); file/partition must be scanned
  PRUNED  — proven impossible for any row to match; reason attached
  UNKNOWN — stats/version insufficient; kept, and reported under "uncertain"

Every PRUNED target carries a machine-readable reason code plus a human-detail
string explaining *why* a match is impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from . import values as V
from .models import Leaf, Op, Predicate
from .transforms import MonthTransform, TRANSFORM_SPEC_VERSION, tzdb_version


class Verdict(str, Enum):
    KEPT = "KEPT"
    PRUNED = "PRUNED"
    UNKNOWN = "UNKNOWN"


class Reason(str, Enum):
    # partition level
    PARTITION_OUTSIDE_CANDIDATES = "PARTITION_OUTSIDE_CANDIDATES"
    PARTITION_NULL_EXCLUDED = "PARTITION_NULL_EXCLUDED"
    TRANSFORM_VERSION_MISMATCH = "TRANSFORM_VERSION_MISMATCH"
    # file level — uncertain (file kept)
    FILE_STATS_MISSING = "FILE_STATS_MISSING"
    FILE_STATS_TRUNCATED = "FILE_STATS_TRUNCATED"
    FILE_STATS_TYPE_MISMATCH = "FILE_STATS_TYPE_MISMATCH"
    FILE_NULL_COUNT_UNKNOWN = "FILE_NULL_COUNT_UNKNOWN"
    FILE_COLUMN_ABSENT = "FILE_COLUMN_ABSENT"
    FILE_STATS_VERSION_MISMATCH = "FILE_STATS_VERSION_MISMATCH"
    FILE_BOUNDARY_INCONCLUSIVE = "FILE_BOUNDARY_INCONCLUSIVE"
    # file level — certain prune
    FILE_ALL_NULL = "FILE_ALL_NULL"
    FILE_NO_NULL = "FILE_NO_NULL"
    FILE_ABOVE_MAX = "FILE_ABOVE_MAX"
    FILE_NE_ALL_EQUAL = "FILE_NE_ALL_EQUAL"
    FILE_IN_NO_MATCH = "FILE_IN_NO_MATCH"
    FILE_BELOW_MIN = "FILE_BELOW_MIN"


class UnknownColumnError(KeyError):
    """Predicate references a column the table does not know."""


class LiteralError(ValueError):
    """Predicate literal cannot be interpreted on the column domain."""


# Tri-state for "must the NULL bucket be considered":
#   FALSE  - proven that NULLs do not match
#   TRUE   - proven that only NULLs can match
#   UNKNOWN- NULLs may or may not match
_T_FALSE, _T_UNKNOWN, _T_TRUE = 0, 1, 2


@dataclass(frozen=True)
class Justification:
    code: str
    detail: str
    leaf: Optional[str] = None  # "column OP" attribution


@dataclass
class Decision:
    verdict: Verdict
    reasons: List[Justification] = field(default_factory=list)

    @property
    def kept(self) -> bool:
        return self.verdict is not Verdict.PRUNED


# ---------------------------------------------------------------------------
# Catalog-side data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    name: str
    type: str  # one of values domain tags


@dataclass
class ColumnStat:
    minimum: Any = None
    maximum: Any = None
    null_count: Optional[int] = None
    min_truncated: bool = False
    max_truncated: bool = False
    present: bool = True


@dataclass
class FileStat:
    path: str
    num_rows: int
    size_bytes: int
    row_groups: int
    stats: Dict[str, ColumnStat]
    stats_version: str          # stats schema version recorded at refresh
    pyarrow_version: str


@dataclass
class PartitionInfo:
    label: str
    is_null: bool
    files: List[FileStat]


@dataclass
class TableContext:
    name: str
    columns: Dict[str, Column]
    transform: Optional[MonthTransform]
    partitions: List[PartitionInfo]
    recorded_transform_version: Optional[str] = None
    recorded_tzdb_version: Optional[str] = None


# ---------------------------------------------------------------------------
# Level 1: conservative candidate partitions
#
# Candidates are a closed month-label interval [lo, hi] (either end may be
# open) over NON-null partitions, plus a tri-state verdict for the explicit
# NULL bucket. Interval endpoints are *labels derived from original values*
# via the transform — never bucket indices compared with literals.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidates:
    lo: Optional[str] = None    # inclusive month label, None => -inf
    hi: Optional[str] = None    # inclusive month label, None => +inf
    null_bucket: int = _T_FALSE
    exact: bool = True          # False => interval is already an over-approximation

    @staticmethod
    def all(null_bucket: int = _T_UNKNOWN) -> "Candidates":
        return Candidates(None, None, null_bucket, exact=True)

    @staticmethod
    def empty(null_bucket: int = _T_FALSE) -> "Candidates":
        return Candidates("", "", null_bucket, exact=True)

    def contains_label(self, label: str) -> bool:
        if self.lo is not None and label < self.lo:
            return False
        if self.hi is not None and label > self.hi:
            return False
        return True

    def is_empty_nonnull(self) -> bool:
        return self.lo == "" and self.hi == "" and self.lo is not None


def _meet(a: Candidates, b: Candidates) -> Candidates:
    """Intersection (AND): lo=max(lower bounds), hi=min(upper bounds)."""
    lo = a.lo if b.lo is None else (b.lo if a.lo is None else max(a.lo, b.lo))
    hi = a.hi if b.hi is None else (b.hi if a.hi is None else min(a.hi, b.hi))
    empty = lo is not None and hi is not None and lo > hi
    return Candidates(
        "" if empty else lo,
        "" if empty else hi,
        _meet_tri(a.null_bucket, b.null_bucket),
        exact=a.exact and b.exact,
    )


def _join(a: Candidates, b: Candidates) -> Candidates:
    """Union (OR): interval hull, which stays a conservative cover."""
    if a.is_empty_nonnull():
        return b
    if b.is_empty_nonnull():
        return a
    # lo: None means -inf; hi: None means +inf.
    lo = None if (a.lo is None or b.lo is None) else min(a.lo, b.lo)
    hi = None if (a.hi is None or b.hi is None) else max(a.hi, b.hi)
    return Candidates(
        lo, hi,
        _join_tri(a.null_bucket, b.null_bucket),
        exact=False,  # hull may include labels neither branch allows
    )


def _meet_tri(a: int, b: int) -> int:
    table = {
        (_T_FALSE, _T_FALSE): _T_FALSE,
        (_T_FALSE, _T_TRUE): _T_FALSE,
        (_T_TRUE, _T_FALSE): _T_FALSE,
        (_T_TRUE, _T_TRUE): _T_TRUE,
    }
    return table.get((a, b), _T_UNKNOWN)


def _join_tri(a: int, b: int) -> int:
    if a == _T_TRUE or b == _T_TRUE:
        return _T_TRUE if (a == _T_TRUE and b == _T_TRUE) else _T_UNKNOWN
    if a == _T_FALSE and b == _T_FALSE:
        return _T_FALSE
    return _T_UNKNOWN


def _canon_scalar(leaf: Leaf, col: Column, raw: Any) -> Any:
    try:
        return V.canonical(raw, col.type)
    except ValueError:
        raise LiteralError(f"column {leaf.column!r}: cannot interpret {raw!r} as {col.type}") from None


def _leaf_candidates(leaf: Leaf, ctx: TableContext) -> Candidates:
    t = ctx.transform
    # Predicates on other columns cannot restrict this partitioning at all.
    if t is None or leaf.column != t.source_column:
        return Candidates.all()

    col = ctx.columns[leaf.column]

    if leaf.op is Op.IS_NULL:
        if leaf.negated:                       # IS NOT NULL: every non-null
            return Candidates.all(_T_FALSE)    # partition may match; null cannot
        return Candidates.empty(_T_TRUE)      # only null bucket

    if leaf.op is Op.EQ:
        v = _canon_scalar(leaf, col, leaf.value)
        lab = t.apply(v)
        return Candidates(lab, lab, _T_FALSE)

    if leaf.op is Op.NE:
        _canon_scalar(leaf, col, leaf.value)  # validate type
        # All non-null labels except possibly one; the cheapest sound choice.
        return Candidates.all(_T_FALSE)

    if leaf.op in (Op.GE, Op.GT):
        v = _canon_scalar(leaf, col, leaf.value)
        lab = t.apply(v)
        # Even GT at the last instant of a month keeps that same month
        # (DST-safe: transform is applied to the literal itself).
        return Candidates(lab, None, _T_FALSE)

    if leaf.op in (Op.LE, Op.LT):
        v = _canon_scalar(leaf, col, leaf.value)
        lab = t.apply(v)
        return Candidates(None, lab, _T_FALSE)

    if leaf.op is Op.BETWEEN:
        lo = _canon_scalar(leaf, col, leaf.value[0])
        hi = _canon_scalar(leaf, col, leaf.value[1])
        a, b = t.month_span_for_bounds(lo, hi)
        if a is not None and b is not None and a > b:
            return Candidates.empty(_T_FALSE)
        return Candidates(a, b, _T_FALSE)

    if leaf.op is Op.IN:
        labels = []
        for raw in leaf.value:
            labels.append(t.apply(_canon_scalar(leaf, col, raw)))
        labels.sort()
        return Candidates(labels[0], labels[-1], _T_FALSE, exact=False)

    return Candidates.all()


def _tree_candidates(node: Predicate, ctx: TableContext) -> Candidates:
    if isinstance(node, Leaf):
        return _leaf_candidates(node, ctx)
    combine = _meet if node.op is Op.AND else _join
    acc = None
    for child in node.children:
        c = _tree_candidates(child, ctx)
        acc = c if acc is None else combine(acc, c)
    return acc


# ---------------------------------------------------------------------------
# Level 2: file statistics
# ---------------------------------------------------------------------------

def _leaf_name(leaf: Leaf) -> str:
    return f"{leaf.column} {leaf.op.value}" + (" NOT" if leaf.op is Op.IS_NULL and leaf.negated else "")


def _uncertain(leaf: Leaf, code: Reason, detail: str) -> Decision:
    return Decision(Verdict.UNKNOWN, [Justification(code.value, detail, _leaf_name(leaf))])


def _pruned(leaf: Leaf, code: Reason, detail: str) -> Decision:
    return Decision(Verdict.PRUNED, [Justification(code.value, detail, _leaf_name(leaf))])


def _and_decisions(ds: List[Decision]) -> Decision:
    # A conjunction is proven false if ANY branch is proven false.
    pruned = [d for d in ds if d.verdict is Verdict.PRUNED]
    if pruned:
        return Decision(Verdict.PRUNED, _dedup_reasons(
            r for d in pruned for r in d.reasons))
    if any(d.verdict is Verdict.UNKNOWN for d in ds):
        return Decision(Verdict.UNKNOWN, _dedup_reasons(
            r for d in ds for r in d.reasons if d.verdict is Verdict.UNKNOWN))
    return Decision(Verdict.KEPT)


def _or_decisions(ds: List[Decision]) -> Decision:
    # A disjunction is proven false only if EVERY branch is proven false.
    if all(d.verdict is Verdict.PRUNED for d in ds):
        return Decision(Verdict.PRUNED, _dedup_reasons(
            r for d in ds for r in d.reasons))
    if any(d.verdict is Verdict.KEPT for d in ds):
        return Decision(Verdict.KEPT)
    return Decision(Verdict.UNKNOWN, _dedup_reasons(
        r for d in ds for r in d.reasons if d.verdict is not Verdict.KEPT))


def _dedup_reasons(reasons) -> List[Justification]:
    seen, out = set(), []
    for r in reasons:
        key = (r.code, r.leaf, r.detail)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _sound_bounds(cs: ColumnStat, col: Column) -> Tuple[Any, Any, Any, Any]:
    """Return (stored_min, stored_max, sound_min, sound_max), canonicalized.

    A stat is a *sound* bound only when recorded in full. A truncated UTF8
    extremum (a legacy writer persisted only a prefix) is NOT a sound bound:
    there is no character that can be appended to a prefix to guarantee it
    orders above every longer string sharing that prefix, so the affected side
    is treated as unknown rather than padded. That is exactly the "possibly
    truncated -> keep the file" rule.

    Concretely:
      * min_truncated => sound_min = None (no usable lower bound)
      * max_truncated => sound_max = None (no usable upper bound)
      * the other side stays usable.
    Raises LiteralError on domain mismatch.
    """
    def canon(raw):
        try:
            return V.canonical(raw, col.type)
        except ValueError:
            raise LiteralError(f"stat {raw!r} not on domain {col.type}") from None

    rmin = canon(cs.minimum) if cs.minimum is not None else None
    rmax = canon(cs.maximum) if cs.maximum is not None else None
    smin = None if cs.min_truncated else rmin
    smax = None if cs.max_truncated else rmax
    return rmin, rmax, smin, smax


def _eval_leaf_on_stats(leaf: Leaf, cs: ColumnStat, col: Column, num_rows: int) -> Decision:
    nc = cs.null_count

    # ---- IS NULL / IS NOT NULL (null-count driven, min/max irrelevant) -----
    if leaf.op is Op.IS_NULL:
        if leaf.negated:
            if nc is not None and num_rows > 0 and nc == num_rows:
                return _pruned(leaf, Reason.FILE_ALL_NULL,
                               f"all {num_rows} values are NULL, IS NOT NULL cannot match")
            if nc is None:
                return _uncertain(leaf, Reason.FILE_NULL_COUNT_UNKNOWN,
                                  "null_count not recorded; cannot prove a non-NULL exists")
            return Decision(Verdict.KEPT)
        if nc is None:
            return _uncertain(leaf, Reason.FILE_NULL_COUNT_UNKNOWN,
                              "null_count not recorded; cannot prove NULL absent")
        if num_rows > 0 and nc == 0:
            return _pruned(leaf, Reason.FILE_NO_NULL,
                           "null_count=0: this file contains no NULL in the column")
        return Decision(Verdict.KEPT)

    # ---- comparison leaves -------------------------------------------------
    # Files whose non-null domain is empty (all NULL) cannot match any
    # comparison, regardless of min/max.
    all_null = nc is not None and num_rows > 0 and nc == num_rows
    if all_null:
        return _pruned(leaf, Reason.FILE_ALL_NULL,
                       f"all {num_rows} values are NULL, {leaf.op.value} cannot match")

    if cs.minimum is None and cs.maximum is None:
        if nc is None:
            return _uncertain(leaf, Reason.FILE_STATS_MISSING,
                              "no min/max and no null_count recorded for column")
        # null_count < num_rows means non-nulls exist, but their domain unknown.
        return _uncertain(leaf, Reason.FILE_STATS_MISSING,
                          "no min/max recorded for non-null values")

    try:
        rmin, rmax, smin, smax = _sound_bounds(cs, col)
    except LiteralError:
        return _uncertain(leaf, Reason.FILE_STATS_TYPE_MISMATCH,
                          f"stored stat is not on domain {col.type}")
    trunc = bool(cs.min_truncated or cs.max_truncated)
    c = V.cmp  # noqa: N806
    lit = lambda v: _canon_scalar(leaf, col, v)  # noqa: E731

    def trunc_uncertain(detail: str) -> Decision:
        return _uncertain(leaf, Reason.FILE_STATS_TRUNCATED, detail)

    if leaf.op in (Op.GE, Op.GT):
        v = lit(leaf.value)
        if smax is not None and c(smax, v) is not None and c(smax, v) < 0:
            return _pruned(leaf, Reason.FILE_ABOVE_MAX,
                           f"sound max {_fmt(smax)} < {leaf.op.value} literal {_fmt(v)}")
        if smax is None:
            if cs.max_truncated:
                return trunc_uncertain(
                    f"max stat truncated to prefix {_fmt(rmax)!r}; no usable "
                    f"upper bound to compare against {leaf.op.value} {_fmt(v)}")
            return _uncertain(leaf, Reason.FILE_STATS_MISSING, "max missing")
        # Non-truncated exact bound: GE on equality is a certain keep; only
        # strict GT at max == literal cannot prove a greater value exists.
        if leaf.op is Op.GT and c(smax, v) == 0:
            return _uncertain(leaf, Reason.FILE_BOUNDARY_INCONCLUSIVE,
                              f"max {_fmt(smax)} equals GT literal {_fmt(v)}; "
                              f"no greater value provable from the bound")
        return Decision(Verdict.KEPT)

    if leaf.op in (Op.LE, Op.LT):
        v = lit(leaf.value)
        if smin is not None and c(smin, v) > 0:
            return _pruned(leaf, Reason.FILE_BELOW_MIN,
                           f"sound min {_fmt(smin)} > {leaf.op.value} literal {_fmt(v)}")
        if smin is None:
            if cs.min_truncated:
                return trunc_uncertain(
                    f"min stat truncated to prefix {_fmt(rmin)!r}; no usable "
                    f"lower bound to compare against {leaf.op.value} {_fmt(v)}")
            return _uncertain(leaf, Reason.FILE_STATS_MISSING, "min missing")
        if leaf.op is Op.LT and c(smin, v) == 0:
            return _uncertain(leaf, Reason.FILE_BOUNDARY_INCONCLUSIVE,
                              f"min {_fmt(smin)} equals LT literal {_fmt(v)}; "
                              f"no smaller value provable from the bound")
        return Decision(Verdict.KEPT)

    if leaf.op is Op.EQ:
        v = lit(leaf.value)
        # Disjointness can be proved from whichever sound bound exists, even
        # when the opposite side is missing/truncated.
        if smin is not None and c(smin, v) > 0:
            return _pruned(leaf, Reason.FILE_BELOW_MIN,
                           f"sound min {_fmt(smin)} > EQ literal {_fmt(v)}")
        if smax is not None and c(smax, v) < 0:
            return _pruned(leaf, Reason.FILE_ABOVE_MAX,
                           f"sound max {_fmt(smax)} < EQ literal {_fmt(v)}")
        # Literal falls inside the sound hull (or the hull is open): we can
        # only claim a definite keep when BOTH bounds are intact.
        if cs.min_truncated or cs.max_truncated:
            return trunc_uncertain(
                f"min/max stat truncated around EQ literal {_fmt(v)}; "
                f"membership unprovable")
        if smin is None or smax is None:
            return _uncertain(leaf, Reason.FILE_STATS_MISSING,
                              "min/max incomplete around EQ literal; "
                              "membership unprovable")
        return Decision(Verdict.KEPT)

    if leaf.op is Op.NE:
        v = lit(leaf.value)
        if (not trunc and smin is not None and smax is not None
                and c(smin, smax) == 0 and c(smin, v) == 0):
            return _pruned(leaf, Reason.FILE_NE_ALL_EQUAL,
                           f"all non-null values equal {_fmt(v)}, NE excludes every row")
        return Decision(Verdict.KEPT)

    if leaf.op is Op.BETWEEN:
        lo, hi = lit(leaf.value[0]), lit(leaf.value[1])
        if c(lo, hi) is not None and c(lo, hi) > 0:
            return _pruned(leaf, Reason.FILE_BELOW_MIN,
                           f"empty BETWEEN range {_fmt(lo)}..{_fmt(hi)}")
        if smax is not None and c(smax, lo) < 0:
            return _pruned(leaf, Reason.FILE_ABOVE_MAX,
                           f"sound max {_fmt(smax)} < BETWEEN low {_fmt(lo)}")
        if smin is not None and c(smin, hi) > 0:
            return _pruned(leaf, Reason.FILE_BELOW_MIN,
                           f"sound min {_fmt(smin)} > BETWEEN high {_fmt(hi)}")
        if cs.min_truncated or cs.max_truncated:
            return trunc_uncertain(
                "truncated stats overlap the BETWEEN window; unprovable")
        if smin is None or smax is None:
            return _uncertain(leaf, Reason.FILE_STATS_MISSING,
                              "cannot prove file domain disjoint from [low, high]")
        return Decision(Verdict.KEPT)

    if leaf.op is Op.IN:
        vs = [lit(x) for x in leaf.value]
        if smax is not None and all(c(smax, v) is not None and c(smax, v) < 0 for v in vs):
            return _pruned(leaf, Reason.FILE_IN_NO_MATCH,
                           f"every IN literal is above sound max {_fmt(smax)}")
        if smin is not None and all(c(smin, v) > 0 for v in vs):
            return _pruned(leaf, Reason.FILE_IN_NO_MATCH,
                           f"every IN literal is below sound min {_fmt(smin)}")
        if cs.min_truncated or cs.max_truncated:
            return trunc_uncertain(
                "truncated stats overlap the IN set; membership unprovable")
        if smin is None or smax is None:
            return _uncertain(leaf, Reason.FILE_STATS_MISSING,
                              "min/max incomplete around IN set")
        return Decision(Verdict.KEPT)

    return Decision(Verdict.UNKNOWN)  # pragma: no cover


def _fmt(v: Any) -> str:
    import datetime as _dt
    if isinstance(v, _dt.datetime):
        return V.json_default(v)
    return repr(v)


def _eval_tree_on_stats(node: Predicate, file: FileStat, ctx: TableContext) -> Decision:
    if isinstance(node, Leaf):
        col = ctx.columns.get(node.column)
        if col is None:
            raise UnknownColumnError(node.column)
        cs = file.stats.get(node.column)
        if cs is None or not cs.present:
            return Decision(Verdict.UNKNOWN, [
                Justification(Reason.FILE_COLUMN_ABSENT.value,
                              f"column {node.column!r} has no stats in file "
                              f"(adapter {file.stats_version}/pyarrow {file.pyarrow_version})",
                              _leaf_name(node))])
        if file.stats_version != STATS_SCHEMA_VERSION:
            return _uncertain(node, Reason.FILE_STATS_VERSION_MISMATCH,
                              f"stats written by schema {file.stats_version}, "
                              f"kernel expects {STATS_SCHEMA_VERSION}")
        return _eval_leaf_on_stats(node, cs, col, file.num_rows)

    ds = [_eval_tree_on_stats(c, file, ctx) for c in node.children]
    return _and_decisions(ds) if node.op is Op.AND else _or_decisions(ds)


STATS_SCHEMA_VERSION = "colstats-v1"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class FileResult:
    path: str
    partition: str
    verdict: str
    reasons: List[Justification] = field(default_factory=list)
    num_rows: int = 0
    size_bytes: int = 0


@dataclass
class PartitionResult:
    label: str
    is_null: bool
    verdict: str
    reason: Optional[Justification]
    files: List[FileResult]


@dataclass
class Plan:
    table: str
    request_id: str
    transform_version: str
    tzdb_version: str
    stats_schema_version: str
    candidates: dict
    partitions: List[PartitionResult]
    uncertain: List[dict]
    failures: List[dict]
    metrics: dict


def plan_prune(ctx: TableContext, predicate: Predicate, request_id: str) -> Plan:
    tzver = tzdb_version()

    # Version guards: cataloged partitions must have been produced with the
    # transform code + tzdb we are about to invert with. Mismatch => keep all.
    version_failures: List[dict] = []
    version_uncertain = False
    if ctx.transform is not None:
        if ctx.recorded_transform_version != TRANSFORM_SPEC_VERSION:
            version_failures.append({
                "code": Reason.TRANSFORM_VERSION_MISMATCH.value,
                "detail": f"catalog partitions built with transform "
                          f"{ctx.recorded_transform_version!r}, kernel runs "
                          f"{TRANSFORM_SPEC_VERSION!r}",
            })
            version_uncertain = True
        if ctx.recorded_tzdb_version != tzver:
            version_failures.append({
                "code": Reason.TRANSFORM_VERSION_MISMATCH.value,
                "detail": f"catalog built with tzdb {ctx.recorded_tzdb_version!r}, "
                          f"runtime tzdb {tzver!r}",
            })
            version_uncertain = True

    if version_uncertain:
        cand = Candidates.all()
    else:
        cand = _tree_candidates(predicate, ctx)

    part_results: List[PartitionResult] = []
    uncertain: List[dict] = []
    total_files = kept_files = pruned_files = 0
    total_rows = scanned_rows_pruned = 0
    total_bytes = scanned_bytes_pruned = 0
    considered_partitions = kept_partitions = pruned_partitions = 0

    for part in ctx.partitions:
        considered_partitions += 1
        if version_uncertain:
            pverdict, preason = Verdict.UNKNOWN, Justification(
                Reason.TRANSFORM_VERSION_MISMATCH.value,
                "version mismatch; partition retained conservatively")
        elif part.is_null:
            if cand.null_bucket == _T_FALSE:
                pverdict, preason = Verdict.PRUNED, Justification(
                    Reason.PARTITION_NULL_EXCLUDED,
                    "predicate provably rejects NULL on the partition column")
            else:
                pverdict, preason = Verdict.KEPT, None
                if cand.null_bucket == _T_UNKNOWN:
                    uncertain.append({"target": f"partition:{part.label}",
                                      "code": "NULL_BUCKET_POSSIBLE",
                                      "detail": "null bucket may match; retained"})
        else:
            if not cand.contains_label(part.label):
                pverdict, preason = Verdict.PRUNED, Justification(
                    Reason.PARTITION_OUTSIDE_CANDIDATES,
                    f"partition label {part.label!r} outside conservative candidate "
                    f"month span [{cand.lo or '-inf'}, {cand.hi or '+inf'}]"
                    + (" (over-approximate hull)" if not cand.exact else ""))
            else:
                pverdict, preason = Verdict.KEPT, None

        if pverdict is Verdict.PRUNED:
            pruned_partitions += 1
            part_results.append(PartitionResult(part.label, part.is_null,
                                                pverdict.value, preason, []))
            for f in part.files:
                total_files += 1
                pruned_files += 1
                scanned_rows_pruned += f.num_rows
                scanned_bytes_pruned += f.size_bytes
                total_rows += f.num_rows
                total_bytes += f.size_bytes
            continue

        kept_partitions += 1
        file_results = []
        for f in part.files:
            total_files += 1
            total_rows += f.num_rows
            total_bytes += f.size_bytes
            d = _eval_tree_on_stats(predicate, f, ctx)
            fr = FileResult(f.path, part.label, d.verdict.value, d.reasons,
                            f.num_rows, f.size_bytes)
            if d.verdict is Verdict.PRUNED:
                pruned_files += 1
                scanned_rows_pruned += f.num_rows
                scanned_bytes_pruned += f.size_bytes
            else:
                kept_files += 1
                if d.verdict is Verdict.UNKNOWN:
                    for r in d.reasons:
                        uncertain.append({"target": f"file:{f.path}",
                                          "code": r.code, "detail": r.detail,
                                          "leaf": r.leaf})
            file_results.append(fr)
        part_results.append(PartitionResult(part.label, part.is_null,
                                            pverdict.value, preason, file_results))

    metrics = {
        "partitions_total": considered_partitions,
        "partitions_pruned": pruned_partitions,
        "partitions_kept": kept_partitions,
        "files_total": total_files,
        "files_pruned": pruned_files,
        "files_kept": kept_files,
        "rows_total": total_rows,
        "rows_pruned": scanned_rows_pruned,
        "bytes_total": total_bytes,
        "bytes_pruned": scanned_bytes_pruned,
    }

    return Plan(
        table=ctx.name, request_id=request_id,
        transform_version=TRANSFORM_SPEC_VERSION, tzdb_version=tzver,
        stats_schema_version=STATS_SCHEMA_VERSION,
        candidates={"lo": cand.lo, "hi": cand.hi,
                    "null_bucket": {0: "EXCLUDED", 1: "POSSIBLE", 2: "REQUIRED"}[cand.null_bucket],
                    "exact": cand.exact},
        partitions=part_results, uncertain=uncertain,
        failures=version_failures, metrics=metrics)
