"""本地合成夹具生成器。

在 ``tests/fixtures/`` 下生成 6 个文件夹，每个含一个可被 PyArrow 读取的
Parquet 文件，并附 ``expected.json`` 说明 *数据真值* 与 *期望审计结论*。

夹具特点：
- ``good_stats/``      数据与统计均正确（含多页、NULL、混合 NaN、-0.0）；
- ``wrong_stats/``     数据正确，但列块/页统计、NULL 计数被故意改错；
- ``no_stats/``        关闭统计写入；
- ``all_null/``        整列全 NULL；
- ``mixed_nan/``       数据含 +/-NaN、+/-0.0、Inf；统计与数据一致（ACCEPTED），
                       以及一份故意错误的 NaN 统计；
- ``truncated_strings/`` 字符串统计带截断标志，边界为真实值的严格前缀；
- ``sorting_wrong/``   数据未排序，页脚却声明 sorting_columns 升序。

所有统计改写走 :mod:`colstats.parquet_adapter`，与被测内核共享的只有
*读取实际数据* 的能力；期望值在本脚本中以独立常量/纯 Python 计算给出。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from colstats.parquet_adapter import (
    StatsPatch,
    parse_file,
    rewrite_file,
)
from colstats.models import SortingClaim

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

_WRITE = dict(
    version="2.6",
    use_dictionary=False,
    column_encoding="PLAIN",
    compression="NONE",
    write_page_index=False,
    write_page_checksum=False,
)


def _write(path: Path, table: pa.Table, **kw) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, **{**_WRITE, **kw})


def _expected(**kw) -> dict:
    return kw


def build_good_stats(root: Path) -> None:
    d = root / "good_stats"
    # 两列、两个行组、每页约 1024 值 -> 强制多页
    ids = list(range(20_000))
    amounts = [
        None if i % 100 == 0 else float(i % 7) * (1 if i % 2 else -1)
        for i in range(20_000)
    ]
    t = pa.table(
        {
            "id": pa.array(ids, pa.int32()),
            "amount": pa.array(amounts, pa.float64()),
        }
    )
    _write(d / "data.parquet", t, row_group_size=10_000, data_page_size=512)
    nulls = sum(1 for v in amounts if v is None)
    meta = {
        "description": "数据与统计完全正确：多页、含 NULL、负数与有符号零",
        "expected_verdict": "ACCEPTED",
        "columns": {
            "id": {"min": 0, "max": 9999, "null_count": 0},
            "amount": {
                "min": -6.0,
                "max": 6.0,
                "null_count": nulls,
                "contains_nan": False,
                "contains_negative_zero": True,
            },
        },
        "num_row_groups": 2,
    }
    (d / "expected.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_wrong_stats(root: Path) -> None:
    d = root / "wrong_stats"
    ids = list(range(20_000))
    codes = [i % 500 for i in range(20_000)]
    t = pa.table(
        {
            "id": pa.array(ids, pa.int32()),
            "code": pa.array(codes, pa.int64()),
        }
    )
    clean = d / "clean.parquet"
    _write(clean, t, row_group_size=10_000, data_page_size=512)
    model = parse_file(clean)

    # 列块级 min/max 错误（id）；页级错误（id page0）；NULL 计数错误（code）
    patches = [
        # id 列块：声明区间完全偏离 -> 范围剪枝会漏行
        StatsPatch(
            row_group=0, column_path="id",
            min_value=500_000, max_value=501_000, physical_type="INT32",
        ),
        # id 第 0 页：min 改错、null_count 虚报
        StatsPatch(
            row_group=0, column_path="id", page_index=0,
            min_value=999_999, null_count=77, physical_type="INT32",
        ),
        # code 列块：null_count 与实际（0）不符
        StatsPatch(
            row_group=1, column_path="code",
            null_count=123, physical_type="INT64",
        ),
        # code 第 3 页 max 改错
        StatsPatch(
            row_group=1, column_path="code", page_index=3,
            max_value=2_000_000, physical_type="INT64",
        ),
    ]
    bad = rewrite_file(model, patches)
    (d / "data.parquet").write_bytes(bad)
    clean.unlink()

    meta = {
        "description": "数据正确但统计被故意改错（块级/页级 min/max 与 NULL）",
        "expected_verdict": "REJECTED",
        "expected_finding_codes": [
            "MIN_MISMATCH",
            "NULL_COUNT_MISMATCH",
            "PAGE_AGGREGATION_MISMATCH",
            "MAX_MISMATCH",
        ],
        "untrusted_columns": ["id", "code"],
        "corrupt_locators": [
            {"row_group": 0, "column": "id", "page": 0},
            {"row_group": 0, "column": "id"},
            {"row_group": 1, "column": "code"},
            {"row_group": 1, "column": "code", "page": 3},
        ],
        "data_truth": {
            "id": {"min": 0, "max": 9999},
            "code": {"min": 0, "max": 499, "null_count": 0},
        },
    }
    (d / "expected.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_no_stats(root: Path) -> None:
    d = root / "no_stats"
    t = pa.table(
        {
            "id": pa.array(list(range(5_000)), pa.int32()),
            "name": pa.array([f"n{i}" for i in range(5_000)], pa.string()),
        }
    )
    _write(
        d / "data.parquet",
        t,
        row_group_size=2_500,
        write_statistics=False,
    )
    (d / "expected.json").write_text(
        json.dumps(
            {
                "description": "文件关闭了统计写入（无 min/max/null_count）",
                "expected_verdict": "UNDECIDABLE",
                "expected_finding_codes": [
                    "NULL_COUNT_MISSING",
                    "MIN_MAX_MISSING",
                ],
                "data_truth": {
                    "id": {"min": 0, "max": 2499, "null_count": 0},
                    "name": {"min": "n0", "max": "n999", "null_count": 0},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def build_all_null(root: Path) -> None:
    d = root / "all_null"
    t = pa.table(
        {
            "id": pa.array(list(range(3_000)), pa.int32()),
            "note": pa.array([None] * 3_000, pa.string()),
            "amount": pa.array([None] * 3_000, pa.float64()),
        }
    )
    _write(d / "data.parquet", t, row_group_size=1_500, data_page_size=256)
    (d / "expected.json").write_text(
        json.dumps(
            {
                "description": "note/amount 两列全 NULL；统计应可接受（无 min/max 合理）",
                "expected_verdict": "ACCEPTED",
                "columns": {
                    "id": {"min": 0, "max": 1499, "null_count": 0},
                    "note": {"null_count": 1500, "has_values": False},
                    "amount": {"null_count": 1500, "has_values": False},
                },
                "query_checks": [
                    {"column": "note", "predicate": "is_null",
                     "expect_all_rows_match": True},
                    {"column": "amount", "predicate": "not_null",
                     "expect_zero_match": True},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8"
    )


def build_mixed_nan(root: Path) -> None:
    d = root / "mixed_nan"
    # 真值（独立常量）：min/max 的 total-order 位置必须明确
    vals = [
        1.5,
        float("nan"),      # +NaN（排在最后）
        -float("nan"),     # -NaN（在 +NaN 前）
        -0.0,
        0.0,
        float("inf"),
        float("-inf"),
        -3.25,
        None,
        2.0,
    ] * 1200
    t = pa.table({"f": pa.array(vals, pa.float64())})
    clean = d / "clean.parquet"
    _write(clean, t, row_group_size=6_000, data_page_size=512)

    # 1) 正确统计版本（PyArrow 通常排除 NaN）
    model = parse_file(clean)
    (d / "data.parquet").write_bytes(clean.read_bytes())

    # 2) 故意把 max 改成 NaN 的坏版本
    bad = rewrite_file(
        model,
        [
            StatsPatch(
                row_group=0, column_path="f",
                min_value=float("-inf"), max_value=float("nan"),
                physical_type="DOUBLE",
            ),
            StatsPatch(
                row_group=0, column_path="f", page_index=0,
                max_value=float("nan"), physical_type="DOUBLE",
            ),
        ],
    )
    (d / "bad_nan_stats.parquet").write_bytes(bad)
    clean.unlink()

    meta = {
        "description": (
            "混合 NaN：+NaN/-NaN、+0.0/-0.0、±Inf、NULL。data.parquet 的统计"
            "与数据一致（min=-Inf/max=+Inf，NaN 不在 min/max 中）；"
            "bad_nan_stats.parquet 把 max 改成 NaN，应被拒绝。"
        ),
        "expected_verdict": "ACCEPTED",
        "bad_file": {
            "name": "bad_nan_stats.parquet",
            "expected_verdict": "REJECTED",
            "expected_finding_codes": ["MAX_MISMATCH", "PAGE_AGGREGATION_MISMATCH"],
        },
        "data_truth": {
            "f": {
                "min": "-Infinity",
                "max": "Infinity",
                "null_count": 1200,
                "contains_nan": True,
                "contains_negative_zero": True,
                "total_order_present": [
                    "-Infinity", -3.25, "-0.0", "+0.0", 1.5, 2.0,
                    "+Infinity", "-NaN", "+NaN",
                ],
            }
        },
    }
    (d / "expected.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_truncated_strings(root: Path) -> None:
    d = root / "truncated_strings"
    # 合法截断：真实区间 [alpha0000, alphazzzz]（"z" 大于十进制数字）。
    # 声明截断下界 "alph"（< 真 min）、截断上界 "b"（> 真 max），
    # 满足规范的覆盖方向；所有数据页使用同一对截断边界，页→块聚合
    # 关系因此成立（trunc_min <= 页聚合 min，trunc_max >= 页聚合 max）。
    # 真实上界 alphazzzz：'z' 大于十进制数字，确保落在截断上界 "b" 之下
    values = [
        "alpha0000" if i == 0 else
        "alphazzzz" if i == 3999 else
        f"alpha{i % 1000:04d}"
        for i in range(4_000)
    ]
    values[0] = "alpha0000"
    values[3999] = "alphazzzz"
    t = pa.table({"word": pa.array(values, pa.string())})
    clean = d / "clean.parquet"
    _write(clean, t, row_group_size=4_000, data_page_size=256)
    model = parse_file(clean)
    actual_min = min(values)
    actual_max = max(values)
    assert actual_min == "alpha0000"
    assert actual_max == "alphazzzz"

    trunc_min, trunc_max = b"alph", b"b"
    assert trunc_min < actual_min.encode() and trunc_max > actual_max.encode()

    # 列块与所有页均声明同一对合法截断边界
    patches = [
        StatsPatch(
            row_group=0, column_path="word",
            min_value=trunc_min, max_value=trunc_max,
            set_min_truncated=True, set_max_truncated=True,
            physical_type="BYTE_ARRAY",
        ),
    ]
    for pi in range(len(model.row_groups[0].chunks[0].pages)):
        patches.append(
            StatsPatch(
                row_group=0, column_path="word", page_index=pi,
                min_value=trunc_min, max_value=trunc_max,
                set_min_truncated=True, set_max_truncated=True,
                physical_type="BYTE_ARRAY",
            )
        )
    (d / "data.parquet").write_bytes(rewrite_file(model, patches))

    # 非法截断：max="alphab" 严格小于真实 max alphazzzz，违反 >= 方向。
    bad = rewrite_file(
        model,
        [
            StatsPatch(
                row_group=0, column_path="word",
                min_value=trunc_min, max_value=b"alphab",
                set_min_truncated=True, set_max_truncated=True,
                physical_type="BYTE_ARRAY",
            )
        ],
    )
    (d / "bad_truncated.parquet").write_bytes(bad)
    clean.unlink()
    (d / "expected.json").write_text(
        json.dumps(
            {
                "description": (
                    "data.parquet 的字符串统计声明截断边界 alph/b，覆盖真实区间 "
                    "[alpha0000, alphazzzz]；严格外侧剪枝可用，落在截断边界上的"
                    "比较必须 UNDECIDABLE。bad_truncated.parquet 的截断 "
                    "max=alphab 小于真实 max，违反方向保证，应被拒绝。"
                ),
                "expected_verdict": "ACCEPTED",
                "bad_file": {
                    "name": "bad_truncated.parquet",
                    "expected_verdict": "REJECTED",
                    "expected_finding_codes": ["TRUNCATED_BOUND_OUTSIDE"],
                },
                "data_truth": {
                    "word": {
                        "min": "alpha0000",
                        "max": "alphazzzz",
                        "truncated_min_claim": "alph",
                        "truncated_max_claim": "b",
                        "null_count": 0,
                    }
                },
                "prune_expectations": [
                    {"predicate": "eq", "value": "aaaa", "decision": "PRUNE"},
                    {"predicate": "eq", "value": "alph", "decision": "UNDECIDABLE"},
                    {"predicate": "lt", "value": "alph", "decision": "UNDECIDABLE"},
                    {"predicate": "lt", "value": "afff", "decision": "PRUNE"},
                    {"predicate": "gt", "value": "b", "decision": "UNDECIDABLE"},
                    {"predicate": "gt", "value": "c", "decision": "PRUNE"},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def build_sorting_wrong(root: Path) -> None:
    d = root / "sorting_wrong"
    # 数据不是有序的（随机大小），但页脚将声明升序
    values = [
        10, 3, 99, 1, 50, 72, 4, 18, 9, 21,
        63, 2, 7, 80, 5, 11, 33, 8, 0, 17,
    ] * 300
    t = pa.table(
        {
            "id": pa.array(range(6_000), pa.int32()),
            "score": pa.array(values, pa.int32()),
        }
    )
    clean = d / "clean.parquet"
    _write(clean, t, row_group_size=6_000, data_page_size=512)
    model = parse_file(clean)
    bad = rewrite_file(
        model,
        [],
        sorting_override=[SortingClaim(column_idx=1, descending=False, nulls_first=False)],
    )
    (d / "data.parquet").write_bytes(bad)
    clean.unlink()
    (d / "expected.json").write_text(
        json.dumps(
            {
                "description": "score 数据无序，但页脚 sorting_columns 声明升序",
                "expected_verdict": "REJECTED",
                "expected_finding_codes": ["SORTING_DECLARATION_VIOLATED"],
                "declared": {"column": "score", "order": "asc"},
                "observed": "unsorted",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


BUILDERS = [
    build_good_stats,
    build_wrong_stats,
    build_no_stats,
    build_all_null,
    build_mixed_nan,
    build_truncated_strings,
    build_sorting_wrong,
]


def build_all(root: Path | None = None) -> None:
    root = root or FIXTURE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    for builder in BUILDERS:
        builder(root)
        print(f"built {builder.__name__}")


if __name__ == "__main__":
    build_all()
