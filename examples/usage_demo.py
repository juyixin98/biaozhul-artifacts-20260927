"""服务调用示例（Python 直接编排层，无需起 HTTP 服务）。

演示：建表 -> 暂存 -> 追加 -> 互不相交分区并发追加(自动合并) ->
同分区覆盖 -> 陈旧基线覆盖被拒 -> 提交响应丢失后的幂等重试 -> 孤立文件清扫。
所有数据均为本地合成夹具。运行：python examples/usage_demo.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lake_txn import errors
from lake_txn.config import local_default
from lake_txn.service import LakeService, StageFileInput

COLUMNS = [
    {"name": "order_id", "type": "int64"},
    {"name": "region", "type": "string"},
    {"name": "amount", "type": "float64"},
]


def stage_inline(svc, rid: str, region: str, ids: list[int]):
    svc.stage_files(
        "orders", rid,
        [StageFileInput(f"{rid}-0", "inline",
                        [{"order_id": i, "region": region, "amount": float(i)} for i in ids])],
    )


def show(title, svc):
    head = svc.catalog.head_snapshot_id("orders")
    rows = svc.read_snapshot_rows("orders", head)
    by_region: dict[str, list[int]] = {}
    for r in rows:
        by_region.setdefault(r["region"], []).append(r["order_id"])
    print(f"\n=== {title}（head=s{head}）===")
    for region in sorted(by_region):
        print(f"  {region}: {by_region[region]}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="lake-demo-"))
    print(f"本地仓库: {workdir}")
    svc = LakeService(local_default(workdir))
    svc.create_table("orders", COLUMNS, "region")

    # 1) 首次追加
    stage_inline(svc, "r-cn", "cn", [1, 2])
    print("append cn ->", svc.commit("orders", "r-cn", "APPEND", 0, ["r-cn-0"]))
    show("初始追加 cn", svc)

    # 2) 两个线程基线都停在 s0，分别追加不相交分区 us/eu -> 自动合并
    barrier = threading.Barrier(2)
    results = {}

    def concurrent_append(tag, region, ids):
        rid = f"r-{region}"
        stage_inline(svc, rid, region, ids)
        barrier.wait()
        try:
            results[region] = svc.commit("orders", rid, "APPEND", 0, [f"{rid}-0"])
        except errors.DomainError as exc:
            results[region] = f"REJECTED {exc.reason_code}"

    threads = [
        threading.Thread(target=concurrent_append, args=("us", "us", [100])),
        threading.Thread(target=concurrent_append, args=("eu", "eu", [200])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n并发互不相交追加:")
    for region, res in results.items():
        if isinstance(res, str):
            print(f"  {region}: {res}")
        else:
            print(f"  {region}: snapshot=s{res['snapshot_id']} merged={res['merged']} "
                  f"parents={res['parent_snapshot_id']}")
    show("自动合并 us+eu", svc)

    # 3) 同分区覆盖：用新行替换 cn（us/eu 保留）
    stage_inline(svc, "r-ow-cn", "cn", [900, 901])
    head = svc.catalog.head_snapshot_id("orders")
    print("\noverwrite cn ->", svc.commit(
        "orders", "r-ow-cn", "OVERWRITE", head, ["r-ow-cn-0"], drop_partitions=["cn"]
    ))
    show("覆盖 cn 后 us/eu 保留", svc)

    # 4) 陈旧基线覆盖被拒（不是最后写获胜）
    stage_inline(svc, "r-stale", "us", [999])
    try:
        svc.commit("orders", "r-stale", "OVERWRITE", 1, ["r-stale-0"], drop_partitions=["us"])
    except errors.DomainError as exc:
        print(f"\n陈旧基线覆盖被拒: {exc.status_code} {exc.reason_code} -- {exc.message}")
        print("  关键状态:", exc.detail)

    # 5) 提交响应丢失：相同 request_id 重放，返回同一快照，不产生重复
    first = svc.get_commit_status("r-cn")
    replay = svc.commit("orders", "r-cn", "APPEND", 0, ["r-cn-0"])
    print(f"\n响应丢失重试: 原快照={first['snapshot_id']} 重放快照={replay['snapshot_id']} "
          f"幂等={replay['idempotent_replay']}")

    # 6) 孤立文件清扫
    ghost = svc.settings.staging_dir / "ghost-request"
    ghost.mkdir(parents=True)
    (ghost / "half-written.parquet").write_bytes(b"orphan")
    report = svc.sweep(grace_seconds=0)
    print(f"\n清扫孤立文件: {report['quarantined_count']} 个进入隔离区")
    for rec in report["records"]:
        print(" ", rec)

    shutil.rmtree(workdir, ignore_errors=True)
    print("\n演示完成。")


if __name__ == "__main__":
    main()
