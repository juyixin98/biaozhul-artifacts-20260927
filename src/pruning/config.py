"""运行配置。"""
from __future__ import annotations

import os


class Config:
    def __init__(self, data_root: str | None = None, db_path: str | None = None):
        self.data_root = data_root or os.environ.get(
            "PRUNING_DATA_ROOT", os.path.join(os.getcwd(), "data"))
        self.db_path = db_path or os.environ.get(
            "PRUNING_DB", os.path.join(self.data_root, "catalog.sqlite"))
