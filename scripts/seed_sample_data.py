#!/usr/bin/env python3
"""向本地 SQLite 注入合成样例数据（确定性、无真实业务数据）。

三批更新，制造可演示的版本链：
 v1: 4 个键（含一个空字节串值，演示“存在但值为空”）
 v2: 更新其中 1 键 + 删除 1 键
 v3: 恢复被删键（删除复原演示）

重复运行会重建 data/sample.db（--reset）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.state_service import StateService  # noqa: E402
from app.coding.params import TreeParams  # noqa: E402
from app.coding.signing import private_key_from_pem  # noqa: E402
from app.core.smt import SparseMerkleTree  # noqa: E402
from app.storage.sqlite_store import SqliteStore  # noqa: E402

DB_PATH = "data/sample.db"
KEY_PATH = "configs/dev_signing_key.pem"


def k(suffix: int) -> bytes:
    # 32 字节定长键，前缀刻意设计成两对“长公共前缀”
    return bytes([0xAB] * 30) + bytes([0x00, suffix])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="删除既有 sample.db 后重建")
    args = ap.parse_args()

    if args.reset and Path(DB_PATH).exists():
        Path(DB_PATH).unlink()

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if not Path(KEY_PATH).exists():
        print("开发密钥不存在，先生成…")
        os.system(f"{sys.executable} scripts/gen_dev_key.py")

    store = SqliteStore(DB_PATH)
    params = TreeParams(32, 256)
    tree = SparseMerkleTree(store, params)
    signer = private_key_from_pem(Path(KEY_PATH).read_bytes())
    svc = StateService(store, tree, signer)

    r1 = svc.apply_updates([
        (k(0x01), b"alice-balance-100"),
        (k(0x02), b"bob-balance-50"),
        (k(0x10), b"charlie-balance-7"),    # 与 0x01/0x02 无长公共前缀
        (k(0x03), b""),                     # 存在但值为空字节串
    ], idem_key="seed-v1")
    print(f"v{r1.version} root={r1.root.hex()[:24]}… changed={r1.changed}")

    r2 = svc.apply_updates([
        (k(0x01), b"alice-balance-120"),    # 改值
        (k(0x02), None),                    # 删除
    ], idem_key="seed-v2")
    print(f"v{r2.version} root={r2.root.hex()[:24]}… changed={r2.changed}")

    r3 = svc.apply_updates([
        (k(0x02), b"bob-balance-50-restored"),  # 删除复原
    ], idem_key="seed-v3")
    print(f"v{r3.version} root={r3.root.hex()[:24]}… changed={r3.changed}")

    print(f"sample DB ready: {DB_PATH} (最新版本 v{r3.version})")
    print(f"公钥（离线核验用）: {KEY_PATH}.pub")
    store.close()


if __name__ == "__main__":
    main()
