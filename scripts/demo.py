#!/usr/bin/env python3
"""本地端到端演示脚本（不依赖运行中的服务）。

依次演示：
1. 生成夹具（若 tests/fixtures 缺失）；
2. 审计每种夹具，打印结论、失败类别与可定位位置（脱敏）；
3. 在"统计错误"文件上对比：盲目信任统计会漏行，而审计禁用坏统计后
   查询结果与暴力全扫完全一致；
4. 演示截断字符串边界的 PRUNE / UNDECIDABLE / SCAN 决定。

用法：
    PYTHONPATH=src python scripts/demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from colstats.parquet_adapter import parse_file, read_column_values  # noqa: E402
from colstats.kernel import audit_file, can_prune  # noqa: E402
from colstats.query import run_query  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def ensure_fixtures() -> None:
    if not (FIX / "good_stats" / "data.parquet").exists():
        import build_fixtures

        build_fixtures.build_all(FIX)


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_audit(name: str, rel: str) -> object:
    path = FIX / rel
    model = parse_file(path)
    result = audit_file(model, request_id=f"demo-{name}")
    print(f"[{name}] {path.name} -> 结论: {result.verdict}")
    print(f"  列可信标志: {result.trusted}")
    if result.truncated_columns:
        print(f"  截断列: {result.truncated_columns}")
    errors = [f for f in result.findings if f.severity.value in ("ERROR", "WARNING")]
    for f in errors[:8]:
        print(f"  - {f.severity.value:7s} {f.code:28s} @ {f.locator}")
        print(f"      原因: {f.message}")
    if len(errors) > 8:
        print(f"  ...（其余 {len(errors) - 8} 条略）")
    if not errors:
        good = next(f for f in result.findings if f.code == "GOOD_STATS")
        print(f"  - INFO    GOOD_STATS  {good.message}")
    return model, result


def demo_bad_stats_query() -> None:
    hr("规则 4：坏统计被禁用剪枝后，查询仍然正确")
    model = parse_file(FIX / "wrong_stats" / "data.parquet")
    audit = audit_file(model)
    target = 500

    # 盲目信任统计会怎样？
    chunk0 = model.row_groups[0].chunks[0]
    naive = can_prune(chunk0.claim, "INT32", "eq", target, trusted=True)
    print(f"坏列块声明区间 [{chunk0.claim.min_claim}, {chunk0.claim.max_claim}]，"
          f"真实数据从 0 开始")
    print(f"  若盲目信任统计，id == {target} 的决定: {naive} "
          f"（会错误地跳过整个行组，漏掉匹配行！）")

    # 审计门禁后的查询
    report = run_query(model, "id", "eq", target, audit=audit)
    print(f"  审计标记 trusted[id] = {audit.trusted['id']}")
    for g in report.groups:
        print(f"  行组 {g.row_group}: 决定={g.chunk_decision} "
              f"扫描行数={g.scanned_rows} 命中={g.matched_rows}")
    brute = [
        v for rg in range(len(model.row_groups))
        for v in read_column_values(model.path, rg, "id")
        if v == target
    ]
    print(f"  查询结果: {report.result}；暴力全扫: {brute}；"
          f"一致: {report.result == brute}")


def demo_truncated() -> None:
    hr("规则 2：字符串截断统计的可信范围")
    model = parse_file(FIX / "truncated_strings" / "data.parquet")
    audit = audit_file(model)
    chunk = model.row_groups[0].chunks[0]
    print(f"真实区间 alpha0000..alphazzzz；声明截断边界 "
          f"{chunk.claim.min_claim!r}..{chunk.claim.max_claim!r}")
    for pred, v in [("eq", "aaaa"), ("eq", "alph"), ("lt", "afff"),
                    ("eq", "alpha0000"), ("gt", "c"), ("gt", "b")]:
        d = can_prune(chunk.claim, "BYTE_ARRAY", pred, v.encode(), trusted=True)
        print(f"  word {pred} {v!r:12s} -> {d}")
    print(f"  审计结论: {audit.verdict}（截断边界合法，可信但只能严格外侧剪枝）")


def demo_nan() -> None:
    hr("规则 1：混合 NaN / 有符号零")
    model = parse_file(FIX / "mixed_nan" / "data.parquet")
    result = audit_file(model)
    chunk = model.row_groups[0].chunks[0]
    print(f"正确 NaN 文件: {result.verdict}；min={chunk.claim.min_claim} "
          f"max={chunk.claim.max_claim} nulls={chunk.claim.null_count}")
    bad_model, bad_result = show_audit("mixed_nan 坏", "mixed_nan/bad_nan_stats.parquet")
    report = run_query(bad_model, "f", "eq", 1.5, audit=bad_result)
    brute = [v for v in read_column_values(bad_model.path, 0, "f") if v == 1.5]
    print(f"  禁用坏统计后 f == 1.5 命中 {len(report.result)} 行，"
          f"暴力全扫 {len(brute)} 行，一致: {report.result == brute}")


def main() -> None:
    ensure_fixtures()
    hr("审计各文件夹具（结论 + 失败类别 + 可定位位置）")
    cases = [
        ("good_stats", "good_stats/data.parquet"),
        ("wrong_stats", "wrong_stats/data.parquet"),
        ("no_stats", "no_stats/data.parquet"),
        ("all_null", "all_null/data.parquet"),
        ("mixed_nan", "mixed_nan/data.parquet"),
        ("truncated_strings", "truncated_strings/data.parquet"),
        ("sorting_wrong", "sorting_wrong/data.parquet"),
    ]
    for name, rel in cases:
        show_audit(name, rel)
    demo_bad_stats_query()
    demo_truncated()
    demo_nan()
    hr("演示完成")


if __name__ == "__main__":
    main()
