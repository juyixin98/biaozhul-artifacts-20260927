"""HTTP 调用示例：启动服务后用 httpx 完整走一遍并演示冲突响应。

先在一个终端启动服务：
    uvicorn lake_txn.api:create_app --factory --port 8000
再运行：python examples/http_client_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=10)

    print("== 建表 ==")
    r = c.post(
        "/v1/tables",
        json={
            "table": "orders",
            "columns": [
                {"name": "order_id", "type": "int64"},
                {"name": "region", "type": "string"},
                {"name": "amount", "type": "float64"},
            ],
            "partition_column": "region",
        },
        headers={"X-Request-ID": "demo-create-table"},
    )
    print(r.status_code, r.json(), "X-Request-ID:", r.headers.get("x-request-id"))

    print("\n== 暂存 cn 文件（服务写 Parquet）==")
    r = c.post(
        "/v1/staging/files",
        json={
            "table": "orders",
            "request_id": "req-cn",
            "files": [
                {
                    "logical_name": "cn-0",
                    "mode": "inline",
                    "records": [
                        {"order_id": 1, "region": "cn", "amount": 10.0},
                        {"order_id": 2, "region": "cn", "amount": 11.0},
                    ],
                }
            ],
        },
    )
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False))

    print("\n== 基于空快照 s0 追加 ==")
    r = c.post(
        "/v1/commits",
        json={"table": "orders", "request_id": "req-cn", "kind": "APPEND",
              "base_snapshot_id": 0, "files": ["cn-0"]},
    )
    print(r.status_code, r.json())

    print("\n== 另一个仍基于 s0 的同分区追加 -> 409 PARTITION_CONFLICT ==")
    c.post("/v1/staging/files", json={
        "table": "orders", "request_id": "req-cn-clash",
        "files": [{"logical_name": "clash-0", "mode": "inline",
                   "records": [{"order_id": 3, "region": "cn", "amount": 12.0}]}]})
    r = c.post(
        "/v1/commits",
        json={"table": "orders", "request_id": "req-cn-clash", "kind": "APPEND",
              "base_snapshot_id": 0, "files": ["clash-0"]},
    )
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))

    print("\n== 提交响应丢失后重放原请求 -> 同一快照，幂等 ==")
    r = c.post(
        "/v1/commits",
        json={"table": "orders", "request_id": "req-cn", "kind": "APPEND",
              "base_snapshot_id": 0, "files": ["cn-0"]},
    )
    print(r.status_code, r.json())

    print("\n== 快照列表与详情 ==")
    print(c.get("/v1/tables/orders/snapshots").status_code)
    print(json.dumps(c.get("/v1/tables/orders/snapshots/1").json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
