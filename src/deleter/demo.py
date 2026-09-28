"""内置演示脚本：重写文件 / 先删后插 / 重复键 / 跨文件删除。

直接驱动服务层，打印每个版本的逐行依据，并把完整快照落盘，
与 tests/fixtures/golden_canonical.json 是同一组语义场景。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .service import DeleterService


def run_demo(workspace: str = "demo_out") -> Path:
    root = Path(workspace)
    if root.exists():
        shutil.rmtree(root)
    svc = DeleterService(root)
    log: list[str] = []

    def emit(title: str) -> None:
        log.append("=" * 72)
        log.append(title)
        report = svc.snapshot("orders")
        for v in report["verdicts"]:
            mark = "DEL " if v["action"] == "delete" else "KEEP"
            why = v["reason"]
            by = f" by {v['by_delete_id']}(seq={v['by_seq']})" if v["by_delete_id"] else ""
            log.append(
                f"  {mark} {v['file_id']}#rn{v['row_number']:<2} "
                f"iseq={v['insert_seq']} values={v['values']} <- {why}{by}")
        for e in report["op_evaluations"]:
            if e["status"] != "applied":
                log.append(f"  [op] {e['delete_id']}: {e['status']}")

    svc.create_table("orders", {"id": "int64", "name": "string"}, ["id"])
    svc.load_file("orders", "fA", {"kind": "inline", "rows": [
        {"id": 1, "name": "a1"}, {"id": 2, "name": "a2"},
        {"id": 3, "name": "a3"}, {"id": 4, "name": "a4"}]})
    svc.load_file("orders", "fB", {"kind": "inline", "rows": [
        {"id": 2, "name": "b2"}, {"id": 5, "name": "b5"},
        {"id": None, "name": "bnull"}]})
    emit("载入 fA/fB 后")

    svc.apply_deletes("orders", [{"delete_id": "eq2", "kind": "equality",
                                  "key": {"id": 2}}])
    svc.apply_deletes("orders", [{"delete_id": "pa2", "kind": "position",
                                  "file_id": "fA", "row_number": 2}])
    emit("等值删除 id=2（跨文件重复键）+ 位置删除 fA#2(id=3)")

    svc.rewrite_files("orders", ["fA"], "fC")
    emit("物理重写 fA->fC（旧行号 pa2 随内容身份失效，不复用）")

    svc.apply_deletes("orders", [{"delete_id": "pc0", "kind": "position",
                                  "file_id": "fC", "row_number": 0}])
    svc.apply_deletes("orders", [{"delete_id": "eq5", "kind": "equality",
                                  "key": {"id": 5}}])
    emit("对新内容 fC#0(id=1) 位置删除 + 等值删除 id=5")

    svc.load_file("orders", "fD", {"kind": "inline", "rows": [
        {"id": 5, "name": "d5-new"}, {"id": 5, "name": "d5-dup"}]})
    svc.apply_deletes("orders", [{"delete_id": "eqnull", "kind": "equality",
                                  "key": {"id": None}}])
    emit("先删后插两个同键 5（晚于 eq5，必须保留）+ NULL 谓词（不命中）")

    out_dir = root / "demo_report"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "walkthrough.txt").write_text("\n".join(log), encoding="utf-8")
    snap = svc.snapshot("orders")
    (out_dir / "snapshot.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    svc.close()
    print("\n".join(log))
    return out_dir
