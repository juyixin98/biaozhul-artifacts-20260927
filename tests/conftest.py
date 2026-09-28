"""共享夹具：每次测试一个全新的临时存储根 + 已注册 orders 表。"""
from __future__ import annotations

import pytest

from merge3.config import ApiConfig, AppConfig, RunLogConfig, StorageConfig
from merge3.service.merge_service import MergeService

FIELDS = [
    {"name": "id", "type": "int64", "nullable": False},
    {"name": "status", "type": "string"},
    {"name": "amount", "type": "int64"},
    {"name": "owner", "type": "string"},
]

BASE_ROWS = [
    {"id": 1, "status": "new", "amount": 100, "owner": "alice"},
    {"id": 2, "status": "new", "amount": 200, "owner": "bob"},
    {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
    {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
]


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        storage=StorageConfig(root_dir=tmp_path / "data"),
        api=ApiConfig(),
        runlog=RunLogConfig(),
    )


@pytest.fixture
def svc(cfg):
    s = MergeService(cfg.storage)
    s.register_table("orders", ["id"], FIELDS)
    return s


@pytest.fixture
def branched(svc):
    """base 快照 + main/develop 两个分支，返回各 ID。"""
    base = svc.write_snapshot("orders", BASE_ROWS)
    svc.create_branch("orders", "main", base.snapshot_id)
    svc.create_branch("orders", "develop", base.snapshot_id)
    return base


@pytest.fixture
def dev_main_heads(svc, branched):
    """开发与主分支各自前进一次（不相交变化），返回 (dev_snap, main_snap)。"""
    dev_rows = [
        {"id": 1, "status": "paid", "amount": 100, "owner": "alice"},   # dev 改 status
        {"id": 2, "status": "new", "amount": 200, "owner": "bob"},
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
        {"id": 5, "status": "new", "amount": 500, "owner": "erin"},      # dev 新增
    ]
    main_rows = [
        {"id": 1, "status": "new", "amount": 100, "owner": "alice"},
        {"id": 2, "status": "new", "amount": 250, "owner": "bob"},       # main 改 amount
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
        {"id": 6, "status": "new", "amount": 600, "owner": "frank"},     # main 新增
    ]
    dev = svc.commit_rows("orders", "develop", dev_rows)
    main = svc.commit_rows("orders", "main", main_rows)
    return dev, main
