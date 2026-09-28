"""生成最小本地合成夹具（Parquet 文件），全部为虚构数据。

用法：
    python -m fixtures.make_fixtures            # 生成到 fixtures/data
    python -m fixtures.make_fixtures --inbound examples/inbound  # 同时生成 import 白名单文件
"""

from __future__ import annotations

import argparse
from pathlib import Path

from lake_txn.format_adapter import ColumnSpec, write_parquet_atomic

COLUMNS = [
    ColumnSpec("order_id", "int64"),
    ColumnSpec("region", "string"),
    ColumnSpec("amount", "float64"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="fixtures/data")
    parser.add_argument("--inbound", default="examples/inbound")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    for region, start in [("cn", 1), ("us", 100), ("eu", 200)]:
        rows = [
            {"order_id": start + i, "region": region, "amount": 10.0 + i}
            for i in range(3)
        ]
        wf = write_parquet_atomic(rows, COLUMNS, out, f"orders_{region}.parquet")
        print(f"wrote {wf.path.relative_to(Path.cwd())} rows={wf.row_count} sha256={wf.sha256[:12]}")

    inbound = Path(args.inbound).resolve()
    inbound.mkdir(parents=True, exist_ok=True)
    wf = write_parquet_atomic(
        [{"order_id": 900, "region": "jp", "amount": 42.0}],
        COLUMNS, inbound, "orders_jp_external.parquet",
    )
    print(f"wrote {wf.path.relative_to(Path.cwd())} rows={wf.row_count} (import 白名单夹具)")


if __name__ == "__main__":
    main()
