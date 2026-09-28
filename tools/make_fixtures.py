"""合成夹具生成器：生成本地 Parquet 数据集，覆盖所有边界场景。

数据集 ``events``（按 ``event_ts`` 月分区，UTC epoch 秒）：
* 跨 1969-12（负时间戳）、2024-01..03；
* 含 NULL 时间戳、NULL 字符串、长字符串（用于截断统计）；
* 普通整数字段 amount、字符串字段 region。

用法::
    python -m tools.make_fixtures --root data
"""
from __future__ import annotations

import argparse
import os

import pyarrow as pa

from pruning.adapter import write_parquet_file
from pruning import transforms as T

SCHEMA = {
    "event_ts": pa.timestamp("us", tz="UTC"),
    "amount": pa.int64(),
    "region": pa.string(),
}


def ts(date_str: str, hh=0, mm=0, ss=0) -> int:
    lo, _ = T.date_range_epoch_bounds(date_str, date_str)
    return lo + hh * 3600 + mm * 60 + ss


def build(root: str) -> dict:
    table = "events"
    created = []

    def emit(month: str, part: str, cols: dict):
        path = os.path.join(root, table, f"event_ts={month}", f"{part}.parquet")
        write_parquet_file(path, cols, SCHEMA)
        created.append(path)
        return path

    # 1969-12（负时间戳）：用于验证负 epoch + 月桶边界
    emit("1969-12", "part-1969-12-a", {
        "event_ts": [ts("1969-12-15"), ts("1969-12-31", 23), None],
        "amount": [10, 20, 30],
        "region": ["us-east", "eu-west", None],
    })

    # 2024-01：两个文件，区间不同，验证 stats 层区分
    emit("2024-01", "part-2024-01-early", {
        "event_ts": [ts("2024-01-01"), ts("2024-01-05"), ts("2024-01-10")],
        "amount": [100, 110, 120],
        "region": ["us-east", "us-west", "us-east"],
    })
    emit("2024-01", "part-2024-01-late", {
        "event_ts": [ts("2024-01-20"), ts("2024-01-28"), None],
        "amount": [200, 210, 220],
        "region": [None, "eu-west", "ap-south"],
    })

    # 2024-02：边界月份的核心，含月首月末
    emit("2024-02", "part-2024-02-a", {
        "event_ts": [ts("2024-02-01"), ts("2024-02-14"), ts("2024-02-29", 23, 59, 59)],
        "amount": [300, 310, 320],
        "region": ["ap-south", "us-east", "longregion-eu-west-0000"],
    })
    emit("2024-02", "part-2024-02-b", {
        "event_ts": [ts("2024-02-10"), None, ts("2024-02-25")],
        "amount": [400, 410, 420],
        "region": ["eu-west", None, "longregion-ap-east-9999"],
    })

    # 2024-03：用于上界外侧
    emit("2024-03", "part-2024-03-a", {
        "event_ts": [ts("2024-03-05"), ts("2024-03-15")],
        "amount": [500, 510],
        "region": ["zz-final-region-x", "us-east"],
    })

    return {"root": root, "table": table, "files": created}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data")
    args = ap.parse_args()
    info = build(args.root)
    print(f"created {len(info['files'])} files under {args.root}/{info['table']}")
    for f in info["files"]:
        print(" -", f)
