#!/usr/bin/env python3
"""本地端到端演示 (不启动 HTTP 服务)。

步骤:
1. 在临时目录构建 8 个合成数据集夹具;
2. 登记到 SQLite;
3. 逐数据集执行审计;
4. 在"正确 / 坏统计 / 无统计"三个数据集上跑同一组谓词,
   对照独立 oracle 全扫, 打印每个页的剪枝决策;
5. 打印脱敏诊断样例。

运行: python demo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from colaudit import adapter as ad  # noqa: E402
from colaudit.audit import audit_dataset  # noqa: E402
from colaudit.catalog import Catalog  # noqa: E402
from colaudit.prune import Predicate  # noqa: E402
from colaudit.query import execute  # noqa: E402
from fixtures import build_all  # noqa: E402
from oracle import load_ground_truth, oracle_predicate  # noqa: E402


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def verdicts_by_page(report: dict) -> dict:
    out: dict = {}
    for v in report["verdicts"]:
        if v["scope"] == "page":
            out.setdefault((v["file"], v["row_group"], v["page"]), {})[
                v["column_name"]
            ] = v
    return out


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="colaudit-demo-"))
    print(f"工作目录: {workdir}")
    fixture_dir = workdir / "fixtures"
    paths = build_all(fixture_dir)
    catalog = Catalog(workdir / "audit.db")

    # ---- 1. 登记 + 审计 --------------------------------------------------
    banner("1. 逐数据集审计")
    reports = {}
    for name, root in paths.items():
        ds = ad.load_dataset(root)
        catalog.register_dataset(
            name,
            root,
            columns=[
                {"name": c.name,
                 "logical_type": c.logical_type.value,
                 "sensitive": c.sensitive}
                for c in ds.columns
            ],
            sensitive=ds.sensitive_columns,
        )
        report = audit_dataset(ds, mask_sensitive=True)
        catalog.save_report(report)
        reports[name] = report
        s = report["summary"]
        print(
            f"{name:20s} trusted={s['trusted']:3d}/"
            f"{s['total_columns_scopes']:<3d} "
            f"untrusted={s['untrusted']:2d} "
            f"verdicts={s['by_verdict']}"
        )

    # ---- 2. 同一谓词, 正确 vs 坏统计 vs 无统计 ---------------------------
    banner("2. 谓词 score gt 5.0: 正确 / 坏统计 / 无统计数据集对比")
    predicates = [
        ("score", "gt", 5.0),
        ("score", "is_null", None),
        ("id", "gt", 110),
    ]
    for ds_name in ("well_formed", "bad_statistics", "no_statistics"):
        ds = ad.load_dataset(paths[ds_name])
        truth = load_ground_truth(paths[ds_name])
        report = reports[ds_name]
        index = verdicts_by_page(report)
        for col, op, val in predicates:
            pred = Predicate(column=col, op=op, value=val)
            result = execute(
                ds, pred, index, request_id=f"demo-{ds_name}",
                redact=False,
            )
            expected = [
                r["id"] for r in oracle_predicate(truth, col, op, val)
            ]
            got = [r["id"] for r in result.rows]
            correct = got == expected
            print(
                f"\n数据集={ds_name:16s} {col} {op} {val}"
            )
            print(
                f"  命中={result.matched_rows:3d} (oracle={len(expected):3d})"
                f"  正确={correct}"
            )
            print(
                f"  页: 总 {result.pages_total}, 剪枝跳过 "
                f"{result.pages_skipped}, 扫描 {result.pages_scanned}"
            )
            for t in result.page_trace:
                print(
                    f"   - {t['file']} rg{t['row_group']} p{t['page']}: "
                    f"{t['action']:20s} {t['reason']}"
                )

    # ---- 3. 坏统计定位输出 -----------------------------------------------
    banner("3. 坏统计定位 (bad_statistics 的 reject 诊断)")
    for e in reports["bad_statistics"]["diagnostics"]:
        if e["decision"] == "reject":
            print(
                f"[{e['code']}] {e['file']} 行组{e['row_group']} "
                f"{e['scope']}"
                + (f"/页{e['page']}" if e["page"] is not None else "")
                + f" 列={e['column_name']} request={e['request_id']}"
            )
            print(f"    {e['message']}")

    # ---- 4. 敏感数据脱敏 ---------------------------------------------------
    banner("4. 敏感列诊断脱敏 (sensitive_demo)")
    shown = 0
    for e in reports["sensitive_demo"]["diagnostics"]:
        if e["column_name"] == "name" and e["scope"] == "page":
            print(
                f"{e['file']} 行组{e['row_group']} 页{e['page']} "
                f"name -> 脱敏后的状态: min={e['state']['actual']['min']} "
                f"max={e['state']['actual']['max']} "
                f"count={e['state']['actual']['count']}"
            )
            shown += 1
            if shown == 2:
                break

    banner("演示完成")
    print(f"SQLite 元数据: {workdir / 'audit.db'}")
    print("可运行: python -m colaudit  (然后访问 /docs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
