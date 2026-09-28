"""端到端演示：在干净的本地目录中完成一次 develop -> main 三方合并。

运行：
    .venv/bin/python scripts/run_demo.py

脚本读取 sample_data/orders_snapshot.json（含三方行集与期望结论），
逐步打印：版本/输入身份 -> 逐键分类与判定依据 -> 冲突解决 -> 提交与两条父引用
-> 最终行集与期望核对。退出码非 0 表示核对失败。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from merge3.config import StorageConfig  # noqa: E402
from merge3.service.merge_service import MergeService  # noqa: E402

DEMO_ROOT = ROOT / "demo_data"


def main() -> int:
    fixture = json.loads((ROOT / "sample_data" / "orders_snapshot.json").read_text("utf-8"))
    if DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)

    svc = MergeService(StorageConfig(root_dir=DEMO_ROOT / "data"), log_echo=False)
    svc.register_table(fixture["table"], fixture["primary_key"], fixture["fields"])

    base = svc.write_snapshot("orders", fixture["base"])
    svc.create_branch("orders", "main", base.snapshot_id)
    svc.create_branch("orders", "develop", base.snapshot_id)
    svc.commit_rows("orders", "develop", fixture["develop"])
    svc.commit_rows("orders", "main", fixture["main"])

    print(f"service version : {svc.__module__}")
    print(f"base ancestor   : {base.snapshot_id}")
    run = svc.start_merge("orders")
    print(f"merge run       : {run.run_id}")
    print(f"input snapshots : base={run.base_snapshot_id}")
    print(f"                  ours(develop)={run.ours_snapshot_id}")
    print(f"                  theirs(main) ={run.theirs_snapshot_id}")
    print("\n== 逐键分类（共同祖先 / 开发 / 主 三方比较） ==")
    ok = True
    for ks in sorted(run.plan.entries, key=lambda s: json.loads(s)[0]):
        e = run.plan.entries[ks]
        rid = e.key["id"]
        want = fixture["expected_classifications"][str(rid)]
        mark = "OK " if e.classification == want else "BAD"
        ok &= e.classification == want
        print(f"  [{mark}] id={rid:<2} {e.classification:<24} 期望={want}")
        print(f"         判定依据: {e.reason}")

    print(f"\n冲突数: {len(run.plan.conflicts)}（必须全部显式解决才能提交）")
    for key, decision in fixture["resolutions_in_demo"].items():
        svc.resolve_conflict(
            run.run_id, [int(key)], decision["kind"], decision.get("custom_row"),
            binding=run.binding(),
        )
        print(f"  id={key} -> {decision['kind']}")

    snap = svc.commit_merge(run.run_id, message="demo merge develop into main")
    _, final_rows = svc.read_snapshot_rows(snap.snapshot_id)

    print("\n== 提交后血缘 ==")
    pm = svc.store.parent_map("orders")
    print(f"  merged snapshot : {snap.snapshot_id}")
    print(f"  parent refs     : {pm[snap.snapshot_id]}")
    print("  (position 0=开发头, 1=主头；旧快照仍全部可达)")

    print("\n== 最终合并行集 ==")
    for r in final_rows:
        print(" ", r)

    expected = fixture["expected_final_rows_after_demo"]
    if final_rows != expected:
        ok = False
        print("\n最终行集与样例期望不一致！")
        print("  expected:", expected)
        print("  actual  :", final_rows)
    else:
        print("\n最终行集与样例期望一致 ✔")

    print(f"\n运行日志: {svc.cfg.log_dir}/run-{run.run_id}.jsonl")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
