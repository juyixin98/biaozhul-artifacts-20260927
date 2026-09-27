#!/usr/bin/env python3
"""把 sample_data/sample.json 播种到一个全新的 SQLite 库。

用法：
    python scripts/seed.py                 # 用默认配置路径
    POSTING_DB_PATH=data/demo.db python scripts/seed.py
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.storage.version_store import VersionStore  # noqa: E402


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    sample_path = ROOT / "sample_data" / "sample.json"
    data = json.loads(sample_path.read_text(encoding="utf-8"))

    db = pathlib.Path(settings.db_path)
    if db.exists():
        print(f"[seed] 目标库已存在，跳过覆盖：{db}（删除后可重新播种）")
        return 0

    store = VersionStore(str(db), block_size=settings.block_size)
    for v in data["versions"]:
        adds = {int(k): text for k, text in v.get("adds", {}).items()}
        version = store.commit(adds=adds, deletes=v.get("deletes", []), note=v.get("note"))
        print(
            f"[seed] 已提交版本 v{version}：+{len(adds)} 篇，"
            f"-{len(v.get('deletes', []))} 篇，全集大小={len(store.universe(version))}"
        )
    print(f"[seed] 完成：{db}（最新版本 v{store.latest_version()}）")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
