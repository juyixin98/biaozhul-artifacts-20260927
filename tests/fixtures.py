"""合成夹具构建器 (独立于被测核心 colaudit)。

只依赖 pyarrow 与标准库: 统计全部由本文件用 Python 原语手工计算,
不 import colaudit.stats/audit, 避免"参考答案由被测实现自身生成"。

生成的数据集:
  well_formed        数据正确, 统计正确
  bad_statistics     数据正确, 统计错误 (min/max、NULL 计数、排序)
  no_statistics      数据正确, 完全无 claims.json
  all_null           整页/整列全 NULL
  mixed_nan          普通浮点、NaN、+0.0、-0.0 混合
  truncated_string   合法截断声明 (影响可信剪枝区间)
  truncated_invalid  非法截断声明 (声明落在真实区间错误一侧)
  sensitive_demo     含敏感列, 用于脱敏验证

每个数据集同时产出 ground_truth.csv (独立真值), tests/oracle.py
只使用 csv 模块读它, 与 Parquet/colaudit 完全无关。
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

DATA_FILE = "data.parquet"
ROWS_PER_RG = 60
PAGES_PER_RG = 3
ROWS_PER_PAGE = ROWS_PER_RG // PAGES_PER_RG  # 20

COLUMNS = [
    {"name": "id", "logical_type": "int"},
    {"name": "score", "logical_type": "float"},
    {"name": "name", "logical_type": "string"},
    {"name": "active", "logical_type": "bool"},
]


# ---------------------------------------------------------------------------
# 独立统计原语 (不引用 colaudit)
# ---------------------------------------------------------------------------
def _is_nan(v: Any) -> bool:
    return isinstance(v, float) and math.isnan(v)


def oracle_stats(rows: list[dict[str, Any]], column: str) -> dict[str, Any]:
    """从真值行手工计算一页/行组统计, 规则与文档约定一致。"""
    values = [r[column] for r in rows]
    count = len(values)
    null_count = sum(1 for v in values if v is None)
    nan_count = sum(1 for v in values if _is_nan(v))
    comparable = [
        v for v in values if v is not None and not _is_nan(v)
    ]

    def key(v: Any) -> tuple[int, Any]:
        if isinstance(v, str):
            return (0, v)
        if isinstance(v, bool):
            return (0, int(v))
        if column == "score":  # float: 归一有符号零用于端点排序
            return (0, v + 0.0)
        return (0, v)

    minv = min(comparable, key=key) if comparable else None
    maxv = max(comparable, key=key) if comparable else None
    # 端点: 保留真实出现的有符号零
    if comparable and minv == 0 and column == "score":
        zeros = [v for v in comparable if v == 0.0]
        minv = min(zeros, key=lambda z: math.copysign(1.0, z))
    if comparable and maxv == 0 and column == "score":
        zeros = [v for v in comparable if v == 0.0]
        maxv = max(zeros, key=lambda z: math.copysign(1.0, z))

    has_pz = any(
        isinstance(v, float) and v == 0.0 and math.copysign(1.0, v) > 0
        for v in comparable
    )
    has_nz = any(
        isinstance(v, float) and v == 0.0 and math.copysign(1.0, v) < 0
        for v in comparable
    )

    # 有序性 (非 NULL 非 NaN, 允许等值; asc 与 desc 跟踪)
    asc_ok = desc_ok = True
    prev = None
    type_key = _oracle_key_fn(column)
    for v in comparable:
        if prev is not None:
            if type_key(v) < type_key(prev):
                asc_ok = False
            if type_key(v) > type_key(prev):
                desc_ok = False
        prev = v
    if not comparable:
        sorted_flag = None
    elif asc_ok:
        sorted_flag = "asc"
    elif desc_ok:
        sorted_flag = "desc"
    else:
        sorted_flag = None

    return {
        "logical_type": _logical_type(column),
        "count": count,
        "null_count": null_count,
        "nan_count": nan_count,
        "min": minv,
        "max": maxv,
        "has_positive_zero": has_pz,
        "has_negative_zero": has_nz,
        "min_truncated": False,
        "max_truncated": False,
        "sorted": sorted_flag,
    }


def _oracle_key_fn(column: str) -> Callable[[Any], Any]:
    def k(v: Any) -> Any:
        if column == "score":
            return v + 0.0
        if isinstance(v, bool):
            return int(v)
        return v

    return k


def _logical_type(column: str) -> str:
    return next(c["logical_type"] for c in COLUMNS if c["name"] == column)


# ---------------------------------------------------------------------------
# 真值行生成
# ---------------------------------------------------------------------------
def _float_value(i: int, spec: str) -> float | None:
    if spec == "signed_zero":
        # +0.0 与 -0.0 混合, 外加普通值
        if i % 6 == 0:
            return -0.0
        if i % 6 == 1:
            return 0.0
        return float((i % 7) - 3) / 2.0
    return float(i % 10)


def make_rows(spec: str) -> list[dict[str, Any]]:
    total = ROWS_PER_RG * 2
    rows: list[dict[str, Any]] = []
    for i in range(total):
        if spec == "bad_statistics":
            # 行组 0 正常; 行组 1 id 人为乱序, 使真实 sorted=None
            if i < ROWS_PER_RG:
                rid = i
            else:
                rid = ROWS_PER_RG + ((i * 37) % ROWS_PER_RG)
            score = None if i % 7 == 0 else float((i * 3) % 13)
            name = None if i % 11 == 0 else f"item_{i:03d}"
            active = i % 2 == 0
        elif spec == "all_null":
            score = None
            name = None
            active = None
            rid = i
        elif spec == "mixed_nan":
            if i % 5 == 0:
                score = float("nan")
            elif i % 5 == 1:
                score = -0.0
            elif i % 5 == 2:
                score = 0.0
            elif i % 5 == 3:
                score = None
            else:
                score = float(i % 4)
            name = None if i % 9 == 0 else f"n{i:03d}"
            active = (i % 2 == 0)
            rid = i
        elif spec == "truncated_string" or spec == "truncated_invalid":
            score = float(i % 8)
            # 行组 0: name 升序, 各页端点固定前缀便于截断声明
            name = _truncated_name(i)
            active = True
            rid = i
        else:
            score = None if i % 7 == 0 else float((i * 3) % 13)
            name = None if i % 11 == 0 else f"item_{i:03d}"
            active = i % 2 == 0
            if spec != "bad_statistics":
                rid = i
        rows.append({"id": rid, "score": score, "name": name,
                     "active": active})
    return rows


def _truncated_name(i: int) -> str | None:
    if i >= ROWS_PER_RG:
        return None if i % 8 == 0 else f"zzz_{i:03d}"
    # 行组 0 内升序; 页 0 以 "aaaa..." 开头
    if i % 10 == 0:
        return None
    page = i // ROWS_PER_PAGE
    bases = ["aaaa_tail_value", "mmmm_tail_value", "tttt_tail_value"]
    return f"{bases[page]}_{i:03d}"


# ---------------------------------------------------------------------------
# Parquet / manifest / ground_truth
# ---------------------------------------------------------------------------
def _arrow_schema() -> pa.Schema:
    return pa.schema([
        pa.field("id", pa.int64()),
        pa.field("score", pa.float64()),
        pa.field("name", pa.string()),
        pa.field("active", pa.bool_()),
    ])


def _to_table(rows: list[dict[str, Any]]) -> pa.Table:
    cols = {
        c["name"]: [r[c["name"]] for r in rows]
        for c in COLUMNS
    }
    return pa.Table.from_pydict(cols, schema=_arrow_schema())


def write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    writer = pq.ParquetWriter(path, _arrow_schema(), compression="snappy")
    try:
        for start in range(0, len(rows), ROWS_PER_RG):
            writer.write_table(_to_table(rows[start:start + ROWS_PER_RG]))
    finally:
        writer.close()


def write_ground_truth(rows: list[dict[str, Any]], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "score", "name", "active"])
        for r in rows:
            writer.writerow([
                r["id"],
                "" if r["score"] is None else (
                    "NaN" if _is_nan(r["score"]) else repr(r["score"])
                ),
                "" if r["name"] is None else r["name"],
                "" if r["active"] is None else (
                    "true" if r["active"] else "false"
                ),
            ])


def build_manifest(
    name: str, *, sensitive: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "version": 1,
        "name": name,
        "columns": [
            {
                "name": c["name"],
                "logical_type": c["logical_type"],
                "sensitive": c["name"] in sensitive,
            }
            for c in COLUMNS
        ],
        "files": [
            {
                "file": DATA_FILE,
                "row_groups": [
                    {
                        "index": rg,
                        "row_count": ROWS_PER_RG,
                        "pages": [
                            {"index": p, "row_count": ROWS_PER_PAGE}
                            for p in range(PAGES_PER_RG)
                        ],
                    }
                    for rg in range(2)
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# claims 生成与变异
# ---------------------------------------------------------------------------
def _slice_pages(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    for rg in range(2):
        base = rg * ROWS_PER_RG
        for p in range(PAGES_PER_RG):
            start = base + p * ROWS_PER_PAGE
            pages.append(rows[start:start + ROWS_PER_PAGE])
    return pages


def build_correct_claims(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pages = _slice_pages(rows)
    rgs = []
    for rg in range(2):
        rg_rows = rows[rg * ROWS_PER_RG:(rg + 1) * ROWS_PER_RG]
        page_entries = []
        for p in range(PAGES_PER_RG):
            stats = {c: oracle_stats(pages[rg * PAGES_PER_RG + p], c)
                     for c in ("id", "score", "name", "active")}
            page_entries.append({"index": p, "stats": stats})
        rg_stats = {
            c: oracle_stats(rg_rows, c)
            for c in ("id", "score", "name", "active")
        }
        rgs.append({
            "index": rg,
            "stats": rg_stats,
            "pages": page_entries,
        })
    return {"version": 1, "files": [{"file": DATA_FILE, "row_groups": rgs}]}


def mutate_bad(claims: dict[str, Any]) -> dict[str, Any]:
    """注入可定位的错误:
    * 文件 data.parquet / 行组 0 / 页 0 / 列 score: min 被改为 -999.0
      (行组 min 同步污染 -> minmax_mismatch);
    * 行组 0 / 页 1 / 列 score: null_count 虚增 5, 但行组声明保持为
      页聚合的真实值 -> aggregation_mismatch (页和 != 行组);
    * 行组 1 / 列 id: sorted 篡改为 desc (真实为 None) -> sorted_mismatch。
    """
    files = claims["files"][0]
    rg0 = files["row_groups"][0]
    page0 = rg0["pages"][0]["stats"]
    page0["score"]["min"] = -999.0
    # 同步篡改行组 min, 使行组声明同样错误
    rg0["stats"]["score"]["min"] = -999.0

    page1 = rg0["pages"][1]["stats"]
    page1["score"]["null_count"] = page1["score"]["null_count"] + 5
    # 故意不修改 rg0 的 null_count, 页聚合和将与行组声明不符

    rg1 = files["row_groups"][1]
    # 行组 1 的 id 真实乱序 (sorted=None), 声明成 desc
    rg1["stats"]["id"]["sorted"] = "desc"
    return claims


def mutate_truncated(
    claims: dict[str, Any], *, invalid: bool
) -> dict[str, Any]:
    """行组 0 页 0 name: 真实 min 形如 'aaaa_tail_value_0001'。
    合法: 声明截断前缀 'aaaa_tail_valu' (min_truncated)。
    非法: 声明一个字典序更大的非前缀 'zzz_bogus_prefix' 作为 min,
    会落在真实区间错误一侧。"""
    rg0 = claims["files"][0]["row_groups"][0]
    name_stats = rg0["pages"][0]["stats"]["name"]
    if invalid:
        # 真实 min 形如 'aaaa_tail_value_0001'; 声明一个非前缀且更大的
        # min 会落在真实区间错误一侧 -> TRUNCATION_INVALID
        name_stats["min"] = "zzz_bogus_prefix"
        name_stats["min_truncated"] = True
    else:
        real_min = name_stats["min"]
        prefix = real_min[:10]  # 'aaaa_tail_'
        assert real_min.startswith(prefix) and prefix < real_min
        name_stats["min"] = prefix
        name_stats["min_truncated"] = True
    # 行组 name 统计: 行组真实 min 来自同一页, 同步为截断前缀
    rg0["stats"]["name"]["min"] = name_stats["min"]
    rg0["stats"]["name"]["min_truncated"] = True
    return claims


# ---------------------------------------------------------------------------
# 顶层构建
# ---------------------------------------------------------------------------
SPECS: dict[str, dict[str, Any]] = {
    "well_formed": {"rows": "default", "claims": "correct"},
    "bad_statistics": {"rows": "default", "claims": "bad"},
    "no_statistics": {"rows": "default", "claims": "none"},
    "all_null": {"rows": "all_null", "claims": "correct"},
    "mixed_nan": {"rows": "mixed_nan", "claims": "correct"},
    "truncated_string": {"rows": "truncated_string", "claims": "truncated"},
    "truncated_invalid": {
        "rows": "truncated_invalid", "claims": "truncated_invalid"
    },
    "sensitive_demo": {"rows": "default", "claims": "correct",
                       "sensitive": ("name",)},
}


def build_all(base: str | Path) -> dict[str, Path]:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}
    for name, spec in SPECS.items():
        root = base / name
        root.mkdir(exist_ok=True)
        rows = make_rows(spec["rows"])
        write_parquet(rows, root / DATA_FILE)
        write_ground_truth(rows, root / "ground_truth.csv")
        manifest = build_manifest(
            name, sensitive=spec.get("sensitive", ())
        )
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        kind = spec["claims"]
        if kind != "none":
            claims = build_correct_claims(rows)
            if kind == "bad":
                claims = mutate_bad(claims)
            elif kind == "truncated":
                claims = mutate_truncated(claims, invalid=False)
            elif kind == "truncated_invalid":
                claims = mutate_truncated(claims, invalid=True)
            (root / "claims.json").write_text(
                json.dumps(claims, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        created[name] = root
    return created


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    paths = build_all(target)
    for name, path in paths.items():
        print(f"built {name:20s} -> {path}")
